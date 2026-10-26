import assert from 'node:assert/strict';
import test from 'node:test';
import { readShellOutputMetadata, readShellOutputPage, SHELL_LOG_PAGE_BYTES } from '../src/utils/shellOutputPage.ts';
const id = 'a'.repeat(32);
const metadata = extra => ({ output_id: id, total_bytes: 10, preview_truncated: false, stream_complete: false, log_status: 'available', ...extra });
const response = (extra = {}) => ({ output: 'part', output_metadata: metadata(), output_page: { cursor: 0, start: 0, next_cursor: 4, lossy: false, source: 'spool', eof: false }, ...extra });
test('log pages are bound to an opaque stream and monotonic cursor', () => {
  const result = readShellOutputPage(response(), id, 0);
  assert.equal(result.output, 'part'); assert.equal(result.page.next_cursor, 4);
  for (const invalid of [undefined, {}, metadata({ output_id: '/private/log' }), metadata({ total_bytes: -1 }), metadata({ total_bytes: 1.5 }), metadata({ log_status: 'unknown' })]) assert.equal(readShellOutputMetadata(invalid), undefined);
  assert.throws(() => readShellOutputPage(response(), 'b'.repeat(32), 0));
  assert.throws(() => readShellOutputPage(response(), id, 1));
});
test('missing retained bytes remain explicit instead of inventing a complete log', () => {
  const result = readShellOutputPage(response({ output_metadata: metadata({ log_status: 'limit_exceeded', stream_complete: true }),
    output_page: { cursor: 0, start: 6, next_cursor: 10, lossy: true, source: 'tail', eof: true } }), id, 0);
  assert.equal(result.page.lossy, true); assert.equal(result.page.source, 'tail'); assert.equal(result.page.eof, true);
});
test('malformed or oversized pages cannot replace the visible log', () => {
  const original = response();
  for (const patch of [{ cursor: -1 }, { start: -1 }, { next_cursor: 11 }, { next_cursor: 0.5 }, { next_cursor: 5 }, { start: 3 }, { eof: true }, { source: '/private/log' }]) {
    assert.throws(() => readShellOutputPage(response({ output_page: { ...original.output_page, ...patch } }), id, 0));
  }
  assert.throws(() => readShellOutputPage(response({ output: 'x'.repeat(SHELL_LOG_PAGE_BYTES + 1) }), id, 0));
});
