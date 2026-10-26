import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import * as vue from 'vue';
import ts from 'typescript';

function loadModule(path, dependencies = {}) {
  const source = readFileSync(new URL(path, import.meta.url), 'utf8');
  const code = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
  const module = { exports: {} };
  new Function('require', 'module', 'exports', code)(name => {
    if (name in dependencies) return dependencies[name];
    if (name === 'vue') return vue;
    throw new Error(`Unexpected dependency ${name}`);
  }, module, module.exports);
  return module.exports;
}

const treeUtils = loadModule('../src/utils/personalDataTree.ts');
const filterUtils = loadModule('../src/utils/libraryFileFilters.ts', { './personalDataTree': treeUtils });
const { libraryFileFormat, libraryFormatLabel, libraryDateBounds, filterLibraryFiles } = filterUtils;
const file = (id, filename = `${id}.csv`) => ({ file_id: id, filename });
const asset = (id, files = [file(id)], extra = {}) => ({ id, name: id, files, status: 'ready', source_type: 'upload', updated_at: '2026-10-20T12:00:00Z', ...extra });
const summary = (count = 90) => ({ file_count: count, total_bytes: count * 1024, formats: ['csv', 'nc', 'none'], updated_at: '2026-10-20T12:00:00Z' });
const flush = async () => { await Promise.resolve(); await vue.nextTick(); await Promise.resolve(); };

function harness(options = {}) {
  const requests = [], statsRequests = [];
  const owner = vue.ref({ id: 'owner-a' });
  const scope = vue.effectScope();
  const { useLibraryBrowser } = loadModule('../src/composables/useLibraryBrowser.ts', {
    '../api/dataLibrary': {
      listDataLibrary: (params, signal) => new Promise((resolve, reject) => requests.push({ params, signal, resolve, reject })),
      getDataLibraryStats: signal => new Promise((resolve, reject) => statsRequests.push({ signal, resolve, reject })),
    },
    '../utils/libraryFileFilters': filterUtils,
    './useAuth': { useAuth: () => ({ currentUser: owner }) },
  });
  const state = scope.run(() => useLibraryBrowser(options));
  return {
    owner, state, requests, statsRequests,
    async settle(items, next_cursor = null, stats = summary(), listIndex = requests.length - 1, statsIndex = statsRequests.length - 1) {
      requests[listIndex].resolve({ items, next_cursor });
      statsRequests[statsIndex]?.resolve(stats);
      await flush();
    },
    close() { scope.stop(); },
  };
}

test('format parsing handles extensionless files, dotfiles, case and compound extensions consistently', () => {
  const cases = [
    ['README', 'none'], ['.env', 'none'], ['name.', 'none'], ['', 'none'],
    ['.env.local', 'local'], ['daily.NC', 'nc'], ['sheet.CSV', 'csv'],
    ['brain.NII.GZ', 'nii.gz'], ['archive.TAR.GZ', 'tar.gz'], ['plain.gz', 'gz'],
    ['.nii.gz', 'gz'], ['.tar.gz', 'gz'],
    ['/private/secret.csv/README', 'none'], ['C:\\private\\data.JSON', 'json'],
  ];
  for (const [filename, expected] of cases) assert.equal(libraryFileFormat({ filename }), expected, filename);
  assert.equal(libraryFormatLabel('none'), '无扩展名');
  assert.equal(libraryFormatLabel('nc'), 'NetCDF (.nc)');
  assert.equal(libraryFormatLabel('xyz'), 'XYZ');
});

test('local dates include the start day and exclude midnight after the entire end day', () => {
  const bounds = libraryDateBounds('2026-10-20', '2026-10-21');
  assert.equal(bounds.valid, true);
  assert.equal(bounds.from, new Date(2026, 9, 20).toISOString());
  assert.equal(bounds.to, new Date(2026, 9, 22).toISOString());
  const lower = Date.parse(bounds.from), upper = Date.parse(bounds.to);
  const at = (id, millis) => asset(id, undefined, { updated_at: new Date(millis).toISOString() });
  const results = filterLibraryFiles([
    at('before', lower - 1), at('start', lower), at('last', upper - 1), at('after', upper),
    asset('unknown', undefined, { updated_at: 'invalid' }),
  ], '', '', '2026-10-20', '2026-10-21');
  assert.deepEqual(results.map(item => item.id), ['start', 'last']);
  assert.equal(libraryDateBounds('2026-10-20', '2026-10-20').valid, true);
  assert.deepEqual(libraryDateBounds('', ''), { from: undefined, to: undefined, valid: true });
  assert.equal(libraryDateBounds('2026-10-20', '').to, undefined);
  assert.equal(libraryDateBounds('', '2026-10-20').from, undefined);
});

test('date bounds use calendar midnight rather than assuming every local day is 24 hours', () => {
  const originalZone = process.env.TZ;
  process.env.TZ = 'America/New_York';
  try {
    const spring = libraryDateBounds('2026-03-08', '2026-03-08');
    assert.equal(spring.from, '2026-03-08T05:00:00.000Z');
    assert.equal(spring.to, '2026-03-09T04:00:00.000Z');
    assert.equal(Date.parse(spring.to) - Date.parse(spring.from), 23 * 3600 * 1000);
    const autumn = libraryDateBounds('2026-11-01', '2026-11-01');
    assert.equal(Date.parse(autumn.to) - Date.parse(autumn.from), 25 * 3600 * 1000);
  } finally {
    if (originalZone === undefined) delete process.env.TZ; else process.env.TZ = originalZone;
  }
});

test('invalid, impossible and reversed calendar dates cannot silently broaden the filter', () => {
  for (const [from, to] of [
    ['2026-10-22', '2026-10-20'], ['2026-02-29', ''], ['', '2026-04-31'],
    ['2026-13-01', ''], ['2026-00-01', ''], ['2026-10-00', ''],
    ['2026-1-01', ''], ['', '2026-10-20T00:00:00Z'],
  ]) {
    assert.equal(libraryDateBounds(from, to).valid, false, `${from} / ${to}`);
    assert.deepEqual(filterLibraryFiles([asset('anything')], '', '', from, to), []);
  }
  assert.equal(libraryDateBounds('2024-02-29', '2024-02-29').valid, true);
});

test('filename and format conditions must match the same public basename while assets stay complete', () => {
  const files = [file('csv', '/private/secret/daily.CSV'), file('nc', 'monthly.nc'), file('dotfile', '.env')];
  const original = asset('study', files, { name: 'daily.nc', description: 'daily NetCDF' });
  assert.deepEqual(filterLibraryFiles([original], 'daily', 'nc', '', ''), []);
  const matches = filterLibraryFiles([original], ' DAILY ', 'csv', '', '');
  assert.deepEqual(matches[0].files.map(item => item.file_id), ['csv']);
  assert.equal(original.files.length, 3);
  assert.deepEqual(filterLibraryFiles([original], 'secret', '', '', ''), []);
  assert.deepEqual(filterLibraryFiles([original], '', 'none', '', '')[0].files.map(item => item.file_id), ['dotfile']);
});

test('list failure still publishes global statistics and a full retry recovers the list', async () => {
  const h = harness();
  try {
    h.requests[0].reject(new Error('/private/owner-a/server-secret'));
    h.statsRequests[0].resolve(summary());
    await flush();
    assert.deepEqual(h.state.items.value, []);
    assert.equal(h.state.stats.value.file_count, 90);
    assert.equal(h.state.loading.value, false);
    assert.match(h.state.error.value, /读取我的数据/);
    assert.doesNotMatch(h.state.error.value, /private|server-secret/);
    void h.state.load();
    await h.settle([asset('recovered')]);
    assert.deepEqual(h.state.items.value.map(item => item.id), ['recovered']);
    assert.equal(h.state.error.value, '');
  } finally { h.close(); }
});

test('statistics failure preserves usable files and a cursor retry also recovers statistics', async () => {
  const h = harness();
  try {
    h.requests[0].resolve({ items: [asset('first')], next_cursor: 'next-page' });
    h.statsRequests[0].reject(new Error('/private/stat-secret'));
    await flush();
    assert.deepEqual(h.state.items.value.map(item => item.id), ['first']);
    assert.equal(h.state.stats.value, null);
    assert.equal(h.state.loading.value, false);
    assert.match(h.state.error.value, /数据统计/);
    assert.doesNotMatch(h.state.error.value, /private|stat-secret/);
    void h.state.load(true);
    assert.equal(h.requests[1].params.cursor, 'next-page');
    assert.equal(h.statsRequests.length, 2);
    await h.settle([asset('second')], null, summary(180));
    assert.deepEqual(h.state.items.value.map(item => item.id), ['first', 'second']);
    assert.equal(h.state.stats.value.file_count, 180);
    assert.equal(h.state.error.value, '');
  } finally { h.close(); }
});

test('pagination deduplicates assets and never replaces full-library statistics with page totals', async () => {
  const h = harness();
  try {
    await h.settle([asset('first')], 'next');
    void h.state.load(true);
    assert.equal(h.statsRequests.length, 1);
    h.requests[1].resolve({ items: [asset('first', undefined, { name: 'updated' }), asset('second')], next_cursor: null });
    await flush();
    assert.deepEqual(h.state.items.value.map(item => item.id), ['first', 'second']);
    assert.equal(h.state.items.value[0].name, 'updated');
    assert.equal(h.state.stats.value.file_count, 90);
    assert.equal(h.state.nextCursor.value, null);
    void h.state.load(true);
    assert.equal(h.requests.length, 2);
  } finally { h.close(); }
});

test('failed pagination keeps the first page and retries the same cursor', async () => {
  const h = harness();
  try {
    await h.settle([asset('first')], 'cursor');
    void h.state.load(true);
    h.requests[1].reject(new Error('offline'));
    await flush();
    assert.deepEqual(h.state.items.value.map(item => item.id), ['first']);
    assert.equal(h.state.nextCursor.value, 'cursor');
    assert.equal(h.state.stats.value.file_count, 90);
    void h.state.load(true);
    assert.equal(h.requests[2].params.cursor, 'cursor');
    h.requests[2].resolve({ items: [asset('second')], next_cursor: null });
    await flush();
    assert.deepEqual(h.state.items.value.map(item => item.id), ['first', 'second']);
    assert.equal(h.state.error.value, '');
  } finally { h.close(); }
});

test('owner changes and logout clear statistics immediately and discard all late responses', async () => {
  const h = harness();
  try {
    await h.settle([asset('owner-a-private')], 'owner-a-cursor', summary(111));
    void h.state.load();
    h.owner.value = { id: 'owner-b' };
    assert.equal(h.requests[1].signal.aborted, true);
    assert.equal(h.statsRequests[1].signal.aborted, true);
    assert.deepEqual(h.state.items.value, []);
    assert.equal(h.state.stats.value, null);
    assert.equal(h.state.nextCursor.value, null);
    await h.settle([asset('owner-b-private')], null, summary(222));
    h.requests[1].resolve({ items: [asset('owner-a-late')], next_cursor: 'private-cursor' });
    h.statsRequests[1].resolve(summary(111));
    await flush();
    assert.deepEqual(h.state.items.value.map(item => item.id), ['owner-b-private']);
    assert.equal(h.state.stats.value.file_count, 222);
    assert.equal(h.state.nextCursor.value, null);
    void h.state.load();
    h.owner.value = null;
    assert.deepEqual(h.state.items.value, []);
    assert.equal(h.state.stats.value, null);
    await h.settle([asset('owner-b-late')], 'late-cursor', summary(333));
    assert.deepEqual(h.state.items.value, []);
    assert.equal(h.state.stats.value, null);
    assert.equal(h.state.loading.value, false);
    assert.equal(h.state.error.value, '');
  } finally { h.close(); }
});

test('rapid filters cancel prior requests, debounce to the last value and reject stale results', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const h = harness();
  try {
    h.state.query.value = 'old';
    assert.equal(h.requests[0].signal.aborted, true);
    t.mock.timers.tick(200);
    assert.equal(h.requests.length, 1);
    h.state.query.value = 'latest';
    h.state.fileFormat.value = 'nc';
    t.mock.timers.tick(249);
    assert.equal(h.requests.length, 1);
    t.mock.timers.tick(1);
    assert.equal(h.requests.length, 2);
    assert.equal(h.requests[1].params.filename, 'latest');
    assert.equal(h.requests[1].params.file_format, 'nc');
    await h.settle([asset('new', [file('latest', 'latest.nc')])], null, summary(120));
    h.requests[0].resolve({ items: [asset('stale')], next_cursor: 'stale-cursor' });
    h.statsRequests[0].resolve(summary(900));
    await flush();
    assert.deepEqual(h.state.visibleItems.value.map(item => item.id), ['new']);
    assert.equal(h.state.stats.value.file_count, 120);
    assert.equal(h.state.nextCursor.value, null);
  } finally { h.close(); }
});

test('filter changes reuse global statistics and reset produces one clean request', async () => {
  const h = harness();
  try {
    await h.settle([asset('first')]);
    h.state.query.value = ' second ';
    h.state.fileFormat.value = 'nc';
    h.state.fromDate.value = '2026-10-01';
    h.state.toDate.value = '2026-10-21';
    h.state.sourceType.value = 'open_data';
    void h.state.load(false, false);
    assert.equal(h.statsRequests.length, 1);
    assert.equal(h.requests[1].params.filename, 'second');
    assert.equal(h.requests[1].params.source_type, 'open_data');
    h.requests[1].resolve({ items: [asset('second', [file('second', 'second.nc')])], next_cursor: null });
    await flush();
    assert.equal(h.state.stats.value.file_count, 90);
    h.state.resetFilters();
    assert.equal(h.requests.length, 3);
    assert.equal(h.statsRequests.length, 1);
    assert.deepEqual(h.requests[2].params, {
      filename: undefined, file_format: undefined, updated_from: undefined, updated_to: undefined, source_type: undefined, cursor: undefined,
    });
    h.requests[2].resolve({ items: [asset('first')], next_cursor: null });
    await flush();
    assert.equal(h.state.error.value, '');
  } finally { h.close(); }
});

test('invalid date ranges stop network calls and clearing them recovers normally', async () => {
  const h = harness();
  try {
    await h.settle([asset('first')]);
    h.state.fromDate.value = '2026-10-22';
    h.state.toDate.value = '2026-10-20';
    await h.state.load();
    assert.equal(h.requests.length, 1);
    assert.equal(h.statsRequests.length, 1);
    assert.equal(h.state.loading.value, false);
    assert.match(h.state.error.value, /结束日期不能早于开始日期/);
    assert.deepEqual(h.state.visibleItems.value, []);
    h.state.resetFilters();
    h.requests[1].resolve({ items: [asset('first')], next_cursor: null });
    await flush();
    assert.equal(h.state.error.value, '');
    assert.equal(h.state.visibleItems.value.length, 1);
  } finally { h.close(); }
});

test('disposing the browser aborts pending work and prevents delayed data from returning', async () => {
  const h = harness();
  h.close();
  assert.equal(h.requests[0].signal.aborted, true);
  assert.equal(h.statsRequests[0].signal.aborted, true);
  await h.settle([asset('late')], 'late-cursor', summary());
  assert.deepEqual(h.state.items.value, []);
  assert.equal(h.state.stats.value, null);
  assert.equal(h.state.loading.value, false);
});


test('changing directory cancels old file pages and carries exact directory scope through filtering', async () => {
  const folderId = vue.ref('root');
  const h = harness({ folderId });
  try {
    assert.equal(h.requests[0].params.folder_id, 'root');
    await h.settle([asset('root')], 'old-cursor');
    void h.state.load(true);
    const stale = h.requests.at(-1);
    folderId.value = 'folder-b';
    assert.equal(stale.signal.aborted, true);
    assert.equal(h.requests.at(-1).params.folder_id, 'folder-b');
    assert.equal(h.requests.at(-1).params.cursor, undefined);
    assert.deepEqual(h.state.items.value, []);
    await h.settle([asset('new-folder')]);
    stale.resolve({ items: [asset('old-folder')], next_cursor: 'wrong' });
    await flush();
    assert.deepEqual(h.state.items.value.map(item => item.id), ['new-folder']);
    h.state.query.value = 'sample';
    const filtered = h.state.load(false, false);
    assert.equal(h.requests.at(-1).params.folder_id, 'folder-b');
    assert.equal(h.requests.at(-1).params.filename, 'sample');
    h.requests.at(-1).resolve({ items: [], next_cursor: null });
    await filtered;
  } finally { h.close(); }
});
