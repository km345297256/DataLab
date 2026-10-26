import assert from 'node:assert/strict';
import test from 'node:test';
import { analysisFollowUps, SKILL_CREATOR_PROMPT } from '../src/utils/completionAdvice.ts';
import { useAnalysisSession } from '../src/composables/useAnalysisSession.ts';

const advice = {
  recommendations: ['比较南北站点的异常月份。', '检验剔除缺测站点后的趋势。', '用年度中位数复核降水变化。'],
  is_skill_candidate: true,
  skill_reason: '站点清洗、分组和趋势检验步骤可对同结构的数据复用。',
};

test('persisted legacy skill entries are removed without rewriting historical advice', () => {
  const saved = { ...advice, recommendations: ['使用/skill-create将这个流程替保存为可复用的技能', '解释站点趋势', '导出年度统计'] };
  const original = structuredClone(saved);
  assert.deepEqual(analysisFollowUps(saved), ['解释站点趋势', '导出年度统计']);
  assert.deepEqual(saved, original);
  assert.deepEqual(analysisFollowUps({ ...saved, recommendations: [SKILL_CREATOR_PROMPT] }), []);
  assert.deepEqual(analysisFollowUps({ ...advice, is_skill_candidate: false }), advice.recommendations);
});

test('three analysis directions remain independent from the skill command', () => {
  assert.deepEqual(analysisFollowUps(advice), advice.recommendations);
  assert.equal(SKILL_CREATOR_PROMPT, '/skill-creator 将这个流程保存为可复用的技能');
  assert.deepEqual(analysisFollowUps({ ...advice, recommendations: [...advice.recommendations, '第四条历史内容'] }), advice.recommendations);
});

test('restored recommendations and skill actions use the same run lifecycle and reject a duplicate send', async () => {
  const requests = [];
  const session = useAnalysisSession({ api: {
    getSessionHistory: async () => ({ session_id: 'analysis-owner', status: 'completed', events: [
      { event: 'done', data: { seq: 1, timestamp: 1, advice } },
    ], has_more: false, next_before_seq: null }),
    chatWithSession: async (...args) => { requests.push(args); return () => {}; },
    createClientMessageId: () => 'followup-id',
  } });
  await session.restore('analysis-owner');
  assert.deepEqual(session.completionAdvice.value, advice);
  await session.send({ message: SKILL_CREATOR_PROMPT, inputFileIds: ['owned-file'], skills: ['analysis-skill'] });
  assert.equal(requests.length, 1);
  assert.equal(requests[0][1], SKILL_CREATOR_PROMPT);
  assert.deepEqual(requests[0][12], ['owned-file']);
  assert.equal(session.completionAdvice.value, undefined);
  assert.equal(await session.send({ message: SKILL_CREATOR_PROMPT }), false);
  assert.equal(requests.length, 1);
  requests[0][8].onMessage({ event: 'done', data: { seq: 2, timestamp: 2, advice: { ...advice, is_skill_candidate: false } } });
  assert.equal(session.completionAdvice.value.is_skill_candidate, false);
  session.dispose();
});
