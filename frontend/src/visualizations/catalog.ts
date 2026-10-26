import { readonly, ref, shallowReadonly, shallowRef } from 'vue';
import { getVisualizationCatalog, setVisualizationEnabled } from '../api/visualization';
import type { VisualizationCatalog } from './contract';
import { pluginPreviewIdentity } from './previewIdentity';
import { ApiError } from '../utils/apiError';

const catalog = shallowRef<VisualizationCatalog | null>(null);
const loading = ref(false);
const error = ref('');
const stale = ref(false);
const updating = ref<string | null>(null);
let generation = 0;
let refreshRequest: Promise<boolean> | null = null;

function publishCatalog(result: VisualizationCatalog) {
  const previous = catalog.value;
  if (!previous) { catalog.value = result; return; }
  const byId = new Map(previous.plugins.map(plugin => [plugin.id, plugin]));
  const plugins = result.plugins.map(plugin => {
    const existing = byId.get(plugin.id);
    return existing && pluginPreviewIdentity(existing) === pluginPreviewIdentity(plugin) ? existing : plugin;
  });
  // Polling confirms authority; equal confirmation is not a content reload.
  if (previous.engine === result.engine && previous.revision === result.revision
    && previous.plugins.length === plugins.length && plugins.every((plugin, index) => plugin === previous.plugins[index])) return;
  catalog.value = { ...result, plugins };
}

export function useVisualizationCatalog() {
  const refresh = (): Promise<boolean> => {
    // Do not let a periodic poll race a state mutation.
    if (updating.value) return Promise.resolve(false);
    // Dataset pages, visible previews and focus events share one confirmation.
    if (refreshRequest) return refreshRequest;
    const current = ++generation;
    loading.value = true;
    const request = (async () => {
      try {
        const result = await getVisualizationCatalog();
        if (current !== generation) return false;
        publishCatalog(result);
        error.value = '';
        stale.value = false;
        return true;
      } catch (cause) {
        if (current !== generation) return false;
        // Only transport failures and unavailable servers may preserve confirmed
        // content. Parsing, authorization, account changes and unexpected errors
        // must invalidate it. Readers still validate authority on every request.
        const transient = cause instanceof ApiError && (
          (cause.status != null && cause.status >= 500 && cause.status < 600)
          || (cause.status == null && cause.code === 503)
        );
        if (catalog.value && transient) {
          stale.value = true;
          error.value = '';
          return false;
        }
        catalog.value = null;
        stale.value = false;
        error.value = '无法连接 Cordis 可视化插件目录。预览已暂停，请重试；文件仍可下载。';
        return false;
      } finally {
        if (current === generation) loading.value = false;
      }
    })();
    refreshRequest = request;
    void request.finally(() => { if (refreshRequest === request) refreshRequest = null; });
    return request;
  };
  const toggle = async (id: string, enabled: boolean) => {
    if (updating.value) return;
    const current = ++generation;
    refreshRequest = null;
    updating.value = id;
    try {
      const result = await setVisualizationEnabled(id, enabled);
      if (current !== generation) return;
      publishCatalog(result);
      error.value = '';
      stale.value = false;
    } catch {
      catalog.value = null;
      stale.value = false;
      error.value = '插件状态未能确认，预览已暂停。请刷新插件目录核实状态。';
    } finally {
      updating.value = null;
      loading.value = false;
    }
  };
  return { catalog: shallowReadonly(catalog), loading: readonly(loading), error: readonly(error), stale: readonly(stale), updating: readonly(updating), refresh, toggle };
}
