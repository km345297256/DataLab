<template>
  <AnalysisDataLayout ref="analysisLayout" v-model:selected-files="attachments" :disabled="isSubmitting">
    <template #header>
      <div :inert="isSubmitting || undefined" class="w-full py-2 sm:py-4 bg-[var(--background-gray-main)]">
        <div class="flex items-center w-full min-w-0">
          <div class="h-8 relative z-20 overflow-hidden flex gap-2 items-center min-w-0 flex-1">
            <div class="flex items-center gap-2 min-w-0 flex-1">
              <AgentSelector class="min-w-0 max-w-full" />
            </div>
          </div>
        </div>
      </div>
    </template>
    <SimpleBar>
    <div class="analysis-home-scroll">
      <div class="analysis-home-content">
        <div class="analysis-introduction">
          <h1>你想分析什么研究问题？</h1>
          <p>从一个问题开始，寻找开放数据，上传自己的文件，或继续使用已保存的数据。</p>
        </div>
        <div class="research-entries mb-4 grid w-full gap-2">
          <button type="button" class="research-entry" :disabled="isSubmitting" @click="startDiscovery"><Search :size="17" /><span>查找开放数据</span></button>
          <button type="button" class="research-entry" :disabled="isSubmitting" @click="composer?.uploadFile()"><Upload :size="17" /><span>上传文件</span></button>
          <button type="button" class="research-entry" :disabled="isSubmitting" @click="analysisLayout?.revealLibrary()"><Database :size="17" /><span>使用我的数据</span></button>
        </div>
        <div class="flex flex-col gap-1 w-full">
          <div class="flex flex-col bg-[var(--background-gray-main)] w-full">
            <div class="[&amp;:not(:empty)]:pb-2 bg-[var(--background-gray-main)] rounded-[22px_22px_0px_0px]">
            </div>
            <ChatBox
              ref="composer"
              show-library-actions
              allow-send-files-only
              :show-mcp-actions="false"
              placeholder="描述研究问题，或告诉我想对数据做什么…"
              :rows="2"
              v-model="message"
              v-model:selected-skills="selectedSkills"
              v-model:selected-connectors="selectedConnectors" show-connector-actions
              v-model:data-discovery="dataDiscovery"
              skill-menu-placement="down"
              @submit="handleSubmit"
              :isRunning="false"
              :disabled="isSubmitting"
              :attachments="attachments"
            />
          </div>
        </div>
      </div>
    </div>
    </SimpleBar>
  </AnalysisDataLayout>
</template>

<script setup lang="ts">
import SimpleBar from '../components/SimpleBar.vue';
import AnalysisDataLayout from '../components/AnalysisDataLayout.vue';
import { ref, onMounted, onUnmounted } from 'vue';
import { useRouter } from 'vue-router';
import { useI18n } from 'vue-i18n';
import ChatBox from '../components/ChatBox.vue';
import AgentSelector from '../components/AgentSelector.vue';
import { createSession } from '../api/agent';
import { showErrorToast } from '../utils/toast';
import { Search, Upload, Database } from 'lucide-vue-next';
import type { FileInfo } from '../api/file';
import { useFilePanel } from '../composables/useFilePanel';
import { useAgentProfile } from '../composables/useAgentProfile';
import { savePendingChat } from '../composables/usePendingChat';
import { takePendingLibraryFiles } from '../composables/useDataSelection';
import { readyExplorationFiles } from '../utils/explorationFiles';
import { useAnalysisDraft } from '../composables/useAnalysisDraft';
import { EVENT_START_NEW_ANALYSIS } from '../composables/useNewAnalysis';
import { eventBus } from '../utils/eventBus';

const { t } = useI18n();
const router = useRouter();
const message = ref('');
const isSubmitting = ref(false);
const attachments = ref<FileInfo[]>([]);
const selectedSkills = ref<string[]>([]);
const selectedConnectors = ref<string[]>([]);
const dataDiscovery = ref(false);
let viewDisposed = false;
let submissionVersion = 0;
const analysisDraft = useAnalysisDraft(
  () => 'new',
  () => ({ message: message.value, files: attachments.value, skills: selectedSkills.value, mcpServers: [], connectors: selectedConnectors.value }),
  draft => {
    message.value = draft.message;
    attachments.value = draft.files;
    selectedSkills.value = draft.skills;
    selectedConnectors.value = draft.connectors;
  },
);
const composer = ref<InstanceType<typeof ChatBox>>();
const analysisLayout = ref<InstanceType<typeof AnalysisDataLayout>>();
function startDiscovery() {
  dataDiscovery.value = true;
  message.value = '我想查找开放数据，研究主题是：';
  composer.value?.focus();
}
const { hideFilePanel } = useFilePanel();
const { selectedProfile } = useAgentProfile();

function resetAnalysis() {
  submissionVersion += 1;
  isSubmitting.value = false;
  message.value = '';
  attachments.value = [];
  selectedSkills.value = [];
  selectedConnectors.value = [];
  dataDiscovery.value = false;
  analysisDraft.discard();
  analysisDraft.restore();
  hideFilePanel();
}

onMounted(() => {
  hideFilePanel();
  analysisDraft.restore();
  const pendingFiles = readyExplorationFiles(takePendingLibraryFiles());
  // "Use these data" is an explicit new selection. Ordinary home navigation
  // has no handoff and keeps the unsent draft's existing selection instead.
  if (pendingFiles.length > 32) showErrorToast('本次分析最多使用 32 个文件，请减少选择。');
  else if (pendingFiles.length) attachments.value = pendingFiles;
  eventBus.on(EVENT_START_NEW_ANALYSIS, resetAnalysis);
})
onUnmounted(() => {
  viewDisposed = true;
  eventBus.off(EVENT_START_NEW_ANALYSIS, resetAnalysis);
});

const handleSubmit = async () => {
  const files = readyExplorationFiles(attachments.value);
  if ((message.value.trim() || files.length) && !isSubmitting.value) {
    const pending = {
      message: message.value.trim() || '请先检查这些数据的内容和质量，并建议可开展的分析。',
      skills: [...selectedSkills.value], mcpServers: [],
      dataConnectors: [...selectedConnectors.value],
      ...(dataDiscovery.value ? { dataDiscovery: true } : {}),
      agentProfileId: selectedProfile.value?.id ?? null, files,
    };
    isSubmitting.value = true;
    const version = ++submissionVersion;

    try {
      // Create new Agent
      const session = await createSession(pending.agentProfileId);
      if (viewDisposed || version !== submissionVersion || !analysisDraft.isCurrent()) return;
      const sessionId = session.session_id;
      savePendingChat({ sessionId, ...pending });

      // Navigate to new route with session_id, passing initial message via state
      const navigationFailure = await router.push({
        path: `/chat/${sessionId}`
      });
      if (!navigationFailure) analysisDraft.discard();
      else isSubmitting.value = false;
    } catch (error) {
      if (viewDisposed || version !== submissionVersion || !analysisDraft.isCurrent()) return;
      console.error('Failed to create session:', error);
      showErrorToast(t('Failed to create session, please try again later'));
      isSubmitting.value = false;
    }
  }
};
</script>

<style scoped>
.analysis-home-scroll { display: flex; flex: 1; min-width: 0; width: 100%; padding: 0 24px; }
.analysis-home-content { display: flex; flex-direction: column; width: 100%; max-width: 800px; margin: 0 auto; padding: clamp(48px, 9vh, 88px) 0 32px; }
.analysis-introduction { padding: 0 2px 24px; }
.analysis-introduction h1 { font-size: 27px; line-height: 1.4; font-weight: 600; letter-spacing: -.02em; }
.analysis-introduction p { margin-top: 10px; max-width: 640px; font-size: 13px; line-height: 24px; color: var(--text-secondary); }
.research-entry { display: flex; align-items: center; gap: 9px; min-height: 46px; padding: 10px 14px; border: 1px solid var(--border-main); border-radius: 8px; background: var(--background-menu-white); color: var(--text-secondary); font-size: 13px; text-align: left; transition: background-color 150ms, border-color 150ms; }
.research-entry svg { flex-shrink: 0; color: #2b7659; }
.research-entry:hover { background: var(--fill-tsp-white-light); border-color: #9bbdad; }
.research-entry:focus-visible { outline: 2px solid #2b7659; outline-offset: 2px; }
.research-entries { grid-template-columns: repeat(3, minmax(0, 1fr)); }
:global(.dark) .research-entry svg { color: #a9cbbb; }
@container analysis-data-content (max-width: 560px) {
  .research-entries { grid-template-columns: 1fr; }
  .analysis-home-scroll { padding: 0 16px; }
  .analysis-home-content { padding-top: 28px; }
  .analysis-home-content h1 { font-size: 23px; }
  .analysis-introduction { padding-bottom: 18px; }
}
</style>
