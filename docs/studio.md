# TinyPrint Studio

The native **TinyPrint** sidebar panel is a label editor. You type on a paper strip
that shows the exact 1-bit bitmap, rendered in your browser while you type, and Print
sends that bitmap to the printer. It is packaged with the integration: no separate web
server, CDN, browser token storage or mobile app.

This feature branch is not proof of physical printer compatibility. Paper output
and the X6h GATT profile still require verification on a powered, reachable device.
A render never connects to Bluetooth; `transport_only` never means
that paper output or QR readability has been confirmed.

## Installation

Update the complete `custom_components/tinyprint` directory (including `frontend`)
through the configured installation method. With a Git-submodule deployment, update
the submodule and its pinned parent pointer rather than overwriting the symlink
through HACS. Restart HA after a successful configuration check. An administrator
with at least one loaded TinyPrint config entry gets the sidebar automatically.
The existing `print_text`, `print_qr`, `print_image` actions and test button remain.
The sidebar language is Russian; template identifiers and action data use stable
English keys.

## Fonts

Two fonts are bundled in `custom_components/tinyprint/frontend/fonts/`; nothing is
downloaded at run time:

- **Roboto Flex** (default): a variable font with free width (25-151) and weight
  (100-1000) axes; sizes 12-96. Latin, Latin Extended and Cyrillic (including
  Ukrainian letters).
- **Tiny5**: a pixel font; sizes 8, 16, 24, 32, 40, 48, width 100 or 200, fixed
  weight 400. It is pixel-perfect only at multiples of 8 and weight 400; the panel
  offers nothing else for `font_size`. Title and heading sizes derived from it are
  also multiples of 8 and may exceed 48.

Both fonts are licensed under the SIL Open Font License 1.1 (texts in the same
directory as `OFL-RobotoFlex.txt` and `OFL-Tiny5.txt`). The QR encoder in the panel is
the vendored Nayuki QR Code generator v1.8.0 (MIT licence,
`frontend/vendor/qrcodegen.js`).

The configured `font_path` of a printer is an **override** used only by the Pillow
renderer (automations, see below). The browser renderer ignores it and always uses
the bundled fonts. The old `/config/tinyprint_fonts` drop-in directory is no longer
searched: set `font_path` explicitly (its directory must appear in
`homeassistant.allowlist_external_dirs` in the **root** configuration, not a package).
A configured `font_path` opens that file as-is, so `font_width` and `font_weight` do
not apply to it; with `font: tiny5` the size is still limited to the Tiny5 sizes.

Characters outside the bundled fonts (emoji, scripts other than Latin, Latin
Extended and Cyrillic): the panel draws and prints the glyphs your device's system
fallback gives (what you see is what is printed, but it differs per device), while
Pillow actions print the font's missing-glyph box. Wrapping of such text can therefore
differ between the panel and an automation.

Provenance: the bundled files are built from pinned upstream sources (google/fonts
commit `45e3d5269e4a17d823f6d84470bc084dc5ae46f6`) by `tools/build_fonts.py`, which
instances the variable upstream fonts (Roboto Flex keeps only width and weight) and
subsets the glyphs.
`frontend/fonts/SHA256SUMS` lists the committed hashes; CI checks them
(`tests/test_fonts.py`). `python tools/build_fonts.py --check` rebuilds into a
temporary directory and fails on any difference (maintainers; needs
`fonttools==4.67.0`).

## Editor

The strip is the printed label, 384 dots wide, drawn as pure black and white pixels.
Zoom is 1x or 2x only (a fractional scale would blur the 1-bit bitmap): 2x is chosen
automatically when the strip area is at least 768 CSS pixels wide, otherwise 1x and a
narrow card scrolls; the zoom button overrides this.

- **Placeholders.** A template shows its example as a dimmed ghost. Examples are
  placeholders only and are never printed. While every field is blank the strip stays
  a ghost and Print is disabled. Typing anything non-blank into any field removes
  every example; fields you leave blank then simply do not appear. Whitespace-only
  counts as blank.
- **In-place editing.** Text fields are edited directly on the strip. Fields that
  cannot be edited in place are docked below it: the QR data field always, the main
  text field when `columns` is 2 or 3, and every text field with Tiny5 at width 200.
- **Columns.** `columns` 1-3 flows the main `text` field of the templates that have
  one (`note`, `heading`, `checklist`, `storage_label`) like newspaper columns;
  titles, footers and QR stay full width.
- **Options bar.** Font, size, width, weight and columns as the chosen font allows
  them. Width and weight sliders show an approximate preview (snapped to width step
  5 and weight step 50) while you drag and the exact render when you release.
  A non-blocking hint appears when strokes may close up (Roboto Flex only: weight
  above 700 at size 24 or less; weight 1000 below size 72 at width below 100; width
  below 50 at weight above 800).
- **Print** is disabled while the editor has no printer, no content, a blank required
  field, text over the limits, a pending, failed or approximate render (including
  while a slider is dragged), no ink on the label, or a job for that printer in flight
  (read from the printer's job sensor). The tooltip says which. Printing never happens
  on page load, edit, upload or printer switch, and there are no automatic retries
  after raster transmission.
- **Copy action** (under the automation section) copies a `tinyprint.print_document`
  action for the current template or QR label. It is unavailable for images and for
  options the Pillow renderer cannot draw.

### Phone and Companion app

The panel needs a browser with canvas text rendering and variable-font support. The
renderer gate passed on Safari 27 on iOS 18.7 (width and weight axes, deterministic
render, bitmap round trip). The Home Assistant Companion app and paper output on a
phone are **not yet verified**; release notes will say when they are.

### Renderer self-check

If the strip looks wrong on a device, open **Диагностика задания** and press
**Проверить рендерер**. It only draws test text on that device (nothing is printed or
sent) and writes plain text: the canvas kind, the user agent, `devicePixelRatio`, one
`PASS`/`FAIL` line per check, and a verdict line `SELFTEST PASS` or
`SELFTEST FAIL <check>`. It contains no label content or printer data. The copy button
appears only where the browser offers the Clipboard API (not on plain-`http` Home
Assistant); otherwise select the text by hand. When reporting a problem, send the
verdict line and the device facts.

## Templates

| Identifier | Layout | Fields |
|---|---|---|
| `note` | Heading, wrapped note, footer | `title`, **`text`**, `footer` |
| `heading` | Large heading and optional detail | **`title`**, `text`, `footer` |
| `checklist` | Drawn checkboxes with wrapping | `title`, **`text`**, `footer` |
| `qr_label` | QR with heading and footer | `title`, **`qr`**, `footer` |
| `storage_label` | Storage-bin label | **`title`**, `location`, `text`, `qr` |
| `spool_label` | Filament label | **`title`**, `material`, `color`, `weight`, `nozzle`, `qr` |

Bold fields are required. Combined text is limited to 3000 characters; QR data to
500 characters. No template evaluates Jinja, HTML or arbitrary expressions. HA
itself can template action data before calling the integration.

## Images and controls

Choose **Photo**, **Text/drawing**, **Logo** or **Soft halftones**, then override
controls. Presets affect only image processing. `method` selects `threshold`,
`floyd_steinberg` or deterministic `ordered` (4x4 Bayer). Available settings:

| Option | Range / choices | Default |
|---|---|---|
| `preset` | `photo`, `line_art`, `logo`, `soft` | `photo` |
| `brightness` | 0.25–2 | preset-dependent |
| `contrast` | 0.25–3 | preset-dependent |
| `sharpness` | 0–3 | preset-dependent |
| `threshold` | 1–254 | 128 |
| `rotate` | 0, 90, 180, 270 degrees clockwise | 0 |
| `invert` | boolean; images only | false |
| `width` | 96–384 dots | 384 |
| `fit` | `width` or `original` (never upscale) | `width` |
| `font` | `roboto_flex`, `tiny5` | `roboto_flex` |
| `font_size` | Roboto Flex 12-96; Tiny5 8, 16, 24, 32, 40, 48 | 24 |
| `font_width` | Roboto Flex 25-151; Tiny5 100 or 200 (200 is browser-only) | 100 |
| `font_weight` | Roboto Flex 100-1000; Tiny5 400 only | 400 |
| `columns` | 1, 2 or 3 (2 and 3 are browser-only) | 1 |
| `align` | `left`, `center`, `right` | `left` |
| `border` | boolean; text/QR/templates | false |
| `qr_size` | 96–360 dots, integer module scaling | 320 |
| `qr_correction` | `M`, `Q`, `H` | `M` |

The legacy `print_text` action keeps its own `font_size` range of 12-64. Title and
heading sizes are derived from `font_size` (title +6, heading +14, footer -4); for
Roboto Flex title and heading are capped at `max(64, font_size)`, for Tiny5 they snap
to multiples of 8.

Image rotation, contrast and halftoning never alter generated text or QR modules.
QR codes always retain a four-module quiet zone and at least three dots per module.
Oversized labels are rejected rather than silently truncated. Output is always
384 dots wide and at most 2048 rows. EXIF orientation and alpha-to-white are handled.

The panel accepts PNG/JPEG/WebP/GIF/BMP uploads up to **2 MiB** (base64 remains
below HA's default 4 MiB WebSocket frame limit). An allowed local `path` used by an
automation may be up to **4 MiB**. Every source is limited to four million decoded
pixels. Remote URLs are rejected. Animated formats use the first frame only. The
panel decodes the image in the browser, before the pixel check, so a small file that
declares huge dimensions can stall the tab before it is rejected (the panel is
admin-only). Browser resizing uses canvas smoothing, not the Pillow LANCZOS filter,
so panel and Pillow output differ for the same image. For larger camera photos,
resize/compress first or supply an appropriately bounded local file in an automation.

## Panel and automations use different renderers

**Studio prints the browser's bitmap**, so what you see is what is printed.
Automations (`print_text`, `print_qr`, `print_image`, `print_document`,
`print_template`, `preview` and the test button) use the server-side Pillow renderer.
The layout is the same, but the output is **not pixel-identical**: stroke weight
differs at sub-pixel level and glyphs may drift by 0-1 px. In development
measurements the two renderers differed in 16-42% of the pixels inked by either, with
equal legibility; different browser engines also differ from each other (up to about
9% of the ink bits between Chromium and Firefox).

- Columns 2 and 3 and Tiny5 at `font_width` 200 are **browser-only**. The Pillow
  actions reject them with a validation error (`columns` must be 1; Tiny5 `font_width`
  must be 100). The panel then offers no copyable action.
- The Pillow renderer honours a configured `font_path`; the browser renderer does
  not. A `print_document` action copied from the panel may therefore differ from the
  strip when `font_path` is set.
- QR: the panel's encoder can choose a larger QR version than Python `qrcode` for
  text mixing digits, letters and other characters. At a size boundary the panel may
  reject a QR (`qr_modules_too_small`) that Python accepts. Readability is unaffected.
- The default font changed to the bundled Roboto Flex. The output of existing
  automations, and therefore their `expected_sha256`, changes. Set the printer's
  `font_path` to the old font file to restore the old output.

## Actions

New Studio actions are admin-only for interactive calls. Trusted HA automation calls
without a user context are supported by HA's standard admin-service helper. Existing
legacy actions retain their original access behavior.

`tinyprint.printer_info` returns devices, current visibility, unchanged `rssi_raw`,
the template/preset catalogs and last metadata-only result. A positive or cached
RSSI must not be silently negated or treated as proof of a good connection. It also
returns, per printer, the `entity_id` of its job sensor; at the top level
`in_flight_states` (the job sensor states that mean a job is running: `rendering`,
`connecting`, `printing`) and `fonts` (per font: label, sizes, width and weight specs
and derived-size rules; the panel builds its controls from it).

```yaml
action: tinyprint.printer_info
response_variable: catalog
```

`tinyprint.print_bitmap` prints a finished 1-bit bitmap. This is what the panel's
Print button calls. Fields (all required):

| Field | Meaning |
|---|---|
| `device_id` | The printer's HA device |
| `rows` | Integer 1-2048 |
| `bitmap_b64` | Standard base64 of exactly `rows * 48` bytes: 384 dots per row, 48 bytes, most significant bit first, bit 1 is ink |

Validation is strict: `rows` must be an integer (not a boolean) in range, the base64
must be standard alphabet with correct padding and no whitespace or URL-safe
characters, and the decoded size must equal `rows * 48`. Otherwise the action fails
with a validation error that never echoes the data. A busy printer also fails (one job
at a time per printer). When called with a response variable it returns the usual job
result plus `bitmap_sha256`, the SHA-256 of the decoded bitmap.

Most people should use the panel. The action is for automations that already have a
bitmap. This example prints two solid black rows (a thin line; 96 bytes of `0xFF`, which is 128 `/`
characters in base64):

```yaml
action: tinyprint.print_bitmap
data:
  device_id: YOUR_HA_DEVICE_ID
  rows: 2
  bitmap_b64: "////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////"
response_variable: result
```

A script can produce `bitmap_b64` for any image. This one puts a picture on white,
fits it to 384 dots, thresholds it and packs it (needs Pillow):

```python
import base64
from PIL import Image, ImageOps

source = ImageOps.exif_transpose(Image.open("label.png")).convert("RGBA")
image = Image.alpha_composite(Image.new("RGBA", source.size, "white"), source).convert("L")
image = image.resize((384, max(1, round(image.height * 384 / image.width))))
rows = image.height
assert 1 <= rows <= 2048
packed = image.point(lambda v: 255 if v < 128 else 0).convert("1").tobytes()
print(rows, base64.b64encode(packed).decode())
```

Dark pixels become 255, so `convert("1")` packs them as 1 bits (ink).

`tinyprint.preview` is for automations and diagnosis, not the panel: it renders with
Pillow and requires a response variable. It returns `png_base64`, width,
height, ink percentage, `bitmap_sha256` and `confirmation: preview_only`.
`device_id` is optional with zero/one configured printer; choose one when multiple
printers exist, because the printer's `font_path` is used. It never establishes a BLE
connection.

```yaml
action: tinyprint.preview
data:
  device_id: YOUR_HA_DEVICE_ID
  document:
    kind: template
    template: spool_label
    fields:
      title: "Spool 042"
      material: "PETG"
      weight: "750 g"
      qr: "https://example.com/spool/42"
    options:
      font_size: 24
      border: true
response_variable: preview
```

`tinyprint.print_document` accepts the same `document` plus a required `device_id`
and renders with Pillow. An optional `expected_sha256` (compared with the
`bitmap_sha256` of a `preview` of the same document) rejects changed
content/font/file before connection. The panel does not use this action or
`expected_sha256`.

```yaml
action: tinyprint.print_document
data:
  device_id: YOUR_HA_DEVICE_ID
  document:
    kind: image
    path: "/config/tinyprint_images/photo.png"
    options:
      preset: photo
      contrast: 1.3
      method: floyd_steinberg
response_variable: print_result
```

A document is `kind: text` plus `text`, `kind: qr` plus `text`, `kind: image` plus
exactly one of `path`/`image_base64`, or `kind: template` plus `template`/`fields`.
Every kind accepts an `options` mapping. `image_base64` is raw base64 or a supported
image data URL. For interactive requests keep raw uploads at/below 2 MiB.

The shorthand `tinyprint.print_template` accepts `device_id`, `template`, `fields`,
`options` and optional `expected_sha256` using that exact same pipeline.

```yaml
action: tinyprint.print_template
data:
  device_id: YOUR_HA_DEVICE_ID
  template: checklist
  fields:
    title: "Before printing"
    text: |-
      Dry filament
      Clean the bed
      Check first layer
```

`tinyprint.diagnose` and `tinyprint.cancel` each require `device_id` and optionally
return a response. Diagnosis only connects, reads real GATT services/properties and
disconnects. It does not restart BlueZ/HA or change adapter settings. A successful
connection is explicitly `connection_only`, not a paper-print check.

## Privacy and persistence

Enable debug only for `custom_components.tinyprint.studio_runtime` when needed.
Info/warning messages already identify random job ID, phase, elapsed time, confirmed
bytes/frames and uncertain delivery. Every hundred writes produces a debug progress
record. Failures retain counters, so a connection failure is distinguishable from a
partial transmission. Phase names include `rendering`, `connecting_gatt`,
`gatt_validation`, `notification_setup`, `transmitting`, `sent` and `diagnosed`.

TinyPrint does not persist uploaded images, text or QR data, and its new logs omit
payloads and exception text. **HA's own service-event logging, debug logs and
script traces can contain action data.** Do not enable broad WebSocket/Core debug
logging for private labels; use a local image path rather than inline base64 for
long-lived automations. These framework traces are not controlled by TinyPrint.

`print_bitmap` is such a case: its service data (the bitmap) is carried in HA
`call_service` events, which the recorder stores (event data above 32768 bytes is
replaced by an empty `{}` and HA logs a warning, so large bitmaps are not kept). To keep
label content out of the database, exclude that event type:

```yaml
recorder:
  exclude:
    event_types:
      - call_service
```

## Hardware verification

To verify hardware after the host is reachable: check `printer_info`, run one
`diagnose`, capture matching BlueZ/btmon output if it fails, then explicitly print
a short note and QR. Photograph the output and scan the QR. No mock test or
`transport_only` result substitutes for this step.

## Bridge transport

With the optional bridge configured in the integration options (see `README.md` and
`docs/bridge.md`), print, `print_bitmap`, `print_document`, `print_template`, `diagnose` and
`cancel` use the bridge instead of local Bluetooth, behind the same printer lock. Extra phases appear in
`last_result.phase`: `bridge_submit`, `bridge_accepted`, `bridge_connecting`,
`bridge_discovering`, `bridge_transmitting`, then `sent`. `last_result` also carries
`bridge_job_id`, and `bridge_cancel` (`acknowledged`, `pending`, `not_found`, `failed`; the `cancel` action also
answers `none_active` when the bridge is idle and `not_applicable` without a bridge) after a
cancel.
`printer_info` gains `transport` (`mode`, `bridge_state`, `bridge_scheme`, `bridge_checked_at`)
and `local_scan_only`: in bridge mode `visible` and `rssi_raw` describe Home Assistant's own
radio, not the printer. The sidebar shows the bridge state instead of "visible over
Bluetooth". `cancel` with no local job asks the bridge to cancel whatever job it reports as
active and returns `bridge_cancel`. A timeout after a print request was sent is reported as
`delivery_uncertain`; the request is never repeated automatically, and a job that may still be
running on the bridge is cancelled rather than left without an owner.

## Verification commands

```sh
python -m pytest tests/test_core.py tests/test_transport.py tests/test_studio_render.py -q  # full CI list: .github/workflows/validate.yml
python -m unittest discover -s tests -p 'test_ha*.py' -v  # in HA 2026.9.3 image
python tests/browser_renderer.py && python tests/browser_studio.py  # Playwright, Chromium and Firefox
```

The browser harness loads the real packaged web component and the real browser
renderer but simulates HA service routing and Bluetooth. Screenshots are labelled accordingly.
It checks mobile layout, upload, presets, stale renders, explicit single printing,
that the sent bytes are the displayed pixels, no print during diagnosis, hidden image
options left out of template documents,
injection safety and browser exceptions.

## Releasing the panel (maintainers)

The panel's files are served from `/tinyprint_static/<PANEL_VERSION>/` (the constant
in `custom_components/tinyprint/studio.py`) without cache headers, and the module URL
carries the same version. Every release that changes anything under `frontend/` must
bump `PANEL_VERSION`, or browsers keep running the old files.
