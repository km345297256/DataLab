<template>
  <main class="my-data-page">
    <header class="my-data-header">
      <button v-if="!isLeftPanelShow" type="button" class="icon-action" aria-label="打开导航" @click="showLeftPanel"><PanelLeft :size="20" /></button>
      <div class="min-w-0 flex-1"><h1>我的数据</h1><p>浏览保存的数据文件，选择后预览或开始分析。</p></div>
      <button type="button" class="action-button" aria-label="新建文件夹" :disabled="deletionBusy" @click="openCreateFolder(currentFolderId)"><FolderPlus :size="15" /><span class="create-folder-full">新建文件夹</span><span class="create-folder-short">新建</span></button>
      <button type="button" class="action-button" :disabled="deletionBusy" @click="startUpload(currentFolderId)"><Upload :size="15" />上传文件</button>
      <button type="button" :disabled="loading" class="icon-action" aria-label="刷新数据列表" title="刷新数据列表" @click="refreshAll"><RefreshCw :size="17" :class="{ 'animate-spin': loading }" /></button>
    </header>

    <div class="my-data-content">
      <LibraryFileControls v-model:query="query" v-model:format="fileFormat" v-model:from="fromDate" v-model:to="toDate" v-model:source="sourceType" :stats="stats" hide-stats @clear="resetFilters" />
      <div v-if="currentFiles.length || selected.size" class="library-toolbar">
        <button type="button" class="action-button" :disabled="deletionBusy || loading || !currentFiles.length || allCurrentSelected" title="仅选择当前列表中已加载的文件" @click="selectCurrentFiles">全选当前列表</button>
        <span class="flex-1" />
        <span class="selected-file-count" aria-live="polite">已选 {{ selected.size }} 个文件</span>
        <button v-if="selected.size" type="button" class="clear-selection" :disabled="deletionBusy" @click="clearSelection">清空选择</button>
        <button v-if="selected.size" type="button" class="action-button danger-action" :disabled="deletionBusy" @click="requestDelete([...selected.values()])"><Trash2 :size="15" />删除所选</button>
        <button v-if="selected.size" type="button" class="primary-button" :disabled="deletionBusy || selectedUnavailable > 0 || selectedFiles.length > 32" @click="startSelectedAnalysis"><MessageSquarePlus :size="15" />分析所选文件</button>
      </div>
      <p v-if="selectedUnavailable" class="text-xs text-[var(--text-tertiary)]">所选文件中有 {{ selectedUnavailable }} 个不可用文件，可删除；分析前请取消勾选不可用文件。</p>
      <p v-else-if="selectedFiles.length > 32" class="text-xs text-[var(--text-tertiary)]">本次分析最多使用 32 个文件，批量删除不受此限制。</p>
      <div class="upload-staging" :class="{ 'has-uploads': uploadFiles.length }">
        <ChatBoxFiles ref="uploadRef" :attachments="uploadFiles" />
        <div v-if="uploadFiles.length" class="flex flex-wrap items-center justify-between gap-2 pt-2"><span class="text-xs text-[var(--text-tertiary)]">上传目标：{{ uploadTargetName }}。上传完成后保存。</span><button type="button" :disabled="deletionBusy || !uploadRef?.isAllUploaded" class="primary-button" @click="saveVisible = true">保存到我的数据</button></div>
      </div>
      <p v-if="selectionError" role="alert" class="text-xs text-red-600">{{ selectionError }}</p>
      <div v-if="error" role="alert" class="library-error"><span>{{ error }}</span><button type="button" @click="load()">重试</button></div>
      <div v-if="folderError" role="alert" class="library-error"><span>{{ folderError }}</span><button type="button" @click="loadFolders">重试读取文件夹</button></div>
      <LibraryFileManager :folders="folders" :folder-id="currentFolderId" :files="currentFiles" :selected-ids="selectedIds" :selection-disabled="deletionBusy" :detail-open="Boolean(inspected)" :loading="loading || foldersLoading" :has-filters="hasFilters" :has-more="Boolean(nextCursor)" @navigate="navigateFolder" @inspect="inspected = $event" @preview="previewSelectedFile" @toggle-file="toggleFile" @toggle-all="toggleCurrentSelection" @delete-file="requestDelete([$event])" @delete-folder="requestFolderDelete" @create-folder="openCreateFolder" @upload="startUpload" @clear-filters="resetFilters">
        <template #details>
          <template v-if="inspected">
            <div class="detail-file-symbol"><FileText :size="34" /></div>
            <h2 class="detail-title">{{ inspected.name }}</h2>
            <p class="detail-kind">{{ formatLabel(inspected.file) }}</p>
            <p v-if="inspected.missing" class="detail-missing">文件暂不可用，可查看保存时的数据记录。</p>
            <div class="detail-actions">
              <button type="button" class="action-button" :disabled="inspected.missing" @click="previewFile"><Eye :size="15" />预览</button>
              <button type="button" class="primary-button" :disabled="inspected.missing" @click="startFileAnalysis"><MessageSquarePlus :size="15" />开始分析</button>
              <button type="button" class="action-button danger-action" :disabled="deletionBusy" @click="requestDelete([inspected])"><Trash2 :size="15" />删除文件</button>
            </div>
            <dl class="detail-facts">
              <div><dt>文件大小</dt><dd>{{ fileSize(inspected.file.size) }}</dd></div>
              <template v-if="inspectedAsset"><div><dt>更新时间</dt><dd>{{ formatDate(inspectedAsset.updated_at) }}</dd></div>
              <div><dt>数据来源</dt><dd>{{ dataLibrarySourceLabels[inspectedAsset.source_type] }}</dd></div>
              <div><dt>所属记录</dt><dd>{{ assetDisplayName }}</dd></div>
              <div v-if="inspected.file.relative_path"><dt>文件位置</dt><dd>{{ inspected.file.relative_path }}</dd></div></template>
            </dl>
            <div v-if="inspectedAsset" class="detail-record">
              <h3>数据记录说明</h3>
              <p>{{ inspectedAsset.description || '暂无说明' }}</p>
              <div v-if="inspectedAsset.tags.length" class="detail-tags"><span v-for="tag in inspectedAsset.tags" :key="tag">{{ tag }}</span></div>
              <a v-if="safeSourceUrl(inspectedAsset.source_url)" :href="safeSourceUrl(inspectedAsset.source_url)" target="_blank" rel="noopener noreferrer" class="detail-source-link">查看原始来源<ExternalLink :size="12" /></a>
              <p v-if="inspectedAsset.source_type === 'open_data'" class="source-verification">{{ inspectedAsset.source_verified ? '已记录来源与获取凭据' : '来源尚未核验' }}</p>
              <RouterLink v-if="inspectedAsset.session_id" :to="`/chat/${encodeURIComponent(inspectedAsset.session_id)}`" class="detail-source-link">查看来源分析</RouterLink>
            </div>
            <div v-if="inspectedAsset" class="detail-record-actions">
              <button type="button" class="record-action" @click="openEdit(inspectedAsset)"><Pencil :size="14" />编辑数据记录</button>
            </div>
          </template>
          
        </template>
      </LibraryFileManager>
      <button v-if="nextCursor" type="button" :disabled="loading" class="load-more" @click="load(true)">{{ loading ? '正在读取…' : '加载更多数据' }}</button>
    </div>
    <SaveToLibraryDialog :visible="saveVisible" :files="uploadFiles" :target-folder-id="uploadTargetId" source-type="upload" @close="saveVisible = false" @saved="handleSaved" />
    <DialogRoot :open="deleteTargets.length > 0" @update:open="value => { if (!value && !deleting) closeDelete(); }">
      <DialogPortal><DialogOverlay class="fixed inset-0 z-[1100] bg-black/40" /><DialogContent class="fixed left-1/2 top-1/2 z-[1101] w-[calc(100%_-_2rem)] max-w-lg -translate-x-1/2 -translate-y-1/2 rounded-2xl border border-[var(--border-main)] bg-[var(--background-menu-white)] p-5 shadow-xl" @escape-key-down="event => { if (deleting) event.preventDefault(); }" @interact-outside="event => { if (deleting) event.preventDefault(); }">
        <DialogTitle class="text-lg font-semibold">删除 {{ deleteTargets.length }} 个文件？</DialogTitle>
        <DialogDescription class="mt-2 text-sm leading-6 text-[var(--text-tertiary)]">将从“我的数据”移除以下文件。已有对话中的文件和其他保存记录会保留。</DialogDescription>
        <ul class="delete-file-list" aria-label="待删除文件"><li v-for="file in deleteTargets" :key="file.id"><FileText :size="15" /><span>{{ file.name }}<small v-if="file.file.relative_path">{{ file.file.relative_path }}</small></span></li></ul>
        <p v-if="deleteError" role="alert" class="mt-3 text-sm text-red-600">{{ deleteError }}</p>
        <p v-if="deleting" role="status" class="mt-3 text-xs text-[var(--text-tertiary)]">正在删除，请稍候…</p>
        <div class="mt-5 flex justify-end gap-2"><button type="button" :disabled="deleting" class="action-button" @click="closeDelete">取消</button><button type="button" :disabled="deleting" class="delete-confirm" @click="confirmDelete">{{ deleting ? '正在删除…' : deleteError ? '重试删除' : '确认删除' }}</button></div>
      </DialogContent></DialogPortal>
    </DialogRoot>
    <DialogRoot :open="Boolean(folderDeleteTarget)" @update:open="value => { if (!value) closeFolderDelete(); }">
      <DialogPortal><DialogOverlay class="fixed inset-0 z-[1100] bg-black/40" /><DialogContent class="fixed left-1/2 top-1/2 z-[1101] w-[calc(100%_-_2rem)] max-w-lg -translate-x-1/2 -translate-y-1/2 rounded-2xl border border-[var(--border-main)] bg-[var(--background-menu-white)] p-5 shadow-xl" @escape-key-down="event => { if (folderDeleting) event.preventDefault(); }" @interact-outside="event => { if (folderDeleting) event.preventDefault(); }">
        <DialogTitle class="text-lg font-semibold">删除文件夹及全部内容？</DialogTitle>
        <DialogDescription class="mt-2 text-sm leading-6 text-[var(--text-tertiary)]">将删除此文件夹、全部子文件夹，以及其中保存的文件。已有分析中的文件和其他保存记录会保留。</DialogDescription>
        <p class="mt-4 break-words rounded-lg border border-[var(--border-main)] p-3 text-sm">我的数据 / {{ folderDeletePreview?.folder.path || folderDeleteTarget?.path }}</p>
        <p v-if="folderDeleteLoading" role="status" class="mt-3 text-sm text-[var(--text-tertiary)]">正在统计完整目录内容…</p>
        <p v-else-if="folderDeleteCountsUnknown" class="mt-3 text-sm">剩余范围暂时无法统计</p>
        <p v-else-if="folderDeletePreview" class="mt-3 text-sm">范围：此文件夹及 {{ folderDeletePreview.subfolder_count }} 个子文件夹，共 {{ folderDeletePreview.file_count }} 个文件。</p>
        <p class="mt-2 text-xs leading-5 text-[var(--text-tertiary)]">包括未加载、未显示和被筛选隐藏的内容。删除时会处理此目录中的最新内容。</p>
        <p v-if="folderDeleteError" role="alert" class="mt-3 text-sm text-red-600">{{ folderDeleteError }}</p>
        <div class="mt-5 flex justify-end gap-2"><button type="button" :disabled="folderDeleting" class="action-button" @click="closeFolderDelete">取消</button><button v-if="!folderDeletePreview" type="button" :disabled="folderDeleteLoading" class="action-button" @click="loadFolderDeletePreview">{{ folderDeleteLoading ? '正在统计…' : '重新统计' }}</button><button type="button" :disabled="folderDeleting || folderDeleteLoading || !folderDeletePreview" class="delete-confirm" @click="confirmFolderDelete">{{ folderDeleting ? '正在删除…' : folderDeleteError && folderDeletePreview ? '重试删除文件夹' : '确认删除文件夹' }}</button></div>
      </DialogContent></DialogPortal>
    </DialogRoot>
    <DialogRoot :open="newFolderVisible" @update:open="value => { if (!folderSaving) newFolderVisible = value; }">
      <DialogPortal><DialogOverlay class="fixed inset-0 z-[1100] bg-black/40" /><DialogContent class="fixed left-1/2 top-1/2 z-[1101] w-[calc(100%_-_2rem)] max-w-sm -translate-x-1/2 -translate-y-1/2 rounded-xl border border-[var(--border-main)] bg-[var(--background-menu-white)] p-5 shadow-xl" @escape-key-down="event => { if (folderSaving) event.preventDefault(); }" @interact-outside="event => { if (folderSaving) event.preventDefault(); }"><DialogTitle class="text-base font-semibold">新建文件夹</DialogTitle><DialogDescription class="mt-2 text-xs text-[var(--text-tertiary)]">创建位置：{{ newFolderParentName }}</DialogDescription><form class="mt-4" @submit.prevent="submitFolder"><label class="text-sm">文件夹名称<input v-model="newFolderName" required maxlength="100" :disabled="folderSaving" class="edit-input" placeholder="输入文件夹名称" /></label><p v-if="newFolderError" role="alert" class="mt-2 text-xs text-red-600">{{ newFolderError }}</p><div class="mt-5 flex justify-end gap-2"><button type="button" :disabled="folderSaving" class="action-button" @click="newFolderVisible = false">取消</button><button type="submit" :disabled="folderSaving || !newFolderName.trim()" class="primary-button">{{ folderSaving ? '正在创建…' : '创建' }}</button></div></form></DialogContent></DialogPortal>
    </DialogRoot>
    <DialogRoot :open="Boolean(editing)" @update:open="value => { if (!value && !editSaving) editing = undefined; }">
      <DialogPortal><DialogOverlay class="fixed inset-0 z-[1100] bg-black/40" /><DialogContent class="fixed left-1/2 top-1/2 z-[1101] w-[calc(100%_-_2rem)] max-w-lg -translate-x-1/2 -translate-y-1/2 rounded-2xl border border-[var(--border-main)] bg-[var(--background-menu-white)] p-5 shadow-xl" @escape-key-down="event => { if (editSaving) event.preventDefault(); }" @interact-outside="event => { if (editSaving) event.preventDefault(); }"><DialogTitle class="text-lg font-semibold">编辑数据记录</DialogTitle><DialogDescription class="mt-1 text-sm text-[var(--text-tertiary)]">修改所属数据记录的名称、说明和标签，文件内容保持不变。</DialogDescription><form class="mt-5 space-y-4" @submit.prevent="saveEdit"><label class="block text-sm">数据名称<input v-model="editName" required maxlength="200" :disabled="editSaving" class="edit-input" /></label><label class="block text-sm">说明<textarea v-model="editDescription" maxlength="4000" rows="3" :disabled="editSaving" class="edit-input" /></label><label class="block text-sm">标签 <span class="text-xs text-[var(--text-tertiary)]">（用逗号分隔，最多 10 个）</span><input v-model="editTags" maxlength="500" :disabled="editSaving" class="edit-input" placeholder="如：气候、实验数据" /></label><p v-if="editError" role="alert" class="text-sm text-red-600">{{ editError }}</p><div class="flex justify-end gap-2"><button type="button" :disabled="editSaving" class="action-button" @click="editing = undefined">取消</button><button type="submit" :disabled="editSaving || !editName.trim()" class="primary-button">{{ editSaving ? '正在保存…' : '保存修改' }}</button></div></form></DialogContent></DialogPortal>
    </DialogRoot>
  </main>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue';
import { useRouter } from 'vue-router';
import { DialogContent, DialogDescription, DialogOverlay, DialogPortal, DialogRoot, DialogTitle } from 'reka-ui';
import { ExternalLink, Eye, FileText, FolderPlus, MessageSquarePlus, PanelLeft, Pencil, RefreshCw, Trash2, Upload } from 'lucide-vue-next';
import ChatBoxFiles from '../components/ChatBoxFiles.vue';
import SaveToLibraryDialog from '../components/SaveToLibraryDialog.vue';
import LibraryFileManager from '../components/LibraryFileManager.vue';
import LibraryFileControls from '../components/LibraryFileControls.vue';
import { dataLibrarySourceLabels, deleteDataLibraryFiles, deleteDataLibraryFolder, previewDataLibraryFolderDeletion, updateDataLibraryAsset, type DataLibraryAsset, type DataLibraryFolder, type DataLibraryFolderDeletionPreview } from '../api/dataLibrary';
import type { FileInfo } from '../api/file';
import { useLibraryBrowser } from '../composables/useLibraryBrowser';
import { useLibraryFolders } from '../composables/useLibraryFolders';
import { useAuth } from '../composables/useAuth';
import { setPendingLibraryFiles } from '../composables/useDataSelection';
import { useFilePanel } from '../composables/useFilePanel';
import { useLeftPanel } from '../composables/useLeftPanel';
import { formatFileSize } from '../utils/fileType';
import { libraryBrowserFiles } from '../utils/libraryColumns';
import { libraryCurrentFiles, libraryFolderBreadcrumbs, type LibraryManagerFile } from '../utils/libraryFileManager';
import { libraryFileFormat, libraryFormatLabel } from '../utils/libraryFileFilters';
import { personalDataDisplayName, personalFileId, safeLibraryRelativePath, type PersonalDataFile } from '../utils/personalDataTree';
import { showErrorToast, showSuccessToast } from '../utils/toast';

const router = useRouter();
const { currentUser } = useAuth();
const { isLeftPanelShow, showLeftPanel } = useLeftPanel();
const { showFilePanel } = useFilePanel();
const currentFolderId = ref('root');
const { folders, loading: foldersLoading, error: folderError, load: loadFolders, createFolder } = useLibraryFolders();
const currentFolder = computed(() => folders.value.find(folder => folder.id === currentFolderId.value));
const { items, visibleItems, stats, query, fileFormat, fromDate, toDate, sourceType, nextCursor, loading, error, load, resetFilters } = useLibraryBrowser({ folderId: currentFolderId });
const currentFiles = computed(() => currentFolderId.value === 'root' ? libraryCurrentFiles(visibleItems.value, '') : currentFolder.value ? libraryCurrentFiles(visibleItems.value, currentFolder.value.path) : []);
const selected = ref(new Map<string, PersonalDataFile>());
const selectedFiles = computed(() => libraryBrowserFiles([...selected.value.values()].filter(value => !value.missing).map(value => value.file)));
const selectedUnavailable = computed(() => [...selected.value.values()].filter(value => value.missing).length);
const selectedIds = computed(() => [...selected.value.keys()]);
const allCurrentSelected = computed(() => currentFiles.value.length > 0 && currentFiles.value.every(file => selected.value.has(file.id)));
const deleteTargets = ref<PersonalDataFile[]>([]);
const deleting = ref(false);
const deleteError = ref('');
let deleteController: AbortController | undefined;
const folderDeleteTarget = ref<DataLibraryFolder | null>(null);
const folderDeletePreview = ref<DataLibraryFolderDeletionPreview | null>(null);
const folderDeleteLoading = ref(false);
const folderDeleteCountsUnknown = ref(false);
const folderDeleting = ref(false);
const folderDeleteError = ref('');
const deletionBusy = computed(() => deleting.value || folderDeleting.value);
let folderDeleteController: AbortController | undefined;
let folderDeleteGeneration = 0;
let navigationAncestorIds: string[] = [];
const selectionError = ref('');
const hasFilters = computed(() => Boolean(query.value || fileFormat.value || fromDate.value || toDate.value || sourceType.value));
const inspected = ref<LibraryManagerFile | null>(null);
const inspectedAsset = computed(() => {
  const node = inspected.value;
  return node?.kind === 'file' ? items.value.find(asset => asset.id === node.assetId) : undefined;
});
const assetDisplayName = computed(() => personalDataDisplayName(inspectedAsset.value?.name ?? '', '未命名数据'));
const uploadFiles = ref<FileInfo[]>([]);
const uploadRef = ref<InstanceType<typeof ChatBoxFiles>>();
const saveVisible = ref(false);
const uploadTargetId = ref<string | null>(null);
const uploadTargetName = computed(() => uploadTargetId.value ? folders.value.find(folder => folder.id === uploadTargetId.value)?.path || '所选文件夹' : '我的数据');
const newFolderVisible = ref(false);
const newFolderName = ref('');
const newFolderParentId = ref<string | null>(null);
const newFolderParentName = computed(() => newFolderParentId.value ? folders.value.find(folder => folder.id === newFolderParentId.value)?.path || '所选文件夹' : '我的数据');
const newFolderError = ref('');
const folderSaving = ref(false);
const editing = ref<DataLibraryAsset>();
const editName = ref('');
const editDescription = ref('');
const editTags = ref('');
const editSaving = ref(false);
const editError = ref('');
let active = true;
let ownerGeneration = 0;

function navigateFolder(id: string) {
  if (id !== 'root' && !folders.value.some(folder => folder.id === id)) return;
  inspected.value = null;
  navigationAncestorIds = libraryFolderBreadcrumbs(folders.value, id).slice(0, -1).reverse().map(folder => folder.id);
  currentFolderId.value = id;
}
function startUpload(id: string) {
  if (deletionBusy.value) return;
  const target = id === 'root' ? null : id;
  if (target && !folders.value.some(folder => folder.id === target)) return;
  if (uploadFiles.value.length && uploadTargetId.value !== target) { showErrorToast('请先保存当前已上传的文件，再向其他文件夹上传。'); return; }
  uploadTargetId.value = target;
  uploadRef.value?.uploadFile();
}
function openCreateFolder(id: string) {
  if (deletionBusy.value) return;
  if (id !== 'root' && !folders.value.some(folder => folder.id === id)) return;
  newFolderParentId.value = id === 'root' ? null : id;
  newFolderName.value = ''; newFolderError.value = ''; newFolderVisible.value = true;
}
async function submitFolder() {
  const name = newFolderName.value.trim();
  if (!name || folderSaving.value || deletionBusy.value) return;
  if (name === '.' || name === '..' || /[\\/:\u0000-\u001f\u007f]/.test(name)) { newFolderError.value = '文件夹名称不能包含斜杠、冒号或特殊控制字符。'; return; }
  const generation = ownerGeneration;
  const parentId = newFolderParentId.value;
  folderSaving.value = true; newFolderError.value = '';
  try {
    await createFolder({ name, parent_id: parentId });
    if (!active || generation !== ownerGeneration) return;
    newFolderVisible.value = false;
    showSuccessToast('文件夹已创建');
  } catch { if (active && generation === ownerGeneration) newFolderError.value = '无法创建文件夹，请检查是否已有同名文件夹后重试。'; }
  finally { if (active && generation === ownerGeneration) folderSaving.value = false; }
}
function refreshAll() { void load(); void loadFolders(); }
function clearSelection() { if (!deletionBusy.value) { selected.value = new Map(); selectionError.value = ''; } }
function toggleFile(node: PersonalDataFile) {
  if (deletionBusy.value) return;
  const next = new Map(selected.value);
  if (next.has(node.id)) next.delete(node.id);
  else next.set(node.id, node);
  selected.value = next;
  selectionError.value = '';
}
function selectCurrentFiles() {
  if (deletionBusy.value || loading.value) return;
  const next = new Map(selected.value);
  for (const file of currentFiles.value) next.set(file.id, file);
  selected.value = next; selectionError.value = '';
}
function toggleCurrentSelection() {
  if (deletionBusy.value || loading.value) return;
  if (!allCurrentSelected.value) { selectCurrentFiles(); return; }
  const next = new Map(selected.value);
  for (const file of currentFiles.value) next.delete(file.id);
  selected.value = next; selectionError.value = '';
}
function startAnalysis(files: FileInfo[]) {
  if (!currentUser.value) return;
  const readyFiles = libraryBrowserFiles(files);
  if (!readyFiles.length) return;
  if (readyFiles.length > 32) { selectionError.value = '本次分析最多使用 32 个文件，请减少选择。'; return; }
  setPendingLibraryFiles(readyFiles);
  void router.push('/chat');
}
function startSelectedAnalysis() {
  if (deletionBusy.value) return;
  if (selectedUnavailable.value) { selectionError.value = '请先取消勾选不可用文件，再开始分析。'; return; }
  startAnalysis(selectedFiles.value);
}
function startFileAnalysis() {
  const node = inspected.value;
  if (node?.kind === 'file' && !node.missing) startAnalysis([node.file]);
}
function previewSelectedFile(file: LibraryManagerFile) { inspected.value = file; previewFile(); }
function previewFile() {
  const node = inspected.value;
  if (node?.kind === 'file' && !node.missing && inspectedAsset.value) {
    showFilePanel(node.file, libraryBrowserFiles(inspectedAsset.value.files));
  }
}
function safeSourceUrl(value: string | null): string | undefined {
  if (!value) return undefined;
  try { const url = new URL(value); return ['https:', 'http:'].includes(url.protocol) ? url.href : undefined; } catch { return undefined; }
}
const formatDate = (value: string) => { const date = new Date(value); return Number.isNaN(date.getTime()) ? '暂未记录' : date.toLocaleString('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }); };
const fileSize = (value?: number) => typeof value === 'number' && Number.isFinite(value) && value >= 0 ? formatFileSize(value) : '暂未记录';
const formatLabel = (file: FileInfo) => libraryFormatLabel(libraryFileFormat(file));
function refresh() { void load(); }
function handleSaved(asset: DataLibraryAsset) {
  const savedIds = new Set(asset.files.map(personalFileId));
  uploadFiles.value = uploadFiles.value.filter(file => !savedIds.has(personalFileId(file)));
}
function openEdit(asset: DataLibraryAsset) {
  editing.value = asset;
  editName.value = personalDataDisplayName(asset.name, '未命名数据');
  editDescription.value = asset.description;
  editTags.value = asset.tags.join('，');
  editError.value = '';
}
async function saveEdit() {
  const asset = editing.value;
  if (!asset || editSaving.value || !editName.value.trim()) return;
  const tags = [...new Set(editTags.value.split(/[,，]/).map(tag => tag.trim()).filter(Boolean))];
  if (tags.length > 10 || tags.some(tag => tag.length > 40)) { editError.value = '最多添加 10 个标签，每个标签不超过 40 个字符。'; return; }
  const generation = ownerGeneration;
  editSaving.value = true;
  editError.value = '';
  try {
    const result = await updateDataLibraryAsset(asset.id, { name: editName.value.trim(), description: editDescription.value.trim(), tags });
    if (!active || generation !== ownerGeneration) return;
    items.value = items.value.map(item => item.id === result.id ? result : item);
    editing.value = undefined;
    showSuccessToast('数据说明已更新');
    window.dispatchEvent(new Event('data-library-changed'));
  } catch { if (active && generation === ownerGeneration) editError.value = '暂时无法保存修改，请重试。'; }
  finally { if (active && generation === ownerGeneration) editSaving.value = false; }
}
function fileReferenceKey(assetId: string, fileId: string) {
  return JSON.stringify(['file', assetId, personalFileId({ file_id: fileId })]);
}
function requestDelete(files: PersonalDataFile[]) {
  if (deletionBusy.value || folderDeleteTarget.value || !currentUser.value || !files.length) return;
  deleteTargets.value = [...new Map(files.map(file => [fileReferenceKey(file.assetId, file.file.file_id), { ...file, file: { ...file.file } }])).values()];
  deleteError.value = '';
}
function closeDelete() {
  if (deleting.value) return;
  deleteTargets.value = []; deleteError.value = '';
}
async function confirmDelete() {
  if (deletionBusy.value || folderDeleteTarget.value || !deleteTargets.value.length || !currentUser.value) return;
  const generation = ownerGeneration;
  const targets = [...deleteTargets.value];
  const removed = new Set<string>();
  let deletedCount = 0;
  let missingCount = 0;
  const controller = new AbortController();
  deleteController = controller;
  deleting.value = true; deleteError.value = '';
  try {
    // Library selection is unrestricted. Send bounded requests for large selections.
    for (let offset = 0; offset < targets.length; offset += 500) {
      if (!active || generation !== ownerGeneration) return;
      const batch = targets.slice(offset, offset + 500);
      const result = await deleteDataLibraryFiles(batch.map(file => ({ asset_id: file.assetId, file_id: personalFileId(file.file) })), controller.signal);
      if (!active || generation !== ownerGeneration) return;
      const outcomes = new Map(result.results.map(item => [fileReferenceKey(item.asset_id, item.file_id), item.status]));
      for (const file of batch) {
        const key = fileReferenceKey(file.assetId, file.file.file_id);
        const status = outcomes.get(key);
        if (status === 'deleted' || status === 'not_found') {
          removed.add(key);
          if (status === 'deleted') deletedCount++;
          else missingCount++;
        }
      }
    }
  } catch {
    // Completed batches are retained; only unconfirmed files remain retryable.
  } finally {
    if (active && generation === ownerGeneration) {
      deleteController = undefined;
      deleting.value = false;
      deleteTargets.value = targets.filter(file => !removed.has(fileReferenceKey(file.assetId, file.file.file_id)));
      if (removed.size) {
        selected.value = new Map([...selected.value].filter(([, file]) => !removed.has(fileReferenceKey(file.assetId, file.file.file_id))));
        items.value = items.value.map(asset => ({ ...asset, files: asset.files.filter(file => !removed.has(fileReferenceKey(asset.id, file.file_id))) })).filter(asset => asset.files.length > 0);
        if (inspected.value && removed.has(fileReferenceKey(inspected.value.assetId, inspected.value.file.file_id))) inspected.value = null;
        if (editing.value && !items.value.some(asset => asset.id === editing.value?.id)) editing.value = undefined;
        selectionError.value = '';
        const messages = [deletedCount ? `已从我的数据删除 ${deletedCount} 个文件` : '', missingCount ? `${missingCount} 个文件已不存在，已更新列表` : ''].filter(Boolean);
        showSuccessToast(messages.join('；'));
        window.dispatchEvent(new Event('data-library-changed'));
      }
      if (deleteTargets.value.length) {
        deleteError.value = `${removed.size ? `已处理 ${removed.size} 个文件；` : ''}还有 ${deleteTargets.value.length} 个文件未确认删除成功，请重试。`;
      }
    }
  }
}
function isWithinFolder(path: string, folderPath: string) {
  return path === folderPath || path.startsWith(`${folderPath}/`);
}
function fileIsWithinFolder(file: FileInfo, folderPath: string) {
  const path = safeLibraryRelativePath(file.relative_path);
  return Boolean(path?.startsWith(`${folderPath}/`));
}
function clearFolderFileSelection(folderPath: string) {
  selected.value = new Map([...selected.value].filter(([, file]) => !fileIsWithinFolder(file.file, folderPath)));
  if (inspected.value && fileIsWithinFolder(inspected.value.file, folderPath)) inspected.value = null;
  selectionError.value = '';
}
function resetFolderDelete() {
  folderDeleteGeneration++;
  folderDeleteController?.abort(); folderDeleteController = undefined;
  folderDeleteTarget.value = null; folderDeletePreview.value = null;
  folderDeleteLoading.value = false; folderDeleteCountsUnknown.value = false; folderDeleting.value = false; folderDeleteError.value = '';
}
function closeFolderDelete() {
  if (!folderDeleting.value) resetFolderDelete();
}
async function requestFolderDelete(id: string) {
  if (id === 'root' || deletionBusy.value || deleteTargets.value.length || folderSaving.value || !currentUser.value) return;
  const folder = folders.value.find(item => item.id === id);
  if (!folder || !safeLibraryRelativePath(folder.path)) return;
  resetFolderDelete();
  folderDeleteTarget.value = { ...folder };
  await loadFolderDeletePreview();
}
async function loadFolderDeletePreview() {
  const target = folderDeleteTarget.value;
  if (!target || folderDeleting.value || !currentUser.value) return;
  folderDeleteController?.abort();
  const requestGeneration = ++folderDeleteGeneration;
  const generation = ownerGeneration;
  const controller = new AbortController();
  folderDeleteController = controller;
  folderDeleteLoading.value = true; folderDeleteCountsUnknown.value = false; folderDeleteError.value = ''; folderDeletePreview.value = null;
  try {
    const preview = await previewDataLibraryFolderDeletion(target.id, controller.signal);
    if (!active || generation !== ownerGeneration || requestGeneration !== folderDeleteGeneration || controller.signal.aborted) return;
    if (preview.folder.id !== target.id || !safeLibraryRelativePath(preview.folder.path) || !/^[a-f0-9]{64}$/.test(preview.deletion_token) || !Number.isSafeInteger(preview.file_count) || preview.file_count < 0 || !Number.isSafeInteger(preview.subfolder_count) || preview.subfolder_count < 0) throw new Error('Invalid folder preview');
    folderDeleteTarget.value = { ...preview.folder };
    folderDeletePreview.value = preview;
  } catch {
    if (active && generation === ownerGeneration && requestGeneration === folderDeleteGeneration && !controller.signal.aborted) folderDeleteError.value = '暂时无法统计完整目录内容，请重新统计后再删除。';
  } finally {
    if (active && generation === ownerGeneration && requestGeneration === folderDeleteGeneration) { folderDeleteLoading.value = false; folderDeleteController = undefined; }
  }
}
async function confirmFolderDelete() {
  const target = folderDeleteTarget.value;
  const preview = folderDeletePreview.value;
  if (!target || !preview || !/^[a-f0-9]{64}$/.test(preview.deletion_token) || folderDeleteLoading.value || deletionBusy.value || deleteTargets.value.length || !currentUser.value) return;
  const generation = ownerGeneration;
  const requestGeneration = ++folderDeleteGeneration;
  const scopeFolderIds = new Set([target.id, ...folders.value.filter(folder => isWithinFolder(folder.path, target.path)).map(folder => folder.id)]);
  const ancestorIds = libraryFolderBreadcrumbs(folders.value, target.id).slice(0, -1).reverse().map(folder => folder.id);
  const controller = new AbortController();
  folderDeleteController = controller;
  folderDeleting.value = true; folderDeleteError.value = '';
  try {
    const result = await deleteDataLibraryFolder(target.id, preview.deletion_token, controller.signal);
    if (!active || generation !== ownerGeneration || requestGeneration !== folderDeleteGeneration) return;
    if (result.folder_id !== target.id || typeof result.completed !== 'boolean') throw new Error('Invalid folder deletion result');
    if (!result.completed) {
      if (result.deleted_file_count > 0) clearFolderFileSelection(target.path);
      folderDeleteCountsUnknown.value = result.remaining_file_count === null || result.remaining_folder_count === null;
      if (result.remaining_file_count !== null && result.remaining_folder_count !== null) {
        folderDeletePreview.value = { folder: target, deletion_token: preview.deletion_token, subfolder_count: Math.max(0, result.remaining_folder_count - 1), file_count: result.remaining_file_count };
      }
      const remaining = folderDeleteCountsUnknown.value ? '剩余范围暂时无法统计' : `还有 ${result.remaining_file_count} 个文件、${result.remaining_folder_count} 个文件夹未完成`;
      folderDeleteError.value = `已删除 ${result.deleted_file_count} 个文件、${result.deleted_folder_count} 个文件夹；${remaining}，请重试。`;
      window.dispatchEvent(new Event('data-library-changed'));
      return;
    }
    const removedFolderIds = new Set([...scopeFolderIds, ...folders.value.filter(folder => isWithinFolder(folder.path, target.path)).map(folder => folder.id)]);
    const shouldNavigate = currentFolder.value && isWithinFolder(currentFolder.value.path, target.path);
    const parentCandidates = [...new Set([result.parent_id, target.parent_id, ...ancestorIds].filter((id): id is string => Boolean(id)))];
    clearFolderFileSelection(target.path);
    items.value = items.value.map(asset => ({ ...asset, files: asset.files.filter(file => !fileIsWithinFolder(file, target.path)) })).filter(asset => asset.files.length > 0);
    if (editing.value && editing.value.files.some(file => fileIsWithinFolder(file, target.path))) editing.value = undefined;
    let uploadTargetReset = false;
    if (uploadTargetId.value && removedFolderIds.has(uploadTargetId.value)) {
      uploadTargetId.value = null; saveVisible.value = false; uploadTargetReset = uploadFiles.value.length > 0;
    }
    if (newFolderParentId.value && removedFolderIds.has(newFolderParentId.value)) {
      newFolderParentId.value = null; newFolderVisible.value = false; newFolderName.value = ''; newFolderError.value = '';
    }
    folders.value = folders.value.filter(folder => !isWithinFolder(folder.path, target.path));
    if (shouldNavigate) navigateFolder(parentCandidates.find(id => folders.value.some(folder => folder.id === id)) ?? 'root');
    selectionError.value = '';
    folderDeleteTarget.value = null; folderDeletePreview.value = null;
    showSuccessToast(`文件夹及全部内容已删除${uploadTargetReset ? '；待保存文件的上传目标已重置为我的数据' : ''}`);
    window.dispatchEvent(new Event('data-library-changed'));
  } catch (error) {
    if (active && generation === ownerGeneration && requestGeneration === folderDeleteGeneration) {
      if ((error as { response?: { status?: number } })?.response?.status === 409) {
        folderDeletePreview.value = null;
        folderDeleteError.value = '文件夹内容已更新，请重新统计后再确认删除。';
      } else folderDeleteError.value = '文件夹删除尚未确认完成，请重试。';
    }
  } finally {
    if (active && generation === ownerGeneration && requestGeneration === folderDeleteGeneration) { folderDeleting.value = false; folderDeleteController = undefined; }
  }
}
watch(() => currentUser.value?.id, () => {
  ownerGeneration++;
  deleteController?.abort(); deleteController = undefined; deleting.value = false; deleteTargets.value = []; deleteError.value = '';
  resetFolderDelete(); navigationAncestorIds = [];
  clearSelection(); uploadFiles.value = []; uploadTargetId.value = null; currentFolderId.value = 'root'; inspected.value = null; saveVisible.value = false; editing.value = undefined;
  newFolderVisible.value = false; newFolderName.value = ''; newFolderParentId.value = null; newFolderError.value = ''; folderSaving.value = false;
  editName.value = ''; editDescription.value = ''; editTags.value = ''; editSaving.value = false; editError.value = '';
}, { flush: 'sync' });
watch(currentFiles, files => { if (inspected.value && !files.some(file => file.id === inspected.value?.id)) inspected.value = null; });
watch([folders, foldersLoading], () => { if (!foldersLoading.value && !folderError.value && currentFolderId.value !== 'root' && !currentFolder.value) navigateFolder(navigationAncestorIds.find(id => folders.value.some(folder => folder.id === id)) ?? 'root'); });
onMounted(() => window.addEventListener('data-library-changed', refresh));
onUnmounted(() => { active = false; ownerGeneration++; deleteController?.abort(); deleting.value = false; deleteTargets.value = []; resetFolderDelete(); clearSelection(); uploadFiles.value = []; window.removeEventListener('data-library-changed', refresh); });
</script>

<style scoped>
.my-data-page { display: flex; flex: 1; min-width: 0; min-height: 0; flex-direction: column; overflow: hidden; background: var(--background-menu-white); color: var(--text-primary); }
.my-data-header { display: flex; flex: none; align-items: center; gap: 12px; padding: 20px 24px 16px; border-bottom: 1px solid var(--border-main); }
.create-folder-short { display: none; }
.my-data-header h1 { font-size: 20px; font-weight: 600; }
.my-data-header p { margin-top: 5px; font-size: 12px; line-height: 1.6; color: var(--text-tertiary); }
.my-data-content { display: flex; flex: 1; min-height: 0; min-width: 0; flex-direction: column; gap: 12px; padding: 18px 24px; }
.library-toolbar { display: flex; flex: none; align-items: center; flex-wrap: wrap; gap: 10px; }
.selected-file-count { font-size: 12px; white-space: nowrap; }
.clear-selection { font-size: 11px; color: var(--text-tertiary); text-decoration: underline; }
.clear-selection:disabled { opacity: .5; cursor: not-allowed; }
.upload-staging { display: contents; }
.upload-staging.has-uploads { display: block; flex: none; min-height: 0; max-height: 155px; overflow-y: auto; padding-bottom: 6px; border-bottom: 1px solid var(--border-main); }
.library-error { display: flex; flex: none; gap: 12px; align-items: center; font-size: 12px; color: var(--text-secondary); }
.library-error button { color: #2b7659; text-decoration: underline; }
.library-state { display: flex; flex: 1; min-height: 180px; align-items: center; justify-content: center; gap: 10px; font-size: 13px; color: var(--text-tertiary); }
.empty-library { flex-direction: column; text-align: center; }
.empty-library h2 { color: var(--text-primary); font-size: 15px; font-weight: 500; }
.empty-library p { max-width: 320px; line-height: 1.7; }
.detail-file-symbol { display: flex; width: 56px; height: 56px; align-items: center; justify-content: center; color: #2b7659; }
.detail-title { margin-top: 8px; font-size: 15px; font-weight: 600; overflow-wrap: anywhere; line-height: 1.6; }
.detail-kind { margin-top: 3px; font-size: 11px; color: var(--text-tertiary); }
.detail-missing { margin-top: 10px; font-size: 12px; color: var(--text-tertiary); }
.detail-actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 16px; }
.detail-facts { display: grid; gap: 12px; margin-top: 22px; font-size: 11px; }
.detail-facts > div { display: grid; grid-template-columns: 64px minmax(0, 1fr); gap: 10px; }
.detail-facts dt { color: var(--text-tertiary); }
.detail-facts dd { margin: 0; color: var(--text-secondary); line-height: 1.7; overflow-wrap: anywhere; }
.detail-record { margin-top: 20px; border-top: 1px solid var(--border-main); padding-top: 16px; }
.detail-record h3 { font-size: 11px; font-weight: 500; color: var(--text-tertiary); }
.detail-record > p { margin-top: 8px; white-space: pre-line; overflow-wrap: anywhere; font-size: 12px; line-height: 1.8; color: var(--text-secondary); }
.detail-tags { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 10px; }
.detail-tags span { padding: 2px 6px; max-width: 100%; border-radius: 4px; background: var(--fill-tsp-gray-main); font-size: 10px; overflow-wrap: anywhere; }
.detail-source-link { display: inline-flex; align-items: center; gap: 5px; margin-top: 10px; color: #2b7659; font-size: 11px; }
.detail-record .source-verification { font-size: 10px; color: var(--text-tertiary); }
.detail-record-actions { display: flex; flex-direction: column; align-items: flex-start; gap: 6px; margin-top: 18px; padding-top: 12px; border-top: 1px solid var(--border-main); }
.record-action { display: flex; align-items: center; gap: 7px; padding: 5px 0; font-size: 11px; color: var(--text-secondary); }
.record-action:hover { color: #2b7659; }
.detail-placeholder { display: flex; flex-direction: column; align-items: center; gap: 12px; padding-top: 44px; text-align: center; color: var(--text-tertiary); }
.detail-placeholder p { font-size: 13px; }
.detail-placeholder span { font-size: 11px; line-height: 1.7; }
.load-more { flex: none; align-self: center; padding: 4px 12px; font-size: 12px; color: #2b7659; }
.action-button { @apply inline-flex items-center justify-center gap-1.5 rounded-md border border-[var(--border-main)] bg-[var(--background-menu-white)] px-2.5 py-2 text-xs hover:bg-[var(--fill-tsp-gray-main)] disabled:opacity-50; }
.danger-action { color: #b42318; }
.delete-confirm { @apply inline-flex items-center justify-center rounded-md bg-red-700 px-3 py-2 text-xs text-white hover:bg-red-800 disabled:cursor-not-allowed disabled:opacity-50; }
.delete-file-list { display: grid; gap: 10px; max-height: 240px; overflow-y: auto; margin-top: 16px; padding: 12px; border: 1px solid var(--border-main); border-radius: 8px; }
.delete-file-list li { display: flex; align-items: flex-start; gap: 8px; font-size: 12px; overflow-wrap: anywhere; }
.delete-file-list svg { flex: none; margin-top: 2px; color: var(--text-tertiary); }
.delete-file-list small { display: block; margin-top: 2px; font-size: 10px; color: var(--text-tertiary); }
.primary-button { @apply inline-flex items-center justify-center gap-1.5 rounded-md bg-[#2b7659] px-3 py-2 text-xs text-white hover:bg-[#245f49] disabled:cursor-not-allowed disabled:opacity-50; }
.icon-action { @apply rounded-md p-2 text-[var(--text-tertiary)] hover:bg-[var(--fill-tsp-gray-main)] hover:text-[var(--text-primary)]; }
.edit-input { @apply mt-1.5 w-full rounded-lg border border-[var(--border-main)] bg-transparent px-3 py-2 text-sm outline-none focus:border-[#2b7659]; }
:global(.dark .detail-file-symbol), :global(.dark .detail-source-link) { color: #a9cbbb; }
@media (max-width: 639px) {
  .my-data-header { padding: 14px 12px; gap: 8px; }
  .my-data-header h1 { font-size: 18px; }
  .my-data-header > .action-button { padding: 7px; font-size: 11px; }
  .my-data-header > .action-button { white-space: nowrap; }
  .my-data-header .create-folder-full { display: none; }
  .my-data-header .create-folder-short { display: inline; }
  .my-data-header .icon-action { padding: 5px; }
  .my-data-header p { display: none; }
  .my-data-content { padding: 12px; gap: 10px; }
  .library-toolbar { gap: 7px; }
}
</style>
