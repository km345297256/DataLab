import { apiClient, type ApiResponse } from './client.ts';
import type { SkillCreationView } from '../types/skillCreation';

const draftUrl = (id: string) => `/skills/drafts/${encodeURIComponent(id)}`;

export async function getSkillDraft(id: string, signal?: AbortSignal): Promise<SkillCreationView> {
  const response = await apiClient.get<ApiResponse<SkillCreationView>>(draftUrl(id), { signal });
  return response.data.data;
}

/** Only an explicit user action calls this endpoint. Creating/reading a draft never installs it. */
export async function installSkillDraft(id: string, signal?: AbortSignal): Promise<SkillCreationView> {
  const response = await apiClient.post<ApiResponse<SkillCreationView>>(`${draftUrl(id)}/install`, undefined, { signal });
  return response.data.data;
}

export async function downloadSkillDraft(id: string, signal?: AbortSignal): Promise<Blob> {
  const response = await apiClient.get<Blob>(`${draftUrl(id)}/download`, { responseType: 'blob', signal });
  return response.data;
}

export interface SkillDraftResource { path: string; content: string }

export async function getSkillDraftResource(id: string, path: string, signal?: AbortSignal): Promise<SkillDraftResource> {
  const response = await apiClient.get<ApiResponse<SkillDraftResource>>(`${draftUrl(id)}/resource`, {
    params: { path }, signal,
  });
  return response.data.data;
}
