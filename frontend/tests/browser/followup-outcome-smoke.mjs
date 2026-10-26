/** Real conversation/outcome/plan SFCs with synthetic fixtures and built CSS.
 * No listening server, existing browser session, API or model calls.
 * Run after npm run build: node tests/browser/followup-outcome-smoke.mjs
 */
import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { compileScript, compileStyle, compileTemplate, parse, registerTS } from '@vue/compiler-sfc';
import ts from 'typescript';
import { existsSync, readFileSync } from 'node:fs';
registerTS(() => ts);
import { createHash } from 'node:crypto';
import { mkdtemp, readFile, readdir, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { browserEngine, launchBrowser } from './runtime.mjs';

const frontend = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const result = (status, reason_code, extra = {}) => ({ status, reason_code, missing: [], can_resume: false, ...extra });
const file = { file_id: 'synthetic-published-file', filename: 'synthetic-chart.png', size: 42, upload_date: '' };
const user = timestamp => ({ type: 'user', content: { content: '请详细解释刚才的结果。', timestamp } });
const assistant = (timestamp, outcome, attachments) => ({ type: 'assistant', content: {
  content: '这是合成回归样例的结果说明。', timestamp,
  metadata: { step_id: 'same-step-id', analysis_outcome: outcome },
  ...(attachments === undefined ? {} : { attachments }),
} });
const explanationFailure = result('failed', 'answer_validation_unavailable');
const deliveredExplanation = result('partial', 'answer_validation_unavailable');
const succeeded = result('succeeded', 'requirements_satisfied');
const incomplete = result('partial', 'artifacts_missing', { missing: [{ kind: 'image', min_count: 1, label: '图表' }] });
const optionalIssue = result('succeeded', 'requirements_satisfied', {
  issues: [{ artifact_name: 'optional-chart.png', kind: 'image', reason_code: 'missing_artifact', blocking: false }],
});
const cases = [
  { id: 'explanation-no-files', messages: [user(1), assistant(2, explanationFailure)], status: 'failed', usableFiles: false, noticeTitle: '未完成' },
  { id: 'explanation-delivered-files', messages: [user(1), assistant(2, deliveredExplanation, [file])], status: 'partial', usableFiles: true, noticeTitle: '部分完成' },
  { id: 'history-files-not-current', messages: [user(1), assistant(2, succeeded, [file]), user(3), assistant(4, explanationFailure)], status: 'failed', usableFiles: false, noticeTitle: '未完成', historicalFile: true },
  { id: 'earlier-response-files-not-current', messages: [user(1), assistant(2, succeeded, [file]), assistant(3, explanationFailure)], status: 'failed', usableFiles: false, noticeTitle: '未完成', historicalFile: true },
  { id: 'user-source-not-delivery', messages: [{ type: 'attachments', content: { role: 'user', attachments: [file], timestamp: 1 } }, user(2), assistant(3, explanationFailure)], status: 'failed', usableFiles: false, noticeTitle: '未完成' },
  { id: 'malformed-not-delivery', messages: [user(1), assistant(2, explanationFailure, [{ file_id: '', filename: 'not-published.txt', size: 0 }])], status: 'failed', usableFiles: false, noticeTitle: '未完成' },
  { id: 'successful-followup', messages: [user(1), assistant(2, incomplete, [file]), user(3), assistant(4, succeeded)], status: 'completed', usableFiles: false, noticeTitle: '已完成', historicalFile: true },
  { id: 'partial-still-incomplete', messages: [user(1), assistant(2, incomplete)], status: 'partial', usableFiles: false, noticeTitle: '部分完成', missing: '待完成：图表 × 1' },
  { id: 'interrupted-no-files', messages: [user(1), assistant(2, result('failed', 'execution_interrupted'))], status: 'stopped', usableFiles: false, noticeTitle: '未完成' },
  { id: 'interrupted-delivered-files', messages: [user(1), assistant(2, result('partial', 'execution_interrupted'), [file])], status: 'stopped', usableFiles: true, noticeTitle: '部分完成' },
  { id: 'optional-issue-no-files', messages: [user(1), assistant(2, optionalIssue)], status: 'completed', usableFiles: false, noticeTitle: '已完成', optionalIssue: true, deliveryAttribution: false },
  { id: 'optional-issue-delivered-files', messages: [user(1), assistant(2, optionalIssue, [file])], status: 'completed', usableFiles: false, noticeTitle: '已完成', optionalIssue: true, deliveryAttribution: true },
];
for (const item of cases) {
  item.currentTimestamp = item.messages.at(-1).content.timestamp;
  // The application's event projector retains delivery metadata on the final
  // answer and emits a separate attachment row for the conversation renderer.
  item.messages = item.messages.flatMap(message => {
    const files = message.type === 'assistant' ? message.content.attachments : undefined;
    return Array.isArray(files) && files.some(item => item.file_id && item.filename)
      ? [message, { type: 'attachments', content: { role: 'assistant', attachments: files, timestamp: message.content.timestamp + 0.1 } }]
      : [message];
  });
  // The structured final outcome, not an old failed step snapshot, owns the UI.
  item.plan = { timestamp: 1, steps: [{ id: 'same-step-id', status: 'failed', description: '合成分析步骤', timestamp: 1 }] };
}
const revisions = [
  { tool_call_id: 'first-edit', name: 'file', function: 'file_write', args: { file: '/home/ubuntu/output/example.py', content: 'first' }, status: 'called', timestamp: 21, content: { success: true } },
  { tool_call_id: 'failed-edit', name: 'file', function: 'file_str_replace', args: { file: '/home/ubuntu/output/example.py', old_str: 'a', new_str: 'b' }, status: 'called', timestamp: 22, content: { success: false } },
  { tool_call_id: 'skipped-edit', name: 'file', function: 'file_write', args: { file: '/home/ubuntu/output/example.py', content: 'last' }, status: 'called', execution_status: 'not_started', timestamp: 23, content: { success: false } },
  { tool_call_id: 'atomic-edit', name: 'file', function: 'file_apply_patch', args: { file: '/home/ubuntu/output/example.py', replacements: [{ old_str: 'a', new_str: 'b', expected_count: 1 }] }, status: 'called', execution_status: 'succeeded', timestamp: 24, content: { success: true } },
  { tool_call_id: 'append-edit', name: 'file', function: 'file_write', args: { file: '/home/ubuntu/output/example.py', content: 'last section', append: true }, status: 'called', execution_status: 'succeeded', timestamp: 25, content: { success: true } },
  { tool_call_id: 'independent-run', name: 'shell', function: 'program_run', args: { id: 'shell', script_path: '/home/ubuntu/output/example.py' }, status: 'called', execution_status: 'succeeded', timestamp: 26, content: { console: [{ output: '', returncode: 0 }] } },
];
const questions = ['比较南北站点的异常月份，并标注置信区间。', '剔除缺测超过 20% 的站点，复核降水趋势。', '使用年度中位数替代均值，检查极端降水对结论的影响。'];
const skillReason = '已完成的站点质检、分组统计和趋势检验可对同结构数据复用。';
const skillCommand = '/skill-creator 将这个流程保存为可复用的技能';
const mapFiles = [{ file_id: 'synthetic-shape', filename: 'layer.shp', size: 100 }, { file_id: 'synthetic-dbf', filename: 'layer.dbf', size: 100 }];
const filesMessage = files => ({ type: 'attachments', content: { role: 'assistant', attachments: files, timestamp: 2 } });
const manualFile = { file_id: 'synthetic-delivered-text', filename: 'saved-result.txt', size: 42, metadata: { artifact_validation_scope: 'file_integrity', content_assessment: 'user_evaluated' } };
const adviceCases = [
  { id: 'candidate', advice: { recommendations: questions, is_skill_candidate: true, skill_reason: skillReason } },
  { id: 'ordinary', advice: { recommendations: questions, is_skill_candidate: false, skill_reason: '' } },
  { id: 'legacy', advice: { recommendations: ['使用/skill-create将这个流程替保存为可复用的技能', ...questions.slice(1)], is_skill_candidate: true, skill_reason: skillReason } },
  { id: 'skill-only', advice: { recommendations: [], is_skill_candidate: true, skill_reason: skillReason } },
  { id: 'empty', advice: { recommendations: [], is_skill_candidate: false, skill_reason: '' } },
  { id: 'readonly', readOnly: true, advice: { recommendations: questions, is_skill_candidate: true, skill_reason: skillReason } },
  { id: 'running', loading: true, advice: { recommendations: questions, is_skill_candidate: true, skill_reason: skillReason } },
  { id: 'zip-no-map', messages: [user(1), filesMessage([{ file_id: 'synthetic-zip', filename: 'figures.zip', size: 100 }])], advice: { shapefile_preview_available: true } },
  { id: 'rar-no-map', messages: [user(1), filesMessage([{ file_id: 'synthetic-rar', filename: 'data.rar', size: 100 }])], advice: { shapefile_preview_available: true, shapefile_preview_file_ids: ['synthetic-rar'] } },
  { id: 'actual-map', messages: [user(1), filesMessage(mapFiles)], advice: { shapefile_preview_available: true, shapefile_preview_file_ids: ['synthetic-shape'] } },
  { id: 'old-map', messages: [user(1), filesMessage(mapFiles), user(3)], advice: { shapefile_preview_available: true } },
  { id: 'manual-delivery', messages: [user(1), filesMessage([manualFile])], advice: {} },
];
for (const item of adviceCases) {
  item.advice = { recommendations: [], is_skill_candidate: false, skill_reason: '', ...item.advice };
}
const entry = `import {createApp,h,ref} from 'vue';
import {createI18n} from 'vue-i18n';
import AnalysisConversation from ${JSON.stringify(resolve(frontend, 'src/components/AnalysisConversation.vue'))};
import CompletionAdvicePanel from ${JSON.stringify(resolve(frontend, 'src/components/CompletionAdvicePanel.vue'))};
import PlanPanel from ${JSON.stringify(resolve(frontend, 'src/components/PlanPanel.vue'))};
import {useFilePanel} from ${JSON.stringify(resolve(frontend, 'src/composables/useFilePanel.ts'))};
import zh from ${JSON.stringify(resolve(frontend, 'src/locales/zh.ts'))};
const cases=${JSON.stringify(cases)}, clicks=[], revisions=${JSON.stringify(revisions)};
const adviceCases=${JSON.stringify(adviceCases)}, adviceClicks=[], adviceRunning=ref(false);
const app=createApp({render:()=>h('main',[...adviceCases.map(item=>h('section',{id:'advice-'+item.id,class:'fixture'},[
  h('h2',item.id),item.readOnly?h(CompletionAdvicePanel,{advice:item.advice,readOnly:true,onFollowUp:text=>adviceClicks.push({id:item.id,text})})
  :h(AnalysisConversation,{messages:item.messages||[],isLoading:item.loading||adviceRunning.value&&item.id==='candidate',completionAdvice:item.advice,messageKey:message=>message.content.timestamp,canResumeAnalysis:()=>false,onFollowUp:text=>adviceClicks.push({id:item.id,text})})
])),h('section',{id:'revision-history'},[h(AnalysisConversation,{messages:[{type:'step',content:{id:'revision-step',description:'Revision timeline',status:'completed',timestamp:20,tools:revisions}}],isLoading:false,messageKey:message=>message.content.timestamp,canResumeAnalysis:()=>false,onToolClick:tool=>clicks.push(tool.tool_call_id)})]),...cases.map(item=>h('section',{id:'case-'+item.id,class:'fixture'},[
  h('h2',item.id),
  h(AnalysisConversation,{messages:item.messages,isLoading:false,messageKey:message=>message.content.timestamp,canResumeAnalysis:()=>false}),
  h('div',{class:'fixture-plan'},[h(PlanPanel,{plan:item.plan,messages:item.messages})]),
]))])});
app.use(createI18n({legacy:false,locale:'zh',messages:{zh},missingWarn:false,fallbackWarn:false}));
app.mount('#app');
window.harness={cases,clicks,adviceCases,adviceClicks,adviceRunning,filePanel:useFilePanel(),unmount:()=>app.unmount()};window.harnessReady=true;`;
// These tool-only branches are never rendered by these conversation fixtures.
// Keep the target chain and the actual attachment components entirely real.
const offScopeTools = new Set(['DeclarativeToolCard.vue', 'ToolApprovalCard.vue', 'MonacoEditor.vue', 'ToolPanel.vue', 'BrowserToolView.vue', 'FileToolView.vue']);
const bundle = await build({
  stdin: { contents: entry, sourcefile: 'followup-outcome-entry.js', resolveDir: frontend },
  bundle: true, write: false, format: 'esm', platform: 'browser', alias: { '@': resolve(frontend, 'src') }, target: 'es2022', logLevel: 'warning',
  define: {
    'import.meta.env': JSON.stringify({ BASE_URL: '/', MODE: 'test', DEV: false, PROD: true }),
    '__VUE_OPTIONS_API__': 'true', '__VUE_PROD_DEVTOOLS__': 'false',
    '__VUE_PROD_HYDRATION_MISMATCH_DETAILS__': 'false', 'process.env.NODE_ENV': '"production"',
  },
  plugins: [{ name: 'real-followup-outcome-sfcs', setup(builder) {
    builder.onLoad({ filter: /\.vue$/ }, async ({ path }) => {
      if (offScopeTools.has(path.split('/').pop())) return { contents: 'export default { render: () => null };', loader: 'js', resolveDir: frontend };
      const source = await readFile(path, 'utf8');
      const id = createHash('sha256').update(path).digest('hex').slice(0, 12);
      const { descriptor, errors } = parse(source, { filename: path });
      if (errors.length) throw errors[0];
      let content;
      if (descriptor.script || descriptor.scriptSetup) content = compileScript(descriptor, { id, inlineTemplate: true, genDefaultAs: '__component', fs: { fileExists: existsSync, readFile: path => readFileSync(path, 'utf8') } }).content;
      else {
        const template = compileTemplate({ source: descriptor.template.content, filename: path, id });
        if (template.errors.length) throw template.errors[0];
        content = `${template.code}\nconst __component={render};`;
      }
      const styles = descriptor.styles.map(style => {
        const compiled = compileStyle({ filename: path, source: style.content, scoped: style.scoped, id: `data-v-${id}` });
        if (compiled.errors.length) throw compiled.errors[0];
        return compiled.code;
      }).join('\n');
      return { contents: `${content}\n__component.__scopeId='data-v-${id}';document.head.appendChild(Object.assign(document.createElement('style'),{textContent:${JSON.stringify(styles)}}));export default __component;`, loader: 'ts', resolveDir: dirname(path) };
    });
  } }],
});
assert.equal(bundle.outputFiles.length, 1, 'Synthetic fixture must be a single in-memory module');
const cssName = (await readdir(resolve(frontend, 'dist/assets'))).find(name => /^index-.*\.css$/.test(name));
assert.ok(cssName, 'Run npm run build before this smoke test');
const projectCss = await readFile(resolve(frontend, 'dist/assets', cssName));
const output = await mkdtemp(join(tmpdir(), 'datalab-followup-outcome-'));
const report = { name: 'followup-outcome', passed: false, cssName,
  cssSha256: createHash('sha256').update(projectCss).digest('hex'),
  errors: [], consoleErrors: [], externalAttempts: [], unexpectedRequests: [], checks: [] };
report.browserEngine = browserEngine;
const browser = await launchBrowser();
const context = await browser.newContext({ viewport: { width: 1200, height: 900 }, serviceWorkers: 'block' });
await context.addInitScript(() => localStorage.setItem('access_token', 'synthetic-browser-token'));
const page = await context.newPage();
page.setDefaultTimeout(10000);
page.on('pageerror', error => report.errors.push(error.message));
page.on('console', message => { if (message.type() === 'error') report.consoleErrors.push(message.text()); });
await context.route('**/*', async route => {
  const request = route.request(), url = new URL(request.url());
  if (url.origin !== 'http://localhost:7001') { report.externalAttempts.push(url.href); return route.abort(); }
  if (url.pathname === '/__followup_outcome__/') return route.fulfill({ contentType: 'text/html', body: '<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/__followup_outcome__/project.css"><style>html,body{margin:0;min-height:100%;font-family:Arial,sans-serif}*{box-sizing:border-box}#app{width:100%;padding:12px}main{max-width:1000px;margin:auto;min-width:0}.fixture{margin:0 0 28px;min-width:0}.fixture h2{font-size:12px;color:#777}.fixture-plan{margin-top:12px}</style></head><body><div id="app"></div><script type="module" src="/__followup_outcome__/entry.js"></script></body></html>' });
  if (url.pathname === '/__followup_outcome__/entry.js') return route.fulfill({ contentType: 'application/javascript', body: Buffer.from(bundle.outputFiles[0].contents) });
  if (url.pathname === '/api/v1/visualizations') return route.fulfill({ json: { code: 0, data: { engine: 'cordis', revision: 'fixture', plugins: [] } } });
  if (url.pathname === '/__followup_outcome__/project.css') return route.fulfill({ contentType: 'text/css', body: projectCss });
  report.unexpectedRequests.push(`${request.method()} ${url.pathname}`);
  return route.abort();
});
try {
  await page.goto('http://localhost:7001/__followup_outcome__/', { waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => window.harnessReady);
  const candidate = page.locator('#advice-candidate');
  const suggestions = candidate.getByRole('region', { name: '推荐追问', exact: true });
  const skillButton = suggestions.getByRole('button', { name: '将本次分析保存为技能', exact: true });
  assert.equal(await suggestions.getByRole('button').count(), 4);
  assert.equal(await suggestions.getByRole('button').last().innerText(), '将本次分析保存为技能');
  assert.equal(await skillButton.locator('svg.lucide-puzzle').count(), 1);
  assert.equal(await skillButton.locator('svg.lucide-chevron-right').count(), 1);
  assert.equal(await candidate.getByText(skillReason, { exact: true }).count(), 0);
  for (const question of questions) await suggestions.getByRole('button', { name: question, exact: true }).click();
  await skillButton.focus(); await page.keyboard.press('Enter');
  assert.deepEqual(await page.evaluate(() => window.harness.adviceClicks), [...questions, skillCommand].map(text => ({ id: 'candidate', text })));
  assert.equal(await page.locator('#advice-ordinary').getByRole('button', { name: '将本次分析保存为技能', exact: true }).count(), 0);
  assert.equal(await page.locator('#advice-legacy').getByRole('region', { name: '推荐追问', exact: true }).getByRole('button').count(), 3);
  assert.equal(await page.locator('#advice-legacy').getByText(/替保存/).count(), 0);
  assert.equal(await page.locator('#advice-legacy').getByRole('button', { name: '将本次分析保存为技能', exact: true }).count(), 1);
  assert.equal(await page.locator('#advice-skill-only').getByRole('region', { name: '推荐追问', exact: true }).count(), 1);
  assert.equal(await page.locator('#advice-skill-only').getByRole('button').count(), 1);
  assert.equal(await page.locator('#advice-empty').getByRole('region').count(), 0);
  assert.equal(await page.locator('#advice-running').getByRole('region').count(), 0);
  const shared = page.locator('#advice-readonly');
  assert.equal(await shared.getByRole('button').count(), 0);
  await shared.getByText('将本次分析保存为技能', { exact: true }).click();
  await shared.getByText(questions[0], { exact: true }).click();
  assert.equal(await page.evaluate(() => window.harness.adviceClicks.length), 4, 'Share view must not dispatch analysis or skill actions');
  await page.evaluate(() => window.harness.adviceRunning.value = true);
  await page.waitForFunction(() => !document.querySelector('#advice-candidate .completion-advice'));
  await page.evaluate(() => window.harness.adviceRunning.value = false);
  await skillButton.waitFor();
  report.checks.push({ recommendations: '3 independent model directions followed by a candidate-only skill action; no skill reason displayed; exact command; keyboard activation; legacy filtering; hidden during runs; read-only share' });
  for (const id of ['zip-no-map', 'rar-no-map', 'old-map']) {
    assert.equal(await page.locator(`#advice-${id}`).getByRole('button', { name: '查看 Shapefile 地图', exact: true }).count(), 0, `${id}: only current delivered .shp files may offer a map shortcut`);
  }
  await page.locator('#advice-actual-map').getByRole('button', { name: '查看 Shapefile 地图', exact: true }).click();
  assert.deepEqual(await page.evaluate(() => ({ file: window.harness.filePanel.fileInfo.value?.file_id, companions: window.harness.filePanel.relatedFiles.value.map(file => file.file_id) })), { file: 'synthetic-shape', companions: ['synthetic-shape', 'synthetic-dbf'] });
  const manualDelivery = page.locator('#advice-manual-delivery');
  assert.doesNotMatch(await manualDelivery.innerText(), /评判|评估|未进行格式内容校验/);
  assert.equal(await page.locator('[title*="评判"], [title*="评估"]').count(), 0);
  await manualDelivery.getByText(manualFile.filename, { exact: true }).click();
  assert.equal(await page.evaluate(() => window.harness.filePanel.fileInfo.value?.file_id), manualFile.file_id);
  report.checks.push({ fileDelivery: 'delivery policy metadata produces no assessment labels or tooltips; ordinary ZIP/RAR and historical maps have no map shortcut; real current .shp opens with its companions' });
  for (const item of cases) {
    const fixture = page.locator(`#case-${item.id}`);
    const current = fixture.locator(`[data-message-key="${item.currentTimestamp}"]`);
    const notice = current.getByRole('status', { name: '分析完成情况', exact: true });
    if (item.id === 'successful-followup') {
      assert.equal(await notice.count(), 0, 'Pure successful answer is represented by stage progress without duplicating a banner');
      assert.equal(await fixture.locator('[data-plan-status]').getAttribute('data-plan-status'), 'completed');
      assert.ok(await fixture.getByText(file.filename, { exact: true }).count() > 0);
      report.checks.push({ id: item.id, pureSuccessWithoutDuplicateBanner: true });
      continue;
    }
    assert.equal(await notice.count(), 1, `${item.id}: real ChatMessage must render its current structured outcome`);
    const text = (await notice.innerText()).replace(/\s+/g, ' ').trim();
    assert.ok(text.startsWith(item.noticeTitle), `${item.id}: outcome title mismatch: ${text}`);
    assert.equal(text.includes('本次已交付的文件仍可查看，内容需结合核验结论使用。'), item.usableFiles, `${item.id}: file availability must reflect only this response's delivered attachments without promising scientific validity`);
    if (!item.usableFiles && !item.optionalIssue) assert.ok(!/文件.*(?:可用|使用|仍保留)/.test(text), `${item.id}: no unsupported file-availability claims`);
    if (item.missing) assert.ok(text.includes(item.missing), `${item.id}: genuinely missing deliverables remain visible`);
    const plan = fixture.locator('[data-plan-status]');
    assert.equal(await plan.getAttribute('data-plan-status'), item.status, `${item.id}: plan must agree with current outcome, not the old failed snapshot`);
    if (item.status === 'completed') {
      const planText = (await plan.innerText()).replace(/\s+/g, ' ').trim();
      assert.ok(planText.includes('阶段记录：已完成') && planText.includes('1 / 1 个阶段完成'), `${item.id}: completed follow-up must display a successful plan`);
      assert.equal(await plan.locator('button > svg.lucide-check').count(), 1, `${item.id}: completed plan has success icon`);
      assert.equal(await plan.locator('svg.lucide-circle-x').count(), 0, `${item.id}: no stale failure icon`);
    }
    if (item.id === 'successful-followup') {
      assert.ok(text.includes('本次分析已完成。'), 'Successful explanation must show completion');
      assert.ok(!/未完成|未确认|核验|待完成|不可|尚未|暂不能/.test(text), 'Successful follow-up must not retain historical failure warnings');
    }
    if (item.historicalFile) {
      assert.ok(await fixture.getByText(file.filename, { exact: true }).count() > 0, `${item.id}: historical delivered file must actually exist in the rendered conversation`);
      assert.ok(!text.includes('本次已交付'), `${item.id}: history cannot create a current-turn delivery claim`);
    }
    if (item.optionalIssue) {
      assert.ok(text.includes('以下文件未计入本次交付。'), `${item.id}: optional failed file is not a delivered artifact`);
      assert.equal(text.includes('本次已交付文件以附件为准。'), item.deliveryAttribution, `${item.id}: attachment attribution requires current delivered files`);
    }
    report.checks.push({ id: item.id, currentNotice: text, planStatus: item.status, currentResponseFilesUsable: item.usableFiles });
  }
  const history = page.locator('#revision-history');
  assert.match(await history.innerText(), /文件保存记录/);
  assert.match(await history.innerText(), /保存完成：写入 1 次、局部修正 1 次、追加 1 次/);
  assert.match(await history.innerText(), /分析程序运行已结束/);
  assert.doesNotMatch(await history.innerText(), /新建|Analysis program execution finished/);
  const disclosure = history.locator('details');
  assert.equal(await disclosure.count(), 1);
  await disclosure.locator('summary').click();
  assert.equal(await disclosure.getAttribute('open'), '');
  for (const [index, label] of ['写入 · 已完成', '局部修正 · 失败', '写入 · 未执行', '局部修正 · 已完成', '追加 · 已完成', '运行记录 · 已完成'].entries()) {
    assert.match(await disclosure.innerText(), new RegExp(label));
    await disclosure.locator('.clickable').nth(index).click();
  }
  assert.deepEqual(await page.evaluate(() => window.harness.clicks), ['first-edit', 'failed-edit', 'skipped-edit', 'atomic-edit', 'append-edit', 'independent-run']);
  assert.equal(await history.locator('.clickable').count(), 8, 'Five saved requests plus their run, one collapsed file card and independent run remain actionable');
  report.checks.push({ revisionHistory: 'all original requests have real state, time and per-call tool output actions; execution remains independent' });
  for (const width of [320, 390, 768, 1200]) {
    await page.setViewportSize({ width, height: 900 });
    const geometry = await page.evaluate(() => ({ viewport: innerWidth, pageWidth: document.documentElement.scrollWidth,
      overflowingNotices: [...document.querySelectorAll('[aria-label="分析完成情况"]')].filter(element => element.scrollWidth > element.clientWidth + 1).length,
    }));
    assert.ok(geometry.pageWidth <= width + 1, `${width}px: conversation must not overflow the viewport`);
    assert.equal(geometry.overflowingNotices, 0, `${width}px: outcome wording and missing-file labels must wrap`);
    assert.equal(await candidate.locator('.completion-advice').evaluate(el => el.scrollWidth > el.clientWidth + 1), false, `${width}px: recommendation and skill action must wrap`);
    report.checks.push({ width, geometry });
    await candidate.screenshot({ path: join(output, `recommendations-${width}.png`) });
    await page.screenshot({ path: join(output, `outcomes-${width}.png`), fullPage: true });
  }
  assert.deepEqual(await page.evaluate(() => window.harness.cases), cases, 'Rendering must not mutate source history or final metadata');
  assert.deepEqual(await page.evaluate(() => window.harness.adviceCases), adviceCases, 'Rendering legacy recommendations must not rewrite stored advice');
  assert.deepEqual(report.errors, []);
  assert.deepEqual(report.consoleErrors, []);
  assert.deepEqual(report.externalAttempts, []);
  assert.deepEqual(report.unexpectedRequests, []);
  await page.evaluate(() => window.harness.unmount());
  assert.equal(await page.locator('#app').evaluate(element => element.childElementCount), 0);
  report.passed = true;
} catch (error) {
  report.failure = error.message;
  report.stack = error.stack;
  await page.screenshot({ path: join(output, 'failure.png'), fullPage: true }).catch(() => {});
  process.exitCode = 1;
} finally {
  await context.close();
  await browser.close();
  await writeFile(join(output, 'results.json'), JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report));
  console.log(`BROWSER_REPORT=${output}`);
}
