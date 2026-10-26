import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import * as vue from 'vue';
import { compileScript, parse } from '@vue/compiler-sfc';
import ts from 'typescript';

function load(path, dependencies = {}) {
  const source = readFileSync(new URL(path, import.meta.url), 'utf8');
  const content = path.endsWith('.vue') ? compileScript(parse(source).descriptor, { id: 'library-columns-test' }).content : source;
  const code = ts.transpileModule(content, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
  const module = { exports: {} };
  new Function('require', 'module', 'exports', code)(name => {
    if (name in dependencies) return dependencies[name];
    if (name === 'vue') return vue;
    if (name.endsWith('.vue') || ['lucide-vue-next', 'reka-ui'].includes(name)) return {};
    throw new Error(`Unexpected dependency ${name}`);
  }, module, module.exports);
  return module.exports;
}
const tree = load('../src/utils/personalDataTree.ts');
const exploration = load('../src/utils/explorationFiles.ts');
const columns = load('../src/utils/libraryColumns.ts', { './personalDataTree': tree, './explorationFiles': exploration });
const filters = load('../src/utils/libraryFileFilters.ts', { './personalDataTree': tree });
const manager = load('../src/utils/libraryFileManager.ts', { './personalDataTree': tree });
const file = (id, relative_path) => ({ file_id: id, filename: `${id}.csv`, relative_path, size: 100, upload_date: '2026-10-21T01:00:00Z' });
const asset = (id, files) => ({ id, name: id, files, status: 'ready', description: '', tags: [], source_type: 'upload', created_at: '2026-10-21T01:00:00Z', updated_at: '2026-10-21T01:00:00Z', source_url: null });
const fixtures = [asset('research', [file('a', 'measurements/2026/a.csv'), file('b', 'measurements/2026/b.csv'), file('note')]), asset('other', [file('x')])];

test('virtual root has files and actual directories without synthetic record wrappers', () => {
  assert.deepEqual(columns.libraryColumnTree([]), []);
  assert.deepEqual(columns.libraryColumnTree([asset('empty', [])]), []);
  const roots = columns.libraryColumnTree([asset('not-a-directory', [file('plain'), file('blank', ''), file('named', 'display.csv'), file('one', 'raw/one.csv'), file('deep', 'raw/year/month/day/deep.csv')])]);
  assert.deepEqual(roots.map(node => [node.kind, node.name]), [['directory', 'raw'], ['file', 'blank.csv'], ['file', 'display.csv'], ['file', 'plain.csv']]);
  assert.equal(roots.some(node => node.name === 'not-a-directory'), false);
  assert.equal(roots[0].root, true);
  assert.equal(roots[0].fileCount, 2);
  assert.equal(roots[0].children.find(node => node.name === 'year').root, false);
  const expanded = new Set();
  const gather = nodes => nodes.forEach(node => { if (node.kind === 'directory') { expanded.add(node.id); gather(node.children); } });
  gather(roots);
  const flattened = tree.flattenPersonalDataTree(roots, expanded);
  assert.equal(flattened.find(node => node.name === 'plain.csv').depth, 0);
  assert.equal(flattened.find(node => node.name === 'deep.csv').depth, 4);
});

test('logical directories merge across records while same-name files and provenance stay distinct', () => {
  const assets = [
    asset('first', [file('one', 'raw/same.csv'), file('duplicate', 'raw/reused.csv'), { ...file('root-collision'), filename: 'raw' }]),
    { ...asset('second', [file('two', 'raw/same.csv'), file('duplicate', 'raw/reused.csv'), file('gone', 'lost/a.csv')]), status: 'missing' },
  ];
  const roots = columns.libraryColumnTree(assets);
  const raw = roots.find(node => node.kind === 'directory' && node.name === 'raw');
  assert.equal(roots.filter(node => node.name === 'raw').length, 2);
  assert.deepEqual(raw.assetIds, ['first', 'second']);
  assert.equal('assetId' in raw, false);
  assert.equal(raw.relativePath, 'raw');
  assert.equal(raw.fileCount, 4);
  assert.equal(raw.missing, false);
  assert.equal(roots.find(node => node.name === 'lost').missing, true);
  const sameName = raw.children.filter(node => node.name === 'same.csv');
  assert.equal(sameName.length, 2);
  assert.notEqual(sameName[0].id, sameName[1].id);
  assert.deepEqual(sameName.map(node => node.assetId), ['first', 'second']);
  const repeated = raw.children.filter(node => node.file.file_id === 'duplicate');
  assert.equal(new Set(repeated.map(node => node.id)).size, 2);
  assert.deepEqual(repeated.map(node => node.relatedFiles.length), [3, 3]);
});

test('column navigation uses actual directories and preserves identities across sorting and filtering', () => {
  const roots = columns.libraryColumnTree(fixtures);
  const data = roots.find(node => node.kind === 'directory');
  const year = data.children[0];
  const path = [data.id, year.id];
  assert.deepEqual(columns.libraryColumns(roots, path).map(column => column.name), ['我的数据', 'measurements', '2026']);
  assert.deepEqual(columns.libraryColumns(roots, path).at(-1).nodes.map(node => node.name), ['a.csv', 'b.csv']);
  assert.deepEqual(columns.resolveLibraryColumnPath(roots, [data.id, roots.find(node => node.kind === 'file').id]).map(node => node.id), [data.id]);
  assert.deepEqual(columns.resolveLibraryColumnPath(columns.libraryColumnTree([fixtures[1]]), path), []);
  const filtered = columns.libraryColumnTree([{ ...fixtures[0], files: [fixtures[0].files[1]] }]);
  assert.deepEqual(columns.resolveLibraryColumnPath(filtered, path).map(node => node.id), path);
  assert.equal(columns.libraryColumns(filtered, path).at(-1).nodes[0].id, columns.libraryColumns(roots, path).at(-1).nodes.find(node => node.name === 'b.csv').id);
  assert.deepEqual(columns.libraryColumnTree([...fixtures].reverse()), roots);
});

function browserHarness() {
  const props = vue.reactive({ items: structuredClone(fixtures), selectedIds: [] });
  const events = [];
  const scope = vue.effectScope();
  const component = load('../src/components/LibraryColumnBrowser.vue', { '../utils/libraryColumns': columns }).default;
  const state = scope.run(() => component.setup(props, { expose() {}, emit: (...args) => events.push(args) }));
  return { props, state, events, close() { scope.stop(); } };
}

test('files stay in their current column and mobile back climbs files then directories to root', async () => {
  const h = browserHarness();
  try {
    const roots = h.state.tree.value;
    const rootFile = roots.find(node => node.kind === 'file');
    h.state.openNode(rootFile, 0);
    assert.equal(h.state.columns.value.length, 1);
    assert.equal(h.state.focusedFile.value.id, rootFile.id);
    h.state.goBack();
    assert.equal(h.state.focused.value, null);
    assert.equal(h.state.columns.value.length, 1);
    const directory = roots.find(node => node.kind === 'directory');
    h.state.openNode(directory, 0);
    assert.equal(h.state.columns.value.length, 2);
    const nested = directory.children[0];
    h.state.openNode(nested, 1);
    h.state.openNode(nested.children[0], 2);
    assert.equal(h.state.columns.value.length, 3);
    h.state.goBack();
    assert.equal(h.state.focused.value.id, nested.id);
    assert.equal(h.state.columns.value.length, 3);
    h.state.goBack();
    assert.equal(h.state.focused.value.id, directory.id);
    assert.equal(h.state.columns.value.length, 2);
    h.state.goBack();
    assert.equal(h.state.focused.value, null);
    assert.equal(h.state.columns.value.length, 1);
    assert.deepEqual(h.props.selectedIds, []);
    await vue.nextTick();
    assert.ok(h.events.every(([name]) => name === 'inspect'));
  } finally { h.close(); }
});

test('browser preview and handoff facts discard host paths and normalize duplicate file identities', () => {
  const files = columns.libraryBrowserFiles([
    { ...file('gridfs:private'), filename: '/private/host/private.csv', relative_path: '../../host/private.csv', file_url: 'file:///private/host/private.csv', metadata: { path: '/private/host/private.csv' } },
    { ...file('private'), filename: 'duplicate.csv' }, file('temp-not-ready'), file('safe', 'public/safe.csv'),
  ]);
  assert.deepEqual(files.map(item => item.file_id), ['private', 'safe']);
  assert.equal(files[0].filename, 'private.csv');
  assert.equal(files[0].relative_path, undefined);
  assert.equal(files[1].relative_path, 'public/safe.csv');
  assert.equal('metadata' in files[0], false);
  assert.equal('file_url' in files[0], false);
  assert.doesNotMatch(JSON.stringify(files), /\/private\/host|\.\.\//);
});

function pageHarness() {
  const owner = vue.ref({ id: 'one' });
  const stateItems = vue.ref(structuredClone(fixtures));
  const handoffs = [], routes = [], previews = [], writes = [], deletions = [], cleanup = [], folderWrites = [];
  const folders = vue.ref([{ id: 'raw', name: 'raw', parent_id: null, path: 'raw' }, { id: 'nested', name: 'nested', parent_id: 'raw', path: 'raw/nested' }]);
  const scope = vue.effectScope();
  const component = load('../src/pages/MyDataPage.vue', {
    vue: { ...vue, onMounted() {}, onUnmounted: fn => cleanup.push(fn) },
    'vue-router': { useRouter: () => ({ push: path => { routes.push(path); return Promise.resolve(); } }) },
    '../api/dataLibrary': { dataLibrarySourceLabels: {}, updateDataLibraryAsset: (...args) => new Promise(resolve => writes.push({ args, resolve })), deleteDataLibraryFiles: (...args) => new Promise(resolve => deletions.push({ args, resolve })) },
    '../composables/useAuth': { useAuth: () => ({ currentUser: owner }) },
    '../composables/useLibraryBrowser': { useLibraryBrowser: () => ({ items: stateItems, visibleItems: vue.computed(() => stateItems.value), stats: vue.ref(null), query: vue.ref(''), fileFormat: vue.ref(''), fromDate: vue.ref(''), toDate: vue.ref(''), sourceType: vue.ref(''), nextCursor: vue.ref(null), loading: vue.ref(false), error: vue.ref(''), load() {}, resetFilters() {} }) },
    '../composables/useLibraryFolders': { useLibraryFolders: () => ({ folders, loading: vue.ref(false), error: vue.ref(''), load() {}, createFolder: input => new Promise(resolve => folderWrites.push({ input, resolve })) }) },
    '../utils/libraryFileManager': manager,
    '../composables/useDataSelection': { setPendingLibraryFiles: files => handoffs.push(files) },
    '../composables/useFilePanel': { useFilePanel: () => ({ showFilePanel: (...args) => previews.push(args) }) },
    '../composables/useLeftPanel': { useLeftPanel: () => ({ isLeftPanelShow: vue.ref(false), showLeftPanel() {} }) },
    '../utils/fileType': { formatFileSize: value => String(value) },
    '../utils/libraryColumns': columns,
    '../utils/libraryFileFilters': filters,
    '../utils/personalDataTree': tree,
    '../utils/toast': { showSuccessToast() {}, showErrorToast() {} },
  }).default;
  const state = scope.run(() => component.setup({}, { expose() {} }));
  return { state, owner, stateItems, handoffs, routes, previews, writes, deletions, folders, folderWrites, close() { scope.stop(); } };
}
function leaf(assetId, raw) { return { kind: 'file', id: JSON.stringify(['file', assetId, tree.personalFileId(raw)]), name: raw.filename, assetId, missing: false, file: raw, relatedFiles: [raw] }; }

test('file detail analysis transfers exactly that file while management selection exceeds the analysis limit', () => {
  const h = pageHarness();
  try {
    h.state.toggleFile(leaf('research', file('a')));
    h.state.inspected.value = leaf('research', file('b'));
    assert.deepEqual(h.handoffs, []);
    h.state.startFileAnalysis();
    assert.deepEqual(h.handoffs[0].map(item => item.file_id), ['b']);
    h.state.startSelectedAnalysis();
    assert.deepEqual(h.handoffs[1].map(item => item.file_id), ['a']);
    assert.deepEqual(h.routes, ['/chat', '/chat']);
    h.state.clearSelection();
    for (let index = 0; index < 33; index++) h.state.toggleFile(leaf('research', file(`f-${index}`)));
    assert.equal(h.state.selectedFiles.value.length, 33);
    assert.equal(h.state.selected.value.size, 33);
    h.state.startSelectedAnalysis();
    assert.equal(h.handoffs.length, 2, 'analysis rejects more than 32 unique files');
    assert.match(h.state.selectionError.value, /32/);
    h.state.toggleFile(leaf('research', file('f-0')));
    assert.equal(h.state.selectedFiles.value.length, 32);
    assert.equal(h.state.selectionError.value, '');
    h.state.startSelectedAnalysis();
    assert.equal(h.handoffs[2].length, 32);
    assert.equal(h.handoffs[2].some(item => item.file_id === 'f-0'), false);
    h.state.inspected.value = { ...leaf('research', file('missing')), missing: true };
    h.state.startFileAnalysis();
    assert.equal(h.handoffs.length, 3);
  } finally { h.close(); }
});

test('preview after file filtering uses every safe sibling in the full record and never starts analysis', () => {
  const h = pageHarness();
  try {
    h.state.inspected.value = leaf('research', file('a', 'measurements/2026/a.csv'));
    h.state.previewFile();
    assert.deepEqual(h.previews[0][1].map(item => item.file_id), ['a', 'b', 'note']);
    assert.deepEqual(h.handoffs, []);
    assert.deepEqual(h.routes, []);
    h.state.inspected.value = columns.libraryColumnTree(fixtures).find(node => node.kind === 'directory');
    assert.equal(h.state.inspectedAsset.value, undefined);
    h.state.startFileAnalysis(); h.state.previewFile();
    assert.equal(h.previews.length, 1);
    assert.deepEqual(h.handoffs, []);
  } finally { h.close(); }
});

test('account changes clear personal selections and prevent old confirmation or edit responses crossing accounts', async () => {
  const h = pageHarness();
  try {
    h.state.toggleFile(leaf('research', file('a')));
    h.state.uploadFiles.value = [file('upload')];
    h.state.requestDelete([leaf('research', file('a'))]);
    assert.equal(h.state.deleteTargets.value.length, 1);
    h.state.openEdit(fixtures[0]);
    h.state.editName.value = 'new name';
    const saving = h.state.saveEdit();
    h.owner.value = { id: 'two' };
    assert.deepEqual(h.state.selectedFiles.value, []);
    assert.deepEqual(h.state.uploadFiles.value, []);
    assert.equal(h.state.editing.value, undefined);
    assert.deepEqual(h.state.deleteTargets.value, []);
    await h.state.confirmDelete();
    assert.equal(h.deletions.length, 0, 'old pending confirmation cannot delete under the new account');
    assert.equal(h.writes.length, 1);
    h.stateItems.value = [fixtures[1]];
    h.writes[0].resolve({ ...fixtures[0], name: 'late private data' });
    await saving;
    assert.deepEqual(h.stateItems.value.map(item => item.id), ['other']);
  } finally { h.close(); }
});


test('root files analyze precisely and cross-record references stay distinct while analysis deduplicates', () => {
  const h = pageHarness();
  try {
    const roots = columns.libraryColumnTree(fixtures);
    h.state.inspected.value = roots.find(node => node.kind === 'file' && node.file.file_id === 'note');
    h.state.startFileAnalysis();
    assert.deepEqual(h.handoffs[0].map(item => item.file_id), ['note']);
    h.state.toggleFile(leaf('research', file('gridfs:shared')));
    assert.equal(h.state.selectedFiles.value.length, 1);
    h.state.toggleFile(leaf('other', file('shared')));
    assert.equal(h.state.selected.value.size, 2);
    assert.equal(h.state.selectedFiles.value.length, 1);
    h.state.startSelectedAnalysis();
    assert.deepEqual(h.handoffs[1].map(item => item.file_id), ['shared']);
    h.state.toggleFile(leaf('other', file('shared')));
    assert.equal(h.state.selected.value.size, 1);
    assert.equal([...h.state.selected.value.values()][0].assetId, 'research');
    assert.equal(h.state.selectedFiles.value.length, 1);
    h.state.inspected.value = columns.libraryColumnTree([asset('a', [file('a', 'raw/a.csv')]), asset('b', [file('b', 'raw/b.csv')])])[0];
    assert.deepEqual(h.state.inspected.value.assetIds, ['a', 'b']);
    assert.equal(h.state.inspectedAsset.value, undefined);
    assert.deepEqual(h.writes, []);
  } finally { h.close(); }
});


test('uploads retain the folder captured before navigation and cannot mix unfinished batches', () => {
  const h = pageHarness();
  try {
    let opened = 0;
    h.state.uploadRef.value = { uploadFile() { opened++; } };
    h.state.startUpload('raw');
    assert.equal(h.state.uploadTargetId.value, 'raw');
    h.state.uploadFiles.value = [file('upload')];
    h.state.navigateFolder('nested');
    assert.equal(h.state.currentFolderId.value, 'nested');
    assert.equal(h.state.uploadTargetId.value, 'raw');
    h.state.startUpload('nested');
    assert.equal(opened, 1);
    assert.equal(h.state.uploadTargetId.value, 'raw');
    h.state.uploadFiles.value = [];
    h.state.startUpload('root');
    assert.equal(opened, 2);
    assert.equal(h.state.uploadTargetId.value, null);
    h.owner.value = { id: 'two' };
    assert.equal(h.state.currentFolderId.value, 'root');
    assert.equal(h.state.uploadTargetId.value, null);
  } finally { h.close(); }
});

test('folder creation captures its parent and ignores late responses after an account switch', async () => {
  const h = pageHarness();
  try {
    h.state.openCreateFolder('raw');
    h.state.newFolderName.value = '../unsafe';
    await h.state.submitFolder();
    assert.equal(h.folderWrites.length, 0);
    assert.match(h.state.newFolderError.value, /名称/);
    h.state.newFolderName.value = 'new folder';
    const saving = h.state.submitFolder();
    h.state.navigateFolder('nested');
    assert.deepEqual(h.folderWrites[0].input, { name: 'new folder', parent_id: 'raw' });
    h.owner.value = { id: 'two' };
    h.folderWrites[0].resolve({ id: 'new-private', name: 'new folder', path: 'raw/new folder', parent_id: 'raw' });
    await saving;
    assert.equal(h.state.newFolderVisible.value, false);
    assert.equal(h.state.newFolderName.value, '');
    assert.equal(h.state.currentFolderId.value, 'root');
  } finally { h.close(); }
});
