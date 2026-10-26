import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import * as vue from 'vue';
import { compileScript, parse } from '@vue/compiler-sfc';
import ts from 'typescript';

function load(path, dependencies = {}) {
  const source = readFileSync(new URL(path, import.meta.url), 'utf8');
  const content = path.endsWith('.vue') ? compileScript(parse(source).descriptor, { id: 'open-data-test' }).content : source;
  const code = ts.transpileModule(content, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
  const module = { exports: {} };
  new Function('require', 'module', 'exports', code)(name => {
    if (name in dependencies) return dependencies[name];
    if (name === 'vue') return vue;
    if (name === 'lucide-vue-next' || name.endsWith('.vue')) return {};
    throw new Error(`Unexpected dependency ${name}`);
  }, module, module.exports);
  return module.exports;
}
const helpers = load('../src/utils/openData.ts');
const exploration = load('../src/utils/explorationFiles.ts');
const file = id => ({ file_id: id, filename: `${id}.csv`, upload_date: '', size: 12 });
const candidate = (id = 'candidate', overrides = {}) => ({ id, source_id: 'catalog', external_id: id, title: id, description: '', source_url: 'https://example.org/data', access: 'downloadable', availability_verified_at: '2026-10-20T01:00:00Z', available_file_count: 2, formats: [], coverage: {}, match_notes: [], limitations: [], ...overrides });
const search = (id = 'first', session_id = 'session', overrides = {}) => ({ id, session_id, query: '城市空气质量与交通', status: 'ready', sources: [], results: [], has_more: false, created_at: '2026-10-20T01:00:00Z', ...overrides });
const flush = async () => { await Promise.resolve(); await vue.nextTick(); await Promise.resolve(); };

test('search timeline preserves message order and deduplicates direct and step tool cards', () => {
  const messages = [
    { type: 'user', content: { timestamp: 1792457999 } },
    { type: 'tool', content: { timestamp: 1792458002, open_data_search_id: 'tool' } },
    { type: 'step', content: { timestamp: 1792458003, tools: [{ open_data_search_id: 'step' }] } },
  ];
  const records = [search('direct'), search('tool'), search('step'), search('direct')];
  const entries = helpers.openDataTimeline(messages, records);
  assert.deepEqual(entries.filter(item => item.kind === 'search').map(item => item.search.id), ['direct']);
  assert.deepEqual(entries.filter(item => item.kind === 'message').map(item => item.message), messages);
  assert.equal(entries[1].kind, 'search');
  assert.equal(helpers.openDataSourceUrl('javascript:alert(1)'), undefined);
  assert.equal(helpers.openDataSourceUrl('https://secret:password@example.org'), undefined);
  assert.equal(helpers.openDataSourceUrl('https://example.org/data'), 'https://example.org/data');
});

test('analysis presentation reorders inputs without changing canonical resume indices or duplicating search cards', () => {
  const messages = [{type:'user',content:{timestamp:10}},
    {type:'task-summary',content:{timestamp:12}},
    {type:'attachments',content:{role:'user',timestamp:10,attachments:[file('input')]}},
    {type:'assistant',content:{timestamp:13}}];
  const presentation = [2,0,1,3].map(index => ({message:messages[index],index}));
  const entries = helpers.openDataTimeline(messages, [search('search','session',{created_at:11})], presentation);
  assert.deepEqual(entries.filter(item=>item.kind==='message').map(item=>item.index),[2,0,1,3]);
  assert.equal(entries.filter(item=>item.kind==='search').length,1);
  assert.equal(entries[2].kind,'search');
});

test('file selection enforces only explicit availability and dependency evidence', () => {
  const options = [{ id: 'data', available: true, required_file_ids: ['metadata'] }, { id: 'metadata', available: true }, { id: 'private', available: false }];
  assert.match(helpers.openDataSelectionIssue(options, []), /请选择/);
  assert.match(helpers.openDataSelectionIssue(options, ['data']), /配套/);
  assert.equal(helpers.openDataSelectionIssue(options, ['data', 'metadata']), '');
  assert.match(helpers.openDataSelectionIssue(options, ['private']), /不可获取/);
  assert.match(helpers.openDataSelectionIssue(options, ['missing']), /不可获取/);
  assert.equal(helpers.openDataSelectionIssue([{ id: 'shape.shp', available: true }, { id: 'shape.dbf', available: true }], ['shape.shp']), '');
});

test('eligible results require obtainable files and honor explicit relevance while keeping old records compatible', () => {
  assert.equal(helpers.isVerifiedOpenDataCandidate(candidate()), true);
  assert.equal(helpers.isVerifiedOpenDataCandidate(candidate('related', { relevance_status: 'related' })), true);
  for (const fields of [
    { availability_verified_at: null }, { availability_verified_at: undefined }, { availability_verified_at: '' },
    { availability_verified_at: 'not-a-date' }, { available_file_count: 0 }, { available_file_count: -1 },
    { available_file_count: 1.5 }, { available_file_count: undefined },
    { access: 'lead' }, { access: 'restricted' }, { access: 'needs_resolution' },
    { relevance_status: 'unrelated' }, { relevance_status: 'uncertain' }, { relevance_status: 'unsupported-status' },
  ]) assert.equal(helpers.isVerifiedOpenDataCandidate(candidate('hidden', fields)), false, JSON.stringify(fields));
});

test('large and unknown file sizes use readable GB and TB progress without guessing totals', () => {
  assert.equal(helpers.openDataSizeLabel(2_000_000_000_000), '2.0 TB');
  assert.equal(helpers.openDataSizeLabel(1_500_000_000), '1.5 GB');
  assert.equal(helpers.openDataSizeLabel(null), '大小未知');
  assert.equal(helpers.openDataProgressLabel(50_000_000_000, 2_000_000_000_000), '50.0 GB / 2.0 TB');
  assert.equal(helpers.openDataProgressLabel(1_500_000_000, null), '1.5 GB · 总大小未知');
  assert.equal(helpers.openDataFilesSizeLabel([{ size: 1_000_000 }, { size: 2_000_000 }]), '3.0 MB');
  assert.equal(helpers.openDataFilesSizeLabel([{ size: 1_000_000 }, { size: null }, {}]), '已知 1.0 MB，另有 2 个文件大小未知');
  assert.equal(helpers.openDataFilesSizeLabel([{ size: null }, { size: Number.NaN }, { size: -1 }]), '大小未知');
  assert.equal(helpers.openDataFilesSizeLabel([{ size: 0 }, { size: null }]), '已知 0 B，另有 1 个文件大小未知');
  assert.equal(helpers.openDataFilesSizeLabel([]), '大小未知');
});

test('coverage translates biological and statistical quantities without confusing samples with files', () => {
  assert.deepEqual(helpers.openDataCoverageFacts({ organism: 'Homo sapiens', sample_count: 100, library_strategy: ['RNA-Seq', 'WGS'], NumberOfFeatures: 12, records: 0, temporal: { start: 2020, end: 2024 }, empty: null }), [
    { label: '物种', value: 'Homo sapiens' }, { label: '样本数', value: '100' },
    { label: '测序方法', value: 'RNA-Seq、WGS' }, { label: '特征数', value: '12' },
    { label: '记录数', value: '0' }, { label: '时间', value: '开始时间：2020；结束时间：2024' },
  ]);
  assert.deepEqual(helpers.openDataCoverageFacts({ sample_count: 0 }), [{ label: '样本数', value: '0' }]);
});

test('full file resolution and acquisition preserve cancellation without a fixed request deadline', async () => {
  const calls = [];
  const api = load('../src/api/openData.ts', { './client': { apiClient: { post: async (...args) => { calls.push(args); return { data: { data: { id: 'response' } } }; } } } });
  const controller = new AbortController();
  await api.resolveOpenDataCandidate('session', 'search', 'candidate', controller.signal);
  await api.acquireOpenData('session', 'search', { candidate_id: 'candidate', file_ids: ['one'] }, controller.signal);
  for (const call of calls) {
    assert.equal(call[2].timeout, 0);
    assert.equal(call[2].signal, controller.signal);
  }
  controller.abort();
  assert.equal(calls[0][2].signal.aborted, true);
  assert.equal(calls[1][2].signal.aborted, true);
});

function harness(componentName, overrides = {}, apiOverrides = {}) {
  const requests = [], events = [], cleanup = [], listeners = new Map(), scope = vue.effectScope();
  const storeCalls = [], previews = [];
  const props = vue.reactive({ sessionId: 'session', searchId: 'first', messages: [], isShare: false, ...overrides });
  const api = {};
  for (const name of ['listOpenDataSearches', 'createOpenDataSearch', 'getOpenDataSearch', 'listOpenDataJobs', 'resolveOpenDataCandidate', 'acquireOpenData', 'continueOpenDataSearch', 'changeOpenDataJob']) {
    api[name] = (...args) => new Promise((resolve, reject) => requests.push({ name, args, resolve, reject }));
  }
  Object.assign(api, apiOverrides);
  const store = {
    sessionId: vue.ref(props.sessionId), jobs: vue.ref([]), searches: vue.ref([]), loading: vue.ref(false), error: vue.ref(''),
    actionBusy: vue.ref(''), actionError: vue.ref(''), preparedJobs: vue.ref(new Set()), downloadingFiles: vue.ref(new Set()),
    focusedJobId: vue.ref(''), focusedSearchId: vue.ref(''), focusRevision: vue.ref(0), captureRevision: () => 1,
    acceptSearch(record) {
      if (record.session_id !== store.sessionId.value) return false;
      store.searches.value = [...store.searches.value.filter(item => item.id !== record.id), record];
      return true;
    },
    acceptJobs(records, options = {}) {
      const accepted = records.filter(item => item.session_id === store.sessionId.value && (!options.searchId || item.search_id === options.searchId));
      store.jobs.value = [...store.jobs.value.filter(item => !accepted.some(next => next.id === item.id)), ...accepted];
      return accepted.length > 0;
    },
    open(options) {
      storeCalls.push(['open', options]);
      if (!options.automatic) { store.focusedJobId.value = options.jobId || ''; store.focusedSearchId.value = options.searchId || ''; store.focusRevision.value += 1; }
      return true;
    },
    async changeJob(job, operation) {
      storeCalls.push(['changeJob', job, operation]);
      const updated = await api.changeOpenDataJob(store.sessionId.value, job.id, operation);
      store.acceptJobs([updated]);
      return true;
    },
    useFiles: (job, onAccepted) => storeCalls.push(['useFiles', job, onAccepted]),
    downloadToLocal: (job, item) => storeCalls.push(['downloadToLocal', job, item]),
    refresh: () => storeCalls.push(['refresh']),
  };
  cleanup.push(vue.watch(() => props.sessionId, id => { store.sessionId.value = id; store.jobs.value = []; store.searches.value = []; }, { flush: 'sync' }));
  const component = load(`../src/components/${componentName}.vue`, {
    vue: { ...vue, onUnmounted: callback => cleanup.push(callback) },
    '../api/openData': api, '../utils/openData': helpers,
    '../composables/useDataAcquisition': { useDataAcquisition: () => store },
    '../composables/useFilePanel': { useFilePanel: () => ({ showFilePanel: (...args) => previews.push(args) }) },
    'vue-router': { RouterLink: {} }, '../api/file': { getFileDownloadUrl: async () => '' },
    '../utils/explorationFiles': exploration,
    '../utils/eventBus': { eventBus: { emit: (...args) => events.push(args), on: (name, callback) => listeners.set(name, callback), off: name => listeners.delete(name) } },
  }).default;
  const state = scope.run(() => component.setup(props, { expose() {}, emit: (...args) => events.push(args) }));
  return { props, state, store, storeCalls, previews, requests, events, listeners, close() { cleanup.forEach(callback => callback()); scope.stop(); } };
}

test('results preserve authoritative relevance order across sources and exclude explicitly unrelated or uncertain records', async () => {
  const records = [
    candidate('a1', { source_id: 'a', relevance_status: 'related', relevance_reason: '说明包含藏文语料' }),
    candidate('a2', { source_id: 'a', relevance_status: 'related' }),
    candidate('swiss', { source_id: 'b', relevance_status: 'unrelated' }),
    candidate('a3', { source_id: 'a', relevance_status: 'related' }),
    candidate('unknown', { source_id: 'c', relevance_status: 'uncertain' }),
    candidate('b1', { source_id: 'b', relevance_status: 'related' }),
    candidate('legacy', { source_id: 'c' }), candidate('a4', { source_id: 'a', relevance_status: 'related' }),
  ];
  const record = search('first', 'session', { results: records, has_more: true });
  const h = harness('OpenDataResults', { initialSearch: record }, { getOpenDataSearch: async () => record, listOpenDataJobs: async () => [] });
  try {
    await flush();
    assert.deepEqual(h.state.verifiedCandidates.value.map(item => item.id), ['a1', 'a2', 'a3', 'b1', 'legacy', 'a4']);
    assert.deepEqual(h.state.visibleCandidates.value.map(item => item.id), ['a1', 'a2', 'a3', 'b1', 'legacy']);
    assert.equal(h.state.visibleCandidates.value[0].relevance_reason, '说明包含藏文语料');
    assert.equal(h.state.hasHiddenResults.value, true);
    assert.equal(h.state.canContinueSearch.value, false, 'Reveal loaded results before requesting another source page');
    h.state.visibleCount.value += 5;
    assert.deepEqual(h.state.visibleCandidates.value.map(item => item.id), ['a1', 'a2', 'a3', 'b1', 'legacy', 'a4']);
    assert.equal(h.state.canContinueSearch.value, true);
    const before = h.requests.length;
    await h.state.prepare(records[2]); await h.state.prepare(records[4]);
    assert.equal(h.requests.length, before, 'Excluded results must not start a file resolution');
    h.store.acceptSearch({ ...record, results: [records[5], records[0], records[1]] }); await flush();
    assert.deepEqual(h.state.visibleCandidates.value.map(item => item.id), ['b1', 'a1', 'a2'], 'Later authoritative ranking remains authoritative');
  } finally { h.close(); }
});

test('pending relevance never claims no results and continuation can retry a pending judgement', async () => {
  const record = search('first', 'session', { results: [], relevance_pending_count: 3, status: 'searching', has_more: true });
  const h = harness('OpenDataResults', { initialSearch: record }, { getOpenDataSearch: async () => record, listOpenDataJobs: async () => [] });
  try {
    await flush();
    assert.equal(h.state.searchPending.value, true);
    assert.match(h.state.searchStatusMessage.value, /正在确认 3.*相关性/);
    assert.doesNotMatch(h.state.searchStatusMessage.value, /未找到/);
    h.state.search.value = { ...record, status: 'partial' };
    assert.equal(h.state.searchPending.value, false);
    assert.match(h.state.searchStatusMessage.value, /3.*待确认相关性/);
    assert.equal(h.state.canContinueSearch.value, true);
    const continuing = h.state.loadMore();
    const request = h.requests.at(-1);
    assert.equal(request.name, 'continueOpenDataSearch');
    request.resolve({ ...record, status: 'queued' });
    await continuing;
    assert.equal(h.state.searchPending.value, true);
    assert.doesNotMatch(h.state.searchStatusMessage.value, /未找到/);
    h.state.search.value = { ...record, status: 'ready', relevance_pending_count: 0, has_more: false };
    assert.equal(h.state.searchStatusMessage.value, '未找到与该需求相关且当前可获取的数据，可调整要求继续查找。');
    assert.equal(h.state.canContinueSearch.value, false);
  } finally { h.close(); }
});

test('relevance removal leaves existing acquired files and task history accessible', async () => {
  const record = search('first', 'session', { results: [candidate('previous', { relevance_status: 'unrelated' })] });
  const h = harness('OpenDataResults', { initialSearch: record });
  try {
    h.requests[0].resolve(record);
    h.requests[1].resolve([{ id: 'saved', session_id: 'session', search_id: 'first', candidate_id: 'previous', status: 'ready', files: [file('kept')], items: [] }]);
    await flush();
    assert.deepEqual(h.state.visibleCandidates.value, []);
    assert.equal(h.state.acquisitionSummary.value, '已保存 1 个文件');
    h.state.viewAcquisitions();
    assert.deepEqual(h.storeCalls.at(-1), ['open', { searchId: 'first', jobId: 'saved' }]);
  } finally { h.close(); }
});

test('saved job rows deduplicate by file identity despite renamed files and retain legacy records', () => {
  const h = harness('DataAcquisitionContent', { isShare: true });
  try {
    const saved = { ...file('saved'), filename: '12345678_observations.csv' };
    const items = [
      { id: 'renamed', filename: 'observations.csv', status: 'ready', file: saved },
      { id: 'legacy', filename: saved.filename, status: 'ready' },
      { id: 'different-file', filename: saved.filename, status: 'ready', file: file('other') },
      { id: 'failed', filename: saved.filename, status: 'failed', file: saved },
      { id: 'pending', filename: saved.filename, status: 'downloading', file: saved },
    ];
    assert.deepEqual(h.state.visibleJobItems({ items, files: [saved] }).map(item => item.id), ['different-file', 'failed', 'pending']);
    assert.deepEqual(h.state.visibleJobItems({ items, files: [] }), items);
  } finally { h.close(); }
});

test('chat restores history, merges a newly created search, and rejects late session lists', async () => {
  const h = harness('OpenDataChat');
  try {
    const submitting = h.state.start('新问题', 'previous', ['ncbi-geo']);
    const create = h.requests.find(item => item.name === 'createOpenDataSearch');
    assert.deepEqual(create.args[1], { query: '新问题', parent_id: 'previous', source_ids: ['ncbi-geo'] });
    create.resolve(search('new'));
    assert.equal(await submitting, true);
    h.requests[0].resolve([search('old')]);
    await flush();
    assert.deepEqual(h.state.searches.value.map(item => item.id), ['new', 'old']);
    const pending = h.state.refresh();
    const stale = h.requests.at(-1);
    h.props.sessionId = 'other';
    await flush();
    assert.equal(stale.args[1].aborted, true);
    h.requests.at(-1).resolve([search('other-record', 'other')]);
    stale.resolve([search('late-private')]);
    await pending; await flush();
    assert.deepEqual(h.state.searches.value.map(item => item.id), ['other-record']);
  } finally { h.close(); }
});

test('uncertain create never auto-retries and public cards make no private requests', async () => {
  const h = harness('OpenDataChat');
  try {
    h.requests[0].resolve([]); await flush();
    const pending = h.state.start('保留的问题');
    h.requests.at(-1).reject(new Error('/private/path'));
    assert.equal(await pending, false);
    assert.match(h.state.error.value, /尚未确认/);
    assert.doesNotMatch(h.state.error.value, /private/);
    assert.equal(h.requests.filter(item => item.name === 'createOpenDataSearch').length, 1);
  } finally { h.close(); }
  for (const name of ['OpenDataChat', 'OpenDataResults']) {
    const shared = harness(name, { isShare: true });
    try { await shared.state.refresh(); assert.equal(shared.requests.length, 0); } finally { shared.close(); }
  }
});

test('result polling keeps completed data on failure and late responses cannot cross sessions', async () => {
  const h = harness('OpenDataResults', { initialSearch: search() });
  try {
    h.requests[0].reject(new Error('offline'));
    h.requests[1].resolve([]); await flush();
    assert.equal(h.state.search.value.id, 'first');
    assert.match(h.state.connectionError.value, /已有结果仍保留/);
    const pending = h.state.refresh(), stale = h.requests.slice(-2);
    h.props.sessionId = 'other'; h.props.searchId = 'new'; await flush();
    assert.equal(stale[0].args[2].aborted, true);
    stale[0].resolve(search('first')); stale[1].resolve([]);
    const latest = h.requests.slice(-2);
    latest[0].resolve(search('new', 'other')); latest[1].resolve([]);
    await pending; await flush();
    assert.equal(h.state.search.value.id, 'new');
    assert.equal(h.state.connectionError.value, '');
  } finally { h.close(); }
});

test('multi-file preparation accepts TB and unknown sizes without reinstating legacy batch caps', async () => {
  const record = search('first', 'session', { results: [candidate()] });
  const h = harness('OpenDataResults', { initialSearch: record }, {
    getOpenDataSearch: async () => record, listOpenDataJobs: async () => [],
  });
  try {
    await flush();
    const resolving = h.state.prepare({ id: 'candidate' });
    const request = h.requests.at(-1);
    request.resolve({ files: [{ id: 'a', filename: 'data.csv', available: true, size: 2_000_000_000_000 }, { id: 'b', filename: 'metadata.txt', available: true, size: null }], limits: { max_file_bytes: null, max_total_bytes: null, max_files: 2 } });
    await resolving;
    assert.deepEqual(h.state.selected.value.candidate, []);
    assert.equal(h.requests.filter(item => item.name === 'acquireOpenData').length, 0);
    h.state.selected.value.candidate = ['a', 'b'];
    assert.equal(h.state.selectionIssue('candidate'), '');
    h.state.resolutions.value.candidate.limits.max_files = 1;
    assert.equal(h.state.selectionIssue('candidate'), '', 'A legacy limit must not reinstate an acquisition count cap');
    h.state.resolutions.value.candidate.limits = undefined;
    assert.equal(h.state.selectionIssue('candidate'), '', 'Missing limits must not restore an old fixed size cap');
    h.state.selected.value.candidate = ['a'];
    assert.equal(h.state.selectionIssue('candidate'), '');
  } finally { h.close(); }
});

test('full resolved files replace old summaries and more than sixteen selected files submit in one acquisition', async () => {
  const original = candidate('candidate', { available_file_count: 6, size: 999 });
  const record = search('first', 'session', { results: [original] });
  const files = Array.from({ length: 24 }, (_, index) => ({ id: `file-${index}`, filename: `file-${index}.csv`, available: true, size: index === 23 ? null : 1_000_000 }));
  const h = harness('OpenDataResults', { initialSearch: record }, { getOpenDataSearch: async () => record, listOpenDataJobs: async () => [] });
  try {
    await flush();
    assert.equal(h.state.candidateFileCount(original), 6);
    const resolving = h.state.prepare(original);
    h.requests.at(-1).resolve({ files: [...files, files[0], { id: 'blocked', filename: 'private.csv', available: false, size: 123 }], limits: { max_file_bytes: null, max_total_bytes: null, max_files: null } });
    await resolving;
    assert.equal(h.state.candidateFileCount(original), 24);
    assert.equal(h.state.candidateSizeLabel(original), '已知 23.0 MB，另有 1 个文件大小未知');
    assert.equal(h.state.selectedFiles('candidate').length, 0);
    h.state.selectFiles('candidate', true);
    assert.equal(h.state.selectedFiles('candidate').length, 24);
    assert.equal(h.state.selectionIssue('candidate'), '');
    h.state.selectFiles('candidate', false);
    assert.equal(h.state.selectedFiles('candidate').length, 0);
    h.state.selectFiles('candidate', true);
    const pending = h.state.acquire('candidate');
    const acquisitions = h.requests.filter(request => request.name === 'acquireOpenData');
    assert.equal(acquisitions.length, 1);
    assert.deepEqual(acquisitions[0].args[2], { candidate_id: 'candidate', file_ids: files.map(item => item.id) });
    acquisitions[0].resolve({ id: 'all-files', session_id: 'session', search_id: 'first', candidate_id: 'candidate', status: 'queued', files: [], items: files.map(item => ({ ...item, status: 'queued' })) });
    await pending;
    assert.equal(h.state.acquisitionSummary.value, '正在获取 24 个文件');
    assert.deepEqual(h.storeCalls, [['open', { searchId: 'first', jobId: 'all-files', automatic: true }]]);
    assert.equal(h.state.candidateFileCount(original), 24, 'An older search refresh must not overwrite the full resolved count');
  } finally { h.close(); }
});

test('a shorter resolved list replaces stale count and size, and clear selection remains explicit', async () => {
  const original = candidate('candidate', { available_file_count: 16, size: 100_000_000 });
  const record = search('first', 'session', { results: [original] });
  const h = harness('OpenDataResults', { initialSearch: record }, { getOpenDataSearch: async () => record, listOpenDataJobs: async () => [] });
  try {
    await flush();
    const resolving = h.state.prepare(original);
    h.requests.at(-1).resolve({ files: Array.from({ length: 3 }, (_, i) => ({ id: String(i), filename: `${i}.csv`, available: true, size: 1_000_000 })) });
    await resolving;
    assert.equal(h.state.candidateFileCount(original), 3);
    assert.equal(h.state.candidateSizeLabel(original), '3.0 MB');
    h.state.selectFiles('candidate', true);
    h.state.selectFiles('candidate', false);
    assert.match(h.state.selectionIssue('candidate'), /请选择/);
    await h.state.acquire('candidate');
    assert.equal(h.requests.some(request => request.name === 'acquireOpenData'), false);
  } finally { h.close(); }
});

test('polling timeout preserves a long-running download and newer byte progress resumes on reconnect', async () => {
  const h = harness('OpenDataResults');
  const job = { id: 'large', session_id: 'session', search_id: 'first', candidate_id: 'candidate', status: 'downloading', files: [], items: [{ id: 'big', filename: 'large.bin', status: 'downloading', bytes_received: 1_000_000_000_000, size: null }] };
  try {
    h.requests[0].resolve(search()); h.requests[1].resolve([job]); await flush();
    const failedPoll = h.state.refresh();
    h.requests[2].resolve(search()); h.requests[3].reject(new Error('timeout exceeded')); await failedPoll;
    assert.equal(h.state.jobs.value[0].status, 'downloading');
    assert.equal(h.state.jobs.value[0].items[0].bytes_received, 1_000_000_000_000);
    assert.match(h.state.connectionError.value, /正在尝试重连/);
    const recovered = h.state.refresh();
    h.requests[4].resolve(search()); h.requests[5].resolve([{ ...job, items: [{ ...job.items[0], bytes_received: 1_500_000_000_000 }] }]); await recovered;
    assert.equal(h.state.jobs.value[0].status, 'downloading');
    assert.equal(h.state.jobs.value[0].items[0].bytes_received, 1_500_000_000_000);
    assert.equal(h.state.connectionError.value, '');
  } finally { h.close(); }
});

test('legacy results are hidden while acquired files remain independently usable after restoration', async () => {
  const legacy = candidate('old', { availability_verified_at: null, available_file_count: 0 });
  const record = search('first', 'session', { results: [legacy, candidate('lead', { access: 'lead' }), candidate('zero', { available_file_count: 0 }), candidate('good')] });
  const h = harness('OpenDataResults', { initialSearch: record });
  try {
    h.requests[0].resolve(record);
    h.requests[1].resolve([
      { id: 'saved', session_id: 'session', search_id: 'first', candidate_id: 'old', status: 'ready', files: [file('kept')], items: [] },
      { id: 'empty-old', session_id: 'session', search_id: 'first', candidate_id: 'old', status: 'failed', files: [], items: [] },
    ]);
    await flush();
    assert.deepEqual(h.state.visibleCandidates.value.map(item => item.id), ['good']);
    assert.deepEqual(h.state.jobs.value.map(item => item.id), ['saved', 'empty-old']);
    h.state.viewAcquisitions();
    assert.deepEqual(h.storeCalls.at(-1), ['open', { searchId: 'first', jobId: 'empty-old' }]);
    assert.match(h.state.acquisitionSummary.value, /已保存 1 个文件.*未完成/);
    const before = h.requests.length;
    await h.state.prepare(legacy);
    assert.equal(h.requests.length, before, 'Hidden legacy results cannot initiate resolve requests');
    h.state.search.value = { ...record, results: [], notes: ['已隐藏旧版尚未核验文件可获取性的记录。'] };
    assert.deepEqual(h.state.visibleCandidates.value, []);
    assert.deepEqual(h.state.jobs.value.map(item => item.id), ['saved', 'empty-old']);
    h.state.search.value = undefined;
    assert.deepEqual(h.state.jobs.value.map(item => item.id), ['saved', 'empty-old'], 'Saved files and actual task history survive a missing search response');
  } finally { h.close(); }
});

test('a candidate losing all available files during resolve is removed without losing saved jobs', async () => {
  const record = search('first', 'session', { results: [candidate()] });
  const h = harness('OpenDataResults', { initialSearch: record }, { getOpenDataSearch: async () => record, listOpenDataJobs: async () => [] });
  try {
    await flush();
    const pending = h.state.prepare(candidate());
    h.requests.at(-1).resolve({ files: [{ id: 'blocked', available: false, filename: 'blocked.csv' }] });
    await pending;
    assert.equal(h.state.visibleCandidates.value.length, 0);
    assert.equal(h.requests.some(item => item.name === 'acquireOpenData'), false);
    assert.match(h.state.actionError.value, /当前没有可获取文件/);
    h.state.search.value = { ...record, results: [candidate('candidate', { availability_verified_at: '2026-10-21T01:00:00Z' })] };
    assert.equal(h.state.visibleCandidates.value.length, 1, 'A later successful verification can restore the candidate');
  } finally { h.close(); }
});

test('temporary full-list failure preserves the verified candidate and permits retry', async () => {
  const record = search('first', 'session', { results: [candidate()] });
  const h = harness('OpenDataResults', { initialSearch: record }, { getOpenDataSearch: async () => record, listOpenDataJobs: async () => [] });
  try {
    await flush();
    const pending = h.state.prepare(candidate());
    h.requests.at(-1).reject(new Error('source temporarily failed'));
    await pending;
    assert.equal(h.state.visibleCandidates.value.length, 1);
    assert.match(h.state.actionError.value, /已保留.*重试/);
    assert.equal(h.requests.some(item => item.name === 'acquireOpenData'), false);
    const retry = h.state.prepare(candidate());
    assert.equal(h.requests.filter(item => item.name === 'resolveOpenDataCandidate').length, 2);
    h.requests.at(-1).resolve({ files: [{ id: 'a', filename: 'a.csv', available: true, size: null }, { id: 'b', filename: 'b.csv', available: true, size: null }] });
    await retry;
    assert.equal(h.state.availableFiles('candidate').length, 2);
    assert.equal(h.state.actionError.value, '');
  } finally { h.close(); }
});

test('switching sessions cancels a long full-list request and rejects its late response', async () => {
  const record = search('first', 'session', { results: [candidate()] });
  const h = harness('OpenDataResults', { initialSearch: record }, { getOpenDataSearch: async (sessionId, searchId) => search(searchId, sessionId), listOpenDataJobs: async () => [] });
  try {
    h.state.search.value = record;
    const resolving = h.state.prepare(candidate());
    const request = h.requests.at(-1);
    h.props.sessionId = 'other'; h.props.searchId = 'other-search';
    await flush();
    assert.equal(request.args[3].aborted, true);
    request.resolve({ files: [{ id: 'late', filename: 'private.csv', available: true, size: 10 }] });
    await resolving; await flush();
    assert.deepEqual(h.state.resolutions.value, {});
    assert.deepEqual(h.state.selected.value, {});
    assert.equal(h.state.actionError.value, '');
    assert.equal(h.requests.some(item => item.name === 'acquireOpenData'), false);
    assert.equal(h.state.search.value.session_id, 'other');
  } finally { h.close(); }
});

test('cancel reading only aborts the matching resolution, preserves results without a false failure, and allows retry', async () => {
  const record = search('first', 'session', { results: [candidate()] });
  const h = harness('OpenDataResults', { initialSearch: record }, { getOpenDataSearch: async () => record, listOpenDataJobs: async () => [] });
  try {
    await flush();
    const resolving = h.state.prepare(candidate());
    const request = h.requests.at(-1);
    h.state.cancelPreparation('different-candidate');
    assert.equal(request.args[3].aborted, false);
    h.state.cancelPreparation('candidate');
    assert.equal(request.args[3].aborted, true);
    request.reject(new Error('cancelled'));
    await resolving;
    assert.equal(h.state.actionBusy.value, '');
    assert.equal(h.state.actionError.value, '');
    assert.equal(h.state.visibleCandidates.value.length, 1);
    assert.equal(h.requests.some(item => item.name === 'changeOpenDataJob'), false);
    const retry = h.state.prepare(candidate());
    h.requests.at(-1).resolve({ files: [{ id: 'one', filename: 'one.csv', available: true }, { id: 'two', filename: 'two.csv', available: true }] });
    await retry;
    assert.equal(h.state.availableFiles('candidate').length, 2);
    h.state.selectFiles('candidate', true);
    const acquiring = h.state.acquire('candidate');
    const acquisition = h.requests.at(-1);
    h.state.cancelPreparation('candidate');
    assert.equal(acquisition.args[3].aborted, false, 'Cancel reading must never abort an acquisition submission');
    acquisition.resolve({ id: 'new', session_id: 'session', search_id: 'first', candidate_id: 'candidate', status: 'queued', files: [], items: [] });
    await acquiring;
  } finally { h.close(); }
});

test('partial file verification keeps confirmed files usable and retry updates counts without dropping valid selections', async () => {
  const record = search('first', 'session', { results: [candidate()] });
  const h = harness('OpenDataResults', { initialSearch: record }, { getOpenDataSearch: async () => record, listOpenDataJobs: async () => [] });
  const one = { id: 'one', filename: 'one.csv', available: true, size: 1_000_000 };
  try {
    await flush();
    const resolving = h.state.prepare(candidate());
    h.requests.at(-1).resolve({ files: [one], verification_pending_count: 2, unavailable_file_count: 3 });
    await resolving;
    assert.equal(h.state.candidateFileCount(candidate()), 1);
    assert.equal(h.state.unresolvedFilesLabel('candidate'), '还有 2 个文件暂未确认；3 个文件当前不可获取');
    assert.equal(h.state.selectionIssue('candidate'), '');
    assert.equal(h.requests.some(item => item.name === 'acquireOpenData'), false, 'A partial list keeps acquisition an explicit choice');
    const cancelled = h.state.prepare(candidate(), true);
    h.state.cancelPreparation('candidate');
    h.requests.at(-1).reject(new Error('cancelled'));
    await cancelled;
    assert.equal(h.state.availableFiles('candidate').length, 1);
    assert.deepEqual(h.state.selected.value.candidate, ['one']);
    assert.equal(h.state.actionError.value, '');
    const retry = h.state.prepare(candidate(), true);
    h.requests.at(-1).resolve({ files: [one, { id: 'two', filename: 'two.csv', available: true, size: null }], verification_pending_count: 0, unavailable_file_count: 4 });
    await retry;
    assert.equal(h.state.candidateFileCount(candidate()), 2);
    assert.equal(h.state.candidateSizeLabel(candidate()), '已知 1.0 MB，另有 1 个文件大小未知');
    assert.deepEqual(h.state.selected.value.candidate, ['one']);
    assert.equal(h.state.unresolvedFilesLabel('candidate'), '4 个文件当前不可获取');
    assert.equal(h.requests.some(item => item.name === 'acquireOpenData'), false);
  } finally { h.close(); }
});

test('Agent acquisition notification refreshes a settled existing card without polling unrelated cards', async () => {
  const h = harness('OpenDataResults');
  try {
    h.requests[0].resolve(search()); h.requests[1].resolve([]); await flush();
    h.listeners.get('open-data-updated')({ sessionId: 'other', searchId: 'first' });
    assert.equal(h.requests.length, 2);
    h.listeners.get('open-data-updated')({ sessionId: 'session', searchId: 'first' });
    assert.equal(h.requests.length, 4);
    h.requests[2].resolve(search());
    h.requests[3].resolve([{ id: 'agent-job', session_id: 'session', search_id: 'first', status: 'downloading', candidate_id: 'candidate', files: [], items: [] }]);
    await flush();
    assert.deepEqual(h.state.jobs.value.map(job => job.id), ['agent-job']);
  } finally { h.close(); }
  assert.equal(h.listeners.size, 0);
});

test('cancelled acquisition retries the same job and restores progress without creating a duplicate', async () => {
  const cancelled = { id: 'cancelled-job', session_id: 'session', search_id: 'first', candidate_id: 'candidate', status: 'cancelled', files: [], items: [] };
  const h = harness('DataAcquisitionContent');
  try {
    h.store.acceptJobs([cancelled]); await flush();
    const retry = h.state.changeJob(cancelled, 'retry');
    const request = h.requests.at(-1);
    assert.equal(request.name, 'changeOpenDataJob');
    assert.deepEqual(request.args.slice(0, 3), ['session', 'cancelled-job', 'retry']);
    request.resolve({ ...cancelled, status: 'queued' });
    await retry;
    assert.equal(h.state.currentJobs.value.length, 1);
    assert.equal(h.state.currentJobs.value[0].id, 'cancelled-job');
    assert.equal(h.state.currentJobs.value[0].status, 'queued');
    assert.equal(h.requests.some(item => item.name === 'acquireOpenData'), false);
  } finally { h.close(); }
});

test('acquire publishes a task and opens once while refresh and completed progress only update the receipt', async () => {
  const record = search('first', 'session', { results: [candidate()] });
  const h = harness('OpenDataResults', { initialSearch: record }, { getOpenDataSearch: async () => record, listOpenDataJobs: async () => [] });
  try {
    await flush();
    h.state.resolutions.value.candidate = { files: [{ id: 'one', filename: 'one.csv', available: true }] };
    h.state.selected.value.candidate = ['one'];
    const pending = h.state.acquire('candidate');
    const job = { id: 'new', session_id: 'session', search_id: 'first', candidate_id: 'candidate', status: 'queued', files: [], items: [{ id: 'one', status: 'queued' }] };
    h.requests.at(-1).resolve(job); await pending; await flush();
    assert.deepEqual(h.storeCalls, [['open', { searchId: 'first', jobId: 'new', automatic: true }]]);
    assert.equal(h.state.acquisitionSummary.value, '正在获取 1 个文件');
    await h.state.refresh();
    h.store.acceptJobs([{ ...job, status: 'ready', files: [file('one'), file('one')], items: [] }]); await flush();
    assert.equal(h.storeCalls.length, 1, 'Poll and completion must not reopen the panel');
    assert.equal(h.state.acquisitionSummary.value, '已保存 1 个文件');
    assert.equal(h.state.receiptAction.value, '查看文件');
    h.state.viewAcquisitions();
    assert.deepEqual(h.storeCalls.at(-1), ['open', { searchId: 'first', jobId: 'new' }]);
    assert.deepEqual(h.store.searches.value.map(record => record.id), ['first']);
  } finally { h.close(); }
});

test('right panel groups all current session tasks and delegates preview, analysis, download and retry to shared state', async () => {
  const h = harness('DataAcquisitionContent');
  const makeJob = (id, status, search_id = 'first') => ({ id, status, search_id, session_id: 'session', candidate_id: id, items: [], files: [] });
  const ready = { ...makeJob('saved', 'ready', 'second'), files: [file('one'), file('one')] };
  try {
    h.store.searches.value = [search('second', 'session', { query: '人口经济', results: [candidate('saved', { title: '城市人口' })] })];
    h.store.jobs.value = [makeJob('pending', 'downloading'), makeJob('stopped', 'cancelled'), ready, { ...makeJob('foreign', 'ready'), session_id: 'other' }];
    assert.deepEqual(h.state.groups.value.map(group => [group.id, group.jobs.map(job => job.id)]), [['pending', ['pending']], ['attention', ['stopped']], ['completed', ['saved']]]);
    assert.equal(h.state.jobTitle(ready), '城市人口');
    assert.equal(h.state.jobQuery(ready), '人口经济');
    assert.equal(h.state.savedFiles(ready).length, 1);
    h.state.previewFile(ready, file('one'));
    assert.equal(h.previews.length, 1);
    assert.equal(h.previews[0][0].file_id, 'one');
    assert.equal(h.previews[0][1].length, 1);
    h.state.previewFile(ready, file('foreign'));
    assert.equal(h.previews.length, 1);
    h.state.useFiles(ready); h.state.downloadToLocal(ready, file('one'));
    assert.deepEqual(h.storeCalls.map(call => call[0]), ['useFiles', 'downloadToLocal']);
    assert.equal(h.events.length, 0, 'Unconfirmed analysis handoff must not hide the drawer');
    h.storeCalls[0][2]();
    assert.deepEqual(h.events, [['input-prepared']]);
    h.state.useFiles({ ...ready, session_id: 'other', id: 'foreign' });
    assert.equal(h.storeCalls.length, 2);
    h.props.isShare = true; await flush();
    h.state.useFiles(ready); h.state.downloadToLocal(ready, file('one')); h.state.previewFile(ready, file('one')); h.state.refresh();
    assert.deepEqual(h.state.currentJobs.value, []);
    assert.equal(h.storeCalls.length, 2, 'A shared view must not expose private task operations');
    assert.equal(h.previews.length, 1);
    h.storeCalls[0][2]();
    assert.equal(h.events.length, 1, 'Late handoff feedback cannot close a different or shared session');
  } finally { h.close(); }
});

test('explicit focus expands a completed task and scrolls once while subsequent progress never scrolls', async () => {
  const h = harness('DataAcquisitionContent');
  let scrolls = 0;
  const ready = { id: 'ready', session_id: 'session', search_id: 'first', candidate_id: 'candidate', status: 'ready', files: [file('one')], items: [] };
  try {
    h.store.acceptJobs([ready]);
    h.state.contentRef.value = { querySelectorAll: () => [{ dataset: { acquisitionJobId: 'ready' }, scrollIntoView: () => { scrolls += 1; } }] };
    assert.equal(h.state.completedExpanded.value, false);
    h.store.open({ searchId: 'first', jobId: 'ready' }); await flush();
    assert.equal(h.state.completedExpanded.value, true);
    assert.equal(scrolls, 1);
    h.store.acceptJobs([{ ...ready, files: [file('one'), file('two')] }]); await flush();
    assert.equal(scrolls, 1);
    h.store.open({ searchId: 'first', jobId: 'ready', automatic: true }); await flush();
    assert.equal(scrolls, 1);
    h.props.sessionId = 'other'; await flush();
    assert.equal(h.state.completedExpanded.value, false);
    assert.deepEqual(h.state.currentJobs.value, []);
  } finally { h.close(); }
});

test('shared store search updates keep a mounted result card current without opening the panel', async () => {
  const h = harness('OpenDataResults');
  try {
    h.requests[0].resolve(search()); h.requests[1].resolve([]); await flush();
    h.store.acceptSearch(search('first', 'session', { results: [candidate('from-shared-poll')] })); await flush();
    assert.deepEqual(h.state.visibleCandidates.value.map(record => record.id), ['from-shared-poll']);
    h.store.acceptSearch(search('other-search', 'session', { results: [candidate('unrelated')] })); await flush();
    assert.deepEqual(h.state.visibleCandidates.value.map(record => record.id), ['from-shared-poll']);
    assert.equal(h.storeCalls.length, 0);
  } finally { h.close(); }
});

test('session files cannot be selected after route changes or when a stale request returns', async () => {
  const requests = [], events = [], cleanup = [], scope = vue.effectScope();
  const route = vue.reactive({ params: { sessionId: 'first' } });
  const visible = vue.ref(true), shared = vue.ref(false);
  const component = load('../src/components/SessionFileList.vue', {
    vue: { ...vue, onUnmounted: callback => cleanup.push(callback) },
    'vue-router': { useRoute: () => route },
    '../api/agent': { getSessionFiles: (...args) => new Promise((resolve, reject) => requests.push({ args, resolve, reject })), getSharedSessionFiles: async () => [] },
    '../api/file': { getFileDownloadUrl: async () => '' },
    '../utils/eventBus': { eventBus: { emit: (...args) => events.push(args) } },
    '../utils/time': {}, '../utils/fileType': {},
    '../composables/useSessionFileList': { useSessionFileList: () => ({ visible, shared, hideSessionFileList() {} }) },
    '../composables/useFilePanel': { useFilePanel: () => ({ showFilePanel() {} }) },
  }).default;
  const state = scope.run(() => component.setup({}, { expose() {} }));
  try {
    requests[0].resolve([file('old')]); await flush();
    route.params.sessionId = 'second';
    state.useForAnalysis(file('old'));
    assert.equal(events.length, 0, 'Immediate route change invalidates old file even before watcher flush');
    await flush(); assert.deepEqual(state.files.value, []);
    const pending = state.fetchFiles();
    requests[2].resolve([file('new')]); await pending;
    requests[1].resolve([file('late')]); await flush();
    assert.deepEqual(state.files.value.map(item => item.file_id), ['new']);
    state.useForAnalysis(file('new'));
    assert.equal(events[0][1].sessionId, 'second');
    const failing = state.fetchFiles(); requests[3].reject(new Error('/private/raw/path')); await failing;
    assert.match(state.error.value, /重试/);
    assert.doesNotMatch(state.error.value, /private/);
    assert.deepEqual(state.files.value, []);
  } finally { cleanup.forEach(callback => callback()); scope.stop(); }
});

test('discovery submit preserves manual sources and routes the legacy genome connector through existing chat', async () => {
  const source = readFileSync(new URL('../src/pages/ChatPage.vue', import.meta.url), 'utf8');
  const ast = ts.createSourceFile('chat.ts', parse(source).descriptor.scriptSetup.content, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS);
  const handler = ast.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === 'submitDiscovery');
  const code = ts.transpileModule(handler.getText(ast), { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.None } }).outputText;
  const requests = [], chats = [], notices = [];
  const scope = {
    inputMessage: vue.ref(' 查找数据 '), sessionId: vue.ref('session'), discoveryParent: vue.ref({ searchId: 'parent' }),
    selectedConnectors: vue.ref(['ncbi-geo']), nextTick: vue.nextTick, isCurrentSession: id => id === 'session',
    openDataChat: vue.ref({ start: async (...args) => { requests.push(args); return true; } }), dataDiscovery: vue.ref(true),
    eventBus: { emit() {} }, EVENT_REFRESH_SESSION_LIST: 'refresh', showInfoToast: message => notices.push(message),
    attachments: vue.ref([file('one')]), selectedSkills: vue.ref(['skill']), selectedMcpServers: vue.ref([]), selectedProfileId: vue.ref('profile'),
    chat: async (...args) => chats.push(args),
  };
  const submit = new Function('scope', `with(scope){${code};return submitDiscovery;}`)(scope);
  await submit();
  assert.deepEqual(requests[0], ['查找数据', 'parent', ['ncbi-geo']]);
  scope.inputMessage.value = '查找基因组'; scope.dataDiscovery.value = true;
  scope.selectedConnectors.value = ['ncbi-datasets-v2', 'ncbi-geo'];
  await submit();
  assert.equal(requests.length, 1);
  assert.equal(chats[0][0], '查找基因组');
  assert.deepEqual(chats[0][1].map(item => item.file_id), ['one']);
  assert.deepEqual([...scope.selectedConnectors.value], ['ncbi-datasets-v2', 'ncbi-geo']);
  assert.match(notices[0], /按所选连接器查找/);
  assert.equal(scope.dataDiscovery.value, false);
});
