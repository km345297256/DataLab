import { apiClient, type ApiResponse } from './client';
import type { FileInfo } from './file';
import { authSessionKey } from '../utils/authTokens.ts';

export type DataLibrarySourceType = 'upload' | 'open_data' | 'analysis';

export interface DataLibraryAsset {
  id: string;
  name: string;
  description: string;
  tags: string[];
  source_type: DataLibrarySourceType;
  files: FileInfo[];
  source_url: string | null;
  session_id: string | null;
  created_at: string;
  updated_at: string;
  version: number;
  status: 'ready' | 'missing';
  source_verified: boolean;
}

export interface DataLibraryPage {
  items: DataLibraryAsset[];
  next_cursor: string | null;
}

export interface DataLibraryStats {
  file_count: number;
  total_bytes: number;
  updated_at: string | null;
  formats: string[];
}

export interface DataLibraryFileReference {
  asset_id: string;
  file_id: string;
}

export interface DeleteDataLibraryFilesResult {
  results: Array<DataLibraryFileReference & {
    status: 'deleted' | 'not_found' | 'failed';
    message?: string | null;
  }>;
  deleted_count: number;
  failed_count: number;
}

export interface DataLibraryFolder {
  id: string;
  parent_id: string | null;
  name: string;
  /** Logical library path only; never a host or storage path. */
  path: string;
  created_at: string;
  updated_at: string;
}

export interface CreateDataLibraryFolderInput {
  name: string;
  parent_id: string | null;
}

export interface DataLibraryFolderDeletionPreview {
  folder: DataLibraryFolder;
  deletion_token: string;
  /** Counts cover the complete subtree, independently of file filters or pages. */
  subfolder_count: number;
  file_count: number;
}

export interface DeleteDataLibraryFolderResult {
  folder_id: string;
  parent_id: string | null;
  completed: boolean;
  deleted_file_count: number;
  deleted_folder_count: number;
  remaining_file_count: number | null;
  remaining_folder_count: number | null;
  message: string | null;
}

export interface DataLibraryFilters {
  /** Omit for the entire library; use 'root' for root-level files. */
  folder_id?: string;
  q?: string;
  source_type?: DataLibrarySourceType;
  cursor?: string | null;
  filename?: string;
  file_format?: string;
  updated_from?: string;
  updated_to?: string;
}

export interface SaveDataLibraryInput {
  name: string;
  file_ids: string[];
  /** Display-only relative paths, never host paths or storage locations. */
  file_paths?: Record<string, string>;
  /** Paths are relative to this folder. Null explicitly chooses the root. */
  target_folder_id?: string | null;
  description?: string;
  tags?: string[];
  source_type?: DataLibrarySourceType;
  session_id?: string;
}

export const dataLibrarySourceLabels: Record<DataLibrarySourceType, string> = {
  upload: '上传数据',
  open_data: '开放数据',
  analysis: '分析成果',
};

export async function listDataLibrary(
  params: DataLibraryFilters = {},
  signal?: AbortSignal,
): Promise<DataLibraryPage> {
  return (await apiClient.get<ApiResponse<DataLibraryPage>>('/data-library', { params, signal })).data.data;
}

export async function getDataLibraryStats(signal?: AbortSignal): Promise<DataLibraryStats> {
  return (await apiClient.get<ApiResponse<DataLibraryStats>>('/data-library/stats', { signal })).data.data;
}

export async function listDataLibraryFolders(signal?: AbortSignal): Promise<{ items: DataLibraryFolder[] }> {
  return (await apiClient.get<ApiResponse<{ items: DataLibraryFolder[] }>>('/data-library/folders', { signal })).data.data;
}

export async function createDataLibraryFolder(input: CreateDataLibraryFolderInput): Promise<DataLibraryFolder> {
  return (await apiClient.post<ApiResponse<DataLibraryFolder>>('/data-library/folders', input)).data.data;
}

export async function previewDataLibraryFolderDeletion(id: string, signal?: AbortSignal): Promise<DataLibraryFolderDeletionPreview> {
  return (await apiClient.get<ApiResponse<DataLibraryFolderDeletionPreview>>(`/data-library/folders/${encodeURIComponent(id)}/deletion-preview`, { signal })).data.data;
}

export async function deleteDataLibraryFolder(id: string, deletionToken: string, signal?: AbortSignal): Promise<DeleteDataLibraryFolderResult> {
  return (await apiClient.delete<ApiResponse<DeleteDataLibraryFolderResult>>(`/data-library/folders/${encodeURIComponent(id)}`, { params: { deletion_token: deletionToken }, signal })).data.data;
}

export async function getDataLibraryAsset(id: string, signal?: AbortSignal): Promise<DataLibraryAsset> {
  return (await apiClient.get<ApiResponse<DataLibraryAsset>>(`/data-library/${encodeURIComponent(id)}`, { signal })).data.data;
}

export async function saveDataLibraryAsset(input: SaveDataLibraryInput): Promise<DataLibraryAsset> {
  return (await apiClient.post<ApiResponse<DataLibraryAsset>>('/data-library', input, { timeout: 120000 })).data.data;
}

export async function updateDataLibraryAsset(
  id: string,
  input: { name?: string; description?: string; tags?: string[] },
): Promise<DataLibraryAsset> {
  return (await apiClient.patch<ApiResponse<DataLibraryAsset>>(`/data-library/${encodeURIComponent(id)}`, input)).data.data;
}

export async function deleteDataLibraryAsset(id: string): Promise<void> {
  await apiClient.delete(`/data-library/${encodeURIComponent(id)}`);
}

/** Remove only these personal-library references; shared file objects remain. */
export async function deleteDataLibraryFiles(
  files: DataLibraryFileReference[],
  signal?: AbortSignal,
): Promise<DeleteDataLibraryFilesResult> {
  return (await apiClient.post<ApiResponse<DeleteDataLibraryFilesResult>>('/data-library/files/delete', { files }, { signal })).data.data;
}

export async function getSessionDataContext(sessionId: string): Promise<FileInfo[]> {
  const owner = authSessionKey();
  // Reopening a conversation while its last selection is still saving must
  // restore the result of that write, rather than an older server snapshot.
  await pendingContextWrites.get(sessionId)?.catch(() => undefined);
  if (owner !== authSessionKey()) throw new DOMException('Account changed', 'AbortError');
  const context = (await apiClient.get<ApiResponse<{ files: FileInfo[]; missing_file_ids?: string[] }>>(`/sessions/${encodeURIComponent(sessionId)}/data-context`)).data.data;
  if (context.missing_file_ids?.length) throw new Error('部分所选文件不可用，请重新选择本轮数据。');
  return context.files;
}

const pendingContextWrites = new Map<string, Promise<FileInfo[]>>();

export function setSessionDataContext(sessionId: string, fileIds: string[]): Promise<FileInfo[]> {
  const owner = authSessionKey();
  // Rapid selection changes must reach the server in the user's order. A failed
  // earlier write must not prevent a later, corrected selection from saving.
  const previous = pendingContextWrites.get(sessionId);
  const ids = [...fileIds];
  const write = (previous ? previous.catch(() => undefined) : Promise.resolve()).then(async () => {
    if (owner !== authSessionKey()) throw new DOMException('Account changed', 'AbortError');
    return (await apiClient.put<ApiResponse<{ files: FileInfo[] }>>(`/sessions/${encodeURIComponent(sessionId)}/data-context`, { file_ids: ids })).data.data.files;
  });
  pendingContextWrites.set(sessionId, write);
  const cleanup = () => { if (pendingContextWrites.get(sessionId) === write) pendingContextWrites.delete(sessionId); };
  void write.then(cleanup, cleanup);
  return write;
}
