import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import * as vue from 'vue';
import { compileScript, parse } from '@vue/compiler-sfc';
import ts from 'typescript';

function load(path, dependencies = {}) {
  const source = readFileSync(new URL(path, import.meta.url), 'utf8');
  const content = path.endsWith('.vue') ? compileScript(parse(source).descriptor, { id: 'library-delete-test' }).content : source;
  const code = ts.transpileModule(content, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
  const module = { exports: {} };
  new Function('require', 'module', 'exports', code)(name => {
    if (name in dependencies) return dependencies[name];
    if (name === 'vue') return vue;
    if (name === 'reka-ui' || name === 'lucide-vue-next' || name.endsWith('.vue')) return {};
    throw new Error(`Unexpected dependency ${name}`);
  }, module, module.exports);
  return module.exports;
}

const tree = load('../src/utils/personalDataTree.ts');
const manager = load('../src/utils/libraryFileManager.ts', { './personalDataTree': tree });
const columns = load('../src/utils/libraryColumns.ts', { './personalDataTree': tree });
const filters = load('../src/utils/libraryFileFilters.ts', { './personalDataTree': tree });
const stamp = '2026-10-22T01:00:00Z';
const deletionToken = 'a'.repeat(64);
const file = (id, path) => ({ file_id: id, filename: `${id.replace(/^gridfs:/, '')}.csv`, relative_path: path, size: 10, upload_date: stamp });
const asset = (id, files, status = 'ready') => ({ id, name: id, files, status, updated_at: stamp, description: '', tags: [], source_type: 'upload' });
const success = (request, status = 'deleted') => ({ results: request.files.map(item => ({ ...item, status })), deleted_count: request.files.length, failed_count: 0 });

function harness(records, folderEntries = [{ id: 'raw', path: 'raw', name: 'raw', parent_id: null }]) {
  const requests = [], successToasts = [], analysis = [], routes = [], cleanup = [];
  const previewRequests = [], folderRequests = [];
  const folders = vue.ref(folderEntries);
  const owner = vue.ref({ id: 'owner-a' });
  const items = vue.ref(records), visibleItems = vue.ref(records);
  const scope = vue.effectScope();
  const browser = {
    items, visibleItems, stats: vue.ref(null), query: vue.ref(''), fileFormat: vue.ref(''),
    fromDate: vue.ref(''), toDate: vue.ref(''), sourceType: vue.ref(''), nextCursor: vue.ref(null),
    loading: vue.ref(false), error: vue.ref(''), refreshes: 0,
    load() { this.refreshes++; }, resetFilters() {},
  };
  // load is destructured by the page, so count through the closure.
  browser.load = () => { browser.refreshes++; };
  const originalWindow = globalThis.window;
  globalThis.window = new EventTarget();
  const component = load('../src/pages/MyDataPage.vue', {
    vue: { ...vue, onMounted: callback => callback(), onUnmounted: callback => cleanup.push(callback) },
    'vue-router': { useRouter: () => ({ push: route => routes.push(route) }) },
    '../api/dataLibrary': {
      dataLibrarySourceLabels: {},
      deleteDataLibraryFiles: (files, signal) => new Promise((resolve, reject) => requests.push({ files, signal, resolve, reject })),
      previewDataLibraryFolderDeletion: (id, signal) => new Promise((resolve, reject) => previewRequests.push({ id, signal, resolve, reject })),
      deleteDataLibraryFolder: (id, token, signal) => new Promise((resolve, reject) => folderRequests.push({ id, token, signal, resolve, reject })),
    },
    '../composables/useAuth': { useAuth: () => ({ currentUser: owner }) },
    '../composables/useLibraryBrowser': { useLibraryBrowser: () => browser },
    '../composables/useLibraryFolders': { useLibraryFolders: () => ({ folders, loading: vue.ref(false), error: vue.ref(''), load() {}, createFolder() {} }) },
    '../composables/useDataSelection': { setPendingLibraryFiles: files => analysis.push(files) },
    '../composables/useFilePanel': { useFilePanel: () => ({ showFilePanel() {} }) },
    '../composables/useLeftPanel': { useLeftPanel: () => ({ isLeftPanelShow: vue.ref(true), showLeftPanel() {} }) },
    '../utils/fileType': { formatFileSize: value => String(value) },
    '../utils/libraryColumns': columns,
    '../utils/libraryFileManager': manager,
    '../utils/libraryFileFilters': filters,
    '../utils/personalDataTree': tree,
    '../utils/toast': { showSuccessToast: message => successToasts.push(message), showErrorToast() {} },
  }).default;
  const state = scope.run(() => component.setup({}, { expose() {} }));
  return { state, owner, browser, folders, previewRequests, folderRequests, requests, successToasts, analysis, routes, close() { cleanup.forEach(callback => callback()); scope.stop(); globalThis.window = originalWindow; } };
}

test('file selection separates references to the same object and includes unavailable files', () => {
  const h = harness([asset('a', [file('shared')]), asset('b', [file('gridfs:shared')]), asset('missing', [file('gone')], 'missing')]);
  try {
    const [first, second] = h.state.currentFiles.value.filter(item => item.file.file_id === 'shared');
    h.state.toggleFile(first);
    assert.deepEqual(h.state.selectedIds.value, [first.id]);
    h.state.toggleFile(second);
    assert.equal(h.state.selected.value.size, 2);
    assert.equal(h.state.selectedFiles.value.length, 1, 'analysis deduplicates the shared object');
    h.state.selectCurrentFiles();
    assert.equal(h.state.selected.value.size, 3);
    assert.equal(h.state.selectedUnavailable.value, 1);
    h.state.startSelectedAnalysis();
    assert.equal(h.analysis.length, 0);
    h.state.requestDelete([...h.state.selected.value.values()]);
    assert.equal(h.state.deleteTargets.value.length, 3);
    h.state.closeDelete();
    assert.equal(h.requests.length, 0, 'cancel sends no deletion');
    assert.equal(h.state.selected.value.size, 3);
  } finally { h.close(); }
});

test('select all is scoped to loaded current-folder files without the analysis limit', () => {
  const h = harness([asset('a', [...Array.from({ length: 40 }, (_, index) => file(`f${index}`)), file('nested', 'raw/nested.csv')])]);
  try {
    h.state.selectCurrentFiles();
    assert.equal(h.state.selected.value.size, 40);
    h.state.startSelectedAnalysis();
    assert.equal(h.analysis.length, 0);
    assert.match(h.state.selectionError.value, /32/);
    h.state.navigateFolder('raw');
    h.state.selectCurrentFiles();
    assert.equal(h.state.selected.value.size, 41);
    h.state.toggleCurrentSelection();
    assert.equal(h.state.selected.value.size, 40, 'unselect all preserves other folders');
    h.state.clearSelection();
    assert.equal(h.state.selected.value.size, 0);
  } finally { h.close(); }
});

test('partial deletion removes only confirmed references, refreshes, and retries failures', async () => {
  const h = harness([asset('a', [file('shared'), file('keep'), file('retry')]), asset('b', [file('shared')]), asset('gone', [file('absent')], 'missing')]);
  try {
    const targets = h.state.currentFiles.value.filter(item => item.assetId !== 'b' && item.file.file_id !== 'keep');
    targets.forEach(h.state.toggleFile);
    h.state.inspected.value = targets.find(item => item.file.file_id === 'shared');
    h.state.requestDelete(targets);
    const deleting = h.state.confirmDelete();
    await h.state.confirmDelete();
    h.state.clearSelection();
    h.state.closeDelete();
    assert.equal(h.requests.length, 1, 'cannot submit twice');
    assert.equal(h.state.selected.value.size, 3, 'selection stays locked during deletion');
    h.requests[0].resolve({ results: [
      { asset_id: 'a', file_id: 'gridfs:shared', status: 'deleted' },
      { asset_id: 'a', file_id: 'retry', status: 'failed', message: '/private/host/path' },
      { asset_id: 'gone', file_id: 'absent', status: 'not_found' },
    ], deleted_count: 1, failed_count: 2 });
    await deleting;
    assert.deepEqual(h.state.items.value.map(item => [item.id, item.files.map(file => file.file_id)]), [['a', ['keep', 'retry']], ['b', ['shared']]]);
    assert.deepEqual(h.state.deleteTargets.value.map(item => item.file.file_id), ['retry']);
    assert.deepEqual([...h.state.selected.value.values()].map(item => item.file.file_id), ['retry']);
    assert.equal(h.state.inspected.value, null);
    assert.match(h.state.deleteError.value, /还有 1 个文件/);
    assert.doesNotMatch(h.state.deleteError.value, /private/);
    assert.match(h.successToasts[0], /1 个文件已不存在/);
    assert.equal(h.browser.refreshes, 1);
    const retry = h.state.confirmDelete();
    assert.deepEqual(h.requests[1].files, [{ asset_id: 'a', file_id: 'retry' }]);
    h.requests[1].resolve(success(h.requests[1]));
    await retry;
    assert.equal(h.state.deleteTargets.value.length, 0);
    assert.equal(h.state.selected.value.size, 0);
    assert.equal(h.browser.refreshes, 2);
  } finally { h.close(); }
});

test('large deletions use bounded batches and preserve successful batches after a network failure', async () => {
  const h = harness([asset('bulk', Array.from({ length: 1001 }, (_, index) => file(`f${index}`)))]);
  try {
    h.state.selectCurrentFiles();
    h.state.requestDelete([...h.state.selected.value.values()]);
    const deleting = h.state.confirmDelete();
    assert.equal(h.requests[0].files.length, 500);
    h.requests[0].resolve(success(h.requests[0]));
    await Promise.resolve();
    assert.equal(h.requests[1].files.length, 500);
    h.requests[1].reject(new Error('connection lost /private/source'));
    await deleting;
    assert.equal(h.state.selected.value.size, 501);
    assert.equal(h.state.deleteTargets.value.length, 501);
    assert.equal(h.state.items.value[0].files.length, 501);
    assert.equal(h.requests.length, 2, 'remaining files wait for an explicit retry');
    const retry = h.state.confirmDelete();
    h.requests[2].resolve(success(h.requests[2]));
    await Promise.resolve();
    assert.equal(h.requests[3].files.length, 1);
    h.requests[3].resolve(success(h.requests[3]));
    await retry;
    assert.equal(h.state.items.value.length, 0);
    assert.equal(h.state.selected.value.size, 0);
  } finally { h.close(); }
});

for (const mode of ['account change', 'unmount']) test(`${mode} aborts deletion and rejects stale results before another batch`, async () => {
  const h = harness([asset('bulk', Array.from({ length: 501 }, (_, index) => file(`f${index}`)))]);
  let closed = false;
  try {
    h.state.selectCurrentFiles();
    h.state.requestDelete([...h.state.selected.value.values()]);
    const deleting = h.state.confirmDelete();
    if (mode === 'account change') h.owner.value = { id: 'owner-b' };
    else { h.close(); closed = true; }
    assert.equal(h.requests[0].signal.aborted, true);
    h.requests[0].resolve(success(h.requests[0]));
    await deleting;
    assert.equal(h.requests.length, 1);
    assert.equal(h.state.selected.value.size, 0);
    assert.equal(h.state.deleteTargets.value.length, 0);
    assert.equal(h.successToasts.length, 0);
    assert.equal(h.browser.refreshes, 0);
  } finally { if (!closed) h.close(); }
});

test('delete API sends scoped references and cancellation signal without file bytes', async () => {
  const calls = [];
  const response = { results: [{ asset_id: 'a', file_id: 'f', status: 'deleted' }], deleted_count: 1, failed_count: 0 };
  const api = load('../src/api/dataLibrary.ts', {
    './client': { apiClient: { post: async (...args) => { calls.push(args); return { data: { data: response } }; } } },
    '../utils/authTokens.ts': { authSessionKey: () => 'owner' },
  });
  const controller = new AbortController();
  assert.deepEqual(await api.deleteDataLibraryFiles([{ asset_id: 'a', file_id: 'f' }], controller.signal), response);
  assert.deepEqual(calls[0], ['/data-library/files/delete', { files: [{ asset_id: 'a', file_id: 'f' }] }, { signal: controller.signal }]);
});

const folder = (id, path, parent_id = null) => ({ id, path, parent_id, name: path.split('/').at(-1), created_at: stamp, updated_at: stamp });
const directoryFixtures = [folder('project', 'project'), folder('raw', 'project/raw', 'project'), folder('nested', 'project/raw/deep', 'raw'), folder('sibling', 'project/raw-other', 'project'), folder('empty', 'empty')];
const folderResult = (id = 'raw', patch = {}) => ({ folder_id: id, parent_id: id === 'raw' ? 'project' : null, completed: true, deleted_file_count: 2, deleted_folder_count: 2, remaining_file_count: 0, remaining_folder_count: 0, message: null, ...patch });
async function prepareFolderDeletion(h, id = 'raw', counts = { file_count: 2000, subfolder_count: 12 }) {
  const reading = h.state.requestFolderDelete(id);
  h.previewRequests.at(-1).resolve({ deletion_token: deletionToken, folder: h.folders.value.find(folder => folder.id === id), ...counts });
  await reading;
}

test('directory confirmation requires complete server counts and cancels without deleting', async () => {
  const h = harness([asset('a', [file('visible', 'project/raw/visible.csv')])], directoryFixtures);
  try {
    await h.state.requestFolderDelete('root');
    assert.equal(h.previewRequests.length, 0);
    const pending = h.state.requestFolderDelete('raw');
    await h.state.confirmFolderDelete();
    assert.equal(h.folderRequests.length, 0, 'unconfirmed preview cannot delete');
    h.previewRequests[0].resolve({ deletion_token: deletionToken, folder: directoryFixtures[1], file_count: 2000, subfolder_count: 12 });
    await pending;
    assert.equal(h.state.folderDeletePreview.value.file_count, 2000, 'count includes unloaded and filtered-out files');
    assert.equal(h.state.folderDeletePreview.value.subfolder_count, 12);
    h.state.closeFolderDelete();
    assert.equal(h.folderRequests.length, 0);
    const next = h.state.requestFolderDelete('raw');
    h.state.closeFolderDelete();
    assert.equal(h.previewRequests[1].signal.aborted, true);
    h.previewRequests[1].resolve({ deletion_token: deletionToken, folder: directoryFixtures[1], file_count: 1, subfolder_count: 0 });
    await next;
    assert.equal(h.state.folderDeleteTarget.value, null, 'late preview cannot reopen a cancelled dialog');
  } finally { h.close(); }
});

test('preview failures keep deletion disabled, allow retry, and never expose private paths', async () => {
  const h = harness([], directoryFixtures);
  try {
    const reading = h.state.requestFolderDelete('raw');
    h.previewRequests[0].reject(new Error('/private/source'));
    await reading;
    assert.match(h.state.folderDeleteError.value, /重新统计/);
    assert.doesNotMatch(h.state.folderDeleteError.value, /private/);
    await h.state.confirmFolderDelete();
    assert.equal(h.folderRequests.length, 0);
    const retry = h.state.loadFolderDeletePreview();
    h.previewRequests[1].resolve({ deletion_token: deletionToken, folder: { ...directoryFixtures[1], path: '/private/source' }, file_count: 1, subfolder_count: 0 });
    await retry;
    assert.equal(h.state.folderDeletePreview.value, null);
    assert.equal(h.state.folderDeleteTarget.value.path, 'project/raw');
    const valid = h.state.loadFolderDeletePreview();
    h.previewRequests[2].resolve({ deletion_token: deletionToken, folder: directoryFixtures[1], file_count: 0, subfolder_count: 0 });
    await valid;
    assert.equal(h.state.folderDeleteError.value, '');
    assert.equal(h.state.folderDeletePreview.value.file_count, 0);
  } finally { h.close(); }
});

test('completed subtree deletion cleans only its scope and returns to its surviving parent', async () => {
  const records = [asset('mixed', [file('same', 'project/raw/a.csv'), file('nested', 'project/raw/deep/b.csv'), file('outside', 'project/raw-other/a.csv'), file('named-like-directory', 'project/raw')]), asset('other', [file('same', 'project/raw-other/shared.csv')])];
  const h = harness(records, directoryFixtures);
  try {
    for (const path of ['project/raw', 'project/raw/deep', 'project/raw-other', 'project']) manager.libraryCurrentFiles(records, path).forEach(h.state.toggleFile);
    h.state.navigateFolder('nested');
    h.state.inspected.value = manager.libraryCurrentFiles(records, 'project/raw/deep')[0];
    h.state.uploadTargetId.value = 'nested';
    h.state.uploadFiles.value = [file('staged')];
    h.state.saveVisible.value = true;
    h.state.newFolderParentId.value = 'nested';
    h.state.newFolderVisible.value = true;
    await prepareFolderDeletion(h);
    const deleting = h.state.confirmFolderDelete();
    await h.state.confirmFolderDelete();
    h.state.requestDelete([...h.state.selected.value.values()]);
    h.state.closeFolderDelete();
    assert.equal(h.folderRequests.length, 1);
    assert.equal(h.folderRequests[0].token, deletionToken);
    assert.equal(h.requests.length, 0);
    assert.equal(h.state.deleteTargets.value.length, 0, 'file deletion cannot open during folder deletion');
    assert.ok(h.state.folderDeleteTarget.value, 'cannot close an in-flight deletion');
    h.folderRequests[0].resolve(folderResult());
    await deleting;
    assert.deepEqual(h.folders.value.map(folder => folder.id), ['project', 'sibling', 'empty']);
    assert.equal(h.state.currentFolderId.value, 'project');
    assert.deepEqual(h.state.items.value.map(item => item.files.map(file => file.file_id)), [['outside', 'named-like-directory'], ['same']]);
    assert.equal(h.state.selected.value.size, 3, 'same object in another location and root file named like folder survive');
    assert.equal(h.state.inspected.value, null);
    assert.equal(h.state.uploadTargetId.value, null);
    assert.equal(h.state.uploadFiles.value.length, 1, 'unsaved uploads are preserved');
    assert.equal(h.state.saveVisible.value, false);
    assert.equal(h.state.newFolderParentId.value, null);
    assert.equal(h.state.newFolderVisible.value, false);
    assert.equal(h.state.folderDeleteTarget.value, null);
    assert.equal(h.browser.refreshes, 1);
  } finally { h.close(); }
});

test('partial directory deletion clears stale in-scope selection and retains a retryable confirmation', async () => {
  const records = [asset('a', [file('inside', 'project/raw/a.csv'), file('outside', 'project/raw-other/b.csv')])];
  const h = harness(records, directoryFixtures);
  try {
    for (const path of ['project/raw', 'project/raw-other']) manager.libraryCurrentFiles(records, path).forEach(h.state.toggleFile);
    h.state.navigateFolder('raw');
    h.state.inspected.value = manager.libraryCurrentFiles(records, 'project/raw')[0];
    await prepareFolderDeletion(h);
    const first = h.state.confirmFolderDelete();
    h.folderRequests[0].resolve(folderResult('raw', { completed: false, deleted_file_count: 1, deleted_folder_count: 0, remaining_file_count: 3, remaining_folder_count: 2, message: '/private/error' }));
    await first;
    assert.equal(h.state.currentFolderId.value, 'raw');
    assert.equal(h.state.folderDeletePreview.value.file_count, 3);
    assert.equal(h.state.folderDeletePreview.value.subfolder_count, 1);
    assert.deepEqual([...h.state.selected.value.values()].map(item => item.file.file_id), ['outside']);
    assert.equal(h.state.inspected.value, null);
    assert.match(h.state.folderDeleteError.value, /请重试/);
    assert.doesNotMatch(h.state.folderDeleteError.value, /private/);
    assert.equal(h.browser.refreshes, 1);
    const retry = h.state.confirmFolderDelete();
    assert.equal(h.folderRequests[1].token, deletionToken, 'partial retries keep the same folder incarnation token');
    h.folderRequests[1].reject(new Error('/private/failed'));
    await retry;
    assert.ok(h.state.folderDeletePreview.value);
    assert.equal(h.state.folderDeleting.value, false);
    const finish = h.state.confirmFolderDelete();
    h.folderRequests[2].resolve(folderResult());
    await finish;
    assert.equal(h.state.folderDeleteTarget.value, null);
    assert.equal(h.state.currentFolderId.value, 'project');
  } finally { h.close(); }
});

test('empty-folder deletion works and missing parent navigation resolves to the closest remaining ancestor', async () => {
  const entries = [...directoryFixtures, folder('year', 'project/year', 'project'), folder('month', 'project/year/month', 'year')];
  const h = harness([], entries);
  try {
    h.state.navigateFolder('month');
    await prepareFolderDeletion(h, 'month', { file_count: 0, subfolder_count: 0 });
    const deleting = h.state.confirmFolderDelete();
    h.folders.value = h.folders.value.filter(folder => folder.id !== 'year');
    h.folderRequests[0].resolve(folderResult('month', { parent_id: 'year', deleted_file_count: 0, deleted_folder_count: 1 }));
    await deleting;
    assert.equal(h.state.currentFolderId.value, 'project');
    assert.equal(h.folders.value.some(folder => folder.id === 'month'), false);
    await prepareFolderDeletion(h, 'empty', { file_count: 0, subfolder_count: 0 });
    const empty = h.state.confirmFolderDelete();
    h.folderRequests[1].resolve(folderResult('empty', { deleted_file_count: 0, deleted_folder_count: 1 }));
    await empty;
    assert.equal(h.state.currentFolderId.value, 'project', 'deleting another folder does not move current view');
  } finally { h.close(); }
});

for (const stage of ['preview', 'delete']) for (const action of ['account switch', 'unmount']) test(`${action} invalidates a late directory ${stage}`, async () => {
  const h = harness([], directoryFixtures);
  let closed = false;
  try {
    let pending, request;
    if (stage === 'preview') { pending = h.state.requestFolderDelete('raw'); request = h.previewRequests[0]; }
    else { await prepareFolderDeletion(h); pending = h.state.confirmFolderDelete(); request = h.folderRequests[0]; }
    if (action === 'account switch') h.owner.value = { id: 'owner-b' };
    else { h.close(); closed = true; }
    assert.equal(request.signal.aborted, true);
    request.resolve(stage === 'preview' ? { deletion_token: deletionToken, folder: directoryFixtures[1], file_count: 1, subfolder_count: 1 } : folderResult());
    await pending;
    assert.equal(h.state.folderDeleteTarget.value, null);
    assert.equal(h.state.folderDeletePreview.value, null);
    assert.equal(h.successToasts.length, 0);
    assert.equal(h.browser.refreshes, 0);
  } finally { if (!closed) h.close(); }
});

test('file deletion confirmation and execution prevent opening a conflicting folder deletion', async () => {
  const h = harness([asset('a', [file('f')])], directoryFixtures);
  try {
    h.state.requestDelete(h.state.currentFiles.value);
    await h.state.requestFolderDelete('raw');
    assert.equal(h.previewRequests.length, 0);
    const deleting = h.state.confirmDelete();
    await h.state.requestFolderDelete('raw');
    assert.equal(h.previewRequests.length, 0);
    h.requests[0].resolve(success(h.requests[0]));
    await deleting;
  } finally { h.close(); }
});

test('directory API uses owner-scoped preview and delete URLs with cancellation and no filtered-file payload', async () => {
  const calls = [];
  const api = load('../src/api/dataLibrary.ts', {
    './client': { apiClient: { get: async (...args) => { calls.push(['get', ...args]); return { data: { data: {} } }; }, delete: async (...args) => { calls.push(['delete', ...args]); return { data: { data: {} } }; } } },
    '../utils/authTokens.ts': { authSessionKey: () => 'owner' },
  });
  const controller = new AbortController();
  await api.previewDataLibraryFolderDeletion('id/with delimiter', controller.signal);
  await api.deleteDataLibraryFolder('id/with delimiter', deletionToken, controller.signal);
  assert.deepEqual(calls, [
    ['get', '/data-library/folders/id%2Fwith%20delimiter/deletion-preview', { signal: controller.signal }],
    ['delete', '/data-library/folders/id%2Fwith%20delimiter', { params: { deletion_token: deletionToken }, signal: controller.signal }],
  ]);
});

test('a recreated directory requires a new preview token before a further delete request', async () => {
  const h = harness([], directoryFixtures);
  try {
    await prepareFolderDeletion(h);
    const deleting = h.state.confirmFolderDelete();
    h.folderRequests[0].reject({ response: { status: 409 } });
    await deleting;
    assert.equal(h.state.folderDeletePreview.value, null);
    assert.match(h.state.folderDeleteError.value, /重新统计/);
    await h.state.confirmFolderDelete();
    assert.equal(h.folderRequests.length, 1);
    const reading = h.state.loadFolderDeletePreview();
    const newToken = 'b'.repeat(64);
    h.previewRequests[1].resolve({ folder: directoryFixtures[1], deletion_token: newToken, file_count: 0, subfolder_count: 0 });
    await reading;
    const retried = h.state.confirmFolderDelete();
    assert.equal(h.folderRequests[1].token, newToken);
    h.folderRequests[1].resolve(folderResult());
    await retried;
  } finally { h.close(); }
});

for (const counts of [
  { remaining_file_count: null, remaining_folder_count: null },
  { remaining_file_count: null, remaining_folder_count: 2 },
  { remaining_file_count: 3, remaining_folder_count: null },
]) test(`unknown remaining directory counts are reported honestly and preserve retry token: ${JSON.stringify(counts)}`, async () => {
  const h = harness([], directoryFixtures);
  try {
    await prepareFolderDeletion(h);
    const deleting = h.state.confirmFolderDelete();
    h.folderRequests[0].resolve(folderResult('raw', { completed: false, ...counts }));
    await deleting;
    assert.equal(h.state.folderDeleteCountsUnknown.value, true);
    assert.match(h.state.folderDeleteError.value, /剩余范围暂时无法统计/);
    assert.doesNotMatch(h.state.folderDeleteError.value, /null|还有 0/);
    assert.equal(h.state.folderDeletePreview.value.deletion_token, deletionToken);
    const retry = h.state.confirmFolderDelete();
    assert.equal(h.folderRequests[1].token, deletionToken);
    h.folderRequests[1].resolve(folderResult('raw', { completed: false, remaining_file_count: 3, remaining_folder_count: 2 }));
    await retry;
    assert.equal(h.state.folderDeleteCountsUnknown.value, false);
    assert.equal(h.state.folderDeletePreview.value.file_count, 3);
    assert.equal(h.state.folderDeletePreview.value.subfolder_count, 1);
    h.state.closeFolderDelete();
    assert.equal(h.state.folderDeleteCountsUnknown.value, false);
  } finally { h.close(); }
});
