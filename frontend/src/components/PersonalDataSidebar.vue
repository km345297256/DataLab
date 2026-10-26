<template>
  <aside ref="sidebarRoot" class="personal-data-sidebar" aria-label="我的数据" tabindex="-1">
    <header class="data-sidebar-header">
      <h2 class="desktop-heading">我的数据</h2>
      <button type="button" class="data-sidebar-heading" :aria-expanded="mobileExpanded" @click="mobileExpanded = !mobileExpanded">
        <span>我的数据</span>
        <ChevronDown class="mobile-chevron" :class="{ expanded: mobileExpanded }" :size="15" />
      </button>
      <span class="selection-count" aria-live="polite">已选 {{ selectedIds.size }}</span>
      <button type="button" class="icon-button refresh-button" aria-label="刷新我的数据" title="刷新我的数据" :disabled="loading || foldersLoading" @click="refresh">
        <RefreshCw :size="15" :class="{ spinning: loading || foldersLoading }" />
      </button>
    </header>
    <div class="data-sidebar-body" :class="{ 'mobile-expanded': mobileExpanded }">
      <LibraryFileControls
        v-model:query="query"
        v-model:format="fileFormat"
        v-model:from="fromDate"
        v-model:to="toDate"
        :stats="stats"
        compact
        @clear="resetFilters"
      />
      <div v-if="(loading || foldersLoading) && !tree.length" role="status" class="sidebar-state">
        <LoaderCircle :size="18" class="spinning" />
        <span>正在读取数据</span>
      </div>
      <div v-else-if="!tree.length && !error && !foldersError" class="sidebar-state empty-state">
        <FolderOpen :size="24" />
        <p>{{ hasFilters ? '没有找到匹配的文件' : '还没有保存的数据' }}</p>
        <p class="empty-hint">{{ hasFilters ? '尝试其他文件名、格式或更新时间。' : '上传文件或保存开放数据后，可在这里选择分析。' }}</p>
        <button v-if="hasFilters" type="button" class="clear-filters" @click="resetFilters">清空筛选</button>
      </div>
      <p v-if="hasFilters && hiddenSelectedCount" class="hidden-selection-hint" aria-live="polite">另有 {{ hiddenSelectedCount }} 个已选文件未显示</p>
      <div v-if="rows.length" class="data-files-heading"><h3>{{ hasFilters ? '匹配文件' : '数据文件' }}</h3><span>{{ loadedFileCount }} 个<template v-if="nextCursor"> · 已加载</template></span></div>
      <div v-if="rows.length" class="data-file-list" role="group" aria-label="个人数据文件">
        <div
          v-for="row in rows"
          :key="row.id"
          class="data-file-row"
          :class="{ 'directory-row': row.kind === 'directory', 'selected-row': row.kind === 'file' && selectedIds.has(personalFileId(row.file)), 'missing-row': row.missing }"
          :style="{ paddingLeft: `${8 + Math.min(row.depth, 4) * 10}px` }"
        >
          <template v-if="row.kind === 'directory'">
            <button
              type="button"
              class="directory-toggle"
              :aria-label="`${expanded.has(row.id) ? '收起' : '展开'}目录 ${row.name}`"
              :aria-expanded="expanded.has(row.id)"
              @click="toggleDirectory(row.id)"
            >
              <ChevronDown v-if="expanded.has(row.id)" :size="14" class="row-chevron" />
              <ChevronRight v-else :size="14" class="row-chevron" />
              <FolderOpen v-if="expanded.has(row.id)" :size="17" class="file-icon" />
              <Folder v-else :size="17" class="file-icon" />
              <span class="row-name" :title="row.name">{{ row.name }}</span>
              <span class="file-count">{{ row.fileCount }}</span>
            </button>
            <span v-if="row.missing" class="missing-label" title="目录中的文件不可用">不可用</span>
          </template>
          <template v-else>
            <input
              type="checkbox"
              class="file-checkbox"
              :aria-label="`选择文件 ${row.name}`"
              :checked="selectedIds.has(personalFileId(row.file))"
              :disabled="selectionDisabled(row)"
              @change="toggleFile(row)"
            />
            <FileText :size="17" class="file-icon" />
            <button type="button" class="file-name-button row-name" :title="row.name" :disabled="row.missing" @click="previewFile(row)">{{ row.name }}</button>
            <button type="button" class="icon-button preview-button" :aria-label="`预览文件 ${row.name}`" :title="`预览 ${row.name}`" :disabled="row.missing" @click.stop="previewFile(row)">
              <Eye :size="16" />
            </button>
          </template>
        </div>
      </div>
      <div v-if="error" class="sidebar-error" role="alert">
        <p>{{ error }}</p>
        <button type="button" @click="retry">重试</button>
      </div>
      <div v-if="foldersError" class="sidebar-error" role="alert"><p>{{ foldersError }}</p><button type="button" @click="loadFolders">重新读取文件夹</button></div>
      <button v-if="nextCursor && !error" type="button" class="load-more" :disabled="loading" @click="load(true)">{{ loading ? '正在读取…' : '加载更多' }}</button>
    </div>
  </aside>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue';
import { ChevronDown, ChevronRight, Eye, FileText, Folder, FolderOpen, LoaderCircle, RefreshCw } from 'lucide-vue-next';
import type { FileInfo } from '../api/file';
import LibraryFileControls from './LibraryFileControls.vue';
import { useAuth } from '../composables/useAuth';
import { useLibraryBrowser } from '../composables/useLibraryBrowser';
import { useLibraryFolders } from '../composables/useLibraryFolders';
import {
  buildPersonalDataTree,
  flattenPersonalDataTree,
  personalFileId,
  type PersonalDataFile,
  type PersonalDataNode,
} from '../utils/personalDataTree';

const props = withDefaults(defineProps<{ selectedFiles: FileInfo[]; disabled?: boolean }>(), { disabled: false });
const emit = defineEmits<{
  (event: 'toggle-file', file: FileInfo): void;
  (event: 'preview', file: FileInfo, relatedFiles: FileInfo[]): void;
}>();
const { currentUser } = useAuth();
const { items, visibleItems, stats, query, fileFormat, fromDate, toDate, nextCursor, loading, error, load, resetFilters } = useLibraryBrowser();
const { folders, loading: foldersLoading, error: foldersError, load: loadFolders } = useLibraryFolders();
const sidebarRoot = ref<HTMLElement>();
const mobileExpanded = ref(false);
const expanded = ref(new Set<string>());
const knownRoots = new Set<string>();
const tree = computed(() => buildPersonalDataTree(visibleItems.value, hasFilters.value ? [] : folders.value));
const rows = computed(() => flattenPersonalDataTree(tree.value, expanded.value));
const selectedIds = computed(() => new Set(props.selectedFiles.map(personalFileId)));
const filterKey = computed(() => JSON.stringify([query.value.trim(), fileFormat.value, fromDate.value, toDate.value]));
const hasFilters = computed(() => Boolean(query.value.trim() || fileFormat.value || fromDate.value || toDate.value));
const visibleFileIds = computed(() => new Set(visibleItems.value.flatMap(asset => asset.files.filter(file => file.file_id).map(personalFileId))));
const loadedFileCount = computed(() => visibleFileIds.value.size);
const hiddenSelectedCount = computed(() => [...selectedIds.value].filter(id => !visibleFileIds.value.has(id)).length);
let unfilteredExpanded: Set<string> | null = null;

watch([tree, filterKey], ([roots]) => {
  if (hasFilters.value) {
    // Searching exposes every matching path without losing the user's normal
    // directory layout or changing which files are selected for analysis.
    if (unfilteredExpanded === null) unfilteredExpanded = new Set(expanded.value);
    const next = new Set<string>();
    const visit = (nodes: PersonalDataNode[]) => {
      for (const node of nodes) {
        if (node.kind === 'directory') { next.add(node.id); visit(node.children); }
      }
    };
    visit(roots);
    expanded.value = next;
    return;
  }
  const next = new Set(unfilteredExpanded ?? expanded.value);
  unfilteredExpanded = null;
  for (const root of roots) {
    if (root.kind !== 'directory') continue;
    if (!knownRoots.has(root.id)) { knownRoots.add(root.id); next.add(root.id); }
  }
  expanded.value = next;
}, { immediate: true });

// useLibraryBrowser owns data isolation and request cancellation. Only reset
// presentation state here; starting a second load would race its owner watcher.
watch(() => currentUser.value?.id, () => {
  knownRoots.clear();
  expanded.value = new Set();
  unfilteredExpanded = null;
  mobileExpanded.value = false;
}, { immediate: true, flush: 'sync' });

function toggleDirectory(id: string) {
  const next = new Set(expanded.value);
  if (next.has(id)) next.delete(id); else next.add(id);
  expanded.value = next;
}

function selectionDisabled(row: PersonalDataFile): boolean {
  return props.disabled || (row.missing && !selectedIds.value.has(personalFileId(row.file)));
}

function toggleFile(row: PersonalDataFile) {
  if (!selectionDisabled(row)) emit('toggle-file', row.file);
}

function previewFile(row: PersonalDataFile) {
  if (row.missing) return;
  const asset = items.value.find(item => item.id === row.assetId);
  if (!asset) return;
  // File previews such as Shapefile need siblings excluded by the current
  // filter. Reuse the tree's safe-name normalization for the complete asset.
  const pending: PersonalDataNode[] = [...buildPersonalDataTree([asset])];
  while (pending.length) {
    const node = pending.pop()!;
    if (node.kind === 'file') { emit('preview', row.file, node.relatedFiles); return; }
    pending.push(...node.children);
  }
}

function refresh() {
  if (currentUser.value?.id) { void load(); void loadFolders(); }
}

function refreshFiles() {
  // useLibraryFolders independently listens to this same event; avoid issuing
  // a second directory request for every newly created folder or saved file.
  if (currentUser.value?.id) void load();
}

function retry() {
  if (currentUser.value?.id) void load(Boolean(items.value.length && nextCursor.value));
}

async function reveal() {
  mobileExpanded.value = true;
  await nextTick();
  sidebarRoot.value?.focus({ preventScroll: true });
  sidebarRoot.value?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
}

onMounted(() => window.addEventListener('data-library-changed', refreshFiles));
onUnmounted(() => window.removeEventListener('data-library-changed', refreshFiles));
defineExpose({ reveal, refresh });
</script>

<style scoped>
.personal-data-sidebar { --data-accent: #2b7659; --data-selection: rgb(43 118 89 / 7%); display: flex; flex-direction: column; min-width: 0; min-height: 0; height: 100%; color: var(--text-primary); }
.dark .personal-data-sidebar { --data-accent: #a9cbbb; --data-selection: rgb(169 203 187 / 10%); }
.data-sidebar-header { display: flex; align-items: center; gap: 8px; flex: none; padding: 18px 16px 10px; }
.desktop-heading { margin: 0 auto 0 0; font-size: 13px; line-height: 22px; font-weight: 600; }
.data-sidebar-heading { display: none; align-items: center; gap: 6px; min-width: 0; margin-right: auto; text-align: left; border-radius: 4px; font-size: 13px; line-height: 22px; font-weight: 600; }
.selection-count { flex: none; font-size: 11px; color: var(--text-tertiary); }
.icon-button { display: inline-flex; align-items: center; justify-content: center; flex: none; width: 26px; height: 28px; border-radius: 5px; }
.icon-button:hover:not(:disabled) { background: var(--fill-tsp-white-dark); }
.icon-button:disabled { opacity: .4; cursor: not-allowed; }
.refresh-button { color: var(--icon-secondary); }
.data-sidebar-body { flex: 1; min-height: 0; overflow-y: auto; overscroll-behavior: contain; padding: 0 16px 16px; }
.clear-filters { color: var(--data-accent); font-size: 12px; text-decoration: underline; }
.hidden-selection-hint { margin: 12px 0; font-size: 11px; line-height: 1.7; color: var(--text-tertiary); }
.data-files-heading { display: flex; align-items: center; justify-content: space-between; margin: 18px 0 10px; }
.data-files-heading h3 { font-size: 13px; font-weight: 600; }
.data-files-heading > span { font-size: 11px; color: var(--text-tertiary); }
.data-file-list { overflow: hidden; border: 1px solid var(--border-main); border-radius: 8px; background: var(--background-gray-main); }
.data-file-row { display: flex; align-items: center; gap: 6px; min-width: 0; min-height: 38px; padding-right: 6px; border-bottom: 1px solid var(--border-main); }
.data-file-row:last-child { border-bottom: 0; }
.data-file-row:hover { background: var(--fill-tsp-white-light); }
.data-file-row.selected-row { background: var(--data-selection); }
.directory-toggle { display: flex; align-items: center; gap: 6px; flex: 1; min-width: 0; min-height: 37px; text-align: left; }
.row-chevron { flex: none; color: var(--icon-secondary); }
.file-icon { flex: none; color: var(--data-accent); }
.row-name { min-width: 0; flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 13px; line-height: 20px; }
.directory-row .row-name { font-weight: 500; }
.file-name-button { text-align: left; color: var(--text-secondary); }
.file-name-button:disabled { cursor: not-allowed; }
.file-count { flex: none; font-size: 10px; color: var(--text-tertiary); padding-right: 2px; }
.file-checkbox { width: 13px; height: 13px; margin: 0 1px 0 0; flex: none; accent-color: var(--data-accent); cursor: pointer; }
.file-checkbox:disabled { cursor: not-allowed; opacity: .45; }
.preview-button { color: var(--data-accent); }
.missing-row .file-icon, .missing-row .row-name { opacity: .55; }
.missing-label { color: var(--text-tertiary); font-size: 10px; white-space: nowrap; }
.sidebar-state { display: flex; align-items: center; justify-content: center; gap: 8px; padding: 38px 8px; color: var(--text-tertiary); font-size: 12px; }
.empty-state { flex-direction: column; text-align: center; }
.empty-state p { margin: 0; }
.empty-hint { line-height: 1.7; font-size: 11px; max-width: 185px; }
.sidebar-error { padding: 16px 6px; font-size: 12px; line-height: 1.7; color: var(--text-secondary); }
.sidebar-error p { margin: 0; }
.sidebar-error button { margin-top: 6px; color: var(--data-accent); text-decoration: underline; }
.load-more { width: 100%; padding: 12px 6px; font-size: 12px; color: var(--data-accent); }
.load-more:disabled { opacity: .5; }
.mobile-chevron { display: none; }
button:focus-visible, .file-checkbox:focus-visible { outline: 2px solid var(--data-accent); outline-offset: 2px; }
.spinning { animation: sidebar-spin 1s linear infinite; }
@keyframes sidebar-spin { to { transform: rotate(360deg); } }
@container analysis-data-layout (max-width: 760px) {
  .personal-data-sidebar { height: auto; }
  .data-sidebar-header { padding: 10px 16px; }
  .desktop-heading { display: none; }
  .data-sidebar-heading { display: flex; flex: 1; }
  .mobile-chevron { display: block; transition: transform .15s ease; }
  .mobile-chevron.expanded { transform: rotate(180deg); }
  .data-sidebar-body { display: none; }
  .data-sidebar-body.mobile-expanded { display: block; max-height: 240px; flex: none; }

}
@media (prefers-reduced-motion: reduce) {
  .spinning { animation: none; }
  .mobile-chevron { transition: none; }
}
</style>
