import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import * as vue from 'vue';
import { compileScript, parse } from '@vue/compiler-sfc';
import ts from 'typescript';

function load(path, dependencies = {}) {
  const source = readFileSync(new URL(path, import.meta.url), 'utf8');
  const content = path.endsWith('.vue') ? compileScript(parse(source).descriptor, { id: 'file-manager-test' }).content : source;
  const code = ts.transpileModule(content, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
  const module = { exports: {} };
  new Function('require', 'module', 'exports', code)(name => {
    if (name in dependencies) return dependencies[name];
    if (name === 'vue') return { ...vue, onMounted() {}, onUnmounted() {} };
    if (name === 'lucide-vue-next' || name === './icons/LibraryEntryIcon.vue') return {};
    throw new Error(`Unexpected dependency ${name}`);
  }, module, module.exports);
  return module.exports;
}
const tree = load('../src/utils/personalDataTree.ts');
const utils = load('../src/utils/libraryFileManager.ts', { './personalDataTree': tree });
const filters = load('../src/utils/libraryFileFilters.ts', { './personalDataTree': tree });
const stamp = '2026-10-21T01:00:00Z';
const folder = (id, path, parent_id = null) => ({ id, path, parent_id, name: path.split('/').at(-1), created_at: stamp, updated_at: stamp });
const folders = [folder('raw', 'raw'), folder('year', 'raw/2026', 'raw'), folder('deep', 'raw/2026/daily', 'year'), folder('empty', 'empty')];
const file = (id, path) => ({ file_id: id, filename: `${id}.csv`, relative_path: path, size: 10, upload_date: stamp });
const asset = (id, files) => ({ id, name: id, files, status: 'ready', updated_at: stamp });

test('folder navigation includes empty folders and nested ancestors independently of file pages', () => {
  const roots = utils.libraryFolderTree([...folders, folder('unsafe', '/private/user')]);
  assert.deepEqual(roots.map(node => node.name), ['empty', 'raw']);
  assert.equal(roots[0].children.length, 0);
  assert.deepEqual(utils.libraryFolderBreadcrumbs(folders, 'deep').map(item => item.name), ['raw', '2026', 'daily']);
  assert.deepEqual(utils.libraryFolderBreadcrumbs(folders, 'root'), []);
  assert.deepEqual(utils.libraryFolderRows(roots, new Set(['raw', 'year'])).map(item => [item.name, item.depth]), [['empty', 0], ['raw', 0], ['2026', 1], ['daily', 2]]);
  assert.deepEqual(utils.libraryFolderRows(roots, new Set()).map(item => item.name), ['empty', 'raw']);
});

test('full server records are projected to direct children while preserving related files and provenance', () => {
  const records = [asset('a', [file('root'), file('direct', 'raw/direct.csv'), file('deep', 'raw/2026/deep.csv')]), asset('b', [file('other', 'raw/direct.csv')])];
  assert.deepEqual(utils.libraryCurrentFiles(records, '').map(item => item.file.file_id), ['root']);
  const raw = utils.libraryCurrentFiles(records, 'raw');
  assert.deepEqual(raw.map(item => item.file.file_id), ['direct', 'other']);
  assert.notEqual(raw[0].id, raw[1].id);
  assert.equal(raw[0].relatedFiles.length, 3);
  assert.deepEqual(raw.map(item => item.assetId), ['a', 'b']);
  assert.deepEqual(utils.libraryCurrentFiles(records, 'raw/2026').map(item => item.file.file_id), ['deep']);
  assert.deepEqual(utils.libraryCurrentFiles(records, 'empty'), []);
  assert.deepEqual(utils.libraryCurrentFiles(records, '../private'), []);
  const safe = utils.libraryCurrentFiles([asset('s', [{ ...file('s'), filename: '/private/user/s.csv', relative_path: '/private/user/s.csv', file_url: 'file:///private/user/s.csv', metadata: { host: '/private/user' } }])], '');
  assert.equal(safe[0].name, 's.csv');
  assert.doesNotMatch(JSON.stringify(safe), /private|file_url|metadata/);
});

function harness() {
  const props = vue.reactive({ folders, folderId: 'root', files: utils.libraryCurrentFiles([asset('a', [file('root')])], ''), selectedIds: [], detailOpen: false, loading: false, hasFilters: false });
  const events = [];
  const scope = vue.effectScope();
  const component = load('../src/components/LibraryFileManager.vue', { '../utils/libraryFileManager': utils, '../utils/libraryFileFilters': filters, '../utils/fileType': { formatFileSize: n => String(n) } }).default;
  const state = scope.run(() => component.setup(props, { expose() {}, emit: (...args) => events.push(args) }));
  return { props, state, events, close() { scope.stop(); } };
}

test('single file inspection is separate from preview and folders enter only through navigation', () => {
  const h = harness();
  try {
    const selected = h.props.files[0];
    h.state.inspectFile(selected);
    assert.deepEqual(h.events.map(([name]) => name), ['inspect']);
    h.state.previewFile(selected);
    assert.deepEqual(h.events.map(([name]) => name), ['inspect', 'inspect', 'preview']);
    assert.equal(h.events[2][1].file.file_id, 'root');
    h.state.focusFolder('raw');
    assert.equal(h.events.at(-1)[0], 'inspect');
    assert.equal(h.events.at(-1)[1], null);
    assert.equal(h.props.folderId, 'root');
    h.state.navigate('raw');
    assert.deepEqual(h.events.at(-1), ['navigate', 'raw']);
    assert.deepEqual(h.props.selectedIds, []);
    h.state.previewFile({ ...selected, missing: true });
    assert.deepEqual(h.events.at(-1), ['navigate', 'raw']);
  } finally { h.close(); }
});

test('breadcrumbs return to parent and context actions retain the target folder without navigation', async () => {
  const h = harness();
  try {
    h.props.folderId = 'deep';
    await vue.nextTick();
    assert.equal(h.state.parentId.value, 'year');
    assert.ok(h.state.expanded.value.has('raw'));
    h.state.context.value = { x: 0, y: 0, folderId: 'empty' };
    h.state.contextAction('create-folder');
    assert.deepEqual(h.events.at(-1), ['create-folder', 'empty']);
    assert.equal(h.state.context.value, null);
    assert.equal(h.props.folderId, 'deep');
    h.state.context.value = { x: 0, y: 0, folderId: 'root' };
    h.state.contextAction('upload');
    assert.deepEqual(h.events.at(-1), ['upload', 'root']);
    h.state.closeDetails();
    assert.deepEqual(h.events.at(-1), ['inspect', null]);
  } finally { h.close(); }
});
