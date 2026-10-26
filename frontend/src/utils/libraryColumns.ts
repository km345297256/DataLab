import type { DataLibraryAsset } from '../api/dataLibrary';
import type { FileInfo } from '../api/file';
import { buildPersonalDataTree, personalDataFiles, type PersonalDataDirectory, type PersonalDataNode } from './personalDataTree';

export type LibraryBrowserNode = PersonalDataNode;
export interface LibraryColumn { id: string; name: string; nodes: PersonalDataNode[] }

/** Keep only safe, public file facts when previewing or handing files to analysis. */
export function libraryBrowserFiles(files: FileInfo[]): FileInfo[] {
  return personalDataFiles(files);
}

export function libraryColumnTree(assets: DataLibraryAsset[]): PersonalDataNode[] {
  return buildPersonalDataTree(assets);
}

/** Resolve only consecutive child directories, dropping stale or foreign path segments. */
export function resolveLibraryColumnPath(roots: PersonalDataNode[], path: readonly string[]): PersonalDataDirectory[] {
  let children: PersonalDataNode[] = roots;
  const result: PersonalDataDirectory[] = [];
  for (const id of path) {
    const directory = children.find(node => node.id === id && node.kind === 'directory');
    if (!directory || directory.kind !== 'directory') break;
    result.push(directory);
    children = directory.children;
  }
  return result;
}

export function libraryColumns(roots: PersonalDataNode[], path: readonly string[]): LibraryColumn[] {
  return [{ id: 'library-root', name: '我的数据', nodes: roots }, ...resolveLibraryColumnPath(roots, path).map(directory => ({ id: directory.id, name: directory.name, nodes: directory.children }))];
}

export function findLibraryColumnNode(roots: readonly PersonalDataNode[], id: string): PersonalDataNode | undefined {
  for (const node of roots) {
    if (node.id === id) return node;
    if (node.kind === 'directory') {
      const child = findLibraryColumnNode(node.children, id);
      if (child) return child;
    }
  }
}
