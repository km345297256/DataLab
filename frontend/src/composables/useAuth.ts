import { computed, ref, watch } from 'vue'
import * as authApi from '../api/auth'
import type { User, LoginRequest, RegisterRequest } from '../api/auth'
import { BASE_URL } from '../api/client'
import { refreshAccessToken } from '../utils/authTokens'
import { clearPendingChats } from './usePendingChat'
import { clearPendingLibraryFiles } from './useDataSelection'
import { analysisDraftStore } from '../utils/analysisDrafts'
import { eventBus } from '../utils/eventBus'

const currentUser = ref<User | null>(null)
watch(() => currentUser.value?.id ?? null, owner => analysisDraftStore.setOwner(owner), { flush: 'sync' })
const isLoading = ref(false)
const authError = ref<string | null>(null)
let initialized = false
let initializing: Promise<void> | null = null
let identityRevision = 0

function clearIdentityState() {
  identityRevision += 1
  authApi.clearAuthToken()
  currentUser.value = null
  initialized = false
  authError.value = null
  clearPendingChats()
  clearPendingLibraryFiles()
  analysisDraftStore.clear()
  eventBus.emit('auth:cleared')
}

function clearAuth() {
  authApi.clearStoredTokens()
  clearIdentityState()
}

if (typeof window !== 'undefined') {
  window.addEventListener('datalab:auth-expired', clearAuth)
  window.addEventListener('storage', (event) => {
    if (event.key === 'refresh_token' && event.oldValue !== event.newValue) {
      // Other tabs must discard all cached content before displaying a new identity.
      clearIdentityState()
      window.location.reload()
    }
  })
}

export function useAuth() {
  const loadCurrentUser = async () => {
    const revision = identityRevision
    const token = authApi.getStoredRefreshToken() || authApi.getStoredToken()
    const user = await authApi.getCurrentUser()
    if (revision === identityRevision && token === (authApi.getStoredRefreshToken() || authApi.getStoredToken())) currentUser.value = user
  }
  const initAuth = async () => {
    if (initialized) return
    if (initializing) return initializing
    const revision = identityRevision
    const operation = (async () => {
      try {
        if (authApi.getStoredToken() || authApi.getStoredRefreshToken()) {
          // Restore fresh credentials before asking the server for this identity.
          const { ensureAccessToken } = await import('../utils/authTokens')
          if (await ensureAccessToken(BASE_URL)) await loadCurrentUser()
        }
      } catch {
        if (revision === identityRevision) clearAuth()
      } finally {
        if (revision === identityRevision) initialized = true
      }
    })()
    initializing = operation
    try { await operation }
    finally { if (initializing === operation) initializing = null }
  }
  const login = async (credentials: LoginRequest) => {
    const revision = ++identityRevision
    isLoading.value = true
    authError.value = null
    try {
      const result = await authApi.login(credentials)
      if (revision !== identityRevision) throw new Error('账号已切换，请重新登录。')
      clearAuth()
      authApi.storeToken(result.access_token)
      authApi.storeRefreshToken(result.refresh_token)
      currentUser.value = result.user
      initialized = true
      return result
    } catch (error) {
      if (revision === identityRevision) authError.value = error instanceof Error ? error.message : '登录失败'
      throw error
    } finally { isLoading.value = false }
  }
  const register = async (data: RegisterRequest) => {
    isLoading.value = true
    authError.value = null
    try { return await authApi.register(data) }
    catch (error) {
      authError.value = error instanceof Error ? error.message : '注册失败'
      throw error
    } finally { isLoading.value = false }
  }
  const logout = async (_silent = false) => {
    const revision = ++identityRevision
    try { await authApi.logout() }
    finally {
      if (revision === identityRevision) {
        clearAuth()
        // Destroy mounted chat/preview state as well as the shared composables.
        window.location.assign('/login')
      }
    }
  }
  const hasRole = (role: string) => currentUser.value?.role === role
  return {
    currentUser: computed(() => currentUser.value),
    isAuthenticated: computed(() => currentUser.value !== null),
    isLoading: computed(() => isLoading.value),
    authError: computed(() => authError.value),
    isAdmin: computed(() => hasRole('admin')),
    isActive: computed(() => currentUser.value?.is_active === true),
    login, register, logout, initAuth, loadCurrentUser,
    refreshAuthToken: async () => Boolean(await refreshAccessToken(BASE_URL)),
    hasRole, clearError: () => { authError.value = null }, clearAuth,
  }
}
