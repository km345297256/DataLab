<template>
  <div class="view-tools">
    <template v-if="context">
      <span>{{ semantics }}<template v-if="context.view.units"> · {{ context.view.units }}</template> · {{ context.view.sampled ? '局部／抽样预览' : '当前读取范围' }}</span>
      <button v-for="format in imageFormats" :key="format" @click="$emit('image', format)">导出 {{ format.toUpperCase() }}</button>
      <button @click="exportJson">保存视图参数</button>
      <label class="import">恢复视图参数<input type="file" accept="application/json,.json" @change="restore" /></label>
      <button @click="$emit('reset')">复位缩放／选区</button>
      <template v-if="color">
        <label><input v-model="locked" type="checkbox" @change="applyColor" />固定色阶</label>
        <template v-if="locked"><label>最小<input v-model.number="low" type="number" step="any" aria-label="色阶最小值" /></label><label>最大<input v-model.number="high" type="number" step="any" aria-label="色阶最大值" /></label><button @click="applyColor">应用色阶</button></template>
      </template>
      <p v-if="context.selection">已选 {{ context.selection.x.map(format).join(' ～ ') }}<template v-if="context.selection.y"> × {{ context.selection.y.map(format).join(' ～ ') }}</template>（两端包含）</p>
    </template>
    <p v-if="error" role="alert">{{ error }}</p>
  </div>
</template>
<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { bindViewContext, parseVisualizationViewContext, saveViewBlob, validRange, type NumericRange, type VisualizationViewContext } from './viewContext';
const props=withDefaults(defineProps<{context:VisualizationViewContext|null;imageFormats?:('png'|'svg')[];color?:boolean;extent?:NumericRange|null}>(),{imageFormats:()=>[],color:false,extent:null});
const emit=defineEmits<{(e:'image',format:'png'|'svg'):void;(e:'restore',context:VisualizationViewContext):void;(e:'color-range',range:NumericRange|undefined):void;(e:'reset'):void}>();
const error=ref(''),locked=ref(false),low=ref(0),high=ref(1);
const format=(value:number)=>Number(value.toPrecision(7)).toString();
const semantics=computed(()=>({raw:'原始存储值',physical:'物理值',derived:'派生统计',unknown:'值语义未声明'}[props.context?.view.valueSemantics??'unknown']));
watch(()=>props.context?.view.colorRange,r=>{locked.value=!!r;const range=r??props.extent;if(range){low.value=range[0];high.value=range[1];}},{immediate:true});
watch(()=>props.extent,r=>{if(r&&!locked.value){low.value=r[0];high.value=r[1];}});
function applyColor(){error.value='';if(locked.value&&!validRange([low.value,high.value],false)){error.value='色阶需要有限数值，且最大值大于最小值。';return;}emit('color-range',locked.value?[low.value,high.value]:undefined);}
function exportJson(){try{saveViewBlob(new Blob([JSON.stringify(parseVisualizationViewContext(props.context),null,2)],{type:'application/json'}),'visualization-view.json');}catch(reason){error.value=reason instanceof Error?reason.message:'参数导出失败。';}}
async function restore(event:Event){const input=event.target as HTMLInputElement,file=input.files?.[0];input.value='';if(!file||!props.context)return;const current=props.context;try{if(file.size>16384)throw new Error('视图参数文件不得超过 16 KiB。');const data=JSON.parse(await file.text());if(props.context!==current)throw new Error('当前视图已变化，请重新导入。');emit('restore',bindViewContext(data,current));error.value='';}catch(reason){error.value=reason instanceof Error?reason.message:'参数读取失败。';}}
</script>
<style scoped>
.view-tools{display:flex;flex-wrap:wrap;gap:8px;align-items:center;font-size:12px;flex-shrink:0;padding:8px 0}.view-tools label{display:flex;gap:4px;align-items:center}.view-tools button,.import{border:1px solid #cbd5e1;border-radius:4px;padding:4px 7px;cursor:pointer}.import input{display:none}.view-tools input[type=number]{width:90px;border:1px solid #cbd5e1}.view-tools p{width:100%;margin:0}.view-tools [role=alert]{color:#b45309}
</style>
