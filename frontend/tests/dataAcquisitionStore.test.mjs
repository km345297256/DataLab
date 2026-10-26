import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import * as vue from 'vue';
import ts from 'typescript';

function compile(path) {
  return ts.transpileModule(readFileSync(new URL(path, import.meta.url), 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText;
}
const code = compile('../src/composables/useDataAcquisition.ts');
const filesCode = compile('../src/utils/explorationFiles.ts');
const file = (id = 'file') => ({ file_id: id, filename: `${id}.csv`, size: 12, upload_date: '' });
const job = (id = 'job', search_id = 'first', session_id = 'session', status = 'downloading', fields = {}) => ({
  id, search_id, session_id, status, candidate_id: `candidate-${id}`,
  items: [{ id: 'item', filename: 'data.csv', status: status === 'ready' ? 'ready' : 'downloading', bytes_received: 5 }], files: [], ...fields,
});
const search = (id = 'first', session_id = 'session', fields = {}) => ({
  id, session_id, query: `query-${id}`, status: 'ready', sources: [], results: [], has_more: false, created_at: '', ...fields,
});
const flush = async () => { await Promise.resolve(); await vue.nextTick(); await Promise.resolve(); };

function harness() {
  const requests = [], events = [], assetEvents = [], links = [], listeners = new Map(), timers = new Map();
  let timerId = 0;
  const api = Object.fromEntries(['listOpenDataJobs', 'listOpenDataSearches', 'changeOpenDataJob'].map(name => [name,
    (...args) => new Promise((resolve, reject) => requests.push({ name, args, resolve, reject })),
  ]));
  const exploration = { exports: {} };
  new Function('module', 'exports', filesCode)(exploration, exploration.exports);
  const module = { exports: {} };
  new Function('require', 'module', 'exports', 'setTimeout', 'clearTimeout', 'window', 'CustomEvent', 'document', code)(name => {
    if (name === 'vue') return vue;
    if (name === '../api/openData') return api;
    if (name === '../api/file') return { getFileDownloadUrl: (...args) => new Promise((resolve, reject) => requests.push({ name: 'download', args, resolve, reject })) };
    if (name === '../utils/explorationFiles') return exploration.exports;
    if (name === '../utils/eventBus') return { eventBus: { on: (name, callback) => listeners.set(name, callback), emit: (...args) => events.push(args) } };
    assert.fail(name);
  }, module, module.exports, (callback, delay) => { timers.set(++timerId, { callback, delay }); return timerId; }, id => timers.delete(id),
  { dispatchEvent: event => assetEvents.push(event.type) }, class CustomEvent { constructor(type) { this.type = type; } },
  { createElement: () => { const link = { click() { links.push({ href: this.href, download: this.download, target: this.target, rel: this.rel }); }, remove() {} }; return link; }, body: { appendChild() {} } });
  const state = module.exports.useDataAcquisition();
  return { state, requests, events, assetEvents, links, listeners, timers, module,
    async start(id = 'session', initialJobs = [], initialSearches = []) {
      const pending = state.activate(id);
      const batch = requests.slice(-2);
      batch.find(row => row.name === 'listOpenDataJobs').resolve(initialJobs);
      batch.find(row => row.name === 'listOpenDataSearches').resolve(initialSearches);
      await pending;
    },
    async settleLists(initialJobs = [], initialSearches = []) {
      const batch = requests.slice(-2);
      batch.find(row => row.name === 'listOpenDataJobs').resolve(initialJobs);
      batch.find(row => row.name === 'listOpenDataSearches').resolve(initialSearches);
      await flush();
    },
    tick() { const [id, timer] = [...timers][0]; timers.delete(id); timer.callback(); },
    close: () => state.activate(),
  };
}

test('singleton activation loads every search and unfiltered job list in parallel', async () => {
  const h = harness();
  try {
    const same = h.module.exports.useDataAcquisition();
    assert.equal(same.jobs, h.state.jobs);
    const pending = h.state.activate('session');
    assert.equal(h.requests.length, 2);
    const request = h.requests.find(row => row.name === 'listOpenDataJobs');
    assert.equal(request.args[0], 'session');
    assert.equal(request.args[1], undefined);
    assert.equal(request.args[2] instanceof AbortSignal, true);
    request.resolve([job('one', 'first'), job('two', 'second'), job('foreign', 'first', 'other')]);
    h.requests.find(row => row.name === 'listOpenDataSearches').resolve([search(), search('second'), search('foreign', 'other')]);
    await pending;
    assert.deepEqual(h.state.jobs.value.map(row => row.id), ['one', 'two']);
    assert.deepEqual(h.state.searches.value.map(row => row.id), ['first', 'second']);
    assert.equal(h.state.panelOpen.value, false);
    h.state.acceptJobs([job('three', 'third')], { searchId: 'third' });
    h.state.acceptJobs([], { searchId: 'first' });
    assert.equal(h.state.jobs.value.length, 3, 'a scoped list cannot erase other searches');
    await h.state.activate('session');
    assert.equal(h.requests.length, 2);
  } finally { await h.close(); }
});

test('switching sessions aborts pending work and rejects late data even after navigating back', async () => {
  const h = harness();
  try {
    const first = h.state.activate('session'), stale = [...h.requests], snapshot = h.state.captureRevision();
    await h.start('other', [job('other', 'different', 'other')], [search('different', 'other')]);
    assert.equal(stale[0].args[1].aborted, true);
    assert.equal(stale[1].args[2].aborted, true);
    stale[0].resolve([search()]); stale[1].resolve([job()]); await first;
    assert.deepEqual(h.state.jobs.value.map(row => row.id), ['other']);
    assert.equal(h.state.acceptJobs([job()]), false);
    await h.start('session');
    assert.equal(h.state.acceptJobs([job()], { revision: snapshot }), false);
    assert.equal(h.state.acceptSearch(search(), { revision: snapshot }), false);
    await h.state.activate();
    const count = h.requests.length;
    assert.equal(h.state.acceptJobs([job()]), false);
    assert.equal(h.state.open(), false);
    await h.state.refresh();
    assert.equal(await h.state.changeJob(job(), 'cancel'), false);
    assert.equal(h.requests.length, count, 'inactive/share routes make no private calls');
  } finally { await h.close(); }
});

test('older list responses cannot roll back newly accepted jobs or source progress', async () => {
  const h = harness();
  try {
    await h.start('session', [job()], [search()]);
    const pending = h.state.refresh(), stale = h.requests.slice(-2), snapshot = h.state.captureRevision();
    h.state.acceptJobs([job('job', 'first', 'session', 'ready', { files: [file()] })]);
    h.state.acceptSearch(search('first', 'session', { effective_query: 'new query' }));
    stale[0].resolve([search()]); stale[1].resolve([job()]); await pending;
    assert.equal(h.state.jobs.value[0].status, 'ready');
    assert.equal(h.state.searches.value[0].effective_query, 'new query');
    assert.equal(h.state.acceptJobs([job()], { revision: snapshot }), false);
    assert.equal(h.state.acceptJobs([job('other', 'second')], { revision: snapshot }), true, 'unrelated new identities remain eligible');
  } finally { await h.close(); }
});

test('provider timestamps reject older records while a newer retry may legitimately reset progress', async () => {
  const h = harness();
  try {
    const newer = '2026-10-21T12:00:02Z', older = '2026-10-21T12:00:01Z';
    await h.start('session', [job('job', 'first', 'session', 'failed', { updated_at: newer })], [search('first', 'session', { updated_at: newer })]);
    assert.equal(h.state.acceptJobs([job('job', 'first', 'session', 'downloading', { updated_at: older })]), false);
    assert.equal(h.state.acceptSearch(search('first', 'session', { updated_at: older })), false);
    assert.equal(h.state.acceptJobs([job('job', 'first', 'session', 'queued', { updated_at: '2026-10-21T12:00:03Z' })]), true);
    assert.equal(h.state.jobs.value[0].status, 'queued');
  } finally { await h.close(); }
});

test('Shanghai browser accepts Mongo naive UTC completion after a microsecond Z POST', async () => {
  const previousTimezone = process.env.TZ;
  process.env.TZ = 'Asia/Shanghai';
  const h = harness();
  try {
    assert.equal(new Date('2026-10-21T09:00:00Z').getTimezoneOffset(), -480);
    const post = '2026-10-21T09:00:00.123456Z', mongoReady = '2026-10-21T09:00:02.456000';
    assert.ok(Date.parse(mongoReady) < Date.parse(post), 'raw Date.parse reproduces the local-time regression');
    await h.start('session', [job('job', 'first', 'session', 'queued', { updated_at: post })], [search('first', 'session', { status: 'searching', updated_at: post })]);
    const pending = h.state.refresh();
    await h.settleLists([job('job', 'first', 'session', 'ready', { updated_at: mongoReady, files: [file()] })],
      [search('first', 'session', { updated_at: mongoReady })]);
    await pending;
    assert.equal(h.state.jobs.value[0].status, 'ready');
    assert.equal(h.state.jobs.value[0].files.length, 1);
    assert.equal(h.state.searches.value[0].status, 'ready');
    assert.equal(h.state.acceptJobs([job('job', 'first', 'session', 'downloading', { updated_at: '2026-10-21T09:00:01.999000' })]), false);
    assert.equal(h.state.acceptSearch(search('first', 'session', { status: 'searching', updated_at: '2026-10-21T09:00:01.999Z' })), false);
    assert.equal(h.state.acceptJobs([job('job', 'first', 'session', 'queued', { updated_at: '2026-10-21T17:00:03.100+08:00' })]), true);
    assert.equal(h.state.acceptJobs([job('job', 'first', 'session', 'failed', { updated_at: '2026-10-21T09:00:02.999Z' })]), false);
    const numericSeconds = Date.parse('2026-10-21T09:00:04Z') / 1000;
    assert.equal(h.state.acceptJobs([job('job', 'first', 'session', 'downloading', { updated_at: numericSeconds })]), true);
    assert.equal(h.state.acceptJobs([job('job', 'first', 'session', 'queued', { updated_at: numericSeconds * 1000 - 1 })]), false);
  } finally {
    await h.close();
    if (previousTimezone === undefined) delete process.env.TZ;
    else process.env.TZ = previousTimezone;
  }
});

test('panel closure keeps polling and completions never reopen or steal focus', async () => {
  const h = harness();
  try {
    await h.start('session', [job()], [search()]);
    assert.equal(h.state.open({ jobId: 'job', automatic: true }), true);
    assert.equal(h.state.focusRevision.value, 0);
    assert.equal(h.state.focusedJobId.value, '');
    h.state.close();
    assert.equal(h.state.open({ automatic: true }), false);
    assert.equal(h.timers.size, 1);
    h.tick();
    await h.settleLists([job('job', 'first', 'session', 'ready', { files: [file()] })], [search()]);
    assert.equal(h.state.panelOpen.value, false);
    assert.equal(h.state.jobs.value[0].status, 'ready');
    assert.equal(h.state.open({ jobId: 'job' }), true);
    assert.equal(h.state.focusedSearchId.value, 'first');
    assert.equal(h.state.focusedJobId.value, 'job');
    assert.equal(h.state.focusRevision.value, 1);
    h.state.externalPanelOpened();
    assert.equal(h.state.panelOpen.value, false);
    assert.equal(h.state.open({ automatic: true }), false);
    h.state.externalPanelClosed();
    assert.equal(h.state.panelOpen.value, false);
    assert.equal(h.state.open({ searchId: 'second' }), true);
    assert.equal(h.state.focusedJobId.value, '');
    h.state.open({ jobId: 'other', automatic: true });
    assert.equal(h.state.focusedSearchId.value, 'second', 'automatic opening never alters explicit focus');
    assert.equal(h.events.filter(([name]) => name === 'open-data-panel-show').length, 4);
    h.state.open({ jobId: 'job' });
    assert.equal(h.state.focusedJobId.value, 'job');
    const previousFocus = h.state.focusRevision.value;
    h.state.close();
    assert.equal(h.state.open(), true);
    assert.equal(h.state.focusedJobId.value, '');
    assert.equal(h.state.focusedSearchId.value, '');
    assert.equal(h.state.focusRevision.value, previousFocus + 1);
    const [, headerOpen] = h.events.at(-1);
    assert.equal(headerOpen.jobId, undefined);
    assert.equal(headerOpen.searchId, undefined);
  } finally { await h.close(); }
});

test('preview occupancy blocks first automatic open but programmatic hide is not user closure', async () => {
  const h = harness();
  try {
    await h.start();
    h.state.externalPanelOpened();
    assert.equal(h.state.open({ automatic: true }), false);
    h.state.externalPanelClosed();
    assert.equal(h.state.open({ automatic: true }), true);
    h.state.externalPanelOpened();
    h.state.externalPanelClosed();
    assert.equal(h.state.panelOpen.value, false);
    assert.equal(h.state.open(), true);
  } finally { await h.close(); }
});

test('cancel and retry reject old polls, preserve all searches, and refresh without opening', async () => {
  const h = harness();
  try {
    await h.start('session', [job(), job('second', 'other')]);
    const snapshot = h.state.captureRevision();
    const cancelling = h.state.changeJob(job(), 'cancel');
    assert.equal(h.state.actionBusy.value, 'cancel:job');
    assert.equal(await h.state.changeJob(job(), 'cancel'), false);
    assert.equal(h.state.acceptJobs([job('job', 'first', 'session', 'ready')]), false, 'busy job rejects external snapshots');
    h.requests.at(-1).resolve(job('job', 'first', 'session', 'cancelled'));
    assert.equal(await cancelling, true);
    assert.equal(h.state.jobs.value[0].status, 'cancelled');
    assert.equal(h.state.acceptJobs([job()], { revision: snapshot }), false);
    await h.settleLists([job('job', 'first', 'session', 'cancelled'), job('second', 'other')]);
    const retrySnapshot = h.state.captureRevision();
    const retrying = h.state.changeJob(h.state.jobs.value[0], 'retry');
    h.requests.at(-1).resolve(job('job', 'first', 'session', 'queued'));
    assert.equal(await retrying, true);
    assert.equal(h.state.acceptJobs([job('job', 'first', 'session', 'cancelled')], { revision: retrySnapshot }), false);
    assert.equal(h.state.jobs.value[0].status, 'queued');
    assert.equal(h.state.jobs.value.length, 2);
    assert.equal(h.state.panelOpen.value, false);
  } finally { await h.close(); }
});

test('failed or foreign action responses preserve files and stale actions cannot enter a new session', async () => {
  const h = harness();
  try {
    await h.start('session', [job()]);
    const invalid = h.state.changeJob(job(), 'cancel');
    h.requests.at(-1).resolve(job('different', 'first', 'session', 'cancelled'));
    assert.equal(await invalid, false);
    assert.match(h.state.actionError.value, /尚未确认/);
    assert.equal(h.state.jobs.value[0].status, 'downloading');
    await h.settleLists([job()]);
    const pending = h.state.changeJob(job(), 'cancel'), request = h.requests.at(-1);
    await h.start('other');
    assert.equal(request.args[3].aborted, true);
    request.resolve(job('job', 'first', 'session', 'cancelled'));
    assert.equal(await pending, false);
    assert.deepEqual(h.state.jobs.value, []);
    assert.equal(h.state.actionBusy.value, '');
  } finally { await h.close(); }
});

test('analysis handoff uses the original search, validates canonical files, and waits for acknowledgment', async () => {
  const h = harness();
  try {
    const ready = job('ready', 'second', 'session', 'ready', { files: [file()] });
    await h.start('session', [ready], [search(), search('second')]);
    assert.equal(h.state.useFiles({ ...ready, files: [file('forged')] }), true);
    const [name, payload] = h.events.at(-1);
    assert.equal(name, 'connector-analysis-draft');
    assert.equal(payload.query, 'query-second');
    assert.deepEqual(payload.files.map(row => row.file_id), ['file']);
    assert.equal(h.state.preparedJobs.value.size, 0);
    payload.onAccepted(true);
    assert.equal(h.state.preparedJobs.value.has('ready'), true);
    h.state.acceptJobs([{ ...ready, files: Array.from({ length: 33 }, (_, index) => file(String(index))) }]);
    assert.equal(h.state.useFiles(ready), false);
    assert.match(h.state.actionError.value, /32/);
    await h.start('other');
    payload.onAccepted(false);
    assert.equal(h.state.actionError.value, '');
    assert.equal(h.state.useFiles(ready), false);
  } finally { await h.close(); }
});

test('signed local download uses canonical file and ignores links arriving after navigation', async () => {
  const h = harness();
  try {
    const ready = job('ready', 'first', 'session', 'ready', { files: [file()] });
    await h.start('session', [ready]);
    assert.equal(await h.state.downloadToLocal(ready, file('unowned')), false);
    const pending = h.state.downloadToLocal(ready, { ...file(), filename: 'forged.txt' });
    assert.equal(h.requests.at(-1).args[0].filename, 'file.csv');
    assert.equal(await h.state.downloadToLocal(ready, file()), false);
    h.requests.at(-1).resolve('https://example.org/signed');
    assert.equal(await pending, true);
    assert.deepEqual(h.links[0], { href: 'https://example.org/signed', download: 'file.csv', target: '_blank', rel: 'noopener noreferrer' });
    const late = h.state.downloadToLocal(ready, file()), stale = h.requests.at(-1);
    await h.start('other');
    stale.resolve('https://example.org/late');
    assert.equal(await late, false);
    assert.equal(h.links.length, 1);
    assert.equal(h.state.downloadingFiles.value.size, 0);
  } finally { await h.close(); }
});

test('an unfinished acquisition does not report an analysis file-count limit', async () => {
  const h = harness();
  try {
    const pending = job();
    await h.start('session', [pending]);
    assert.equal(h.state.useFiles(pending), false);
    assert.match(h.state.actionError.value, /等待获取完成/);
    assert.doesNotMatch(h.state.actionError.value, /32|最多/);
    assert.equal(h.events.some(([name]) => name === 'connector-analysis-draft'), false);
  } finally { await h.close(); }
});

test('analysis acceptance callback runs after preparation, never on refusal or a late confirmation', async () => {
  const h = harness();
  try {
    const ready = job('ready', 'first', 'session', 'ready', { files: [file()] });
    await h.start('session', [ready], [search()]);
    const preparedAtCallback = [];
    const onAccepted = () => preparedAtCallback.push(h.state.preparedJobs.value.has(ready.id));
    h.state.useFiles(ready, onAccepted);
    const rejected = h.events.at(-1)[1];
    rejected.onAccepted(false);
    assert.deepEqual(preparedAtCallback, []);
    assert.equal(h.state.preparedJobs.value.has(ready.id), false);
    h.state.useFiles(ready, onAccepted);
    h.events.at(-1)[1].onAccepted(true);
    assert.deepEqual(preparedAtCallback, [true], 'preparedJobs is updated before the success callback');
    h.state.useFiles(ready, onAccepted);
    const late = h.events.at(-1)[1];
    await h.start('other');
    late.onAccepted(true);
    late.onAccepted(false);
    assert.deepEqual(preparedAtCallback, [true]);
    assert.equal(h.state.preparedJobs.value.size, 0);
    assert.equal(h.state.actionError.value, '');
  } finally { await h.close(); }
});

test('partial polling failures retain data, assets emit once, and SSE refresh stays session scoped', async () => {
  const h = harness();
  try {
    const ready = job('ready', 'first', 'session', 'ready', { files: [file()], asset_id: 'asset' });
    await h.start('session', [ready]);
    h.state.acceptJobs([ready]);
    assert.deepEqual(h.assetEvents, ['data-library-changed']);
    const pending = h.state.refresh();
    h.requests.at(-2).reject(new Error('/private/path'));
    h.requests.at(-1).resolve([]);
    await pending;
    assert.equal(h.state.jobs.value.length, 1);
    assert.match(h.state.error.value, /已有结果仍保留/);
    assert.doesNotMatch(h.state.error.value, /private/);
    const before = h.requests.length;
    h.listeners.get('open-data-updated')({ sessionId: 'other' });
    assert.equal(h.requests.length, before);
    h.listeners.get('open-data-updated')({ sessionId: 'session' });
    assert.equal(h.requests.length, before + 2);
    await h.settleLists([ready]);
    assert.equal(h.state.error.value, '');
    assert.deepEqual(h.assetEvents, ['data-library-changed']);
    assert.equal(h.state.panelOpen.value, false);
  } finally { await h.close(); }
});

test('job listing omits search_id entirely for session-wide requests', async () => {
  const requests = [], module = { exports: {} };
  new Function('require', 'module', 'exports', compile('../src/api/openData.ts'))(() => ({ apiClient: {
    get: async (...args) => { requests.push(args); return { data: { data: [] } }; },
  } }), module, module.exports);
  const signal = new AbortController().signal;
  await module.exports.listOpenDataJobs('session', undefined, signal);
  assert.deepEqual(requests[0], ['/sessions/session/acquisitions', { signal }]);
  await module.exports.listOpenDataJobs('session', 'search', signal);
  assert.deepEqual(requests[1][1], { params: { search_id: 'search' }, signal });
});

test('identity cleanup cancels polling, hides private downloads, and rejects late jobs', async () => {
  const h = harness()
  try {
    await h.start('alice-session', [job('alice-job', 'first', 'alice-session')], [search('first', 'alice-session')])
    h.state.open()
    const pending = h.state.refresh(), oldRequests = h.requests.slice(-2)
    h.listeners.get('auth:cleared')()
    assert.equal(h.state.sessionId.value, undefined)
    assert.equal(h.state.panelOpen.value, false)
    assert.equal(h.state.jobs.value.length, 0)
    assert.equal(h.timers.size, 0)
    oldRequests[0].resolve([search('first', 'alice-session')]); oldRequests[1].resolve([job('alice-job', 'first', 'alice-session')]); await pending
    assert.equal(h.state.jobs.value.length, 0)
  } finally { await h.close() }
})
