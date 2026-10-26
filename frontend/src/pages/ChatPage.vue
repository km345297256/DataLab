<template>
  <AnalysisDataLayout v-model:selected-files="attachments" :disabled="isLoading || isSavingDataContext || isRestoringHistory || dataContextFailed">
    <template #header>
      <div ref="observerRef"
        class="flex w-full min-w-0 flex-row items-center justify-between py-2 sm:py-3 gap-3 bg-[var(--background-gray-main)] flex-shrink-0">
        <div class="chat-header-agent hidden min-w-0 max-w-[40%] items-center sm:flex">
          <AgentSelector class="min-w-0" />
        </div>
        <div class="max-w-full sm:max-w-[768px] flex min-w-0 flex-1 flex-col gap-[4px] overflow-hidden">
          <div
            class="text-[var(--text-secondary)] text-sm font-medium w-full flex flex-row items-center justify-between flex-1 min-w-0 gap-2">
            <div class="flex flex-row items-center gap-[6px] flex-1 min-w-0">
              <span class="whitespace-nowrap text-ellipsis overflow-hidden">
                {{ title }}
              </span>
            </div>
            <div class="flex items-center gap-2 flex-shrink-0">
              <button type="button" @click="handleFileListShow" class="task-data-entry"
                aria-label="任务数据" :aria-expanded="taskData.panelOpen.value" aria-controls="task-data-panel">
                <FileSearch :size="17" aria-hidden="true" />
                <span class="hidden sm:inline">任务数据</span>
                <span v-if="taskData.items.value.length" class="task-data-entry-count">{{ taskData.items.value.length }}</span>
              </button>
              <span class="relative flex-shrink-0" aria-expanded="false" aria-haspopup="dialog">
                <Popover>
                  <PopoverTrigger as-child>
                    <button
                      class="h-10 w-10 px-0 sm:h-8 sm:w-auto sm:px-3 rounded-full inline-flex items-center justify-center gap-1 clickable outline outline-1 outline-offset-[-1px] outline-[var(--border-btn-main)] hover:bg-[var(--fill-tsp-white-light)] sm:me-1.5"
                      :aria-label="t('Share')">
                      <ShareIcon color="var(--icon-secondary)" />
                      <span class="hidden text-[var(--text-secondary)] text-sm font-medium sm:inline">{{ t('Share') }}</span>
                    </button>
                  </PopoverTrigger>
                  <PopoverContent>
                    <div
                      class="flex w-full flex-col">
                      <div class="flex flex-col pt-[12px] px-[16px] pb-[16px]">
                        <!-- Private mode option -->
                        <div @click="handleShareModeChange('private')"
                          :class="{'pointer-events-none opacity-50': sharingLoading}"
                          class="flex items-center gap-[10px] px-[8px] -mx-[8px] py-[8px] rounded-[8px] clickable hover:bg-[var(--fill-tsp-white-main)]">
                          <div
                            :class="shareMode === 'private' ? 'bg-[var(--Button-primary-black)]' : 'bg-[var(--fill-tsp-white-dark)]'"
                            class="w-[32px] h-[32px] rounded-[8px] flex items-center justify-center">
                            <Lock :size="16" :stroke="shareMode === 'private' ? 'var(--text-onblack)' : 'var(--icon-primary)'" :stroke-width="2" /></div>
                          <div class="flex flex-col flex-1 min-w-0">
                            <div class="text-sm font-medium text-[var(--text-primary)]">{{ t('Private Only') }}</div>
                            <div class="text-[13px] text-[var(--text-tertiary)]">{{ t('Only visible to you') }}</div>
                          </div><Check :size="20" :class="shareMode === 'private' ? 'ml-auto' : 'ml-auto invisible'" :color="shareMode === 'private' ? 'var(--icon-primary)' : 'var(--icon-tertiary)'" />
                        </div>
                        <!-- Public mode option -->
                        <div @click="handleShareModeChange('public')"
                          :class="{'pointer-events-none opacity-50': sharingLoading}"
                          class="flex items-center gap-[10px] px-[8px] -mx-[8px] py-[8px] rounded-[8px] clickable hover:bg-[var(--fill-tsp-white-main)]">
                          <div
                            :class="shareMode === 'public' ? 'bg-[var(--Button-primary-black)]' : 'bg-[var(--fill-tsp-white-dark)]'"
                            class="w-[32px] h-[32px] rounded-[8px] flex items-center justify-center">
                            <Globe :size="16" :stroke="shareMode === 'public' ? 'var(--text-onblack)' : 'var(--icon-primary)'" :stroke-width="2" /></div>
                          <div class="flex flex-col flex-1 min-w-0">
                            <div class="text-sm font-medium text-[var(--text-primary)]">{{ t('Public Access') }}</div>
                            <div class="text-[13px] text-[var(--text-tertiary)]">{{ t('Anyone with the link can view') }}</div>
                          </div><Check :size="20" :class="shareMode === 'public' ? 'ml-auto' : 'ml-auto invisible'" :color="shareMode === 'public' ? 'var(--icon-primary)' : 'var(--icon-tertiary)'" />
                        </div>
                        <div class="border-t border-[var(--border-main)] mt-[4px]"></div>
                        
                        <!-- Show instant share button when in private mode -->
                        <div v-if="shareMode === 'private'">
                          <button @click.stop="handleInstantShare"
                            :disabled="sharingLoading"
                            class="inline-flex items-center justify-center whitespace-nowrap font-medium transition-colors hover:opacity-90 active:opacity-80 bg-[var(--Button-primary-black)] text-[var(--text-onblack)] h-[36px] px-[12px] rounded-[10px] gap-[6px] text-sm min-w-16 mt-[16px] w-full disabled:opacity-50 disabled:cursor-not-allowed"
                            data-tabindex="" tabindex="-1">
                            <div v-if="sharingLoading" class="w-4 h-4 border-2 border-white border-t-transparent rounded-full animate-spin"></div>
                            <Link v-else :size="16" stroke="currentColor" :stroke-width="2" />
                            {{ sharingLoading ? t('Sharing...') : t('Share Instantly') }}
                          </button>
                        </div>
                        
                        <!-- Show copy link button when in public mode -->
                        <div v-else>
                          <button @click.stop="handleCopyLink"
                            :class="linkCopied ? 'inline-flex items-center justify-center whitespace-nowrap font-medium transition-colors active:opacity-80 bg-[var(--Button-primary-white)] text-[var(--text-primary)] hover:opacity-70 active:hover-60 h-[36px] px-[12px] rounded-[10px] gap-[6px] text-sm min-w-16 mt-[16px] w-full border border-[var(--border-btn-main)] shadow-none' : 'inline-flex items-center justify-center whitespace-nowrap font-medium transition-colors hover:opacity-90 active:opacity-80 bg-[var(--Button-primary-black)] text-[var(--text-onblack)] h-[36px] px-[12px] rounded-[10px] gap-[6px] text-sm min-w-16 mt-[16px] w-full'"
                            data-tabindex="" tabindex="-1">
                            <Link v-if="!linkCopied" :size="16" stroke="currentColor" :stroke-width="2" />
                            <Check v-else :size="16" color="var(--text-primary)" />
                            {{ linkCopied ? t('Link Copied') : t('Copy Link') }}
                          </button>
                        </div>
                      </div>
                    </div>
                  </PopoverContent>
                </Popover>
              </span>
            </div>
          </div>
          <div class="w-full flex justify-between items-center">
          </div>
        </div>
      </div>
    </template>
  <div class="analysis-chat-layout">
  <SimpleBar ref="simpleBarRef" class="analysis-message-scroll min-h-0" @scroll="handleScroll">
    <div ref="chatContainerRef" class="analysis-chat-timeline relative flex min-h-full flex-1 flex-col min-w-0 px-4 sm:px-6">
      <div class="mx-auto w-full max-w-[800px] flex flex-col flex-1">
        <div class="analysis-conversation analysis-chat-messages flex flex-col w-full gap-3 pb-8 pt-5 flex-1 sm:pt-7">
          <button v-if="hasMoreHistory" type="button" :disabled="isLoadingHistory" class="mx-auto rounded-lg px-4 py-2 text-sm text-[var(--text-secondary)] disabled:opacity-50" @click="loadEarlierHistory">
            {{ isLoadingHistory ? '正在加载历史记录…' : '加载更早的对话' }}
          </button>
          <OpenDataChat ref="openDataChat" :session-id="sessionId" :messages="messages" :message-entries="presentationEntries" @count="openDataCount = $event" v-slot="{ entries }">
          <AnalysisConversation :entries="entries" :messages="messages" :session-id="sessionId" :is-loading="isLoading"
            :completion-advice="completionAdvice" :message-key="messageKey" :can-resume-analysis="canResumeAnalysis"
            :before-summary-toggle="analysis.beforeSummaryToggle" @resume-analysis="resumeAnalysis"
            :pending-clarification-id="pendingClarification?.id" :waiting-for-user="waitingForUser"
            :can-answer-clarification="!isSavingDataContext && !isRestoringHistory && !dataContextFailed && !canRetryInput"
            :allow-follow-up="!isSavingDataContext && !isRestoringHistory && !dataContextFailed"
            @answer-clarification="handleClarificationAnswer"
            @tool-click="handleToolClick" @jupyter-opened="handleJupyterOpened" @follow-up="handleFollowUpClick">
            <template #search="{ search }"><OpenDataResults :search-id="search.id" :session-id="sessionId" :initial-search="search" /></template>
          </AnalysisConversation>
          </OpenDataChat>

          <!-- Loading indicator -->
          <LoadingIndicator v-if="isLoading && !isRestoringHistory && !connectionNotice && !hasRunningStep" role="status" aria-live="polite" :text="analysisProgress || $t('Thinking')" />
          <div v-if="!messages.length && !openDataCount && !isLoading && !isRestoringHistory && !connectionNotice" class="flex flex-1 flex-col items-center justify-center px-6 py-12 text-center">
            <h2 class="text-lg font-medium text-[var(--text-primary)]">尚未开始对话</h2>
            <p class="mt-2 max-w-md text-sm leading-6 text-[var(--text-secondary)]">从左侧“我的数据”选择文件，或描述你需要查找和分析的问题。</p>
          </div>
        </div>

      </div>
    </div>
  </SimpleBar>
        <div class="analysis-chat-footer mobile-safe-bottom">
        <div ref="composerColumn" class="analysis-chat-input">
          <button @click="handleFollow" v-if="!follow"
            aria-label="回到最新消息" class="flex items-center justify-center w-[36px] h-[36px] rounded-full bg-[var(--background-white-main)] hover:bg-[var(--background-gray-main)] clickable border border-[var(--border-main)] shadow-[0px_5px_16px_0px_var(--shadow-S),0px_0px_1.25px_0px_var(--shadow-S)] absolute -top-12 left-1/2 -translate-x-1/2">
            <ArrowDown class="text-[var(--icon-primary)]" :size="20" />
          </button>
          <div v-if="connectionNotice" role="status" aria-live="polite" class="mb-2 rounded-xl border border-[var(--border-main)] bg-[var(--background-menu-white)] px-4 py-3 text-sm">
            <p class="font-medium text-[var(--text-primary)]">{{ isLoading ? '正在恢复连接' : '连接状态' }}</p>
            <p class="mt-1 leading-5 text-[var(--text-secondary)]">{{ connectionNotice }}</p>
            <button v-if="isLoading" type="button" class="mt-2 underline" @click="stopReconnecting">停止重连</button>
            <button v-else type="button" class="mt-2 underline disabled:opacity-50" :disabled="isRestoringHistory" @click="refreshConnectionState">{{ canRetryInput ? '恢复原提交或停止后的结果' : '重新获取状态' }}</button>
          </div>
          <div v-if="dataContextFailed" role="alert" class="mb-2 rounded-xl border border-amber-400 px-4 py-3 text-sm">
            <p>本轮数据选择未能完整恢复。请清空选择后重新添加数据，避免遗漏输入。</p>
            <button type="button" class="mt-2 underline" @click="resetDataSelection">清空并重新选择数据</button>
          </div>
          <p v-if="isRestoringHistory" role="status" class="mb-2 px-4 text-sm text-[var(--text-secondary)]">正在读取会话状态…</p>
          <p v-else-if="isSavingDataContext" role="status" class="mb-2 px-4 text-sm text-[var(--text-secondary)]">正在确认本次分析数据…</p>
          <div v-if="availableInputFiles.length || inputError || (sessionId && (messages.length || isLoading) && !connectionNotice)" class="analysis-chat-status">
            <PlanPanel compact :popover-width="composerColumnWidth" :messages="messages" v-if="sessionId && (messages.length || isLoading) && !connectionNotice" :key="sessionId" :plan="plan || undefined" :session-id="sessionId" :revision="lastEventSeq" :is-running="isLoading" />
            <AnalysisInputsPanel compact show-task-data @view-all="handleFileListShow" v-if="availableInputFiles.length || inputError" :files="availableInputFiles" :selected-ids="currentInputIds" :loading="inputLoading" :error="inputError" :running="isLoading || isRestoringHistory" @toggle="toggleAnalysisInput" @refresh="refreshAnalysisInputs" />
          </div>
          <div v-if="visualizationContexts.length" class="mb-2 rounded-lg border border-[var(--border-main)] p-2 text-xs" aria-label="本轮可视化范围">
            <p class="mb-1 text-[var(--text-secondary)]">本轮分析范围 · 发送时校验原始文件版本</p>
            <div v-for="(context, index) in visualizationContexts" :key="`${context.fileId}:${context.pluginId}`" class="flex items-center gap-2 py-1">
              <span class="min-w-0 flex-1 break-words">{{ context.view.title || attachments.find(file => visualizationFileId(file.file_id) === visualizationFileId(context.fileId))?.filename || '数据窗口' }} · {{ context.selection ? `X ${context.selection.x.join(' ～ ')}${context.selection.y ? ' / Y ' + context.selection.y.join(' ～ ') : ''}` : '当前变量 / 窗口' }}</span>
              <button type="button" class="shrink-0 underline disabled:opacity-50" :disabled="taskDataLocked" @click="visualizationContexts.splice(index, 1)">移除范围</button>
            </div>
          </div>
          <ChatBox ref="composer" class="!pb-0" compact-composer collapse-context :show-mcp-actions="false" :placeholder="waitingForUser ? '请回答上方问题，或补充你的要求…' : '输入分析问题，或针对已选资料继续提问…'" v-model="inputMessage" v-model:selected-skills="selectedSkills" v-model:selected-mcp-servers="selectedMcpServers" v-model:selected-connectors="selectedConnectors" v-model:data-discovery="dataDiscovery" :discovery-target="discoveryParent?.query" show-connector-actions show-library-actions allow-send-files-only :session-id="sessionId" :rows="1" @submit="handleSubmit" :isRunning="isLoading" @stop="handleStop"
            :disabled="isSavingDataContext || isRestoringHistory || dataContextFailed" :inert="isSavingDataContext || undefined" :attachments="attachments" />
          <p class="mt-1.5 text-center text-[10px] text-[var(--text-tertiary)]">DataLab 也可能会犯错。请核查重要信息。</p>
        </div>
        </div>
  </div>
    <VersionBadge class="analysis-version" />
    <ToolPanel ref="toolPanel" :size="toolPanelSize" :sessionId="sessionId" :realTime="realTime"
      :isShare="false"
      @jumpToRealTime="jumpToRealTime" />
  </AnalysisDataLayout>
</template>

<script setup lang="ts">
import SimpleBar from '../components/SimpleBar.vue';
import AnalysisDataLayout from '../components/AnalysisDataLayout.vue';
import { ref, computed, onMounted, watch, nextTick, onUnmounted } from 'vue';
import { useElementSize } from '@vueuse/core';
import { useRouter } from 'vue-router';
import { useI18n } from 'vue-i18n';
import ChatBox from '../components/ChatBox.vue';
import AnalysisConversation from '../components/AnalysisConversation.vue';
import OpenDataChat from '../components/OpenDataChat.vue';
import OpenDataResults from '../components/OpenDataResults.vue';
import * as agentApi from '../api/agent';
import { ToolContent } from '../types/message';
import VersionBadge from '../components/VersionBadge.vue';
import ToolPanel from '../components/ToolPanel.vue'
import PlanPanel from '../components/PlanPanel.vue';
import { ArrowDown, FileSearch, Lock, Globe, Link, Check } from 'lucide-vue-next';
import AgentSelector from '../components/AgentSelector.vue';
import ShareIcon from '@/components/icons/ShareIcon.vue';
import { showErrorToast, showInfoToast, showSuccessToast } from '../utils/toast';
import type { FileInfo } from '../api/file';
import { useTaskData } from '../composables/useTaskData'
import { useFilePanel } from '../composables/useFilePanel'
import { consumePendingChat, type PendingChat } from '../composables/usePendingChat'
import { readyExplorationFiles, mergeExplorationFiles } from '../utils/explorationFiles';
import { useAgentProfile } from '../composables/useAgentProfile'
import { copyToClipboard } from '../utils/dom'
import { findCurrentTurnRunningStep } from '../utils/chatTimeline';
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover';
import LoadingIndicator from '@/components/ui/LoadingIndicator.vue';
import { eventBus } from '../utils/eventBus';
import { EVENT_REFRESH_SESSION_LIST, EVENT_SESSION_RENAMED } from '../constants/event';
import { useAnalysisDraft } from '../composables/useAnalysisDraft';

import { useAnalysisSession } from '../composables/useAnalysisSession';
import type { ClarificationAnswer } from '../types/clarification';
import { parseVisualizationViewContext, type VisualizationViewContext } from '../visualizations/viewContext';
import { uploadAnalysisPrompt } from '../utils/analysisInputs';
import { ConversationPresentationIndex } from '../utils/analysisPresentation';
import AnalysisInputsPanel from '../components/AnalysisInputsPanel.vue';
const router = useRouter();
const composerColumn = ref<HTMLElement | null>(null);
const { width: composerColumnWidth } = useElementSize(composerColumn);
const { t } = useI18n();
const taskData = useTaskData();
const { hideFilePanel } = useFilePanel();
const { selectedProfileId } = useAgentProfile();
const openDataChat = ref<InstanceType<typeof OpenDataChat>>();
const composer = ref<InstanceType<typeof ChatBox>>();
const toolPanel = ref<InstanceType<typeof ToolPanel>>();
const simpleBarRef = ref<InstanceType<typeof SimpleBar>>();
const observerRef = ref<HTMLDivElement>();
const chatContainerRef = ref<HTMLDivElement>();
const openDataCount = ref(0), dataDiscovery = ref(false);
const discoveryParent = ref<{ searchId: string; query: string }>();
const inputMessage = ref(''), attachments = ref<FileInfo[]>([]), selectedConnectors = ref<string[]>([]);
const visualizationContexts = ref<VisualizationViewContext[]>([]);
const visualizationFileId = (id: string) => id.replace(/^gridfs:/, '');
watch(() => attachments.value.map(file => file.file_id), ids => {
  visualizationContexts.value = visualizationContexts.value.filter(context => ids.some(id => visualizationFileId(id) === visualizationFileId(context.fileId)));
}, { flush: 'sync' });
const title = ref(t('New Chat')), titleManuallySet = ref(false);
const realTime = ref(true), toolPanelSize = ref(0);
const shareMode = ref<'private' | 'public'>('private'), linkCopied = ref(false), sharingLoading = ref(false);
const isSavingDataContext = ref(false), dataContextFailed = ref(false);
const inputFiles = ref<FileInfo[]>([]), inputLoading = ref(false), inputError = ref('');
let viewDisposed = false, viewGeneration = 0, inputGeneration = 0;
let inputRequest: AbortController | undefined;
watch(dataDiscovery, value => { if (!value) discoveryParent.value = undefined; });
const analysis = useAnalysisSession({
  api: agentApi,
  getViewport: () => simpleBarRef.value?.getScrollElement(),
  onNewTurn: () => { toolPanel.value?.hideToolPanel(); realTime.value = false; },
  onTitle: value => { if (!titleManuallySet.value) title.value = value; },
  onTerminal: () => {
    eventBus.emit(EVENT_REFRESH_SESSION_LIST);
    if (!analysis.isRestoringHistory.value) void refreshAnalysisInputs();
    void taskData.refresh();
  },
  onHistoryError: () => showErrorToast('历史记录加载失败，请重试。'),
  onInputRejected: request => {
    // Restore the rejected submission only when the user has not written a newer draft.
    if (!inputMessage.value.trim()) {
      inputMessage.value = request.message;
      attachments.value = request.files ?? [];
      visualizationContexts.value = request.visualizationContexts ?? [];
      selectedSkills.value = request.skills ?? [];
      selectedMcpServers.value = request.mcpServers ?? [];
      selectedConnectors.value = request.dataConnectors ?? [];
    }
  },
});
const { sessionId, messages, isLoading, waitingForUser, pendingClarification, connectionNotice, analysisProgress, hasMoreHistory,
  isLoadingHistory, isRestoringHistory, timelineRevision, follow, plan, lastNoMessageTool,
  lastEventSeq, selectedSkills, selectedMcpServers, completionAdvice, messageKey,
  loadEarlierHistory, canRetryInput, retryPendingInput, stopReconnecting } = analysis;
const analysisDraft = useAnalysisDraft(
  () => sessionId.value ? `session:${sessionId.value}` : undefined,
  () => ({ message: inputMessage.value, files: attachments.value, skills: selectedSkills.value,
    mcpServers: selectedMcpServers.value, connectors: selectedConnectors.value, visualizationContexts: visualizationContexts.value }),
  draft => {
    inputMessage.value = draft.message; attachments.value = draft.files;
    selectedSkills.value = draft.skills; selectedMcpServers.value = draft.mcpServers;
    selectedConnectors.value = draft.connectors;
    visualizationContexts.value = draft.visualizationContexts ?? [];
  },
);
const presentation = new ConversationPresentationIndex();
const presentationEntries = computed(() => { timelineRevision.value; return presentation.update(messages.value); });
const hasRunningStep = computed(() => Boolean(findCurrentTurnRunningStep(messages.value)));
const shownInputFiles = computed(() => isLoading.value ? analysis.activeInputFiles.value : attachments.value);
const currentInputIds = computed(() => shownInputFiles.value.map(file => file.file_id));
const availableInputFiles = computed(() => isLoading.value ? analysis.activeInputFiles.value : mergeExplorationFiles(inputFiles.value, attachments.value));
const taskDataLocked = computed(() => isLoading.value || isRestoringHistory.value || isSavingDataContext.value || inputLoading.value || dataContextFailed.value);
watch([sessionId, currentInputIds, taskDataLocked, shownInputFiles], ([id, ids, locked, files]) => {
  if (id) taskData.syncSelection(id, ids, locked, files);
}, { immediate: true });
function handleTaskDataInputChange(payload: unknown) {
  const change = payload as { sessionId?: string; files?: FileInfo[]; selected?: boolean };
  if (!change || !change.sessionId || !isCurrentSession(change.sessionId) || taskDataLocked.value) return;
  const files = readyExplorationFiles(change.files ?? []);
  if (change.selected) {
    const merged = mergeExplorationFiles(attachments.value, files);
    if (merged.length > 32) { showErrorToast('本轮最多使用 32 个文件，请先移除部分文件。'); return; }
    inputFiles.value = mergeExplorationFiles(inputFiles.value, files);
    attachments.value = merged;
  } else {
    const ids = new Set(files.map(file => file.file_id));
    attachments.value = attachments.value.filter(file => !ids.has(file.file_id));
  }
}
function handleVisualizationDraft(payload: unknown) {
  const draft = payload as { sessionId?: string; file?: FileInfo; context?: unknown; onAccepted?: (accepted: boolean) => void };
  if (!draft || !draft.sessionId || !isCurrentSession(draft.sessionId) || taskDataLocked.value) { draft?.onAccepted?.(false); return; }
  try {
    const context = parseVisualizationViewContext(draft.context);
    const item = taskData.items.value.find(item => item.available && visualizationFileId(item.file.file_id) === visualizationFileId(context.fileId));
    if (!item || draft.file?.file_id !== context.fileId) throw new Error('请从当前任务数据重新打开文件。');
    const contexts = visualizationContexts.value.filter(item => item.fileId !== context.fileId || item.pluginId !== context.pluginId);
    if (contexts.length >= 4) throw new Error('每轮最多分析 4 个可视化范围，请先移除部分范围。');
    const files = mergeExplorationFiles(attachments.value, [item.file]);
    if (files.length > 32) throw new Error('本轮最多使用 32 个文件，请先移除部分资料。');
    attachments.value = files;
    visualizationContexts.value = [...contexts, context];
    inputFiles.value = mergeExplorationFiles(inputFiles.value, [item.file]);
    if (!inputMessage.value.trim()) inputMessage.value = '请分析所选可视化范围，说明主要特征、异常及数据限制。';
    dataDiscovery.value = false;
    composer.value?.focus();
    draft.onAccepted?.(true);
  } catch (reason) {
    showErrorToast(reason instanceof Error ? reason.message : '无法加入可视化范围。');
    draft.onAccepted?.(false);
  }
}
function toggleAnalysisInput(id: string) {
  if (isLoading.value || isRestoringHistory.value || inputLoading.value) return;
  const existing = attachments.value.some(file => file.file_id === id);
  if (existing) attachments.value = attachments.value.filter(file => file.file_id !== id);
  else {
    const file = inputFiles.value.find(item => item.file_id === id);
    if (file) {
      const next = mergeExplorationFiles(attachments.value, [file]);
      if (next.length > 32) { showErrorToast('本轮最多使用 32 个文件，请先移除部分文件。'); return; }
      attachments.value = next;
    }
  }
}
async function refreshAnalysisInputs() {
  const id = sessionId.value;
  if (!id || viewDisposed) return;
  const version = ++inputGeneration;
  inputRequest?.abort();
  const request = new AbortController(); inputRequest = request;
  inputLoading.value = true;
  try {
    const result = await agentApi.getSessionAnalysisInputs(id, request.signal);
    if (viewDisposed || request.signal.aborted || version !== inputGeneration || id !== sessionId.value) return;
    inputFiles.value = result.files;
    inputError.value = '';
    return result;
  } catch {
    if (!viewDisposed && !request.signal.aborted && version === inputGeneration) inputError.value = '暂时无法确认会话资料，请重新加载。';
  } finally { if (version === inputGeneration) inputLoading.value = false; }
}
const isCurrentSession = (id: string) => !viewDisposed && analysisDraft.isCurrent() && id === sessionId.value;
const handleSessionRenamed = (payload: unknown) => {
  const renamed = payload as { sessionId?: string; title?: string };
  if (!renamed.sessionId || !renamed.title) return;
  if (renamed.sessionId === sessionId.value) {
    title.value = renamed.title;
    titleManuallySet.value = true;
  }
}

const handleSubmit = () => {
  if (isSavingDataContext.value || isRestoringHistory.value || dataContextFailed.value) return;
  if (dataDiscovery.value && !waitingForUser.value) { void submitDiscovery(); return; }
  chat(inputMessage.value, attachments.value, selectedSkills.value, selectedMcpServers.value, selectedProfileId.value);
}

async function submitDiscovery() {
  const query = inputMessage.value.trim(), activeSessionId = sessionId.value;
  const parentId = discoveryParent.value?.searchId;
  const sourceIds = [...selectedConnectors.value];
  if (!query || !activeSessionId) return;
  await nextTick();
  if (!isCurrentSession(activeSessionId)) return;
  if (sourceIds.includes('ncbi-datasets-v2')) {
    dataDiscovery.value = false;
    showInfoToast('按所选连接器查找：NCBI Datasets 将使用基因组检索工具。');
    await chat(query, attachments.value, selectedSkills.value, selectedMcpServers.value, selectedProfileId.value);
    return;
  }
  const accepted = await openDataChat.value?.start(query, parentId, sourceIds);
  if (!accepted || !isCurrentSession(activeSessionId)) return;
  if (inputMessage.value.trim() === query && discoveryParent.value?.searchId === parentId) {
    inputMessage.value = '';
    dataDiscovery.value = false;
  }
  eventBus.emit(EVENT_REFRESH_SESSION_LIST);
}

function handleOpenDataRefine(payload: unknown) {
  const target = payload as { sessionId: string; searchId: string; query: string };
  if (target.sessionId !== sessionId.value || viewDisposed) return;
  dataDiscovery.value = true;
  discoveryParent.value = { searchId: target.searchId, query: target.query };
  if (!inputMessage.value.trim()) inputMessage.value = target.query;
  composer.value?.focus();
}

const handleFollowUpClick = (question: string) => {
  const message = question.trim();
  if (!message || isLoading.value || isSavingDataContext.value || isRestoringHistory.value || dataContextFailed.value) return;
  chat(message, attachments.value, selectedSkills.value, selectedMcpServers.value, selectedProfileId.value);
}

const handleClarificationAnswer = (answer: ClarificationAnswer) => {
  if (!waitingForUser.value || pendingClarification.value?.id !== answer.clarificationId
    || isLoading.value || isSavingDataContext.value || isRestoringHistory.value || dataContextFailed.value || canRetryInput.value) return;
  void chat(answer.message, attachments.value, selectedSkills.value, selectedMcpServers.value, selectedProfileId.value, answer.clarificationId);
};

const canResumeAnalysis = (index: number) => !isSavingDataContext.value && !dataContextFailed.value && analysis.canResumeAnalysis(index);
const resumeAnalysis = (index: number) => { if (canResumeAnalysis(index)) void analysis.resumeAnalysis(index); };

const chat = async (message = '', files: FileInfo[] = [], skills: string[] = [], mcpServers: string[] = [], agentProfileId: string | null = selectedProfileId.value, clarificationId?: string) => {
  if (!sessionId.value || !isCurrentSession(sessionId.value) || isSavingDataContext.value || dataContextFailed.value) return;
  const id = sessionId.value;
  files = readyExplorationFiles(files);
  const request = { message: uploadAnalysisPrompt(message, files), files, skills: [...skills],
    mcpServers: [...mcpServers], dataConnectors: [...selectedConnectors.value], agentProfileId,
    inputFileIds: files.map(file => file.file_id),
    clarificationId,
    visualizationContexts: visualizationContexts.value.filter(context => files.some(file => visualizationFileId(file.file_id) === visualizationFileId(context.fileId))) };
  const accepted = await analysis.send(request);
  if (!accepted || !isCurrentSession(id)) return;
  if (inputMessage.value.trim() === message.trim()) inputMessage.value = '';
  visualizationContexts.value = visualizationContexts.value.filter(context => !request.visualizationContexts.includes(context));
  analysisDraft.accept({ message: '', files: request.files, skills: request.skills,
    mcpServers: request.mcpServers, connectors: request.dataConnectors });
};
const refreshConnectionState = async () => {
  if (canRetryInput.value) { await retryPendingInput(); return; }
  analysisDraft.save();
  await restoreSession();
};
const restoreSession = async () => {
  const id = sessionId.value;
  if (!id || viewDisposed) return;
  const generation = ++viewGeneration;
  isSavingDataContext.value = true;
  try {
    const session = await analysis.restore(id);
    if (!session || generation !== viewGeneration || !isCurrentSession(id)) return;
    const inputs = await refreshAnalysisInputs();
    if (generation !== viewGeneration || !isCurrentSession(id)) return;
    dataContextFailed.value = !inputs;
    const selected = new Set(inputs?.selected_file_ids ?? []);
    const restoredFiles = readyExplorationFiles((inputs?.files ?? []).filter(file => selected.has(file.file_id)));
    if (selected.size !== restoredFiles.length) dataContextFailed.value = true;
    shareMode.value = session.is_shared ? 'public' : 'private';
    titleManuallySet.value = session.title_manually_set;
    title.value = session.title || t('New Chat');
    selectedConnectors.value = [];
    for (const event of session.events) {
      if (event.event === 'message' && 'role' in event.data && event.data.role === 'user') {
        selectedConnectors.value = event.data.metadata?.data_connectors ?? [];
      }
    }
    analysisDraft.restore({ message: '', files: restoredFiles, skills: selectedSkills.value,
      mcpServers: selectedMcpServers.value, connectors: selectedConnectors.value });
    realTime.value = true;
    void agentApi.clearUnreadMessageCount(id);
  } catch {
    if (generation === viewGeneration && !viewDisposed) connectionNotice.value = '无法读取会话状态，请稍后重试。';
  } finally { if (generation === viewGeneration) isSavingDataContext.value = false; }
};
const resetDataSelection = () => { attachments.value = []; dataContextFailed.value = false; };
// Initialize active conversation
function handleConnectorDraft(payload: unknown) {
  const draft = payload as { sessionId: string; files: FileInfo[]; query?: string; onAccepted?: (accepted: boolean) => void };
  if (viewDisposed || draft.sessionId !== sessionId.value) return;
  const merged = mergeExplorationFiles(attachments.value, draft.files);
  if (merged.length > 32) { draft.onAccepted?.(false); showErrorToast('本轮最多使用 32 个文件，请先移除部分文件。'); return; }
  attachments.value = merged;
  if (!inputMessage.value.trim()) inputMessage.value = draft.query
    ? `针对“${draft.query}”，请先检查这些文件的内容和适用范围，再开展分析。`
    : '请先检查这些文件，说明可开展的分析：';
  else if (draft.query && !inputMessage.value.includes(draft.query)) {
    inputMessage.value += `\n\n本次加入的数据对应问题：${draft.query}。请先检查这些文件的内容和适用范围。`;
  }
  dataDiscovery.value = false;
  draft.onAccepted?.(true);
}

async function submitPendingChat(pending: PendingChat) {
  // Restore the consumed handoff before the first API write so a failed save
  // leaves the original question and selection available for an ordinary retry.
  inputMessage.value = pending.message;
  attachments.value = readyExplorationFiles(pending.files);
  selectedConnectors.value = pending.dataConnectors ?? [];
  selectedSkills.value = pending.skills ?? [];
  selectedMcpServers.value = pending.mcpServers ?? [];
  if (pending.dataDiscovery) { dataDiscovery.value = true; await submitDiscovery(); return; }
  await chat(pending.message, attachments.value, selectedSkills.value, selectedMcpServers.value, pending.agentProfileId ?? null);
}

onMounted(() => {
  eventBus.on('task-data-input-change', handleTaskDataInputChange);
  eventBus.on('visualization-analysis-draft', handleVisualizationDraft);
  eventBus.on('connector-analysis-draft', handleConnectorDraft);
  eventBus.on('exploration-data-selected', handleConnectorDraft);
  eventBus.on('open-data-refine', handleOpenDataRefine);
  eventBus.on(EVENT_SESSION_RENAMED, handleSessionRenamed);
  hideFilePanel();
  const routeParams = router.currentRoute.value.params;
  if (routeParams.sessionId) {
    // If sessionId is included in URL, use it directly
    sessionId.value = String(routeParams.sessionId) as string;
    const pendingChat = consumePendingChat(sessionId.value);
    if (pendingChat?.message) {
      analysisDraft.restore();
      void submitPendingChat(pendingChat);
    } else {
      restoreSession();
    }
  }


});

onUnmounted(() => {
  viewDisposed = true; viewGeneration += 1; inputGeneration += 1;
  inputRequest?.abort(); analysis.dispose();
  eventBus.off('task-data-input-change', handleTaskDataInputChange);
  eventBus.off('visualization-analysis-draft', handleVisualizationDraft);
  eventBus.off('connector-analysis-draft', handleConnectorDraft);
  eventBus.off('exploration-data-selected', handleConnectorDraft);
  eventBus.off('open-data-refine', handleOpenDataRefine);
  eventBus.off(EVENT_SESSION_RENAMED, handleSessionRenamed);
});
const isLastNoMessageTool = (tool: ToolContent) => {
  return tool.tool_call_id === lastNoMessageTool.value?.tool_call_id;
}

const isLiveTool = (tool: ToolContent) => {
  if (tool.status === 'calling') {
    return true;
  }
  if (!isLastNoMessageTool(tool)) {
    return false;
  }
  if (tool.timestamp > Date.now() - 5 * 60 * 1000) {
    return true;
  }
  return false;
}

const handleToolClick = (tool: ToolContent) => {
  realTime.value = false;
  if (sessionId.value) {
    toolPanel.value?.showToolPanel(tool, false);
  }
}

const handleJupyterOpened = (tool: ToolContent) => {
  realTime.value = true;
  toolPanel.value?.showToolPanel(tool, true);
}

const jumpToRealTime = () => {
  realTime.value = true;
  if (lastNoMessageTool.value) {
    toolPanel.value?.showToolPanel(lastNoMessageTool.value, isLiveTool(lastNoMessageTool.value));
  }
}

const handleFollow = () => analysis.followTail();
const handleScroll = () => {
  if (analysis.isViewportReaderScroll()) follow.value = simpleBarRef.value?.isScrolledToBottom() ?? false;
};
const handleStop = () => analysis.stop();
const handleFileListShow = () => {
  taskData.open('all')
}

// Share functionality handlers
const handleShareModeChange = async (mode: 'private' | 'public') => {
  if (!sessionId.value || sharingLoading.value) return;
  
  // If mode is same as current, no need to call API
  if (shareMode.value === mode) {
    linkCopied.value = false;
    return;
  }
  
  try {
    sharingLoading.value = true;
    
    if (mode === 'public') {
      await agentApi.shareSession(sessionId.value);
    } else {
      await agentApi.unshareSession(sessionId.value);
    }
    
    shareMode.value = mode;
    linkCopied.value = false;
  } catch (error) {
    console.error('Error changing share mode:', error);
    showErrorToast(t('Failed to change sharing settings'));
  } finally {
    sharingLoading.value = false;
  }
}

const handleInstantShare = async () => {
  if (!sessionId.value) return;
  
  try {
    sharingLoading.value = true;
    await agentApi.shareSession(sessionId.value);
    shareMode.value = 'public';
    linkCopied.value = false;
  } catch (error) {
    console.error('Error sharing session:', error);
    showErrorToast(t('Failed to share session'));
  } finally {
    sharingLoading.value = false;
  }
}

const handleCopyLink = async () => {
  if (!sessionId.value) return;
  
  const shareUrl = `${window.location.origin}/share/${sessionId.value}`;
  
  try {
    const success = await copyToClipboard(shareUrl);
    
    if (success) {
      linkCopied.value = true;
      setTimeout(() => {
        linkCopied.value = false;
      }, 3000);
      showSuccessToast(t('Link copied to clipboard'));
    } else {
      showErrorToast(t('Failed to copy link'));
    }
  } catch (error) {
    console.error('Error copying share link:', error);
    showErrorToast(t('Failed to copy link'));
  }
}
</script>

<style scoped>
.task-data-entry { display: inline-flex; align-items: center; justify-content: center; flex-shrink: 0; gap: 6px; min-height: 32px; padding: 6px 10px; border: 1px solid var(--border-btn-main); border-radius: 999px; color: var(--text-secondary); font-size: 13px; cursor: pointer; }
.task-data-entry:hover, .task-data-entry[aria-expanded="true"] { background: var(--fill-tsp-white-light); }
.task-data-entry:focus-visible { outline: 2px solid var(--focus-ring-color, #2b7659); outline-offset: 2px; }
.task-data-entry-count { min-width: 17px; padding: 1px 4px; border-radius: 999px; font-size: 10px; background: var(--fill-tsp-gray-main); }
.analysis-chat-layout { display: flex; flex-direction: column; flex: 1; min-width: 0; min-height: 0; overflow: hidden; }
.analysis-message-scroll { flex: 1 1 0%; height: auto; overscroll-behavior: contain; }
.analysis-chat-footer { flex-shrink: 0; padding: 8px 24px 6px; background: var(--background-gray-main); }
.analysis-chat-input { position: relative; width: 100%; max-width: 800px; margin: 0 auto; }
.analysis-chat-status { display: flex; align-items: center; gap: 8px; min-width: 0; margin-bottom: 6px; }
.analysis-chat-messages :deep(.prose) { font-size: 14px; line-height: 1.7; overflow-wrap: anywhere; }
.analysis-chat-messages :deep(.prose p) { margin-top: .55em; margin-bottom: .55em; }
.analysis-chat-messages :deep(.prose li) { margin-top: .2em; margin-bottom: .2em; }
.analysis-chat-messages :deep(.prose pre) { max-width: 100%; overflow-x: auto; overflow-wrap: normal; }
@container analysis-data-layout (max-width: 1000px) {
  .chat-header-agent { display: none; }
}
@container analysis-data-content (max-width: 560px) {
  .analysis-chat-footer { padding: 6px 12px max(6px, env(safe-area-inset-bottom)); }
  .analysis-version { display: none; }
}
</style>
