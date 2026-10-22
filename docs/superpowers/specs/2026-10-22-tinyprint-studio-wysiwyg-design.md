# TinyPrint Studio: WYSIWYG editor, columns, variable fonts, print history

Status: design approved in conversation 2026-10-22; written spec awaiting review.
Companion spec (implemented after this one): `2026-10-22-tinyprint-transport-reliability-design.md`.

## Problem

Studio today (0.2.2) is a form on the left and a server-rendered preview on the right:

1. The preview exists only after a click on **Preview** (`frontend/tinyprint-panel.js:95,113,259`).
   Every edit invalidates it and disables Print (`:246`).
2. Template examples are pre-filled into the inputs as real values (`showFields`, `:178-183`), so they
   are previewed and printed, and they satisfy `required`.
3. Template, fields and preview are three separate things. What is on screen is not the paper.
4. Text is always one full-width column. There is no font choice, no weight or width control, and no
   bundled font (the user must install a TTF; `docs/studio.md:21`).
5. Nothing is remembered. A printed label cannot be found or reprinted.

## Decisions (confirmed by the user)

| Topic | Decision |
| --- | --- |
| Renderer | The browser renders and packs the 1-bit bitmap; the server only validates and prints it. |
| Editing surface | One paper strip: the real bitmap with the fields placed on it. No Preview button. |
| Examples | Placeholders only. Ignored entirely once any field has user input. |
| Columns | 1, 2 or 3 newspaper columns for the main text. |
| Fonts | Bundle Roboto Flex (variable width and weight) and Tiny5 (LED / dot-matrix look). |
| History | Every print job from every entry point is recorded and can be reopened or reprinted. |
| Order | This spec first, transport reliability second. |

## Feasibility evidence (spike, 2026-10-22, throwaway code, not in the repo)

A canvas renderer **can** be exact when the browser packs the bitmap and the preview is rebuilt from the
packed bytes. Measured in Chromium 156, Firefox 157 and WebKit 27.2 (Linux WPE port; macOS/iOS Safari
and mobile were **not** tested):

- Round trip (render, pack, unpack, compare) is bit-identical in all three engines.
- Same engine, same input: identical SHA-256 across repeats, reload, devicePixelRatio 1/1.5/2, CSS zoom.
- Different engines differ in bits (Chromium vs Firefox: 9.1% of ink bits; Firefox vs WebKit: 0.1%).
- Browser vs Pillow differs in 16-42% of ink-union pixels (sub-pixel stroke weight, 0-1 px drift) with
  equal legibility. Line breaks are identical to Pillow with libraqm in 16 of 16 test cases.
- 384x645 label with 3 columns and a QR: 4-6 ms. Worst case 384x2048: 98,304 bytes packed.

Consequences designed in below: the browser bitmap is the source of truth; history stores the bitmap;
Python automations stay on Pillow and are not pixel-identical to Studio.

## Architecture

```
panel (JS renderer) --print_bitmap{device_id, rows, bitmap_b64, recipe}--> HA service
        |                                                                      |
        | live preview = unpacked packed bytes                                 | validate, one printer lock,
        v                                                                      | record history, transport
   paper strip                                                                 v
                                                                         printer (unchanged transport)
```

- `print_bitmap` (admin-only service, new): `device_id`, `rows` (1..2048, `bool` rejected), `bitmap_b64`
  (strict base64 decoding to exactly `rows * 48` bytes; encoded length bounded before decoding),
  optional `recipe` (JSON object, at most 64 KiB, stored for history, never interpreted by the server).
  Extract the rows, base64 and size checks from `bridge_wire.parse_print_request` (`:97-123`) into one
  shared function used by both the bridge and `print_bitmap`; do not copy them. Validation precedes any radio activity.
  Returns the same result shape as `print_document` plus `bitmap_sha256` computed by the server.
- The Studio panel stops calling `preview` and `print_document`. It still calls `printer_info` for the
  printer list and the template catalog (field names, required flags, captions, placeholders).
- `preview`, `print_document`, `print_template`, `print_text`, `print_qr`, `print_image` remain for
  automations and keep their Pillow renderer and `expected_sha256` contract unchanged.
- No auto print: nothing prints on load, edit, upload or history open. Print and Reprint are explicit clicks.

## Browser renderer

Contract (all measured in the spike):

1. Fonts are served from the integration's frontend static path as pre-instanced files with `opsz` pinned
   (unpinned `opsz` is the one axis browsers disagree on). One `FontFace` per (wdth, wght) pair with
   single-value descriptors, awaited before drawing; a missing face silently falls back, so the renderer
   must assert `document.fonts.check` and fail loudly.
2. Canvas: `OffscreenCanvas(384, H)`, 2D context with default `alpha: true` (`alpha: false` yields LCD
   colour fringes), `textRendering = 'geometricPrecision'` (Chromium `auto` rounds advances), black fill on
   transparent, integer x and baseline. `ctx.font` uses numeric weight and a quoted family; percent
   stretch is rejected by the font shorthand.
3. Pack: a pixel is black iff alpha >= 96 (the server's `>= 160` white threshold on an opaque-white
   background), MSB first, 1 = ink, 48 bytes per row. The preview image is built by unpacking those bytes.
4. Layout parity with `studio_render.py` (`_label`, `_text_block`, `_stack`): same block order, insets
   (10; 34 for checklist), sizes (title +6, heading +14, footer -4), border, 8-row top margin, 2048-row
   cap. Differences are bugs, checked by the golden test below.
5. QR: vendored dependency-free JS encoder (Nayuki QR Code generator, MIT, single file) with the same
   integer module scaling, quiet zone of 4 modules and minimum 3 dots per module as `_qr`. Module-for-module
   equality with Python `qrcode` holds when version and mask are forced (12 of 14 test strings); identical
   output is not required, readability is.
6. Images: threshold, Floyd-Steinberg and 4x4 Bayer ordered dither and the four presets are ported to JS
   operating on canvas pixels. Downscaling uses canvas smoothing, which differs slightly from Pillow
   LANCZOS. Pixel and size limits (4 million pixels, 2 MiB upload) are unchanged.

## Fonts

| Font | Axes exposed | Notes |
| --- | --- | --- |
| Roboto Flex | width 25-151 (default 100), weight 100-1000 (default 400) | `opsz` pinned at 14, parametric axes pinned. Subset to Latin, Latin-Ext, Cyrillic U+0400-052F, common punctuation, No, rouble, euro. OFL 1.1; ship `OFL.txt` beside it. |
| Tiny5 | width 100 or 200 | Weight fixed 400. Pixel-perfect only at weight 400, width 100/200 and sizes that are multiples of 8 (spike: 0% intermediate alpha). The UI restricts Tiny5 to those values. |

- Sliders expose the full Roboto Flex ranges. No hard clamps: the WYSIWYG strip is the legibility check.
  A non-blocking hint appears when stroke counters are likely to close (weight > 700 at <= 24 px, weight
  1000 below 72 px with width < 100, width < 50 with weight > 800).
- `font_size` range grows from 12-48 to 12-96 for Roboto Flex; Tiny5 uses 8, 16, 24, 32, 40, 48.
- A reproducible `tools/build_fonts.py` produces the shipped files from pinned upstream sources and prints
  their SHA-256 (Roboto Flex upstream: google/fonts commit `45e3d5269e4a17d823f6d84470bc084dc5ae46f6`,
  file SHA-256 `9b523f7d82593df0107173849ebb8c817471a1df4b4fb2c3cbf40cfd810c8281`). Fonts are committed
  as binaries with their licenses; CI verifies the hashes.
- Python renderer: bundled Roboto Flex becomes the default font when none is configured, and gains
  `font`, `font_width`, `font_weight` options applied with `set_variation_by_axes` in fvar order
  (pass the full list; out-of-range values are silently clamped by Pillow, so validate first;
  re-apply axes after `font_variant`; one font object per render, never shared across threads).
  `font_path` keeps working as an override.

## Editor surface and placeholders

- The paper strip is the editor: a 384-dot wide bitmap shown at 1x or 2x with `image-rendering: pixelated`.
  The renderer returns block geometry (`field`, `x`, `y`, `w`, `h`) alongside the bitmap, and an input
  overlay sits on each block.
- Overlay inputs have transparent text and a native caret, and use the same font file, size, axes and
  wrap width through CSS, so the caret tracks the bitmap text. Wrap mismatches can only misplace the caret,
  never change output. The bitmap is always what is printed.
- Template fields live inside the strip. Option controls (font, size, width, weight, align, border,
  columns, QR options) are a compact bar above it; hidden image-only options are not sent.
- **Multi-column mode:** a native textarea cannot flow into columns. The main text field is therefore
  edited in a docked, visible-text textarea directly under the strip while the strip updates live;
  title, footer and other fields keep in-place overlays. This is the least certain part of the design:
  the first implementation task is a prototype that confirms caret alignment in Chromium and Firefox.
- Live preview: every input re-renders on `input`, coalesced to one render per animation frame
  (latest wins). There is no server call, so the server's `Semaphore(2)` "Preview is busy" case cannot occur.
  The `Preview` button, the hint text, and the "refresh preview" notices are removed.
- Print is enabled iff the current bitmap has ink, required template fields are non-blank, a device
  is selected and no render is pending.

Placeholder rule (the user's wording: examples live in placeholders and are ignored as soon as one field is
filled):

1. Drafts start empty. Each field's `placeholder` is its catalog example text (greyed); no code path
   treats an example as a field value.
2. If every field is blank: the strip shows the example rendered as a dimmed ghost (never packed, never
   sent), and Print is disabled.
3. If any field has non-blank user input: ghosts and placeholders disappear from the strip; blank fields
   render nothing. Required-field validation still applies and names the missing field.
4. Switching templates keeps each template's own draft (as today); a template with no input shows its ghost.

## Columns

- Option `columns` in {1, 2, 3}, default 1. Gutter 8 dots. Usable width is 364 (384 minus insets 10 + 10),
  so column width is `floor((364 - 8*(n-1)) / n)`: 178 for two, 116 for three. Checklist inset (34) applies
  inside each column.
- Applies to the main text of every template and to `kind: text`. Title, footer, QR and spool rows stay full
  width.
- Flow: greedy wrap at column width, then split into n columns minimising the tallest column, splitting at
  line boundaries; checklist items are atomic (never split across columns). Wrapping uses `measureText`
  with the same binary search as `_text_block`. Ink overhangs `measureText` width by up to 1.1 px, so the
  gutter is measured from advance width.
- Python renderer does not get columns in this change (non-goal).

## Print history

- Store: `homeassistant.helpers.storage.Store`, key `tinyprint.history`, version 1, saved with
  `async_delay_save`. A ring buffer capped at 200 entries and 5 MiB of serialised data; the oldest entry
  goes first.
- Recording point: the common job layer (`StudioPrinter._async_print_job` and its legacy-action
  equivalents; the plan must confirm every entry point funnels through one call site) after the bitmap
  exists and when the outcome is known. Recorded sources: `studio`, `service` (all actions),
  `button` (test print), `history` (reprint).
- Entry: `id`, `created` (UTC ISO-8601), `device_id`, `source`, `status` (`sent`, `failed`,
  `delivery_uncertain`, `cancelled`), `phase`, `rows`, `ink_percent`, `bitmap_sha256`, `bitmap_z`
  (zlib + base64 of the packed bitmap), `recipe` (document JSON for Studio and template/text/QR actions;
  `null` for images, whose originals are never stored), `title` (derived, at most 80 chars, for the list),
  `job_id`, `timings` (object, filled by the transport spec; may be `null`).
- Failed and `delivery_uncertain` jobs are recorded too. A reprint of a `delivery_uncertain` entry warns
  that the first copy may have printed.
- Admin-only services: `history_list` (`SupportsResponse.ONLY`; returns entries without `bitmap_z`, with a
  small thumbnail data URL), `history_get` (one entry with `recipe` and `bitmap_z`), `history_delete`,
  `history_clear`, and `history_reprint` (`device_id`, `id`; prints the stored bitmap, never re-renders).
- Panel: a history drawer. Each row: thumbnail, title, time, status badge. Actions: **Open in editor**
  (loads recipe, re-renders in the current browser; may differ in bits from the stored bitmap because
  engines differ; the drawer shows the stored bitmap for comparison) and **Reprint** (stored bitmap, exact).
- Privacy: content now persists in `/config/.storage/tinyprint.history` and HA backups. Logs and
  diagnostics stay payload-free (existing test at `tests/test_ha_studio.py` keeps passing). Documentation
  says so plainly, with the clear action.

## Documentation, tests, versioning

- Rewrite `docs/studio.md` (preview, templates and placeholders, options table, history, privacy
  section that currently promises no persistence at `:168-173`); update the panel header comment
  (`tinyprint-panel.js:1`), `preview_result` docstring (`studio_render.py:468`) and the "file is not stored on
  the server" hints (`tinyprint-panel.js:85,225`).
- The catalog's `example` dicts become placeholder text only. `tests/test_studio_render.py:37-41` keeps
  rendering each template from them as fixture data.
- Rewrite `tests/browser_studio.py` (no `#preview`; live preview, ghost rule, columns, history, print-only-on-
  click, hidden image options not sent, injection safety). Run it on Chromium and Firefox.
- New pytest: `print_bitmap` validation matrix (size, base64, rows, bool, oversize), history store (ring
  cap, byte cap, failed jobs recorded, reprint prints the stored bytes), Pillow font axes in fvar order and
  validation, bundled font hash check.
- Golden parity test: for each template and for columns 1-3, compare Python line breaks and block
  geometry against the browser (offset tolerance 1 px). Parity gaps fail the build.
- Bump `PANEL_VERSION` (`studio.py:19`), `manifest.json` and the hardcoded version in
  `tests/browser_studio.py:47` together (currently 0.2.2; this change is 0.3.0).

## Non-goals

- Python columns and Python/JS pixel parity.
- A custom JS rasteriser on font outlines (would be bit-exact across engines; not needed because the browser
  bitmap is what prints).
- Verifying iOS/macOS Safari, Windows, or the HA Companion app rendering. Treated as unverified until
  tested; the spike covered Linux engines only.
- Cloud sync, export or import of history.

## Risks

| Risk | Mitigation |
| --- | --- |
| Caret drift in in-place overlays | Prototype first; docked textarea fallback already specified. |
| Cross-engine bit differences | History stores the bitmap; reprint uses it; nothing compares bitmaps across engines. |
| JS and Python layouts diverge | Golden parity test in CI. |
| `.storage` growth | 200 entries and 5 MiB caps, enforced on write. |
| Slow mobile devices | Latest-wins coalescing per frame; measure on a phone during acceptance. |

## Acceptance

1. Editing any field updates the strip within one animation frame on a desktop browser, with no network
   call and no Preview button anywhere.
2. With all fields blank the strip shows ghost examples and Print is disabled; typing one character in any
   field removes every example and the printed bitmap contains none.
3. The bytes sent in `print_bitmap` equal the bytes whose unpacked image is displayed (asserted in the
   browser test by hashing both).
4. Columns 2 and 3 produce no line wider than its column; the golden parity test passes.
5. Roboto Flex width and weight sliders visibly change the strip across their full ranges; Tiny5 renders
   with uniform pixel blocks at sizes 8, 16, 24.
6. Every job (Studio, each automation action, test button, reprint) appears in history with the right
   status; a reprint sends exactly the stored bytes.
7. A physical print of one Cyrillic label in each font and a 2-column label is photographed. Software
   success is never reported as paper output.
