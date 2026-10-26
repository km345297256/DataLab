import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import * as vue from 'vue';
import { compileScript, parse } from '@vue/compiler-sfc';
import ts from 'typescript';
import { readyExplorationFiles } from '../src/utils/explorationFiles.ts';

const source = readFileSync(new URL('../src/pages/HomePage.vue', import.meta.url), 'utf8');
const script = ts.transpileModule(compileScript(parse(source).descriptor, { id: 'analysis-home' }).content, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText;

function homeHarness(files = [], options = {}) {
  const mounted = [], unmounted = [], pendingChats = [], navigation = [], catalogRequests = [], discarded = [];
  const listeners = new Map();
  const scope = vue.effectScope();
  const module = { exports: {} };
  const imports = {
    vue: { ...vue, onMounted: callback => mounted.push(callback), onUnmounted: callback => unmounted.push(callback) },
    'vue-router': { useRouter: () => ({ push: async target => { navigation.push(target); return options.navigationFailure; } }) },
    'vue-i18n': { useI18n: () => ({ t: value => value }) },
    'lucide-vue-next': {},
    '../api/agent': { createSession: options.createSession ?? (async () => ({ session_id: 'new-analysis' })) },
    '../api/dataConnector': {
      listDataConnectors: async () => {
        catalogRequests.push('catalog');
        return [{ id: 'ncbi_datasets' }, { id: 'ncbi_geo' }, { id: 'ncbi_sra' }];
      },
    },
    '../utils/toast': { showErrorToast: message => assert.fail(message) },
    '../composables/useLeftPanel': { useLeftPanel: () => ({ toggleLeftPanel() {}, isLeftPanelShow: vue.ref(true) }) },
    '../composables/useFilePanel': { useFilePanel: () => ({ hideFilePanel() {} }) },
    '../composables/useAgentProfile': { useAgentProfile: () => ({ selectedProfile: vue.ref({ id: 'research' }) }) },
    '../composables/usePendingChat': { savePendingChat: pending => pendingChats.push(pending) },
    '../composables/useDataSelection': { takePendingLibraryFiles: () => files },
    '../composables/useNewAnalysis': { EVENT_START_NEW_ANALYSIS: 'datalab:new-analysis' },
    '../utils/eventBus': { eventBus: {
      on: (name, listener) => listeners.set(name, listener),
      off: name => listeners.delete(name),
    } },
    '../utils/explorationFiles': { readyExplorationFiles },
    '../composables/useAnalysisDraft': { useAnalysisDraft: (_key, _read, apply) => ({
      restore: () => { if (options.cachedDraft) apply(options.cachedDraft); },
      discard: () => { discarded.push(true); options.cachedDraft = undefined; },
      isCurrent: () => true,
    }) },
  };
  new Function('require', 'module', 'exports', script)(name => {
    if (name.endsWith('.vue')) return {};
    assert.ok(name in imports, `Unexpected home dependency: ${name}`);
    return imports[name];
  }, module, module.exports);
  const state = scope.run(() => module.exports.default.setup({}, { expose() {} }));
  return {
    state, pendingChats, navigation, catalogRequests, discarded, listeners,
    mount: () => Promise.all(mounted.map(callback => callback())),
    close: () => { unmounted.forEach(callback => callback()); scope.stop(); },
  };
}

test('explicit new analysis clears the editor and prevents an earlier create response reopening a task', async () => {
  let resolve;
  const h = homeHarness([], { createSession: () => new Promise(done => { resolve = done; }) });
  try {
    await h.mount();
    h.state.message.value = '旧问题';
    h.state.attachments.value = [{ file_id: 'old', filename: 'old.csv', upload_date: '' }];
    h.state.selectedSkills.value = ['quality'];
    h.state.selectedConnectors.value = ['chosen'];
    const submitting = h.state.handleSubmit();
    h.listeners.get('datalab:new-analysis')();
    assert.equal(h.state.message.value, '');
    assert.deepEqual(h.state.attachments.value, []);
    assert.deepEqual(h.state.selectedSkills.value, []);
    assert.deepEqual(h.state.selectedConnectors.value, []);
    assert.equal(h.state.isSubmitting.value, false);
    resolve({ session_id: 'previous-request' });
    await submitting;
    assert.deepEqual(h.navigation, []);
    assert.deepEqual(h.pendingChats, []);
  } finally { h.close(); }
  assert.equal(h.listeners.size, 0);
});

test('a new analysis starts without connectors and discovering data does not opt into any source', async () => {
  const h = homeHarness();
  try {
    await h.mount();
    assert.deepEqual(h.state.selectedConnectors.value, []);
    assert.deepEqual(h.catalogRequests, []);
    h.state.startDiscovery();
    assert.match(h.state.message.value, /查找开放数据/);
    assert.equal(h.state.dataDiscovery.value, true);
    assert.deepEqual(h.state.selectedConnectors.value, []);
    await h.state.handleSubmit();
    assert.deepEqual(h.pendingChats[0].dataConnectors, []);
    assert.equal(h.pendingChats[0].dataDiscovery, true);
    assert.deepEqual(h.navigation, [{ path: '/chat/new-analysis' }]);
    assert.equal(h.discarded.length, 1, 'Successful navigation consumes the new analysis draft');
  } finally { h.close(); }
});

test('a cancelled navigation retains the new-analysis draft for retry', async () => {
  const h = homeHarness([], { navigationFailure: { type: 'cancelled' } });
  try {
    await h.mount();
    h.state.message.value = '分析问题';
    await h.state.handleSubmit();
    assert.deepEqual(h.discarded, []);
    assert.equal(h.state.isSubmitting.value, false);
    assert.equal(h.state.message.value, '分析问题');
  } finally { h.close(); }
});

test('returning home during session creation prevents a late response from navigating back into analysis', async () => {
  let resolve;
  const h = homeHarness([], { createSession: () => new Promise(done => { resolve = done; }) });
  await h.mount();
  h.state.message.value = '分析问题';
  const submitting = h.state.handleSubmit();
  h.close();
  resolve({ session_id: 'late-session' });
  await submitting;
  assert.deepEqual(h.navigation, []);
  assert.deepEqual(h.pendingChats, []);
  assert.deepEqual(h.discarded, []);
});

test('personal-library handoff replaces the file selection while keeping the unsent question and capabilities', async () => {
  const selected = { file_id: 'selected', filename: 'selected.csv', upload_date: '' };
  const added = { file_id: 'added', filename: 'added.csv', upload_date: '' };
  const h = homeHarness([added], { cachedDraft: {
    message: '未发送的问题', files: [selected], skills: ['quality'], mcpServers: [], connectors: ['chosen'],
  } });
  try {
    await h.mount();
    assert.equal(h.state.message.value, '未发送的问题');
    assert.deepEqual(h.state.attachments.value.map(file => file.file_id), ['added']);
    assert.deepEqual([...h.state.selectedSkills.value], ['quality']);
    assert.deepEqual([...h.state.selectedConnectors.value], ['chosen']);
  } finally { h.close(); }
});

test('a full 32-file personal-library handoff cannot combine with another 32-file draft and exceed the analysis limit', async () => {
  const files = prefix => Array.from({ length: 32 }, (_, index) => ({
    file_id: `${prefix}-${index}`, filename: `${prefix}-${index}.csv`, upload_date: '',
  }));
  const incoming = files('chosen');
  const h = homeHarness(incoming, { cachedDraft: {
    message: '保留的问题', files: files('old'), skills: [], mcpServers: [], connectors: [],
  } });
  try {
    await h.mount();
    assert.equal(h.state.message.value, '保留的问题');
    assert.equal(h.state.attachments.value.length, 32);
    assert.deepEqual(h.state.attachments.value.map(file => file.file_id), incoming.map(file => file.file_id));
    await h.state.handleSubmit();
    assert.equal(h.pendingChats[0].files.length, 32);
  } finally { h.close(); }
});

test('an explicitly selected connector is preserved when starting analysis with saved data', async () => {
  const h = homeHarness([{ file_id: 'private-table', filename: 'my-data.csv', size: 40 }]);
  try {
    await h.mount();
    h.state.selectedConnectors.value = ['ncbi_geo'];
    await h.state.handleSubmit();
    assert.deepEqual(h.pendingChats[0].dataConnectors, ['ncbi_geo']);
    assert.deepEqual(h.pendingChats[0].files.map(file => file.file_id), ['private-table']);
    assert.equal(h.pendingChats[0].agentProfileId, 'research');
    assert.match(h.pendingChats[0].message, /检查这些数据/);
  } finally { h.close(); }
});


test('first submission freezes question, files, capabilities, source mode and profile before createSession resolves', async () => {
  let resolve;
  const h = homeHarness([], { createSession: () => new Promise(done => { resolve = done; }) });
  try {
    await h.mount();
    h.state.message.value = 'first question';
    h.state.attachments.value = [{ file_id: 'first', filename: 'first.csv', upload_date: '' }];
    h.state.selectedSkills.value = ['first-skill'];
    h.state.selectedConnectors.value = ['first-source'];
    h.state.dataDiscovery.value = true;
    const pending = h.state.handleSubmit();
    h.state.message.value = 'later question';
    h.state.attachments.value = [{ file_id: 'later', filename: 'later.csv' }];
    h.state.selectedSkills.value = ['later-skill'];
    h.state.selectedConnectors.value = ['later-source'];
    h.state.dataDiscovery.value = false;
    h.state.selectedProfile.value = { id: 'later-profile' };
    resolve({ session_id: 'new-analysis' }); await pending;
    assert.deepEqual(JSON.parse(JSON.stringify(h.pendingChats[0])), {
      sessionId: 'new-analysis', message: 'first question', files: [{ file_id: 'first', filename: 'first.csv', upload_date: '' }],
      skills: ['first-skill'], mcpServers: [], dataConnectors: ['first-source'], dataDiscovery: true, agentProfileId: 'research',
    });
  } finally { h.close(); }
});
