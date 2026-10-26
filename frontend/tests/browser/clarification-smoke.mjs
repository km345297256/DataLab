/** Real clarification card: explicit choices, retry, read-only history, narrow layout. */
import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { compileScript, compileStyle, parse } from '@vue/compiler-sfc';
import { readFile, readdir, mkdtemp, writeFile } from 'node:fs/promises';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { tmpdir } from 'node:os';
import { launchBrowser } from './runtime.mjs';

const frontend = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const entry = `import {createApp,h,ref} from 'vue';
import Card from './src/components/AnalysisClarificationCard.vue';
const pending=ref(true),disabled=ref(false),answers=[];
const clarification={id:'synthetic-question',stage:'request',questions:[
 {id:'unit',question:'这项实验的独立样本单位是什么？',reason:'样本单位会影响样本量和统计检验，当前说明不足以确定。',options:['每个独立培养瓶','每条测量记录']},
 {id:'control',question:'哪个处理组应作为对照组？',reason:'不同对照组会改变效应的解释。',options:['低磷、常温组','以数据字典指定的组为准']}
]};
createApp({render:()=>h('main',{style:'max-width:800px;margin:auto;padding:16px'},[
 h(Card,{clarification,pending:pending.value,disabled:disabled.value,onAnswer:value=>{answers.push(value);disabled.value=true;}})
])}).mount('#app');
window.harness={pending,disabled,answers};window.ready=true;`;
const bundle = await build({
  stdin: { contents: entry, sourcefile: 'clarification-entry.js', resolveDir: frontend },
  bundle: true, write: false, format: 'esm', platform: 'browser', target: 'es2022', logLevel: 'warning',
  define: { '__VUE_OPTIONS_API__': 'true', '__VUE_PROD_DEVTOOLS__': 'false',
    '__VUE_PROD_HYDRATION_MISMATCH_DETAILS__': 'false', 'process.env.NODE_ENV': '"production"' },
  plugins: [{ name: 'real-clarification-sfc', setup(builder) {
    builder.onLoad({ filter: /\.vue$/ }, async ({ path }) => {
      const { descriptor } = parse(await readFile(path, 'utf8'), { filename: path });
      const id = 'clarification-smoke';
      const compiled = compileScript(descriptor, { id, inlineTemplate: true, genDefaultAs: '__component' });
      const css = descriptor.styles.map(style => compileStyle({ source: style.content,
        scoped: style.scoped, filename: path, id: `data-v-${id}` }).code).join('\n');
      return { contents: `${compiled.content}\n__component.__scopeId='data-v-${id}';document.head.appendChild(Object.assign(document.createElement('style'),{textContent:${JSON.stringify(css)}}));export default __component;`,
        loader: 'ts', resolveDir: dirname(path) };
    });
  } }],
});
const cssName = (await readdir(join(frontend, 'dist/assets'))).find(name => /^index-.*\.css$/.test(name));
assert.ok(cssName, 'Build the frontend before this browser check');
const css = await readFile(join(frontend, 'dist/assets', cssName));
const output = await mkdtemp(join(tmpdir(), 'datalab-clarification-'));
const report = { passed: false, errors: [], externalRequests: [], checks: [] };
const browser = await launchBrowser();
try {
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, serviceWorkers: 'block' });
  const page = await context.newPage();
  page.on('pageerror', error => report.errors.push(error.message));
  await context.route('**/*', route => {
    const url = new URL(route.request().url());
    if (url.origin !== 'http://localhost:7001') {
      report.externalRequests.push(url.href); return route.abort();
    }
    if (url.pathname === '/entry.js') return route.fulfill({ contentType: 'application/javascript', body: Buffer.from(bundle.outputFiles[0].contents) });
    if (url.pathname === '/style.css') return route.fulfill({ contentType: 'text/css', body: css });
    return route.fulfill({ contentType: 'text/html', body: '<!doctype html><html lang="zh"><head><meta charset="utf-8"><link rel="stylesheet" href="/style.css"><style>html,body{margin:0;min-height:100%;font-family:Arial,sans-serif}*{box-sizing:border-box}</style></head><body><div id="app"></div><script type="module" src="/entry.js"></script></body></html>' });
  });
  await page.goto('http://localhost:7001/clarification');
  await page.waitForFunction(() => window.ready);
  const submit = page.getByRole('button', { name: '确认并继续' });
  assert.equal(await page.getByRole('radio', { checked: true }).count(), 0);
  assert.equal(await submit.isDisabled(), true);
  report.checks.push('no preselected answers or automatic execution');
  await page.getByRole('radio', { name: '每个独立培养瓶', exact: true }).click();
  assert.equal(await submit.isDisabled(), true);
  await page.getByRole('textbox', { name: '回答：哪个处理组应作为对照组？' }).fill('低磷、常温组；请保留批次效应。');
  assert.equal(await submit.isEnabled(), true);
  assert.equal(await page.evaluate(() => window.harness.answers.length), 0);
  await page.screenshot({ path: join(output, 'desktop.png'), fullPage: true });
  await submit.click();
  assert.equal(await page.evaluate(() => window.harness.answers.length), 1);
  assert.equal(await page.getByRole('button', { name: '正在提交…' }).isDisabled(), true);
  const answer = await page.evaluate(() => window.harness.answers[0]);
  assert.equal(answer.clarificationId, 'synthetic-question');
  assert.match(answer.message, /每个独立培养瓶/);
  assert.match(answer.message, /保留批次效应/);
  report.checks.push('explicit confirmation binds complete answers to the question');
  await page.evaluate(() => { window.harness.disabled.value = false; });
  await page.getByRole('button', { name: '确认并继续' }).waitFor();
  assert.equal(await page.getByRole('textbox', { name: '回答：哪个处理组应作为对照组？' }).inputValue(), '低磷、常温组；请保留批次效应。');
  report.checks.push('failed submission can retain and retry the answer');
  await page.setViewportSize({ width: 375, height: 900 });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  await page.screenshot({ path: join(output, 'narrow.png'), fullPage: true });
  report.checks.push('narrow layout has no horizontal overflow');
  await page.evaluate(() => { window.harness.pending.value = false; });
  await page.getByRole('heading', { name: '确认问题记录' }).waitFor();
  assert.equal(await page.getByRole('button', { name: '确认并继续' }).count(), 0);
  assert.equal(await page.getByRole('textbox').count(), 0);
  report.checks.push('historical questions are read-only');
  assert.deepEqual(report.errors, []);
  assert.deepEqual(report.externalRequests, []);
  report.passed = true;
} finally {
  await browser.close();
  await writeFile(join(output, 'report.json'), JSON.stringify(report, null, 2));
  console.log(JSON.stringify({ output, ...report }));
}
