import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import * as vue from 'vue'
import { compileScript, parse } from '@vue/compiler-sfc'
import ts from 'typescript'

function loadComponent(path, dependencies) {
  const source = readFileSync(new URL(path, import.meta.url), 'utf8')
  const code = ts.transpileModule(compileScript(parse(source).descriptor, { id: 'accounts-test' }).content, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText
  const module = { exports: {} }
  new Function('require', 'module', 'exports', code)(name => {
    if (name in dependencies) return dependencies[name]
    throw new Error(`Unexpected import: ${name}`)
  }, module, module.exports)
  return module.exports.default.setup({}, { expose() {} })
}
const user = (id, fields = {}) => ({ id, fullname: id, email: `${id}@example.test`, role: 'user', registration_status: 'approved', is_active: true, ...fields })
function harness() {
  const requests = [], updates = [], decisions = [], disposers = []
  const state = loadComponent('../src/components/admin/RegistrationManagement.vue', {
    vue: { ...vue, onMounted() {}, onUnmounted: callback => disposers.push(callback) },
    '@/api/auth': {
      listUsers: (...args) => new Promise((resolve, reject) => requests.push({ args, resolve, reject })),
      updateUser: async (...args) => { updates.push(args); return user(args[0], args[1]) },
      decideRegistration: async (...args) => { decisions.push(args); return user(args[0]) },
    },
    '@/composables/useAuth': { useAuth: () => ({ currentUser: vue.ref(user('admin', { role: 'admin' })) }) },
    '@/components/ui/dialog': {},
  })
  return { state, requests, updates, decisions, close: () => disposers.forEach(dispose => dispose()) }
}
const flush = async () => { await Promise.resolve(); await Promise.resolve() }

test('user filters go to the server, and a stale page cannot replace a newer result', async () => {
  const h = harness()
  try {
    const old = h.state.load()
    h.state.query.value = '  alice  '; h.state.status.value = 'inactive'; h.state.role.value = 'user'
    h.state.offset.value = 50; h.state.applyFilters()
    assert.equal(h.requests[0].args[1].aborted, true)
    assert.deepEqual(h.requests[1].args[0], { query: 'alice', status: 'inactive', role: 'user', limit: 25, offset: 0 })
    h.requests[1].resolve({ users: [user('alice', { is_active: false })], total: 1 }); await flush()
    h.requests[0].resolve({ users: [user('bob')], total: 99 }); await old
    assert.equal(h.state.users.value[0].id, 'alice')
    assert.equal(h.state.total.value, 1)
    assert.equal(h.state.accountStatus(h.state.users.value[0]), '已停用')
  } finally { h.close() }
})

test('changing role requires explicit confirmation and refreshes authoritative user state', async () => {
  const h = harness()
  try {
    h.state.prepare(user('alice'), 'role')
    h.state.selectedRole.value = 'admin'
    assert.equal(h.updates.length, 0, 'opening a dialog does not grant privileges')
    const pending = h.state.confirmAction(); await flush()
    assert.deepEqual(h.updates, [['alice', { role: 'admin' }]])
    assert.equal(h.state.pending.value, null)
    h.requests.at(-1).resolve({ users: [user('alice', { role: 'admin' })], total: 1 }); await pending
    assert.equal(h.state.users.value[0].role, 'admin')
  } finally { h.close() }
})

test('registration approval and account deactivation use different authorization operations', async () => {
  const h = harness()
  try {
    h.state.prepare(user('new', { registration_status: 'pending', is_active: false }), 'approve')
    const approval = h.state.confirmAction(); await flush()
    assert.deepEqual(h.decisions, [['new', 'approved']])
    h.requests.at(-1).resolve({ users: [], total: 0 }); await approval
    h.state.prepare(user('alice'), 'deactivate')
    const deactivation = h.state.confirmAction(); await flush()
    assert.deepEqual(h.updates, [['alice', { is_active: false }]])
    h.requests.at(-1).resolve({ users: [], total: 0 }); await deactivation
  } finally { h.close() }
})

test('unmount cancels the private user list and discards a late response', async () => {
  const h = harness()
  const pending = h.state.load(); h.close()
  assert.equal(h.requests[0].args[1].aborted, true)
  h.requests[0].resolve({ users: [user('alice')], total: 1 }); await pending
  assert.deepEqual(h.state.users.value, [])
})

function accountHarness() {
  const updates = [], passwordChanges = [], cleared = [], disposers = []
  const owner = vue.ref(user('alice'))
  const state = loadComponent('../src/components/settings/AccountProfileSettings.vue', {
    vue: { ...vue, onUnmounted: callback => disposers.push(callback) },
    '@/api/auth': { changeFullname: async value => updates.push(value), changePassword: async value => passwordChanges.push(value) },
    '@/composables/useAuth': { useAuth: () => ({ currentUser: owner, loadCurrentUser: async () => { owner.value = { ...owner.value, fullname: updates.at(-1).fullname } }, clearAuth: () => cleared.push('cleared'), logout: async () => {} }) },
  })
  return { state, updates, passwordChanges, cleared, close: () => disposers.forEach(dispose => dispose()) }
}

test('profile saves validated names to the current account and refreshes the visible identity', async () => {
  const h = accountHarness()
  try {
    h.state.fullname.value = ' x '; await h.state.saveName()
    assert.equal(h.updates.length, 0)
    h.state.fullname.value = '  科研用户  '; await h.state.saveName()
    assert.deepEqual(h.updates, [{ fullname: '科研用户' }])
    assert.equal(h.state.currentUser.value.fullname, '科研用户')
    assert.equal(h.state.profileNotice.value, '姓名已保存。')
  } finally { h.close() }
})

test('password confirmation prevents mismatches and clears credentials after a successful change', async () => {
  const previous = globalThis.window, navigation = [], h = accountHarness()
  globalThis.window = { location: { assign: path => navigation.push(path) } }
  try {
    h.state.oldPassword.value = 'old-secret'; h.state.newPassword.value = 'new-secret'; h.state.confirmPassword.value = 'different'
    await h.state.savePassword()
    assert.equal(h.passwordChanges.length, 0)
    h.state.confirmPassword.value = 'new-secret'; await h.state.savePassword()
    assert.deepEqual(h.passwordChanges, [{ old_password: 'old-secret', new_password: 'new-secret' }])
    assert.deepEqual(h.cleared, ['cleared'])
    assert.deepEqual(navigation, ['/login?password_changed=1'])
    h.close()
    assert.equal(h.state.newPassword.value, '')
    assert.equal(h.state.oldPassword.value, '')
  } finally { globalThis.window = previous; h.close() }
})

test('administrator task navigation respects content authorization independently of admin role', () => {
  const navigation = []
  const state = loadComponent('../src/components/admin/TaskManagement.vue', {
    vue: { ...vue, onMounted() {} },
    'vue-router': { useRouter: () => ({ push: path => navigation.push(path) }) },
    './AdminPager.vue': {}, '@/api/admin': { listAdminTasks: async () => ({ tasks: [], total: 0 }) },
    '@/utils/toast': { showErrorToast() {} },
  })
  state.openReplay({ session_id: 'private', can_replay: false })
  state.openReplay({ session_id: 'owned', can_replay: true })
  assert.deepEqual(navigation, ['/chat/admin/tasks/owned/replay'])
})
