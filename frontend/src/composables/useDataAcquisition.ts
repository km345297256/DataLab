import { ref } from 'vue';
import { changeOpenDataJob, listOpenDataJobs, listOpenDataSearches, type OpenDataJob, type OpenDataSearch } from '../api/openData';
import { getFileDownloadUrl, type FileInfo } from '../api/file';
import { readyExplorationFiles } from '../utils/explorationFiles';
import { eventBus } from '../utils/eventBus';

// One private conversation owns this shared state. The private chat layout,
// rather than cards or the panel's mounting lifecycle, calls activate().
const sessionId = ref<string>();
const jobs = ref<OpenDataJob[]>([]);
const searches = ref<OpenDataSearch[]>([]);
const loading = ref(false), error = ref(''), actionBusy = ref(''), actionError = ref('');
const panelOpen = ref(false), focusedSearchId = ref(''), focusedJobId = ref(''), focusRevision = ref(0);
const preparedJobs = ref(new Set<string>()), downloadingFiles = ref(new Set<string>());
let generation = 0, revision = 0, sessionRevision = 0, failures = 0;
let userClosed = false, externalOccupied = false, automaticOpened = false;
let request: AbortController | undefined;
let timer: ReturnType<typeof setTimeout> | undefined;
let busyJobId = '';
const actions = new Set<AbortController>();
const jobRevisions = new Map<string, number>(), searchRevisions = new Map<string, number>();
const announcedAssets = new Set<string>();

type Snapshot = { revision?: number };
type JobSnapshot = Snapshot & { searchId?: string };
type PanelFocus = { searchId?: string; jobId?: string; automatic?: boolean };
const isPending = (job: OpenDataJob) => ['queued', 'downloading'].includes(job.status);
const captureRevision = () => revision;

function clearTimer() {
  if (timer) clearTimeout(timer);
  timer = undefined;
}

function schedule() {
  clearTimer();
  if (sessionId.value && !actionBusy.value && !loading.value && (error.value || jobs.value.some(isPending)
      || searches.value.some(search => ['queued', 'searching'].includes(search.status)))) {
    timer = setTimeout(() => void refresh(), Math.min(30000, 2500 * 2 ** Math.min(failures, 4)));
  }
}

function fresh(snapshot: Snapshot, previous?: number) {
  return snapshot.revision === undefined || (snapshot.revision >= sessionRevision && (previous ?? 0) <= snapshot.revision);
}

function olderServerRecord(incoming: { updated_at?: string | number }, current?: { updated_at?: string | number }) {
  const timestamp = (value?: string | number) => {
    if (typeof value === 'number') return value < 1e12 ? value * 1000 : value;
    // Mongo stores UTC but its default datetime decoder omits tzinfo. A POST
    // may still carry the original aware timestamp. Treat only bare ISO
    // datetimes as UTC so local browser timezone cannot reverse their order.
    const text = value || '';
    const utc = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?$/.test(text) ? `${text}Z` : text;
    return Date.parse(utc);
  };
  const received = timestamp(incoming.updated_at), previous = timestamp(current?.updated_at);
  return Number.isFinite(received) && Number.isFinite(previous) && received < previous;
}

function acceptSearch(search: OpenDataSearch, snapshot: Snapshot = {}): boolean {
  if (!sessionId.value || search.session_id !== sessionId.value || !fresh(snapshot, searchRevisions.get(search.id))) return false;
  const index = searches.value.findIndex(item => item.id === search.id);
  if (olderServerRecord(search, searches.value[index])) return false;
  searchRevisions.set(search.id, ++revision);
  if (index < 0) searches.value = [...searches.value, search];
  else searches.value = searches.value.map(item => item.id === search.id ? search : item);
  schedule();
  return true;
}

function acceptJobs(incoming: OpenDataJob[], snapshot: JobSnapshot = {}): boolean {
  if (!sessionId.value) return false;
  const merged = new Map(jobs.value.map(job => [job.id, job]));
  let accepted = false;
  for (const job of incoming) {
    if (job.session_id !== sessionId.value || (snapshot.searchId && job.search_id !== snapshot.searchId)
        || job.id === busyJobId || !fresh(snapshot, jobRevisions.get(job.id))) continue;
    const previous = merged.get(job.id);
    if ((previous && previous.search_id !== job.search_id) || olderServerRecord(job, previous)) continue;
    jobRevisions.set(job.id, ++revision);
    merged.set(job.id, job);
    accepted = true;
    if (job.asset_id && !announcedAssets.has(job.asset_id)) {
      announcedAssets.add(job.asset_id);
      if (typeof window !== 'undefined') window.dispatchEvent(new CustomEvent('data-library-changed'));
    }
  }
  if (accepted) jobs.value = [...merged.values()];
  schedule();
  return accepted;
}

async function refresh(): Promise<void> {
  if (!sessionId.value || actionBusy.value) return;
  clearTimer();
  request?.abort();
  const controller = new AbortController(); request = controller;
  const activeSession = sessionId.value, version = generation, snapshot = captureRevision();
  loading.value = true;
  const results = await Promise.allSettled([
    listOpenDataSearches(activeSession, controller.signal),
    listOpenDataJobs(activeSession, undefined, controller.signal),
  ]);
  if (version !== generation || controller.signal.aborted || request !== controller) return;
  request = undefined;
  const [searchResult, jobResult] = results;
  if (searchResult.status === 'fulfilled') {
    for (const search of searchResult.value) acceptSearch(search, { revision: snapshot });
  }
  if (jobResult.status === 'fulfilled') acceptJobs(jobResult.value, { revision: snapshot });
  if (results.some(result => result.status === 'rejected')) {
    failures += 1;
    error.value = '暂时无法确认最新进度，已有结果仍保留，正在尝试重连。';
  } else { failures = 0; error.value = ''; }
  loading.value = false;
  schedule();
}

async function activate(id?: string): Promise<void> {
  const next = id?.trim() || undefined;
  if (next === sessionId.value) return;
  generation += 1;
  sessionRevision = ++revision;
  request?.abort(); request = undefined;
  actions.forEach(controller => controller.abort()); actions.clear();
  clearTimer();
  sessionId.value = next;
  jobs.value = []; searches.value = [];
  jobRevisions.clear(); searchRevisions.clear(); announcedAssets.clear();
  preparedJobs.value = new Set(); downloadingFiles.value = new Set();
  loading.value = false; error.value = ''; actionBusy.value = ''; actionError.value = ''; busyJobId = '';
  panelOpen.value = false; focusedSearchId.value = ''; focusedJobId.value = ''; focusRevision.value = 0;
  userClosed = false; externalOccupied = false; automaticOpened = false; failures = 0;
  if (next) await refresh();
}

function open(focus: PanelFocus = {}): boolean {
  if (!sessionId.value || (focus.automatic && (userClosed || externalOccupied || (automaticOpened && !panelOpen.value)))) return false;
  if (!focus.automatic && focus.jobId) {
    focusedJobId.value = focus.jobId;
    focusedSearchId.value = jobs.value.find(job => job.id === focus.jobId)?.search_id || focus.searchId || '';
  } else if (!focus.automatic && focus.searchId) {
    focusedSearchId.value = focus.searchId;
    focusedJobId.value = '';
  } else if (!focus.automatic) {
    focusedSearchId.value = '';
    focusedJobId.value = '';
  }
  if (!focus.automatic) { userClosed = false; focusRevision.value += 1; }
  else automaticOpened = true;
  externalOccupied = false;
  panelOpen.value = true;
  eventBus.emit('open-data-panel-show', { sessionId: sessionId.value, searchId: focusedSearchId.value || undefined,
    jobId: focusedJobId.value || undefined, automatic: Boolean(focus.automatic) });
  return true;
}

function close() { userClosed = true; panelOpen.value = false; }
function externalPanelOpened() { externalOccupied = true; panelOpen.value = false; }
function externalPanelClosed() { externalOccupied = false; }

function currentJob(job: OpenDataJob): OpenDataJob | undefined {
  if (!sessionId.value || job.session_id !== sessionId.value) return undefined;
  return jobs.value.find(item => item.id === job.id && item.session_id === sessionId.value && item.search_id === job.search_id);
}

async function changeJob(job: OpenDataJob, operation: 'cancel' | 'retry'): Promise<boolean> {
  const current = currentJob(job);
  if (!current || actionBusy.value || (operation === 'cancel' ? !isPending(current) : !['failed', 'partial', 'cancelled'].includes(current.status))) return false;
  request?.abort(); request = undefined; clearTimer(); loading.value = false;
  const version = generation, activeSession = sessionId.value!, controller = new AbortController();
  busyJobId = current.id;
  jobRevisions.set(current.id, ++revision);
  actions.add(controller); actionBusy.value = `${operation}:${current.id}`; actionError.value = '';
  try {
    const updated = await changeOpenDataJob(activeSession, current.id, operation, controller.signal);
    if (version !== generation || controller.signal.aborted) return false;
    if (updated.id !== current.id || updated.session_id !== activeSession || updated.search_id !== current.search_id) throw new Error('invalid_job_response');
    busyJobId = '';
    acceptJobs([updated]);
    return true;
  } catch {
    if (version === generation && !controller.signal.aborted) actionError.value = '操作尚未确认完成，已有记录仍保留。请查看最新状态后重试。';
    return false;
  } finally {
    actions.delete(controller);
    if (version === generation) { busyJobId = ''; actionBusy.value = ''; void refresh(); }
  }
}

function useFiles(job: OpenDataJob, onAccepted?: () => void): boolean {
  const current = currentJob(job);
  if (!current) return false;
  const files = readyExplorationFiles(current.files), version = generation;
  if (!files.length) { actionError.value = '还没有已获取的文件，请等待获取完成后再加入分析。'; return false; }
  if (files.length > 32) { actionError.value = '文件已获取。每轮分析最多使用 32 个文件，请从我的数据选择本轮需要的文件。'; return false; }
  actionError.value = '';
  eventBus.emit('connector-analysis-draft', {
    sessionId: sessionId.value, files, query: searches.value.find(search => search.id === current.search_id)?.query, jobId: current.id,
    onAccepted: (accepted: boolean) => {
      if (version !== generation) return;
      if (accepted) {
        preparedJobs.value = new Set([...preparedJobs.value, current.id]);
        onAccepted?.();
      }
      else actionError.value = '本轮已有文件较多，请先移除部分输入后再添加。';
    },
  });
  return true;
}

async function downloadToLocal(job: OpenDataJob, file: FileInfo): Promise<boolean> {
  const current = currentJob(job), canonical = current?.files.find(item => item.file_id === file.file_id);
  if (!current || !canonical || downloadingFiles.value.has(canonical.file_id)) return false;
  const version = generation;
  downloadingFiles.value = new Set([...downloadingFiles.value, canonical.file_id]);
  actionError.value = '';
  try {
    const url = await getFileDownloadUrl(canonical);
    if (version !== generation) return false;
    const link = document.createElement('a');
    link.href = url; link.download = canonical.filename; link.target = '_blank'; link.rel = 'noopener noreferrer';
    document.body.appendChild(link); link.click(); link.remove();
    return true;
  } catch {
    if (version === generation) actionError.value = '下载链接暂时无法准备，请重试或从我的数据下载。';
    return false;
  } finally {
    if (version === generation) downloadingFiles.value = new Set([...downloadingFiles.value].filter(id => id !== canonical.file_id));
  }
}

eventBus.on('open-data-updated', (payload: unknown) => {
  const update = payload as { sessionId?: string } | undefined;
  if (sessionId.value && update?.sessionId === sessionId.value) void refresh();
});
eventBus.on('auth:cleared', () => { void activate(); });

export function useDataAcquisition() {
  return { sessionId, jobs, searches, loading, error, actionBusy, actionError, panelOpen, focusedSearchId, focusedJobId,
    focusRevision, preparedJobs, downloadingFiles, activate, refresh, captureRevision, acceptSearch, acceptJobs,
    open, close, externalPanelOpened, externalPanelClosed, changeJob, useFiles, downloadToLocal };
}
