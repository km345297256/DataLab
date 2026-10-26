import { computed, onScopeDispose, ref, watch, type Ref } from 'vue';
import { getDataLibraryStats, listDataLibrary, type DataLibraryAsset, type DataLibrarySourceType, type DataLibraryStats } from '../api/dataLibrary';
import { filterLibraryFiles, libraryDateBounds } from '../utils/libraryFileFilters';
import { useAuth } from './useAuth';

/** Global owner-scoped statistics with server-filtered, paginated file browsing. */
export function useLibraryBrowser(options: { folderId?: Ref<string> } = {}) {
  const { currentUser } = useAuth();
  const items = ref<DataLibraryAsset[]>([]);
  const stats = ref<DataLibraryStats | null>(null);
  const query = ref('');
  const fileFormat = ref('');
  const fromDate = ref('');
  const toDate = ref('');
  const sourceType = ref<DataLibrarySourceType | ''>('');
  const nextCursor = ref<string | null>(null);
  const loading = ref(false);
  const error = ref('');
  const visibleItems = computed(() => filterLibraryFiles(items.value, query.value, fileFormat.value, fromDate.value, toDate.value));
  let generation = 0;
  let controller: AbortController | undefined;
  let debounce: ReturnType<typeof setTimeout> | undefined;
  let active = true;
  let resetting = false;

  function cancel() {
    generation++;
    controller?.abort();
    clearTimeout(debounce);
    loading.value = false;
  }

  async function load(more = false, refreshStats = true) {
    if (!active || !currentUser.value?.id || (more && (loading.value || !nextCursor.value))) return;
    cancel();
    const requestGeneration = generation;
    const request = new AbortController();
    controller = request;
    const cursor = more ? nextCursor.value : undefined;
    if (!more) { items.value = []; nextCursor.value = null; }
    error.value = '';
    const dates = libraryDateBounds(fromDate.value, toDate.value);
    if (!dates.valid) { error.value = '请选择有效的更新时间范围，结束日期不能早于开始日期。'; return; }
    loading.value = true;
    const readStats = stats.value === null || (!more && refreshStats);
    const [page, summary] = await Promise.allSettled([
      listDataLibrary({ filename: query.value.trim() || undefined, file_format: fileFormat.value || undefined,
        updated_from: dates.from, updated_to: dates.to, source_type: sourceType.value || undefined,
        ...(options.folderId ? { folder_id: options.folderId.value } : {}), cursor }, request.signal),
      readStats ? getDataLibraryStats(request.signal) : Promise.resolve(stats.value),
    ]);
    if (!active || requestGeneration !== generation || request.signal.aborted) return;
    if (page.status === 'fulfilled') {
      items.value = more ? [...new Map([...items.value, ...page.value.items].map(item => [item.id, item])).values()] : page.value.items;
      nextCursor.value = page.value.next_cursor;
    } else error.value = '暂时无法读取我的数据，请重试。';
    if (summary.status === 'fulfilled') stats.value = summary.value;
    else { stats.value = null; error.value ||= '暂时无法读取数据统计，请重试。'; }
    loading.value = false;
  }

  function resetFilters() {
    resetting = true;
    query.value = ''; fileFormat.value = ''; fromDate.value = ''; toDate.value = ''; sourceType.value = '';
    resetting = false;
    void load(false, false);
  }
  watch([query, fileFormat, fromDate, toDate, sourceType], () => {
    if (resetting) return;
    cancel(); items.value = []; nextCursor.value = null; error.value = '';
    debounce = setTimeout(() => { void load(false, false); }, 250);
  }, { flush: 'sync' });
  if (options.folderId) watch(options.folderId, () => { void load(false, false); }, { flush: 'sync' });
  watch(() => currentUser.value?.id, () => {
    cancel(); items.value = []; stats.value = null; nextCursor.value = null; error.value = '';
    resetFilters();
  }, { immediate: true, flush: 'sync' });
  onScopeDispose(() => { active = false; cancel(); items.value = []; stats.value = null; });
  return { items, visibleItems, stats, query, fileFormat, fromDate, toDate, sourceType, nextCursor, loading, error, load, cancel, resetFilters };
}
