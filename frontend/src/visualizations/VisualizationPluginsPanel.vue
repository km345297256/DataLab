<template>
  <section class="mb-6 rounded-xl border border-[var(--border-main)] bg-[var(--background-menu-white)] p-4 sm:p-5">
    <div class="mb-3 flex flex-wrap items-start justify-between gap-3">
      <div><h2 class="text-lg font-semibold text-[var(--text-primary)]">预览视图</h2><p class="mt-1 text-sm leading-6 text-[var(--text-tertiary)]">开关保存在当前账号。启用后可在匹配文件的预览中切换视图；停用后隐藏该视图。开关不会改变文件访问权限。</p></div>
      <button type="button" class="shrink-0 rounded-lg border border-[var(--border-main)] px-3 py-2 text-sm text-[var(--text-primary)] disabled:opacity-50" :disabled="loading || !!updating" @click="refresh">刷新目录</button>
    </div>
    <p class="mb-4 text-xs leading-5 text-[var(--text-tertiary)]">预览只读。能否打开仍取决于文件内容、格式子类、读取预算和服务状态；支持某种后缀不代表支持该格式的全部功能。</p>
    <div class="mb-4 flex flex-col gap-3">
      <label class="flex min-w-0 items-center gap-2 rounded-lg border border-[var(--border-main)] bg-[var(--background-gray-main)] px-3 py-2">
        <Search class="size-4 shrink-0 text-[var(--icon-secondary)]" aria-hidden="true" />
        <span class="sr-only">搜索预览视图</span>
        <input v-model="searchQuery" type="search" class="h-7 min-w-0 flex-1 bg-transparent text-sm text-[var(--text-primary)] outline-none" placeholder="搜索名称、格式或用途，如 CSV、地图、显微图像" />
      </label>
      <div class="flex flex-wrap items-center gap-3 text-sm text-[var(--text-secondary)]">
        <label class="flex items-center gap-2">视图类型<select v-model="viewKind" class="rounded-lg border border-[var(--border-main)] bg-[var(--background-menu-white)] p-2"><option value="">全部类型</option><option v-for="kind in availableKinds" :key="kind" :value="kind">{{ viewKindLabel(kind) }}</option></select></label>
        <label class="flex items-center gap-2">状态<select v-model="stateFilter" class="rounded-lg border border-[var(--border-main)] bg-[var(--background-menu-white)] p-2"><option value="all">全部状态</option><option value="enabled">已启用</option><option value="disabled">已停用</option></select></label>
        <button v-if="searchQuery || viewKind || stateFilter !== 'all'" type="button" class="rounded px-1 py-2 text-sm underline" @click="resetFilters">清除筛选</button>
      </div>
      <p v-if="catalog" role="status" aria-live="polite" class="text-xs text-[var(--text-tertiary)]">已登记 {{ catalog.plugins.length }} 个视图 · 已启用 {{ enabledCount }} 个 · 当前显示 {{ filteredPlugins.length }} 个</p>
    </div>
    <div v-if="stale" role="status" class="mb-3 rounded-lg border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900">目录暂未更新，当前显示上次确认的设置。请刷新目录后再修改开关。</div>
    <div v-if="error" role="alert" class="mb-3 rounded-lg border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900">{{ error }}</div>
    <p v-if="!catalog && loading" class="py-6 text-center text-sm text-[var(--text-tertiary)]">正在加载插件…</p>
    <div v-if="catalog" class="grid gap-3 lg:grid-cols-2">
      <article v-for="plugin in filteredPlugins" :key="plugin.id" class="min-w-0 rounded-lg border border-[var(--border-main)] bg-[var(--background-gray-main)] p-4" :data-plugin-id="plugin.id">
        <div class="flex items-start justify-between gap-3">
          <div class="min-w-0"><h3 class="text-sm font-semibold text-[var(--text-primary)]">{{ plugin.name }}</h3><p class="mt-1 text-[11px] text-[var(--text-tertiary)]">{{ viewKindLabel(plugin.view_kind) }} · {{ plugin.capabilities.shared ? '支持已授权的共享预览' : '共享页不提供此视图' }}</p></div>
          <button type="button" role="switch" :aria-checked="plugin.enabled" :aria-label="`${plugin.enabled ? '停用' : '启用'} ${plugin.name}`" :disabled="loading || stale || !!updating" class="shrink-0 rounded-md border px-3 py-1.5 text-xs disabled:opacity-50" :class="plugin.enabled ? 'border-emerald-300 bg-emerald-50 text-emerald-800' : 'border-[var(--border-main)] text-[var(--text-secondary)]'" @click="toggle(plugin.id, !plugin.enabled)">{{ updating === plugin.id ? '保存中…' : plugin.enabled ? '已启用 · 停用' : '已停用 · 启用' }}</button>
        </div>
        <p class="mt-2 text-xs leading-5 text-[var(--text-tertiary)]">{{ plugin.description }}</p>
        <div class="mt-3 flex flex-wrap gap-1 text-[11px] text-[var(--text-secondary)]"><span v-for="format in formats(plugin).slice(0, 8)" :key="format" class="rounded border border-[var(--border-main)] px-1.5 py-0.5">{{ format }}</span><span v-if="formats(plugin).length > 8" class="px-1 py-0.5">另 {{ formats(plugin).length - 8 }} 种格式</span></div>
        <p class="mt-3 text-xs leading-5 text-[var(--text-secondary)]">{{ budgetLabel(plugin) }}</p>
        <details class="mt-2 text-xs text-[var(--text-tertiary)]">
          <summary class="cursor-pointer py-1">格式与预览说明</summary>
          <p class="mt-2 break-words leading-5">支持格式：{{ formats(plugin).join('、') }}</p>
          <p class="mt-2 leading-5">{{ plugin.capabilities.operations.map(operationLabel).join(' · ') }}。实际读取器可能采用更小的上限。</p>
          <p class="mt-2 break-words">{{ plugin.id }} · v{{ plugin.version }}</p>
        </details>
        <p v-if="plugin.capabilities.operations.includes('job')" class="mt-2 text-xs text-[var(--text-tertiary)]">启用后不会自动分析；进入预览并明确启动才会运行，可取消。</p>
      </article>
    </div>
    <p v-if="catalog && !filteredPlugins.length" class="py-6 text-sm text-[var(--text-tertiary)]">没有匹配的可视化插件。</p>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { Search } from 'lucide-vue-next';
import { useVisualizationCatalog } from './catalog';
import { viewKindLabel, type VisualizationKind, type VisualizationPlugin } from './contract';
const props = withDefaults(defineProps<{ query?: string }>(), { query: '' });
const { catalog, loading, error, stale, updating, refresh, toggle } = useVisualizationCatalog();
const searchQuery = ref('');
const viewKind = ref<VisualizationKind | ''>('');
const stateFilter = ref<'all' | 'enabled' | 'disabled'>('all');
const availableKinds = computed(() => [...new Set((catalog.value?.plugins ?? []).map(plugin => plugin.view_kind))]);
const enabledCount = computed(() => (catalog.value?.plugins ?? []).filter(plugin => plugin.enabled).length);
const byteLabel = (bytes: number) => bytes < 1024 * 1024 ? `${Math.round(bytes / 1024)} KiB` : `${Math.round(bytes / 1024 / 1024)} MiB`;
const operationLabel = (operation: string) => ({ bytes: '读取后显示', page: '分页浏览', preview: '数据预览', prepare: '预览准备', job: '需明确启动分析' })[operation] || operation;
const formats = (plugin: VisualizationPlugin) => [...plugin.extensions.map(extension => `.${extension}`), ...plugin.filenames];
const budgetLabel = (plugin: VisualizationPlugin) => {
  const size = byteLabel(plugin.limits.max_input_bytes);
  switch (plugin.capabilities.input_mode) {
    case 'page': return `每页最多读取 ${size}；不以整文件大小限制分页浏览。`;
    case 'prefix': return `最多读取开头 ${size} 进行抽样；结果不代表全文件。`;
    case 'window': return `每次选区或时间窗累计读取最多 ${size}；源大小、解码和结果另有限制。`;
    default: return `整文件读取预算 ${size}；解码和结果另有限制。`;
  }
};
const resetFilters = () => { searchQuery.value = ''; viewKind.value = ''; stateFilter.value = 'all'; };
const filteredPlugins = computed(() => {
  const terms = `${props.query} ${searchQuery.value}`.trim().toLowerCase().split(/\s+/).filter(Boolean);
  return (catalog.value?.plugins ?? []).filter((plugin) => {
    if (viewKind.value && plugin.view_kind !== viewKind.value) return false;
    if (stateFilter.value !== 'all' && plugin.enabled !== (stateFilter.value === 'enabled')) return false;
    const text = [plugin.name, plugin.id, plugin.description, viewKindLabel(plugin.view_kind), ...formats(plugin)].join(' ').toLowerCase();
    return terms.every(term => text.includes(term));
  });
});
onMounted(() => { void refresh(); });
</script>
