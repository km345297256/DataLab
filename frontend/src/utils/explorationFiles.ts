import type { FileInfo } from '../api/file';

// A draft can contain uploads in flight. Only real, completed objects can be
// saved or attached to an analysis; keep browser File objects out of navigation.
export function readyExplorationFiles(files: FileInfo[]): FileInfo[] {
  const seen = new Set<string>();
  return files.filter(file => {
    const id = file.file_id?.replace(/^gridfs:/, '');
    const status = (file as FileInfo & { status?: string }).status;
    if (!id || id.startsWith('temp-') || status === 'uploading' || status === 'failed' || seen.has(id)) return false;
    seen.add(id);
    return true;
  }).map(file => ({
    file_id: file.file_id.replace(/^gridfs:/, ''), filename: file.filename, content_type: file.content_type,
    size: file.size, upload_date: file.upload_date,
  }));
}

export function mergeExplorationFiles(current: FileInfo[], added: FileInfo[]): FileInfo[] {
  const ids = new Set(current.map(file => file.file_id.replace(/^gridfs:/, '')));
  return [...current, ...readyExplorationFiles(added).filter(file => {
    if (ids.has(file.file_id)) return false;
    ids.add(file.file_id);
    return true;
  })];
}

/** Toggle one library file without discarding uploads that are still in flight. */
export function toggleExplorationFile(current: FileInfo[], file: FileInfo): FileInfo[] {
  const id = file.file_id.replace(/^gridfs:/, '');
  if (current.some(item => item.file_id.replace(/^gridfs:/, '') === id)) {
    return current.filter(item => item.file_id.replace(/^gridfs:/, '') !== id);
  }
  const next = mergeExplorationFiles(current, [file]);
  if (new Set(next.map(item => item.file_id.replace(/^gridfs:/, ''))).size > 32) {
    throw new Error('Too many analysis inputs');
  }
  return next;
}
