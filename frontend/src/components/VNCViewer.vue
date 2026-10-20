<template>
  <div
    ref="vncContainer"
    class="vnc-container"
    style="position: relative; display: flex; width: 100%; height: 100%; overflow: auto; background: rgb(40, 40, 40);">
    <div v-if="connectionState !== 'connected'" class="vnc-status">
      <span>{{ connectionState === 'error' ? '电脑画面连接失败' : '正在连接电脑画面...' }}</span>
      <button v-if="connectionState === 'error'" type="button" @click="initVNCConnection">重试</button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onBeforeUnmount, watch, nextTick } from 'vue';
import { getVNCUrl } from '@/api/agent';
// @ts-ignore
import RFB from '@novnc/novnc';

const props = defineProps<{
  sessionId: string;
  enabled?: boolean;
  viewOnly?: boolean;
}>();

const emit = defineEmits<{
  connected: [];
  disconnected: [reason?: any];
  credentialsRequired: [];
}>();

const vncContainer = ref<HTMLDivElement | null>(null);
let rfb: RFB | null = null;
const connectionState = ref<'connecting' | 'connected' | 'error'>('connecting');

const initVNCConnection = async () => {
  if (!vncContainer.value || !props.enabled) return;

  connectionState.value = 'connecting';

  // Disconnect existing connection
  if (rfb) {
    rfb.disconnect();
    rfb = null;
  }

  try {
    const wsUrl = await getVNCUrl(props.sessionId);

    // Create NoVNC connection
    rfb = new RFB(vncContainer.value, wsUrl, {
      credentials: { password: '' },
      shared: true,
      repeaterID: '',
      wsProtocols: ['binary'],
      // Scaling options
      scaleViewport: true,  // Automatically scale to fit container
      //resizeSession: true   // Request server to adjust resolution
    });

    // Set viewOnly based on props, default to false (interactive)
    rfb.viewOnly = props.viewOnly ?? false;
    rfb.scaleViewport = true;
    //rfb.resizeSession = true;

    rfb.addEventListener('connect', () => {
      console.log('VNC connection successful');
      connectionState.value = 'connected';
      emit('connected');
    });

    rfb.addEventListener('disconnect', (e: any) => {
      console.log('VNC connection disconnected', e);
      if (props.enabled) connectionState.value = 'error';
      emit('disconnected', e);
    });

    rfb.addEventListener('credentialsrequired', () => {
      console.log('VNC credentials required');
      emit('credentialsRequired');
    });
  } catch (error) {
    console.error('Failed to initialize VNC connection:', error);
    connectionState.value = 'error';
  }
};

const disconnect = () => {
  if (rfb) {
    rfb.disconnect();
    rfb = null;
  }
};

// Watch for session ID or enabled state changes
watch([() => props.sessionId, () => props.enabled], () => {
  if (props.enabled && vncContainer.value) {
    initVNCConnection();
  } else {
    disconnect();
  }
}, { immediate: true });

// Watch for container availability
watch(vncContainer, () => {
  if (vncContainer.value && props.enabled) {
    nextTick(initVNCConnection);
  }
});

onBeforeUnmount(() => {
  disconnect();
});

// Expose methods for parent component
defineExpose({
  disconnect,
  initConnection: initVNCConnection
});
</script>

<style scoped>
.vnc-status {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 12px;
  color: #6b7280;
  background: #fff;
  z-index: 2;
}

.vnc-status button {
  padding: 5px 14px;
  border: 1px solid #d1d5db;
  border-radius: 6px;
  background: #fff;
  color: #374151;
  cursor: pointer;
}
</style>
