// Backend API client configuration
import axios from 'axios';
import type { AxiosError } from 'axios';
import { accessToken, authSessionKey, refreshAccessToken } from '../utils/authTokens.ts';
import { ApiError, apiResponseMessage, normalizeApiError } from '../utils/apiError.ts';
import { startAuthenticatedSSE } from '../utils/authenticatedSSE.ts';
import type { SSECallbacks, SSEOptions } from '../utils/sseConnection.ts';

export { ApiError } from '../utils/apiError.ts';
export type { SSECallbacks, SSEOptions } from '../utils/sseConnection.ts';

// API configuration
export const API_CONFIG = {
  host: import.meta.env?.VITE_API_URL || '',
  version: 'v1',
  timeout: 30000, // Request timeout in milliseconds
};

// Complete API base URL
export const BASE_URL = API_CONFIG.host 
  ? `${API_CONFIG.host}/api/${API_CONFIG.version}` 
  : `/api/${API_CONFIG.version}`;

// Unified response format
export interface ApiResponse<T> {
  code: number;
  msg: string;
  data: T;
}

// Create axios instance
export const apiClient = axios.create({
  baseURL: BASE_URL,
  timeout: API_CONFIG.timeout,
  headers: {
    'Content-Type': 'application/json',
  },
});

const publicAuthEndpoint = (url = '') => /^\/auth\/(login|register|refresh|status|send-verification-code|reset-password)(?:\?|$)/.test(url);
type SessionRequest = { _authRetried?: boolean; _authSession?: string | null };

apiClient.interceptors.request.use((config) => {
  (config as typeof config & SessionRequest)._authSession = authSessionKey();
  const token = accessToken();
  if (token) config.headers.set('Authorization', `Bearer ${token}`);
  else config.headers.delete('Authorization');
  config.headers.delete('X-API-Key');
  return config;
});

// Response interceptor, unified error handling.
apiClient.interceptors.response.use(
  (response) => {
    const config = response.config as typeof response.config & SessionRequest;
    if (!publicAuthEndpoint(config.url) && config._authSession !== authSessionKey()) {
      return Promise.reject(new ApiError('账号已切换，请重新执行操作。', { status: 409 }));
    }
    // Check backend response format
    if (response.data && typeof response.data.code === 'number') {
      // If it's a business logic error (code not 0), convert to error handling
      if (response.data.code !== 0) {
        const apiError = new ApiError(apiResponseMessage(response.data) ?? 'Unknown error', {
          status: response.status,
          code: response.data.code,
          details: response.data,
        });
        return Promise.reject(apiError);
      }
    }
    return response;
  },
  async (error: AxiosError) => {
    const config = error.config as (typeof error.config & SessionRequest);
    if (config && !publicAuthEndpoint(config.url) && config._authSession !== authSessionKey()) {
      return Promise.reject(new ApiError('账号已切换，请重新执行操作。', { status: 409 }));
    }
    if (error.response?.status === 401 && config && !config._authRetried && !publicAuthEndpoint(config.url)) {
      config._authRetried = true;
      const token = await refreshAccessToken(BASE_URL);
      if (config._authSession !== authSessionKey()) {
        return Promise.reject(new ApiError('登录已失效或账号已切换，请重新登录。', { status: 401 }));
      }
      if (token) {
        config.headers.set('Authorization', `Bearer ${token}`);
        return apiClient.request(config);
      }
    }
    const apiError = normalizeApiError(error);

    // Request/response objects may carry credential-form input. Log only the
    // status, never Axios config, API details, or provider error bodies.
    console.error('API request failed', apiError.status ?? 'network');
    return Promise.reject(apiError);
  }
); 

/**
 * Generic SSE connection function
 * @param endpoint - API endpoint (relative to BASE_URL)
 * @param options - Request options
 * @param callbacks - Event callbacks
 * @returns Function to cancel the SSE connection
 */
export const createSSEConnection = async <T = any>(
  endpoint: string,
  options: SSEOptions = {},
  callbacks: SSECallbacks<T> = {}
): Promise<() => void> => {
  const connection = startAuthenticatedSSE(`${BASE_URL}${endpoint}`, BASE_URL, options, callbacks);
  // Caller failures must not leak provider bodies or credential-bearing errors.
  void connection.finished.catch(() => console.error('SSE callback failed'));
  return connection.cancel;
}; 
