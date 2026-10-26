import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import * as vue from 'vue';
import ts from 'typescript';
import { createAnalysisDraftStore, emptyAnalysisDraft, copyAnalysisDraft } from '../src/utils/analysisDrafts.ts';

const file = (id, extras = {}) => ({ file_id: id, filename: `${id.replace('gridfs:', '')}.csv`, upload_date: '', ...extras });
const draft = (changes = {}) => ({ ...emptyAnalysisDraft(), ...changes });

function harness(store, owner, key, initial = draft()) {
  let editor = vue.reactive(copyAnalysisDraft(initial));
  const hooks = [];
  const scope = vue.effectScope();
  const source = readFileSync(new URL('../src/composables/useAnalysisDraft.ts', import.meta.url), 'utf8');
  const code = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
  const module = { exports: {} };
  new Function('require', 'module', 'exports', code)(name => {
    if (name === 'vue') return { ...vue, onBeforeUnmount: callback => hooks.push(callback) };
    if (name === './useAuth') return { useAuth: () => ({ currentUser: owner }) };
    if (name === '../utils/analysisDrafts') return { analysisDraftStore: store, emptyAnalysisDraft, copyAnalysisDraft };
    throw new Error(`Unexpected dependency: ${name}`);
  }, module, module.exports);
  const state = scope.run(() => module.exports.useAnalysisDraft(
    () => key, () => editor, restored => { editor = vue.reactive(restored); },
  ));
  return { state, get editor() { return editor; }, close() { hooks.forEach(callback => callback()); scope.stop(); } };
}

function identity() {
  const store = createAnalysisDraftStore();
  const owner = vue.ref({ id: 'researcher-a' });
  store.setOwner(owner.value.id);
  return { store, owner };
}

test('returning to a new analysis restores unsent input and explicit selections without browser storage', () => {
  const { store, owner } = identity();
  const first = harness(store, owner, 'new');
  first.state.restore();
  first.editor.message = '比较气象数据';
  first.editor.files = [file('gridfs:input', { metadata: { host_path: '/private/raw.csv' }, file_url: '/private/raw.csv', relative_path: '/private/raw.csv' })];
  first.editor.skills = ['quality'];
  first.editor.connectors = ['open-data'];
  first.close();
  const second = harness(store, owner, 'new');
  second.state.restore();
  assert.equal(second.editor.message, '比较气象数据');
  assert.deepEqual(second.editor.files.map(item => item.file_id), ['input']);
  assert.deepEqual([...second.editor.skills], ['quality']);
  assert.deepEqual([...second.editor.connectors], ['open-data']);
  assert.doesNotMatch(JSON.stringify(second.editor), /private|metadata|file_url|relative_path/);
  // Confirmed navigation to the created session consumes the new-task draft.
  second.state.discard();
  second.close();
  assert.equal(store.open(owner.value.id).read('new'), undefined);
});

test('unchanged server selections never mask fresh context when returning to a task', () => {
  const { store, owner } = identity();
  const first = harness(store, owner, 'session:one', draft({ files: [file('old')], skills: ['old-skill'] }));
  first.state.restore();
  first.close();
  assert.equal(store.open(owner.value.id).read('session:one'), undefined);
  const second = harness(store, owner, 'session:one', draft({ files: [file('new')], skills: ['new-skill'] }));
  second.state.restore();
  assert.deepEqual(second.editor.files.map(item => item.file_id), ['new']);
  assert.deepEqual([...second.editor.skills], ['new-skill']);
  second.close();
});

test('repeated status refreshes preserve the unsent question and choices after leaving and reopening', () => {
  const { store, owner } = identity();
  const firstServer = draft({ files: [file('server-old')], skills: ['server-skill'] });
  const first = harness(store, owner, 'session:one', firstServer);
  first.state.restore(firstServer);
  first.editor.message = '尚未发送的问题 M';
  first.editor.files = [file('unsent-file')];
  first.editor.skills = ['chosen-skill'];
  first.editor.connectors = ['chosen-connector'];
  const nextServer = draft({ files: [file('server-new')], skills: ['new-server-skill'] });
  for (let refresh = 0; refresh < 3; refresh++) {
    first.state.save();
    // The page has fetched new context, while its textarea still contains M.
    first.editor.files = nextServer.files;
    first.editor.skills = nextServer.skills;
    first.state.restore(nextServer);
    assert.equal(first.editor.message, '尚未发送的问题 M');
    assert.deepEqual(first.editor.files.map(item => item.file_id), ['unsent-file']);
    assert.deepEqual([...first.editor.skills], ['chosen-skill']);
    assert.deepEqual([...first.editor.connectors], ['chosen-connector']);
  }
  first.close();
  const returned = harness(store, owner, 'session:one', nextServer);
  returned.state.restore(nextServer);
  assert.equal(returned.editor.message, '尚未发送的问题 M');
  assert.deepEqual(returned.editor.files.map(item => item.file_id), ['unsent-file']);
  assert.deepEqual([...returned.editor.connectors], ['chosen-connector']);
  returned.close();
});

test('repeated local restore keeps the original baseline and clean server refreshes do not create a stale draft', () => {
  const { store, owner } = identity();
  const initial = draft({ files: [file('old')] });
  const first = harness(store, owner, 'session:one', initial);
  first.state.restore();
  first.editor.message = '未发送';
  first.state.restore();
  first.close();
  const second = harness(store, owner, 'session:one', initial);
  second.state.restore(initial);
  assert.equal(second.editor.message, '未发送');
  second.editor.message = '';
  second.state.save();
  const nextServer = draft({ files: [file('updated')] });
  second.state.restore(nextServer);
  second.close();
  assert.equal(store.open(owner.value.id).read('session:one'), undefined);
  const third = harness(store, owner, 'session:one', draft({ files: [file('latest')] }));
  third.state.restore();
  assert.deepEqual(third.editor.files.map(item => item.file_id), ['latest']);
  third.close();
});

test('unsent deselections and connector edits are isolated by session and survive a failed history reload', () => {
  const { store, owner } = identity();
  const first = harness(store, owner, 'session:one', draft({ files: [file('selected')], connectors: ['old'] }));
  first.state.restore();
  first.editor.files = [];
  first.editor.connectors = ['chosen'];
  first.close();
  // No restore call: a failed server read must not replace the cached input with an empty editor.
  harness(store, owner, 'session:one').close();
  const other = harness(store, owner, 'session:two', draft({ files: [file('other')] }));
  other.state.restore();
  assert.deepEqual(other.editor.files.map(item => item.file_id), ['other']);
  other.close();
  const returned = harness(store, owner, 'session:one', draft({ files: [file('selected')], connectors: ['old'] }));
  returned.state.restore();
  assert.deepEqual([...returned.editor.files], []);
  assert.deepEqual([...returned.editor.connectors], ['chosen']);
  returned.close();
});

test('accepted submissions become the baseline while newer unsent edits survive', () => {
  const { store, owner } = identity();
  const first = harness(store, owner, 'session:one');
  first.state.restore();
  first.editor.files = [file('accepted'), file('newer')];
  first.editor.message = '还想补充一个问题';
  first.state.accept(draft({ files: [file('accepted')] }));
  first.close();
  const returned = harness(store, owner, 'session:one', draft({ files: [file('accepted')] }));
  returned.state.restore();
  assert.equal(returned.editor.message, '还想补充一个问题');
  assert.deepEqual(returned.editor.files.map(item => item.file_id), ['accepted', 'newer']);
  returned.editor.message = '';
  returned.state.accept(draft({ files: returned.editor.files }));
  returned.close();
  assert.equal(store.open(owner.value.id).read('session:one'), undefined);
});

test('account switches immediately clear visible drafts and invalidate old async writers, including signing back into the same account', () => {
  const { store, owner } = identity();
  const first = harness(store, owner, 'new');
  first.state.restore();
  first.editor.message = 'private';
  first.editor.files = [file('private')];
  first.state.save();
  store.setOwner(null);
  owner.value = null;
  assert.equal(first.editor.message, '');
  assert.deepEqual([...first.editor.files], []);
  assert.equal(first.state.isCurrent(), false);
  store.setOwner('researcher-b');
  owner.value = { id: 'researcher-b' };
  assert.equal(store.open('researcher-b').read('new'), undefined);
  store.setOwner('researcher-a');
  owner.value = { id: 'researcher-a' };
  first.editor.message = 'late stale result';
  first.state.accept(draft());
  first.close();
  assert.equal(first.state.isCurrent(), false);
  assert.equal(store.open('researcher-a').read('new'), undefined);
});

test('draft snapshots do not share mutable file/selection objects or cache unfinished upload handles', () => {
  const { store, owner } = identity();
  const access = store.open(owner.value.id);
  const original = draft({ files: [file('ready'), file('temp-upload', { status: 'uploading', file: new File(['raw'], 'raw.csv') })], skills: ['skill'] });
  access.save('new', original);
  original.files[0].filename = 'changed.csv';
  original.skills[0] = 'changed';
  const first = access.read('new');
  assert.deepEqual(first.files.map(item => item.file_id), ['ready']);
  assert.equal(first.files[0].filename, 'ready.csv');
  first.files[0].filename = 'changed again.csv';
  first.skills[0] = 'changed again';
  assert.equal(access.read('new').files[0].filename, 'ready.csv');
  assert.deepEqual(access.read('new').skills, ['skill']);
});
