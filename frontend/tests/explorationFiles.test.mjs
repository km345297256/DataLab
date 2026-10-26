import assert from 'node:assert/strict';
import test from 'node:test';
import { readyExplorationFiles, mergeExplorationFiles, toggleExplorationFile } from '../src/utils/explorationFiles.ts';

const file = (id, extra = {}) => ({ file_id: id, filename: 'observations.csv', content_type: 'text/csv', size: 10, upload_date: '2026-10-20T00:00:00Z', ...extra });

test('exploration inputs normalize GridFS aliases before deduplication', () => {
  const files = readyExplorationFiles([file('gridfs:shared'), file('shared'), file('second'), file('gridfs:second')]);
  assert.deepEqual(files.map(item => item.file_id), ['shared', 'second']);
  assert.deepEqual(readyExplorationFiles([file('shared'), file('gridfs:shared')]).map(item => item.file_id), ['shared']);
});

test('navigation-ready inputs contain only public file fields, without browser files, metadata or paths', () => {
  const upload = file('gridfs:upload', {
    file: new File(['sample'], 'observations.csv', { type: 'text/csv' }),
    metadata: { host_path: '/private/data/observations.csv', owner: 'private-owner' },
    relative_path: '/private/data/observations.csv',
    file_url: 'https://example.test/temporary?signature=private',
    status: 'success', progress: 100,
  });
  const result = readyExplorationFiles([upload]);
  assert.deepEqual(result, [file('upload')]);
  assert.notEqual(result[0], upload);
  assert.doesNotMatch(JSON.stringify(result), /private|file_url|metadata|progress|relative_path/);
  assert.equal(upload.file_id, 'gridfs:upload');
  assert.ok(upload.file instanceof File, 'The original upload draft remains intact');
});

test('in-progress, failed and temporary uploads cannot be saved or used as analysis inputs', () => {
  const result = readyExplorationFiles([
    file(''), file('temp-upload'), file('gridfs:temp-upload'),
    file('in-progress', { status: 'uploading' }), file('failed-upload', { status: 'failed' }),
    file('finished-upload', { status: 'success' }), file('saved-file'),
  ]);
  assert.deepEqual(result.map(item => item.file_id), ['finished-upload', 'saved-file']);
});

test('merging library files preserves ongoing uploads while excluding aliased and repeated additions', () => {
  const pending = file('temp-upload', { status: 'uploading', file: new File(['draft'], 'draft.csv') });
  const current = [file('gridfs:already-selected'), pending];
  const merged = mergeExplorationFiles(current, [file('already-selected'), file('new'), file('gridfs:new'), file('failed', { status: 'failed' })]);
  assert.deepEqual(merged.map(item => item.file_id), ['gridfs:already-selected', 'temp-upload', 'new']);
  assert.equal(merged[1], pending);
  assert.equal(current.length, 2, 'Merging does not mutate the active composer array');
});

test('sidebar selection removes all aliases without discarding pending uploads or other selected files', () => {
  const pending = file('temp-pending', { status: 'uploading', progress: 20 });
  const current = [file('gridfs:one'), file('one'), pending, file('two')];
  const removed = toggleExplorationFile(current, file('one'));
  assert.deepEqual(removed.map(item => item.file_id), ['temp-pending', 'two']);
  assert.equal(removed[0], pending);
  const added = toggleExplorationFile(removed, file('gridfs:one'));
  assert.deepEqual(added.map(item => item.file_id), ['temp-pending', 'two', 'one']);
  assert.equal(current.length, 4);
});

test('sidebar additions enforce the shared limit but selected files can always be removed', () => {
  const current = Array.from({ length: 32 }, (_, index) => file(`file-${index}`));
  assert.throws(() => toggleExplorationFile(current, file('extra')), /Too many/);
  assert.equal(current.length, 32);
  assert.equal(toggleExplorationFile(current, file('file-3')).length, 31);
});
