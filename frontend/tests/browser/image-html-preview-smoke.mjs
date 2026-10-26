/** Real image/HTML SFCs, synthetic authorized resources, no production services. */
import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { compileScript, parse } from '@vue/compiler-sfc';
import { readFile } from 'node:fs/promises';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { launchBrowser, browserEngine } from './runtime.mjs';
const frontend=resolve(dirname(fileURLToPath(import.meta.url)),'../..');
const png='iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGPQCOgBAAGoAQW7cALPAAAAAElFTkSuQmCC';
const entry=`import {createApp,h,ref} from 'vue';
import ImagePreview from './src/components/filePreviews/ImageFilePreview.vue';
import HtmlPreview from './src/components/filePreviews/HtmlFilePreview.vue';
const current=ref({file_id:'image',filename:'image.png'}),mode=ref('image');
const png=Uint8Array.from(atob(${JSON.stringify(png)}),c=>c.charCodeAt(0)).buffer;
window.previewMessages=[];window.addEventListener('message',e=>{if(e.data?.previewProbe)window.previewMessages.push(e.data);});
window.previewHarness={png,html:'',fail:false,reads:0,resources:new Map(),files:[]};
window.previewHarness.resources.set('app.js',new TextEncoder().encode('document.querySelector("#relative").textContent="relative script ready";').buffer);
window.previewHarness.resources.set('main.css',new TextEncoder().encode('#relative{color:rgb(20,40,60)}').buffer);
window.previewHarness.resources.set('plot.png',png);
window.previewHarness.files=[...window.previewHarness.resources].map(([name,bytes])=>({file_id:name,filename:name,size:bytes.byteLength}));
const plugin={enabled:true,id:'test',contract_version:2,capabilities:{operations:['bytes']},limits:{max_input_bytes:33554432,max_output_bytes:16777216}};
const app=createApp({render:()=>h(mode.value==='image'?ImagePreview:HtmlPreview,{file:current.value,plugin})});app.mount('#app');
window.showImage=(kind)=>{mode.value='image';window.previewHarness.kind=kind;current.value={file_id:'image-'+Math.random(),filename:'image.png'};};
window.showHtml=html=>{mode.value='html';window.previewHarness.html=html;current.value={file_id:'report-'+Math.random(),filename:'report.html'};};
window.ready=true;`;
const stub=`import {ref} from 'vue';
export const useRoute=()=>({params:{sessionId:'synthetic'},path:'/chat/synthetic'});
export const useFilePanel=()=>({relatedFiles:ref(window.previewHarness.files)});
export const useSessionFileList=()=>({shared:ref(false)});
export const getSessionFiles=async()=>window.previewHarness.files;
export const getSharedSessionFiles=getSessionFiles;
export const loadPluginText=async()=>window.previewHarness.html;
export const loadPluginBytes=async(file,plugin,signal,resource)=>{
 const s=window.previewHarness;s.reads++;if(signal.aborted)throw new DOMException('cancelled','AbortError');
 if(s.fail){s.fail=false;throw new Error('synthetic transport failure');}
 if(resource)return s.resources.get(resource.file_id);
 if(s.kind==='giant'){const b=s.png.slice(0);new DataView(b).setUint32(16,16000);new DataView(b).setUint32(20,16000);return b;}
 if(s.kind==='broken'){const b=new Uint8Array(45);b.set(new Uint8Array(s.png,0,33));b.set([0,0,0,0,73,69,78,68,0,0,0,0],33);return b.buffer;}
 return s.png;
};`;
const bundle=await build({stdin:{contents:entry,resolveDir:frontend,sourcefile:'image-html-entry.js'},bundle:true,write:false,format:'esm',platform:'browser',target:'es2022',logLevel:'warning',define:{'process.env.NODE_ENV':'"production"','__VUE_OPTIONS_API__':'true','__VUE_PROD_DEVTOOLS__':'false','__VUE_PROD_HYDRATION_MISMATCH_DETAILS__':'false'},plugins:[{name:'preview-harness',setup(b){
 b.onResolve({filter:/^(?:vue-router|.*\/(?:api\/agent|composables\/useFilePanel|composables\/useSessionFileList|visualizations\/runtime))$/},()=>({path:'preview-dependencies',namespace:'synthetic'}));
 b.onLoad({filter:/.*/,namespace:'synthetic'},()=>({contents:stub,loader:'js',resolveDir:frontend}));
 b.onLoad({filter:/\.vue$/},async({path})=>{const {descriptor,errors}=parse(await readFile(path,'utf8'),{filename:path});if(errors.length)throw errors[0];return{contents:compileScript(descriptor,{id:path,inlineTemplate:true}).content,loader:'ts',resolveDir:dirname(path)};});
}}]});
const browser=await launchBrowser();const context=await browser.newContext();const page=await context.newPage();page.setDefaultTimeout(8000);
const external=[],errors=[],consoleErrors=[];page.on('console',msg=>{if(msg.type()==='error')consoleErrors.push(msg.text());});page.on('pageerror',e=>errors.push(e.message));
await context.route('**/*',async route=>{const url=new URL(route.request().url());if(['blob:','data:'].includes(url.protocol))return route.continue();if(url.origin!=='http://preview.test'){external.push(url.href);return route.abort();}if(url.pathname==='/entry.js')return route.fulfill({contentType:'application/javascript',body:Buffer.from(bundle.outputFiles[0].contents)});return route.fulfill({contentType:'text/html',body:'<!doctype html><style>html,body,#app{height:100%;margin:0}#app>div{height:100%;display:flex;flex-direction:column}iframe{width:100%;height:600px}</style><div id="app"></div><script type="module" src="/entry.js"></script>'});});
try{
 await page.goto('http://preview.test/');await page.waitForFunction(()=>window.ready);await page.waitForFunction(()=>document.querySelector('#app img')?.naturalWidth===1);
 await page.evaluate(()=>window.showImage('giant'));await page.getByRole('alert').filter({hasText:'安全解码上限'}).waitFor();assert.equal(await page.locator('#app img').count(),0);
 await page.evaluate(()=>window.showImage('broken'));await page.getByRole('alert').filter({hasText:'浏览器未能解码'}).waitFor();
 await page.evaluate(()=>{window.previewHarness.kind='normal';});await page.getByRole('button',{name:'重试',exact:true}).click();await page.waitForFunction(()=>document.querySelector('#app img')?.naturalWidth===1);
 await page.evaluate(()=>{window.previewHarness.fail=true;window.showImage('normal');});await page.getByRole('alert').filter({hasText:'图片加载失败'}).waitFor();await page.getByRole('button',{name:'重试',exact:true}).click();await page.waitForFunction(()=>document.querySelector('#app img')?.naturalWidth===1);
 const html=`<base href="https://external.invalid/"><meta http-equiv="refresh" content="0;url=https://external.invalid/refresh"><meta http-equiv="Content-Security-Policy" content="default-src *"><link rel="stylesheet" href="main.css"><div id="relative">waiting</div><script src="app.js"></script><button id="run" onclick="this.textContent='clicked'">Run</button><img id="plot" src="plot.png"><img id="inline" src="data:image/png;base64,${png}"><img src="https://external.invalid/image"><script src="https://external.invalid/script"></script><form action="https://external.invalid/form"><input value="data"><button id="form">submit</button></form><a id="external" href="https://external.invalid/link" target="_blank">external</a><button id="navigate" onclick="location.href='https://external.invalid/navigation'">Navigate</button><script>fetch('https://external.invalid/fetch').catch(()=>{});new Image().src='https://external.invalid/dynamic-image';window.open('https://external.invalid/popup');</script>`;
 const probe=`<script>function probe(kind){const names=['relative','plot','inline','run','navigate'];const data={previewProbe:true,kind};for(const name of names){const el=document.getElementById(name),r=el.getBoundingClientRect();data[name]={text:el.textContent,width:el.naturalWidth,color:getComputedStyle(el).color,x:r.x+r.width/2,y:r.y+r.height/2};}try{parent.document.body;data.isolated=false;}catch{data.isolated=true;}data.forms=document.querySelectorAll('form').length;data.href=document.getElementById('external').getAttribute('href');top.postMessage(data,'*');}addEventListener('load',()=>probe('ready'));document.addEventListener('click',()=>probe('click'));<\/script>`;
 await page.evaluate(html=>window.showHtml(html),html+probe);
 await page.waitForFunction(()=>window.previewMessages.some(m=>m.kind==='ready'));
 const state=await page.evaluate(()=>window.previewMessages.find(m=>m.kind==='ready'));
 assert.equal(state.relative.text,'relative script ready');assert.equal(state.relative.color,'rgb(20, 40, 60)');
 assert.equal(state.plot.width,1);assert.equal(state.inline.width,1);assert.equal(state.forms,0);assert.equal(state.href,null);assert.equal(state.isolated,true);
 const box=await page.locator('iframe[title="隔离 HTML 预览"]').boundingBox();assert.ok(box);
 await page.mouse.click(box.x+state.run.x,box.y+state.run.y);
 await page.waitForFunction(()=>window.previewMessages.some(m=>m.kind==='click'&&m.run.text==='clicked'));
 await page.mouse.click(box.x+state.navigate.x,box.y+state.navigate.y);
 await page.waitForTimeout(150);assert.deepEqual(external,[],'CSP and parent frame policy must block all external requests, including script navigation');assert.equal(context.pages().length,1);
 assert.deepEqual(errors,[]);console.log(JSON.stringify({engine:browserEngine,passed:true,checks:['image pixel budget','image decode error retry','image transport retry','relative JS/CSS/image','embedded PNG','inline interaction','opaque origin','external resource/fetch/popup/navigation blocked']},null,2));
}catch(error){console.error(JSON.stringify({frames:page.frames().map(f=>f.url()),body:await page.locator('body').innerText(),errors,consoleErrors,external},null,2));throw error;}finally{await browser.close();}
