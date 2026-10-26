import { onUnmounted, ref } from 'vue';
import { useI18n } from 'vue-i18n';
import { deletePersonalSkill, type SkillInfo } from '../api/skill';
import { useAuth } from './useAuth';
import { eventBus } from '../utils/eventBus';
import { EVENT_SKILL_CATALOG_UPDATED } from '../constants/event';
import { showSuccessToast } from '../utils/toast';

export function usePersonalSkillDeletion(onDeleted: (skill: SkillInfo) => void) {
  const { t } = useI18n();
  const { currentUser } = useAuth();
  const deletingSkillId = ref<string | null>(null);
  const skillToDelete = ref<SkillInfo | null>(null);
  const deleteError = ref('');
  let active = true, confirmationOwner: string | undefined;
  onUnmounted(() => { active = false; });

  const canDeletePersonalSkill = (skill: SkillInfo) => Boolean(
    skill.source === 'personal' && currentUser.value?.id
    && (skill.owner_user_id || skill.user_id) === currentUser.value.id,
  );

  function requestDeleteSkill(skill: SkillInfo) {
    if (!active || deletingSkillId.value || !canDeletePersonalSkill(skill)) return;
    confirmationOwner = currentUser.value?.id;
    skillToDelete.value = skill;
    deleteError.value = '';
  }

  function cancelDeleteSkill() {
    if (deletingSkillId.value) return;
    skillToDelete.value = null;
    deleteError.value = '';
  }

  async function confirmDeleteSkill() {
    const skill = skillToDelete.value, owner = confirmationOwner;
    if (!active || !skill || deletingSkillId.value || currentUser.value?.id !== owner || !canDeletePersonalSkill(skill)) return;
    deletingSkillId.value = skill.id;
    deleteError.value = '';
    try {
      await deletePersonalSkill(skill.id);
    } catch {
      if (active && currentUser.value?.id === owner) deleteError.value = t('Failed to delete skill');
      return;
    } finally {
      deletingSkillId.value = null;
    }
    if (currentUser.value?.id !== owner) return;
    if (active) {
      skillToDelete.value = null;
      onDeleted(skill);
    }
    eventBus.emit(EVENT_SKILL_CATALOG_UPDATED, { deletedSkillId: skill.id, deletedSkillName: skill.name });
    showSuccessToast(t('Skill deleted'));
  }

  return { canDeletePersonalSkill, deletingSkillId, skillToDelete, deleteError, requestDeleteSkill, confirmDeleteSkill, cancelDeleteSkill };
}
