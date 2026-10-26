import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import * as vue from 'vue';
import { compileScript, parse } from '@vue/compiler-sfc';
import ts from 'typescript';
import * as presentation from '../src/utils/analysisPresentation.ts';
import * as skillCreation from '../src/utils/skillCreation.ts';
import * as previewActions from '../src/utils/previewActions.ts';

const script = ts.transpileModule(compileScript(parse(readFileSync(new URL('../src/components/AnalysisConversation.vue', import.meta.url), 'utf8')).descriptor, { id: 'conversation-actions' }).content,
  { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
const user = timestamp => ({ type: 'user', content: { timestamp, content: 'Synthetic input' } });
const delivered = (file, timestamp = 3) => ({ type: 'attachments', content: { timestamp, role: 'assistant', attachments: [file] } });
const zip = { file_id: 'current-zip', filename: 'current.zip' };
const layer = [{ file_id: 'map-shp', filename: 'map.shp' }, { file_id: 'map-dbf', filename: 'map.dbf' }];
function harness(messages, completionAdvice) {
  const opened = [], errors = [], requests = [], emitted = [];
  let active = true;
  const scope = vue.effectScope();
  const props = vue.reactive({ messages, completionAdvice, sessionId: 'first', isLoading: false, messageKey: message => message.content.timestamp, canResumeAnalysis: () => false });
  const show = (file, files) => { if (active) opened.push({ file, files }); };
  const imports = {
    vue, 'lucide-vue-next': {},
    '../api/file': { prepareShapefilePreview: () => new Promise(resolve => requests.push(resolve)) },
    '../types/message': { isConsecutiveAssistant: () => false },
    '../utils/analysisPresentation': presentation,
    '../utils/skillCreation': skillCreation,
    '../utils/previewActions': previewActions,
    '../composables/useFilePanel': { useFilePanel: () => { vue.onScopeDispose(() => { active = false; }); return { showFilePanel: show, beginFilePreview: () => ({ isCurrent: () => active, show }) }; } },
    '../utils/toast': { showErrorToast: message => errors.push(message) },
  };
  const module = { exports: {} };
  new Function('require', 'module', 'exports', script)(name => { if (name.endsWith('.vue')) return {}; assert.ok(name in imports, name); return imports[name]; }, module, module.exports);
  const state = scope.run(() => module.exports.default.setup(props, { expose() {}, emit: (...args) => emitted.push(args) }));
  return { props, state, opened, errors, requests, emitted, close: () => scope.stop() };
}

test('skill recovery creates one explicit follow-up, never an analysis resume or automatic replay', () => {
  const failure = { type: 'assistant', content: { timestamp: 2, content: '技能未完成', metadata: {
    skill_creation_failure: { stage: 'implementation', code: 'script_syntax_invalid', retryable: true },
  } } };
  const h = harness([user(1), failure]);
  try {
    const current = h.props.messages[1];
    assert.equal(h.state.canRetrySkillCreation(current), true);
    assert.deepEqual(h.emitted, [], 'mounting a failed history never retries');
    h.state.retrySkillCreation(current);
    h.state.retrySkillCreation(current);
    assert.deepEqual(h.emitted, [['followUp', '/skill-creator 将本次已完成的分析流程保存为可复用的技能']]);
    assert.equal(h.state.canRetrySkillCreation(current), false);
  } finally { h.close(); }
});

for (const boundary of ['running', 'waiting', 'context-not-ready', 'non-retryable', 'new-turn', 'newer-response']) {
  test(`skill recovery action respects ${boundary}`, () => {
    const h = harness([user(1), { type: 'assistant', content: { timestamp: 2, content: '技能未完成', metadata: {
      skill_creation_failure: { stage: 'implementation', code: 'script_syntax_invalid', retryable: true },
    } } }]);
    try {
      const failure = h.props.messages[1];
      if (boundary === 'running') h.props.isLoading = true;
      if (boundary === 'waiting') h.props.waitingForUser = true;
      if (boundary === 'context-not-ready') h.props.allowFollowUp = false;
      if (boundary === 'non-retryable') failure.content.metadata.skill_creation_failure.retryable = false;
      if (boundary === 'new-turn') h.props.messages.push(user(3));
      if (boundary === 'newer-response') h.props.messages.push({ type: 'assistant', content: { timestamp: 3, content: '最新回答' } });
      assert.equal(h.state.canRetrySkillCreation(failure), false);
      h.state.retrySkillCreation(failure);
      assert.deepEqual(h.emitted, []);
    } finally { h.close(); }
  });
}

test('molecular shortcut uses only current-turn delivered files, never user input or historic artifacts', () => {
  const old = { file_id: 'old', filename: 'old.pdb' }, current = { file_id: 'new', filename: 'new.pdb' };
  const h = harness([user(1), delivered(old, 2), user(3), { type: 'attachments', content: { role: 'user', attachments: [old], timestamp: 4 } }]);
  try {
    h.state.openMolecularPreview(); assert.equal(h.opened.length, 0); assert.equal(h.errors.length, 1);
    h.props.messages.push(delivered(current, 5)); h.state.openMolecularPreview();
    assert.equal(h.opened.length, 1); assert.equal(h.opened[0].file.file_id, 'new');
    assert.deepEqual(h.opened[0].files.map(file => file.file_id), ['new']);
  } finally { h.close(); }
});

for (const filename of ['current.zip', 'other.rar', 'map.zip', 'result.svg']) {
  for (const explicit of [false, true]) {
    test(`Shapefile shortcut never infers a map from ${filename} (${explicit ? 'explicit' : 'legacy'} advice)`, () => {
      const advice = { shapefile_preview_available: true, ...(explicit ? { shapefile_preview_file_ids: [zip.file_id] } : {}) };
      const h = harness([user(1), delivered({ ...zip, filename })], advice);
      try {
        assert.deepEqual(h.state.shapefileSources.value, []);
        h.state.openShapefilePreview();
        assert.deepEqual(h.opened, []);
        assert.deepEqual(h.requests, [], 'No archive inspection or extraction is started by the shortcut');
      } finally { h.close(); }
    });
  }
}

for (const explicit of [false, true]) {
  test(`actual delivered Shapefile opens with its current-turn companions (${explicit ? 'explicit' : 'legacy'} advice)`, () => {
    const advice = { shapefile_preview_available: true, ...(explicit ? { shapefile_preview_file_ids: [layer[0].file_id] } : {}) };
    const h = harness([user(1), ...layer.map((file, index) => delivered(file, 2 + index))], advice);
    try {
      assert.deepEqual(h.state.shapefileSources.value, [layer[0]]);
      h.state.openShapefilePreview();
      assert.deepEqual(h.opened, [{ file: layer[0], files: layer }]);
      assert.deepEqual(h.requests, []);
      assert.deepEqual(h.errors, []);
    } finally { h.close(); }
  });
}

for (const boundary of ['missing-advice', 'disabled', 'wrong-id', 'empty-ids', 'malformed-ids', 'new-turn', 'user-input', 'running', 'unmount']) {
  test(`Shapefile shortcut refuses ${boundary}`, () => {
    const h = harness([user(1), delivered(layer[0])], { shapefile_preview_available: true, shapefile_preview_file_ids: [layer[0].file_id] });
    try {
      if (boundary === 'missing-advice') h.props.completionAdvice = undefined;
      if (boundary === 'disabled') h.props.completionAdvice.shapefile_preview_available = false;
      if (boundary === 'wrong-id') h.props.completionAdvice.shapefile_preview_file_ids = ['historic-shape'];
      if (boundary === 'empty-ids') h.props.completionAdvice.shapefile_preview_file_ids = [];
      if (boundary === 'malformed-ids') h.props.completionAdvice.shapefile_preview_file_ids = [null];
      if (boundary === 'new-turn') h.props.messages.push(user(10));
      if (boundary === 'user-input') h.props.messages[1].content.role = 'user';
      if (boundary === 'running') h.props.isLoading = true;
      if (boundary === 'unmount') h.close();
      h.state.openShapefilePreview();
      assert.deepEqual(h.opened, []);
      assert.deepEqual(h.requests, []);
    } finally { h.close(); }
  });
}
