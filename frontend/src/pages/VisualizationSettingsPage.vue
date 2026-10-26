<template>
  <main class="flex h-full min-w-0 flex-col overflow-hidden bg-[var(--background-gray-main)]">
    <header class="mobile-safe-top flex items-start gap-3 border-b border-[var(--border-main)] px-4 pb-4 sm:px-6 sm:py-5">
      <button v-if="!isLeftPanelShow" type="button" class="flex size-11 shrink-0 items-center justify-center rounded-lg hover:bg-[var(--fill-tsp-gray-main)]" aria-label="打开导航" @click="showLeftPanel"><PanelLeft class="size-5 text-[var(--icon-secondary)]" /></button>
      <div class="min-w-0 flex-1">
        <h1 class="text-2xl font-semibold text-[var(--text-primary)]">可视化设置</h1>
        <p class="mt-1 text-sm leading-6 text-[var(--text-tertiary)]">为当前账号选择文件预览视图，了解支持的格式和读取范围。</p>
      </div>
      <RouterLink to="/account" class="shrink-0 rounded-lg px-2 py-2 text-sm text-[var(--text-secondary)] hover:bg-[var(--fill-tsp-gray-main)]">个人中心</RouterLink>
    </header>
    <div class="min-h-0 flex-1 overflow-y-auto px-4 py-4 sm:px-6 sm:py-5">
      <VisualizationPluginsPanel class="mx-auto max-w-[1080px]" />
    </div>
  </main>
</template>

<script setup lang="ts">
import { PanelLeft } from 'lucide-vue-next';
import { RouterLink } from 'vue-router';
import { useLeftPanel } from '@/composables/useLeftPanel';
import VisualizationPluginsPanel from '@/visualizations/VisualizationPluginsPanel.vue';

const { isLeftPanelShow, showLeftPanel } = useLeftPanel();
</script>
