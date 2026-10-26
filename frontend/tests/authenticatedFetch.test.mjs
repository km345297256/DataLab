import assert from 'node:assert/strict';
import test from 'node:test';
import { authenticatedApiFetch } from '../src/utils/authenticatedFetch.ts';

const token = (name = 'current', expires = 300) =>
  `${name}.${Buffer.from(JSON.stringify({ exp: Date.now() / 1000 + expires })).toString('base64url')}.signature`;
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return { promise, resolve }; };
const refreshed = access => new Response(JSON.stringify({ code: 0, data: { access_token: access } }));

function browser(t) {
  const previous = { storage: globalThis.localStorage, window: globalThis.window, fetch: globalThis.fetch };
  const values = new Map([['access_token', token()], ['refresh_token', 'current-refresh']]);
  globalThis.localStorage = {
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: key => values.delete(key),
  };
  globalThis.window = { location: { href: 'http://localhost:10030/chat' }, dispatchEvent() {} };
  t.after(() => {
    globalThis.localStorage = previous.storage; globalThis.window = previous.window; globalThis.fetch = previous.fetch;
  });
  return values;
}

test('owner API fetch carries Bearer and preserves the bounded preview body and abort signal', async t => {
  const values = browser(t), controller = new AbortController();
  const headers = new Headers({ 'Content-Type': 'application/json', Authorization: 'Bearer ignored' });
  globalThis.fetch = async (url, init) => {
    assert.equal(url, 'http://localhost:10030/api/v1/files/owned/visualization');
    assert.equal(init.headers.get('Authorization'), `Bearer ${values.get('access_token')}`);
    assert.equal(init.headers.get('Content-Type'), 'application/json');
    assert.equal(init.body, '{"operation":"page"}');
    assert.equal(init.signal, controller.signal);
    assert.equal(init.redirect, 'error');
    return new Response('private page');
  };
  const result = await authenticatedApiFetch('/api/v1', '/api/v1/files/owned/visualization', {
    method: 'POST', body: '{"operation":"page"}', headers, signal: controller.signal,
  });
  assert.equal(await result.text(), 'private page');
  assert.equal(headers.get('Authorization'), 'Bearer ignored', 'caller headers remain immutable');
});

test('a rejected token is refreshed once and the response stream is released before retry', async t => {
  browser(t);
  const renewed = token('renewed'), calls = [];
  let cancelled = false;
  globalThis.fetch = async (url, init) => {
    calls.push({ url, init });
    if (String(url).endsWith('/auth/refresh')) return refreshed(renewed);
    if (calls.length === 1) return new Response(new ReadableStream({ cancel() { cancelled = true; } }), { status: 401 });
    assert.equal(cancelled, true);
    assert.equal(init.headers.get('Authorization'), `Bearer ${renewed}`);
    return new Response('still denied', { status: 401 });
  };
  const result = await authenticatedApiFetch('/api/v1', '/api/v1/files/owned/visualization', {
    method: 'POST', body: '{"operation":"bytes"}',
  });
  assert.equal(result.status, 401);
  assert.equal(calls.length, 3, 'second 401 does not start a refresh loop');
  assert.equal(calls[0].init.body, calls[2].init.body);
});

test('foreign origins, non-API paths and credential URLs never receive an authenticated request', async t => {
  browser(t);
  let calls = 0; globalThis.fetch = async () => { calls++; assert.fail('No network call expected'); };
  for (const url of [
    'https://other.example/api/v1/files/owned', '//other.example/api/v1/files/owned',
    'http://localhost:10031/api/v1/files/owned', '/assets/file.csv', '/api/v10/files/owned',
    '/api/v1/../../private', 'http://user@localhost:10030/api/v1/files/owned',
    'data:text/plain,test',
  ]) {
    await assert.rejects(authenticatedApiFetch('/api/v1', url), /configured API/);
  }
  assert.equal(calls, 0);
});

test('an explicitly configured API origin is allowed without authorizing adjacent origins', async t => {
  browser(t);
  globalThis.fetch = async (url, init) => {
    assert.equal(url, 'https://api.example.test/api/v1/files/owned/visualization');
    assert.ok(init.headers.get('Authorization')?.startsWith('Bearer '));
    return new Response('ok');
  };
  await authenticatedApiFetch('https://api.example.test/api/v1', 'https://api.example.test/api/v1/files/owned/visualization');
  await assert.rejects(authenticatedApiFetch('https://api.example.test/api/v1', 'https://api.example.test.evil/api/v1/files/owned'), /configured API/);
});

test('an already cancelled preview performs no request or credential refresh', async t => {
  browser(t); const controller = new AbortController(); controller.abort();
  globalThis.fetch = async () => assert.fail('No network call expected');
  await assert.rejects(authenticatedApiFetch('/api/v1', '/api/v1/files/owned/visualization', { signal: controller.signal }), { name: 'AbortError' });
});

test('closing while a shared credential refresh is pending aborts promptly and never reopens the preview', async t => {
  const values = browser(t), refresh = deferred(), started = deferred(), controller = new AbortController();
  values.set('access_token', token('expired', -100));
  let reads = 0;
  globalThis.fetch = async url => {
    assert.ok(String(url).endsWith('/auth/refresh')); reads++; started.resolve();
    return refresh.promise;
  };
  const pending = authenticatedApiFetch('/api/v1', '/api/v1/files/owned/visualization', { signal: controller.signal });
  await started.promise; controller.abort();
  await assert.rejects(pending, { name: 'AbortError' });
  refresh.resolve(refreshed(token('renewed')));
  for (let i = 0; i < 20; i++) await Promise.resolve();
  assert.equal(reads, 1);
});

test('an anonymous API request preserves server authority without inventing credentials or retrying', async t => {
  const values = browser(t); values.clear(); let calls = 0;
  globalThis.fetch = async (_url, init) => {
    calls++; assert.equal(init.headers.has('Authorization'), false);
    return new Response('unauthorized', { status: 401 });
  };
  const response = await authenticatedApiFetch('/api/v1', '/api/v1/files/owned/visualization');
  assert.equal(response.status, 401); assert.equal(await response.text(), 'unauthorized'); assert.equal(calls, 1);
});

test('private fetch discards Alice response after switching to Bob and never replays it', async t => {
  const values = browser(t), started = deferred(), pending = deferred()
  let reads = 0
  globalThis.fetch = async () => { reads++; started.resolve(); return pending.promise }
  const request = authenticatedApiFetch('/api/v1', '/api/v1/files/alice/visualization')
  await started.promise
  values.set('access_token', token('bob')); values.set('refresh_token', 'bob-refresh')
  pending.resolve(new Response('Alice private content', { status: 401 }))
  await assert.rejects(request, { name: 'AbortError' })
  assert.equal(reads, 1)
})
