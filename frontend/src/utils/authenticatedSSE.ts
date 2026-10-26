import { authSessionKey, ensureAccessToken } from './authTokens.ts';
import { eventBus } from './eventBus.ts';
import { startSSEConnection, type SSECallbacks, type SSEOptions } from './sseConnection.ts';

/** A stream and all of its retries belong to the identity that opened it. */
export function startAuthenticatedSSE<T>(
  url: string,
  baseUrl: string,
  options: SSEOptions,
  callbacks: SSECallbacks<T>,
  start: typeof startSSEConnection = startSSEConnection,
) {
  const owner = authSessionKey();
  let active = true;
  const isCurrent = () => active && owner === authSessionKey();
  const assertCurrent = () => { if (!isCurrent()) throw new Error('账号已切换，请重新打开分析。'); };
  const headers = { ...options.headers };
  // A caller-provided header must not survive logout or an identity transition.
  for (const key of Object.keys(headers)) if (['authorization', 'x-api-key'].includes(key.toLowerCase())) delete headers[key];
  const connection = start<T>(url, {
    ...options,
    headers,
    getBody: () => { assertCurrent(); return options.getBody ? options.getBody() : options.body; },
    getHeaders: async (): Promise<Record<string, string>> => {
      assertCurrent();
      const token = await ensureAccessToken(baseUrl);
      assertCurrent();
      return token ? { Authorization: `Bearer ${token}` } : {};
    },
    isComplete: options.isComplete ? () => !isCurrent() || options.isComplete!() : undefined,
  }, {
    onOpen: () => { if (isCurrent()) callbacks.onOpen?.(); },
    onMessage: event => { if (isCurrent()) callbacks.onMessage?.(event); },
    onClose: () => { if (isCurrent()) callbacks.onClose?.(); },
    onError: error => { if (isCurrent()) callbacks.onError?.(error); },
    onRetry: state => { if (isCurrent()) callbacks.onRetry?.(state); },
  });
  const cancel = () => { active = false; eventBus.off('auth:cleared', cancel); connection.cancel(); };
  eventBus.on('auth:cleared', cancel);
  void connection.finished.then(() => eventBus.off('auth:cleared', cancel), () => eventBus.off('auth:cleared', cancel));
  return { cancel, finished: connection.finished };
}
