import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import * as vue from 'vue';
import { compileScript, parse } from '@vue/compiler-sfc';
import ts from 'typescript';

function loadModule(path, dependencies = {}) {
  const source = readFileSync(new URL(path, import.meta.url), 'utf8');
  const content = path.endsWith('.vue') ? compileScript(parse(source).descriptor, { id: 'library-test' }).content : source;
  const code = ts.transpileModule(content, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
  const module = { exports: {} };
  new Function('require', 'module', 'exports', code)(name => {
    if (name in dependencies) return dependencies[name];
    if (name === '../utils/authTokens.ts') return { authSessionKey: () => null };
    if (name === 'vue') return vue;
    if (name === 'reka-ui' || name === 'lucide-vue-next') return {};
    throw new Error(`Unexpected dependency ${name}`);
  }, module, module.exports);
  return module.exports;
}

const file = id => ({ file_id: id, filename: `${id}.csv`, upload_date: '2026-10-20T00:00:00Z', size: 10 });
const asset = (id, ids = [id]) => ({ id, name: id, description: '', tags: [], source_type: 'upload', files: ids.map(file), status: 'ready', source_url: null, session_id: null });
const flush = async () => { await Promise.resolve(); await vue.nextTick(); };

test('library handoff is consumed once, deduplicates files, and can be cleared on account change', () => {
  const selection = loadModule('../src/composables/useDataSelection.ts');
  const original = file('one');
  selection.setPendingLibraryFiles([original, original, file('two')]);
  original.filename = 'changed.csv';
  const files = selection.takePendingLibraryFiles();
  assert.deepEqual(files.map(item => item.file_id), ['one', 'two']);
  assert.equal(files[0].filename, 'one.csv');
  assert.deepEqual(selection.takePendingLibraryFiles(), []);
  selection.setPendingLibraryFiles([file('private')]);
  selection.clearPendingLibraryFiles();
  assert.deepEqual(selection.takePendingLibraryFiles(), []);
});

function listHarness() {
  const requests = [];
  const scope = vue.effectScope();
  const { useDataLibraryList } = loadModule('../src/composables/useDataLibraryList.ts', {
    '../api/dataLibrary': { listDataLibrary: (...args) => new Promise((resolve, reject) => requests.push({ args, resolve, reject })) },
  });
  const state = scope.run(useDataLibraryList);
  return { state, requests, close: () => scope.stop() };
}

test('library search cancels stale requests and never mixes results from different searches', async () => {
  const h = listHarness();
  try {
    const first = h.state.load();
    h.state.query.value = 'climate';
    await flush();
    assert.equal(h.requests[0].args[1].aborted, true);
    const second = h.state.load();
    assert.equal(h.requests[1].args[0].q, 'climate');
    h.requests[1].resolve({ items: [asset('new')], next_cursor: null });
    await second;
    h.requests[0].resolve({ items: [asset('stale')], next_cursor: 'wrong' });
    await first;
    assert.deepEqual(h.state.items.value.map(item => item.id), ['new']);
    assert.equal(h.state.nextCursor.value, null);
    assert.equal(h.state.loading.value, false);
  } finally { h.close(); }
});

test('pagination preserves current items on failure and can retry the same cursor', async () => {
  const h = listHarness();
  try {
    const first = h.state.load();
    h.requests[0].resolve({ items: [asset('one')], next_cursor: 'next' });
    await first;
    const more = h.state.load(true);
    assert.equal(h.requests[1].args[0].cursor, 'next');
    h.requests[1].reject(new Error('/private/host/path'));
    await more;
    assert.deepEqual(h.state.items.value.map(item => item.id), ['one']);
    assert.doesNotMatch(h.state.error.value, /private/);
    assert.equal(h.state.nextCursor.value, 'next');
    const retry = h.state.load(true);
    h.requests[2].resolve({ items: [asset('one'), asset('two')], next_cursor: null });
    await retry;
    assert.deepEqual(h.state.items.value.map(item => item.id), ['one', 'two']);
  } finally { h.close(); }
});

test('unmount clears loaded data and rejects a late library response', async () => {
  const h = listHarness();
  const pending = h.state.load();
  h.close();
  assert.equal(h.requests[0].args[1].aborted, true);
  h.requests[0].resolve({ items: [asset('private')], next_cursor: null });
  await pending;
  assert.deepEqual(h.state.items.value, []);
});

function saveHarness(overrides = {}) {
  const requests = [], events = [], cleanup = [];
  const owner = vue.ref({ id: 'owner-a' });
  const scope = vue.effectScope();
  const component = loadModule('../src/components/SaveToLibraryDialog.vue', {
    vue: { ...vue, onUnmounted: callback => cleanup.push(callback) },
    '../api/dataLibrary': { dataLibrarySourceLabels: {}, saveDataLibraryAsset: input => new Promise((resolve, reject) => requests.push({ input, resolve, reject })) },
    '../utils/fileType': { formatFileSize: value => String(value) },
    '../utils/toast': { showSuccessToast: () => {} },
    '../composables/useAuth': { useAuth: () => ({ currentUser: owner }) },
    './LibraryFolderSelect.vue': {},
  }).default;
  const props = vue.reactive({ visible: true, files: [file('one'), file('two')], sourceType: 'upload', ...overrides });
  const state = scope.run(() => component.setup(props, { expose() {}, emit: (...args) => events.push(args) }));
  // The child directory selector has finished its owner-scoped initial load.
  state.folderReady.value = true;
  return { props, state, requests, events, owner, close() { cleanup.forEach(callback => callback()); scope.stop(); } };
}

test('saving uses only selected real files and preserves the draft when saving fails', async () => {
  const h = saveHarness({ files: [file('one'), file('one'), file('temp-upload'), file('two')] });
  try {
    h.state.name.value = '  Experiment  ';
    h.state.toggleFile('two');
    const saving = h.state.save();
    assert.deepEqual(h.requests[0].input.file_ids, ['one']);
    assert.equal(h.requests[0].input.name, 'Experiment');
    h.requests[0].reject(new Error('/private/source/data.csv'));
    await saving;
    assert.equal(h.events.length, 0);
    assert.equal(h.state.name.value, '  Experiment  ');
    assert.deepEqual([...h.state.selectedIds.value], ['one']);
    assert.match(h.state.error.value, /未确认成功/);
    assert.doesNotMatch(h.state.error.value, /private/);
  } finally { h.close(); }
});

test('saving forty files preserves every selected file in one save request', async () => {
  const files = Array.from({ length: 40 }, (_, index) => file(`data-${index}`));
  const h = saveHarness({ files: [...files, files[0], file('temp-upload')] });
  try {
    assert.equal(h.state.selectedIds.value.size, 40);
    assert.equal(h.state.error.value, '');
    h.state.toggleFile('data-39');
    assert.equal(h.state.selectedIds.value.size, 39);
    h.state.toggleFile('data-39');
    assert.equal(h.state.selectedIds.value.size, 40);
    const saving = h.state.save();
    assert.equal(h.requests.length, 1);
    assert.deepEqual(h.requests[0].input.file_ids, files.map(item => item.file_id));
    h.requests[0].resolve(asset('all-forty', files.map(item => item.file_id)));
    await saving;
    assert.equal(h.events[0][0], 'saved');
    assert.equal(h.events[0][1].files.length, 40);
  } finally { h.close(); }
});

test('saving preserves selected logical directories and never sends private or invalid paths', async () => {
  const h = saveHarness({ files: [
    { ...file('one'), relative_path: '实验一/原始数据/one.csv' },
    { ...file('two'), relative_path: '/private/two.csv' },
    { ...file('three'), relative_path: '../three.csv' },
    { ...file('four'), relative_path: 'data/renamed.csv' },
    { ...file('five'), relative_path: 'data/five.csv' },
  ] });
  try {
    h.state.toggleFile('five');
    const saving = h.state.save();
    assert.deepEqual(h.requests[0].input.file_paths, { one: '实验一/原始数据/one.csv' });
    assert.deepEqual(h.requests[0].input.file_ids, ['one', 'two', 'three', 'four']);
    h.requests[0].resolve(asset('saved'));
    await saving;
  } finally { h.close(); }
});

test('flat files with repeated filenames do not create duplicate directory mappings', async () => {
  const h = saveHarness({ files: [
    { ...file('one'), filename: 'table.csv', relative_path: 'table.csv' },
    { ...file('two'), filename: 'table.csv', relative_path: 'table.csv' },
  ] });
  try {
    const saving = h.state.save();
    assert.equal('file_paths' in h.requests[0].input, false);
    h.requests[0].resolve(asset('saved'));
    await saving;
  } finally { h.close(); }
});

test('a confirmed save refreshes the library without putting private data in the event', async () => {
  const originalWindow = globalThis.window;
  const events = [];
  globalThis.window = { dispatchEvent: event => events.push(event) };
  const h = saveHarness();
  try {
    const saving = h.state.save();
    assert.equal(events.length, 0);
    h.requests[0].resolve(asset('saved'));
    await saving;
    assert.equal(events.length, 1);
    assert.equal(events[0].type, 'data-library-changed');
    assert.equal(events[0].detail, null);
  } finally {
    h.close();
    if (originalWindow === undefined) delete globalThis.window;
    else globalThis.window = originalWindow;
  }
});

test('a save from an old session cannot close or populate a new dialog', async () => {
  const h = saveHarness({ sessionId: 'old', sourceType: 'analysis' });
  try {
    const saving = h.state.save();
    assert.equal(h.requests[0].input.session_id, 'old');
    h.props.sessionId = 'new';
    await flush();
    h.state.name.value = 'New draft';
    h.requests[0].resolve(asset('old'));
    await saving;
    assert.deepEqual(h.events, []);
    assert.equal(h.state.name.value, 'New draft');
  } finally { h.close(); }
});

test('only a confirmed save emits an asset and closes the dialog', async () => {
  const h = saveHarness();
  try {
    const saving = h.state.save();
    const saved = asset('persisted');
    h.requests[0].resolve(saved);
    await saving;
    assert.deepEqual(h.events, [['saved', saved], ['close']]);
  } finally { h.close(); }
});

test('unspecified source type lets the server infer file provenance from receipts', async () => {
  const h = saveHarness({ sourceType: undefined });
  try {
    const saving = h.state.save();
    assert.equal('source_type' in h.requests[0].input, false);
    h.requests[0].resolve(asset('inferred'));
    await saving;
  } finally { h.close(); }
});

test('saving chooses an explicit target and never prefixes an existing target path twice', async () => {
  const h = saveHarness({ targetFolderId: 'study-folder', files: [
    { ...file('one'), relative_path: '实验一/one.csv' },
    { ...file('two'), relative_path: '实验一/原始数据/two.csv' },
  ] });
  try {
    assert.equal(h.state.selectedFolderId.value, 'study-folder');
    h.state.selectedFolderPath.value = '实验一';
    const saving = h.state.save();
    assert.equal(h.requests[0].input.target_folder_id, 'study-folder');
    assert.deepEqual(h.requests[0].input.file_paths, { two: '原始数据/two.csv' });
    h.requests[0].resolve(asset('saved'));
    await saving;
  } finally { h.close(); }
});

test('root uploads explicitly use root and file conflicts preserve the chosen folder and draft', async () => {
  const h = saveHarness();
  try {
    let saving = h.state.save();
    assert.equal(h.requests[0].input.target_folder_id, null);
    h.requests[0].reject({ response: { status: 409, data: { msg: '/private/secret' } } });
    await saving;
    assert.match(h.state.error.value, /同名文件/);
    assert.doesNotMatch(h.state.error.value, /private/);
    h.state.selectedFolderId.value = 'other-folder';
    h.state.selectedFolderPath.value = '其他';
    saving = h.state.save();
    assert.equal(h.requests[1].input.target_folder_id, 'other-folder');
    assert.equal(h.state.selectedIds.value.size, 2);
    h.requests[1].resolve(asset('saved'));
    await saving;
  } finally { h.close(); }
});

test('saving waits for a confirmed folder choice and folder creation cannot race a save', async () => {
  const h = saveHarness({ targetFolderId: 'missing-folder' });
  try {
    h.state.folderReady.value = false;
    await h.state.save();
    h.state.folderReady.value = true;
    h.state.folderCreating.value = true;
    await h.state.save();
    assert.equal(h.requests.length, 0);
    h.state.folderCreating.value = false;
    const saving = h.state.save();
    await h.state.save();
    assert.equal(h.requests.length, 1);
    h.requests[0].resolve(asset('saved'));
    await saving;
  } finally { h.close(); }
});

test('account changes clear the saving draft and an old save cannot notify the new account', async () => {
  const h = saveHarness({ targetFolderId: 'private-folder' });
  try {
    const saving = h.state.save();
    h.owner.value = { id: 'owner-b' };
    assert.equal(h.state.name.value, '');
    assert.equal(h.state.selectedIds.value.size, 0);
    assert.equal(h.state.selectedFolderId.value, null);
    h.requests[0].resolve(asset('private-result'));
    await saving;
    assert.deepEqual(h.events, [['close']]);
  } finally { h.close(); }
});

test('context writes are serialized per session and a failed update does not block the next', async () => {
  const requests = [];
  const api = loadModule('../src/api/dataLibrary.ts', {
    './client': { apiClient: { put: (...args) => new Promise((resolve, reject) => requests.push({ args, resolve, reject })) } },
  });
  const old = api.setSessionDataContext('same', ['old']);
  const ids = ['new'];
  const latest = api.setSessionDataContext('same', ids);
  ids.push('mutated');
  const independent = api.setSessionDataContext('other', ['separate']);
  await flush();
  assert.equal(requests.length, 2);
  assert.deepEqual(requests[0].args[1], { file_ids: ['old'] });
  assert.match(requests[1].args[0], /other/);
  const rejection = assert.rejects(old, /failed/);
  requests[0].reject(new Error('failed'));
  await rejection;
  await flush();
  assert.equal(requests.length, 3);
  assert.deepEqual(requests[2].args[1], { file_ids: ['new'] });
  requests[2].resolve({ data: { data: { files: [file('new')] } } });
  requests[1].resolve({ data: { data: { files: [file('separate')] } } });
  assert.deepEqual((await latest).map(item => item.file_id), ['new']);
  await independent;
});

test('missing persisted inputs cannot silently become a smaller active selection', async () => {
  const api = loadModule('../src/api/dataLibrary.ts', {
    './client': { apiClient: { get: async () => ({ data: { data: { files: [file('available')], missing_file_ids: ['missing'] } } }) } },
  });
  await assert.rejects(api.getSessionDataContext('saved-exploration'), /部分所选文件不可用/);
});

test('reopening a conversation waits for its most recent pending selection write', async () => {
  let resolveWrite;
  let reads = 0;
  const api = loadModule('../src/api/dataLibrary.ts', {
    './client': { apiClient: {
      put: () => new Promise(resolve => { resolveWrite = resolve; }),
      get: async () => { reads++; return { data: { data: { files: [file('latest')], missing_file_ids: [] } } }; },
    } },
  });
  const write = api.setSessionDataContext('same', ['latest']);
  const reading = api.getSessionDataContext('same');
  await flush();
  assert.equal(reads, 0);
  resolveWrite({ data: { data: { files: [file('latest')] } } });
  await write;
  assert.deepEqual((await reading).map(item => item.file_id), ['latest']);
  assert.equal(reads, 1);
});

test('picker excludes active files, deduplicates overlaps and enforces the exploration limit', () => {
  const scope = vue.effectScope();
  const events = [];
  const component = loadModule('../src/components/DataLibraryPicker.vue', {
    '../api/dataLibrary': { dataLibrarySourceLabels: {} },
    '../composables/useDataLibraryList': { useDataLibraryList: () => ({ items: vue.ref([]), query: vue.ref(''), sourceType: vue.ref(''), nextCursor: vue.ref(null), loading: vue.ref(false), error: vue.ref(''), load() {}, cancel() {} }) },
  }).default;
  try {
    const state = scope.run(() => component.setup({ visible: false, excludeFileIds: ['active'] }, { expose() {}, emit: (...args) => events.push(args) }));
    state.toggleAsset(asset('first', ['active', 'shared', 'first']));
    state.toggleAsset(asset('second', ['shared', 'second']));
    assert.deepEqual(state.selectedFiles.value.map(item => item.file_id), ['shared', 'first', 'second']);
    state.toggleAsset(asset('too-many', Array.from({ length: 32 }, (_, i) => `large-${i}`)));
    assert.equal(state.selected.value.size, 2);
    assert.match(state.selectionError.value, /32/);
    assert.deepEqual(state.availableFiles({ ...asset('missing'), status: 'missing' }), []);
    state.confirmSelection();
    assert.equal(events[0][0], 'select');
    assert.equal(events[0][1].length, 3);
    assert.equal(events[1][0], 'close');
  } finally { scope.stop(); }
});

test('queued data-context writes and reads cannot continue under a replacement account', async () => {
  let owner = 'alice', resolveWrite, calls = 0
  const api = loadModule('../src/api/dataLibrary.ts', {
    '../utils/authTokens.ts': { authSessionKey: () => owner },
    './client': { apiClient: {
      put: () => { calls++; return new Promise(resolve => { resolveWrite = resolve }) },
      get: () => { calls++; assert.fail('old account read must not be sent as new user') },
    } },
  })
  const first = api.setSessionDataContext('alice-session', ['alice-one'])
  const queued = api.setSessionDataContext('alice-session', ['alice-two'])
  const queuedRejected = assert.rejects(queued, { name: 'AbortError' })
  const reading = api.getSessionDataContext('alice-session')
  const readRejected = assert.rejects(reading, { name: 'AbortError' })
  await flush(); owner = 'bob'
  resolveWrite({ data: { data: { files: [file('alice-one')] } } })
  await Promise.all([first, queuedRejected, readRejected])
  assert.equal(calls, 1)
})
