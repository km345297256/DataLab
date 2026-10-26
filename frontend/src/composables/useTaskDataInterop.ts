import { onScopeDispose } from 'vue';
import { useTaskData } from './useTaskData';
import { useDataAcquisition } from './useDataAcquisition';
import { useFilePanel } from './useFilePanel';
import { eventBus } from '../utils/eventBus';
import { EVENT_SHOW_ACQUISITION_PANEL, EVENT_SHOW_FILE_PANEL, EVENT_SHOW_TOOL_PANEL } from '../constants/event';

/** Task data, previews and tools share one right-hand workspace. */
export function useTaskDataInterop() {
  const taskData = useTaskData();
  const acquisition = useDataAcquisition();
  const { hideFilePanel } = useFilePanel();
  const acquisitionOpened = (payload: unknown) => {
    const request = payload as { sessionId?: string; automatic?: boolean };
    if (!request || request.sessionId !== taskData.sessionId.value) return;
    // A tool closing during the open event can release the legacy acquisition
    // occupancy flag. Automatic updates must still respect the data view.
    if (request.automatic && taskData.panelOpen.value) {
      acquisition.externalPanelOpened();
      return;
    }
    taskData.open('download');
  };
  const dataOpened = () => {
    hideFilePanel();
    acquisition.externalPanelOpened();
  };
  const dataClosed = () => {
    acquisition.close();
    acquisition.externalPanelClosed();
  };
  const fileOpened = () => taskData.suspend();
  const toolOpened = () => {
    taskData.previewFromTaskData.value = false;
    taskData.suspend();
  };
  const listeners: [string, (payload: unknown) => void][] = [
    [EVENT_SHOW_ACQUISITION_PANEL, acquisitionOpened],
    ['task-data-panel-show', dataOpened], ['task-data-panel-close', dataClosed],
    [EVENT_SHOW_FILE_PANEL, fileOpened], [EVENT_SHOW_TOOL_PANEL, toolOpened],
  ];
  for (const [name, listener] of listeners) eventBus.on(name, listener);
  onScopeDispose(() => { for (const [name, listener] of listeners) eventBus.off(name, listener); });
}
