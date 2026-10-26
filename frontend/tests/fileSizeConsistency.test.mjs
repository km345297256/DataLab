import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import ts from 'typescript';

function load(path) {
  const module = { exports: {} };
  const code = ts.transpileModule(readFileSync(new URL(path, import.meta.url), 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  new Function('require', 'module', 'exports', code)(name => {
    if (name === 'vue-i18n') return { useI18n: () => ({ t: text => text }) };
    if (name.endsWith('.vue')) return {};
    assert.fail(`Unexpected import: ${name}`);
  }, module, module.exports);
  return module.exports;
}
const { formatFileSize } = load('../src/utils/fileType.ts');
const { openDataSizeLabel } = load('../src/utils/openData.ts');

test('file sizes keep the same value from acquisition through library and analysis attachments', () => {
  for (const bytes of [0, 999, 1000, 1024, 80_700, 3_400_000, 4_800_000_000, 2_000_000_000_000]) {
    const acquisition = openDataSizeLabel(bytes).replace(/\.0(?= )/, '');
    assert.equal(formatFileSize(bytes), acquisition, `${bytes} bytes`);
  }
  assert.equal(formatFileSize(3_400_000), '3.4 MB');
  assert.equal(formatFileSize(4_800_000_000), '4.8 GB');
});

test('missing file sizes are not confused with an empty file', () => {
  for (const bytes of [null, undefined, NaN, Infinity, -1]) {
    assert.equal(formatFileSize(bytes), '大小未知');
    assert.equal(openDataSizeLabel(bytes), '大小未知');
  }
  assert.equal(formatFileSize(0), '0 B');
});
