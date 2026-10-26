import { onScopeDispose, ref, watch } from 'vue';
import { listDataLibrary, type DataLibraryAsset, type DataLibrarySourceType } from '../api/dataLibrary';

export function useDataLibraryList() {
  const items = ref<DataLibraryAsset[]>([]);
  const query = ref('');
  const sourceType = ref<DataLibrarySourceType | ''>('');
  const nextCursor = ref<string | null>(null);
  const loading = ref(false);
  const error = ref('');
  let generation = 0;
  let controller: AbortController | undefined;
  let debounce: ReturnType<typeof setTimeout> | undefined;
  let active = true;

  const cancel = () => {
    generation += 1;
    controller?.abort();
    clearTimeout(debounce);
    loading.value = false;
  };

  async function load(more = false) {
    if (!active || (more && (loading.value || !nextCursor.value))) return;
    cancel();
    const requestGeneration = generation;
    const request = new AbortController();
    controller = request;
    loading.value = true;
    error.value = '';
    if (!more) { items.value = []; nextCursor.value = null; }
    try {
      const result = await listDataLibrary({ q: query.value.trim() || undefined, source_type: sourceType.value || undefined, cursor: more ? nextCursor.value : undefined }, request.signal);
      if (!active || generation !== requestGeneration) return;
      items.value = more ? [...new Map([...items.value, ...result.items].map(item => [item.id, item])).values()] : result.items;
      nextCursor.value = result.next_cursor;
    } catch {
      if (active && generation === requestGeneration && !request.signal.aborted) error.value = '暂时无法读取我的数据，请重试。';
    } finally {
      if (active && generation === requestGeneration) loading.value = false;
    }
  }

  watch([query, sourceType], () => {
    cancel();
    items.value = [];
    nextCursor.value = null;
    debounce = setTimeout(() => { void load(); }, 250);
  });
  onScopeDispose(() => { active = false; cancel(); items.value = []; });
  return { items, query, sourceType, nextCursor, loading, error, load, cancel };
}
