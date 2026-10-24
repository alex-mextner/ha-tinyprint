// Paper strip: the 1-bit bitmap plus the inputs that edit it, in place (transparent overlays on the
// blocks) or docked below it. The strip image under an overlay is the crisp bitmap, so the overlay
// shows only the native caret and selection; its text is transparent and uses the renderer's own face.
import { BLANK } from '../renderer/blank.js';

/** Task 2 outcome (measured prefix widths and baseline, user-confirmed caret): 'overlay' | 'docked-all'. */
export const EDIT_POLICY = 'overlay';

const BASELINE_CACHE_LIMIT = 64; // font sizes and line heights seen: a handful in practice, never unbounded
const WIDTH = 384; // paper dots
const BAND_PAD = 8; // rows of paper margin above and below the lines of every text band
const LOOSE_ROW = { single: 44, multi: 96 }; // CSS px of an input shown without a strip to sit on
const LOOSE_GAP = 12;
// The order the renderer stacks the fields: DOM (Tab) order follows what the eye follows.
const VISUAL_ORDER = ['title', 'location', 'material', 'color', 'weight', 'nozzle', 'text', 'qr', 'footer'];
const CARET_COLOR = '#d6001c';

/** Integer scales only (1x, 2x): any other factor resamples the 1-bit bitmap and blurs it.
 * 2x when the box can hold the whole doubled strip, else 1x (a narrow card scrolls instead of blurring). */
export function autoZoom(availableWidth) {
  return availableWidth >= 2 * WIDTH ? 2 : 1;
}

/**
 * @param {string} field
 * @param {{ columns: number, policy: string, font: string, width: number }} ctx
 * @returns {'overlay' | 'docked'}
 */
export function fieldEditMode(field, { columns, policy, font, width }) {
  if (policy === 'docked-all' || field === 'qr') return 'docked';
  // A native textarea cannot flow into columns.
  if (field === 'text' && columns > 1) return 'docked';
  // Tiny5 is stretched by canvas scale (scaleX = width / 100): a 1x overlay would drift from the bitmap.
  if (font === 'tiny5' && width !== 100) return 'docked';
  return 'overlay';
}

const px = (value) => `${Math.round(value * 100) / 100}px`;

export class PaperStrip {
  #container;
  #chips;
  #empty;
  #emptyText;
  #stage;
  #box;
  #layer;
  #canvas;
  #docked;
  #entries = new Map(); // key -> { key, spec, mode, el, wrap, composing }
  #modeOf = () => 'docked';
  #onInput = () => {};
  #result = null;
  #zoom = 1;
  #area;
  #disabled = false;
  #baselines = new Map(); // 'font file/size/line height/pixel ratio' -> measured baseline offset

  constructor(container) {
    this.#container = container;
    this.#chips = document.createElement('div');
    this.#chips.id = 'chips';
    this.#chips.className = 'chips';
    this.#chips.hidden = true;
    this.#area = document.createElement('div');
    this.#area.className = 'canvas';
    this.#empty = document.createElement('div');
    this.#empty.id = 'empty';
    this.#empty.className = 'empty';
    const symbol = document.createElement('span');
    symbol.className = 'symbol';
    symbol.setAttribute('aria-hidden', 'true');
    symbol.textContent = '▤';
    this.#emptyText = document.createElement('span');
    this.#emptyText.id = 'empty-text';
    this.#empty.append(symbol, this.#emptyText);
    this.#stage = document.createElement('div');
    this.#stage.className = 'stage';
    this.#stage.hidden = true;
    this.#box = document.createElement('div');
    this.#box.className = 'box';
    this.#canvas = document.createElement('canvas');
    this.#canvas.id = 'raster';
    this.#canvas.width = WIDTH;
    this.#canvas.height = 1;
    this.#canvas.setAttribute('role', 'img');
    this.#canvas.setAttribute('aria-label', 'Готовый чёрно-белый растр для печати');
    this.#layer = document.createElement('div');
    this.#layer.className = 'overlays';
    this.#layer.style.cssText = 'position:absolute;left:0;top:0';
    this.#box.append(this.#canvas, this.#layer);
    this.#stage.append(this.#box);
    this.#area.append(this.#empty, this.#stage);
    this.#docked = document.createElement('div');
    this.#docked.id = 'docked';
    this.#container.append(this.#chips, this.#area, this.#docked);
  }

  /**
   * Draws the result and places the inputs on its blocks and slots. A result without pixels (nothing
   * to draw, or a failure) shows `emptyText` and keeps every input reachable in a plain stack.
   */
  show(result, emptyText) {
    this.#result = result;
    const shown = Boolean(result?.pixels);
    this.#empty.hidden = shown;
    this.#emptyText.textContent = emptyText;
    this.#canvas.hidden = !shown;
    if (shown) {
      if (this.#canvas.height !== result.rows) this.#canvas.height = result.rows;
      this.#canvas.getContext('2d').putImageData(result.pixels, 0, 0);
      this.#canvas.classList.toggle('ghost', Boolean(result.ghost));
    } else {
      this.#canvas.classList.remove('ghost');
    }
    this.#place();
  }

  get zoom() {
    return this.#zoom;
  }

  setZoom(zoom) {
    this.#zoom = zoom;
    this.#place();
  }

  /** CSS px the strip may occupy: the content box of the scroll area (its padding is not paper room). */
  availableWidth() {
    const style = getComputedStyle(this.#area);
    return this.#area.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
  }

  /**
   * @param {{ key: string, caption: string, multiline: boolean, value: string, placeholder: string,
   *   maxLength: number, required: boolean }[]} fields
   * @param {(key: string) => 'overlay' | 'docked'} mode
   * @param {(key: string, value: string) => void} onInput
   */
  setFields(fields, mode, onInput) {
    this.#modeOf = mode;
    this.#onInput = onInput;
    const rank = (key) => (VISUAL_ORDER.includes(key) ? VISUAL_ORDER.indexOf(key) : VISUAL_ORDER.length);
    const ordered = [...fields].sort((a, b) => rank(a.key) - rank(b.key));
    const keep = new Set(ordered.map((spec) => spec.key));
    for (const [key, entry] of this.#entries) {
      if (keep.has(key)) continue;
      entry.wrap?.remove();
      entry.el.remove();
      this.#entries.delete(key);
    }
    const next = new Map();
    for (const spec of ordered) {
      let entry = this.#entries.get(spec.key);
      const wanted = this.#modeOf(spec.key);
      if (entry && entry.mode !== wanted) {
        entry.wrap?.remove();
        entry.el.remove();
        entry = undefined;
      }
      entry ??= this.#create(spec, wanted);
      entry.spec = spec;
      this.#sync(entry);
      next.set(spec.key, entry);
    }
    this.#entries = next;
    this.#arrange();
    this.#updateChips();
    this.#place();
  }

  focusField(key) {
    const entry = this.#entries.get(key);
    if (!entry) return;
    entry.el.focus();
    const end = entry.el.value.length;
    entry.el.setSelectionRange(end, end);
  }

  setDisabled(disabled) {
    this.#disabled = disabled;
    for (const { el } of this.#entries.values()) el.disabled = disabled;
    this.#updateChips();
  }

  dispose() {
    this.#entries.clear();
    this.#container.replaceChildren();
    this.#canvas.width = 0;
  }

  #create(spec, mode) {
    const overlay = mode === 'overlay';
    const el = document.createElement(overlay || spec.multiline ? 'textarea' : 'input');
    el.id = `field-${spec.key}`;
    el.dataset.field = spec.key;
    el.className = overlay ? 'overlay' : 'docked-input';
    el.disabled = this.#disabled;
    if (overlay) {
      el.spellcheck = false; // a squiggle would be drawn under the transparent text, over the bitmap
      el.autocomplete = 'off';
    }
    const entry = { key: spec.key, spec, mode, el, wrap: null, composing: false };
    if (!overlay) {
      entry.wrap = document.createElement('div');
      entry.wrap.className = 'field';
      entry.label = document.createElement('label');
      entry.label.htmlFor = el.id;
      entry.wrap.append(entry.label, el);
    }
    el.addEventListener('compositionstart', () => {
      entry.composing = true;
    });
    // A composition is only handed over once it ends: a re-render in between must not touch it.
    el.addEventListener('compositionend', () => {
      entry.composing = false;
      this.#commit(entry);
    });
    el.addEventListener('beforeinput', (event) => {
      if (!entry.spec.multiline && (event.inputType === 'insertLineBreak' || event.inputType === 'insertParagraph')) {
        event.preventDefault();
      }
    });
    el.addEventListener('input', (event) => {
      if (!event.isComposing && !entry.composing) this.#commit(entry);
    });
    return entry;
  }

  /** Brings an existing element up to date with its field spec; the value is written only when it differs. */
  #sync(entry) {
    const { el, spec } = entry;
    el.maxLength = spec.maxLength;
    el.required = spec.required;
    // The example is a placeholder only; overlays make it transparent (the ghost is drawn on the strip).
    el.placeholder = spec.placeholder;
    if (entry.mode === 'overlay') el.setAttribute('aria-label', spec.caption);
    else entry.label.textContent = spec.caption;
    if (!entry.composing && el.value !== spec.value) el.value = spec.value;
  }

  #commit(entry) {
    const { el } = entry;
    if (!entry.spec.multiline && /[\r\n]/.test(el.value)) {
      // A pasted line break: one line stays one line, with the caret where it was.
      const { selectionStart, selectionEnd } = el;
      el.value = el.value.replace(/[\r\n]/g, ' ');
      el.setSelectionRange(selectionStart, selectionEnd);
    }
    this.#updateChips();
    this.#onInput(entry.key, el.value);
  }

  /** DOM order = visual order, so Tab walks the strip top to bottom; moved only when it is wrong. */
  #arrange() {
    const overlays = [];
    const docked = [];
    for (const entry of this.#entries.values()) (entry.mode === 'overlay' ? overlays : docked).push(entry);
    const place = (parent, nodes) => {
      const stale = nodes.some((node, i) => parent.children[i] !== node);
      if (stale) for (const node of nodes) parent.append(node);
    };
    place(this.#layer, overlays.map((entry) => entry.el));
    place(this.#docked, docked.map((entry) => entry.wrap));
  }

  #updateChips() {
    const wanted = [...this.#entries.values()].filter((entry) => entry.mode === 'overlay' && BLANK.test(entry.el.value));
    const shown = [...this.#chips.children];
    const same = wanted.length === shown.length && wanted.every((entry, i) => shown[i].dataset.field === entry.key && shown[i].textContent === `+ ${entry.spec.caption}`);
    if (!same) {
      this.#chips.replaceChildren(
        ...wanted.map((entry) => {
          const chip = document.createElement('button');
          chip.type = 'button';
          chip.className = 'chip';
          chip.dataset.field = entry.key;
          chip.textContent = `+ ${entry.spec.caption}`;
          chip.addEventListener('click', () => this.focusField(entry.key));
          return chip;
        }),
      );
    }
    for (const chip of this.#chips.children) chip.disabled = this.#disabled;
    this.#chips.hidden = wanted.length === 0;
  }

  /** Where an overlay sits on the strip, in paper dots, or null when this render has no place for it. */
  #geometry(key) {
    const { blocks = [], slots = [], face } = this.#result;
    const block = blocks.find((candidate) => candidate.field === key && candidate.kind === 'text' && !candidate.columns);
    if (block) {
      return {
        kind: 'block',
        x: block.x,
        y: block.text_y,
        w: block.w,
        h: block.h - 2 * BAND_PAD,
        step: block.step,
        size: block.size,
        align: block.align,
        ascent: block.ascent,
        descent: block.descent,
        face: block.face ?? face,
      };
    }
    for (const owner of blocks) {
      const anchor = owner.anchors?.find((candidate) => candidate.field === key);
      if (anchor) {
        // The caption ("Материал: ") is on the strip; the input starts where the value starts.
        return {
          kind: 'anchor',
          x: anchor.x,
          y: anchor.text_y,
          w: anchor.w,
          h: anchor.h,
          step: owner.step,
          size: owner.size,
          align: 'left',
          ascent: owner.ascent,
          descent: owner.descent,
          face: owner.face ?? face,
        };
      }
    }
    const slot = slots.find((candidate) => candidate.field === key);
    // A slot carries the type of the block that will replace it (layout.js `slotOf`); QR has none.
    if (!slot || !face || slot.size === undefined) return null;
    return { kind: 'slot', ...slot, face };
  }

  /**
   * Distance from the top of a line box to its baseline, as this browser lays it out (Chrome rounds it
   * down to a whole pixel, Firefox keeps halves), measured on a hidden probe with the overlay's font
   * and line height. Falls back to centring the glyph row when the probe has no layout.
   */
  #baselineOffset({ face, size, step, ascent, descent }) {
    // Width and weight axes do not move the baseline; browser zoom and screen density change its rounding.
    const key = `${face.entry.file}/${size}/${step}/${devicePixelRatio}`;
    const known = this.#baselines.get(key);
    if (known !== undefined) return known;
    const probe = document.createElement('div');
    probe.style.cssText = `position:fixed;left:0;top:0;visibility:hidden;pointer-events:none;white-space:pre;text-rendering:geometricPrecision;font:${face.css(size)};line-height:${step}px`;
    const mark = document.createElement('span');
    mark.style.cssText = 'display:inline-block;width:0;height:0';
    probe.append('H', mark);
    this.#area.append(probe);
    const box = probe.getBoundingClientRect();
    const offset = mark.getBoundingClientRect().bottom - box.top;
    probe.remove();
    if (box.height === 0) return (step + ascent - descent) / 2; // no layout right now (hidden panel): not cached
    if (this.#baselines.size >= BASELINE_CACHE_LIMIT) this.#baselines.delete(this.#baselines.keys().next().value); // first inserted
    this.#baselines.set(key, offset);
    return offset;
  }

  #place() {
    const loose = !this.#result?.pixels;
    const zoom = this.#zoom;
    let stack = 0;
    for (const entry of this.#entries.values()) {
      if (entry.mode !== 'overlay') continue;
      const { el } = entry;
      const geometry = loose ? null : this.#geometry(entry.key);
      el.classList.toggle('loose', loose);
      el.classList.toggle('slot', geometry?.kind === 'slot');
      if (loose) {
        const height = entry.spec.multiline ? LOOSE_ROW.multi : LOOSE_ROW.single;
        el.style.cssText = `position:absolute;left:0;top:${px(stack)};width:${WIDTH}px;height:${px(height)};margin:0;resize:none`;
        stack += height + LOOSE_GAP;
        continue;
      }
      if (!geometry) {
        el.style.cssText = 'display:none';
        continue;
      }
      // The bitmap puts the baseline `ascent` below the row top; the browser puts the text baseline
      // `baselineOffset` below the top of the line box. Moving the box by the difference makes them coincide.
      const shift = geometry.ascent - this.#baselineOffset(geometry);
      el.rows = Math.max(1, Math.round(geometry.h / geometry.step));
      el.style.cssText = [
        'position:absolute',
        `left:${px(geometry.x)}`,
        `top:${px(geometry.y + shift)}`,
        `width:${px(geometry.w)}`,
        `height:${px(geometry.h)}`,
        'min-height:0',
        'margin:0',
        'padding:0',
        'border:0',
        'border-radius:0',
        'background:transparent',
        'color:transparent',
        `caret-color:${CARET_COLOR}`,
        `font:${geometry.face.css(geometry.size)}`,
        `line-height:${px(geometry.step)}`,
        'direction:ltr',
        `text-align:${geometry.align}`,
        'text-rendering:geometricPrecision',
        'white-space:pre-wrap',
        'overflow-wrap:break-word',
        'overflow:hidden',
        'resize:none',
        'box-sizing:border-box',
      ].join(';');
    }
    const rows = loose ? Math.max(0, stack - LOOSE_GAP) : this.#result.rows;
    this.#stage.hidden = rows === 0;
    this.#stage.style.cssText = `position:relative;flex:none;width:${WIDTH * zoom}px;height:${rows * zoom}px`;
    this.#box.style.cssText = `position:relative;width:${WIDTH}px;height:${rows}px;transform:scale(${zoom});transform-origin:0 0`;
  }
}
