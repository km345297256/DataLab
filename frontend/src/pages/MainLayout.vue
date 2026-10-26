<template>
  <div class="app-viewport flex overflow-hidden bg-white">
    <button
      v-if="!isAnalysisWorkspace && isLeftPanelShow"
      type="button"
      class="fixed inset-0 z-40 bg-black/25 backdrop-blur-[1px] sm:hidden"
      aria-label="关闭导航"
      @click="hideLeftPanel"
    />
    <LeftPanel v-if="!isAnalysisWorkspace" />
    <div class="flex-1 min-w-0 h-full py-0 pr-0 relative">
      <div class="flex h-full bg-[var(--background-gray-main)]">
        <div class="flex flex-1 min-w-0 min-h-0">
          <router-view v-slot="{ Component }">
            <component :is="Component" :key="route.name === 'AnalysisTask' ? String(route.params.sessionId) : route.name" />
          </router-view>
          <FilePanel resizable :min-content-width="isAnalysisWorkspace ? 780 : 420" />
          <TaskDataPanel v-if="route.name === 'AnalysisTask'" />
        </div>
      </div>
    </div>
  </div>
  <TakeOverView />
  <CustomDialog />
  <SessionFileList />
  <SettingsDialog />
  <ContextMenu />
</template>

<script setup lang="ts">
import LeftPanel from '@/components/LeftPanel.vue';
import CustomDialog from '@/components/ui/CustomDialog.vue';
import ContextMenu from '@/components/ui/ContextMenu.vue';
import TakeOverView from '@/components/TakeOverView.vue';
import SessionFileList from '@/components/SessionFileList.vue';
import FilePanel from '@/components/FilePanel.vue';
import TaskDataPanel from '@/components/TaskDataPanel.vue';
import SettingsDialog from '@/components/settings/SettingsDialog.vue';
import { computed, onMounted, onUnmounted, watch } from 'vue';
import { useRoute } from 'vue-router';
import { useLeftPanel } from '@/composables/useLeftPanel';
import { useNewAnalysis } from '@/composables/useNewAnalysis';
import { useDataAcquisition } from '@/composables/useDataAcquisition';
import { useAcquisitionPanelInterop } from '@/composables/useAcquisitionPanelInterop';
import { useTaskData } from '@/composables/useTaskData';
import { useTaskDataInterop } from '@/composables/useTaskDataInterop';

const route = useRoute();
const acquisitions = useDataAcquisition();
useAcquisitionPanelInterop();
useTaskDataInterop();
const taskData = useTaskData();
watch(() => route.name === 'AnalysisTask' ? String(route.params.sessionId || '') : undefined,
  sessionId => { void acquisitions.activate(sessionId || undefined); void taskData.activate(sessionId || undefined); }, { immediate: true });
// A completed download may exist even when its automatic library save failed.
// Refresh actual files on materialization, independent of library notifications.
watch(() => acquisitions.jobs.value.flatMap(job => job.files.map(file => file.file_id)).sort().join(','),
  () => { if (taskData.sessionId.value) void taskData.refresh(); });
const { startNewAnalysis } = useNewAnalysis();
const isAnalysisWorkspace = computed(() => route.meta.analysisWorkspace === true);
const { isLeftPanelShow, hideLeftPanel } = useLeftPanel();

const isMobileViewport = () => window.matchMedia('(max-width: 639px)').matches;
const handleKeydown = (event: KeyboardEvent) => {
  if (!event.isComposing && (event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
    event.preventDefault();
    void startNewAnalysis();
  }
};

onMounted(() => {
  if (isMobileViewport()) hideLeftPanel();
  window.addEventListener('keydown', handleKeydown);
});
onUnmounted(() => window.removeEventListener('keydown', handleKeydown));
onUnmounted(() => { void acquisitions.activate(undefined); void taskData.activate(undefined); });

watch(
  () => route.fullPath,
  () => {
    if (isMobileViewport()) hideLeftPanel();
  }
);
</script>
