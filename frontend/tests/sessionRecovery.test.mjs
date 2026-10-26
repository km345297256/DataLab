import { useAnalysisSession } from '../src/composables/useAnalysisSession.ts';
import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import ts from 'typescript';
import { ref } from 'vue';
import { shouldRecoverSession } from '../src/utils/sessionRecovery.ts';
import { readyExplorationFiles } from '../src/utils/explorationFiles.ts';

const user = { event: 'message', data: { role: 'user', metadata: { data_connectors: ['ncbi-datasets-v2'] } } };
const done = { event: 'done', data: {} };
test('new and metadata-only pending sessions never open a chat stream', () => {
  assert.equal(shouldRecoverSession('pending', []), false);
  assert.equal(shouldRecoverSession('pending', [{ event: 'title', data: { title: 'API sample' } }]), false);
  assert.equal(shouldRecoverSession('pending', [user, done]), false);
  assert.equal(shouldRecoverSession('pending', [user]), true);
  assert.equal(shouldRecoverSession('pending', [user, done, user]), true);
  assert.equal(shouldRecoverSession('running', []), true);
  assert.equal(shouldRecoverSession('completed', [user]), false);
  assert.equal(shouldRecoverSession('waiting', [user]), false);
});

const source = readFileSync(new URL('../src/pages/ChatPage.vue', import.meta.url), 'utf8');
const script = source.match(/<script setup[^>]*>([\s\S]*?)<\/script>/)[1];
const ast = ts.createSourceFile('ChatPage', script, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS);
function handler(name, scope) {
  const statement = ast.statements.find(node => ts.isVariableStatement(node) && node.declarationList.declarations.some(item => item.name.getText(ast) === name));
  const code = ts.transpileModule(statement.getText(ast), {compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.None}}).outputText;
  return new Function('scope', `with(scope) { ${code}; return ${name}; }`)(scope);
}
test('actual shared controller leaves empty pending sessions idle and resumes accepted pending input', async () => {
  for (const events of [[], [{ ...user, data: { ...user.data, content: 'question', timestamp: 1, seq: 1 } }]]) {
    let streams = 0;
    const session = useAnalysisSession({ api: {
      getSessionHistory: async () => ({ events, status: 'pending', title: 'API sample', has_more: false }),
      chatWithSession: async () => { streams++; return () => {}; },
    } });
    await session.restore('sample');
    assert.equal(streams, events.length ? 1 : 0);
    assert.equal(session.isRestoringHistory.value, false);
    session.dispose();
  }
});

function restorationScope(context) {
  const notices = [];
  const scope = {
    sessionId: ref('sample'), viewGeneration: 1, viewDisposed: false,
    isRestoringHistory: ref(false), isSavingDataContext: ref(false), dataContextFailed: ref(false),
    shareMode: ref('private'), titleManuallySet: ref(false), realTime: ref(true), title: ref(''),
    selectedSkills: ref([]), selectedMcpServers: ref([]), selectedConnectors: ref([]), isLoading: ref(false),
    connectionNotice: ref(''), attachments: ref([]), readyExplorationFiles,
    showErrorToast: notice => notices.push(notice),
    isCurrentSession: id => id === scope.sessionId.value && !scope.viewDisposed, t: x => x,
    analysis: { restore: async () => ({ events: [], status: 'completed', title: 'Saved exploration', has_more: false }) },
    agentApi: { clearUnreadMessageCount() {} },
    refreshAnalysisInputs: async () => {
      try { const files = await context(); return { files, selected_file_ids: [...new Set(files.map(file => file.file_id))] }; }
      catch { return undefined; }
    },
    analysisDraft: { restore(value) { scope.attachments.value = value.files; } },
  };
  return { scope, notices };
}

test('restoring an exploration reuses its explicit saved selection without auto-attaching outputs', async () => {
  const selected = { file_id: 'selected', filename: 'input.csv', upload_date: '2026-10-20T00:00:00Z', metadata: { private: 'must not enter draft' } };
  const { scope, notices } = restorationScope(async () => [selected, selected]);
  await handler('restoreSession', scope)();
  assert.deepEqual(scope.attachments.value, [{ file_id: 'selected', filename: 'input.csv', content_type: undefined, size: undefined, upload_date: selected.upload_date }]);
  assert.deepEqual(notices, []);
  assert.equal(scope.isRestoringHistory.value, false);
});

test('actual history restoration gives the draft cache a server-only baseline, never the unsent textarea', async () => {
  const selected = { file_id: 'server-selected', filename: 'input.csv', upload_date: '' };
  const { scope } = restorationScope(async () => [selected]);
  scope.inputMessage = ref('未发送的问题 M');
  let baseline;
  scope.analysisDraft.restore = restored => { baseline = restored; };
  await handler('restoreSession', scope)();
  assert.equal(baseline.message, '');
  assert.deepEqual(baseline.files.map(file => file.file_id), ['server-selected']);
  assert.deepEqual(baseline.skills, []);
  assert.deepEqual(baseline.connectors, []);
  assert.equal(scope.inputMessage.value, '未发送的问题 M');
});

test('failed context restoration preserves history while reporting unavailable selected inputs', async () => {
  const { scope, notices } = restorationScope(async () => { throw new Error('/private/host/data'); });
  await handler('restoreSession', scope)();
  assert.equal(scope.title.value, 'Saved exploration');
  assert.deepEqual(scope.attachments.value, []);
  assert.equal(scope.dataContextFailed.value, true);
  assert.deepEqual(notices, []);
});

test('restoring unsent draft files never bypasses the server context failure that disables sending', async () => {
  const { scope } = restorationScope(async () => { throw new Error('Saved input is no longer accessible'); });
  scope.analysisDraft.restore = () => {
    scope.attachments.value = [{ file_id: 'cached-input', filename: 'input.csv', upload_date: '' }];
    scope.selectedConnectors.value = ['explicit-choice'];
  };
  await handler('restoreSession', scope)();
  assert.deepEqual(scope.attachments.value.map(file => file.file_id), ['cached-input']);
  assert.equal(scope.dataContextFailed.value, true);
  assert.equal(scope.isLoading.value, false);
});

test('the composer remains disabled until selected input restoration completes', async () => {
  let resolve;
  const { scope } = restorationScope(() => new Promise(done => { resolve = done; }));
  const restoring = handler('restoreSession', scope)();
  await Promise.resolve(); await Promise.resolve();
  assert.equal(typeof resolve, 'function');
  assert.equal(scope.isSavingDataContext.value, true);
  resolve([]);
  await restoring;
  assert.equal(scope.isRestoringHistory.value, false);
});

test('late selected-data responses cannot populate a different conversation', async () => {
  let resolve;
  const { scope } = restorationScope(() => new Promise(done => { resolve = done; }));
  const restoring = handler('restoreSession', scope)();
  await Promise.resolve(); await Promise.resolve();
  assert.equal(typeof resolve, 'function');
  scope.sessionId.value = 'other';
  scope.viewGeneration++;
  resolve([{ file_id: 'old-private', filename: 'old.csv', upload_date: '' }]);
  await restoring;
  assert.deepEqual(scope.attachments.value, []);
  assert.equal(scope.title.value, '');
});

test('stopping recovery cancels viewer retries while retaining the original request identity', async () => {
  let cancelled = 0;
  const requests = [];
  const session = useAnalysisSession({ api: {
    createClientMessageId: () => 'same-input',
    chatWithSession: async (...args) => { requests.push(args); return () => cancelled++; },
  } });
  session.sessionId.value = 'sample';
  await session.send({ message: 'analyze', dataConnectors: ['ncbi-datasets-v2'] });
  session.stopReconnecting();
  assert.equal(cancelled, 1); assert.equal(session.isLoading.value, false);
  assert.match(session.connectionNotice.value, /已停止自动重连/);
  await session.retryPendingInput();
  assert.equal(requests[0][10], requests[1][10]);
  assert.deepEqual(requests[1][15], ['ncbi-datasets-v2']);
  session.dispose();
});

test('leaving the DataLab page aborts input reads and disposes the viewer without stopping backend work', () => {
  const callback = ast.statements.find(node => ts.isExpressionStatement(node) && ts.isCallExpression(node.expression)
    && node.expression.expression.getText(ast) === 'onUnmounted').expression.arguments[0];
  const code = ts.transpileModule(`const leave = ${callback.getText(ast)};`, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.None },
  }).outputText;
  const calls = [];
  const subscriptions = [];
  const scope = {
    viewDisposed: false, viewGeneration: 7, inputGeneration: 2,
    inputRequest: { abort: () => calls.push('abort-inputs') },
    analysis: { dispose: () => calls.push('dispose-viewer') },
    eventBus: { off: (name, callback) => { calls.push(`unsubscribe:${name}`); subscriptions.push([name, callback]); } },
    EVENT_SESSION_RENAMED: 'rename', handleConnectorDraft() {}, handleSessionRenamed() {}, handleOpenDataRefine() {}, handleTaskDataInputChange() {}, handleVisualizationDraft() {},
    agentApi: { stopSession: () => assert.fail('Returning home must not stop backend work') },
  };
  new Function('scope', `with(scope) { ${code}; return leave; }`)(scope)();
  assert.equal(scope.viewDisposed, true); assert.equal(scope.viewGeneration, 8);
  assert.ok(calls.includes('abort-inputs')); assert.ok(calls.includes('dispose-viewer'));
  assert.ok(subscriptions.some(([name, callback]) => name === 'task-data-input-change' && callback === scope.handleTaskDataInputChange));
  assert.ok(subscriptions.some(([name, callback]) => name === 'visualization-analysis-draft' && callback === scope.handleVisualizationDraft));
});
