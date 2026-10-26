import type { OpenDataCandidate, OpenDataFileOption, OpenDataSearch } from '../api/openData';
import type { Message, StepContent, ToolContent } from '../types/message';

export const openDataSourceUrl = (value: string): string | undefined => {
  try {
    const url = new URL(value);
    return ['https:', 'http:'].includes(url.protocol) && !url.username && !url.password ? url.href : undefined;
  } catch { return undefined; }
};

export function openDataTimestamp(value: string | number): number {
  const parsed = typeof value === 'number' ? (value < 1e12 ? value * 1000 : value) : Date.parse(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

export function openDataSizeLabel(value?: number | null): string {
  if (value == null || !Number.isFinite(value) || value < 0) return '大小未知';
  if (value < 1000) return `${value} B`;
  const index = Math.min(Math.floor(Math.log(value) / Math.log(1000)), 5);
  return `${(value / 1000 ** index).toFixed(1)} ${['B', 'KB', 'MB', 'GB', 'TB', 'PB'][index]}`;
}

export function openDataProgressLabel(received: number, total?: number | null): string {
  const current = openDataSizeLabel(Number.isFinite(received) && received > 0 ? received : 0);
  return total != null && Number.isFinite(total) && total >= 0
    ? `${current} / ${openDataSizeLabel(total)}` : `${current} · 总大小未知`;
}

/** Count and submit the same distinct, currently available file options. */
export function openDataAvailableFiles(options: OpenDataFileOption[]): OpenDataFileOption[] {
  return [...new Map(options.map(file => [file.id, file])).values()].filter(file => file.id && file.available);
}

/** A partial sum is not the total size when the source omits some file sizes. */
export function openDataFilesSizeLabel(files: Array<{ size?: number | null }>): string {
  const known = files.filter(file => file.size != null && Number.isFinite(file.size) && file.size >= 0);
  if (!known.length) return '大小未知';
  const size = openDataSizeLabel(known.reduce((sum, file) => sum + file.size!, 0));
  const unknown = files.length - known.length;
  return unknown ? `已知 ${size}，另有 ${unknown} 个文件大小未知` : size;
}

export function openDataCoverageFacts(coverage: Record<string, unknown> = {}): Array<{ label: string; value: string }> {
  const labels: Record<string, string> = {
    temporal: '时间', spatial: '地区', time: '时间', geography: '地区', granularity: '数据粒度',
    variables: '变量', country: '地区', start_year: '开始年份', end_year: '结束年份', start: '开始时间', end: '结束时间',
    organism: '物种', sample_count: '样本数', library_strategy: '测序方法', records: '记录数',
    NumberOfInstances: '样本数', NumberOfFeatures: '特征数', scope: '数据范围', series_key: '序列编号', m49: '地区代码',
  };
  const display = (value: unknown): string => Array.isArray(value) ? value.map(display).join('、')
    : value && typeof value === 'object' ? Object.entries(value).filter(([, item]) => item != null && item !== '')
      .map(([key, item]) => `${labels[key] || key}：${display(item)}`).join('；')
    : typeof value === 'boolean' ? value ? '是' : '否' : String(value);
  return Object.entries(coverage).filter(([, value]) => value != null && value !== '')
    .map(([key, value]) => ({ label: labels[key] || key, value: display(value) })).filter(fact => fact.value);
}

export type OpenDataTimelineEntry = { kind: 'message'; message: Message; index: number } | { kind: 'search'; search: OpenDataSearch };

/** Require obtainable files and honor explicit relevance decisions; old records remain compatible. */
export function isVerifiedOpenDataCandidate(candidate: OpenDataCandidate): boolean {
  return candidate.access === 'downloadable'
    && (candidate.relevance_status == null || candidate.relevance_status === 'related')
    && typeof candidate.availability_verified_at === 'string'
    && Boolean(candidate.availability_verified_at.trim())
    && Number.isFinite(Date.parse(candidate.availability_verified_at))
    && Number.isInteger(candidate.available_file_count) && candidate.available_file_count > 0;
}

/** Keep message order intact and never duplicate a search already shown by a tool. */
export function openDataTimeline(messages: Message[], searches: OpenDataSearch[], presentation?: { message: Message; index: number }[]): OpenDataTimelineEntry[] {
  const represented = new Set<string>();
  for (const message of messages) {
    const tools = message.type === 'tool' ? [message.content as ToolContent]
      : message.type === 'step' ? (message.content as StepContent).tools : [];
    for (const tool of tools) {
      const id = (tool as ToolContent & { open_data_search_id?: string }).open_data_search_id;
      if (id) represented.add(id);
    }
  }
  const remaining = [...new Map(searches.map(search => [search.id, search])).values()]
    .filter(search => !represented.has(search.id))
    .sort((left, right) => openDataTimestamp(left.created_at) - openDataTimestamp(right.created_at));
  const entries: OpenDataTimelineEntry[] = [];
  let cursor = 0;
  for (const { index, message } of presentation ?? messages.map((message, index) => ({ message, index }))) {
    const timestamp = openDataTimestamp(message.content.timestamp);
    while (cursor < remaining.length && openDataTimestamp(remaining[cursor]!.created_at) < timestamp) {
      entries.push({ kind: 'search', search: remaining[cursor++]! });
    }
    entries.push({ kind: 'message', message, index });
  }
  for (const search of remaining.slice(cursor)) entries.push({ kind: 'search', search });
  return entries;
}

/** Required groups are enforced when the source provides relationships; never infer them from a filename. */
export function openDataSelectionIssue(options: OpenDataFileOption[], selectedIds: string[]): string {
  if (!selectedIds.length) return '请选择需要获取的文件。';
  const selected = new Set(selectedIds);
  const files = options.filter(file => selected.has(file.id));
  if (files.length !== selected.size || files.some(file => !file.available)) return '部分所选文件当前不可获取，请重新选择。';
  if (options.some(file => file.required && !selected.has(file.id))) return '请同时选择数据源标注的必要配套文件。';
  for (const file of files) {
    if (file.required_file_ids?.some(id => !selected.has(id))) return '请同时选择这份数据需要的配套文件。';
  }
  return '';
}
