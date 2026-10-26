import { useRouter } from 'vue-router';
import { useAuth } from './useAuth';
import { analysisDraftStore } from '../utils/analysisDrafts';
import { eventBus } from '../utils/eventBus';

export const EVENT_START_NEW_ANALYSIS = 'datalab:new-analysis';

export function useNewAnalysis() {
  const router = useRouter();
  const { currentUser } = useAuth();
  function startNewAnalysis() {
    analysisDraftStore.open(currentUser.value?.id ?? null).remove('new');
    eventBus.emit(EVENT_START_NEW_ANALYSIS);
    return router.push('/chat');
  }
  return { startNewAnalysis };
}
