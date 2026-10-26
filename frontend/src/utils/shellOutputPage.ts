/** Pagination carries an opaque stream identity and byte offsets, never a host path. */
export interface ShellOutputMetadata {
  output_id: string;
  total_bytes: number;
  preview_truncated: boolean;
  log_status: 'available' | 'unavailable' | 'limit_exceeded' | 'closed';
  stream_complete: boolean;
}
export interface ShellOutputPage {
  cursor: number;
  start: number;
  next_cursor: number;
  lossy: boolean;
  source: 'spool' | 'tail';
  eof: boolean;
}
export const SHELL_LOG_PAGE_BYTES = 16 * 1024;
export function readShellOutputMetadata(value: unknown): ShellOutputMetadata | undefined {
  if (!value || typeof value !== 'object') return;
  const data = value as ShellOutputMetadata;
  if (typeof data.output_id !== 'string' || !/^[a-f0-9]{32}$/.test(data.output_id)
    || !Number.isSafeInteger(data.total_bytes) || data.total_bytes < 0
    || typeof data.preview_truncated !== 'boolean' || typeof data.stream_complete !== 'boolean'
    || !['available', 'unavailable', 'limit_exceeded', 'closed'].includes(data.log_status)) return;
  return { output_id: data.output_id, total_bytes: data.total_bytes, preview_truncated: data.preview_truncated,
    log_status: data.log_status, stream_complete: data.stream_complete };
}
export function readShellOutputPage(response: { output: string; output_metadata?: unknown; output_page?: unknown }, outputId: string, cursor: number) {
  const metadata = readShellOutputMetadata(response.output_metadata);
  const page = response.output_page as ShellOutputPage | undefined;
  if (!metadata || metadata.output_id !== outputId || !page
    || !Number.isSafeInteger(page.cursor) || page.cursor !== cursor
    || !Number.isSafeInteger(page.start) || page.start < cursor
    || !Number.isSafeInteger(page.next_cursor) || page.next_cursor < page.start || page.next_cursor > metadata.total_bytes
    || typeof page.lossy !== 'boolean' || page.lossy !== (page.start !== cursor)
    || !['spool', 'tail'].includes(page.source) || typeof page.eof !== 'boolean'
    || (page.eof && (!metadata.stream_complete || page.next_cursor !== metadata.total_bytes))
    || typeof response.output !== 'string' || page.next_cursor - page.start > SHELL_LOG_PAGE_BYTES
    || new TextEncoder().encode(response.output).length !== page.next_cursor - page.start) {
    throw new Error('日志分页响应无效，请返回最新输出后重试。');
  }
  return { metadata, page, output: response.output };
}
