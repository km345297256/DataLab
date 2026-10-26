<template>
  <section class="library-controls" :class="{ compact }" :aria-label="hideStats ? '数据筛选' : '数据统计与筛选'">
    <dl v-if="!hideStats" class="library-stats" aria-label="全部数据统计">
      <div><dt>文件个数</dt><dd>{{ stats ? stats.file_count.toLocaleString('zh-CN') : '—' }}<small v-if="stats"> 个</small></dd></div>
      <div><dt>文件大小</dt><dd>{{ stats ? formatFileSize(stats.total_bytes) : '—' }}</dd></div>
      <div class="updated-stat"><dt>最近更新</dt><dd :title="updatedLabel">{{ updatedLabel }}</dd></div>
    </dl>
    <div class="filter-toolbar">
      <label class="filename-search control-shell"><Search :size="15" /><input :value="query" type="search" maxlength="200" aria-label="按文件名查找" placeholder="按文件名查找" @input="$emit('update:query', ($event.target as HTMLInputElement).value)" /></label>
      <select :value="format" aria-label="筛选文件格式" @change="$emit('update:format', ($event.target as HTMLSelectElement).value)">
        <option value="">全部格式</option><option v-for="item in formats" :key="item" :value="item">{{ libraryFormatLabel(item) }}</option>
      </select>
      <button class="date-toggle" :class="{ active: from || to }" :aria-expanded="datesVisible" @click="datesVisible = !datesVisible"><CalendarDays :size="14" />{{ from || to ? '时间已筛选' : '更新时间' }}<ChevronDown :size="12" /></button>
      <select v-if="source !== undefined" :value="source" aria-label="筛选数据来源" @change="$emit('update:source', ($event.target as HTMLSelectElement).value as DataLibrarySourceType | '')">
        <option value="">全部来源</option><option v-for="(label, key) in dataLibrarySourceLabels" :key="key" :value="key">{{ label }}</option>
      </select>
      <button v-if="query || format || from || to || source" class="clear-filters" @click="$emit('clear')">清除筛选</button>
    </div>
    <div v-if="datesVisible || from || to" class="date-range">
      <label>开始日期<input type="date" :value="from" :max="to || undefined" aria-label="更新开始日期" @input="$emit('update:from', ($event.target as HTMLInputElement).value)" /></label>
      <span aria-hidden="true">—</span>
      <label>结束日期<input type="date" :value="to" :min="from || undefined" aria-label="更新结束日期" @input="$emit('update:to', ($event.target as HTMLInputElement).value)" /></label>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue';
import { CalendarDays, ChevronDown, Search } from 'lucide-vue-next';
import { dataLibrarySourceLabels, type DataLibraryStats, type DataLibrarySourceType } from '../api/dataLibrary';
import { formatFileSize } from '../utils/fileType';
import { libraryFormatLabel } from '../utils/libraryFileFilters';
const props = defineProps<{ stats: DataLibraryStats | null; query: string; format: string; from: string; to: string; source?: DataLibrarySourceType | ''; compact?: boolean; hideStats?: boolean }>();
defineEmits<{ (event: 'update:query', value: string): void; (event: 'update:format', value: string): void; (event: 'update:from', value: string): void; (event: 'update:to', value: string): void; (event: 'update:source', value: DataLibrarySourceType | ''): void; (event: 'clear'): void }>();
const datesVisible = ref(false);
const formats = computed(() => [...new Set([...(props.stats?.formats ?? []), ...(props.format ? [props.format] : [])])].sort());
const updatedLabel = computed(() => {
  if (!props.stats) return '—';
  const date = new Date(props.stats.updated_at ?? '');
  return Number.isNaN(date.getTime()) ? '暂无更新' : date.toLocaleString('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false });
});
</script>

<style scoped>
.library-controls { --library-accent: #2b7659; min-width: 0; }
.dark .library-controls { --library-accent: #a9cbbb; }
.library-stats { display: grid; grid-template-columns: 1fr 1fr 1.5fr; gap: 16px; margin: 0 0 18px; padding-bottom: 18px; border-bottom: 1px solid var(--border-main); }
.library-stats dt { font-size: 12px; color: var(--text-tertiary); }
.library-stats dd { margin: 6px 0 0; font-size: 18px; font-weight: 600; color: var(--text-primary); overflow-wrap: anywhere; }
.library-stats small { font-size: 12px; font-weight: 400; color: var(--text-tertiary); }
.updated-stat dd { font-size: 14px; font-weight: 500; line-height: 27px; }
.filter-toolbar { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }
.filename-search { display: flex; align-items: center; gap: 8px; flex: 1; min-width: 180px; padding: 8px 10px; border: 1px solid var(--border-main); border-radius: 7px; color: var(--text-tertiary); background: var(--background-menu-white); }
.filename-search input { width: 100%; min-width: 0; font-size: 13px; background: transparent; color: var(--text-primary); outline: none; }
select, .date-toggle { min-width: 0; border: 1px solid var(--border-main); border-radius: 7px; padding: 8px 10px; font-size: 12px; color: var(--text-secondary); background: var(--background-menu-white); }
.date-toggle { display: inline-flex; align-items: center; gap: 6px; }
.date-toggle.active, .clear-filters { color: var(--library-accent); }
.clear-filters { padding: 6px 2px; font-size: 12px; }
.date-range { display: flex; align-items: end; gap: 8px; margin-top: 10px; max-width: 420px; }
.date-range label { min-width: 0; flex: 1; color: var(--text-tertiary); font-size: 11px; }
.date-range input { width: 100%; min-width: 0; margin-top: 4px; border: 1px solid var(--border-main); padding: 6px; border-radius: 5px; color: var(--text-secondary); background: var(--background-menu-white); font-size: 12px; }
.date-range > span { padding-bottom: 8px; color: var(--text-tertiary); }
button:focus-visible { outline: 2px solid var(--library-accent); outline-offset: 2px; }
.compact .library-stats { grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 12px; padding-bottom: 12px; }
.compact .library-stats dt { font-size: 11px; }
.compact .library-stats dd { font-size: 17px; }
.compact .updated-stat { grid-column: 1 / -1; display: flex; align-items: baseline; gap: 12px; }
.compact .updated-stat dd { margin: 0; font-size: 11px; line-height: 1.6; font-weight: 400; }
.compact .filename-search { flex: 1 0 100%; min-width: 0; }
.compact .filter-toolbar select { flex: 1; max-width: 100%; }
.compact .date-toggle { flex: none; }
@media (max-width: 560px) {
  .library-stats { grid-template-columns: 1fr 1fr; gap: 12px; }
  .updated-stat { grid-column: 1 / -1; display: flex; align-items: baseline; gap: 12px; }
  .updated-stat dd { margin: 0; font-size: 12px; }
}
</style>
