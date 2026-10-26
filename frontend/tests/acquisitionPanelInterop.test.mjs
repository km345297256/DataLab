import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import * as vue from 'vue';
import mitt from 'mitt';
import ts from 'typescript';

function fixture() {
  const eventBus = mitt();
  let occupied = false, opens = 0, closes = 0;
  const acquisitions = { externalPanelOpened() { occupied = true; opens++; }, externalPanelClosed() { occupied = false; closes++; } };
  const names = Object.fromEntries(['SHOW_FILE', 'SHOW_TOOL', 'HIDE_FILE', 'HIDE_TOOL'].map(key => [`EVENT_${key}_PANEL`, key]));
  const source = readFileSync(new URL('../src/composables/useAcquisitionPanelInterop.ts', import.meta.url), 'utf8');
  const script = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS } }).outputText;
  const module = { exports: {} };
  new Function('require', 'module', 'exports', script)(id => {
    if (id === 'vue') return vue;
    if (id.endsWith('/eventBus')) return { eventBus };
    if (id.endsWith('/event')) return names;
    if (id === './useDataAcquisition') return { useDataAcquisition: () => acquisitions };
    throw new Error(id);
  }, module, module.exports);
  const scope = vue.effectScope();
  scope.run(module.exports.useAcquisitionPanelInterop);
  return { eventBus, scope, occupied: () => occupied, calls: () => [opens, closes] };
}

test('late close from a replaced file does not release the active tool view', () => {
  const h = fixture();
  try {
    h.eventBus.emit('SHOW_FILE'); h.eventBus.emit('SHOW_TOOL'); h.eventBus.emit('HIDE_FILE');
    assert.equal(h.occupied(), true);
    assert.deepEqual(h.calls(), [2, 0]);
    h.eventBus.emit('HIDE_TOOL');
    assert.equal(h.occupied(), false);
  } finally { h.scope.stop(); }
});

test('returning from tool to file preserves file precedence and closed views release the region', () => {
  const h = fixture();
  try {
    h.eventBus.emit('SHOW_TOOL'); h.eventBus.emit('SHOW_FILE'); h.eventBus.emit('HIDE_TOOL');
    assert.equal(h.occupied(), true);
    h.eventBus.emit('HIDE_FILE');
    assert.equal(h.occupied(), false);
  } finally { h.scope.stop(); }
});

test('layout teardown removes only its own listeners', () => {
  const h = fixture(); let unrelated = 0;
  h.eventBus.on('SHOW_FILE', () => unrelated++);
  h.scope.stop(); h.eventBus.emit('SHOW_FILE');
  assert.equal(unrelated, 1); assert.deepEqual(h.calls(), [0, 0]);
});
