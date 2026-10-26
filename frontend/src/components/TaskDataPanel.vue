<template>
  <div ref="container" class="task-data-slot" :class="{ 'is-open': panelOpen, 'is-compact': compact }" :style="{ width: panelOpen && !compact ? `${panelWidth}px` : '0px' }">
    <aside v-if="panelOpen" id="task-data-panel" data-testid="task-data-panel" class="task-data-panel" :role="compact ? 'dialog' : 'complementary'" :aria-modal="compact ? true : undefined" aria-label="任务数据" @keydown="handleKeydown">
      <div v-if="!compact" class="task-data-resize" role="separator" tabindex="0" aria-label="调整任务数据面板宽度" aria-orientation="vertical" :aria-valuemin="minimumWidth" :aria-valuemax="maximumWidth" :aria-valuenow="panelWidth" @pointerdown="startResize" @keydown="resizeKeydown"><span /></div>
      <header class="task-data-heading">
        <div><h2>任务数据 <span>{{ count }}</span></h2><p>本任务的资料与分析成果</p></div>
        <div class="task-heading-actions"><button type="button" class="task-icon-button" :disabled="loading" aria-label="刷新任务数据" title="刷新任务数据" @click="refresh"><RefreshCw :size="16" :class="{ spinning: loading }" /></button><button ref="closeButton" type="button" class="task-icon-button" aria-label="收起任务数据" title="收起任务数据" @click="closePanel"><X :size="19" /></button></div>
      </header>
      <div class="task-data-tabs" role="tablist" aria-label="任务数据分类">
        <button v-for="entry in tabs" :key="entry.id" :id="`task-data-tab-${entry.id}`" type="button" role="tab" :aria-selected="tab === entry.id" aria-controls="task-data-results" :tabindex="tab === entry.id ? 0 : -1" @click="tab = entry.id" @keydown="tabKeydown($event, entry.id)">{{ entry.label }} <span>{{ counts[entry.id] }}</span></button>
      </div>
      <div class="task-data-filters">
        <label class="task-data-search control-shell"><Search :size="15" aria-hidden="true" /><input v-model="query" type="search" maxlength="200" aria-label="查找任务文件" placeholder="按文件名查找" /></label>
        <div class="task-filter-row">
          <button type="button" :aria-pressed="onlySelected" @click="onlySelected = !onlySelected"><Check :size="13" :class="{ invisible: !onlySelected }" aria-hidden="true" />本轮已选 <span>{{ selectedIds.length }}</span></button>
          <button type="button" :aria-pressed="onlyUnsaved" @click="onlyUnsaved = !onlyUnsaved"><Check :size="13" :class="{ invisible: !onlyUnsaved }" aria-hidden="true" />未保存</button>
          <button v-if="query || onlySelected || onlyUnsaved" type="button" class="task-clear-filters" @click="clearFilters">清除筛选</button>
        </div>
      </div>
      <div ref="resultsElement" id="task-data-results" class="task-data-results" role="tabpanel" :aria-labelledby="`task-data-tab-${tab}`">
        <DataAcquisitionContent v-if="tab === 'download'" tasks-only />
        <p v-if="error" role="alert" class="task-data-notice is-warning">{{ error }} <button type="button" :disabled="loading" @click="refresh">重试</button></p>
        <p v-if="loading && !items.length" role="status" class="task-data-empty">正在读取任务数据…</p>
        <p v-else-if="!filteredItems.length" class="task-data-empty">{{ items.length ? '没有符合条件的文件' : '本任务还没有数据文件' }}</p>
        <ul v-else class="task-file-list" aria-label="任务文件">
          <li v-for="item in filteredItems" :key="item.file.file_id" class="task-file-row" :data-task-file-id="item.file.file_id">
            <div class="task-file-main">
              <input type="checkbox" :checked="saveSelection.has(item.file.file_id)" :disabled="!item.available || Boolean(item.saved_locations.length)" :aria-label="`选择保存 ${item.file.filename}`" :title="item.saved_locations.length ? '已保存，无需重复保存' : '选择批量保存'" @change="toggleSave(item.file.file_id)" />
              <FileText :size="18" class="task-file-icon" />
              <button type="button" class="task-file-name" :disabled="!item.available" :title="item.file.filename" @click="preview(item)">{{ item.file.filename }}</button>
              <button type="button" class="task-icon-button" :disabled="!item.available" :aria-label="`预览 ${item.file.filename}`" title="预览文件" @click="preview(item)"><Eye :size="16" /></button>
            </div>
            <div class="task-file-details">
              <p class="task-file-meta"><span>{{ categoryLabels[item.category] }}</span><span v-if="item.round_number">第 {{ item.round_number }} 轮</span><span v-if="item.created_at" :title="fullDate(item.created_at)">{{ shortDate(item.created_at) }}</span><span v-if="item.file.size != null">{{ formatFileSize(item.file.size) }}</span></p>
              <p v-if="selectedIds.includes(item.file.file_id)" class="task-file-input"><Check :size="12" />本轮分析已选</p>
              <p v-for="location in item.saved_locations" :key="`${location.asset_id}:${location.file_path}`" class="task-file-location" :title="`我的数据 / ${location.file_path}`"><FolderOpen :size="12" /><span>我的数据 / {{ location.file_path }}</span></p>
              <TaskDataFileActions :file-id="item.file.file_id" compact />
            </div>
          </li>
        </ul>
        <p v-if="unavailableCount" class="task-data-notice">{{ unavailableCount }} 个历史文件已不可用。</p>
      </div>
      <footer class="task-data-footer">
        <p v-if="actionError || batchError" role="alert" class="is-warning">{{ batchError || actionError }}</p>
        <div v-if="saveSelection.size" class="task-batch-actions"><span>已选 {{ saveSelection.size }} 个待保存文件</span><button type="button" class="task-save-button" @click="openBatchSave"><FolderPlus :size="14" />保存到我的数据</button><button type="button" class="task-icon-button" aria-label="清除待保存选择" @click="saveSelection = new Set()"><X :size="15" /></button></div>
        <p v-else>{{ locked ? '当前分析或恢复期间，资料选择暂时锁定。' : '勾选文件可批量保存，加入分析后将在下一轮使用。' }}</p>
      </footer>
    </aside>
    <SaveToLibraryDialog v-if="saveVisible && sessionId" :visible="saveVisible" :files="filesToSave" :session-id="sessionId" :default-name="filesToSave.length === 1 ? filesToSave[0]?.filename : '任务数据'" @close="saveVisible = false" @saved="saved" />
  </div>
</template>
<script setup lang="ts">
import { computed, nextTick, onUnmounted, ref, watch } from 'vue';
import { Check, Eye, FileText, FolderOpen, FolderPlus, RefreshCw, Search, X } from 'lucide-vue-next';
import type { TaskDataItem, TaskDataTab } from '../api/taskData';
import { useTaskData } from '../composables/useTaskData';
import { useResizeObserver } from '../composables/useResizeObserver';
import { useFilePanel } from '../composables/useFilePanel';
import { formatFileSize } from '../utils/fileType';
import TaskDataFileActions from './TaskDataFileActions.vue';
import DataAcquisitionContent from './DataAcquisitionContent.vue';
import SaveToLibraryDialog from './SaveToLibraryDialog.vue';
const taskData = useTaskData();
const { sessionId, items, selectedIds, locked, panelOpen, previewFromTaskData, tab, loading, error, actionError, unavailableCount, count, counts, refresh } = taskData;
const { showFilePanel } = useFilePanel();
const tabs: Array<{ id: TaskDataTab; label: string }> = [{ id: 'all', label: '全部' }, { id: 'upload', label: '上传与选用' }, { id: 'analysis', label: '分析成果' }, { id: 'download', label: '搜索下载' }];
const categoryLabels = { upload: '上传与选用', analysis: '分析成果', download: '搜索下载' };
const query = ref(''), onlySelected = ref(false), onlyUnsaved = ref(false);
const saveSelection = ref(new Set<string>()), saveVisible = ref(false), batchError = ref('');
const filesToSave = computed(() => items.value.filter(item => saveSelection.value.has(item.file.file_id) && item.available && !item.saved_locations.length).map(item => item.file));
const filteredItems = computed(() => items.value.filter(item => (tab.value === 'all' || item.category === tab.value)
  && (!onlySelected.value || selectedIds.value.includes(item.file.file_id))
  && (!onlyUnsaved.value || !item.saved_locations.length)
  && item.file.filename.toLocaleLowerCase().includes(query.value.trim().toLocaleLowerCase())));
function clearFilters() { query.value = ''; onlySelected.value = false; onlyUnsaved.value = false; }
function toggleSave(id: string) {
  const item = items.value.find(item => item.file.file_id === id);
  if (!item?.available || item.saved_locations.length) return;
  const next = new Set(saveSelection.value);
  if (next.has(id)) next.delete(id);
  else if (next.size < 32) next.add(id);
  else { batchError.value = '一次最多保存 32 个文件，请分批保存。'; return; }
  saveSelection.value = next; batchError.value = '';
}
function openBatchSave() { if (filesToSave.value.length && filesToSave.value.length <= 32) saveVisible.value = true; }
function saved() { saveVisible.value = false; saveSelection.value = new Set(); batchError.value = ''; void refresh(); }
function preview(item: TaskDataItem) {
  if (!item.available || !items.value.some(current => current.file.file_id === item.file.file_id)) return;
  previewFromTaskData.value = true; taskData.suspend(); showFilePanel(item.file, filteredItems.value.filter(entry => entry.available).map(entry => entry.file));
}
function shortDate(value: string) { const date = new Date(value); return Number.isNaN(date.getTime()) ? '' : date.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }); }
function fullDate(value: string) { const date = new Date(value); return Number.isNaN(date.getTime()) ? '' : date.toLocaleString('zh-CN', { hour12: false }); }
function tabKeydown(event: KeyboardEvent, id: TaskDataTab) {
  const index = tabs.findIndex(entry => entry.id === id);
  const next = event.key === 'ArrowRight' ? (index + 1) % tabs.length : event.key === 'ArrowLeft' ? (index + tabs.length - 1) % tabs.length : event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : undefined;
  if (next === undefined) return;
  event.preventDefault(); tab.value = tabs[next]!.id;
  void nextTick(() => container.value?.querySelector<HTMLButtonElement>(`#task-data-tab-${tab.value}`)?.focus());
}
watch(sessionId, () => { clearFilters(); saveSelection.value = new Set(); saveVisible.value = false; batchError.value = ''; });
watch(items, () => {
  const allowed = new Set(items.value.filter(item => item.available && !item.saved_locations.length).map(item => item.file.file_id));
  saveSelection.value = new Set([...saveSelection.value].filter(id => allowed.has(id)));
  if (!saveSelection.value.size) saveVisible.value = false;
});
const container = ref<HTMLElement>(), closeButton = ref<HTMLButtonElement>(), resultsElement = ref<HTMLElement>();
const viewKey = computed(() => JSON.stringify([sessionId.value, tab.value, query.value, onlySelected.value, onlyUnsaved.value]));
let renderedViewKey = viewKey.value;
let scrollSnapshot: { key: string; top: number } | undefined;
const { size } = useResizeObserver(container, { target: 'parent', property: 'width' });
const compact = computed(() => size.value < 1180);
const maximumWidth = computed(() => Math.max(280, size.value - 780));
const minimumWidth = computed(() => Math.min(360, maximumWidth.value));
const preferredWidth = ref(420);
const panelWidth = computed(() => Math.round(Math.min(maximumWidth.value, Math.max(minimumWidth.value, preferredWidth.value))));
let returnFocus: HTMLElement | null = null;
let resizing = false, startX = 0, startWidth = 0, oldCursor = '', oldSelect = '';
watch(panelOpen, async shown => {
  if (!shown) {
    if (resultsElement.value) scrollSnapshot = { key: renderedViewKey, top: resultsElement.value.scrollTop };
    stopResize(); return;
  }
  renderedViewKey = viewKey.value;
  returnFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  await nextTick();
  if (panelOpen.value) {
    closeButton.value?.focus({ preventScroll: true });
    if (resultsElement.value) resultsElement.value.scrollTop = scrollSnapshot?.key === renderedViewKey ? scrollSnapshot.top : 0;
  }
});
watch(viewKey, async key => {
  if (!panelOpen.value) return;
  renderedViewKey = key;
  await nextTick();
  if (panelOpen.value && viewKey.value === key && resultsElement.value) resultsElement.value.scrollTop = 0;
});
watch(compact, async narrow => { stopResize(); if (narrow && panelOpen.value) { await nextTick(); closeButton.value?.focus({ preventScroll: true }); } });
function closePanel() {
  taskData.close();
  void nextTick(() => {
    const target = returnFocus?.isConnected && returnFocus !== document.body && returnFocus.getClientRects().length ? returnFocus : document.querySelector<HTMLElement>('[aria-controls="task-data-panel"]');
    target?.focus({ preventScroll: true });
  });
}
function handleKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); closePanel(); return; }
  if (!compact.value || event.key !== 'Tab') return;
  const focusable = [...(container.value?.querySelectorAll<HTMLElement>('button:not(:disabled):not([tabindex="-1"]), a[href], input:not(:disabled), select:not(:disabled), summary, [tabindex="0"]') || [])].filter(element => element.getClientRects().length > 0);
  const first = focusable[0], last = focusable[focusable.length - 1];
  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
}
function moveResize(event: PointerEvent) { if (resizing) preferredWidth.value = Math.min(maximumWidth.value, Math.max(minimumWidth.value, startWidth + startX - event.clientX)); }
function stopResize() {
  if (!resizing) return;
  resizing = false; window.removeEventListener('pointermove', moveResize); window.removeEventListener('pointerup', stopResize); window.removeEventListener('pointercancel', stopResize); window.removeEventListener('blur', stopResize);
  document.body.style.cursor = oldCursor; document.body.style.userSelect = oldSelect;
}
function startResize(event: PointerEvent) {
  if (compact.value || event.button !== 0) return;
  event.preventDefault(); resizing = true; startX = event.clientX; startWidth = panelWidth.value; oldCursor = document.body.style.cursor; oldSelect = document.body.style.userSelect;
  document.body.style.cursor = 'col-resize'; document.body.style.userSelect = 'none';
  window.addEventListener('pointermove', moveResize); window.addEventListener('pointerup', stopResize); window.addEventListener('pointercancel', stopResize); window.addEventListener('blur', stopResize);
}
function resizeKeydown(event: KeyboardEvent) {
  const widths: Record<string, number> = { ArrowLeft: panelWidth.value + 24, ArrowRight: panelWidth.value - 24, Home: minimumWidth.value, End: maximumWidth.value };
  if (widths[event.key] == null) return;
  event.preventDefault(); preferredWidth.value = Math.min(maximumWidth.value, Math.max(minimumWidth.value, widths[event.key]!));
}
onUnmounted(stopResize);
</script>
<style scoped>
.task-data-slot { position: relative; flex: none; min-width: 0; height: 100%; }
.task-data-panel { position: relative; display: flex; flex-direction: column; height: 100%; min-height: 0; border-left: 1px solid var(--border-main); background: var(--background-menu-white); }
.is-compact .task-data-panel { position: fixed; inset: 0; z-index: 60; width: 100%; height: 100dvh; padding-top: env(safe-area-inset-top); padding-bottom: env(safe-area-inset-bottom); border: 0; }
.task-data-heading { display: flex; flex: none; align-items: center; justify-content: space-between; padding: 16px; gap: 12px; }
.task-data-heading h2 { font-size: 15px; font-weight: 600; }.task-data-heading h2 span { margin-left: 6px; font-size: 12px; font-weight: 400; color: var(--text-tertiary); }
.task-data-heading p { margin-top: 4px; font-size: 11px; color: var(--text-tertiary); }.task-heading-actions { display: flex; gap: 4px; }
.task-icon-button { display: inline-flex; flex: none; align-items: center; justify-content: center; width: 28px; height: 28px; border-radius: 6px; color: var(--text-secondary); }.task-icon-button:hover { background: var(--fill-tsp-white-light); }
.task-data-tabs { display: flex; flex: none; gap: 2px; padding: 0 12px; border-bottom: 1px solid var(--border-main); }.task-data-tabs button { flex: 1; min-width: 0; padding: 10px 2px; border-bottom: 2px solid transparent; color: var(--text-secondary); font-size: 11px; white-space: nowrap; }.task-data-tabs button[aria-selected=true] { border-bottom-color: var(--control-focus-color); color: var(--control-focus-color); font-weight: 600; }.task-data-tabs button span { font-size: 10px; opacity: .7; }
.task-data-filters { flex: none; padding: 12px 16px; }.task-data-search { display: flex; gap: 8px; align-items: center; padding: 8px 10px; border: 1px solid var(--border-main); border-radius: 7px; color: var(--text-tertiary); }.task-data-search input { width: 100%; min-width: 0; background: transparent; font-size: 12px; color: var(--text-primary); }
.task-filter-row { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 9px; }
.task-filter-row button { display: inline-flex; align-items: center; justify-content: center; gap: 4px; min-height: 28px; padding: 4px 8px; border: 1px solid transparent; border-radius: 6px; font-size: 12px; font-weight: 500; color: var(--text-secondary); background: var(--fill-tsp-white-light); cursor: pointer; transition: background-color .15s, color .15s; }
.task-filter-row button[aria-pressed=false]:hover { background: var(--fill-tsp-white-dark); }
.task-filter-row button[aria-pressed=true] { color: var(--background-menu-white); background: var(--control-focus-color); border-color: var(--control-focus-color); }
.task-filter-row button svg { flex: none; }
.task-filter-row .task-clear-filters { margin-left: auto; background: transparent; color: var(--text-tertiary); font-weight: 400; }
.task-data-results { min-height: 0; flex: 1; overflow-y: auto; overscroll-behavior: contain; padding: 0 16px; }.task-data-results :deep(.acquisition-content) { flex: none; overflow: visible; padding: 0 0 14px; }
.task-file-list { list-style: none; margin: 0; padding: 0; }.task-file-row { padding: 12px 0; border-bottom: 1px solid var(--border-main); }.task-file-main { display: flex; align-items: center; min-width: 0; gap: 8px; }.task-file-main input { flex: none; width: 14px; height: 14px; margin: 0; }.task-file-icon { flex: none; color: var(--control-focus-color); }.task-file-name { min-width: 0; flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; text-align: left; font-size: 12px; font-weight: 500; }.task-file-name:hover { text-decoration: underline; text-underline-offset: 3px; }
.task-file-details { margin-left: 48px; }.task-file-meta { display: flex; flex-wrap: wrap; gap: 3px 7px; margin: 2px 0 4px; font-size: 10px; color: var(--text-tertiary); }.task-file-input { display: flex; align-items: center; gap: 3px; color: var(--control-focus-color); font-size: 10px; margin: 4px 0; }.task-file-location { display: flex; align-items: center; gap: 4px; margin: 4px 0; color: var(--text-tertiary); font-size: 10px; }.task-file-location svg { flex: none; }.task-file-location span { min-width: 0; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
.task-data-empty { padding: 32px 8px; text-align: center; font-size: 12px; color: var(--text-tertiary); }.task-data-notice { padding: 10px 0; font-size: 11px; line-height: 1.6; color: var(--text-tertiary); }.task-data-notice button { text-decoration: underline; }
.task-data-footer { flex: none; padding: 12px 16px; border-top: 1px solid var(--border-main); }.task-data-footer p { font-size: 11px; line-height: 1.6; color: var(--text-tertiary); }.task-batch-actions { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; font-size: 11px; color: var(--text-secondary); }.task-save-button { display: inline-flex; align-items: center; gap: 5px; margin-left: auto; border-radius: 6px; padding: 7px 9px; background: var(--control-focus-color); color: var(--background-menu-white); font-size: 11px; }.is-warning, .task-data-footer p.is-warning { color: var(--function-error); }
button:disabled { opacity: .45; cursor: not-allowed; }.task-data-resize { position: absolute; z-index: 2; top: 0; bottom: 0; left: -4px; width: 8px; cursor: col-resize; touch-action: none; }.task-data-resize:hover span, .task-data-resize:focus span { display: block; width: 2px; height: 100%; margin-left: 3px; background: var(--control-focus-color); }.spinning { animation: task-spin 1s linear infinite; }@keyframes task-spin { to { transform: rotate(360deg); } }
@media(max-width:360px) { .task-data-tabs { padding: 0 6px; }.task-data-tabs button { font-size: 10px; }.task-file-details { margin-left: 22px; } }
</style>
