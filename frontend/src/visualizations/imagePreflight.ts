/** Inspect compressed headers before handing any bytes to a browser decoder. */
export const IMAGE_INPUT_BYTES = 32 * 1024 * 1024;
export const IMAGE_MAX_PIXELS = 16 * 1024 * 1024;
export const IMAGE_DECODE_BYTES = 128 * 1024 * 1024;
export const IMAGE_MAX_FRAMES = 128;
export class ImagePreviewError extends Error {}
export interface ImageHeader { width: number; height: number; frames: number; mime: string }
const invalid = (): never => { throw new ImagePreviewError('图片文件头不完整或格式不支持安全预览，请下载后查看。'); };
export function checkImageInput(size: number) {
  if (!Number.isSafeInteger(size) || size < 1 || size > IMAGE_INPUT_BYTES) throw new ImagePreviewError('图片超过 32 MiB 读取上限或为空，请下载后查看。');
}
function budget(width: number, height: number, frames: number, mime: string): ImageHeader {
  // Eight bytes per pixel also covers high bit-depth images. Animation uses the
  // full logical canvas for every frame, regardless of compressed subrect size.
  if (![width, height, frames].every(n => Number.isSafeInteger(n) && n > 0) || width > 16384 || height > 16384
    || width * height > IMAGE_MAX_PIXELS || frames > IMAGE_MAX_FRAMES || width * height * frames * 8 > IMAGE_DECODE_BYTES) {
    throw new ImagePreviewError('图片超过安全解码上限（16M 像素、128 帧或 128 MiB 累计像素数据），请缩小后预览。');
  }
  return { width, height, frames, mime };
}
export function inspectImage(bytes: ArrayBuffer): ImageHeader {
  checkImageInput(bytes.byteLength);
  const b = new Uint8Array(bytes), v = new DataView(bytes);
  const need = (at: number, count: number) => { if (at < 0 || count < 0 || at + count > b.length) invalid(); };
  const word = (at: number, count: number) => { need(at, count); return String.fromCharCode(...b.subarray(at, at + count)); };
  const u16 = (at: number, le = false) => { need(at, 2); return v.getUint16(at, le); };
  const u32 = (at: number, le = false) => { need(at, 4); return v.getUint32(at, le); };
  const u24 = (at: number) => { need(at, 3); return b[at] | b[at + 1] << 8 | b[at + 2] << 16; };

  if (b.length >= 8 && word(0, 8) === '\x89PNG\r\n\x1a\n') {
    let at = 8, width = 0, height = 0, declared = 0, frames = 0, ended = false;
    while (at < b.length) {
      need(at, 12); const size = u32(at), type = word(at + 4, 4), data = at + 8; need(data, size + 4);
      if (at === 8 && (type !== 'IHDR' || size !== 13)) invalid();
      if (type === 'IHDR') { if (width || size !== 13) invalid(); width = u32(data); height = u32(data + 4); budget(width, height, 1, 'image/png'); }
      if (type === 'acTL') { if (declared || size !== 8) invalid(); declared = u32(data); budget(width, height, declared, 'image/png'); }
      if (type === 'fcTL') {
        if (!declared || size !== 26) invalid(); frames++; budget(width, height, frames, 'image/png');
        const fw = u32(data + 4), fh = u32(data + 8);
        if (!fw || !fh || u32(data + 12) + fw > width || u32(data + 16) + fh > height) invalid();
      }
      at = data + size + 4;
      if (type === 'IEND') { if (size) invalid(); ended = true; break; }
    }
    if (!ended || !width || (declared && declared !== frames)) invalid();
    return budget(width, height, declared || 1, 'image/png');
  }
  if (b.length >= 2 && b[0] === 0xff && b[1] === 0xd8) {
    let at = 2, header: ImageHeader | undefined;
    while (at < b.length) {
      if (b[at++] !== 0xff) invalid(); while (b[at] === 0xff) at++;
      need(at, 1); const marker = b[at++];
      if (marker === 0xda && header) return header;
      if (marker === 0xd9) break;
      if (marker === 0x01 || marker >= 0xd0 && marker <= 0xd7) continue;
      const size = u16(at); if (size < 2) invalid(); need(at, size);
      if ([0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7, 0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf].includes(marker)) {
        if (size < 8 || header) invalid(); header = budget(u16(at + 5), u16(at + 3), 1, 'image/jpeg');
      }
      at += size;
    }
    invalid();
  }
  if (b.length >= 13 && ['GIF87a', 'GIF89a'].includes(word(0, 6))) {
    const width = u16(6, true), height = u16(8, true); budget(width, height, 1, 'image/gif');
    let at = 13 + (b[10] & 0x80 ? 3 * (1 << ((b[10] & 7) + 1)) : 0), frames = 0;
    const skipBlocks = () => { while (true) { need(at, 1); const size = b[at++]; if (!size) return; need(at, size); at += size; } };
    while (at < b.length) {
      const type = b[at++];
      if (type === 0x3b) return budget(width, height, frames, 'image/gif');
      if (type === 0x21) { need(at++, 1); skipBlocks(); continue; }
      if (type !== 0x2c) invalid();
      need(at, 9); const fw = u16(at + 4, true), fh = u16(at + 6, true);
      if (!fw || !fh || u16(at, true) + fw > width || u16(at + 2, true) + fh > height) invalid();
      const packed = b[at + 8]; at += 9 + (packed & 0x80 ? 3 * (1 << ((packed & 7) + 1)) : 0);
      need(at++, 1); skipBlocks(); frames++; budget(width, height, frames, 'image/gif');
    }
    invalid();
  }
  if (b.length >= 12 && word(0, 4) === 'RIFF' && word(8, 4) === 'WEBP') {
    const end = u32(4, true) + 8; if (end !== b.length) invalid();
    let width = 0, height = 0, frames = 0, animated = false, image = false;
    const dimensions = (type: string, at: number, size: number): [number, number] => {
      if (type === 'VP8 ') { if (size < 10 || word(at + 3, 3) !== '\x9d\x01\x2a') invalid(); return [u16(at + 6, true) & 0x3fff, u16(at + 8, true) & 0x3fff]; }
      if (type === 'VP8L') { if (size < 5 || b[at] !== 0x2f) invalid(); const bits = u32(at + 1, true); return [(bits & 0x3fff) + 1, ((bits >>> 14) & 0x3fff) + 1]; }
      return invalid();
    };
    for (let at = 12; at < end;) {
      need(at, 8); const type = word(at, 4), size = u32(at + 4, true), data = at + 8; need(data, size);
      if (type === 'VP8X') { if (at !== 12 || size !== 10) invalid(); width = u24(data + 4) + 1; height = u24(data + 7) + 1; animated = !!(b[data] & 2); budget(width, height, 1, 'image/webp'); }
      if (type === 'VP8 ' || type === 'VP8L') {
        if (image || animated) invalid(); const [w, h] = dimensions(type, data, size);
        if (width && (w !== width || h !== height)) invalid(); width = w; height = h; image = true;
      }
      if (type === 'ANMF') {
        if (!animated || size < 24) invalid(); const fw = u24(data + 6) + 1, fh = u24(data + 9) + 1;
        if (u24(data) * 2 + fw > width || u24(data + 3) * 2 + fh > height) invalid();
        let found = false;
        for (let child = data + 16; child < data + size;) {
          need(child, 8); const kind = word(child, 4), length = u32(child + 4, true);
          if (child + 8 + length > data + size) invalid();
          if (kind === 'VP8 ' || kind === 'VP8L') { if (found) invalid(); const [w, h] = dimensions(kind, child + 8, length); if (w !== fw || h !== fh) invalid(); found = true; }
          child += 8 + length + (length & 1);
        }
        if (!found) invalid(); frames++; budget(width, height, frames, 'image/webp');
      }
      at = data + size + (size & 1);
    }
    if (animated ? !frames : !image) invalid();
    return budget(width, height, animated ? frames : 1, 'image/webp');
  }
  if (b.length >= 26 && word(0, 2) === 'BM') {
    const header = u32(14, true); need(14, header);
    if (header === 12) return budget(u16(18, true), u16(20, true), 1, 'image/bmp');
    if (header >= 40) return budget(v.getInt32(18, true), Math.abs(v.getInt32(22, true)), 1, 'image/bmp');
    invalid();
  }
  if (b.length >= 6 && u16(0, true) === 0 && u16(2, true) === 1) {
    const count = u16(4, true); if (!count || count > IMAGE_MAX_FRAMES) invalid(); need(6, count * 16);
    let width = 0, height = 0, pixels = 0;
    for (let n = 0; n < count; n++) {
      const at = 6 + n * 16, w = b[at] || 256, h = b[at + 1] || 256, size = u32(at + 8, true), offset = u32(at + 12, true);
      need(offset, size); if (!size || offset < 6 + count * 16) invalid();
      // An ICO can embed a PNG whose true dimensions exceed its directory hint.
      if (size >= 8 && word(offset, 8) === '\x89PNG\r\n\x1a\n') {
        const nested = inspectImage(bytes.slice(offset, offset + size)); if (nested.width !== w || nested.height !== h || nested.frames !== 1) invalid();
      } else {
        if (size < 40 || u32(offset, true) < 40 || v.getInt32(offset + 4, true) !== w || Math.abs(v.getInt32(offset + 8, true)) !== h * 2) invalid();
      }
      pixels += w * h; width = Math.max(width, w); height = Math.max(height, h);
    }
    if (pixels * 8 > IMAGE_DECODE_BYTES) invalid(); return budget(width, height, count, 'image/x-icon');
  }
  if (b.length >= 12 && word(4, 4) === 'ftyp') throw new ImagePreviewError('此 AVIF / HEIC / HEIF 变体尚不能在解码前确认像素和帧预算，请转换为 PNG / JPEG 或下载查看。');
  return invalid();
}
