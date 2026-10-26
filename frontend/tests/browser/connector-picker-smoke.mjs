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

const frontend = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const require = createRequire(resolve(frontend, 'package.json'));
let playwrightEntry = process.env.VISUALIZATION_PLAYWRIGHT_MODULE;
if (!playwrightEntry) {
  try { playwrightEntry = require.resolve('playwright'); }
  catch { playwrightEntry = join(homedir(), '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright/index.mjs'); }
}
const { chromium } = await import(pathToFileURL(resolve(playwrightEntry)).href);
registerTS(() => ts);
const entry = `import {createApp,h,ref} from 'vue';
import {createRouter,createMemoryHistory,RouterView} from 'vue-router';
import Picker from './src/components/DataConnectorPicker.vue';
import Management from './src/components/DataConnectorsPanel.vue';
const open=ref(false),selected=ref([]),disabled=ref(false);
window.harness={open,selected,disabled};
const Home={render:()=>h('main',{class:'p-8 space-y-6'},[
 h('button',{onClick:()=>open.value=true},'连接器'),
 h(Picker,{open:open.value,'onUpdate:open':v=>open.value=v,selected:selected.value,'onUpdate:selected':v=>selected.value=v,disabled:disabled.value}),
 h('label',[h('input',{type:'checkbox',id:'native-checkbox'}),'普通复选框']),
 h('label',[h('input',{type:'radio',id:'native-radio'}),'普通单选框']),
 h('a',{href:'#test',id:'native-link'},'普通链接'),
 h('label',{class:'control-shell rounded border p-2'},[h('input',{id:'compound-text',placeholder:'文本控件'})])
])};
const router=createRouter({history:createMemoryHistory(),routes:[{path:'/',component:Home},{path:'/connectors',component:Management}]});
const app=createApp({render:()=>h(RouterView)});app.use(router);app.mount('#app');window.harnessReady=true;`;
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
const output = await mkdtemp(join(tmpdir(), 'datalab-connector-picker-'));
const report = { passed: false, errors: [], externalAttempts: [], checks: {} };
const chrome = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const browser = await chromium.launch({ executablePath: process.env.VISUALIZATION_BROWSER_EXECUTABLE || (existsSync(chrome) ? chrome : undefined), headless: true, args: ['--disable-background-networking', '--disable-component-update'] });
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
const page = await context.newPage(); page.setDefaultTimeout(10000);
page.on('pageerror', error => report.errors.push(error.message));
const names = [ ['ncbi-datasets-v2','NCBI Datasets'], ['ncbi-geo','NCBI GEO'], ['ncbi-sra','NCBI SRA'], ['zenodo','Zenodo'], ['datacite','DataCite'], ['worldbank','World Bank'], ['harvard-dataverse','Harvard Dataverse'], ['openml','OpenML'], ['borealis','Borealis'], ['dataverse-nl','DataverseNL'], ['recherche-data-gouv','Recherche Data Gouv'], ['pangaea','PANGAEA'], ['gbif','GBIF'], ['sciencedb','ScienceDB 科学数据银行'], ['unknown','未来来源'] ];
const foundationSources = [['oeis','OEIS'],['miplib','MIPLIB'],['gwosc','GWOSC'],['nasa-cdaweb','NASA CDAWeb'],['crystallography-open-database','Crystallography Open Database'],['nist-thermoml','NIST ThermoML'],['nomad','NOMAD'],['cds-vizier','VizieR'],['mast','MAST']];
names.push(...foundationSources);
// Exercise all bundled batch marks, including adapters pending activation.
const provenance=JSON.parse(await readFile(resolve(frontend,'public/images/connectors/sources.json'),'utf8'));
for(const asset of provenance.assets.filter(item=>item.file.startsWith('batch30/') || item.file.startsWith('expansion-next/') || item.file.startsWith('oct22/') || item.file.startsWith('oct23/') || item.file.startsWith('foundation/'))){
 if(!names.some(([id])=>id===asset.source_id))names.push([asset.source_id,asset.source_id]);
}
const neutralSources = [['asu-research-data','ASU Research Data'],['natural-earth','Natural Earth'],['metabolomics-workbench','Metabolomics Workbench'],['who-gho','WHO GHO'],['oeis','OEIS'],['miplib','MIPLIB']];
for(const entry of neutralSources)if(!names.some(([id])=>id===entry[0]))names.push(entry);
const catalog=names.map(([id,title])=>({id,title,description:`${title} 的管理说明`,limitations:['来源的详细能力说明'],operations:[{name:'search',title:'检索数据',description:'接口说明',input_schema:{}}],auth:'none',domains:['example.org']}));
await context.route('**/*', async route => {
 const url=new URL(route.request().url());
 if(url.origin!=='http://localhost:7001'){report.externalAttempts.push(url.href);return route.abort();}
 if(url.pathname==='/__connectors__/')return route.fulfill({contentType:'text/html',body:'<!doctype html><html><head><meta charset="utf-8"><link rel="stylesheet" href="/__connectors__/project.css"></head><body><div id="app"></div><script type="module" src="/__connectors__/entry.js"></script></body></html>'});
 if(url.pathname==='/__connectors__/entry.js')return route.fulfill({contentType:'application/javascript',body:Buffer.from(bundle.outputFiles[0].contents)});
 if(url.pathname==='/__connectors__/project.css')return route.fulfill({contentType:'text/css',body:css});
 if(url.pathname==='/api/v1/data-connectors')return route.fulfill({contentType:'application/json',body:JSON.stringify({code:0,msg:'success',data:catalog})});
 if(/^\/images\/connectors\/(?:(?:batch30|expansion-next|oct22|oct23|foundation)\/)?[a-z0-9-]+\.(svg|png|ico|jpg)$/.test(url.pathname)){
   const filename=resolve(frontend,'public',url.pathname.slice(1));
   if(existsSync(filename))return route.fulfill({body:await readFile(filename),contentType:url.pathname.endsWith('.svg')?'image/svg+xml':url.pathname.endsWith('.png')?'image/png':url.pathname.endsWith('.jpg')?'image/jpeg':'image/x-icon'});
   return route.fulfill({status:404,body:''});
 }
 if(url.pathname==='/favicon.ico')return route.fulfill({status:204,body:''});
 throw new Error(`Unexpected request: ${url.pathname}`);
});
const focusStyle=async locator=>locator.evaluate(el=>{const s=getComputedStyle(el);return {color:s.outlineColor,width:s.outlineWidth,style:s.outlineStyle,accent:s.accentColor,shadow:s.boxShadow};});
try{
 await page.goto('http://localhost:7001/__connectors__/');
 await page.getByRole('button',{name:'连接器',exact:true}).click();
 const dialog=page.getByRole('dialog');
 await dialog.getByRole('checkbox',{name:'NCBI Datasets',exact:true}).waitFor();
 assert.equal(await dialog.getByRole('checkbox').count(),names.length);
 assert.equal(await dialog.locator('.connector-description').count(),names.length);
 assert.equal(await dialog.getByText(/详细能力说明|检索数据/).count(),0);
 const description = dialog.locator('.connector-description').first();
 assert.equal(await description.textContent(),catalog[0].description);
 assert.equal(await description.getAttribute('title'),catalog[0].description);
 assert.equal(await description.evaluate(el=>getComputedStyle(el).whiteSpace),'nowrap');
 assert.equal(await dialog.getByRole('checkbox',{name:'NCBI Datasets',exact:true}).getAttribute('aria-description'),catalog[0].description);
 await dialog.getByText(/不选择时，系统自动匹配相关来源/).waitFor();
 assert.ok(await dialog.getAttribute('aria-describedby'));
 await page.waitForFunction(()=>[...document.querySelectorAll('.connector-logo img')].every(img=>img.complete&&img.naturalWidth>0));
 const loadedLogos=await dialog.locator('.connector-logo img').count();
 assert.equal(loadedLogos,names.length-1-neutralSources.length,'Verified official marks load locally; documented exceptions use neutral icons');
 for(const [,title] of neutralSources){
  const row=dialog.locator('.connector-option').filter({has:page.getByRole('checkbox',{name:title,exact:true})});
  assert.equal(await row.locator('img').count(),0,`${title} uses a neutral fallback`);
  assert.equal(await row.locator('.connector-logo svg').count(),1);
 }
 report.checks.neutralSources=neutralSources.map(([id])=>id);
 for(const [id,title] of foundationSources){
  const row=dialog.locator('.connector-option').filter({has:page.getByRole('checkbox',{name:title,exact:true})});
  assert.equal(await row.count(),1,`${id} has its source name`);
  assert.equal(await row.locator('img').count(),['oeis','miplib'].includes(id)?0:1,`${id} uses only verified branding`);
 }
 assert.equal(await dialog.getByRole('checkbox',{name:'LMFDB',exact:true}).count(),0);
 report.checks.foundationSources={names:foundationSources.map(([id])=>id),officialLogos:7,neutralFallbacks:['oeis','miplib'],lmfdbEnabled:false};
 await dialog.getByRole('checkbox',{name:'GWOSC',exact:true}).scrollIntoViewIfNeeded();
 await dialog.screenshot({path:join(output,'foundation-sources.png')});
 await dialog.getByRole('checkbox',{name:'tudo-data',exact:true}).scrollIntoViewIfNeeded();
 await dialog.screenshot({path:join(output,'new-sources.png')});
 await dialog.getByRole('checkbox',{name:'WHO GHO',exact:true}).scrollIntoViewIfNeeded();
 await dialog.screenshot({path:join(output,'neutral-fallbacks.png')});
 await dialog.getByRole('checkbox',{name:'NCBI Datasets',exact:true}).scrollIntoViewIfNeeded();
 assert.equal(await dialog.locator('.connector-option').filter({hasText:'未来来源'}).locator('img').count(),0);
 report.checks.compactSources={choices:names.length,loadedLogos,unknownSource:'neutral fallback icon'};
 for(const name of ['darus','uruguay-open-data']){
  const mark=dialog.locator('.connector-option').filter({hasText:name}).locator('.connector-logo');
  assert.equal(await mark.evaluate(el=>getComputedStyle(el).backgroundColor),'rgb(38, 59, 52)');
 }
 const france=dialog.locator('.connector-option').filter({hasText:'Recherche Data Gouv'});
 const franceLogo=await france.locator('img').evaluate(img=>({width:img.naturalWidth,height:img.naturalHeight}));
 assert.ok(franceLogo.width/franceLogo.height>=.8 && franceLogo.width/franceLogo.height<=1.25,'France uses the official square site icon, not an illegible reduced wordmark');
 await france.screenshot({path:join(output,'france-logo.png')});
 report.checks.franceLogo=franceLogo;
 const ncbi=dialog.getByRole('checkbox',{name:'NCBI Datasets',exact:true});
 await ncbi.focus(); await page.keyboard.press('Space');
 assert.equal(await ncbi.isChecked(),true);
 assert.deepEqual(await page.evaluate(()=>window.harness.selected.value),['ncbi-datasets-v2']);
 const light=await focusStyle(ncbi);assert.equal(light.color,'rgb(43, 118, 89)');assert.equal(light.width,'2px');assert.equal(light.style,'solid');assert.equal(light.accent,'rgb(43, 118, 89)');assert.equal(light.shadow,'none');
 report.checks.lightFocus=light;
 await dialog.screenshot({path:join(output,'desktop.png')});
 await dialog.getByRole('searchbox').fill('Zenodo');
 assert.equal(await dialog.getByRole('checkbox').count(),1);
 await dialog.getByRole('checkbox',{name:'Zenodo',exact:true}).check();
 await dialog.getByRole('searchbox').fill('没有这个来源');
 await dialog.getByText('没有匹配的数据来源。').waitFor();
 assert.deepEqual(await page.evaluate(()=>window.harness.selected.value),['ncbi-datasets-v2','zenodo']);
 await dialog.getByRole('searchbox').fill('');
 await page.evaluate(()=>window.harness.disabled.value=true);
 assert.equal(await ncbi.isDisabled(),true);assert.equal(await dialog.getByRole('button',{name:'清空选择'}).isDisabled(),true);
 await page.evaluate(()=>window.harness.disabled.value=false);
 await dialog.getByRole('button',{name:'清空选择'}).click();
 assert.deepEqual(await page.evaluate(()=>window.harness.selected.value),[]);
 report.checks.selection='keyboard and pointer selection, search preserves selection, disabled controls, clear restores automatic matching';
 await page.evaluate(()=>document.documentElement.classList.add('dark'));
 await page.keyboard.press('Tab'); await ncbi.focus();
 const dark=await focusStyle(ncbi);assert.equal(dark.color,'rgb(169, 203, 187)');assert.equal(dark.width,'2px');assert.equal(dark.accent,'rgb(169, 203, 187)');report.checks.darkFocus=dark;
 await page.waitForFunction(()=>getComputedStyle(document.querySelector('.connector-option')).backgroundColor==='rgb(56, 55, 57)' && getComputedStyle(document.querySelector('.connector-logo')).backgroundColor==='rgb(255, 255, 255)');
 await dialog.screenshot({path:join(output,'dark.png')});
 await page.evaluate(()=>document.documentElement.classList.remove('dark'));
 await page.waitForFunction(()=>getComputedStyle(document.querySelector('.connector-option')).backgroundColor==='rgb(255, 255, 255)');
 for(const width of [390,320]){
  await page.setViewportSize({width,height:760});
  const d=await dialog.evaluate(el=>({width:el.getBoundingClientRect().width,scroll:el.scrollWidth,client:el.clientWidth,viewport:innerWidth,columns:getComputedStyle(el.querySelector('.connector-options')).gridTemplateColumns}));
  assert.ok(d.width<=width);assert.ok(d.scroll<=d.client);assert.ok(!d.columns.includes(' '));report.checks[`mobile${width}`]=d;
  await dialog.screenshot({path:join(output,`mobile-${width}.png`)});
 }
 await dialog.getByRole('button',{name:'完成',exact:true}).click();
 await dialog.waitFor({state:'detached'});
 await page.keyboard.press('Tab');
 for(const id of ['native-checkbox','native-radio','native-link']){
  const control=page.locator(`#${id}`);await control.focus();const style=await focusStyle(control);assert.equal(style.color,'rgb(43, 118, 89)');assert.equal(style.width,'2px');
 }
 await page.locator('#compound-text').focus();
 const shell=page.locator('.control-shell');const shellStyle=await focusStyle(shell);assert.equal(shellStyle.width,'1px');assert.equal(shellStyle.color,'rgb(43, 118, 89)');assert.equal((await focusStyle(page.locator('#compound-text'))).style,'none');
 report.checks.globalControls='native checkbox/radio/link retain green keyboard outlines; compound text has one inset focus edge';
 await page.getByRole('button',{name:'连接器',exact:true}).click();
 await page.getByRole('dialog').getByRole('link',{name:'查看数据连接器'}).click();
 await page.getByText('NCBI Datasets 的管理说明',{exact:true}).waitFor();
 assert.equal(await page.getByText('来源的详细能力说明',{exact:true}).count(),names.length);
 report.checks.management='details link navigates to unchanged management descriptions and capabilities';
 assert.deepEqual(report.externalAttempts,[]);assert.deepEqual(report.errors,[]);report.passed=true;
}finally{
 await writeFile(join(output,'report.json'),JSON.stringify(report,null,2));
 console.log(JSON.stringify({output,...report},null,2));await browser.close();
}
