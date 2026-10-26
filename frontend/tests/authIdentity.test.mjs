import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import * as vue from 'vue'
import ts from 'typescript'
const code = ts.transpileModule(readFileSync(new URL('../src/composables/useAuth.ts', import.meta.url), 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText
const user = id => ({ id, fullname: id, email: `${id}@example.test`, role: 'user', is_active: true, registration_status: 'approved' })
const credentials = id => ({ user: user(id), access_token: `${id}-access`, refresh_token: `${id}-refresh` })
function harness() {
  const values = new Map(), requests = [], events = [], listeners = new Map(), navigation = [], cleared = []
  const auth = {
    getStoredToken: () => values.get('access'), getStoredRefreshToken: () => values.get('refresh'),
    storeToken: token => values.set('access', token), storeRefreshToken: token => values.set('refresh', token),
    clearStoredTokens: () => values.clear(), clearAuthToken() {},
    login: args => new Promise((resolve, reject) => requests.push({ name: 'login', args, resolve, reject })),
    getCurrentUser: () => new Promise((resolve, reject) => requests.push({ name: 'me', resolve, reject })),
    logout: async () => {},
  }
  const dependencies = {
    vue, '../api/auth': auth, '../api/client': { BASE_URL: '/api/v1' },
    '../utils/authTokens': { ensureAccessToken: async () => values.get('access'), refreshAccessToken: async () => values.get('access') },
    './usePendingChat': { clearPendingChats: () => cleared.push('chats') },
    './useDataSelection': { clearPendingLibraryFiles: () => cleared.push('files') },
    '../utils/analysisDrafts': { analysisDraftStore: { clear: () => cleared.push('drafts'), setOwner: owner => events.push(['owner', owner]) } },
    '../utils/eventBus': { eventBus: { emit: name => events.push([name]) } },
  }
  const module = { exports: {} }
  const scope = vue.effectScope()
  scope.run(() => new Function('require', 'module', 'exports', 'window', code)(name => {
    if (name in dependencies) return dependencies[name]
    throw new Error(`Unexpected dependency ${name}`)
  }, module, module.exports, { addEventListener: (name, callback) => listeners.set(name, callback), location: { assign: path => navigation.push(path), reload: () => navigation.push('reload') } }))
  return { state: module.exports.useAuth(), values, requests, events, listeners, navigation, cleared, close: () => scope.stop() }
}

test('a late current-user response cannot restore the previous account after logout', async () => {
  const h = harness()
  try {
    h.values.set('access', 'alice-access'); h.values.set('refresh', 'alice-refresh')
    const pending = h.state.loadCurrentUser()
    h.state.clearAuth()
    h.requests[0].resolve(user('alice')); await pending
    assert.equal(h.state.currentUser.value, null)
    assert.deepEqual(h.cleared, ['chats', 'files', 'drafts'])
    assert.ok(h.events.some(([name]) => name === 'auth:cleared'))
  } finally { h.close() }
})

test('overlapping account logins cannot restore the earlier identity or its credentials', async () => {
  const h = harness()
  try {
    const alice = h.state.login({ email: 'alice@example.test', password: 'secret' })
    const aliceRejected = assert.rejects(alice, /账号已切换/)
    const bob = h.state.login({ email: 'bob@example.test', password: 'secret' })
    h.requests[1].resolve(credentials('bob')); await bob
    h.requests[0].resolve(credentials('alice')); await aliceRejected
    assert.equal(h.state.currentUser.value.id, 'bob')
    assert.equal(h.values.get('access'), 'bob-access')
    assert.equal(h.values.get('refresh'), 'bob-refresh')
  } finally { h.close() }
})

test('a login change from another tab clears private state without deleting its new credentials', async () => {
  const h = harness()
  try {
    h.values.set('access', 'bob-access'); h.values.set('refresh', 'bob-refresh')
    h.listeners.get('storage')({ key: 'refresh_token', oldValue: 'alice-refresh', newValue: 'bob-refresh' })
    assert.equal(h.values.get('refresh'), 'bob-refresh')
    assert.deepEqual(h.navigation, ['reload'])
    assert.equal(h.state.currentUser.value, null)
    assert.ok(h.events.some(([name]) => name === 'auth:cleared'))
  } finally { h.close() }
})
