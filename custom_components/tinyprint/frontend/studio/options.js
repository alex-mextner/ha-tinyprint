// Pure option rules: grid snapping, font coercion, preset application, font hints.
export { snapToGrid, APPROX_WIDTH_STEP, APPROX_WEIGHT_STEP } from '../renderer/index.js';

export const ENHANCEMENT_KEYS = ['method', 'contrast', 'brightness', 'sharpness'];

/** Spec "Fonts": non-blocking hint when stroke counters are likely to close (Roboto Flex only). */
export function closingCounterHint(font, size, width, weight) {
  if (font !== 'roboto_flex') return null;
  const closes =
    (weight > 700 && size <= 24) || (weight === 1000 && size < 72 && width < 100) || (width < 50 && weight > 800);
  return closes ? 'Тонкие штрихи могут слипнуться: увеличьте размер или уменьшите жирность' : null;
}

function nearest(values, wanted) {
  let best = values[0];
  for (const value of values) {
    const gap = Math.abs(value - wanted);
    const bestGap = Math.abs(best - wanted);
    if (gap < bestGap || (gap === bestGap && value < best)) best = value;
  }
  return best;
}

const allows = (spec, value) => (spec.values ? spec.values.includes(value) : value >= spec.min && value <= spec.max);

/**
 * Options for `fontName`: the nearest allowed size (a tie takes the smaller, unlike Python's
 * `derived_size`, which rounds half up). Width and weight go to the font's defaults when the font
 * changes, otherwise they are kept while still valid. Everything else (columns, ...) is kept.
 */
export function coerceForFont(options, fonts, fontName) {
  const entry = fonts[fontName];
  const switching = options.font !== fontName;
  const { sizes, width, weight } = entry;
  const size = sizes.values
    ? nearest(sizes.values, options.font_size)
    : Math.min(sizes.max, Math.max(sizes.min, options.font_size));
  const keepWidth = !switching && allows(width, options.font_width);
  const weightValue = weight.kind === 'fixed' ? weight.value : weight.default;
  const keepWeight = !switching && (weight.kind === 'fixed' ? options.font_weight === weight.value : allows(weight, options.font_weight));
  return {
    ...options,
    font: fontName,
    font_size: size,
    font_width: keepWidth ? options.font_width : width.default,
    font_weight: keepWeight ? options.font_weight : weightValue,
  };
}

/** Options the Pillow renderer cannot draw: a recipe using them is browser-only. */
export function usesBrowserOnlyOptions(options) {
  return options.columns > 1 || (options.font === 'tiny5' && options.font_width === 200);
}

/**
 * Copies the preset's enhancement values into the options. The renderer validates `preset` but
 * never applies it, so the document the panel sends or records must carry resolved values.
 */
export function applyPreset(options, presets, name) {
  if (!Object.hasOwn(presets, name)) throw new Error(`unknown preset ${name}`);
  const values = {};
  for (const key of ENHANCEMENT_KEYS) values[key] = presets[name][key];
  return { ...options, preset: name, ...values };
}
