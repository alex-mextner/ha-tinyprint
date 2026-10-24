// Browser renderer: validates a document, plans and draws it on a canvas and packs the 1-bit
// bitmap. Ghost (dimmed example) and approximate (slider drag) results are display-only: they
// carry `exact: false` and no `document`; a ghost carries no bitmap at all.
import { FaceRegistry } from './fonts.js';
import { RenderError } from './errors.js';
import { BLANK } from './blank.js';
import { planLabel, drawPlan } from './layout.js';
import { processImage } from './image.js';
import { qrModules } from './qr.js';
import { MAX_ROWS, WIDTH, inkPercent, packAlpha, packInk, unpack } from './pack.js';

export { RenderError } from './errors.js';

/** Same keys and values as `studio_render.DEFAULTS`. */
export const DEFAULT_OPTIONS = Object.freeze({
  preset: 'photo',
  method: 'floyd_steinberg',
  threshold: 128,
  brightness: 1.1,
  contrast: 1.25,
  sharpness: 1.2,
  rotate: 0,
  invert: false,
  width: 384,
  fit: 'width',
  font: 'roboto_flex',
  font_size: 24,
  font_width: 100,
  font_weight: 400,
  columns: 1,
  align: 'left',
  border: false,
  qr_size: 320,
  qr_correction: 'M',
});

export const APPROX_WIDTH_STEP = 5;
export const APPROX_WEIGHT_STEP = 50;

const FACE_LIMIT = 8; // the registry's eviction protection is a heuristic (R12): never fewer
const DIM_ALPHA = 0.35; // ghost text opacity
const DIM_GRAY = Math.round(255 * (1 - DIM_ALPHA));
const TEXT_LIMIT = 3000;
const QR_LIMIT = 500;
const PRESETS = ['photo', 'line_art', 'logo', 'soft'];
const METHODS = ['threshold', 'floyd_steinberg', 'ordered'];
const ALIGNS = ['left', 'center', 'right'];
const FITS = ['width', 'original'];
const LEVELS = ['M', 'Q', 'H'];
const ROTATIONS = [0, 90, 180, 270];
const RANGES = {
  width: [96, 384, true],
  threshold: [1, 254, true],
  qr_size: [96, 360, true],
  brightness: [0.25, 2, false],
  contrast: [0.25, 3, false],
  sharpness: [0, 3, false],
};
const DOCUMENT_KEYS = {
  template: ['kind', 'template', 'fields', 'options'],
  text: ['kind', 'text', 'options'],
  qr: ['kind', 'text', 'options'],
  image: ['kind', 'options'],
};
const SELF_TEST_TEXT = 'Привет, мир 0123 Wxyz';

const bad = (message) => new RenderError('bad_document', message);
const isObject = (value) => value !== null && typeof value === 'object' && !Array.isArray(value);
// Code points, like Python's len(), counted without building an array.
function length(text) {
  let count = 0;
  for (const _ of text) count += 1;
  return count;
}

/** Multiples of `step` from 0, half up (475 -> 500 at step 50), clamped to [min, max]. */
export function snapToGrid(value, step, min, max) {
  return Math.min(max, Math.max(min, Math.floor(value / step + 0.5) * step));
}

function checkNumber(key, value, low, high, integer) {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < low || value > high) {
    throw bad(`${key} must be between ${low} and ${high}`);
  }
  if (integer && !Number.isInteger(value)) throw bad(`${key} must be an integer`);
}

function checkChoice(key, value, choices) {
  if (!choices.includes(value)) throw bad(`invalid ${key}`);
}

function checkFont(options, fonts) {
  if (typeof options.font !== 'string' || !Object.hasOwn(fonts, options.font)) {
    throw bad('unknown font');
  }
  const { sizes, width: widthSpec, weight: weightSpec } = fonts[options.font];
  const { font_size: size, font_width: width, font_weight: weight } = options;
  if (sizes.values) {
    if (!sizes.values.includes(size)) throw bad(`font_size must be one of ${sizes.values.join(', ')}`);
  } else {
    checkNumber('font_size', size, sizes.min, sizes.max, true);
  }
  if (widthSpec.kind === 'axis') {
    checkNumber('font_width', width, widthSpec.min, widthSpec.max, true);
  } else if (!widthSpec.values.includes(width)) {
    throw bad(`font_width must be one of ${widthSpec.values.join(', ')}`);
  }
  if (weightSpec.kind === 'axis') {
    checkNumber('font_weight', weight, weightSpec.min, weightSpec.max, true);
  } else if (weight !== weightSpec.value) {
    throw bad(`font_weight must be ${weightSpec.value} for this font`);
  }
}

/** Complete, validated options: defaults merged, every value in its Python range. */
function completeOptions(raw, fonts) {
  // Python's normalize_options treats a missing or null `options` as {}.
  if (raw !== undefined && raw !== null && !isObject(raw)) throw bad('options must be an object');
  const options = { ...DEFAULT_OPTIONS, ...raw };
  if (Object.keys(options).some((key) => !Object.hasOwn(DEFAULT_OPTIONS, key))) {
    throw bad('unknown rendering option');
  }
  checkChoice('preset', options.preset, PRESETS);
  for (const [key, [low, high, integer]] of Object.entries(RANGES)) {
    checkNumber(key, options[key], low, high, integer);
  }
  checkChoice('method', options.method, METHODS);
  checkChoice('rotate', options.rotate, ROTATIONS);
  checkChoice('align', options.align, ALIGNS);
  checkChoice('fit', options.fit, FITS);
  checkChoice('qr_correction', options.qr_correction, LEVELS);
  for (const key of ['invert', 'border']) {
    if (typeof options[key] !== 'boolean') throw bad(`${key} must be a boolean`);
  }
  checkChoice('columns', options.columns, [1, 2, 3]);
  checkFont(options, fonts);
  return options;
}

function checkText(name, value, limit, required) {
  // A code point is at most two UTF-16 units: reject huge strings without materializing them.
  if (typeof value !== 'string' || value.length > 2 * limit || length(value) > limit || (required && BLANK.test(value))) {
    throw bad(`${name} must be text of at most ${limit} characters and not blank where required`);
  }
}

function checkFields(document, templates) {
  const { template } = document;
  if (typeof template !== 'string' || !Object.hasOwn(templates, template)) {
    throw bad('unknown template');
  }
  const spec = templates[template];
  const fields = document.fields === undefined ? {} : document.fields;
  if (!isObject(fields)) throw bad('fields must be an object');
  if (Object.keys(fields).some((key) => !spec.fields.includes(key))) throw bad('unknown template field');
  let combined = 0;
  for (const key of spec.fields) {
    const value = Object.hasOwn(fields, key) ? fields[key] : '';
    checkText(key, value, key === 'qr' ? QR_LIMIT : TEXT_LIMIT, spec.required.includes(key));
    if (key !== 'qr') combined += length(value);
  }
  if (combined > TEXT_LIMIT) throw bad('combined label text exceeds 3000 characters');
}

/** The effective document: complete options, content checked before layout sees it. */
function normalize(document, { fonts, templates }, approximate) {
  if (!isObject(document)) throw bad('document must be an object');
  const known = typeof document.kind === 'string' && Object.hasOwn(DOCUMENT_KEYS, document.kind);
  const allowed = known ? DOCUMENT_KEYS[document.kind] : null;
  if (!allowed) throw bad(`unsupported document kind ${document.kind}`);
  if (Object.keys(document).some((key) => !allowed.includes(key))) throw bad('unknown document key');
  const options = completeOptions(document.options, fonts);
  if (document.kind === 'template') checkFields(document, templates);
  else if (document.kind === 'text') checkText('text', document.text, TEXT_LIMIT, true);
  else if (document.kind === 'qr') {
    checkText('text', document.text, QR_LIMIT, false);
    if (BLANK.test(document.text)) throw new RenderError('blank_qr');
  }
  if (approximate) {
    const { width, weight } = fonts[options.font];
    if (width.kind === 'axis') {
      options.font_width = snapToGrid(options.font_width, APPROX_WIDTH_STEP, width.min, width.max);
    }
    if (weight.kind === 'axis') {
      options.font_weight = snapToGrid(options.font_weight, APPROX_WEIGHT_STEP, weight.min, weight.max);
    }
  }
  const effective = { ...document, options };
  if (document.kind === 'template') effective.fields = { ...document.fields };
  return effective;
}

function makeCanvas(rows) {
  if (typeof OffscreenCanvas === 'function') return new OffscreenCanvas(WIDTH, rows);
  const element = document.createElement('canvas');
  element.width = WIDTH;
  element.height = rows;
  return element;
}

// Resizing a canvas resets its context state, so every resize configures it again.
function configure(ctx) {
  ctx.textRendering = 'geometricPrecision';
  // 'inherit' would take the page's direction: an RTL Home Assistant locale must not change the bidi base.
  ctx.direction = 'ltr';
}

function makeContext(canvas) {
  const ctx = canvas.getContext('2d', { alpha: true, willReadFrequently: true });
  configure(ctx);
  return ctx;
}

// A face is usable only while its own FontFace instance is registered and loaded (a different
// FontFace of the same family does not count): an evicted face would make the canvas fall back
// to another font without any error.
function isLive(face) {
  return face.fontFace.status === 'loaded' && document.fonts.has(face.fontFace);
}

function dimmed(ink, rows) {
  const image = new ImageData(WIDTH, rows);
  const { data } = image;
  for (let i = 0; i < WIDTH * rows; i += 1) {
    const value = ink[i] ? DIM_GRAY : 255;
    data[i * 4] = value;
    data[i * 4 + 1] = value;
    data[i * 4 + 2] = value;
    data[i * 4 + 3] = 255;
  }
  return image;
}

export class Renderer {
  #registry;
  #tables;
  #measure = null;
  #surface = null;
  #pixels = null;
  #disposed = false;
  #isolated = 0; // > 0 while selfTest runs: its renders never touch a buffer someone may hold

  /** @param {{ baseUrl: URL, fonts: object, templates: object }} config */
  constructor({ baseUrl, fonts, templates }) {
    this.#registry = new FaceRegistry(baseUrl, FACE_LIMIT);
    this.#tables = { fonts, templates };
  }

  // Calls `use(face)` in the very turn the face was checked: nothing can evict it in between.
  async #withLiveFace(options, use) {
    const entry = this.#tables.fonts[options.font];
    for (let attempt = 0; attempt < 2; attempt += 1) {
      if (this.#disposed) throw new RenderError('font_load_failed', 'renderer was disposed');
      const face = await this.#registry.face(options.font, entry, options.font_width, options.font_weight);
      if (this.#disposed) throw new RenderError('font_load_failed', 'renderer was disposed');
      if (isLive(face)) return use(face);
    }
    throw new RenderError('font_load_failed', `font ${options.font} is not registered in the page`);
  }

  /**
   * The Face for exactly these options. During a slider drag the strip is drawn with the snapped
   * values (`approximate`): style the overlay from `result.face` then, not from this.
   */
  async faceFor(options) {
    const complete = completeOptions(options, this.#tables.fonts);
    return this.#withLiveFace(complete, (face) => face);
  }

  #measuring() {
    if (!this.#measure) this.#measure = makeContext(makeCanvas(1));
    return this.#measure;
  }

  #drawing(rows) {
    if (!this.#surface) {
      const canvas = makeCanvas(rows);
      this.#surface = { canvas, ctx: makeContext(canvas) };
    }
    const { canvas, ctx } = this.#surface;
    // A new height reallocates the bitmap and resets the context; an unchanged one is only cleared
    // (every draw restores the state it changes, so nothing leaks from one render to the next).
    if (canvas.height !== rows) {
      canvas.height = rows;
      configure(ctx);
    }
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.globalAlpha = 1;
    ctx.clearRect(0, 0, WIDTH, rows);
    return ctx;
  }

  // The returned ImageData is reused by the next render with the same row count: the scheduler
  // hands a result to the panel (which copies it with putImageData) before the next run starts.
  #unpacked(bitmap, rows) {
    if (this.#isolated > 0) return unpack(bitmap, rows);
    const reuse = this.#pixels?.height === rows ? this.#pixels : undefined;
    this.#pixels = unpack(bitmap, rows, reuse);
    return this.#pixels;
  }

  /** @returns {Promise<object>} rejects only with `RenderError` */
  async render(input) {
    if (this.#disposed) throw new RenderError('font_load_failed', 'renderer was disposed');
    try {
      return await this.#render(input);
    } catch (error) {
      if (error instanceof RenderError) throw error;
      const wrapped = bad(`render failed: ${error}`);
      wrapped.cause = error;
      throw wrapped;
    }
  }

  async #render(input) {
    if (!isObject(input)) throw bad('render input must be an object');
    const ghost = Boolean(input.ghost);
    const exact = !ghost && !input.approximate;
    const document = normalize(input.document, this.#tables, !ghost && Boolean(input.approximate));
    const approximate = !ghost && !exact;
    // Nothing a recipe or print job could be built from leaves an inexact render: `document` is
    // null for ghost and approximate results alike, `snapped` only reports the numbers drawn.
    const result = (parts) => ({
      generation: input.generation,
      exact,
      ghost,
      blocks: [],
      slots: [],
      face: null,
      snapped: approximate
        ? { width: document.options.font_width, weight: document.options.font_weight }
        : null,
      ...parts,
      document: exact ? parts.document : null,
    });
    if (document.kind === 'image') {
      const source = input.image;
      if (!isObject(source) || !source.bitmap || source.id === undefined) throw bad('image document needs an image');
      const { ink, rows } = processImage(source, document.options);
      if (rows > MAX_ROWS) throw new RenderError('too_tall', 'label exceeds 2048 rows');
      if (ghost) {
        return result({ rows, bitmap: null, pixels: dimmed(ink, rows), inkPercent: 0, document: null });
      }
      const bitmap = packInk(ink, rows);
      return result({
        rows,
        bitmap,
        pixels: this.#unpacked(bitmap, rows),
        inkPercent: inkPercent(bitmap),
        document,
      });
    }
    // A QR document draws no text, so it needs no font.
    if (document.kind === 'qr') return this.#draw(null, document, ghost, result);
    return this.#withLiveFace(document.options, (face) => this.#draw(face, document, ghost, result));
  }

  // Synchronous from planning to packing: the face checked by the caller stays valid throughout.
  #draw(face, document, ghost, result) {
    const plan = planLabel(document, face, this.#measuring(), this.#tables.templates, qrModules);
    const { rows } = plan;
    if (rows > MAX_ROWS) throw new RenderError('too_tall', 'label exceeds 2048 rows');
    const ctx = this.#drawing(rows);
    const parts = { rows, blocks: plan.blocks, slots: plan.slots, face };
    if (ghost) {
      ctx.save();
      try {
        ctx.fillStyle = '#fff';
        ctx.fillRect(0, 0, WIDTH, rows);
        ctx.globalAlpha = DIM_ALPHA;
        drawPlan(ctx, plan);
      } finally {
        ctx.restore();
      }
      return result({
        ...parts,
        bitmap: null,
        pixels: ctx.getImageData(0, 0, WIDTH, rows),
        inkPercent: 0,
        document: null,
      });
    }
    ctx.save();
    try {
      drawPlan(ctx, plan);
    } finally {
      ctx.restore();
    }
    const bitmap = packAlpha(ctx.getImageData(0, 0, WIDTH, rows).data, WIDTH, rows);
    return result({
      ...parts,
      bitmap,
      pixels: this.#unpacked(bitmap, rows),
      inkPercent: inkPercent(bitmap),
      document,
    });
  }

  async #textWidth(width, weight) {
    const options = { ...DEFAULT_OPTIONS, font_width: width, font_weight: weight };
    return this.#withLiveFace(options, (face) => {
      const ctx = this.#measuring();
      ctx.font = face.css(24);
      return ctx.measureText(SELF_TEST_TEXT).width * face.scaleX;
    });
  }

  /** Checks this engine can run the renderer; never throws. */
  async selfTest() {
    // Its renders must not write into an ImageData that someone may still hold.
    this.#isolated += 1;
    try {
      return await this.#runSelfTest();
    } finally {
      this.#isolated -= 1;
    }
  }

  async #runSelfTest() {
    const fixed = { kind: 'text', text: SELF_TEST_TEXT, options: {} };
    const checks = [];
    const run = async (name, body) => {
      try {
        checks.push({ name, ok: true, detail: await body() });
      } catch (error) {
        checks.push({ name, ok: false, detail: error instanceof RenderError ? `${error.code}: ${error.message}` : String(error) });
      }
    };
    const strictly = (values) => values.every((value, i) => i === 0 || value > values[i - 1]);
    await run('faces_loaded', async () => {
      const families = [];
      for (const [key, entry] of Object.entries(this.#tables.fonts)) {
        const options = {
          ...DEFAULT_OPTIONS,
          font: key,
          font_width: entry.width.default,
          font_weight: entry.weight.kind === 'fixed' ? entry.weight.value : entry.weight.default,
        };
        families.push(await this.#withLiveFace(options, (face) => face.family));
      }
      return families.join(', ');
    });
    await run('width_axis_changes_glyph_widths', async () => {
      const widths = [];
      for (const width of [25, 50, 75, 100]) widths.push(await this.#textWidth(width, 400));
      if (!strictly(widths)) throw new Error(`widths ${widths.join(' ')} do not increase`);
      return widths.map((w) => w.toFixed(1)).join(' < ');
    });
    await run('weight_axis_changes_glyph_widths', async () => {
      const light = await this.#textWidth(100, 100);
      const heavy = await this.#textWidth(100, 1000);
      if (!(heavy > light)) throw new Error(`weight 1000 (${heavy}) is not wider than weight 100 (${light})`);
      return `${light.toFixed(1)} < ${heavy.toFixed(1)}`;
    });
    await run('render_is_deterministic', async () => {
      const first = await this.render({ document: fixed, image: null, ghost: false, approximate: false, generation: 0 });
      const again = await this.render({ document: fixed, image: null, ghost: false, approximate: false, generation: 0 });
      if (first.inkPercent === 0) throw new Error('the test label has no ink');
      if (first.rows !== again.rows || first.bitmap.some((byte, i) => byte !== again.bitmap[i])) {
        throw new Error('two renders differ');
      }
      return `${first.rows} rows, ${first.inkPercent.toFixed(1)}% ink`;
    });
    await run('pack_roundtrip_matches_display', async () => {
      const rows = 5;
      const ink = new Uint8Array(WIDTH * rows);
      for (let i = 0; i < ink.length; i += 1) ink[i] = (i * 7 + (i >> 5)) % 3 === 0 ? 1 : 0;
      const shown = unpack(packInk(ink, rows), rows).data;
      for (let i = 0; i < ink.length; i += 1) {
        if (shown[i * 4] !== (ink[i] ? 0 : 255) || shown[i * 4 + 3] !== 255) throw new Error(`pattern dot ${i} differs`);
      }
      const result = await this.render({ document: fixed, image: null, ghost: false, approximate: false, generation: 0 });
      const label = unpack(result.bitmap, result.rows).data;
      for (let i = 0; i < label.length; i += 1) {
        if (result.pixels.data[i] !== label[i]) throw new Error(`label pixel byte ${i} differs from the bitmap`);
      }
      return `${rows}-row pattern and ${result.rows}-row label`;
    });
    return {
      ok: checks.every((check) => check.ok),
      engine: { userAgent: navigator.userAgent, offscreenCanvas: typeof OffscreenCanvas === 'function' },
      checks,
    };
  }

  dispose() {
    this.#disposed = true;
    this.#registry.dispose();
    this.#measure = null;
    this.#surface = null;
    this.#pixels = null;
  }
}
