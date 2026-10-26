import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import * as vue from 'vue';
import { renderToString } from '@vue/server-renderer';
import { compileScript, parse } from '@vue/compiler-sfc';
import ts from 'typescript';
import * as clarification from '../src/utils/analysisClarification.ts';
import { useAnalysisSession } from '../src/composables/useAnalysisSession.ts';
import { projectHistoryMessages } from '../src/utils/sessionHistory.ts';
import { createAgentStreamRecovery } from '../src/utils/agentStreamRecovery.ts';
import { SSEConnectionError } from '../src/utils/sseConnection.ts';

const request = {
  id: 'clarification-1', stage: 'request', source_event_seq: 1,
  questions: [{ id: 'unit', question: '哪个字段代表独立样本？', reason: '样本单位会影响统计结果。', options: ['培养瓶', '测量记录'] }],
};
const frame = (seq, event, data = {}) => ({ event, data: { seq, event_id: `${seq}-0`, timestamp: seq, ...data } });
const user = (seq = 1, metadata = {}) => frame(seq, 'message', { role: 'user', content: '请分析所选文件', metadata });
const ask = (seq = 3, value = request) => frame(seq, 'message', { role: 'assistant', content: '请确认独立样本。', metadata: { clarification: value } });
const wait = (seq = 4) => frame(seq, 'wait');
const running = (seq = 2) => frame(seq, 'step', { id: 'analysis', description: '分析所选资料', status: 'running' });

function cardComponent(inlineTemplate = false) {
  const source = readFileSync(new URL('../src/components/AnalysisClarificationCard.vue', import.meta.url), 'utf8');
  const script = ts.transpileModule(compileScript(parse(source).descriptor, { id: 'clarification-card', inlineTemplate }).content,
    { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
  const module = { exports: {} };
  const imports = { vue, 'lucide-vue-next': { CircleHelp: { render: () => null } }, '../utils/analysisClarification': clarification };
  new Function('require', 'module', 'exports', script)(name => { assert.ok(name in imports, name); return imports[name]; }, module, module.exports);
  return module.exports.default;
}

function cardHarness(t, overrides = {}) {
  const scope = vue.effectScope(), events = [];
  const props = vue.reactive({ clarification: request, pending: true, disabled: false, ...overrides });
  const state = scope.run(() => cardComponent().setup(props, { expose() {}, emit: (...args) => events.push(args) }));
  t.after(() => scope.stop());
  return { props, state, events };
}

function sessionHarness(t, extra = {}) {
  const calls = [];
  let input = 0;
  const state = useAnalysisSession({ api: {
    createClientMessageId: () => `reply-${++input}`,
    chatWithSession: async (...args) => { calls.push(args); return () => {}; },
    ...extra,
  } });
  state.sessionId.value = 'session';
  t.after(() => state.dispose());
  return { state, calls };
}

test('no answer or option is selected on mount; choosing an option never submits', async t => {
  const h = cardHarness(t);
  assert.equal(h.state.answerText.value, undefined);
  h.state.submit();
  assert.deepEqual(h.events, []);
  h.state.choose('unit', '培养瓶');
  await vue.nextTick();
  assert.deepEqual(h.events, []);
  h.state.submit();
  h.state.submit();
  assert.deepEqual(h.events, [['answer', { clarificationId: request.id, message: '1. 哪个字段代表独立样本？\n我的确认：培养瓶' }]]);
});

test('free answers require every question, and are preserved after a rejected send', async t => {
  const second = { id: 'unit2', question: '是否按批次分层？', reason: '需要明确处理范围。', options: [] };
  const h = cardHarness(t, { clarification: { ...request, questions: [...request.questions, second] } });
  h.state.answers.value.unit = '每个培养瓶是独立样本';
  h.state.submit(); assert.equal(h.events.length, 0);
  h.state.answers.value.unit2 = '  按批次分层  ';
  h.state.submit(); assert.equal(h.events.length, 1);
  assert.match(h.events[0][1].message, /2\. 是否按批次分层？\n我的确认：按批次分层$/);
  h.props.disabled = true; await vue.nextTick();
  h.props.disabled = false; await vue.nextTick();
  assert.equal(h.state.answers.value.unit, '每个培养瓶是独立样本');
  h.state.submit(); assert.equal(h.events.length, 2);
});

test('old/shared cards are read-only, and a new request never reuses old answers', async t => {
  const h = cardHarness(t, { pending: false });
  h.state.choose('unit', '培养瓶'); h.state.submit();
  assert.deepEqual(h.events, []);
  const html = await renderToString(vue.createSSRApp(cardComponent(true), h.props));
  assert.match(html, /确认问题记录/);
  assert.doesNotMatch(html, /<textarea|type="submit"|role="radio"/);
  h.props.pending = true; h.state.choose('unit', '培养瓶');
  h.props.clarification = { ...request, id: 'new-request' }; await vue.nextTick();
  assert.equal(h.state.answerText.value, undefined);
});

test('a pending card renders the reason, choices and an initially disabled confirmation', async () => {
  const html = await renderToString(vue.createSSRApp(cardComponent(true), { clarification: request, pending: true }));
  assert.match(html, /样本单位会影响统计结果/);
  assert.match(html, /aria-checked="false"/);
  assert.match(html, /type="submit" disabled/);
  assert.match(html, /<textarea/);
});

test('malformed metadata never becomes an actionable request', () => {
  for (const value of [null, {}, { ...request, id: '' }, { ...request, stage: 'unknown' },
    { ...request, questions: [] }, { ...request, questions: [request.questions[0], request.questions[0]] },
    { ...request, questions: [{ ...request.questions[0], options: [null] }] }]) {
    assert.equal(clarification.readAnalysisClarification(value), undefined);
  }
  assert.equal(clarification.readAnalysisClarification(request), request);
  assert.equal(clarification.clarificationAnswerText({ ...request, questions: [{ ...request.questions[0], id: '__proto__' }] }, {}), undefined);
});

test('wait marks the task as waiting without failing the analysis; plain composer replies carry the current id', async t => {
  const h = sessionHarness(t);
  for (const event of [user(), running(), ask(), wait()]) h.state.handleEvent(event);
  assert.equal(h.state.waitingForUser.value, true);
  assert.equal(h.state.pendingClarification.value.id, request.id);
  assert.equal(h.state.isLoading.value, false);
  assert.equal(h.state.messages.value.find(message => message.type === 'step').content.status, 'running');
  assert.equal(await h.state.send({ message: '培养瓶' }), true);
  assert.equal(h.calls[0][17], request.id);
  assert.equal(h.state.waitingForUser.value, false);
  assert.equal(h.state.pendingClarification.value, undefined);
  assert.equal(h.state.messages.value.find(message => message.type === 'step').content.status, 'running');
  assert.equal(await h.state.send({ message: '再次点击', clarificationId: request.id }), false);
  assert.equal(h.calls.length, 1);
});

test('stale cards cannot answer a newer question and terminal events remove pending confirmation', async t => {
  const h = sessionHarness(t);
  for (const event of [user(), ask(2, { ...request, id: 'new' }), wait(3)]) h.state.handleEvent(event);
  assert.equal(await h.state.send({ message: '培养瓶', clarificationId: request.id }), false);
  assert.equal(h.calls.length, 0);
  for (const terminal of ['done', 'error']) {
    const session = sessionHarness(t).state;
    session.handleEvent(ask()); session.handleEvent(wait());
    session.handleEvent(frame(5, terminal, { error: '已停止' }));
    assert.equal(session.waitingForUser.value, false);
    assert.equal(session.pendingClarification.value, undefined);
  }
});

test('refresh restores a pending question without starting execution or choosing an answer', async t => {
  const events = [user(), running(), ask(), wait()];
  const h = sessionHarness(t, { getSessionHistory: async () => ({ session_id: 'session', status: 'waiting', created_at: 1, events, has_more: false }) });
  await h.state.restore('session');
  assert.equal(h.state.waitingForUser.value, true);
  assert.equal(h.state.pendingClarification.value.id, request.id);
  assert.equal(h.calls.length, 0);
  const history = projectHistoryMessages([...events, user(5, { clarification_id: request.id })]);
  assert.equal(history.find(message => message.type === 'step').content.status, 'running');
  assert.equal(history.find(message => message.type === 'assistant').content.metadata.clarification.id, request.id);
});

test('HTTP rejection restores the pending question and allows an explicit retry', async t => {
  const h = sessionHarness(t);
  for (const event of [user(), ask(2), wait(3)]) h.state.handleEvent(event);
  await h.state.send({ message: '培养瓶', clarificationId: request.id });
  h.calls[0][8].onError(new SSEConnectionError('http', '请补充答案', 422));
  assert.equal(h.state.waitingForUser.value, true);
  assert.equal(h.state.pendingClarification.value.id, request.id);
  assert.equal(h.state.messages.value.filter(message => message.type === 'user').length, 1);
  assert.equal(await h.state.send({ message: '每个培养瓶独立', clarificationId: request.id }), true);
  assert.equal(h.calls.length, 2);
});

test('transport uncertainty preserves one reply identity and never silently resends a new answer', async t => {
  const h = sessionHarness(t, { getInputReceipt: async () => ({ client_message_id: 'reply-1', accepted: false }) });
  for (const event of [user(), ask(2), wait(3)]) h.state.handleEvent(event);
  await h.state.send({ message: '培养瓶', clarificationId: request.id });
  h.calls[0][8].onError(new SSEConnectionError('transport', '连接中断'));
  assert.equal(h.state.canRetryInput.value, true);
  assert.equal(await h.state.send({ message: '新的答案', clarificationId: request.id }), false);
  await h.state.retryPendingInput();
  assert.equal(h.calls.length, 2);
  assert.equal(h.calls[1][10], h.calls[0][10]);
  assert.equal(h.calls[1][17], request.id);
});

test('stream replay retains clarification identity until acceptance, then removes it from observation', () => {
  const recovery = createAgentStreamRecovery({ message: '培养瓶', client_message_id: 'reply', clarification_id: request.id });
  assert.equal(recovery.getBody().clarification_id, request.id);
  recovery.accept(user(5, { client_message_id: 'reply', clarification_id: request.id }));
  assert.equal(recovery.getBody().message, '');
  assert.equal(recovery.getBody().clarification_id, undefined);
});

test('the chat API sends clarification_id only for an explicit answer, never continuation or observation', async () => {
  const source = readFileSync(new URL('../src/api/agent.ts', import.meta.url), 'utf8');
  const ast = ts.createSourceFile('agent.ts', source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS);
  const statement = ast.statements.find(node => ts.isVariableStatement(node)
    && node.declarationList.declarations.some(item => item.name.getText(ast) === 'chatWithSession'));
  const code = ts.transpileModule(statement.getText(ast).replace(/^export\s+/, ''), {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.None },
  }).outputText;
  const bodies = [];
  const chat = new Function('scope', `with (scope) { ${code}; return chatWithSession; }`)({
    createAgentStreamRecovery, createClientMessageId: () => 'reply',
    createSSEConnection: async (_url, options) => { bodies.push(options.getBody()); return () => {}; },
  });
  await chat('session', '培养瓶', undefined, undefined, [], [], [], null, {}, undefined, 'reply', undefined,
    ['selected-input'], 1, undefined, [], [], request.id);
  assert.equal(bodies[0].clarification_id, request.id);
  assert.deepEqual(bodies[0].input_file_ids, ['selected-input']);
  await chat('session', '', undefined, undefined, [], [], [], null, {}, undefined, 'resume', 'checkpoint',
    undefined, 1, undefined, [], [], request.id);
  assert.equal(bodies[1].clarification_id, undefined);
  await chat('session', '', undefined, undefined, [], [], [], null, {}, undefined, undefined, undefined,
    undefined, 1, 'observed-input', [], [], request.id);
  assert.equal(bodies[2].clarification_id, undefined);
});
