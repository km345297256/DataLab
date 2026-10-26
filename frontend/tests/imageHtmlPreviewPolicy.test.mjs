import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createRequire } from 'node:module';
import { inspectImage, checkImageInput, IMAGE_INPUT_BYTES } from '../src/visualizations/imagePreflight.ts';
import { prepareHtmlPreview, HTML_RESOURCE_BYTES, HTML_RESOURCE_CONCURRENCY } from '../src/visualizations/htmlPreviewPolicy.ts';
const require = createRequire(import.meta.url), { JSDOM } = createRequire(require.resolve('jsroot'))('jsdom');
const pngBytes = () => Uint8Array.from(Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGPQCOgBAAGoAQW7cALPAAAAAElFTkSuQmCC', 'base64')).buffer;
const buffer = (...parts) => Uint8Array.from(parts.flatMap(part => typeof part === 'string' ? [...Buffer.from(part, 'latin1')] : [...part])).buffer;
const be32 = n => [n >>> 24, n >>> 16 & 255, n >>> 8 & 255, n & 255];
const le32 = n => [...be32(n)].reverse();
const le16 = n => [n & 255, n >> 8];
const chunk = (type, data) => [ ...be32(data.length), ...Buffer.from(type), ...data, 0, 0, 0, 0 ];
const png = (w, h, frames = 0) => buffer('\x89PNG\r\n\x1a\n', chunk('IHDR', [...be32(w), ...be32(h), 8, 6, 0, 0, 0]), ...(frames ? [chunk('acTL', [...be32(frames),0,0,0,0]), ...Array.from({length:frames}, (_,i) => chunk('fcTL', [...be32(i),...be32(w),...be32(h),0,0,0,0,0,0,0,0,0,1,0,10,0,0]))] : []), chunk('IEND', []));
const jpeg = (w,h) => buffer([255,216,255,192,0,11,8,h>>8,h&255,w>>8,w&255,1,1,0x11,0,255,218,0,2,255,217]);
const gif = (w,h,frames=1) => buffer('GIF89a',le16(w),le16(h),[0,0,0],...Array.from({length:frames},()=>[0x2c,0,0,0,0,...le16(w),...le16(h),0,2,1,0,0]),[0x3b]);
const webpChunk = (kind, data) => [...Buffer.from(kind),...le32(data.length),...data,...(data.length&1?[0]:[])];
const webp = (w,h) => { const bits=(w-1)|((h-1)<<14), inner=webpChunk('VP8L',[0x2f,...le32(bits)]); return buffer('RIFF',le32(inner.length+4),'WEBP',inner); };
const bmp = (w,h) => buffer('BM', le32(54),[0,0,0,0],le32(54),le32(40),le32(w),le32(h),[1,0,24,0],Array(24).fill(0));
for (const [format, make] of [['png',png],['jpeg',jpeg],['gif',gif],['webp',webp],['bmp',bmp]]) {
  test(`${format} dimensions are inspected from bytes before decoder allocation`, () => assert.deepEqual(inspectImage(make(640,480)), {width:640,height:480,frames:1,mime:`image/${format}`}));
  test(`${format} giant dimensions are rejected despite a tiny compressed file`, () => assert.throws(()=>inspectImage(make(16000,16000)),/安全解码/));
}
test('input byte cap rejects unknown invalid or oversized sizes',()=>{ for(const n of [0,-1,NaN,Infinity,IMAGE_INPUT_BYTES+1]) assert.throws(()=>checkImageInput(n)); });
test('animated GIF and APNG enforce frame count and aggregate canvas bytes',()=>{
  assert.equal(inspectImage(gif(10,10,3)).frames,3); assert.equal(inspectImage(png(10,10,3)).frames,3);
  for(const bytes of [gif(10,10,129),png(10,10,129),gif(1024,1024,17),png(1024,1024,17)]) assert.throws(()=>inspectImage(bytes),/安全解码/);
});
test('truncated and foreign image headers fail closed, including unbounded ISO image containers',()=>{
  for(const bytes of [pngBytes().slice(0,30),gif(20,20).slice(0,-1),webp(5,5).slice(0,-2),buffer('not-an-image'),buffer(le32(20),'ftypavif')]) assert.throws(()=>inspectImage(bytes));
});
test('ICO directory hints cannot conceal an oversized embedded PNG',()=>{
  const nested=png(10000,10000), dir=[0,0,1,0,1,0,1,1,0,0,1,0,32,0,...le32(nested.byteLength),...le32(22)];
  assert.throws(()=>inspectImage(buffer(dir,new Uint8Array(nested))),/安全解码/);
});
function context(t) {
  const dom=new JSDOM('');t.after(()=>dom.window.close());const cleanups=[];let active=true;
  const load={assertCurrent(){if(!active)throw new DOMException('cancelled','AbortError');},onDispose(fn){cleanups.push(fn);}};
  t.after(()=>cleanups.forEach(fn=>fn()));
  return {doc:dom.window.document,load,stop(){active=false;}, async prepare(html,files=[],read=async()=>new ArrayBuffer(0)) {return prepareHtmlPreview(html,{file_id:'report',filename:'report.html'},files,load,read,dom.window.document);}, report(result){const holder=dom.window.document.createElement('template');holder.innerHTML=result.html;return holder.content.querySelector('iframe').getAttribute('srcdoc');}};
}
const file = (name, size=1) => ({file_id:name,filename:name,size});
test('HTML trusted policy precedes content and removes base refresh forms external links and original CSP',async(t)=>{
  const h=context(t), result=await h.prepare('<head><base href="https://outside.test"><meta http-equiv="refresh" content="0;url=https://outside.test"><meta http-equiv="Content-Security-Policy" content="default-src *"></head><body><form action="https://outside.test"><button onclick="this.textContent=\'ok\'">Run</button></form><script>window.ready=true</script><a href="https://outside.test" target="_blank" ping="https://outside.test">external</a><img src="https://outside.test/image"><iframe srcdoc="bad"></iframe></body>');
  const report=h.report(result); assert.ok(report.indexOf('Content-Security-Policy')<report.indexOf('window.ready')); assert.match(report,/frame-src 'none'/);
  assert.match(report,/window.ready=true/);assert.match(report,/onclick=/);assert.doesNotMatch(report,/<base|http-equiv="refresh"|<form|<iframe|https:\/\/outside|default-src \*/);
});
test('authorized relative scripts styles and images remain, with duplicate resources fetched once',async(t)=>{
  const h=context(t), seen=[];const files=[file('app.js',9),file('main.css',3),file('plot.png',pngBytes().byteLength)];
  const result=await h.prepare('<script src=app.js></script><link rel=stylesheet href="main.css"><img src="plot.png"><img src="plot.png#again"><style>.plot{background:url("plot.png")}</style>',files,async(f)=>{seen.push(f.filename);return f.filename==='plot.png'?pngBytes():new TextEncoder().encode(f.filename==='app.js'?'void(0);;':'a{}').buffer;});
  const report=h.report(result);assert.equal(seen.length,3);assert.equal(result.resources,3);assert.match(report,/<script src="data:application\/javascript;base64,/);assert.match(report,/<link rel="stylesheet" href="data:text\/css;base64,/);assert.match(report,/background:url\("data:image\/png;base64,/);assert.match(report,/#again/);
});
test('embedded raster figures survive, while arbitrary data HTML/script and preexisting blob URLs are rejected',async(t)=>{
  const h=context(t), data='data:image/png;base64,'+Buffer.from(pngBytes()).toString('base64');
  const result=await h.prepare(`<img src="${data}"><img src="${data}"><script src="data:text/javascript,alert(1)"></script><img src="blob:https://outside/x"><style>x{background:url(https://outside/x)}</style>`);
  const report=h.report(result);assert.equal(result.resources,1);assert.equal((report.match(/<img src="data:image\/png;base64,/g)||[]).length,2);assert.doesNotMatch(report,/data:text|outside/);
});
test('resource count per-resource and aggregate size reject before any reads',async(t)=>{
  const h=context(t);let calls=0;const read=async()=>{calls++;return new ArrayBuffer(0);};
  for(const files of [Array.from({length:33},(_,i)=>file(`${i}.js`)),[file('big.js',HTML_RESOURCE_BYTES+1)],Array.from({length:5},(_,i)=>file(`${i}.js`,HTML_RESOURCE_BYTES))]) {
    await assert.rejects(h.prepare(files.map(f=>`<script src="${f.filename}"></script>`).join(''),files,read),/资源预算/);
  }
  assert.equal(calls,0);
});
test('resource loading is concurrency bounded and cancellation discards late data',async(t)=>{
  const h=context(t), files=Array.from({length:9},(_,i)=>file(`${i}.js`));let active=0,peak=0;const seenCaps=[];
  await h.prepare(files.map(f=>`<script src="${f.filename}"></script>`).join(''),files,async(_f,cap)=>{seenCaps.push(cap);active++;peak=Math.max(peak,active);await new Promise(r=>setImmediate(r));active--;return new Uint8Array([32]).buffer;});
  assert.equal(peak,HTML_RESOURCE_CONCURRENCY);assert.deepEqual(seenCaps,Array(9).fill(1));
  let finish;const wait=new Promise(r=>finish=r), before=t.mock.method(URL,'createObjectURL',()=>{assert.fail('cancelled resource created URL');});
  const run=h.prepare('<script src="late.js"></script>',[file('late.js')],()=>wait);h.stop();finish(new Uint8Array([32]).buffer);await assert.rejects(run,/cancelled/);assert.equal(before.mock.callCount(),0);
});
test('dishonest metadata and image pixel bombs in a related file cannot bypass resource budgets',async(t)=>{
  const h=context(t);await assert.rejects(h.prepare('<script src="a.js"></script>',[file('a.js')],async()=>new ArrayBuffer(2)),/资源预算/);
  const huge=png(16000,16000);await assert.rejects(h.prepare('<img src="a.png">',[file('a.png',huge.byteLength)],async()=>huge),/安全像素/);
});
test('HTML repeated resources cannot multiply a small read into an unbounded document',async(t)=>{
  const h=context(t), resource=file('repeated.js',1024*1024);
  await assert.rejects(h.prepare('<script src="repeated.js"></script>'.repeat(32),[resource],async()=>new ArrayBuffer(resource.size)),/展开后/);
});
test('HTML rejects excessive element markup before building its DOM',async(t)=>{
  const h=context(t);await assert.rejects(h.prepare('<i></i>'.repeat(10001)),/20,000/);
});
test('HTML sums decoded pixels across authorized images even when compressed input is tiny',async(t)=>{
  const h=context(t), image=png(4096,3072), files=[file('one.png',image.byteLength),file('two.png',image.byteLength)];
  assert.doesNotThrow(()=>inspectImage(image));
  await assert.rejects(h.prepare('<img src="one.png"><img src="two.png">',files,async()=>image),/合计超过 128 MiB/);
  const repeated=await h.prepare('<img src="one.png"><img src="one.png#again">',[files[0]],async()=>image);
  assert.equal(repeated.resources,1);
  await assert.rejects(h.prepare('<img src=one.dat><img src=two.dat>',[file('one.dat',image.byteLength),file('two.dat',image.byteLength)],async()=>image),/合计超过 128 MiB/);
});
test('HTML embedded image decode budget counts distinct URLs once and combines with related images',async(t)=>{
  const h=context(t), first=png(4096,3072), second=png(3072,4096);
  const data=bytes=>'data:image/png;base64,'+Buffer.from(bytes).toString('base64');
  await assert.rejects(h.prepare(`<img src="${data(first)}"><img src="${data(second)}">`),/合计超过 128 MiB/);
  const repeated=await h.prepare(`<img src="${data(first)}"><img src="${data(first)}">`);assert.equal(repeated.resources,1);
  await assert.rejects(h.prepare(`<img src="${data(first)}"><img src="second.png">`,[file('second.png',second.byteLength)],async()=>second),/合计超过 128 MiB/);
});
