// Image preparation, a port of studio_render `_load_image` and `_image`: composite on white,
// rotate, resize to the paper width, enhance (Pillow ImageEnhance semantics), invert, then
// threshold / Floyd-Steinberg / ordered dither. The resize uses canvas smoothing, so resized
// bitmaps differ slightly from Pillow LANCZOS; the enhancement and dither steps are exact.
import { RenderError } from './errors.js';
import { MAX_ROWS, WIDTH } from './pack.js';

const MAX_UPLOAD_BYTES = 2 * 1024 * 1024;
const MAX_SOURCE_PIXELS = 4_000_000;
const CACHE_SIZE = 4;
const BAYER = [0, 8, 2, 10, 12, 4, 14, 6, 3, 11, 1, 9, 15, 7, 13, 5];
const METHODS = ['threshold', 'floyd_steinberg', 'ordered'];

let nextId = 1;

/**
 * @param {Blob} blob
 * @returns {Promise<{ id: number, bitmap: ImageBitmap }>}
 */
export async function decodeUpload(blob) {
  if (blob.size > MAX_UPLOAD_BYTES) throw new RenderError('image_too_large');
  let bitmap;
  try {
    bitmap = await createImageBitmap(blob, { imageOrientation: 'from-image' });
  } catch {
    throw new RenderError('image_unreadable');
  }
  if (bitmap.width * bitmap.height > MAX_SOURCE_PIXELS) {
    bitmap.close();
    throw new RenderError('image_too_large');
  }
  const id = nextId;
  nextId += 1;
  return { id, bitmap };
}

/** Python `round()` (half to even) for the scaled height. */
function pyRound(value) {
  const floor = Math.floor(value);
  const rest = value - floor;
  if (rest !== 0.5) return rest < 0.5 ? floor : floor + 1;
  return floor % 2 === 0 ? floor : floor + 1;
}

function canvas(width, height) {
  if (typeof OffscreenCanvas === 'function') return new OffscreenCanvas(width, height);
  const element = document.createElement('canvas');
  element.width = width;
  element.height = height;
  return element;
}

// Clockwise quarter turns as exact integer matrices (a, b, c, d, e, f) over a w x h drawing.
const TURNS = {
  0: () => [1, 0, 0, 1, 0, 0],
  90: (_, h) => [0, 1, -1, 0, h, 0],
  180: (w, h) => [-1, 0, 0, -1, w, h],
  270: (w, h) => [0, -1, 1, 0, 0, w],
};

/** The gray layer: composited on white, rotated and resized. Returns { gray, width, rows }. */
function buildLayer(bitmap, rotate, width, fit) {
  const turned = rotate === 90 || rotate === 270;
  const sourceWidth = turned ? bitmap.height : bitmap.width;
  const sourceHeight = turned ? bitmap.width : bitmap.height;
  const target = fit === 'original' ? Math.min(width, sourceWidth) : width;
  const rows = Math.max(1, pyRound((sourceHeight * target) / sourceWidth));
  if (rows > MAX_ROWS) throw new RenderError('too_tall');
  const drawWidth = turned ? rows : target;
  const drawHeight = turned ? target : rows;
  const surface = canvas(target, rows);
  const ctx = surface.getContext('2d', { willReadFrequently: true });
  ctx.fillStyle = '#fff';
  ctx.fillRect(0, 0, target, rows);
  ctx.imageSmoothingEnabled = true;
  ctx.imageSmoothingQuality = 'high';
  ctx.setTransform(...TURNS[rotate](drawWidth, drawHeight));
  ctx.drawImage(bitmap, 0, 0, drawWidth, drawHeight);
  const rgba = ctx.getImageData(0, 0, target, rows).data;
  const gray = new Uint8Array(target * rows);
  for (let i = 0; i < gray.length; i += 1) {
    // Pillow's "L" conversion.
    gray[i] = (rgba[i * 4] * 19595 + rgba[i * 4 + 1] * 38470 + rgba[i * 4 + 2] * 7471 + 0x8000) >> 16;
  }
  return { gray, width: target, rows };
}

const layers = new Map();

function layerFor(source, rotate, width, fit) {
  const key = `${source.id}|${rotate}|${width}|${fit}`;
  const hit = layers.get(key);
  if (hit) {
    layers.delete(key);
    layers.set(key, hit);
    return { layer: hit, state: 'hit' };
  }
  const layer = buildLayer(source.bitmap, rotate, width, fit);
  layers.set(key, layer);
  if (layers.size > CACHE_SIZE) layers.delete(layers.keys().next().value);
  return { layer, state: 'miss' };
}

const f32 = Math.fround;

/** Pillow Image.blend(degenerate, image, factor) for one pixel: float32 math, truncated, clipped. */
function blend(low, high, factor) {
  const value = f32(low + f32(factor * (high - low)));
  return value <= 0 ? 0 : value >= 255 ? 255 : Math.trunc(value);
}

function enhanceBrightness(px, factor) {
  const alpha = f32(factor);
  for (let i = 0; i < px.length; i += 1) px[i] = blend(0, px[i], alpha);
}

function enhanceContrast(px, factor) {
  let sum = 0;
  for (let i = 0; i < px.length; i += 1) sum += px[i];
  const mean = Math.floor(sum / px.length + 0.5);
  const alpha = f32(factor);
  for (let i = 0; i < px.length; i += 1) px[i] = blend(mean, px[i], alpha);
}

function enhanceSharpness(px, width, rows, factor) {
  const alpha = f32(factor);
  const original = Uint8Array.from(px);
  for (let y = 1; y < rows - 1; y += 1) {
    for (let x = 1; x < width - 1; x += 1) {
      const i = y * width + x;
      const sum =
        original[i - width - 1] + original[i - width] + original[i - width + 1] +
        original[i - 1] + 5 * original[i] + original[i + 1] +
        original[i + width - 1] + original[i + width] + original[i + width + 1];
      // floor(sum / 13 + 0.5), in integers: SMOOTH rounds to nearest. Border pixels stay as they are.
      px[i] = blend(Math.floor((2 * sum + 13) / 26), original[i], alpha);
    }
  }
}

function floydSteinberg(px, width, rows, threshold) {
  const white = new Uint8Array(px.length);
  let current = new Int32Array(width + 2);
  let next = new Int32Array(width + 2);
  const shift = 128 - threshold;
  for (let y = 0; y < rows; y += 1) {
    next.fill(0);
    for (let x = 0; x < width; x += 1) {
      const wanted = Math.min(255, Math.max(0, px[y * width + x] + shift));
      // Errors are kept in sixteenths and divided (truncating) where they are applied: Pillow.
      const value = Math.min(255, Math.max(0, wanted + Math.trunc(current[x + 1] / 16)));
      const out = value > 128 ? 255 : 0;
      white[y * width + x] = out ? 1 : 0;
      const error = value - out;
      current[x + 2] += error * 7;
      next[x] += error * 3;
      next[x + 1] += error * 5;
      next[x + 2] += error;
    }
    [current, next] = [next, current];
  }
  return white;
}

function ordered(px, width, rows, threshold) {
  const white = new Uint8Array(px.length);
  for (let y = 0; y < rows; y += 1) {
    for (let x = 0; x < width; x += 1) {
      const limit = (BAYER[(y % 4) * 4 + (x % 4)] + 0.5) * 16 + threshold - 128;
      white[y * width + x] = px[y * width + x] >= limit ? 1 : 0;
    }
  }
  return white;
}

function check(options) {
  const bad = (what) => new RenderError('bad_document', `image option ${what}`);
  if (![0, 90, 180, 270].includes(options.rotate)) throw bad('rotate');
  if (!Number.isInteger(options.width) || options.width < 96 || options.width > WIDTH) throw bad('width');
  if (options.fit !== 'width' && options.fit !== 'original') throw bad('fit');
  if (!METHODS.includes(options.method)) throw bad('method');
  if (!(options.threshold >= 1 && options.threshold <= 254)) throw bad('threshold');
  for (const key of ['brightness', 'contrast', 'sharpness']) {
    if (!Number.isFinite(options[key])) throw bad(key);
  }
}

/**
 * @param {{ id: number, bitmap: ImageBitmap }} source from `decodeUpload`
 * @param {{ rotate: number, width: number, fit: string, brightness: number, contrast: number,
 *   sharpness: number, invert: boolean, method: string, threshold: number }} options
 * @returns {{ ink: Uint8Array, width: 384, rows: number, layer: 'hit' | 'miss' }} ink is one 0/1
 *   byte per dot, the image centred on white (feed it to `packInk`)
 */
export function processImage(source, options) {
  check(options);
  const { layer, state } = layerFor(source, options.rotate, options.width, options.fit);
  const { width, rows } = layer;
  // The cached layer stays untouched; everything below works on a copy.
  const px = Uint8Array.from(layer.gray);
  if (options.brightness !== 1) enhanceBrightness(px, options.brightness);
  if (options.contrast !== 1) enhanceContrast(px, options.contrast);
  if (options.sharpness !== 1) enhanceSharpness(px, width, rows, options.sharpness);
  if (options.invert) for (let i = 0; i < px.length; i += 1) px[i] = 255 - px[i];
  let white;
  if (options.method === 'threshold') {
    white = px.map((p) => (p >= options.threshold ? 1 : 0));
  } else if (options.method === 'floyd_steinberg') {
    white = floydSteinberg(px, width, rows, options.threshold);
  } else {
    white = ordered(px, width, rows, options.threshold);
  }
  const ink = new Uint8Array(WIDTH * rows);
  const left = (WIDTH - width) >> 1;
  for (let y = 0; y < rows; y += 1) {
    for (let x = 0; x < width; x += 1) {
      ink[y * WIDTH + left + x] = white[y * width + x] ^ 1;
    }
  }
  return { ink, width: WIDTH, rows, layer: state };
}
