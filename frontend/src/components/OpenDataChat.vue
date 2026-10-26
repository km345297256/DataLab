<template>
  <div class="contents">
    <p v-if="error" role="status" class="rounded-lg border border-amber-300 px-3 py-2 text-sm">
      {{ error }} <button type="button" class="ml-2 underline" :disabled="loading" @click="refresh">重新读取</button>
    </p>
    <slot :entries="entries" />
    <p v-if="submitting" role="status" class="text-sm text-[var(--text-secondary)]">正在记录查找要求…你可以继续编辑下一条消息。</p>
  </div>
</template>

<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue';
import type { Message } from '../types/message';
import { createOpenDataSearch, listOpenDataSearches, type OpenDataSearch } from '../api/openData';
import { openDataTimeline } from '../utils/openData';

const props = defineProps<{ sessionId?: string; messages: Message[]; messageEntries?: { message: Message; index: number }[]; isShare?: boolean }>();
const emit = defineEmits<{ (event: 'count', value: number): void }>();
const searches = ref<OpenDataSearch[]>([]);
const error = ref('');
const loading = ref(false), submitting = ref(false);
const entries = computed(() => openDataTimeline(props.messages, searches.value, props.messageEntries));
let generation = 0;
let request: AbortController | undefined;
let submitRequest: AbortController | undefined;
let active = true;

async function refresh() {
  if (!active || props.isShare || !props.sessionId) return;
  const sessionId = props.sessionId;
  const version = generation;
  request?.abort();
  const controller = new AbortController();
  request = controller;
  loading.value = true;
  try {
    const result = await listOpenDataSearches(sessionId, controller.signal);
    if (!active || version !== generation || controller.signal.aborted) return;
    // An in-flight list must not erase a search created after the list started.
    searches.value = [...new Map([...searches.value, ...result.filter(item => item.session_id === sessionId)].map(item => [item.id, item])).values()];
    error.value = '';
    emit('count', searches.value.length);
  } catch {
    if (active && version === generation && !controller.signal.aborted) error.value = '查找记录暂时无法完整读取，已有结果仍保留。';
  } finally {
    if (active && version === generation && !controller.signal.aborted) loading.value = false;
  }
}

async function start(query: string, parentId?: string, sourceIds?: string[]): Promise<boolean> {
  if (!active || props.isShare || !props.sessionId || submitting.value || !query.trim()) return false;
  const sessionId = props.sessionId, version = generation;
  submitRequest = new AbortController();
  const controller = submitRequest;
  submitting.value = true;
  try {
    const search = await createOpenDataSearch(sessionId, { query: query.trim(), ...(parentId ? { parent_id: parentId } : {}), ...(sourceIds?.length ? { source_ids: [...sourceIds] } : {}) }, controller.signal);
    if (!active || version !== generation || controller.signal.aborted || search.session_id !== sessionId) return false;
    searches.value = [...searches.value.filter(item => item.id !== search.id), search];
    error.value = '';
    emit('count', searches.value.length);
    return true;
  } catch {
    if (active && version === generation && !controller.signal.aborted) {
      error.value = '查找提交尚未确认，输入已保留。请先重新读取记录，再决定是否重试。';
    }
    return false;
  } finally {
    if (active && version === generation) submitting.value = false;
  }
}

watch(() => [props.sessionId, props.isShare] as const, () => {
  generation += 1;
  request?.abort(); submitRequest?.abort();
  searches.value = []; error.value = ''; loading.value = false; submitting.value = false;
  emit('count', 0);
  void refresh();
}, { immediate: true });
onUnmounted(() => { active = false; generation += 1; request?.abort(); submitRequest?.abort(); });
defineExpose({ start, refresh });
</script>
