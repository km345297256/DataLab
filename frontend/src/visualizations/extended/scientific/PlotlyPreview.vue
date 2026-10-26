<template>
  <PreviewFrame class="plotly-preview" :loading="loading" :error="error" note="Plotly · 只读数值预览；空值保持缺失，布尔值按 false=0/true=1 绘制，不执行表格公式。">
    <form v-if="arrayFile" class="plotly-controls plotly-array-settings" @submit.prevent="loadData()">
      <label v-if="variables.length" class="plotly-field">变量 <select v-model="variable" @change="indices = []; sourceShape = []"><option v-for="name in variables" :key="name">{{ name }}</option></select></label>
      <label class="plotly-field">视图 <select v-model="arrayKind"><option value="series">数值曲线</option><option value="heatmap">数组热图</option></select></label>
      <label v-for="(size, index) in sliceShape" :key="index" class="plotly-field">dim_{{ index }} <input v-model.number="indices[index]" type="number" min="0" :max="size - 1" step="1" /></label>
      <button type="submit" class="plotly-draw" :disabled="loading">读取变量／切片</button>
    </form>
    <form v-else class="plotly-controls plotly-window-settings" @submit.prevent="loadData()">
      <p class="plotly-window-budget">每次显示最多 200 行 × 100 列。数据行从 1 开始，不含列名；换窗口后重新读取。</p>
      <div class="plotly-window-inputs">
        <label class="plotly-field">起始数据行 <input v-model.number="rowStart" type="number" min="1" :max="MAX_ROW_OFFSET + 1" step="1" :disabled="loading" /></label>
        <label class="plotly-field">起始列 <input v-model.number="columnStart" type="number" min="1" :max="table?.totalColumns ?? MAX_COLUMN_OFFSET + 1" step="1" :disabled="loading" /></label>
        <button type="submit" class="plotly-draw" :disabled="loading">读取窗口</button>
      </div>
      <div class="plotly-window-navigation">
        <button type="button" class="plotly-draw" :disabled="loading || !table || table.rowOffset === 0" @click="moveWindow('row', -1)">上一行窗口</button>
        <button type="button" class="plotly-draw" :disabled="loading || !table?.hasMoreRows || table.rowOffset + PLOTLY_ROWS > MAX_ROW_OFFSET" @click="moveWindow('row', 1)">下一行窗口</button>
        <button type="button" class="plotly-draw" :disabled="loading || !table || table.columnOffset === 0" @click="moveWindow('column', -1)">上一列窗口</button>
        <button type="button" class="plotly-draw" :disabled="loading || !table?.hasMoreColumns" @click="moveWindow('column', 1)">下一列窗口</button>
      </div>
    </form>
    <p v-if="table" class="plotly-window-summary" aria-live="polite">{{ windowSummary }}</p>
    <p v-if="array" class="plotly-window-summary">{{ arraySummary }}</p>
    <form v-if="table" class="plotly-controls plotly-chart-settings" @submit.prevent="draw">
      <div class="plotly-table-settings">
        <label class="plotly-field plotly-x-field">横轴 <select v-model.number="xColumn"><option :value="-1">数据行号</option><option v-for="(name, index) in table.columns" :key="index" :value="index">{{ name }}</option></select></label>
        <label class="plotly-field">图式 <select v-model="mode"><option value="lines">曲线</option><option value="markers">散点</option><option value="histogram">直方图</option></select></label>
        <button type="submit" class="plotly-draw" :disabled="loading || !columns.length">绘制</button>
      </div>
      <fieldset class="plotly-series">
        <legend>纵轴 <span>已选 {{ yColumns.length }} / 8 项</span></legend>
        <div v-if="columns.length" class="plotly-series-list">
          <label v-for="index in columns" :key="index" class="plotly-series-option" :title="table.columns[index]">
            <input v-model="yColumns" type="checkbox" :value="index" :disabled="yColumns.length >= 8 && !yColumns.includes(index)" />
            <span class="plotly-series-name">{{ table.columns[index] }}</span>
          </label>
        </div>
        <p v-else class="plotly-hint">当前预览中没有可绘制的数值列。</p>
        <p v-if="columns.length && !yColumns.length" class="plotly-hint">勾选数值列后，点击「绘制」更新图表。</p>
      </fieldset>
    </form>
    <p v-if="table" class="plotly-hint">选列、切换图式后点击「绘制」，使用当前窗口更新，不重新读取。空值与非数值内容保留为缺失；直方图只统计当前窗口的有效数值。</p>
    <ViewToolbar :context="viewContext" :image-formats="['png','svg']" :color="!!array && array.shape.length === 2" :extent="array ? finiteExtent(array.values) : null" @image="exportImage" @restore="restoreView" @color-range="setColorRange" @reset="resetView" />
    <p v-if="table && mode === 'histogram'" class="plotly-hint">直方图仅反映当前窗口分布；桶范围不会作为原始行选区发送分析。</p>
    <p class="plotly-hint">{{ semanticNote }} 拖动缩放或使用图内框选工具选择范围；双击复位。选区仅在当前读取窗口内。</p>
    <div ref="target" class="plotly-chart" aria-label="Plotly 数值图" />
    <p v-if="typeof metadata.calendar === 'string' && metadata.calendar" class="plotly-hint">时间日历：{{ metadata.calendar }}；时间值未转换为公历日期。</p>
    <p v-if="sampled || table?.partial" class="plotly-sampling-note">当前为局部窗口／切片或等步长抽样，不代表完整数据分布，也不保证保留全部极值。</p>
    <p v-if="table?.hasMoreRows && table.rowOffset + PLOTLY_ROWS > MAX_ROW_OFFSET" class="plotly-sampling-note">已到此预览支持的起始行上限；后续数据请使用分析工具读取。</p>
    <p v-for="(warning, index) in warnings" :key="index" class="plotly-sampling-note">{{ warning }}</p>
  </PreviewFrame>
</template>
<script setup lang="ts">
import { computed, nextTick, ref, shallowRef, watch } from 'vue';
import type { FileInfo } from '../../../api/file';
import type { VisualizationPlugin } from '../../contract';
import { usePreviewLoad } from '../../../composables/usePreviewLoad';
import { requestPreview } from '../runtime';
import { numericCell, numericColumns, parseArray, plainLabel, type PreviewArray } from './data';
import { MAX_COLUMN_OFFSET, MAX_ROW_OFFSET, PLOTLY_ROWS, PLOTLY_COLUMNS, parsePlotlyTableWindow, preservePlotlyColumns, plotlyWindowOptions, type PlotlyTableWindow } from './plotlyData';
import PreviewFrame from './PreviewFrame.vue';
import { loadBrowserLibrary } from './browserLibraries';
import ViewToolbar from '../../ViewToolbar.vue';
import { assertViewExtent, viewSemantics, saveViewDataUrl, type NumericRange, type ViewSelection, type VisualizationViewContext } from '../../viewContext';
import { finiteExtent } from '../../plot';
import { bindPlotlyInteractions } from '../../plotlyInteractions';
const props = defineProps<{ file: FileInfo; plugin: VisualizationPlugin }>();
const emit=defineEmits<{(e:'view-context',context:VisualizationViewContext|null):void}>();
const viewContext=shallowRef<VisualizationViewContext|null>(null),colorRange=ref<NumericRange>(),selection=ref<ViewSelection>(),zoomRange=ref<{x:NumericRange;y?:NumericRange}>(),metadata=ref<Record<string,unknown>>({});
const semanticNote=computed(()=>`${viewSemantics(metadata.value).valueSemantics==='physical'?'物理值':'原始存储值'}${typeof metadata.value.units==='string'&&metadata.value.units?' · '+metadata.value.units:''}；${typeof metadata.value.missing_value_policy==='string'?metadata.value.missing_value_policy:'空值与非数值不按零处理'}。`);
let appliedParams:Record<string,unknown>={},appliedChart={x:-1,ys:[] as number[],mode:'lines'};
let interactionCleanup:(()=>void)|undefined;
function refreshInteractions(){interactionCleanup?.();interactionCleanup=target.value?attachInteractions(target.value):undefined;}
function publishContext(){if(!version.value||(!table.value&&!array.value))return;viewContext.value={schemaVersion:1,fileId:props.file.file_id,version:version.value,pluginId:props.plugin.id,pluginVersion:props.plugin.version,adapter:props.plugin.adapter,family:'plotly',request:{operation:'preview',params:JSON.parse(JSON.stringify(appliedParams))},view:{kind:String(appliedParams.kind),...viewSemantics(metadata.value),sampled:sampled.value||!!table.value?.partial,...(table.value?{xColumn:appliedChart.x,yColumns:[...appliedChart.ys],mode:appliedChart.mode}:{}),...(array.value?{sourceShape:[...sourceShape.value],strides:[...strides.value]}:{}),...(colorRange.value?{colorRange:[...colorRange.value] as NumericRange}:{}),...(zoomRange.value?{xRange:zoomRange.value.x,...(zoomRange.value.y?{yRange:zoomRange.value.y}:{})}:{})},...(selection.value?{selection:JSON.parse(JSON.stringify(selection.value))}:{})};emit('view-context',viewContext.value);}
function clearContext(){viewContext.value=null;emit('view-context',null);}
async function setColorRange(range:NumericRange|undefined){colorRange.value=range;await draw();}
async function resetView(){selection.value=undefined;zoomRange.value=undefined;if(plotly&&target.value)await plotly.relayout(target.value,{'xaxis.autorange':true,'yaxis.autorange':true});publishContext();}
async function exportImage(format:'png'|'svg'){try{if(plotly&&target.value&&viewContext.value)saveViewDataUrl(await plotly.toImage(target.value,{format,width:1000,height:650}),`plotly-view.${format}`);}catch{error.value='图像导出失败，请重试。';}}
async function restoreView(context:VisualizationViewContext){const p=context.request.params;if(p.kind==='table'){rowStart.value=Number(p.row_offset)+1;columnStart.value=Number(p.column_offset)+1;}else{arrayKind.value=String(p.kind);variable.value=String(p.variable??'');indices.value=[...(p.indices as number[])];}colorRange.value=context.view.colorRange;loadedKey='';await loadData(context);if(!viewContext.value||version.value!==context.version)return;if(table.value){if((context.view.xColumn??-1)>=table.value.columns.length||(context.view.yColumns??[]).some(i=>i>=table.value!.columns.length)){error.value='恢复参数的绘图列不在当前窗口。';return;}xColumn.value=context.view.xColumn??-1;yColumns.value=context.view.yColumns??[];mode.value=context.view.mode??'lines';}const xs=array.value?Array.from({length:array.value.shape[array.value.shape.length-1]!},(_,i)=>i*(strides.value[strides.value.length-1]??1)):table.value?.rows.map((row,i)=>xColumn.value<0?table.value!.rowOffset+i+1:numericCell(row[xColumn.value])).filter((n):n is number=>n!==null)??[];try{if(xs.length&&context.view.mode!=='histogram')assertViewExtent(context,[Math.min(...xs),Math.max(...xs)],array.value?.shape.length===2?[0,(array.value.shape[0]!-1)*(strides.value[0]??1)]:undefined);}catch(reason){error.value=reason instanceof Error?reason.message:'恢复范围无效。';return;}selection.value=context.view.mode==='histogram'?undefined:context.selection;zoomRange.value=context.view.mode!=='histogram'&&context.view.xRange?{x:context.view.xRange,...(context.view.yRange?{y:context.view.yRange}:{})}:undefined;await draw();}
function attachInteractions(element:HTMLElement){if(table.value&&appliedChart.mode==='histogram')return()=>{};const x=array.value?Array.from({length:array.value.shape[array.value.shape.length-1]!},(_,i)=>i*(strides.value[strides.value.length-1]??1)):table.value?.rows.map((row,i)=>appliedChart.x<0?table.value!.rowOffset+i+1:numericCell(row[appliedChart.x])).filter((n):n is number=>n!==null)??[];if(!x.length)return()=>{};const y=array.value?.shape.length===2?[0,(array.value.shape[0]!-1)*(strides.value[0]??1)] as NumericRange:undefined;return bindPlotlyInteractions(element,{x:[Math.min(...x),Math.max(...x)],...(y?{y}:{}),coordinateSystem:array.value||appliedChart.x<0?'source-index':'axis-values'},(s,z)=>{selection.value=s;zoomRange.value=z;publishContext();});}
const scope = usePreviewLoad(), target = ref<HTMLDivElement>(), loading = ref(false), error = ref(''), sampled = ref(false);
const table = shallowRef<PlotlyTableWindow>(), xColumn = ref(-1), yColumns = ref<number[]>([]), mode = ref('lines');
// A failed page read may discard its data without losing the user's column choices.
const tableSelection = shallowRef<{ columnOffset: number; x: number; ys: number[] }>();
const rowStart = ref(1), columnStart = ref(1), warnings = ref<string[]>([]), version = ref<string>();
const array = shallowRef<PreviewArray>(), variables = ref<string[]>([]), variable = ref(''), arrayKind = ref('series'), sourceShape = ref<number[]>([]), indices = ref<number[]>([]), strides = ref<number[]>([]);
const selectedIndices = ref<number[]>([]);
const arrayFile = computed(() => /\.(npy|npz|mat)$/i.test(props.file.filename));
const sliceShape = computed(() => sourceShape.value.slice(0, Math.max(0, sourceShape.value.length - (arrayKind.value === 'series' ? 1 : 2))));
const columns = computed(() => table.value ? [...new Set([...numericColumns(table.value), ...yColumns.value])].sort((a, b) => a - b) : []);
const windowSummary = computed(() => {
  const current = table.value;
  if (!current) return '';
  const rows = current.rows.length ? `数据行 ${current.rowOffset + 1}–${current.rowOffset + current.rows.length}` : '无数据行';
  const total = current.totalRows === null ? '总行数未统计，后面仍有数据' : `共 ${current.totalRows} 行`;
  return `当前绘图窗口：${rows}，列 ${current.columnOffset + 1}–${current.columnOffset + current.columns.length} / ${current.totalColumns}；${total}。`;
});
const arraySummary = computed(() => {
  if (!array.value) return '';
  const fixed = selectedIndices.value.map((value, index) => `dim_${index}=${value}`).join('，');
  return `源数组 ${sourceShape.value.join(' × ')}；${fixed ? `固定 ${fixed}；` : ''}当前显示 ${array.value.shape.join(' × ')} 个值；显示维度步长 ${strides.value.join(' × ')}。索引从 0 开始，统计仅适用于当前切片。`;
});
let loadedKey = '';
const selectionKey = (options: Record<string, unknown>) => JSON.stringify(Object.keys(options).sort().map(key => [key, options[key]]));
let plotly: typeof import('plotly.js-cartesian-dist-min').default | undefined;
async function draw() {
  if (loading.value || !plotly || !target.value || (!array.value && !table.value)) return;
  const drawable = array.value?.values.some(value => value !== null)
    || yColumns.value.some(index => table.value?.rows.some(row => numericCell(row[index]) !== null));
  try { await renderPlot(); if (drawable) error.value = ''; refreshInteractions();publishContext(); }
  catch { error.value = '图表绘制失败，请调整当前窗口或重新读取。'; }
}
async function renderPlot() {
  if (!plotly || !target.value) return;
  if (array.value) {
    const values = array.value, rank = values.shape.length;
    const x = Array.from({ length: values.shape[rank - 1]! }, (_, index) => index * (strides.value[rank - 1] ?? 1));
    const traces = rank === 1 ? [{ type: 'scatter' as const, mode: 'lines' as const, x, y: values.values, connectgaps: false }] : [{ type: 'heatmap' as const, x, y: Array.from({ length: values.shape[0]! }, (_, index) => index * (strides.value[0] ?? 1)), z: Array.from({ length: values.shape[0]! }, (_, row) => values.values.slice(row * values.shape[1]!, (row + 1) * values.shape[1]!)), colorscale: 'Viridis', ...(colorRange.value?{zmin:colorRange.value[0],zmax:colorRange.value[1]}:{}) }];
    await plotly.react(target.value, traces, { margin: { l: 60, r: 30, b: 50, t: 30 }, xaxis: { title: { text: '原数组索引' }, ...(zoomRange.value?{range:zoomRange.value.x}:{}) }, yaxis: { title: { text: rank === 1 ? '数值' : '原数组索引' }, ...(zoomRange.value?.y?{range:zoomRange.value.y}:{}) } }, { responsive: true, displaylogo: false, displayModeBar: true, modeBarButtonsToRemove: ['toImage'] });
    return;
  }
  if (!table.value) return;
  const current = table.value;
  const chart={x:xColumn.value,ys:[...yColumns.value],mode:mode.value};
  if(appliedChart.x!==chart.x||appliedChart.mode!==chart.mode){selection.value=undefined;zoomRange.value=undefined;}
  const traces = yColumns.value.slice(0, 8).map((index) => {
    const y = current.rows.map((row) => numericCell(row[index]));
    const columnName = current.columns[index]!;
    const label = { name: columnName.length > 18 ? `#${current.columnOffset + index + 1} ${columnName.slice(0, 18)}…` : columnName, meta: { columnName }, hovertemplate: '%{meta.columnName}<br>x: %{x}<br>y: %{y}<extra></extra>' };
    return mode.value === 'histogram' ? { type: 'histogram' as const, x: y.filter((value): value is number => value !== null), ...label, hovertemplate: '%{meta.columnName}<br>数值区间: %{x}<br>当前窗口计数: %{y}<extra></extra>' } : { type: 'scatter' as const, mode: mode.value as 'lines' | 'markers', ...label, x: current.rows.map((row, rowIndex) => { const value = row[xColumn.value]; return xColumn.value < 0 ? current.rowOffset + rowIndex + 1 : typeof value === 'string' ? plainLabel(value) : typeof value === 'boolean' ? Number(value) : value; }), y, connectgaps: false };
  });
  // Plotly 4 supports a scrolling legend height cap; upstream TS types lag this option.
  const legend: Partial<import('plotly.js').Legend> & { maxheight: number } = { orientation: 'h', x: 0, y: -0.3, xanchor: 'left', yanchor: 'top', maxheight: 88, font: { size: 11 } };
  await plotly.react(target.value, traces, { margin: { l: 52, r: 16, b: 60, t: 24 }, autosize: true, showlegend: true, legend, xaxis: { ...(zoomRange.value?{range:zoomRange.value.x}:{}), automargin: true, title: { text: mode.value === 'histogram' ? '当前窗口数值' : xColumn.value < 0 ? '数据行号（不含列名）' : current.columns[xColumn.value], standoff: 12 } }, yaxis: { title: { text: mode.value === 'histogram' ? '当前窗口计数' : '数值' } } }, { responsive: true, displaylogo: false, displayModeBar: true, modeBarButtonsToRemove: ['toImage'], plotGlPixelRatio: 1 });appliedChart=chart;
}
async function moveWindow(axis: 'row' | 'column', direction: -1 | 1) {
  if (!table.value || loading.value) return;
  rowStart.value = table.value.rowOffset + 1;
  columnStart.value = table.value.columnOffset + 1;
  if (axis === 'row') rowStart.value = Math.max(1, rowStart.value + direction * PLOTLY_ROWS);
  else columnStart.value = Math.max(1, columnStart.value + direction * PLOTLY_COLUMNS);
  await loadData();
}
async function loadData(restored?:VisualizationViewContext) {
  if(restored?.schemaVersion!==1)restored=undefined;
  let options: Record<string, unknown>;
  try {
    options = arrayFile.value ? { kind: arrayKind.value, ...(variable.value ? { variable: variable.value } : {}), indices: sliceShape.value.map((size, index) => {
      const value = indices.value[index] ?? 0;
      if (!Number.isSafeInteger(value) || value < 0 || value >= size) throw new Error('数组切片索引超出维度范围。');
      return value;
    }) } : { kind: 'table', ...plotlyWindowOptions(rowStart.value, columnStart.value) };
  } catch (reason) { error.value = reason instanceof Error ? reason.message : '读取范围无效。'; return; }
  if(restored)options={...restored.request.params};
  const key = selectionKey(options);
  if (!loading.value && !error.value && key === loadedKey && (table.value || array.value)) { await draw(); return; }
  if (table.value) tableSelection.value = { columnOffset: table.value.columnOffset, x: xColumn.value, ys: [...yColumns.value] };
  const previous = tableSelection.value;
  clearContext();selection.value=undefined;zoomRange.value=undefined;
  const load = scope.begin(); loading.value = true; error.value = ''; table.value = undefined; array.value = undefined; warnings.value = []; sampled.value = false;
  try {
    const [result, library] = await Promise.all([requestPreview(props.file, props.plugin, { ...options, ...(version.value ? { version: version.value } : {}) }, load.signal), loadBrowserLibrary('plotly', load.signal)]);
    load.assertCurrent(); plotly = library;
    if (typeof result.version !== 'string' || !/^[a-f0-9]{64}$/.test(result.version)) throw new Error('预览缺少文件版本，请重新打开。');
    version.value = result.version;
    sampled.value = result.sampled === true;metadata.value=(result.metadata as Record<string,unknown>)??{};
    warnings.value = Array.isArray(result.warnings) ? result.warnings.filter((warning): warning is string => typeof warning === 'string').slice(0, 32).map(warning => plainLabel(warning)) : [];
    if (arrayFile.value) {
      array.value = parseArray(result.array);
      const choices = result.choices as Record<string, unknown> | undefined, selected = result.selected as Record<string, unknown> | undefined, metadata = result.metadata as Record<string, unknown> | undefined;
      variables.value = Array.isArray(choices?.variables) ? choices.variables.filter((value): value is string => typeof value === 'string').slice(0, 128) : [];
      if (typeof selected?.variable === 'string') variable.value = selected.variable;
      sourceShape.value = Array.isArray(metadata?.source_shape) ? metadata.source_shape as number[] : array.value.shape;
      strides.value = Array.isArray(metadata?.strides) ? metadata.strides as number[] : array.value.shape.map(() => 1);
      selectedIndices.value = Array.isArray(selected?.indices) ? selected.indices as number[] : options.indices as number[];
      indices.value = [...selectedIndices.value];
      appliedParams={...options,...(variable.value?{variable:variable.value}:{}),indices:[...selectedIndices.value]};loadedKey=selectionKey(appliedParams);
    } else {
      table.value = parsePlotlyTableWindow(result.table, { rowOffset: Number(options.row_offset), columnOffset: Number(options.column_offset) });
      const selection = preservePlotlyColumns(previous, table.value, previous?.x ?? -1, previous?.ys ?? []);
      yColumns.value = selection.ys; xColumn.value = selection.x;
      rowStart.value = table.value.rowOffset + 1; columnStart.value = table.value.columnOffset + 1;
      loadedKey = key;appliedParams={...options};
    }
    await nextTick(); load.assertCurrent();
    const element = target.value!; load.onDispose(() => { plotly?.purge(element); });
    await renderPlot(); load.assertCurrent();refreshInteractions();load.onDispose(()=>{interactionCleanup?.();interactionCleanup=undefined;});publishContext();
    if (typeof ResizeObserver !== 'undefined') { const resize = new ResizeObserver(() => { if (load.isCurrent()) void plotly?.Plots.resize(element); }); resize.observe(element); load.onDispose(() => resize.disconnect()); }
  } catch (reason) { if (load.isCurrent()) error.value = reason instanceof Error ? reason.message : '图表加载失败。'; }
  finally { if (load.isCurrent()) loading.value = false; }
}
watch([() => props.file.file_id, () => props.plugin.id, () => props.plugin.version], () => {
  clearContext();colorRange.value=undefined;selection.value=undefined;zoomRange.value=undefined;
  variable.value = ''; variables.value = []; sourceShape.value = []; indices.value = []; selectedIndices.value = [];
  rowStart.value = 1; columnStart.value = 1; version.value = undefined; loadedKey = ''; table.value = undefined; array.value = undefined;
  tableSelection.value = undefined;
  xColumn.value = -1; yColumns.value = []; void loadData();
}, { immediate: true });
</script>

<style scoped>
.plotly-preview {
  container: plotly-preview / inline-size;
  min-width: 0;
  gap: 12px;
}

/* Controls keep their natural height; short panels scroll instead of squeezing labels. */
.plotly-preview > :deep(p) {
  flex-shrink: 0;
  margin: 0;
  line-height: 1.6;
}

.plotly-controls {
  flex: 0 0 auto;
  min-width: 0;
  margin: 0;
  padding: 14px;
  border: 1px solid var(--border-main);
  border-radius: 10px;
  background: var(--background-white-main, #fff);
  font-size: 13px;
  line-height: 1.5;
}

.plotly-table-settings {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(100px, 160px) auto;
  align-items: end;
  gap: 12px;
}

.plotly-array-settings {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 180px), 1fr));
  align-items: end;
  gap: 12px;
}

.plotly-window-budget { margin: 0 0 10px; color: var(--text-secondary); font-size: 12px; }
.plotly-window-inputs { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr) auto; gap: 10px; align-items: end; }
.plotly-window-navigation { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; }
.plotly-window-navigation .plotly-draw { flex: 1 1 130px; padding-inline: 10px; font-size: 12px; }
.plotly-window-summary { color: var(--text-secondary); font-size: 12px; overflow-wrap: anywhere; }

.plotly-field {
  display: flex;
  min-width: 0;
  flex-direction: column;
  gap: 6px;
  font-weight: 500;
}

.plotly-field select,
.plotly-field input {
  box-sizing: border-box;
  display: block;
  width: 100%;
  min-width: 0;
  max-width: 100%;
  height: 38px;
  margin: 0;
  padding: 0 10px;
  border: 1px solid var(--border-main);
  border-radius: 6px;
  background: var(--background-white-main, #fff);
  color: var(--text-primary);
  font: inherit;
  font-weight: 400;
  text-overflow: ellipsis;
}

.plotly-draw {
  min-height: 38px;
  padding: 8px 20px;
  border: 1px solid #b5d8cc;
  border-radius: 6px;
  background: #edf7f2;
  color: #28654f;
  font: inherit;
  font-weight: 500;
  cursor: pointer;
}

.plotly-draw:hover:not(:disabled) { background: #e0f0e8; }
.plotly-draw:disabled { opacity: 0.5; cursor: not-allowed; }
.plotly-field select:focus-visible,
.plotly-field input:focus-visible,
.plotly-draw:focus-visible,
.plotly-series-option input:focus-visible {
  outline: 2px solid #398469;
  outline-offset: 2px;
}

.plotly-series {
  min-width: 0;
  margin: 14px 0 0;
  padding: 0;
  border: 0;
}

.plotly-series legend {
  width: 100%;
  margin-bottom: 8px;
  padding: 0;
  font-weight: 500;
}

.plotly-series legend span {
  margin-left: 8px;
  color: var(--text-secondary);
  font-size: 12px;
  font-weight: 400;
}

.plotly-series-list {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 180px), 1fr));
  gap: 6px 10px;
  max-height: 126px;
  overflow: auto;
  overscroll-behavior: contain;
  padding: 3px;
}

.plotly-series-option {
  display: flex;
  min-width: 0;
  align-items: center;
  gap: 8px;
  padding: 6px 8px;
  border: 1px solid var(--border-main);
  border-radius: 6px;
  cursor: pointer;
}

.plotly-series-option:has(:checked) {
  border-color: #b5d8cc;
  background: #f0f8f4;
  color: #28654f;
}

.plotly-series-option:has(:disabled) { opacity: 0.5; cursor: not-allowed; }
.plotly-series-option input {
  flex: 0 0 14px;
  width: 14px;
  height: 14px;
  margin: 0;
  accent-color: #398469;
}

.plotly-series-name { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.plotly-hint { margin: 8px 0 0; color: var(--text-secondary); font-size: 12px; }
.plotly-chart { flex: 1 0 360px; width: 100%; min-width: 0; min-height: 360px; overflow: hidden; border-radius: 8px; background: #fff; }
.plotly-sampling-note { color: #a45b16; font-size: 12px; }

@container plotly-preview (max-width: 520px) {
  .plotly-table-settings { grid-template-columns: minmax(0, 1fr) auto; }
  .plotly-window-inputs { grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); }
  .plotly-window-inputs > button { grid-column: 1 / -1; }
  .plotly-x-field { grid-column: 1 / -1; }
  .plotly-controls { padding: 12px; }
}
</style>
