import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

import { apiClient } from '../src/api/client.ts';
import { getPluginRuntime, reloadPluginRuntime } from '../src/api/pluginRuntime.ts';


function adapterResponse(config, data) {
  return {
    config,
    data,
    headers: {},
    request: {},
    status: 200,
    statusText: 'OK',
  };
}


const snapshot = {
  engine: 'cordis',
  version: '4.0.2',
  status: 'healthy',
  healthy: true,
  revision: 'abc123',
  manifest_digest: 'def456',
  execution_bundle_digest: 'bundle789',
  plugin_count: 1,
  tool_count: 1,
  plugins: [{
    plugin: 'climate',
    version: '1.0.0',
    manifest_digest: 'plugin-digest',
    tool_count: 1,
    tools: [{
      name: 'climate.average',
      description: 'Calculate an average',
      scopes: ['dataset:read'],
      timeout_seconds: 90,
      plugin: 'climate',
      version: '1.0.0',
    }],
    errors: [],
  }],
  tools: [],
  last_error: null,
};


test('plugin runtime API uses the dedicated snapshot and reload endpoints', async () => {
  const originalAdapter = apiClient.defaults.adapter;
  const calls = [];
  apiClient.defaults.adapter = async (config) => {
    calls.push({
      action: config.headers.get('X-AI-DataSeek-Action'),
      method: config.method,
      url: config.url,
    });
    return adapterResponse(config, { code: 0, msg: 'success', data: snapshot });
  };

  try {
    assert.deepEqual(await getPluginRuntime(), snapshot);
    assert.deepEqual(await reloadPluginRuntime(), snapshot);
    assert.deepEqual(calls, [
      { action: undefined, method: 'get', url: '/plugins/runtime' },
      {
        action: 'plugin-runtime-reload',
        method: 'post',
        url: '/plugins/runtime/reload',
      },
    ]);
  } finally {
    apiClient.defaults.adapter = originalAdapter;
  }
});


test('the product catalog hides runtime administration without removing runtime APIs', async () => {
  const source = await readFile(new URL('../src/pages/PluginsPage.vue', import.meta.url), 'utf8');
  assert.match(source, /activeTab === 'skills'/);
  assert.match(source, /activeTab === 'connectors'/);
  assert.doesNotMatch(source, /activeTab === '(mcp|renderers|runtime|presets|credentials)'/);
  assert.doesNotMatch(source, /reloadPluginRuntime|loadMcpServers|loadRenderers|loadRuntime|loadPresets|ToolCredentialsPanel/);
});


test('skills and connectors have independent routes and navigation entries', async () => {
  const routerSource = await readFile(new URL('../src/router/index.ts', import.meta.url), 'utf8');
  const navigationSource = await readFile(new URL('../src/components/LeftPanel.vue', import.meta.url), 'utf8');

  assert.match(routerSource, /path:\s*'skills',[\s\S]*?name:\s*'SkillsLibrary',[\s\S]*?catalogType:\s*'skills'/);
  assert.match(routerSource, /path:\s*'connectors',[\s\S]*?name:\s*'DataConnectors',[\s\S]*?catalogType:\s*'connectors'/);
  assert.match(navigationSource, /router\.push\('\/skills'\)/);
  assert.match(navigationSource, /router\.push\('\/connectors'\)/);
  assert.doesNotMatch(navigationSource, /机构数据|分析能力与数据源/);
});


test('public runtime DTO omits the internal JSON Schema parameters', async () => {
  const source = await readFile(new URL('../src/api/pluginRuntime.ts', import.meta.url), 'utf8');

  assert.doesNotMatch(source, /parameters:\s*Record/);
  assert.match(source, /X-AI-DataSeek-Action/);
  assert.match(source, /plugin-runtime-reload/);
});
