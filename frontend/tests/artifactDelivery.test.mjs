import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import * as vue from 'vue';
import { renderToString } from '@vue/server-renderer';
import { compileScript, parse } from '@vue/compiler-sfc';
import ts from 'typescript';
import { readAnalysisOutcome, analysisOutcomeIsComplete } from '../src/utils/analysisOutcome.ts';

const file = metadata => ({ file_id: 'saved-file', filename: 'result.svg', size: 400, metadata });
const manual = { artifact_validation_scope: 'file_integrity', content_assessment: 'user_evaluated' };

const componentSource = readFileSync(new URL('../src/components/AttachmentsMessage.vue', import.meta.url), 'utf8');
const script = compileScript(parse(componentSource).descriptor, { id: 'artifact-delivery', inlineTemplate: true });
const compiled = ts.transpileModule(script.content, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText;
const imports = {
  vue, 'lucide-vue-next': { Eye: { render: () => null }, FileSearch: { render: () => null }, PackageOpen: { render: () => null } }, 'vue-i18n': { useI18n: () => ({ t: value => value }) },
  '../utils/fileType': { formatFileSize: () => '400 B', getFileTypeText: () => 'SVG', getFileType: () => ({ icon: 'span' }) },
  '../composables/useSessionFileList': { useSessionFileList: () => ({ showSessionFileList() {} }) },
  '../composables/useFilePanel': { useFilePanel: () => ({ showFilePanel() {} }) },
};
const module = { exports: {} };
new Function('require', 'module', 'exports', compiled)(name => { assert.ok(name in imports, name); return imports[name]; }, module, module.exports);
const render = (role, metadata) => renderToString(vue.createSSRApp(module.exports.default, {
  content: { role, attachments: [file(metadata)] }, hideAllFilesButton: true,
}));

test('actual attachment cards do not turn delivery policy metadata into assessment notices or tooltips', async () => {
  for (const role of ['assistant', 'user']) {
    for (const metadata of [undefined, {}, manual, { artifact_validation_scope: 'file_integrity' }]) {
      const html = await render(role, metadata);
      assert.match(html, /result.svg/);
      assert.doesNotMatch(html, /评判|评估|未进行格式内容校验|内容检查通过|科学验证通过|文件无效/);
    }
  }
});

test('saved files and preview errors cannot rewrite historical failure or missing-delivery outcomes', () => {
  for (const reason_code of ['artifact_validation_failed', 'delivery_failed', 'tool_execution_failed']) {
    const historical = { status: 'partial', reason_code, missing: [{ kind: 'image', min_count: 10, label: '图表', formats: ['svg'] }],
      can_resume: false, issues: [{ artifact_name: 'result.svg', kind: 'image', reason_code: 'invalid_content', blocking: true }] };
    const before = JSON.stringify(historical);
    const restored = readAnalysisOutcome(historical);
    assert.equal(restored.status, 'partial');
    assert.equal(restored.missing[0].min_count, 10);
    assert.equal(analysisOutcomeIsComplete(restored), false);
    assert.equal(JSON.stringify(historical), before);
  }
});
