<template>
  <DialogRoot :open="true" @update:open="value => { if (!value) emit('close'); }">
    <DialogPortal>
      <DialogOverlay class="skill-preview-overlay" />
      <DialogContent class="skill-preview-dialog" @close-auto-focus="event => event.preventDefault()">
        <header class="skill-preview-heading">
          <div>
            <DialogTitle class="skill-preview-title">技能文件预览</DialogTitle>
            <DialogDescription class="skill-preview-description">{{ skillName }} · 只读</DialogDescription>
          </div>
          <DialogClose class="skill-preview-icon-button" aria-label="关闭预览"><X :size="20" aria-hidden="true" /></DialogClose>
        </header>
        <div class="skill-preview-toolbar">
          <label class="skill-preview-selector">
            <FileCode2 :size="16" aria-hidden="true" />
            <select v-model="selected" aria-label="选择技能文件">
              <option v-for="path in resources" :key="path" :value="path">{{ path }}</option>
            </select>
          </label>
          <div class="skill-preview-actions">
            <div v-if="isMarkdown" class="skill-preview-modes" role="group" aria-label="查看方式">
              <button type="button" :aria-pressed="!sourceMode" @click="sourceMode = false">预览</button>
              <button type="button" :aria-pressed="sourceMode" @click="sourceMode = true">源码</button>
            </div>
            <button type="button" :disabled="loading || resource === null" @click="copyContent"><Copy :size="14" aria-hidden="true" />复制内容</button>
          </div>
        </div>
        <p v-if="copyNotice" role="status" class="skill-preview-notice">{{ copyNotice }}</p>
        <div class="skill-preview-body" :aria-busy="loading">
          <p v-if="loading" role="status" class="skill-preview-state"><LoaderCircle :size="18" class="animate-spin" aria-hidden="true" />正在读取文件…</p>
          <div v-else-if="error" role="alert" class="skill-preview-state"><p>{{ error }}</p><button type="button" @click="load">重试预览</button></div>
          <p v-else-if="resource?.content === ''" class="skill-preview-state">此文件为空。</p>
          <article v-else-if="resource && isMarkdown && !sourceMode" class="skill-preview-markdown prose prose-gray max-w-none dark:prose-invert" @click="followResource" v-html="renderedMarkdown" />
          <pre v-else-if="resource" class="skill-preview-source" tabindex="0" :aria-label="`${selected} 文件内容`"><code>{{ displayedSource }}</code></pre>
        </div>
      </DialogContent>
    </DialogPortal>
  </DialogRoot>
</template>

<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue';
import { DialogClose, DialogContent, DialogDescription, DialogOverlay, DialogPortal, DialogRoot, DialogTitle } from 'reka-ui';
import { Copy, FileCode2, LoaderCircle, X } from 'lucide-vue-next';
import { getSkillDraftResource, type SkillDraftResource } from '../api/skillDraft';
import { formatSkillResource, renderSkillMarkdown } from '../utils/skillResourcePreview';

const props = defineProps<{ draftId: string; skillName: string; resources: string[]; initialPath: string }>();
const emit = defineEmits<{ close: [] }>();
const selected = ref(props.initialPath), sourceMode = ref(false);
const resource = ref<SkillDraftResource | null>(null);
const loading = ref(false), error = ref(''), copyNotice = ref('');
const isMarkdown = computed(() => /\.(md|markdown)$/i.test(selected.value));
const displayedSource = computed(() => resource.value ? formatSkillResource(selected.value, resource.value.content) : '');
const renderedMarkdown = computed(() => resource.value && isMarkdown.value && !sourceMode.value
  ? renderSkillMarkdown(resource.value.content, selected.value, props.resources) : '');
let controller: AbortController | null = null, version = 0, active = true;

async function load() {
  controller?.abort();
  const request = new AbortController(); controller = request;
  const current = ++version, path = selected.value;
  resource.value = null; error.value = ''; copyNotice.value = ''; loading.value = true;
  try {
    if (!props.resources.includes(path)) throw new Error('Unavailable resource');
    const result = await getSkillDraftResource(props.draftId, path, request.signal);
    if (!active || version !== current) return;
    if (result.path !== path || typeof result.content !== 'string') throw new Error('Invalid resource response');
    resource.value = result;
  } catch {
    if (active && version === current) error.value = '暂时无法预览此文件，请重试。';
  } finally {
    if (active && version === current) loading.value = false;
  }
}

async function copyContent() {
  if (!resource.value || loading.value) return;
  const current = version;
  try {
    await navigator.clipboard.writeText(resource.value.content);
    if (active && version === current) copyNotice.value = '已复制文件原文。';
  } catch {
    if (active && version === current) copyNotice.value = '复制失败，请选中文本后复制。';
  }
}

function followResource(event: MouseEvent) {
  const link = (event.target as Element).closest<HTMLAnchorElement>('a[data-skill-resource]');
  if (!link) return;
  event.preventDefault();
  if (link.dataset.skillResource && props.resources.includes(link.dataset.skillResource)) selected.value = link.dataset.skillResource;
}

watch(() => [props.draftId, selected.value], () => { sourceMode.value = false; void load(); }, { immediate: true });
onUnmounted(() => { active = false; version++; controller?.abort(); resource.value = null; });
</script>

<style scoped>
.skill-preview-overlay{position:fixed;inset:0;z-index:1100;background:rgb(0 0 0 / .35)}
.skill-preview-dialog{position:fixed;left:50%;top:50%;z-index:1101;transform:translate(-50%,-50%);display:flex;flex-direction:column;width:min(960px,calc(100vw - 32px));height:min(800px,85dvh);max-height:calc(100dvh - 32px);min-width:0;overflow:hidden;border:1px solid var(--border-main);border-radius:16px;background:var(--background-menu-white);color:var(--text-primary);box-shadow:0 20px 70px rgb(0 0 0 / .2);outline:none}
.skill-preview-heading{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:18px 20px}.skill-preview-heading>div{min-width:0}.skill-preview-title{font-size:16px;font-weight:600}.skill-preview-description{margin-top:4px;font-size:12px;color:var(--text-tertiary);overflow-wrap:anywhere}.skill-preview-icon-button{display:flex;align-items:center;justify-content:center;width:32px;height:32px;flex-shrink:0;border-radius:8px}.skill-preview-icon-button:hover{background:var(--fill-tsp-gray-main)}
.skill-preview-toolbar{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px;padding:12px 20px;border-top:1px solid var(--border-light);border-bottom:1px solid var(--border-light)}.skill-preview-selector{display:flex;align-items:center;gap:8px;min-width:0;flex:1 1 250px}.skill-preview-selector>svg{flex-shrink:0}.skill-preview-selector select{min-width:0;width:100%;padding:6px 4px;background:var(--background-menu-white);color:var(--text-primary);font-size:13px;text-overflow:ellipsis}.skill-preview-actions{display:flex;align-items:center;flex-wrap:wrap;gap:12px}.skill-preview-actions button,.skill-preview-state button{display:inline-flex;align-items:center;justify-content:center;gap:5px;padding:6px 9px;border:1px solid var(--border-main);border-radius:7px;font-size:12px}.skill-preview-actions button:disabled{opacity:.45}.skill-preview-modes{display:flex;gap:3px}.skill-preview-modes button{border-color:transparent}.skill-preview-modes button[aria-pressed=true]{background:var(--fill-tsp-gray-main);border-color:var(--border-main)}.skill-preview-notice{padding:7px 20px;font-size:12px;color:var(--text-tertiary)}
.skill-preview-body{flex:1;min-height:0;min-width:0;overflow:auto;overscroll-behavior:contain}.skill-preview-state{display:flex;align-items:center;justify-content:center;flex-wrap:wrap;gap:12px;padding:40px 20px;font-size:13px;color:var(--text-secondary)}.skill-preview-source{margin:0;padding:20px;font:12px/1.75 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;tab-size:4;white-space:pre-wrap;overflow-wrap:anywhere}.skill-preview-markdown{padding:20px;font-size:14px;line-height:1.8;overflow-wrap:anywhere}.skill-preview-markdown :deep(pre){max-width:100%;overflow:auto}.skill-preview-markdown :deep(table){display:block;max-width:100%;overflow:auto}.skill-preview-markdown :deep(a[href]){color:#2563eb;cursor:pointer;text-decoration:underline}.skill-preview-markdown :deep(a:not([href])){color:inherit}
button:focus-visible,select:focus-visible,pre:focus-visible{outline:2px solid var(--text-secondary);outline-offset:2px}@media(max-width:480px){.skill-preview-dialog{width:calc(100vw - 16px);height:90dvh;border-radius:12px}.skill-preview-heading{padding:14px}.skill-preview-toolbar{padding:10px 14px}.skill-preview-actions{width:100%;justify-content:flex-end}.skill-preview-source,.skill-preview-markdown{padding:14px}}
</style>
