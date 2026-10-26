import assert from 'node:assert/strict'
import test from 'node:test'
import { startAuthenticatedSSE } from '../src/utils/authenticatedSSE.ts'
import { eventBus } from '../src/utils/eventBus.ts'

function harness(t) {
  const previous = { storage: globalThis.localStorage, fetch: globalThis.fetch }
  const values = new Map([['refresh_token', 'alice-refresh'], ['access_token', 'header.' + Buffer.from(JSON.stringify({ exp: Date.now() / 1000 + 300 })).toString('base64url') + '.signature']])
  globalThis.localStorage = { getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value), removeItem: key => values.delete(key) }
  const calls = []; let options, callbacks, cancelled = 0, finish
  const pending = new Promise(resolve => { finish = resolve })
  const connection = startAuthenticatedSSE('/api/v1/sessions/alice/events', '/api/v1', { retryOnError: true, headers: { Authorization: 'Bearer stale', 'X-API-Key': 'legacy' }, getBody: () => ({ session: 'alice' }) }, {
    onOpen: () => calls.push('open'), onMessage: event => calls.push(event), onError: error => calls.push(error), onClose: () => calls.push('close'), onRetry: () => calls.push('retry'),
  }, (_url, suppliedOptions, suppliedCallbacks) => {
    options = suppliedOptions; callbacks = suppliedCallbacks
    return { cancel: () => { cancelled++; finish() }, finished: pending }
  })
  t.after(() => { connection.cancel(); globalThis.localStorage = previous.storage; globalThis.fetch = previous.fetch })
  return { connection, values, calls, options, callbacks, cancelled: () => cancelled }
}

test('SSE reconnect and late callbacks stay bound to their initial account', async t => {
  const h = harness(t)
  assert.deepEqual(h.options.headers, {})
  assert.ok((await h.options.getHeaders()).Authorization.startsWith('Bearer '))
  h.callbacks.onOpen(); assert.deepEqual(h.calls, ['open'])
  h.values.set('refresh_token', 'bob-refresh')
  await assert.rejects(h.options.getHeaders(), /账号已切换/)
  assert.throws(h.options.getBody, /账号已切换/)
  h.callbacks.onMessage({ event: 'message', data: { private: 'alice' } }); h.callbacks.onRetry({}); h.callbacks.onError(new Error('old')); h.callbacks.onClose()
  assert.deepEqual(h.calls, ['open'])
})

test('identity cleanup immediately cancels an active stream and releases its event listener', async t => {
  const h = harness(t)
  eventBus.emit('auth:cleared')
  assert.equal(h.cancelled(), 1)
  await h.connection.finished
  eventBus.emit('auth:cleared')
  assert.equal(h.cancelled(), 1)
})

test('identity switch while refreshing headers prevents a stream attempt using replacement credentials', async t => {
  const h = harness(t)
  h.values.set('access_token', 'expired')
  let resolveRefresh
  globalThis.fetch = async () => new Promise(resolve => { resolveRefresh = resolve })
  const pending = h.options.getHeaders()
  h.values.set('refresh_token', 'bob-refresh'); h.values.set('access_token', 'bob-access')
  resolveRefresh({ ok: true, json: async () => ({ code: 0, data: { access_token: 'alice-renewed' } }) })
  await assert.rejects(pending, /账号已切换/)
  assert.equal(h.values.get('access_token'), 'bob-access')
})
