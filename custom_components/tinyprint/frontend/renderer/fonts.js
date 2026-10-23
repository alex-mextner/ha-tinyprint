// Font registry: one fetch per file, one FontFace per (font, width, weight), LRU-bounded.
import { RenderError } from './errors.js';

function hhea(buffer) {
  const view = new DataView(buffer);
  const tables = new Map();
  const count = view.getUint16(4);
  for (let i = 0; i < count; i += 1) {
    const at = 12 + i * 16;
    const tag = String.fromCharCode(...new Uint8Array(buffer, at, 4));
    tables.set(tag, view.getUint32(at + 8));
  }
  const head = tables.get('head');
  const hheaAt = tables.get('hhea');
  if (head === undefined || hheaAt === undefined) {
    throw new RenderError('font_load_failed', 'font has no head/hhea table');
  }
  return {
    unitsPerEm: view.getUint16(head + 18),
    ascender: view.getInt16(hheaAt + 4),
    descender: view.getInt16(hheaAt + 6),
  };
}

function makeFace(family, buffer, entry) {
  let metrics;
  try {
    metrics = hhea(buffer);
  } catch (error) {
    throw new RenderError('font_load_failed', `font metrics unreadable: ${error}`);
  }
  const { unitsPerEm, ascender, descender } = metrics;
  return {
    family,
    entry,
    scaleX: 1,
    css: (size) => `${size}px "${family}"`,
    ascent: (size) => Math.ceil((ascender * size) / unitsPerEm),
    descent: (size) => Math.ceil((-descender * size) / unitsPerEm),
  };
}

// Tiny5 shares one loaded face across widths; the horizontal scale belongs to the request.
const withScale = (scaled, width) => (face) => (scaled ? { ...face, scaleX: width / 100 } : face);

export class FaceRegistry {
  constructor(baseUrl, limit = 8) {
    this.baseUrl = baseUrl;
    this.limit = limit;
    this.buffers = new Map(); // file -> Promise<ArrayBuffer>
    this.faces = new Map(); // family -> {promise, fontFace, settled, fresh}; Map order is LRU order
  }

  #buffer(file) {
    let pending = this.buffers.get(file);
    if (!pending) {
      pending = this.#fetch(file);
      this.buffers.set(file, pending);
      pending.catch(() => this.buffers.delete(file));
    }
    return pending;
  }

  async #fetch(file) {
    const url = new URL(`fonts/${file}`, this.baseUrl);
    let response;
    try {
      response = await fetch(url);
    } catch (error) {
      throw new RenderError('font_missing', `font ${file} could not be fetched: ${error}`);
    }
    if (!response.ok) throw new RenderError('font_missing', `font ${file}: HTTP ${response.status}`);
    return response.arrayBuffer();
  }

  async #load(family, entry, stretch, weight, record) {
    const pending = this.#buffer(entry.file);
    try {
      const buffer = await pending;
      const fontFace = new FontFace(family, buffer, { stretch: `${stretch}%`, weight: String(weight) });
      try {
        await fontFace.load();
      } catch (error) {
        throw new RenderError('font_load_failed', `font ${entry.file}: ${error}`);
      }
      if (fontFace.status !== 'loaded') {
        throw new RenderError('font_load_failed', `font ${entry.file}: status ${fontFace.status}`);
      }
      // Parse before registering: a face that fails here must leave nothing in document.fonts.
      const face = makeFace(family, buffer, entry);
      face.fontFace = fontFace; // the instance the page registered: callers check it is still there
      document.fonts.add(fontFace);
      if (!document.fonts.has(fontFace)) {
        throw new RenderError('font_load_failed', `font ${entry.file} was not registered`);
      }
      record.fontFace = fontFace;
      return face;
    } catch (error) {
      // A corrupt buffer must not stay cached: the next request refetches.
      if (this.buffers.get(entry.file) === pending) this.buffers.delete(entry.file);
      throw error;
    }
  }

  /** `key` is the font key of printer_info.fonts; `entry` its table row. */
  face(key, entry, width, weight) {
    const scaled = entry.width.kind === 'scale';
    const stretch = scaled ? 100 : width;
    const usedWeight = entry.weight.kind === 'fixed' ? entry.weight.value : weight;
    const family = `tp-${key}-w${stretch}-W${usedWeight}`;
    const known = this.faces.get(family);
    if (known) {
      this.faces.delete(family);
      this.faces.set(family, known);
      return known.promise.then(withScale(scaled, width));
    }
    const record = { promise: null, fontFace: null, settled: false, fresh: false };
    record.promise = this.#load(family, entry, stretch, usedWeight, record);
    this.faces.set(family, record);
    record.promise.then(
      () => {
        record.settled = true;
        // Settling counts as use: move to the most-recent end (Map order is LRU order).
        if (this.faces.get(family) === record) {
          this.faces.delete(family);
          this.faces.set(family, record);
        }
        record.fresh = true;
        setTimeout(() => {
          record.fresh = false;
          this.#evict();
        }, 0);
        this.#evict();
      },
      () => {
        if (this.faces.get(family) === record) this.faces.delete(family);
      },
    );
    return record.promise.then(withScale(scaled, width));
  }

  // Only settled records are evictable, least recently used first. A record that settled in
  // the current task is "fresh": its caller is resuming this very turn, so it is skipped and
  // gets evicted by the later pass (burst of more than `limit` loads) once the task ends.
  #evict() {
    for (const [family, record] of this.faces) {
      if (this.faces.size <= this.limit) return;
      if (!record.settled || record.fresh) continue;
      this.faces.delete(family);
      if (record.fontFace) document.fonts.delete(record.fontFace);
    }
  }

  dispose() {
    for (const record of this.faces.values()) {
      if (record.fontFace) document.fonts.delete(record.fontFace);
      else record.promise.then(() => document.fonts.delete(record.fontFace), () => {});
    }
    this.faces.clear();
    this.buffers.clear();
  }
}
