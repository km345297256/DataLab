<template>
  <div ref="container" class="acquisition-panel-slot" :class="{ 'is-open': panelOpen, 'is-compact': compact }"
    :style="{ width: panelOpen && !compact ? `${panelWidth}px` : '0px' }">
    <aside v-if="panelOpen" id="data-acquisition-panel" data-testid="data-acquisition-panel"
      class="acquisition-panel" :role="compact ? 'dialog' : 'complementary'" :aria-modal="compact ? true : undefined"
      aria-label="数据获取" @keydown="handleKeydown">
      <div v-if="!compact" class="acquisition-resize" role="separator" tabindex="0" aria-label="调整数据获取面板宽度"
        aria-orientation="vertical" :aria-valuemin="minimumWidth" :aria-valuemax="maximumWidth" :aria-valuenow="panelWidth"
        @pointerdown="startResize" @keydown="resizeKeydown"><span /></div>
      <header class="acquisition-panel-heading">
        <div><h2>数据获取</h2><p>当前对话的获取任务</p></div>
        <button ref="closeButton" type="button" class="acquisition-close" aria-label="收起数据获取" title="收起数据获取"
          @click="closePanel"><X :size="20" /></button>
      </header>
      <DataAcquisitionContent @input-prepared="returnToComposer" />
      <footer class="acquisition-panel-footer">收起后继续获取，完成的文件会保存到我的数据。</footer>
    </aside>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onUnmounted, ref, watch } from 'vue';
import { X } from 'lucide-vue-next';
import DataAcquisitionContent from './DataAcquisitionContent.vue';
import { useDataAcquisition } from '../composables/useDataAcquisition';
import { useResizeObserver } from '../composables/useResizeObserver';
import { eventBus } from '../utils/eventBus';
import { EVENT_SHOW_ACQUISITION_PANEL } from '../constants/event';

const { panelOpen, close } = useDataAcquisition();
const container = ref<HTMLElement>();
const closeButton = ref<HTMLButtonElement>();
const { size } = useResizeObserver(container, { target: 'parent', property: 'width' });
const compact = computed(() => size.value < 1180);
const maximumWidth = computed(() => Math.max(280, size.value - 780));
const minimumWidth = computed(() => Math.min(360, maximumWidth.value));
const preferredWidth = ref(400);
const panelWidth = computed(() => Math.round(Math.min(maximumWidth.value, Math.max(minimumWidth.value, preferredWidth.value))));
let returnFocus: HTMLElement | null = null;
let automatic = false;
let resizing = false, startX = 0, startWidth = 0;
let oldCursor = '', oldSelect = '';
const noteOpen = (payload: unknown) => { automatic = Boolean((payload as { automatic?: boolean })?.automatic); };
eventBus.on(EVENT_SHOW_ACQUISITION_PANEL, noteOpen);

watch(panelOpen, async shown => {
  if (!shown) { stopResize(); return; }
  returnFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  await nextTick();
  if (panelOpen.value && (compact.value || !automatic)) closeButton.value?.focus({ preventScroll: true });
});
watch(compact, async narrow => {
  stopResize();
  if (narrow && panelOpen.value) {
    await nextTick();
    closeButton.value?.focus({ preventScroll: true });
  }
});

function closePanel() {
  close();
  void nextTick(() => {
    const target = returnFocus?.isConnected && returnFocus !== document.body && returnFocus !== document.documentElement && returnFocus.getClientRects().length
      ? returnFocus : document.querySelector<HTMLElement>('.acquisition-entry');
    target?.focus({ preventScroll: true });
  });
}
function returnToComposer() {
  if (!compact.value) return;
  close();
  void nextTick(() => document.querySelector<HTMLTextAreaElement>('textarea.chat-message-input')?.focus({ preventScroll: true }));
}
function handleKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); closePanel(); return; }
  if (!compact.value || event.key !== 'Tab') return;
  const focusable = [...(container.value?.querySelectorAll<HTMLElement>('button:not(:disabled), a[href], input:not(:disabled), summary, [tabindex="0"]') || [])]
    .filter(element => element.getClientRects().length > 0);
  const first = focusable[0], last = focusable[focusable.length - 1];
  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
}
function moveResize(event: PointerEvent) {
  if (resizing) preferredWidth.value = Math.min(maximumWidth.value, Math.max(minimumWidth.value, startWidth + startX - event.clientX));
}
function stopResize() {
  if (!resizing) return;
  resizing = false;
  window.removeEventListener('pointermove', moveResize);
  window.removeEventListener('pointerup', stopResize);
  window.removeEventListener('pointercancel', stopResize);
  window.removeEventListener('blur', stopResize);
  document.body.style.cursor = oldCursor;
  document.body.style.userSelect = oldSelect;
}
function startResize(event: PointerEvent) {
  if (compact.value || event.button !== 0) return;
  event.preventDefault(); resizing = true; startX = event.clientX; startWidth = panelWidth.value;
  oldCursor = document.body.style.cursor; oldSelect = document.body.style.userSelect;
  document.body.style.cursor = 'col-resize'; document.body.style.userSelect = 'none';
  window.addEventListener('pointermove', moveResize);
  window.addEventListener('pointerup', stopResize);
  window.addEventListener('pointercancel', stopResize);
  window.addEventListener('blur', stopResize);
}
function resizeKeydown(event: KeyboardEvent) {
  const widths: Record<string, number> = { ArrowLeft: panelWidth.value + 24, ArrowRight: panelWidth.value - 24, Home: minimumWidth.value, End: maximumWidth.value };
  if (widths[event.key] == null) return;
  event.preventDefault(); preferredWidth.value = Math.min(maximumWidth.value, Math.max(minimumWidth.value, widths[event.key]!));
}
onUnmounted(() => { stopResize(); eventBus.off(EVENT_SHOW_ACQUISITION_PANEL, noteOpen); });
</script>

<style scoped>
.acquisition-panel-slot { position: relative; flex: none; min-width: 0; height: 100%; }
.acquisition-panel { height: 100%; min-height: 0; display: flex; flex-direction: column; background: var(--background-white-main); border-left: 1px solid var(--border-main); position: relative; }
.is-compact .acquisition-panel { position: fixed; inset: 0; z-index: 60; border: 0; width: 100%; height: 100dvh; padding-top: env(safe-area-inset-top); padding-bottom: env(safe-area-inset-bottom); }
.acquisition-panel-heading { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 18px 16px; flex: none; border-bottom: 1px solid var(--border-main); }
.acquisition-panel-heading h2 { font-size: 15px; font-weight: 600; }
.acquisition-panel-heading p { margin-top: 4px; font-size: 12px; color: var(--text-secondary); }
.acquisition-close { display: inline-flex; padding: 6px; border-radius: 7px; color: var(--text-secondary); }
.acquisition-close:hover { background: var(--fill-tsp-gray-main); }
.acquisition-close:focus-visible, .acquisition-resize:focus-visible { outline: 2px solid #2b7659; outline-offset: 2px; }
.acquisition-panel-footer { flex: none; padding: 10px 16px; font-size: 11px; color: var(--text-tertiary); border-top: 1px solid var(--border-main); line-height: 1.5; }
.acquisition-resize { position: absolute; z-index: 2; top: 0; bottom: 0; left: -4px; width: 8px; cursor: col-resize; touch-action: none; }
.acquisition-resize:hover span, .acquisition-resize:focus span { display: block; height: 100%; width: 2px; margin-left: 3px; background: #2b7659; }
</style>
