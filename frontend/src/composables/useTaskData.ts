import { computed, ref } from 'vue';
import { getTaskData, type TaskDataItem, type TaskDataTab } from '../api/taskData';
import type { FileInfo } from '../api/file';
import { authSessionKey } from '../utils/authTokens';
import { eventBus } from '../utils/eventBus';

// The private chat owns activation. File previews and the panel share this
// state, but neither may carry a previous conversation or account forward.
const sessionId = ref<string>();
const items = ref<TaskDataItem[]>([]);
const selectedIds = ref<string[]>([]);
const locked = ref(true);
const panelOpen = ref(false);
const previewFromTaskData = ref(false);
const tab = ref<TaskDataTab>('all');
const loading = ref(false), error = ref(''), actionError = ref('');
const unavailableCount = ref(0);
let generation = 0;
let owner: string | null = null;
let request: AbortController | undefined;
let serverItems: TaskDataItem[] = [];
const draftFiles = new Map<string, FileInfo>();
const active = computed(() => Boolean(sessionId.value));
const count = computed(() => items.value.length);
const counts = computed(() => ({ all: items.value.length,
  upload: items.value.filter(item => item.category === 'upload').length,
  analysis: items.value.filter(item => item.category === 'analysis').length,
  download: items.value.filter(item => item.category === 'download').length }));

async function refresh(): Promise<void> {
  if (!sessionId.value) return;
  request?.abort();
  const controller = new AbortController(); request = controller;
  const version = generation, id = sessionId.value, account = owner;
  loading.value = true;
  try {
    const draftIds = [...draftFiles.keys()];
    const batches = Array.from({ length: Math.ceil(draftIds.length / 32) }, (_, index) => draftIds.slice(index * 32, index * 32 + 32));
    // Each request validates draft references against the authenticated owner.
    // For many historical uploads, include a baseline to avoid double-counting
    // unavailable session files in the repeated full snapshots.
    const responses = await Promise.all((batches.length > 1 ? [[], ...batches] : batches.length ? batches : [[]])
      .map(ids => getTaskData(id, ids, controller.signal)));
    if (version !== generation || controller.signal.aborted || request !== controller || account !== authSessionKey()) return;
    // Server supplies one row per id; defensively preserve this invariant if a
    // response ever contains repeated references from several analysis rounds.
    serverItems = [...new Map(responses.flatMap(response => response.items).filter(item => item.file?.file_id && !item.file.file_id.startsWith('temp-'))
      .map(item => [item.file.file_id, item])).values()];
    items.value = serverItems;
    const baseline = responses[0]?.unavailable_count || 0;
    unavailableCount.value = batches.length > 1 ? baseline + responses.slice(1).reduce((sum, response) => sum + Math.max(0, response.unavailable_count - baseline), 0) : baseline;
    error.value = '';
    // response.selected_file_ids describes the server snapshot. Never let a
    // late refresh overwrite the current unsent input selection from ChatPage.
  } catch {
    if (version === generation && !controller.signal.aborted && account === authSessionKey()) error.value = '暂时无法读取任务数据，请重试。';
  } finally {
    if (version === generation && request === controller) { loading.value = false; request = undefined; }
  }
}
async function activate(id?: string): Promise<void> {
  const next = id?.trim() || undefined, account = authSessionKey();
  if (next === sessionId.value && owner === account) return;
  generation += 1; request?.abort(); request = undefined;
  owner = account; sessionId.value = next;
  serverItems = []; draftFiles.clear(); items.value = []; selectedIds.value = []; locked.value = true;
  panelOpen.value = false; previewFromTaskData.value = false; tab.value = 'all';
  loading.value = false; error.value = ''; actionError.value = ''; unavailableCount.value = 0;
  if (next) await refresh();
}
function syncSelection(id: string, ids: string[], isLocked: boolean, uploadedFiles: FileInfo[] = []) {
  if (id !== sessionId.value || owner !== authSessionKey()) return;
  let added = false;
  for (const file of uploadedFiles) if (file.file_id && !file.file_id.startsWith('temp-')) {
    if (!draftFiles.has(file.file_id)) added = true;
    draftFiles.set(file.file_id, file);
  }
  // Metadata supplied by a draft never establishes access or saved status.
  // Only the verified server projection becomes visible in the panel.
  if (added) void refresh();
  selectedIds.value = [...new Set(ids)]; locked.value = isLocked; actionError.value = '';
}
function open(nextTab: TaskDataTab = tab.value): boolean {
  if (!sessionId.value || owner !== authSessionKey()) return false;
  tab.value = nextTab; previewFromTaskData.value = false; panelOpen.value = true;
  eventBus.emit('task-data-panel-show', { sessionId: sessionId.value, tab: nextTab });
  void refresh();
  return true;
}
function close() {
  panelOpen.value = false; previewFromTaskData.value = false;
  eventBus.emit('task-data-panel-close', { sessionId: sessionId.value });
}
function suspend() { panelOpen.value = false; }
function changeInputSelection(files: FileInfo[], selected: boolean): boolean {
  if (!sessionId.value || locked.value || owner !== authSessionKey()) return false;
  const known = new Map(items.value.filter(item => item.available).map(item => [item.file.file_id, item.file]));
  const ready = [...new Set(files.map(file => file.file_id))].map(id => known.get(id)).filter((file): file is FileInfo => Boolean(file));
  if (!ready.length || ready.length !== new Set(files.map(file => file.file_id)).size) return false;
  if (selected && new Set([...selectedIds.value, ...ready.map(file => file.file_id)]).size > 32) {
    actionError.value = '本轮最多使用 32 个文件，请先移出部分资料。'; return false;
  }
  actionError.value = '';
  eventBus.emit('task-data-input-change', { sessionId: sessionId.value, files: ready, selected });
  return true;
}
function cleared() {
  // Force invalidation even if the route has not left the previous session.
  generation += 1; request?.abort(); request = undefined;
  sessionId.value = undefined; owner = null; serverItems = []; draftFiles.clear(); items.value = []; selectedIds.value = [];
  locked.value = true; panelOpen.value = false; previewFromTaskData.value = false;
  loading.value = false; error.value = ''; actionError.value = ''; unavailableCount.value = 0; tab.value = 'all';
}
eventBus.on('auth:cleared', cleared);
if (typeof window !== 'undefined') {
  window.addEventListener('data-library-changed', () => { if (sessionId.value) void refresh(); });
  window.addEventListener('datalab:auth-expired', cleared);
}
export function useTaskData() {
  return { sessionId, active, items, selectedIds, locked, panelOpen, previewFromTaskData, tab, loading, error, actionError,
    unavailableCount, count, counts, activate, refresh, syncSelection, open, close, suspend, changeInputSelection };
}
