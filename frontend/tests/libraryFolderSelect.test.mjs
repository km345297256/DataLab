import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import * as vue from 'vue';
import { compileScript, parse } from '@vue/compiler-sfc';
import ts from 'typescript';

function harness(overrides = {}) {
  const source = readFileSync(new URL('../src/components/LibraryFolderSelect.vue', import.meta.url), 'utf8');
  const code = ts.transpileModule(compileScript(parse(source).descriptor, { id: 'folder-select' }).content, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  const scope = vue.effectScope(), cleanups = [], requests = [], events = [];
  const owner = vue.ref({ id: 'alice' });
  const folders = vue.ref([{ id: 'parent', parent_id: null, name: '实验', path: '实验' }, { id: 'empty', parent_id: 'parent', name: '空目录', path: '实验/空目录' }]);
  const loading = vue.ref(false), error = vue.ref('');
  const dependencies = {
    vue: { ...vue, useId: () => 'test', onUnmounted: callback => cleanups.push(callback) },
    'lucide-vue-next': {},
    '../composables/useAuth': { useAuth: () => ({ currentUser: owner }) },
    '../composables/useLibraryFolders': { useLibraryFolders: () => ({ folders, loading, error, load() {},
      createFolder: input => new Promise((resolve, reject) => requests.push({ input, resolve, reject })),
    }) },
  };
  const module = { exports: {} };
  new Function('require', 'module', 'exports', code)(name => {
    if (!(name in dependencies)) throw new Error(`Unexpected dependency ${name}`);
    return dependencies[name];
  }, module, module.exports);
  const props = vue.reactive({ modelValue: 'parent', disabled: false, ...overrides });
  const state = scope.run(() => module.exports.default.setup(props, { expose() {}, emit: (...args) => events.push(args) }));
  return { state, props, folders, loading, error, owner, events, requests,
    close() { cleanups.forEach(callback => callback()); scope.stop(); } };
}

test('folder selection includes complete empty directories and reports a selected logical path', () => {
  const h = harness({ modelValue: 'empty' });
  try {
    assert.deepEqual(h.state.sortedFolders.value.map(folder => folder.id), ['parent', 'empty']);
    assert.equal(h.state.selectedFolder.value.path, '实验/空目录');
    assert.equal(h.state.ready.value, true);
    assert.ok(h.events.some(event => event[0] === 'path-change' && event[1] === '实验/空目录'));
    h.state.selectFolder('');
    assert.deepEqual(h.events.at(-1), ['update:modelValue', null]);
    h.props.modelValue = 'deleted';
    assert.equal(h.state.ready.value, false);
  } finally { h.close(); }
});

test('creating a subfolder is single-flight and selects the server-confirmed directory', async () => {
  const h = harness();
  try {
    h.state.showCreate.value = true;
    h.state.folderName.value = ' 原始数据 ';
    const pending = h.state.create();
    await h.state.create();
    h.state.selectFolder('empty');
    assert.equal(h.requests.length, 1);
    assert.deepEqual(h.requests[0].input, { name: '原始数据', parent_id: 'parent' });
    assert.equal(h.events.filter(event => event[0] === 'update:modelValue').length, 0);
    h.requests[0].resolve({ id: 'created', parent_id: 'parent', name: '原始数据', path: '实验/原始数据' });
    await pending;
    assert.ok(h.events.some(event => event[0] === 'update:modelValue' && event[1] === 'created'));
    assert.equal(h.state.creating.value, false);
    assert.equal(h.state.showCreate.value, false);
    assert.equal(h.state.folderName.value, '');
  } finally { h.close(); }
});

test('folder conflicts retain the entered name and show a safe Chinese error', async () => {
  const h = harness();
  try {
    h.state.folderName.value = '已有目录';
    const pending = h.state.create();
    h.requests[0].reject({ response: { status: 409, data: { detail: '/private/source/secret' } } });
    await pending;
    assert.equal(h.state.folderName.value, '已有目录');
    assert.match(h.state.createError.value, /同名文件夹/);
    assert.doesNotMatch(h.state.createError.value, /private|secret/);
    assert.equal(h.state.creating.value, false);
  } finally { h.close(); }
});

test('unsafe names are rejected before persistence and root creation uses null parent', async () => {
  const h = harness({ modelValue: null });
  try {
    for (const name of ['../private', 'C:\\private', '..', 'bad\u0000name', '界'.repeat(86)]) {
      h.state.folderName.value = name;
      await h.state.create();
      assert.match(h.state.createError.value, /文件夹名称/);
    }
    assert.equal(h.requests.length, 0);
    h.state.folderName.value = '新目录';
    const pending = h.state.create();
    assert.equal(h.requests[0].input.parent_id, null);
    h.requests[0].resolve({ id: 'new', name: '新目录', path: '新目录', parent_id: null });
    await pending;
  } finally { h.close(); }
});

test('an old account creation response cannot select its private directory after account switch', async () => {
  const h = harness();
  try {
    h.state.folderName.value = '私人目录';
    const pending = h.state.create();
    h.owner.value = { id: 'bob' };
    assert.equal(h.state.folderName.value, '');
    h.requests[0].resolve({ id: 'private', name: '私人目录', path: '实验/私人目录', parent_id: 'parent' });
    await pending;
    assert.equal(h.events.filter(event => event[0] === 'update:modelValue').length, 0);
    assert.equal(h.state.creating.value, false);
  } finally { h.close(); }
});
