/** Real conversation and skill-result SFCs with synthetic draft APIs. No live account or model. */
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
const draft = { id:'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', name:'svg-english-to-chinese', description:'将英文 SVG 图示转换为中文，保持图形结构与专有名词，并交付可分享的 SVG 和 PNG。', capabilities:['翻译 SVG 中的英文标签并保留 HDFS 等专有名词','适配中文文本布局并导出 PNG','检查 XML、viewBox 和导出图片尺寸'], triggers:['将SVG转换成中文'], applicability:['含英文标签的 SVG 科研流程图'], resources:['SKILL.md','scripts/render_and_qa.py','references/workflow.json'], validation:{package:'passed',script_syntax:'passed',workflow_execution:'not_run'}, installed:false, installed_skill_id:null };
const syntheticDrafts = [
 {id:'eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee',state:'passed',validation:{...draft.validation,synthetic_execution:'passed',synthetic_case_count:3,synthetic_passed_count:3}},
 {id:'ffffffffffffffffffffffffffffffff',state:'unsupported',validation:{...draft.validation,synthetic_execution:'unsupported',synthetic_case_count:0,synthetic_passed_count:0}},
];
const tool = {tool_call_id:'create-skill',name:'skill',function:'skill_create_from_session',status:'called',timestamp:3,args:{},skill_creation:draft};
const entry = `import {createApp,h,ref,nextTick} from 'vue';
import {createI18n} from 'vue-i18n';
import AnalysisConversation from ${JSON.stringify(resolve(frontend, 'src/components/AnalysisConversation.vue'))};
import ToolUse from ${JSON.stringify(resolve(frontend, 'src/components/ToolUse.vue'))};
import SkillCreationCard from ${JSON.stringify(resolve(frontend, 'src/components/SkillCreationCard.vue'))};
import {projectHistoryMessages} from ${JSON.stringify(resolve(frontend, 'src/utils/sessionHistory.ts'))};
import {eventBus} from ${JSON.stringify(resolve(frontend, 'src/utils/eventBus.ts'))};
import {EVENT_SKILL_CATALOG_UPDATED} from ${JSON.stringify(resolve(frontend, 'src/constants/event.ts'))};
import zh from ${JSON.stringify(resolve(frontend, 'src/locales/zh.ts'))};
const draft=${JSON.stringify(draft)}, tool=${JSON.stringify(tool)}, syntheticDrafts=${JSON.stringify(syntheticDrafts)};
const catalogEvents=[];const catalogUpdated=()=>catalogEvents.push(EVENT_SKILL_CATALOG_UPDATED);eventBus.on(EVENT_SKILL_CATALOG_UPDATED,catalogUpdated);
const events=ref([{event:'message',data:{role:'user',content:'将本次流程保存为技能',timestamp:1}},{event:'step',data:{id:'create-step',status:'running',description:'创建可复用技能',timestamp:2}},{event:'tool',data:tool}]);
const messages=ref(projectHistoryMessages(events.value)), loading=ref(true), mount=ref(1), failing=ref(false), stale=ref(false), validation=ref(false);
const creationFailure=ref(false), failureLoading=ref(false), failureFollowups=[];
const failedMessages=ref(projectHistoryMessages([
 {event:'message',data:{role:'user',content:'/skill-creator 将本次流程保存为技能',timestamp:10}},
 {event:'tool',data:{tool_call_id:'failed-create',name:'skill',function:'skill_create_from_session',status:'called',execution_status:'failed',timestamp:11,args:{}}},
 {event:'message',data:{role:'assistant',content:'脚本生成未完成：脚本存在语法错误，无法解析。需要修正语法或补全脚本后重试。未创建技能，也未添加到技能库。\\n\\n已有分析结果保持不变。重试只重新整理技能，不会重跑原分析。',metadata:{skill_creation_failure:{stage:'implementation',code:'script_syntax_invalid',retryable:true}},timestamp:12}},
 {event:'done',data:{timestamp:13}},
]));
const app=createApp({render:()=>h('main',[
 h('section',{id:'owner'},[h(AnalysisConversation,{key:mount.value,messages:messages.value,isLoading:loading.value,messageKey:message=>message.content.timestamp,canResumeAnalysis:()=>false})]),
 creationFailure.value?h('section',{id:'creation-failure'},[h(AnalysisConversation,{messages:failedMessages.value,isLoading:failureLoading.value,messageKey:message=>message.content.timestamp,canResumeAnalysis:()=>false,onFollowUp:question=>{failureFollowups.push(question);failureLoading.value=true;}})]):null,
 h('section',{id:'shared'},[h(ToolUse,{tool:{...tool,skill_creation:{...draft,id:'cccccccccccccccccccccccccccccccc',description:'PRIVATE-DESCRIPTION',name:'PRIVATE-NAME'}},isShare:true})]),
 failing.value?h('section',{id:'failing'},[h(SkillCreationCard,{initial:{...draft,id:'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'}})]):null,
 stale.value?h('section',{id:'stale'},[h(SkillCreationCard,{initial:{...draft,id:'dddddddddddddddddddddddddddddddd'}})]):null,
 validation.value?h('section',{id:'validation'},syntheticDrafts.map(item=>h('section',{id:'validation-'+item.state,key:item.id},[h(SkillCreationCard,{initial:{...draft,id:item.id}})]))):null
])});
app.use(createI18n({legacy:false,locale:'zh',messages:{zh},missingWarn:false,fallbackWarn:false}));app.mount('#app');
window.harness={catalogEvents,notifyDeleted(){eventBus.emit(EVENT_SKILL_CATALOG_UPDATED,{deletedSkillId:'user-skill',deletedSkillName:draft.name});},creationFailure,failureFollowups,failureLoading,failedMessages,finish(){events.value.push({event:'step',data:{id:'create-step',status:'completed',description:'创建可复用技能',timestamp:4}},{event:'message',data:{role:'assistant',content:'技能已创建，请选择是否添加到技能库，也可以直接下载。',timestamp:5}},{event:'done',data:{timestamp:6}});messages.value=projectHistoryMessages(events.value);loading.value=false;},reload(){mount.value++;messages.value=projectHistoryMessages(events.value);},failing,stale,validation,unmount:()=>{eventBus.off(EVENT_SKILL_CATALOG_UPDATED,catalogUpdated);app.unmount();}};window.harnessReady=true;`;
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
const output = await mkdtemp(join(tmpdir(), 'datalab-skill-creation-'));
const report = { name:'skill-creation', browserEngine, passed:false, checks:[], errors:[], unexpectedRequests:[], requests:[] };
const browser = await launchBrowser();
const context = await browser.newContext({ viewport:{width:1000,height:1000}, acceptDownloads:true, serviceWorkers:'block' });
await context.addInitScript(() => localStorage.setItem('access_token','synthetic-browser-token'));
const page = await context.newPage();
page.setDefaultTimeout(10000);
page.on('pageerror', error => report.errors.push(error.message));
let installed = false, installCalls = 0, failInstall = true, failDownload = true, releaseStale;
await context.route('**/*', async route => {
 const request=route.request(), url=new URL(request.url());
 if(url.origin !== 'http://localhost:7001') { report.unexpectedRequests.push(url.href);return route.abort(); }
 if(url.pathname === '/__skill_creation__/') return route.fulfill({contentType:'text/html',body:'<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/__skill_creation__/project.css"><style>html,body{margin:0;min-height:100%;font-family:Arial,sans-serif}*{box-sizing:border-box}#app{padding:16px}main{max-width:820px;margin:auto;min-width:0}#shared,#failing,#stale{margin-top:30px}</style></head><body><div id="app"></div><script type="module" src="/__skill_creation__/entry.js"></script></body></html>'});
 if(url.pathname === '/__skill_creation__/entry.js') return route.fulfill({contentType:'application/javascript',body:Buffer.from(bundle.outputFiles[0].contents)});
 if(url.pathname === '/__skill_creation__/project.css') return route.fulfill({contentType:'text/css',body:projectCss});
 if(url.pathname === '/api/v1/visualizations') return route.fulfill({json:{code:0,data:{engine:'cordis',revision:'fixture',plugins:[]}}});
 const match=url.pathname.match(/^\/api\/v1\/skills\/drafts\/([^/]+)(?:\/(install|download))?$/);
 if(match){
  const [,id,action]=match;report.requests.push({id,action:action||'read',method:request.method(),authorization:request.headers().authorization});
  if(id==='cccccccccccccccccccccccccccccccc') { report.unexpectedRequests.push('shared draft API accessed');return route.abort(); }
  if(id==='dddddddddddddddddddddddddddddddd') await new Promise(resolve => {releaseStale=resolve;});
  if(action==='install') {
   installCalls++;
   if(id==='bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb' && failInstall){failInstall=false;return route.fulfill({status:503,json:{code:503,msg:'Synthetic temporary failure',data:null}});}
   await new Promise(resolve => setTimeout(resolve,150));
   if(id==='aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa') installed=true;
   return route.fulfill({json:{code:0,data:{...draft,id,installed:true,installed_skill_id:'user-skill'}}});
  }
  if(action==='download') {
   if(id==='bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb' && failDownload){failDownload=false;return route.fulfill({status:503,json:{code:503,msg:'Synthetic temporary failure',data:null}});}
   // A minimal ZIP signature is sufficient here: backend archive tests validate members.
   return route.fulfill({contentType:'application/zip',body:Buffer.from('PK\x05\x06'+'\x00'.repeat(18),'binary')});
  }
  const synthetic=syntheticDrafts.find(item=>item.id===id);
  return route.fulfill({json:{code:0,data:{...draft,id,validation:synthetic?.validation||draft.validation,installed:id==='aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' && installed,installed_skill_id:installed?'user-skill':null}}});
 }
 report.unexpectedRequests.push(`${request.method()} ${url.pathname}`);return route.abort();
});
try {
 await page.goto('http://localhost:7001/__skill_creation__/',{waitUntil:'domcontentloaded'});
 await page.waitForFunction(() => window.harnessReady);
 const owner=page.locator('#owner');
 const add=owner.getByRole('button',{name:'添加到我的技能',exact:true});
 await add.waitFor();await page.waitForFunction(() => !document.querySelector('#owner .skill-primary').disabled);
 assert.equal(installCalls,0,'Creating or reading the result must not install');
 assert.equal(await owner.getByRole('region',{name:'技能创建结果',exact:true}).count(),1);
 assert.equal(await owner.getByText('Creating skill',{exact:true}).count(),0,'Creation receipt must replace ordinary tool chip');
 assert.ok((await owner.innerText()).includes('尚未重新运行技能中的完整流程'));
 await owner.locator('summary').filter({hasText:'包含 3 个文件'}).click();
 assert.equal(await owner.getByText('scripts/render_and_qa.py',{exact:true}).count(),1);
 let downloadEvent=page.waitForEvent('download');await owner.getByRole('button',{name:'下载技能包',exact:true}).click();
 assert.equal((await downloadEvent).suggestedFilename(),'svg-english-to-chinese.zip');
 assert.equal(installCalls,0,'Downloading an uninstalled draft must not install');
 assert.deepEqual(await page.evaluate(()=>window.harness.catalogEvents),[],'Creating, reading and downloading do not publish installation changes');
 report.checks.push('Introduction, capability list, static checks, archive members and independent authenticated ZIP download');
 await page.evaluate(() => window.harness.finish());
 await owner.getByText('技能已创建，请选择是否添加到技能库，也可以直接下载。',{exact:true}).waitFor();
 assert.equal(await owner.getByRole('region',{name:'技能创建结果',exact:true}).count(),1,'Completion/collapsed details must retain one result card');
 const countBefore=report.requests.filter(item=>item.action==='read').length;
 const summary=owner.getByRole('button').filter({hasText:/完成思考|执行过程|用时|思考/}).first();
 if(await summary.count()){await summary.click();assert.equal(await owner.getByRole('region',{name:'技能创建结果',exact:true}).count(),1);await summary.click();}
 assert.equal(report.requests.filter(item=>item.action==='read').length,countBefore,'Collapsing history must not remount the persistent result');
 await add.focus();await page.keyboard.press('Enter');
 await page.waitForFunction(() => document.querySelector('#owner .skill-primary').disabled);
 assert.equal(await owner.locator('.skill-primary').isDisabled(),true);
 await owner.getByRole('button',{name:'已添加到我的技能',exact:true}).waitFor();
 assert.equal(installCalls,1,'Only the explicit add click installs, once');
 assert.deepEqual(await page.evaluate(()=>window.harness.catalogEvents),['EVENT_SKILL_CATALOG_UPDATED'],'Successful add refreshes the existing composer skill menu immediately');
 await page.evaluate(() => window.harness.reload());
 await owner.getByRole('button',{name:'已添加到我的技能',exact:true}).waitFor();
 assert.equal(installCalls,1,'Restoring a history page must only read installation state');
 assert.equal(await page.evaluate(()=>window.harness.catalogEvents.length),1,'History reads must not repeat catalog mutations');
 assert.equal(await owner.getByRole('region',{name:'技能创建结果',exact:true}).count(),1);
 downloadEvent=page.waitForEvent('download');await owner.getByRole('button',{name:'下载技能包',exact:true}).click();await downloadEvent;
 report.checks.push('Live completion, collapsed execution details, explicit installation, single POST and historical installation-state refresh');
 const shared=page.locator('#shared');assert.equal(await shared.getByRole('button').count(),0);
 assert.ok(!(await shared.innerText()).includes('PRIVATE'));
 assert.ok(report.requests.every(item=>item.id!=='cccccccccccccccccccccccccccccccc'));
 await page.evaluate(() => window.harness.failing.value=true);
 const failing=page.locator('#failing');await failing.getByRole('button',{name:'添加到我的技能',exact:true}).waitFor();
 await page.waitForFunction(() => !document.querySelector('#failing .skill-primary').disabled);
 await failing.getByRole('button',{name:'添加到我的技能',exact:true}).click();
 await failing.getByRole('alert').filter({hasText:'添加失败'}).waitFor();
 assert.equal(await page.evaluate(()=>window.harness.catalogEvents.length),1,'Failed installations must not signal a successful catalog change');
 assert.equal(await failing.getByRole('button',{name:'添加到我的技能',exact:true}).isDisabled(),false);
 await failing.getByRole('button',{name:'下载技能包',exact:true}).click();await failing.getByRole('alert').filter({hasText:'下载失败'}).waitFor();
 downloadEvent=page.waitForEvent('download');await failing.getByRole('button',{name:'下载技能包',exact:true}).click();await downloadEvent;
 await failing.getByRole('button',{name:'添加到我的技能',exact:true}).click();await failing.getByRole('button',{name:'已添加到我的技能',exact:true}).waitFor();
 assert.deepEqual(await page.evaluate(()=>window.harness.catalogEvents),['EVENT_SKILL_CATALOG_UPDATED','EVENT_SKILL_CATALOG_UPDATED'],'A successful retry publishes the standard refresh event once');
 report.checks.push('Share view reveals no draft metadata or actions; installation/download failure retries remain independent');
 const installsBeforeValidation=installCalls;
 await page.evaluate(()=>window.harness.validation.value=true);
 const passed=page.locator('#validation-passed'), unsupported=page.locator('#validation-unsupported');
 // Initial cards contain only legacy static fields. These verdicts must arrive
 // from the owner-scoped draft refresh, not be inferred from syntax success.
 await passed.getByText('3 个合成用例运行验证通过',{exact:true}).waitFor();
 await passed.getByText('验证使用小型合成数据；实际数据的完整分析尚未运行。',{exact:true}).waitFor();
 assert.equal(await passed.getByText('以上为静态检查，尚未重新运行技能中的完整流程。',{exact:true}).count(),0);
 await unsupported.getByText('此技能尚无适用的自动运行验证，首次使用前仍需检查合成用例。',{exact:true}).waitFor();
 assert.equal(await unsupported.getByText(/个合成用例运行验证通过/).count(),0,'Unsupported is not a zero-case success');
 assert.equal(await unsupported.getByText('验证使用小型合成数据；实际数据的完整分析尚未运行。',{exact:true}).count(),0,'Unsupported cannot claim tests ran');
 assert.ok((await unsupported.innerText()).includes('脚本语法检查通过'),'Static success stays distinct from unsupported execution');
 assert.equal(installCalls,installsBeforeValidation,'Displaying validation status must not install or run the skill');
 assert.ok(report.requests.filter(item=>syntheticDrafts.some(draft=>draft.id===item.id)).every(item=>item.action==='read' && item.method==='GET'));
 assert.deepEqual(await page.evaluate(()=>window.harness.catalogEvents),['EVENT_SKILL_CATALOG_UPDATED','EVENT_SKILL_CATALOG_UPDATED']);
 report.checks.push('Owner refresh distinguishes three passed synthetic cases from unsupported validation; neither claims real-data execution or starts installation');
 installed=false;
 const installsBeforeDeletion=installCalls;
 await page.evaluate(()=>window.harness.notifyDeleted());
 await owner.getByRole('button',{name:'添加到我的技能',exact:true}).waitFor();
 await page.waitForFunction(()=>!document.querySelector('#owner .skill-primary').disabled);
 assert.equal(installCalls,installsBeforeDeletion,'Deletion-state refresh must never re-add a deleted skill automatically');
 downloadEvent=page.waitForEvent('download');await owner.getByRole('button',{name:'下载技能包',exact:true}).click();await downloadEvent;
 await owner.getByRole('button',{name:'添加到我的技能',exact:true}).click();
 await owner.getByRole('button',{name:'已添加到我的技能',exact:true}).waitFor();
 assert.equal(installCalls,installsBeforeDeletion+1,'A deleted personal skill is restored only after an explicit add');
 report.checks.push('Deleted skill refreshes the mounted result card, remains downloadable, and requires an explicit click to add again');

 await page.evaluate(()=>window.harness.creationFailure.value=true);
 const creationFailure=page.locator('#creation-failure');
 await creationFailure.getByRole('button',{name:'重试创建技能',exact:true}).waitFor();
 assert.match(await creationFailure.innerText(), /脚本存在语法错误/);
 assert.doesNotMatch(await creationFailure.innerText(), /参数化或输入只读要求/);
 assert.ok((await creationFailure.innerText()).includes('已有分析结果保持不变'));
 assert.ok(!(await creationFailure.innerText()).includes('分析尚未完成'));
 assert.ok(!(await creationFailure.innerText()).includes('Task error'));
 assert.equal(await creationFailure.getByText('创建未完成',{exact:true}).count(),1);
 assert.equal(await creationFailure.getByRole('button',{name:'继续分析',exact:true}).count(),0);
 assert.deepEqual(await page.evaluate(()=>window.harness.failureFollowups),[],'Reading a failed creation must never retry');
 await page.setViewportSize({width:390,height:1000});
 await creationFailure.screenshot({path:join(output,'skill-creation-failure-390.png')});
 await creationFailure.getByRole('button',{name:'重试创建技能',exact:true}).click();
 assert.deepEqual(await page.evaluate(()=>window.harness.failureFollowups),['/skill-creator 将本次已完成的分析流程保存为可复用的技能']);
 assert.equal(await creationFailure.getByRole('button',{name:'重试创建技能',exact:true}).count(),0);
 await page.evaluate(()=>{window.harness.failedMessages.value.push({type:'user',content:{content:'一个新的分析',timestamp:14}});window.harness.failureLoading.value=false;});
 assert.equal(await creationFailure.getByRole('button',{name:'重试创建技能',exact:true}).count(),0,'Old creation failure cannot target the wrong new turn');
 report.checks.push('Creation failures preserve analysis results and expose one explicit current-turn creation retry, without automatic analysis replay');

 for(const width of [1000,390]){
  await page.setViewportSize({width,height:1000});
  const dimensions=await page.evaluate(()=>({pageWidth:document.documentElement.scrollWidth,cards:[...document.querySelectorAll('.skill-creation-card')].map(el=>({width:el.clientWidth,scroll:el.scrollWidth}))}));
  assert.ok(dimensions.pageWidth<=width+1);assert.ok(dimensions.cards.every(item=>item.scroll<=item.width+1));
  await owner.screenshot({path:join(output,`skill-created-${width}.png`)});
  await page.locator('#validation').screenshot({path:join(output,`skill-validation-${width}.png`)});
  report.checks.push({width,noHorizontalOverflow:true});
 }
 await page.evaluate(() => window.harness.stale.value=true);
 await page.waitForFunction(()=>document.querySelector('#stale .skill-creation-card'));
 while(!releaseStale) await new Promise(resolve=>setTimeout(resolve,10));
 await page.evaluate(() => window.harness.stale.value=false);releaseStale();
 assert.equal(await page.locator('#stale').count(),0);
 assert.deepEqual(report.errors,[]);assert.deepEqual(report.unexpectedRequests,[]);
 assert.ok(report.requests.every(item=>item.authorization==='Bearer synthetic-browser-token'));
 await page.evaluate(() => window.harness.unmount());report.passed=true;
} catch(error) {
 report.failure=error.message;report.stack=error.stack;await page.screenshot({path:join(output,'failure.png'),fullPage:true}).catch(()=>{});process.exitCode=1;
} finally {await context.close();await browser.close();await writeFile(join(output,'results.json'),JSON.stringify(report,null,2));console.log(JSON.stringify(report));console.log(`BROWSER_REPORT=${output}`);}
