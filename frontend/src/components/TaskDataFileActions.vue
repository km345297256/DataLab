<template>
  <div v-if="item" class="task-file-actions" :class="{ compact }">
    <button type="button" class="task-file-action" :disabled="locked || !item.available" :title="locked ? '当前分析或恢复期间暂不能更改输入' : undefined" @click="changeInputSelection([item.file], !isInput)">
      <Minus v-if="isInput" :size="13" /><Plus v-else :size="13" />{{ isInput ? '移出分析' : '加入分析' }}
    </button>
    <span v-if="item.saved_locations.length" class="task-file-saved" :title="savedPaths"><Check :size="13" />已保存</span>
    <button v-else type="button" class="task-file-action" :disabled="!item.available" @click="savingVisible = true"><FolderPlus :size="13" />保存到我的数据</button>
    <SaveToLibraryDialog v-if="savingVisible && item && sessionId" :visible="savingVisible" :files="[item.file]" :session-id="sessionId" :source-type="sourceType" :default-name="item.file.filename" @close="savingVisible = false" @saved="saved" />
  </div>
</template>
<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { Check, FolderPlus, Minus, Plus } from 'lucide-vue-next';
import SaveToLibraryDialog from './SaveToLibraryDialog.vue';
import { useTaskData } from '../composables/useTaskData';
import type { DataLibrarySourceType } from '../api/dataLibrary';
const props = defineProps<{ fileId: string; compact?: boolean }>();
const { items, selectedIds, locked, sessionId, changeInputSelection, refresh } = useTaskData();
const savingVisible = ref(false);
const item = computed(() => items.value.find(entry => entry.file.file_id === props.fileId));
const isInput = computed(() => selectedIds.value.includes(props.fileId));
const sourceType = computed<DataLibrarySourceType>(() => item.value?.category === 'download' ? 'open_data' : item.value?.category === 'analysis' ? 'analysis' : 'upload');
const savedPaths = computed(() => item.value?.saved_locations.map(location => `我的数据 / ${location.file_path}`).join('\n') || '');
function saved() { savingVisible.value = false; void refresh(); }
watch([sessionId, () => props.fileId], () => { savingVisible.value = false; });
watch(() => item.value?.saved_locations.length, saved => { if (saved) savingVisible.value = false; });
</script>
<style scoped>
.task-file-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 14px; }
.task-file-action, .task-file-saved { display: inline-flex; align-items: center; gap: 4px; min-height: 30px; font-size: 12px; color: var(--control-focus-color); }
.task-file-action { border-radius: 5px; padding: 3px 4px; }
.task-file-action:hover { background: var(--control-selected-fill); }
.task-file-action:disabled { opacity: .45; cursor: not-allowed; }
.task-file-saved { color: var(--text-tertiary); }
.compact { gap: 4px 10px; }
.compact .task-file-action, .compact .task-file-saved { min-height: 26px; font-size: 11px; }
</style>
