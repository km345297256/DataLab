<template>
    <div class="absolute z-[1000] pointer-events-auto" v-if="visible">
        <div class="w-full h-full bg-black/60 backdrop-blur-[4px] fixed inset-0 data-[state=open]:animate-dialog-bg-fade-in data-[state=closed]:animate-dialog-bg-fade-out"
            style="position: fixed; overflow: auto; inset: 0px;" @click="hideSessionFileList"></div>
        <div role="dialog"
            class="bg-[var(--background-menu-white)] rounded-[20px] border border-white/5 fixed left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 max-w-[95%] max-h-[95%] overflow-auto data-[state=open]:animate-dialog-slide-in-from-bottom data-[state=closed]:animate-dialog-slide-out-to-bottom h-[680px] flex flex-col"
            style="width: 600px;">
            <div class="p-0">
                <h3 class="text-[var(--text-primary)] text-[18px] leading-[24px] font-semibold flex items-center"></h3>
            </div>
            <header class="flex items-center pt-6 pr-6 pl-6 pb-2.5">
                <h1 class="flex-1 text-[var(--text-primary)] text-lg font-semibold">{{ $t('All Files in This Task') }}</h1>
                <div class="flex items-center gap-4">
                    <div @click="hideSessionFileList"
                        class="flex h-7 w-7 items-center justify-center cursor-pointer hover:bg-[var(--fill-tsp-gray-main)] rounded-md">
                        <X class="size-5 text-[var(--icon-tertiary)]" />
                    </div>
                </div>
            </header>
            <div class="flex items-center justify-between gap-3 px-6 pt-1 pb-2">
                <div class="flex items-center gap-2 text-xs text-[var(--text-secondary)]">
                    <span>{{ $t('Sort by') }}</span>
                    <select
                        v-model="sortBy"
                        class="h-8 rounded-[10px] border border-[var(--border-btn-main)] bg-[var(--background-menu-white)] px-2 text-sm text-[var(--text-primary)] focus:outline-none focus:ring-[1.5px] focus:ring-[var(--border-dark)]">
                        <option value="upload_date">{{ $t('Creation time') }}</option>
                        <option value="filename">{{ $t('File name') }}</option>
                        <option value="size">{{ $t('File size') }}</option>
                    </select>
                    <select
                        v-model="sortOrder"
                        class="h-8 rounded-[10px] border border-[var(--border-btn-main)] bg-[var(--background-menu-white)] px-2 text-sm text-[var(--text-primary)] focus:outline-none focus:ring-[1.5px] focus:ring-[var(--border-dark)]">
                        <option value="desc">{{ $t('Descending') }}</option>
                        <option value="asc">{{ $t('Ascending') }}</option>
                    </select>
                </div>
            </div>
            <div v-if="!shared && activeSessionId()" class="max-h-[45vh] overflow-y-auto px-6">
                <DataSourceCandidates :key="activeSessionId()" :session-id="activeSessionId()" :files="files" />
            </div>
            <div class="flex-1 min-h-0 flex flex-col">
                <p v-if="loading" class="px-6 py-8 text-sm text-[var(--text-secondary)]" role="status">正在读取当前会话的文件…</p>
                <div v-else-if="error" class="px-6 py-8 text-sm text-[var(--text-secondary)]" role="alert">
                    {{ error }} <button type="button" class="ml-2 underline" @click="fetchFiles">重试</button>
                </div>
                <div v-else-if="files.length > 0" class="flex-1 min-h-0 overflow-auto px-3 mt-4 pb-4">
                    <div class="flex flex-col gap-1 first:pt-0 pt-2">
                        <div class="">
                            <div v-for="file in files"
                                :key="file.file_id || file.filename"
                                class="flex items-center gap-3 px-3 py-2.5 hover:bg-[var(--fill-tsp-gray-main)] transition-colors rounded-lg clickable">
                                <div v-if="!shared" class="flex shrink-0 flex-col gap-1 text-xs">
                                    <button type="button" class="rounded border border-[var(--border-main)] px-2 py-1" @click="useForAnalysis(file)">继续分析</button>
                                    <button type="button" class="rounded border border-[var(--border-main)] px-2 py-1" @click="saveFiles = [file]">保存到我的数据</button>
                                </div>
                                <div class="relative flex items-center justify-center">
                                    <component :is="getFileType(file.filename).icon" />
                                </div>
                                <div @click="showFile(file)" class="flex flex-col gap-1 flex-grow flex-1 min-w-0">
                                    <div class="flex justify-between items-center flex-1 min-w-0">
                                        <div class="flex flex-col flex-1 min-w-0 max-w-[100%]">
                                            <div class="flex-1 min-w-0 flex gap-2 items-center">
                                                <span
                                                    class="inline-block whitespace-nowrap text-sm text-[var(--text-primary)]"
                                                    style="overflow: hidden; text-overflow: ellipsis;">{{ file.filename
                                                    }}</span>
                                                <div class="flex gap-2 flex-shrink-0 items-center"></div>
                                            </div>
                                            <span class="text-xs text-[var(--text-tertiary)]">{{
                                                formatRelativeTime(parseISODateTime(file.upload_date)) }}</span>
                                            <span class="text-xs text-[var(--text-tertiary)]">{{ formatFileSize(file.size) }}</span>
                                        </div>
                                        <div @click="downloadFile(file)"
                                            class="flex items-center justify-center cursor-pointer hover:bg-[var(--fill-tsp-gray-main)] rounded-md w-8 h-8 text-[var(--icon-tertiary)]"
                                            aria-expanded="false" aria-haspopup="dialog">
                                            <Download class="size-5 text-[var(--icon-tertiary)]" />
                                        </div>
                                    </div>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>
                <div v-else class="flex-1 min-h-0 flex flex-col items-center justify-center gap-3">
                    <File />
                    <p class="text-[var(--icon-tertiary)] text-[14px]">{{ $t('No Content') }}</p>
                </div>
            </div>
        </div>
        <SaveToLibraryDialog :visible="saveFiles.length > 0" :files="saveFiles" :session-id="activeSessionId()" @close="saveFiles = []" @saved="saveFiles = []" />
    </div>
</template>

<script setup lang="ts">
import DataSourceCandidates from './DataSourceCandidates.vue';
import SaveToLibraryDialog from './SaveToLibraryDialog.vue';
import { eventBus } from '../utils/eventBus';
import { X, Download, File } from 'lucide-vue-next';
import { onUnmounted, ref, watch } from 'vue';
import { useRoute } from 'vue-router';
import type { FileInfo } from '../api/file';
import { getFileDownloadUrl } from '../api/file';
import { getSessionFiles, getSharedSessionFiles, type SessionFileSortBy, type SessionFileSortOrder } from '../api/agent';
import { formatRelativeTime, parseISODateTime } from '../utils/time';
import { getFileType } from '../utils/fileType';
import { useSessionFileList } from '../composables/useSessionFileList';
import { useFilePanel } from '../composables/useFilePanel';

const route = useRoute();
const props = defineProps<{
    sessionId?: string;
}>();
const files = ref<FileInfo[]>([]);
const saveFiles = ref<FileInfo[]>([]);
const loading = ref(false);
const error = ref('');
let generation = 0;
let active = true;
let loadedSessionId = '';
function useForAnalysis(file: FileInfo) {
    if (shared.value || !isCurrentFile(file)) return;
    eventBus.emit('exploration-data-selected', { sessionId: activeSessionId(), files: [file] });
    hideSessionFileList();
}
const sortBy = ref<SessionFileSortBy>('upload_date');
const sortOrder = ref<SessionFileSortOrder>('desc');

const { showFilePanel } = useFilePanel();

const { visible, hideSessionFileList, shared } = useSessionFileList();

const activeSessionId = () => props.sessionId || route.params.sessionId as string || '';

function isCurrentFile(file: FileInfo) {
    return active && visible.value && !loading.value && loadedSessionId === activeSessionId()
        && files.value.some(item => item.file_id === file.file_id);
}

const fetchFiles = async () => {
    const version = ++generation;
    const sessionId = activeSessionId();
    const isShared = shared.value;
    files.value = [];
    saveFiles.value = [];
    loadedSessionId = '';
    error.value = '';
    loading.value = false;
    if (!active || !visible.value || !sessionId) return;
    loading.value = true;
    const sortOptions = {
        sort_by: sortBy.value,
        sort_order: sortOrder.value,
    };
    const isCurrent = () => active && version === generation && visible.value
        && activeSessionId() === sessionId && shared.value === isShared;
    try {
        const response = isShared
            ? await getSharedSessionFiles(sessionId, sortOptions)
            : await getSessionFiles(sessionId, sortOptions);
        if (!isCurrent()) return;
        files.value = response;
        loadedSessionId = sessionId;
    } catch {
        if (isCurrent()) error.value = '未能读取当前会话的文件，请重试。';
    } finally {
        if (isCurrent()) loading.value = false;
    }
}

const downloadFile = async (fileInfo: FileInfo) => {
    if (!isCurrentFile(fileInfo)) return;
    const version = generation;
    const url = await getFileDownloadUrl(fileInfo);
    if (version === generation && isCurrentFile(fileInfo)) window.open(url, '_blank');
}

const showFile = (file: FileInfo) => {
    if (!isCurrentFile(file)) return;
    showFilePanel(file, files.value);
    hideSessionFileList();
}

const formatFileSize = (size?: number) => {
    if (size == null) return '-';
    if (size < 1024) return `${size} B`;
    const units = ['KB', 'MB', 'GB', 'TB'];
    let value = size / 1024;
    let unitIndex = 0;
    while (value >= 1024 && unitIndex < units.length - 1) {
        value /= 1024;
        unitIndex += 1;
    }
    return `${value.toFixed(value >= 10 ? 0 : 1)} ${units[unitIndex]}`;
}

watch([activeSessionId, visible, shared, sortBy, sortOrder], () => { void fetchFiles(); }, { immediate: true });
onUnmounted(() => {
    active = false;
    generation += 1;
    files.value = [];
    saveFiles.value = [];
});
</script>
