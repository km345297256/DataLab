import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import * as vue from 'vue';
import { renderToString } from '@vue/server-renderer';
import { compileScript, parse } from '@vue/compiler-sfc';
import ts from 'typescript';
import * as presentation from '../src/utils/analysisPresentation.ts';
import { createMessageKey } from '../src/utils/sessionHistory.ts';
import { uploadAnalysisPrompt, toggleInputFile } from '../src/utils/analysisInputs.ts';
import { isLatestAssistantMessage } from '../src/utils/chatTimeline.ts';
import { isConsecutiveAssistant } from '../src/types/message.ts';

const file = (id, filename = 'result.png') => ({ file_id: id, filename, upload_date: '', file_url: `/files/${id}` });
const attachments = (role, files) => ({ type: 'attachments', content: { role, attachments: files, timestamp: 1 } });

test('input attachments precede their question without mutating execution order or message identities', () => {
  const messages = [
    { type: 'user', content: { content: 'Visualize these files', timestamp: 1 } },
    attachments('user', [file('input-one'), file('input-two')]),
    { type: 'assistant', content: { content: 'Analysis result', timestamp: 2 } },
    attachments('assistant', [file('result')]),
  ];
  const original = [...messages];
  const key = createMessageKey();
  const keys = messages.map(key);
  const entries = presentation.conversationEntries(messages);
  assert.deepEqual(entries.map(entry => entry.index), [1, 0, 2, 3]);
  assert.deepEqual(messages, original);
  for (const { message, index } of entries) {
    assert.equal(message, messages[index]);
    assert.equal(key(message), keys[index]);
  }
  assert.deepEqual(presentation.currentTurnDeliveries(messages).map(item => item.file_id), ['result']);
});

test('input grouping moves past an inserted task summary but never crosses a user turn', () => {
  const summary = { type: 'task-summary', content: { timestamp: 3 } };
  const messages = [
    { type: 'user', content: { content: 'first' } }, summary,
    attachments('user', [file('first-input')]), { type: 'step', content: { tools: [] } },
    { type: 'assistant', content: { content: 'first result' } }, attachments('assistant', [file('first-result')]),
    { type: 'user', content: { content: 'second' } },
    { type: 'task-summary', content: { timestamp: 5 } }, attachments('user', [file('second-input')]),
    { type: 'assistant', content: { content: 'second result' } },
  ];
  const entries = presentation.conversationEntries(messages);
  assert.deepEqual(entries.map(entry => entry.index), [2, 0, 1, 3, 4, 5, 8, 6, 7, 9]);
  assert.equal(presentation.precedingTaskSummary(messages, entries.find(entry => entry.message.type === 'step').index), summary);
});

test('text-only turns and standalone attachments keep their order', () => {
  const messages = [attachments('user', [file('standalone')]),
    { type: 'user', content: { content: 'no new attachment' } },
    { type: 'assistant', content: { content: 'answer' } }, attachments('assistant', [file('result')])];
  assert.deepEqual(presentation.conversationEntries(messages).map(entry => entry.index), [0, 1, 2, 3]);
  assert.deepEqual(presentation.conversationEntries([]), []);
});

test('gallery contains only published assistant images; scripts and input images are not results', () => {
  assert.deepEqual(presentation.deliveredImages(attachments('user', [file('input')])), []);
  const message = attachments('assistant', [file('plot'), file('code', 'plot.py'), file('table', 'values.csv')]);
  assert.deepEqual(presentation.deliveredImages(message).map(item => item.file_id), ['plot']);
  assert.equal(presentation.deliveredFiles(message).length, 3);
});

test('specialized preview suggestions use only current-turn verified attachments', () => {
  const messages = [attachments('assistant', [file('old', 'old.shp')]),
    { type: 'user', content: { content: 'new question' } }, attachments('user', [file('source')]),
    attachments('assistant', [file('new')]), attachments('assistant', [file('new')])];
  assert.deepEqual(presentation.currentTurnDeliveries(messages).map(item => item.file_id), ['new']);
});

test('task folding never reaches across a user turn', () => {
  const summary = { type: 'task-summary', content: { timestamp: 1 } };
  const messages = [summary, { type: 'step' }, { type: 'user' }, { type: 'step' }];
  assert.equal(presentation.precedingTaskSummary(messages, 1), summary);
  assert.equal(presentation.precedingTaskSummary(messages, 3), undefined);
});

test('file-only uploads have a visible request while normal requests stay unchanged', () => {
  assert.match(uploadAnalysisPrompt('', [file('x')]), /概览.*内容.*结构/);
  assert.equal(uploadAnalysisPrompt('  compare these  ', [file('x')]), 'compare these');
  assert.equal(uploadAnalysisPrompt(' ', []), '');
});

test('same-named inputs are selected by identity, explicitly clearing remains empty', () => {
  const files = [file('a', 'same.csv'), file('b', 'same.csv')];
  assert.deepEqual(toggleInputFile(['a'], 'b', files), ['a', 'b']);
  assert.deepEqual(toggleInputFile(['a'], 'a', files), []);
  assert.deepEqual(toggleInputFile(['a'], 'foreign', files), ['a']);
});

test('DataLab integrates delivered images without replacing OpenData and library behavior', () => {
  const page = readFileSync(new URL('../src/pages/ChatPage.vue', import.meta.url), 'utf8');
  for (const marker of ['<OpenDataChat', '<OpenDataResults', '<AnalysisDataLayout', '<AnalysisConversation', 'analysis.beforeSummaryToggle']) assert.ok(page.includes(marker), marker);
  const conversation = readFileSync(new URL('../src/components/AnalysisConversation.vue', import.meta.url), 'utf8');
  for (const marker of ['deliveredImages(message)', 'showFilePanel(file, deliveredFiles(message))', 'name="search"']) assert.ok(conversation.includes(marker), marker);
});
