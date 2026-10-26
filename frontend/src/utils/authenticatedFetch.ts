import { authSessionKey, ensureAccessToken, refreshAccessToken } from './authTokens.ts';

function assertActive(signal?: AbortSignal | null): void {
  if (signal?.aborted) throw signal.reason ?? new DOMException('Request cancelled', 'AbortError');
}

// Credential refresh is shared with other active requests. Closing one preview
// stops waiting immediately without aborting that shared refresh.
function awaitCredentials(request: Promise<string | null>, signal?: AbortSignal | null): Promise<string | null> {
  if (!signal) return request;
  return new Promise((resolve, reject) => {
    const abort = () => reject(signal.reason ?? new DOMException('Request cancelled', 'AbortError'));
    signal.addEventListener('abort', abort, { once: true });
    request.then(resolve, reject).finally(() => signal.removeEventListener('abort', abort));
    if (signal.aborted) abort();
  });
}

/**
 * Fetch a private API resource with the same credentials as Axios. The caller
 * supplies the configured API base; signed download URLs use ordinary fetch.
 * Bodies must be replayable (JSON strings are used by the preview endpoints).
 */
export async function authenticatedApiFetch(
  baseUrl: string,
  input: string | URL,
  init: RequestInit = {},
): Promise<Response> {
  const location = typeof window !== 'undefined' ? window.location?.href : undefined;
  const base = new URL(baseUrl, location);
  const target = new URL(input, base);
  const prefix = base.pathname.replace(/\/+$/, '');
  // Never send a user's Bearer token to a file URL, another origin or a path
  // outside the API, including URL-normalized directory traversal.
  if (!['http:', 'https:'].includes(base.protocol) || base.username || base.password
    || target.origin !== base.origin || target.username || target.password
    || !(target.pathname === prefix || target.pathname.startsWith(`${prefix}/`))) {
    throw new Error('Authenticated requests must remain within the configured API.');
  }
  assertActive(init.signal);
  const owner = authSessionKey();
  const assertOwner = () => {
    if (owner !== authSessionKey()) throw new DOMException('Account changed', 'AbortError');
  };
  const token = await awaitCredentials(ensureAccessToken(baseUrl), init.signal);
  assertActive(init.signal);
  assertOwner();
  const send = (credential: string | null) => {
    const headers = new Headers(init.headers);
    headers.delete('Authorization');
    if (credential) headers.set('Authorization', `Bearer ${credential}`);
    // API endpoints do not redirect. Fail closed rather than forwarding
    // credentials to a redirect target outside the checked API namespace.
    return fetch(target.href, { ...init, headers, redirect: 'error' });
  };
  const response = await send(token);
  if (owner !== authSessionKey()) {
    await response.body?.cancel().catch(() => undefined);
    assertOwner();
  }
  if (response.status !== 401 || !token) return response;
  try {
    assertActive(init.signal);
    const refreshed = await awaitCredentials(refreshAccessToken(baseUrl), init.signal);
    assertActive(init.signal);
    assertOwner();
    if (!refreshed) return response;
    await response.body?.cancel();
    assertActive(init.signal);
    const retried = await send(refreshed);
    if (owner !== authSessionKey()) {
      await retried.body?.cancel().catch(() => undefined);
      assertOwner();
    }
    return retried;
  } catch (error) {
    await response.body?.cancel().catch(() => undefined);
    throw error;
  }
}
