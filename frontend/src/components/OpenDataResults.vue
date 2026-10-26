<template>
  <section class="open-data-card" aria-label="开放数据查找">
    <p v-if="isShare" class="open-data-note">开放数据获取记录仅对会话所有者可见。</p>
    <template v-else>
      <header class="open-data-heading">
        <Search :size="18" class="shrink-0" />
        <div class="min-w-0 flex-1"><p class="open-data-eyebrow">{{ search?.parent_id ? '细化后的查找 · 之前的结果已保留' : '查找开放数据' }}</p><h3 class="break-words">{{ search?.query || '正在读取查找记录…' }}</h3></div>
        <LoaderCircle v-if="searchPending || loading && !search" :size="17" class="shrink-0 animate-spin" />
      </header>
      <div class="open-data-body">
        <p v-if="connectionError" role="status" class="open-data-warning">{{ connectionError }} <button type="button" class="underline" :disabled="loading" @click="refresh">重新获取状态</button></p>
        <template v-if="search">
          <p v-if="searchStatusMessage" role="status" class="open-data-note">{{ searchStatusMessage }}</p>
          <div class="open-data-toolbar"><span v-if="verifiedCandidates.length" class="open-data-note">{{ verifiedCandidates.length }} 个可获取的数据集</span><button type="button" class="open-data-link" @click="refine">在输入框细化要求</button></div>
          <article v-for="candidate in visibleCandidates" :key="candidate.id" class="open-data-candidate">
            <div class="flex flex-wrap items-start justify-between gap-2"><h4 class="min-w-0 flex-1 font-medium break-words">{{ candidate.title }}</h4><span class="open-data-badge">可获取 · {{ candidateFileCount(candidate) }} 个文件</span></div>
            <p v-if="candidate.relevance_reason?.trim()" class="open-data-note open-data-relevance" :title="candidate.relevance_reason">匹配依据：{{ candidate.relevance_reason.trim() }}</p>
            <p class="open-data-note mt-1">{{ sourceTitle(candidate.source_id) }}<span v-if="candidate.external_id"> · {{ candidate.external_id }}</span></p>
            <p v-if="candidate.description" class="open-data-description">{{ candidate.description }}</p>
            <dl class="open-data-facts">
              <div><dt>格式</dt><dd>{{ candidate.formats?.join(' / ') || '待确认' }}</dd></div>
              <div><dt>{{ resolutions[candidate.id] ? '可获取文件大小' : '已确认文件大小' }}</dt><dd>{{ candidateSizeLabel(candidate) }}</dd></div>
              <div v-for="fact in openDataCoverageFacts(candidate.coverage)" :key="fact.label"><dt>{{ fact.label }}</dt><dd>{{ fact.value }}</dd></div>
              <div v-if="!Object.keys(candidate.coverage || {}).length"><dt>覆盖范围</dt><dd>来源未提供</dd></div>
            </dl>
            <details class="open-data-details open-data-source-notes">
              <summary>来源说明</summary>
              <p>来自：{{ sourceTitle(candidate.source_id) }}</p>
              <p>许可：{{ candidate.license || '来源未声明' }}</p>
              <p v-if="candidate.description">{{ candidate.description }}</p>
              <p v-for="note in uniqueNotes(candidate.limitations)" :key="note">{{ note }}</p>
            </details>
            <div class="open-data-actions">
              <button type="button" class="open-data-primary" :disabled="Boolean(actionBusy) || candidatePending(candidate.id)" @click="prepare(candidate)"><Download :size="14" />{{ actionBusy === `resolve:${candidate.id}` ? '正在读取文件…' : candidatePending(candidate.id) ? '正在获取数据' : '获取数据' }}</button>
              <button v-if="actionBusy === `resolve:${candidate.id}`" type="button" class="open-data-link" @click="cancelPreparation(candidate.id)">取消读取</button>
              <a v-if="openDataSourceUrl(candidate.source_url)" :href="openDataSourceUrl(candidate.source_url)" target="_blank" rel="noopener noreferrer" class="open-data-link inline-flex items-center gap-1">查看原始来源<ExternalLink :size="12" /></a>
            </div>

            <div v-if="expandedFiles === candidate.id && resolutions[candidate.id]" class="open-data-file-selection">
              <p class="font-medium text-sm">选择本次需要的文件</p>
              <div class="open-data-selection-toolbar">
                <p class="open-data-note">可获取 {{ availableFiles(candidate.id).length }} 个文件 · 已选 {{ selectedFiles(candidate.id).length }} 个</p>
                <div class="flex gap-3">
                  <button type="button" class="open-data-link" :disabled="Boolean(actionBusy) || candidatePending(candidate.id) || selectedFiles(candidate.id).length === availableFiles(candidate.id).length" @click="selectFiles(candidate.id, true)">全选</button>
                  <button type="button" class="open-data-link" :disabled="Boolean(actionBusy) || candidatePending(candidate.id) || !selectedFiles(candidate.id).length" @click="selectFiles(candidate.id, false)">清空</button>
                </div>
              </div>
              <p v-if="selectedFiles(candidate.id).length" class="open-data-note mt-1">已选文件大小：{{ openDataFilesSizeLabel(selectedFiles(candidate.id)) }}</p>
              <p v-if="unresolvedFilesLabel(candidate.id)" class="open-data-note mt-1">{{ unresolvedFilesLabel(candidate.id) }}<button v-if="resolutions[candidate.id]?.verification_pending_count" type="button" class="open-data-link ml-2" :disabled="Boolean(actionBusy) || candidatePending(candidate.id)" @click="prepare(candidate, true)">重试读取</button></p>
              <div class="max-h-72 overflow-y-auto mt-2">
                <label v-for="file in availableFiles(candidate.id)" :key="file.id" class="open-data-file">
                  <input v-model="selected[candidate.id]" type="checkbox" :value="file.id" :disabled="!file.available || Boolean(actionBusy) || candidatePending(candidate.id)" :aria-label="`选择 ${file.filename}`" />
                  <span class="min-w-0"><strong class="block break-all font-normal">{{ file.filename }}</strong><span class="open-data-note">{{ file.kind || file.content_type || '格式待确认' }} · {{ sizeLabel(file.size) }}<template v-if="file.required"> · 必要配套文件</template></span><span v-if="file.reason" class="block open-data-note">{{ file.reason }}</span></span>
                </label>
              </div>
              <p v-if="!resolutions[candidate.id]?.files.length" class="open-data-note my-2">尚未解析出可获取文件，请查看来源说明或选择其他候选。</p>
              <p v-if="selected[candidate.id]?.length && selectionIssue(candidate.id)" role="status" class="open-data-warning">{{ selectionIssue(candidate.id) }}</p>
              <button type="button" class="open-data-primary mt-3" :disabled="Boolean(actionBusy) || candidatePending(candidate.id) || Boolean(selectionIssue(candidate.id))" @click="acquire(candidate.id)">获取数据（{{ selectedFiles(candidate.id).length }} 个文件）</button>
            </div>

          </article>
          <button v-if="hasHiddenResults" type="button" class="open-data-secondary w-full mt-3" @click="visibleCount += 5">显示更多结果（当前显示 {{ visibleCandidates.length }} / {{ verifiedCandidates.length }}）</button>
          <button v-if="!searchPending && search.sources.some(source => source.status === 'failed')" type="button" class="open-data-secondary w-full mt-3" :disabled="Boolean(actionBusy)" @click="loadMore">重试未完成来源</button>
          <button v-if="canContinueSearch" type="button" class="open-data-secondary w-full mt-3" :disabled="Boolean(actionBusy) || searchPending" @click="loadMore">{{ actionBusy === 'more' ? '正在继续查找…' : '继续查找' }}</button>
        </template>
        <div v-if="jobs.length" class="open-data-receipt" aria-label="数据获取状态">
          <span role="status">{{ acquisitionSummary }}</span>
          <button type="button" class="open-data-link" @click="viewAcquisitions">{{ receiptAction }}</button>
        </div>
        <p v-if="actionError" role="alert" class="open-data-warning">{{ actionError }}</p>
      </div>
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue';
import { Download, ExternalLink, LoaderCircle, Search } from 'lucide-vue-next';
import { useDataAcquisition } from '../composables/useDataAcquisition';
import { acquireOpenData, continueOpenDataSearch, getOpenDataSearch, listOpenDataJobs, resolveOpenDataCandidate, type OpenDataCandidate, type OpenDataJob, type OpenDataResolution, type OpenDataSearch } from '../api/openData';
import { isVerifiedOpenDataCandidate, openDataSelectionIssue, openDataSizeLabel, openDataSourceUrl, openDataAvailableFiles, openDataFilesSizeLabel, openDataCoverageFacts } from '../utils/openData';
import { eventBus } from '../utils/eventBus';

const props = defineProps<{ searchId: string; sessionId?: string; isShare?: boolean; initialSearch?: OpenDataSearch }>();
const search = ref<OpenDataSearch>();
const acquisition = useDataAcquisition();
const jobs = computed(() => props.isShare ? [] : acquisition.jobs.value.filter(job => job.session_id === props.sessionId && job.search_id === props.searchId));
const resolutions = ref<Record<string, OpenDataResolution>>({});
const selected = ref<Record<string, string[]>>({});
const expandedFiles = ref('');
const visibleCount = ref(5);
const unavailableCandidates = ref(new Map<string, string | null | undefined>());
// The service owns relevance ordering across sources; filtering must preserve that order.
const verifiedCandidates = computed(() => (search.value?.results || [])
  .filter(candidate => isVerifiedOpenDataCandidate(candidate)
    && (!unavailableCandidates.value.has(candidate.id) || unavailableCandidates.value.get(candidate.id) !== candidate.availability_verified_at)));
const visibleCandidates = computed(() => verifiedCandidates.value.slice(0, visibleCount.value));
const loading = ref(false), connectionError = ref(''), actionError = ref(''), actionBusy = ref('');
const searchPending = computed(() => Boolean(search.value && ['queued', 'searching'].includes(search.value.status)));
const relevancePendingCount = computed(() => Math.max(0, Math.floor(search.value?.relevance_pending_count || 0)));
const hasHiddenResults = computed(() => verifiedCandidates.value.length > visibleCount.value);
const canContinueSearch = computed(() => !hasHiddenResults.value && Boolean(search.value?.has_more || relevancePendingCount.value));
const searchStatusMessage = computed(() => {
  if (!search.value) return '';
  if (relevancePendingCount.value) return searchPending.value
    ? `正在确认 ${relevancePendingCount.value} 个候选数据集与需求的相关性…`
    : `有 ${relevancePendingCount.value} 个候选数据集待确认相关性，可继续查找。`;
  if (searchPending.value) return search.value.status === 'queued' ? '正在等待查找数据…' : '正在查找相关且可获取的数据…';
  return verifiedCandidates.value.length ? '' : '未找到与该需求相关且当前可获取的数据，可调整要求继续查找。';
});
const isJobPending = (job: OpenDataJob) => ['queued', 'downloading'].includes(job.status);
const candidateJobs = (id: string) => jobs.value.filter(job => job.candidate_id === id);
const candidatePending = (id: string) => candidateJobs(id).some(isJobPending);
const pendingFileCount = computed(() => jobs.value.filter(isJobPending).reduce((count, job) => count + job.items.filter(item => ['queued', 'downloading'].includes(item.status)).length, 0));
const savedFileCount = computed(() => new Set(jobs.value.flatMap(job => job.files.map(file => file.file_id))).size);
const hasPendingJobs = computed(() => jobs.value.some(isJobPending));
const needsAttention = computed(() => jobs.value.some(job => ['failed', 'partial'].includes(job.status) || Boolean(job.error)));
const acquisitionSummary = computed(() => [
  hasPendingJobs.value ? pendingFileCount.value ? `正在获取 ${pendingFileCount.value} 个文件` : '正在获取数据' : '',
  savedFileCount.value ? `已保存 ${savedFileCount.value} 个文件` : '',
  needsAttention.value ? '有未完成项' : '',
].filter(Boolean).join(' · ') || '获取已取消');
const receiptAction = computed(() => hasPendingJobs.value || needsAttention.value ? '查看进度' : savedFileCount.value ? '查看文件' : '查看记录');
function viewAcquisitions() {
  if (props.isShare || !jobs.value.length) return;
  const job = jobs.value.find(isJobPending) || jobs.value.find(job => ['failed', 'partial'].includes(job.status)) || jobs.value[0];
  acquisition.open({ searchId: props.searchId, jobId: job?.id });
}
const sourceTitle = (id: string) => search.value?.sources.find(source => source.id === id)?.title || id;
const availableFiles = (id: string) => openDataAvailableFiles(resolutions.value[id]?.files || []);
const selectedFiles = (id: string) => {
  const ids = new Set(selected.value[id] || []);
  return availableFiles(id).filter(file => ids.has(file.id));
};
const candidateFileCount = (candidate: OpenDataCandidate) => resolutions.value[candidate.id] ? availableFiles(candidate.id).length : candidate.available_file_count;
const candidateSizeLabel = (candidate: OpenDataCandidate) => resolutions.value[candidate.id]
  ? openDataFilesSizeLabel(availableFiles(candidate.id)) : openDataSizeLabel(candidate.size);
function selectFiles(id: string, all: boolean) {
  if (!active || props.isShare || !props.sessionId || actionBusy.value || candidatePending(id)) return;
  selected.value[id] = all ? availableFiles(id).map(file => file.id) : [];
}
function unresolvedFilesLabel(id: string) {
  const result = resolutions.value[id];
  return [
    result?.verification_pending_count ? `还有 ${result.verification_pending_count} 个文件暂未确认` : '',
    result?.unavailable_file_count ? `${result.unavailable_file_count} 个文件当前不可获取` : '',
  ].filter(Boolean).join('；');
}
const uniqueNotes = (notes?: string[]) => [...new Set((notes || []).map(note => note.trim()).filter(Boolean))];
let generation = 0, failures = 0;
let active = true;
let request: AbortController | undefined;
let timer: ReturnType<typeof setTimeout> | undefined;
const actions = new Map<string, AbortController>();

function cancelPreparation(candidateId: string) {
  const name = `resolve:${candidateId}`;
  if (actionBusy.value === name) actions.get(name)?.abort();
}

const sizeLabel = openDataSizeLabel;
function selectionIssue(id: string) {
  return openDataSelectionIssue(availableFiles(id), selected.value[id] || []);
}

function schedule() {
  if (timer) clearTimeout(timer);
  if (active && !props.isShare && props.sessionId && (connectionError.value || searchPending.value || jobs.value.some(isJobPending))) {
    timer = setTimeout(() => void refresh(), Math.min(30000, 2500 * 2 ** failures));
  }
}
async function refresh() {
  if (!active || props.isShare || !props.sessionId || !props.searchId || actionBusy.value) return;
  if (timer) clearTimeout(timer);
  request?.abort();
  const controller = new AbortController(); request = controller;
  const version = generation, sessionId = props.sessionId, searchId = props.searchId;
  const revision = acquisition.captureRevision();
  loading.value = true;
  const results = await Promise.allSettled([getOpenDataSearch(sessionId, searchId, controller.signal), listOpenDataJobs(sessionId, searchId, controller.signal)]);
  if (!active || version !== generation || controller.signal.aborted) return;
  const [searchResult, jobResult] = results;
  if (searchResult.status === 'fulfilled' && searchResult.value.id === searchId && searchResult.value.session_id === sessionId
      && acquisition.acceptSearch(searchResult.value, { revision })) search.value = searchResult.value;
  if (jobResult.status === 'fulfilled') acquisition.acceptJobs(jobResult.value, { searchId, revision });
  if (results.some(result => result.status === 'rejected')) {
    failures += 1;
    connectionError.value = '暂时无法确认最新进度，已有结果仍保留，正在尝试重连。';
  } else { failures = 0; connectionError.value = ''; }
  loading.value = false;
  schedule();
}
async function action(name: string, perform: (sessionId: string, searchId: string, signal: AbortSignal) => Promise<() => void>): Promise<boolean> {
  if (!active || props.isShare || !props.sessionId || actionBusy.value) return false;
  request?.abort();
  if (timer) clearTimeout(timer);
  const controller = new AbortController(), version = generation;
  actions.set(name, controller); actionBusy.value = name; actionError.value = '';
  try {
    const apply = await perform(props.sessionId, props.searchId, controller.signal);
    if (!active || version !== generation || controller.signal.aborted) return false;
    apply();
    return true;
  } catch {
    if (active && version === generation && !controller.signal.aborted) actionError.value = '操作尚未确认完成，已有记录仍保留。请查看最新状态后重试。';
    return false;
  } finally {
    if (actions.get(name) === controller) actions.delete(name);
    if (active && version === generation) { actionBusy.value = ''; loading.value = false; void refresh(); }
  }
}
async function prepare(candidate: OpenDataCandidate, reread = false) {
  if (!active || props.isShare || actionBusy.value || !props.sessionId) return;
  if (!verifiedCandidates.value.some(item => item.id === candidate.id)) return;
  if (resolutions.value[candidate.id] && !reread) { expandedFiles.value = expandedFiles.value === candidate.id ? '' : candidate.id; return; }
  const version = generation;
  let preparationSignal: AbortSignal | undefined;
  const success = await action(`resolve:${candidate.id}`, async (sessionId, searchId, signal) => {
    preparationSignal = signal;
    const result = await resolveOpenDataCandidate(sessionId, searchId, candidate.id, signal);
    return () => {
      const available = openDataAvailableFiles(result.files);
      if (!available.length) {
        unavailableCandidates.value = new Map([...unavailableCandidates.value, [candidate.id, candidate.availability_verified_at]]);
        actionError.value = '这份数据当前没有可获取文件，已从结果中移除。已保存的历史文件仍可使用。';
        return;
      }
      resolutions.value[candidate.id] = { ...result, files: available };
      const previousSelection = new Set(selected.value[candidate.id] || []);
      selected.value[candidate.id] = reread
        ? available.filter(file => previousSelection.has(file.id)).map(file => file.id)
        : available.length === 1 ? [available[0]!.id] : [];
      expandedFiles.value = candidate.id;
    };
  });
  if (!success && active && version === generation && !preparationSignal?.aborted) {
    actionError.value = reread
      ? '暂时未能读取完整文件列表，已有结果仍保留，请重试读取。'
      : '暂时未能读取完整文件列表，已保留这份数据。请点击“获取数据”重试。';
  }
  // A complete single-file result needs no second confirmation; partial lists keep the choice visible.
  if (success && !reread && resolutions.value[candidate.id]?.files.length === 1
      && !resolutions.value[candidate.id]?.verification_pending_count && !selectionIssue(candidate.id)) await acquire(candidate.id);
}
async function acquire(candidateId: string) {
  if (!verifiedCandidates.value.some(candidate => candidate.id === candidateId)) return;
  const issue = selectionIssue(candidateId);
  if (issue) { actionError.value = issue; return; }
  if (candidatePending(candidateId)) return;
  const fileIds = selectedFiles(candidateId).map(file => file.id);
  await action(`acquire:${candidateId}`, async (sessionId, searchId, signal) => {
    const job = await acquireOpenData(sessionId, searchId, { candidate_id: candidateId, file_ids: fileIds }, signal);
    return () => {
      if (search.value) acquisition.acceptSearch(search.value);
      if (acquisition.acceptJobs([job], { searchId })) acquisition.open({ searchId, jobId: job.id, automatic: true });
      expandedFiles.value = '';
    };
  });
}
async function loadMore() {
  await action('more', async (sessionId, searchId, signal) => {
    const result = await continueOpenDataSearch(sessionId, searchId, signal);
    return () => { if (result.id === props.searchId && result.session_id === props.sessionId && acquisition.acceptSearch(result)) search.value = result; };
  });
}
function refine() {
  if (!search.value || props.isShare) return;
  eventBus.emit('open-data-refine', { sessionId: props.sessionId, searchId: props.searchId, query: search.value.query });
}
function handleOpenDataUpdated(payload: unknown) {
  const update = payload as { sessionId?: string; searchId?: string };
  if (update?.sessionId === props.sessionId && update.searchId === props.searchId) void refresh();
}
watch(() => acquisition.searches.value.find(record => !props.isShare && record.session_id === props.sessionId && record.id === props.searchId), record => {
  if (record) search.value = record;
});
watch(() => [props.sessionId, props.searchId, props.isShare] as const, () => {
  generation += 1; request?.abort(); actions.forEach(controller => controller.abort()); actions.clear();
  if (timer) clearTimeout(timer);
  failures = 0; resolutions.value = {}; selected.value = {}; expandedFiles.value = '';
  unavailableCandidates.value = new Map(); visibleCount.value = 5; actionBusy.value = ''; actionError.value = ''; connectionError.value = ''; loading.value = false;
  search.value = !props.isShare ? acquisition.searches.value.find(record => record.session_id === props.sessionId && record.id === props.searchId)
    || (props.initialSearch?.id === props.searchId && props.initialSearch.session_id === props.sessionId ? props.initialSearch : undefined) : undefined;
  if (search.value) acquisition.acceptSearch(search.value);
  void refresh();
}, { immediate: true });
eventBus.on('open-data-updated', handleOpenDataUpdated);
onUnmounted(() => { active = false; generation += 1; request?.abort(); actions.forEach(controller => controller.abort()); if (timer) clearTimeout(timer); eventBus.off('open-data-updated', handleOpenDataUpdated); });
</script>

<style scoped>
.open-data-card { border: 1px solid var(--border-main); border-radius: 14px; background: var(--background-white-main); overflow: hidden; margin: 8px 0; }
.open-data-heading { display: flex; align-items: start; gap: 10px; padding: 16px; border-bottom: 1px solid var(--border-main); color: var(--text-primary); }
.open-data-heading h3 { font-size: 14px; line-height: 1.7; font-weight: 600; }
.open-data-eyebrow { font-size: 11px; color: var(--text-secondary); margin-bottom: 3px; }
.open-data-body { padding: 12px 16px 16px; }
.open-data-note { font-size: 12px; line-height: 1.6; color: var(--text-secondary); }
.open-data-relevance { margin-top: 4px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.open-data-card > .open-data-note { padding: 14px; }
.open-data-badge { font-size: 11px; padding: 3px 7px; background: var(--fill-tsp-gray-main); border-radius: 5px; color: var(--text-secondary); }
.open-data-toolbar { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 10px; align-items: center; margin: 12px 0; }
.open-data-link { color: #2b7659; font-size: 12px; text-decoration: underline; text-underline-offset: 3px; }
.open-data-candidate { border-top: 1px solid var(--border-main); padding: 16px 0; }
.open-data-candidate:last-of-type { padding-bottom: 0; }
.open-data-description { font-size: 13px; line-height: 1.7; color: var(--text-secondary); margin: 10px 0; white-space: pre-wrap; overflow-wrap: anywhere; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.open-data-facts { display: flex; gap: 6px 20px; flex-wrap: wrap; margin: 10px 0; font-size: 12px; }
.open-data-facts div { max-width: 100%; display: flex; gap: 7px; }
.open-data-facts dt { color: var(--text-tertiary); flex-shrink: 0; }
.open-data-facts dd { overflow-wrap: anywhere; }
.open-data-notes { margin: 8px 0; font-size: 12px; line-height: 1.7; padding-left: 16px; list-style: disc; }
.open-data-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 14px; margin-top: 12px; }
.open-data-primary, .open-data-secondary { display: inline-flex; align-items: center; justify-content: center; gap: 6px; padding: 8px 12px; border-radius: 8px; font-size: 12px; }
.open-data-primary { background: #2b7659; color: white; }
.open-data-secondary { border: 1px solid var(--border-main); }
button:disabled { opacity: .5; cursor: not-allowed; }
button:focus-visible, a:focus-visible, summary:focus-visible { outline: 2px solid #2b7659; outline-offset: 3px; }
.open-data-selection-toolbar { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 8px 16px; margin-top: 8px; }
.open-data-file-selection { border: 1px solid var(--border-main); border-radius: 10px; padding: 12px; margin-top: 12px; }
.open-data-receipt { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 8px 16px; margin-top: 16px; padding-top: 12px; border-top: 1px solid var(--border-main); font-size: 12px; color: var(--text-secondary); }
.open-data-file { display: flex; align-items: start; gap: 9px; padding: 9px 0; font-size: 12px; border-top: 1px solid var(--border-light); }
.open-data-file input { margin-top: 3px; }
.open-data-warning { font-size: 12px; line-height: 1.6; color: #a15c16; margin: 8px 0; overflow-wrap: anywhere; }
.open-data-details { font-size: 12px; color: var(--text-secondary); margin: 10px 0; }
.open-data-details summary { cursor: pointer; }
.open-data-details p { margin-top: 5px; overflow-wrap: anywhere; }
</style>
