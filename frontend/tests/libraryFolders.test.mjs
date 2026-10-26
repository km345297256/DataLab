import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import * as vue from 'vue';
import ts from 'typescript';

function load(path, dependencies = {}) {
  const source = readFileSync(new URL(path, import.meta.url), 'utf8');
  const code = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
  const module = { exports: {} };
  new Function('require', 'module', 'exports', code)(name => {
    if (name in dependencies) return dependencies[name];
    if (name === 'vue') return vue;
    throw new Error(`Unexpected dependency ${name}`);
  }, module, module.exports);
  return module.exports;
}
const tree = load('../src/utils/personalDataTree.ts');
const folder = (id, path, parent_id = null) => ({ id, path, parent_id, name: path.split('/').at(-1), created_at: '2026-10-21T00:00:00Z', updated_at: '2026-10-21T00:00:00Z' });
const tick = async () => { await Promise.resolve(); await vue.nextTick(); await Promise.resolve(); };
function harness() {
  const reads = [], writes = [];
  const owner = vue.ref({ id: 'owner-a' });
  const scope = vue.effectScope();
  const { useLibraryFolders } = load('../src/composables/useLibraryFolders.ts', {
    '../api/dataLibrary': {
      listDataLibraryFolders: signal => new Promise((resolve, reject) => reads.push({ signal, resolve, reject })),
      createDataLibraryFolder: input => new Promise((resolve, reject) => writes.push({ input, resolve, reject })),
    },
    './useAuth': { useAuth: () => ({ currentUser: owner }) },
    '../utils/personalDataTree': tree,
  });
  return { owner, reads, writes, state: scope.run(useLibraryFolders), close() { scope.stop(); } };
}

test('empty directories are independent of file pages and merge with legacy path directories', () => {
  const directories = [folder('empty', '实验/待上传', 'parent'), folder('parent', '实验'), folder('unsafe', '/private/secret')];
  const file = { file_id: 'csv', filename: 'table.csv', relative_path: '实验/table.csv' };
  const nodes = tree.buildPersonalDataTree([{ id: 'record', name: 'not-a-folder', status: 'ready', files: [file] }], directories);
  assert.equal(nodes.length, 1);
  assert.equal(nodes[0].name, '实验');
  assert.equal(nodes[0].children[0].name, '待上传');
  assert.equal(nodes[0].children[0].fileCount, 0);
  assert.equal(nodes[0].children[0].missing, false);
  assert.equal(nodes[0].fileCount, 1);
  assert.equal(tree.buildPersonalDataTree([], directories)[0].id, nodes[0].id);
  assert.deepEqual(tree.buildPersonalDataTree([], directories)[0].assetIds, []);
});

test('directory reads cancel stale loads and clear every owner-bound field on account changes', async () => {
  const h = harness();
  try {
    const first = h.reads[0];
    h.owner.value = { id: 'owner-b' };
    assert.equal(first.signal.aborted, true);
    h.reads[1].resolve({ items: [folder('b', '自己的目录')] });
    await tick();
    first.resolve({ items: [folder('a', '其他人的目录')] });
    await tick();
    assert.deepEqual(h.state.folders.value.map(item => item.id), ['b']);
    h.owner.value = null;
    assert.deepEqual(h.state.folders.value, []);
    assert.equal(h.state.loading.value, false);
    assert.equal(h.state.error.value, '');
  } finally { h.close(); }
});

test('creating a folder invalidates a pre-creation snapshot and uses the selected parent', async () => {
  const h = harness();
  try {
    const stale = h.reads[0];
    const creating = h.state.createFolder({ name: '子目录', parent_id: 'parent' });
    assert.deepEqual(h.writes[0].input, { name: '子目录', parent_id: 'parent' });
    h.writes[0].resolve(folder('child', '实验/子目录', 'parent'));
    const result = await creating;
    assert.equal(result.id, 'child');
    assert.equal(stale.signal.aborted, true);
    stale.resolve({ items: [] });
    await tick();
    assert.deepEqual(h.state.folders.value.map(item => item.id), ['child']);
  } finally { h.close(); }
});

test('in-flight creation cannot publish another user directory after account switch or unmount', async () => {
  const h = harness();
  const creating = h.state.createFolder({ name: 'private', parent_id: null });
  h.owner.value = { id: 'owner-b' };
  h.writes[0].resolve(folder('private', 'private'));
  await assert.rejects(creating, /账号已切换/);
  assert.deepEqual(h.state.folders.value, []);
  h.close();
  h.reads[1].resolve({ items: [folder('late', 'late')] });
  await tick();
  assert.deepEqual(h.state.folders.value, []);
});

test('directory failure preserves the last same-owner navigation, exposes a safe error and retries', async () => {
  const h = harness();
  try {
    h.reads[0].resolve({ items: [folder('ok', '实验'), folder('unsafe', '/private/server/path')] });
    await tick();
    assert.deepEqual(h.state.folders.value.map(item => item.id), ['ok']);
    const retry = h.state.load();
    h.reads[1].reject(new Error('/private/server/secret'));
    await retry;
    assert.deepEqual(h.state.folders.value.map(item => item.id), ['ok']);
    assert.match(h.state.error.value, /读取文件夹/);
    assert.doesNotMatch(h.state.error.value, /private|secret/);
    const recovery = h.state.load();
    h.reads[2].resolve({ items: [] });
    await recovery;
    assert.equal(h.state.error.value, '');
    assert.deepEqual(h.state.folders.value, []);
  } finally { h.close(); }
});
