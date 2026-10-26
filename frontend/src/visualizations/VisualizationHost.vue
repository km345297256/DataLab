<template>
  <div class="flex min-h-0 flex-1 flex-col">
    <div class="flex shrink-0 flex-wrap items-center gap-2 border-b border-[var(--border-main)] px-4 py-2 text-xs text-[var(--text-secondary)]">
      <label v-if="candidates.length" class="flex min-w-0 flex-1 items-center gap-2">
        可视化
        <select :value="selectedPlugin?.id || ''" class="min-w-0 flex-1 rounded-md border border-[var(--border-main)] bg-[var(--background-menu-white)] px-2 py-1.5 text-[var(--text-primary)]" aria-label="选择可视化插件" @change="selectedId = ($event.target as HTMLSelectElement).value">
          <option v-for="plugin in candidates" :key="plugin.id" :value="plugin.id">{{ plugin.name }} · {{ viewKindLabel(plugin.view_kind) }}{{ visualizationBlockedReason(plugin, file, shared) ? ' · 暂不可用' : '' }}</option>
        </select>
      </label>
      <button type="button" class="shrink-0 underline disabled:opacity-50" :disabled="loading" @click="manualRefresh">刷新</button>
      <button v-if="viewContext && !shared && taskData.active.value" type="button" class="shrink-0 rounded border border-[var(--border-main)] px-2 py-1.5 disabled:opacity-50" :disabled="taskData.locked.value" @click="addViewToAnalysis">{{ viewContext.selection ? '分析当前选区' : '分析当前窗口' }}</button>
    </div>
    <p v-if="stale" role="status" class="border-b border-amber-200 bg-amber-50 px-4 py-2 text-xs text-amber-900">插件目录暂时无法更新，已保留当前预览。连接恢复后会自动确认状态。</p>
    <p v-if="profileBusy" role="status" class="px-4 py-2 text-xs text-[var(--text-tertiary)]">正在有界识别文件内容…</p>
    <p v-else-if="profile" class="px-4 py-2 text-xs text-[var(--text-tertiary)]">{{ profileLabel(profile) }} · 可手动切换视图</p>
    <p v-else-if="profileError" class="px-4 py-2 text-xs text-[var(--text-tertiary)]">{{ profileError }}</p>
    <p v-if="selectionNotice" role="status" class="px-4 py-2 text-xs text-[var(--text-secondary)]">{{ selectionNotice }}</p>
    <div v-if="error" role="alert" class="m-4 rounded-lg border border-amber-300 bg-amber-50 p-4 text-sm text-amber-900">{{ error }}</div>
    <div v-else-if="!catalog && loading" role="status" class="p-8 text-center text-sm text-[var(--text-tertiary)]">正在加载 Cordis 可视化插件…</div>
    <div v-else-if="!selectedPlugin" class="p-8 text-center text-sm leading-6 text-[var(--text-tertiary)]">此格式暂不支持在线预览，可下载原始文件。</div>
    <div v-else-if="blockedReason" role="alert" class="p-8 text-center text-sm leading-6 text-[var(--text-tertiary)]">{{ blockedReason }}</div>
    <component v-else-if="adapter && !profileBusy" :is="adapter" :key="instanceKey" :file="file" :plugin="selectedPlugin" @view-context="receiveViewContext" />
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue';
import { useRoute } from 'vue-router';
import type { FileInfo } from '../api/file';
import { useVisualizationCatalog } from './catalog';
import { matchingVisualizations, viewKindLabel } from './contract';
import { getVisualizationAdapter } from './adapters';
import { requestVisualization } from './runtime';
import { filePreviewIdentity, pluginPreviewIdentity } from './previewIdentity';
import { canProfileFilename, parseContentProfile, profileCandidates, profileLabel, type ContentProfile } from './contentProfile';
import { preferredVisualization, visualizationBlockedReason } from './selection';
import { parseVisualizationViewContext, type VisualizationViewContext } from './viewContext';
import { useTaskData } from '../composables/useTaskData';
import { eventBus } from '../utils/eventBus';

const props = defineProps<{ file: FileInfo }>();
const route = useRoute();
const shared = computed(() => route.path.startsWith('/share/'));
const { catalog, error, stale, loading, refresh } = useVisualizationCatalog();
const selectedId = ref<string | null>(null);
const taskData = useTaskData();
const viewContext = ref<VisualizationViewContext | null>(null), selectionNotice = ref('');
function receiveViewContext(value: unknown) {
  viewContext.value = null;
  if (value == null) return;
  try {
    const context = parseVisualizationViewContext(value);
    if (context.fileId !== props.file.file_id || context.pluginId !== selectedPlugin.value?.id
      || context.pluginVersion !== selectedPlugin.value.version || context.adapter !== selectedPlugin.value.adapter) return;
    viewContext.value = context;
  } catch { /* Invalid or obsolete child scopes never become analysis inputs. */ }
}
function addViewToAnalysis() {
  if (!viewContext.value || shared.value || taskData.locked.value || !taskData.sessionId.value) return;
  const context = parseVisualizationViewContext(viewContext.value);
  eventBus.emit('visualization-analysis-draft', { sessionId: taskData.sessionId.value, file: props.file, context,
    onAccepted: (accepted: boolean) => { selectionNotice.value = accepted ? '范围已加入输入框；可补充问题后发送。发送时会重新校验文件和范围。' : '当前无法加入范围，请确认文件已在本任务数据中且分析未运行。'; } });
}
const refreshEpoch = ref(0);
const profile = ref<ContentProfile | null>(null), profileBusy = ref(false), profileError = ref('');
const filenameCandidates = computed(() => matchingVisualizations(catalog.value?.plugins ?? [], props.file.filename));
const candidates = computed(() => profileCandidates(filenameCandidates.value, profile.value));
const selectedPlugin = computed(() => preferredVisualization(candidates.value, props.file, shared.value, selectedId.value));
const adapter = computed(() => selectedPlugin.value ? getVisualizationAdapter(selectedPlugin.value.adapter) : null);
const fileIdentity = computed(() => filePreviewIdentity(props.file));
// A capability version/state revision, file, or user-selected view replacement owns a
// fresh Vue scope. Unmount aborts requests and disposes canvas/WebGL/blob resources.
const instanceKey = computed(() => JSON.stringify([fileIdentity.value, catalog.value?.revision,
  selectedPlugin.value ? pluginPreviewIdentity(selectedPlugin.value) : null, refreshEpoch.value]));
watch(instanceKey, () => { viewContext.value = null; selectionNotice.value = ''; }, { flush: 'sync' });
const manualRefresh = async () => {
  const previousKey = instanceKey.value;
  const confirmed = await refresh();
  // A genuine capability/file change already replaced the scope. Do not load it twice,
  // or refresh a different file that the user opened while confirmation was pending.
  if (confirmed && catalog.value && !error.value && instanceKey.value === previousKey) refreshEpoch.value += 1;
};
const blockedReason = computed(() => selectedPlugin.value ? visualizationBlockedReason(selectedPlugin.value, props.file, shared.value) : '');
watch(() => props.file.file_id, () => { selectedId.value = null; });
// A profile only chooses among already authorized, enabled candidates. A fresh
// generation owns its request; disable/reload/close cannot resurrect old content.
watch(() => JSON.stringify([fileIdentity.value, props.file.filename, catalog.value?.revision, refreshEpoch.value,
  filenameCandidates.value.map(pluginPreviewIdentity), shared.value]), async (_, __, onCleanup) => {
  profile.value = null; profileError.value = ''; profileBusy.value = false;
  if (shared.value || !canProfileFilename(props.file.filename)) return;
  const plugin = filenameCandidates.value.find(p => p.capabilities.operations.includes('prepare')
    && ['tiff', 'plotly', 'h5web', 'viv', 'openlayers'].includes(p.adapter)
    && !visualizationBlockedReason(p, props.file, shared.value));
  if (!plugin) return;
  const controller = new AbortController();
  onCleanup(() => controller.abort());
  profileBusy.value = true;
  try {
    const result = await requestVisualization(props.file, plugin, 'prepare', {}, controller.signal);
    if (controller.signal.aborted) return;
    if (result.kind !== 'resources' || result.metadata.purpose !== 'content-profile' || result.revision !== catalog.value?.revision) throw new Error('内容画像已过期。');
    profile.value = parseContentProfile(result.payload.profile);
  } catch {
    if (!controller.signal.aborted) profileError.value = '内容识别未完成；仍可选择已有视图，由对应读取器验证格式。';
  } finally { if (!controller.signal.aborted) profileBusy.value = false; }
}, { immediate: true });
let poll: ReturnType<typeof setInterval> | undefined;
const refreshVisible = () => { if (document.visibilityState !== 'hidden') void refresh(); };
onMounted(() => {
  void refresh();
  poll = setInterval(refreshVisible, 15000);
  window.addEventListener('focus', refreshVisible);
});
onUnmounted(() => {
  if (poll) clearInterval(poll);
  window.removeEventListener('focus', refreshVisible);
});
</script>
