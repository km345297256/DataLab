/** Browser credentials shared by Axios, streaming chat and multipart uploads. */
const ACCESS = 'access_token'
const REFRESH = 'refresh_token'
let refreshInFlight: { owner: string; promise: Promise<string | null> } | null = null

export function accessToken(): string | null {
  return typeof localStorage === 'undefined' ? null : localStorage.getItem(ACCESS)
}

/** Stable through access-token refresh, different after every account login. */
export function authSessionKey(): string | null {
  return typeof localStorage === 'undefined' ? null : localStorage.getItem(REFRESH) || accessToken()
}

export function clearTokens(): void {
  localStorage.removeItem(ACCESS)
  localStorage.removeItem(REFRESH)
}

function expireSession(): void {
  clearTokens()
  window.dispatchEvent(new Event('datalab:auth-expired'))
}

export async function refreshAccessToken(baseUrl: string): Promise<string | null> {
  const refresh = localStorage.getItem(REFRESH)
  if (!refresh) {
    if (accessToken()) expireSession()
    return null
  }
  if (refreshInFlight?.owner === refresh) return refreshInFlight.promise
  const operation = (async () => {
    try {
      const response = await fetch(`${baseUrl}/auth/refresh`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ refresh_token: refresh }),
      })
      const body = await response.json()
      if (!response.ok || body.code !== 0 || typeof body.data?.access_token !== 'string') {
        throw new Error('Session expired')
      }
      // A logout or account change during refresh must never restore old credentials.
      if (localStorage.getItem(REFRESH) !== refresh) return null
      localStorage.setItem(ACCESS, body.data.access_token)
      return body.data.access_token as string
    } catch {
      if (localStorage.getItem(REFRESH) === refresh) expireSession()
      return null
    } finally {
      if (refreshInFlight?.owner === refresh) refreshInFlight = null
    }
  })()
  refreshInFlight = { owner: refresh, promise: operation }
  return operation
}

export async function ensureAccessToken(baseUrl: string): Promise<string | null> {
  const token = accessToken()
  if (!token) return localStorage.getItem(REFRESH) ? refreshAccessToken(baseUrl) : null
  try {
    const payload = JSON.parse(atob(token.split('.')[1].replace(/-/g, '+').replace(/_/g, '/')))
    if (Number(payload.exp) * 1000 > Date.now() + 30_000) return token
  } catch { /* Invalid credentials will be refreshed or rejected by the API. */ }
  return refreshAccessToken(baseUrl)
}
