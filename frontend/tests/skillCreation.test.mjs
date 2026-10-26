import assert from 'node:assert/strict';
import test from 'node:test';
import { apiClient } from '../src/api/client.ts';
import { downloadSkillDraft, getSkillDraft, installSkillDraft } from '../src/api/skillDraft.ts';
import { messageSkillCreations, skillCreationProgressText, skillPackageFilename } from '../src/utils/skillCreation.ts';
import { mergeAnalysisToolEvent } from '../src/utils/analysisJob.ts';
import { projectHistoryMessages } from '../src/utils/sessionHistory.ts';

const draft = { id: 'draft-one', name: 'station-analysis', description: '站点质量检查与分析', capabilities: ['检查缺测与异常值'], triggers: ['分析站点'], applicability: ['站点观测表'], resources: ['SKILL.md', 'scripts/analyze.py'], installed: false, installed_skill_id: null, validation: { package: 'passed', script_syntax: 'passed', workflow_execution: 'not_run' } };
const tool = overrides => ({ tool_call_id: 'create-one', name: 'skill', function: 'skill_create_from_session', status: 'called', args: {}, timestamp: 3, skill_creation: draft, ...overrides });

test('review response, required revision and actual acceptance have separate progress labels', () => {
  const progress = { stage: 'method_review', attempt: 1, code: null };
  assert.equal(skillCreationProgressText({ ...progress, status: 'response_received' }), '方法完整性审查 · 审查结果已返回');
  assert.equal(skillCreationProgressText({ ...progress, status: 'revision_required' }), '方法完整性审查 · 需要修订');
  assert.equal(skillCreationProgressText({ ...progress, status: 'accepted' }), '方法完整性审查 · 审查通过');
});

test('safe stage progress survives history merging without becoming a draft or another operation', () => {
  const progress = { stage: 'workflow', status: 'repairing', attempt: 2, code: 'invalid_schema' };
  const history = projectHistoryMessages([
    { event: 'message', data: { role: 'user', content: '创建技能', timestamp: 1 } },
    { event: 'tool', data: tool({ status: 'calling', skill_creation: null, skill_creation_progress: progress }) },
    { event: 'tool', data: tool({ status: 'called', skill_creation: null, execution_status: 'failed', timestamp: 4 }) },
  ]);
  const entries = history.filter(message => message.type === 'tool');
  assert.equal(entries.length, 1);
  assert.deepEqual(entries[0].content.skill_creation_progress, progress);
  assert.deepEqual(history.flatMap(messageSkillCreations), []);
  assert.equal(skillCreationProgressText(progress), '工作流设计 · 修正中');
  assert.equal(skillCreationProgressText({ ...progress, attempt: 0, status: 'passed' }), '工作流设计 · 沿用已检查版本');
  assert.equal(skillCreationProgressText({ ...progress, stage: 'private-path' }), '');
});

test('late lifecycle events retain a skill receipt in live and restored tool state', () => {
  assert.deepEqual(mergeAnalysisToolEvent(tool(), tool({ status: 'calling', skill_creation: null })).skill_creation, draft);
  const history = projectHistoryMessages([
    { event: 'message', data: { role: 'user', content: '创建技能', timestamp: 1 } },
    { event: 'step', data: { id: 'step-one', description: '保存流程', status: 'running', timestamp: 2 } },
    { event: 'tool', data: tool() },
    { event: 'tool', data: tool({ skill_creation: null, timestamp: 4 }) },
    { event: 'step', data: { id: 'step-one', description: '保存流程', status: 'completed', timestamp: 5 } },
    { event: 'done', data: { timestamp: 6 } },
  ]);
  assert.ok(history.some(message => message.type === 'task-summary'), 'execution details will collapse after completion');
  assert.deepEqual(history.flatMap(messageSkillCreations), [draft]);
});

test('creation cards deduplicate tool snapshots while retaining distinct draft results', () => {
  assert.deepEqual(messageSkillCreations({ type: 'step', content: { tools: [tool(), tool(), tool({ skill_creation: { ...draft, id: 'second' } })] } }).map(item => item.id), ['draft-one', 'second']);
  assert.deepEqual(messageSkillCreations({ type: 'assistant', content: { content: '技能名称只是普通文本', skill_creation: draft } }), []);
});

test('skill download filenames are local basenames and do not contain path or control characters', () => {
  assert.equal(skillPackageFilename('station-analysis'), 'station-analysis.zip');
  assert.equal(skillPackageFilename('../bad/name\u0000:example'), '-bad-name--example.zip');
  assert.equal(skillPackageFilename('...'), 'skill.zip');
});

test('draft reading and downloading never install; the explicit add action uses a separate POST', async () => {
  const previousAdapter = apiClient.defaults.adapter;
  const requests = [];
  apiClient.defaults.adapter = async config => {
    requests.push({ method: config.method, url: config.url, responseType: config.responseType });
    return { config, status: 200, statusText: 'OK', headers: {}, data: config.url.endsWith('/download')
      ? new Blob(['synthetic-zip'], { type: 'application/zip' })
      : { code: 0, msg: '', data: { ...draft, installed: config.method === 'post' } } };
  };
  try {
    assert.equal((await getSkillDraft('owner/id')).installed, false);
    assert.equal(await (await downloadSkillDraft('owner/id')).text(), 'synthetic-zip');
    assert.ok(requests.every(request => request.method === 'get'));
    assert.equal((await installSkillDraft('owner/id')).installed, true);
    assert.deepEqual(requests, [
      { method: 'get', url: '/skills/drafts/owner%2Fid', responseType: undefined },
      { method: 'get', url: '/skills/drafts/owner%2Fid/download', responseType: 'blob' },
      { method: 'post', url: '/skills/drafts/owner%2Fid/install', responseType: undefined },
    ]);
  } finally { apiClient.defaults.adapter = previousAdapter; }
});
