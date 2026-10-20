<template>
  <Teleport to="body">
    <div v-if="visible" class="fixed inset-0 z-[1100] flex items-center justify-center bg-black/45 p-4" @click.self="$emit('close')">
      <section class="flex max-h-[88vh] w-full max-w-2xl flex-col overflow-hidden rounded-lg border border-[var(--border-main)] bg-[var(--background-menu-white)] shadow-xl">
        <header class="flex items-center border-b border-[var(--border-main)] px-5 py-4">
          <div class="min-w-0 flex-1">
            <h2 class="text-base font-semibold">保存为数据产品</h2>
            <p class="mt-1 text-xs text-[var(--text-tertiary)]">选择本次任务成果，保存后将在数据集左侧长期展示。</p>
          </div>
          <button class="icon-button" title="关闭" aria-label="关闭" @click="$emit('close')"><X class="size-4" /></button>
        </header>
        <div class="min-h-0 flex-1 space-y-4 overflow-y-auto p-5">
          <div v-if="loading" class="flex h-48 items-center justify-center text-sm text-[var(--text-tertiary)]"><LoaderCircle class="mr-2 size-4 animate-spin" />正在整理成果物</div>
          <template v-else>
            <label class="block text-xs font-medium">产品名称<input v-model="name" maxlength="200" class="mt-1.5 w-full rounded-md border border-[var(--border-main)] bg-transparent px-3 py-2 text-sm outline-none focus:border-[#2b7659]" /></label>
            <label class="block text-xs font-medium">产品描述<textarea v-model="description" rows="3" maxlength="4000" class="mt-1.5 w-full resize-y rounded-md border border-[var(--border-main)] bg-transparent px-3 py-2 text-sm outline-none focus:border-[#2b7659]" /></label>
            <dl class="grid grid-cols-2 gap-3 text-xs">
              <div><dt class="text-[var(--text-tertiary)]">生成方式</dt><dd class="mt-1">{{ generationMethodLabel }}</dd></div>
              <div><dt class="text-[var(--text-tertiary)]">来源任务</dt><dd class="mt-1 truncate">{{ sessionId }}</dd></div>
            </dl>
            <div>
              <div class="mb-2 flex items-center justify-between"><h3 class="text-xs font-semibold">产品文件</h3><span class="text-[10px] text-[var(--text-tertiary)]">已选 {{ selectedIds.size }} 项</span></div>
              <div class="overflow-hidden rounded-md border border-[var(--border-main)]">
                <div v-for="group in groupedFiles" :key="group.role" class="border-b border-[var(--border-main)] last:border-b-0">
                  <button type="button" class="flex w-full items-center gap-2 bg-[var(--background-gray-main)] px-3 py-2 text-left text-xs font-medium" @click="toggleGroup(group.role)">
                    <ChevronDown class="size-3.5 transition-transform" :class="expandedRoles.has(group.role) ? '' : '-rotate-90'" />{{ roleLabel(group.role) }}<span class="ml-auto text-[10px] text-[var(--text-tertiary)]">{{ group.files.length }}</span>
                  </button>
                  <div v-if="expandedRoles.has(group.role)">
                    <label v-for="file in group.files" :key="file.file_id" class="flex min-w-0 items-center gap-2 border-t border-[var(--border-main)] px-3 py-2 text-xs">
                      <input type="checkbox" :checked="selectedIds.has(file.file_id)" @change="toggleFile(file.file_id)" />
                      <FileText class="size-3.5 shrink-0 text-[#2b7659]" />
                      <span class="min-w-0 flex-1 truncate" :title="file.relative_path">{{ file.relative_path }}</span>
                      <label class="flex shrink-0 items-center gap-1 text-[10px] text-[var(--text-tertiary)]" title="设为主文件"><input v-model="primaryFileId" type="radio" name="primary-product-file" :value="file.file_id" :disabled="!selectedIds.has(file.file_id)" />主文件</label>
                    </label>
                  </div>
                </div>
                <div v-if="!draftFiles.length" class="px-3 py-8 text-center text-xs text-[var(--text-tertiary)]">本次任务暂无可保存成果物</div>
              </div>
            </div>
          </template>
        </div>
        <footer class="flex justify-end gap-2 border-t border-[var(--border-main)] px-5 py-4">
          <button class="rounded-md px-4 py-2 text-sm hover:bg-[var(--fill-tsp-white-light)]" @click="$emit('close')">取消</button>
          <button class="rounded-md bg-[#2b7659] px-4 py-2 text-sm text-white disabled:cursor-not-allowed disabled:opacity-50" :disabled="loading || saving || !name.trim() || !selectedIds.size" @click="save">
            <LoaderCircle v-if="saving" class="mr-1 inline size-3.5 animate-spin" />保存数据产品
          </button>
        </footer>
      </section>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { ChevronDown, FileText, LoaderCircle, X } from 'lucide-vue-next';
import { createDataProduct, getDataProductDraft } from '@/api/agent';
import type { DataProductFile } from '@/api/dataset';
import { showErrorToast, showSuccessToast } from '@/utils/toast';

const props = defineProps<{ visible: boolean; sessionId: string; datasetId: string }>();
const emit = defineEmits<{ (e: 'close'): void; (e: 'saved'): void }>();
const loading = ref(false); const saving = ref(false); const name = ref(''); const description = ref('');
const generationMethod = ref('agent_tool'); const draftFiles = ref<DataProductFile[]>([]);
const selectedIds = ref(new Set<string>()); const primaryFileId = ref<string | null>(null);
const expandedRoles = ref(new Set(['data', 'chart', 'source', 'report', 'other']));
const groupedFiles = computed(() => ['data', 'chart', 'source', 'report', 'other'].map(role => ({ role, files: draftFiles.value.filter(file => file.role === role) })).filter(group => group.files.length));
const generationMethodLabel = computed(() => generationMethod.value === 'agent_tool' ? 'Agent Tool 生成' : generationMethod.value);
const roleLabel = (role: string) => ({ data: '数据', chart: '图表', source: '源代码', report: '报告', other: '其他附件' }[role] || role);
const toggleGroup = (role: string) => { const next = new Set(expandedRoles.value); next.has(role) ? next.delete(role) : next.add(role); expandedRoles.value = next; };
const toggleFile = (id: string) => { const next = new Set(selectedIds.value); next.has(id) ? next.delete(id) : next.add(id); selectedIds.value = next; if (primaryFileId.value === id && !next.has(id)) primaryFileId.value = null; };

async function load() {
  if (!props.sessionId) return;
  loading.value = true;
  try {
    const draft = await getDataProductDraft(props.sessionId);
    name.value = draft.suggested_name; description.value = draft.suggested_description; generationMethod.value = draft.generation_method; draftFiles.value = draft.files;
    selectedIds.value = new Set(draft.files.map(file => file.file_id));
    primaryFileId.value = draft.files.find(file => file.role === 'data')?.file_id || draft.files[0]?.file_id || null;
  } catch { showErrorToast('无法读取本次任务成果物'); } finally { loading.value = false; }
}
async function save() {
  saving.value = true;
  try {
    await createDataProduct(props.sessionId, props.datasetId, { name: name.value, description: description.value, generation_method: generationMethod.value, selected_file_ids: [...selectedIds.value], primary_file_id: primaryFileId.value });
    showSuccessToast('数据产品已保存'); emit('saved'); emit('close');
  } catch { showErrorToast('保存数据产品失败'); } finally { saving.value = false; }
}
watch(() => props.visible, value => { if (value) void load(); });
</script>
