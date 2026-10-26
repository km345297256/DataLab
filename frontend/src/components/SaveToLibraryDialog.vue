<template>
  <DialogRoot :open="visible" @update:open="value => { if (!value && !busy) emit('close'); }">
    <DialogPortal>
      <DialogOverlay class="fixed inset-0 z-[1100] bg-black/40" />
      <DialogContent class="fixed left-1/2 top-1/2 z-[1101] flex max-h-[90vh] w-[calc(100%_-_2rem)] max-w-xl -translate-x-1/2 -translate-y-1/2 flex-col rounded-2xl border border-[var(--border-main)] bg-[var(--background-menu-white)] shadow-xl" @escape-key-down="event => { if (busy) event.preventDefault(); }" @interact-outside="event => { if (busy) event.preventDefault(); }">
        <header class="border-b border-[var(--border-main)] px-5 py-4">
          <DialogTitle class="pr-8 text-lg font-semibold">保存到我的数据</DialogTitle>
          <DialogDescription class="mt-1 text-sm text-[var(--text-tertiary)]">保存实际文件，下次分析时可以继续使用。删除对话不会删除已保存的数据。</DialogDescription>
          <button type="button" :disabled="busy" class="absolute right-4 top-4 rounded-md p-1.5 hover:bg-[var(--fill-tsp-gray-main)] disabled:opacity-40" aria-label="关闭" @click="emit('close')"><X class="size-4" /></button>
        </header>
        <form id="save-library-form" class="min-h-0 space-y-4 overflow-y-auto p-5" @submit.prevent="save">
          <label class="block text-sm font-medium">数据名称<input v-model="name" required maxlength="200" :disabled="busy" class="mt-1.5 w-full rounded-lg border border-[var(--border-main)] bg-transparent px-3 py-2 font-normal outline-none focus:border-[#2b7659]" placeholder="为这份数据起个名称" /></label>
          <LibraryFolderSelect v-if="visible" :key="folderPickerKey" v-model="selectedFolderId" :disabled="saving" @path-change="selectedFolderPath = $event" @busy="folderCreating = $event" @ready="folderReady = $event" />
          <label class="block text-sm font-medium">说明 <span class="font-normal text-[var(--text-tertiary)]">（可选）</span><textarea v-model="description" rows="2" maxlength="4000" :disabled="busy" class="mt-1.5 w-full resize-y rounded-lg border border-[var(--border-main)] bg-transparent px-3 py-2 font-normal outline-none focus:border-[#2b7659]" placeholder="记录数据用途或分析方法" /></label>
          <div class="flex items-center justify-between text-xs text-[var(--text-tertiary)]"><span>{{ sourceType ? dataLibrarySourceLabels[sourceType] : '自动记录文件来源' }} · 仅自己可见</span><span>已选 {{ selectedIds.size }} 个文件</span></div>
          <fieldset :disabled="busy" class="max-h-52 overflow-y-auto rounded-lg border border-[var(--border-main)]">
            <label v-for="file in uniqueFiles" :key="file.file_id" class="flex cursor-pointer items-center gap-3 border-b border-[var(--border-main)] px-3 py-2.5 last:border-b-0 hover:bg-[var(--fill-tsp-gray-main)]">
              <input type="checkbox" :checked="selectedIds.has(file.file_id)" :aria-label="`选择 ${file.filename}`" @change="toggleFile(file.file_id)" />
              <FileText class="size-4 shrink-0 text-[#2b7659]" /><span class="min-w-0 flex-1 truncate text-sm">{{ file.filename }}</span><span class="shrink-0 text-xs text-[var(--text-tertiary)]">{{ formatFileSize(file.size) }}</span>
            </label>
            <p v-if="!uniqueFiles.length" class="p-5 text-center text-sm text-[var(--text-tertiary)]">暂无可以保存的文件</p>
          </fieldset>
          <p v-if="error" role="alert" class="text-sm text-red-600">{{ error }}</p>
        </form>
        <footer class="flex justify-end gap-2 border-t border-[var(--border-main)] px-5 py-4">
          <button type="button" :disabled="busy" class="rounded-lg px-4 py-2 text-sm hover:bg-[var(--fill-tsp-gray-main)] disabled:opacity-40" @click="emit('close')">取消</button>
          <button type="submit" form="save-library-form" :disabled="busy || !folderReady || !name.trim() || !selectedIds.size" class="flex items-center gap-2 rounded-lg bg-[#2b7659] px-4 py-2 text-sm text-white disabled:cursor-not-allowed disabled:opacity-50"><LoaderCircle v-if="saving" class="size-4 animate-spin" />{{ saving ? '正在保存…' : '保存数据' }}</button>
        </footer>
      </DialogContent>
    </DialogPortal>
  </DialogRoot>
</template>

<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue';
import { DialogContent, DialogDescription, DialogOverlay, DialogPortal, DialogRoot, DialogTitle } from 'reka-ui';
import { FileText, LoaderCircle, X } from 'lucide-vue-next';
import type { FileInfo } from '../api/file';
import LibraryFolderSelect from './LibraryFolderSelect.vue';
import { useAuth } from '../composables/useAuth';
import { dataLibrarySourceLabels, saveDataLibraryAsset, type DataLibraryAsset, type DataLibrarySourceType } from '../api/dataLibrary';
import { formatFileSize } from '../utils/fileType';
import { showSuccessToast } from '../utils/toast';

const props = defineProps<{ visible: boolean; files: FileInfo[]; sessionId?: string; sourceType?: DataLibrarySourceType; defaultName?: string; targetFolderId?: string | null }>();
const emit = defineEmits<{ (event: 'close'): void; (event: 'saved', asset: DataLibraryAsset): void }>();
const { currentUser } = useAuth();
const name = ref('');
const description = ref('');
const selectedIds = ref(new Set<string>());
const saving = ref(false);
const folderCreating = ref(false);
const folderReady = ref(false);
const folderPickerKey = ref(0);
const selectedFolderId = ref<string | null>(null);
const selectedFolderPath = ref<string | null>(null);
const busy = computed(() => saving.value || folderCreating.value);
const error = ref('');
const uniqueFiles = computed(() => [...new Map(props.files.filter(file => file.file_id && !file.file_id.startsWith('temp-')).map(file => [file.file_id, file])).values()]);
let generation = 0;
let active = true;

function toggleFile(id: string) {
  const next = new Set(selectedIds.value);
  if (next.has(id)) next.delete(id);
  else next.add(id);
  selectedIds.value = next;
  error.value = '';
}

function reset() {
  generation += 1;
  saving.value = false;
  folderCreating.value = false;
  selectedFolderId.value = props.targetFolderId ?? null;
  selectedFolderPath.value = null;
  folderReady.value = false;
  folderPickerKey.value++;
  name.value = props.defaultName || uniqueFiles.value[0]?.filename || '';
  description.value = '';
  selectedIds.value = new Set(uniqueFiles.value.map(file => file.file_id));
  error.value = '';
}

function logicalPaths(files: FileInfo[]): Record<string, string> {
  const paths: Record<string, string> = {};
  const byteLength = (value: string) => new TextEncoder().encode(value).length;
  for (const file of files) {
    let path = file.relative_path;
    if (!path || !path.includes('/') || byteLength(path) > 1024 || /[\\:\x00-\x1f\x7f-\x9f]/.test(path)) continue;
    const parts = path.split('/');
    if (parts.length > 16 || parts[parts.length - 1] !== file.filename || parts.some(part =>
      !part || part === '.' || part === '..' || part !== part.trim() || byteLength(part) > 255,
    )) continue;
    // Existing library files carry paths from the library root. The API expects
    // paths relative to the chosen folder; do not prepend that folder twice.
    if (selectedFolderPath.value && path.startsWith(`${selectedFolderPath.value}/`)) {
      path = path.slice(selectedFolderPath.value.length + 1);
    }
    if (path.includes('/')) paths[file.file_id] = path;
  }
  return paths;
}

async function save() {
  if (busy.value || !folderReady.value || !currentUser.value?.id || !name.value.trim() || !selectedIds.value.size) return;
  const requestGeneration = generation;
  const requestOwner = currentUser.value.id;
  const fileIds = uniqueFiles.value.filter(file => selectedIds.value.has(file.file_id)).map(file => file.file_id);
  if (!fileIds.length) return;
  saving.value = true;
  error.value = '';
  try {
    const filePaths = logicalPaths(uniqueFiles.value.filter(file => selectedIds.value.has(file.file_id)));
    const asset = await saveDataLibraryAsset({ name: name.value.trim(), description: description.value.trim(), file_ids: fileIds, target_folder_id: selectedFolderId.value, ...(Object.keys(filePaths).length ? { file_paths: filePaths } : {}), ...(props.sourceType ? { source_type: props.sourceType } : {}), ...(props.sessionId ? { session_id: props.sessionId } : {}) });
    if (currentUser.value?.id === requestOwner && typeof window !== 'undefined') window.dispatchEvent(new CustomEvent('data-library-changed'));
    if (!active || generation !== requestGeneration || !props.visible) return;
    showSuccessToast('已保存到我的数据');
    emit('saved', asset);
    emit('close');
  } catch (failure) {
    if (active && generation === requestGeneration) {
      const status = (failure as { response?: { status?: number } })?.response?.status;
      error.value = status === 409
        ? '所选位置已有同名文件或文件夹，请更换保存位置后重试。'
        : status === 404 ? '所选文件或文件夹已不可用，请刷新后重新选择。'
          : '保存未确认成功，请检查文件是否可用后重试。';
    }
  } finally {
    if (active && generation === requestGeneration) saving.value = false;
  }
}

watch(() => [props.visible, props.sessionId] as const, ([visible]) => { if (visible) reset(); else { generation += 1; saving.value = false; } }, { immediate: true });
watch(() => currentUser.value?.id, () => {
  generation++;
  saving.value = false; folderCreating.value = false; folderReady.value = false;
  name.value = ''; description.value = ''; error.value = '';
  selectedIds.value = new Set(); selectedFolderId.value = null; selectedFolderPath.value = null;
  if (props.visible) emit('close');
}, { flush: 'sync' });
onUnmounted(() => { active = false; generation += 1; });
</script>
