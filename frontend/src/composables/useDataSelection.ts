import type { FileInfo } from '../api/file';

// Keep the handoff between My Data and the composer in memory. Neither browser
// storage nor URL parameters need to contain file metadata or local paths.
let pendingFiles: FileInfo[] = [];

export function setPendingLibraryFiles(files: FileInfo[]): void {
  pendingFiles = [...new Map(files.map(file => [file.file_id, { ...file }])).values()];
}

export function takePendingLibraryFiles(): FileInfo[] {
  const files = pendingFiles;
  pendingFiles = [];
  return files;
}

export function clearPendingLibraryFiles(): void {
  pendingFiles = [];
}
