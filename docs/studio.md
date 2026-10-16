# TinyPrint Studio (0.2.1)

The native **TinyPrint** sidebar contains a label editor, exact one-bit preview,
image preparation and explicit print/cancel/diagnose controls. It is packaged with
the integration: no separate web server, CDN, browser token storage or mobile app.

This feature branch is not proof of physical printer compatibility. Paper output
and the X6h GATT profile still require verification on a powered, reachable device.
A successful preview never connects to Bluetooth; `transport_only` never means
that paper output or QR readability has been confirmed.

## Installation

Update the complete `custom_components/tinyprint` directory (including `frontend`)
through the configured installation method. With a Git-submodule deployment, update
the submodule and its pinned parent pointer rather than overwriting the symlink
through HACS. Restart HA after a successful configuration check. An administrator
with at least one loaded TinyPrint config entry gets the sidebar automatically.
The existing `print_text`, `print_qr`, `print_image` actions and test button remain.

No fonts are bundled or downloaded. Use TinyPrint's font option to select an
existing Unicode TTF/OTF file; its directory must appear in
`homeassistant.allowlist_external_dirs` in the **root** configuration, not a package.
Text requires a font; a QR-only preview does not. The current sidebar language is
Russian; template identifiers and action data use stable English keys.

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
| `font_size` | 12–48 pixels | 24 |
| `align` | `left`, `center`, `right` | `left` |
| `border` | boolean; text/QR/templates | false |
| `qr_size` | 96–360 dots, integer module scaling | 320 |
| `qr_correction` | `M`, `Q`, `H` | `M` |

Image rotation, contrast and halftoning never alter generated text or QR modules.
QR codes always retain a four-module quiet zone and at least three dots per module.
Oversized labels are rejected rather than silently truncated. Output is always
384 dots wide and at most 2048 rows. EXIF orientation and alpha-to-white are handled.

The sidebar accepts PNG/JPEG/WebP/GIF/BMP uploads up to **2 MiB** (base64 remains
below HA's default 4 MiB WebSocket frame limit). An allowed local `path` may be up
to **4 MiB**. Every source is limited to four million decoded pixels. Remote URLs
are rejected. Animated formats use the first frame only. For larger camera photos,
resize/compress first or supply an appropriately bounded local file in an automation.

## Actions: same renderer, same output

New Studio actions are admin-only for interactive calls. Trusted HA automation calls
without a user context are supported by HA's standard admin-service helper. Existing
legacy actions retain their original access behavior.

`tinyprint.printer_info` returns devices, current visibility, unchanged `rssi_raw`,
the template/preset catalogs and last metadata-only result. A positive or cached
RSSI must not be silently negated or treated as proof of a good connection.

```yaml
action: tinyprint.printer_info
response_variable: catalog
```

`tinyprint.preview` requires a response variable and returns `png_base64`, width,
height, ink percentage, `bitmap_sha256` and `confirmation: preview_only`.
`device_id` is optional with zero/one configured printer; choose one when multiple
fonts/printers exist. It never establishes a BLE connection.

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

`tinyprint.print_document` accepts the same `document` plus a required `device_id`.
An optional `expected_sha256` rejects changed content/font/file before connection.
The sidebar always sends the preview hash. There is no print on page load, preview,
file upload or reconnection; there are no automatic retries after raster transmission.

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

## Logs, privacy and hardware verification

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

To verify hardware after the host is reachable: check `printer_info`, run one
`diagnose`, capture matching BlueZ/btmon output if it fails, then explicitly print
a short note and QR. Photograph the output and scan the QR. No mock test or
`transport_only` result substitutes for this step.

## Bridge transport

With the optional bridge configured in the integration options (see `README.md` and
`docs/bridge.md`), print, `print_document`, `print_template`, `diagnose` and `cancel` use the
bridge instead of local Bluetooth, behind the same printer lock. Extra phases appear in
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
python -m pytest tests/test_core.py tests/test_transport.py tests/test_studio_render.py -q
python -m unittest discover -s tests -p 'test_ha*.py' -v  # in HA 2026.9.3 image
python tests/browser_studio.py                          # requires Playwright/Chromium
```

The browser harness loads the real packaged web component and real renderer but
simulates HA service routing and Bluetooth. Screenshots are labelled accordingly.
It checks mobile layout, upload, presets, stale previews, explicit single printing,
no print during diagnosis, hidden image options left out of template previews,
injection safety and browser exceptions.
