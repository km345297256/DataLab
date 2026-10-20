<template>
  <Teleport to="body">
    <div v-if="visible && product && file" class="fixed inset-0 z-[1200] flex items-center justify-center bg-black/50 p-4" @click.self="$emit('close')">
      <section class="flex max-h-[88vh] w-full max-w-xl flex-col overflow-hidden rounded-lg border border-[var(--border-main)] bg-[var(--background-menu-white)] shadow-2xl">
        <header class="flex items-center justify-between border-b border-[var(--border-main)] px-5 py-4">
          <div><h2 class="text-base font-semibold">移动产品文件</h2><p class="mt-1 text-xs text-[var(--text-tertiary)]">选择目标目录，底层文件不会被移动或复制。</p></div>
          <button type="button" class="icon-button" title="关闭" aria-label="关闭" @click="$emit('close')"><X class="size-4" /></button>
        </header>
        <div class="min-h-0 flex-1 overflow-y-auto p-5">
          <dl class="grid grid-cols-[88px_minmax(0,1fr)] gap-x-3 gap-y-2 rounded-md bg-[var(--background-gray-main)] p-3 text-xs">
            <dt class="text-[var(--text-tertiary)]">文件名</dt><dd class="truncate font-medium" :title="file.filename">{{ file.filename }}</dd>
            <dt class="text-[var(--text-tertiary)]">当前路径</dt><dd class="break-all">{{ file.relative_path }}</dd>
            <dt class="text-[var(--text-tertiary)]">文件类型</dt><dd>{{ file.content_type || extensionLabel }}</dd>
            <dt class="text-[var(--text-tertiary)]">文件大小</dt><dd>{{ formatFileSize(file.size) }}</dd>
            <dt class="text-[var(--text-tertiary)]">文件角色</dt><dd>{{ roleLabel }}</dd>
            <dt class="text-[var(--text-tertiary)]">记录时间</dt><dd>{{ formatTime(file.created_at || product.created_at) }}</dd>
          </dl>
          <div class="mb-2 mt-5 flex items-center justify-between"><h3 class="text-sm font-medium">选择目标目录</h3><span class="text-xs text-[var(--text-tertiary)]">{{ selectedDirectory || '根目录' }}</span></div>
          <div class="overflow-hidden rounded-md border border-[var(--border-main)]">
            <button type="button" class="flex w-full items-center gap-2 border-b border-[var(--border-main)] px-3 py-2 text-left text-sm hover:bg-[var(--background-gray-main)]" :class="selectedDirectory === '' ? 'bg-[#edf6f1] text-[#216348] dark:bg-[#263a32]' : ''" @click="selectedDirectory = ''"><FolderOpen class="size-4" />根目录<Check v-if="selectedDirectory === ''" class="ml-auto size-4" /></button>
            <button v-for="directory in directoryRows" :key="directory.path" type="button" class="flex w-full items-center gap-2 border-b border-[var(--border-main)] px-3 py-2 text-left text-sm last:border-b-0 hover:bg-[var(--background-gray-main)]" :class="selectedDirectory === directory.path ? 'bg-[#edf6f1] text-[#216348] dark:bg-[#263a32]' : ''" :style="{ paddingLeft: `${12 + directory.depth * 18}px` }" @click="selectedDirectory = directory.path"><Folder class="size-4 shrink-0" /><span class="truncate">{{ directory.name }}</span><Check v-if="selectedDirectory === directory.path" class="ml-auto size-4 shrink-0" /></button>
            <div v-if="!directoryRows.length" class="px-3 py-6 text-center text-xs text-[var(--text-tertiary)]">当前产品还没有子目录</div>
          </div>
        </div>
        <footer class="flex justify-end gap-2 border-t border-[var(--border-main)] px-5 py-4"><button type="button" class="rounded-md px-4 py-2 text-sm hover:bg-[var(--background-gray-main)]" @click="$emit('close')">取消</button><button type="button" class="rounded-md bg-[#2b7659] px-4 py-2 text-sm text-white disabled:opacity-50" :disabled="saving || selectedDirectory === currentDirectory" @click="move">{{ saving ? '正在移动...' : '移动到此目录' }}</button></footer>
      </section>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { Check, Folder, FolderOpen, X } from 'lucide-vue-next';
import type { DataProduct, DataProductFile } from '@/api/dataset';
import { formatFileSize } from '@/utils/fileType';

const props = defineProps<{ visible: boolean; product?: DataProduct; file?: DataProductFile; saving?: boolean }>();
const emit = defineEmits<{ (event: 'close'): void; (event: 'move', directory: string): void }>();
const selectedDirectory = ref('');
const currentDirectory = computed(() => { const path = props.file?.relative_path || ''; const index = path.lastIndexOf('/'); return index < 0 ? '' : path.slice(0, index); });
const directoryRows = computed(() => {
  const paths = new Set(props.product?.directories || []);
  for (const item of props.product?.files || []) { const parts = item.relative_path.split('/').filter(Boolean); for (let i = 1; i < parts.length; i += 1) paths.add(parts.slice(0, i).join('/')); }
  return [...paths].filter(Boolean).sort((a, b) => a.localeCompare(b)).map(path => ({ path, name: path.split('/').pop() || path, depth: path.split('/').length - 1 }));
});
const extensionLabel = computed(() => props.file?.filename.split('.').pop()?.toUpperCase() || '未知');
const roleLabel = computed(() => ({ data: '数据', chart: '图表', source: '源代码', report: '报告', other: '其他' }[props.file?.role || 'other']));
const formatTime = (value?: string | null) => value ? new Intl.DateTimeFormat('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }).format(new Date(value)) : '未记录';
const move = () => emit('move', selectedDirectory.value);
watch(() => props.visible, value => { if (value) selectedDirectory.value = currentDirectory.value; });
</script>
