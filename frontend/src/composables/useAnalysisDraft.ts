import { onBeforeUnmount, watch } from 'vue';
import { useAuth } from './useAuth';
import { analysisDraftStore, copyAnalysisDraft, emptyAnalysisDraft, type AnalysisDraft } from '../utils/analysisDrafts';

/** Cache only unsent changes while a view is away, never a running task's state. */
export function useAnalysisDraft(
  key: () => string | undefined,
  readEditor: () => AnalysisDraft,
  applyEditor: (draft: AnalysisDraft) => void,
) {
  const { currentUser } = useAuth();
  const ownerId = currentUser.value?.id ?? null;
  const access = analysisDraftStore.open(ownerId);
  let baseline: AnalysisDraft | undefined;
  let active = false;
  const sameDraft = (left: AnalysisDraft, right: AnalysisDraft) => JSON.stringify(left) === JSON.stringify(right);

  function save() {
    const draftKey = key();
    if (!active || !draftKey || !baseline || currentUser.value?.id !== ownerId) return;
    const draft = copyAnalysisDraft(readEditor());
    if (sameDraft(draft, baseline)) access.remove(draftKey);
    else access.save(draftKey, draft);
  }

  function restore(serverDraft?: AnalysisDraft) {
    const draftKey = key();
    if (!draftKey || !access.isCurrent() || currentUser.value?.id !== ownerId) return;
    // A repeated restore must never promote unsent editor text to the baseline.
    // Session pages pass the freshly fetched server fields explicitly; new
    // analyses capture their initial empty editor only on the first restore.
    baseline = copyAnalysisDraft(serverDraft ?? baseline ?? readEditor());
    const cached = access.read(draftKey);
    if (cached) applyEditor(cached);
    else if (serverDraft) applyEditor(copyAnalysisDraft(baseline));
    active = true;
  }

  function accept(draft: AnalysisDraft) {
    baseline = copyAnalysisDraft(draft);
    active = true;
    save();
  }

  function discard() {
    const draftKey = key();
    if (draftKey) access.remove(draftKey);
    active = false;
  }

  // A stale component must neither display nor write the previous user's draft.
  watch(() => currentUser.value?.id, nextOwner => {
    if (nextOwner === ownerId) return;
    active = false;
    baseline = undefined;
    applyEditor(emptyAnalysisDraft());
  }, { flush: 'sync' });
  onBeforeUnmount(save);

  return {
    restore, save, accept, discard,
    isCurrent: () => access.isCurrent() && currentUser.value?.id === ownerId,
  };
}
