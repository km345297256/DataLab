<template>
  <PreviewFrame :loading="loading" :error="error" note="H5Web · 文件内变量树与有界数值切片；不接收外部 HDF 服务地址。">
    <details class="mb-3 rounded border p-2 text-xs" :open="!datasets.length">
      <summary>文件内目录 · {{ nodes.length }} 个节点（最多 256 个／8 层）</summary>
      <ul class="max-h-48 overflow-auto"><li v-for="node in nodes" :key="node.path" class="py-1">
        <button v-if="datasets.some(item => item.path === node.path)" type="button" class="underline" :disabled="loading" @click="path = node.path; indices = []">{{ node.path }}</button><span v-else>{{ node.path }}</span>
        · {{ node.node_type }}<span v-if="node.dtype"> · {{ node.dtype }}</span><span v-if="node.shape"> · {{ node.shape.join(' × ') }}</span>
      </li></ul>
    </details>
    <p v-for="warning in warnings" :key="warning" class="mb-2 text-xs text-amber-700">{{ warning }}</p>
    <form class="mb-2 flex flex-wrap gap-3 text-xs" @submit.prevent="loadData">
      <label>变量 <select v-model="path" @change="indices = []"><option v-for="node in datasets" :key="node.path" :value="node.path">{{ node.path }} · {{ node.shape?.join(' × ') }}</option></select></label>
      <label>图式 <select v-model="kind"><option value="series">曲线</option><option value="heatmap">热图</option></select></label>
      <label v-for="(size, index) in sliceShape" :key="index">dim_{{ index }} <input v-model.number="indices[index]" type="number" min="0" :max="size - 1" step="1" class="w-16 border" /></label>
      <button class="rounded border px-2" type="submit" :disabled="loading || !path">读取切片</button>
    </form>
    <ViewToolbar :context="viewContext" :color="renderedArray?.shape.length === 2" :extent="renderedArray ? finiteExtent(renderedArray.values) : null" @restore="restoreView" @color-range="setColorRange" @reset="resetView" />
    <p v-if="viewContext" class="mb-2 text-xs">{{ semanticNote }}；其余维度按当前索引固定。参数恢复会重新校验文件版本。</p>
    <div v-show="datasets.length" ref="target" class="flex flex-none flex-col bg-white" style="height: 480px" />
    <p v-if="!loading && !datasets.length" class="text-xs">目录中没有当前预算内可绘制的数值节点；元数据仍可浏览。</p>
    <p v-if="typeof metadata.calendar === 'string' && metadata.calendar" class="text-xs">时间日历：{{ metadata.calendar }}；时间值未转换为公历日期。</p>
    <p v-if="sampled" class="text-xs text-amber-700">显示有界切片／降采样；其余维度默认索引 0。</p>
  </PreviewFrame>
</template>
<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue';
import type { FileInfo } from '../../../api/file';
import type { VisualizationPlugin } from '../../contract';
import { usePreviewLoad } from '../../../composables/usePreviewLoad';
import { requestPreview } from '../runtime';
import { parseArray, plainLabel, type PreviewArray } from './data';
import ViewToolbar from '../../ViewToolbar.vue';
import { viewSemantics, type NumericRange, type VisualizationViewContext } from '../../viewContext';
import { finiteExtent } from '../../plot';
import PreviewFrame from './PreviewFrame.vue';
const props = defineProps<{ file: FileInfo; plugin: VisualizationPlugin }>();
const emit=defineEmits<{(e:'view-context',context:VisualizationViewContext|null):void}>();
const viewContext=ref<VisualizationViewContext|null>(null),colorRange=ref<NumericRange>(),renderedArray=ref<PreviewArray>(),metadata=ref<Record<string,unknown>>({});
let appliedParams:Record<string,unknown>={},redraw:(()=>void)|undefined,renderEpoch=0;
const semanticNote=computed(()=>`${viewSemantics(metadata.value).valueSemantics==='physical'?'物理值':'原始存储值'}${typeof metadata.value.units==='string'&&metadata.value.units?' · '+metadata.value.units:''}；${typeof metadata.value.missing_value_policy==='string'?metadata.value.missing_value_policy:'非有限值保持缺失'}`);
function publishContext(){if(!version.value||!renderedArray.value)return;viewContext.value={schemaVersion:1,fileId:props.file.file_id,version:version.value,pluginId:props.plugin.id,pluginVersion:props.plugin.version,adapter:props.plugin.adapter,family:'h5web',request:{operation:'preview',params:JSON.parse(JSON.stringify(appliedParams))},view:{kind:String(appliedParams.kind),title:String(appliedParams.path),...viewSemantics(metadata.value),sampled:sampled.value,...(colorRange.value?{colorRange:[...colorRange.value] as NumericRange}:{})}};emit('view-context',viewContext.value);}
function clearContext(){viewContext.value=null;emit('view-context',null);redraw=undefined;}
function setColorRange(range:NumericRange|undefined){colorRange.value=range;redraw?.();publishContext();}
function resetView(){renderEpoch++;redraw?.();publishContext();}
async function restoreView(context:VisualizationViewContext){const p=context.request.params;if(!datasets.value.some(d=>d.path===p.path)){error.value='恢复参数中的变量不在当前目录。';return;}path.value=String(p.path);kind.value=p.kind as 'series'|'heatmap';indices.value=[...(p.indices as number[])];colorRange.value=context.view.colorRange;await loadData();}
const scope = usePreviewLoad(), target = ref<HTMLDivElement>(), loading = ref(false), error = ref(''), sampled = ref(false);
const datasets = ref<Array<{ path: string; shape?: number[] }>>([]), path = ref(''), kind = ref<'series' | 'heatmap'>('series');
const nodes = ref<Array<{ path: string; node_type: string; shape?: number[]; dtype?: string }>>([]), warnings = ref<string[]>([]), version = ref<string>();
const indices = ref<number[]>([]);
const sliceShape = computed(() => { const shape = datasets.value.find((node) => node.path === path.value)?.shape ?? []; return shape.slice(0, Math.max(0, shape.length - (kind.value === 'series' ? 1 : 2))); });
async function loadData() {
  clearContext();const load = scope.begin(); loading.value = true; error.value = '';
  try {
  const requested={path:path.value,kind:kind.value,indices:sliceShape.value.map((size,index)=>{const n=indices.value[index]??0;if(!Number.isSafeInteger(n)||n<0||n>=size)throw new Error('切片索引超出范围。');return n;})};
    const result = await requestPreview(props.file, props.plugin, { version: version.value, ...requested }, load.signal);
    load.assertCurrent();
    if(result.version!==version.value)throw new Error('文件版本变化，请重新打开。');
    const array = parseArray(result.array);
    const [React, ReactDom, H5, ndarray] = await Promise.all([import('react'), import('react-dom/client'), import('@h5web/lib'), import('ndarray'), import('@h5web/lib/styles.css')]);
    load.assertCurrent(); await nextTick(); load.assertCurrent();
    // React 18 publishes CommonJS: dynamic-import interop may expose only default.
    const react = React.default ?? React, reactDom = ReactDom.default ?? ReactDom;
    const root = reactDom.createRoot(target.value!); load.onDispose(() => root.unmount());
    const finite = array.values.filter((value): value is number => value !== null);
    const domain: [number, number] | undefined = finite.length ? [Math.min(...finite), Math.max(...finite)] : undefined;
    const dataArray = ndarray.default(Float64Array.from(array.values.map((value) => value ?? NaN)), array.shape);
    const meta = result.metadata as Record<string, unknown> | undefined;
    metadata.value=meta??{};renderedArray.value=array;appliedParams=requested;
    const steps = Array.isArray(meta?.strides) && meta.strides.every((step) => typeof step === 'number' && step >= 1) ? meta.strides as number[] : array.shape.map(() => 1);
    const common = { dataArray, domain, title: plainLabel(requested.path), showGrid: true, abscissaParams: { label: '原数组索引', value: Float64Array.from({ length: array.shape[array.shape.length - 1]! }, (_, index) => index * (steps[steps.length - 1] ?? 1)) } };
    redraw=()=>root.render(array.shape.length === 1 ? react.createElement(H5.LineVis, {...common,key:renderEpoch}) : react.createElement(H5.HeatmapVis, { ...common,key:renderEpoch,domain:colorRange.value??domain, colorMap: 'Viridis', ordinateParams: { label: '原数组索引', value: Float64Array.from({ length: array.shape[0]! }, (_, index) => index * (steps[0] ?? 1)) } }));redraw();
    warnings.value = Array.isArray(result.warnings) ? result.warnings.map(value => plainLabel(value)) : [];
    sampled.value = result.sampled === true;publishContext();
  } catch (reason) { if (load.isCurrent()) error.value = reason instanceof Error ? reason.message : 'HDF5 切片读取失败。'; }
  finally { if (load.isCurrent()) loading.value = false; }
}
watch([() => props.file.file_id, () => props.plugin.id], async () => {
  clearContext();colorRange.value=undefined;renderedArray.value=undefined;
  const load = scope.begin(); loading.value = true; error.value = ''; datasets.value = []; nodes.value = []; warnings.value = []; version.value = undefined; path.value = '';
  try {
    const result = await requestPreview(props.file, props.plugin, { kind: 'tree' }, load.signal); load.assertCurrent();
    if (!Array.isArray(result.tree) || result.tree.length > 256 || typeof result.version !== 'string' || !/^[a-f0-9]{64}$/.test(result.version)) throw new Error('变量目录或文件版本无效。');
    version.value = result.version;
    nodes.value = result.tree.filter((node): node is { path: string; node_type: string; shape?: number[]; dtype?: string } => !!node && typeof node === 'object' && typeof node.path === 'string' && node.path.length <= 512 && typeof node.node_type === 'string');
    const variables = (result.choices as { variables?: unknown[] } | undefined)?.variables;
    if (!Array.isArray(variables)) throw new Error('变量目录缺少批准的数值节点。');
    datasets.value = nodes.value.filter(node => node.node_type === 'dataset' && variables.includes(node.path) && Array.isArray(node.shape) && node.shape.length > 0);
    warnings.value = Array.isArray(result.warnings) ? result.warnings.map(value => plainLabel(value)) : [];
    sampled.value = result.sampled === true;
    path.value = datasets.value[0]?.path ?? '';
    if (path.value) await loadData();
  } catch (reason) { if (load.isCurrent()) error.value = reason instanceof Error ? reason.message : 'HDF5 目录读取失败。'; }
  finally { if (load.isCurrent()) loading.value = false; }
}, { immediate: true });
</script>
