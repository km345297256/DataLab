import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import * as vue from 'vue';
import { compileScript, parse } from '@vue/compiler-sfc';
import ts from 'typescript';

const source = readFileSync(new URL('../src/pages/PersonalCenterPage.vue', import.meta.url), 'utf8');
const content = compileScript(parse(source).descriptor, { id: 'personal-center-test' }).content;
const compiled = ts.transpileModule(content, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText;

function harness(role = 'user') {
  const owner = vue.ref({ id: 'researcher-a', fullname: '研究员 A', email: 'a@example.test', role });
  const openedSettings = [];
  const dependencies = {
    vue,
    'vue-router': {},
    'lucide-vue-next': {},
    '../composables/useAuth': { useAuth: () => ({ currentUser: owner, isAdmin: vue.computed(() => owner.value?.role === 'admin') }) },
    '../composables/useLeftPanel': { useLeftPanel: () => ({ isLeftPanelShow: vue.ref(false), showLeftPanel() {} }) },
    '../composables/useSettingsDialog': { useSettingsDialog: () => ({ openSettingsDialog: tab => openedSettings.push(tab) }) },
  };
  const module = { exports: {} };
  new Function('require', 'module', 'exports', compiled)(name => {
    if (name in dependencies) return dependencies[name];
    throw new Error(`Unexpected capability or history fetch: ${name}`);
  }, module, module.exports);
  const scope = vue.effectScope();
  const state = scope.run(() => module.exports.default.setup({}, { expose() {} }));
  return { state, owner, openedSettings, close: () => scope.stop() };
}

test('researchers see data, skills, connectors and personal visualization settings without administrator capability', () => {
  const h = harness();
  try {
    assert.deepEqual(h.state.managementEntries.value.map(entry => entry.to), ['/my-data', '/skills', '/connectors', '/visualizations']);
    assert.equal(h.state.currentUser.value.email, 'a@example.test');
    h.state.openAccountSettings();
    assert.deepEqual(h.openedSettings, ['account']);
  } finally { h.close(); }
});

test('administrator entry follows the current identity immediately', () => {
  const h = harness('admin');
  try {
    assert.equal(h.state.managementEntries.value.at(-1).to, '/chat/admin');
    h.owner.value = { id: 'researcher-b', fullname: '研究员 B', role: 'user' };
    assert.equal(h.state.managementEntries.value.some(entry => entry.to === '/chat/admin'), false);
    assert.equal(h.state.currentUser.value.fullname, '研究员 B');
    h.owner.value = null;
    assert.equal(h.state.managementEntries.value.some(entry => entry.to === '/chat/admin'), false);
    assert.equal(h.state.avatarLetter.value, 'D');
  } finally { h.close(); }
});
