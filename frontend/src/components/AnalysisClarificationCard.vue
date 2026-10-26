<template>
  <section class="clarification-card" aria-label="分析确认问题">
    <header class="clarification-header">
      <CircleHelp :size="18" aria-hidden="true" />
      <div>
        <h3>{{ pending ? '需要你确认' : '确认问题记录' }}</h3>
        <p>{{ pending ? '请明确下面的问题，确认后继续。系统不会代你选择答案。' : '此问题已不能在这里提交，答复见后续对话。' }}</p>
      </div>
    </header>
    <form @submit.prevent="submit">
      <fieldset v-for="(question, index) in clarification.questions" :key="question.id" :disabled="!pending || disabled || submitted" class="clarification-question">
        <legend>{{ clarification.questions.length > 1 ? `${index + 1}. ` : '' }}{{ question.question }}</legend>
        <p v-if="question.reason" class="clarification-reason">{{ question.reason }}</p>
        <template v-if="pending">
          <div v-if="question.options.length" class="clarification-options" role="radiogroup" :aria-label="question.question">
            <button v-for="option in question.options" :key="option" type="button" role="radio"
              :aria-checked="answers[question.id] === option" :class="{ selected: answers[question.id] === option }"
              @click="choose(question.id, option)">{{ option }}</button>
          </div>
          <label class="clarification-answer-label">
            <span>{{ question.options.length ? '也可以填写自己的答案' : '你的答案' }}</span>
            <textarea v-model="answers[question.id]" rows="2" maxlength="4000" :aria-label="`回答：${question.question}`" placeholder="请填写你的答案…" />
          </label>
        </template>
        <ul v-else-if="question.options.length" class="clarification-record-options"><li v-for="option in question.options" :key="option">{{ option }}</li></ul>
      </fieldset>
      <footer v-if="pending" class="clarification-footer">
        <span>也可在下方输入框补充说明。</span>
        <button type="submit" :disabled="disabled || submitted || !answerText">{{ submitted ? '正在提交…' : '确认并继续' }}</button>
      </footer>
    </form>
  </section>
</template>

<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue';
import { CircleHelp } from 'lucide-vue-next';
import type { AnalysisClarification, ClarificationAnswer } from '../types/clarification';
import { clarificationAnswerText } from '../utils/analysisClarification';

const props = defineProps<{ clarification: AnalysisClarification; pending?: boolean; disabled?: boolean }>();
const emit = defineEmits<{ (event: 'answer', answer: ClarificationAnswer): void }>();
const answers = ref<Record<string, string>>(Object.create(null));
const submitted = ref(false);
const answerText = computed(() => clarificationAnswerText(props.clarification, answers.value));
watch(() => props.clarification.id, () => { answers.value = Object.create(null); submitted.value = false; });
watch(() => [props.pending, props.disabled], () => {
  if (props.pending && !props.disabled) submitted.value = false;
});
function choose(id: string, value: string) {
  if (!props.pending || props.disabled || submitted.value) return;
  answers.value[id] = value;
}
function submit() {
  if (!props.pending || props.disabled || submitted.value || !answerText.value) return;
  submitted.value = true;
  emit('answer', { clarificationId: props.clarification.id, message: answerText.value });
  // A parent may decline submission after its context changes. Keep the draft
  // usable in that case; a real send disables the card before the next tick.
  void nextTick(() => { if (props.pending && !props.disabled) submitted.value = false; });
}
</script>

<style scoped>
.clarification-card { border: 1px solid var(--border-main); border-radius: 12px; padding: 18px; background: var(--background-menu-white); }
.clarification-header { display: flex; align-items: flex-start; gap: 10px; color: var(--text-primary); }
.clarification-header > svg { flex-shrink: 0; margin-top: 2px; color: #2b7659; }
.clarification-header h3 { margin: 0; font-size: 14px; font-weight: 600; }
.clarification-header p, .clarification-reason { margin: 5px 0 0; color: var(--text-secondary); font-size: 12px; line-height: 1.65; }
.clarification-question { min-width: 0; margin-top: 18px; }
.clarification-question legend { font-size: 14px; font-weight: 500; color: var(--text-primary); overflow-wrap: anywhere; }
.clarification-options { display: flex; flex-wrap: wrap; gap: 7px; margin-top: 10px; }
.clarification-options button { padding: 7px 11px; border: 1px solid var(--border-main); border-radius: 8px; color: var(--text-primary); font-size: 13px; text-align: left; overflow-wrap: anywhere; }
.clarification-options button:hover:enabled, .clarification-options button.selected { border-color: #2b7659; background: color-mix(in srgb, #2b7659 10%, transparent); }
.clarification-answer-label { display: block; margin-top: 10px; color: var(--text-secondary); font-size: 12px; }
.clarification-answer-label textarea { display: block; width: 100%; margin-top: 5px; resize: vertical; padding: 8px 10px; border: 1px solid var(--border-main); border-radius: 8px; background: var(--background-gray-main); color: var(--text-primary); font-size: 13px; line-height: 1.6; }
.clarification-footer { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 12px; margin-top: 14px; }
.clarification-footer span, .clarification-record-options { font-size: 12px; color: var(--text-secondary); }
.clarification-record-options { padding-left: 20px; margin-top: 8px; list-style: disc; }
.clarification-footer button { padding: 8px 15px; border-radius: 8px; background: #2b7659; color: white; font-size: 13px; }
button:disabled, fieldset:disabled textarea { opacity: .55; cursor: not-allowed; }
button:focus-visible, textarea:focus-visible { outline: 2px solid #2b7659; outline-offset: 2px; }
</style>
