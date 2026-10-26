<template>
  <div
    class="h-[36px] flex items-center px-3 w-full bg-[var(--background-gray-main)] border-b border-[var(--border-main)] rounded-t-[12px] shadow-[inset_0px_1px_0px_0px_#FFFFFF] dark:shadow-[inset_0px_1px_0px_0px_#FFFFFF30]">
    <div class="flex-1 flex items-center justify-center">
      <div class="max-w-[250px] truncate text-[var(--text-tertiary)] text-sm font-medium text-center">{{
        shellSessionId }}
      </div>
    </div>
  </div>
  <div v-if="outputMetadata || outputPageError" class="shrink-0 border-b border-[var(--border-main)] px-3 py-2 text-xs text-[var(--text-secondary)]">
    <div class="flex flex-wrap items-center gap-3">
      <span v-if="outputMetadata?.preview_truncated && !outputPage">当前为头尾摘要，部分日志未展示。</span>
      <span v-if="outputPage">字节 {{ outputPage.start }}–{{ outputPage.next_cursor }} / {{ outputMetadata?.total_bytes }}</span>
      <button v-if="!outputPage && outputMetadata && shellSessionId" type="button" class="underline" :disabled="pageLoading" @click="loadOutputPage(0)">分段读取日志</button>
      <template v-if="outputPage">
        <button type="button" class="underline disabled:opacity-50" :disabled="pageLoading || !previousCursors.length" @click="previousOutputPage">上一段</button>
        <button type="button" class="underline disabled:opacity-50" :disabled="pageLoading || outputPage.eof" @click="nextOutputPage">{{ outputPage.next_cursor === outputMetadata?.total_bytes ? '刷新日志' : '下一段' }}</button>
        <button type="button" class="underline" :disabled="pageLoading" @click="returnToPreview">返回最新输出</button>
      </template>
      <span v-if="pageLoading" role="status">正在读取日志…</span>
    </div>
    <p v-if="outputPage?.lossy" class="mt-1 text-amber-700">部分日志已不可读取，本段从仍保留的位置开始。</p>
    <p v-if="outputPageError" role="alert" class="mt-1 text-amber-700">{{ outputPageError }}</p>
  </div>
  <div class="flex-1 min-h-0 w-full overflow-y-auto">
    <div dir="ltr" data-orientation="horizontal" class="flex flex-col flex-1 min-h-0">
      <div data-state="active" data-orientation="horizontal" role="tabpanel"
        id="radix-:r5m:-content-setup" tabindex="0"
        class="py-2 focus-visible:outline-none data-[state=inactive]:hidden flex-1 font-mono text-sm leading-relaxed px-3 outline-none overflow-auto whitespace-pre-wrap break-all"
        style="animation-duration: 0s;">
        <code v-html="shell"></code>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref, computed, watch, onUnmounted } from 'vue';
import { viewShellSession } from '@/api/agent';
import { ToolContent } from '@/types/message';
import { sanitizeToolDisplayText } from '@/utils/toolDisplay';
import { readShellOutputMetadata, readShellOutputPage, SHELL_LOG_PAGE_BYTES, type ShellOutputMetadata, type ShellOutputPage } from '@/utils/shellOutputPage';
import { shellConsoleExitText } from '@/utils/shellConsole';
//import { showErrorToast } from '@/utils/toast';

const props = defineProps<{
  sessionId: string;
  toolContent: ToolContent;
  live: boolean;
}>();

defineExpose({
  loadContent: () => {
    loadShellContent();
  }
});

const shell = ref('');
const outputMetadata = ref<ShellOutputMetadata>();
const outputPage = ref<ShellOutputPage>();
const outputPageError = ref(''), pageLoading = ref(false);
const previousCursors = ref<number[]>([]);
let pageRequest: AbortController | undefined;
let pageGeneration = 0;
async function loadOutputPage(cursor: number, direction: 'next' | 'previous' | 'first' = 'first') {
  const metadata = outputMetadata.value;
  const id = shellSessionId.value;
  if (!metadata || !id || pageLoading.value) return;
  const generation = ++pageGeneration;
  const request = new AbortController(); pageRequest?.abort(); pageRequest = request;
  pageLoading.value = true; outputPageError.value = '';
  try {
    const response = await viewShellSession(props.sessionId, id,
      { output_id: metadata.output_id, cursor, max_bytes: SHELL_LOG_PAGE_BYTES }, request.signal);
    if (request.signal.aborted || generation !== pageGeneration) return;
    const result = readShellOutputPage(response, metadata.output_id, cursor);
    if (direction === 'next' && outputPage.value && cursor !== outputPage.value.cursor) previousCursors.value.push(outputPage.value.cursor);
    else if (direction === 'previous') previousCursors.value.pop();
    else if (direction === 'first') previousCursors.value = [];
    outputMetadata.value = result.metadata; outputPage.value = result.page;
    shell.value = escapeHtml(sanitizeToolDisplayText(result.output));
    stopAutoRefresh();
  } catch {
    if (!request.signal.aborted && generation === pageGeneration) outputPageError.value = '日志暂时不可读取，可能已随任务环境清理。已有输出仍保留。';
  } finally { if (generation === pageGeneration) pageLoading.value = false; }
}
const nextOutputPage = () => { if (outputPage.value && !outputPage.value.eof) void loadOutputPage(outputPage.value.next_cursor, 'next'); };
const previousOutputPage = () => { const cursor = previousCursors.value[previousCursors.value.length - 1]; if (cursor != null) void loadOutputPage(cursor, 'previous'); };
function resetOutputPage() {
  pageGeneration += 1; pageRequest?.abort(); pageLoading.value = false;
  outputPage.value = undefined; outputPageError.value = ''; previousCursors.value = [];
}
function returnToPreview() { resetOutputPage(); void loadShellContent(); startAutoRefresh(); }

const refreshTimer = ref<ReturnType<typeof setInterval> | null>(null);
const loadVersion = ref(0);

// Get shellSessionId from toolContent
const shellSessionId = computed(() => {
  if (props.toolContent && props.toolContent.args.id) {
    return props.toolContent.args.id;
  }
  return props.toolContent.content?.session_id || '';
});

const toolIdentity = computed(() => [
  props.sessionId,
  props.toolContent.tool_call_id,
  props.toolContent.status,
  props.live ? 'live' : 'snapshot',
].join(':'));

const shouldLoadLiveShellSession = computed(() => props.live && props.toolContent.function === 'shell_view');
// Older synthetic dataset/spill console records encoded success as 0/1.
// They are operation summaries, not evidence of a process exit.
const showProcessExit = computed(() => !props.toolContent.spill
  && !['dataset_analysis_run', 'dataset_quicklook', 'python_inspect'].includes(props.toolContent.function));

const escapeHtml = (value: unknown) => String(value ?? '')
  .replace(/&/g, '&amp;')
  .replace(/</g, '&lt;')
  .replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;')
  .replace(/'/g, '&#39;');

const updateShellContent = (console: any) => {
  if (!console) {
    shell.value = '';
    return;
  }

  if (typeof console === 'string') {
    shell.value = escapeHtml(sanitizeToolDisplayText(console));
    return;
  }

  if (!Array.isArray(console)) {
    shell.value = escapeHtml(sanitizeToolDisplayText(console?.output ?? ''));
    const exit = showProcessExit.value ? shellConsoleExitText(console) : '';
    if (exit) shell.value += `\n<span style="color: var(--text-tertiary);">${escapeHtml(exit)}</span>\n`;
    return;
  }

  let newShell = '';
  for (const e of console) {
    const prompt = escapeHtml(sanitizeToolDisplayText(e?.ps1));
    const command = escapeHtml(sanitizeToolDisplayText(e?.command));
    const output = escapeHtml(sanitizeToolDisplayText(e?.output));
    if (prompt || command) {
      newShell += `<span style="color: rgb(0, 187, 0);">${prompt}</span>${command ? `<span> ${command}</span>` : ''}\n`;
    }
    if (output) {
      newShell += `<span>${output}</span>\n`;
    }
    const exit = showProcessExit.value ? shellConsoleExitText(e) : '';
    if (exit) newShell += `<span style="color: var(--text-tertiary);">${escapeHtml(exit)}</span>\n`;
  }
  if (newShell !== shell.value) {
    shell.value = newShell;
  }
}

const showPendingShellContent = () => {
  const command = sanitizeToolDisplayText(props.toolContent.args?.command);
  shell.value = command
    ? `<span style="color: rgb(0, 187, 0);">$</span><span> ${escapeHtml(command)}</span>\n<span>Waiting for command output...</span>`
    : 'Waiting for command output...';
};

const showCommandSnapshotFallback = () => {
  const command = sanitizeToolDisplayText(props.toolContent.args?.command);
  if (!command) {
    shell.value = '';
    return;
  }
  const statusText = props.toolContent.status === 'called'
    ? '调用已结束；未提供命令输出或退出信息。'
    : 'Waiting for command output...';
  shell.value = `<span style="color: rgb(0, 187, 0);">$</span><span> ${escapeHtml(command)}</span>\n<span>${statusText}</span>`;
};

// Function to load Shell session content
const loadShellContent = async () => {
  if (outputPage.value || pageLoading.value) return;
  const version = ++loadVersion.value;
  if (!shouldLoadLiveShellSession.value) {
    outputMetadata.value = readShellOutputMetadata(props.toolContent.content?.output_metadata);
    updateShellContent(props.toolContent.content?.console ?? props.toolContent.content?.output);
    if (!shell.value) {
      showCommandSnapshotFallback();
    }
    return;
  }
  
  if (!shellSessionId.value) {
    shell.value = '';
    return;
  }

  try {
    const response = await viewShellSession(props.sessionId, shellSessionId.value);
    if (version !== loadVersion.value) return;
    outputMetadata.value = readShellOutputMetadata(response.output_metadata);
    updateShellContent(response.console?.length ? response.console : response.output);
    if (!shell.value && props.toolContent.status === 'calling') {
      showPendingShellContent();
    }
  } catch (error) {
    if (version !== loadVersion.value) return;
    console.error("Failed to load shell content:", error);
  }
};

// Start auto-refresh timer
const startAutoRefresh = () => {
  if (refreshTimer.value) {
    clearInterval(refreshTimer.value);
  }
  
  if (shouldLoadLiveShellSession.value && shellSessionId.value) {
    refreshTimer.value = setInterval(() => {
      loadShellContent();
    }, 5000);
  }
};

// Stop auto-refresh timer
const stopAutoRefresh = () => {
  if (refreshTimer.value) {
    clearInterval(refreshTimer.value);
    refreshTimer.value = null;
  }
};

watch(toolIdentity, () => {
  resetOutputPage(); outputMetadata.value = undefined;
  shell.value = '';
  loadVersion.value += 1;
  loadShellContent();
  startAutoRefresh();
});

watch(() => props.toolContent.timestamp, () => {
  loadShellContent();
});

// Watch for live prop changes
watch(() => props.live, (live: boolean) => {
  if (live && shouldLoadLiveShellSession.value) {
    loadShellContent();
    startAutoRefresh();
  } else {
    loadShellContent();
    stopAutoRefresh();
  }
});

// Load content and set up refresh timer when component is mounted
onMounted(() => {
  loadShellContent();
  startAutoRefresh();
});

// Clear timer when component is unmounted
onUnmounted(() => {
  resetOutputPage(); loadVersion.value += 1;
  stopAutoRefresh();
});
</script>
