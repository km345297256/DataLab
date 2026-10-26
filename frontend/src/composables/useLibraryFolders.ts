import { onScopeDispose, ref, watch } from 'vue';
import { createDataLibraryFolder, listDataLibraryFolders, type CreateDataLibraryFolderInput, type DataLibraryFolder } from '../api/dataLibrary';
import { useAuth } from './useAuth';
import { safeLibraryRelativePath } from '../utils/personalDataTree';

/** Complete directory navigation, independent of paginated file results. */
export function useLibraryFolders() {
  const { currentUser } = useAuth();
  const folders = ref<DataLibraryFolder[]>([]);
  const loading = ref(false);
  const error = ref('');
  let active = true;
  let generation = 0;
  let ownerGeneration = 0;
  let controller: AbortController | undefined;

  function cancel() {
    generation++;
    controller?.abort();
    loading.value = false;
  }

  async function load() {
    if (!active || !currentUser.value?.id) return;
    cancel();
    const requestGeneration = generation;
    const request = new AbortController();
    controller = request;
    loading.value = true;
    error.value = '';
    try {
      const result = await listDataLibraryFolders(request.signal);
      if (!active || request.signal.aborted || requestGeneration !== generation) return;
      folders.value = result.items.filter(folder => safeLibraryRelativePath(folder.path));
    } catch {
      if (active && !request.signal.aborted && requestGeneration === generation) error.value = '暂时无法读取文件夹，请重试。';
    } finally {
      if (active && requestGeneration === generation) loading.value = false;
    }
  }

  async function createFolder(input: CreateDataLibraryFolderInput) {
    if (!active || !currentUser.value?.id) throw new Error('请先登录后创建文件夹。');
    const ownerAtStart = ownerGeneration;
    const folder = await createDataLibraryFolder(input);
    if (!active || ownerAtStart !== ownerGeneration) throw new Error('账号已切换，请重新查看文件夹。');
    if (!safeLibraryRelativePath(folder.path)) throw new Error('文件夹路径无效，请刷新后重试。');
    // Invalidate reads that started before the creation, so they cannot erase it.
    cancel();
    folders.value = [...new Map([...folders.value, folder].map(item => [item.id, item])).values()];
    if (typeof window !== 'undefined') window.dispatchEvent(new Event('data-library-changed'));
    return folder;
  }

  function refresh() { void load(); }
  if (typeof window !== 'undefined') window.addEventListener('data-library-changed', refresh);
  watch(() => currentUser.value?.id, () => {
    ownerGeneration++;
    cancel(); folders.value = []; error.value = '';
    void load();
  }, { immediate: true, flush: 'sync' });
  onScopeDispose(() => {
    active = false; ownerGeneration++; cancel(); folders.value = [];
    if (typeof window !== 'undefined') window.removeEventListener('data-library-changed', refresh);
  });
  return { folders, loading, error, load, cancel, createFolder };
}
