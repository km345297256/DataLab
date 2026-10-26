<template>
  <section class="skill-creation-card" aria-label="技能创建结果">
    <p v-if="isShare" class="skill-note">技能创建记录仅对会话所有者可见。</p>
    <template v-else>
      <header class="skill-heading">
        <div class="skill-icon"><Puzzle :size="23" aria-hidden="true" /></div>
        <div class="min-w-0 flex-1">
          <p class="skill-eyebrow">{{ draft.installed ? '已添加到我的技能' : '技能已创建 · 待你选择' }}</p>
          <h3>{{ draft.name }}</h3>
        </div>
      </header>
      <div class="skill-body">
        <p v-if="draft.description" class="skill-description">{{ draft.description }}</p>
        <section v-if="draft.capabilities?.length" class="skill-section" aria-label="技能能力">
          <h4>可以做什么</h4>
          <ul><li v-for="capability in draft.capabilities" :key="capability">{{ capability }}</li></ul>
        </section>
        <section v-if="draft.applicability?.length" class="skill-section" aria-label="技能适用范围">
          <h4>适用场景</h4>
          <ul><li v-for="scope in draft.applicability" :key="scope">{{ scope }}</li></ul>
        </section>
        <p class="skill-validation"><CheckCircle2 :size="15" aria-hidden="true" />技能包结构检查通过<span v-if="draft.validation.script_syntax === 'passed'"> · 脚本语法检查通过</span></p>
        <p v-if="draft.validation.synthetic_execution === 'passed'" class="skill-validation"><CheckCircle2 :size="15" aria-hidden="true" />{{ draft.validation.synthetic_passed_count }} 个合成用例运行验证通过</p>
        <p v-if="draft.validation.synthetic_execution === 'unsupported'" class="skill-note">此技能尚无适用的自动运行验证，首次使用前仍需检查合成用例。</p>
        <p v-else-if="draft.validation.synthetic_execution !== 'passed'" class="skill-note">以上为静态检查，尚未重新运行技能中的完整流程。</p>
        <p v-if="draft.validation.synthetic_execution === 'passed'" class="skill-note">验证使用小型合成数据；实际数据的完整分析尚未运行。</p>
        <details v-if="draft.resources.length" class="skill-files">
          <summary>包含 {{ draft.resources.length }} 个文件</summary>
          <ul><li v-for="file in draft.resources" :key="file"><button type="button" :disabled="!available" :aria-label="`预览 ${file}`" @click="openPreview(file, $event)"><FileCode2 :size="14" aria-hidden="true" /><span>{{ file }}</span><Eye :size="14" class="skill-file-preview-icon" aria-hidden="true" /></button></li></ul>
        </details>
        <p v-if="readError" role="alert" class="skill-error">{{ readError }} <button type="button" :disabled="loading" @click="refresh">重试读取</button></p>
        <p v-if="actionError" role="alert" class="skill-error">{{ actionError }}</p>
        <p v-if="actionNotice" role="status" class="skill-note">{{ actionNotice }}</p>
      </div>
      <footer class="skill-footer">
        <p class="skill-note">{{ draft.installed ? '可在分析技能库中管理和使用。' : '你可以添加到技能库，或直接下载技能包。' }}</p>
        <div class="skill-actions">
          <button type="button" class="skill-secondary" :disabled="!available || downloading" @click="download">
            <LoaderCircle v-if="downloading" :size="15" class="animate-spin" aria-hidden="true" /><Download v-else :size="15" aria-hidden="true" />{{ downloading ? '正在下载…' : '下载技能包' }}
          </button>
          <button type="button" class="skill-primary" :disabled="!available || installing || draft.installed" @click="install">
            <LoaderCircle v-if="loading || installing" :size="15" class="animate-spin" aria-hidden="true" /><Check v-else-if="draft.installed" :size="15" aria-hidden="true" /><Plus v-else :size="15" aria-hidden="true" />{{ draft.installed ? '已添加到我的技能' : installing ? '正在添加…' : '添加到我的技能' }}
          </button>
        </div>
      </footer>
      <SkillResourcePreview v-if="previewPath" :key="draft.id" :draft-id="draft.id" :skill-name="draft.name" :resources="draft.resources" :initial-path="previewPath" @close="closePreview" />
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed, nextTick, onUnmounted, ref, watch } from 'vue';
import { Check, CheckCircle2, Download, Eye, FileCode2, LoaderCircle, Plus, Puzzle } from 'lucide-vue-next';
import SkillResourcePreview from './SkillResourcePreview.vue';
import { downloadSkillDraft, getSkillDraft, installSkillDraft } from '../api/skillDraft';
import type { SkillCreationView } from '../types/skillCreation';
import { skillPackageFilename } from '../utils/skillCreation';
import { eventBus } from '../utils/eventBus';
import { EVENT_SKILL_CATALOG_UPDATED } from '../constants/event';

const props = defineProps<{ initial: SkillCreationView; isShare?: boolean }>();
const draft = ref(props.initial);
const loading = ref(false), loaded = ref(false), installing = ref(false), downloading = ref(false);
const readError = ref(''), actionError = ref(''), actionNotice = ref('');
const previewPath = ref('');
let previewTrigger: HTMLElement | null = null;
function openPreview(path: string, event: MouseEvent) {
  if (!available.value || !draft.value.resources.includes(path)) return;
  previewTrigger = event.currentTarget as HTMLElement;
  previewPath.value = path;
}
async function closePreview() {
  previewPath.value = '';
  await nextTick();
  if (active && !props.isShare && previewTrigger?.isConnected) previewTrigger.focus();
  previewTrigger = null;
}
const available = computed(() => loaded.value && !loading.value && !readError.value && !props.isShare);
let generation = 0, active = true, refreshQueued = false;
const requests = new Set<AbortController>();
const urls = new Set<string>();
function request() { const controller = new AbortController(); requests.add(controller); return controller; }
function current(version: number, id: string) { return active && !props.isShare && version === generation && props.initial.id === id; }
function abort() { generation += 1; for (const request of requests) request.abort(); requests.clear(); }

async function refresh() {
  if (!active || props.isShare || loading.value || installing.value) return;
  const id = props.initial.id, version = generation, controller = request();
  loading.value = true;
  try {
    const result = await getSkillDraft(id, controller.signal);
    if (!current(version, id)) return;
    draft.value = result; loaded.value = true; readError.value = '';
  } catch {
    if (current(version, id)) readError.value = '暂时无法读取技能状态，请重试。';
  } finally {
    requests.delete(controller);
    if (current(version, id)) { loading.value = false; drainRefresh(); }
  }
}

function drainRefresh() {
  if (!refreshQueued || loading.value || installing.value) return;
  refreshQueued = false;
  void refresh();
}

function handleSkillCatalogUpdated(payload: unknown) {
  if (!active || props.isShare || !payload || typeof payload !== 'object') return;
  if (!('deletedSkillId' in payload) || typeof payload.deletedSkillId !== 'string') return;
  if (payload.deletedSkillId !== draft.value.installed_skill_id
    && (!('deletedSkillName' in payload) || payload.deletedSkillName !== draft.value.name)) return;
  actionNotice.value = ''; actionError.value = '';
  refreshQueued = true;
  drainRefresh();
}
eventBus.on(EVENT_SKILL_CATALOG_UPDATED, handleSkillCatalogUpdated);

async function install() {
  if (!available.value || installing.value || draft.value.installed) return;
  const id = props.initial.id, version = generation, controller = request();
  installing.value = true; actionError.value = ''; actionNotice.value = '';
  try {
    const result = await installSkillDraft(id, controller.signal);
    if (!current(version, id)) return;
    draft.value = result;
    if (result.installed) {
      actionNotice.value = '已添加到我的技能，可在需要时选择使用。';
      eventBus.emit(EVENT_SKILL_CATALOG_UPDATED);
    } else actionError.value = '尚未添加成功，请重试。';
  } catch {
    if (current(version, id)) actionError.value = '添加失败，请重试；你也可以直接下载技能包。';
  } finally {
    requests.delete(controller);
    if (current(version, id)) { installing.value = false; drainRefresh(); }
  }
}

async function download() {
  if (!available.value || downloading.value) return;
  const id = props.initial.id, version = generation, controller = request();
  downloading.value = true; actionError.value = ''; actionNotice.value = '';
  try {
    const blob = await downloadSkillDraft(id, controller.signal);
    if (!current(version, id)) return;
    const url = URL.createObjectURL(blob); urls.add(url);
    const link = document.createElement('a'); link.href = url; link.download = skillPackageFilename(draft.value.name);
    document.body.appendChild(link); link.click(); link.remove();
    setTimeout(() => { URL.revokeObjectURL(url); urls.delete(url); }, 1000);
    actionNotice.value = '技能包已准备下载。';
  } catch {
    if (current(version, id)) actionError.value = '下载失败，请重试。';
  } finally { requests.delete(controller); if (current(version, id)) downloading.value = false; }
}

watch(() => [props.initial.id, props.isShare] as const, () => {
  abort(); draft.value = props.initial; refreshQueued = false;
  previewPath.value = ''; previewTrigger = null;
  loading.value = false; loaded.value = false; installing.value = false; downloading.value = false;
  readError.value = ''; actionError.value = ''; actionNotice.value = '';
  if (!props.isShare) void refresh();
}, { immediate: true });
onUnmounted(() => { active = false; eventBus.off(EVENT_SKILL_CATALOG_UPDATED, handleSkillCatalogUpdated); abort(); for (const url of urls) URL.revokeObjectURL(url); urls.clear(); });
</script>

<style scoped>
.skill-creation-card{margin:8px 0;min-width:0;max-width:100%;overflow:hidden;border:1px solid var(--border-main);border-radius:16px;background:var(--background-menu-white);color:var(--text-primary)}
.skill-heading{display:flex;align-items:center;gap:12px;padding:18px 20px 12px}.skill-icon{display:flex;align-items:center;justify-content:center;width:44px;height:44px;flex-shrink:0;border:1px solid var(--border-light);border-radius:12px}.skill-eyebrow{font-size:11px;color:var(--text-tertiary);margin-bottom:3px}.skill-heading h3{font-size:16px;line-height:1.5;font-weight:600;overflow-wrap:anywhere}.skill-body{padding:0 20px 16px}.skill-description{font-size:14px;line-height:1.75;white-space:pre-line;overflow-wrap:anywhere}.skill-section{margin-top:14px;font-size:13px;line-height:1.7;overflow-wrap:anywhere}.skill-section h4{font-weight:600;margin-bottom:4px}.skill-section ul{list-style:disc;padding-left:20px}.skill-section li+li{margin-top:3px}.skill-validation{display:flex;align-items:center;flex-wrap:wrap;gap:5px;margin-top:16px;font-size:12px;color:#247357}.skill-note{font-size:12px;line-height:1.7;color:var(--text-tertiary)}.skill-validation+.skill-note{margin-top:4px}.skill-files{margin-top:14px;font-size:12px}.skill-files summary{cursor:pointer;color:var(--text-secondary)}.skill-files ul{margin-top:8px;max-height:190px;overflow:auto}.skill-files li{display:flex;align-items:flex-start;gap:6px;padding:3px 0;color:var(--text-secondary)}.skill-files li svg{flex-shrink:0;margin-top:2px}.skill-files li span{overflow-wrap:anywhere}.skill-error{font-size:12px;line-height:1.7;margin-top:12px;color:#a74c2c}.skill-error button{text-decoration:underline}.skill-footer{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:14px;padding:14px 20px;border-top:1px solid var(--border-light)}.skill-actions{display:flex;flex-wrap:wrap;gap:8px}.skill-primary,.skill-secondary{display:inline-flex;align-items:center;justify-content:center;gap:6px;min-height:38px;border-radius:10px;padding:8px 12px;font-size:13px;font-weight:500}.skill-primary{background:var(--text-primary);color:var(--background-menu-white)}.skill-secondary{border:1px solid var(--border-main);background:var(--background-menu-white);color:var(--text-primary)}.skill-primary:hover,.skill-secondary:hover{opacity:.85}.skill-primary:disabled,.skill-secondary:disabled{opacity:.55;cursor:default}.skill-creation-card> .skill-note{padding:14px 18px}.skill-body> .skill-note[role=status]{margin-top:10px}button:focus-visible,summary:focus-visible{outline:2px solid var(--text-secondary);outline-offset:3px}@media(max-width:480px){.skill-heading{padding:15px 14px 10px}.skill-body{padding:0 14px 14px}.skill-footer{padding:12px 14px}.skill-actions{width:100%}.skill-actions button{flex:1;white-space:nowrap}}
</style>

<style scoped>
.skill-files li{display:block;padding:0}.skill-files li button{display:flex;align-items:flex-start;gap:7px;width:100%;min-width:0;padding:6px;border-radius:6px;text-align:left;color:var(--text-secondary)}.skill-files li button:hover:not(:disabled){background:var(--fill-tsp-gray-main);color:var(--text-primary)}.skill-files li button:hover:not(:disabled) span{text-decoration:underline}.skill-files li button:disabled{opacity:.55;cursor:default}.skill-files li button span{min-width:0;flex:1}.skill-file-preview-icon{opacity:.55}
</style>
