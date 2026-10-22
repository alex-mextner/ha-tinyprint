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

- `print_bitmap` (new, registered with `async_register_admin_service` like the existing studio services in
  `studio.py`): `device_id`, `rows` (1..2048, `bool` rejected), `bitmap_b64` (strict base64 decoding to
  exactly `rows * 48` bytes; encoded length bounded before decoding), optional `recipe` (JSON object, at
  most 64 KiB, stored for history, never interpreted by the server), optional `title` (text, at most
  80 chars, supplied by the panel) and optional `source` (`studio` or `service`, default `service`; the
  panel sends `studio`; the server derives titles itself only for the actions it renders).
  Extract the rows, base64 and size checks from `bridge_wire.parse_print_request` (`:97-123`) into a
  neutral module (`bitmap.py`) imported by both `bridge_wire` and `print_bitmap`; do not copy them.
  Validation precedes any radio activity.
  Returns the same result shape as `print_document` plus `bitmap_sha256` computed by the server.
- The legacy actions `print_text`, `print_qr` and `print_image` are registered with plain
  `hass.services.async_register` (`__init__.py:76-82`), so non-admin HA users can call them and thereby
  create history entries. History content is therefore untrusted input (see Print history).
- The Studio panel stops calling `preview` and `print_document`. It still calls `printer_info` for the
  printer list (each printer with its job sensor `entity_id`) and the template catalog (field names,
  required flags, captions, placeholders).
- `preview`, `print_document`, `print_template`, `print_text`, `print_qr`, `print_image` remain for
  automations and keep their Pillow renderer and `expected_sha256` contract unchanged.
- No auto print: nothing prints on load, edit, upload or history open. Print and Reprint are explicit clicks.

## Browser renderer

Contract (all measured in the spike):

1. Fonts are served from the integration's frontend static path as files with `opsz` pinned (unpinned
   `opsz` is the one axis browsers disagree on). One shared `ArrayBuffer` per font file feeds one `FontFace`
   per (wdth, wght) pair with single-value descriptors, awaited before drawing. Faces live in a small LRU
   cache (8 entries) and are removed from `document.fonts` on eviction. `document.fonts.check` returns true
   for a family with no registered face, so it cannot detect a missing face; the renderer asserts that the
   specific `FontFace` object has `status === 'loaded'` and is in `document.fonts`, and fails loudly
   otherwise (a missing face silently falls back).
2. Canvas: `OffscreenCanvas(384, H)`, 2D context with default `alpha: true` (`alpha: false` yields LCD
   colour fringes), `textRendering = 'geometricPrecision'` (Chromium `auto` rounds advances), black fill on
   transparent, integer x and baseline. `ctx.font` uses numeric weight and a quoted family; percent
   stretch is rejected by the font shorthand.
3. Pack: a pixel is black iff alpha >= 96 (the server's `>= 160` white threshold on an opaque-white
   background), MSB first, 1 = ink, 48 bytes per row. The preview image is built by unpacking those bytes.
   Image blocks are first composited onto white (transparent pixels become white, EXIF orientation
   applied, as `_load_image` does) and the dither output is packed directly.
4. Layout parity with `studio_render.py` (`_label`, `_text_block`, `_stack`): same block order, insets
   (10; 34 for checklist), derived sizes including the existing clamps (title `min(64, size + 6)`, heading
   `min(64, size + 14)`, footer `max(12, size - 4)`, `studio_render.py:305-325`), line step from integer
   ascent and descent as FreeType reports them (JS rounds the font metrics to integers), border, 8-row top
   margin, 2048-row cap. Differences are bugs, checked by the golden test below.
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
| Tiny5 | width 100 or 200 | Weight fixed 400. Static font, so `font_width` means an integer horizontal pixel scale (100 = 1x, 200 = 2x) applied at draw time; pinned and hashed like Roboto Flex. Pixel-perfect only at weight 400, width 100/200 and sizes that are multiples of 8 (spike: 0% intermediate alpha). For Tiny5 the derived title, heading and footer sizes are rounded half up to a multiple of 8 with a minimum of 8 (instead of the +6/+14/-4 clamps above); the per-font table carries this rule and both renderers read it. The UI restricts Tiny5 to those values. |

- Sliders expose the full Roboto Flex ranges. No hard clamps: the WYSIWYG strip is the legibility check.
  A non-blocking hint appears when stroke counters are likely to close (weight > 700 at <= 24 px, weight
  1000 below 72 px with width < 100, width < 50 with weight > 800).
- `font_size` range grows from 12-48 to 12-96 for Roboto Flex; Tiny5 uses 8, 16, 24, 32, 40, 48. The
  catalog served by `printer_info` carries one per-font table (allowed sizes, width kind and values,
  derived-size rule) that the UI, the validation and the tests all read. `normalize_options`
  (`studio_render.py:150-158`) moves to the same size bounds and gains `columns` (only `1` is accepted by
  Pillow) and Tiny5 `font_width` (only `100`). A recipe that uses columns 2-3 or Tiny5 width 200 is
  browser-only: the Pillow actions reject it with the normal validation error, and **Open in editor** is
  its replay path. Legacy `print_text` keeps its own 12-64 range (`__init__.py:74`).
- A reproducible `tools/build_fonts.py` produces the shipped files from pinned upstream sources and prints
  their SHA-256 (Roboto Flex upstream: google/fonts commit `45e3d5269e4a17d823f6d84470bc084dc5ae46f6`,
  file SHA-256 `9b523f7d82593df0107173849ebb8c817471a1df4b4fb2c3cbf40cfd810c8281`). Fonts are committed
  as binaries with their licenses; CI verifies the hashes.
- Python renderer: bundled Roboto Flex becomes the default font when none is configured (a found system
  font from `FONT_CANDIDATES` no longer counts; this changes the output and `expected_sha256` of existing
  automations and goes into the release notes), and gains
  `font`, `font_width`, `font_weight` options applied with `set_variation_by_axes` in fvar order
  (pass the full list; out-of-range values are silently clamped by Pillow, so validate first;
  re-apply axes after `font_variant`; one font object per render, never shared across threads).
  `font` is a closed enum of the bundled names (rejected otherwise, never joined into a path).
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
- Print is enabled iff the current bitmap has ink, some field has non-blank input (an uploaded image or
  QR data counts as input), required template fields are non-blank, a device is selected, no render is
  pending, the last render succeeded, and no job for that device is in flight. In-flight state comes from
  the panel's own pending call and from the printer's job sensor: the panel reads
  `hass.states[entity_id]` (HA pushes it into the panel's `hass` setter on every state change, so no
  subscription code is needed), using the `entity_id` that `printer_info` returns. A job started elsewhere
  therefore disables Print and Reprint too; if the state is stale the click still gets "Printer is busy"
  from `async_print` and is shown as a notice. The rule is one pure function returning a list of blocker
  codes, used for the button state, the tooltip and the tests. Each render carries a generation number; the
  bitmap, geometry and `recipe` used by Print come from the same generation, and a result from an older
  generation is discarded.

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
  so column width is `floor((364 - 8*(n-1)) / n)`: 178 for two, 116 for three. The checklist indent is
  24 dots relative to the column edge (the single-column 34 is the page offset 10 plus 24).
- Applies to the main text of every template and to `kind: text`. Title, footer, QR and spool rows stay full
  width.
- Flow: greedy wrap at column width, then split into n columns minimising the tallest column, splitting at
  line boundaries; checklist items are atomic (never split across columns). Wrapping uses `measureText`
  with the same binary search as `_text_block`. Ink overhangs `measureText` width by up to 1.1 px, so the
  gutter and the "no line wider than its column" invariant are measured on advance width, not ink.
- Python renderer does not get columns in this change (non-goal). Parity with Python is therefore tested
  for `columns = 1` only; columns 2 and 3 are tested in JS against invariants (see Documentation, tests).

## Print history

- Store: `homeassistant.helpers.storage.Store`, key `tinyprint.history`, version 1, saved with
  `async_delay_save`. One domain-level history object (not one per config entry), loaded before any print
  service or button is usable, so a first record cannot overwrite stored history. All mutations run on the
  event loop. A ring buffer capped at 200 entries and 5 MiB of serialised data; the byte cap takes
  precedence, so 200 entries holds only for small labels (photo labels of about 130 KB retain roughly 25-40).
- Recording point: `StudioPrinter._async_print_job` (`studio_runtime.py:172`), the one function every entry
  point reaches because `__init__` imports `StudioPrinter` as the printer class. Recording happens inside
  the owned job task, so a cancelled or unloading job is still recorded. `source` is `studio` or `service`
  from the `print_bitmap` parameter, `service` for all legacy and template actions, `button` for the test
  print and `history` for a reprint. A history write failure is logged and never changes the job result.
- Entry: `id` (`uuid4().hex`, validated on every input), `created` (UTC ISO-8601), `device_id`, `source`,
  `status` (`sent`, `failed`, `delivery_uncertain`, `cancelled`), `phase`, `rows`, `ink_percent`,
  `bitmap_sha256`, `bitmap_z` (zlib + base64 of the packed bitmap), `thumb` (PNG of at most 96 dots wide,
  base64, made inline in the job task and counted against the byte cap), `recipe` (document JSON
  for Studio and template/text/QR actions; `null` for images and for `print_bitmap` calls without one;
  originals of images are never stored, and the server cannot check that a client recipe holds no image
  data, so that is a client guarantee), `title` (at most 80 chars, for the list; derived from the text
  for text and template actions, a generic label for QR and image actions, never a filesystem path),
  `job_id`, `timings` (object, defined by the transport spec; may be `null`).
- Failed and `delivery_uncertain` jobs are recorded too. Status precedence: no byte reached the printer and
  the job was cancelled is `cancelled`; no byte reached the printer otherwise is `failed`; any failure,
  cancel or printer fault after the first write is `delivery_uncertain`; `sent` only when completion was
  confirmed. A job that failed before any radio activity from a legacy action stores no `bitmap_z` (it
  was never printed and cannot be reprinted), which keeps a looping non-admin caller from filling the
  store. A reprint of a `delivery_uncertain` entry warns that the first copy may have printed.
- Admin-only services, all registered with `async_register_admin_service`: `history_list` (`limit` default
  50, `offset`; `SupportsResponse.ONLY`; returns entries without `bitmap_z` and `recipe`, with `thumb`),
  `history_get` (`SupportsResponse.ONLY`; one entry with `recipe` and `bitmap_z`), `history_delete`,
  `history_clear`, and `history_reprint` (`device_id`, `id`; prints the stored bitmap, never re-renders;
  decompresses with an output bound of `2048 * 48 + 1` bytes and re-validates `rows` and the decoded
  length through the shared validator before any radio activity).
- Panel: a history drawer. Each row: thumbnail, title, time, status badge. Actions: **Open in editor**
  (loads recipe, re-renders in the current browser; may differ in bits from the stored bitmap because
  engines differ; the drawer shows the stored bitmap for comparison; disabled when `recipe` is `null`;
  an unknown font or option falls back to the default with a visible notice) and **Reprint** (stored
  bitmap, exact).
- Untrusted content: non-admin users can create entries through the legacy actions, and the admin panel
  renders them. `title`, `recipe` strings and every other history field are written with `textContent`
  or property assignment, never `innerHTML`; `thumb` is accepted only when it matches
  `^[A-Za-z0-9+/=]+$` and is placed in a `data:image/png;base64,` URL built by the panel. Because those
  callers can also print in a loop, they can evict older entries; this is accepted and documented.
- Privacy: content now persists in `/config/.storage/tinyprint.history` and HA backups. Home Assistant
  also fires a `call_service` event carrying the full `service_data` for every service call, and the
  recorder stores events by default (it skips only event types excluded in `recorder:`). The recorder
  drops event data above 32768 bytes with a logged error (`MAX_EVENT_DATA_BYTES`), so the bitmap and
  recipe of small `print_bitmap` calls (up to roughly 500 rows) land in the recorder database and larger
  ones do not, at the cost of an error log line per print (verified against HA 2026.9.3 sources; the
  existing `print_document` text has the same exposure). Documentation says so
  plainly and shows `recorder: exclude: event_types: [call_service]`. `history_clear` does not purge
  backups or recorder rows, and history of a removed printer is not deleted automatically. Logs and
  diagnostics stay payload-free (existing test at `tests/test_ha_studio.py` keeps passing).

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
- Golden parity test: the expected Python line breaks and block geometry for each template with
  `columns = 1` are committed as `tests/fixtures/layout_cases.json` (regenerated by pytest in the HA-image
  job, which fails on drift and requires libraqm; without it Pillow lays out differently). The Playwright
  browser job (Chromium and Firefox, installed explicitly) compares the browser result with that file
  (offset tolerance 1 px). Lines within 1 px of the wrap width are asserted separately. For columns 2 and 3
  the browser test asserts invariants: no line wider than its column, tallest column minimal, checklist
  items never split. Parity gaps fail the build.
- Injection test: markup planted through `print_text` and through `print_bitmap` `title` and `recipe` is
  shown as text in the history drawer. Authorisation test: a non-admin call to `print_bitmap` and each
  `history_*` service is rejected.
- The six new services get `services.yaml` entries and `strings.json` / `translations/{en,ru}.json` text.
- Bump the version together everywhere it appears: `PANEL_VERSION` (`studio.py:21`), `manifest.json:32`,
  `diagnostics.py:15`, `tests/browser_studio.py:47` and `tests/test_ha_bridge.py:400-406` (currently 0.2.2;
  this change is 0.3.0).

## Non-goals

- Python columns and Python/JS pixel parity.
- A custom JS rasteriser on font outlines (would be bit-exact across engines; not needed because the browser
  bitmap is what prints).
- Guaranteeing iOS/macOS Safari, Windows or HA Companion app (WKWebView) rendering. The spike covered
  Linux engines only. Phone support is verified in the first implementation task, not assumed (see Risks).
- Cloud sync, export or import of history.

## Risks

| Risk | Mitigation |
| --- | --- |
| Caret drift in in-place overlays | Prototype first; docked textarea fallback already specified. |
| Cross-engine bit differences | History stores the bitmap; reprint uses it; nothing compares bitmaps across engines. |
| JS and Python layouts diverge | Golden parity test in CI. |
| `.storage` growth | 200 entries and 5 MiB caps, enforced on write. |
| Slow mobile devices | Latest-wins coalescing per frame; measure on a phone during acceptance. |
| Phone engines untested (iOS Safari, HA Companion app; voice-dictating users likely use a phone) | First plan task, before any panel work: run the spike recipe on the user's phone and in the Companion app. `OffscreenCanvas` needs iOS 16.4 or later, so the renderer uses a detached `HTMLCanvasElement` when it is absent (spike: bits identical across canvas kinds). Check that single-value `FontFace` `stretch` and `weight` descriptors change glyph widths on the phone. If they do not, fall back to the fontkit outline path (spike: works in all three engines, 0.67 MB). |

## Acceptance

1. Editing any field updates the strip within one animation frame once its fonts are loaded, on a desktop
   browser, with no network call and no Preview button anywhere.
2. With all fields blank the strip shows ghost examples and Print is disabled; typing one character in any
   field removes every example and the printed bitmap contains none.
3. The bytes sent in `print_bitmap` equal the bytes whose unpacked image is displayed (asserted in the
   browser test by hashing both).
4. Columns 2 and 3 produce no line wider than its column; the columns=1 golden parity test and the columns
   2-3 invariant tests pass.
5. Roboto Flex width and weight sliders visibly change the strip across their full ranges; Tiny5 renders
   with uniform pixel blocks at sizes 8, 16, 24.
6. Every job (Studio, each automation action, test button, reprint) appears in history with the right
   status; a reprint sends exactly the stored bytes.
7. A physical print of one Cyrillic label in each font and a 2-column label is photographed. Software
   success is never reported as paper output.
8. The renderer self-check passes on the user's phone and in the Companion app: axis values change glyph
   widths, a render is deterministic across two runs, and the packed bytes round-trip to the displayed image.
