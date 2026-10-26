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
const entry = `import {createApp,h,ref} from 'vue';
import {createI18n} from 'vue-i18n';
import {createRouter,createMemoryHistory} from 'vue-router';
import PluginsPage from ${JSON.stringify(resolve(frontend,'src/pages/PluginsPage.vue'))};
import SkillSettings from ${JSON.stringify(resolve(frontend,'src/components/settings/SkillSettings.vue'))};
import {Dialog,DialogContent} from ${JSON.stringify(resolve(frontend,'src/components/ui/dialog/index.ts'))};
import CustomDialog from ${JSON.stringify(resolve(frontend,'src/components/ui/CustomDialog.vue'))};
import zh from ${JSON.stringify(resolve(frontend,'src/locales/zh.ts'))};
const mode=ref('catalog');
const router=createRouter({history:createMemoryHistory(),routes:[{path:'/',component:{render:()=>null},meta:{catalogType:'skills'}}]});
const app=createApp({render:()=>h('main',[mode.value==='catalog'?h(PluginsPage):h(Dialog,{open:true},{default:()=>h(DialogContent,{class:'w-[min(900px,calc(100vw-32px))]'},{default:()=>h(SkillSettings)})}),h(CustomDialog)])});
app.use(router);app.use(createI18n({legacy:false,locale:'zh',messages:{zh},missingWarn:false,fallbackWarn:false}));
await router.push('/');await router.isReady();app.mount('#app');window.harness={mode};window.harnessReady=true;`;
const offScopeTools = new Set(['DataConnectorsPanel.vue', 'DeclarativeToolCard.vue', 'ToolApprovalCard.vue', 'MonacoEditor.vue', 'ToolPanel.vue', 'BrowserToolView.vue', 'FileToolView.vue']);
const bundle = await build({
  stdin: { contents: entry, sourcefile: 'followup-outcome-entry.js', resolveDir: frontend },
  bundle: true, write: false, format: 'esm', platform: 'browser', alias: { '@': resolve(frontend, 'src') }, target: 'es2022', logLevel: 'warning',
  define: {
    'import.meta.env': JSON.stringify({ BASE_URL: '/', MODE: 'test', DEV: false, PROD: true }),
    '__VUE_OPTIONS_API__': 'true', '__VUE_PROD_DEVTOOLS__': 'false',
    '__VUE_PROD_HYDRATION_MISMATCH_DETAILS__': 'false', 'process.env.NODE_ENV': '"production"',
  },
  plugins: [{ name: 'real-followup-outcome-sfcs', setup(builder) {
    builder.onLoad({filter:/\/composables\/useAuth\.ts$/},()=>({contents:`import {ref} from 'vue'; const currentUser=ref({id:'owner-a'}); export const useAuth=()=>({currentUser});`,loader:'ts',resolveDir:frontend}));
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
const output=await mkdtemp(join(tmpdir(),'datalab-personal-skill-delete-'));
const report={name:'personal-skill-delete',browserEngine,passed:false,checks:[],errors:[],unexpectedRequests:[],deleteCalls:[]};
const makeSkill=(id,extra={})=>({id,name:id,title:id,description:'科研数据分析技能',source:'personal',scope:'user',owner_user_id:'owner-a',installed:true,triggers:[],...extra});
let skills=[makeSkill('my-analysis'),makeSkill('my-shared',{scope:'global'}),makeSkill('shared-by-other',{scope:'global',owner_user_id:'owner-b'}),makeSkill('official-analysis',{source:'official',owner_user_id:null})];
let failNext=true;
const browser=await launchBrowser();const context=await browser.newContext({viewport:{width:390,height:1000},serviceWorkers:'block'});
await context.addInitScript(()=>localStorage.setItem('access_token','synthetic-browser-token'));
const page=await context.newPage();page.setDefaultTimeout(10000);page.on('pageerror',error=>report.errors.push(error.message));
await page.route('**/*',async route=>{
 const request=route.request(),url=new URL(request.url());
 if(url.origin!=='http://localhost:7001'){report.unexpectedRequests.push(url.href);return route.abort();}
 if(url.pathname==='/__delete__/')return route.fulfill({contentType:'text/html',body:'<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/__delete__/project.css"><style>html,body,#app{margin:0;height:100%;font-family:Arial,sans-serif}*{box-sizing:border-box}main{height:100%;min-width:0}</style></head><body><div id="app"></div><script type="module" src="/ __delete__/entry.js"></script></body></html>'.replace('/ __delete__','/__delete__')});
 if(url.pathname==='/__delete__/entry.js')return route.fulfill({contentType:'application/javascript',body:Buffer.from(bundle.outputFiles[0].contents)});
 if(url.pathname==='/__delete__/project.css')return route.fulfill({contentType:'text/css',body:projectCss});
 const ok=data=>route.fulfill({json:{code:0,data}});
 if(['/api/v1/skills','/api/v1/skills/catalog'].includes(url.pathname))return ok({skills});
 if(url.pathname==='/api/v1/skills/preferences')return ok({auto_enabled_skills:skills.filter(s=>s.owner_user_id==='owner-a').map(s=>s.name)});
 const match=url.pathname.match(/^\/api\/v1\/skills\/([^/]+)$/);
 if(match){
  const id=decodeURIComponent(match[1]),skill=skills.find(s=>s.id===id);
  if(request.method()==='DELETE'){
   report.deleteCalls.push(id);await new Promise(resolve=>setTimeout(resolve,180));
   if(failNext){failNext=false;return route.fulfill({status:503,json:{code:503,msg:'Synthetic failure',data:null}});}
   skills=skills.filter(s=>s.id!==id);return ok({});
  }
  if(skill)return ok({skill,tree:[{name:'SKILL.md',path:'SKILL.md',type:'file'}],files:[{path:'SKILL.md',content:'# Scientific skill',binary:false}]});
 }
 report.unexpectedRequests.push(`${request.method()} ${url.pathname}`);return route.abort();
});
try{
 await page.goto('http://localhost:7001/__delete__/',{waitUntil:'domcontentloaded'});await page.waitForFunction(()=>window.harnessReady);
 const mine=page.getByRole('button',{name:'删除技能：my-analysis',exact:true});await mine.waitFor();
 assert.equal(await page.getByRole('button',{name:/删除技能：/}).count(),2,'Only owned personal skills expose delete');
 assert.equal(await page.getByRole('button',{name:'删除技能：shared-by-other',exact:true}).count(),0);
 await mine.click();let confirm=page.getByRole('dialog').filter({hasText:'删除这个个人技能？'});await confirm.waitFor();
 assert.ok((await confirm.innerText()).includes('my-analysis'));assert.deepEqual(report.deleteCalls,[]);
 await page.screenshot({path:join(output,'personal-skill-delete-confirm-390.png'),fullPage:true});
 await confirm.getByRole('button',{name:'取消',exact:true}).click();assert.deepEqual(report.deleteCalls,[]);
 await mine.click();await confirm.getByRole('button',{name:'删除',exact:true}).click();await confirm.getByRole('alert').waitFor();assert.equal(await page.locator('button[aria-label="删除技能：my-analysis"]').count(),1,'API failure retains the skill');
 await confirm.getByRole('button',{name:'删除',exact:true}).click();await confirm.waitFor({state:'hidden'});await mine.waitFor({state:'hidden'});
 assert.deepEqual(report.deleteCalls,['my-analysis','my-analysis']);
 report.checks.push('Owner-only delete controls, named confirmation/cancel, failed deletion preserves the row, success removes it');
 await page.getByText('my-shared',{exact:true}).click();
 const detail=page.getByRole('dialog').filter({has:page.getByRole('heading',{name:'my-shared',exact:true})});await detail.waitFor();
 await detail.getByRole('button',{name:'删除技能：my-shared',exact:true}).click();
 await confirm.getByRole('button',{name:'删除',exact:true}).click();await confirm.waitFor({state:'hidden'});await detail.waitFor({state:'hidden'});
 assert.ok(!skills.some(s=>s.id==='my-shared'));
 report.checks.push('Detail deletion removes an owned globally visible skill and closes stale detail/editor state');
 skills.push(makeSkill('settings-owned'));
 await page.evaluate(()=>window.harness.mode.value='settings');
 const settingDelete=page.getByRole('button',{name:'删除技能：settings-owned',exact:true});await settingDelete.waitFor();
 await page.screenshot({path:join(output,'personal-skill-delete-settings-390.png'),fullPage:true});
 await settingDelete.click();await confirm.waitFor();await page.screenshot({path:join(output,'personal-skill-delete-settings-confirm-390.png'),fullPage:true});await confirm.getByRole('button',{name:'删除',exact:true}).click();await confirm.waitFor({state:'hidden'});await settingDelete.waitFor({state:'hidden'});
 assert.equal(await page.getByRole('switch',{name:'自动启用技能 settings-owned'}).count(),0);
 assert.equal(await page.getByRole('button',{name:/删除技能：/}).count(),0);
 report.checks.push('Settings deletes an owned skill and removes its automatic-use control');
 for(const width of [390,1000]){
  await page.setViewportSize({width,height:1000});
  assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
 }
 assert.deepEqual(report.errors,[]);assert.deepEqual(report.unexpectedRequests,[]);report.passed=true;
}catch(error){report.failure=error.message;report.stack=error.stack;await page.screenshot({path:join(output,'failure.png'),fullPage:true}).catch(()=>{});process.exitCode=1;}
finally{await context.close();await browser.close();await writeFile(join(output,'results.json'),JSON.stringify(report,null,2));console.log(JSON.stringify(report));console.log(`BROWSER_REPORT=${output}`);}
