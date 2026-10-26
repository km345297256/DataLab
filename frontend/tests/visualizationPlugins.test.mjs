import { viewModules } from './helpers/viewModules.mjs';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { test } from 'node:test';
import * as vue from 'vue';
import { compileScript, parse } from '@vue/compiler-sfc';
import ts from 'typescript';
import { parseVisualizationCatalog, matchingVisualizations, selectVisualization } from '../src/visualizations/contract.ts';
import { coordinateEdges, finiteExtent, seriesSegments } from '../src/visualizations/plot.ts';
import { readBoundedBinary, validateTiffDimensions } from '../src/visualizations/boundedBinary.ts';
import { usePreviewLoad } from '../src/composables/usePreviewLoad.ts';
import { VISUALIZATION_ADAPTERS } from '../src/visualizations/adapters.generated.ts';
import * as contentProfile from '../src/visualizations/contentProfile.ts';
import * as previewIdentity from '../src/visualizations/previewIdentity.ts';
import * as selection from '../src/visualizations/selection.ts';
import { ApiError, normalizeApiError } from '../src/utils/apiError.ts';

const descriptor = (overrides = {}) => { const spec = VISUALIZATION_ADAPTERS[overrides.adapter || 'tiff'] || VISUALIZATION_ADAPTERS.tiff; return ({ contract_version: 2, id: 'tiff', version: '1.0.0', name: 'TIFF', description: 'image', extensions: ['tif', 'tiff'], filenames: [], view_kind: spec.view_kind, adapter: 'tiff', reader: spec.readers[0], capabilities: spec.capabilities, default_enabled: true, enabled: true, priority: 20, permissions: ['file:read'], limits: { max_input_bytes: 64 * 1024 * 1024, max_output_bytes: 512 * 1024 }, ...overrides }); };
const catalogOf = (...plugins) => ({ engine: 'cordis', revision: 'rev1', plugins });
const ncMap = () => descriptor({ id: 'netcdf-map', extensions: ['nc'], adapter: 'scientific-map', reader: 'netcdf', view_kind: 'map' });
const ncSeries = () => descriptor({ id: 'netcdf-series', extensions: ['nc'], adapter: 'scientific-series', reader: 'netcdf', view_kind: 'series', priority: 10 });
const file = (id, extension = 'nc') => ({ file_id: id, filename: `${id}.${extension}`, upload_date: '', size: 200 });
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
const flush = async () => { for (let index = 0; index < 20; index++) await Promise.resolve(); await vue.nextTick(); };
const source = (path) => readFileSync(new URL(path, import.meta.url), 'utf8');

test('Cordis catalog supports several independently selectable views for the same format', () => {
  const catalog = parseVisualizationCatalog(catalogOf(ncSeries(), ncMap()));
  assert.deepEqual(matchingVisualizations(catalog.plugins, 'DATA.NC').map((item) => item.id), ['netcdf-map', 'netcdf-series']);
  assert.equal(selectVisualization(catalog.plugins, 'data.nc', null).id, 'netcdf-map');
  assert.equal(selectVisualization(catalog.plugins, 'data.nc', 'netcdf-series').id, 'netcdf-series');
  catalog.plugins[1].enabled = false;
  assert.equal(selectVisualization(catalog.plugins, 'data.nc', 'netcdf-map').id, 'netcdf-series');
});

test('stopped TIFF has no fallback and special filenames obey exactly the same enabled policy', () => {
  const molecular = descriptor({ id: 'molecular', adapter: 'molecular', view_kind: 'structure', extensions: ['cif'], filenames: ['POSCAR', 'CONTCAR'] });
  assert.equal(selectVisualization([molecular], 'POSCAR', null).id, 'molecular');
  assert.equal(selectVisualization([{ ...molecular, enabled: false }], 'POSCAR', null), null);
  assert.equal(selectVisualization([descriptor({ enabled: false })], 'a.TIF', null), null);
  assert.equal(selectVisualization([], 'a.png', null), null);
});

test('matching is deterministic, basename-scoped and never falls through to unrelated data', () => {
  const one = descriptor({ id: 'a', priority: 20 }), two = descriptor({ id: 'b', priority: 20 });
  assert.equal(selectVisualization([two, one], 'result.tif', null).id, 'a');
  assert.equal(selectVisualization([one], 'result.tif.js', null), null);
  assert.equal(selectVisualization([one], 'tif', null), null);
  assert.equal(selectVisualization([descriptor({ extensions: ['fastq.gz'] })], 'sample.fastq.gz', null).id, 'tiff');
});

test('default preview preserves product order while skipping whole-file and share restrictions', () => {
  const whole = descriptor({ id: 'map', priority: 100, limits: { max_input_bytes: 64, max_output_bytes: 1024 } });
  const window = descriptor({ id: 'window', adapter: 'array-window', priority: 10, limits: { max_input_bytes: 16, max_output_bytes: 1024 } });
  const page = descriptor({ id: 'page', adapter: 'csv', limits: { max_input_bytes: 8, max_output_bytes: 1024 } });
  assert.equal(selection.preferredVisualization([whole, window], { size: 32 }, false).id, 'map', 'small files keep the established default');
  assert.equal(selection.preferredVisualization([whole, window], { size: 1000 }, false).id, 'window', 'the window budget is not a source size limit');
  assert.equal(selection.preferredVisualization([whole, page], { size: 1000 }, false).id, 'page');
  assert.equal(selection.preferredVisualization([whole, window], { size: 1000 }, false, 'map').id, 'map', 'manual unavailable choices remain inspectable');
  assert.match(selection.visualizationBlockedReason(whole, { size: 1000 }, false), /上限/);
  const privateView = { ...whole, capabilities: { ...whole.capabilities, shared: false } };
  const sharedView = { ...whole, id: 'shared', capabilities: { ...whole.capabilities, shared: true } };
  assert.equal(selection.preferredVisualization([privateView, sharedView], { size: 32 }, true).id, 'shared');
  assert.equal(selection.preferredVisualization([privateView, sharedView], { size: 32 }, true, 'map').id, 'map');
  assert.match(selection.visualizationBlockedReason(privateView, { size: 32 }, true), /共享/);
  assert.equal(selection.preferredVisualization([], { size: 32 }, false), null);
});

test('content profile recommendations cannot override source-size eligibility or explicit user choice', () => {
  const tiff = descriptor({ limits: { max_input_bytes: 64, max_output_bytes: 1024 } });
  const geo = descriptor({ id: 'geotiff', adapter: 'openlayers', limits: { max_input_bytes: 32, max_output_bytes: 1024 } });
  const candidates = contentProfile.profileCandidates([tiff, geo], { container: 'tiff', traits: ['geotiff'] });
  assert.equal(candidates[0].id, 'geotiff');
  assert.equal(selection.preferredVisualization(candidates, { size: 40 }, false).id, 'tiff');
  assert.equal(selection.preferredVisualization(candidates, { size: 20 }, false).id, 'geotiff');
  assert.equal(selection.preferredVisualization(candidates, { size: 20 }, false, 'tiff').id, 'tiff');
});

test('every checked-in Cordis manifest is accepted by the frontend contract without reinterpretation', () => {
  const directory = new URL('../../plugin-host/visualizations/', import.meta.url);
  const plugins = readdirSync(directory).filter((name) => name.endsWith('.json')).map((name) => {
    const plugin = JSON.parse(readFileSync(new URL(name, directory), 'utf8'));
    return { ...plugin, enabled: plugin.default_enabled };
  });
  assert.equal(plugins.length, 84);
  for (const [filename,id] of [['sample.ibd','viz-mysql-sdi'],['sample.sst','viz-sst-records'],['sample.ldb','viz-sst-records']]) {
    assert.equal(selectVisualization(plugins, filename)?.id, id);
  }
  assert.equal(plugins.filter(plugin => plugin.contract_version === 2).length, 84);
  assert.ok(plugins.every(plugin => !Object.hasOwn(plugin, 'data_kind') && !plugin.adapter.startsWith('v2-')));
  assert.deepEqual(parseVisualizationCatalog(catalogOf(...plugins)).plugins, plugins);
});

test('database plugins match independently without taking over SQLite or the Shapefile default', () => {
  const directory = new URL('../../plugin-host/visualizations/', import.meta.url);
  const plugins = readdirSync(directory).filter(name => name.endsWith('.json')).map(name => {
    const plugin = JSON.parse(readFileSync(new URL(name, directory), 'utf8'));
    return { ...plugin, enabled: plugin.default_enabled };
  });
  for (const [filename, id] of [['sample.DUCKDB', 'viz-duckdb-table'], ['sample.ddb', 'viz-duckdb-table'],
    ['sample.mdb', 'viz-access-table'], ['sample.ACCDB', 'viz-access-table'], ['sample.sqlite', 'viz-sqlite-table'], ['sample.db', 'viz-sqlite-table']]) {
    assert.equal(selectVisualization(plugins, filename, null)?.id, id);
  }
  assert.equal(selectVisualization(plugins, 'sample.dbf', null)?.id, 'shapefile');
  assert.equal(selectVisualization(plugins, 'sample.dbf', 'viz-dbf-table')?.id, 'viz-dbf-table');
  const stopped = plugins.map(plugin => ({ ...plugin, enabled: plugin.enabled && plugin.id !== 'viz-duckdb-table' }));
  assert.equal(selectVisualization(stopped, 'sample.duckdb', null), null);
  assert.equal(selectVisualization(stopped, 'sample.mdb', null)?.id, 'viz-access-table');
  assert.equal(selectVisualization(stopped, 'sample.dbf', 'viz-dbf-table')?.id, 'viz-dbf-table');
});

test('dump and record plugins preserve ordinary SQL and archive defaults', () => {
  const directory = new URL('../../plugin-host/visualizations/', import.meta.url);
  const plugins = readdirSync(directory).filter(name => name.endsWith('.json')).map(name => {
    const plugin = JSON.parse(readFileSync(new URL(name, directory), 'utf8'));
    return { ...plugin, enabled: plugin.default_enabled };
  });
  assert.equal(selectVisualization(plugins, 'source.sql', null)?.id, 'text');
  assert.equal(selectVisualization(plugins, 'source.sql', 'viz-sql-dump')?.id, 'viz-sql-dump');
  assert.equal(selectVisualization(plugins, 'archive.tar', null)?.id, 'viz-archive-directory');
  assert.equal(selectVisualization(plugins, 'archive.tar', 'viz-postgres-dump')?.id, 'viz-postgres-dump');
  for (const [ext,id] of [['dump','viz-postgres-dump'],['bson','viz-bson'],['rdb','viz-redis-rdb']]) {
    assert.equal(selectVisualization(plugins, 'sample.'+ext, null)?.id, id);
    const stopped = plugins.map(p => ({...p,enabled:p.enabled&&p.id!==id}));
    assert.equal(selectVisualization(stopped, 'sample.'+ext, id), null);
  }
});

for (const [label, change] of [
  ['unknown contract', { contract_version: 3 }], ['obsolete contract', { contract_version: 1 }], ['remote adapter', { adapter: 'https://evil.example/script.js' }],
  ['write permission', { permissions: ['file:write'] }], ['wrong reader kind', { data_kind: 'scientific' }],
  ['wrong view', { view_kind: 'map' }], ['unbounded bytes', { limits: { max_input_bytes: -1, max_output_bytes: 1 } }],
]) test(`catalog fails closed for ${label}`, () => assert.throws(() => parseVisualizationCatalog(catalogOf(descriptor(change)))));

test('catalog rejects duplicate capability identities and untrusted engine', () => {
  assert.throws(() => parseVisualizationCatalog(catalogOf(descriptor(), descriptor())));
  assert.throws(() => parseVisualizationCatalog({ ...catalogOf(descriptor()), engine: 'legacy' }));
});

test('legacy renderer configs cannot execute arbitrary API/component entries or masquerade as images', () => {
  const registry = source('../src/renderers/registry.ts');
  assert.doesNotMatch(registry, /ImageFilePreview|preview:|apiClient|fetch\(/);
  const fileType = source('../src/utils/fileType.ts');
  assert.doesNotMatch(fileType, /FilePreview|findRendererByFilename|preview:/);
  const adapters = source('../src/visualizations/adapters.ts');
  assert.doesNotMatch(adapters.replace(/^\s*\/\/.*$/gm, ''), /import\((?:plugin|descriptor|manifest)|eval\(|new Function|api_url|iframe/);
});

test('plot preserves missing-value gaps, singleton points, and nonuniform map cell widths', () => {
  assert.deepEqual(seriesSegments([0, 1, 2, 3, 4], [2, null, 6, 7, null]), [[[0, 2]], [[2, 6], [3, 7]]]);
  assert.deepEqual(finiteExtent([null, 0, 10, NaN]), [0, 10]);
  assert.equal(finiteExtent([null, NaN]), null);
  assert.deepEqual(coordinateEdges([0, 10, 11]), [-5, 5, 10.5, 11.5]);
  assert.deepEqual(coordinateEdges([85, 75], -90, 90), [90, 80, 70]);
});

test('binary preview transfer enforces observed bytes even with a false Content-Length', async () => {
  let cancelled = false;
  const body = new ReadableStream({ start(controller) { controller.enqueue(new Uint8Array(10)); controller.enqueue(new Uint8Array(10)); }, cancel() { cancelled = true; } });
  await assert.rejects(readBoundedBinary(new Response(body, { headers: { 'content-length': '1' } }), 12, new AbortController().signal), /上限/);
  assert.equal(cancelled, true);
});

test('binary preview transfer cancellation releases a blocked stream and never returns old data', async () => {
  const controller = new AbortController();
  let cancelled = false;
  const body = new ReadableStream({ cancel() { cancelled = true; } });
  const pending = readBoundedBinary(new Response(body), 12, controller.signal);
  controller.abort();
  await assert.rejects(pending, { name: 'AbortError' });
  assert.equal(cancelled, true);
});

test('TIFF dimensions and unpacked bytes are checked before any decompression', () => {
  assert.deepEqual(validateTiffDimensions({ t256: [100], t257: [100], t258: [8, 8, 8] }), { width: 100, height: 100 });
  assert.throws(() => validateTiffDimensions({ t256: [100000], t257: [100000] }), /安全/);
  assert.throws(() => validateTiffDimensions({ t256: [4096], t257: [4096], t258: [64, 64, 64] }), /安全/);
  assert.throws(() => validateTiffDimensions({ t256: [10], t257: [10], t258: Array(100).fill(8) }), /安全/);
  const component = source('../src/components/filePreviews/TiffFilePreview.vue');
  assert.ok(component.indexOf('validateTiffDimensions(ifds[0])') < component.indexOf('UTIF.decodeImage(buffer'));
});

function compileSfc(path, dependencies, inlineTemplate = false) {
  dependencies={...viewModules, '../composables/useTaskData': { useTaskData: () => ({ active: vue.ref(false), locked: vue.ref(true), sessionId: vue.ref() }) }, '../utils/eventBus': { eventBus: { emit() {} } }, ...dependencies};
  const descriptor = parse(source(path)).descriptor;
  const code = compileScript(descriptor, { id: 'visualization-test', inlineTemplate }).content;
  const js = ts.transpileModule(code, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
  const module = { exports: {} };
  new Function('require', 'module', 'exports', js)((id) => {
    if (id === 'vue') return vue;
    assert.ok(id in dependencies, `Unexpected dependency ${id}`);
    return dependencies[id];
  }, module, module.exports);
  return module.exports.default;
}

function mountScientific(t, getData, plugin = ncSeries()) {
  const component = compileSfc('../src/visualizations/ScientificFilePreview.vue', {
    '../api/visualization': { getScientificVisualization: (file, _plugin, options, signal) => getData(file.file_id, options, signal) },
    '../composables/usePreviewLoad': { usePreviewLoad },
    './plot': { coordinateEdges, finiteExtent, seriesSegments, heatColor: () => '', tickLabel: String },
    './assets/world-outline.json': { default: { geometry: { coordinates: [] } } },
  });
  const props = vue.reactive({ file: file('a'), plugin });
  const scope = vue.effectScope();
  const state = scope.run(() => component.setup(props, { expose() {}, emit() {} }));
  t.after(() => scope.stop());
  return { props, state, unmount: () => scope.stop() };
}
const seriesResponse = (overrides = {}) => ({ kind: 'series', plugin_id: 'netcdf-series', revision: 'r', version: 'v1', reader: 'netcdf', variables: [{ name: 'temperature', dimensions: [{ name: 'time', size: 5 }, { name: 'station', size: 3 }], shape: [5, 3], units: 'K' }], selected_variable: 'temperature', x_label: 'time', y_label: 'temperature [K]', x: [0, 1], y: [1, 2], width: 0, height: 0, values: [], extent: null, metadata: { x_dimension: 'time', indices: { station: 0 } }, warnings: [], sampled: false, ...overrides });

for (const outcome of ['success', 'error']) test(`scientific plugin ignores late ${outcome} from a replaced file`, async (t) => {
  const old = deferred(), calls = [];
  const { props, state } = mountScientific(t, (id, options, signal) => { calls.push({ id, options, signal }); return id === 'a' ? old.promise : Promise.resolve(seriesResponse({ version: 'b' })); });
  props.file = file('b'); await flush();
  assert.equal(calls[0].signal.aborted, true);
  assert.equal(state.data.value.version, 'b');
  if (outcome === 'success') old.resolve(seriesResponse({ version: 'old' })); else old.reject(new Error('old'));
  await flush();
  assert.equal(state.data.value.version, 'b'); assert.equal(state.error.value, '');
});

test('scientific request cancellation on plugin disposal suppresses ignored-abort callbacks', async (t) => {
  const result = deferred(); let signal;
  const preview = mountScientific(t, (_id, _options, value) => { signal = value; return result.promise; });
  preview.unmount(); assert.equal(signal.aborted, true);
  result.resolve(seriesResponse()); await flush(); assert.equal(preview.state.data.value, null);
});

test('scientific axis and slice controls exclude the x axis and pin the file version', async (t) => {
  const calls = [];
  const { state } = mountScientific(t, async (_id, options) => { calls.push(options); return seriesResponse(); });
  await flush(); state.indices.value.station = 2; state.indices.value.time = 4;
  await state.load(false);
  assert.deepEqual(calls.at(-1), { plugin_id: 'netcdf-series', variable: 'temperature', x_dimension: 'time', indices: { station: 2 }, version: 'v1' });
  await state.load(true); assert.equal(calls.at(-1).version, undefined);
});

test('FITS HDU selection never sends NetCDF variable options or stale slice indices', async (t) => {
  const calls = [];
  const { state } = mountScientific(t, async (_id, options) => { calls.push(options); return seriesResponse({ plugin_id: 'fits-series', reader: 'fits', selected_variable: 'HDU 0', variables: [{ name: 'HDU 0', dimensions: [{ name: 'axis0', size: 3 }], shape: [3] }], metadata: { hdu: 0, hdus: [{ index: 0, shape: [3] }, { index: 1, shape: [5] }], x_dimension: 'axis0' } }); }, descriptor({ id: 'fits-series', adapter: 'scientific-series', view_kind: 'series', reader: 'fits' }));
  await flush(); state.hdu.value = 1; state.changeHdu(); await state.load(false);
  assert.deepEqual(calls.at(-1), { plugin_id: 'fits-series', hdu: 1, version: 'v1' });
});

test('plugin host actually unmounts old adapters on view switch, disable, file switch and close', async (t) => {
  const oldWindow = globalThis.window;
  globalThis.window = { addEventListener() {}, removeEventListener() {} };
  t.after(() => { if (oldWindow === undefined) delete globalThis.window; else globalThis.window = oldWindow; });
  const catalog = vue.ref(catalogOf(ncMap(), ncSeries()));
  const error = vue.ref('');
  const stale = vue.ref(false);
  let confirmRefresh = async () => true;
  const instances = [];
  const Adapter = vue.defineComponent({ props: ['file', 'plugin'], setup(props) {
    const item = { file: props.file.file_id, plugin: props.plugin.id, disposed: false };
    instances.push(item); vue.onScopeDispose(() => { item.disposed = true; }); return () => vue.h('canvas');
  } });
  const Host = compileSfc('../src/visualizations/VisualizationHost.vue', {
    'vue-router': { useRoute: () => ({ path: '/chat/1' }), RouterLink: { render: () => vue.h('a') } },
    './catalog': { useVisualizationCatalog: () => ({ catalog, error, stale, loading: vue.ref(false), refresh: () => confirmRefresh() }) },
    './contract': { matchingVisualizations, selectVisualization, viewKindLabel: (value) => value },
    './adapters': { getVisualizationAdapter: () => Adapter },
    './runtime': { requestVisualization: async () => { throw new Error('Unexpected profile request'); } },
    './contentProfile': contentProfile,
    './selection': selection,
    './previewIdentity': previewIdentity,
  }, true);
  const renderer = vue.createRenderer({
    createElement: (type) => ({ type, children: [], props: {} }), createText: (text) => ({ text }), createComment: (text) => ({ text }),
    insert: (node, parent) => { parent.children ??= []; parent.children.push(node); node.parent = parent; },
    remove: (node) => { if (node.parent) node.parent.children = node.parent.children.filter((value) => value !== node); },
    setElementText: (node, text) => { node.text = text; }, setText: (node, text) => { node.text = text; },
    parentNode: (node) => node.parent, nextSibling: () => null, patchProp: (node, key, _old, value) => { node.props[key] = value; },
  });
  const shown = vue.ref(true), currentFile = vue.ref(file('a'));
  const Root = { setup: () => () => shown.value ? vue.h(Host, { file: currentFile.value }) : null };
  const container = { children: [] }; const app = renderer.createApp(Root); app.mount(container); t.after(() => app.unmount());
  await flush(); assert.equal(instances[0].plugin, 'netcdf-map');
  function find(node, type) { if (node.type === type) return node; for (const child of node.children ?? []) { const found = find(child, type); if (found) return found; } }
  catalog.value = structuredClone(vue.toRaw(catalog.value)); await flush();
  assert.equal(instances.length, 1, 'equivalent polling objects keep the same scope');
  stale.value = true; await flush();
  assert.equal(instances.length, 1, 'temporary directory outages keep the displayed reader scope');
  assert.equal(instances[0].disposed, false);
  function allText(node) { return [node.text ?? '', ...(node.children ?? []).map(allText)].join(' '); }
  assert.match(allText(container), /已保留当前预览/);
  stale.value = false; await flush();
  assert.equal(instances.length, 1, 'an unchanged recovery does not discard zoom/slice state');
  let active = instances.at(-1);
  await find(container, 'button').props.onClick(); await flush();
  assert.equal(active.disposed, true, 'manual refresh explicitly reloads an unchanged file');
  assert.equal(instances.length, 2);
  confirmRefresh = async () => false;
  await find(container, 'button').props.onClick(); await flush();
  assert.equal(instances.length, 2, 'unconfirmed manual refresh cannot start a new reader');
  confirmRefresh = async () => { catalog.value.revision = 'rev2'; return true; };
  await find(container, 'button').props.onClick(); await flush();
  assert.equal(instances.length, 3, 'revision change during manual confirmation reloads once, not twice');
  active = instances.at(-1);
  catalog.value.plugins[0].limits.max_output_bytes /= 2; await flush();
  assert.equal(active.disposed, true, 'same-version capability/limit changes still replace the reader scope');
  active = instances.at(-1);
  find(container, 'select').props.onChange({ target: { value: 'netcdf-series' } }); await flush();
  assert.equal(active.disposed, true); assert.equal(instances.at(-1).plugin, 'netcdf-series');
  active = instances.at(-1);
  catalog.value.plugins[1].enabled = false; await flush(); assert.equal(active.disposed, true); assert.equal(instances.at(-1).plugin, 'netcdf-map');
  active = instances.at(-1);
  currentFile.value = file('b'); await flush(); assert.equal(active.disposed, true); assert.equal(instances.at(-1).file, 'b');
  catalog.value.plugins[0].enabled = false; await flush(); assert.equal(instances.at(-1).disposed, true);
  catalog.value.plugins[0].enabled = true; await flush();
  error.value = 'runtime unavailable'; catalog.value = null; await flush(); assert.equal(instances.at(-1).disposed, true);
  error.value = ''; catalog.value = catalogOf(ncMap()); await flush();
  shown.value = false; await flush(); assert.equal(instances.at(-1).disposed, true);
});

function catalogStore(api) {
  const code = ts.transpileModule(source('../src/visualizations/catalog.ts'), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
  const module = { exports: {} };
  const dependencies = { vue, '../api/visualization': api, './previewIdentity': previewIdentity, '../utils/apiError': { ApiError } };
  new Function('require', 'module', 'exports', code)((id) => {
    assert.ok(id in dependencies, `Unexpected catalog dependency ${id}`);
    return dependencies[id];
  }, module, module.exports);
  return module.exports.useVisualizationCatalog();
}

test('host profiles an eligible alternative and keeps a manual view when the profile resolves', async t => {
  const originalWindow = globalThis.window;
  globalThis.window = { addEventListener() {}, removeEventListener() {} };
  let app;
  t.after(() => { app?.unmount(); if (originalWindow === undefined) delete globalThis.window; else globalThis.window = originalWindow; });
  const tooSmall = descriptor({ id: 'too-small', priority: 30, limits: { max_input_bytes: 16, max_output_bytes: 1024 } });
  const available = descriptor({ id: 'available', limits: { max_input_bytes: 64, max_output_bytes: 1024 } });
  const geo = descriptor({ id: 'geo', adapter: 'openlayers', priority: 10, limits: { max_input_bytes: 64, max_output_bytes: 1024 } });
  const catalog = vue.ref(catalogOf(tooSmall, available, geo));
  const pending = deferred(), calls = [], instances = [];
  const Host = compileSfc('../src/visualizations/VisualizationHost.vue', {
    'vue-router': { useRoute: () => ({ path: '/chat/1' }) },
    './catalog': { useVisualizationCatalog: () => ({ catalog, error: vue.ref(''), stale: vue.ref(false), loading: vue.ref(false), refresh: async () => true }) },
    './contract': { matchingVisualizations, viewKindLabel: value => value },
    './adapters': { getVisualizationAdapter: () => vue.defineComponent({ props: ['plugin'], setup(props) { instances.push(props.plugin.id); return () => vue.h('canvas'); } }) },
    './runtime': { requestVisualization: (...args) => { calls.push(args); return pending.promise; } },
    './contentProfile': contentProfile,
    './selection': selection,
    './previewIdentity': previewIdentity,
  }, true);
  const renderer = vue.createRenderer({
    createElement: type => ({ type, children: [], props: {} }), createText: text => ({ text }), createComment: text => ({ text }),
    insert: (node, parent) => { parent.children ??= []; parent.children.push(node); node.parent = parent; },
    remove: node => { if (node.parent) node.parent.children = node.parent.children.filter(value => value !== node); },
    setElementText: (node, text) => { node.text = text; }, setText: (node, text) => { node.text = text; },
    parentNode: node => node.parent, nextSibling: () => null, patchProp: (node, key, _old, value) => { node.props[key] = value; },
  });
  const container = { children: [] };
  app = renderer.createApp(Host, { file: { ...file('profile', 'tif'), size: 40 } });
  app.mount(container);
  await flush();
  assert.equal(calls.length, 1);
  assert.equal(calls[0][1].id, 'available', 'an over-budget first profiler must not suppress an eligible one');
  function find(node, type) { if (node.type === type) return node; for (const child of node.children ?? []) { const found = find(child, type); if (found) return found; } }
  const chooser = find(container, 'select');
  assert.equal(chooser.props.value, 'available');
  assert.match(find(chooser, 'option').text, /暂不可用/);
  chooser.props.onChange({ target: { value: 'available' } }); await flush();
  pending.resolve({ kind: 'resources', revision: 'rev1', metadata: { purpose: 'content-profile' }, payload: { profile: {
    profile_version: 1, container: 'tiff', dialect: 'tiff', traits: ['geotiff'], evidence: [], bytes_read: 40, truncated: false,
  } } });
  await flush();
  assert.equal(find(container, 'select').props.value, 'available', 'a later profile recommendation must not override the manual view');
  assert.deepEqual(instances, ['available']);
  find(container, 'select').props.onChange({ target: { value: 'too-small' } }); await flush();
  assert.equal(find(container, 'select').props.value, 'too-small');
  assert.equal(instances.length, 1, 'manually chosen over-budget views show their reason without starting the renderer');
});

test('equal catalog confirmations preserve snapshot and descriptor references', async () => {
  const effective = catalogOf(descriptor(), ncSeries());
  const store = catalogStore({ getVisualizationCatalog: async () => structuredClone(effective) });
  assert.equal(await store.refresh(), true);
  const snapshot = store.catalog.value, first = snapshot.plugins[0];
  for (let index = 0; index < 5; index++) {
    // Descriptor object keys and matcher ordering have no semantic significance.
    effective.plugins[0] = Object.fromEntries(Object.entries(effective.plugins[0]).reverse());
    effective.plugins[0].extensions.reverse();
    await store.refresh();
    assert.equal(store.catalog.value, snapshot);
    assert.equal(store.catalog.value.plugins[0], first);
  }
});

for (const [label, change] of [
  ['enabled state', p => { p.enabled = false; }],
  ['version', p => { p.version = '2.0.0'; }],
  ['input limit', p => { p.limits.max_input_bytes /= 2; }],
  ['output limit', p => { p.limits.max_output_bytes /= 2; }],
  ['capability', p => { p.capabilities.shared = !p.capabilities.shared; }],
  ['matcher', p => { p.extensions.push('cdf'); }],
  ['priority', p => { p.priority++; }],
  ['name', p => { p.name = 'Updated'; }],
]) test(`same revision still publishes changed ${label} without replacing unrelated descriptors`, async () => {
  const effective = structuredClone(catalogOf(ncMap(), ncSeries()));
  const store = catalogStore({ getVisualizationCatalog: async () => structuredClone(effective) });
  await store.refresh();
  const snapshot = store.catalog.value, unrelated = snapshot.plugins[1];
  change(effective.plugins[0]);
  await store.refresh();
  assert.notEqual(store.catalog.value, snapshot);
  assert.equal(store.catalog.value.revision, snapshot.revision);
  assert.notEqual(store.catalog.value.plugins[0], snapshot.plugins[0]);
  assert.equal(store.catalog.value.plugins[1], unrelated);
});

test('catalog revision, removal and recovery from failed confirmation remain authoritative', async () => {
  let effective = catalogOf(ncMap(), ncSeries()), failed = false;
  const store = catalogStore({ getVisualizationCatalog: async () => {
    if (failed) throw new Error('offline');
    return structuredClone(effective);
  } });
  await store.refresh(); const first = store.catalog.value;
  effective.revision = 'rev2'; await store.refresh();
  assert.notEqual(store.catalog.value, first);
  assert.equal(store.catalog.value.plugins[0], first.plugins[0]);
  effective.plugins.pop(); await store.refresh(); assert.equal(store.catalog.value.plugins.length, 1);
  failed = true; assert.equal(await store.refresh(), false); assert.equal(store.catalog.value, null);
  failed = false; assert.equal(await store.refresh(), true); assert.equal(store.error.value, '');
  assert.notEqual(store.catalog.value.plugins[0], first.plugins[0], 'recovery must not reuse an invalidated preview scope');
});

for (const [label, failure] of [
  ['network disconnect', normalizeApiError({ request: {}, message: 'Network Error' })],
  ['timeout', normalizeApiError({ request: {}, code: 'ECONNABORTED' })],
  ['HTTP 503', new ApiError('service unavailable', { status: 503 })],
]) test(`a ${label} preserves confirmed content until authoritative recovery`, async () => {
  let effective = catalogOf(ncMap(), ncSeries()), outage = false;
  const store = catalogStore({ getVisualizationCatalog: async () => {
    if (outage) throw failure;
    return structuredClone(effective);
  } });
  await store.refresh(); const confirmed = store.catalog.value;
  outage = true;
  assert.equal(await store.refresh(), false);
  assert.equal(store.catalog.value, confirmed);
  assert.equal(store.stale.value, true);
  assert.equal(store.error.value, '');
  outage = false; await store.refresh();
  assert.equal(store.catalog.value, confirmed);
  assert.equal(store.stale.value, false);
  outage = true; await store.refresh();
  effective.plugins[0].enabled = false;
  outage = false; await store.refresh();
  assert.equal(store.catalog.value.plugins[0].enabled, false, 'reconnection must apply revocation');
  assert.equal(store.stale.value, false);
});

for (const [label, failure] of [
  ['unauthenticated', new ApiError('login required', { status: 401 })],
  ['forbidden', new ApiError('forbidden', { status: 403 })],
  ['account changed', new ApiError('account changed', { status: 409 })],
  ['invalid contract', new Error('invalid contract')],
]) test(`${label} clears even a previously stale catalog`, async () => {
  let rejected;
  const store = catalogStore({ getVisualizationCatalog: async () => {
    if (rejected) throw rejected;
    return catalogOf(descriptor());
  } });
  await store.refresh();
  rejected = new ApiError('offline', { status: 503 }); await store.refresh();
  assert.equal(store.stale.value, true);
  rejected = failure; await store.refresh();
  assert.equal(store.catalog.value, null);
  assert.equal(store.stale.value, false);
  assert.match(store.error.value, /暂停/);
});

test('an outage without prior confirmation never invents a stale catalog', async () => {
  const store = catalogStore({ getVisualizationCatalog: async () => { throw new ApiError('offline', { status: 503 }); } });
  await store.refresh();
  assert.equal(store.catalog.value, null);
  assert.equal(store.stale.value, false);
  assert.match(store.error.value, /暂停/);
});

test('overlapping page, preview and focus polls share one authoritative request', async () => {
  const response = deferred(); let reads = 0;
  const store = catalogStore({ getVisualizationCatalog: () => { reads++; return response.promise; } });
  const first = store.refresh(), second = store.refresh(), third = store.refresh();
  assert.equal(first, second); assert.equal(first, third); assert.equal(reads, 1);
  response.resolve(catalogOf(ncMap())); assert.deepEqual(await Promise.all([first, second, third]), [true, true, true]);
});

test('file identity excludes transport URLs and arbitrary metadata but tracks public content revisions', () => {
  const original = file('identity');
  const key = previewIdentity.filePreviewIdentity(original);
  assert.equal(previewIdentity.filePreviewIdentity({ ...original, file_url: 'https://signed.invalid/renewed',
    metadata: { arbitrary_path: '/private/never-a-preview-key', updated_status: 'ready' } }), key);
  assert.notEqual(previewIdentity.filePreviewIdentity({ ...original, metadata: { dataset_file_version: 'v2' } }), key);
  assert.notEqual(previewIdentity.filePreviewIdentity({ ...original, size: 201 }), key);
  assert.notEqual(previewIdentity.filePreviewIdentity({ ...original, filename: 'renamed.nc' }), key);
  assert.doesNotMatch(key, /signed|private/);
});

test('authoritative plugin state survives a late pre-toggle poll and network failure fails closed', async () => {
  const old = deferred(), stateResponse = deferred(); let readCount = 0;
  const store = catalogStore({ getVisualizationCatalog: () => { readCount++; return old.promise; }, setVisualizationEnabled: () => stateResponse.promise });
  const pendingPoll = store.refresh();
  const pendingStop = store.toggle('tiff', false);
  await store.refresh(); assert.equal(readCount, 1, 'polling cannot race an in-flight mutation');
  stateResponse.resolve(catalogOf(descriptor({ enabled: false }))); await pendingStop;
  old.resolve(catalogOf(descriptor({ enabled: true }))); await pendingPoll;
  assert.equal(store.catalog.value.plugins[0].enabled, false);
  const failure = catalogStore({ getVisualizationCatalog: async () => { throw new Error('network'); } });
  await failure.refresh(); assert.equal(failure.catalog.value, null); assert.match(failure.error.value, /暂停/);
});

test('a failed state confirmation never leaves a previously active renderer running', async () => {
  const store = catalogStore({ getVisualizationCatalog: async () => catalogOf(descriptor()), setVisualizationEnabled: async () => { throw new ApiError('offline', { status: 503 }); } });
  await store.refresh(); assert.equal(store.catalog.value.plugins[0].enabled, true);
  await store.toggle('tiff', false);
  assert.equal(store.catalog.value, null); assert.match(store.error.value, /未能确认/);
  assert.equal(store.stale.value, false, 'an uncertain mutation cannot use stale authority');
});

test('file preview host is destroyed when hidden, and signed share scientific readers stay closed', () => {
  const panel = source('../src/components/FilePanel.vue');
  assert.match(panel, /v-if="isShow && visible && fileInfo && fileType"/);
  assert.match(panel, /<VisualizationHost :key="fileInfo.file_id"/);
  const host = source('../src/visualizations/VisualizationHost.vue');
  assert.match(host, /visualizationBlockedReason\(selectedPlugin.value/);
  assert.doesNotMatch(host, /data_kind|startsWith\('v2-'/);
  assert.match(host, /catalog.value\?\.revision/);
  assert.doesNotMatch(host, /localStorage|sessionStorage|window\.open\(.*plugin|fileType.preview/);
});

test('host sends only the current loaded view to a private unlocked analysis and invalidates replacements', async t => {
  const plugin = ncSeries(), catalog = vue.ref(catalogOf(plugin)), events = [];
  const task = { active: vue.ref(true), locked: vue.ref(false), sessionId: vue.ref('session') };
  const route = vue.reactive({ path: '/chat/session' });
  const Host = compileSfc('../src/visualizations/VisualizationHost.vue', {
    'vue-router': { useRoute: () => route },
    './catalog': { useVisualizationCatalog: () => ({ catalog, error: vue.ref(''), stale: vue.ref(false), loading: vue.ref(false), refresh: async () => true }) },
    './contract': { matchingVisualizations, viewKindLabel: String }, './adapters': { getVisualizationAdapter: () => ({}) },
    './runtime': { requestVisualization: async () => { throw new Error('not expected'); } },
    './contentProfile': contentProfile, './selection': selection, './previewIdentity': previewIdentity,
    '../composables/useTaskData': { useTaskData: () => task }, '../utils/eventBus': { eventBus: { emit: (...args) => events.push(args) } },
  });
  const scope = vue.effectScope(), props = vue.reactive({ file: file('a') });
  const state = scope.run(() => Host.setup(props, { expose() {} })); t.after(() => scope.stop());
  const context = { schemaVersion: 1, fileId: 'a', version: 'a'.repeat(64), pluginId: plugin.id, pluginVersion: plugin.version, adapter: plugin.adapter, family: 'scientific',
    request: { operation: 'preview', params: { variable: 'temperature', x_dimension: 'time' } }, view: { kind: 'series', valueSemantics: 'physical', sampled: true } };
  state.receiveViewContext(context); state.addViewToAnalysis();
  assert.equal(events.length, 1); assert.equal(events[0][0], 'visualization-analysis-draft');
  assert.deepEqual(events[0][1].context, context);
  task.locked.value = true; state.addViewToAnalysis(); assert.equal(events.length, 1);
  task.locked.value = false; route.path = '/share/token'; state.addViewToAnalysis(); assert.equal(events.length, 1);
  route.path = '/chat/session'; catalog.value = { ...catalog.value, revision: 'new' }; await flush();
  assert.equal(state.viewContext.value, null);
  state.receiveViewContext({ ...context, fileId: 'foreign' }); assert.equal(state.viewContext.value, null);
  state.receiveViewContext(context); props.file = file('b'); await flush(); assert.equal(state.viewContext.value, null);
});
