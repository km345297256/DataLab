<template>
  <section class="folder-picker" aria-label="保存位置">
    <div class="folder-picker-heading"><label :for="selectId">保存位置</label><button type="button" class="new-folder-button" :disabled="disabled || creating || loading || !!error" @click="showCreate = !showCreate"><FolderPlus :size="14" />新建文件夹</button></div>
    <div class="folder-select-row"><Folder :size="16" /><select :id="selectId" :value="modelValue ?? ''" :disabled="disabled || creating || loading" aria-label="保存到文件夹" @change="selectFolder(($event.target as HTMLSelectElement).value)"><option value="">我的数据（根目录）</option><option v-if="modelValue && !selectedFolder" :value="modelValue" disabled>当前文件夹不可用</option><option v-for="folder in sortedFolders" :key="folder.id" :value="folder.id">{{ folder.path }}</option></select></div>
    <p v-if="loading" class="folder-message" role="status">正在读取文件夹…</p>
    <p v-else-if="error" class="folder-error" role="alert">{{ error }} <button type="button" :disabled="disabled || creating" @click="load">重试</button></p>
    <p v-else-if="modelValue && !selectedFolder" class="folder-error" role="alert">所选文件夹已不可用，请重新选择保存位置。</p>
    <div v-if="showCreate" class="new-folder-form">
      <p class="folder-message">在“{{ selectedFolder?.path || '我的数据' }}”中新建文件夹</p>
      <div class="new-folder-row"><input v-model="folderName" maxlength="255" :disabled="disabled || creating" aria-label="新文件夹名称" placeholder="文件夹名称" @keydown.enter.prevent="create" /><button type="button" class="create-folder-button" :disabled="disabled || creating || !folderName.trim() || !ready" @click="create"><LoaderCircle v-if="creating" :size="13" class="animate-spin" />{{ creating ? '正在创建…' : '创建' }}</button><button type="button" :disabled="disabled || creating" class="cancel-folder-button" @click="cancelCreate">取消</button></div>
      <p v-if="createError" class="folder-error" role="alert">{{ createError }}</p>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onUnmounted, ref, useId, watch } from 'vue';
import { Folder, FolderPlus, LoaderCircle } from 'lucide-vue-next';
import { useAuth } from '../composables/useAuth';
import { useLibraryFolders } from '../composables/useLibraryFolders';

const props = withDefaults(defineProps<{ modelValue: string | null; disabled?: boolean }>(), { disabled: false });
const emit = defineEmits<{
  (event: 'update:modelValue', value: string | null): void;
  (event: 'path-change', path: string | null): void;
  (event: 'busy', value: boolean): void;
  (event: 'ready', value: boolean): void;
}>();
const selectId = `save-folder-${useId()}`;
const { currentUser } = useAuth();
const { folders, loading, error, load, createFolder } = useLibraryFolders();
const sortedFolders = computed(() => [...folders.value].sort((a, b) => a.path.localeCompare(b.path, 'zh-CN')));
const selectedFolder = computed(() => folders.value.find(folder => folder.id === props.modelValue));
const ready = computed(() => !loading.value && !error.value && (!props.modelValue || Boolean(selectedFolder.value)));
const showCreate = ref(false);
const folderName = ref('');
const creating = ref(false);
const createError = ref('');
let generation = 0;
let active = true;

watch(ready, value => emit('ready', value), { immediate: true });
watch(() => selectedFolder.value?.path ?? null, value => emit('path-change', value), { immediate: true });
watch(creating, value => emit('busy', value), { flush: 'sync' });
watch(() => props.modelValue, () => { createError.value = ''; });
watch(() => currentUser.value?.id, () => {
  generation++;
  creating.value = false;
  showCreate.value = false;
  folderName.value = '';
  createError.value = '';
}, { flush: 'sync' });

function selectFolder(value: string) {
  if (creating.value || props.disabled) return;
  emit('update:modelValue', value || null);
}

function cancelCreate() {
  if (creating.value) return;
  showCreate.value = false;
  folderName.value = '';
  createError.value = '';
}

async function create() {
  if (creating.value || props.disabled || !ready.value || !currentUser.value?.id) return;
  const name = folderName.value.trim();
  if (!name) return;
  if (name === '.' || name === '..' || /[\\/:\x00-\x1f\x7f-\x9f]/.test(name) || new TextEncoder().encode(name).length > 255) {
    createError.value = '文件夹名称不能包含路径符号，且不能超过 255 字节。';
    return;
  }
  const requestGeneration = generation;
  creating.value = true;
  createError.value = '';
  try {
    const folder = await createFolder({ name, parent_id: props.modelValue });
    if (!active || requestGeneration !== generation) return;
    if (!folder) { createError.value = '未能确认文件夹创建成功，请刷新后重试。'; return; }
    emit('update:modelValue', folder.id);
    emit('path-change', folder.path);
    folderName.value = '';
    showCreate.value = false;
  } catch (failure) {
    if (!active || requestGeneration !== generation) return;
    const status = (failure as { response?: { status?: number } })?.response?.status;
    createError.value = status === 409 ? '该位置已有同名文件夹或文件，请更换名称。' : '未能确认文件夹创建成功，请刷新后重试。';
  } finally {
    if (active && requestGeneration === generation) creating.value = false;
  }
}

onUnmounted(() => { active = false; generation++; });
</script>

<style scoped>
.folder-picker-heading { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 6px; font-size: 14px; font-weight: 500; }
.new-folder-button { display: inline-flex; align-items: center; gap: 4px; padding: 3px 0; color: var(--text-brand); font-size: 12px; font-weight: 400; }
.folder-select-row { display: flex; align-items: center; gap: 8px; }
.folder-select-row > svg { flex: none; color: var(--text-brand); }
.folder-select-row select, .new-folder-row input { width: 100%; min-width: 0; padding: 8px 10px; border: 1px solid var(--border-main); border-radius: 7px; background: var(--background-menu-white); color: var(--text-primary); font-size: 13px; }
.folder-message { margin-top: 6px; color: var(--text-tertiary); font-size: 12px; }
.folder-error { margin-top: 6px; color: var(--function-error); font-size: 12px; line-height: 1.6; }
.folder-error button { text-decoration: underline; }
.new-folder-form { margin-top: 10px; }
.new-folder-row { display: flex; align-items: center; gap: 8px; margin-top: 6px; }
.create-folder-button { display: inline-flex; flex: none; align-items: center; gap: 4px; padding: 8px 10px; border: 1px solid var(--border-main); border-radius: 7px; color: var(--text-brand); font-size: 12px; }
.cancel-folder-button { flex: none; padding: 8px 2px; color: var(--text-tertiary); font-size: 12px; }
button:disabled, select:disabled, input:disabled { opacity: .5; cursor: not-allowed; }
</style>
