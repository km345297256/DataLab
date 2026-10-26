<template>
  <section class="file-manager" aria-label="文件管理器" @keydown.esc="closeTransient">
    <div class="manager-navigation">
      <button type="button" class="manager-icon mobile-tree-toggle" :aria-expanded="treeVisible" aria-label="显示目录导航" @click="treeVisible = !treeVisible"><PanelLeft :size="17" /></button>
      <button type="button" class="manager-icon" :disabled="folderId === 'root'" aria-label="返回上一级目录" @click="navigate(parentId)"><ArrowLeft :size="17" /></button>
      <nav class="manager-breadcrumbs" aria-label="数据目录位置">
        <button type="button" @click="navigate('root')">我的数据</button>
        <template v-for="folder in breadcrumbs" :key="folder.id"><ChevronRight :size="13" /><button type="button" :title="folder.name" @click="navigate(folder.id)">{{ folder.name }}</button></template>
      </nav>
      <button v-if="folderId !== 'root'" type="button" class="manager-icon delete-action" :disabled="selectionDisabled" aria-label="删除当前文件夹" title="删除当前文件夹及全部内容" @click="deleteFolder(folderId)"><Trash2 :size="16" /></button>
      <div class="view-switch" role="group" aria-label="文件显示方式">
        <button type="button" class="manager-icon" :aria-pressed="view === 'list'" aria-label="列表视图" @click="view = 'list'"><List :size="17" /></button>
        <button type="button" class="manager-icon" :aria-pressed="view === 'icons'" aria-label="图标视图" @click="view = 'icons'"><LayoutGrid :size="16" /></button>
      </div>
    </div>
    <div class="manager-body" :class="{ 'show-mobile-tree': treeVisible, 'show-file-details': detailOpen }">
      <aside class="folder-navigation" aria-label="文件夹导航" @contextmenu.prevent="openContext($event, folderId)">
        <div class="tree-heading">文件夹</div>
        <button type="button" class="tree-root" :class="{ active: folderId === 'root' }" @click="navigate('root')" @contextmenu.prevent.stop="openContext($event, 'root')"><FolderOpen :size="17" />我的数据</button>
        <div v-for="folder in folderRows" :key="folder.id" class="folder-tree-row" :class="{ active: folderId === folder.id }" :style="{ paddingLeft: `${8 + Math.min(folder.depth, 8) * 12}px` }" @contextmenu.prevent.stop="openContext($event, folder.id)">
          <button v-if="folder.children.length" type="button" class="tree-expander" :aria-expanded="expanded.has(folder.id)" :aria-label="`${expanded.has(folder.id) ? '收起' : '展开'}目录 ${folder.name}`" @click="toggleExpanded(folder.id)"><ChevronDown v-if="expanded.has(folder.id)" :size="13" /><ChevronRight v-else :size="13" /></button><span v-else class="tree-expander" />
          <button type="button" class="tree-folder-name" :aria-label="`前往目录 ${folder.name}`" :title="folder.path" @click="navigate(folder.id)"><FolderOpen v-if="folderId === folder.id" :size="16" /><Folder v-else :size="16" /><span>{{ folder.name }}</span></button>
          <button type="button" class="manager-icon tree-more" :aria-label="`文件夹 ${folder.name} 的操作`" title="文件夹操作" @click.stop="openButtonContext($event, folder.id)"><MoreHorizontal :size="15" /></button>
        </div>
      </aside>
      <div class="manager-list-area" @contextmenu.prevent="openContext($event, folderId)">
        <div v-if="loading" class="manager-loading" role="status"><LoaderCircle :size="15" class="animate-spin" />正在读取文件</div>
        <div v-if="view === 'list'" class="file-table" :class="{ 'empty-table': !childFolders.length && !files.length }" role="table" aria-label="当前目录内容">
          <div class="file-table-header file-table-grid" role="row"><input type="checkbox" aria-label="全选当前列表" :checked="allSelected" :indeterminate="partlySelected" :disabled="selectionDisabled || loading || !files.length" @change="emit('toggle-all')" /><span role="columnheader">名称</span><span role="columnheader" class="format-cell">格式</span><span role="columnheader" class="size-cell">大小</span><span role="columnheader" class="date-cell">更新时间</span><span aria-hidden="true" /></div>
          <div class="file-table-rows">
            <div v-for="folder in childFolders" :key="folder.id" class="file-table-grid manager-row folder-list-row" :class="{ 'focused-row': focusedFolder === folder.id }" role="row" @click="focusFolder(folder.id)" @dblclick="navigate(folder.id)" @contextmenu.prevent.stop="openContext($event, folder.id)">
              <span /><button type="button" class="entry-name" :aria-label="`选择文件夹 ${folder.name}`" :title="folder.name" @click.stop="focusFolder(folder.id)" @dblclick.stop="navigate(folder.id)" @keydown.enter="navigate(folder.id)"><Folder :size="18" /><span>{{ folder.name }}</span></button><span class="entry-meta format-cell">文件夹</span><span class="entry-meta size-cell">—</span><span class="entry-meta date-cell">{{ formatDate(folder.updated_at) }}</span><div class="row-actions"><button type="button" class="manager-icon" :aria-label="`打开文件夹 ${folder.name}`" @click.stop="navigate(folder.id)"><ChevronRight :size="15" /></button><button type="button" class="manager-icon" :aria-label="`文件夹 ${folder.name} 的操作`" @click.stop="openButtonContext($event, folder.id)"><MoreHorizontal :size="15" /></button></div>
            </div>
            <div v-for="file in files" :key="file.id" class="file-table-grid manager-row" :class="{ 'focused-row': focusedFile === file.id, 'unavailable-row': file.missing }" role="row" @click="inspectFile(file)" @dblclick="previewFile(file)">
              <input type="checkbox" :aria-label="`选择文件 ${file.name}`" :checked="selectedIds.includes(file.id)" :disabled="selectionDisabled" @click.stop @change="emit('toggle-file', file)" />
              <button type="button" class="entry-name" :aria-label="`查看文件 ${file.name}`" :title="file.name" @click.stop="inspectFile(file)" @dblclick.stop="previewFile(file)"><FileText :size="18" /><span>{{ file.name }}<small v-if="file.missing">不可用</small></span></button><span class="entry-meta format-cell" :title="format(file.file)">{{ format(file.file) }}</span><span class="entry-meta size-cell">{{ size(file.file.size) }}</span><span class="entry-meta date-cell">{{ formatDate(file.updatedAt) }}</span><div class="row-actions"><button type="button" class="manager-icon" :disabled="file.missing" :aria-label="`预览文件 ${file.name}`" title="预览文件" @click.stop="previewFile(file)"><Eye :size="16" /></button><button type="button" class="manager-icon delete-action" :disabled="selectionDisabled" :aria-label="`删除文件 ${file.name}`" title="删除文件" @click.stop="emit('delete-file', file)"><Trash2 :size="15" /></button></div>
            </div>
          </div>
        </div>
        <div v-else class="file-icon-grid" :class="{ 'has-selection': selectedIds.length }" aria-label="当前目录图标视图">
          <div v-for="folder in childFolders" :key="folder.id" class="file-tile folder-tile" :class="{ 'focused-row': focusedFolder === folder.id }" @click="focusFolder(folder.id)" @dblclick="navigate(folder.id)" @contextmenu.prevent.stop="openContext($event, folder.id)">
            <button type="button" class="tile-main" :aria-label="`选择文件夹 ${folder.name}`" :title="folder.name" @click.stop="focusFolder(folder.id)" @dblclick.stop="navigate(folder.id)" @keydown.enter="navigate(folder.id)">
              <span class="tile-icon"><LibraryEntryIcon kind="folder" :size="64" /></span>
              <span class="tile-name">{{ folder.name }}</span>
            </button>
            <div class="tile-actions">
              <button type="button" :aria-label="`打开文件夹 ${folder.name}`" title="打开文件夹" @click.stop="navigate(folder.id)"><ArrowUpRight :size="14" /></button>
              <button type="button" :aria-label="`文件夹 ${folder.name} 的操作`" title="更多操作" @click.stop="openButtonContext($event, folder.id)"><MoreHorizontal :size="16" /></button>
            </div>
          </div>
          <div v-for="file in files" :key="file.id" class="file-tile" :class="{ 'focused-row': focusedFile === file.id, 'is-checked': selectedIds.includes(file.id), 'unavailable-row': file.missing }" @click="inspectFile(file)" @dblclick="previewFile(file)">
            <input class="tile-check" type="checkbox" :aria-label="`选择文件 ${file.name}`" :checked="selectedIds.includes(file.id)" :disabled="selectionDisabled" @click.stop @change="emit('toggle-file', file)" />
            <button type="button" class="tile-main" :aria-label="`查看文件 ${file.name}`" :title="`${file.name}\n${format(file.file)}`" @click.stop="inspectFile(file)" @dblclick.stop="previewFile(file)">
              <span class="tile-icon"><LibraryEntryIcon :filename="file.name" :size="64" /></span>
              <span class="tile-name">{{ file.name }}</span>
              <span v-if="file.missing" class="tile-unavailable">不可用</span>
            </button>
            <div class="tile-actions"><button type="button" :disabled="file.missing" :aria-label="`预览文件 ${file.name}`" title="预览文件" @click.stop="previewFile(file)"><Eye :size="15" /></button><button type="button" class="delete-action" :disabled="selectionDisabled" :aria-label="`删除文件 ${file.name}`" title="删除文件" @click.stop="emit('delete-file', file)"><Trash2 :size="14" /></button></div>
          </div>
        </div>
        <div v-if="!loading && !childFolders.length && !files.length" class="manager-empty"><FolderOpen :size="36" /><h2>{{ hasFilters ? '没有找到匹配的文件' : '此文件夹为空' }}</h2><p>{{ hasFilters ? '调整筛选，或在左侧切换文件夹。' : '新建文件夹，或上传文件开始整理数据。' }}</p><div><button type="button" @click="emit('create-folder', folderId)"><FolderPlus :size="15" />新建文件夹</button><button type="button" @click="emit('upload', folderId)"><Upload :size="15" />上传文件</button></div><button v-if="hasFilters" type="button" class="empty-clear" @click="emit('clear-filters')">清除筛选</button></div>
        <div class="manager-list-footer">{{ childFolders.length }} 个文件夹 · {{ files.length }} 个文件<span v-if="hasMore"> · 已加载</span></div>
      </div>
      <aside v-if="detailOpen" class="manager-details" aria-label="文件详情"><header><span>文件详情</span><button type="button" class="manager-icon" aria-label="关闭文件详情" @click="closeDetails"><X :size="17" /></button></header><div class="manager-detail-content"><slot name="details" /></div></aside>
    </div>
    <Teleport to="body"><div v-if="context" class="manager-context" role="menu" aria-label="文件夹操作" @keydown.esc="context = null" :style="{ left: `${context.x}px`, top: `${context.y}px` }" @click.stop @contextmenu.prevent><button v-if="context.folderId !== folderId" type="button" role="menuitem" @click="navigate(context.folderId)"><FolderOpen :size="16" />打开文件夹</button><button type="button" role="menuitem" :disabled="selectionDisabled" @click="contextAction('create-folder')"><FolderPlus :size="16" />新建文件夹</button><button type="button" role="menuitem" :disabled="selectionDisabled" @click="contextAction('upload')"><Upload :size="16" />上传文件</button><button v-if="context.folderId !== 'root'" type="button" role="menuitem" class="delete-action" :disabled="selectionDisabled" @click="contextAction('delete-folder')"><Trash2 :size="16" />删除文件夹及全部内容</button></div></Teleport>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue';
import { ArrowLeft, ArrowUpRight, ChevronDown, ChevronRight, Eye, FileText, Folder, FolderOpen, FolderPlus, LayoutGrid, List, LoaderCircle, MoreHorizontal, PanelLeft, Trash2, Upload, X } from 'lucide-vue-next';
import type { DataLibraryFolder } from '../api/dataLibrary';
import type { FileInfo } from '../api/file';
import { libraryFolderBreadcrumbs, libraryFolderRows, libraryFolderTree, type LibraryManagerFile } from '../utils/libraryFileManager';
import { libraryFileFormat, libraryFormatLabel } from '../utils/libraryFileFilters';
import { formatFileSize } from '../utils/fileType';
import LibraryEntryIcon from './icons/LibraryEntryIcon.vue';

const props = defineProps<{ folders: DataLibraryFolder[]; folderId: string; files: LibraryManagerFile[]; selectedIds: string[]; selectionDisabled?: boolean; detailOpen: boolean; loading: boolean; hasFilters: boolean; hasMore?: boolean }>();
const emit = defineEmits<{ (event: 'navigate', id: string): void; (event: 'inspect', file: LibraryManagerFile | null): void; (event: 'preview' | 'toggle-file' | 'delete-file', file: LibraryManagerFile): void; (event: 'create-folder' | 'upload' | 'delete-folder', folderId: string): void; (event: 'clear-filters' | 'toggle-all'): void }>();
const view = ref<'list' | 'icons'>('list');
const allSelected = computed(() => props.files.length > 0 && props.files.every(file => props.selectedIds.includes(file.id)));
const partlySelected = computed(() => !allSelected.value && props.files.some(file => props.selectedIds.includes(file.id)));
const expanded = ref(new Set<string>());
const treeVisible = ref(false);
const focusedFolder = ref('');
const focusedFile = ref('');
const context = ref<{ x: number; y: number; folderId: string } | null>(null);
const tree = computed(() => libraryFolderTree(props.folders));
const folderRows = computed(() => libraryFolderRows(tree.value, expanded.value));
const breadcrumbs = computed(() => libraryFolderBreadcrumbs(props.folders, props.folderId));
const parentId = computed(() => breadcrumbs.value[breadcrumbs.value.length - 2]?.id ?? 'root');
const childFolders = computed(() => props.folderId === 'root' ? tree.value : findFolder(tree.value, props.folderId)?.children ?? []);
function findFolder(nodes: ReturnType<typeof libraryFolderTree>, id: string): ReturnType<typeof libraryFolderTree>[number] | undefined { for (const node of nodes) { if (node.id === id) return node; const match = findFolder(node.children, id); if (match) return match; } }
function toggleExpanded(id: string) { const next = new Set(expanded.value); next.has(id) ? next.delete(id) : next.add(id); expanded.value = next; }
function navigate(id: string) { context.value = null; treeVisible.value = false; focusedFolder.value = ''; focusedFile.value = ''; emit('inspect', null); emit('navigate', id); }
function focusFolder(id: string) { focusedFolder.value = id; focusedFile.value = ''; emit('inspect', null); }
function inspectFile(file: LibraryManagerFile) { focusedFile.value = file.id; focusedFolder.value = ''; emit('inspect', file); }
function previewFile(file: LibraryManagerFile) { if (!file.missing) { inspectFile(file); emit('preview', file); } }
function closeDetails() { focusedFile.value = ''; emit('inspect', null); }
function closeTransient() { context.value = null; treeVisible.value = false; }
function openContext(event: MouseEvent, folderId: string) { context.value = { x: Math.min(event.clientX, window.innerWidth - 210), y: Math.min(event.clientY, window.innerHeight - 200), folderId }; }
function openButtonContext(event: MouseEvent, folderId: string) { const rect = (event.currentTarget as HTMLElement).getBoundingClientRect(); context.value = { x: Math.max(8, Math.min(rect.left, window.innerWidth - 210)), y: Math.min(rect.bottom, window.innerHeight - 200), folderId }; }
function deleteFolder(id: string) { if (id !== 'root' && !props.selectionDisabled) emit('delete-folder', id); context.value = null; }
function contextAction(action: 'create-folder' | 'upload' | 'delete-folder') {
  if (!context.value || props.selectionDisabled) return;
  if (action === 'delete-folder') { deleteFolder(context.value.folderId); return; }
  emit(action, context.value.folderId); context.value = null;
}
function dismissContext() { context.value = null; }
const size = (value?: number) => typeof value === 'number' && Number.isFinite(value) && value >= 0 ? formatFileSize(value) : '—';
const format = (file: FileInfo) => libraryFormatLabel(libraryFileFormat(file));
const formatDate = (value: string) => { const date = new Date(value); return Number.isNaN(date.getTime()) ? '—' : date.toLocaleDateString('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit' }); };
watch(breadcrumbs, value => { expanded.value = new Set([...expanded.value, ...value.map(folder => folder.id)]); }, { immediate: true });
watch(() => props.folderId, () => { closeTransient(); focusedFolder.value = ''; focusedFile.value = ''; });
watch(() => props.detailOpen, value => { if (!value) focusedFile.value = ''; });
onMounted(() => window.addEventListener('click', dismissContext));
onUnmounted(() => window.removeEventListener('click', dismissContext));
</script>

<style scoped>
.file-manager { container: file-manager / inline-size; display: flex; flex: 1; flex-direction: column; min-width: 0; min-height: 0; overflow: hidden; border: 1px solid var(--border-main); border-radius: 10px; background: var(--background-menu-white); }
.manager-navigation { display: flex; align-items: center; gap: 6px; min-height: 46px; flex: none; padding: 6px 10px; border-bottom: 1px solid var(--border-main); }
.manager-icon { display: inline-flex; flex: none; align-items: center; justify-content: center; width: 28px; height: 28px; border-radius: 5px; color: var(--text-secondary); }
.manager-icon:hover:not(:disabled), .manager-breadcrumbs button:hover { background: var(--fill-tsp-gray-main); }
.manager-icon:disabled { opacity: .35; }
.manager-breadcrumbs { display: flex; flex: 1; min-width: 0; align-items: center; gap: 3px; overflow-x: auto; font-size: 12px; }
.manager-breadcrumbs button { max-width: 160px; flex: none; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; padding: 5px 6px; border-radius: 4px; }
.manager-breadcrumbs svg { flex: none; color: var(--icon-tertiary); }
.view-switch { display: flex; flex: none; gap: 3px; padding-left: 5px; }
.view-switch [aria-pressed=true] { background: var(--fill-tsp-gray-main); color: var(--text-primary); }
.mobile-tree-toggle { display: none; }
.manager-body { display: flex; flex: 1; min-width: 0; min-height: 0; position: relative; }
.folder-navigation { width: 188px; flex: none; overflow-y: auto; padding: 10px 6px; background: var(--background-gray-main); border-right: 1px solid var(--border-main); }
.tree-heading { padding: 4px 10px 10px; color: var(--text-tertiary); font-size: 11px; }
.tree-root, .folder-tree-row { display: flex; min-height: 34px; align-items: center; gap: 5px; border-radius: 5px; font-size: 12px; }
.tree-root { padding: 7px 10px; width: 100%; gap: 7px; text-align: left; }
.tree-root.active, .folder-tree-row.active, .focused-row { background: #2b765914; }
.folder-tree-row:hover, .tree-root:hover { background: #2b76590a; }
.tree-expander { display: flex; width: 16px; height: 26px; flex: none; align-items: center; justify-content: center; color: var(--icon-secondary); }
.tree-folder-name { display: flex; flex: 1; min-width: 0; align-items: center; gap: 6px; text-align: left; padding: 6px 4px 6px 0; }
.tree-more { width: 24px; opacity: .5; }
.folder-tree-row:hover .tree-more, .tree-more:focus-visible { opacity: 1; }
.tree-folder-name span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.tree-root svg, .tree-folder-name svg, .entry-name svg { color: #2b7659; flex: none; }
.manager-list-area { display: flex; flex: 1; flex-direction: column; min-width: 0; min-height: 0; overflow: hidden; }
.manager-loading { display: flex; gap: 7px; align-items: center; padding: 7px 14px; font-size: 11px; color: var(--text-tertiary); }
.file-table { display: flex; flex: 1; min-height: 0; flex-direction: column; }
.file-table.empty-table { flex: none; }
.file-table-grid { display: grid; grid-template-columns: 20px minmax(120px, 1fr) 76px 76px 100px 54px; align-items: center; column-gap: 8px; padding: 0 12px; }
.file-table-header { min-height: 34px; flex: none; background: var(--background-gray-main); border-bottom: 1px solid var(--border-main); color: var(--text-tertiary); font-size: 11px; }
.file-table-rows { overflow-y: auto; min-height: 0; }
.manager-row { min-height: 42px; border-bottom: 1px solid var(--border-light); }
.manager-row:hover { background: #2b765909; }
.entry-name { display: flex; min-width: 0; align-items: center; gap: 8px; text-align: left; min-height: 40px; font-size: 12px; }
.entry-name > span { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.entry-name small { margin-left: 6px; font-size: 10px; color: var(--text-tertiary); }
.entry-meta { overflow: hidden; white-space: nowrap; text-overflow: ellipsis; font-size: 11px; color: var(--text-tertiary); }
.row-actions { display: flex; gap: 0; }
.delete-action { color: #b42318; }
.tile-actions .delete-action { color: #b42318; }
input[type=checkbox] { width: 13px; height: 13px; accent-color: #2b7659; }
.unavailable-row { color: var(--text-tertiary); }
.unavailable-row svg { opacity: .5; }
.file-icon-grid { --tile-hover: #2b765909; --tile-selection: #2b765914; --tile-selection-border: #9bbdad; display: grid; flex: 1; min-height: 0; align-content: start; justify-content: start; grid-template-columns: repeat(auto-fill, 116px); column-gap: 10px; row-gap: 14px; padding: 20px; overflow-y: auto; }
.file-tile { position: relative; min-width: 0; align-self: start; border: 1px solid transparent; border-radius: 6px; }
.file-tile:hover { background: var(--tile-hover); }
.file-tile.focused-row, .file-tile.is-checked { background: var(--tile-selection); border-color: var(--tile-selection-border); }
.tile-main { display: flex; width: 100%; min-width: 0; min-height: 140px; flex-direction: column; align-items: center; gap: 7px; padding: 22px 5px 9px; border-radius: 5px; font-size: 12px; color: var(--text-primary); }
.tile-icon { display: flex; flex: none; width: 72px; height: 64px; align-items: center; justify-content: center; }
.tile-name { display: -webkit-box; width: 100%; overflow: hidden; -webkit-box-orient: vertical; -webkit-line-clamp: 2; overflow-wrap: anywhere; line-height: 18px; min-height: 36px; text-align: center; }
.tile-unavailable { margin-top: -4px; color: var(--text-tertiary); font-size: 10px; }
.file-tile > .tile-check { position: absolute; z-index: 1; left: 6px; top: 6px; width: 15px; height: 15px; margin: 0; opacity: 0; pointer-events: none; accent-color: var(--control-focus-color); }
.tile-actions { position: absolute; top: 3px; right: 3px; display: flex; align-items: center; gap: 1px; opacity: 0; pointer-events: none; }
.tile-actions button { display: inline-flex; width: 25px; height: 25px; align-items: center; justify-content: center; border: 1px solid transparent; border-radius: 4px; color: var(--text-secondary); background: var(--background-menu-white); }
.tile-actions button:hover:not(:disabled) { border-color: var(--tile-selection-border); color: var(--text-primary); }
.tile-actions button:disabled { opacity: .4; }
.file-tile:hover > .tile-check, .file-tile:focus-within > .tile-check, .file-tile.focused-row > .tile-check, .has-selection .tile-check, .file-tile:hover .tile-actions, .file-tile:focus-within .tile-actions, .file-tile.focused-row .tile-actions, .file-tile.is-checked .tile-actions { opacity: 1; pointer-events: auto; }
.file-tile:has(.tile-main:focus-visible) { border-color: var(--control-focus-color); }
:global(.dark .file-icon-grid) { --tile-hover: #ffffff09; --tile-selection: #a9cbbb14; --tile-selection-border: #a9cbbb80; }
@media (hover: none), (pointer: coarse) { .file-tile > .tile-check, .tile-actions { opacity: 1; pointer-events: auto; } .tile-actions button { width: 28px; height: 28px; } }
.manager-empty { display: flex; flex: 1; flex-direction: column; align-items: center; justify-content: center; gap: 10px; padding: 30px 14px; text-align: center; color: var(--text-tertiary); }
.manager-empty h2 { font-size: 14px; color: var(--text-primary); }
.manager-empty p { font-size: 12px; }
.manager-empty > div { display: flex; flex-wrap: wrap; justify-content: center; gap: 9px; margin-top: 8px; }
.manager-empty button { display: inline-flex; gap: 6px; align-items: center; padding: 7px 10px; border: 1px solid var(--border-main); border-radius: 6px; font-size: 12px; color: var(--text-secondary); }
.manager-empty .empty-clear { border: 0; text-decoration: underline; }
.manager-list-footer { flex: none; padding: 8px 12px; margin-top: auto; border-top: 1px solid var(--border-main); font-size: 10px; color: var(--text-tertiary); }
.manager-details { width: 278px; flex: none; display: flex; flex-direction: column; min-height: 0; border-left: 1px solid var(--border-main); }
.manager-details > header { display: flex; justify-content: space-between; align-items: center; gap: 10px; padding: 8px 14px; flex: none; border-bottom: 1px solid var(--border-main); font-size: 12px; }
.manager-detail-content { flex: 1; min-height: 0; overflow-y: auto; padding: 14px 18px 20px; }
.manager-context { position: fixed; z-index: 1050; min-width: 196px; padding: 5px; border: 1px solid var(--border-main); border-radius: 8px; background: var(--background-menu-white); color: var(--text-primary); box-shadow: 0 5px 22px #0002; }
.manager-context button { display: flex; align-items: center; gap: 9px; width: 100%; padding: 9px 11px; font-size: 12px; border-radius: 4px; }
.manager-context button:disabled { opacity: .45; cursor: not-allowed; }
.manager-context button:hover:not(:disabled) { background: var(--fill-tsp-gray-main); }
button:focus-visible, input[type=checkbox]:focus-visible { outline: 2px solid #6b927f; outline-offset: 1px; }
:global(.dark .file-manager .tree-root svg), :global(.dark .file-manager .tree-folder-name svg), :global(.dark .file-manager .entry-name svg) { color: #a9cbbb; }
@container file-manager (max-width: 1080px) { .show-file-details .folder-navigation { display: none; } .show-file-details .date-cell { display: none; } .show-file-details .file-table-grid { grid-template-columns: 20px minmax(100px, 1fr) 58px 64px 54px; } }
@container file-manager (max-width: 850px) { .folder-navigation { display: none; } .mobile-tree-toggle { display: inline-flex; } .show-mobile-tree .folder-navigation { display: block; position: absolute; z-index: 4; left: 0; top: 0; bottom: 0; width: 220px; box-shadow: 3px 0 12px #0001; } .date-cell { display: none; } .file-table-grid { grid-template-columns: 20px minmax(100px, 1fr) 64px 64px 54px; } }
@container file-manager (max-width: 620px) { .show-file-details .manager-list-area { display: none; } .manager-details { width: 100%; border-left: 0; } .size-cell { display: none; } .file-table-grid { grid-template-columns: 16px minmax(80px, 1fr) 52px 50px; column-gap: 6px; padding: 0 8px; } .manager-navigation { padding: 6px; gap: 2px; } .view-switch { padding-left: 0; } .manager-breadcrumbs button { max-width: 100px; } .file-icon-grid { grid-template-columns: repeat(auto-fill, 104px); column-gap: 8px; row-gap: 10px; padding: 10px; } }
</style>
