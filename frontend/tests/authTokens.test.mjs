import assert from 'node:assert/strict'
import test from 'node:test'
import { clearTokens, ensureAccessToken, refreshAccessToken } from '../src/utils/authTokens.ts'

function browser() {
  const previous = { storage: globalThis.localStorage, window: globalThis.window, fetch: globalThis.fetch }
  const values = new Map()
  const events = []
  globalThis.localStorage = { getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value), removeItem: key => values.delete(key) }
  globalThis.window = { dispatchEvent: event => events.push(event.type) }
  return { values, events, restore() { globalThis.localStorage = previous.storage; globalThis.window = previous.window; globalThis.fetch = previous.fetch } }
}
const result = token => ({ ok: true, json: async () => ({ code: 0, data: { access_token: token } }) })

test('concurrent expired requests share one refresh and preserve the active account', async () => {
  const env = browser()
  try {
    env.values.set('refresh_token', 'alice-refresh')
    let requests = 0
    let finish
    globalThis.fetch = async (_url, options) => {
      requests++
      assert.equal(JSON.parse(options.body).refresh_token, 'alice-refresh')
      return new Promise(resolve => { finish = resolve })
    }
    const first = refreshAccessToken('/api/v1')
    const second = refreshAccessToken('/api/v1')
    finish(result('alice-new-access'))
    assert.deepEqual(await Promise.all([first, second]), ['alice-new-access', 'alice-new-access'])
    assert.equal(requests, 1)
    assert.equal(env.values.get('access_token'), 'alice-new-access')
  } finally { env.restore() }
})

test('an in-flight refresh cannot restore credentials after logout or account replacement', async () => {
  const env = browser()
  try {
    env.values.set('refresh_token', 'alice-refresh')
    let finish
    globalThis.fetch = async () => new Promise(resolve => { finish = resolve })
    const oldRequest = refreshAccessToken('/api/v1')
    clearTokens()
    env.values.set('refresh_token', 'bob-refresh')
    env.values.set('access_token', 'bob-access')
    finish(result('alice-new-access'))
    assert.equal(await oldRequest, null)
    assert.equal(env.values.get('access_token'), 'bob-access')
    assert.equal(env.values.get('refresh_token'), 'bob-refresh')
  } finally { env.restore() }
})

test('rejected refresh clears expired identity and notifies mounted views', async () => {
  const env = browser()
  try {
    env.values.set('access_token', 'expired-access')
    env.values.set('refresh_token', 'expired-refresh')
    globalThis.fetch = async () => ({ ok: false, status: 401, json: async () => ({ code: 401 }) })
    assert.equal(await refreshAccessToken('/api/v1'), null)
    assert.equal(env.values.size, 0)
    assert.deepEqual(env.events, ['datalab:auth-expired'])
  } finally { env.restore() }
})

test('a fresh access token is used directly without a refresh network call', async () => {
  const env = browser()
  try {
    const token = 'header.' + Buffer.from(JSON.stringify({ exp: Date.now() / 1000 + 300 })).toString('base64url') + '.signature'
    env.values.set('access_token', token)
    globalThis.fetch = () => { throw new Error('Unexpected refresh') }
    assert.equal(await ensureAccessToken('/api/v1'), token)
  } finally { env.restore() }
})

test('a new account refresh does not reuse an old account in-flight request', async () => {
  const env = browser()
  try {
    const requests = []
    globalThis.fetch = async (_url, options) => new Promise(resolve => requests.push({ owner: JSON.parse(options.body).refresh_token, resolve }))
    env.values.set('refresh_token', 'alice-refresh')
    const alice = refreshAccessToken('/api/v1')
    env.values.set('refresh_token', 'bob-refresh')
    const bob = refreshAccessToken('/api/v1')
    assert.deepEqual(requests.map(row => row.owner), ['alice-refresh', 'bob-refresh'])
    requests[0].resolve(result('alice-access'))
    assert.equal(await alice, null)
    requests[1].resolve(result('bob-access'))
    assert.equal(await bob, 'bob-access')
    assert.equal(env.values.get('access_token'), 'bob-access')
  } finally { env.restore() }
})

test('an expired access token without refresh credentials clears the visible identity', async () => {
  const env = browser()
  try {
    env.values.set('access_token', 'expired')
    assert.equal(await ensureAccessToken('/api/v1'), null)
    assert.equal(env.values.size, 0)
    assert.deepEqual(env.events, ['datalab:auth-expired'])
  } finally { env.restore() }
})
