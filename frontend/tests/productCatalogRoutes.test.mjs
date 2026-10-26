import assert from 'node:assert/strict';
import { readFileSync, existsSync } from 'node:fs';
import test from 'node:test';
import * as vue from 'vue';
import { createMemoryHistory, createRouter } from 'vue-router';
import { compileScript, parse } from '@vue/compiler-sfc';
import ts from 'typescript';

const source = path => readFileSync(new URL(path, import.meta.url), 'utf8');

function productRouter() {
  const text = source('../src/router/index.ts');
  const ast = ts.createSourceFile('router', text, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS);
  const declaration = ast.statements.find(node => ts.isVariableStatement(node) && node.declarationList.declarations.some(item => item.name.getText(ast) === 'routes'));
  const code = ts.transpileModule(declaration.getText(ast), { compilerOptions: { module: ts.ModuleKind.None, target: ts.ScriptTarget.ES2022 } }).outputText;
  const scope = Object.fromEntries(['MainLayout', 'PersonalCenterPage', 'HomePage', 'ChatPage', 'ShareLayout', 'SharePage', 'PluginsPage', 'VisualizationSettingsPage', 'AdminPage'].map(name => [name, { name }]));
  const routes = new Function('scope', `with(scope) { ${code}; return routes; }`)(scope);
  return createRouter({ history: createMemoryHistory(), routes });
}

test('default entry and home bookmarks open analysis while management stays in personal center', () => {
  const router = productRouter();
  for (const path of ['/', '/home']) {
    const route = router.resolve(path);
    assert.equal(route.name, 'NewAnalysis');
    assert.equal(route.meta.analysisWorkspace, true);
    assert.equal(route.meta.requiresAuth, true);
  }
  for (const [path, name] of [['/chat', 'NewAnalysis'], ['/chat/saved-task', 'AnalysisTask']]) {
    const route = router.resolve(path);
    assert.equal(route.name, name);
    assert.equal(route.meta.analysisWorkspace, true);
    assert.equal(route.meta.requiresAuth, true);
  }
  assert.equal(router.resolve('/account').name, 'PersonalCenter');
  for (const path of ['/account', '/my-data', '/skills', '/connectors', '/visualizations', '/chat/visualizations', '/chat/admin', '/chat/admin/tasks/saved-task/replay']) {
    const route = router.resolve(path);
    assert.notEqual(route.meta.analysisWorkspace, true);
    assert.equal(route.meta.personalCenter, true);
    assert.equal(route.meta.requiresAuth, true);
  }
  assert.equal(router.resolve('/chat/admin').meta.adminOnly, true);
  assert.equal(router.resolve('/chat/admin/tasks/saved-task/replay').meta.adminOnly, true);
});

test('skill and connector routes select independent product identities, regardless of query tabs', () => {
  const router = productRouter();
  for (const [path, name, type] of [['/skills', 'SkillsLibrary', 'skills'], ['/chat/skills', 'SkillsLibrary', 'skills'], ['/connectors', 'DataConnectors', 'connectors'], ['/chat/connectors', 'DataConnectors', 'connectors']]) {
    const resolved = router.resolve(`${path}?tab=credentials&settings=agent-profiles`);
    assert.equal(resolved.name, name);
    assert.equal(resolved.meta.catalogType, type);
  }
});

test('legacy plugin bookmarks preserve visualization settings and discard hidden administration queries', async () => {
  const router = productRouter();
  for (const tab of ['mcp', 'runtime', 'presets', 'credentials', 'unknown', 'skills']) {
    await router.push(`/plugins?tab=${tab}&settings=agent-profiles`);
    assert.equal(router.currentRoute.value.path, '/skills');
    assert.deepEqual(router.currentRoute.value.query, {});
  }
  await router.push('/plugins?tab=connectors&settings=agent-profiles');
  assert.equal(router.currentRoute.value.path, '/connectors');
  assert.deepEqual(router.currentRoute.value.query, {});
  for (const base of ['/plugins', '/chat/plugins']) {
    await router.push(`${base}?tab=renderers&settings=agent-profiles`);
    assert.equal(router.currentRoute.value.path, '/visualizations');
    assert.equal(router.currentRoute.value.name, 'VisualizationSettings');
    assert.equal(router.currentRoute.value.meta.requiresAuth, true);
    assert.notEqual(router.currentRoute.value.meta.adminOnly, true);
    assert.deepEqual(router.currentRoute.value.query, {});
  }
});

function catalogHarness(type = 'skills') {
  const route = vue.reactive({ meta: { catalogType: type }, query: {} });
  const requests = [];
  const text = source('../src/pages/PluginsPage.vue');
  const script = compileScript(parse(text).descriptor, { id: 'product-catalog' });
  const code = ts.transpileModule(script.content, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
  const module = { exports: {} };
  const scope = vue.effectScope();
  new Function('require', 'module', 'exports', code)(name => {
    if (name === 'vue') return vue;
    if (name === 'vue-router') return { useRoute: () => route };
    if (name === 'vue-i18n') return { useI18n: () => ({ t: text => text }) };
    if (name === 'marked') return { marked: { setOptions() {}, parse: text => text } };
    if (name === 'dompurify') return { default: { sanitize: text => text } };
    if (name === '@/composables/useLeftPanel') return { useLeftPanel: () => ({ isLeftPanelShow: vue.ref(false), toggleLeftPanel() {} }) };
    if (name === '@/composables/useAuth') return { useAuth: () => ({ currentUser: vue.ref({ id: 'researcher', role: 'user' }) }) };
    if (name === '@/composables/usePersonalSkillDeletion') return { usePersonalSkillDeletion: () => ({ canDeletePersonalSkill: () => false, deletingSkillId: vue.ref(null), skillToDelete: vue.ref(null), deleteError: vue.ref(''), requestDeleteSkill() {}, confirmDeleteSkill() {}, cancelDeleteSkill() {} }) };
    if (name === '@/utils/skillCatalog') return { skillCategories: {}, filterSkillCatalog: skills => skills };
    if (name === '@/api/skill') return { listSkillCatalog: async () => { requests.push('skills'); return []; } };
    if (name === '@/utils/toast') return { showErrorToast() {}, showSuccessToast() {} };
    if (name.endsWith('.vue') || name === 'lucide-vue-next' || name === '@/components/ui/dialog') return {};
    assert.fail(`Hidden capability unexpectedly loaded: ${name}`);
  }, module, module.exports);
  const state = scope.run(() => module.exports.default.setup({}, { expose() {} }));
  return { route, state, requests, close: () => scope.stop() };
}

test('catalog ignores hidden tabs and loads only skills when switching from connectors', async () => {
  const h = catalogHarness('connectors');
  try {
    assert.equal(h.state.pageTitle.value, '数据连接器');
    assert.deepEqual(h.requests, []);
    for (const tab of ['mcp', 'renderers', 'runtime', 'presets', 'credentials']) {
      h.route.query = { tab, settings: 'agent-profiles' };
      await vue.nextTick();
      assert.equal(h.state.activeTab.value, 'connectors');
      assert.deepEqual(h.requests, []);
    }
    h.route.meta.catalogType = 'skills';
    await vue.nextTick();
    assert.equal(h.state.pageTitle.value, '分析技能库');
    assert.deepEqual(h.requests, ['skills']);
    h.state.skillDetailOpen.value = true;
    h.route.meta.catalogType = 'connectors';
    await vue.nextTick();
    assert.equal(h.state.skillDetailOpen.value, false);
  } finally { h.close(); }
});

test('institutional pages and their dedicated API are removed while ordinary file previews remain', () => {
  for (const path of ['../src/pages/DatasetSeekPage.vue', '../src/pages/DatasetManagementPage.vue', '../src/pages/DatasetSetupPage.vue', '../src/api/dataset.ts', '../src/components/DataProductDialog.vue']) {
    assert.equal(existsSync(new URL(path, import.meta.url)), false);
  }
  assert.doesNotMatch(source('../src/router/index.ts'), /DatasetSeekPage|DatasetManagementPage|DatasetSetupPage/);
  assert.ok(existsSync(new URL('../src/components/FilePanel.vue', import.meta.url)));
  assert.ok(existsSync(new URL('../src/pages/SharePage.vue', import.meta.url)));
  assert.doesNotMatch(source('../src/visualizations/VisualizationHost.vue'), /管理可视化插件|\/plugins\?tab=/);
  assert.doesNotMatch(source('../src/components/DataConnectorsPanel.vue'), /管理 MCP 服务|\/plugins\?tab=/);
});
