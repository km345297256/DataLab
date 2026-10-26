import type { FileInfo } from '../api/file';
import type { PreviewLoad } from '../composables/usePreviewLoad';
import { findRelatedFile, isRelativeResourceUrl, splitResourceUrl } from '../utils/relativeFileResources.ts';
import { IMAGE_DECODE_BYTES, inspectImage, type ImageHeader } from './imagePreflight.ts';

export const HTML_INPUT_BYTES = 16 * 1024 * 1024;
export const HTML_RESOURCE_BYTES = 8 * 1024 * 1024;
export const HTML_TOTAL_BYTES = 32 * 1024 * 1024;
export const HTML_RESOURCE_COUNT = 32;
export const HTML_RESOURCE_CONCURRENCY = 3;
export const HTML_DOCUMENT_CHARS = 32 * 1024 * 1024;
export class HtmlPreviewError extends Error {}
const limitError = () => new HtmlPreviewError('HTML 预览超过资源预算（正文 16 MiB、单个资源 8 MiB、总计 32 MiB 或 32 个资源），请精简报告后重试。');
const escapeAttribute = (value: string) => value.replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;');
export const HTML_PREVIEW_CSP = "default-src 'none'; script-src 'unsafe-inline' data: blob:; style-src 'unsafe-inline' data: blob:; img-src blob: data:; font-src blob: data:; media-src blob: data:; connect-src 'none'; object-src 'none'; frame-src 'none'; worker-src 'none'; base-uri 'none'; form-action 'none'";
const viewportStyle = 'html,body{margin:0;min-width:0;max-width:100%;min-height:100%;overflow:auto}body{box-sizing:border-box}img,video,canvas,svg{max-width:100%;height:auto}table{max-width:100%}pre,code{white-space:pre-wrap;overflow-wrap:anywhere}*{box-sizing:border-box}';
// A parent-origin blob cannot be read by nested opaque-origin frames. Authorized
// bounded resources are embedded in this preview only, with no persistent cache.
function resourceUrl(bytes: Uint8Array, mime: string): string {
  const chunks: string[] = [];
  for (let at = 0; at < bytes.length; at += 8192) chunks.push(String.fromCharCode(...bytes.subarray(at, at + 8192)));
  return `data:${mime};base64,${btoa(chunks.join(''))}`;
}

/** The trusted outer frame's frame-src policy also blocks report self-navigation.
 * Both frames retain opaque origins; scripts cannot remove this ancestor policy.
 * A CSP on the report alone cannot reliably forbid its own location navigation.
 */
export function isolateHtmlReport(content: string): string {
  const meta = `<meta http-equiv="Content-Security-Policy" content="${escapeAttribute(HTML_PREVIEW_CSP)}"><meta name="referrer" content="no-referrer">`;
  const report = `<!doctype html><html><head><meta charset="utf-8">${meta}<style>${viewportStyle}</style></head><body>${content}</body></html>`;
  return `<!doctype html><html><head><meta charset="utf-8">${meta}<style>html,body,iframe{margin:0;border:0;width:100%;height:100%;display:block;overflow:hidden}</style></head><body><iframe title="HTML 报告内容" sandbox="allow-scripts" referrerpolicy="no-referrer" srcdoc="${escapeAttribute(report)}"></iframe></body></html>`;
}

export interface HtmlPreviewResult { html: string; missing: string[]; blocked: number; resources: number }
/** Parse in an inert template: no resource can fetch while the policy is built. */
export async function prepareHtmlPreview(html: string, file: FileInfo, files: FileInfo[], load: PreviewLoad,
  readResource: (file: FileInfo, maxBytes: number) => Promise<ArrayBuffer>, doc: Document = document): Promise<HtmlPreviewResult> {
  const initialBytes = new TextEncoder().encode(html).byteLength;
  if (initialBytes > HTML_INPUT_BYTES) throw limitError();
  let tags = 0;
  for (const _match of html.matchAll(/<[a-z!/?]/gi)) if (++tags > 20_000) throw new HtmlPreviewError('HTML 元素过多，超过 20,000 个标签的预览预算，请精简报告后重试。');
  const template = doc.createElement('template'); template.innerHTML = html;
  const pending: Array<{ value: string; apply: (value: string) => void }> = [];
  const missing = new Set<string>(); let blocked = 0, embeddedBytes = 0, decodedImageBytes = 0;
  const chargeImage = (header: ImageHeader) => {
    decodedImageBytes += header.width * header.height * header.frames * 8;
    if (decodedImageBytes > IMAGE_DECODE_BYTES) throw new HtmlPreviewError('报告中的图片合计超过 128 MiB 安全解码预算，请缩小或减少图片后重试。');
  };
  const embedded = new Map<string, string>();
  const embeddedImage = (value: string): string | undefined => {
    if (embedded.has(value)) return embedded.get(value);
    const match = /^data:image\/(?:png|jpeg|gif|webp|bmp|x-icon);base64,([a-z\d+/=\s]+)$/i.exec(value);
    if (!match) return;
    // Check base64 length before allocating its decoded representation.
    if (match[1].length > HTML_RESOURCE_BYTES * 4 / 3 + 4) throw limitError();
    let raw: string; try { raw = atob(match[1]); } catch { return; }
    embeddedBytes += raw.length;
    if (initialBytes + embeddedBytes > HTML_TOTAL_BYTES || embedded.size >= HTML_RESOURCE_COUNT) throw limitError();
    const bytes = Uint8Array.from(raw, c => c.charCodeAt(0));
    let header: ImageHeader; try { header = inspectImage(bytes.buffer); } catch { blocked++; return; }
    chargeImage(header);
    const url = resourceUrl(bytes, header.mime);
    embedded.set(value, url); return url;
  };
  const cssPattern = /url\(\s*(["']?)(.*?)\1\s*\)/gi;
  const relative = (value: string) => isRelativeResourceUrl(value) && !/^[a-z][a-z0-9+.-]*:/i.test(value.trim());
  const css = (value: string, apply: (value: string) => void) => {
    let rewritten = value.replace(/@import\s+(?:url\([^)]*\)|["'][^"']*["'])[^;]*;?/gi, () => { blocked++; return ''; });
    const replacements = new Map<string, string>();
    rewritten = rewritten.replace(cssPattern, (full, _quote, url: string) => {
      if (url.trim().startsWith('#')) return full;
      const inline = embeddedImage(url.trim()); if (inline) return `url("${inline}")`;
      if (!relative(url)) { blocked++; return 'url("")'; }
      pending.push({ value: url, apply: (resolved) => { replacements.set(url, resolved); apply(rewritten.replace(cssPattern, (all, _q, key: string) => relative(key) ? `url("${replacements.get(key) || ''}")` : all)); } });
      return full;
    });
    apply(rewritten.replace(cssPattern, (full, _q, url: string) => relative(url) ? 'url("")' : full));
  };
  const walk = (root: DocumentFragment) => {
    for (const element of Array.from(root.querySelectorAll('*'))) {
      const tag = element.tagName.toLowerCase();
      if (['base', 'meta', 'iframe', 'frame', 'frameset', 'object', 'embed', 'applet'].includes(tag)) { blocked++; element.remove(); continue; }
      if (tag === 'template') { walk((element as HTMLTemplateElement).content); continue; }
      if (tag === 'form') { blocked++; element.replaceWith(...Array.from(element.childNodes)); }
      if (tag === 'link' && element.getAttribute('rel')?.toLowerCase() !== 'stylesheet') { element.remove(); continue; }
      for (const attr of Array.from(element.attributes)) {
        const name = attr.name.toLowerCase(), value = attr.value.trim();
        if (['action', 'formaction', 'target', 'formtarget', 'ping', 'download', 'srcset', 'imagesrcset', 'background', 'manifest', 'srcdoc', 'nonce', 'integrity', 'crossorigin'].includes(name)) { element.removeAttribute(attr.name); continue; }
        if (name === 'style') { css(attr.value, text => element.setAttribute('style', text)); continue; }
        if (!['src', 'href', 'xlink:href', 'poster', 'data'].includes(name)) continue;
        if (value.startsWith('#') && (name === 'href' || name === 'xlink:href')) continue;
        // Navigational links remain readable text. Resource links are authorized
        // individually; absolute blob/data URLs supplied by the report are not.
        element.removeAttribute(attr.name);
        if (tag === 'img' && name === 'src' || tag === 'image' && ['href', 'xlink:href'].includes(name)) {
          const inline = embeddedImage(value); if (inline) { element.setAttribute(attr.name, inline); continue; }
        }
        if (tag === 'a' || tag === 'area' || !relative(value)) { blocked++; continue; }
        pending.push({ value, apply: resolved => { if (resolved) element.setAttribute(attr.name, resolved); } });
      }
      if (tag === 'style') css(element.textContent || '', text => { element.textContent = text; });
    }
  };
  walk(template.content);
  const byId = new Map<string, { file: FileInfo; targets: typeof pending }>();
  for (const target of pending) {
    const resource = findRelatedFile(file, files, target.value);
    if (!resource) { missing.add(target.value); continue; }
    if (!byId.has(resource.file_id)) byId.set(resource.file_id, { file: resource, targets: [] });
    byId.get(resource.file_id)!.targets.push(target);
  }
  const resources = Array.from(byId.values());
  if (resources.length + embedded.size > HTML_RESOURCE_COUNT || pending.length > 1024) throw limitError();
  let reserved = initialBytes + embeddedBytes;
  const reservations = resources.map(({ file }) => {
    const size = file.size ?? HTML_RESOURCE_BYTES;
    if (!Number.isSafeInteger(size) || size < 0 || size > HTML_RESOURCE_BYTES) throw limitError();
    reserved += size; if (reserved > HTML_TOTAL_BYTES) throw limitError(); return size;
  });
  let next = 0, actual = initialBytes + embeddedBytes, expandedChars = html.length, stopped = false;
  // Each resource has an independent read cap; reservation prevents concurrent
  // unknown-length reads from exceeding the aggregate admission budget.
  await Promise.all(Array.from({ length: Math.min(HTML_RESOURCE_CONCURRENCY, resources.length) }, async () => {
    try {
      while (next < resources.length && !stopped) {
        load.assertCurrent(); const index = next++, resource = resources[index];
        const bytes = await readResource(resource.file, reservations[index]); load.assertCurrent();
        if (stopped) return;
        // Metadata is not trusted as the enforcement: reject a dishonest size.
        if (bytes.byteLength > HTML_RESOURCE_BYTES || bytes.byteLength > reservations[index]) throw limitError();
        actual += bytes.byteLength; if (actual > HTML_TOTAL_BYTES) throw limitError();
        // Deduplicated reads can still be repeated thousands of times in the
        // document. Admit their expanded representation before encoding/copying.
        expandedChars += (Math.ceil(bytes.byteLength / 3) * 4 + 256) * resource.targets.length;
        if (expandedChars > HTML_DOCUMENT_CHARS) throw new HtmlPreviewError('HTML 资源展开后超过 32 MiB 页面预算，请减少重复嵌入的资源。');
        let mime = resourceMime(resource.file.filename) || resource.file.content_type || 'application/octet-stream';
        const magic = String.fromCharCode(...new Uint8Array(bytes, 0, Math.min(12, bytes.byteLength)));
        const rasterHeader = /^(?:\x89PNG\r\n\x1a\n|\xff\xd8|GIF8[79]a|BM|\0\0\x01\0)/.test(magic) || magic.startsWith('RIFF') && magic.slice(8) === 'WEBP';
        if (rasterHeader || /^image\/(?:png|jpeg|gif|webp|bmp|x-icon)$/.test(mime)) {
          let header: ImageHeader;
          try { header = inspectImage(bytes); } catch { throw new HtmlPreviewError('报告中的图片无法在安全像素和帧预算内预览，请缩小图片后重试。'); }
          chargeImage(header); mime = header.mime;
        }
        const url = resourceUrl(new Uint8Array(bytes), mime);
        for (const target of resource.targets) target.apply(`${url}${splitResourceUrl(target.value).suffix}`);
      }
    } catch (error) { stopped = true; throw error; }
  }));
  load.assertCurrent();
  const content = template.innerHTML;
  if (content.length > HTML_DOCUMENT_CHARS) throw new HtmlPreviewError('HTML 资源展开后超过 32 MiB 页面预算，请精简报告。');
  return { html: isolateHtmlReport(content), missing: [...missing], blocked, resources: resources.length + embedded.size };
}
function resourceMime(filename: string): string {
  const extension = filename.split('.').pop()?.toLowerCase();
  return ({ js: 'application/javascript', mjs: 'application/javascript', css: 'text/css', png: 'image/png', jpg: 'image/jpeg', jpeg: 'image/jpeg', gif: 'image/gif', webp: 'image/webp', svg: 'image/svg+xml', woff: 'font/woff', woff2: 'font/woff2' } as Record<string, string>)[extension || ''] || '';
}
