<template>
  <Popover v-model:open="open">
    <PopoverTrigger as-child>
      <button type="button" class="history-trigger" aria-label="历史分析" title="历史分析">
        <History :size="17" /><span>历史分析</span><ChevronDown :size="13" class="history-chevron" />
      </button>
    </PopoverTrigger>
    <PopoverContent align="end" class="history-popover">
      <div class="history-heading">
        <h2>历史分析</h2>
        <button type="button" class="history-icon" aria-label="刷新历史分析" :disabled="loading" @click="loadSessions"><RefreshCw :size="15" :class="{ spinning: loading }" /></button>
        <button type="button" class="history-new" @click="newAnalysis"><SquarePen :size="14" />新建分析</button>
      </div>
      <div class="history-list">
        <p v-if="loading && !sessions.length" role="status" class="history-state">正在读取分析任务…</p>
        <div v-else-if="error" role="alert" class="history-state">
          <p>{{ error }}</p><button type="button" class="history-retry" @click="loadSessions">重试</button>
        </div>
        <p v-else-if="!sessions.length" class="history-state">还没有分析任务</p>
        <div v-for="session in sortedSessions" :key="session.session_id" class="history-item" :class="{ current: route.params.sessionId === session.session_id }">
          <div v-if="renamingId === session.session_id" class="history-rename">
            <input v-model="renameDraft" aria-label="分析名称" maxlength="200" @keydown.enter.prevent="saveRename(session)" @keydown.esc.prevent="renamingId = null" />
            <button type="button" :disabled="saving" @click="saveRename(session)">保存</button>
            <button type="button" :disabled="saving" @click="renamingId = null">取消</button>
          </div>
          <template v-else>
            <RouterLink :to="`/chat/${encodeURIComponent(session.session_id)}`" class="history-task" :aria-label="`继续分析：${session.title || '未命名分析'}`">
              <span class="history-title">{{ session.title || '未命名分析' }}</span>
              <span class="history-meta">
                <span :class="{ running: session.status === SessionStatus.RUNNING }">{{ statusLabel(session.status) }}</span>
                <span v-if="session.latest_message_at">{{ formatDate(session.latest_message_at) }}</span>
                <span v-if="session.unread_message_count > 0" class="history-unread">{{ session.unread_message_count > 99 ? '99+' : session.unread_message_count }} 条未读</span>
              </span>
            </RouterLink>
            <div class="history-actions">
              <button v-if="session.is_owner" type="button" class="history-icon" :aria-label="`重命名分析：${session.title || '未命名分析'}`" @click="beginRename(session)"><Pencil :size="14" /></button>
              <button type="button" class="history-icon" :aria-label="`删除分析：${session.title || '未命名分析'}`" @click="confirmDelete(session)"><Trash2 :size="14" /></button>
            </div>
          </template>
        </div>
      </div>
    </PopoverContent>
  </Popover>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { ChevronDown, History, Pencil, RefreshCw, SquarePen, Trash2 } from 'lucide-vue-next';
import { Popover, PopoverContent, PopoverTrigger } from './ui/popover';
import { deleteSession, getSessions, updateSessionTitle } from '../api/agent';
import { useAuth } from '../composables/useAuth';
import { useDialog } from '../composables/useDialog';
import { useNewAnalysis } from '../composables/useNewAnalysis';
import { EVENT_REFRESH_SESSION_LIST, EVENT_SESSION_RENAMED } from '../constants/event';
import { eventBus } from '../utils/eventBus';
import { showErrorToast } from '../utils/toast';
import { SessionStatus, type ListSessionItem } from '../types/response';

const route = useRoute();
const router = useRouter();
const { currentUser } = useAuth();
const { showConfirmDialog } = useDialog();
const { startNewAnalysis } = useNewAnalysis();
const open = ref(false);
const sessions = ref<ListSessionItem[]>([]);
const loading = ref(false);
const error = ref('');
const renamingId = ref<string | null>(null);
const renameDraft = ref('');
const saving = ref(false);
const sortedSessions = computed(() => [...sessions.value].sort((a, b) => (b.latest_message_at ?? 0) - (a.latest_message_at ?? 0)));
let requestVersion = 0;
let active = true;

async function loadSessions() {
  if (!currentUser.value || !open.value) return;
  const version = ++requestVersion;
  loading.value = true;
  error.value = '';
  try {
    const response = await getSessions();
    if (active && version === requestVersion) sessions.value = response.sessions;
  } catch {
    if (active && version === requestVersion) error.value = '暂时无法读取分析任务，请重试。';
  } finally {
    if (active && version === requestVersion) loading.value = false;
  }
}

function updateTitle(payload: unknown) {
  if (!payload || typeof payload !== 'object' || !('sessionId' in payload) || !('title' in payload)
    || typeof payload.sessionId !== 'string' || typeof payload.title !== 'string') return;
  const session = sessions.value.find(item => item.session_id === payload.sessionId);
  if (session) session.title = payload.title;
}

function beginRename(session: ListSessionItem) {
  renamingId.value = session.session_id;
  renameDraft.value = session.title || '未命名分析';
}

async function saveRename(session: ListSessionItem) {
  const title = renameDraft.value.trim();
  if (!title || saving.value) return;
  const owner = currentUser.value?.id;
  saving.value = true;
  try {
    await updateSessionTitle(session.session_id, title);
    if (!active || currentUser.value?.id !== owner) return;
    session.title = title;
    renamingId.value = null;
    eventBus.emit(EVENT_SESSION_RENAMED, { sessionId: session.session_id, title });
  } catch {
    if (active && currentUser.value?.id === owner) showErrorToast('重命名失败，请重试。');
  } finally {
    if (active && currentUser.value?.id === owner) saving.value = false;
  }
}

function confirmDelete(session: ListSessionItem) {
  const owner = currentUser.value?.id;
  open.value = false;
  showConfirmDialog({
    title: '删除这次分析？',
    content: '删除后无法恢复此分析的对话记录。',
    confirmText: '删除',
    cancelText: '取消',
    confirmType: 'danger',
    onConfirm: async () => {
      if (currentUser.value?.id !== owner) return;
      try {
        await deleteSession(session.session_id);
        if (currentUser.value?.id !== owner) return;
        sessions.value = sessions.value.filter(item => item.session_id !== session.session_id);
        eventBus.emit(EVENT_REFRESH_SESSION_LIST);
        if (route.params.sessionId === session.session_id) await router.push('/chat');
      } catch {
        if (currentUser.value?.id === owner) showErrorToast('删除失败，请重试。');
      }
    },
  });
}

function newAnalysis() { open.value = false; void startNewAnalysis(); }
function statusLabel(status: SessionStatus) {
  return { pending: '准备中', running: '分析中', waiting: '等待输入', completed: '已完成' }[status] || '分析任务';
}
function formatDate(timestamp: number) {
  return new Date(timestamp * 1000).toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

watch(open, value => { if (value) void loadSessions(); else renamingId.value = null; });
watch(() => route.fullPath, () => { open.value = false; });
watch(() => currentUser.value?.id, () => {
  requestVersion += 1;
  open.value = false;
  sessions.value = [];
  loading.value = false;
  saving.value = false;
  error.value = '';
  renamingId.value = null;
}, { flush: 'sync' });
onMounted(() => {
  eventBus.on(EVENT_REFRESH_SESSION_LIST, loadSessions);
  eventBus.on(EVENT_SESSION_RENAMED, updateTitle);
});
onUnmounted(() => {
  active = false;
  requestVersion += 1;
  eventBus.off(EVENT_REFRESH_SESSION_LIST, loadSessions);
  eventBus.off(EVENT_SESSION_RENAMED, updateTitle);
});
</script>

<style scoped>
.history-trigger { display: inline-flex; align-items: center; gap: 6px; height: 32px; padding: 0 8px; border-radius: 6px; white-space: nowrap; font-size: 13px; color: var(--text-secondary); }
.history-trigger:hover, .history-icon:hover, .history-new:hover { background: var(--fill-tsp-white-light); }
.history-trigger:focus-visible, .history-icon:focus-visible, .history-new:focus-visible { outline: 2px solid #6b927f; outline-offset: 2px; }
:global([data-slot="popover-content"].history-popover) { width: min(380px, calc(100vw - 24px)); padding: 0; border: 1px solid var(--border-main); border-radius: 10px; background: var(--background-menu-white); color: var(--text-primary); box-shadow: 0 8px 28px var(--shadow-S); }
.history-heading { display: flex; align-items: center; gap: 6px; padding: 10px 12px; border-bottom: 1px solid var(--border-main); }
.history-heading h2 { margin-right: auto; font-size: 13px; font-weight: 600; }
.history-icon { display: flex; flex-shrink: 0; align-items: center; justify-content: center; width: 28px; height: 28px; border-radius: 5px; color: var(--text-tertiary); }
.history-new { display: flex; align-items: center; gap: 5px; padding: 6px 8px; border-radius: 5px; font-size: 12px; color: var(--text-secondary); }
.history-list { max-height: min(480px, 65dvh); overflow-y: auto; padding: 6px; }
.history-state { padding: 28px 12px; text-align: center; font-size: 13px; color: var(--text-tertiary); }
.history-retry { margin-top: 8px; color: #2b7659; text-decoration: underline; }
.history-item { display: flex; align-items: center; min-width: 0; border: 1px solid transparent; border-radius: 7px; }
.history-item:hover { background: var(--fill-tsp-white-light); }
.history-item.current { border-color: #9bbdad; background: rgb(43 118 89 / 6%); }
.history-task { display: flex; flex: 1; flex-direction: column; gap: 5px; min-width: 0; padding: 10px; }
.history-title { overflow: hidden; white-space: nowrap; text-overflow: ellipsis; font-size: 13px; }
.history-meta { display: flex; flex-wrap: wrap; gap: 8px; font-size: 10px; color: var(--text-tertiary); }
.running, .history-unread { color: #2b7659; }
.history-actions { display: flex; padding-right: 5px; }
.history-rename { display: flex; flex: 1; gap: 6px; padding: 10px; font-size: 12px; }
.history-rename input { flex: 1; min-width: 0; padding: 5px 6px; border: 1px solid var(--border-btn-main); border-radius: 4px; background: transparent; }
.history-rename button { flex-shrink: 0; }
:global(.dark) .running, :global(.dark) .history-unread, :global(.dark) .history-retry { color: #a9cbbb; }
.spinning { animation: spin 1s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
@container analysis-data-layout (max-width: 760px) {
  .history-trigger { width: 32px; padding: 0; justify-content: center; }
  .history-trigger span, .history-chevron { display: none; }
}
</style>
