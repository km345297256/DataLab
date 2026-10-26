<template>
  <DialogRoot :open="visible" @update:open="value => { if (!value) emit('close'); }">
    <DialogPortal>
      <DialogOverlay class="fixed inset-0 z-[1100] bg-black/40" />
      <DialogContent class="fixed left-1/2 top-1/2 z-[1101] flex max-h-[90vh] w-[calc(100%_-_2rem)] max-w-2xl -translate-x-1/2 -translate-y-1/2 flex-col rounded-2xl border border-[var(--border-main)] bg-[var(--background-menu-white)] shadow-xl">
        <header class="border-b border-[var(--border-main)] px-5 py-4">
          <DialogTitle class="pr-8 text-lg font-semibold">使用我的数据</DialogTitle>
          <DialogDescription class="mt-1 text-sm text-[var(--text-tertiary)]">选择已保存的数据，加入本次分析。可以组合使用不同来源的数据。</DialogDescription>
          <button type="button" class="absolute right-4 top-4 rounded-md p-1.5 hover:bg-[var(--fill-tsp-gray-main)]" aria-label="关闭" @click="emit('close')"><X class="size-4" /></button>
        </header>
        <div class="flex gap-2 px-5 pt-4">
          <label class="relative min-w-0 flex-1"><Search class="absolute left-3 top-2.5 size-4 text-[var(--text-tertiary)]" /><input v-model="query" type="search" aria-label="搜索我的数据" placeholder="搜索名称、说明或标签" class="w-full rounded-lg border border-[var(--border-main)] bg-transparent py-2 pl-9 pr-3 text-sm outline-none focus:border-[#2b7659]" /></label>
          <select v-model="sourceType" aria-label="筛选数据来源" class="rounded-lg border border-[var(--border-main)] bg-[var(--background-menu-white)] px-2 text-sm"><option value="">全部来源</option><option v-for="(label, key) in dataLibrarySourceLabels" :key="key" :value="key">{{ label }}</option></select>
        </div>
        <div class="min-h-[180px] flex-1 overflow-y-auto px-5 py-4">
          <div v-if="loading && !items.length" class="flex items-center justify-center gap-2 py-16 text-sm text-[var(--text-tertiary)]"><LoaderCircle class="size-4 animate-spin" />正在读取数据</div>
          <div v-else-if="error" role="alert" class="py-12 text-center text-sm"><p class="text-red-600">{{ error }}</p><button class="mt-3 text-[#2b7659] underline" @click="load()">重试</button></div>
          <div v-else-if="!items.length" class="py-12 text-center text-sm text-[var(--text-tertiary)]">{{ query || sourceType ? '没有找到匹配的数据' : '还没有保存数据。可以先上传文件，或在开放数据检索后保存。' }}</div>
          <div v-else class="space-y-2">
            <label v-for="asset in items" :key="asset.id" class="flex items-start gap-3 rounded-xl border p-3" :class="[selected.has(asset.id) ? 'border-[#2b7659] bg-[#2b7659]/5' : 'border-[var(--border-main)]', availableFiles(asset).length ? 'cursor-pointer' : 'opacity-50']">
              <input type="checkbox" class="mt-1" :checked="selected.has(asset.id)" :disabled="!availableFiles(asset).length" @change="toggleAsset(asset)" />
              <div class="min-w-0 flex-1"><p class="truncate text-sm font-medium">{{ asset.name }}</p><p v-if="asset.description" class="mt-1 line-clamp-2 text-xs text-[var(--text-tertiary)]">{{ asset.description }}</p><p class="mt-1.5 text-xs text-[var(--text-tertiary)]">{{ dataLibrarySourceLabels[asset.source_type] }} · {{ asset.files.length }} 个文件<span v-if="asset.status !== 'ready'"> · 文件不可用</span><span v-else-if="!availableFiles(asset).length"> · 已在本次分析中</span></p></div>
            </label>
          </div>
          <button v-if="nextCursor && !error" :disabled="loading" class="mt-4 w-full rounded-lg py-2 text-sm text-[#2b7659] hover:bg-[#2b7659]/5 disabled:opacity-50" @click="load(true)">{{ loading ? '正在读取…' : '加载更多' }}</button>
        </div>
        <footer class="border-t border-[var(--border-main)] px-5 py-4">
          <p v-if="selectionError" role="alert" class="mb-3 text-sm text-red-600">{{ selectionError }}</p>
          <div class="flex items-center justify-between gap-3"><span class="text-xs text-[var(--text-tertiary)]">已选 {{ selected.size }} 份数据，共 {{ selectedFiles.length }} 个文件</span><button :disabled="!selectedFiles.length" class="rounded-lg bg-[#2b7659] px-4 py-2 text-sm text-white disabled:cursor-not-allowed disabled:opacity-50" @click="confirmSelection">加入本次分析</button></div>
        </footer>
      </DialogContent>
    </DialogPortal>
  </DialogRoot>
</template>

<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue';
import { DialogContent, DialogDescription, DialogOverlay, DialogPortal, DialogRoot, DialogTitle } from 'reka-ui';
import { LoaderCircle, Search, X } from 'lucide-vue-next';
import type { FileInfo } from '../api/file';
import { dataLibrarySourceLabels, type DataLibraryAsset } from '../api/dataLibrary';
import { useDataLibraryList } from '../composables/useDataLibraryList';

const props = withDefaults(defineProps<{ visible: boolean; excludeFileIds?: string[] }>(), { excludeFileIds: () => [] });
const emit = defineEmits<{ (event: 'close'): void; (event: 'select', files: FileInfo[], assets: DataLibraryAsset[]): void }>();
const { items, query, sourceType, nextCursor, loading, error, load, cancel } = useDataLibraryList();
const selected = ref(new Map<string, DataLibraryAsset>());
const selectionError = ref('');
const availableFiles = (asset: DataLibraryAsset) => asset.status === 'ready' ? asset.files.filter(file => !props.excludeFileIds.includes(file.file_id)) : [];
const selectedFiles = computed(() => [...new Map([...selected.value.values()].flatMap(availableFiles).map(file => [file.file_id, file])).values()]);

function toggleAsset(asset: DataLibraryAsset) {
  const next = new Map(selected.value);
  if (next.has(asset.id)) next.delete(asset.id); else next.set(asset.id, asset);
  const fileIds = new Set([...props.excludeFileIds, ...[...next.values()].flatMap(availableFiles).map(file => file.file_id)]);
  if (fileIds.size > 32) { selectionError.value = '本次分析最多使用 32 个文件，请减少选择。'; return; }
  selected.value = next;
  selectionError.value = '';
}

function confirmSelection() {
  if (!selectedFiles.value.length) return;
  emit('select', selectedFiles.value, [...selected.value.values()]);
  emit('close');
}

watch(() => props.visible, async visible => {
  selected.value = new Map();
  selectionError.value = '';
  if (!visible) { cancel(); return; }
  query.value = '';
  sourceType.value = '';
  await nextTick();
  if (props.visible) void load();
}, { immediate: true });
</script>
