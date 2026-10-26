import type { FileInfo } from '../api/file';
import type { CompletionAdviceData } from '../types/event';

/** A ZIP/RAR extension is not evidence of a Shapefile dataset. */
export function shapefilePreviewSources(files: FileInfo[], advice?: CompletionAdviceData): FileInfo[] {
  if (advice?.shapefile_preview_available !== true) return [];
  const ids = advice.shapefile_preview_file_ids;
  if (ids !== undefined && (!Array.isArray(ids) || ids.some(id => typeof id !== 'string' || !id.trim()))) return [];
  return files.filter(file => typeof file.file_id === 'string' && Boolean(file.file_id.trim())
    && /\.shp$/i.test(file.filename) && (ids === undefined || ids.includes(file.file_id)));
}
