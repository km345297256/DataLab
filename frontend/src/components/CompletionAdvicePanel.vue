<template>
  <div v-if="recommendations.length || advice.is_skill_candidate" class="completion-advice flex min-w-0 flex-col gap-3">
    <section class="rounded-xl border border-[var(--border-main)] bg-[var(--background-menu-white)] p-4" aria-label="推荐追问">
      <h3 class="mb-2 text-sm font-medium text-[var(--text-primary)]">推荐追问</h3>
      <div class="flex flex-col gap-2 text-sm text-[var(--text-secondary)]">
        <component :is="readOnly ? 'div' : 'button'" v-for="(item, index) in recommendations" :key="`${item}-${index}`"
          :type="readOnly ? undefined : 'button'"
          class="flex w-full items-center justify-between gap-3 rounded-lg bg-[var(--background-gray-main)] px-3 py-2 text-left"
          :class="{ 'hover:bg-[var(--fill-tsp-white-dark)] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2': !readOnly }"
          @click="sendFollowUp(item)">
          <MessageCircle aria-hidden="true" class="size-4 shrink-0 text-[var(--text-tertiary)]" />
          <span class="min-w-0 flex-1 break-words">{{ item }}</span>
          <ChevronRight v-if="!readOnly" aria-hidden="true" class="size-4 shrink-0" />
        </component>
        <component v-if="advice.is_skill_candidate" :is="readOnly ? 'div' : 'button'" :type="readOnly ? undefined : 'button'"
          class="flex w-full items-center justify-between gap-3 rounded-lg bg-[var(--background-gray-main)] px-3 py-2 text-left"
          :class="{ 'hover:bg-[var(--fill-tsp-white-dark)] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2': !readOnly }"
          @click="sendFollowUp(SKILL_CREATOR_PROMPT)">
          <Puzzle aria-hidden="true" class="size-4 shrink-0 text-[var(--text-tertiary)]" />
          <span class="min-w-0 flex-1 break-words">将本次分析保存为技能</span>
          <ChevronRight v-if="!readOnly" aria-hidden="true" class="size-4 shrink-0" />
        </component>
      </div>
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue';
import { ChevronRight, MessageCircle, Puzzle } from 'lucide-vue-next';
import type { CompletionAdviceData } from '../types/event';
import { analysisFollowUps, SKILL_CREATOR_PROMPT } from '../utils/completionAdvice';

const props = withDefaults(defineProps<{ advice: CompletionAdviceData; readOnly?: boolean }>(), { readOnly: false });
const emit = defineEmits<{ (event: 'followUp', question: string): void }>();
const recommendations = computed(() => analysisFollowUps(props.advice));
function sendFollowUp(question: string) {
  if (!props.readOnly) emit('followUp', question);
}
</script>
