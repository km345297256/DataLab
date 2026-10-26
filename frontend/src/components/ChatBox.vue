<template>
    <div ref="composerRef" class="chat-composer pb-3 relative">
        <div
            class="chat-composer-surface relative flex flex-col bg-[var(--fill-input-chat)] border border-[var(--border-btn-main)] transition-colors"
            :class="compactComposer
                ? 'min-h-[56px] max-h-[70vh] gap-1 rounded-[18px] py-2'
                : 'max-h-[70vh] gap-2.5 rounded-[16px] py-3'">
            <button v-if="collapseContext && hasContext" type="button" class="chat-context-toggle" :aria-expanded="contextVisible" @click="contextExpanded = !contextExpanded">
                <Paperclip :size="13" /><span class="truncate">{{ contextSummary }}</span><ChevronDown :size="13" :class="{ 'rotate-180': contextVisible }" />
            </button>
            <div v-show="(!collapseContext && (showLibraryActions || hasContext)) || contextVisible" class="chat-context" :class="{ 'chat-context--collapsible': collapseContext }">
            <div v-if="showLibraryActions" class="flex flex-wrap items-center gap-x-3 gap-y-1 px-4 text-[11px] text-[var(--text-tertiary)]">
                <span>{{ isRunning ? '下一轮数据草稿' : '本轮使用的数据' }} · {{ readyFiles.length }} 个文件</span>
                <button v-if="readyFiles.length" type="button" class="hover:underline disabled:opacity-50" :disabled="disabled" @click="saveLibraryOpen = true">保存到我的数据</button>
            </div>
            <p v-if="showLibraryActions && attachments.length > 32" role="alert" class="px-4 text-xs text-red-600">每轮最多使用 32 个文件，请移除部分文件后发送。</p>
            <div v-if="dataDiscovery" class="flex items-center gap-2 px-4 text-xs text-[#2b7659]">
                <Search :size="13" /><span class="min-w-0 flex-1 truncate">{{ discoveryTarget ? `细化查找：${discoveryTarget}` : '查找开放数据' }} · {{ selectedConnectors.length ? `按所选 ${selectedConnectors.length} 个来源查找` : '系统自动选择相关来源' }}</span>
                <button type="button" :disabled="disabled" aria-label="退出查找模式" @click="emit('update:dataDiscovery', false)"><X :size="13" /></button>
            </div>
            <ChatBoxFiles v-if="showFileActions || attachments.length" ref="chatBoxFileListRef" :attachments="attachments" />
            <div v-if="showConnectorActions && selectedConnectors.length" class="flex flex-wrap gap-1.5 px-4">
                <div v-for="id in selectedConnectors" :key="id" class="inline-flex max-w-full items-center gap-2 rounded-md border border-[var(--border-main)] px-2 py-1.5 text-xs">
                    <PlugZap :size="14" /><span class="truncate">{{ connectorTitle(id) }}</span>
                    <button type="button" :disabled="disabled || isRunning" :aria-label="'移除连接器 ' + connectorTitle(id)" @click="selectedConnectors = selectedConnectors.filter(item => item !== id)"><X :size="13" /></button>
                </div>
            </div>
            <div v-if="visibleSelectedSkills.length" class="flex max-h-20 flex-wrap gap-1.5 overflow-y-auto px-4">
                <div
                    v-for="skillName in visibleSelectedSkills"
                    :key="skillName"
                    class="inline-flex h-8 max-w-full items-center gap-1.5 rounded-md border border-[var(--border-main)] bg-[var(--background-menu-white)] px-2 text-xs text-[var(--text-primary)] shadow-sm">
                    <Puzzle :size="14" class="shrink-0 text-[var(--icon-secondary)]" />
                    <span class="truncate font-medium" :title="skillName">{{ skillLabel(skillName, labelSkills) }}</span>
                    <button
                        type="button"
                        class="ml-0.5 flex size-5 shrink-0 items-center justify-center rounded hover:bg-[var(--fill-tsp-white-dark)]"
                        :aria-label="t('Remove skill', { name: skillLabel(skillName, labelSkills) })"
                        :title="t('Remove selected skill')"
                        @click="removeSkill(skillName)">
                        <X :size="13" />
                    </button>
                </div>
            </div>
            </div>
            <div class="chat-box-entry" :class="{ 'chat-box-entry--compact': compactComposer }">
            <div
                class="chat-box-editor min-w-0 overflow-y-auto"
                :class="compactComposer ? 'min-h-10' : 'pl-4 pr-2'">
                <textarea
                    ref="textareaRef"
                    class="chat-message-input flex w-full flex-1 rounded-md border-0 border-input bg-transparent p-0 pt-[1px] text-sm leading-6 shadow-none placeholder:text-[var(--text-disable)] focus-visible:outline-none focus-visible:ring-0 focus-visible:ring-offset-0 disabled:cursor-not-allowed disabled:opacity-50"
                    :class="compactComposer
                        ? '!h-10 !min-h-10 !max-h-10 !resize-none !overflow-y-auto !py-[9px] leading-5'
                        : 'min-h-[46px] max-h-[55vh] resize-y overflow-y-auto'"
                    :rows="rows" :value="modelValue"
                    :disabled="disabled"
                    @input="handleInput"
                    @keydown="handleKeydown" :placeholder="placeholder || t('Give DataLab a task to work on...')"></textarea>
            </div>
            <footer class="chat-box-footer flex w-full flex-row justify-between px-2 sm:px-3">
                <div class="chat-box-actions-start flex items-center gap-1 sm:gap-2">
                    <div ref="actionMenuRef" class="relative">
                        <button
                            type="button"
                            class="relative inline-flex h-10 w-10 shrink-0 items-center justify-center rounded-full border border-[var(--border-main)] text-[var(--text-secondary)] hover:bg-[var(--fill-tsp-gray-main)] sm:h-8 sm:w-8"
                            :class="actionMenuOpen ? 'bg-[var(--fill-tsp-gray-main)]' : ''"
                            :disabled="disabled"
                            :aria-expanded="actionMenuOpen"
                            aria-haspopup="menu"
                            :title="t('Add content')"
                            @click="toggleActionMenu"
                        >
                            <Plus :size="18" />
                            <span v-if="visibleSelectedSkills.length > 0"
                                class="absolute -right-1 -top-1 flex h-4 min-w-4 items-center justify-center rounded-full bg-[var(--Button-primary-black)] px-1 text-[10px] leading-none text-[var(--text-onblack)]">
                                {{ visibleSelectedSkills.length }}
                            </span>
                        </button>

                        <template v-if="actionMenuOpen">
                            <div
                                class="absolute left-0 z-[60] w-[154px] overflow-hidden rounded-lg border border-[var(--border-main)] bg-[var(--background-menu-white)] p-1.5 shadow-[0_12px_32px_rgba(0,0,0,0.16)] sm:w-[190px]"
                                :class="skillMenuPlacement === 'down' ? 'top-full mt-2' : 'bottom-full mb-2'"
                                role="menu"
                            >
                                <button v-if="showFileActions" type="button" class="flex h-9 w-full items-center gap-2 rounded-md px-2 text-left text-[13px] text-[var(--text-primary)] hover:bg-[var(--fill-tsp-white-light)]" role="menuitem" @click="handleUploadFileAction">
                                    <Paperclip :size="15" class="shrink-0" />
                                    <span class="truncate">{{ t('Upload file') }}</span>
                                </button>
                                <button
                                    type="button"
                                    class="flex h-9 w-full items-center gap-2 rounded-md px-2 text-left text-[13px] text-[var(--text-primary)] hover:bg-[var(--fill-tsp-white-light)]"
                                    :class="actionSkillMenuOpen ? 'bg-[var(--fill-tsp-white-main)]' : ''"
                                    role="menuitem"
                                    aria-haspopup="menu"
                                    :aria-expanded="actionSkillMenuOpen"
                                    @mouseenter="openActionSkillMenu"
                                    @click="focusActionSkillMenu"
                                >
                                    <Puzzle :size="15" class="shrink-0" />
                                    <span class="min-w-0 flex-1 truncate">{{ t('Use skill') }}</span>
                                    <ChevronRight :size="14" class="shrink-0 text-[var(--icon-tertiary)]" />
                                </button>
                            </div>

                            <div
                                v-if="actionSkillMenuOpen"
                                class="fixed left-3 right-3 z-[61] flex max-w-[330px] flex-col overflow-hidden rounded-lg border border-[var(--border-main)] bg-[var(--background-menu-white)] shadow-[0_12px_32px_rgba(0,0,0,0.16)] lg:absolute lg:left-[196px] lg:right-auto lg:w-[330px]"
                                :class="skillMenuPlacement === 'down' ? 'top-20 max-h-[min(390px,calc(100dvh-100px))] lg:top-full lg:mt-2 lg:max-h-[min(390px,52vh)]' : 'bottom-24 max-h-[min(390px,calc(100dvh-112px))] lg:bottom-full lg:mb-2 lg:max-h-[min(390px,52vh)]'"
                                role="menu"
                                :aria-label="t('Available skills')"
                            >
                                <div class="control-shell flex h-10 shrink-0 items-center gap-2 border-b border-[var(--border-main)] px-2.5">
                                    <Search :size="14" class="shrink-0 text-[var(--icon-tertiary)]" />
                                    <input
                                        ref="actionSkillSearchInputRef"
                                        v-model="actionSkillQuery"
                                        type="search"
                                        class="h-7 min-w-0 flex-1 bg-transparent text-[13px] text-[var(--text-primary)] outline-none placeholder:text-[var(--text-tertiary)]"
                                        :placeholder="t('Search skills')"
                                        @keydown.esc.prevent="closeActionSkillMenu"
                                    />
                                </div>
                                <div v-if="skillsLoading" class="px-3 py-5 text-center text-xs text-[var(--text-tertiary)]">{{ t('Loading') }}...</div>
                                <div v-else-if="actionFilteredSkills.length === 0" class="px-3 py-5 text-center text-xs text-[var(--text-tertiary)]">{{ t('No matching skills') }}</div>
                                <div v-else class="min-h-0 flex-1 overflow-y-auto px-1.5 pb-2.5 pt-1.5">
                                    <button
                                        v-for="skill in actionFilteredSkills"
                                        :key="skill.name"
                                        type="button"
                                        class="flex min-h-10 w-full items-start gap-2 rounded-md px-2 py-1.5 text-left hover:bg-[var(--fill-tsp-white-light)]"
                                        role="menuitemcheckbox"
                                        :aria-checked="selectedSkills.includes(skill.name)"
                                        @click="toggleActionSkill(skill)"
                                    >
                                        <Puzzle :size="14" class="mt-0.5 shrink-0 text-[var(--icon-secondary)]" />
                                        <span class="min-w-0 flex-1">
                                            <span class="flex items-center gap-1.5 text-[13px] font-medium leading-4 text-[var(--text-primary)]">
                                                <span class="truncate">{{ skill.title || skill.name }}</span>
                                                <Check v-if="selectedSkills.includes(skill.name)" :size="13" class="shrink-0" />
                                            </span>
                                            <span class="mt-1 block text-[11px] text-[var(--text-tertiary)]">
                                                {{ t(skillCategories[skillCategory(skill)]) }} · {{ skill.version || t('Legacy skill') }}
                                                <span v-if="skill.format === 'data-skill-v1'"> · {{ t('Skill not verified') }}</span>
                                            </span>
                                            <span v-if="skill.description" class="mt-0.5 block truncate text-[11px] leading-4 text-[var(--text-tertiary)]">{{ skill.description }}</span>
                                        </span>
                                    </button>
                                </div>
                                <div class="grid shrink-0 grid-cols-2 gap-1 border-t border-[var(--border-main)] p-2">
                                    <button type="button" class="flex h-8 items-center gap-1.5 rounded-md px-2 text-[12px] text-[var(--text-secondary)] hover:bg-[var(--fill-tsp-white-light)]" @click="openSkillPicker">
                                        <CirclePlus :size="14" />
                                        <span class="truncate">{{ t('Add skill') }}</span>
                                    </button>
                                    <button type="button" class="flex h-8 items-center gap-1.5 rounded-md px-2 text-[12px] text-[var(--text-secondary)] hover:bg-[var(--fill-tsp-white-light)]" @click="openSkillManagement">
                                        <Settings2 :size="14" />
                                        <span class="truncate">{{ t('Manage skills') }}</span>
                                    </button>
                                </div>
                            </div>
                        </template>
                    </div>
                    <button v-if="showConnectorActions" type="button" @click="openConnectorPicker"
                        class="inline-flex h-8 items-center gap-1.5 rounded-full border border-[var(--border-main)] px-3 text-xs text-[var(--text-secondary)] hover:bg-[var(--fill-tsp-gray-main)]"
                        :class="{ 'chat-connector-action--compact': compactComposer && collapseContext }"
                        :disabled="disabled || isRunning" aria-haspopup="dialog" :aria-expanded="connectorPickerOpen" :aria-label="`连接器${selectedConnectors.length ? ` ${selectedConnectors.length}` : ''}`" title="选择数据连接器">
                        <PlugZap :size="14" /><span :class="{ 'sr-only': compactComposer && collapseContext }">连接器</span><span v-if="selectedConnectors.length" :class="{ 'chat-connector-count': compactComposer && collapseContext }">{{ selectedConnectors.length }}</span>
                    </button>
                    <button v-if="showMcpActions" @click="mcpDialogOpen = true"
                        class="relative rounded-full border border-[var(--border-main)] inline-flex items-center justify-center gap-1 clickable cursor-pointer text-xs text-[var(--text-secondary)] hover:bg-[var(--fill-tsp-gray-main)] w-10 h-10 sm:w-8 sm:h-8 p-0 data-[popover-trigger]:bg-[var(--fill-tsp-gray-main)] shrink-0"
                        :disabled="disabled"
                        aria-expanded="false" aria-haspopup="dialog"
                        :title="t('MCP Tools')">
                        <PlugZap :size="16" />
                        <span v-if="selectedMcpServers.length > 0"
                            class="absolute -right-1 -top-1 flex h-4 min-w-4 items-center justify-center rounded-full bg-[var(--Button-primary-black)] px-1 text-[10px] leading-none text-[var(--text-onblack)]">
                            {{ selectedMcpServers.length }}
                        </span>
                    </button>
                </div>
                <div class="chat-box-actions-end flex gap-2">
                    <button v-if="!isRunning || sendEnabled || hideStopButton"
                        type="button"
                        class="whitespace-nowrap text-sm font-medium focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-50 text-primary-foreground hover:bg-primary/90 p-0 w-10 h-10 sm:w-8 sm:h-8 rounded-full flex items-center justify-center transition-colors hover:opacity-90"
                        :class="!sendEnabled ? 'cursor-not-allowed bg-[var(--fill-tsp-white-dark)]' : 'cursor-pointer bg-[var(--Button-primary-black)]'"
                        :disabled="!sendEnabled"
                        :aria-label="t('Send message')"
                        :title="t('Send message')"
                        @click="handleSubmit">
                        <SendIcon :disabled="!sendEnabled" />
                    </button>
                    <button v-else-if="!hideStopButton" type="button" :aria-label="t('Stop task')" :title="t('Stop task')" @click="handleStop"
                        class="inline-flex items-center justify-center whitespace-nowrap text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring bg-[var(--Button-primary-black)] text-[var(--text-onblack)] gap-[4px] hover:opacity-90 rounded-full p-0 w-10 h-10 sm:w-8 sm:h-8">
                        <span class="sr-only">{{ t('Stop task') }}</span>
                        <div class="w-[10px] h-[10px] bg-[var(--icon-onblack)] rounded-[2px]">
                        </div>
                    </button>
                </div>
            </footer>
            </div>
        </div>
        <DataConnectorPicker v-if="showConnectorActions" v-model:open="connectorPickerOpen" v-model:selected="selectedConnectors" :disabled="disabled || isRunning" @catalog="connectorCatalog = $event" />
        <SkillDialog v-model:open="skillDialogOpen" v-model:selected-skills="selectedSkills" />
        <MCPDialog v-if="showMcpActions" v-model:open="mcpDialogOpen" v-model:selected-servers="selectedMcpServers" />
        <DataLibraryPicker v-if="showLibraryActions" :exclude-file-ids="readyFiles.map(file => file.file_id)" :visible="libraryPickerOpen" @close="libraryPickerOpen = false" @select="addLibraryFiles" />
        <SaveToLibraryDialog v-if="showLibraryActions" :visible="saveLibraryOpen" :files="readyFiles" :session-id="sessionId" @close="saveLibraryOpen = false" @saved="saveLibraryOpen = false" />
    </div>
</template>

<script setup lang="ts">
import { ref, watch, computed, nextTick, onMounted, onUnmounted } from 'vue';
import DataConnectorPicker from './DataConnectorPicker.vue';
import DataLibraryPicker from './DataLibraryPicker.vue';
import SaveToLibraryDialog from './SaveToLibraryDialog.vue';
import { readyExplorationFiles, mergeExplorationFiles } from '../utils/explorationFiles';
import { listDataConnectors, type DataConnector } from '@/api/dataConnector';
import SendIcon from './icons/SendIcon.vue';
import { useI18n } from 'vue-i18n';
import ChatBoxFiles from './ChatBoxFiles.vue';
import SkillDialog from './SkillDialog.vue';
import { skillLabel } from '@/utils/skillLabel';
import { filterSkillCatalog, skillCategory, skillCategories } from '@/utils/skillCatalog';
import MCPDialog from './MCPDialog.vue';
import { Check, ChevronDown, ChevronRight, CirclePlus, Paperclip, PlugZap, Plus, Puzzle, Search, Settings2, X } from 'lucide-vue-next';
import type { FileInfo } from '../api/file';
import { getSkillPreferences, listSkills, listSkillCatalog, type SkillInfo } from '../api/skill';
import { showErrorToast } from '../utils/toast';
import { useSettingsDialog } from '../composables/useSettingsDialog';
import { eventBus } from '../utils/eventBus';
import { EVENT_SKILL_CATALOG_UPDATED, EVENT_SKILL_PREFERENCES_UPDATED } from '../constants/event';

const { t } = useI18n();
const { openSettingsDialog } = useSettingsDialog();
const hasTextInput = ref(false);
const chatBoxFileListRef = ref();
const skillDialogOpen = ref(false);
const libraryPickerOpen = ref(false);
const saveLibraryOpen = ref(false);
const readyFiles = computed(() => readyExplorationFiles(props.attachments));
const contextExpanded = ref(false);
const hasUploadActivity = computed(() => props.attachments.some(file => ['uploading', 'failed'].includes((file as FileInfo & { status?: string }).status ?? '')));
const hasContext = computed(() => Boolean(props.attachments.length || selectedConnectors.value.length || visibleSelectedSkills.value.length || props.dataDiscovery));
const contextVisible = computed(() => hasContext.value && (contextExpanded.value || hasUploadActivity.value || props.attachments.length > 32));
const contextSummary = computed(() => [
    props.dataDiscovery ? '查找开放数据' : '',
    props.attachments.length ? `${props.isRunning ? '下轮数据' : '数据'} ${props.attachments.length}` : '',
    hasUploadActivity.value ? '上传状态' : '',
    selectedConnectors.value.length ? `连接器 ${selectedConnectors.value.length}` : '',
    visibleSelectedSkills.value.length ? `技能 ${visibleSelectedSkills.value.length}` : '',
].filter(Boolean).join(' · '));
const addLibraryFiles = (files: FileInfo[]) => {
    const merged = mergeExplorationFiles(props.attachments, files);
    if (merged.length > 32) { showErrorToast('每轮最多使用 32 个文件，请先移除部分文件。'); return; }
    props.attachments.splice(0, props.attachments.length, ...merged);
    libraryPickerOpen.value = false;
};
const connectorPickerOpen = ref(false);
const connectorCatalog = ref<DataConnector[]>([]);
const selectedConnectors = computed({ get: () => props.selectedConnectors ?? [], set: value => emit('update:selectedConnectors', value) });
const connectorTitle = (id: string) => connectorCatalog.value.find(item => item.id === id)?.title || id;
const openConnectorPicker = () => { actionMenuOpen.value = false; connectorPickerOpen.value = true; };

const mcpDialogOpen = ref(false);
const composerRef = ref<HTMLElement>();
const textareaRef = ref<HTMLTextAreaElement>();
const actionMenuRef = ref<HTMLElement>();
const actionSkillSearchInputRef = ref<HTMLInputElement>();
const availableSkills = ref<SkillInfo[]>([]);
const labelSkills = ref<SkillInfo[]>([]);
const skillsLoading = ref(false);
const skillsLoaded = ref(false);
const autoEnabledSkillNames = ref(new Set<string>());
const actionMenuOpen = ref(false);
const actionSkillMenuOpen = ref(false);
const actionSkillQuery = ref('');

const props = withDefaults(defineProps<{
    modelValue: string;
    rows: number;
    isRunning: boolean;
    attachments: FileInfo[];
    selectedSkills?: string[];
    selectedMcpServers?: string[];
    selectedConnectors?: string[];
    showConnectorActions?: boolean;
    dataDiscovery?: boolean;
    discoveryTarget?: string;
    showLibraryActions?: boolean;
    sessionId?: string;
    skillMenuPlacement?: 'up' | 'down';
    hideStopButton?: boolean;
    allowSendFilesOnly?: boolean;
    placeholder?: string;
    showFileActions?: boolean;
    showMcpActions?: boolean;
    disabled?: boolean;
    submitOnEnter?: boolean;
    compactComposer?: boolean;
    collapseContext?: boolean;
}>(), {
    showConnectorActions: false,
    dataDiscovery: false,
    showLibraryActions: false,
    showFileActions: true,
    showMcpActions: true,
    disabled: false,
    submitOnEnter: false,
    compactComposer: false,
    collapseContext: false,
});

const emit = defineEmits<{
    (e: 'update:modelValue', value: string): void;
    (e: 'update:selectedSkills', value: string[]): void;
    (e: 'update:selectedMcpServers', value: string[]): void;
    (e: 'update:selectedConnectors', value: string[]): void;
    (e: 'update:dataDiscovery', value: boolean): void;
    (e: 'submit'): void;
    (e: 'stop'): void;
}>();

const selectedSkills = computed({
    get: () => props.selectedSkills ?? [],
    set: (value: string[]) => emit('update:selectedSkills', value),
});

const selectedMcpServers = computed({
    get: () => props.selectedMcpServers ?? [],
    set: (value: string[]) => emit('update:selectedMcpServers', value),
});

const visibleSelectedSkills = computed(() => selectedSkills.value.filter(
    (name) => !autoEnabledSkillNames.value.has(name.trim().toLowerCase()),
));
watch(hasContext, present => { if (!present) contextExpanded.value = false; });

const sendEnabled = computed(() => {
    if (props.disabled || (props.isRunning && !props.dataDiscovery) || (props.showLibraryActions && props.attachments.length > 32)) return false;
    const hasFiles = (props.attachments?.length ?? 0) > 0;
    const allUploaded = chatBoxFileListRef.value?.isAllUploaded ?? true;
    if (hasFiles && !allUploaded) return false;
    if (props.allowSendFilesOnly) {
        return hasTextInput.value || hasFiles;
    }
    return hasTextInput.value;
});

const actionFilteredSkills = computed(() => {
    const query = actionSkillQuery.value.trim().toLocaleLowerCase();
    const manuallySelectable = availableSkills.value.filter(
        (skill) => !autoEnabledSkillNames.value.has(skill.name.trim().toLowerCase()),
    );
    return filterSkillCatalog(manuallySelectable, { query, category: '', domain: '', source: 'all', installed: false });
});

const setAutoEnabledSkills = (names: string[]) => {
    autoEnabledSkillNames.value = new Set(names.map((name) => name.trim().toLowerCase()));
};

const handleSkillPreferencesUpdated = (payload: unknown) => {
    if (Array.isArray(payload) && payload.every((name) => typeof name === 'string')) {
        setAutoEnabledSkills(payload);
    }
};

let skillCatalogRevision = 0;
const loadSkillLabels = async () => {
    const revision = skillCatalogRevision;
    try { const skills = await listSkillCatalog(); if (revision === skillCatalogRevision) labelSkills.value = skills; }
    catch { if (revision === skillCatalogRevision) labelSkills.value = []; }
};

const handleSkillCatalogUpdated = (payload: unknown) => {
    skillCatalogRevision += 1;
    if (payload && typeof payload === 'object' && 'deletedSkillName' in payload && typeof payload.deletedSkillName === 'string') {
        const name = payload.deletedSkillName;
        selectedSkills.value = selectedSkills.value.filter(item => item !== name);
        availableSkills.value = availableSkills.value.filter(item => item.name !== name);
        autoEnabledSkillNames.value.delete(name.trim().toLowerCase());
    }
    void loadSkillLabels();
    void loadAutoEnabledSkills();
    skillsLoaded.value = false;
    if (actionSkillMenuOpen.value) void loadAvailableSkills();
};

const loadAutoEnabledSkills = async () => {
    const revision = skillCatalogRevision;
    try {
        const preferences = await getSkillPreferences();
        if (revision === skillCatalogRevision) setAutoEnabledSkills(preferences);
    } catch (error) {
        console.error('Failed to load automatic skill preferences:', error);
    }
};

const loadAvailableSkills = async () => {
    if (skillsLoaded.value || skillsLoading.value) return;
    const revision = skillCatalogRevision;
    skillsLoading.value = true;
    try {
        const [skills, preferences] = await Promise.all([listSkills(), getSkillPreferences()]);
        if (revision !== skillCatalogRevision) return;
        availableSkills.value = skills;
        setAutoEnabledSkills(preferences);
        skillsLoaded.value = true;
    } catch (error) {
        if (revision !== skillCatalogRevision) return;
        console.error('Failed to load skills:', error);
        showErrorToast(t('Failed to load skills'));
        actionSkillMenuOpen.value = false;
    } finally {
        skillsLoading.value = false;
        if (revision !== skillCatalogRevision && actionSkillMenuOpen.value) void loadAvailableSkills();
    }
};

const handleInput = (event: Event) => {
    const target = event.target as HTMLTextAreaElement;
    emit('update:modelValue', target.value);
};

const handleKeydown = (event: KeyboardEvent) => {
    if (event.isComposing || event.keyCode === 229) {
        // Let the input method commit the selected candidate without triggering an action.
        return;
    }

    if (event.key !== 'Enter') return;
    if (props.submitOnEnter) {
        if (event.shiftKey || event.ctrlKey || event.metaKey || event.altKey) return;
        event.preventDefault();
        if (sendEnabled.value) handleSubmit();
        return;
    }
    if (!event.ctrlKey && !event.metaKey) {
        return;
    }

    // Ctrl/Command + Enter submits; plain Enter keeps textarea newline behavior.
    if (sendEnabled.value) {
        event.preventDefault();
        handleSubmit();
    }
};

const removeSkill = (name: string) => {
    selectedSkills.value = selectedSkills.value.filter((skill) => skill !== name);
};

const toggleActionMenu = () => {
    if (props.disabled) return;
    actionMenuOpen.value = !actionMenuOpen.value;
    if (!actionMenuOpen.value) closeActionSkillMenu();
};

const openActionSkillMenu = () => {
    if (actionSkillMenuOpen.value) return;
    actionSkillMenuOpen.value = true;
    actionSkillQuery.value = '';
    loadAvailableSkills();
};

const focusActionSkillMenu = () => {
    openActionSkillMenu();
    nextTick(() => actionSkillSearchInputRef.value?.focus());
};

const closeActionSkillMenu = () => {
    actionSkillMenuOpen.value = false;
    actionSkillQuery.value = '';
};

const toggleActionSkill = (skill: SkillInfo) => {
    if (selectedSkills.value.includes(skill.name)) {
        removeSkill(skill.name);
    } else {
        selectedSkills.value = [...selectedSkills.value, skill.name];
    }
};

const openSkillPicker = () => {
    actionMenuOpen.value = false;
    closeActionSkillMenu();
    skillDialogOpen.value = true;
};

const openSkillManagement = () => {
    actionMenuOpen.value = false;
    closeActionSkillMenu();
    openSettingsDialog('skills');
};

const handleSubmit = () => {
    if (!sendEnabled.value) return;
    actionMenuOpen.value = false;
    closeActionSkillMenu();
    emit('submit');
};

const handleStop = () => {
    emit('stop');
};

const uploadFile = () => {
    chatBoxFileListRef.value?.uploadFile();
};

const handleUploadFileAction = () => {
    actionMenuOpen.value = false;
    closeActionSkillMenu();
    uploadFile();
};

watch(() => props.modelValue, (value) => {
    hasTextInput.value = value.trim() !== '';
}, { immediate: true });

watch(skillDialogOpen, (isOpen, wasOpen) => {
    if (!isOpen && wasOpen) skillsLoaded.value = false;
});

watch(() => props.disabled, (disabled) => {
    if (!disabled) return;
    actionMenuOpen.value = false;
    closeActionSkillMenu();
});

const handleDocumentPointerDown = (event: PointerEvent) => {
    const target = event.target as Node;
    if (actionMenuOpen.value && !actionMenuRef.value?.contains(target)) {
        actionMenuOpen.value = false;
        closeActionSkillMenu();
    }
};

const handleDocumentKeydown = (event: KeyboardEvent) => {
    if (event.key !== 'Escape' || !actionMenuOpen.value) return;
    if (actionSkillMenuOpen.value) closeActionSkillMenu();
    else actionMenuOpen.value = false;
};

onMounted(() => {
    document.addEventListener('pointerdown', handleDocumentPointerDown);
    document.addEventListener('keydown', handleDocumentKeydown);
    eventBus.on(EVENT_SKILL_CATALOG_UPDATED, handleSkillCatalogUpdated);
    eventBus.on(EVENT_SKILL_PREFERENCES_UPDATED, handleSkillPreferencesUpdated);
    loadAutoEnabledSkills();
    void loadSkillLabels();
    if (props.showConnectorActions) void listDataConnectors().then(items => { connectorCatalog.value = items; }).catch(() => {});
});
onUnmounted(() => {
    document.removeEventListener('pointerdown', handleDocumentPointerDown);
    document.removeEventListener('keydown', handleDocumentKeydown);
    eventBus.off(EVENT_SKILL_CATALOG_UPDATED, handleSkillCatalogUpdated);
    eventBus.off(EVENT_SKILL_PREFERENCES_UPDATED, handleSkillPreferencesUpdated);
});
defineExpose({ uploadFile, openConnectorPicker, openLibraryPicker: () => { libraryPickerOpen.value = true; }, focus: () => textareaRef.value?.focus() });
</script>

<style scoped>
.chat-context { display: flex; min-height: 0; flex-direction: column; gap: 10px; }
.chat-context--collapsible { max-height: min(180px, 25dvh); overflow-y: auto; overscroll-behavior: contain; padding: 4px 0 8px; }
.chat-context-toggle { display: flex; align-items: center; align-self: flex-start; min-width: 0; max-width: calc(100% - 24px); gap: 6px; margin: 0 12px; padding: 2px 4px; color: var(--text-tertiary); font-size: 11px; line-height: 20px; border-radius: 5px; }
.chat-context-toggle:hover { color: var(--text-primary); background: var(--fill-tsp-gray-main); }
.chat-context-toggle svg { flex: none; }
.chat-connector-action--compact { position: relative; justify-content: center; width: 32px; flex-shrink: 0; padding: 0; }
.chat-connector-count { position: absolute; top: -5px; right: -5px; min-width: 14px; padding: 0 3px; border-radius: 8px; font-size: 9px; line-height: 14px; background: var(--fill-tsp-white-dark); color: var(--text-secondary); }
.chat-box-entry { display: flex; min-width: 0; flex-direction: column; gap: 12px; }
/* Reserve space for every visible action, including DataLab connectors. */
.chat-box-entry--compact { display: grid; grid-template-columns: max-content minmax(0, 1fr) max-content; align-items: center; gap: 8px; padding: 0 12px; }
.chat-box-entry--compact > .chat-box-editor { grid-column: 2; grid-row: 1; }
.chat-box-entry--compact > .chat-box-footer { display: contents; }
.chat-box-entry--compact .chat-box-actions-start { grid-column: 1; grid-row: 1; }
.chat-box-entry--compact .chat-box-actions-end { grid-column: 3; grid-row: 1; }
@media (max-width: 639px) { .chat-box-entry--compact { gap: 6px; padding: 0 8px; } }

.chat-composer-surface { box-shadow: 0 2px 10px rgb(0 0 0 / 1%); }
.chat-composer-surface:focus-within { border-color: #9bbdad; }
</style>
