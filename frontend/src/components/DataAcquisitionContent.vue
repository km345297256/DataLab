<template>
  <div ref="contentRef" class="acquisition-content" aria-label="当前会话的数据获取任务">
    <p v-if="isShare || !sessionId" class="acquisition-note">获取任务仅对当前会话所有者可见。</p>
    <template v-else>
      <p v-if="error" role="status" class="acquisition-warning">{{ error }} <button type="button" class="acquisition-link" :disabled="loading" @click="refresh">重新读取</button></p>
      <p v-if="loading && !currentJobs.length" role="status" class="acquisition-note">正在读取获取任务…</p>
      <p v-else-if="!currentJobs.length && !tasksOnly" class="acquisition-empty">还没有获取任务。在聊天中找到数据后，点击“获取数据”。</p>
      <section v-for="group in groups" :key="group.id" class="acquisition-group" :aria-label="group.label">
        <button v-if="group.id === 'completed'" type="button" class="acquisition-group-heading acquisition-collapse" :aria-expanded="completedExpanded" @click="completedExpanded = !completedExpanded">
          <span>{{ group.label }} <span class="acquisition-count">{{ group.jobs.length }} 个任务</span></span><ChevronDown :size="15" :class="{ 'is-collapsed': !completedExpanded }" />
        </button>
        <h3 v-else class="acquisition-group-heading">{{ group.label }} <span class="acquisition-count">{{ group.jobs.length }} 个任务</span></h3>
        <div v-show="group.id !== 'completed' || completedExpanded">
          <article v-for="job in group.jobs" :key="job.id" :data-acquisition-job-id="job.id" :data-acquisition-search-id="job.search_id" class="acquisition-job" tabindex="-1">
            <div class="acquisition-job-heading"><h4>{{ jobTitle(job) }}</h4><span class="acquisition-state" :class="{ 'is-warning': ['failed', 'partial'].includes(job.status) }">{{ jobLabel(job.status) }}</span></div>
            <p v-if="jobQuery(job) && jobQuery(job) !== jobTitle(job)" class="acquisition-query">{{ jobQuery(job) }}</p>
            <p v-if="job.items.length" class="acquisition-note">已获取 {{ job.items.filter(item => item.status === 'ready').length }} / {{ job.items.length }} 个文件</p>
            <p v-if="job.asset_id" class="acquisition-note">已保存到我的数据</p>
            <p v-if="job.error" role="status" class="acquisition-warning">{{ job.error }}</p>
            <ul v-if="visibleJobItems(job).length" class="acquisition-items">
              <li v-for="item in visibleJobItems(job)" :key="item.id" class="acquisition-item">
                <div class="acquisition-file-name">{{ item.filename }}</div>
                <p class="acquisition-note">{{ itemLabel(item.status, job.status) }}<template v-if="item.status === 'downloading' || item.bytes_received"> · {{ openDataProgressLabel(item.bytes_received, item.size) }}</template></p>
                <div v-if="isPending(job) && item.status === 'downloading' && item.size && item.size > 0" class="acquisition-progress" role="progressbar" :aria-label="`${item.filename} 获取进度`" :aria-valuenow="progressPercent(item)" aria-valuemin="0" aria-valuemax="100"><span :style="{ width: `${progressPercent(item)}%` }" /></div>
                <p v-if="item.message" class="acquisition-item-message" :class="item.status === 'failed' ? 'acquisition-warning' : 'acquisition-note'">{{ item.message }}</p>
              </li>
            </ul>
            <ul v-if="!tasksOnly && savedFiles(job).length" class="acquisition-files" aria-label="已获取文件">
              <li v-for="file in savedFiles(job)" :key="file.file_id">
                <button type="button" class="acquisition-file-preview" :aria-label="`预览 ${file.filename}`" @click="previewFile(job, file)"><FileText :size="16" /><span class="acquisition-file-name">{{ file.filename }}</span></button>
                <div class="acquisition-file-meta"><span class="acquisition-note">{{ openDataSizeLabel(file.size) }}</span><button type="button" class="acquisition-link" :disabled="downloadingFiles.has(file.file_id)" :aria-label="`下载 ${file.filename} 到本地`" @click="downloadToLocal(job, file)">{{ downloadingFiles.has(file.file_id) ? '正在准备下载…' : '下载到本地' }}</button></div>
              </li>
            </ul>
            <div class="acquisition-actions">
              <button v-if="!tasksOnly && savedFiles(job).length" type="button" class="acquisition-primary" @click="useFiles(job)">加入分析输入</button>
              <button v-if="isPending(job)" type="button" class="acquisition-link" :disabled="Boolean(actionBusy)" @click="changeJob(job, 'cancel')">取消获取</button>
              <button v-if="['failed', 'partial', 'cancelled'].includes(job.status)" type="button" class="acquisition-link" :disabled="Boolean(actionBusy)" @click="changeJob(job, 'retry')">{{ job.status === 'cancelled' ? '重新获取' : '重试失败项' }}</button>
            </div>
            <p v-if="preparedJobs.has(job.id)" role="status" class="acquisition-success">已加入输入框并保留原问题，补充要求后发送即可。</p>
          </article>
        </div>
      </section>
      <p v-if="actionError" role="alert" class="acquisition-warning">{{ actionError }}</p>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue';
import { ChevronDown, FileText } from 'lucide-vue-next';
import type { FileInfo } from '../api/file';
import type { OpenDataJob } from '../api/openData';
import { useDataAcquisition } from '../composables/useDataAcquisition';
import { useFilePanel } from '../composables/useFilePanel';
import { openDataProgressLabel, openDataSizeLabel } from '../utils/openData';

const props = defineProps<{ isShare?: boolean; tasksOnly?: boolean }>();
const emit = defineEmits<{ (event: 'input-prepared'): void }>();
const acquisition = useDataAcquisition();
const { sessionId, jobs, searches, loading, error, actionBusy, actionError, preparedJobs, downloadingFiles, focusedJobId, focusedSearchId, focusRevision } = acquisition;
const { showFilePanel } = useFilePanel();
const contentRef = ref<HTMLElement>();
const completedExpanded = ref(false);
const currentJobs = computed(() => props.isShare || !sessionId.value ? [] : jobs.value.filter(job => job.session_id === sessionId.value));
const isPending = (job: OpenDataJob) => ['queued', 'downloading'].includes(job.status);
const groups = computed(() => [
  { id: 'pending', label: '进行中', jobs: currentJobs.value.filter(isPending) },
  { id: 'attention', label: '需处理', jobs: currentJobs.value.filter(job => !isPending(job) && (job.status !== 'ready' || Boolean(job.error))) },
  { id: 'completed', label: '已完成', jobs: currentJobs.value.filter(job => job.status === 'ready' && !job.error) },
].filter(group => group.jobs.length && (!props.tasksOnly || group.id !== 'completed')));
const jobSearch = (job: OpenDataJob) => searches.value.find(search => search.session_id === sessionId.value && search.id === job.search_id);
const jobTitle = (job: OpenDataJob) => jobSearch(job)?.results.find(candidate => candidate.id === job.candidate_id)?.title || jobSearch(job)?.query || '此前获取的数据文件';
const jobQuery = (job: OpenDataJob) => jobSearch(job)?.query;
const savedFiles = (job: OpenDataJob) => [...new Map(job.files.filter(file => file.file_id).map(file => [file.file_id, file])).values()];
const visibleJobItems = (job: OpenDataJob) => job.items.filter(item => item.status !== 'ready' || !job.files.some(file =>
  item.file?.file_id ? file.file_id === item.file.file_id : file.filename === item.filename));
const progressPercent = (item: OpenDataJob['items'][number]) => item.size && item.size > 0 ? Math.max(0, Math.min(100, Math.round(item.bytes_received / item.size * 100))) : 0;
function jobLabel(status: OpenDataJob['status']) { return ({ queued: '等待获取', downloading: '正在获取', ready: '获取完成', partial: '部分完成', failed: '获取未完成', cancelled: '已取消' })[status]; }
function itemLabel(status: string, jobStatus?: string) {
  if (jobStatus === 'cancelled' && ['queued', 'downloading'].includes(status)) return '已停止';
  return ({ queued: '等待', downloading: '获取中', ready: '已获取', failed: '未完成' } as Record<string, string>)[status] || status;
}
const isCurrentJob = (job: OpenDataJob) => !props.isShare && Boolean(sessionId.value) && job.session_id === sessionId.value && currentJobs.value.some(current => current.id === job.id && current.search_id === job.search_id);
function changeJob(job: OpenDataJob, operation: 'cancel' | 'retry') { if (isCurrentJob(job)) return acquisition.changeJob(job, operation); }
function useFiles(job: OpenDataJob) {
  if (!isCurrentJob(job)) return;
  acquisition.useFiles(job, () => { if (isCurrentJob(job)) emit('input-prepared'); });
}
function downloadToLocal(job: OpenDataJob, file: FileInfo) { if (isCurrentJob(job)) return acquisition.downloadToLocal(job, file); }
function refresh() { if (!props.isShare && sessionId.value) return acquisition.refresh(); }
function previewFile(job: OpenDataJob, file: FileInfo) {
  if (!isCurrentJob(job) || !savedFiles(job).some(saved => saved.file_id === file.file_id)) return;
  showFilePanel(file, savedFiles(job));
}
watch(sessionId, () => { completedExpanded.value = false; });
watch(focusRevision, async revision => {
  if (!revision || props.isShare) return;
  const job = currentJobs.value.find(job => focusedJobId.value ? job.id === focusedJobId.value : job.search_id === focusedSearchId.value);
  if (!job) return;
  if (job.status === 'ready' && !job.error) completedExpanded.value = true;
  await nextTick();
  if (revision !== focusRevision.value || !isCurrentJob(job)) return;
  const element = [...(contentRef.value?.querySelectorAll<HTMLElement>('[data-acquisition-job-id]') || [])].find(element => element.dataset.acquisitionJobId === job.id);
  element?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
}, { immediate: true });
</script>

<style scoped>
.acquisition-content { flex: 1; min-height: 0; overflow-y: auto; overscroll-behavior: contain; padding: 16px; color: var(--text-primary); font-size: 13px; }
.acquisition-note, .acquisition-query, .acquisition-empty { color: var(--text-secondary); font-size: 12px; line-height: 1.6; overflow-wrap: anywhere; }
.acquisition-empty { padding: 24px 0; }
.acquisition-group + .acquisition-group { margin-top: 24px; }
.acquisition-group-heading { display: flex; gap: 7px; align-items: center; margin-bottom: 10px; font-size: 13px; font-weight: 600; }
.acquisition-count { color: var(--text-tertiary); font-weight: 400; }
.acquisition-collapse { width: 100%; justify-content: space-between; text-align: left; }
.acquisition-collapse svg { transition: transform .15s; }
.acquisition-collapse .is-collapsed { transform: rotate(-90deg); }
.acquisition-job { border-top: 1px solid var(--border-main); padding: 14px 0; scroll-margin-top: 16px; }
.acquisition-job:focus-visible { outline: 2px solid var(--text-brand); outline-offset: 3px; }
.acquisition-job-heading { display: flex; align-items: flex-start; gap: 12px; justify-content: space-between; }
.acquisition-job-heading h4 { min-width: 0; font-weight: 600; line-height: 1.6; overflow-wrap: anywhere; }
.acquisition-state { flex: none; font-size: 11px; color: var(--text-secondary); padding-top: 2px; }
.acquisition-query { margin: 5px 0; }
.acquisition-items { display: grid; gap: 12px; margin-top: 12px; }
.acquisition-file-name { min-width: 0; overflow-wrap: anywhere; line-height: 1.6; }
.acquisition-progress { height: 4px; margin: 6px 0; background: var(--fill-tsp-gray-main); border-radius: 3px; overflow: hidden; }
.acquisition-progress span { display: block; height: 100%; background: #2b7659; }
.acquisition-item-message { margin-top: 4px; }
.acquisition-files { display: grid; gap: 12px; margin-top: 12px; }
.acquisition-file-preview { display: flex; align-items: flex-start; gap: 7px; width: 100%; text-align: left; }
.acquisition-file-preview svg { flex: none; margin-top: 2px; color: var(--text-secondary); }
.acquisition-file-preview:hover .acquisition-file-name { text-decoration: underline; }
.acquisition-file-meta { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 6px 14px; margin: 3px 0 0 23px; }
.acquisition-actions { display: flex; flex-wrap: wrap; gap: 14px; align-items: center; margin-top: 12px; }
.acquisition-primary { border-radius: 8px; background: #2b7659; color: white; font-size: 12px; padding: 8px 12px; }
.acquisition-link { color: #2b7659; font-size: 12px; text-decoration: underline; text-underline-offset: 3px; }
.acquisition-warning, .is-warning { color: #a15c16; }
.acquisition-warning { font-size: 12px; line-height: 1.6; margin: 6px 0; overflow-wrap: anywhere; }
.acquisition-success { font-size: 12px; line-height: 1.6; color: #2b7659; margin-top: 10px; }
button:disabled { opacity: .5; cursor: not-allowed; }
button:focus-visible { outline: 2px solid #2b7659; outline-offset: 3px; }
</style>
