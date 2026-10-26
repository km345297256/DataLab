import type { FileInfo } from '../api/file';
import type { VisualizationPlugin } from './contract';

/** This is a UI availability hint. Every actual read still needs server authorization. */
export function visualizationBlockedReason(plugin: VisualizationPlugin, file: Pick<FileInfo, 'size'>, shared: boolean): string {
  if (shared && !plugin.capabilities.shared) return '此插件不开放共享读取。请在原会话中使用此插件，或下载原始文件。';
  if (file.size != null && (!Number.isFinite(file.size) || file.size < 0)) return '文件大小信息无效，请刷新后重试。';
  // Window/page/prefix budgets constrain a read, not the size of the source.
  if (plugin.capabilities.input_mode === 'whole' && file.size != null && file.size > plugin.limits.max_input_bytes) {
    return `文件超出该插件的安全预览上限（${Math.round(plugin.limits.max_input_bytes / 1024 / 1024)} MiB），请切换其他视图或下载后分析。`;
  }
  return '';
}

/** Keep explicit choices, including unavailable views whose limits must stay inspectable. */
export function preferredVisualization(candidates: readonly VisualizationPlugin[], file: Pick<FileInfo, 'size'>, shared: boolean, selectedId: string | null = null): VisualizationPlugin | null {
  const selected = candidates.find(plugin => plugin.id === selectedId);
  if (selected) return selected;
  const available = candidates.filter(plugin => !visualizationBlockedReason(plugin, file, shared));
  // Preserve the content-profile/product ordering within eligible candidates.
  return available[0] ?? candidates[0] ?? null;
}
