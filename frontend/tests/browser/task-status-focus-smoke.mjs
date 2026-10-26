/** Real PlanPanel/SimpleBar SFCs, built utility CSS and native browser input.
 * Synthetic local fixtures only: no listening server, model call or real API.
 * Run after npm run build: node tests/browser/task-status-focus-smoke.mjs
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
// WebKit follows macOS keyboard navigation: Option-Tab includes buttons.
const nextTab = browserEngine === 'webkit' ? 'Alt+Tab' : 'Tab';
const previousTab = browserEngine === 'webkit' ? 'Alt+Shift+Tab' : 'Shift+Tab';
const outcome = (status, reason_code, missing = []) => ({ status, reason_code, missing, can_resume: false });
const step = (id, status, description = `合成步骤 ${id}`) => ({ id, status, description, timestamp: 1 });
const messages = (stepId, result) => [
  { type: 'user', content: { content: '合成分析请求', timestamp: 1 } },
  { type: 'assistant', content: { content: '合成结果，不参与状态推断', timestamp: 2, metadata: { step_id: stepId, analysis_outcome: result } } },
];
const cases = [
  { id: 'completed', steps: [step('done', 'completed')], expected: 'completed', title: '阶段记录：已完成', completed: 1, icon: 'stage-success', stepStatuses: ['completed'] },
  { id: 'failed', steps: [step('failed', 'failed')], expected: 'failed', title: '阶段记录：失败', completed: 0, icon: 'lucide-circle-x', stepStatuses: ['failed'] },
  { id: 'mixed', steps: [step('done', 'completed'), step('failed', 'failed')], expected: 'partial', title: '阶段记录：部分完成', completed: 1, icon: 'lucide-circle-alert', stepStatuses: ['completed', 'failed'] },
  { id: 'partial-result', steps: [step('partial', 'failed')], messages: messages('partial', outcome('partial', 'artifacts_missing', [{ kind: 'image', min_count: 1, label: '图表' }])), expected: 'partial', title: '阶段记录：部分完成', completed: 0, icon: 'lucide-circle-alert', stepStatuses: ['partial'] },
  { id: 'stopped', steps: [step('stopped', 'failed')], messages: messages('stopped', outcome('failed', 'user_cancelled')), expected: 'stopped', title: '阶段记录：已停止', completed: 0, icon: 'lucide-circle-pause', stepStatuses: ['stopped'] },
  { id: 'running-after-failure', steps: [step('failed', 'failed'), step('running', 'running', '正在生成合成图表'), step('next', 'pending')], expected: 'running', title: '正在生成合成图表', completed: 0, icon: 'lucide-clock', stepStatuses: ['failed', 'running', 'pending'] },
  { id: 'pending', steps: [step('next', 'pending', '等待合成数据分析')], expected: 'pending', title: '等待合成数据分析', completed: 0, icon: 'lucide-clock', stepStatuses: ['pending'] },
  { id: 'empty', steps: [], expected: 'pending', completed: 0, icon: 'lucide-clock', stepStatuses: [] },
];

const entry = `import {createApp,h,nextTick} from 'vue';
import {createI18n} from 'vue-i18n';
import PlanPanel from ${JSON.stringify(resolve(frontend, 'src/components/PlanPanel.vue'))};
import SimpleBar from ${JSON.stringify(resolve(frontend, 'src/components/SimpleBar.vue'))};
import {Select,SelectTrigger,SelectValue,SelectContent,SelectItem} from './src/components/ui/select';
import {Popover,PopoverTrigger,PopoverContent} from './src/components/ui/popover';
import {Dialog,DialogTrigger,DialogContent,DialogTitle,DialogDescription} from './src/components/ui/dialog';
import zh from ${JSON.stringify(resolve(frontend, 'src/locales/zh.ts'))};
const cases=${JSON.stringify(cases)};
const app=createApp({render:()=>h('main',[
  h('section',{id:'focus-fixture'},[
    h('button',{id:'before-region',type:'button'},'滚动区域前的按钮'),
    h('div',{id:'scroll-fixture'},[h(SimpleBar,{},()=>h('div',{id:'scroll-body'},[
      h('p',{id:'pointer-target'},'点击这里检查滚动容器焦点，不显示整页蓝框。'),
      h('button',{id:'inner-button',type:'button'},'区域内键盘按钮'),
      h('div',{id:'scroll-filler'},Array.from({length:35},(_,index)=>h('p','合成滚动内容 '+index))),
    ]))]),
    h('button',{id:'after-region',type:'button'},'滚动区域后的按钮'),
  ]),
  h('section',{id:'control-fixture'},[
    h('input',{id:'standalone-field',placeholder:'普通输入框',class:'border rounded px-3 py-2'}),
    h('label',{id:'compound-field',class:'control-shell border rounded p-2'},[
      h('input',{id:'compound-editor',placeholder:'复合输入框'}),h('button',{id:'compound-action',type:'button'},'查看'),
    ]),
    h('label',{id:'compound-select-shell',class:'control-shell border rounded p-2'},[
      h('select',{id:'compound-select'},[h('option',{value:'one'},'组合下拉框')]),
    ]),
    h('input',{id:'invalid-field','aria-invalid':'true',class:'border rounded px-3 py-2'}),
    h('label',[h('input',{id:'choice-control',type:'checkbox'}),'勾选文件']),
    h(Select,{defaultValue:'one'},()=>[
      h(SelectTrigger,{id:'styled-select'},()=>h(SelectValue)),
      h(SelectContent,{},()=>[h(SelectItem,{value:'one'},()=> '选项一'),h(SelectItem,{value:'two'},()=> '选项二')]),
    ]),
    h(Popover,{},()=>[
      h(PopoverTrigger,{asChild:true},()=>h('button',{id:'open-popover',type:'button'},'打开浮层')),
      h(PopoverContent,{id:'styled-popover',class:'p-4 border',onOpenAutoFocus:event=>{event.preventDefault();event.target.focus();}},()=>h('button',{id:'popover-action',type:'button'},'浮层操作')),
    ]),
    h(Dialog,{},()=>[
      h(DialogTrigger,{asChild:true},()=>h('button',{id:'open-dialog',type:'button'},'打开对话框')),
      h(DialogContent,{id:'styled-dialog',class:'p-6',onOpenAutoFocus:event=>{event.preventDefault();event.target.focus();}},()=>[
        h(DialogTitle,{},()=> '对话框'),h(DialogDescription,{},()=> '边框检查'),h('button',{id:'dialog-action',type:'button'},'对话框操作'),
      ]),
    ]),
  ]),
  h('section',{id:'status-fixtures'},cases.map(item=>h('section',{id:'case-'+item.id,class:'status-fixture'},[
    h('h2',item.id),h(PlanPanel,{plan:{steps:item.steps,timestamp:1},messages:item.messages||[]}),
  ]))),
])});
app.use(createI18n({legacy:false,locale:'zh',messages:{zh},missingWarn:false,fallbackWarn:false}));
app.mount('#app');
window.harness={cases,flush:()=>nextTick(),unmount:()=>app.unmount()};
window.harnessReady=true;`;

const bundle = await build({
  stdin: { contents: entry, sourcefile: 'task-status-focus-entry.js', resolveDir: frontend },
  bundle: true, write: false, format: 'esm', platform: 'browser', alias: { '@': resolve(frontend, 'src') }, target: 'es2022', logLevel: 'warning',
  define: {
    'import.meta.env': JSON.stringify({ BASE_URL: '/', MODE: 'test', DEV: false, PROD: true }),
    '__VUE_OPTIONS_API__': 'true', '__VUE_PROD_DEVTOOLS__': 'false',
    '__VUE_PROD_HYDRATION_MISMATCH_DETAILS__': 'false', 'process.env.NODE_ENV': '"production"',
  },
  plugins: [{ name: 'real-task-status-focus-sfcs', setup(builder) {
    builder.onLoad({ filter: /\.vue$/ }, async ({ path }) => {
      const source = await readFile(path, 'utf8');
      const id = createHash('sha256').update(path).digest('hex').slice(0, 12);
      const { descriptor, errors } = parse(source, { filename: path });
      if (errors.length) throw errors[0];
      let content;
      if (descriptor.script || descriptor.scriptSetup) {
        content = compileScript(descriptor, { id, inlineTemplate: true, genDefaultAs: '__component', fs: { fileExists: existsSync, readFile: path => readFileSync(path, 'utf8') } }).content;
      } else {
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
assert.equal(bundle.outputFiles.length, 1, 'The fixture must stay a single in-memory module');
const cssName = (await readdir(resolve(frontend, 'dist/assets'))).find(name => /^index-.*\.css$/.test(name));
assert.ok(cssName, 'Run npm run build before this smoke test');
const projectCss = await readFile(resolve(frontend, 'dist/assets', cssName));
const output = await mkdtemp(join(tmpdir(), 'datalab-task-status-focus-'));
const report = {
  name: 'task-status-focus', passed: false, cssName,
  cssSha256: createHash('sha256').update(projectCss).digest('hex'),
  errors: [], consoleErrors: [], externalAttempts: [], unexpectedRequests: [], checks: [],
};
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
  if (url.pathname === '/__task_status_focus__/') return route.fulfill({ contentType: 'text/html', body: `<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/__task_status_focus__/project.css"><style>
html,body{margin:0;min-height:100%;font-family:Arial,sans-serif}*{box-sizing:border-box}
#app{width:100%;padding:16px 12px}main{max-width:1100px;margin:auto;min-width:0}
#focus-fixture{margin-bottom:24px}#focus-fixture button{padding:6px 8px;border:1px solid #888;border-radius:4px;background:white}
#control-fixture{display:flex;flex-wrap:wrap;gap:12px;margin:16px 0}#control-fixture input,#control-fixture select{min-width:0;max-width:100%}#control-fixture label{display:flex;align-items:center;gap:8px;min-width:0;max-width:100%}#compound-editor{width:130px}
#scroll-fixture{height:260px;width:100%;margin:10px 0;border:1px solid #ddd}
#scroll-body{width:100%;min-width:0;padding:16px}#pointer-target{margin:0 0 12px;min-height:30px}
#scroll-filler p{padding:18px 0;margin:0}.status-fixture{margin:16px 0;min-width:0}.status-fixture h2{margin:0 0 4px;font-size:12px;color:#777}
</style></head><body><div id="app"></div><script type="module" src="/__task_status_focus__/entry.js"></script></body></html>` });
  if (url.pathname === '/__task_status_focus__/entry.js') return route.fulfill({ contentType: 'application/javascript', body: Buffer.from(bundle.outputFiles[0].contents) });
  if (url.pathname === '/__task_status_focus__/project.css') return route.fulfill({ contentType: 'text/css', body: projectCss });
  report.unexpectedRequests.push(`${request.method()} ${url.pathname}`);
  return route.abort();
});

const region = page.getByRole('region', { name: 'scrollable content', exact: true });
const readFocus = locator => locator.evaluate(element => {
  const style = getComputedStyle(element);
  return { active: document.activeElement === element, visible: element.matches(':focus-visible'),
    outlineStyle: style.outlineStyle, outlineWidth: style.outlineWidth, outlineColor: style.outlineColor, outlineOffset: style.outlineOffset,
    boxShadow: style.boxShadow, scrollTop: element.scrollTop, scrollHeight: element.scrollHeight, clientHeight: element.clientHeight };
});
const waitForScrollToSettle = locator => locator.evaluate(element => new Promise((resolve, reject) => {
  let previous = element.scrollTop, stableFrames = 0;
  const started = performance.now();
  function observe() {
    const current = element.scrollTop;
    stableFrames = Math.abs(current - previous) < 0.5 ? stableFrames + 1 : 0;
    previous = current;
    if (stableFrames >= 6) return resolve();
    if (performance.now() - started > 3000) return reject(new Error('Native scroll did not settle'));
    requestAnimationFrame(observe);
  }
  requestAnimationFrame(observe);
}));

try {
  await page.goto('http://localhost:7001/__task_status_focus__/', { waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => window.harnessReady);
  for (const width of [320, 390, 768, 1200]) {
    await page.setViewportSize({ width, height: 900 });
    await region.evaluate(element => { element.scrollTop = 0; });
    await page.getByRole('button', { name: '滚动区域前的按钮', exact: true }).click();
    await page.locator('#pointer-target').click();
    const pointer = await readFocus(region);
    assert.equal(pointer.active, true, `${width}px: pointer focus must actually reach the scroll region`);
    assert.equal(pointer.outlineStyle, 'none', `${width}px: pointer click must not show browser-default blue outline`);
    assert.equal(pointer.outlineWidth, '0px', `${width}px: the outer focus frame must have no width`);
    assert.ok(pointer.scrollHeight > pointer.clientHeight, `${width}px: fixture must genuinely overflow vertically`);

    await page.getByRole('button', { name: '滚动区域前的按钮', exact: true }).click();
    await page.keyboard.press(nextTab);
    const keyboard = await readFocus(region);
    assert.equal(keyboard.active, true, `${width}px: Tab must reach the scroll region`);
    assert.equal(keyboard.visible, true, `${width}px: native keyboard focus must remain distinguishable`);
    assert.equal(keyboard.outlineStyle, 'none', `${width}px: keyboard focus must not restore the outer blue frame`);
    assert.ok(keyboard.boxShadow.includes('inset'), `${width}px: keyboard focus needs its scoped inset marker`);
    assert.ok(keyboard.boxShadow.includes('1px'), `${width}px: the inset marker should be a quiet 1px line`);
    await page.keyboard.press('PageDown');
    await page.waitForFunction(start => document.querySelector('.simplebar-content-wrapper').scrollTop > start, keyboard.scrollTop);
    // PageDown scrolls asynchronously. Finish this animation before changing
    // focus or viewport so its later frames cannot leak into the next case.
    await waitForScrollToSettle(region);
    const paged = await readFocus(region);
    assert.equal(paged.active, true, `${width}px: PageDown must retain scroll-region focus`);
    assert.ok(paged.scrollTop > keyboard.scrollTop, `${width}px: PageDown must scroll actual contents`);
    await page.keyboard.press(nextTab);
    const child = await readFocus(page.locator('#inner-button'));
    assert.equal(child.active, true, `${width}px: child buttons must remain in the keyboard tab order`);
    assert.equal(child.visible, true, `${width}px: child buttons must retain native keyboard focus visibility`);
    assert.ok((child.outlineStyle !== 'none' && parseFloat(child.outlineWidth) > 0) || child.boxShadow !== 'none', `${width}px: removing the container frame must not remove child-button focus indicators`);
    const geometry = await page.evaluate(() => ({ viewportWidth: innerWidth, pageWidth: document.documentElement.scrollWidth,
      regionWidth: document.querySelector('.simplebar-content-wrapper').clientWidth,
      regionScrollWidth: document.querySelector('.simplebar-content-wrapper').scrollWidth }));
    assert.ok(geometry.pageWidth <= geometry.viewportWidth + 1, `${width}px: no horizontal viewport overflow`);
    assert.ok(geometry.regionScrollWidth <= geometry.regionWidth + 1, `${width}px: scroll region must not overflow horizontally`);
    report.checks.push({ width, pointer, keyboard, pageDownScrollTop: paged.scrollTop, child, geometry });
  }

  await page.setViewportSize({ width: 1200, height: 900 });
  // Exercise both native and composed controls: one focus edge, never a ring
  // on a shell and its editor at the same time. Pointer and keyboard differ.
  const edge = async (id, width, offset) => {
    const value = await readFocus(page.locator(id));
    assert.equal(value.outlineWidth,width,`${id}: expected a single focus edge`);
    assert.equal(value.outlineOffset,offset,`${id}: focus edge must follow the control boundary`);
    assert.equal(value.boxShadow,'none',`${id}: no second ring shadow`);
    return value;
  };
  await page.locator('#standalone-field').click();
  await edge('#standalone-field','1px','-1px');
  await page.locator('#compound-editor').click();
  await edge('#compound-field','1px','-1px');
  assert.equal((await readFocus(page.locator('#compound-editor'))).outlineStyle,'none');
  await page.keyboard.press(nextTab);
  const compoundButton=await readFocus(page.locator('#compound-action'));
  assert.equal(compoundButton.active,true);assert.equal(compoundButton.outlineWidth,'2px');
  assert.equal((await readFocus(page.locator('#compound-field'))).outlineStyle,'none','A nested button gets its own marker without outlining the input shell');
  await page.locator('#compound-select').focus();
  await edge('#compound-select-shell','1px','-1px');
  assert.equal((await readFocus(page.locator('#compound-select'))).outlineStyle,'none');
  await page.locator('#invalid-field').focus();
  const invalidStyle=await page.locator('#invalid-field').evaluate(el=>{
    const marker=document.createElement('span');marker.style.color='var(--function-error)';el.parentElement.append(marker);
    const expected=getComputedStyle(marker).color;marker.remove();return {actual:getComputedStyle(el).outlineColor,expected};
  });
  assert.equal(invalidStyle.actual,invalidStyle.expected,'Validation errors keep their error-colored edge');
  await page.locator('#choice-control').click();
  const pointerChoice=await readFocus(page.locator('#choice-control'));
  assert.equal(pointerChoice.outlineStyle,'none','Pointer selection must not add a keyboard frame');
  await page.keyboard.press(nextTab);await page.keyboard.press(previousTab);
  const keyboardChoice=await edge('#choice-control','2px','2px');
  assert.equal(keyboardChoice.active,true);assert.equal(keyboardChoice.visible,true);
  await page.keyboard.press(nextTab);
  await edge('#styled-select','1px','-1px');
  await page.keyboard.press('ArrowDown');
  await page.locator('[data-slot="select-content"]').waitFor();
  assert.equal((await readFocus(page.locator('[data-slot="select-content"]'))).outlineStyle,'none');
  await page.keyboard.press('Escape');
  await edge('#styled-select','1px','-1px');
  for(const kind of ['popover','dialog']){
    await page.locator('#open-'+kind).click();
    const container=page.locator(`[data-slot="${kind}-content"]`);await container.waitFor();
    await container.focus();
    const containerFocus=await readFocus(container);
    assert.equal(containerFocus.active,true,`${kind}: the check must exercise a focused popup container`);
    assert.equal(containerFocus.outlineStyle,'none',`${kind}: popup containers must not gain a browser focus frame: ${JSON.stringify(containerFocus)}`);
    if(kind==='popover'){
      const style=await container.evaluate(el=>({border:getComputedStyle(el).borderTopWidth,shadow:getComputedStyle(el).boxShadow}));
      assert.equal(style.border,'1px');assert.ok(!style.shadow.includes('0px 0px 0px 1px'),'The popover must not draw a second border via its shadow');
    }
    await page.keyboard.press(nextTab);
    const action=await readFocus(page.locator('#'+kind+'-action'));
    assert.equal(action.active,true);assert.equal(action.outlineWidth,'2px');
    await page.keyboard.press('Escape');
  }
  report.checks.push({id:'shared-controls',result:'native text/select and Reka select have one inset edge; nested controls have no duplicate shell ring; pointer checkbox has no frame and keyboard remains visible; popup containers retain a single border'});
  for (const item of cases) {
    const fixture = page.locator(`#case-${item.id}`);
    const summary = fixture.locator('[data-plan-status]');
    assert.equal(await summary.getAttribute('data-plan-status'), item.expected, `${item.id}: summary status`);
    const text = (await summary.innerText()).replace(/\s+/g, ' ').trim();
    if (item.title) assert.ok(text.includes(item.title), `${item.id}: expected ${item.title}, received ${text}`);
    if (item.expected !== 'completed') assert.ok(!text.includes('阶段记录：已完成'), `${item.id}: a non-success must not claim task completion`);
    if (item.steps.length) assert.ok(text.includes(`${item.completed} / ${item.steps.length} 个阶段完成`), `${item.id}: only succeeded steps count as completed; received ${text}`);
    assert.equal(await summary.locator(item.expected === 'completed' ? 'button > svg.lucide-check' : `button > svg.${item.icon}`).count(), 1, `${item.id}: icon and status must agree`);
    if (item.expected === 'completed') assert.equal(await summary.locator('svg.lucide-circle-x').count(), 0, `${item.id}: completed cannot retain a failure icon`);
    if (item.expected === 'running') assert.equal(await summary.locator('svg.lucide-circle-x').count(), 0, `${item.id}: earlier failure cannot replace the active icon`);
    await summary.getByRole('button', { name: '查看任务进度与执行记录', exact: true }).click();
    const renderedSteps = await fixture.locator('[data-plan-step-status]').evaluateAll(elements => elements.map(element => element.dataset.planStepStatus));
    assert.deepEqual(renderedSteps, item.stepStatuses, `${item.id}: expanded rows must use the same status semantics`);
    report.checks.push({ id: item.id, summary: text, status: item.expected, icon: item.icon, expandedSteps: renderedSteps });
  }

  await page.screenshot({ path: join(output, 'statuses-expanded-1200.png'), fullPage: true });
  await page.setViewportSize({ width: 320, height: 900 });
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), 'Expanded task rows must not overflow a narrow viewport');
  await page.screenshot({ path: join(output, 'statuses-expanded-320.png'), fullPage: true });
  assert.deepEqual(await page.evaluate(() => window.harness.cases), cases, 'Rendering and input must not mutate synthetic source history');
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
