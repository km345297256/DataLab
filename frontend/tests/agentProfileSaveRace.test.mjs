import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import * as vue from 'vue';
import { compileScript, parse } from '@vue/compiler-sfc';
import ts from 'typescript';
import { createAgentProfileDirectory } from '../src/utils/agentProfileDirectory.ts';
import { copyAgentToolRuntime, sameAgentToolRuntime } from '../src/utils/agentToolRuntime.ts';

const compiled = ts.transpileModule(compileScript(parse(readFileSync(new URL('../src/components/settings/AgentProfileRuntimeSettings.vue', import.meta.url), 'utf8')).descriptor, { id: 'profile-save-race' }).content,
  { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
const flush = async () => { for (let i = 0; i < 8; i++) await Promise.resolve(); await vue.nextTick(); };

test('actual settings save supersedes a catalog read that began before profile creation', async () => {
  const reads = [];
  const directory = createAgentProfileDirectory(() => new Promise(resolve => reads.push(resolve)), { getItem: () => null, setItem() {}, removeItem() {} });
  const profile = { id: 'created', name: 'Created profile', tool_runtime: copyAgentToolRuntime() };
  const calls = [];
  const imports = {
    vue: { ...vue, onMounted() {} },
    'vue-i18n': { useI18n: () => ({ t: key => key }) },
    'vue-router': { useRouter: () => ({ push() {} }) },
    '@/api/agentProfile': { listAgentProfiles: async () => [], createRuntimePresetProfile: async input => { calls.push(input); return profile; } },
    '@/api/domainPreset': { getDomainPresetCatalog: async () => ({ presets: [] }) },
    '@/composables/useAgentProfile': { useAgentProfile: () => directory },
    '@/composables/useAuth': { useAuth: () => ({ isAdmin: vue.ref(true) }) },
    '@/composables/useSettingsDialog': { useSettingsDialog: () => ({ closeSettingsDialog() {} }) },
    '@/utils/agentToolRuntime': { copyAgentToolRuntime, sameAgentToolRuntime },
    '@/utils/toast': { showErrorToast: message => assert.fail(message), showSuccessToast() {} },
  };
  const module = { exports: {} };
  new Function('require', 'module', 'exports', compiled)(name => { assert.ok(name in imports, name); return imports[name]; }, module, module.exports);
  const scope = vue.effectScope();
  try {
    const state = scope.run(() => module.exports.default.setup({}, { expose() {} }));
    const stale = directory.refreshProfiles(); await flush();
    state.catalog.value = { defaults: copyAgentToolRuntime(), presets: [{ id: 'general' }] };
    state.startCreate(); state.newProfileName.value = 'Created profile';
    await state.save(); await flush();
    assert.equal(calls.length, 1);
    assert.equal(reads.length, 2, 'Save must start a fresh read, not reuse the pre-save request');
    reads[1]([profile]); await flush();
    assert.deepEqual(directory.profiles.value.map(item => item.id), ['created']);
    reads[0]([]); await stale;
    assert.deepEqual(directory.profiles.value.map(item => item.id), ['created'], 'The late old result cannot hide the newly created profile');
  } finally { scope.stop(); }
});
