/** Exercise the real generated-skill card and preview dialog using synthetic owner APIs. */
import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { compileScript, compileStyle, compileTemplate, parse, registerTS } from '@vue/compiler-sfc';
import ts from 'typescript';
import { existsSync, readFileSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { mkdtemp, readFile, readdir, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { browserEngine, launchBrowser } from './runtime.mjs';

registerTS(() => ts);
const frontend = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const files = {
  'SKILL.md': '# 可复用的数据检查\n\n**检查输入结构**，保持输入只读。\n\n[查看分析脚本](scripts/analyze.py)\n\n[查看数据说明](references/input-contract.json)\n\n[外部资料](https://example.org/reference)\n\n[javascript 链接](javascript:window.__previewExecuted=true)\n\n![远程图片](https://example.org/private-pixel.png)\n\n<script>window.__previewExecuted=true</script>\n<img src="https://example.org/raw-pixel.png" onerror="window.__previewExecuted=true">\n<iframe src="https://example.org/frame"></iframe>\n<svg onload="window.__previewExecuted=true"></svg>\n<a href="#" data-skill-resource="scripts/analyze.py">伪造资源属性</a>\n\n| 输入 | 输出 |\n| --- | --- |\n| 选定文件 | 检查报告 |\n',
  'scripts/analyze.py': '# 仅预览源码，不执行程序\nprint("<script>window.__previewExecuted = true</script>")\n' + '# ' + 'long-content-'.repeat(80) + '\n',
  'references/input-contract.json': '{"large_id":900719925474099312345,"decimal":1.2300e-20,"negative_zero":-0,"quoted":"a:b,{c}","nested":[{},[],{"字段":"中文"}]}',
  'references/retry.json': '{"retry":"加载成功"}',
  'references/slow.json': '{"late":"STALE-RESOURCE-CONTENT"}',
  'references/empty.txt': '',
  'references/说明 & 配置.json': '{"路径":"使用查询参数精确传递"}',
};
const ownerId = 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa';
const sharedId = 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb';
const secondId = 'cccccccccccccccccccccccccccccccc';
const draft = { id: ownerId, name: 'data-inspection', description: '查看输入结构并生成独立检查结果。', capabilities: ['检查结构与边界条件'], triggers: ['检查文件结构'], applicability: ['需要重复使用的数据检查流程'], resources: Object.keys(files), validation: { package: 'passed', script_syntax: 'passed', workflow_execution: 'not_run' }, installed: false, installed_skill_id: null };
const observedSources = ['src/components/SkillCreationCard.vue', 'src/components/SkillResourcePreview.vue', 'src/utils/skillResourcePreview.ts', 'src/api/skillDraft.ts'];
const sourceHashes = async () => Object.fromEntries(await Promise.all(observedSources.map(async path => [path, createHash('sha256').update(await readFile(resolve(frontend, path))).digest('hex')])));
const beforeHashes = await sourceHashes();
const entry = `import {createApp,h,ref} from 'vue';
import SkillCreationCard from ${JSON.stringify(resolve(frontend, 'src/components/SkillCreationCard.vue'))};
const draft=${JSON.stringify(draft)};
const mounted=ref(true), owner=ref(draft), shared=ref(false);
const app=createApp({render:()=>h('main',[
 mounted.value?h('section',{id:'owner'},[h(SkillCreationCard,{initial:owner.value,isShare:shared.value})]):null,
 h('section',{id:'shared'},[h(SkillCreationCard,{initial:{...draft,id:${JSON.stringify(sharedId)},name:'PRIVATE-SHARED-NAME',description:'PRIVATE-SHARED-DESCRIPTION'},isShare:true})])
])});
app.mount('#app');
window.harness={mounted,owner,shared,replaceOwner(id){owner.value={...draft,id};},unmount(){app.unmount();}};
window.harnessReady=true;`;

const bundle = await build({
  stdin: { contents: entry, sourcefile: 'skill-resource-preview-entry.js', resolveDir: frontend },
  bundle: true, write: false, format: 'esm', platform: 'browser', alias: { '@': resolve(frontend, 'src') }, target: 'es2022', logLevel: 'warning',
  define: { 'import.meta.env': JSON.stringify({ BASE_URL: '/', MODE: 'test', DEV: false, PROD: true }), '__VUE_OPTIONS_API__': 'true', '__VUE_PROD_DEVTOOLS__': 'false', '__VUE_PROD_HYDRATION_MISMATCH_DETAILS__': 'false', 'process.env.NODE_ENV': '"production"' },
  plugins: [{ name: 'real-skill-preview-sfcs', setup(builder) {
    builder.onLoad({ filter: /\.vue$/ }, async ({ path }) => {
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
assert.equal(bundle.outputFiles.length, 1);
const cssName = (await readdir(resolve(frontend, 'dist/assets'))).find(name => /^index-.*\.css$/.test(name));
assert.ok(cssName, 'Run npm run build before the preview smoke test');
const projectCss = await readFile(resolve(frontend, 'dist/assets', cssName));
const output = await mkdtemp(join(tmpdir(), 'datalab-skill-resource-preview-'));
const report = { name: 'skill-resource-preview', browserEngine, passed: false, sourceHashes: beforeHashes, checks: [], errors: [], unexpectedRequests: [], requests: [] };
const browser = await launchBrowser();
const context = await browser.newContext({ viewport: { width: 1200, height: 1000 }, serviceWorkers: 'block' });
await context.addInitScript(() => {
  localStorage.setItem('access_token', 'synthetic-preview-token');
  window.__previewExecuted = false;
  window.__copied = [];
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: async value => { window.__copied.push(value); } } });
});
const page = await context.newPage();
page.setDefaultTimeout(10000);
page.on('pageerror', error => report.errors.push(error.message));
let retryFailed = false, delayNext = false, pendingRelease = null, pendingStarted = null, releaseFinished = null;
function delayResource() {
  delayNext = true;
  return new Promise(resolve => { pendingStarted = resolve; });
}
async function releaseResource() {
  assert.ok(pendingRelease, 'The delayed request must be observed before release');
  const done = releaseFinished;
  pendingRelease(); pendingRelease = null;
  await done;
}
await context.route('**/*', async route => {
  const request = route.request(), url = new URL(request.url());
  if (url.origin !== 'http://localhost:7001') { report.unexpectedRequests.push(url.href); return route.abort(); }
  if (url.pathname === '/__skill_preview__/') return route.fulfill({ contentType: 'text/html', body: '<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/__skill_preview__/project.css"><style>html,body{margin:0;min-height:100%;font-family:Arial,sans-serif}*{box-sizing:border-box}#app{padding:16px}main{max-width:820px;margin:auto;min-width:0}#shared{margin-top:20px}</style></head><body><div id="app"></div><script type="module" src="/__skill_preview__/entry.js"></script></body></html>' });
  if (url.pathname === '/__skill_preview__/entry.js') return route.fulfill({ contentType: 'application/javascript', body: Buffer.from(bundle.outputFiles[0].contents) });
  if (url.pathname === '/__skill_preview__/project.css') return route.fulfill({ contentType: 'text/css', body: projectCss });
  const match = url.pathname.match(/^\/api\/v1\/skills\/drafts\/([^/]+)(?:\/(resource|install|download))?$/);
  if (!match) { report.unexpectedRequests.push(`${request.method()} ${url.pathname}`); return route.abort(); }
  const [, id, action] = match, path = url.searchParams.get('path');
  report.requests.push({ id, action: action || 'read', path, method: request.method(), authorization: request.headers().authorization });
  if (id === sharedId || request.method() !== 'GET' || ['install', 'download'].includes(action)) { report.unexpectedRequests.push('Preview caused an unauthorized draft access or mutation'); return route.abort(); }
  if (!action) return route.fulfill({ json: { code: 0, data: { ...draft, id } } });
  if (path === 'references/retry.json' && !retryFailed) {
    retryFailed = true;
    return route.fulfill({ status: 503, json: { code: 503, msg: 'Synthetic temporary failure', data: null } });
  }
  let markFinished;
  if (path === 'references/slow.json' && delayNext) {
    delayNext = false;
    releaseFinished = new Promise(resolve => { markFinished = resolve; });
    const pause = new Promise(resolve => { pendingRelease = resolve; });
    pendingStarted?.(); pendingStarted = null;
    await pause;
  }
  try {
    if (!(path in files)) return await route.fulfill({ status: 404, json: { code: 404, msg: 'Not found', data: null } });
    await route.fulfill({ json: { code: 0, data: { path, content: files[path], binary: false } } });
  } catch (error) {
    // Aborting a closed/switched preview cancels the browser request. It is
    // expected that Playwright cannot deliver its intentionally late reply.
    if (!markFinished) throw error;
  } finally { markFinished?.(); }
});

try {
  await page.goto('http://localhost:7001/__skill_preview__/', { waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => window.harnessReady);
  const owner = page.locator('#owner');
  await owner.locator('summary').click();
  const markdownTrigger = owner.getByRole('button', { name: '预览 SKILL.md', exact: true });
  await page.waitForFunction(() => !document.querySelector('#owner .skill-files button').disabled);
  assert.equal(report.requests.filter(item => item.action === 'resource').length, 0, 'The file list must not fetch content before a user opens a file');
  assert.equal(await page.locator('#shared').getByRole('button').count(), 0);
  assert.doesNotMatch(await page.locator('#shared').innerText(), /PRIVATE/);
  await markdownTrigger.click();
  const dialog = page.getByRole('dialog', { name: '技能文件预览', exact: true });
  await dialog.getByRole('heading', { name: '可复用的数据检查', exact: true }).waitFor();
  assert.equal(await dialog.locator('img,iframe,script,svg[onload],form').count(), 0);
  assert.equal(await page.evaluate(() => window.__previewExecuted), false);
  assert.equal(await dialog.getByText('伪造资源属性', { exact: true }).getAttribute('data-skill-resource'), null);
  assert.equal(await dialog.getByText('javascript 链接', { exact: true }).getAttribute('href'), null);
  assert.equal(await dialog.getByRole('link', { name: '外部资料', exact: true }).getAttribute('rel'), 'noopener noreferrer');
  assert.deepEqual(report.unexpectedRequests, [], 'Rendering Markdown must never fetch remote content');
  await dialog.getByRole('button', { name: '源码', exact: true }).click();
  assert.equal(await dialog.getByLabel('SKILL.md 文件内容', { exact: true }).textContent(), files['SKILL.md']);
  await dialog.getByRole('button', { name: '复制内容', exact: true }).click();
  await dialog.getByText('已复制文件原文。', { exact: true }).waitFor();
  assert.deepEqual(await page.evaluate(() => window.__copied), [files['SKILL.md']]);
  await dialog.getByRole('button', { name: '预览', exact: true }).click();
  await dialog.getByRole('link', { name: '查看分析脚本', exact: true }).click();
  assert.equal(await dialog.getByLabel('scripts/analyze.py 文件内容', { exact: true }).textContent(), files['scripts/analyze.py']);
  assert.equal(await page.evaluate(() => window.__previewExecuted), false, 'Code content must only be text');
  assert.equal(await dialog.getByLabel('选择技能文件').inputValue(), 'scripts/analyze.py');
  report.checks.push('Click-only resource reads, rendered Markdown/source toggle, explicit copy, internal package link navigation, scripts remain inert text, and no remote media/script requests');

  await dialog.getByLabel('选择技能文件').selectOption('references/input-contract.json');
  const jsonPreview = dialog.getByLabel('references/input-contract.json 文件内容', { exact: true });
  await jsonPreview.waitFor();
  const formatted = await jsonPreview.textContent();
  assert.ok(formatted.includes('\n'), 'Compact JSON should be readable');
  assert.ok(formatted.includes('900719925474099312345'));
  assert.ok(formatted.includes('1.2300e-20'));
  assert.ok(formatted.includes('"negative_zero": -0'));
  assert.ok(formatted.includes('"quoted": "a:b,{c}"'));
  await dialog.getByRole('button', { name: '复制内容', exact: true }).click();
  await page.waitForFunction(() => window.__copied.length === 2);
  assert.equal((await page.evaluate(() => window.__copied))[1], files['references/input-contract.json'], 'Copy returns the exact original, not formatted or rounded values');
  for (const width of [1200, 390]) {
    await page.setViewportSize({ width, height: 1000 });
    const dimensions = await dialog.evaluate(element => {
      const bounds = element.getBoundingClientRect();
      const body = element.querySelector('.skill-preview-body');
      return { x: bounds.x, right: bounds.right, width: element.clientWidth, scrollWidth: element.scrollWidth, bodyWidth: body.clientWidth, bodyScrollWidth: body.scrollWidth, documentWidth: document.documentElement.scrollWidth };
    });
    assert.ok(dimensions.x >= 0 && dimensions.right <= width + 1, `Dialog must fit ${width}px viewport`);
    assert.ok(dimensions.scrollWidth <= dimensions.width + 1 && dimensions.bodyScrollWidth <= dimensions.bodyWidth + 1 && dimensions.documentWidth <= width + 1);
    await dialog.screenshot({ path: join(output, `skill-resource-json-${width}.png`) });
    report.checks.push({ width, noHorizontalOverflow: true });
  }
  await dialog.getByLabel('选择技能文件').selectOption('references/说明 & 配置.json');
  await dialog.getByLabel('references/说明 & 配置.json 文件内容', { exact: true }).waitFor();
  assert.ok(report.requests.some(item => item.path === 'references/说明 & 配置.json'), 'Nested Unicode and ampersands must reach the API as one exact query parameter');
  await dialog.getByLabel('选择技能文件').selectOption('references/empty.txt');
  await dialog.getByText('此文件为空。', { exact: true }).waitFor();
  report.checks.push('JSON preserves large integers, exponent spelling, negative zero and strings; copy preserves source bytes; Unicode nested paths and empty files are handled');

  await dialog.getByLabel('选择技能文件').selectOption('references/retry.json');
  await dialog.getByRole('alert').waitFor();
  assert.equal(await dialog.getByRole('button', { name: '复制内容', exact: true }).isDisabled(), true);
  await dialog.getByRole('button', { name: '重试预览', exact: true }).click();
  await dialog.getByLabel('references/retry.json 文件内容', { exact: true }).waitFor();
  assert.equal(report.requests.filter(item => item.path === 'references/retry.json').length, 2);
  report.checks.push('A failed file read clears previous content and supports explicit retry without installation');

  let started = delayResource();
  await dialog.getByLabel('选择技能文件').selectOption('references/slow.json');
  await started;
  await dialog.getByText('正在读取文件…', { exact: true }).waitFor();
  assert.equal(await dialog.locator('pre').count(), 0, 'Old content must disappear while a new file is pending');
  await dialog.getByLabel('选择技能文件').selectOption('scripts/analyze.py');
  await dialog.getByLabel('scripts/analyze.py 文件内容', { exact: true }).waitFor();
  await releaseResource();
  assert.equal(await dialog.getByLabel('选择技能文件').inputValue(), 'scripts/analyze.py');
  assert.doesNotMatch(await dialog.innerText(), /STALE-RESOURCE-CONTENT/);

  started = delayResource();
  await dialog.getByLabel('选择技能文件').selectOption('references/slow.json');
  await started;
  await dialog.getByRole('button', { name: '关闭预览', exact: true }).click();
  await dialog.waitFor({ state: 'hidden' });
  await releaseResource();
  await page.waitForFunction(() => document.activeElement?.getAttribute('aria-label') === '预览 SKILL.md');
  assert.equal(await page.getByRole('dialog').count(), 0);
  report.checks.push('Late replies after switching and closing cannot overwrite content or reopen the dialog; closing returns keyboard focus to the file');

  await markdownTrigger.focus();
  await page.keyboard.press('Enter');
  await dialog.getByRole('heading', { name: '可复用的数据检查', exact: true }).waitFor();
  await dialog.screenshot({ path: join(output, 'skill-resource-markdown-390.png') });
  await page.keyboard.press('Escape');
  await dialog.waitFor({ state: 'hidden' });
  report.checks.push('Keyboard Enter opens a file and Escape dismisses its preview');

  await markdownTrigger.click();
  started = delayResource();
  await dialog.getByLabel('选择技能文件').selectOption('references/slow.json');
  await started;
  await page.evaluate(() => { window.harness.shared.value = true; });
  await dialog.waitFor({ state: 'hidden' });
  await releaseResource();
  assert.equal(await owner.getByRole('button').count(), 0);
  assert.doesNotMatch(await owner.innerText(), /可复用的数据检查|STALE-RESOURCE-CONTENT/);
  await page.evaluate(() => { window.harness.shared.value = false; });
  await owner.locator('summary').click();
  await page.waitForFunction(() => !document.querySelector('#owner .skill-files button').disabled);
  await markdownTrigger.click();
  started = delayResource();
  await dialog.getByLabel('选择技能文件').selectOption('references/slow.json');
  await started;
  await page.evaluate(id => { window.harness.replaceOwner(id); }, secondId);
  await dialog.waitFor({ state: 'hidden' });
  await releaseResource();
  assert.doesNotMatch(await owner.innerText(), /STALE-RESOURCE-CONTENT/);
  await page.waitForFunction(() => !document.querySelector('#owner .skill-files button').disabled);
  await markdownTrigger.click();
  await dialog.getByRole('heading', { name: '可复用的数据检查', exact: true }).waitFor();
  assert.ok(report.requests.some(item => item.action === 'resource' && item.id === secondId));
  started = delayResource();
  await dialog.getByLabel('选择技能文件').selectOption('references/slow.json');
  await started;
  await page.evaluate(() => { window.harness.mounted.value = false; });
  await dialog.waitFor({ state: 'hidden' });
  await releaseResource();
  assert.equal(await page.locator('#owner').count(), 0);
  report.checks.push('Share transitions, draft replacement and parent unmount clear the preview and ignore pending content; the new draft uses its own owner endpoint');

  assert.ok(report.requests.every(item => item.method === 'GET' && item.authorization === 'Bearer synthetic-preview-token' && item.id !== sharedId));
  assert.equal(await page.evaluate(() => window.__previewExecuted), false);
  assert.deepEqual(report.errors, []);
  assert.deepEqual(report.unexpectedRequests, []);
  assert.deepEqual(await sourceHashes(), beforeHashes, 'Production files changed during this browser run');
  await page.evaluate(() => window.harness.unmount());
  report.passed = true;
} catch (error) {
  report.failure = error.message; report.stack = error.stack;
  await page.screenshot({ path: join(output, 'failure.png'), fullPage: true }).catch(() => {});
  process.exitCode = 1;
} finally {
  pendingRelease?.();
  await context.close(); await browser.close();
  await writeFile(join(output, 'results.json'), JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report)); console.log(`BROWSER_REPORT=${output}`);
}
