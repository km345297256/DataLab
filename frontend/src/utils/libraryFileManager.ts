import type { DataLibraryAsset, DataLibraryFolder } from '../api/dataLibrary';
import { personalDataFiles, personalDataDisplayName, safeLibraryRelativePath, type PersonalDataFile } from './personalDataTree';

export type LibraryManagerFile = PersonalDataFile & { updatedAt: string };
export type LibraryManagerFolder = DataLibraryFolder & { children: LibraryManagerFolder[] };
export type LibraryFolderRow = LibraryManagerFolder & { depth: number };

/** Folders are owner-scoped logical paths. Never infer them from record names. */
export function libraryFolderTree(folders: DataLibraryFolder[]): LibraryManagerFolder[] {
  const nodes = new Map<string, LibraryManagerFolder>();
  for (const folder of folders) {
    if (!folder.id || !safeLibraryRelativePath(folder.path)) continue;
    nodes.set(folder.id, { ...folder, name: personalDataDisplayName(folder.name, '未命名文件夹'), children: [] });
  }
  const paths = new Map([...nodes.values()].map(folder => [folder.path, folder]));
  const roots: LibraryManagerFolder[] = [];
  for (const folder of nodes.values()) {
    const parentPath = folder.path.split('/').slice(0, -1).join('/');
    const parent = parentPath ? paths.get(parentPath) : undefined;
    if (parent && parent.id !== folder.id) parent.children.push(folder);
    else roots.push(folder);
  }
  const sort = (items: LibraryManagerFolder[]) => {
    items.sort((a, b) => a.name.localeCompare(b.name, 'zh-CN') || a.id.localeCompare(b.id));
    items.forEach(item => sort(item.children));
  };
  sort(roots);
  return roots;
}

export function libraryFolderRows(roots: LibraryManagerFolder[], expanded: ReadonlySet<string>): LibraryFolderRow[] {
  const rows: LibraryFolderRow[] = [];
  const visit = (nodes: LibraryManagerFolder[], depth: number) => nodes.forEach(node => {
    rows.push({ ...node, depth });
    if (expanded.has(node.id)) visit(node.children, depth + 1);
  });
  visit(roots, 0);
  return rows;
}

export function libraryFolderBreadcrumbs(folders: DataLibraryFolder[], folderId: string): DataLibraryFolder[] {
  const current = folders.find(folder => folder.id === folderId);
  if (!current || !safeLibraryRelativePath(current.path)) return [];
  const paths = new Map(folders.filter(folder => safeLibraryRelativePath(folder.path)).map(folder => [folder.path, folder]));
  const parts = current.path.split('/');
  return parts.flatMap((_, index) => {
    const folder = paths.get(parts.slice(0, index + 1).join('/'));
    return folder ? [folder] : [];
  });
}

/** The server returns complete records; show only files immediately in this folder. */
export function libraryCurrentFiles(assets: DataLibraryAsset[], path: string): LibraryManagerFile[] {
  if (path && !safeLibraryRelativePath(path)) return [];
  const result: LibraryManagerFile[] = [];
  for (const asset of assets) {
    const files = personalDataFiles(asset.files);
    for (const file of files) {
      const parts = file.relative_path?.split('/') ?? [file.filename];
      if (parts.slice(0, -1).join('/') !== path) continue;
      result.push({
        kind: 'file', id: JSON.stringify(['file', asset.id, file.file_id]), assetId: asset.id,
        name: parts[parts.length - 1], missing: asset.status !== 'ready', file,
        relatedFiles: files, updatedAt: asset.updated_at,
      });
    }
  }
  return result.sort((a, b) => a.name.localeCompare(b.name, 'zh-CN') || a.id.localeCompare(b.id));
}
