import assert from 'node:assert/strict';
import test from 'node:test';
import { isPlaceholderAssistantMessage } from '../src/utils/analysisPresentation.ts';

test('recognizes only standalone assistant placeholder messages', () => {
  for (const content of [
    'placeholder',
    'placeholder - do not send',
    'placeholder_not_used',
    'TBD.',
    '待补充',
    '占位文本',
    '暂无结果',
  ]) {
    assert.equal(isPlaceholderAssistantMessage(content), true);
  }

  assert.equal(isPlaceholderAssistantMessage('The placeholder value is missing.'), false);
  assert.equal(isPlaceholderAssistantMessage('暂无结果，因为文件无法读取。'), false);
});
