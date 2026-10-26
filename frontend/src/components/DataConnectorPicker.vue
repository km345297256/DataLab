<template>
  <Dialog v-model:open="open">
    <DialogContent class="w-[min(600px,calc(100vw-24px))]">
      <DialogHeader><DialogTitle>选择数据连接器</DialogTitle></DialogHeader>
      <div class="space-y-4 px-6 pb-6">
        <DialogDescription class="text-sm leading-6 text-[var(--text-secondary)]">不选择时，系统自动匹配相关来源；也可以指定本次任务的数据来源。</DialogDescription>
        <div v-if="catalog.length > 8" class="control-shell connector-filter">
          <Search class="size-4 shrink-0 text-[var(--text-tertiary)]" aria-hidden="true" />
          <input v-model="query" type="search" placeholder="按名称查找来源" aria-label="按名称查找数据来源" class="min-w-0 flex-1 bg-transparent text-sm outline-none" />
        </div>
        <p v-if="loading" role="status" class="text-sm text-[var(--text-secondary)]">正在加载…</p>
        <p v-else-if="error" role="alert" class="text-sm">{{ error }} <button class="underline" @click="load">重试</button></p>
        <p v-else-if="!catalog.length" class="text-sm">暂无可用的数据连接器。</p>
        <div v-else class="connector-options" role="group" aria-label="可选数据来源">
          <label v-for="item in visibleCatalog" :key="item.id" class="connector-option" :class="{ 'is-selected': selected.includes(item.id), 'is-disabled': disabled }">
            <span class="connector-logo" :class="{ 'connector-logo-dark': connectorLogoNeedsDarkBackground(item.id) }" aria-hidden="true">
              <img v-if="connectorLogo(item.id) && !failedLogos.includes(item.id)" :src="connectorLogo(item.id)" alt="" width="28" height="28" @error="failedLogos.push(item.id)" />
              <Database v-else class="size-6 text-[var(--text-tertiary)]" />
            </span>
            <span class="min-w-0">
              <span class="block text-sm font-medium leading-5">{{ item.title }}</span>
              <span v-if="item.description" class="connector-description" :title="item.description">{{ item.description }}</span>
            </span>
            <input type="checkbox" :aria-label="item.title" :aria-description="item.description" :checked="selected.includes(item.id)" :disabled="disabled" @change="toggle(item.id)" />
          </label>
          <p v-if="!visibleCatalog.length" class="col-span-full py-5 text-center text-sm text-[var(--text-tertiary)]">没有匹配的数据来源。</p>
        </div>
        <div class="flex min-h-5 items-center justify-between gap-3 text-xs text-[var(--text-secondary)]" aria-live="polite">
          <span>{{ selected.length ? `已选择 ${selected.length} 个来源` : '自动匹配相关来源' }}</span>
          <button v-if="selected.length" type="button" :disabled="disabled" class="connector-clear" @click="emit('update:selected', [])">清空选择</button>
        </div>
        <div class="flex items-center justify-between gap-4 border-t border-[var(--border-main)] pt-4">
          <RouterLink to="/connectors" class="text-sm text-[var(--text-secondary)] underline underline-offset-4" @click="open = false">查看数据连接器</RouterLink>
          <button type="button" class="rounded-lg bg-[var(--Button-primary-black)] px-5 py-2 text-sm text-[var(--text-onblack)]" @click="open = false">完成</button>
        </div>
      </div>
    </DialogContent>
  </Dialog>
</template>
<script setup lang="ts">
import { ref, computed, watch } from 'vue';
import { Database, Search } from 'lucide-vue-next';
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { listDataConnectors, type DataConnector } from '@/api/dataConnector';
import { connectorLogo, connectorLogoNeedsDarkBackground } from '@/utils/connectorBranding';
const props = defineProps<{ open: boolean; selected: string[]; disabled?: boolean }>();
const emit = defineEmits<{ (event: 'update:open', value: boolean): void; (event: 'update:selected', value: string[]): void; (event: 'catalog', value: DataConnector[]): void }>();
const open = computed({ get: () => props.open, set: value => emit('update:open', value) });
const catalog = ref<DataConnector[]>([]), error = ref(''), loading = ref(false), query = ref('');
const failedLogos = ref<string[]>([]);
const visibleCatalog = computed(() => {
  const term = query.value.trim().toLocaleLowerCase();
  return catalog.value.filter(item => item.title.toLocaleLowerCase().includes(term));
});
async function load() {
  loading.value = true; error.value = '';
  try { catalog.value = await listDataConnectors(); emit('catalog', catalog.value); }
  catch { error.value = '无法读取连接器目录。'; }
  finally { loading.value = false; }
}
function toggle(id: string) {
  if (props.disabled) return;
  emit('update:selected', props.selected.includes(id) ? props.selected.filter(item => item !== id) : [...props.selected, id]);
}
watch(open, value => { if (value) { query.value = ''; void load(); } }, { immediate: true });
</script>
<style scoped>
.connector-filter { display: flex; align-items: center; gap: 8px; border: 1px solid var(--border-main); border-radius: 8px; padding: 9px 12px; }
.connector-options { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; max-height: min(45vh, 380px); overflow-y: auto; padding: 3px; margin: -3px; }
.connector-option { display: grid; grid-template-columns: 32px minmax(0, 1fr) 16px; align-items: center; gap: 10px; min-height: 64px; padding: 12px; border: 1px solid var(--border-main); border-radius: 10px; background: var(--background-menu-white); cursor: pointer; transition: border-color .15s, background-color .15s; }
.connector-option:hover:not(.is-disabled) { border-color: var(--border-input-active); background: var(--fill-tsp-white-light); }
.connector-option.is-selected { border-color: var(--control-focus-color); background: var(--control-selected-fill); }
.connector-option.is-disabled { cursor: not-allowed; opacity: .6; }
.connector-logo { display: flex; width: 32px; height: 32px; align-items: center; justify-content: center; flex: none; }
.connector-logo img { width: 28px; height: 28px; object-fit: contain; }
.connector-description { display: block; overflow: hidden; margin-top: 3px; color: var(--text-tertiary); font-size: 12px; line-height: 18px; text-overflow: ellipsis; white-space: nowrap; }
.connector-option input { width: 16px; height: 16px; margin: 0; cursor: inherit; }
.connector-clear { color: var(--control-focus-color); text-decoration: underline; text-underline-offset: 3px; }
.connector-clear:disabled { cursor: not-allowed; opacity: .5; }
:global(.dark .connector-logo:has(img)) { background: white; border-radius: 6px; }
.connector-logo-dark, :global(.dark .connector-logo.connector-logo-dark:has(img)) { background: #263b34; border-radius: 6px; }
@media (max-width: 479px) { .connector-options { grid-template-columns: minmax(0, 1fr); } .connector-option { min-height: 58px; } }
</style>
