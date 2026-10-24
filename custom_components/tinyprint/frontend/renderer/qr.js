// QR code for the label: the vendored Nayuki encoder plus the same integer module scaling and
// 4-module quiet zone as studio_render `_qr_part`.
import qrcodegen from '../vendor/qrcodegen.js';
import { RenderError } from './errors.js';
import { BLANK } from './blank.js';
import { WIDTH } from './pack.js';

const { QrCode, QrSegment } = qrcodegen;
const LEVELS = { M: QrCode.Ecc.MEDIUM, Q: QrCode.Ecc.QUARTILE, H: QrCode.Ecc.HIGH };
const QUIET = 4;
const MIN_SCALE = 3;
const CACHE_SIZE = 4;

/** Small LRU over a Map: a hit moves the entry to the newest position. */
function cached(cache, key, make) {
  if (cache.has(key)) {
    const value = cache.get(key);
    cache.delete(key);
    cache.set(key, value);
    return value;
  }
  const value = make();
  cache.set(key, value);
  if (cache.size > CACHE_SIZE) cache.delete(cache.keys().next().value);
  return value;
}

const symbols = new Map();
const layers = new Map();

function encode(text, ecc) {
  if (typeof text !== 'string' || BLANK.test(text)) throw new RenderError('blank_qr');
  if (!Object.hasOwn(LEVELS, ecc)) throw new RenderError('bad_document', `unknown QR level ${ecc}`);
  return cached(symbols, JSON.stringify([text, ecc]), () => {
    try {
      // No ECC boost, automatic version and mask.
      return QrCode.encodeSegments(QrSegment.makeSegments(text), LEVELS[ecc], 1, 40, -1, false);
    } catch (error) {
      // The message is the one of the vendored v1.8.0; other RangeErrors are not size problems.
      if (error instanceof RangeError && error.message === 'Data too long') {
        throw new RenderError('qr_too_long', 'QR data is too large');
      }
      // encodeURI inside makeSegments rejects an unpaired surrogate (Python: UnicodeEncodeError).
      if (error instanceof URIError) throw new RenderError('bad_document', 'QR text is not valid Unicode');
      throw error;
    }
  });
}

/** Symbol size in modules without the quiet zone; the counter `planLabel` takes. */
export function qrModules(text, ecc) {
  return encode(text, ecc).size;
}

/** Integer dots per module: the same rule and errors for planning, `qrLayer` and drawing. */
export function qrScale(modules, qrSize) {
  const scale = Math.floor(qrSize / (modules + 2 * QUIET));
  if (!(scale >= MIN_SCALE)) throw new RenderError('qr_modules_too_small');
  if ((modules + 2 * QUIET) * scale > WIDTH) throw new RenderError('bad_document', 'QR is wider than the paper');
  return scale;
}

/**
 * The code as a 384-wide 0/1 ink image. An unchanged (text, ecc, qrSize) returns the same object.
 * @param {string} text
 * @param {'M'|'Q'|'H'} ecc
 * @param {number} qrSize target edge in dots
 * @returns {{ink: Uint8Array, width: 384, rows: number, modules: number}}
 */
export function qrLayer(text, ecc, qrSize) {
  const code = encode(text, ecc);
  return cached(layers, JSON.stringify([text, ecc, qrSize]), () => {
    const modules = code.size;
    const scale = qrScale(modules, qrSize);
    const rows = (modules + 2 * QUIET) * scale;
    const left = Math.floor((WIDTH - rows) / 2);
    const ink = new Uint8Array(WIDTH * rows);
    for (let my = 0; my < modules; my += 1) {
      for (let mx = 0; mx < modules; mx += 1) {
        if (!code.getModule(mx, my)) continue;
        const x0 = left + (QUIET + mx) * scale;
        for (let dy = 0; dy < scale; dy += 1) {
          const start = ((QUIET + my) * scale + dy) * WIDTH + x0;
          ink.fill(1, start, start + scale);
        }
      }
    }
    return { ink, width: WIDTH, rows, modules };
  });
}

/** Fills the dark modules of a planned QR block (`x`, `y`, `w`, `modules`, `data`, `ecc`). */
export function drawQr(ctx, block) {
  const code = encode(block.data, block.ecc);
  if (code.size !== block.modules) throw new RenderError('bad_document', 'QR plan does not match its data');
  const scale = block.w / (block.modules + 2 * QUIET);
  for (let my = 0; my < block.modules; my += 1) {
    for (let mx = 0; mx < block.modules; mx += 1) {
      if (code.getModule(mx, my)) {
        ctx.fillRect(block.x + (QUIET + mx) * scale, block.y + (QUIET + my) * scale, scale, scale);
      }
    }
  }
}
