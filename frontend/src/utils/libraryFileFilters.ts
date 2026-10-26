import type { FileInfo } from '../api/file';
import type { DataLibraryAsset } from '../api/dataLibrary';
import { personalDataDisplayName } from './personalDataTree';

export function libraryFileFormat(file: Pick<FileInfo, 'filename'>): string {
  const name = personalDataDisplayName(file.filename).toLowerCase();
  if (name.length > 7 && name.endsWith('.nii.gz')) return 'nii.gz';
  if (name.length > 7 && name.endsWith('.tar.gz')) return 'tar.gz';
  const index = name.lastIndexOf('.');
  return index > 0 && index < name.length - 1 ? name.slice(index + 1) : 'none';
}

export function libraryFormatLabel(format: string): string {
  const names: Record<string, string> = { nc: 'NetCDF', nc4: 'NetCDF', netcdf: 'NetCDF', xls: 'Excel', xlsx: 'Excel', 'nii.gz': 'NIfTI', h5: 'HDF5', hdf5: 'HDF5', mseed: 'MiniSEED', md: 'Markdown', txt: '文本' };
  return format === 'none' ? '无扩展名' : names[format] ? `${names[format]} (.${format})` : format.toUpperCase();
}

/** UI dates refer to the user's local calendar days, including the end day. */
export function libraryDateBounds(from: string, to: string): { from?: string; to?: string; valid: boolean } {
  function parse(value: string, nextDay = false): string | undefined {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return undefined;
    const [year, month, day] = value.split('-').map(Number);
    const date = new Date(year, month - 1, day);
    if (date.getFullYear() !== year || date.getMonth() !== month - 1 || date.getDate() !== day) return undefined;
    if (nextDay) date.setDate(date.getDate() + 1);
    return date.toISOString();
  }
  const lower = from ? parse(from) : undefined;
  const upper = to ? parse(to, true) : undefined;
  return { from: lower, to: upper, valid: (!from || Boolean(lower)) && (!to || Boolean(upper)) && (!lower || !upper || lower < upper) };
}

export function filterLibraryFiles(assets: DataLibraryAsset[], query: string, format: string, from: string, to: string): DataLibraryAsset[] {
  const bounds = libraryDateBounds(from, to);
  if (!bounds.valid) return [];
  const needle = query.trim().toLowerCase();
  return assets.flatMap(asset => {
    const updated = new Date(asset.updated_at).getTime();
    if ((bounds.from && !(updated >= Date.parse(bounds.from))) || (bounds.to && !(updated < Date.parse(bounds.to)))) return [];
    const files = asset.files.filter(file => (!needle || personalDataDisplayName(file.filename).toLowerCase().includes(needle)) && (!format || libraryFileFormat(file) === format));
    return files.length ? [{ ...asset, files }] : [];
  });
}
