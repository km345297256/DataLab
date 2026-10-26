import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import * as vue from 'vue';
import ts from 'typescript';
const code=ts.transpileModule(readFileSync(new URL('../src/composables/useTaskData.ts',import.meta.url),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
const file=id=>({file_id:id,filename:`${id}.csv`,size:12,upload_date:'2026-10-22T00:00:00Z'});
const item=(id,category='upload',saved_locations=[])=>({file:file(id),category,created_at:'2026-10-22T00:00:00Z',round_number:1,available:true,saved_locations});
const response=(items=[],selected_file_ids=[],unavailable_count=0)=>({items,selected_file_ids,unavailable_count});
const flush=async()=>{await Promise.resolve();await Promise.resolve();await vue.nextTick();};
function harness(){
 const requests=[],events=[],listeners=new Map(),windowEvents=new Map();let owner='owner-a';
 const module={exports:{}};
 new Function('require','module','exports','window',code)(name=>{
  if(name==='vue')return vue;
  if(name==='../api/taskData')return {getTaskData:(...args)=>new Promise((resolve,reject)=>requests.push({args,resolve,reject}))};
  if(name==='../utils/authTokens')return {authSessionKey:()=>owner};
  if(name==='../utils/eventBus')return {eventBus:{on:(name,callback)=>listeners.set(name,callback),emit:(...args)=>events.push(args)}};
  assert.fail(name);
 },module,module.exports,{addEventListener:(name,callback)=>windowEvents.set(name,callback)});
 const state=module.exports.useTaskData();
 return {state,requests,events,listeners,windowEvents,module,setOwner:value=>owner=value,
  async start(id='session',items=[]){const pending=state.activate(id);requests.at(-1).resolve(response(items));await pending;},
  async settle(items=[],selected=[],unavailable=0){requests.at(-1).resolve(response(items,selected,unavailable));await flush();},
 };
}
test('task data is shared, deduplicated and does not overwrite draft input selection',async()=>{
 const h=harness();await h.start('session',[item('a'),item('a'),item('b','analysis')]);
 assert.equal(h.module.exports.useTaskData().items,h.state.items);
 assert.equal(h.state.count.value,2);assert.deepEqual(h.state.counts.value,{all:2,upload:1,analysis:1,download:0});
 h.state.syncSelection('session',['b'],false);const pending=h.state.refresh();
 h.requests.at(-1).resolve(response([item('a'),item('b','analysis')],['a']));await pending;
 assert.deepEqual(h.state.selectedIds.value,['b']);assert.equal(h.state.locked.value,false);
});
test('session switch and logout discard pending responses and all previous private state',async()=>{
 const h=harness();const first=h.state.activate('a'),stale=h.requests.at(-1);
 await h.start('b',[item('b')]);assert.equal(stale.args[2].aborted,true);
 stale.resolve(response([item('a')]));await first;assert.equal(h.state.items.value[0].file.file_id,'b');
 h.state.syncSelection('b',['b'],false);h.state.previewFromTaskData.value=true;h.state.open('analysis');
 const pending=h.requests.at(-1);h.listeners.get('auth:cleared')();pending.resolve(response([item('secret')]));await flush();
 assert.equal(h.state.active.value,false);assert.equal(h.state.panelOpen.value,false);assert.equal(h.state.previewFromTaskData.value,false);
 assert.deepEqual(h.state.items.value,[]);assert.deepEqual(h.state.selectedIds.value,[]);assert.equal(h.state.open(),false);
});
test('account changes invalidate same-route data and writes even before route cleanup',async()=>{
 const h=harness();await h.start('same',[item('a')]);h.state.syncSelection('same',[],false);
 const pending=h.state.refresh(),old=h.requests.at(-1);h.setOwner('owner-b');
 assert.equal(h.state.changeInputSelection([file('a')],true),false);assert.equal(h.state.open(),false);
 old.resolve(response([item('old-secret')]));await pending;assert.equal(h.state.items.value[0].file.file_id,'a');
 const next=h.state.activate('same');assert.deepEqual(h.state.items.value,[]);h.requests.at(-1).resolve(response([item('b')]));await next;
 assert.equal(h.state.items.value[0].file.file_id,'b');
});
test('draft files require server authorization, preserve category and saved state, and cannot revive excluded files',async()=>{
 const h=harness();await h.start('session',[item('result','analysis')]);
 const saved=[{asset_id:'asset',asset_name:'Saved',folder_id:'folder',folder_path:'Research',file_path:'Research/new.csv'}];
 h.state.syncSelection('session',['new','result'],false,[file('new'),file('result'),file('forbidden'),file('temp-local')]);
 assert.deepEqual(h.requests.at(-1).args[1],['new','result','forbidden']);
 assert.deepEqual(h.state.items.value.map(row=>row.file.file_id),['result'],'Unverified metadata must not appear');
 await h.settle([item('result','analysis'),item('new','upload',saved)],[],1);
 assert.equal(h.state.items.value[0].category,'analysis');assert.equal(h.state.items.value[1].saved_locations.length,1);
 assert.equal(h.state.unavailableCount.value,1);assert.ok(!h.state.items.value.some(row=>row.file.file_id==='forbidden'));
 h.state.syncSelection('session',[],false,[]);const requestCount=h.requests.length;
 h.state.syncSelection('session',[],false,[file('new')]);assert.equal(h.requests.length,requestCount,'Stable draft refs must not create refresh loops');
 const pending=h.state.refresh();assert.deepEqual(h.requests.at(-1).args[1],['new','result','forbidden']);
 h.requests.at(-1).resolve(response([item('result','analysis')],[],2));await pending;
 assert.deepEqual(h.state.items.value.map(row=>row.file.file_id),['result'],'Server exclusion is authoritative even for a retained draft ref');
});
test('large accumulated draft histories use batches of 32 and do not duplicate session files or missing counts',async()=>{
 const h=harness();await h.start('session',[item('base')]);
 const drafts=Array.from({length:65},(_,index)=>file(`draft-${index}`));
 h.state.syncSelection('session',[],false,drafts);
 const batch=h.requests.slice(-4);assert.deepEqual(batch.map(row=>row.args[1].length),[0,32,32,1]);
 for(const request of batch)request.resolve(response([item('base'),...request.args[1].map(id=>item(id))],[],request.args[1].length?3:2));
 await flush();assert.equal(h.state.items.value.length,66);assert.equal(h.state.unavailableCount.value,5);
});
test('input changes are explicit, owner/session guarded, locked during recovery and capped at 32',async()=>{
 const h=harness();await h.start('session',Array.from({length:33},(_,index)=>item(`f${index}`)));
 assert.equal(h.state.changeInputSelection([file('f0')],true),false);
 h.state.syncSelection('session',Array.from({length:32},(_,index)=>`f${index}`),false);
 assert.equal(h.state.changeInputSelection([file('f32')],true),false);assert.match(h.state.actionError.value,/32/);
 assert.equal(h.state.changeInputSelection([file('f0')],false),true);
 assert.deepEqual(h.events.at(-1),['task-data-input-change',{sessionId:'session',files:[file('f0')],selected:false}]);
 assert.equal(h.state.selectedIds.value.length,32,'The store waits for ChatPage to confirm selection');
 assert.equal(h.state.changeInputSelection([file('foreign')],true),false);
 h.state.syncSelection('session',[],true);assert.equal(h.state.changeInputSelection([file('f0')],true),false);
});
test('panel suspend preserves preview context, close notifies interop and library saves trigger refresh',async()=>{
 const h=harness();await h.start('session',[item('a')]);h.state.open('download');
 assert.deepEqual(h.events.at(-1),['task-data-panel-show',{sessionId:'session',tab:'download'}]);
 h.state.previewFromTaskData.value=true;const before=h.events.length;h.state.suspend();
 assert.equal(h.events.length,before);assert.equal(h.state.panelOpen.value,false);assert.equal(h.state.previewFromTaskData.value,true);
 h.state.open('all');assert.equal(h.state.previewFromTaskData.value,false);
 h.state.close();assert.deepEqual(h.events.at(-1),['task-data-panel-close',{sessionId:'session'}]);
 const requests=h.requests.length;h.windowEvents.get('data-library-changed')();assert.equal(h.requests.length,requests+1);
 await h.settle([item('a','upload',[{asset_id:'saved',asset_name:'Saved',folder_id:null,folder_path:'',file_path:'a.csv'}])]);
 assert.equal(h.state.items.value[0].saved_locations.length,1);
});
test('failed refresh preserves current rows and a newer refresh cannot be rolled back',async()=>{
 const h=harness();await h.start('session',[item('a')]);
 const fail=h.state.refresh();h.requests.at(-1).reject(new Error('offline'));await fail;
 assert.match(h.state.error.value,/重试/);assert.equal(h.state.items.value[0].file.file_id,'a');
 const old=h.state.refresh(),older=h.requests.at(-1);const latest=h.state.refresh();
 h.requests.at(-1).resolve(response([item('b')]));await latest;older.resolve(response([item('a')]));await old;
 assert.equal(h.state.items.value[0].file.file_id,'b');assert.equal(h.state.error.value,'');assert.equal(h.state.loading.value,false);
});
