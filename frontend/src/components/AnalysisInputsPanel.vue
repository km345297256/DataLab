<template>
  <section class="analysis-inputs-panel" :class="compact ? 'analysis-inputs-panel--compact' : 'my-3 overflow-hidden rounded-xl border border-[var(--border-main)] bg-[var(--background-menu-white)]'" aria-label="本次分析资料">
    <Popover v-model:open="expanded">
    <PopoverTrigger v-if="compact" as-child>
      <button type="button" class="analysis-inputs-trigger" :class="{ 'analysis-inputs-trigger--error': error }" :aria-expanded="expanded" :title="error || (running ? '本轮资料已锁定' : '选择下一次分析使用的资料')">
        <FileText class="size-3.5 shrink-0" />
        <span>分析资料</span>
        <span v-if="error" role="status" class="text-[11px]">读取失败</span>
        <span v-else-if="files.length" class="text-[11px] text-[var(--text-tertiary)]">已选 {{ selectedIds?.length || 0 }}/{{ files.length }}</span>
        <ChevronDown class="size-3 shrink-0 transition-transform" :class="expanded ? '' : 'rotate-180'" />
      </button>
    </PopoverTrigger>
    <header v-else class="flex items-center justify-between gap-2 px-3 py-2.5">
      <button type="button" class="flex min-w-0 items-center gap-2 text-sm font-medium" :aria-expanded="expanded" @click="expanded = !expanded">
        <ChevronDown class="size-4 transition-transform" :class="expanded ? '' : '-rotate-90'" />
        本次分析资料<span v-if="files.length" class="text-xs font-normal text-[var(--text-tertiary)]">已选 {{ selectedIds?.length || 0 }} / {{ files.length }}</span>
      </button>
      <span v-if="running" class="text-xs text-[var(--text-tertiary)]">本轮资料已锁定</span>
    </header>
    <component :is="compact ? PopoverContent : 'div'" v-if="expanded" :class="compact ? 'analysis-inputs-content' : 'border-t border-[var(--border-main)] px-3 py-2'" side="top" align="end" :side-offset="8" :collision-padding="12" aria-label="分析资料选择" @open-auto-focus="focusContent">
      <header v-if="compact" class="analysis-inputs-heading">
        <h3>分析资料</h3>
        <span>{{ running ? '本轮资料已锁定' : `已选 ${selectedIds?.length || 0} / ${files.length}` }}</span>
      </header>
      <p v-if="loading" role="status" class="py-2 text-xs text-[var(--text-tertiary)]">正在读取会话资料…</p>
      <p v-else-if="error" role="alert" class="py-2 text-xs text-amber-700">{{ error }} <button type="button" class="underline" @click="$emit('refresh')">重新加载</button></p>
      <template v-else>
        <p v-if="!files.length" class="py-2 text-xs text-[var(--text-tertiary)]">可直接提问，或通过下方输入框上传数据。可通过“保存到我的数据”保留需要复用的资料。</p>
        <template v-else>
          <p class="analysis-inputs-help">勾选下次分析使用的文件，新上传的文件会自动加入。</p>
          <ul class="analysis-inputs-list" :class="compact ? '' : 'max-h-56 overflow-y-auto'">
            <li v-for="file in files" :key="file.file_id" class="analysis-input-file" :class="{ 'is-selected': selectedIds?.includes(file.file_id), 'is-locked': running }">
              <label class="analysis-input-file-selection">
                <input type="checkbox" :checked="selectedIds?.includes(file.file_id)" :disabled="running" :aria-label="`分析资料 ${file.filename} (${file.file_id})`" @change="$emit('toggle', file.file_id)" />
                <FileText class="size-4 shrink-0 text-[var(--control-focus-color)]" />
                <span class="min-w-0 flex-1">
                  <span class="block truncate text-xs font-medium" :title="file.filename">{{ file.filename }}</span>
                  <span class="mt-0.5 block text-[11px] text-[var(--text-tertiary)]">{{ file.size != null ? formatSize(file.size) : '文件大小未知' }}</span>
                </span>
              </label>
              <button type="button" class="analysis-input-preview" :aria-label="`预览 ${file.filename}`" title="预览文件" @click="showFilePanel(file, files)"><Eye class="size-4" /></button>
            </li>
          </ul>
          <p class="analysis-inputs-note">取消勾选不会删除文件，分析成果需手动选入。</p>
        </template>
      </template>
      <button v-if="showTaskData" type="button" class="analysis-inputs-all" @click="expanded = false; $emit('view-all')">查看全部任务数据</button>
    </component>
    </Popover>
  </section>
</template>

<script setup lang="ts">
import { ref } from 'vue';
import { ChevronDown, Eye, FileText } from 'lucide-vue-next';
import type { FileInfo } from '../api/file';
import { useFilePanel } from '../composables/useFilePanel';
import { Popover, PopoverContent, PopoverTrigger } from './ui/popover';

defineProps<{ files: FileInfo[]; selectedIds?: string[]; loading: boolean; error: string; running: boolean; compact?: boolean; showTaskData?: boolean }>();
defineEmits<{ (event: 'toggle', fileId: string): void; (event: 'refresh'): void; (event: 'view-all'): void }>();
const expanded = ref(false);
const { showFilePanel } = useFilePanel();
function focusContent(event: Event) {
  // Announce the panel first; Tab then reaches the file controls in order.
  event.preventDefault();
  if (event.target instanceof HTMLElement) {
    const content = event.target.matches('[data-slot="popover-content"]')
      ? event.target
      : event.target.querySelector<HTMLElement>('[data-slot="popover-content"]');
    content?.focus({ preventScroll: true });
  }
}
function formatSize(size: number) {
  if (size < 1024) return `${size} B`;
  if (size < 1024 ** 2) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / 1024 ** 2).toFixed(1)} MB`;
}
</script>

<style scoped>
.analysis-inputs-panel--compact { flex: none; min-width: 0; }
.analysis-inputs-trigger { display: flex; align-items: center; gap: 5px; min-height: 32px; padding: 5px 8px; border-radius: 7px; font-size: 12px; white-space: nowrap; color: var(--text-secondary); }
.analysis-inputs-trigger:hover, .analysis-inputs-trigger[aria-expanded="true"] { background: var(--fill-tsp-white-dark); }
.analysis-inputs-trigger:focus-visible { outline: 2px solid var(--text-tertiary); outline-offset: 1px; }
.analysis-inputs-trigger--error { color: var(--function-error); }
/* PopoverContent teleports through a wrapper and does not inherit this scope. */
:global([data-slot="popover-content"].analysis-inputs-content) { z-index: 65; width: min(420px, calc(100vw - 24px)); max-height: min(360px, 50dvh, var(--reka-popover-content-available-height)); overflow-y: auto; overscroll-behavior: contain; padding: 16px; border-radius: 12px; color: var(--text-primary); }
.analysis-inputs-heading { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 8px; }
.analysis-inputs-heading h3 { font-size: 14px; font-weight: 600; }
.analysis-inputs-heading > span { flex: none; color: var(--text-tertiary); font-size: 11px; }
.analysis-inputs-help { margin-bottom: 12px; color: var(--text-secondary); font-size: 12px; line-height: 1.7; }
.analysis-inputs-list { margin: 0; padding: 0; list-style: none; }
.analysis-input-file { display: flex; align-items: center; min-width: 0; gap: 6px; margin-top: 4px; padding: 8px; border-radius: 8px; }
.analysis-input-file:first-child { margin-top: 0; }
.analysis-input-file:hover { background: var(--fill-tsp-white-light); }
.analysis-input-file.is-selected { background: var(--control-selected-fill); }
.analysis-input-file-selection { display: flex; flex: 1; align-items: center; min-width: 0; gap: 10px; min-height: 36px; cursor: pointer; }
.analysis-input-file-selection input { flex: none; width: 15px; height: 15px; margin: 0; cursor: inherit; }
.analysis-input-file.is-locked .analysis-input-file-selection { cursor: default; }
.analysis-input-preview { display: flex; flex: none; align-items: center; justify-content: center; width: 30px; height: 30px; border-radius: 6px; color: var(--control-focus-color); }
.analysis-input-preview:hover { background: var(--fill-tsp-white-dark); }
.analysis-inputs-note { margin-top: 12px; padding-top: 10px; border-top: 1px solid var(--border-light); color: var(--text-tertiary); font-size: 11px; line-height: 1.7; }
.analysis-inputs-all { margin-top: 12px; font-size: 12px; color: var(--text-brand); padding: 4px 0; }
</style>
