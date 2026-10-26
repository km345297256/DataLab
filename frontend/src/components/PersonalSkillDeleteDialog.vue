<template>
  <Dialog :open="Boolean(skill)" @update:open="value => { if (!value && !busy) emit('cancel'); }">
    <DialogContent class="w-[min(440px,calc(100vw-32px))]" @escape-key-down="event => { if (busy) event.preventDefault(); }" @interact-outside="event => { if (busy) event.preventDefault(); }">
      <DialogHeader>
        <DialogTitle>{{ t('Delete personal skill?') }}</DialogTitle>
        <DialogDescription class="mt-3 break-words text-sm leading-6 text-[var(--text-tertiary)]">
          {{ t('Delete personal skill confirmation', { name: skill?.title || skill?.name || '' }) }}
        </DialogDescription>
      </DialogHeader>
      <p v-if="error" role="alert" class="px-6 pb-3 text-sm text-[var(--function-error)]">{{ error }}</p>
      <div class="flex justify-end gap-2 px-6 pb-5">
        <button type="button" class="rounded-lg border border-[var(--border-main)] px-3 py-2 text-sm disabled:opacity-50" :disabled="busy" @click="emit('cancel')">{{ t('Cancel') }}</button>
        <button type="button" class="inline-flex items-center gap-1.5 rounded-lg bg-[var(--function-error)] px-3 py-2 text-sm text-white disabled:opacity-50" :disabled="busy" @click="emit('confirm')">
          <LoaderCircle v-if="busy" :size="15" class="animate-spin" />{{ t(busy ? 'Deleting skill...' : 'Delete') }}
        </button>
      </div>
    </DialogContent>
  </Dialog>
</template>

<script setup lang="ts">
import { useI18n } from 'vue-i18n';
import { LoaderCircle } from 'lucide-vue-next';
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from './ui/dialog';
import type { SkillInfo } from '../api/skill';
defineProps<{ skill: SkillInfo | null; busy: boolean; error: string }>();
const emit = defineEmits<{ (e: 'confirm'): void; (e: 'cancel'): void }>();
const { t } = useI18n();
</script>
