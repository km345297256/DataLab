import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import * as vue from 'vue';
import { compileScript, parse } from '@vue/compiler-sfc';
import mitt from 'mitt';
import ts from 'typescript';

const catalogEvent = 'EVENT_SKILL_CATALOG_UPDATED';
const preferencesEvent = 'EVENT_SKILL_PREFERENCES_UPDATED';
const flush = async () => { for (let i = 0; i < 8; i++) await Promise.resolve(); await vue.nextTick(); };
function deferred() { let resolve, reject; const promise = new Promise((a,b) => {resolve=a;reject=b;}); return {promise,resolve,reject}; }
function load(path, dependencies) {
  const raw = readFileSync(new URL(path, import.meta.url), 'utf8');
  const source = path.endsWith('.vue') ? compileScript(parse(raw).descriptor,{id:'personal-deletion-test'}).content : raw;
  const code = ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
  const module={exports:{}};
  new Function('require','module','exports',code)(name=> {
    if(name in dependencies)return dependencies[name];
    if(name.endsWith('.vue'))return {};
    throw new Error(`Unexpected dependency ${name}`);
  },module,module.exports);
  return module.exports;
}
const skill = (id='mine', extra={}) => ({id,name:id,title:`技能 ${id}`,description:'说明',triggers:[],installed:true,source:'personal',scope:'user',owner_user_id:'owner-a',...extra});
function harness(component = null) {
  const unmounted=[],mounted=[],deletions=[],toasts=[],deleted=[],events=[],scope=vue.effectScope();
  const currentUser=vue.ref({id:'owner-a'}),eventBus=mitt();
  eventBus.on('*',(type,payload)=>events.push({type,payload}));
  const currentSkills=[skill(),skill('other',{owner_user_id:'owner-b'}),skill('official',{source:'official',owner_user_id:null})];
  const api={deletePersonalSkill:id=>{const request={id,...deferred()};deletions.push(request);return request.promise;},listSkills:async()=>currentSkills,listSkillCatalog:async()=>currentSkills,getSkillPreferences:async()=>['mine','other']};
  const vueApi={...vue,onUnmounted:fn=>unmounted.push(fn),onMounted:fn=>mounted.push(fn)};
  const i18n={useI18n:()=>({t:(text,vars)=>text+ (vars?JSON.stringify(vars):'')})};
  const toast={showSuccessToast:text=>toasts.push({type:'success',text}),showErrorToast:text=>toasts.push({type:'error',text})};
  const constants={EVENT_SKILL_CATALOG_UPDATED:catalogEvent,EVENT_SKILL_PREFERENCES_UPDATED:preferencesEvent};
  const auth={useAuth:()=>({currentUser})};
  const deletion=load('../src/composables/usePersonalSkillDeletion.ts',{
    vue:vueApi,'vue-i18n':i18n,'../api/skill':api,'./useAuth':auth,
    '../utils/eventBus':{eventBus},'../constants/event':constants,'../utils/toast':toast,
  });
  let state;
  if(component) {
    const mod=load(component,{
      vue:vueApi,'vue-i18n':i18n,'lucide-vue-next':{},
      'vue-router':{useRoute:()=>vue.reactive({meta:{catalogType:'skills'}}),useRouter:()=>({push(){}})},
      '@/utils/skillCatalog':{skillCategories:{},filterSkillCatalog:items=>items},
      '@/composables/useLeftPanel':{useLeftPanel:()=>({isLeftPanelShow:vue.ref(true),toggleLeftPanel(){}})},
      '@/composables/useSettingsDialog':{useSettingsDialog:()=>({closeSettingsDialog(){}})},
      '@/components/ui/dialog':{},marked:{marked:{setOptions(){}}},dompurify:{},
      '@/api/skill':api,'@/utils/toast':toast,'@/utils/eventBus':{eventBus},'@/constants/event':constants,
      '@/composables/usePersonalSkillDeletion':deletion,'@/composables/useAuth':auth,
    }).default;
    state=scope.run(()=>mod.setup({}, {expose(){}}));
  } else state=scope.run(()=>deletion.usePersonalSkillDeletion(item=>deleted.push(item)));
  return {state,currentUser,eventBus,api,currentSkills,deletions,toasts,deleted,events,mounted,close(){unmounted.forEach(fn=>fn());scope.stop();}};
}

test('delete is offered only to the actual owner of a personal skill, including owned shared skills and legacy owner fields', () => {
  const h=harness();
  try {
    assert.equal(h.state.canDeletePersonalSkill(skill()),true);
    assert.equal(h.state.canDeletePersonalSkill(skill('global',{scope:'global'})),true);
    assert.equal(h.state.canDeletePersonalSkill(skill('legacy',{owner_user_id:null,user_id:'owner-a'})),true);
    for(const item of [skill('other',{owner_user_id:'owner-b'}),skill('official',{source:'official'}),skill('nobody',{owner_user_id:null})]) {
      assert.equal(h.state.canDeletePersonalSkill(item),false);h.state.requestDeleteSkill(item);
    }
    assert.equal(h.state.skillToDelete.value,null);
    h.currentUser.value=null;
    assert.equal(h.state.canDeletePersonalSkill(skill()),false);
  } finally{h.close();}
});

test('confirmation names the skill; cancel and pending requests do not remove it; duplicate submit sends one delete',async()=>{
  const h=harness();try {
    h.state.requestDeleteSkill(skill());
    assert.equal(h.deletions.length,0);
    assert.equal(h.state.skillToDelete.value.title,'技能 mine');
    h.state.cancelDeleteSkill();assert.equal(h.state.skillToDelete.value,null);assert.equal(h.deletions.length,0);h.state.requestDeleteSkill(skill());
    const run=h.state.confirmDeleteSkill();
    await h.state.confirmDeleteSkill();
    assert.equal(h.deletions.length,1);assert.equal(h.state.deletingSkillId.value,'mine');assert.equal(h.deleted.length,0);assert.deepEqual(h.events,[]);
    h.deletions[0].resolve();await run;
    assert.equal(h.state.deletingSkillId.value,null);assert.equal(h.deleted[0].id,'mine');
    assert.deepEqual(h.events,[{type:catalogEvent,payload:{deletedSkillId:'mine',deletedSkillName:'mine'}}]);
  }finally{h.close();}
});

test('server rejection preserves the item and selections, clears pending state, and allows retry',async()=>{
  const h=harness();try {
    h.state.requestDeleteSkill(skill());const run=h.state.confirmDeleteSkill();h.deletions[0].reject(new Error('403'));await run;
    assert.equal(h.deleted.length,0);assert.deepEqual(h.events,[]);assert.equal(h.state.deletingSkillId.value,null);assert.equal(h.state.deleteError.value,'Failed to delete skill');assert.equal(h.state.skillToDelete.value.id,'mine');
    h.state.requestDeleteSkill(skill());assert.equal(h.state.deleteError.value,'');assert.equal(h.state.skillToDelete.value.id,'mine');
  }finally{h.close();}
});

test('account switches invalidate confirmations and ignore old-account deletion responses',async()=>{
  const h=harness();try{
    h.state.requestDeleteSkill(skill());h.currentUser.value={id:'owner-b'};await h.state.confirmDeleteSkill();assert.equal(h.deletions.length,0);
    h.currentUser.value={id:'owner-a'};h.state.requestDeleteSkill(skill());const run=h.state.confirmDeleteSkill();h.currentUser.value={id:'owner-b'};h.deletions[0].resolve();await run;
    assert.equal(h.deleted.length,0);assert.equal(h.events.length,0);assert.equal(h.toasts.length,0);
  }finally{h.close();}
});

test('closing settings during an accepted request still notifies current-account consumers without touching the unmounted view',async()=>{
  const h=harness();h.state.requestDeleteSkill(skill());const run=h.state.confirmDeleteSkill();h.close();h.deletions[0].resolve();await run;
  assert.equal(h.deleted.length,0);assert.equal(h.events[0].type,catalogEvent);
});

test('skill settings remove the deleted skill and its automatic preference only after success',async()=>{
  const h=harness('../src/components/settings/SkillSettings.vue');try{
    await h.state.load();h.state.requestDeleteSkill(h.state.skills.value[0]);const run=h.state.confirmDeleteSkill();
    assert.equal(h.state.skills.value.length,3);assert.equal(h.state.autoEnabledSkills.value.has('mine'),true);
    h.deletions[0].resolve();await run;
    assert.deepEqual(h.state.skills.value.map(s=>s.id),['other','official']);assert.deepEqual([...h.state.autoEnabledSkills.value],['other']);
    assert.deepEqual(h.events[0],{type:preferencesEvent,payload:['other']});
  }finally{h.close();}
});

test('catalog deletion clears the matching detail/editor and keeps unrelated skills',async()=>{
  const h=harness('../src/pages/PluginsPage.vue');try{
    await h.state.loadSkills();const item=h.state.skills.value[0];h.state.selectedSkillDetail.value={skill:item,tree:[],files:[]};h.state.selectedSkillFilePath.value='SKILL.md';h.state.skillFileDraft.value='unsaved';h.state.editingSkillFile.value=true;h.state.skillDetailOpen.value=true;
    h.state.requestDeleteSkill(item);const run=h.state.confirmDeleteSkill();h.deletions[0].resolve();await run;await flush();
    assert.deepEqual(h.state.skills.value.map(s=>s.id),['other','official']);assert.equal(h.state.selectedSkillDetail.value,null);assert.equal(h.state.skillDetailOpen.value,false);assert.equal(h.state.skillFileDraft.value,'');assert.equal(h.state.editingSkillFile.value,false);
  }finally{h.close();}
});

test('an open skill picker removes a deleted choice and ignores catalog responses started before deletion', async () => {
  const mounted=[],unmounted=[],events=[],requests=[],eventBus=mitt(),scope=vue.effectScope();
  const props=vue.reactive({open:true,selectedSkills:['mine','keep']});
  const mod=load('../src/components/SkillDialog.vue',{
    vue:{...vue,onMounted:fn=>mounted.push(fn),onUnmounted:fn=>unmounted.push(fn)},
    'vue-i18n':{useI18n:()=>({t:s=>s})},'lucide-vue-next':{},'@/components/ui/dialog':{},
    '@/utils/skillCatalog':{skillCategories:{},filterSkillCatalog:items=>items},
    '@/api/skill':{listSkills:()=>{const request=deferred();requests.push(request);return request.promise;},getSkillPreferences:async()=>[]},
    '@/utils/toast':{showErrorToast(){},showSuccessToast(){}},'@/utils/eventBus':{eventBus},'@/constants/event':{EVENT_SKILL_CATALOG_UPDATED:catalogEvent},
  }).default;
  const state=scope.run(()=>mod.setup(props,{expose(){},emit:(...args)=>events.push(args)}));
  try {
    mounted.forEach(fn=>fn());
    state.skills.value=[skill('mine'),skill('keep')];
    const old=state.loadSkills();
    eventBus.emit(catalogEvent,{deletedSkillId:'mine',deletedSkillName:'mine'});
    assert.deepEqual(events.at(-1),['update:selectedSkills',['keep']]);
    assert.deepEqual(state.skills.value.map(s=>s.id),['keep']);
    requests[0].resolve([skill('mine')]);await old;await flush();
    assert.deepEqual(state.skills.value.map(s=>s.id),['keep']);
    assert.equal(state.loading.value,true);
    requests[1].resolve([skill('keep')]);await flush();
    assert.equal(state.loading.value,false);
  } finally {unmounted.forEach(fn=>fn());scope.stop();}
  assert.equal(eventBus.all.get(catalogEvent)?.length??0,0);
});

test('a result card queues deletion refresh behind an older read and restores the explicit add action without an install',async()=>{
  const requests=[],unmounted=[],eventBus=mitt(),scope=vue.effectScope();
  const draft={id:'draft',name:'mine',installed:true,installed_skill_id:'mine',validation:{},resources:[]};
  const props=vue.reactive({initial:draft,isShare:false});
  const component=load('../src/components/SkillCreationCard.vue',{
    vue:{...vue,onUnmounted:fn=>unmounted.push(fn)},'lucide-vue-next':{},
    '../api/skillDraft':{getSkillDraft:()=>{const request=deferred();requests.push(request);return request.promise;},installSkillDraft:()=>{throw new Error('refresh must not install');}},
    '../utils/skillCreation':{skillPackageFilename:name=>name+'.zip'},'../utils/eventBus':{eventBus},'../constants/event':{EVENT_SKILL_CATALOG_UPDATED:catalogEvent},
  }).default;
  const state=scope.run(()=>component.setup(props,{expose(){}}));
  try {
    eventBus.emit(catalogEvent,{deletedSkillId:'unrelated',deletedSkillName:'unrelated'});assert.equal(requests.length,1);
    eventBus.emit(catalogEvent,{deletedSkillId:'mine',deletedSkillName:'mine'});assert.equal(requests.length,1,'wait for the in-flight read');
    requests[0].resolve(draft);await flush();assert.equal(requests.length,2,'read the fresh post-delete state');
    assert.equal(state.available.value,false,'do not expose a stale add/download action while refreshing');
    requests[1].resolve({...draft,installed:false,installed_skill_id:null});await flush();
    assert.equal(state.draft.value.installed,false);assert.equal(state.available.value,true);
  }finally{unmounted.forEach(fn=>fn());scope.stop();}
  assert.equal(eventBus.all.get(catalogEvent)?.length??0,0);
});
