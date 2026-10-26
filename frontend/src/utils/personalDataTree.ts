import type { DataLibraryAsset, DataLibraryFolder } from '../api/dataLibrary';
import type { FileInfo } from '../api/file';

export interface PersonalDataDirectory {
  kind: 'directory';
  id: string;
  name: string;
  assetIds: string[];
  relativePath: string;
  root: boolean;
  missing: boolean;
  fileCount: number;
  children: PersonalDataNode[];
}

export interface PersonalDataFile {
  kind: 'file';
  id: string;
  name: string;
  assetId: string;
  missing: boolean;
  file: FileInfo;
  relatedFiles: FileInfo[];
}

export type PersonalDataNode = PersonalDataDirectory | PersonalDataFile;
export type PersonalDataRow = PersonalDataNode & { depth: number };

export const personalFileId = (file: Pick<FileInfo, 'file_id'>): string =>
  file.file_id.replace(/^gridfs:/, '');

// Only this explicit API field may create directories. Legacy filenames and
// metadata can contain host paths and must never become directory breadcrumbs.
export function safeLibraryRelativePath(value?: string): string | undefined {
  if (!value || value.length > 4096 || /^[\\/]/.test(value) || /[\\:\u0000-\u001f\u007f]/.test(value)) return undefined;
  const parts = value.split('/');
  if (parts.length > 32 || parts.some(part => !part.trim() || part === '.' || part === '..')) return undefined;
  return value;
}

export function personalDataDisplayName(value: string, fallback = '未命名文件'): string {
  const name = value.replace(/\\/g, '/').split('/').pop()?.replace(/[\u0000-\u001f\u007f]/g, '').trim();
  return name && name !== '.' && name !== '..' ? name : fallback;
}

/** Public file facts shared by tree display, preview and analysis handoff. */
export function personalDataFiles(files: FileInfo[]): FileInfo[] {
  const seen = new Set<string>();
  const result: FileInfo[] = [];
  for (const file of files) {
    const id = file.file_id ? personalFileId(file) : '';
    const status = (file as FileInfo & { status?: string }).status;
    if (!id || id.startsWith('temp-') || status === 'uploading' || status === 'failed' || seen.has(id)) continue;
    seen.add(id);
    result.push({
      file_id: id, filename: personalDataDisplayName(file.filename),
      relative_path: safeLibraryRelativePath(file.relative_path), content_type: file.content_type,
      size: file.size, upload_date: file.upload_date,
    });
  }
  return result;
}

/** Project safe logical paths into one virtual root; records are provenance, not folders. */
export function buildPersonalDataTree(assets: DataLibraryAsset[], folders: readonly DataLibraryFolder[] = []): PersonalDataNode[] {
  const roots: PersonalDataNode[] = [];
  const directories = new Map<string, PersonalDataDirectory>();
  const ensureDirectories = (parts: string[], assetId?: string): PersonalDataNode[] => {
    let children = roots;
    for (let index = 0; index < parts.length; index++) {
      const relativePath = parts.slice(0, index + 1).join('/');
      let directory = directories.get(relativePath);
      if (!directory) {
        directory = {
          kind: 'directory', id: JSON.stringify(['directory', relativePath]),
          name: parts[index], relativePath, assetIds: [], root: index === 0,
          missing: false, fileCount: 0, children: [],
        };
        directories.set(relativePath, directory);
        children.push(directory);
      }
      if (assetId && !directory.assetIds.includes(assetId)) directory.assetIds.push(assetId);
      children = directory.children;
    }
    return children;
  };
  for (const folder of folders) {
    const path = safeLibraryRelativePath(folder.path);
    if (path) ensureDirectories(path.split('/'));
  }
  for (const asset of assets) {
    const files = personalDataFiles(asset.files);
    for (const file of files) {
      const parts = file.relative_path?.split('/') ?? [file.filename];
      const children = ensureDirectories(parts.slice(0, -1), asset.id);
      children.push({
        kind: 'file', id: JSON.stringify(['file', asset.id, file.file_id]), assetId: asset.id,
        name: parts[parts.length - 1], missing: asset.status !== 'ready', file, relatedFiles: files,
      });
    }
  }
  const finish = (nodes: PersonalDataNode[]) => {
    for (const node of nodes) {
      if (node.kind !== 'directory') continue;
      finish(node.children);
      node.fileCount = node.children.reduce((total, child) => total + (child.kind === 'file' ? 1 : child.fileCount), 0);
      node.missing = node.children.length > 0 && node.children.every(child => child.missing);
      node.assetIds.sort();
    }
    nodes.sort((left, right) => {
      if (left.kind !== right.kind) return left.kind === 'directory' ? -1 : 1;
      return left.name.localeCompare(right.name, 'zh-CN') || left.id.localeCompare(right.id);
    });
  };
  finish(roots);
  return roots;
}

export function flattenPersonalDataTree(
  roots: readonly PersonalDataNode[],
  expanded: ReadonlySet<string>,
): PersonalDataRow[] {
  const rows: PersonalDataRow[] = [];
  const visit = (nodes: readonly PersonalDataNode[], depth: number) => {
    for (const node of nodes) {
      rows.push({ ...node, depth });
      if (node.kind === 'directory' && expanded.has(node.id)) visit(node.children, depth + 1);
    }
  };
  visit(roots, 0);
  return rows;
}
