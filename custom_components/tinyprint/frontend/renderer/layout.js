// Browser port of the Python label layout (studio_render.py): wrap, band geometry, anchors, slots.
// Measuring and drawing use the same context font (`face.css(size)`) and `ctx.measureText`; the
// caller owns the context recipe (OffscreenCanvas, textRendering = 'geometricPrecision').
import { RenderError } from './errors.js';

const WIDTH = 384;
const MAX_ROWS = 2048;
const MARGIN = 10; // right margin of every text band and left inset of non-checklist text
const CHECK_INSET = 34; // checklist text starts after the checkbox
const PAD = 8; // paper margin above and below every text band
const CHECKBOX = { x0: 10, x1: 24, top: 7, bottom: 21 }; // PIL rectangle inside a text row
const WRAP_PROBE_LIMIT = 512; // Python never measures more than this many code points at once
const SPOOL_NAMES = { material: 'Материал', color: 'Цвет', weight: 'Остаток', nozzle: 'Сопло' };
const QR_LEVELS = new Set(['M', 'Q', 'H']);
const GUTTER = 8; // space between newspaper columns (advance width; ink may overhang by ~1 px)
const CHECK_INDENT = CHECK_INSET - MARGIN; // checklist text starts this far after its column edge
const COLUMN_COUNTS = new Set([1, 2, 3]);

/** Python's `round()`: ties go to the even neighbour (JS `Math.round` rounds them up). */
export function pyRound(value) {
  const floor = Math.floor(value);
  const diff = value - floor;
  if (diff < 0.5) return floor;
  if (diff > 0.5) return floor + 1;
  return floor % 2 === 0 ? floor : floor + 1;
}

const codePoints = (text) => Array.from(text);

/** Same rule as `fonts.derived_size`. */
export function derivedSize(entry, role, size) {
  const rule = entry.derived;
  if (role !== 'title' && role !== 'heading' && role !== 'footer') {
    throw new RenderError('bad_document', `unknown font role ${role}`);
  }
  let value = size + rule[role];
  if (rule.snap) value = Math.floor(value / rule.snap + 0.5) * rule.snap;
  value = Math.max(rule.floor, value);
  if (rule.cap && role !== 'footer') value = Math.min(Math.max(rule.cap, size), value);
  return value;
}

// str.expandtabs(4): the column counts code points and restarts after \n and \r.
function expandTabs(text) {
  let out = '';
  let column = 0;
  for (const char of text) {
    if (char === '\t') {
      const pad = 4 - (column % 4);
      out += ' '.repeat(pad);
      column += pad;
    } else {
      out += char;
      column = char === '\n' || char === '\r' ? 0 : column + 1;
    }
  }
  return out;
}

/**
 * `_wrap` of the Python renderer: greedy cut by binary search on code points, backing off to
 * the last space. A line always keeps at least one code point. No row cap (`planLabel` checks).
 * @returns {{ text: string, check: boolean, head: boolean }[]} `head` marks the first line of a
 *   paragraph (the empty paragraph's one blank line included), `check` the checklist ones
 */
export function wrapText(measure, text, maxWidth, { checklist = false } = {}) {
  const lines = [];
  for (const paragraph of expandTabs(text).split('\n')) {
    const chars = codePoints(paragraph.replace(/\r+$/, ''));
    let start = 0;
    let first = true;
    while (start < chars.length) {
      // One code point past the probe limit: Python's rfind may land on it.
      const window = chars.slice(start, start + WRAP_PROBE_LIMIT + 1);
      let low = 1;
      let high = Math.min(window.length, WRAP_PROBE_LIMIT);
      while (low < high) {
        const middle = (low + high + 1) >> 1;
        if (measure(window.slice(0, middle).join('')) <= maxWidth) low = middle;
        else high = middle - 1;
      }
      let cut = low;
      if (start + cut < chars.length) {
        const split = window.lastIndexOf(' ', cut);
        if (split > 0) cut = split;
      }
      lines.push({ text: window.slice(0, cut).join(''), check: checklist && first, head: first });
      first = false;
      start += cut;
      while (start < chars.length && chars[start] === ' ') start += 1;
    }
    if (first) lines.push({ text: '', check: false, head: true });
  }
  return lines;
}

// Float start of a line as Pillow draws it (R4); `drawX` below is the integer the browser uses.
function startX(block, width) {
  const inset = block.x;
  if (block.checklist || block.align === 'left') return inset;
  if (block.align === 'center') return Math.max(inset, (WIDTH - width) / 2);
  return Math.max(inset, WIDTH - MARGIN - width);
}

function drawX(block, width) {
  const inset = block.x;
  if (block.checklist || block.align === 'left') return inset;
  if (block.align === 'center') return Math.max(inset, pyRound((WIDTH - width) / 2));
  return Math.max(inset, WIDTH - MARGIN - pyRound(width));
}

// Start of a line inside a newspaper column. Checklist text ignores `align` like the single
// column; a line wider than its column (a lone oversized glyph) never starts left of it.
function columnX(block, column, width) {
  if (block.checklist) return column.x + CHECK_INDENT;
  if (block.align === 'left') return column.x;
  if (block.align === 'center') return column.x + Math.max(0, pyRound((column.w - width) / 2));
  return column.x + Math.max(0, column.w - pyRound(width));
}

/**
 * Splits the line counts of `items` into `n` contiguous columns with the smallest tallest column.
 * Among equal tallest columns the line counts read left to right are lexicographically largest
 * (earlier columns fill first), which is what filling each column up to the minimal limit gives.
 * @param {number[]} items line count of every unbreakable item
 * @returns {number[][]} `n` columns, each the item line counts it holds (a column may be empty)
 */
export function flowColumns(items, n) {
  const fits = (limit) => {
    let used = 1;
    let height = 0;
    for (const count of items) {
      if (count > limit) return false;
      if (height + count > limit) {
        used += 1;
        height = 0;
      }
      height += count;
    }
    return used <= n;
  };
  let low = items.reduce((most, count) => Math.max(most, count), 0);
  let high = items.reduce((sum, count) => sum + count, 0);
  while (low < high) {
    const middle = (low + high) >> 1;
    if (fits(middle)) high = middle;
    else low = middle + 1;
  }
  const columns = Array.from({ length: n }, () => []);
  let column = 0;
  let height = 0;
  for (const count of items) {
    if (height + count > low) {
      column += 1;
      height = 0;
    }
    columns[column].push(count);
    height += count;
  }
  return columns;
}

const columnWidth = (count) => Math.floor((WIDTH - 2 * MARGIN - GUTTER * (count - 1)) / count);

// The wrapped lines of the main text laid out as newspaper columns: items are lines, except in a
// checklist where one paragraph (all its wrapped lines) is one item and never splits.
function newspaperColumns(wrapped, widths, checklist, count) {
  const colW = columnWidth(count);
  const items = [];
  for (const line of wrapped) {
    if (checklist && !line.head) items[items.length - 1] += 1;
    else items.push(1);
  }
  let first = 0;
  return flowColumns(items, count).map((group, i) => {
    const lines = group.reduce((sum, itemLines) => sum + itemLines, 0);
    const column = {
      x: MARGIN + i * (colW + GUTTER),
      w: colW,
      lines: wrapped.slice(first, first + lines).map((line) => line.text),
      checks: wrapped.slice(first, first + lines).map((line) => line.check),
      widths: widths.slice(first, first + lines),
      items: group,
    };
    first += lines;
    return column;
  });
}

function textPart(field, text, size, align, checklist, face, ctx, columns = 1) {
  const ascent = face.ascent(size);
  const descent = face.descent(size);
  const step = ascent + descent + 5;
  const inset = checklist ? CHECK_INSET : MARGIN;
  ctx.font = face.css(size);
  const measure = (s) => ctx.measureText(s).width * face.scaleX;
  // A column is its width less the checklist's checkbox indent; one column is the whole text band.
  const colW = columnWidth(columns);
  const wrapWidth = colW - (checklist ? CHECK_INDENT : 0);
  const wrapped = wrapText(measure, text, wrapWidth, { checklist });
  const widths = wrapped.map((line) => measure(line.text));
  const part = {
    field,
    kind: 'text',
    x: inset,
    w: WIDTH - inset - MARGIN,
    h: Math.max(1, wrapped.length) * step + 2 * PAD,
    step,
    size,
    ascent,
    descent,
    lines: wrapped.map((line) => line.text),
    checks: wrapped.map((line) => line.check),
    widths,
    align: checklist ? 'left' : align,
    checklist,
    face,
  };
  if (columns > 1) {
    // The block spans the full usable width; `columns` hold what each column draws.
    part.x = MARGIN;
    part.w = WIDTH - 2 * MARGIN;
    part.columns = newspaperColumns(wrapped, widths, checklist, columns);
    const tallest = Math.max(...part.columns.map((column) => column.lines.length));
    part.h = Math.max(1, tallest) * step + 2 * PAD;
  }
  return part;
}

// Where each `Name: value` row's value starts: the first line holding more ink characters
// than "Name:" has, so a prefix that wraps itself (large sizes) moves the value down.
function spoolPart(rows, size, align, face, ctx) {
  const ascent = face.ascent(size);
  const descent = face.descent(size);
  const step = ascent + descent + 5;
  ctx.font = face.css(size);
  const measure = (s) => ctx.measureText(s).width * face.scaleX;
  const spans = [];
  const wrapped = [];
  for (const [key, name, value] of rows) {
    const first = wrapped.length;
    wrapped.push(...wrapText(measure, `${name}: ${value}`, WIDTH - 2 * MARGIN));
    spans.push({ key, name, first, count: wrapped.length - first });
  }
  const part = {
    field: 'spool_rows',
    kind: 'text',
    x: MARGIN,
    w: WIDTH - 2 * MARGIN,
    h: wrapped.length * step + 2 * PAD,
    step,
    size,
    ascent,
    descent,
    lines: wrapped.map((line) => line.text),
    checks: wrapped.map(() => false),
    widths: wrapped.map((line) => measure(line.text)),
    align,
    checklist: false,
    face,
  };
  part.anchors = spans.map(({ key, name, first, count }) => {
    const prefix = codePoints(`${name}:`).length;
    let seen = 0;
    let offset = 0;
    for (; offset < count; offset += 1) {
      seen += codePoints(wrapped[first + offset].text.replaceAll(' ', '')).length;
      if (seen > prefix) break;
    }
    offset = Math.min(offset, count - 1);
    const start = first + offset;
    let x = startX(part, part.widths[start]);
    if (offset === 0) x += measure(`${name}: `);
    x = Math.round(x); // Python `_half_up`, not `round()`: the same tie rule as Math.round
    return { field: key, x, first: start, count: count - offset, w: WIDTH - MARGIN - x };
  });
  return part;
}

// Band of a QR code: the symbol plus a 4-module quiet zone, scaled by whole pixels. The module
// count comes from `qrModules(text, ecc)` (the encoder lives in qr.js).
function qrPart(field, text, options, qrModules) {
  if (typeof text !== 'string' || text.trim() === '') throw new RenderError('blank_qr');
  if (typeof qrModules !== 'function') {
    throw new RenderError('bad_document', 'QR planning needs a module counter');
  }
  const ecc = options.qr_correction;
  if (!QR_LEVELS.has(ecc)) throw new RenderError('bad_document', `unknown QR level ${ecc}`);
  const modules = qrModules(text, ecc);
  const scale = Math.floor(options.qr_size / (modules + 8));
  if (scale < 3) throw new RenderError('qr_modules_too_small');
  const size = (modules + 8) * scale;
  return {
    field,
    kind: 'qr',
    x: Math.floor((WIDTH - size) / 2),
    w: size,
    h: size,
    step: 0,
    size: 0,
    ascent: 0,
    descent: 0,
    lines: [],
    modules,
    data: text,
    ecc,
  };
}

function documentParts(document, face, ctx, templates, qrModules) {
  if (document === null || typeof document !== 'object') {
    throw new RenderError('bad_document', 'document must be an object');
  }
  const { options } = document;
  if (options === null || typeof options !== 'object') {
    throw new RenderError('bad_document', 'document needs complete options');
  }
  const size = options.font_size;
  const { columns } = options; // options are complete, so there is no default to fall back to
  if (!COLUMN_COUNTS.has(columns)) {
    throw new RenderError('bad_document', 'columns must be 1, 2 or 3');
  }
  // `columns` flows only the main text; title, location, spool rows, QR and footer stay full width.
  const text = (field, value, fieldSize, align, checklist = false, flow = 1) =>
    textPart(field, value, fieldSize, align, checklist, face, ctx, flow);
  if (document.kind === 'qr') return [qrPart('qr', document.text, options, qrModules)];
  if (document.kind === 'text') {
    return [text('text', document.text, size, options.align, false, columns)];
  }
  if (document.kind !== 'template') {
    throw new RenderError('bad_document', `unsupported document kind ${document.kind}`);
  }
  const known = Object.hasOwn(templates, document.template);
  const spec = known ? templates[document.template] : undefined;
  const fields = document.fields ?? {};
  if (spec === undefined || typeof fields !== 'object' || Array.isArray(fields)) {
    throw new RenderError('bad_document', 'unknown template or invalid fields');
  }
  if (Object.keys(fields).some((key) => !spec.fields.includes(key))) {
    throw new RenderError('bad_document', 'unknown template field');
  }
  const parts = [];
  if (fields.title) {
    const role = document.template === 'heading' ? 'heading' : 'title';
    parts.push(text('title', fields.title, derivedSize(face.entry, role, size), 'center'));
  }
  if (fields.location) parts.push(text('location', fields.location, size, options.align));
  if (document.template === 'spool_label') {
    const rows = Object.entries(SPOOL_NAMES)
      .filter(([key]) => fields[key])
      .map(([key, name]) => [key, name, fields[key]]);
    if (rows.length > 0) parts.push(spoolPart(rows, size, options.align, face, ctx));
  }
  if (fields.text) {
    const checklist = document.template === 'checklist';
    parts.push(text('text', fields.text, size, options.align, checklist, columns));
  }
  if (fields.qr) parts.push(qrPart('qr', fields.qr, options, qrModules));
  if (fields.footer) {
    parts.push(text('footer', fields.footer, derivedSize(face.entry, 'footer', size), 'center'));
  }
  return parts;
}

function placeParts(parts, border) {
  if (parts.length === 0) throw new RenderError('bad_document', 'nothing to render');
  const rows = parts.reduce((sum, part) => sum + part.h, 0) + 2 * PAD;
  if (rows > MAX_ROWS) throw new RenderError('too_tall', 'label exceeds 2048 rows');
  let y = PAD;
  const blocks = parts.map((part) => {
    const block = { ...part, y, text_y: part.kind === 'qr' ? y : y + PAD };
    if (part.anchors) {
      block.anchors = part.anchors.map(({ field, x, first, count, w }) => ({
        field,
        x,
        text_y: block.text_y + first * block.step,
        w,
        h: count * block.step,
      }));
    }
    y += part.h;
    return block;
  });
  return { rows, border, blocks };
}

// Where a blank field's first line would be drawn, read off a plan that holds it as "x".
function slotOf(blocks, field) {
  for (const block of blocks) {
    if (block.field === field) {
      if (block.columns) {
        // The first line of the main text starts in the first column.
        const [first] = block.columns;
        const indent = block.checklist ? CHECK_INDENT : 0;
        return { field, x: first.x + indent, y: block.text_y, w: first.w - indent, h: block.step };
      }
      const h = block.kind === 'qr' ? block.h : block.step;
      return { field, x: block.x, y: block.text_y, w: block.w, h };
    }
    const anchor = block.anchors?.find((candidate) => candidate.field === field);
    if (anchor) return { field, x: anchor.x, y: anchor.text_y, w: anchor.w, h: anchor.h };
  }
  return null;
}

function slotsOf(document, face, ctx, templates, qrModules) {
  if (document.kind !== 'template') return [];
  const fields = document.fields ?? {};
  const slots = [];
  for (const field of templates[document.template].fields) {
    // A QR slot is planned only when the caller can size QR bands.
    if (fields[field] || (field === 'qr' && typeof qrModules !== 'function')) continue;
    const probe = { ...document, fields: { ...fields, [field]: 'x' } };
    try {
      const plan = placeParts(
        documentParts(probe, face, ctx, templates, qrModules),
        Boolean(document.options.border),
      );
      const slot = slotOf(plan.blocks, field);
      if (slot) slots.push(slot);
    } catch (error) {
      // Only a label too tall for the paper loses its slot; any other error is a real defect.
      if (!(error instanceof RenderError) || error.code !== 'too_tall') throw error;
    }
  }
  return slots;
}

/**
 * @param {object} document `{kind, template?, fields?, text?, options}`; `options` is complete.
 * @param {object} face a Face from `FaceRegistry` (carries its font table entry)
 * @param {CanvasRenderingContext2D | OffscreenCanvasRenderingContext2D} ctx measuring context
 * @param {object} templates the TEMPLATES table of printer_info
 * @param {(text: string, ecc: string) => number} [qrModules] symbol size without quiet zone;
 *   required for documents that hold a QR code; without it blank QR fields get no slot
 */
export function planLabel(document, face, ctx, templates, qrModules) {
  const plan = placeParts(
    documentParts(document, face, ctx, templates, qrModules),
    Boolean(document.options.border),
  );
  plan.slots = slotsOf(document, face, ctx, templates, qrModules);
  return plan;
}

// PIL `rectangle((x0, y0, x1, y1), outline=0, width=2)` as four inclusive-coordinate bars.
function frame(ctx, x0, y0, x1, y1) {
  const w = x1 - x0 + 1;
  const h = y1 - y0 + 1;
  ctx.fillRect(x0, y0, w, 2);
  ctx.fillRect(x0, y1 - 1, w, 2);
  ctx.fillRect(x0, y0, 2, h);
  ctx.fillRect(x1 - 1, y0, 2, h);
}

/** Draws text, checkboxes and the border; the QR layer is added by qr.js on top of the plan. */
export function drawPlan(ctx, plan) {
  ctx.save();
  ctx.fillStyle = '#000';
  ctx.textAlign = 'left';
  ctx.textBaseline = 'alphabetic';
  for (const block of plan.blocks) {
    if (block.kind !== 'text') continue;
    const { face } = block;
    ctx.font = face.css(block.size);
    const groups = block.columns
      ? block.columns.map((column) => ({
          ...column,
          box: column.x,
          startX: (width) => columnX(block, column, width),
        }))
      : [{ ...block, box: CHECKBOX.x0, startX: (width) => drawX(block, width) }];
    for (const group of groups) {
      group.lines.forEach((line, i) => {
        const y = block.text_y + i * block.step;
        if (group.checks[i]) {
          const x0 = group.box;
          frame(ctx, x0, y + CHECKBOX.top, x0 + CHECKBOX.x1 - CHECKBOX.x0, y + CHECKBOX.bottom);
        }
        if (line === '') return;
        const x = group.startX(group.widths[i]);
        if (face.scaleX === 1) {
          ctx.fillText(line, x, y + block.ascent);
        } else {
          ctx.save();
          ctx.scale(face.scaleX, 1);
          ctx.fillText(line, x / face.scaleX, y + block.ascent);
          ctx.restore();
        }
      });
    }
  }
  if (plan.border) frame(ctx, 2, 2, WIDTH - 3, plan.rows - 3);
  ctx.restore();
}
