import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import * as vue from 'vue';
import { renderToString } from '@vue/server-renderer';
import { compileScript, parse } from '@vue/compiler-sfc';
import ts from 'typescript';

const source = readFileSync(new URL('../src/visualizations/VisualizationPluginsPanel.vue', import.meta.url), 'utf8');
const names = { table: '表格', text: '文本', series: '数值曲线' };
const fixture = (name, enabled = true) => ({
  ...JSON.parse(readFileSync(new URL(`../../plugin-host/visualizations/${name}.json`, import.meta.url), 'utf8')),
  enabled,
});

function harness({ inlineTemplate = false, stale = false } = {}) {
  const catalog = vue.ref({ engine: 'cordis', revision: 'confirmed', plugins: [fixture('csv'), fixture('text'), fixture('fastq-quality', false), fixture('edf-signals')] });
  const refreshed = [];
  const toggled = [];
  const mounted = [];
  const registry = { catalog, loading: vue.ref(false), error: vue.ref(''), stale: vue.ref(stale), updating: vue.ref(null), refresh: () => refreshed.push(true), toggle: (id, enabled) => toggled.push([id, enabled]) };
  const script = compileScript(parse(source).descriptor, { id: 'visualization-settings-test', inlineTemplate });
  const code = ts.transpileModule(script.content, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
  const module = { exports: {} };
  new Function('require', 'module', 'exports', code)(name => {
    if (name === 'vue') return inlineTemplate ? vue : { ...vue, onMounted: callback => mounted.push(callback) };
    if (name === 'lucide-vue-next') return { Search: { render: () => vue.h('span') } };
    if (name === './catalog') return { useVisualizationCatalog: () => registry };
    if (name === './contract') return { viewKindLabel: kind => names[kind] ?? kind };
    throw new Error(`Unexpected settings dependency: ${name}`);
  }, module, module.exports);
  const props = vue.reactive({ query: '' });
  const scope = vue.effectScope();
  const component = module.exports.default;
  const state = inlineTemplate ? null : scope.run(() => component.setup(props, { expose() {} }));
  return { state, props, registry, refreshed, toggled, mounted, component, close: () => scope.stop() };
}

test('visualization settings combine format search, type and account state filters', () => {
  const h = harness();
  try {
    assert.equal(h.state.enabledCount.value, 3);
    assert.equal(h.state.filteredPlugins.value.length, 4);
    h.state.searchQuery.value = ' .CSV ';
    assert.deepEqual(h.state.filteredPlugins.value.map(plugin => plugin.id), ['csv']);
    h.state.resetFilters();
    h.state.viewKind.value = 'series';
    h.state.stateFilter.value = 'disabled';
    assert.deepEqual(h.state.filteredPlugins.value.map(plugin => plugin.id), ['fastq-quality']);
    h.registry.catalog.value.plugins.find(plugin => plugin.id === 'fastq-quality').enabled = true;
    assert.equal(h.state.enabledCount.value, 4);
    assert.equal(h.state.filteredPlugins.value.length, 0);
    h.state.resetFilters();
    h.props.query = 'EDF';
    h.state.searchQuery.value = '生理';
    assert.deepEqual(h.state.filteredPlugins.value.map(plugin => plugin.id), ['viz-edf-signals']);
    h.mounted.forEach(callback => callback());
    assert.equal(h.refreshed.length, 1);
  } finally { h.close(); }
});

test('reading budgets describe pages, prefixes and windows without claiming whole-file support', () => {
  const h = harness();
  try {
    assert.match(h.state.budgetLabel(fixture('csv')), /每页最多读取 128 KiB/);
    assert.match(h.state.budgetLabel(fixture('fastq-quality')), /开头 16 MiB.*结果不代表全文件/);
    assert.match(h.state.budgetLabel(fixture('edf-signals')), /每次选区或时间窗累计读取最多 8 MiB.*源大小、解码和结果另有限制/);
    assert.match(h.state.budgetLabel(fixture('image')), /整文件读取预算 32 MiB.*解码和结果另有限制/);
  } finally { h.close(); }
});

test('rendered settings explain personal scope and disable changes until a stale catalog is refreshed', async () => {
  const h = harness({ inlineTemplate: true, stale: true });
  try {
    const html = await renderToString(vue.createSSRApp(h.component, { query: '' }));
    assert.match(html, /开关保存在当前账号/);
    assert.match(html, /开关不会改变文件访问权限/);
    assert.doesNotMatch(html, /本机共享配置|无外部地图请求/);
    assert.match(html, /当前显示上次确认的设置/);
    const switches = html.match(/<button[^>]*role="switch"[^>]*>/g);
    assert.equal(switches.length, 4);
    assert.ok(switches.every(button => /\sdisabled(?:[\s=>]|$)/.test(button)));
    assert.match(html, /支持已授权的共享预览/);
    assert.match(html, /共享页不提供此视图/);
    h.registry.stale.value = false;
    const currentHtml = await renderToString(vue.createSSRApp(h.component, { query: '' }));
    assert.ok(currentHtml.match(/<button[^>]*role="switch"[^>]*>/g).every(button => !/\sdisabled(?:[\s=>]|$)/.test(button)));
  } finally { h.close(); }
});
