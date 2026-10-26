import { apiClient, type ApiResponse } from './client';
import type { FileInfo } from './file';

export interface OpenDataCandidate {
  id: string;
  source_id: string;
  external_id: string;
  title: string;
  description: string;
  source_url: string;
  access: 'downloadable' | 'needs_resolution' | 'restricted' | 'lead';
  availability_verified_at: string | null;
  available_file_count: number;
  formats: string[];
  size?: number | null;
  license?: string | null;
  coverage: Record<string, unknown>;
  match_notes: string[];
  relevance_status?: 'related' | 'unrelated' | 'uncertain';
  relevance_reason?: string;
  limitations: string[];
  system_notes?: string[];
}

export interface OpenDataSearch {
  id: string;
  session_id: string;
  query: string;
  parent_id?: string | null;
  status: 'queued' | 'searching' | 'ready' | 'partial' | 'failed';
  sources: Array<{ id: string; title: string; status: string; count: number; error?: string | null; warnings?: string[] }>;
  results: OpenDataCandidate[];
  has_more: boolean;
  created_at: string | number;
  updated_at?: string | number;
  effective_query?: string | null;
  relevance_pending_count?: number;
  notes?: string[];
}

export interface OpenDataFileOption {
  id: string;
  filename: string;
  size: number | null;
  content_type?: string | null;
  available: boolean;
  reason?: string | null;
  kind?: string | null;
  group_id?: string | null;
  group_role?: string | null;
  required_file_ids?: string[];
  required?: boolean;
}

export interface OpenDataLimits { max_file_bytes: number | null; max_total_bytes: number | null; max_files: number | null }
export interface OpenDataResolution {
  files: OpenDataFileOption[];
  limits?: OpenDataLimits;
  verification_pending_count?: number;
  unavailable_file_count?: number;
}

export interface OpenDataJob {
  id: string;
  search_id: string;
  candidate_id: string;
  session_id: string;
  status: 'queued' | 'downloading' | 'ready' | 'partial' | 'failed' | 'cancelled';
  items: Array<{
    id: string; filename: string; status: 'queued' | 'downloading' | 'ready' | 'failed';
    message?: string | null; bytes_received: number; size?: number | null; file?: FileInfo;
  }>;
  files: FileInfo[];
  asset_id?: string | null;
  error?: string | null;
  updated_at?: string | number;
}

const base = (sessionId: string) => `/sessions/${encodeURIComponent(sessionId)}`;
const searchPath = (sessionId: string, searchId: string) => `${base(sessionId)}/data-searches/${encodeURIComponent(searchId)}`;

export async function createOpenDataSearch(sessionId: string, input: { query: string; parent_id?: string; source_ids?: string[] }, signal?: AbortSignal): Promise<OpenDataSearch> {
  return (await apiClient.post<ApiResponse<OpenDataSearch>>(`${base(sessionId)}/data-searches`, input, { signal })).data.data;
}
export async function listOpenDataSearches(sessionId: string, signal?: AbortSignal): Promise<OpenDataSearch[]> {
  return (await apiClient.get<ApiResponse<OpenDataSearch[]>>(`${base(sessionId)}/data-searches`, { signal })).data.data;
}
export async function getOpenDataSearch(sessionId: string, searchId: string, signal?: AbortSignal): Promise<OpenDataSearch> {
  return (await apiClient.get<ApiResponse<OpenDataSearch>>(searchPath(sessionId, searchId), { signal })).data.data;
}
export async function continueOpenDataSearch(sessionId: string, searchId: string, signal?: AbortSignal): Promise<OpenDataSearch> {
  return (await apiClient.post<ApiResponse<OpenDataSearch>>(`${searchPath(sessionId, searchId)}/continue`, {}, { signal })).data.data;
}
export async function resolveOpenDataCandidate(sessionId: string, searchId: string, candidateId: string, signal?: AbortSignal): Promise<OpenDataResolution> {
  return (await apiClient.post<ApiResponse<OpenDataResolution>>(`${searchPath(sessionId, searchId)}/candidates/${encodeURIComponent(candidateId)}/resolve`, {}, { signal, timeout: 0 })).data.data;
}
export async function acquireOpenData(sessionId: string, searchId: string, input: { candidate_id: string; file_ids: string[] }, signal?: AbortSignal): Promise<OpenDataJob> {
  return (await apiClient.post<ApiResponse<OpenDataJob>>(`${searchPath(sessionId, searchId)}/acquisitions`, input, { signal, timeout: 0 })).data.data;
}
export async function listOpenDataJobs(sessionId: string, searchId?: string, signal?: AbortSignal): Promise<OpenDataJob[]> {
  return (await apiClient.get<ApiResponse<OpenDataJob[]>>(`${base(sessionId)}/acquisitions`, { ...(searchId ? { params: { search_id: searchId } } : {}), signal })).data.data;
}
export async function changeOpenDataJob(sessionId: string, jobId: string, action: 'cancel' | 'retry', signal?: AbortSignal): Promise<OpenDataJob> {
  return (await apiClient.post<ApiResponse<OpenDataJob>>(`${base(sessionId)}/acquisitions/${encodeURIComponent(jobId)}/${action}`, {}, { signal })).data.data;
}
