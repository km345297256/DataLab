<template>
  <div class="flex min-h-0 flex-1 flex-col bg-[var(--background-gray-main)]">
    <div class="flex shrink-0 flex-wrap items-center justify-between gap-2 border-b border-[var(--border-main)] px-4 py-2 text-xs text-[var(--text-tertiary)]">
      <div class="flex flex-wrap items-center gap-2">
        <span class="font-medium text-[var(--text-secondary)]">HTML 预览</span>
        <span v-if="resolvedCount">已载入 {{ resolvedCount }} 个报告资源</span>
        <span v-if="blockedCount">已阻止 {{ blockedCount }} 处外部或主动资源</span>
        <span v-if="missingResources.length" class="text-[var(--function-warning,#b7791f)]">未找到 {{ missingResources.length }} 个资源</span>
      </div>
      <span>仅使用当前报告资源 · 外部连接、表单和弹窗已禁用</span>
    </div>

    <div v-if="status" class="flex min-h-0 flex-1 items-center justify-center p-4">
      <div class="max-w-[520px] rounded-xl border border-[var(--border-main)] bg-[var(--background-menu-white)] px-4 py-3 text-sm text-[var(--text-secondary)]">
        {{ status }}
        <button v-if="failed" type="button" class="mt-3 block rounded-lg border border-[var(--border-main)] px-3 py-1.5" @click="loadHtml(file)">重试</button>
      </div>
    </div>

    <div v-else class="min-h-0 flex-1 overflow-hidden bg-white">
      <iframe
        class="h-full w-full border-0 bg-white"
        sandbox="allow-scripts"
        title="隔离 HTML 预览"
        referrerpolicy="no-referrer"
        :srcdoc="previewHtml"
      />
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue';
import { useRoute } from 'vue-router';
import type { FileInfo } from '../../api/file';
import { loadPluginBytes, loadPluginText } from '../../visualizations/runtime';
import type { VisualizationPlugin } from '../../visualizations/contract';
import { getSessionFiles, getSharedSessionFiles } from '../../api/agent';
import { useFilePanel } from '../../composables/useFilePanel';
import { useSessionFileList } from '../../composables/useSessionFileList';
import { usePreviewLoad, type PreviewLoad } from '../../composables/usePreviewLoad';
import { HTML_INPUT_BYTES, HTML_RESOURCE_BYTES, HtmlPreviewError, prepareHtmlPreview } from '../../visualizations/htmlPreviewPolicy';

const props = defineProps<{
  file: FileInfo;
  plugin: VisualizationPlugin;
}>();

const { relatedFiles } = useFilePanel();
const { shared } = useSessionFileList();
const route = useRoute();
const status = ref('');
const previewHtml = ref('');
const missingResources = ref<string[]>([]);
const resolvedCount = ref(0);
const blockedCount = ref(0);
const failed = ref(false);
const loads = usePreviewLoad();

const ensureRelatedFiles = async (load: PreviewLoad, force = false) => {
  load.assertCurrent();
  if (!force && relatedFiles.value.length > 1) return;
  const sessionId = route.params.sessionId as string;
  if (!sessionId) return;
  try {
    const sessionFiles = shared.value || route.path.startsWith('/share/')
      ? await getSharedSessionFiles(sessionId)
      : await getSessionFiles(sessionId);
    load.assertCurrent();
    const filesById = new Map(
      [...relatedFiles.value, ...sessionFiles].map((file) => [file.file_id, file]),
    );
    relatedFiles.value = Array.from(filesById.values());
  } catch (error) {
    if (load.isCurrent()) console.warn('Failed to load related HTML files:', error);
  }
};

const loadHtml = async (file: FileInfo) => {
  const load = loads.begin();
  status.value = '正在加载 HTML...';
  previewHtml.value = '';
  missingResources.value = [];
  resolvedCount.value = 0;
  blockedCount.value = 0;
  failed.value = false;

  try {
    if (file.size != null && (!Number.isSafeInteger(file.size) || file.size < 0 || file.size > HTML_INPUT_BYTES)) throw new HtmlPreviewError('HTML 正文超过 16 MiB 预览上限，请精简报告后重试。');
    const html = await loadPluginText(file, { ...props.plugin, limits: { ...props.plugin.limits, max_input_bytes: HTML_INPUT_BYTES } }, load.signal);
    load.assertCurrent();
    await ensureRelatedFiles(load, true);
    if (!load.isCurrent()) return;
    const result = await prepareHtmlPreview(html, file, relatedFiles.value, load, (resource, maxBytes) =>
      loadPluginBytes(file, { ...props.plugin, limits: { ...props.plugin.limits, max_input_bytes: Math.min(HTML_RESOURCE_BYTES, maxBytes) } }, load.signal, resource));
    if (!load.isCurrent()) return;
    previewHtml.value = result.html;
    resolvedCount.value = result.resources;
    blockedCount.value = result.blocked;
    missingResources.value = result.missing;
    status.value = '';
  } catch (error) {
    if (!load.isCurrent()) return;
    // Cancel sibling reads and discard the incomplete document on failure.
    loads.begin();
    failed.value = true;
    status.value = error instanceof HtmlPreviewError ? error.message : 'HTML 预览失败。请确认文件可访问，且关联资源已同步到当前任务文件列表。';
  }
};

watch(() => props.file, loadHtml, { immediate: true });
</script>
