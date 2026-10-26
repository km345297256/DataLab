import { ref } from 'vue';
import { eventBus } from '../utils/eventBus';
import { useTaskData } from './useTaskData';

const visible = ref(false);
const shared = ref(false);
eventBus.on('auth:cleared', () => { visible.value = false; shared.value = false; });

export function useSessionFileList() {
    const showSessionFileList = (isShared: boolean = false) => {
        if (!isShared && useTaskData().active.value) {
            useTaskData().open('all');
            return;
        }
        visible.value = true;
        shared.value = isShared;
    }

    const hideSessionFileList = () => {
        visible.value = false;
    }

    return {
        visible,
        shared,
        showSessionFileList,
        hideSessionFileList
    }
}
