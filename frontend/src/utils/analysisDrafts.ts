import type { FileInfo } from '../api/file';
import { readyExplorationFiles } from './explorationFiles.ts';
import type { VisualizationViewContext } from '../visualizations/viewContext';

/** Only editable input belongs in a draft; task status and history stay on the server. */
export interface AnalysisDraft {
  message: string;
  files: FileInfo[];
  skills: string[];
  mcpServers: string[];
  connectors: string[];
  visualizationContexts?: VisualizationViewContext[];
}

export const emptyAnalysisDraft = (): AnalysisDraft => ({
  message: '', files: [], skills: [], mcpServers: [], connectors: [],
});

export const copyAnalysisDraft = (draft: AnalysisDraft): AnalysisDraft => ({
  message: draft.message,
  files: readyExplorationFiles(draft.files),
  skills: [...draft.skills],
  mcpServers: [...draft.mcpServers],
  connectors: [...draft.connectors],
  ...(draft.visualizationContexts?.length ? { visualizationContexts: JSON.parse(JSON.stringify(draft.visualizationContexts)) as VisualizationViewContext[] } : {}),
});

export function createAnalysisDraftStore() {
  let owner: string | null = null;
  let generation = 0;
  const drafts = new Map<string, AnalysisDraft>();

  return {
    setOwner(nextOwner: string | null) {
      if (owner === nextOwner) return;
      owner = nextOwner;
      generation += 1;
      drafts.clear();
    },
    clear() {
      generation += 1;
      drafts.clear();
    },
    open(ownerId: string | null) {
      const openedGeneration = generation;
      const isCurrent = () => Boolean(ownerId && owner === ownerId && openedGeneration === generation);
      return {
        isCurrent,
        read(key: string): AnalysisDraft | undefined {
          const draft = isCurrent() ? drafts.get(key) : undefined;
          return draft ? copyAnalysisDraft(draft) : undefined;
        },
        save(key: string, draft: AnalysisDraft) {
          if (!isCurrent()) return;
          drafts.delete(key);
          drafts.set(key, copyAnalysisDraft(draft));
          // Bound long-lived browser tabs without persisting anything to storage.
          if (drafts.size > 50) drafts.delete(drafts.keys().next().value!);
        },
        remove(key: string) {
          if (isCurrent()) drafts.delete(key);
        },
      };
    },
  };
}

export const analysisDraftStore = createAnalysisDraftStore();
