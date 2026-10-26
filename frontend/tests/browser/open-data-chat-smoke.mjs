/** Real Vue SFCs + browser actions, deterministic mock API, no remote requests. */
import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { compileScript, compileStyle, parse, registerTS } from '@vue/compiler-sfc';
import ts from 'typescript';
import { createHash } from 'node:crypto';
import { existsSync, readFileSync } from 'node:fs';
import { mkdtemp, readFile, readdir, writeFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { homedir, tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

import { browserEngine, launchBrowser } from './runtime.mjs';

const frontend = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
registerTS(() => ts);
const entry = `import {createApp,h,ref,watch,onUnmounted} from 'vue';
import {createI18n} from 'vue-i18n';
import {createRouter,createMemoryHistory} from 'vue-router';
import Chat from './src/components/OpenDataChat.vue';
import Results from './src/components/OpenDataResults.vue';
import Composer from './src/components/ChatBox.vue';
import DataAcquisitionPanel from './src/components/DataAcquisitionPanel.vue';
import {useDataAcquisition} from './src/composables/useDataAcquisition';
import {useAcquisitionPanelInterop} from './src/composables/useAcquisitionPanelInterop';
import {useFilePanel} from './src/composables/useFilePanel';
import {EVENT_SHOW_FILE_PANEL,EVENT_SHOW_TOOL_PANEL,EVENT_HIDE_FILE_PANEL,EVENT_HIDE_TOOL_PANEL} from './src/constants/event';
import {eventBus} from './src/utils/eventBus';
const filePanel=useFilePanel();
const acquisition=useDataAcquisition(),opened=ref(0),occupied=ref('');
const sid=ref('session'),text=ref(''),mode=ref(false),parent=ref(),files=ref([]),chat=ref(),shared=ref(false),running=ref(false),connectors=ref([]);
window.harness={filePanel,sid,text,mode,parent,files,chat,shared,running,acquisition,opened,occupied,notify:update=>eventBus.emit('open-data-updated',update),showExternal:kind=>{occupied.value=kind;eventBus.emit(kind==='preview'?EVENT_SHOW_FILE_PANEL:EVENT_SHOW_TOOL_PANEL)},closeExternal:()=>{const kind=occupied.value;occupied.value='';if(kind==='preview')filePanel.hideFilePanel();else eventBus.emit(EVENT_HIDE_TOOL_PANEL)}};
eventBus.on('open-data-panel-show',()=>{opened.value++;occupied.value=''});
eventBus.on(EVENT_SHOW_FILE_PANEL,()=>{occupied.value='preview'});
eventBus.on(EVENT_SHOW_TOOL_PANEL,()=>{occupied.value='tool'});
eventBus.on('open-data-refine',target=>{mode.value=true;parent.value=target;text.value=target.query});
eventBus.on('connector-analysis-draft',draft=>{if(draft.sessionId!==sid.value)return;files.value=draft.files;text.value='请检查文件是否适用于：'+draft.query;mode.value=false;draft.onAccepted?.(true)});
const submit=async()=>{if(!mode.value)return;const query=text.value;if(await chat.value.start(query,parent.value?.searchId,connectors.value)){text.value='';mode.value=false;parent.value=undefined}};
const app=createApp({setup(){useAcquisitionPanelInterop();watch([sid,shared],()=>{files.value=[];void acquisition.activate(shared.value?undefined:sid.value)},{immediate:true});onUnmounted(()=>void acquisition.activate());},render:()=>h('div',{class:'fixture-workspace'},[h('main',{class:'fixture-chat'},[
 h('p',{class:'fixture-label'},'交互回归 · 以下为合成数据，非真实检索结果'),
 // Fixture-only queue entry: production uses TaskDataPanel, covered by analysis-lifecycle-smoke.
 h('div',{class:'fixture-toolbar'},[h('button',{onClick:()=>mode.value=true},'查找开放数据'),h('button',{type:'button',class:'acquisition-entry','data-testid':'fixture-acquisition-entry','aria-label':'测试夹具：查看获取队列','aria-controls':'data-acquisition-panel','aria-expanded':acquisition.panelOpen.value,onClick:()=>acquisition.open()},'测试夹具：查看获取队列')]),
 shared.value?h(Results,{sessionId:sid.value,searchId:'s1',isShare:true}):h(Chat,{ref:chat,sessionId:sid.value,messages:[]},{default:({entries})=>entries.map(e=>e.kind==='search'?h(Results,{key:e.search.id,sessionId:sid.value,searchId:e.search.id,initialSearch:e.search}):null)}),
 h(Composer,{modelValue:text.value,'onUpdate:modelValue':v=>text.value=v,dataDiscovery:mode.value,'onUpdate:dataDiscovery':v=>mode.value=v,discoveryTarget:parent.value?.query,showConnectorActions:true,selectedConnectors:connectors.value,'onUpdate:selectedConnectors':value=>connectors.value=value,showFileActions:false,showMcpActions:false,showLibraryActions:false,rows:2,isRunning:running.value,attachments:files.value,selectedSkills:[],onSubmit:submit})
]),h(DataAcquisitionPanel),occupied.value?h('aside',{class:'fixture-external',role:'complementary','aria-label':occupied.value==='preview'?'文件预览':'工具输出'},occupied.value==='preview'?'文件预览占用右侧区域':'工具输出占用右侧区域'):null])});app.use(createI18n({legacy:false,locale:'en',messages:{en:{}},missingWarn:false,fallbackWarn:false}));app.use(createRouter({history:createMemoryHistory(),routes:[{path:'/',component:{render:()=>null}},{path:'/my-data',component:{render:()=>null}}]}));app.mount('#app');window.unmountHarness=()=>app.unmount();window.harnessReady=true;`;
const bundle = await build({
  stdin: { contents: entry, sourcefile: 'open-data-entry.js', resolveDir: frontend }, bundle: true, write: false,
  format: 'esm', platform: 'browser', target: 'es2022', logLevel: 'warning',
  alias: { '@': resolve(frontend, 'src') },
  define: { 'import.meta.env': JSON.stringify({ BASE_URL: '/', MODE: 'test', DEV: false, PROD: true }),
    '__VUE_OPTIONS_API__': 'true', '__VUE_PROD_DEVTOOLS__': 'false', '__VUE_PROD_HYDRATION_MISMATCH_DETAILS__': 'false', 'process.env.NODE_ENV': '"production"' },
  plugins: [{ name: 'real-open-data-sfc', setup(builder) {
    builder.onLoad({ filter: /\.vue$/ }, async ({ path }) => {
      let source = await readFile(path, 'utf8');
      if (!source.includes('<script')) source += '\n<script setup>const __marker = true;</script>';
      const id = createHash('sha256').update(path).digest('hex').slice(0, 12);
      const { descriptor, errors } = parse(source, { filename: path });
      if (errors.length) throw errors[0];
      const compiled = compileScript(descriptor, { id, inlineTemplate: true, genDefaultAs: '__component', fs: { fileExists: existsSync, readFile: path => readFileSync(path, 'utf8') } });
      const styles = descriptor.styles.map(style => compileStyle({ filename: path, source: style.content, scoped: style.scoped, id: `data-v-${id}` }).code).join('\n');
      return { contents: `${compiled.content}\n__component.__scopeId='data-v-${id}';document.head.appendChild(Object.assign(document.createElement('style'),{textContent:${JSON.stringify(styles)}}));export default __component;`, loader: 'ts', resolveDir: dirname(path) };
    });
  } }],
});
assert.equal(bundle.outputFiles.length, 1);
const cssName = (await readdir(resolve(frontend, 'dist/assets'))).find(name => /^index-.*\.css$/.test(name));
assert.ok(cssName, 'Run npm run build before browser verification');
const css = await readFile(resolve(frontend, 'dist/assets', cssName));
const output = await mkdtemp(join(tmpdir(), 'datalab-open-data-'));
const report = { browserEngine, passed: false, errors: [], externalAttempts: [], unexpectedRequests: [], actions: [], checks: {} };
const chrome = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const browser = await launchBrowser();
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
const page = await context.newPage(); page.setDefaultTimeout(10000);
page.on('pageerror', error => report.errors.push(error.message));
const records = [], jobs = [];
let fetchFailure = false, tickJobs = false, holdJobReads = false;
const pendingJobReads = [], reads = [];
let bulkResolutionMode='complete';
const pendingBulkResolutions=[];
let nextSearch = 0, nextJob = 0;
const file = (id, filename) => ({ file_id: id, filename, size: 120, upload_date: '2026-10-20T01:00:00Z', content_type: 'text/csv' });
const candidate = { id: 'city', source_id: 'catalog', external_id: 'synthetic-city', title: '城市空气质量与交通观测（合成示例）', description: '用于界面回归的合成数据。数据包含观测值与字段说明。', source_url: 'https://example.org/synthetic', access: 'downloadable', availability_verified_at: '2026-10-21T00:00:00Z', available_file_count: 3, formats: ['CSV'], size: 2_070_000_000_000, license: null, coverage: {}, match_notes: ['检索词命中标题，尚未检查数据内容'], limitations: ['观测仅覆盖样例城市'], system_notes: ['获取前会再次检查文件状态', '获取前会再次检查文件状态'] };
const lead = { ...candidate, id: 'lead', title: '城市人口年度统计（仅线索）', access: 'lead', source_url: 'javascript:alert(1)' };
const restricted = { ...candidate, id: 'restricted', title: '需申请的受限数据', access: 'restricted' };
const unavailable = { ...candidate, id: 'zero', title: '没有可获取文件的数据', available_file_count: 0 };
const legacy = { ...candidate, id: 'legacy', title: '尚未核验的旧记录', availability_verified_at: null };
const verificationMessage = '已确认当前可读';
const cityOptions = [
  { id: 'air', filename: 'air.csv', content_type: 'text/csv', size: 2_000_000_000_000, available: true, required_file_ids: ['metadata'], reason: verificationMessage },
  { id: 'metadata', filename: 'metadata.csv', content_type: 'text/csv', size: null, available: true, reason: verificationMessage },
  { id: 'large', filename: 'large.csv', content_type: 'text/csv', size: 70_000_000_000, available: true, reason: verificationMessage },
];
const bulkOptions = Array.from({length:24},(_,index)=>({id:`part-${index+1}`,filename:`observations-${String(index+1).padStart(2,'0')}.csv`,content_type:'text/csv',size:index===23?null:1000,available:true,reason:verificationMessage}));
const bulkCandidate = {...candidate,id:'bulk',title:'连续 24 份观测文件（合成示例）',available_file_count:24,size:null};
const makeSearch = (id, body, sessionId = 'session') => ({ id, session_id: sessionId, query: body.query, parent_id: body.parent_id, status: 'partial', sources: [{ id: 'catalog', title: '合成目录', status: 'ready', count: 5 }, { id: 'offline', title: '不可用来源', status: 'failed', count: 0, error: '临时不可用' }], results: [candidate, lead, restricted, unavailable, legacy], has_more: true, created_at: new Date(Date.now() + records.length).toISOString(), effective_query: 'urban air traffic', notes: ['仅用于界面测试'] });
await context.route('**/*', async route => {
  const request = route.request(), url = new URL(request.url()), method = request.method();
  const ok = data => route.fulfill({ contentType: 'application/json', body: JSON.stringify({ code: 0, msg: 'success', data }) });
  if (url.origin !== 'http://localhost:7001') { report.externalAttempts.push(url.href); return route.abort(); }
  if (url.pathname === '/__open_data__/') return route.fulfill({ contentType: 'text/html', body: '<!doctype html><html><head><meta charset="utf-8"><link rel="stylesheet" href="/__open_data__/project.css"><style>html,body,#app{margin:0;min-height:100%;font-family:Arial,sans-serif}.fixture-workspace{height:100vh;display:flex;overflow:hidden}.fixture-chat{min-width:0;flex:1;overflow-y:auto;padding:24px 16px}.fixture-chat>section,.fixture-chat>p,.fixture-chat>button{max-width:800px;margin-left:auto;margin-right:auto}.fixture-toolbar{display:flex;align-items:center;justify-content:space-between;gap:16px;margin-bottom:18px}.fixture-external{flex:0 0 40%;padding:24px;background:#eef2ee}.fixture-label{font-size:12px;color:#68736d;margin-bottom:16px}*{box-sizing:border-box}</style></head><body><div id="app"></div><script type="module" src="/__open_data__/entry.js"></script></body></html>' });
  if (url.pathname === '/__open_data__/entry.js') return route.fulfill({ contentType: 'application/javascript', body: Buffer.from(bundle.outputFiles[0].contents) });
  if (url.pathname === '/__open_data__/project.css') return route.fulfill({ contentType: 'text/css', body: css });
  if (url.pathname === '/api/v1/files/air-data/signed-url') return ok({ signed_url: '/__open_data__/download/air.csv' });
  if (url.pathname === '/__open_data__/download/air.csv') return route.fulfill({ contentType: 'text/csv', headers: { 'Content-Disposition': 'attachment; filename="air.csv"' }, body: 'city,value\nSynthetic,1\n' });
  if (/^\/images\/connectors\/[a-z0-9-]+\.(svg|png|ico|jpg)$/.test(url.pathname)) {
    const image = resolve(frontend, 'public', url.pathname.slice(1));
    return existsSync(image)
      ? route.fulfill({ body: await readFile(image), contentType: url.pathname.endsWith('.svg') ? 'image/svg+xml' : url.pathname.endsWith('.png') ? 'image/png' : url.pathname.endsWith('.jpg') ? 'image/jpeg' : 'image/x-icon' })
      : route.fulfill({ status: 404, body: '' });
  }
  if (url.pathname === '/api/v1/data-connectors') return ok([{ id: 'ncbi-geo', title: 'NCBI GEO', description: '合成目录连接器选项', operations: [], limitations: [] }]);
  if (/^\/api\/v1\/skills/.test(url.pathname)) return ok(url.pathname.endsWith('/preferences') ? { auto_enabled_skills: [] } : []);
  const match = url.pathname.match(/^\/api\/v1\/sessions\/([^/]+)(.*)$/);
  const sessionId = match?.[1], path = match?.[2] || url.pathname;
  if (method === 'GET' && sessionId) reads.push({sessionId,path,searchId:url.searchParams.get('search_id')});
  if (method === 'GET' && path === '/data-searches') return ok(records.filter(item=>item.session_id===sessionId));
  if (method === 'POST' && path === '/data-searches') { const body = request.postDataJSON(); report.actions.push({ kind: 'search', body }); const record = makeSearch(`s${++nextSearch}`, body, sessionId); records.push(record); return ok(record); }
  const searchMatch = path.match(/^\/data-searches\/([^/]+)$/);
  if (method === 'GET' && searchMatch) {
    if (fetchFailure) return route.fulfill({ status: 503, contentType: 'application/json', body: '{"code":503,"msg":"offline","data":null}' });
    return ok(records.find(item => item.id === searchMatch[1] && item.session_id === sessionId));
  }
  if (method === 'GET' && path === '/acquisitions') {
    if (tickJobs) for (const job of jobs) if (job.status === 'downloading') { job.status = 'partial'; job.files = [file('air-data', '12345678_air.csv')]; job.asset_id = 'saved-asset'; job.error = '字段说明获取失败，可重试失败项'; Object.assign(job.items[0], { status: 'ready', bytes_received: 120, file: job.files[0], message: '已下载并完成文件校验；分析适用性尚待确认' }); Object.assign(job.items[1], { status: 'failed', message: '字段说明获取失败' }); }
    const found = jobs.filter(item => item.session_id===sessionId && (!url.searchParams.has('search_id') || item.search_id === url.searchParams.get('search_id')));
    if (holdJobReads && sessionId === 'session') { const snapshot=structuredClone(found); await new Promise(resolve=>pendingJobReads.push(async()=>{try{await ok(snapshot)}catch{}resolve()})); return; }
    return ok(found);
  }
  const resolution = path.match(/^\/data-searches\/([^/]+)\/candidates\/(city|bulk)\/resolve$/);
  if (method === 'POST' && resolution) {
    const bulk=resolution[2]==='bulk';
    if(bulk&&bulkResolutionMode==='failure')return route.fulfill({status:503,contentType:'application/json',body:'{"code":503,"msg":"temporary file lookup failure","data":null}'});
    const payload={files:bulk?(bulkResolutionMode==='partial'?bulkOptions.slice(0,1):bulkOptions):cityOptions,
      verification_pending_count:bulk&&bulkResolutionMode==='partial'?23:0,unavailable_file_count:0,
      limits:{max_file_bytes:null,max_total_bytes:null,max_files:null}};
    if(bulk&&bulkResolutionMode==='held'){
      await new Promise(resolve=>pendingBulkResolutions.push(async()=>{try{await ok(payload)}catch{}resolve()}));return;
    }
    return ok(payload);
  }
  const acquisition = path.match(/^\/data-searches\/([^/]+)\/acquisitions$/);
  if (method === 'POST' && acquisition) { const body = request.postDataJSON(); report.actions.push({ kind: 'acquire', search: acquisition[1], body }); const options=body.candidate_id==='bulk'?bulkOptions:cityOptions; const job = { id: `j${++nextJob}`, search_id: acquisition[1], session_id: sessionId, candidate_id: body.candidate_id, status: 'downloading', files: [], items: body.file_ids.map((id, index) => {const option=options.find(option=>option.id===id);return { id, filename: option.filename, status: 'downloading', bytes_received: body.candidate_id==='bulk'?0:index ? 1_000_000_000 : 50_000_000_000, size: option.size };}) }; jobs.push(job); return ok(job); }
  const operation = path.match(/^\/acquisitions\/([^/]+)\/(retry|cancel)$/);
  if (method === 'POST' && operation) { const job = jobs.find(item => item.id === operation[1]); report.actions.push({ kind: operation[2], job: job.id }); if (operation[2] === 'retry') { job.status = 'ready'; job.error = null; job.files = [file('air-data', '12345678_air.csv'), file('metadata-data', '87654321_metadata.csv')]; job.items.forEach((item, index) => Object.assign(item, { status: 'ready', file: job.files[index], message: '已下载并完成文件校验；分析适用性尚待确认' })); } else job.status = 'cancelled'; return ok(job); }
  const more = path.match(/^\/data-searches\/([^/]+)\/continue$/);
  if (method === 'POST' && more) { report.actions.push({ kind: 'more', search: more[1] }); const record = records.find(item => item.id === more[1]); record.has_more = false; return ok(record); }
  report.unexpectedRequests.push(`${method} ${url.pathname}`); return route.abort();
});
try {
  await page.goto('http://localhost:7001/__open_data__/', { waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => window.harnessReady);
  const input = page.locator('textarea');
  const panel = page.getByTestId('data-acquisition-panel');
  const searchCards = page.getByRole('region', { name: '开放数据查找', exact: true });
  const jobCard = id => panel.locator(`[data-acquisition-job-id="${id}"]`);
  const receipt = index => searchCards.nth(index).locator('[aria-label="数据获取状态"]');
  const reopen = async index => { await receipt(index).getByRole('button').click(); await panel.waitFor(); };
  const close = async () => { await panel.getByRole('button', { name: '收起数据获取', exact: true }).click(); await panel.waitFor({state:'detached'}); };
  const prepare = async index => {
    const card = searchCards.nth(index);
    await card.getByText('可获取 · 3 个文件',{exact:true}).waitFor();
    await card.getByRole('button', { name: '获取数据', exact: true }).click();
    await card.getByText('可获取 3 个文件 · 已选 0 个',{exact:true}).waitFor();
    assert.equal(await card.getByRole('checkbox').count(),3);
    assert.equal(await card.getByText(/16 个文件|本批最多|每批最多/).count(),0);
    await card.locator('.open-data-facts').getByText('已知 2.1 TB，另有 1 个文件大小未知',{exact:true}).waitFor();
    assert.equal(await card.getByText(verificationMessage,{exact:true}).evaluateAll(nodes=>nodes.length===3&&nodes.every(node=>node.classList.contains('open-data-note')&&!node.classList.contains('open-data-warning'))),true);
    await card.getByRole('checkbox', { name: '选择 air.csv', exact: true }).check();
    await card.getByText('请同时选择这份数据需要的配套文件。', { exact: true }).waitFor();
    assert.equal(await card.getByRole('button', { name: '获取数据（1 个文件）' }).isDisabled(), true);
    await card.getByRole('checkbox', { name: '选择 metadata.csv', exact: true }).check();
    await card.getByRole('button', { name: '获取数据（2 个文件）', exact: true }).click();
  };
  const dimensions = async () => page.evaluate(() => ({viewport:innerWidth,width:document.documentElement.scrollWidth,
    panelWidth:document.querySelector('[data-testid="data-acquisition-panel"]')?.getBoundingClientRect().width,
    contents:[...document.querySelectorAll('.acquisition-content,.acquisition-job')].filter(node=>node.getClientRects().length).map(node=>({width:node.clientWidth,scrollWidth:node.scrollWidth}))}));
  const noOverflow = async label => { const d=await dimensions();assert.ok(d.width<=d.viewport,`${label} document overflow: ${JSON.stringify(d)}`);assert.ok(d.contents.every(node=>node.scrollWidth<=node.width+1),`${label} content overflow: ${JSON.stringify(d)}`);report.checks[label]=d; };

  assert.equal(await panel.count(), 0);
  await page.getByRole('button', { name: '连接器', exact: true }).click();
  await page.getByRole('dialog').getByRole('checkbox').check();
  await page.getByRole('button', { name: '完成', exact: true }).click();
  await page.getByRole('button', { name: '查找开放数据', exact: true }).click();
  await page.getByText(/按所选 1 个来源查找/).waitFor();
  const originalQuery = '我想分析城市空气质量与交通的关系，寻找可用数据';
  await input.fill(originalQuery);
  await page.getByRole('button', { name: 'Send message', exact: true }).click();
  await searchCards.first().getByRole('heading', { name: candidate.title, exact: true }).waitFor();
  for (const hidden of [lead, restricted, unavailable, legacy]) assert.equal(await page.getByRole('heading', { name: hidden.title, exact: true }).count(), 0);
  assert.equal(report.actions[0].body.query, originalQuery);
  assert.deepEqual(report.actions[0].body.source_ids, ['ncbi-geo']);
  assert.equal(await page.getByText('系统获取说明', { exact: true }).count(), 0);
  assert.equal(await page.getByText('获取前会再次检查文件状态', { exact: true }).count(), 0);
  const sourceNotes = searchCards.first().locator('details.open-data-source-notes');
  assert.equal(await sourceNotes.getByText('观测仅覆盖样例城市', { exact: true }).isVisible(), false);
  await sourceNotes.locator('summary').click();
  await sourceNotes.getByText('观测仅覆盖样例城市', { exact: true }).waitFor();
  await sourceNotes.getByText('许可：来源未声明', { exact: true }).waitFor();
  await sourceNotes.locator('summary').click();
  assert.equal(await page.locator('a[href^="javascript:"]').count(), 0);
  report.checks.verifiedOnly = 'unverified, restricted, empty and metadata leads hidden; source notes attributed separately; removed system block stays absent';

  await prepare(0);
  await panel.waitFor();
  assert.equal(await panel.getAttribute('role'), 'complementary');
  assert.equal(await page.evaluate(() => window.harness.opened.value), 1);
  assert.equal(await searchCards.locator('.open-data-job,[data-acquisition-job-id]').count(), 0);
  await jobCard('j1').getByText('正在获取', { exact: true }).waitFor();
  await jobCard('j1').getByText(/50.0 GB \/ 2.0 TB/).waitFor();
  await jobCard('j1').getByText(/1.0 GB · 总大小未知/).waitFor();
  for (const message of ['正在校验文件', '正在保存文件']) {
    Object.assign(jobs[0].items[0], {bytes_received:jobs[0].items[0].size,message});
    await page.evaluate(() => window.harness.notify({sessionId:'session',searchId:'s1'}));
    await jobCard('j1').getByText(message,{exact:true}).waitFor();
    assert.equal(await jobCard('j1').getByText(message,{exact:true}).evaluate(node=>node.classList.contains('acquisition-note')&&!node.classList.contains('acquisition-warning')),true);
  }
  await noOverflow('desktopDownloading');
  await page.screenshot({path:join(output,'desktop-fetching.png')});
  await close();
  assert.equal(report.actions.some(action=>action.kind==='cancel'),false);
  await page.evaluate(()=>{window.harness.running.value=true});
  await input.fill('下载时仍可继续编辑');
  tickJobs = true;
  await page.evaluate(()=>window.harness.notify({sessionId:'session',searchId:'s1'}));
  await page.waitForFunction(()=>window.harness.acquisition.jobs.value[0]?.status==='partial');
  assert.equal(await panel.count(),0);
  assert.equal(await page.evaluate(()=>window.harness.opened.value),1);
  assert.equal(await input.isEnabled(),true);
  assert.equal(await input.inputValue(),'下载时仍可继续编辑');
  report.checks.autoOpen = 'first acquisition opens once; explicit close preserves background progress and never reopens on completion';

  await reopen(0);
  assert.equal(await page.evaluate(()=>window.harness.acquisition.focusedJobId.value),'j1');
  await jobCard('j1').getByText('字段说明获取失败',{exact:true}).waitFor();
  assert.equal(await jobCard('j1').getByText('字段说明获取失败',{exact:true}).evaluate(node=>node.classList.contains('acquisition-warning')),true);
  await jobCard('j1').getByRole('button',{name:'重试失败项',exact:true}).click();
  await page.waitForFunction(()=>window.harness.acquisition.jobs.value.find(job=>job.id==='j1')?.status==='ready');
  // A completed section is collapsed until an explicit summary request focuses it.
  await close();
  await reopen(0);
  await jobCard('j1').getByText('获取完成',{exact:true}).waitFor();
  assert.equal(await jobCard('j1').locator('.acquisition-item-message').count(),0);
  const downloadPromise=page.waitForEvent('download');
  await jobCard('j1').getByRole('button',{name:'下载 12345678_air.csv 到本地',exact:true}).click();
  assert.equal((await downloadPromise).suggestedFilename(),'12345678_air.csv');
  await jobCard('j1').getByRole('button',{name:'加入分析输入',exact:true}).click();
  await jobCard('j1').getByText('已加入输入框并保留原问题，补充要求后发送即可。',{exact:true}).waitFor();
  assert.match(await input.inputValue(),/城市空气质量与交通/);
  assert.deepEqual(await page.evaluate(()=>window.harness.files.value.map(file=>file.file_id)),['air-data','metadata-data']);
  assert.equal(await page.evaluate(()=>window.harness.running.value),true);
  report.checks.handoff = 'partial retry preserves successes; confirmed download and next-turn input retain the original question without replacing active analysis';

  await jobCard('j1').getByRole('button',{name:'预览 12345678_air.csv',exact:true}).click();
  assert.equal(await page.evaluate(()=>window.harness.filePanel.fileInfo.value.file_id),'air-data');
  await page.getByRole('complementary',{name:'文件预览',exact:true}).waitFor();
  assert.equal(await panel.count(),0);
  await searchCards.first().getByRole('button',{name:'在输入框细化要求',exact:true}).click();
  await input.fill('限定 2020—2024 年，按城市和月份汇总');
  await page.getByRole('button',{name:'Send message',exact:true}).click();
  await searchCards.nth(1).getByRole('heading',{name:candidate.title,exact:true}).waitFor();
  assert.equal(records[1].parent_id,'s1');
  tickJobs=false;
  const openedBeforeSecond=await page.evaluate(()=>window.harness.opened.value);
  await prepare(1);
  await page.waitForFunction(()=>window.harness.acquisition.jobs.value.length===2);
  assert.equal(await panel.count(),0);
  assert.equal(await page.evaluate(()=>window.harness.opened.value),openedBeforeSecond);
  assert.equal(await page.getByRole('complementary',{name:'文件预览',exact:true}).isVisible(),true);
  await page.evaluate(()=>window.harness.closeExternal());
  assert.equal(await panel.count(),0);
  await reopen(1);
  assert.equal(await page.evaluate(()=>window.harness.acquisition.focusedSearchId.value),'s2');
  assert.equal(await panel.locator('[data-acquisition-job-id]').count(),2);
  await jobCard('j2').getByRole('button',{name:'取消获取',exact:true}).click();
  await jobCard('j2').getByText('已取消',{exact:true}).waitFor();
  assert.equal(jobs[0].status,'ready');
  const beforeRetry=jobs.length;
  await jobCard('j2').getByRole('button',{name:'重新获取',exact:true}).click();
  await page.waitForFunction(()=>window.harness.acquisition.jobs.value.find(job=>job.id==='j2')?.status==='ready');
  assert.equal(jobs.length,beforeRetry);
  assert.deepEqual(report.actions.at(-1),{kind:'retry',job:'j2'});
  await page.evaluate(()=>window.harness.showExternal('tool'));
  await page.getByRole('complementary',{name:'工具输出',exact:true}).waitFor();
  await page.evaluate(()=>window.harness.notify({sessionId:'session',searchId:'s2'}));
  assert.equal(await panel.count(),0);
  await page.evaluate(()=>window.harness.closeExternal());
  await reopen(1);
  report.checks.mergedQueue = 'one conversation queue combines searches; summary targets exact job; preview/tool occupancy wins; cancel/retry affects only selected job';

  // Restore old jobs even when their former candidates are hidden by new verification rules.
  records[0].results=[];
  records[0].notes=['已隐藏旧版尚未核验文件可获取性的记录；历史获取任务及已保存文件仍保留。'];
  await page.reload({waitUntil:'domcontentloaded'});
  await page.waitForFunction(()=>window.harnessReady&&window.harness.acquisition.jobs.value.length===2);
  assert.equal(await panel.count(),0);
  assert.equal(await searchCards.locator('[data-acquisition-job-id]').count(),0);
  await reopen(0);
  await jobCard('j1').getByRole('heading',{name:originalQuery,exact:true}).waitFor();
  const restoredDownload=page.waitForEvent('download');
  await jobCard('j1').getByRole('button',{name:'下载 12345678_air.csv 到本地',exact:true}).click();
  assert.equal((await restoredDownload).suggestedFilename(),'12345678_air.csv');
  await jobCard('j1').getByRole('button',{name:'加入分析输入',exact:true}).click();
  assert.match(await input.inputValue(),/城市空气质量与交通/);
  report.checks.reload = 'refresh restores session-wide jobs with no inline cards; old acquired files remain downloadable and usable without visible candidates';
  await noOverflow('desktopRestored');
  await page.screenshot({path:join(output,'desktop.png')});

  await page.setViewportSize({width:390,height:844});
  await page.waitForFunction(()=>document.querySelector('[data-testid="data-acquisition-panel"]')?.getAttribute('role')==='dialog');
  assert.equal(await panel.getAttribute('aria-modal'),'true');
  await noOverflow('mobileDrawer');
  const footer=await panel.locator('.acquisition-panel-footer').boundingBox(),bounds=await panel.boundingBox();
  assert.ok(Math.abs((footer.y+footer.height)-(bounds.y+bounds.height))<=2,'mobile panel footer must remain at viewport bottom');
  await page.screenshot({path:join(output,'mobile.png')});
  await jobCard('j1').getByRole('button',{name:'加入分析输入',exact:true}).click();
  await panel.waitFor({state:'detached'});
  await page.waitForFunction(()=>document.activeElement?.matches('textarea.chat-message-input'));
  assert.match(await input.inputValue(),/城市空气质量与交通/);
  assert.deepEqual(await page.evaluate(()=>window.harness.files.value.map(file=>file.file_id)),['air-data','metadata-data']);
  await page.screenshot({path:join(output,'mobile-joined.png')});
  report.checks.mobileJoin='successful file handoff closes compact drawer and focuses composer with selected files and original question intact';
  await reopen(0);
  await page.setViewportSize({width:390,height:600});
  const scrolling=await panel.locator('.acquisition-content').evaluate(node=>({client:node.clientHeight,scroll:node.scrollHeight,overflow:getComputedStyle(node).overflowY}));
  assert.ok(scrolling.scroll>scrolling.client&&['auto','scroll'].includes(scrolling.overflow),'long acquisition queue must scroll inside the drawer');
  report.checks.mobileScrolling=scrolling;
  await page.setViewportSize({width:390,height:844});
  await panel.getByRole('button',{name:'收起数据获取',exact:true}).focus();
  await page.keyboard.press('Shift+Tab');
  assert.equal(await page.evaluate(()=>Boolean(document.activeElement?.closest('[data-testid="data-acquisition-panel"]'))),true);
  await page.keyboard.press('Escape');
  await panel.waitFor({state:'detached'});
  await input.fill('抽屉收起后继续输入分析要求');
  assert.equal(await input.inputValue(),'抽屉收起后继续输入分析要求');
  await noOverflow('mobileCollapsed');
  await page.screenshot({path:join(output,'mobile-collapsed.png')});
  report.checks.mobileFocus = 'compact full-screen dialog traps focus; Escape closes without cancellation and returns to usable composer';

  // Hold an old private response across a conversation switch, then release it.
  holdJobReads=true;
  const oldRequest=page.waitForRequest(request=>request.method()==='GET'&&request.url().includes('/sessions/session/acquisitions'));
  await page.evaluate(()=>window.harness.notify({sessionId:'session',searchId:'s1'}));
  await oldRequest;
  await page.evaluate(()=>{window.harness.sid.value='other-session'});
  await page.waitForFunction(()=>window.harness.acquisition.sessionId.value==='other-session'&&!window.harness.acquisition.loading.value);
  holdJobReads=false;
  for(const release of pendingJobReads.splice(0))await release();
  await page.getByTestId('fixture-acquisition-entry').click();
  await panel.getByText('还没有获取任务。在聊天中找到数据后，点击“获取数据”。',{exact:true}).waitFor();
  assert.equal(await panel.locator('[data-acquisition-job-id]').count(),0);
  assert.equal(await page.getByText('12345678_air.csv',{exact:true}).count(),0);
  await close();
  // A preview transition may remove the old trigger before opening this panel.
  // Restoring body/html is not a usable keyboard destination: use this fixture's queue entry.
  await page.evaluate(()=>{
    const transient=document.createElement('button');
    transient.textContent='Temporary preview return trigger';
    document.body.appendChild(transient);transient.focus();transient.remove();
    if(document.activeElement!==document.body)throw new Error('Fixture must start with body focus');
    window.harness.acquisition.open();
  });
  await panel.waitFor();
  await close();
  await page.waitForFunction(()=>document.activeElement?.matches('[data-testid="fixture-acquisition-entry"]'));
  report.checks.focusFallback='removed preview trigger leaves body focused; closing acquisition panel restores the fixture queue entry';
  await page.evaluate(()=>{window.harness.sid.value='session'});
  await page.waitForFunction(()=>window.harness.acquisition.jobs.value.length===2);
  await page.evaluate(()=>{window.harness.shared.value=true});
  await page.waitForFunction(()=>!window.harness.acquisition.sessionId.value&&window.harness.acquisition.jobs.value.length===0);
  await page.getByText('开放数据获取记录仅对会话所有者可见。',{exact:true}).waitFor();
  assert.equal(await panel.count(),0);
  assert.equal(await page.getByRole('heading',{name:candidate.title,exact:true}).count(),0);
  assert.equal(await page.getByText('12345678_air.csv',{exact:true}).count(),0);
  report.checks.isolation = 'late old-session response cannot rehydrate another chat; shared view clears private queue and files';

  // Exercise more than the removed internal batch limit through the real selection UI.
  // An isolated conversation keeps the preceding restoration and privacy checks intact.
  tickJobs=false;
  const bulkSearch={...makeSearch('bulk-search',{query:'分析连续 24 份观测数据'},'bulk-session'),status:'ready',sources:[{id:'catalog',title:'合成目录',status:'ready',count:1}],results:[bulkCandidate],has_more:false};
  records.push(bulkSearch);
  await page.setViewportSize({width:1440,height:1000});
  await page.evaluate(()=>{window.harness.shared.value=false;window.harness.sid.value='bulk-session'});
  await searchCards.getByRole('heading',{name:bulkCandidate.title,exact:true}).waitFor();
  const bulkCard=searchCards.first();
  await bulkCard.getByText('可获取 · 24 个文件',{exact:true}).waitFor();
  await bulkCard.locator('.open-data-facts').getByText('大小未知',{exact:true}).waitFor();
  const acquireCountBeforeResolve=report.actions.filter(action=>action.kind==='acquire').length;
  bulkResolutionMode='held';
  await bulkCard.getByRole('button',{name:'获取数据',exact:true}).click();
  await bulkCard.getByRole('button',{name:'取消读取',exact:true}).click();
  await bulkCard.getByRole('button',{name:'获取数据',exact:true}).waitFor();
  for(const release of pendingBulkResolutions.splice(0))await release();
  assert.equal(await bulkCard.getByRole('alert').count(),0);
  assert.equal(await bulkCard.getByRole('checkbox').count(),0,'cancelled resolution must not apply late files');
  assert.equal(await bulkCard.getByRole('heading',{name:bulkCandidate.title,exact:true}).count(),1);
  bulkResolutionMode='failure';
  await bulkCard.getByRole('button',{name:'获取数据',exact:true}).click();
  await bulkCard.getByRole('alert').getByText('暂时未能读取完整文件列表，已保留这份数据。请点击“获取数据”重试。',{exact:true}).waitFor();
  assert.equal(await bulkCard.getByRole('heading',{name:bulkCandidate.title,exact:true}).count(),1);
  bulkResolutionMode='partial';
  await bulkCard.getByRole('button',{name:'获取数据',exact:true}).click();
  await bulkCard.getByText('可获取 1 个文件 · 已选 1 个',{exact:true}).waitFor();
  await bulkCard.getByText('还有 23 个文件暂未确认').waitFor();
  assert.equal(report.actions.filter(action=>action.kind==='acquire').length,acquireCountBeforeResolve,'one usable file plus pending verification must not acquire automatically');
  assert.equal(await panel.count(),0);
  bulkResolutionMode='complete';
  await bulkCard.getByRole('button',{name:'重试读取',exact:true}).click();
  await bulkCard.getByText('可获取 24 个文件 · 已选 1 个',{exact:true}).waitFor();
  assert.equal(await bulkCard.getByRole('checkbox',{name:'选择 observations-01.csv',exact:true}).isChecked(),true);
  assert.equal(await bulkCard.getByText('还有 23 个文件暂未确认').count(),0);
  await bulkCard.getByRole('button',{name:'清空',exact:true}).click();
  await bulkCard.getByText('可获取 24 个文件 · 已选 0 个',{exact:true}).waitFor();
  assert.equal(await bulkCard.getByRole('checkbox').count(),24);
  assert.equal(await bulkCard.getByText(/16 个文件|本批最多|每批最多/).count(),0);
  await bulkCard.locator('.open-data-facts').getByText('已知 23.0 KB，另有 1 个文件大小未知',{exact:true}).waitFor();
  assert.equal(await bulkCard.getByText(verificationMessage,{exact:true}).evaluateAll(nodes=>nodes.length===24&&nodes.every(node=>node.classList.contains('open-data-note')&&!node.classList.contains('open-data-warning'))),true);
  await bulkCard.getByRole('button',{name:'全选',exact:true}).click();
  await bulkCard.getByText('可获取 24 个文件 · 已选 24 个',{exact:true}).waitFor();
  assert.equal(await bulkCard.locator('input[type="checkbox"]:checked').count(),24);
  await bulkCard.getByText('已选文件大小：已知 23.0 KB，另有 1 个文件大小未知',{exact:true}).waitFor();
  await bulkCard.getByRole('button',{name:'清空',exact:true}).click();
  await bulkCard.getByText('可获取 24 个文件 · 已选 0 个',{exact:true}).waitFor();
  assert.equal(await bulkCard.locator('input[type="checkbox"]:checked').count(),0);
  await bulkCard.getByRole('button',{name:'全选',exact:true}).click();
  await page.screenshot({path:join(output,'desktop-24-selected.png')});
  await page.setViewportSize({width:390,height:844});
  await noOverflow('mobile24Selection');
  await page.screenshot({path:join(output,'mobile-24-selected.png')});
  const beforeBulk=report.actions.filter(action=>action.kind==='acquire').length;
  await bulkCard.getByRole('button',{name:'获取数据（24 个文件）',exact:true}).click();
  await panel.waitFor();
  const bulkRequests=report.actions.filter(action=>action.kind==='acquire').slice(beforeBulk);
  assert.equal(bulkRequests.length,1,'all 24 selected files must be submitted in one acquisition request');
  assert.deepEqual(bulkRequests[0].body.file_ids,bulkOptions.map(option=>option.id));
  const bulkJob=jobs.find(job=>job.search_id==='bulk-search');
  await jobCard(bulkJob.id).getByText('已获取 0 / 24 个文件',{exact:true}).waitFor();
  assert.equal(await jobCard(bulkJob.id).locator('.acquisition-item').count(),24);
  await noOverflow('mobile24Queue');
  await close();
  await receipt(0).getByText('正在获取 24 个文件',{exact:true}).waitFor();
  bulkJob.status='ready';bulkJob.asset_id='bulk-asset';bulkJob.files=bulkOptions.map(option=>file(option.id,option.filename));
  bulkJob.items.forEach((item,index)=>Object.assign(item,{status:'ready',file:bulkJob.files[index],bytes_received:120,size:120,message:'已下载并完成文件校验；分析适用性尚待确认'}));
  await page.evaluate(()=>window.harness.notify({sessionId:'bulk-session',searchId:'bulk-search'}));
  await receipt(0).getByText('已保存 24 个文件',{exact:true}).waitFor();
  assert.equal(await panel.count(),0);
  await reopen(0);
  await jobCard(bulkJob.id).getByText('已获取 24 / 24 个文件',{exact:true}).waitFor();
  await jobCard(bulkJob.id).getByText('获取完成',{exact:true}).waitFor();
  assert.equal(await jobCard(bulkJob.id).locator('.acquisition-warning').count(),0);
  report.checks.unlimitedFiles='real 3-file and 24-file counts; select all/clear; one request contains every 24 IDs; pending/saved/file totals stay distinct; unknown size is never a partial total; successful verification is neutral';
  report.checks.fileLookupRecovery='cancel ignores late result without hiding candidate; temporary 503 retains retry; one verified plus 23 pending does not acquire; retry preserves valid selection and resolves all 24';

  // Preserve server relevance order across sources, including legacy verified rows.
  await close();
  const relevanceReason='记录提供对应主题的原始观测值，并包含研究对象、变量与采样说明。'.repeat(5);
  const relevanceRows=Array.from({length:7},(_,index)=>({...candidate,id:`ranked-${index+1}`,title:`相关性顺序 ${index+1}（合成示例）`,source_id:index<3?'source-a':'source-b',relevance_status:'related',relevance_reason:index===0?relevanceReason:undefined}));
  delete relevanceRows[4].relevance_status;
  const unrelated={...candidate,id:'unrelated',title:'明确无关：只有通用词相同',relevance_status:'unrelated'};
  const uncertain={...candidate,id:'uncertain',title:'待确认：尚无主题匹配证据',relevance_status:'uncertain'};
  const relevanceSearch={...makeSearch('relevance-search',{query:'相关性排序回归'},'relevance-session'),status:'ready',sources:[{id:'source-a',title:'来源 A',status:'ready',count:3},{id:'source-b',title:'来源 B',status:'ready',count:4}],results:[unrelated,...relevanceRows.slice(0,2),uncertain,...relevanceRows.slice(2)],has_more:true};
  records.push(relevanceSearch);
  const pendingSearch={...makeSearch('relevance-pending',{query:'相关性待确认回归'},'relevance-session'),status:'searching',results:[uncertain],has_more:false,relevance_pending_count:2};
  records.push(pendingSearch);
  await page.setViewportSize({width:1440,height:1000});
  await page.evaluate(()=>{window.harness.sid.value='relevance-session'});
  const rankedCard=searchCards.filter({has:page.getByRole('heading',{name:'相关性排序回归',exact:true})});
  const pendingCard=searchCards.filter({has:page.getByRole('heading',{name:'相关性待确认回归',exact:true})});
  await rankedCard.getByRole('heading',{name:relevanceRows[0].title,exact:true}).waitFor();
  assert.deepEqual(await rankedCard.locator('.open-data-candidate h4').allTextContents(),relevanceRows.slice(0,5).map(row=>row.title));
  assert.equal(await page.getByRole('heading',{name:unrelated.title,exact:true}).count(),0);
  assert.equal(await page.getByRole('heading',{name:uncertain.title,exact:true}).count(),0);
  await rankedCard.getByText('7 个可获取的数据集',{exact:true}).waitFor();
  const matchReason=rankedCard.locator('.open-data-relevance');
  assert.equal(await matchReason.textContent(),`匹配依据：${relevanceReason}`);
  assert.equal(await matchReason.getAttribute('title'),relevanceReason);
  assert.deepEqual(await matchReason.evaluate(node=>({whiteSpace:getComputedStyle(node).whiteSpace,overflow:getComputedStyle(node).overflow,textOverflow:getComputedStyle(node).textOverflow})),{whiteSpace:'nowrap',overflow:'hidden',textOverflow:'ellipsis'});
  assert.equal(await rankedCard.getByRole('button',{name:'继续查找',exact:true}).count(),0);
  const searchesBeforeExpansion=report.actions.filter(action=>action.kind==='more').length;
  await rankedCard.getByRole('button',{name:'显示更多结果（当前显示 5 / 7）',exact:true}).click();
  assert.deepEqual(await rankedCard.locator('.open-data-candidate h4').allTextContents(),relevanceRows.map(row=>row.title));
  assert.equal(report.actions.filter(action=>action.kind==='more').length,searchesBeforeExpansion,'show more must reveal existing candidates before requesting another provider page');
  await rankedCard.getByRole('button',{name:'继续查找',exact:true}).waitFor();
  await pendingCard.getByText('正在确认 2 个候选数据集与需求的相关性…',{exact:true}).waitFor();
  assert.equal(await pendingCard.getByText(/未找到与该需求相关/).count(),0);
  pendingSearch.status='partial';
  await page.evaluate(()=>window.harness.notify({sessionId:'relevance-session',searchId:'relevance-pending'}));
  await pendingCard.getByText('有 2 个候选数据集待确认相关性，可继续查找。',{exact:true}).waitFor();
  assert.equal(await pendingCard.locator('.open-data-candidate').count(),0);
  assert.equal(await pendingCard.getByText(/未找到与该需求相关/).count(),0);
  await rankedCard.evaluate(node=>node.scrollIntoView({block:'start'}));
  await noOverflow('desktopRelevance');
  await page.screenshot({path:join(output,'desktop-relevance.png')});
  await pendingCard.evaluate(node=>node.scrollIntoView({block:'center'}));
  await page.screenshot({path:join(output,'desktop-relevance-pending.png')});
  await page.setViewportSize({width:390,height:844});
  await rankedCard.evaluate(node=>node.scrollIntoView({block:'start'}));
  await noOverflow('mobileRelevance');
  await page.screenshot({path:join(output,'mobile-relevance.png')});
  report.checks.relevance='authoritative order retained across sources; explicit unrelated/uncertain hidden; legacy verified rows remain; reason uses one-line ellipsis with full title; local expansion precedes further search; pending is never called an empty result';
  assert.deepEqual(report.errors,[]);
  assert.deepEqual(report.externalAttempts,[]);
  assert.deepEqual(report.unexpectedRequests,[]);
  report.checks.sessionWideRestore=reads.some(read=>read.path==='/acquisitions'&&!read.searchId)&&reads.some(read=>read.path==='/data-searches');
  assert.equal(report.checks.sessionWideRestore,true);
  report.passed=true;
} finally {
  if(!report.passed)await page.screenshot({path:join(output,'failure.png')}).catch(()=>{});
  await writeFile(join(output, 'report.json'), JSON.stringify(report, null, 2));
  console.log(JSON.stringify({ output, ...report }, null, 2));
  await browser.close();
}
