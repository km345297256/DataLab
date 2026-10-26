import assert from 'node:assert/strict';
import test from 'node:test';
import { apiClient } from '../src/api/client.ts';
import { getSkillDraftResource } from '../src/api/skillDraft.ts';
import { formatSkillResource } from '../src/utils/skillResourcePreview.ts';

test('JSON preview preserves numeric literals, escaped strings and empty structures', () => {
  const raw = '{"id":9007199254740993,"value":1.234567890123456789,"tiny":1e-400,"text":"a, {b}: \\"quoted\\" \\\\ \\n中文","list":[{},[],null,true,-0]}';
  const formatted = formatSkillResource('references/values.json', raw);
  assert.ok(formatted.includes('\n  "id": 9007199254740993,'));
  assert.ok(formatted.includes('1.234567890123456789'));
  assert.ok(formatted.includes('1e-400'));
  assert.ok(formatted.includes('\\n中文'));
  assert.ok(formatted.includes('\n    -0\n'));
  assert.deepEqual(JSON.parse(formatted), JSON.parse(raw));
  assert.equal(formatSkillResource('empty.json', '{}'), '{}');
  assert.equal(formatSkillResource('array.json', '[]'), '[]');
  const deep = '['.repeat(65) + '0' + ']'.repeat(65);
  assert.equal(formatSkillResource('deep.json', deep), deep, 'Deep content remains complete without indentation expansion');
});

test('invalid JSON and source files remain readable verbatim', () => {
  const source = 'if True:\n    print("<script>not HTML</script>")\n';
  assert.equal(formatSkillResource('scripts/analyze.py', source), source);
  assert.equal(formatSkillResource('references/broken.json', '{"truncated":'), '{"truncated":');
});

test('single-file preview is an authenticated-client GET with a separate encoded path parameter', async () => {
  const original = apiClient.defaults.adapter;
  const calls = [];
  const path = 'references/含空格 name+#.json';
  const controller = new AbortController();
  apiClient.defaults.adapter = async config => {
    calls.push(config);
    return { config, status: 200, statusText: 'OK', headers: {}, data: { code: 0, data: { path, content: '{}' } } };
  };
  try {
    assert.deepEqual(await getSkillDraftResource('owner/id', path, controller.signal), { path, content: '{}' });
    assert.equal(calls.length, 1);
    assert.equal(calls[0].method, 'get');
    assert.equal(calls[0].url, '/skills/drafts/owner%2Fid/resource');
    assert.deepEqual(calls[0].params, { path });
    assert.equal(calls[0].signal, controller.signal);
  } finally { apiClient.defaults.adapter = original; }
});
