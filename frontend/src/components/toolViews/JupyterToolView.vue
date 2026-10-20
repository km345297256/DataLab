<template>
  <div class="jupyter-view">
    <div v-if="!embedUrl" class="jupyter-empty">JupyterLab 地址不可用</div>
    <iframe
      v-else
      :key="embedUrl"
      class="jupyter-frame"
      :src="embedUrl"
      title="JupyterLab"
      allow="clipboard-read; clipboard-write"
      referrerpolicy="no-referrer"
    />
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue';
import type { ToolContent } from '@/types/message';

const props = defineProps<{
  toolContent: ToolContent;
}>();

const embedUrl = computed(() => {
  const value = props.toolContent?.content?.embed_url;
  if (typeof value !== 'string') return '';
  return value.startsWith('/api/v1/sessions/') && value.includes('/jupyter-proxy/')
    ? value
    : '';
});
</script>

<style scoped>
.jupyter-view,
.jupyter-frame {
  width: 100%;
  height: 100%;
  min-height: 0;
}

.jupyter-view {
  display: flex;
  flex: 1;
  background: #fff;
}

.jupyter-frame {
  border: 0;
}

.jupyter-empty {
  margin: auto;
  color: #6b7280;
  font-size: 14px;
}
</style>
