import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import * as vue from 'vue';
import { compileScript, parse } from '@vue/compiler-sfc';
import ts from 'typescript';

function loadModule(path, dependencies = {}) {
  const source = readFileSync(new URL(path, import.meta.url), 'utf8');
  const content = path.endsWith('.vue') ? compileScript(parse(source).descriptor, { id: 'sidebar-test' }).content : source;
  const code = ts.transpileModule(content, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
  const module = { exports: {} };
  new Function('require', 'module', 'exports', code)(name => {
    if (name in dependencies) return dependencies[name];
    if (name === 'vue') return vue;
    if (name === 'lucide-vue-next') return {};
    throw new Error(`Unexpected dependency ${name}`);
  }, module, module.exports);
  return module.exports;
}

const treeUtils = loadModule('../src/utils/personalDataTree.ts');
const filterUtils = loadModule('../src/utils/libraryFileFilters.ts', { './personalDataTree': treeUtils });
const { buildPersonalDataTree, flattenPersonalDataTree, safeLibraryRelativePath } = treeUtils;
const file = (id, filename = `${id}.csv`, relative_path) => ({ file_id: id, filename, relative_path, upload_date: '2026-10-20T00:00:00Z' });
const asset = (id, files = [file(id)], extra = {}) => ({ id, name: '同名数据', files, status: 'ready', source_type: 'upload', updated_at: '2026-10-20T12:00:00Z', ...extra });
const flush = async () => { await Promise.resolve(); await vue.nextTick(); await Promise.resolve(); };

test('only validated relative_path creates directories, with no host paths in visible names', () => {
  const files = [
    file('safe', 'daily.csv', 'observations/2026/daily.csv'),
    file('legacy', '/Users/researcher/private/legacy.csv'),
    file('windows', 'C:\\private\\measurement.csv', 'C:\\private\\measurement.csv'),
    file('absolute', 'absolute.csv', '/private/absolute.csv'),
    file('traversal', 'traversal.csv', 'raw/../traversal.csv'),
  ];
  const roots = buildPersonalDataTree([asset('study', files)]);
  assert.deepEqual(roots.map(node => node.name), ['observations', 'absolute.csv', 'legacy.csv', 'measurement.csv', 'traversal.csv']);
  const raw = roots[0];
  assert.equal(raw.children[0].name, '2026');
  assert.equal(raw.children[0].children[0].name, 'daily.csv');
  const all = flattenPersonalDataTree(roots, new Set([raw.id, raw.children[0].id]));
  assert.doesNotMatch(all.map(row => row.name).join('\n'), /Users|private|researcher|C:/);
  assert.equal(all.find(row => row.kind === 'file' && row.file.file_id === 'legacy').file.filename, 'legacy.csv');
  for (const path of ['/root/a', '../a', 'a/../b', './a', 'a//b', '\\\\server\\a', 'C:/a', 'https://host/a', 'a\u0000/b']) {
    assert.equal(safeLibraryRelativePath(path), undefined, path);
  }
});

test('real directory paths merge across assets while colliding files remain distinct', () => {
  const roots = buildPersonalDataTree([
    asset('first', [file('one', 'data.csv', 'raw/data.csv'), file('two', 'data.csv', 'raw/data.csv'), file('three', 'raw')]),
    asset('second', [file('four', 'data.csv', 'raw/data.csv')]),
  ]);
  const directories = roots.filter(node => node.kind === 'directory');
  assert.equal(directories.length, 1);
  assert.equal(directories[0].name, 'raw');
  assert.equal(directories[0].fileCount, 3);
  assert.deepEqual(new Set(directories[0].assetIds), new Set(['first', 'second']));
  assert.equal(new Set(directories[0].children.map(row => row.id)).size, 3);
  assert.deepEqual(new Set(directories[0].children.map(row => row.assetId)), new Set(['first', 'second']));
  assert.equal(roots.filter(row => row.name === 'raw').length, 2);
  assert.deepEqual(flattenPersonalDataTree(roots, new Set()).map(row => row.id), roots.map(root => root.id));
  assert.equal(roots.some(row => row.name === '同名数据'), false);
});

function harness(overrides = {}) {
  const requests = [], statsRequests = [], folderRequests = [], events = [], mounted = [], unmounted = [];
  let folderItems = overrides.folderItems || [];
  const owner = vue.ref({ id: 'owner-a' });
  const scope = vue.effectScope();
  const libraryStats = { file_count: 80, total_bytes: 40960, formats: ['csv', 'nc', 'shp'], updated_at: '2026-10-20T12:00:00Z' };
  const browserModule = loadModule('../src/composables/useLibraryBrowser.ts', {
    '../api/dataLibrary': {
      listDataLibrary: (params, signal) => new Promise((resolve, reject) => requests.push({ params, signal, resolve, reject })),
      getDataLibraryStats: signal => { statsRequests.push({ signal }); return Promise.resolve(libraryStats); },
    },
    '../utils/libraryFileFilters': filterUtils,
    './useAuth': { useAuth: () => ({ currentUser: owner }) },
  });
  const listeners = new Map(), handlers = new Map();
  const browser = {
    addEventListener(type, callback) {
      if (!handlers.has(type)) handlers.set(type, new Set());
      handlers.get(type).add(callback);
      listeners.set(type, () => [...handlers.get(type)].forEach(handler => handler()));
    },
    removeEventListener(type, callback) {
      handlers.get(type)?.delete(callback);
      if (!handlers.get(type)?.size) { handlers.delete(type); listeners.delete(type); }
    },
    dispatchEvent(event) { listeners.get(event.type)?.(); },
  };
  const previousWindow = globalThis.window;
  globalThis.window = browser;
  const foldersModule = loadModule('../src/composables/useLibraryFolders.ts', {
    '../api/dataLibrary': {
      listDataLibraryFolders: signal => { folderRequests.push({ signal }); return Promise.resolve({ items: [...folderItems] }); },
      createDataLibraryFolder: async () => { throw new Error('Not expected in sidebar'); },
    },
    './useAuth': { useAuth: () => ({ currentUser: owner }) },
    '../utils/personalDataTree': treeUtils,
  });
  const component = loadModule('../src/components/PersonalDataSidebar.vue', {
    vue: { ...vue, onMounted: callback => mounted.push(callback), onUnmounted: callback => unmounted.push(callback) },
    '../composables/useAuth': { useAuth: () => ({ currentUser: owner }) },
    '../composables/useLibraryBrowser': browserModule,
    '../composables/useLibraryFolders': foldersModule,
    './LibraryFileControls.vue': {},
    '../utils/personalDataTree': treeUtils,
  }).default;
  const props = vue.reactive({ selectedFiles: [], disabled: false, ...overrides });
  const state = scope.run(() => component.setup(props, { expose() {}, emit: (...args) => events.push(args) }));
  mounted.forEach(callback => callback());
  return {
    owner, props, state, requests, statsRequests, folderRequests, events, listeners, libraryStats,
    setFolderItems(value) { folderItems = value; },
    async resolve(items, next_cursor = null, index = requests.length - 1) {
      requests[index].resolve({ items, next_cursor }); await flush();
    },
    close() { unmounted.forEach(callback => callback()); scope.stop(); globalThis.window = previousWindow; },
  };
}

test('real root directories expand initially, nested expansion works and folding keeps selected files', async () => {
  const h = harness();
  try {
    await h.resolve([asset('study', [file('one', 'one.csv'), file('two', 'two.csv', 'nested/deeper/two.csv')])]);
    assert.deepEqual(h.state.rows.value.map(row => row.name), ['nested', 'deeper', 'one.csv']);
    assert.equal(h.state.selectedIds.value.size, 0);
    const directory = h.state.rows.value.find(row => row.name === 'deeper');
    h.state.toggleDirectory(directory.id);
    const row = h.state.rows.value.find(row => row.kind === 'file' && row.file.file_id === 'two');
    assert.equal(row.depth, 2);
    h.state.toggleFile(row);
    assert.deepEqual(h.events.map(event => event[0]), ['toggle-file']);
    h.props.selectedFiles = [row.file];
    h.state.toggleDirectory(h.state.tree.value[0].id);
    assert.deepEqual(h.state.rows.value.map(row => row.name), ['nested', 'one.csv']);
    assert.equal(h.state.selectedIds.value.has('two'), true);
    h.state.toggleDirectory(h.state.tree.value[0].id);
    assert.ok(h.state.rows.value.some(item => item.id === row.id));
  } finally { h.close(); }
});

test('files without directory paths are immediately selectable and previewable at depth zero', async () => {
  const h = harness();
  try {
    await h.resolve([asset('study', [file('one', 'one.csv'), file('two', 'two.nc', 'two.nc')])]);
    assert.deepEqual(h.state.rows.value.map(row => [row.kind, row.name, row.depth]), [
      ['file', 'one.csv', 0], ['file', 'two.nc', 0],
    ]);
    assert.equal(h.state.expanded.value.size, 0);
    const row = h.state.rows.value[1];
    h.state.toggleFile(row);
    h.props.selectedFiles = [row.file];
    h.state.previewFile(row);
    assert.deepEqual(h.events.map(event => event[0]), ['toggle-file', 'preview']);
    assert.equal(h.events[1][1].file_id, 'two');
    assert.equal(h.events[1][2].length, 2);
    assert.deepEqual([...h.state.selectedIds.value], ['two']);
  } finally { h.close(); }
});

test('merged directories keep per-file preview provenance and available siblings', async () => {
  const h = harness();
  try {
    const assets = [
      asset('first', [file('first-shp', 'regions.shp', 'maps/regions.shp'), file('first-dbf', 'regions.dbf', 'maps/regions.dbf')]),
      asset('second', [file('second-shp', 'regions.shp', 'maps/regions.shp'), file('second-dbf', 'regions.dbf', 'maps/regions.dbf')]),
    ];
    await h.resolve(assets);
    assert.equal(h.state.tree.value.length, 1);
    assert.equal(h.state.tree.value[0].name, 'maps');
    assert.equal(h.state.tree.value[0].fileCount, 4);
    h.state.fileFormat.value = 'shp';
    void h.state.load(false, false);
    await h.resolve(assets);
    const files = h.state.rows.value.filter(row => row.kind === 'file');
    assert.equal(files.length, 2);
    for (const row of files) h.state.previewFile(row);
    assert.deepEqual(h.events.map(event => new Set(event[2].map(file => file.file_id))), [
      new Set(['first-shp', 'first-dbf']), new Set(['second-shp', 'second-dbf']),
    ]);
    assert.equal(h.state.selectedIds.value.size, 0);
  } finally { h.close(); }
});

test('preview is independent of selection and works while analysis selection is disabled', async () => {
  const h = harness();
  try {
    await h.resolve([asset('study', [file('one'), file('two')])]);
    const row = h.state.rows.value.find(row => row.kind === 'file');
    h.state.previewFile(row);
    assert.deepEqual(h.events.map(event => event[0]), ['preview']);
    assert.equal(h.events[0][2].length, 2);
    assert.equal(h.state.selectedIds.value.size, 0);
    h.props.disabled = true;
    h.state.toggleFile(row);
    h.state.previewFile(row);
    assert.deepEqual(h.events.map(event => event[0]), ['preview', 'preview']);
  } finally { h.close(); }
});

test('duplicate files share selection and missing files can be deselected but not selected', async () => {
  const h = harness({ selectedFiles: [file('same'), file('same')] });
  try {
    await h.resolve([asset('ready', [file('same')]), asset('missing', [file('same'), file('other')], { status: 'missing' })]);
    assert.equal(h.state.selectedIds.value.size, 1);
    const same = h.state.rows.value.filter(row => row.kind === 'file' && row.file.file_id === 'same');
    assert.equal(same.length, 2);
    assert.ok(same.every(row => h.state.selectedIds.value.has(row.file.file_id)));
    const missing = same.find(row => row.missing);
    h.state.toggleFile(missing);
    assert.equal(h.events[0][0], 'toggle-file');
    h.props.selectedFiles = [];
    assert.equal(h.state.selectionDisabled(missing), true);
    h.state.toggleFile(missing);
    assert.equal(h.events.length, 1);
  } finally { h.close(); }
});

test('account changes immediately clear the list and discard old-owner responses', async () => {
  const h = harness();
  try {
    await h.resolve([asset('owner-a-private', [file('private')], { description: 'owner-a private description' })], 'old-cursor');
    h.state.mobileExpanded.value = true;
    const oldRow = h.state.rows.value.find(row => row.kind === 'file');
    h.state.load(true);
    assert.equal(h.requests[1].params.cursor, 'old-cursor');
    h.owner.value = { id: 'owner-b' };
    assert.equal(h.state.items.value.length, 0);
    assert.equal(h.state.nextCursor.value, null);
    assert.equal(h.state.stats.value, null);
    assert.equal(h.state.expanded.value.size, 0);
    assert.equal(h.state.mobileExpanded.value, false);
    h.state.previewFile(oldRow);
    assert.deepEqual(h.events, []);
    assert.equal(h.requests[1].signal.aborted, true);
    h.requests[1].resolve({ items: [asset('owner-a-late')], next_cursor: 'secret' });
    await flush();
    assert.equal(h.state.items.value.length, 0);
    await h.resolve([asset('owner-b-private')]);
    assert.deepEqual(h.state.items.value.map(item => item.id), ['owner-b-private']);
    assert.equal(h.state.rows.value.some(row => row.assetId === 'owner-a-private'), false);
    h.owner.value = null;
    assert.equal(h.state.items.value.length, 0);
    assert.equal(h.state.error.value, '');
    assert.equal(h.state.stats.value, null);
    assert.equal(h.requests.length, 3);
  } finally { h.close(); }
});

test('library statistics remain global while filtered rows count only visible unique files', async () => {
  const h = harness();
  try {
    await h.resolve([asset('first', [file('same')]), asset('second', [file('gridfs:same'), file('other', 'other.nc')])], 'next');
    assert.deepEqual(h.state.stats.value, h.libraryStats);
    assert.equal(h.state.loadedFileCount.value, 2);
    h.state.fileFormat.value = 'nc';
    void h.state.load(false, false);
    await h.resolve([asset('second', [file('gridfs:same'), file('other', 'other.nc')])]);
    assert.equal(h.state.loadedFileCount.value, 1);
    assert.equal(h.state.stats.value.file_count, 80);
    assert.equal(h.statsRequests.length, 1);
    assert.equal(h.requests.at(-1).params.file_format, 'nc');
    assert.equal(h.state.rows.value.filter(row => row.kind === 'file').length, 1);
  } finally { h.close(); }
});

test('search expands matching paths, retains hidden selections and restores prior folding on clear', async () => {
  const h = harness({ selectedFiles: [file('hidden')] });
  try {
    const assets = [asset('study', [file('hidden', 'hidden.csv'), file('match', 'match.csv', 'observations/2026/match.csv')])];
    await h.resolve(assets);
    const rootId = h.state.tree.value[0].id;
    h.state.toggleDirectory(rootId);
    assert.deepEqual(h.state.rows.value.map(row => row.name), ['observations', 'hidden.csv']);
    h.state.query.value = 'match';
    void h.state.load(false, false);
    await h.resolve(assets);
    assert.deepEqual(h.state.rows.value.map(row => row.name), ['observations', '2026', 'match.csv']);
    assert.equal(h.state.hiddenSelectedCount.value, 1);
    assert.deepEqual([...h.state.selectedIds.value], ['hidden']);
    assert.deepEqual(h.events, []);
    h.state.resetFilters();
    await h.resolve(assets);
    assert.equal(h.state.hasFilters.value, false);
    assert.equal(h.state.expanded.value.has(rootId), false);
    assert.deepEqual(h.state.rows.value.map(row => row.name), ['observations', 'hidden.csv']);
    assert.deepEqual([...h.state.selectedIds.value], ['hidden']);
    assert.equal(h.state.hiddenSelectedCount.value, 0);
  } finally { h.close(); }
});

test('filtered multi-file preview receives all safely named dependencies without changing selection', async () => {
  const h = harness({ selectedFiles: [file('csv')], disabled: true });
  try {
    const assets = [asset('map', [
      file('shp', '/private/observations/regions.shp', 'maps/regions.shp'),
      file('dbf', 'C:\\private\\regions.dbf', 'maps/regions.dbf'),
      file('shx', 'regions.shx', 'maps/regions.shx'),
      file('csv', 'measurements.csv'),
    ])];
    await h.resolve(assets);
    h.state.fileFormat.value = 'shp';
    void h.state.load(false, false);
    await h.resolve(assets);
    const visibleFiles = h.state.rows.value.filter(row => row.kind === 'file');
    assert.equal(visibleFiles.length, 1);
    assert.equal(visibleFiles[0].name, 'regions.shp');
    h.state.previewFile(visibleFiles[0]);
    assert.deepEqual(h.events.map(event => event[0]), ['preview']);
    assert.deepEqual(new Set(h.events[0][2].map(file => file.file_id)), new Set(['shp', 'dbf', 'shx', 'csv']));
    assert.doesNotMatch(h.events[0][2].map(file => file.filename).join(' '), /private|C:|observations/);
    assert.deepEqual([...h.state.selectedIds.value], ['csv']);
    h.state.toggleFile(visibleFiles[0]);
    assert.equal(h.events.length, 1);
  } finally { h.close(); }
});

test('filename, format and update dates work together and empty results keep selections', async () => {
  const h = harness({ selectedFiles: [file('selected')] });
  try {
    await h.resolve([asset('first')]);
    h.state.query.value = 'daily';
    h.state.fileFormat.value = 'nc';
    h.state.fromDate.value = '2026-10-19';
    h.state.toDate.value = '2026-10-21';
    void h.state.load(false, false);
    assert.equal(h.requests.at(-1).params.filename, 'daily');
    assert.equal(h.requests.at(-1).params.file_format, 'nc');
    assert.ok(h.requests.at(-1).params.updated_from);
    assert.ok(h.requests.at(-1).params.updated_to);
    await h.resolve([
      asset('match', [file('netcdf', 'daily.nc'), file('csv', 'daily.csv'), file('other', 'monthly.nc')]),
      asset('old', [file('old', 'daily.nc')], { updated_at: '2020-01-01T00:00:00Z' }),
    ]);
    assert.deepEqual(h.state.rows.value.filter(row => row.kind === 'file').map(row => row.name), ['daily.nc']);
    h.state.query.value = 'unmatched';
    void h.state.load(false, false);
    await h.resolve([]);
    assert.equal(h.state.hasFilters.value, true);
    assert.equal(h.state.visibleItems.value.length, 0);
    assert.equal(h.state.rows.value.length, 0);
    assert.equal(h.state.error.value, '');
    assert.equal(h.state.loading.value, false);
    assert.deepEqual([...h.state.selectedIds.value], ['selected']);
    assert.equal(h.state.hiddenSelectedCount.value, 1);
  } finally { h.close(); }
});

test('missing files remain unavailable after filtering', async () => {
  const h = harness();
  try {
    h.state.query.value = 'one';
    void h.state.load(false, false);
    await h.resolve([asset('missing', [file('one')], { status: 'missing' })]);
    const row = h.state.rows.value.find(item => item.kind === 'file');
    h.state.previewFile(row);
    h.state.toggleFile(row);
    assert.deepEqual(h.events, []);
  } finally { h.close(); }
});

test('pagination failure keeps current files and retry continues the same cursor', async () => {
  const h = harness();
  try {
    await h.resolve([asset('first')], 'next');
    h.state.load(true);
    h.requests[1].reject(new Error('/private/host/source'));
    await flush();
    assert.deepEqual(h.state.items.value.map(item => item.id), ['first']);
    assert.doesNotMatch(h.state.error.value, /private/);
    h.state.retry();
    assert.equal(h.requests[2].params.cursor, 'next');
    await h.resolve([asset('second')]);
    assert.deepEqual(h.state.items.value.map(item => item.id), ['first', 'second']);
  } finally { h.close(); }
});

test('refresh event reloads the library and reveal opens the narrow layout', async () => {
  const h = harness();
  try {
    await h.resolve([asset('first')]);
    h.listeners.get('data-library-changed')();
    assert.equal(h.requests.length, 2);
    await h.resolve([asset('saved')]);
    assert.deepEqual(h.state.items.value.map(item => item.id), ['saved']);
    await h.state.reveal();
    assert.equal(h.state.mobileExpanded.value, true);
  } finally { h.close(); }
  assert.equal(h.listeners.size, 0);
});

test('complete directories include empty folders independently of paginated files', async () => {
  const h = harness({ folderItems: [
    { id: 'root', name: '实验', path: '实验', parent_id: null },
    { id: 'empty', name: '空目录', path: '实验/空目录', parent_id: 'root' },
  ] });
  try {
    await h.resolve([]);
    assert.deepEqual(h.state.rows.value.map(row => row.name), ['实验', '空目录']);
    assert.ok(h.state.rows.value.every(row => row.kind === 'directory' && !row.missing && row.fileCount === 0));
    assert.equal(h.state.loadedFileCount.value, 0);
    h.state.query.value = 'not found';
    void h.state.load(false, false);
    await h.resolve([]);
    assert.equal(h.state.rows.value.length, 0);
    assert.equal(h.state.folders.value.length, 2);
  } finally { h.close(); }
});

test('folder change event refreshes directories once and preserves selected analysis inputs', async () => {
  const h = harness({ selectedFiles: [file('selected')] });
  try {
    await h.resolve([asset('first')]);
    assert.equal(h.folderRequests.length, 1);
    h.setFolderItems([{ id: 'new', name: '新建文件夹', path: '新建文件夹', parent_id: null }]);
    h.listeners.get('data-library-changed')();
    await h.resolve([asset('first')]);
    assert.equal(h.folderRequests.length, 2);
    assert.ok(h.state.rows.value.some(row => row.kind === 'directory' && row.name === '新建文件夹'));
    assert.deepEqual([...h.state.selectedIds.value], ['selected']);
    h.setFolderItems([]);
    h.owner.value = { id: 'owner-b' };
    assert.equal(h.state.folders.value.length, 0);
    assert.equal(h.state.rows.value.length, 0);
    await h.resolve([]);
    assert.equal(h.state.rows.value.length, 0);
  } finally { h.close(); }
});
