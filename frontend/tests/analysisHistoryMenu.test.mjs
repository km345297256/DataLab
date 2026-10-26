import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import * as vue from 'vue';
import { compileScript, parse } from '@vue/compiler-sfc';
import mitt from 'mitt';
import ts from 'typescript';
import { createAnalysisDraftStore, emptyAnalysisDraft } from '../src/utils/analysisDrafts.ts';

const refreshEvent = 'EVENT_REFRESH_SESSION_LIST';
const renamedEvent = 'EVENT_SESSION_RENAMED';
const flush = async () => { await Promise.resolve(); await vue.nextTick(); await Promise.resolve(); };

function loadModule(path, dependencies) {
  const source = readFileSync(new URL(path, import.meta.url), 'utf8');
  const content = path.endsWith('.vue') ? compileScript(parse(source).descriptor, { id: 'analysis-history-test' }).content : source;
  const code = ts.transpileModule(content, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  const module = { exports: {} };
  new Function('require', 'module', 'exports', code)(name => {
    if (name in dependencies) return dependencies[name];
    throw new Error(`Unexpected dependency: ${name}`);
  }, module, module.exports);
  return module.exports;
}

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

const session = (id, updated = 1) => ({
  session_id: id,
  title: `分析 ${id}`,
  status: 'completed',
  latest_message_at: updated,
  unread_message_count: 0,
  is_owner: true,
});

function harness() {
  const scope = vue.effectScope();
  const mounted = [], unmounted = [];
  const requests = [], renames = [], deletions = [], confirmations = [], navigation = [], toasts = [], events = [];
  const currentUser = vue.ref({ id: 'researcher-a' });
  const route = vue.reactive({ fullPath: '/chat/task-a', params: { sessionId: 'task-a' } });
  const eventBus = mitt();
  eventBus.on('*', (type, payload) => events.push({ type, payload }));
  let newAnalyses = 0;
  let closed = false;
  const component = loadModule('../src/components/AnalysisHistoryMenu.vue', {
    vue: { ...vue, onMounted: callback => mounted.push(callback), onUnmounted: callback => unmounted.push(callback) },
    'vue-router': { useRoute: () => route, useRouter: () => ({ push: async path => navigation.push(path) }) },
    'lucide-vue-next': {},
    './ui/popover': {},
    '../api/agent': {
      getSessions: () => { const request = deferred(); requests.push(request); return request.promise; },
      updateSessionTitle: (id, title) => { const request = { id, title, ...deferred() }; renames.push(request); return request.promise; },
      deleteSession: id => { const request = { id, ...deferred() }; deletions.push(request); return request.promise; },
    },
    '../composables/useAuth': { useAuth: () => ({ currentUser }) },
    '../composables/useDialog': { useDialog: () => ({ showConfirmDialog: options => confirmations.push(options) }) },
    '../composables/useNewAnalysis': { useNewAnalysis: () => ({ startNewAnalysis: () => { newAnalyses++; } }) },
    '../constants/event': { EVENT_REFRESH_SESSION_LIST: refreshEvent, EVENT_SESSION_RENAMED: renamedEvent },
    '../utils/eventBus': { eventBus },
    '../utils/toast': { showErrorToast: message => toasts.push(message) },
    '../types/response': { SessionStatus: { RUNNING: 'running' } },
  }).default;
  const state = scope.run(() => component.setup({}, { expose() {} }));
  mounted.forEach(callback => callback());
  return {
    state, currentUser, route, eventBus, requests, renames, deletions, confirmations, navigation, toasts, events,
    get newAnalyses() { return newAnalyses; },
    async open() { state.open.value = true; await flush(); },
    async resolve(items, index = requests.length - 1) { requests[index].resolve({ sessions: items }); await flush(); },
    close() {
      if (closed) return;
      closed = true;
      unmounted.forEach(callback => callback());
      scope.stop();
    },
  };
}

test('history loads when opened and sorts latest activity without reordering its source', async () => {
  const h = harness();
  try {
    assert.equal(h.requests.length, 0);
    await h.open();
    assert.equal(h.requests.length, 1);
    assert.equal(h.state.loading.value, true);
    await h.resolve([session('old', 3), session('new', 30), session('undated', undefined)]);
    assert.deepEqual(h.state.sortedSessions.value.map(item => item.session_id), ['new', 'old', 'undated']);
    assert.deepEqual(h.state.sessions.value.map(item => item.session_id), ['old', 'new', 'undated']);
    assert.equal(h.state.loading.value, false);
    h.state.open.value = false;
    await flush();
    h.eventBus.emit(refreshEvent);
    assert.equal(h.requests.length, 1, 'closed history does not fetch on background refresh events');
    await h.open();
    assert.equal(h.requests.length, 2, 'reopening obtains current task status');
    await h.resolve([session('refreshed', 40)]);
  } finally { h.close(); }
});

test('account changes clear visible history and ignore responses started for the previous account', async () => {
  const h = harness();
  try {
    await h.open();
    await h.resolve([session('private-a')]);
    const oldLoad = h.state.loadSessions();
    h.currentUser.value = { id: 'researcher-b' };
    assert.deepEqual([...h.state.sessions.value], []);
    assert.equal(h.state.open.value, false);
    assert.equal(h.state.loading.value, false);
    await flush();
    await h.open();
    assert.equal(h.requests.length, 3);
    await h.resolve([session('late-private-a')], 1);
    await oldLoad;
    assert.deepEqual([...h.state.sessions.value], []);
    assert.equal(h.state.loading.value, true, 'old response cannot clear the new account loading state');
    await h.resolve([session('private-b')], 2);
    assert.deepEqual(h.state.sessions.value.map(item => item.session_id), ['private-b']);
  } finally { h.close(); }
});

test('unmount removes event listeners and ignores late history success or failure', async () => {
  for (const fail of [false, true]) {
    const h = harness();
    await h.open();
    h.close();
    assert.equal(h.eventBus.all.get(refreshEvent)?.length ?? 0, 0);
    assert.equal(h.eventBus.all.get(renamedEvent)?.length ?? 0, 0);
    if (fail) h.requests[0].reject(new Error('late failure'));
    else h.requests[0].resolve({ sessions: [session('late-private')] });
    await flush();
    assert.deepEqual([...h.state.sessions.value], []);
    assert.equal(h.state.error.value, '');
    h.eventBus.emit(refreshEvent);
    assert.equal(h.requests.length, 1);
  }
});

test('rename events update the matching item and successful edits notify the rest of the workspace', async () => {
  const h = harness();
  try {
    await h.open();
    await h.resolve([session('task-a'), session('task-b')]);
    for (const payload of [null, { sessionId: 'task-a', title: 12 }, { title: 'invalid' }]) {
      h.eventBus.emit(renamedEvent, payload);
    }
    assert.equal(h.state.sessions.value[0].title, '分析 task-a');
    h.eventBus.emit(renamedEvent, { sessionId: 'task-a', title: '外部更新的名称' });
    assert.equal(h.state.sessions.value[0].title, '外部更新的名称');
    assert.equal(h.state.sessions.value[1].title, '分析 task-b');
    const item = h.state.sessions.value[0];
    h.state.beginRename(item);
    h.state.renameDraft.value = '  新名称  ';
    const save = h.state.saveRename(item);
    assert.equal(h.renames[0].title, '新名称');
    assert.equal(item.title, '外部更新的名称', 'failed or pending requests must not rename the visible task');
    h.renames[0].resolve();
    await save;
    assert.equal(item.title, '新名称');
    assert.equal(h.state.renamingId.value, null);
    assert.deepEqual(h.events.at(-1), { type: renamedEvent, payload: { sessionId: 'task-a', title: '新名称' } });
  } finally { h.close(); }
});

test('deleting the current task waits for confirmation and server success before navigation', async () => {
  const h = harness();
  try {
    await h.open();
    await h.resolve([session('task-a'), session('task-b')]);
    h.state.confirmDelete(h.state.sessions.value[0]);
    assert.equal(h.state.open.value, false);
    assert.equal(h.deletions.length, 0, 'opening the confirmation does not delete data');
    const deletion = h.confirmations[0].onConfirm();
    assert.equal(h.deletions[0].id, 'task-a');
    assert.deepEqual(h.navigation, []);
    assert.equal(h.state.sessions.value.length, 2);
    h.deletions[0].resolve();
    await deletion;
    assert.deepEqual(h.navigation, ['/chat']);
    assert.deepEqual(h.state.sessions.value.map(item => item.session_id), ['task-b']);
    assert.ok(h.events.some(item => item.type === refreshEvent));
  } finally { h.close(); }
});

test('failed deletion keeps the current task and deleting another task does not navigate', async () => {
  const h = harness();
  try {
    await h.open();
    await h.resolve([session('task-a'), session('task-b')]);
    h.state.confirmDelete(h.state.sessions.value[0]);
    const failed = h.confirmations[0].onConfirm();
    h.deletions[0].reject(new Error('request failed'));
    await failed;
    assert.deepEqual(h.navigation, []);
    assert.equal(h.state.sessions.value.length, 2);
    assert.deepEqual(h.toasts, ['删除失败，请重试。']);
    assert.equal(h.events.some(item => item.type === refreshEvent), false);
    h.state.confirmDelete(h.state.sessions.value[1]);
    const other = h.confirmations[1].onConfirm();
    h.deletions[1].resolve();
    await other;
    assert.deepEqual(h.navigation, []);
    assert.deepEqual(h.state.sessions.value.map(item => item.session_id), ['task-a']);
  } finally { h.close(); }
});

test('account switches invalidate pending delete confirmations and late delete results', async () => {
  const h = harness();
  try {
    await h.open();
    await h.resolve([session('task-a')]);
    h.state.confirmDelete(h.state.sessions.value[0]);
    h.currentUser.value = { id: 'researcher-b' };
    await h.confirmations[0].onConfirm();
    assert.equal(h.deletions.length, 0);
    await h.open();
    await h.resolve([session('task-a')]);
    h.state.confirmDelete(h.state.sessions.value[0]);
    const deletion = h.confirmations[1].onConfirm();
    h.currentUser.value = { id: 'researcher-c' };
    h.deletions[0].resolve();
    await deletion;
    assert.deepEqual(h.navigation, []);
    assert.equal(h.events.some(item => item.type === refreshEvent), false);
  } finally { h.close(); }
});

test('new analysis closes history and uses the shared new-analysis helper', async () => {
  const h = harness();
  try {
    await h.open();
    await h.resolve([]);
    h.state.newAnalysis();
    assert.equal(h.state.open.value, false);
    assert.equal(h.newAnalyses, 1);
    assert.deepEqual(h.navigation, []);
  } finally { h.close(); }
});

test('new-analysis helper removes only the new draft and broadcasts before navigation', async () => {
  const store = createAnalysisDraftStore();
  store.setOwner('researcher-a');
  const access = store.open('researcher-a');
  access.save('new', { ...emptyAnalysisDraft(), message: '未开始的分析' });
  access.save('session:task-a', { ...emptyAnalysisDraft(), message: '现有任务尚未发送的问题' });
  const order = [];
  const navigation = deferred();
  const { useNewAnalysis, EVENT_START_NEW_ANALYSIS } = loadModule('../src/composables/useNewAnalysis.ts', {
    'vue-router': { useRouter: () => ({ push: path => { order.push(['navigate', path]); return navigation.promise; } }) },
    './useAuth': { useAuth: () => ({ currentUser: vue.ref({ id: 'researcher-a' }) }) },
    '../utils/analysisDrafts': { analysisDraftStore: store },
    '../utils/eventBus': {
      eventBus: { emit: event => {
        assert.equal(access.read('new'), undefined, 'the cached new draft is cleared before listeners reset their editor');
        order.push(['event', event]);
      } },
    },
  });
  const result = useNewAnalysis().startNewAnalysis();
  assert.equal(result, navigation.promise);
  assert.deepEqual(order, [['event', EVENT_START_NEW_ANALYSIS], ['navigate', '/chat']]);
  assert.equal(access.read('session:task-a').message, '现有任务尚未发送的问题');
  navigation.resolve();
  await result;
});
