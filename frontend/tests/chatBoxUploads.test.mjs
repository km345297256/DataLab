import assert from 'node:assert/strict';
import { File } from 'node:buffer';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import * as vue from 'vue';
import { compileScript, parse } from '@vue/compiler-sfc';
import ts from 'typescript';
import { readyExplorationFiles, mergeExplorationFiles } from '../src/utils/explorationFiles.ts';

// Execute the production SFC setup functions with Vue's real refs/computed/watch.
// Network calls are controlled promises; no upload algorithm is reimplemented.
function compile(name) {
  const source = readFileSync(new URL(`../src/components/${name}.vue`, import.meta.url), 'utf8');
  return ts.transpileModule(compileScript(parse(source).descriptor, { id: `${name}-upload-test` }).content, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText;
}
const scripts = { files: compile('ChatBoxFiles'), composer: compile('ChatBox') };
const flush = async () => { for (let index = 0; index < 6; index++) await Promise.resolve(); await vue.nextTick(); };
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

function mount(script, props, modules = {}) {
  const scope = vue.effectScope();
  const module = { exports: {} };
  const events = [];
  const unmounted = [];
  let exposed;
  const imports = {
    vue: { ...vue, onMounted() {}, onUnmounted: callback => { if (script === scripts.files) unmounted.push(callback); } },
    'vue-i18n': { useI18n: () => ({ t: value => value }) },
    'lucide-vue-next': {},
    '../utils/fileType': { getFileType: () => ({ icon: {} }), getFileTypeText: () => 'File', formatFileSize: String },
    '../composables/useFilePanel': { useFilePanel: () => ({ showFilePanel() {} }) },
    '../composables/useSettingsDialog': { useSettingsDialog: () => ({ openSettingsDialog() {} }) },
    '../utils/explorationFiles': { readyExplorationFiles, mergeExplorationFiles },
    '@/api/dataConnector': { listDataConnectors: async () => [] },
    '@/utils/skillLabel': { skillLabel: value => value },
    '@/utils/skillCatalog': { filterSkillCatalog: value => value, skillCategory: () => '', skillCategories: [] },
    '../api/skill': { getSkillPreferences: async () => [], listSkills: async () => [], listSkillCatalog: async () => [] },
    '../utils/toast': { showErrorToast() {} },
    '../utils/eventBus': { eventBus: { on() {}, off() {} } },
    '../constants/event': { EVENT_SKILL_CATALOG_UPDATED: 'catalog', EVENT_SKILL_PREFERENCES_UPDATED: 'preferences' },
    ...modules,
  };
  new Function('require', 'module', 'exports', script)(name => {
    if (name.endsWith('.vue')) return {};
    assert.ok(name in imports, `Unexpected component dependency: ${name}`);
    return imports[name];
  }, module, module.exports);
  const state = scope.run(() => module.exports.default.setup(props, {
    expose: value => { exposed = value; }, emit: (...args) => events.push(args),
  }));
  return { state, exposed, events, close: () => { unmounted.forEach(callback => callback()); scope.stop(); } };
}

function harness(hooks = {}) {
  const calls = [];
  const filesByUpload = new Map();
  let generation = 0;
  const api = {
    async initLargeUpload(file) {
      const id = `upload-${++generation}`;
      calls.push({ method: 'init', id, file });
      filesByUpload.set(id, file);
      if (hooks.init) return hooks.init(id, file);
      return { upload_id: id, part_size: 4 };
    },
    async uploadLargeFilePart(id, number, blob, progress) {
      calls.push({ method: 'part', id, number, blob });
      if (hooks.part) return hooks.part(id, number, blob, progress);
      progress(blob.size);
      return { etag: `etag-${number}`, size: blob.size };
    },
    async completeLargeUpload(id, parts) {
      calls.push({ method: 'complete', id, parts });
      if (hooks.complete) return hooks.complete(id, parts);
      return { file_id: `saved-${id}`, filename: filesByUpload.get(id).name, size: filesByUpload.get(id).size };
    },
    async abortLargeUpload(id) {
      calls.push({ method: 'abort', id });
      if (hooks.abort) return hooks.abort(id);
    },
  };
  const props = vue.reactive({ attachments: [] });
  const uploader = mount(scripts.files, props, { '../api/file': api });
  return { ...uploader, props, calls, api };
}

async function muteExpectedErrors(callback) {
  const error = console.error, warn = console.warn;
  console.error = () => {};
  console.warn = () => {};
  try { await callback(); } finally { console.error = error; console.warn = warn; }
}

test('a small selected file uses init, one part and complete, and only then becomes sendable', async () => {
  const complete = deferred();
  const h = harness({ complete: () => complete.promise });
  try {
    let clicks = 0;
    h.state.fileInput.value = { click: () => clicks++ };
    h.exposed.uploadFile();
    assert.equal(clicks, 1);
    const file = new File(['abc'], 'small.csv', { type: 'text/csv' });
    const input = { files: [file], value: 'selected-file' };
    const selected = h.state.handleFileSelect({ target: input });
    await flush();
    assert.deepEqual(h.calls.map(call => call.method), ['init', 'part', 'complete']);
    assert.equal(await h.calls[1].blob.text(), 'abc');
    assert.equal(h.exposed.getFiles().length, 1);
    assert.equal(h.exposed.isAllUploaded.value, false);
    assert.equal(h.exposed.getFiles()[0].status, 'uploading');
    assert.equal(h.exposed.getFiles()[0].progress, 99);
    complete.resolve({ file_id: 'server-file', filename: 'small.csv', size: 3 });
    await selected;
    assert.equal(input.value, '');
    assert.equal(h.exposed.isAllUploaded.value, true);
    assert.equal(h.exposed.getFiles()[0].file_id, 'server-file');
    assert.equal(h.exposed.getFiles()[0].file, null);
    assert.equal(h.exposed.getFiles()[0].progress, 100);
  } finally { h.close(); }
});

test('multipart uploads keep at most three workers, slice exact bytes and complete every part once', async () => {
  const pending = new Map();
  let inFlight = 0, maxInFlight = 0;
  const h = harness({ part: (_id, number, blob) => {
    const part = deferred();
    pending.set(number, { ...part, blob });
    inFlight++;
    maxInFlight = Math.max(inFlight, maxInFlight);
    return part.promise.finally(() => { inFlight--; });
  } });
  try {
    const upload = h.state.processLargeFileUpload(new File(['abcdefghijklmn'], 'multi.csv'));
    await flush();
    assert.deepEqual([...pending.keys()], [1, 2, 3]);
    pending.get(2).resolve({ etag: 'etag-2', size: 4 }); await flush();
    assert.deepEqual([...pending.keys()], [1, 2, 3, 4]);
    assert.equal(h.calls.some(call => call.method === 'complete'), false);
    for (const number of [4, 1, 3]) pending.get(number).resolve({ etag: `etag-${number}`, size: pending.get(number).blob.size });
    await upload;
    assert.equal(maxInFlight, 3);
    assert.deepEqual(await Promise.all([...pending.values()].map(part => part.blob.text())), ['abcd', 'efgh', 'ijkl', 'mn']);
    const completion = h.calls.filter(call => call.method === 'complete');
    assert.equal(completion.length, 1);
    assert.deepEqual(completion[0].parts.toSorted((a, b) => a.part_number - b.part_number), [
      { part_number: 1, etag: 'etag-1', size: 4 }, { part_number: 2, etag: 'etag-2', size: 4 },
      { part_number: 3, etag: 'etag-3', size: 4 }, { part_number: 4, etag: 'etag-4', size: 2 },
    ]);
    assert.equal(h.exposed.isAllUploaded.value, true);
    assert.equal(h.calls.some(call => call.method === 'abort'), false);
  } finally { h.close(); }
});

test('a failed part waits for in-flight workers and cleanup; retry reuses one card and ignores double activation', async () => {
  await muteExpectedErrors(async () => {
    const pending = new Map(), cleanup = deferred(), retryInit = deferred();
    const h = harness({
      init: id => id === 'upload-2' ? retryInit.promise : { upload_id: id, part_size: 4 },
      part: (id, number, blob) => {
        if (id !== 'upload-1') return { etag: `retry-${number}`, size: blob.size };
        const request = deferred(); pending.set(number, request); return request.promise;
      },
      abort: () => cleanup.promise,
    });
    try {
      const upload = h.state.processLargeFileUpload(new File(['abcdefghijklmnop'], 'retry.csv'));
      await flush();
      const card = h.exposed.getFiles()[0], originalId = card.file_id;
      pending.get(1).reject(new Error('Part failed')); await flush();
      assert.equal(h.calls.some(call => call.method === 'abort'), false);
      pending.get(2).resolve({ etag: 'etag-2', size: 4 }); await flush();
      assert.equal(pending.has(4), false);
      assert.equal(h.calls.some(call => call.method === 'abort'), false);
      pending.get(3).reject(new Error('Another in-flight part failed')); await flush();
      assert.equal(h.calls.filter(call => call.method === 'abort').length, 1);
      assert.equal(h.calls.some(call => call.method === 'complete'), false);
      assert.equal(card.status, 'uploading');
      assert.equal(h.exposed.isAllUploaded.value, false);
      await h.state.retryUpload(card);
      assert.equal(h.calls.filter(call => call.method === 'init').length, 1);
      cleanup.resolve(); await upload;
      assert.equal(card.status, 'failed');
      assert.equal(h.exposed.isAllUploaded.value, false);
      const retry = h.state.retryUpload(card);
      const duplicate = h.state.retryUpload(card);
      assert.equal(h.exposed.getFiles().length, 1);
      assert.equal(h.exposed.getFiles()[0].file_id, originalId);
      assert.equal(h.calls.filter(call => call.method === 'init').length, 2);
      retryInit.resolve({ upload_id: 'upload-2', part_size: 4 });
      await Promise.all([retry, duplicate]);
      assert.equal(h.exposed.getFiles().length, 1);
      assert.equal(h.exposed.getFiles()[0].file_id, 'saved-upload-2');
      assert.equal(h.exposed.isAllUploaded.value, true);
      assert.deepEqual(h.calls.filter(call => call.method === 'complete').map(call => call.id), ['upload-2']);
    } finally { h.close(); }
  });
});

test('initialization failure leaves one retryable card and does not abort an upload that never existed', async () => {
  await muteExpectedErrors(async () => {
    const h = harness({ init: () => Promise.reject(new Error('Initialization unavailable')) });
    try {
      await h.state.processLargeFileUpload(new File(['abc'], 'not-started.csv'));
      assert.deepEqual(h.calls.map(call => call.method), ['init']);
      assert.equal(h.exposed.getFiles().length, 1);
      assert.equal(h.exposed.getFiles()[0].status, 'failed');
      assert.equal(h.exposed.getFiles()[0].file.name, 'not-started.csv');
      assert.equal(h.exposed.isAllUploaded.value, false);
    } finally { h.close(); }
  });
});

for (const allowSendFilesOnly of [false, true]) {
  test(`composer cannot submit pending or failed uploads even with text (files-only enabled: ${allowSendFilesOnly})`, async () => {
    const h = harness();
    const props = vue.reactive({ modelValue: 'Analyze this file', rows: 3, isRunning: false,
      attachments: h.props.attachments, allowSendFilesOnly, showLibraryActions: true, disabled: false });
    const composer = mount(scripts.composer, props);
    try {
      composer.state.chatBoxFileListRef.value = vue.proxyRefs(h.exposed);
      for (const status of ['uploading', 'failed']) {
        h.props.attachments.splice(0, h.props.attachments.length, { file_id: 'temp-pending', filename: 'data.csv', status });
        assert.equal(composer.state.sendEnabled.value, false);
        composer.state.handleSubmit();
        assert.equal(composer.events.filter(([event]) => event === 'submit').length, 0);
      }
      h.props.attachments.splice(0, h.props.attachments.length, { file_id: 'stored-file', filename: 'data.csv', status: 'success' });
      assert.equal(composer.state.sendEnabled.value, true);
      composer.state.handleSubmit();
      assert.equal(composer.events.filter(([event]) => event === 'submit').length, 1);
    } finally { composer.close(); h.close(); }
  });
}


for (const boundary of ['replace-draft', 'unmount']) {
  test(`an upload batch stops and aborts its old upload after ${boundary}`, async () => {
    const pendingInit = deferred();
    const h = harness({ init: () => pendingInit.promise });
    try {
      const upload = h.state.handleFileSelect({ target: { files: [new File(['first'], 'first.csv'), new File(['second'], 'second.csv')], value: '' } });
      await flush();
      assert.equal(h.calls.length, 1);
      if (boundary === 'replace-draft') h.props.attachments = [];
      else h.close();
      await flush();
      pendingInit.resolve({ upload_id: 'upload-1', part_size: 4 });
      await upload;
      assert.deepEqual(h.calls.map(call => call.method), ['init', 'abort']);
      if (boundary === 'replace-draft') assert.deepEqual(h.exposed.getFiles(), []);
    } finally { h.close(); }
  });
}

test('replacing a draft during multipart work waits for owned workers then aborts without scheduling more parts', async () => {
  const pendingPart = deferred();
  const h = harness({ part: () => pendingPart.promise });
  try {
    const upload = h.state.processLargeFileUpload(new File(['abcdefghijklmnop'], 'first.csv'));
    await flush();
    assert.equal(h.calls.filter(call => call.method === 'part').length, 3);
    h.props.attachments = [];
    pendingPart.resolve({ etag: 'part', size: 4 });
    await upload;
    assert.equal(h.calls.filter(call => call.method === 'part').length, 3);
    assert.equal(h.calls.some(call => call.method === 'complete'), false);
    assert.equal(h.calls.at(-1).method, 'abort');
    assert.deepEqual(h.exposed.getFiles(), []);
  } finally { h.close(); }
});

test('deleting a personal skill clears selected and automatic skills even while the picker is closed', async () => {
  const staleSkills = deferred(), stalePreferences = deferred();
  let preferenceCalls = 0;
  const props = vue.reactive({ modelValue: '', attachments: [], selectedSkills: ['mine', 'keep'] });
  const h = mount(scripts.composer, props, { '../api/skill': {
    listSkills: () => staleSkills.promise,
    listSkillCatalog: async () => [],
    getSkillPreferences: () => ++preferenceCalls === 1 ? stalePreferences.promise : Promise.resolve(['keep']),
  } });
  try {
    h.state.availableSkills.value = [{id:'mine-id', name:'mine'}, {id:'keep-id', name:'keep'}];
    h.state.setAutoEnabledSkills(['mine', 'keep']);
    const oldLoad = h.state.loadAvailableSkills();
    h.state.handleSkillCatalogUpdated({deletedSkillId:'mine-id', deletedSkillName:'mine'});
    assert.deepEqual(h.events.find(([type]) => type === 'update:selectedSkills'), ['update:selectedSkills', ['keep']]);
    assert.deepEqual(h.state.availableSkills.value.map(item => item.name), ['keep']);
    assert.equal(h.state.autoEnabledSkillNames.value.has('mine'), false);
    staleSkills.resolve([{id:'mine-id', name:'mine'}]); stalePreferences.resolve(['mine']);
    await oldLoad; await flush();
    assert.equal(h.state.autoEnabledSkillNames.value.has('mine'), false, 'a pre-delete request must not restore stale automatic use');
    assert.deepEqual(h.state.availableSkills.value.map(item => item.name), ['keep']);
    assert.equal(h.state.skillsLoaded.value, false);
  } finally { h.close(); }
});
