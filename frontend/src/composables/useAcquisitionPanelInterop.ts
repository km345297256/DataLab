import { onScopeDispose } from 'vue';
import { useDataAcquisition } from './useDataAcquisition';
import { eventBus } from '../utils/eventBus';
import { EVENT_SHOW_FILE_PANEL, EVENT_SHOW_TOOL_PANEL, EVENT_HIDE_FILE_PANEL, EVENT_HIDE_TOOL_PANEL } from '../constants/event';

/** One shared right-side region: explicit file/tool views take precedence. */
export function useAcquisitionPanelInterop() {
  const acquisitions = useDataAcquisition();
  let occupying: 'file' | 'tool' | undefined;
  const fileOpened = () => { occupying = 'file'; acquisitions.externalPanelOpened(); };
  const toolOpened = () => { occupying = 'tool'; acquisitions.externalPanelOpened(); };
  const fileClosed = () => {
    if (occupying === 'file') { occupying = undefined; acquisitions.externalPanelClosed(); }
  };
  const toolClosed = () => {
    if (occupying === 'tool') { occupying = undefined; acquisitions.externalPanelClosed(); }
  };
  eventBus.on(EVENT_SHOW_FILE_PANEL, fileOpened);
  eventBus.on(EVENT_SHOW_TOOL_PANEL, toolOpened);
  eventBus.on(EVENT_HIDE_FILE_PANEL, fileClosed);
  eventBus.on(EVENT_HIDE_TOOL_PANEL, toolClosed);
  onScopeDispose(() => {
    eventBus.off(EVENT_SHOW_FILE_PANEL, fileOpened);
    eventBus.off(EVENT_SHOW_TOOL_PANEL, toolOpened);
    eventBus.off(EVENT_HIDE_FILE_PANEL, fileClosed);
    eventBus.off(EVENT_HIDE_TOOL_PANEL, toolClosed);
  });
}
