<template>
  <section class="library-browser" aria-label="数据分栏浏览器">
    <nav class="library-breadcrumb" aria-label="数据目录位置">
      <button type="button" class="browser-back" :disabled="!path.length && !focusedFile" aria-label="返回上一级目录" @click="goBack"><ArrowLeft :size="16" /></button>
      <button type="button" class="breadcrumb-label" @click="goTo(-1)">我的数据</button>
      <template v-for="(directory, index) in resolvedPath" :key="directory.id">
        <ChevronRight :size="13" class="breadcrumb-divider" />
        <button type="button" class="breadcrumb-label" :title="directory.name" @click="goTo(index)">{{ directory.name }}</button>
      </template>
      <span v-if="focusedFile" class="breadcrumb-current" :title="focusedFile.name">{{ focusedFile.name }}</span>
    </nav>
    <div class="library-browser-body" :class="{ 'mobile-file-details': focusedFile }">
      <div ref="columnsRef" class="library-columns" aria-label="目录与文件">
        <section v-for="(column, index) in columns" :key="column.id" class="library-column" :class="{ 'current-column': index === columns.length - 1 }" :aria-label="`${column.name} 的内容`">
          <header><span :title="column.name">{{ column.name }}</span><span>{{ column.nodes.length }}</span></header>
          <div class="column-rows">
            <p v-if="!column.nodes.length" class="column-empty">此目录暂无匹配的文件</p>
            <div v-for="node in column.nodes" :key="node.id" class="column-row" :class="{ 'row-focused': isFocused(node), 'row-missing': node.missing }">
              <input v-if="node.kind === 'file'" type="checkbox" :aria-label="`选择文件 ${node.name}`" :checked="selectedIds.includes(node.file.file_id)" :disabled="node.missing" @change="toggleSelection(node)" />
              <button type="button" class="column-row-button" :aria-label="`${node.kind === 'directory' ? '打开目录' : '查看文件'} ${node.name}`" :title="node.name" @click="openNode(node, index)">
                <FolderOpen v-if="node.kind === 'directory' && isFocused(node)" :size="17" class="browser-file-icon" />
                <Folder v-else-if="node.kind === 'directory'" :size="17" class="browser-file-icon" />
                <FileText v-else :size="17" class="browser-file-icon" />
                <span class="column-file-name">{{ node.name }}</span>
                <span v-if="node.missing" class="column-unavailable">不可用</span>
                <ChevronRight v-if="node.kind === 'directory'" :size="14" class="column-row-chevron" />
              </button>
            </div>
          </div>
        </section>
      </div>
      <aside class="library-details" aria-label="数据详情"><slot name="details" /></aside>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue';
import { ArrowLeft, ChevronRight, FileText, Folder, FolderOpen } from 'lucide-vue-next';
import type { DataLibraryAsset } from '../api/dataLibrary';
import type { PersonalDataFile } from '../utils/personalDataTree';
import { findLibraryColumnNode, libraryColumns, libraryColumnTree, resolveLibraryColumnPath, type LibraryBrowserNode } from '../utils/libraryColumns';

const props = defineProps<{ items: DataLibraryAsset[]; selectedIds: string[] }>();
const emit = defineEmits<{ (event: 'inspect', node: LibraryBrowserNode | null): void; (event: 'toggle-file', node: PersonalDataFile): void }>();
const tree = computed(() => libraryColumnTree(props.items));
const path = ref<string[]>([]);
const focusedId = ref('');
const columnsRef = ref<HTMLElement>();
const resolvedPath = computed(() => resolveLibraryColumnPath(tree.value, path.value));
const columns = computed(() => libraryColumns(tree.value, path.value));
const focused = computed(() => findLibraryColumnNode(tree.value, focusedId.value) ?? resolvedPath.value[resolvedPath.value.length - 1] ?? null);
const focusedFile = computed(() => focused.value?.kind === 'file' ? focused.value : null);

function isFocused(node: LibraryBrowserNode) { return focusedId.value === node.id || resolvedPath.value.some(directory => directory.id === node.id); }
function openNode(node: LibraryBrowserNode, columnIndex: number) {
  path.value = resolvedPath.value.slice(0, columnIndex).map(directory => directory.id);
  if (node.kind === 'directory') path.value.push(node.id);
  focusedId.value = node.id;
  void nextTick(() => { if (columnsRef.value) columnsRef.value.scrollLeft = columnsRef.value.scrollWidth; });
}
function toggleSelection(node: PersonalDataFile) { if (!node.missing) emit('toggle-file', node); }
function goTo(index: number) {
  path.value = resolvedPath.value.slice(0, index + 1).map(directory => directory.id);
  focusedId.value = path.value[path.value.length - 1] ?? '';
}
function goBack() { if (focusedFile.value) focusedId.value = path.value[path.value.length - 1] ?? ''; else goTo(resolvedPath.value.length - 2); }
watch(tree, () => {
  path.value = resolvedPath.value.map(directory => directory.id);
  if (!findLibraryColumnNode(tree.value, focusedId.value)) focusedId.value = path.value[path.value.length - 1] ?? '';
}, { flush: 'sync' });
watch(focused, node => emit('inspect', node), { immediate: true });
</script>

<style scoped>
.library-browser { container: library-browser / inline-size; display: flex; flex: 1; min-width: 0; min-height: 0; flex-direction: column; overflow: hidden; border: 1px solid var(--border-main); border-radius: 10px; background: var(--background-menu-white); }
.library-breadcrumb { display: flex; flex: none; min-height: 42px; align-items: center; gap: 5px; overflow-x: auto; padding: 5px 10px; border-bottom: 1px solid var(--border-main); background: var(--background-gray-main); font-size: 12px; }
.browser-back { display: flex; width: 28px; height: 28px; flex: none; align-items: center; justify-content: center; margin-right: 3px; border-radius: 5px; }
.browser-back:disabled { opacity: .35; }
.breadcrumb-label { max-width: 150px; overflow: hidden; flex-shrink: 0; padding: 4px; border-radius: 4px; text-overflow: ellipsis; white-space: nowrap; }
.breadcrumb-current { max-width: 180px; overflow: hidden; flex-shrink: 0; padding-left: 8px; text-overflow: ellipsis; white-space: nowrap; color: var(--text-tertiary); }
.breadcrumb-divider { flex: none; color: var(--icon-tertiary); }
.library-browser-body { display: flex; flex: 1; min-width: 0; min-height: 0; }
.library-columns { display: flex; flex: 1; min-width: 0; min-height: 0; overflow-x: auto; overscroll-behavior: contain; }
.library-column { display: flex; width: 220px; min-width: 200px; flex: 0 0 220px; min-height: 0; flex-direction: column; border-right: 1px solid var(--border-main); }
.library-column header { display: flex; min-height: 38px; flex: none; align-items: center; gap: 8px; justify-content: space-between; padding: 8px 12px; font-size: 11px; color: var(--text-tertiary); }
.library-column header span:first-child { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.column-rows { flex: 1; min-height: 0; overflow-y: auto; padding: 0 5px 10px; }
.column-row { display: flex; min-width: 0; align-items: center; border-radius: 5px; padding: 0 5px; }
.column-row:hover, .breadcrumb-label:hover, .browser-back:hover:not(:disabled) { background: var(--fill-tsp-white-light); }
.column-row.row-focused { background: #2b765912; }
.column-row-button { display: flex; flex: 1; min-width: 0; min-height: 36px; align-items: center; gap: 8px; padding: 6px 3px; text-align: left; }
.column-file-name { min-width: 0; flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 12px; }
.browser-file-icon { flex: none; color: #2b7659; }
.column-row-chevron { flex: none; color: var(--icon-tertiary); }
.column-row input { flex: none; width: 13px; height: 13px; margin-right: 6px; accent-color: #2b7659; }
.row-missing { color: var(--text-tertiary); }
.row-missing .browser-file-icon { opacity: .5; }
.column-unavailable { font-size: 9px; color: var(--text-tertiary); }
.column-empty { padding: 16px 10px; font-size: 12px; line-height: 1.7; color: var(--text-tertiary); }
.library-details { width: 272px; flex: none; min-height: 0; overflow-y: auto; padding: 20px; border-left: 1px solid var(--border-main); }
button:focus-visible, input:focus-visible { outline: 2px solid #6b927f; outline-offset: 1px; }
:global(.dark .browser-file-icon) { color: #a9cbbb; }
:global(.dark .column-row.row-focused) { background: #a9cbbb18; }
@container library-browser (max-width: 760px) {
  .library-column { display: none; }
  .library-column.current-column { display: flex; width: 100%; flex-basis: 100%; border-right: 0; }
  .library-details { display: none; }
  .mobile-file-details .library-columns { display: none; }
  .mobile-file-details .library-details { display: block; width: 100%; border-left: 0; }
  .column-row-button { min-height: 42px; }
  .column-file-name { font-size: 13px; }
  .breadcrumb-label { max-width: 110px; }
}
</style>
