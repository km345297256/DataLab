<template>
  <section class="plan-panel" :class="compact ? 'plan-panel--compact' : 'mb-2 overflow-hidden rounded-xl border border-[var(--border-main)] bg-[var(--background-menu-white)]'" aria-label="任务进度" :data-plan-status="presentation.status">
    <Popover v-model:open="isExpanded">
    <component :is="compact ? PopoverTrigger : 'div'" :as-child="compact || undefined">
    <button type="button" class="plan-panel-trigger flex w-full items-center gap-2 text-left" :class="compact ? 'min-h-8 rounded-lg px-2 py-1' : 'px-4 py-2'" :aria-expanded="isExpanded" aria-label="查看任务进度与执行记录" @click="!compact && (isExpanded = !isExpanded)">
      <StepSuccessIcon v-if="completed" />
      <CircleX v-else-if="failed" class="shrink-0 text-[var(--function-error)]" :size="16" />
      <CircleAlert v-else-if="!sessionId && presentation.status === 'partial'" class="shrink-0 text-amber-600" :size="16" />
      <CirclePause v-else-if="!sessionId && presentation.status === 'stopped'" class="shrink-0" :size="16" />
      <Clock v-else class="shrink-0 text-[var(--icon-secondary)]" :size="16" />
      <span class="min-w-0 flex-1 truncate text-[var(--text-secondary)]" :class="compact ? 'text-xs' : 'text-sm'" :title="currentLabel">{{ currentLabel }}</span>
      <span v-if="plan?.steps.length" class="plan-stage-count shrink-0 text-xs text-[var(--text-tertiary)]">{{ compact ? `${presentation.completed}/${presentation.total}` : planProgress }}</span>
      <ChevronDown v-if="isExpanded" :size="16" /><ChevronUp v-else :size="16" />
    </button>
    </component>
    <component :is="compact ? PopoverContent : 'div'" v-if="isExpanded" class="space-y-4 overflow-y-auto p-4" :class="compact ? 'plan-panel-content' : 'max-h-[45vh] border-t border-[var(--border-main)]'" :style="compact && popoverWidth ? { '--plan-panel-width': `${popoverWidth}px` } : undefined" side="top" align="start" :side-offset="8" :collision-padding="12" aria-label="任务进度与执行记录">
      <div v-if="plan?.steps.length">
        <h3 class="mb-2 text-sm font-medium">{{ $t('Task Progress') }}</h3>
        <ol class="space-y-2">
          <li v-for="step in presentation.steps" :data-plan-step-status="step.displayStatus" :key="step.id" class="flex items-start gap-2 text-sm">
            <StepSuccessIcon v-if="step.displayStatus === 'completed'" />
            <CircleX v-else-if="step.displayStatus === 'failed'" class="shrink-0 text-[var(--function-error)]" :size="16" />
            <CircleAlert v-else-if="step.displayStatus === 'partial'" class="shrink-0 text-amber-600" :size="16" />
            <CirclePause v-else-if="step.displayStatus === 'stopped'" class="shrink-0" :size="16" />
            <Clock v-else class="shrink-0" :size="16" />
            <span class="min-w-0 break-words">{{ step.description }} <span class="text-xs text-[var(--text-tertiary)]">· {{ stageLabels[step.displayStatus] }}</span></span>
          </li>
        </ol>
      </div>
      <DataRunHistory v-if="sessionId" :runs="runs" :loading="loading" :error="error" :has-more="nextBefore !== null" @refresh="load(false)" @more="load(true)" />
    </component>
    </Popover>
  </section>
</template>
<script setup lang="ts">
import { ref, computed, toRef } from 'vue';
import { ChevronUp, ChevronDown, Clock, CircleX, CircleAlert, CirclePause } from 'lucide-vue-next';
import StepSuccessIcon from './icons/StepSuccessIcon.vue';
import DataRunHistory from './DataRunHistory.vue';
import { Popover, PopoverContent, PopoverTrigger } from './ui/popover';
import { useDataRuns } from '@/composables/useDataRuns';
import { taskRunLabel } from '@/utils/taskProgress';
import { presentPlan } from '@/utils/planPresentation';
import type { Message } from '@/types/message';
import type { PlanEventData } from '../types/event';
const props = defineProps<{ plan?: PlanEventData; sessionId?: string; revision?: number; isRunning?: boolean; messages?: Message[]; compact?: boolean; popoverWidth?: number }>();
const isExpanded = ref(false);
const { runs, nextBefore, loading, error, load } = useDataRuns(toRef(props, 'sessionId'), toRef(props, 'revision'));
const latest = computed(() => runs.value[0]);
const presentation = computed(() => presentPlan(props.plan ?? { steps: [], timestamp: 0 }, props.messages ?? []));
const planProgress = computed(() => `${presentation.value.completed} / ${presentation.value.total} 个阶段完成`);
// A finished step/plan does not establish overall success; the run does.
const completed = computed(() => props.sessionId ? !props.isRunning && !error.value && latest.value?.history_complete && latest.value.status === 'succeeded' : presentation.value.status === 'completed');
const failed = computed(() => props.sessionId ? !props.isRunning && !error.value && latest.value?.status === 'failed' : presentation.value.status === 'failed');
const currentLabel = computed(() => props.sessionId
  ? taskRunLabel(latest.value, Boolean(props.isRunning), error.value)
  : presentation.value.activeDescription || `阶段记录：${stageLabels[presentation.value.status]}`);
const stageLabels = { completed: '已完成', partial: '部分完成', failed: '失败', stopped: '已停止', running: '进行中', pending: '待执行', unknown: '结果待确认' };
</script>

<style scoped>
.plan-panel--compact { flex: 1; min-width: 0; }
.plan-panel--compact .plan-panel-trigger:hover, .plan-panel--compact .plan-panel-trigger[aria-expanded="true"] { background: var(--fill-tsp-white-dark); }
.plan-panel-trigger:focus-visible { outline: 2px solid var(--text-tertiary); outline-offset: 1px; }
.plan-panel--compact .plan-panel-trigger > svg { flex-shrink: 0; }
/* PopoverContent teleports through a wrapper and does not inherit this scope. */
:global([data-slot="popover-content"].plan-panel-content) { z-index: 65; width: min(var(--plan-panel-width, 520px), calc(100vw - 24px)); max-height: min(320px, 45dvh, var(--reka-popover-content-available-height)); overscroll-behavior: contain; border-radius: 12px; color: var(--text-primary); }
</style>
