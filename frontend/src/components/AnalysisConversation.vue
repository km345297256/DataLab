<template>
  <div class="analysis-conversation flex min-w-0 max-w-full flex-col gap-2">
    <template v-for="entry in renderEntries" :key="entry.kind === 'message' ? `message:${messageKey(entry.message)}` : `search:${entry.search.id}`">
      <slot v-if="entry.kind === 'search'" name="search" :search="entry.search" />
      <template v-else v-for="{ message, index } in [entry]" :key="messageKey(message)">
      <div v-if="showMessage(message, index)" :data-message-key="messageKey(message)" class="analysis-message">
        <ChatMessage
          :message="message"
          suppress-skill-creation
          :session-id="sessionId || undefined"
          :hide-header="isConsecutiveAssistant(messages, index)"
          :show-assistant-actions="!isLoading && presentation.latestAssistant === message"
          :allow-analysis-resume="canResumeAnalysis(index)"
          :clarification-pending="Boolean(pendingClarificationId && (message.content as MessageContent).metadata?.clarification?.id === pendingClarificationId)"
          :can-answer-clarification="canAnswerClarification && waitingForUser && !isLoading"
          :waiting-for-user="waitingForUser"
          :task-summary-expanded="expanded.has(messageKey(message))"
          :show-product-button="allowProducts && !isLoading && presentation.latestAssistant === message"
          @resume-analysis="$emit('resumeAnalysis', index)"
          @tool-click="$emit('toolClick', $event)"
          @jupyter-opened="$emit('jupyterOpened', $event)"
          @task-summary-toggle="toggleSummary(message)"
          @show-product="$emit('showProduct')"
          @answer-clarification="$emit('answerClarification', $event)"
        />
        <button v-if="canRetrySkillCreation(message)" type="button"
          class="mt-2 rounded-lg border border-[var(--border-main)] bg-[var(--background-menu-white)] px-3 py-2 text-sm font-medium text-[var(--text-primary)] hover:bg-[var(--fill-tsp-gray-main)]"
          @click="retrySkillCreation(message)">重试创建技能</button>
      </div>
      <SkillCreationCard v-for="draft in messageSkillCreations(message)" :key="draft.id" :initial="draft" />
      <section v-if="deliveredImages(message).length" class="mb-3 mt-1 overflow-hidden rounded-xl border border-[var(--border-main)] bg-[var(--background-menu-white)]" aria-label="可视化成果">
        <header class="flex items-center justify-between border-b border-[var(--border-main)] px-3.5 py-2.5">
          <span class="flex items-center gap-2 text-xs font-medium"><ImageIcon class="size-3.5 text-[#2b7659]" />可视化成果</span>
          <span class="text-[10px] text-[var(--text-tertiary)]">点击图片查看与下载</span>
        </header>
        <div class="grid gap-3 p-3" :class="deliveredImages(message).length > 1 ? 'sm:grid-cols-2' : 'grid-cols-1'">
          <button v-for="file in deliveredImages(message)" :key="file.file_id" type="button"
            class="overflow-hidden rounded-lg border border-[var(--border-main)] bg-[var(--background-gray-main)] text-left"
            @click="showFilePanel(file, deliveredFiles(message))">
            <AnalysisResultImage :file="file" />
            <span class="flex items-center justify-between gap-3 px-3 py-2 text-xs">
              <span class="truncate font-medium">{{ file.filename }}</span><span class="shrink-0 text-[var(--text-tertiary)]">查看成果</span>
            </span>
          </button>
        </div>
      </section>
      </template>
    </template>
    <CompletionAdvicePanel v-if="completionAdvice && !isLoading" class="mt-4" :advice="completionAdvice" @follow-up="$emit('followUp', $event)" />
    <button v-if="shapefileSources.length && !isLoading" type="button"
      class="mt-3 rounded-lg border border-[var(--border-main)] bg-[var(--background-menu-white)] px-4 py-3 text-left text-sm" @click="openShapefilePreview">查看 Shapefile 地图</button>
    <button v-if="completionAdvice?.molecular_preview_available && !isLoading" type="button"
      class="mt-3 rounded-lg border border-[var(--border-main)] bg-[var(--background-menu-white)] px-4 py-3 text-left text-sm" @click="openMolecularPreview">查看分子结构 3D 预览</button>
    <slot />
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue';
import { Image as ImageIcon } from 'lucide-vue-next';
import ChatMessage from './ChatMessage.vue';
import SkillCreationCard from './SkillCreationCard.vue';
import { messageSkillCreations } from '../utils/skillCreation';
import AnalysisResultImage from './AnalysisResultImage.vue';
import CompletionAdvicePanel from './CompletionAdvicePanel.vue';
import type { CompletionAdviceData } from '../types/event';
import { isConsecutiveAssistant, type Message, type MessageContent, type ToolContent } from '../types/message';
import type { ClarificationAnswer } from '../types/clarification';
import { ConversationPresentationIndex, currentTurnDeliveries, deliveredFiles, deliveredImages } from '../utils/analysisPresentation';
import { useFilePanel } from '../composables/useFilePanel';
import type { OpenDataTimelineEntry } from '../utils/openData';
import { showErrorToast } from '../utils/toast';
import { shapefilePreviewSources } from '../utils/previewActions';

const props = withDefaults(defineProps<{
  messages: Message[];
  entries?: OpenDataTimelineEntry[];
  sessionId?: string | null;
  isLoading: boolean;
  completionAdvice?: CompletionAdviceData;
  canResumeAnalysis: (index: number) => boolean;
  messageKey: (message: Message) => number;
  allowProducts?: boolean;
  beforeSummaryToggle?: (key: number) => (() => void) | undefined;
  pendingClarificationId?: string;
  waitingForUser?: boolean;
  canAnswerClarification?: boolean;
  allowFollowUp?: boolean;
}>(), { allowProducts: false, allowFollowUp: true });
const emit = defineEmits<{
  (event: 'resumeAnalysis', index: number): void;
  (event: 'toolClick', tool: ToolContent): void;
  (event: 'jupyterOpened', tool: ToolContent): void;
  (event: 'showProduct'): void;
  (event: 'followUp', question: string): void;
  (event: 'answerClarification', answer: ClarificationAnswer): void;
}>();
const { showFilePanel, beginFilePreview } = useFilePanel();
const shapefileSources = computed(() => shapefilePreviewSources(currentTurnDeliveries(props.messages), props.completionAdvice));
const retryingSkillMessage = ref<number | null>(null);
const expanded = ref(new Set<number>());
const presentation = new ConversationPresentationIndex();
const renderEntries = computed<OpenDataTimelineEntry[]>(() => {
  const display = presentation.update(props.messages);
  return props.entries ?? display.map(entry => ({ kind: 'message', ...entry }));
});
watch(() => props.sessionId, () => { expanded.value = new Set(); retryingSkillMessage.value = null; });
function canRetrySkillCreation(message: Message) {
  if (props.isLoading || props.waitingForUser || props.allowFollowUp === false || message.type !== 'assistant'
      || !(message.content as MessageContent).metadata?.skill_creation_failure?.retryable
      || retryingSkillMessage.value === props.messageKey(message)) return false;
  // Only the latest response can start this follow-up. An old failure cannot
  // accidentally create a skill from a newer, unrelated analysis turn.
  for (let index = props.messages.length - 1; index >= 0; index--) {
    const current = props.messages[index];
    if (current.type === 'user') return false;
    if (current.type === 'assistant') return current === message;
  }
  return false;
}
function retrySkillCreation(message: Message) {
  if (!canRetrySkillCreation(message)) return;
  retryingSkillMessage.value = props.messageKey(message);
  // A new explicit creation request, never resumeAnalysis or checkpoint replay.
  emit('followUp', '/skill-creator 将本次已完成的分析流程保存为可复用的技能');
}
function toggleSummary(message: Message) {
  const key = props.messageKey(message);
  const settle = props.beforeSummaryToggle?.(key);
  const next = new Set(expanded.value);
  if (next.has(key)) next.delete(key); else next.add(key);
  expanded.value = next;
  void nextTick(() => settle?.());
}
function showMessage(message: Message, _index: number) {
  if (message.type !== 'step') return true;
  const summary = presentation.summaryFor(message);
  return !summary || expanded.value.has(props.messageKey(summary));
}
function openShapefilePreview() {
  if (props.isLoading) return;
  const files = currentTurnDeliveries(props.messages);
  const candidates = shapefilePreviewSources(files, props.completionAdvice);
  const source = candidates[candidates.length - 1];
  if (!source) { showErrorToast('本轮没有可供地图预览的已交付文件。'); return; }
  const request = beginFilePreview();
  if (request.isCurrent()) request.show(source, files);
}
function openMolecularPreview() {
  if (props.isLoading) return;
  const files = currentTurnDeliveries(props.messages).filter(file => /\.(cif|pdb|ent|mol|sdf|xyz|mol2|vasp)$/i.test(file.filename) || /^(poscar|contcar)$/i.test(file.filename));
  const source = files[files.length - 1];
  if (source) showFilePanel(source, files);
  else showErrorToast('本轮没有可供分子结构预览的已交付文件。');
}
</script>

<style scoped>
.analysis-conversation, .analysis-message { overflow-x: clip; overflow-y: visible; }
.analysis-message { min-width: 0; max-width: 100%; overflow-wrap: anywhere; font-size: 14px; line-height: 1.65; }
.analysis-message :deep(.prose) { width: 100%; min-width: 0; max-width: 100% !important; overflow-wrap: anywhere; word-break: break-word; font-size: 14px !important; line-height: 1.7 !important; }
.analysis-message :deep(.prose p) { margin-top: .55em; margin-bottom: .55em; }
.analysis-message :deep(.prose pre) { max-width: 100%; overflow-x: auto; overscroll-behavior-x: contain; white-space: pre; overflow-wrap: normal; word-break: normal; }
.analysis-message :deep(.prose pre code) { white-space: inherit; overflow-wrap: inherit; word-break: inherit; }
.analysis-message :deep(.prose table) { display: block; width: 100%; max-width: 100%; overflow-x: auto; }
.analysis-message :deep(.prose a), .analysis-message :deep(.prose :not(pre) > code) { overflow-wrap: anywhere; word-break: break-word; }
.analysis-message :deep(.prose img), .analysis-message :deep(.prose video), .analysis-message :deep(.prose canvas), .analysis-message :deep(.prose svg) { max-width: 100%; height: auto; }
</style>
