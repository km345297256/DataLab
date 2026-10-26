<template>
  <section class="analysis-data-layout flex h-full min-h-0 min-w-0 flex-1">
    <div class="analysis-data-body flex min-h-0 min-w-0 flex-1">
      <aside class="analysis-data-sidebar shrink-0" aria-label="分析数据侧栏">
        <div class="workspace-brand mobile-safe-top">
          <RouterLink to="/chat" class="workspace-home-link" aria-label="DataLab，新建分析" title="新建分析" @click.prevent="startNewAnalysis">
            <ManusLogoTextIcon :width="132" :height="30" />
          </RouterLink>
          <RouterLink to="/account" class="workspace-account-link mobile-account-link" aria-label="个人中心" :title="accountName">
            <span class="workspace-avatar" aria-hidden="true">{{ avatarLetter }}</span>
          </RouterLink>
        </div>
        <div class="workspace-library">
          <PersonalDataSidebar
            ref="sidebar"
            :selected-files="selectedFiles"
            :disabled="disabled"
            @toggle-file="toggleFile"
            @preview="preview"
          />
        </div>
        <div class="workspace-account-footer">
          <RouterLink to="/account" class="workspace-account-link" aria-label="个人中心" title="个人中心">
            <span class="workspace-avatar" aria-hidden="true">{{ avatarLetter }}</span>
            <span class="workspace-account-name">{{ accountName }}</span>
            <ChevronRight :size="16" class="workspace-account-chevron" aria-hidden="true" />
          </RouterLink>
        </div>
      </aside>
      <div class="analysis-data-main flex min-h-0 min-w-0 flex-1 flex-col">
        <header class="workspace-header shrink-0 px-3 sm:px-5">
          <div class="workspace-header-content">
            <slot name="header" />
          </div>
          <div class="workspace-history">
            <AnalysisHistoryMenu />
          </div>
        </header>
        <div class="analysis-data-content flex min-h-0 min-w-0 flex-1">
          <slot />
        </div>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue';
import { ChevronRight } from 'lucide-vue-next';
import type { FileInfo } from '../api/file';
import PersonalDataSidebar from './PersonalDataSidebar.vue';
import AnalysisHistoryMenu from './AnalysisHistoryMenu.vue';
import ManusLogoTextIcon from './icons/ManusLogoTextIcon.vue';
import { useFilePanel } from '../composables/useFilePanel';
import { useAuth } from '../composables/useAuth';
import { useNewAnalysis } from '../composables/useNewAnalysis';
import { toggleExplorationFile } from '../utils/explorationFiles';
import { showErrorToast } from '../utils/toast';

const props = withDefaults(defineProps<{ selectedFiles: FileInfo[]; disabled?: boolean }>(), { disabled: false });
const emit = defineEmits<{ (event: 'update:selectedFiles', files: FileInfo[]): void }>();
const sidebar = ref<InstanceType<typeof PersonalDataSidebar>>();
const { showFilePanel } = useFilePanel();
const { currentUser } = useAuth();
const { startNewAnalysis } = useNewAnalysis();
const accountName = computed(() => currentUser.value?.fullname || '个人中心');
const avatarLetter = computed(() => currentUser.value?.fullname?.charAt(0)?.toUpperCase() || 'D');

function toggleFile(file: FileInfo) {
  if (props.disabled) return;
  try { emit('update:selectedFiles', toggleExplorationFile(props.selectedFiles, file)); }
  catch { showErrorToast('本次分析最多使用 32 个文件，请先移除部分文件。'); }
}

function preview(file: FileInfo, relatedFiles: FileInfo[]) {
  showFilePanel(file, relatedFiles);
}

defineExpose({ revealLibrary: () => sidebar.value?.reveal() });
</script>

<style scoped>
.analysis-data-layout { container: analysis-data-layout / inline-size; }
.analysis-data-sidebar { display: flex; flex-direction: column; width: 304px; min-height: 0; overflow: hidden; border-right: 1px solid var(--border-main); background: var(--background-menu-white); }
.workspace-brand { display: flex; align-items: center; min-height: 64px; padding: 12px 16px; flex-shrink: 0; border-bottom: 1px solid var(--border-main); }
.workspace-home-link { display: inline-flex; align-items: center; padding: 2px 4px; border-radius: 6px; }
.workspace-home-link:hover { background: var(--fill-tsp-gray-main); }
.workspace-home-link:focus-visible, .workspace-account-link:focus-visible { outline: 2px solid var(--control-focus-color); outline-offset: 3px; }
.workspace-library { flex: 1; min-height: 0; overflow: hidden; }
.workspace-account-footer { flex: none; border-top: 1px solid var(--border-main); padding: 10px 12px; }
.workspace-account-link { display: flex; align-items: center; gap: 10px; min-width: 0; min-height: 40px; padding: 4px; border-radius: 7px; color: var(--text-primary); }
.workspace-account-link:hover { background: var(--fill-tsp-white-light); }
.workspace-avatar { display: flex; align-items: center; justify-content: center; flex: none; width: 30px; height: 30px; border-radius: 50%; color: white; background: #3b82f6; font-size: 13px; font-weight: 600; }
.workspace-account-name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 13px; }
.workspace-account-chevron { flex: none; margin-right: 4px; color: var(--icon-tertiary); }
.mobile-account-link { display: none; }
.analysis-data-main { background: var(--background-gray-main); }
.workspace-header { min-height: 64px; min-width: 0; display: flex; align-items: center; gap: 12px; }
.workspace-header-content { flex: 1; min-width: 0; }
.workspace-history { flex: none; }
.analysis-data-content { container: analysis-data-content / inline-size; }
@container analysis-data-layout (max-width: 760px) {
  .analysis-data-body { flex-direction: column; }
  .analysis-data-sidebar { width: 100%; max-height: 44%; border-right: 0; border-bottom: 1px solid var(--border-main); }
  .workspace-brand { min-height: 52px; padding: 8px 16px; }
  .workspace-library { flex: initial; overflow: auto; }
  .workspace-account-footer { display: none; }
  .mobile-account-link { display: flex; min-height: 34px; padding: 2px; margin-left: auto; }
  .workspace-header { min-height: 44px; }
}
</style>
