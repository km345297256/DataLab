/** Portable, inert view parameters. Source values and storage locations never belong here. */
export type NumericRange = [number, number];
export interface ViewSelection { kind: 'range'; coordinateSystem: 'axis-values' | 'source-index'; bounds: 'inclusive'; x: NumericRange; y?: NumericRange }
export interface VisualizationViewContext {
  schemaVersion: 1; fileId: string; version: string; pluginId: string; pluginVersion: string; adapter: string;
  family: 'scientific' | 'plotly' | 'array-window' | 'h5web';
  request: { operation: 'preview'; params: Record<string, unknown> };
  view: { kind: string; title?: string; xLabel?: string; yLabel?: string; units?: string; valueSemantics: 'raw' | 'physical' | 'derived' | 'unknown'; sampled: boolean;
    colorRange?: NumericRange; xRange?: NumericRange; yRange?: NumericRange; xColumn?: number; yColumns?: number[]; mode?: string; sourceShape?: number[]; strides?: number[] };
  selection?: ViewSelection;
}
const record = (v: unknown): v is Record<string, unknown> => !!v && typeof v === 'object' && !Array.isArray(v);
const allowed = (v: Record<string, unknown>, names: string[]) => Object.keys(v).every(k => names.includes(k));
const text = (v: unknown, max = 256): v is string => typeof v === 'string' && v.length <= max && !/[\x00-\x1f\x7f]|\/Users\/|\/home\/|\/tmp\/|\/private\/|\/var\/|file:|https?:\/\/|[A-Za-z]:\\/i.test(v);
const int = (v: unknown, max = 2147483647, min = 0): v is number => typeof v === 'number' && Number.isSafeInteger(v) && v >= min && v <= max;
const ints = (v: unknown, maxLength = 16) => Array.isArray(v) && v.length <= maxLength && v.every(n => int(n));
export function validRange(v: unknown, allowPoint = true): v is NumericRange { return Array.isArray(v) && v.length === 2 && v.every(n => typeof n === 'number' && Number.isFinite(n)) && (allowPoint ? v[0] <= v[1] : v[0] < v[1]); }
function fail(): never { throw new Error('视图参数无效或不属于当前文件版本。'); }
export function parseVisualizationViewContext(value: unknown): VisualizationViewContext {
  const encoded=JSON.stringify(value);if(!encoded||new TextEncoder().encode(encoded).length>16384)fail();
  let nodes=0;const visit=(v:unknown)=>{if(++nodes>512)fail();if(v&&typeof v==='object')for(const item of Object.values(v))visit(item);};visit(value);
  if (!record(value) || !allowed(value, ['schemaVersion','fileId','version','pluginId','pluginVersion','adapter','family','request','view','selection'])
    || value.schemaVersion !== 1 || !/^(?:(?:minio|gridfs):)?[A-Za-z0-9_-]{1,128}$/.test(String(value.fileId)) || !/^[a-z0-9][a-z0-9-]{0,127}$/.test(String(value.pluginId)) || !text(value.pluginVersion, 64)
    || !text(value.adapter, 64) || !/^[a-f0-9]{64}$/.test(String(value.version)) || !['scientific','plotly','array-window','h5web'].includes(String(value.family))
    || !record(value.request) || !allowed(value.request, ['operation','params']) || value.request.operation !== 'preview' || !record(value.request.params) || !record(value.view)) fail();
  const p = value.request.params, v = value.view;
  if (value.family === 'scientific') {
    if (!allowed(p, ['variable','x_dimension','indices','hdu']) || ['variable','x_dimension'].some(k => p[k] !== undefined && !text(p[k])) || p.hdu !== undefined && !int(p.hdu, 1023)
      || p.indices !== undefined && (!record(p.indices) || Object.keys(p.indices).length > 16 || Object.entries(p.indices).some(([k,n]) => !text(k,128) || ['__proto__','prototype','constructor'].includes(k) || !int(n)))) fail();
  } else if (value.family === 'plotly') {
    if (p.kind === 'table') { if (!allowed(p,['kind','row_offset','column_offset']) || !int(p.row_offset,100000) || !int(p.column_offset,16383)) fail(); }
    else if (!['series','heatmap'].includes(String(p.kind)) || !allowed(p,['kind','variable','indices']) || p.variable !== undefined && !text(p.variable) || !ints(p.indices)) fail();
  } else if (value.family === 'h5web') {
    if (!allowed(p,['kind','path','indices']) || !['series','heatmap'].includes(String(p.kind)) || !text(p.path) || !String(p.path).startsWith('/') || !ints(p.indices)) fail();
  } else {
    if (!allowed(p,['kind','variable','decode','selection']) || !['series','image'].includes(String(p.kind)) || !/^v-[0-9a-f]{32}$/.test(String(p.variable)) || !['raw','physical'].includes(String(p.decode))
      || !Array.isArray(p.selection) || !p.selection.length || p.selection.length > 8) fail();
    let count=0, elements=1;
    for (const s of p.selection) { if (int(s)) continue; if (!record(s) || !allowed(s,['start','stop','step']) || !int(s.start) || !int(s.stop) || !int(s.step) || s.step < 1 || s.stop <= s.start) fail(); count++; elements *= Math.ceil((s.stop-s.start)/s.step); }
    if (count !== (p.kind === 'series' ? 1 : 2) || elements > 16384) fail();
  }
  if (!allowed(v,['kind','title','xLabel','yLabel','units','valueSemantics','sampled','colorRange','xRange','yRange','xColumn','yColumns','mode','sourceShape','strides'])
    || !['series','quality','image','map','table','heatmap'].includes(String(v.kind)) || !['raw','physical','derived','unknown'].includes(String(v.valueSemantics)) || typeof v.sampled !== 'boolean'
    || ['title','xLabel','yLabel','units'].some(k=>v[k]!==undefined&&!text(v[k])) || ['colorRange','xRange','yRange'].some(k=>v[k]!==undefined&&!validRange(v[k],k!=='colorRange'))
    || v.xColumn!==undefined&&!int(v.xColumn,99,-1) || v.yColumns!==undefined&&(!ints(v.yColumns,8)||(v.yColumns as number[]).some(n=>n>99))
    || v.mode!==undefined&&!['lines','markers','histogram'].includes(String(v.mode)) || ['sourceShape','strides'].some(k=>v[k]!==undefined&&!ints(v[k]))) fail();
  if (value.selection!==undefined) { const s=value.selection; if(!record(s)||!allowed(s,['kind','coordinateSystem','bounds','x','y'])||s.kind!=='range'||s.bounds!=='inclusive'||!['axis-values','source-index'].includes(String(s.coordinateSystem))||!validRange(s.x)||s.y!==undefined&&!validRange(s.y))fail(); }
  return JSON.parse(encoded) as VisualizationViewContext;
}
export function bindViewContext(value: unknown, current: VisualizationViewContext): VisualizationViewContext {
  const parsed=parseVisualizationViewContext(value);
  for(const key of ['fileId','version','pluginId','pluginVersion','adapter','family'] as const)if(parsed[key]!==current[key])fail();
  return parsed;
}
export function assertViewExtent(value: VisualizationViewContext, x: NumericRange, y?: NumericRange) {
  const inside=(range:NumericRange|undefined,extent:NumericRange|undefined)=>{
    if(!range)return; if(!extent)fail();
    const low=Math.min(...extent),high=Math.max(...extent),epsilon=Math.max(1,Math.abs(low),Math.abs(high))*1e-12;
    if(range[0]<low-epsilon||range[1]>high+epsilon)throw new Error('恢复的缩放或选区超出当前读取范围。');
  };
  inside(value.selection?.x,x);inside(value.selection?.y,y);inside(value.view.xRange,x);inside(value.view.yRange,y);
}
export function viewSemantics(metadata: Record<string, unknown> = {}) {
  const semantics=String(metadata.value_semantics??'');
  return { valueSemantics: (semantics.startsWith('physical')?'physical':semantics.startsWith('raw')?'raw':semantics==='derived'?'derived':'unknown') as VisualizationViewContext['view']['valueSemantics'],
    units: typeof metadata.units==='string'?metadata.units.slice(0,160):'' };
}
export function saveViewBlob(blob: Blob, filename: string) {
  const url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=filename;a.click();setTimeout(()=>URL.revokeObjectURL(url),0);
}
export function saveViewDataUrl(url: string, filename: string) { if(!/^data:image\/(png|svg\+xml)[;,]/.test(url))throw new Error('图像导出失败。'); const a=document.createElement('a');a.href=url;a.download=filename;a.click(); }
