import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import ts from 'typescript';

const source = readFileSync(new URL('../src/api/visualization.ts', import.meta.url), 'utf8');
const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
function apiFor(response) {
  const module = { exports: {} };
  const dependencies = {
    './client': { apiClient: {} },
    '../visualizations/contract': { parseVisualizationCatalog() { assert.fail('Unexpected catalog request'); } },
    '../visualizations/runtime': { requestVisualization: async () => response },
  };
  new Function('require', 'module', 'exports', compiled)(id => {
    assert.ok(id in dependencies, `Unexpected dependency ${id}`); return dependencies[id];
  }, module, module.exports);
  return module.exports;
}
function fits() {
  return { plugin_id: 'fits-image', version: 'a'.repeat(64), revision: 'b'.repeat(64),
    metadata: { source_shape: [129, 257], strides: [2, 3], axis_coordinates: 'source pixel indices' },
    warnings: [], sampled: true, payload: {
      view_kind: 'image', selected_variable: 'HDU 0', variables: [], x_label: 'original X', y_label: 'original Y',
      x: Array.from({ length: 86 }, (_, index) => index * 3), y: Array.from({ length: 65 }, (_, index) => index * 2),
      width: 86, height: 65, values: Array(86 * 65).fill(1), extent: null,
    } };
}
const read = value => apiFor(value).getScientificVisualization({ file_id: 'opaque' },
  { id: 'fits-image', reader: 'fits' }, { plugin_id: 'fits-image' }, new AbortController().signal);

test('FITS API retains original pixel coordinates after bounded image sampling', async () => {
  const result = await read(fits());
  assert.equal(result.x.at(-1), 255); assert.equal(result.y.at(-1), 128);
  assert.deepEqual(result.metadata.strides, [2, 3]);
});
test('FITS API rejects resized, unbound or missing source pixel coordinates', async () => {
  for (const mutate of [
    r => { r.payload.x = Array.from({ length: 86 }, (_, index) => index); },
    r => { r.metadata.strides = [1, 1]; },
    r => { r.metadata.source_shape = [128, 257]; },
    r => { r.payload.y[1] = null; },
    r => { r.metadata.axis_coordinates = 'display pixels'; },
    r => { r.metadata.source_shape[0] = true; },
  ]) {
    const result = fits(); mutate(result);
    await assert.rejects(read(result), /原始像素坐标/);
  }
});
