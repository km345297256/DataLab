import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

import { ApiError, apiClient } from '../src/api/client.ts';


function adapterResponse(config, data, status = 200, statusText = 'OK') {
  return {
    config,
    data,
    headers: {},
    request: {},
    status,
    statusText,
  };
}


test('business failures are rejected as ApiError instances', async () => {
  const failure = await apiClient.request({
    url: '/test-business-error',
    adapter: async (config) => adapterResponse(config, {
      code: 400,
      msg: 'Dataset source directory does not exist',
      data: null,
    }),
  }).catch((error) => error);

  assert.ok(failure instanceof Error);
  assert.ok(failure instanceof ApiError);
  assert.equal(failure.status, 200);
  assert.equal(failure.code, 400);
  assert.equal(failure.message, 'Dataset source directory does not exist');
  assert.equal(failure.details.code, 400);
});


test('Axios-like failures are rejected as ApiError instances with response metadata', async () => {
  const details = {
    detail: [{ loc: ['body', 'name'], msg: 'Field required' }],
  };
  const originalConsoleError = console.error;
  console.error = () => {};

  try {
    const failure = await apiClient.request({
      url: '/test-http-error',
      adapter: async (config) => Promise.reject({
        message: 'Request failed with status code 422',
        request: {},
        response: adapterResponse(config, details, 422, 'Unprocessable Entity'),
      }),
    }).catch((error) => error);

    assert.ok(failure instanceof Error);
    assert.ok(failure instanceof ApiError);
    assert.equal(failure.status, 422);
    assert.equal(failure.code, 422);
    assert.equal(failure.message, 'Field required');
    assert.equal(failure.details, details);
  } finally {
    console.error = originalConsoleError;
  }
});


test('requests never send legacy authentication headers', async () => {
  await apiClient.request({
    url: '/test-no-auth-headers',
    headers: {
      Authorization: 'Bearer legacy-token',
      'X-API-Key': 'legacy-key',
    },
    adapter: async (config) => {
      assert.equal(config.headers.get('Authorization'), undefined);
      assert.equal(config.headers.get('X-API-Key'), undefined);
      return adapterResponse(config, { code: 0, msg: 'success', data: null });
    },
  });
});


test('API transports contain no SSO redirect dependency', async () => {
  const source = await readFile(new URL('../src/api/client.ts', import.meta.url), 'utf8');

  assert.doesNotMatch(source, /space\.4fair\.cn|requireSSOToken|redirectToSSO/);
});


test('requests attach the current account token, replacing stale headers', async () => {
  const previous = globalThis.localStorage;
  globalThis.localStorage = { getItem: key => key === 'access_token' ? 'current-account' : null };
  try {
    await apiClient.request({
      url: '/private-data', headers: { Authorization: 'Bearer stale-account' },
      adapter: async config => {
        assert.equal(config.headers.get('Authorization'), 'Bearer current-account');
        return adapterResponse(config, { code: 0, msg: 'success', data: null });
      },
    });
  } finally { globalThis.localStorage = previous; }
});

test('a late private response from another account is never exposed to the new account', async () => {
  const previous = globalThis.localStorage
  let owner = 'alice'
  globalThis.localStorage = { getItem: key => `${owner}-${key}` }
  try {
    await assert.rejects(apiClient.request({
      url: '/private-data',
      adapter: async config => {
        owner = 'bob'
        return adapterResponse(config, { code: 0, msg: 'success', data: { secret: 'alice-file' } })
      },
    }), error => error.status === 409 && error.message.includes('账号已切换'))
  } finally { globalThis.localStorage = previous }
})

test('a stale unauthorized mutation cannot refresh and replay using another account', async () => {
  const previous = { storage: globalThis.localStorage, fetch: globalThis.fetch }
  let owner = 'alice', attempts = 0
  globalThis.localStorage = { getItem: key => `${owner}-${key}` }
  globalThis.fetch = () => assert.fail('must not refresh Bob for Alice mutation')
  try {
    await assert.rejects(apiClient.request({
      method: 'POST', url: '/private-data', data: { name: 'alice-private-file' },
      adapter: async config => {
        attempts++; owner = 'bob'
        throw { config, response: adapterResponse(config, {}, 401) }
      },
    }), error => error.status === 409)
    assert.equal(attempts, 1)
  } finally { globalThis.localStorage = previous.storage; globalThis.fetch = previous.fetch }
})
