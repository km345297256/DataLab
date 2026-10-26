<template>
  <div class="flex h-full w-full flex-col overflow-hidden bg-[var(--background-gray-main)]">
    <header class="mobile-safe-top flex items-start gap-3 border-b border-[var(--border-main)] px-3 pb-4 sm:px-6 sm:py-5">
      <button v-if="!isLeftPanelShow" type="button" class="flex h-11 w-11 shrink-0 items-center justify-center rounded-lg hover:bg-[var(--fill-tsp-gray-main)]" :aria-label="t('Open navigation')" @click="toggleLeftPanel"><PanelLeft class="size-5 text-[var(--icon-secondary)]" /></button>
      <div class="min-w-0 flex-1"><h1 class="text-2xl font-semibold text-[var(--text-primary)]">{{ pageTitle }}</h1><p class="mt-1 text-sm leading-6 text-[var(--text-tertiary)]">{{ pageDescription }}</p></div>
    </header>
    <div class="flex-1 overflow-y-auto px-4 py-4 sm:px-6 sm:py-5">
      <div class="mx-auto flex max-w-[1080px] flex-col gap-4 sm:gap-6">
        <DataConnectorsPanel v-if="activeTab === 'connectors'" />
        <div v-else class="rounded-xl border border-[var(--border-main)] bg-[var(--background-menu-white)] p-3 sm:p-4">
          <label class="control-shell flex items-center gap-3 rounded-xl border border-[var(--border-main)] bg-[var(--background-gray-main)] px-3 py-2"><Search :size="18" class="shrink-0 text-[var(--icon-secondary)]" /><input v-model="skillSearchQuery" class="h-8 min-w-0 flex-1 bg-transparent text-sm text-[var(--text-primary)] outline-none" :placeholder="t('Search skills')" /></label>
          <div class="mt-3 flex gap-1 overflow-x-auto" role="tablist" :aria-label="t('Publisher')"><button v-for="source in sourceFilters" :key="source.key" type="button" class="shrink-0 rounded-lg px-3 py-1.5 text-sm" :class="pluginSource === source.key ? 'bg-[var(--Button-primary-black)] text-[var(--text-onblack)]' : 'text-[var(--text-secondary)] hover:bg-[var(--fill-tsp-white-light)]'" @click="pluginSource = source.key">{{ source.label }}</button></div>
        </div>
        <section v-if="activeTab === 'skills'" class="rounded-xl border border-[var(--border-main)] bg-[var(--background-menu-white)] p-4 sm:p-5">
          <div class="mb-4 flex flex-col items-stretch gap-3 sm:flex-row sm:items-center sm:justify-between">
            <div>
              <h2 class="text-lg font-semibold text-[var(--text-primary)]">{{ t('Skills') }}</h2>
              <p class="mt-1 text-sm text-[var(--text-tertiary)]">{{ t('Data skills catalog introduction') }}</p>
            </div>
            <button class="inline-flex h-11 w-full items-center justify-center gap-1.5 rounded-lg border border-[var(--border-btn-main)] px-3 text-sm text-[var(--text-primary)] hover:bg-[var(--fill-tsp-white-light)] sm:h-9 sm:w-auto" :disabled="skillUploading" @click="skillFileInput?.click()">
              <Upload :size="15" />
              {{ skillUploading ? t('Uploading...') : t('Upload') }}
            </button>
            <input ref="skillFileInput" class="hidden" type="file" accept=".md,.zip" @change="uploadSkillPackage" />
          </div>

          <div class="mb-4 flex flex-wrap items-center gap-3">
            <label class="flex items-center gap-2 text-sm text-[var(--text-secondary)]">
              {{ t('Workflow category') }}
              <select v-model="skillCategoryFilter" class="rounded-lg border border-[var(--border-main)] bg-[var(--background-menu-white)] p-2">
                <option value="">{{ t('All categories') }}</option>
                <option v-for="(label, key) in skillCategories" :key="key" :value="key">{{ t(label) }}</option>
              </select>
            </label>
            <label class="flex items-center gap-2 text-sm text-[var(--text-secondary)]">
              {{ t('Data domain') }}
              <select v-model="skillDomainFilter" class="max-w-[220px] rounded-lg border border-[var(--border-main)] bg-[var(--background-menu-white)] p-2">
                <option value="">{{ t('All domains') }}</option>
                <option v-for="domain in skillDomains" :key="domain" :value="domain">{{ domain }}</option>
              </select>
            </label>
            <label class="flex items-center gap-2 text-sm text-[var(--text-secondary)]">
              <input v-model="skillInstalledOnly" type="checkbox" />{{ t('Installed skills only') }}
            </label>
            <button v-if="skillCategoryFilter || skillDomainFilter || skillInstalledOnly || skillSearchQuery" class="text-sm underline" @click="resetSkillFilters">{{ t('Reset skill filters') }}</button>
          </div>
          <div v-if="skillLoading" class="py-8 text-center text-sm text-[var(--text-tertiary)]">{{ t('Loading') }}...</div>
          <div v-else-if="filteredSkills.length === 0" class="rounded-xl border border-dashed border-[var(--border-main)] py-8 text-center text-sm text-[var(--text-tertiary)]">{{ skills.length ? t('No matching skills') : t('No skills yet') }}</div>
          <div v-else class="grid gap-3 md:grid-cols-2">
            <article v-for="skill in pagedSkills" :key="skill.id" class="min-h-[124px] cursor-pointer rounded-lg border border-[var(--border-main)] bg-[var(--background-gray-main)] p-4 hover:bg-[var(--fill-tsp-white-light)]" @click="openSkill(skill.name)">
              <div class="flex items-start justify-between gap-3">
                <div class="min-w-0">
                  <div class="truncate text-sm font-semibold text-[var(--text-primary)]">{{ skill.title || skill.name }}</div>
                  <div class="description-clamp mt-2 text-xs leading-5 text-[var(--text-tertiary)]">{{ skill.description || t('No description') }}</div>
                </div>
                <div class="flex shrink-0 items-center gap-1.5">
                  <span class="rounded border border-[var(--border-main)] px-1.5 py-0.5 text-[11px] text-[var(--text-tertiary)]">{{ t(skill.source === 'official' ? 'Official' : 'Personal') }}</span>
                  <select
                    v-if="canManageSkillScope(skill)"
                    :value="skill.scope"
                    class="h-8 rounded-md border border-[var(--border-main)] bg-[var(--background-menu-white)] px-2 text-xs text-[var(--text-secondary)] outline-none disabled:opacity-50"
                    :disabled="skillScopeUpdating === skill.id || deletingSkillId === skill.id"
                    :aria-label="t('Skill visibility')"
                    @click.stop
                    @change.stop="changeSkillScope(skill, ($event.target as HTMLSelectElement).value as 'user' | 'global')"
                  >
                    <option value="user">{{ t('Personal') }}</option>
                    <option value="global">{{ t('Global') }}</option>
                  </select>
                  <button
                    v-if="canDeletePersonalSkill(skill)"
                    type="button"
                    class="flex h-8 items-center gap-1 rounded-md border border-[var(--border-main)] px-2 text-xs text-[var(--function-error)] hover:bg-[var(--fill-tsp-white-light)] disabled:opacity-50"
                    :disabled="Boolean(deletingSkillId) || skillScopeUpdating === skill.id"
                    :aria-label="t('Delete skill named', { name: skill.title || skill.name })"
                    @click.stop="requestDeleteSkill(skill)"
                  ><LoaderCircle v-if="deletingSkillId === skill.id" :size="14" class="animate-spin" /><Trash2 v-else :size="14" />{{ t(deletingSkillId === skill.id ? 'Deleting skill...' : 'Delete') }}</button>
                  <button
                    v-else
                    class="flex size-8 items-center justify-center rounded-md border border-[var(--border-main)] disabled:opacity-60"
                    :disabled="pluginUpdating === `skill:${skill.id}` || canManageSkillScope(skill)"
                    :title="t(skill.installed && !canManageSkillScope(skill) ? 'Remove from my plugins' : skill.installed ? 'Installed' : 'Add to my plugins')"
                    @click.stop="toggleSkillInstallation(skill)"
                  ><Check v-if="skill.installed" :size="15" /><Plus v-else :size="15" /></button>
                </div>
              </div>
              <SkillMetadata :skill="skill" />
              <div v-if="skill.triggers.length" class="mt-3 flex flex-wrap gap-1">
                <span v-for="trigger in skill.triggers.slice(0, 8)" :key="trigger" class="rounded border border-[var(--border-main)] px-1.5 py-0.5 text-[11px] text-[var(--text-tertiary)]">{{ trigger }}</span>
              </div>
            </article>
          </div>
          <div v-if="skillTotalPages > 1" class="mt-4 flex items-center justify-end gap-2">
            <button class="rounded-lg border border-[var(--border-main)] px-2 py-1 text-xs text-[var(--text-secondary)] disabled:opacity-40" :disabled="skillPage === 1" @click="skillPage--">{{ t('Previous') }}</button>
            <span class="text-xs text-[var(--text-tertiary)]">{{ skillPage }} / {{ skillTotalPages }}</span>
            <button class="rounded-lg border border-[var(--border-main)] px-2 py-1 text-xs text-[var(--text-secondary)] disabled:opacity-40" :disabled="skillPage === skillTotalPages" @click="skillPage++">{{ t('Next') }}</button>
          </div>
        </section>

      </div>
    </div>
    <Dialog v-model:open="skillDetailOpen">
      <DialogContent class="w-[min(1100px,calc(100vw-32px))]">
        <DialogHeader>
          <DialogTitle>{{ selectedSkillDetail?.skill.title || selectedSkillDetail?.skill.name || t('Skill Detail') }}</DialogTitle>
          <p v-if="selectedSkillDetail?.skill.description" class="mt-2 whitespace-pre-wrap text-sm leading-5 text-[var(--text-tertiary)]">
            {{ selectedSkillDetail.skill.description }}
          </p>
          <SkillMetadata v-if="selectedSkillDetail" :skill="selectedSkillDetail.skill" />
          <button
            v-if="selectedSkillDetail && canDeletePersonalSkill(selectedSkillDetail.skill)"
            type="button"
            class="mt-3 inline-flex items-center gap-1.5 self-start rounded-lg border border-[var(--border-main)] px-3 py-2 text-xs text-[var(--function-error)] disabled:opacity-50"
            :disabled="Boolean(deletingSkillId) || savingSkillFile"
            :aria-label="t('Delete skill named', { name: selectedSkillDetail.skill.title || selectedSkillDetail.skill.name })"
            @click="requestDeleteSkill(selectedSkillDetail.skill)"
          ><Trash2 :size="14" />{{ t(deletingSkillId ? 'Deleting skill...' : 'Delete') }}</button>
          <p v-if="selectedSkillDetail?.skill.format === 'data-skill-v1'" class="mt-2 break-all text-xs text-[var(--text-tertiary)]">
            {{ selectedSkillDetail.skill.package_id }} · {{ t('Skill release immutable') }}
            <br />SHA-256: {{ selectedSkillDetail.skill.package_sha256 }}
          </p>
        </DialogHeader>
        <div class="grid h-[70vh] grid-cols-[280px_1fr] overflow-hidden px-6 pb-5 pt-2">
          <div class="overflow-y-auto border-r border-[var(--border-main)] pr-3">
            <div v-for="node in selectedSkillDetail?.tree || []" :key="node.path">
              <SkillTreeNode :node="node" :active-path="selectedSkillFilePath" @select="selectSkillFile" />
            </div>
          </div>
          <div class="min-w-0 overflow-y-auto pl-4">
            <div v-if="selectedSkillFile" class="h-full">
              <div class="mb-3 flex items-center justify-between gap-3">
                <div class="min-w-0 truncate text-xs text-[var(--text-tertiary)]">{{ selectedSkillFile.path }}</div>
                <div class="flex shrink-0 items-center gap-2">
                  <button
                    v-if="!editingSkillFile && selectedSkillDetail?.skill.format !== 'data-skill-v1'"
                    class="rounded-lg border border-[var(--border-main)] px-2 py-1 text-xs text-[var(--text-secondary)] hover:bg-[var(--fill-tsp-white-light)]"
                    :disabled="selectedSkillFile.binary"
                    @click="startEditSkillFile"
                  >
                    {{ t('Edit') }}
                  </button>
                  <template v-else-if="editingSkillFile && selectedSkillDetail?.skill.format !== 'data-skill-v1'">
                    <button class="rounded-lg border border-[var(--border-main)] px-2 py-1 text-xs text-[var(--text-secondary)] hover:bg-[var(--fill-tsp-white-light)]" @click="cancelEditSkillFile">{{ t('Cancel') }}</button>
                    <button class="rounded-lg bg-[var(--Button-primary-black)] px-2 py-1 text-xs text-[var(--text-onblack)] disabled:opacity-50" :disabled="savingSkillFile" @click="saveSkillFile">{{ savingSkillFile ? t('Saving...') : t('Save') }}</button>
                  </template>
                </div>
              </div>
              <textarea
                v-if="editingSkillFile"
                v-model="skillFileDraft"
                class="h-[calc(70vh-92px)] w-full resize-none rounded-xl border border-[var(--border-main)] bg-[var(--background-gray-main)] p-4 font-mono text-sm leading-6 text-[var(--text-primary)] outline-none"
                spellcheck="false"
              />
              <div
                v-else-if="isMarkdownFile(selectedSkillFile.path)"
                class="prose prose-sm max-w-none rounded-xl border border-[var(--border-main)] bg-[var(--background-gray-main)] p-4 text-[var(--text-primary)] dark:prose-invert"
                v-html="renderMarkdown(selectedSkillFile.content)"
              >
              </div>
              <pre v-else class="min-h-full overflow-x-auto rounded-xl border border-[var(--border-main)] bg-[var(--background-gray-main)] p-4 text-sm leading-6 text-[var(--text-primary)]"><code>{{ selectedSkillFile.content }}</code></pre>
            </div>
            <div v-else class="flex h-full items-center justify-center text-sm text-[var(--text-tertiary)]">{{ t('Select a file to preview') }}</div>
          </div>
        </div>
      </DialogContent>
    </Dialog>
    <PersonalSkillDeleteDialog :skill="skillToDelete" :busy="Boolean(deletingSkillId)" :error="deleteError" @confirm="confirmDeleteSkill" @cancel="cancelDeleteSkill" />
  </div>
</template>

<script setup lang="ts">
import DataConnectorsPanel from '@/components/DataConnectorsPanel.vue';
import SkillMetadata from '@/components/SkillMetadata.vue';
import PersonalSkillDeleteDialog from '@/components/PersonalSkillDeleteDialog.vue';
import { skillCategories, filterSkillCatalog } from '@/utils/skillCatalog';
import { computed, defineComponent, h, ref, watch } from 'vue';
import { useI18n } from 'vue-i18n';
import { useRoute } from 'vue-router';
import { Check, Plus, Upload, Folder, FileText, Search, PanelLeft, Trash2, LoaderCircle } from 'lucide-vue-next';
import { marked } from 'marked';
import DOMPurify from 'dompurify';
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { getSkillDetail, installSkill, listSkillCatalog, uninstallSkill, updateSkillFile, updateSkillScope, uploadSkill, type SkillDetailResponse, type SkillFileContent, type SkillFileNode, type SkillInfo } from '@/api/skill';
import { showErrorToast, showSuccessToast } from '@/utils/toast';
import { useLeftPanel } from '@/composables/useLeftPanel';
import { useAuth } from '@/composables/useAuth';
import { usePersonalSkillDeletion } from '@/composables/usePersonalSkillDeletion';

const { t } = useI18n();
const route = useRoute();
const { isLeftPanelShow, toggleLeftPanel } = useLeftPanel();
const { currentUser } = useAuth();
// Only route metadata controls the product page. Legacy tab/settings query
// parameters cannot reveal hidden runtime administration categories.
const activeTab = computed(() => route.meta.catalogType === 'connectors' ? 'connectors' : 'skills');
const pageTitle = computed(() => activeTab.value === 'connectors' ? '数据连接器' : '分析技能库');
const pageDescription = computed(() => activeTab.value === 'connectors'
  ? '了解可用的开放数据源，在对话中检索、获取并保存数据。'
  : '发现并管理可复用的分析技能，在分析数据时按需使用。');
type PluginSource = 'all' | 'official' | 'personal';
const pluginSource = ref<PluginSource>('all');
const sourceFilters = computed<Array<{ key: PluginSource; label: string }>>(() => [
  { key: 'all', label: t('All') }, { key: 'official', label: t('Official') }, { key: 'personal', label: t('Personal') },
]);

const PAGE_SIZE = 6;

const pluginUpdating = ref<string | null>(null);

const skillSearchQuery = ref('');

const skills = ref<SkillInfo[]>([]);

const skillLoading = ref(false);

const skillUploading = ref(false);

const skillFileInput = ref<HTMLInputElement>();

const skillDetailOpen = ref(false);

const selectedSkillDetail = ref<SkillDetailResponse | null>(null);

const selectedSkillFilePath = ref('');

const skillPage = ref(1);

const skillCategoryFilter = ref('');

const skillDomainFilter = ref('');

const skillInstalledOnly = ref(false);

const skillDomains = computed(() => [...new Set(skills.value.flatMap(skill => skill.domains || []))].sort());

const resetSkillFilters = () => {
  skillCategoryFilter.value = '';
  skillDomainFilter.value = '';
  skillInstalledOnly.value = false;
  skillSearchQuery.value = '';
  pluginSource.value = 'all';
};

const editingSkillFile = ref(false);

const savingSkillFile = ref(false);

const skillFileDraft = ref('');

const skillScopeUpdating = ref<string | null>(null);
let skillLoadRevision = 0;

const { canDeletePersonalSkill, deletingSkillId, skillToDelete, deleteError, requestDeleteSkill, confirmDeleteSkill, cancelDeleteSkill } = usePersonalSkillDeletion((skill) => {
  skillLoadRevision += 1;
  skillLoading.value = false;
  skills.value = skills.value.filter(item => item.id !== skill.id);
  if (selectedSkillDetail.value?.skill.id === skill.id) {
    skillDetailOpen.value = false;
    selectedSkillDetail.value = null;
    selectedSkillFilePath.value = '';
    editingSkillFile.value = false;
    skillFileDraft.value = '';
  }
});

const normalizedSkillSearch = computed(() => skillSearchQuery.value.trim().toLowerCase());

const filteredSkills = computed(() => filterSkillCatalog(skills.value, {
  query: normalizedSkillSearch.value, source: pluginSource.value,
  category: skillCategoryFilter.value, domain: skillDomainFilter.value,
  installed: skillInstalledOnly.value,
}));

const skillTotalPages = computed(() => Math.max(1, Math.ceil(filteredSkills.value.length / PAGE_SIZE)));

const pagedSkills = computed(() => filteredSkills.value.slice((skillPage.value - 1) * PAGE_SIZE, skillPage.value * PAGE_SIZE));

const selectedSkillFile = computed<SkillFileContent | null>(() => {
  if (!selectedSkillDetail.value || !selectedSkillFilePath.value) return null;
  return selectedSkillDetail.value.files.find((file) => file.path === selectedSkillFilePath.value) || null;
});

const loadSkills = async () => {
  const revision = ++skillLoadRevision;
  skillLoading.value = true;
  try {
    const result = await listSkillCatalog();
    if (revision === skillLoadRevision) skills.value = result;
  } catch (error) {
    if (revision !== skillLoadRevision) return;
    console.error('Failed to load skills:', error);
    showErrorToast(t('Failed to load skills'));
  } finally {
    if (revision === skillLoadRevision) skillLoading.value = false;
  }
};

const canManageSkillScope = (skill: SkillInfo) => {
  const ownerId = skill.owner_user_id || skill.user_id;
  return Boolean(currentUser.value?.id && ownerId === currentUser.value.id);
};

const toggleSkillInstallation = async (skill: SkillInfo) => {
  if (canManageSkillScope(skill)) return;
  const key = `skill:${skill.id}`;
  pluginUpdating.value = key;
  try {
    const updated = skill.installed
      ? await uninstallSkill(skill.id)
      : await installSkill(skill.id);
    Object.assign(skill, updated);
    showSuccessToast(t(updated.installed ? 'Plugin added' : 'Plugin removed'));
  } catch (error) {
    console.error('Failed to update skill installation:', error);
    showErrorToast(t('Failed to update plugin'));
  } finally {
    pluginUpdating.value = null;
  }
};

const changeSkillScope = async (skill: SkillInfo, scope: 'user' | 'global') => {
  if (!canManageSkillScope(skill) || skill.scope === scope || deletingSkillId.value === skill.id) return;
  const previousScope = skill.scope;
  skillScopeUpdating.value = skill.id;
  try {
    const updated = await updateSkillScope(skill.id, scope);
    Object.assign(skill, updated);
    if (selectedSkillDetail.value?.skill.id === skill.id) {
      selectedSkillDetail.value.skill = updated;
    }
    showSuccessToast(t('Skill visibility updated'));
  } catch (error) {
    skill.scope = previousScope;
    console.error('Failed to update skill visibility:', error);
    showErrorToast(t('Failed to update skill visibility'));
    await loadSkills();
  } finally {
    skillScopeUpdating.value = null;
  }
};

const uploadSkillPackage = async (event: Event) => {
  const input = event.target as HTMLInputElement;
  const file = input.files?.[0];
  if (!file) return;
  skillUploading.value = true;
  try {
    await uploadSkill(file);
    await loadSkills();
    showSuccessToast(t('Skill uploaded'));
  } catch (error) {
    console.error('Failed to upload skill:', error);
    showErrorToast(t('Failed to upload skill'));
  } finally {
    skillUploading.value = false;
    input.value = '';
  }
};

const firstFilePath = (nodes: SkillFileNode[]): string => {
  for (const node of nodes) {
    if (node.type === 'file') return node.path;
    const childPath = firstFilePath(node.children || []);
    if (childPath) return childPath;
  }
  return '';
};

const openSkill = async (name: string) => {
  const revision = skillLoadRevision;
  try {
    const detail = await getSkillDetail(name);
    if (revision !== skillLoadRevision) return;
    selectedSkillDetail.value = detail;
    selectedSkillFilePath.value = firstFilePath(selectedSkillDetail.value.tree);
    editingSkillFile.value = false;
    skillFileDraft.value = '';
    skillDetailOpen.value = true;
  } catch (error) {
    if (revision !== skillLoadRevision) return;
    console.error('Failed to load skill detail:', error);
    showErrorToast(t('Failed to load skill detail'));
  }
};

const selectSkillFile = (path: string) => {
  selectedSkillFilePath.value = path;
  editingSkillFile.value = false;
  skillFileDraft.value = '';
};

const startEditSkillFile = () => {
  if (!selectedSkillFile.value || selectedSkillFile.value.binary) return;
  skillFileDraft.value = selectedSkillFile.value.content;
  editingSkillFile.value = true;
};

const cancelEditSkillFile = () => {
  editingSkillFile.value = false;
  skillFileDraft.value = '';
};

const saveSkillFile = async () => {
  if (!selectedSkillDetail.value || !selectedSkillFile.value) return;
  savingSkillFile.value = true;
  try {
    const path = selectedSkillFile.value.path;
    selectedSkillDetail.value = await updateSkillFile(selectedSkillDetail.value.skill.name, path, skillFileDraft.value);
    selectedSkillFilePath.value = path;
    editingSkillFile.value = false;
    skillFileDraft.value = '';
    await loadSkills();
    showSuccessToast(t('Skill file saved'));
  } catch (error) {
    console.error('Failed to save skill file:', error);
    showErrorToast(t('Failed to save skill file'));
  } finally {
    savingSkillFile.value = false;
  }
};

const isMarkdownFile = (path: string) => path.toLowerCase().endsWith('.md') || path.toLowerCase().endsWith('.markdown');

const renderMarkdown = (content: string) => DOMPurify.sanitize(marked.parse(content || '') as string);

const SkillTreeNode = defineComponent({
  name: 'SkillTreeNode',
  props: {
    node: { type: Object as () => SkillFileNode, required: true },
    activePath: { type: String, default: '' },
  },
  emits: ['select'],
  setup(props, { emit }) {
    const renderNode = (node: SkillFileNode, depth = 0): any => {
      const isFile = node.type === 'file';
      return h('div', [
        h(
          'button',
          {
            class: [
              'flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left text-sm',
              isFile ? 'cursor-pointer hover:bg-[var(--fill-tsp-white-light)]' : 'cursor-default',
              props.activePath === node.path ? 'bg-[var(--fill-tsp-white-main)] text-[var(--text-primary)]' : 'text-[var(--text-secondary)]',
            ],
            style: { paddingLeft: `${8 + depth * 14}px` },
            onClick: () => {
              if (isFile) emit('select', node.path);
            },
          },
          [
            h(isFile ? FileText : Folder, { size: 14, class: 'shrink-0 text-[var(--icon-secondary)]' }),
            h('span', { class: 'truncate' }, node.name),
          ],
        ),
        ...(node.children || []).map((child) => renderNode(child, depth + 1)),
      ]);
    };
    return () => renderNode(props.node);
  },
});

marked.setOptions({ breaks: true, gfm: true });
watch([skillSearchQuery, skillCategoryFilter, skillDomainFilter, skillInstalledOnly, pluginSource], () => { skillPage.value = 1; });
watch(filteredSkills, () => { if (skillPage.value > skillTotalPages.value) skillPage.value = skillTotalPages.value; });
watch(activeTab, (tab) => {
  skillDetailOpen.value = false;
  if (tab === 'skills') void loadSkills();
}, { immediate: true });
</script>
<style scoped>
.description-clamp { display: -webkit-box; overflow: hidden; overflow-wrap: anywhere; -webkit-box-orient: vertical; -webkit-line-clamp: 2; }
</style>
