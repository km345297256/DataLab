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
const entry = `import {createApp,h,ref,watch} from 'vue';
import {createI18n} from 'vue-i18n';
import {createRouter,createMemoryHistory,RouterView} from 'vue-router';
import ChatPage from './src/pages/ChatPage.vue';
import TaskDataPanel from './src/components/TaskDataPanel.vue';
import FilePanel from './src/components/FilePanel.vue';
import {useTaskData} from './src/composables/useTaskData';
import {useTaskDataInterop} from './src/composables/useTaskDataInterop';
import {useAcquisitionPanelInterop} from './src/composables/useAcquisitionPanelInterop';
import {useDataAcquisition} from './src/composables/useDataAcquisition';
import ShellToolView from './src/components/toolViews/ShellToolView.vue';
import {useAuth} from './src/composables/useAuth';
import {eventBus} from './src/utils/eventBus';
const shell=ref(false),streams=[],requests=[];
const nativeFetch=window.fetch.bind(window);
window.fetch=async(input,options={})=>{
 const url=String(input);
 if(!/\\/sessions\\/[^/]+\\/chat$/.test(url))return nativeFetch(input,options);
 const body=JSON.parse(options.body),record={body,headers:Object.fromEntries(new Headers(options.headers)),aborted:false};
 requests.push(record);
 const stream=new ReadableStream({start(controller){record.controller=controller;options.signal?.addEventListener('abort',()=>{record.aborted=true;try{controller.error(new DOMException('aborted','AbortError'))}catch{}})}});
 streams.push(record);
 return new Response(stream,{headers:{'Content-Type':'text/event-stream'}});
};
const router=createRouter({history:createMemoryHistory(),routes:[{path:'/chat/:sessionId',component:ChatPage},{path:'/:pathMatch(.*)*',component:{render:()=>null}}]});
window.harness={router,requests,streams,shell,
 automaticAcquisition(){eventBus.emit('open-data-panel-show',{sessionId:router.currentRoute.value.params.sessionId,automatic:true})},
 emit(index,event,seq,data={}){const r=streams[index];if(r.aborted)return; r.controller.enqueue(new TextEncoder().encode('id: '+seq+'-0\\nevent: '+event+'\\ndata: '+JSON.stringify({seq,event_id:seq+'-0',timestamp:1760000000+seq,...data})+'\\n\\n'));},
 fail(index){streams[index].controller.error(new TypeError('synthetic transport loss'));},
 draft(files,query){eventBus.emit('connector-analysis-draft',{sessionId:router.currentRoute.value.params.sessionId,files,query})}};
const app=createApp({setup(){
 const taskData=useTaskData(),acquisition=useDataAcquisition();useTaskDataInterop();useAcquisitionPanelInterop();
 watch(()=>router.currentRoute.value.params.sessionId,id=>{taskData.activate(id);acquisition.activate(id)},{immediate:true,flush:'sync'});
 return ()=>shell.value?h('main',{class:'shell-fixture'},[h(ShellToolView,{sessionId:'session-a',live:false,toolContent:{tool_call_id:'log-call',name:'shell',function:'shell_exec',args:{id:'shell-1'},status:'called',content:{console:[{output:'HEAD\\nTAIL',returncode:0}],output_metadata:{output_id:'a'.repeat(32),total_bytes:12,preview_truncated:true,log_status:'available',stream_complete:true}}}})]):h('div',{style:'display:flex;width:100%;height:100%;min-width:0'},[h(RouterView,null,{default:({Component,route})=>Component?h(Component,{key:route.fullPath}):null}),h(TaskDataPanel),h(FilePanel,{resizable:true,minContentWidth:780})]);}});
app.use(createI18n({legacy:false,locale:'en',messages:{en:{}},missingWarn:false,fallbackWarn:false}));app.use(router);
await useAuth().loadCurrentUser();await router.push('/chat/session-a');await router.isReady();app.mount('#app');window.unmountHarness=()=>app.unmount();window.harnessReady=true;`;
const bundle = await build({
  stdin: { contents: entry, sourcefile: 'open-data-entry.js', resolveDir: frontend }, bundle: true, write: false,
  format: 'esm', platform: 'browser', target: 'es2022', logLevel: 'warning',
  alias: { '@': resolve(frontend, 'src') },
  define: { 'import.meta.env': JSON.stringify({ BASE_URL: '/', MODE: 'test', DEV: false, PROD: true }),
    '__VUE_OPTIONS_API__': 'true', '__VUE_PROD_DEVTOOLS__': 'false', '__VUE_PROD_HYDRATION_MISMATCH_DETAILS__': 'false', 'process.env.NODE_ENV': '"production"' },
  plugins: [{ name: 'real-open-data-sfc', setup(builder) {
    builder.onLoad({ filter: /\.vue$/ }, async ({ path }) => {
      let source = await readFile(path, 'utf8');
      // Only unrelated sidebars/tool chrome are replaced; ChatPage, composer,
      // messages, OpenData, run status, inputs, result images and shell are real.
      if (/\/(PersonalDataSidebar|AnalysisHistoryMenu|BrowserToolView|FileToolView|MonacoEditor)\.vue$/.test(path)) source='<template><span>我的数据</span></template>';
      if (/\/VisualizationHost\.vue$/.test(path)) source='<script setup>defineProps({file:Object})</script><template><div class="preview-fixture">{{file.filename}}</div></template>';
      if (/\/ToolPanel\.vue$/.test(path)) source='<script setup>defineExpose({hideToolPanel(){},showToolPanel(){}})</script><template><span /></template>';
      if (!parse(source).descriptor.script && !parse(source).descriptor.scriptSetup) source += '\n<script setup>const __marker = true;</script>';
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

const output = await mkdtemp(join(tmpdir(), 'datalab-analysis-lifecycle-'));
const report={browserEngine,passed:false,checks:{},errors:[],unexpectedRequests:[],externalAttempts:[]};
const chrome='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const browser=await launchBrowser();
const context=await browser.newContext({viewport:{width:1440,height:1000}});
const page=await context.newPage();page.setDefaultTimeout(12000);
page.on('pageerror',error=>report.errors.push(error.message));
const jwt=exp=>`e30.${Buffer.from(JSON.stringify({exp})).toString('base64url')}.signature`;
const validToken=jwt(Math.floor(Date.now()/1000)+3600);
await context.addInitScript(token=>{localStorage.setItem('access_token',token);localStorage.setItem('refresh_token','synthetic-refresh');},validToken);
const file=(id,name)=>({file_id:id,filename:name,size:100,upload_date:'2026-10-20T01:00:00Z',content_type:name.endsWith('.svg')?'image/svg+xml':'text/csv'});
const input=file('original','original.csv'),nextFile=file('new-draft','next.csv'),svg=file('safe-svg','plot.svg');
const downloaded=file('downloaded','download.csv'),oldOutput=file('old-output','plot.svg');
let savedOutput=false;const savedRequests=[];
const taskItem=(f,category,round_number=null,saved=false)=>({file:f,category,round_number,created_at:f.upload_date,available:true,saved_locations:saved?[{asset_id:'saved-'+f.file_id,asset_name:f.filename,folder_id:'results',folder_path:'研究/成果',file_path:'研究/成果/'+f.filename}]:[]});
const event=(event,seq,data)=>({event,data:{seq,event_id:`${seq}-0`,timestamp:1760000000+seq,...data}});
const recent=[event('message',100,{role:'user',content:'历史问题：合成数据的范围',attachments:[input],metadata:{client_message_id:'historic-input',data_connectors:['ncbi-geo']}}),event('step',101,{id:'historic-step',status:'completed',description:'历史执行过程'}),event('message',102,{role:'assistant',content:'历史结果保留 ~10% 估计值。',metadata:{analysis_outcome:{status:'succeeded',reason_code:'completed',scope:'step',missing:[],can_resume:false}}}),event('done',103,{})];
const early=Array.from({length:40},(_,i)=>event('message',i+1,{role:i%2?'assistant':'user',content:`更早记录 ${i+1}，合成样本用于分页阅读。`}));
const plugins=await Promise.all(['svg','image'].map(async name=>({...JSON.parse(await readFile(resolve(frontend,`../plugin-host/visualizations/${name}.json`),'utf8')),enabled:true})));
const png='iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Zl1sAAAAASUVORK5CYII=';
let stops=0,refreshes=0,svgRequests=0;const shellReads=[];
await context.route('**/*',async route=>{
 if (route.request().url().startsWith('blob:')) return route.continue();
 const request=route.request(),url=new URL(request.url()),method=request.method();
 const ok=data=>route.fulfill({contentType:'application/json',body:JSON.stringify({code:0,msg:'success',data})});
 if(url.origin!=='http://localhost:7001'){report.externalAttempts.push(url.href);return route.abort();}
 if(url.pathname==='/__analysis__/')return route.fulfill({contentType:'text/html',body:'<!doctype html><html><head><meta charset="utf-8"><link rel="stylesheet" href="/__analysis__/project.css"><style>html,body,#app{margin:0;height:100%;font-family:Arial,sans-serif}#app{display:flex}*{box-sizing:border-box}.shell-fixture{height:500px;width:700px;display:flex;flex-direction:column}</style></head><body><div id="app"></div><script type="module" src="/__analysis__/entry.js"></script></body></html>'});
 if(url.pathname==='/__analysis__/entry.js')return route.fulfill({contentType:'application/javascript',body:Buffer.from(bundle.outputFiles[0].contents)});
 if(url.pathname==='/__analysis__/project.css')return route.fulfill({contentType:'text/css',body:css});
 if(url.pathname==='/datalab-logo.png')return route.fulfill({contentType:'image/png',body:Buffer.from(png,'base64')});
 if(url.pathname==='/api/v1/auth/me')return ok({id:'synthetic-owner',fullname:'测试用户',email:'test@example.invalid',is_active:true,role:'user'});
 if(url.pathname==='/api/v1/auth/refresh'){refreshes++;return ok({access_token:validToken});}
 if(url.pathname==='/api/v1/agent-profiles')return ok([]);
 if(url.pathname==='/api/v1/config/frontend')return ok({});
 if(url.pathname==='/api/v1/visualizations')return ok({engine:'cordis',revision:'gate',plugins});
 if(url.pathname==='/api/v1/files/safe-svg/visualization'){
  assert.equal(request.headers().authorization,`Bearer ${validToken}`);svgRequests++;
  assert.deepEqual(request.postDataJSON(),{plugin_id:'viz-svg',operation:'preview',kind:'image',options:{}});
  return ok({contract_version:2,plugin_id:'viz-svg',version:'b'.repeat(64),revision:'c'.repeat(64),kind:'media',payload:{media_type:'image/png',view_kind:'image',data_base64:png},metadata:{source_format:'SVG',rasterized:true,width:1,height:1},warnings:[],sampled:false});
 }
 if(url.pathname==='/api/v1/data-library/folders')return ok({items:[{id:'results',parent_id:null,name:'成果',path:'研究/成果',created_at:input.upload_date,updated_at:input.upload_date}]});
 if(url.pathname==='/api/v1/data-library'&&method==='POST'){const body=request.postDataJSON();savedRequests.push(body);savedOutput=true;return ok({id:'saved-output',name:body.name,description:'',tags:[],source_type:body.source_type,files:[svg],source_url:null,session_id:'session-a',created_at:input.upload_date,updated_at:input.upload_date,version:1,status:'ready',source_verified:true});}
 if(url.pathname==='/api/v1/data-connectors')return ok([{id:'ncbi-geo',title:'NCBI GEO',description:'合成连接器',operations:[],limitations:[]}]);
 if(/^\/api\/v1\/skills/.test(url.pathname))return ok(url.pathname.endsWith('/preferences')?{auto_enabled_skills:[]}:[]);
 const match=url.pathname.match(/^\/api\/v1\/sessions\/([^/]+)(.*)$/),sid=match?.[1],path=match?.[2];
 if(path==='/history')return ok({session_id:sid,title:sid==='session-a'?'合成分析 A':'合成分析 B',status:'completed',title_manually_set:true,is_shared:false,created_at:1760000000,events:sid==='session-a'?(url.searchParams.has('before_seq')?early:recent):[],has_more:sid==='session-a'&&!url.searchParams.has('before_seq'),next_before_seq:100});
 if(path==='/task-data'){
  const items=sid==='session-a'?[taskItem(input,'upload',null,true),taskItem(svg,'analysis',2,savedOutput),taskItem(oldOutput,'analysis',1),taskItem(downloaded,'download',null,true)]:[];
  for(const id of url.searchParams.getAll('file_ids'))if(id===nextFile.file_id&&!items.some(item=>item.file.file_id===id))items.push(taskItem(nextFile,'upload'));
  return ok({items,selected_file_ids:sid==='session-a'?['original']:[],unavailable_count:0});
 }
 if(path==='/analysis-inputs')return ok({files:sid==='session-a'?[input]:[],selected_file_ids:sid==='session-a'?[input.file_id]:[]});
 if(path==='/clear_unread_message_count')return ok(null);
 if(path==='/data-searches')return ok(sid==='session-a'?[{id:'historic-search',session_id:sid,query:'合成城市数据查找',status:'completed',sources:[],results:[],has_more:false,created_at:new Date(1760000101500).toISOString()}]:[]);
 if(path==='/data-searches/historic-search')return ok({id:'historic-search',session_id:sid,query:'合成城市数据查找',status:'completed',sources:[],results:[],has_more:false,created_at:new Date(1760000101500).toISOString()});
 if(path==='/acquisitions')return ok([]);
 if(path==='/runs')return ok({runs:[],next_before_seq:null});
 if(path==='/feedback')return ok(null);
 if(path?.startsWith('/inputs/'))return ok({client_message_id:decodeURIComponent(path.slice(8)),accepted:false,state:null});
 if(path==='/stop'){stops++;return ok({confirmed:true});}
 if(path==='/shell'){
   const body=request.postDataJSON();shellReads.push(body);assert.equal(body.max_bytes,16384);assert.equal(body.output_id,'a'.repeat(32));
   const start=body.cursor,output=start===0?'first\n':'last!\n';
   return ok({output,output_metadata:{output_id:'a'.repeat(32),total_bytes:12,preview_truncated:true,log_status:'available',stream_complete:true},output_page:{cursor:start,start,next_cursor:start===0?6:12,lossy:false,source:'spool',eof:start!==0}});
 }
 report.unexpectedRequests.push(`${method} ${url.pathname}`);return route.abort();
});
const layoutGeometry=()=>page.evaluate(()=>{
 const bounds=selector=>{const r=document.querySelector(selector).getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height,bottom:r.bottom};};
 const scroll=document.querySelector('.analysis-message-scroll .simplebar-content-wrapper');
 return {layout:bounds('.analysis-chat-layout'),messages:bounds('.analysis-message-scroll'),footer:bounds('.analysis-chat-footer'),
  composer:bounds('.chat-composer-surface'),scrollTop:scroll.scrollTop,scrollHeight:scroll.scrollHeight,clientHeight:scroll.clientHeight,
  pageTop:window.scrollY,pageHeight:document.documentElement.scrollHeight,viewportHeight:innerHeight};
});
function assertStableFooter(before,after,label){
 for(const key of ['y','height','bottom'])assert.ok(Math.abs(before.footer[key]-after.footer[key])<=2,`${label}: footer ${key} moved`);
 assert.ok(Math.abs(before.messages.height-after.messages.height)<=2,`${label}: message area was squeezed`);
 assert.equal(after.pageTop,before.pageTop,`${label}: the document must not scroll`);
}
const inputPanelToggle=page.getByRole('region',{name:'本次分析资料',exact:true}).getByRole('button',{name:/分析资料/});
async function openInputs(){if(await inputPanelToggle.getAttribute('aria-expanded')!=='true')await inputPanelToggle.click();}
async function closeInputs(){if(await inputPanelToggle.getAttribute('aria-expanded')==='true'){await page.keyboard.press('Escape');await page.waitForFunction(()=>document.querySelector('[aria-label="本次分析资料"] button[aria-expanded]')?.getAttribute('aria-expanded')==='false');}}
async function checkFloatingPanel(toggle,label){
 const before=await layoutGeometry();await toggle.click();assert.equal(await toggle.getAttribute('aria-expanded'),'true');
 assertStableFooter(before,await layoutGeometry(),label);
 if(label==='execution progress'){
  const viewport=page.viewportSize();
  for(const width of [viewport.width,960,390]){
   await page.setViewportSize({width,height:viewport.height});
   await page.waitForFunction(()=>{
    const popup=document.querySelector('.plan-panel-content')?.getBoundingClientRect();
    const composer=document.querySelector('.analysis-chat-input .chat-composer')?.getBoundingClientRect();
    return popup&&composer&&Math.abs(popup.width-composer.width)<2&&Math.abs(popup.left-composer.left)<2;
   });
  }
  await page.setViewportSize(viewport);
  await page.waitForFunction(()=>Math.abs(document.querySelector('.plan-panel-content').getBoundingClientRect().width-document.querySelector('.analysis-chat-input .chat-composer').getBoundingClientRect().width)<2);
  report.checks.progressMatchesComposer=true;
 }
 await page.keyboard.press('Escape');assert.equal(await toggle.getAttribute('aria-expanded'),'false');
 assertStableFooter(before,await layoutGeometry(),`${label} closed`);
}
try{
 await page.goto('http://localhost:7001/__analysis__/',{waitUntil:'domcontentloaded'});
 await page.waitForFunction(()=>window.harnessReady);
 await page.getByText('历史结果保留 ~10% 估计值。',{exact:true}).waitFor();
 await page.getByRole('region',{name:'开放数据查找',exact:true}).waitFor();
 assert.equal(await page.getByRole('button',{name:'Stop task',exact:true}).count(),0);
 const inputBox=page.locator('textarea');await inputBox.waitFor();
 const initialLayout=await layoutGeometry();
 assert.ok(initialLayout.messages.height/initialLayout.layout.height>.75,'Collapsed composer must leave at least 75% of the workspace for messages');
 assert.ok(initialLayout.footer.bottom<=initialLayout.viewportHeight+1,'The composer must stay inside the viewport');
 assert.ok(initialLayout.pageHeight<=initialLayout.viewportHeight+1,'The page must not acquire a second vertical scrollbar');
 await checkFloatingPanel(inputPanelToggle,'analysis inputs');
 await checkFloatingPanel(page.getByRole('button',{name:'查看任务进度与执行记录',exact:true}),'execution progress');
 const contextToggle=page.locator('.chat-context-toggle');
 await contextToggle.waitFor();assert.equal(await contextToggle.getAttribute('aria-expanded'),'false');
 assert.equal(await page.locator('.chat-context').isVisible(),false,'Historical files and connectors must start collapsed');
 await contextToggle.click();
 await page.getByRole('button',{name:'移除连接器 NCBI GEO',exact:true}).waitFor();
 assert.equal(await page.getByRole('button',{name:'移除连接器 NCBI GEO',exact:true}).isEnabled(),true);
 assert.equal(await inputBox.isVisible(),true,'Expanding draft context must keep the editor reachable');
 await contextToggle.click();assert.equal(await contextToggle.getAttribute('aria-expanded'),'false');
 assertStableFooter(initialLayout,await layoutGeometry(),'collapsed draft context');
 report.checks.chatViewport={messageRatio:initialLayout.messages.height/initialLayout.layout.height,inputsFloat:true,progressFloats:true,escapeClosesPanels:true,contextCollapsed:true};
 await openInputs();
 const original=page.getByRole('checkbox',{name:'分析资料 original.csv (original)',exact:true});await original.waitFor();assert.equal(await original.isChecked(),true);
 const inputPopup=page.locator('[data-slot="popover-content"].analysis-inputs-content');
 const popupStyle=await inputPopup.evaluate(el=>{const s=getComputedStyle(el);return {padding:parseFloat(s.paddingLeft),width:el.getBoundingClientRect().width,maxHeight:s.maxHeight,focused:document.activeElement===el};});
 assert.ok(popupStyle.padding>=12,'Teleported materials popup must retain its inner spacing');
 assert.ok(popupStyle.width<=420,'Teleported materials popup must retain its compact width');
 assert.notEqual(popupStyle.maxHeight,'none','Long file lists must scroll inside the bounded popup');
 assert.equal(popupStyle.focused,true,'Opening the popup must not auto-focus the first checkbox');
 report.checks.materialsPopup=popupStyle;
 await closeInputs();
 // Exercise the real task data controls with the real ChatPage draft.
 await inputBox.fill('统一任务数据不得覆盖这个草稿');
 await page.getByRole('button',{name:'任务数据',exact:true}).click();
 const taskPanel=page.locator('#task-data-panel');await taskPanel.waitFor();
 await page.locator('[data-task-file-id="original"] button').filter({hasText:'移出分析'}).waitFor();
 assert.equal(await taskPanel.locator('[data-task-file-id]').count(),4);
 assert.equal(await taskPanel.locator('[data-task-file-id="original"] input[type=checkbox]').isDisabled(),true);
 await taskPanel.getByRole('tab',{name:/分析成果/}).click();
 assert.equal(await taskPanel.locator('[data-task-file-id]').count(),2,'Same-name versions retain separate rows');
 await page.evaluate(()=>window.harness.automaticAcquisition());
 assert.equal(await taskPanel.getByRole('tab',{name:/分析成果/}).getAttribute('aria-selected'),'true','Automatic downloads must not replace the current data view');
 const resultRow=taskPanel.locator('[data-task-file-id="safe-svg"]');
 await resultRow.getByRole('button',{name:'加入分析',exact:true}).click();
 await resultRow.getByRole('button',{name:'移出分析',exact:true}).waitFor();
 assert.equal(await inputBox.inputValue(),'统一任务数据不得覆盖这个草稿');
 assert.equal(await taskPanel.locator('[data-task-file-id]').count(),2,'Selecting a result does not change its origin');
 await resultRow.getByRole('button',{name:'移出分析',exact:true}).click();
 await resultRow.getByRole('button',{name:'保存到我的数据',exact:true}).click();
 const saveDialog=page.getByRole('dialog',{name:'保存到我的数据',exact:true});await saveDialog.waitFor();
 await saveDialog.getByRole('combobox',{name:'保存到文件夹',exact:true}).selectOption('results');
 await saveDialog.getByRole('button',{name:'保存数据',exact:true}).click();
 await saveDialog.waitFor({state:'detached'});await resultRow.getByText('已保存',{exact:true}).waitFor();
 assert.equal(savedRequests.length,1);assert.deepEqual(savedRequests[0].file_ids,['safe-svg']);assert.equal(savedRequests[0].target_folder_id,'results');assert.equal(savedRequests[0].source_type,'analysis');
 await page.setViewportSize({width:1440,height:420});
 await taskPanel.locator('.task-data-results').evaluate(element=>element.scrollTop=60);
 const savedScroll=await taskPanel.locator('.task-data-results').evaluate(element=>element.scrollTop);
 await taskPanel.locator('[data-task-file-id="old-output"]').getByRole('button',{name:'预览 plot.svg',exact:true}).click();
 await taskPanel.waitFor({state:'detached'});
 await page.getByRole('button',{name:'返回任务数据',exact:true}).click();
 await taskPanel.waitFor();assert.equal(await taskPanel.getByRole('tab',{name:/分析成果/}).getAttribute('aria-selected'),'true');
 assert.ok(await page.locator('.analysis-data-layout').evaluate(element=>element.getBoundingClientRect().width)>=1000,'Returning to data must release the preview width immediately');
 const restoredScroll=await taskPanel.locator('.task-data-results').evaluate(element=>element.scrollTop);
 assert.ok(Math.abs(restoredScroll-savedScroll)<2,`Preview return preserves list scroll (${savedScroll} -> ${restoredScroll})`);
 await page.setViewportSize({width:1440,height:1000});
 await page.screenshot({path:join(output,'task-data-panel.png'),fullPage:true});
 await taskPanel.getByRole('button',{name:'收起任务数据',exact:true}).click();
 await page.getByRole('button',{name:'任务数据',exact:true}).click();
 await taskPanel.waitFor();await taskPanel.getByRole('tab',{name:/搜索下载/}).click();
 await taskPanel.waitFor();assert.equal(await taskPanel.getByRole('tab',{name:/搜索下载/}).getAttribute('aria-selected'),'true');
 assert.equal(await taskPanel.locator('[data-task-file-id]').count(),1);
 await taskPanel.getByRole('button',{name:'收起任务数据',exact:true}).click();
 await openInputs();await page.getByRole('button',{name:'查看全部任务数据',exact:true}).click();
 await taskPanel.waitFor();assert.equal(await taskPanel.getByRole('tab',{name:/全部/}).getAttribute('aria-selected'),'true');
 await page.setViewportSize({width:390,height:844});
 await page.waitForFunction(()=>document.querySelector('#task-data-panel')?.getAttribute('role')==='dialog');
 const taskBounds=await taskPanel.boundingBox();assert.ok(taskBounds.width<=390&&taskBounds.x>=0);
 await page.keyboard.press('Escape');await taskPanel.waitFor({state:'detached'});
 await page.setViewportSize({width:1440,height:1000});await inputBox.fill('');
 report.checks.unifiedTaskData={originAndVersions:true,draftPreserved:true,folderSave:true,previewReturnsToTab:true,allEntriesConverge:true,mobileCloses:true};
 // Loading earlier turns keeps the currently visible message at its reading position.
 const anchor=page.getByText('历史问题：合成数据的范围',{exact:true});
 await page.getByRole('button',{name:'加载更早的对话',exact:true}).scrollIntoViewIfNeeded();const before=await anchor.boundingBox();
 await page.getByRole('button',{name:'加载更早的对话',exact:true}).click();
 await page.getByText('更早记录 1，合成样本用于分页阅读。',{exact:true}).waitFor({state:'attached'});
 await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
 const after=await anchor.boundingBox();report.checks.historyAnchorMetrics={before,after,layout:await layoutGeometry()};assert.ok(Math.abs(after.y-before.y)<30,`history reading anchor drifted ${after.y-before.y}`);
 report.checks.historyAnchor=true;report.checks.openDataMix=true;
 const scrollBefore=await layoutGeometry();assert.ok(scrollBefore.scrollHeight>scrollBefore.clientHeight+400,'Long history must overflow the message region');
 const scrollRegion=page.locator('.analysis-message-scroll .simplebar-content-wrapper');
 await scrollRegion.evaluate(element=>{element.scrollTop=Math.min(800,element.scrollHeight-element.clientHeight);});
 const wheelBefore=await layoutGeometry();const scrollBox=await scrollRegion.boundingBox();
 await page.mouse.move(scrollBox.x+scrollBox.width/2,scrollBox.y+scrollBox.height/2);await page.mouse.wheel(0,-350);
 await page.waitForFunction(top=>document.querySelector('.analysis-message-scroll .simplebar-content-wrapper').scrollTop<top-100,wheelBefore.scrollTop);
 const wheelAfter=await layoutGeometry();assertStableFooter(wheelBefore,wheelAfter,'message wheel scroll');
 assert.equal(wheelAfter.pageTop,0,'History wheel scrolling must not scroll the document');
 report.checks.independentMessageScroll={before:wheelBefore.scrollTop,after:wheelAfter.scrollTop,fixedFooter:true};
 await page.evaluate(expired=>localStorage.setItem('access_token',expired),jwt(1));
 await inputBox.fill('分析当前输入并生成图表');await page.getByRole('button',{name:'Send message',exact:true}).click();
 await page.waitForFunction(()=>window.harness.requests.length===1);
 assert.equal(refreshes,1);const initial=await page.evaluate(()=>window.harness.requests[0]);
 assert.equal(initial.headers.authorization,`Bearer ${validToken}`);assert.deepEqual(initial.body.input_file_ids,['original']);assert.deepEqual(initial.body.data_connectors,['ncbi-geo']);
 await page.evaluate(()=>window.harness.fail(0));
 await page.getByRole('button',{name:'停止重连',exact:true}).click();
 await inputBox.fill('下一轮草稿必须保留');
 await page.getByRole('button',{name:'恢复原提交或停止后的结果',exact:true}).click();
 await page.waitForFunction(()=>window.harness.requests.length===2);
 const retry=await page.evaluate(()=>window.harness.requests[1]);assert.deepEqual(retry.body,initial.body);
 await page.evaluate(({input})=>{const h=window.harness;h.emit(1,'message',110,{role:'user',content:'分析当前输入并生成图表',attachments:[input],metadata:{client_message_id:h.requests[1].body.client_message_id,data_connectors:['ncbi-geo']}});h.emit(1,'step',111,{id:'new-step',status:'running',description:'计算合成数据'});},{input});
 await page.getByText('思考中...',{exact:true}).waitFor();
 await page.evaluate(nextFile=>window.harness.draft([nextFile]),nextFile);
 assert.equal(await inputBox.inputValue(),'下一轮草稿必须保留');await openInputs();assert.equal(await original.isDisabled(),true);
 assert.equal(await page.getByRole('checkbox',{name:/分析资料 next.csv/}).count(),0);
 assert.equal(await page.getByText('分析当前输入并生成图表',{exact:true}).count(),1);
 report.checks.authAndSameIdRecovery=true;report.checks.frozenInputsAndNextDraft=true;await closeInputs();
 await page.getByRole('button',{name:'Stop task',exact:true}).click();
 await page.waitForFunction(()=>window.harness.requests.length===3);assert.equal(stops,1);
 const observe=await page.evaluate(()=>window.harness.requests[2].body);
 assert.equal(observe.observe_client_message_id,initial.body.client_message_id);
 for(const key of ['attachments','skills','mcp_servers','data_connectors','input_file_ids','client_message_id','resume_from'])assert.equal(key in observe,false,`stop observation must omit ${key}`);
 assert.equal(observe.message,'');
 await page.evaluate(svg=>{const h=window.harness;h.emit(2,'message',112,{role:'assistant',content:'停止前已保存图表，计算没有全部完成。',attachments:[svg],metadata:{analysis_outcome:{status:'partial',reason_code:'request_cancelled',scope:'step',missing:[{kind:'data',reason:'not_saved'}],can_resume:false}}});h.emit(2,'done',113,{});},svg);
 await page.getByText('停止前已保存图表，计算没有全部完成。',{exact:true}).waitFor();
 await page.getByRole('button',{name:'Send message',exact:true}).waitFor();
 await page.getByRole('region',{name:'可视化成果',exact:true}).scrollIntoViewIfNeeded();
 const preview=page.getByRole('img',{name:'plot.svg',exact:true});await preview.scrollIntoViewIfNeeded();
 await page.waitForFunction(()=>[...document.images].some(image=>image.alt==='plot.svg'&&image.src.startsWith('blob:')&&image.complete&&image.naturalWidth===1));
 assert.ok(svgRequests>=1&&svgRequests<=2);assert.equal(await page.locator('object,embed,iframe[src*="safe-svg"],svg script').count(),0);
 report.checks.stopObservationAndSvg=true;
 await page.evaluate(()=>window.harness.router.push('/chat/session-b'));
 await page.getByText('合成分析 B',{exact:true}).waitFor();
 assert.equal(await page.getByText('停止前已保存图表，计算没有全部完成。',{exact:true}).count(),0);
 assert.equal(stops,1,'navigation must not stop server work');
 await page.evaluate(()=>window.harness.router.push('/chat/session-a'));
 await page.getByText('合成分析 A',{exact:true}).waitFor();
 await page.waitForFunction(()=>document.querySelector('textarea')?.value==='下一轮草稿必须保留');
 const draftLayout=await layoutGeometry();
 assert.ok(draftLayout.messages.height/draftLayout.layout.height>.70,'A preserved draft must leave at least 70% of the workspace for messages');
 assert.equal(await contextToggle.getAttribute('aria-expanded'),'false','Restored draft attachment cards must stay collapsed');
 await contextToggle.click();await page.locator('.chat-context').getByText('next.csv',{exact:true}).waitFor();
 await contextToggle.click();await openInputs();
 assert.equal(await page.getByRole('checkbox',{name:/分析资料 next.csv/}).isChecked(),true);
 report.checks.routeDraftIsolation=true;
 await page.screenshot({path:join(output,'chat-page.png'),fullPage:true});
 await page.getByRole('checkbox',{name:'分析资料 original.csv (original)',exact:true}).uncheck();
 await page.getByRole('checkbox',{name:/分析资料 next.csv/}).click();
 await page.getByRole('checkbox',{name:/分析资料 next.csv/}).waitFor({state:'detached'});
 await closeInputs();
 await inputBox.fill('不用文件，解释分析方法');
 await page.getByRole('button',{name:'Send message',exact:true}).click();
 await page.waitForFunction(()=>window.harness.requests.length===4);
 assert.deepEqual(await page.evaluate(()=>window.harness.requests[3].body.input_file_ids),[]);
 assert.deepEqual(await page.evaluate(()=>window.harness.requests[3].body.attachments),[]);
 report.checks.explicitEmptySelection=true;

 await page.evaluate(()=>window.harness.shell.value=true);
 await page.getByRole('button',{name:'分段读取日志',exact:true}).click();
 await page.getByRole('tabpanel').getByText('first',{exact:true}).waitFor();
 await page.getByRole('button',{name:'下一段',exact:true}).click();
 await page.getByRole('tabpanel').getByText('last!',{exact:true}).waitFor();
 await page.getByRole('button',{name:'上一段',exact:true}).click();
 await page.getByRole('tabpanel').getByText('first',{exact:true}).waitFor();
 assert.deepEqual(shellReads.map(read=>read.cursor),[0,6,0]);
 report.checks.boundedLogPaging=true;
 await page.screenshot({path:join(output,'shell-pages.png'),fullPage:true});
 assert.deepEqual(report.errors,[]);assert.deepEqual(report.unexpectedRequests,[]);assert.deepEqual(report.externalAttempts,[]);
 report.passed=true;
}finally{
 await page.screenshot({path:join(output,'last-state.png'),fullPage:true}).catch(()=>{});
 await writeFile(join(output,'report.json'),JSON.stringify(report,null,2));
 console.log(JSON.stringify({...report,output},null,2));await browser.close();
}
