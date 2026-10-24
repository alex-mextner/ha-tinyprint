// Pure editor state rules: input detection, ghost documents, Print blockers and result gating.
import { BLANK } from '../renderer/blank.js';

export const BLOCKERS = [
  'no_device',
  'no_input',
  'required_blank',
  'text_too_long',
  'render_pending',
  'render_failed',
  'render_approximate',
  'no_ink',
  'job_in_flight',
];

/** User-facing text per blocker (tooltip and the line under Print); the caller appends the field captions to `required_blank`. */
export const BLOCKER_TEXT = Object.freeze({
  no_device: 'Выберите принтер',
  no_input: 'Введите текст, QR или загрузите изображение',
  required_blank: 'Заполните обязательные поля:',
  text_too_long: 'Текст слишком длинный',
  render_pending: 'Идёт отрисовка',
  render_failed: 'Не удалось отрисовать этикетку',
  render_approximate: 'Предпросмотр приблизительный: дождитесь точной отрисовки',
  no_ink: 'На этикетке нечего печатать',
  job_in_flight: 'Принтер занят предыдущим заданием',
});

// Same limits as studio_render `_text` / `normalize_document`.
export const MAX_FIELD_CHARS = 3000;
export const MAX_QR_CHARS = 500;
export const MAX_TOTAL_CHARS = 3000;

// Code points as Python `len`, counted without materializing a huge paste: a code point is at most
// two UTF-16 units, so beyond twice the limit the answer is already "too long".
function codePoints(value, limit) {
  if (value.length > 2 * limit) return limit + 1;
  let count = 0;
  for (const _ of value) count += 1;
  return count;
}

/** Non-string or whitespace-only, with Python's `str.strip()` notion of whitespace. */
export function isBlank(value) {
  return typeof value !== 'string' || BLANK.test(value);
}

export function hasUserInput(s) {
  if (s.mode === 'qr') return !isBlank(s.qrText);
  if (s.mode === 'image') return Boolean(s.hasImage);
  return Object.values(s.fields).some((value) => !isBlank(value));
}

export function textTooLong(s, templates) {
  if (s.mode === 'qr') return typeof s.qrText === 'string' && codePoints(s.qrText, MAX_QR_CHARS) > MAX_QR_CHARS;
  if (s.mode !== 'template') return false;
  let combined = 0;
  for (const key of templates[s.template].fields) {
    const value = s.fields[key];
    if (typeof value !== 'string') continue;
    const length = codePoints(value, key === 'qr' ? MAX_QR_CHARS : MAX_FIELD_CHARS);
    if (key === 'qr') {
      if (length > MAX_QR_CHARS) return true;
    } else {
      if (length > MAX_FIELD_CHARS) return true;
      combined += length;
    }
  }
  return combined > MAX_TOTAL_CHARS;
}

export function missingRequired(s, templates) {
  if (s.mode !== 'template') return [];
  return templates[s.template].required.filter((key) => isBlank(s.fields[key]));
}

/**
 * What the strip renders. No input: the template's example as a ghost (placeholders only, never
 * printable). Any input: only the non-blank fields, so every example and blank field disappears.
 */
export function effectiveDocument(s, templates, options) {
  if (s.mode === 'qr') {
    if (!hasUserInput(s)) return { document: null, ghost: false };
    return { document: { kind: 'qr', text: s.qrText, options: { ...options } }, ghost: false };
  }
  if (s.mode === 'image') {
    if (!hasUserInput(s)) return { document: null, ghost: false };
    return { document: { kind: 'image', options: { ...options } }, ghost: false };
  }
  const ghost = !hasUserInput(s);
  const source = ghost ? templates[s.template].example : s.fields;
  const fields = {};
  for (const [key, value] of Object.entries(source)) {
    if (!isBlank(value)) fields[key] = value;
  }
  return { document: { kind: 'template', template: s.template, fields, options: { ...options } }, ghost };
}

/** The ONE Print-enable rule: every blocking code, in BLOCKERS order; empty means Print is allowed. */
export function printBlockers(s) {
  const active = {
    no_device: !s.deviceId,
    no_input: !s.hasInput,
    required_blank: s.missing.length > 0,
    text_too_long: s.textTooLong,
    render_pending: s.renderPending,
    render_failed: !s.renderOk,
    render_approximate: !s.renderExact,
    no_ink: !s.hasInk,
    job_in_flight: s.inFlight,
  };
  return BLOCKERS.filter((code) => active[code]);
}

/** Per-template drafts: each template keeps its own field values while the user switches. */
export const emptyDrafts = () => ({});

export const draftFor = (drafts, template) => ({ ...drafts[template] });

export function withDraftField(drafts, template, key, value) {
  return { ...drafts, [template]: { ...drafts[template], [key]: value } };
}

/**
 * The result a print or recipe may be built from, or null: exact (so never a ghost or an
 * approximate drawing, which carry no document), of the newest generation, and nothing in flight.
 */
export function printableResult(result, { generation, pending }) {
  if (!result || pending || result.generation !== generation) return null;
  if (result.exact !== true || result.ghost || !result.document) return null;
  return result;
}

/**
 * A result safe to keep: the renderer reuses its pixel buffer for the next same-height render, so
 * the kept result holds a copy of the packed bytes (what Print sends) and no pixels at all; the strip
 * is drawn from the incoming result before this is called.
 */
export function keepResult(result) {
  return { ...result, bitmap: result.bitmap ? result.bitmap.slice() : result.bitmap, pixels: null };
}

export function bitmapHasInk(bitmap) {
  return Boolean(bitmap) && bitmap.some((byte) => byte !== 0);
}
