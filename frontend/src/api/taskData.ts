import { apiClient, type ApiResponse } from './client';
import type { FileInfo } from './file';

export type TaskDataCategory = 'upload' | 'analysis' | 'download';
export type TaskDataTab = 'all' | TaskDataCategory;
export interface TaskDataSavedLocation {
  asset_id: string;
  asset_name: string;
  folder_id: string | null;
  /** Logical library paths only, never storage or host locations. */
  folder_path: string;
  file_path: string;
}
export interface TaskDataItem {
  file: FileInfo;
  category: TaskDataCategory;
  created_at: string | null;
  round_number: number | null;
  available: boolean;
  saved_locations: TaskDataSavedLocation[];
}
export interface TaskDataResponse {
  items: TaskDataItem[];
  selected_file_ids: string[];
  unavailable_count: number;
}
export async function getTaskData(sessionId: string, fileIds: string[] = [], signal?: AbortSignal): Promise<TaskDataResponse> {
  return (await apiClient.get<ApiResponse<TaskDataResponse>>(`/sessions/${encodeURIComponent(sessionId)}/task-data`, { signal, params: fileIds.length ? { file_ids: fileIds } : undefined, paramsSerializer: { indexes: null } })).data.data;
}
