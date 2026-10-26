import assert from 'node:assert/strict';
import test from 'node:test';
import { useAnalysisSession } from '../src/composables/useAnalysisSession.ts';
import { createAgentStreamRecovery } from '../src/utils/agentStreamRecovery.ts';
import { createAnalysisDraftStore, emptyAnalysisDraft } from '../src/utils/analysisDrafts.ts';
import { SSEConnectionError } from '../src/utils/sseConnection.ts';

const view = () => ({ schemaVersion: 1, fileId: 'minio:abc', version: 'a'.repeat(64), pluginId: 'fits-image', pluginVersion: '2.1.0', adapter: 'scientific-image', family: 'scientific',
  request: { operation: 'preview', params: { hdu: 0 } }, view: { kind: 'image', valueSemantics: 'physical', sampled: true },
  selection: { kind: 'range', coordinateSystem: 'source-index', bounds: 'inclusive', x: [8, 16], y: [0, 8] } });

test('retry retains the original range and version after a preview changes', async () => {
  const calls = [];
  const state = useAnalysisSession({ api: {
    chatWithSession: async (...args) => { calls.push(args); return () => {}; }, createClientMessageId: () => 'input',
    createSession: async () => ({ session_id: 'session' }), getSessionHistory: async () => ({ events: [] }), stopSession: async () => {},
    getInputReceipt: async () => ({ client_message_id: 'input', accepted: false, state: null }),
  } });
  state.sessionId.value = 'session';
  const context = view();
  await state.send({ message: 'Explain range', files: [{ file_id: 'minio:abc', filename: 'a.fits' }], inputFileIds: ['minio:abc'], visualizationContexts: [context] });
  context.selection.x[0] = 999; context.version = 'b'.repeat(64);
  assert.deepEqual(calls[0][16][0].selection.x, [8, 16]);
  calls[0][8].onError(new SSEConnectionError('transport', 'offline'));
  await state.retryPendingInput();
  assert.equal(calls[1][10], calls[0][10]);
  assert.deepEqual(calls[1][16], calls[0][16]);
  state.dispose();
});

test('accepted reconnects cannot resubmit visualization context', () => {
  const context = view();
  const stream = createAgentStreamRecovery({ message: 'Explain', client_message_id: 'id', visualization_contexts: [context] });
  context.selection.x[0] = 99;
  assert.deepEqual(stream.getBody().visualization_contexts[0].selection.x, [8, 16]);
  stream.accept({ event: 'message', data: { seq: 1, event_id: '1-0', version: 1, content: 'Explain', role: 'user' } });
  assert.equal(stream.getBody().message, '');
  assert.equal(stream.getBody().visualization_contexts, undefined);
});

test('unsent selections are independently copied and account-scoped', () => {
  const store = createAnalysisDraftStore(); store.setOwner('a');
  const access = store.open('a'), context = view();
  access.save('session', { ...emptyAnalysisDraft(), visualizationContexts: [context] });
  context.selection.x[0] = 999;
  const saved = access.read('session');
  assert.deepEqual(saved.visualizationContexts[0].selection.x, [8, 16]);
  saved.visualizationContexts[0].selection.x[0] = 888;
  assert.deepEqual(access.read('session').visualizationContexts[0].selection.x, [8, 16]);
  store.setOwner('b');
  assert.equal(access.read('session'), undefined);
  assert.equal(store.open('b').read('session'), undefined);
});
