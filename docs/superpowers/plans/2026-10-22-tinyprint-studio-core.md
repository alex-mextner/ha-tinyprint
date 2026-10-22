# TinyPrint Studio Core (WYSIWYG editor, browser renderer, fonts, `print_bitmap`) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace Studio's click-to-preview form with a live paper-strip editor whose browser renderer packs the bitmap that is printed, add columns and bundled variable fonts, and add the `print_bitmap` service.

**Architecture:** The panel renders and packs the 1-bit bitmap in the browser (canvas, `FontFace` per width/weight pair, vendored QR encoder, ported dithering) and sends only `{device_id, rows, bitmap_b64}` to the new admin service `tinyprint.print_bitmap`; Pillow renderers stay for automations and default to the bundled Roboto Flex. The panel is split into ES modules served from a version-prefixed static path (no build step, no CDN). Plans 2 (print history) and 3 (transport reliability) extend this plan's interfaces and run after it.

**Tech Stack:** Python 3.14, Pillow 12.3.0 (raqm), qrcode 8.2, Home Assistant 2026.9.3, vanilla ES modules, Playwright (Chromium + Firefox), Nayuki QR Code generator (MIT, vendored), fontTools (build tool only).

**Spec:** `docs/superpowers/specs/2026-10-22-tinyprint-studio-wysiwyg-design.md` (tracked in GitHub issue #25; commits end with `Refs #25`). Plan 1 of 3 (2 = print history, 3 = transport reliability, see `docs/superpowers/specs/2026-10-22-tinyprint-transport-reliability-design.md`). Plan date 2026-10-22.

## Global Constraints

- Bitmap: 384 dots wide, 48 bytes per row, ink = 1, MSB first, 1..2048 rows. `print_bitmap`: `rows` int 1..2048, `bool` rejected; `bitmap_b64` strict base64 decoding to exactly `rows * 48` bytes; encoded length bounded before decoding (`MAX_BITMAP_B64_LENGTH = 131072`). Validation precedes any radio activity.
- `bitmap.py` imports only the standard library and `.protocol` (the bridge host loads it through `bridge/tinyprint_bridge/_shared.py`; `bridge/install.sh` copies a fixed module list).
- Browser renderer contract: `opsz` pinned (14) in the served files; one shared `ArrayBuffer` per font file; one `FontFace` per (wdth, wght) pair under a unique family name (e.g. `tp-rf-w37-W800`) with single-value `stretch`/`weight` descriptors; faces awaited, asserted `status === 'loaded'` and in `document.fonts`, LRU of 8, removed from `document.fonts` on eviction; slider drag snaps width to step 5 and weight to step 50, exact value rendered on release. `OffscreenCanvas(384, H)` (detached `HTMLCanvasElement` when absent), 2D context `alpha: true`, `textRendering = 'geometricPrecision'`, black on transparent, integer x and baseline. `ctx.font` uses numeric weight and a quoted family. Pack: black iff alpha >= 96, MSB first. The preview is built by unpacking the packed bytes; images are composited on white first.
- Layout parity with `studio_render.py`: same block order; insets 10 (34 for checklist); derived sizes title `min(max(64, size), size + 6)`, heading `min(max(64, size), size + 14)`, footer `max(12, size - 4)` (the 64 cap becomes `max(64, size)` in BOTH renderers); integer line step from FreeType ascent/descent (scaled magnitudes rounded up); border; 8-row top margin; 2048-row cap.
- QR: vendored Nayuki encoder; same integer module scaling, quiet zone 4 modules, minimum 3 dots per module as `_qr`. Images: threshold, Floyd-Steinberg, 4x4 Bayer and the four presets ported to JS; limits 4 million pixels, 2 MiB upload unchanged.
- Fonts: Roboto Flex width 25-151 (default 100), weight 100-1000 (default 400), `opsz` pinned at 14, parametric axes pinned, subset Latin, Latin-Ext, Cyrillic U+0400-052F, common punctuation, No, rouble, euro, OFL 1.1 with `OFL.txt` beside it. Tiny5 static, weight 400, `font_width` 100 or 200 (integer horizontal pixel scale), sizes 8, 16, 24, 32, 40, 48; derived title/heading/footer = `size + 6`, `size + 14`, `size - 4` rounded half up to a multiple of 8, minimum 8. Roboto Flex `font_size` 12-96. Upstream pin: google/fonts commit `45e3d5269e4a17d823f6d84470bc084dc5ae46f6`, Roboto Flex file SHA-256 `9b523f7d82593df0107173849ebb8c817471a1df4b4fb2c3cbf40cfd810c8281`.
- Python renderers: bundled Roboto Flex is the default font when none is configured (a found system font no longer counts); `font`, `font_width`, `font_weight` options applied with `set_variation_by_axes` in fvar order (full list, validate first because Pillow clamps silently, re-apply after `font_variant`, one font object per render); `font` is a closed enum (never joined into a path); `font_path` stays an override; `columns` accepted only as `1`; Tiny5 `font_width` only `100`; legacy `print_text` keeps 12-64.
- Columns (browser only): 1, 2, 3, gutter 8, usable width 364, column width `floor((364 - 8*(n-1)) / n)` = 178 / 116, checklist indent 24 relative to the column edge, greedy wrap then split into n columns minimising the tallest, split at line boundaries, checklist items atomic, measured on advance width, applies to main text only (title, footer, QR, spool rows full width).
- Placeholder rule: drafts start empty; placeholder = catalog example text; all blank -> dimmed ghost (never packed or sent), Print disabled; any non-blank input -> no ghost, blank fields render nothing; required-field validation names the missing field; each template keeps its own draft.
- Print enabled iff: bitmap has ink, some field non-blank, required fields non-blank, device selected, no render pending, last render succeeded and is exact, no job for that device in flight. One pure function returns blocker codes. Every render carries a generation number; older results are discarded. No auto print; Print is an explicit click.
- `unknown` / `unavailable` sensor states are never in flight. Panel UI strings are Russian (existing convention); docs are English.
- Panel and docs never use `innerHTML` for user-supplied text. No CDN, no build step, no token storage.
- Existing automations keep working: `preview`, `print_document`, `print_template`, `print_text`, `print_qr`, `print_image` stay; the default-font change alters their output and `expected_sha256` in 0.3.0 (pin `font_path` to restore).
- Version after this plan: 0.3.0 (before: 0.2.2). CI: explicit pytest file list in the `tests` job of `.github/workflows/validate.yml`; `tests/test_ha*.py` run via `unittest` in the HA 2026.9.3 image (so those files MUST be `unittest.TestCase`); `python tests/browser_*.py` in the `browser` job; `ruff check . && ruff format --check .`.

## Conventions used in every task

- Local commands (run from the worktree root):
  - `PY` = `python -m pytest <files> -q`
  - `HA <file>` = `docker run --rm --entrypoint /bin/sh -v "$PWD:/workspace" -w /workspace ghcr.io/home-assistant/home-assistant:2026.9.3 -c 'python -m pip install --target /tmp/test-deps --no-deps qrcode==8.2 && PYTHONPATH=/workspace:/workspace/bridge:/tmp/test-deps python -m unittest discover -s tests -p "<file>" -v'`
  - `BR` = `python tests/browser_renderer.py && python tests/browser_studio.py` (browsers from env `TINYPRINT_BROWSERS`, default `chromium,firefox`; `CHROMIUM_PATH` still honoured)
  - `LINT` = `ruff check . && ruff format --check .`
- Commit message form: `feat|fix|test|docs|chore: <summary> (Refs #25)`. One commit per task unless a step says otherwise. Never `git add -A`.
- Browser JS is exercised from Python Playwright scripts: `page.evaluate("async () => { const m = await import('/tinyprint_static/t/<module>.js'); ... }")`. The harness (Task 10) serves `custom_components/tinyprint/frontend/` at `https://ha.test/tinyprint_static/t/`.
- Python `unittest` files for the HA job subclass `unittest.TestCase`/`IsolatedAsyncioTestCase`; pure files use pytest functions.

## Decision: split the panel into ES modules

The renderer (fonts, layout, columns, QR, dither, pack), editor state and strip add several thousand lines; one file cannot be reviewed or tested piecewise, and pure modules (blockers, scheduler, layout) are importable from the browser tests. Split, no build step: relative `import`s only. Cache coherence without bundling: the static path becomes `/tinyprint_static/<PANEL_VERSION>/` so every relative import is versioned by its URL prefix (Task 17).

## Review Focus

1. Emoji / astral characters, combining marks and tabs in text: line breaks must equal Python's (code-point slicing, `expandtabs(4)`), no crash (golden cases, Task 9/11).
2. Typing or dragging faster than rendering, then clicking Print: a stale or approximate render must never be sent (scheduler + panel tests, Tasks 16/17/19).
3. Text that renders to nothing or exceeds 2048 rows, QR data too long for `qr_size`, images over 4 million pixels: visible notice, Print disabled, previous strip never shown as current (Tasks 11/13/14/17).
4. A device whose job sensor is missing (`entity_id: null`) or is `unknown`/`unavailable` after a restart: Print stays usable (Tasks 4/16/17).
5. A font file that fails to load (404, blocked, offline): renderer fails loudly, never draws in a fallback font (Tasks 10/15/20).
6. Blank fields have no block on the strip: they must still be reachable and keep focus while the first character is typed (Task 18).

---

## File Structure

| Path | Responsibility |
| --- | --- |
| `custom_components/tinyprint/bitmap.py` (new) | `BitmapError`, `check_rows`, `decode_bitmap_b64`, size constants |
| `custom_components/tinyprint/fonts.py` (new) | `FONTS` table, `derived_size`, `bundled_font_path`, `open_font` |
| `custom_components/tinyprint/frontend/fonts/` (new) | `RobotoFlex.ttf`, `Tiny5-Regular.ttf`, `OFL-RobotoFlex.txt`, `OFL-Tiny5.txt`, `SHA256SUMS` |
| `custom_components/tinyprint/frontend/renderer/{fonts,pack,layout,qr,image,index}.js` (new) | browser renderer; `index.js` exports `Renderer`, `DEFAULT_OPTIONS`, `RenderError` |
| `custom_components/tinyprint/frontend/vendor/qrcodegen.js` (new) | Nayuki QR encoder (MIT) |
| `custom_components/tinyprint/frontend/studio/{state,status,options,live,strip}.js` (new) | editor logic: drafts/blockers, job status, option rules, frame scheduler, paper strip |
| `custom_components/tinyprint/frontend/tinyprint-panel.js` | panel shell (custom element, service calls, devices, print/cancel/diagnose) |
| `studio.py`, `studio_runtime.py`, `studio_render.py`, `render.py`, `sensor.py`, `bridge_wire.py` | modified as per tasks |
| `tools/build_fonts.py`, `tools/gen_layout_cases.py` (new) | reproducible font build; golden layout generator |
| `tests/` | `test_bitmap.py`, `test_fonts.py`, `test_ha_layout_parity.py`, `browser_harness.py`, `browser_renderer.py` (new); `browser_studio.py` rewritten; `fixtures/layout_cases.json` |

---

### Task 1: MANUAL (needs the user) - phone and Companion-app renderer gate

**Files:** none in the repo. Throwaway under `~/.cache/tinyprint-research/gates/` (never committed, never `/tmp`).

**Interfaces:** Produces a go/no-go decision and the measured phone facts for the PR description. Consumes nothing.

- [ ] **Step 1: Build the throwaway gate page** `gate/index.html` + the pinned upstream font. Download `https://raw.githubusercontent.com/google/fonts/45e3d5269e4a17d823f6d84470bc084dc5ae46f6/ofl/robotoflex/RobotoFlex%5BGRAD%2CXOPQ%2CXTRA%2CYOPQ%2CYTAS%2CYTDE%2CYTFI%2CYTLC%2CYTUC%2Copsz%2Cslnt%2Cwdth%2Cwght%5D.ttf`, verify SHA-256 `9b523f7d82593df0107173849ebb8c817471a1df4b4fb2c3cbf40cfd810c8281`. The page registers one `FontFace` per (wdth, wght) in {(25,400), (100,400), (151,400), (100,100), (100,1000)} with single-value `stretch: '<w>%'` and `weight: '<W>'` descriptors under unique family names, awaits them, and prints a `<pre>` with JSON and one verdict line `GATE PASS` / `GATE FAIL <check>`:
  1. `faceLoaded`: every `FontFace.status === 'loaded'` and `document.fonts.has(face)`.
  2. `widthAxis`: `ctx.measureText('Привет, Hello').width` at (151,400) > 1.3 x at (25,400).
  3. `weightAxis`: at (100,1000) > 1.05 x at (100,100).
  4. `deterministic`: two draws of the same string on a 384x64 canvas (`alpha: true`, `textRendering = 'geometricPrecision'`) give identical alpha arrays.
  5. `packRoundTrip`: pack (alpha >= 96, MSB first) -> unpack -> repack is byte-identical.
  6. Info only: `typeof OffscreenCanvas`, `navigator.userAgent`, `window.devicePixelRatio`. (No `crypto.subtle`: it is absent in plain-http contexts.)
- [ ] **Step 2: MANUAL (needs the user).** Ask the user to copy the folder to HA `/config/www/tinyprint-gate/` and open `<HA URL>/local/tinyprint-gate/index.html` (a) in the phone's browser, (b) inside the HA Companion app (add a dashboard with a Webpage card pointing at that path), on every phone/OS the user prints from. **Pass condition:** `GATE PASS` on every device, screenshot or pasted JSON per device. **Stop rule:** `GATE FAIL widthAxis` or `weightAxis` anywhere (single-value descriptors do not change glyph widths there) means STOP: do not start any panel or renderer work (Tasks 10-20; the Python-only Tasks 3-9 do not depend on the gate); report to the user and re-plan (candidate outside this design: the fontkit outline-rasteriser measured in the spike). Any other failure: report and wait for the user's call.
- [ ] **Step 3:** Record device, OS, engine, JSON in the PR description. No commit.

### Task 2: Caret-alignment prototype for in-place overlays (automated measurement + MANUAL visual check)

**Files:** throwaway `~/.cache/tinyprint-research/overlay/` (not committed). Result is one decision: `EDIT_POLICY` value used by Task 18.

**Interfaces:** Produces `EDIT_POLICY: 'overlay' | 'docked-all'` (Task 18 sets this constant; every later task treats both values as valid).

- [ ] **Step 1:** Using the pinned upstream Roboto Flex and Tiny5 (`.../ofl/tiny5/Tiny5%5BBLED%2CJITT%2CROND%2Cwdth%2Cwght%5D.ttf`, SHA-256 `588626f2c1b164f55927c0d8123f4bc134651aad7d257da68fc19b0824b0d8fa`, Tiny5 used at its default instance), write a page that draws text on a canvas with the Task 1 recipe and overlays an `<input>`/`<textarea>` with transparent text, native caret (`caret-color`), same family/size/weight and padding/border 0.
- [ ] **Step 2:** Playwright script (Chromium and Firefox) measures, for font cases (Roboto 100/400 24px, 37/800 32px, 151/300 24px, Tiny5 16px) x strings (Latin, Cyrillic, mixed with spaces): for every prefix length k, canvas `measureText(prefix).width` vs the `scrollWidth` of an input (width 1px, padding 0) holding that prefix. **Pass:** |difference| <= 1 px everywhere in both engines. Also record, for three multi-paragraph texts at wrap width 364, whether textarea visual lines equal the canvas greedy wrap (recorded only; wrap mismatches only misplace the caret).
- [ ] **Step 3: MANUAL (needs the user).** In headed Chromium and headed Firefox on the user's desktop, the user types and selects in the prototype overlay for all four font cases. **Pass condition:** the caret and selection highlight sit on glyph boundaries within 1 dot. **Stop rule:** any visible drift in either browser sets the decision to `docked-all`; do not "tune" CSS to pass.
- [ ] **Step 4:** State the decision (`overlay` or `docked-all`) and the numbers in the PR description. No commit.

### Task 3: `bitmap.py` shared validator (C1)

**Files:**
- Create: `custom_components/tinyprint/bitmap.py`, `tests/test_bitmap.py`
- Modify: `custom_components/tinyprint/bridge_wire.py` (`_check_rows`, `parse_print_request`, `print_request_body`, size constants at `:27-28`), `bridge/install.sh` (both `for module in ...` loops, `:119`, `:136`), `bridge/tinyprint_bridge/_shared.py` (docstring), `tests/test_bridge_installer.py` (`SHARED` set `:16`), `tests/test_bridge_wire.py` (uses of `w.MAX_BITMAP_BYTES` / `w.MAX_BITMAP_B64_LENGTH`), `.github/workflows/validate.yml` (`tests` job file list)

**Interfaces:**
- Produces (`bitmap.py`, imports only stdlib and `.protocol`):
  ```python
  ROW_BYTES: int; MAX_ROWS: int            # re-used from .protocol (48, 2048)
  MAX_BITMAP_BYTES = MAX_ROWS * ROW_BYTES  # 98304
  MAX_BITMAP_B64_LENGTH = 4 * ((MAX_BITMAP_BYTES + 2) // 3)  # 131072
  class BitmapError(ValueError): ...
  def check_rows(value: object) -> int
  def decode_bitmap_b64(rows: int, encoded: object) -> bytes
  ```
  Messages never echo input. Plan 2 reuses these for `history_reprint`.

- [ ] **Step 1: Write `tests/test_bitmap.py` (red).** Load with `from shared_loader import load; b = load("bitmap")`. Tests:
  - `test_check_rows_rejects_bool_zero_overflow_and_non_int`: parametrize `True, False, 0, -1, 2049, "5", 5.0, None` -> `BitmapError`; `1` and `2048` return themselves.
  - `test_decode_returns_exact_bytes`: rows=2, `bytes(range(96))` round-trips.
  - `test_decode_rejects_non_string_and_bad_alphabet`: `b"AAAA"`, `None`, `5`, `"A@=="`, `"AA-_"` (urlsafe), `"AA\nAA"`, bad padding.
  - `test_encoded_length_is_bounded_before_decoding`: `"!" * (b.MAX_BITMAP_B64_LENGTH + 1)` raises with "size limit" in the message (not the alphabet message), proving the bound is checked first.
  - `test_decode_rejects_length_mismatch`: rows=2 with 47*2 and 49*2 bytes.
  - `test_errors_do_not_echo_input`: marker `"SECRETMARKER"` in a bad value is absent from `str(error)`.
  - `test_constants`: `MAX_BITMAP_B64_LENGTH == 131072`, `MAX_BITMAP_BYTES == 2048 * 48`, and `BitmapError` subclasses `ValueError`.
  - In `tests/test_bridge_wire.py` add `test_bitmap_errors_map_to_bad_request`: bool rows / bad base64 / size mismatch through `parse_print_request` raise `WireError` with `.code == "bad_request"`.
- [ ] **Step 2:** `python -m pytest tests/test_bitmap.py -q` -> FAIL (module missing).
- [ ] **Step 3:** Create `bitmap.py` by moving the logic of `bridge_wire._check_rows` and the decode block of `parse_print_request` (strict `b64decode(validate=True)`, `type(value) is not int`). Rewrite `bridge_wire.parse_print_request` / `print_request_body` to call `check_rows` / `decode_bitmap_b64`, mapping `BitmapError` to `WireError("bad_request", ...)` via `_bad`; delete the moved constants/helper from `bridge_wire` (clean cutover: update `test_bridge_wire.py` references to `load("bitmap")` constants). Add `bitmap` to both install loops, the `_shared.py` docstring list and `SHARED` in `test_bridge_installer.py`.
- [ ] **Step 4:** Add `tests/test_bitmap.py` to the `tests` job list in `validate.yml`. Run `PY tests/test_bitmap.py tests/test_bridge_wire.py tests/test_bridge_installer.py tests/test_bridge_client.py tests/test_bridge_server.py` -> PASS; `LINT`.
- [ ] **Step 5: Commit** `refactor: share bitmap validation between bridge wire and print_bitmap (Refs #25)`.

### Task 4: In-flight states and `entity_id` in `printer_info` (C3)

**Files:**
- Modify: `custom_components/tinyprint/sensor.py`, `custom_components/tinyprint/studio.py` (`printer_info`), `tests/test_ha_studio.py`

**Interfaces:**
- Produces: `sensor.IN_FLIGHT_STATES = frozenset({"rendering", "connecting", "printing"})`, `sensor.SETTLED_STATES = frozenset({"idle", "sent", "error"})`. `printer_info` per printer: `"entity_id": str | None` (entity registry lookup `async_get_entity_id("sensor", DOMAIN, f"{address}_job_status")`), top-level `"in_flight_states": ["connecting", "printing", "rendering"]` (sorted). Plan 3 adds `waiting_for_printer` to `_attr_options` and `IN_FLIGHT_STATES`.

- [ ] **Step 1: Tests (red) in `tests/test_ha_studio.py`:**
  - `test_sensor_options_are_exactly_in_flight_plus_settled`: `set(TinyPrintJobSensor._attr_options) == IN_FLIGHT_STATES | SETTLED_STATES` and `IN_FLIGHT_STATES.isdisjoint(SETTLED_STATES)`.
  - `test_printer_info_returns_job_sensor_entity_id_and_in_flight_states`: patch `studio.er.async_get` to a registry whose `async_get_entity_id` records args and returns `"sensor.test_print_job"`; assert the recorded call is `("sensor", "tinyprint", "AA:BB:CC:DD:EE:FF_job_status")` (the unique-id contract), `printers[0]["entity_id"] == "sensor.test_print_job"`, `in_flight_states == ["connecting", "printing", "rendering"]`, and `"unknown"`/`"unavailable"` absent.
  - `test_printer_info_entity_id_is_none_when_the_sensor_is_not_registered` (registry returns `None`).
  - Update `test_printer_info_preserves_raw_rssi` to patch `studio.er.async_get` as well (registries are not set up in this bare `HomeAssistant`).
- [ ] **Step 2:** `HA test_ha_studio.py` -> the new tests FAIL.
- [ ] **Step 3:** Add the two frozensets next to `_attr_options` in `sensor.py` (keep `_attr_options` a list, order unchanged). In `studio.py` import `entity_registry as er` and `IN_FLIGHT_STATES`; add the two fields to `printer_info`.
- [ ] **Step 4:** `HA test_ha_studio.py` -> PASS; `LINT`.
- [ ] **Step 5: Commit** `feat: expose job sensor entity_id and in-flight states in printer_info (Refs #25)`.

### Task 5: `print_bitmap` service (C2)

**Files:**
- Modify: `custom_components/tinyprint/studio.py`, `custom_components/tinyprint/studio_runtime.py` (`StudioPrinter._render`), `custom_components/tinyprint/services.yaml`, `strings.json`, `translations/en.json`, `translations/ru.json` (mirror the `print_qr` entries; Russian text in `services.yaml` like the other Studio services), `tests/test_ha_studio.py`

**Interfaces:**
- Produces: service `tinyprint.print_bitmap` (`async_register_admin_service`, `SupportsResponse.OPTIONAL`), fields `device_id` (str), `rows`, `bitmap_b64` (schema type `object`: `bitmap.py` is the only validator). Handler named `print_bitmap` in `async_register_studio_services`. Response = `print_document`'s result dict plus `"bitmap_sha256"` (SHA-256 hex of the packed bytes, server-computed). Calls `printer.async_print("bitmap", {"rows": int, "bitmap": bytes}, 24)`. `StudioPrinter._render(kind == "bitmap")` returns `(payload["bitmap"], payload["rows"])` unchanged. Plan 2 adds optional `recipe`, `title`, `source` and the `source` keyword; this task adds none of them.
- Consumes: Task 3 `decode_bitmap_b64`, `BitmapError`.

- [ ] **Step 1: Tests (red), `tests/test_ha_studio.py`:**
  - `test_new_services_always_register` gains `"print_bitmap"`.
  - `test_print_bitmap_rejects_each_invalid_input_before_printing`: `subTest` matrix via real service calls with a stub printer whose `async_print` is `AsyncMock`: `rows=True`, `0`, `2049`, `"1"`, `bitmap_b64` invalid base64, `"A" * (MAX_BITMAP_B64_LENGTH + 4)`, 47 bytes for rows=1, 49 bytes for rows=1 -> `ServiceValidationError`, `async_print.assert_not_awaited()`.
  - `test_print_bitmap_prints_exact_bytes_and_returns_server_hash`: `async_print` returns `{"phase": "sent"}`; call rows=2 with `bytes(range(96))`; assert awaited with `("bitmap", {"rows": 2, "bitmap": bytes(range(96))}, 24)`; result `phase == "sent"` and `bitmap_sha256 == hashlib.sha256(bytes(range(96))).hexdigest()`.
  - `test_print_bitmap_requires_admin`: pattern of `test_nonadmin_cannot_use_studio_actions` -> `Unauthorized`, `async_print` not awaited.
  - `test_studio_printer_render_bitmap_returns_payload_unchanged`: real `StudioPrinter._render("bitmap", {"rows": 3, "bitmap": b}, 24) == (b, 3)`.
  - `test_print_bitmap_bytes_reach_the_transport_unchanged`: patterned on `test_configured_tail_rows_reach_the_transport`, real `StudioPrinter.async_print("bitmap", ...)` -> the mocked `send_bitmap` receives the identical bytes and rows.
- [ ] **Step 2:** `HA test_ha_studio.py` -> FAIL.
- [ ] **Step 3:** Implement per Interfaces. Wrap `BitmapError` in `ServiceValidationError(str(error)) from error`; decode before `async_print`.
- [ ] **Step 4:** `HA test_ha_studio.py` -> PASS; hassfest-relevant: `python -c "import yaml,json; yaml.safe_load(open('custom_components/tinyprint/services.yaml')); [json.load(open(f)) for f in ('custom_components/tinyprint/strings.json','custom_components/tinyprint/translations/en.json','custom_components/tinyprint/translations/ru.json')]"`; `LINT`.
- [ ] **Step 5: Commit** `feat: add admin-only print_bitmap service (Refs #25)`.

### Task 6: Bundled fonts, build tool and font table

**Files:**
- Create: `tools/build_fonts.py`, `custom_components/tinyprint/fonts.py`, `custom_components/tinyprint/frontend/fonts/{RobotoFlex.ttf,Tiny5-Regular.ttf,OFL-RobotoFlex.txt,OFL-Tiny5.txt,SHA256SUMS}`, `tests/test_fonts.py`
- Modify: `custom_components/tinyprint/studio.py` (`printer_info` gains `"fonts": FONTS`), `.github/workflows/validate.yml` (add `tests/test_fonts.py`), `tests/test_ha_studio.py`

**Interfaces:**
- Produces (`fonts.py`, Pillow imported at module level; never loaded by the bridge):
  ```python
  DEFAULT_FONT = "roboto_flex"
  FONTS: dict[str, dict]   # JSON-serialisable; served by printer_info["fonts"]
  # "roboto_flex": {"label": "Roboto Flex", "file": "RobotoFlex.ttf",
  #   "sizes": {"min": 12, "max": 96},
  #   "width": {"kind": "axis", "min": 25, "max": 151, "default": 100},
  #   "weight": {"kind": "axis", "min": 100, "max": 1000, "default": 400},
  #   "derived": {"title": 6, "heading": 14, "footer": -4, "cap": 64, "floor": 12, "snap": None}}
  # "tiny5": {"label": "Tiny5", "file": "Tiny5-Regular.ttf",
  #   "sizes": {"values": [8, 16, 24, 32, 40, 48]},
  #   "width": {"kind": "scale", "values": [100, 200], "default": 100},
  #   "weight": {"kind": "fixed", "value": 400},
  #   "derived": {"title": 6, "heading": 14, "footer": -4, "cap": None, "floor": 8, "snap": 8}}
  def font_entry(name: object) -> dict            # ValueError unless name is a str key of FONTS
  def bundled_font_path(name: str) -> Path        # frontend/fonts/<file>
  def derived_size(name: str, role: str, size: int) -> int
  def open_font(name: str, size: int, width: int = 100, weight: int = 400) -> ImageFont.FreeTypeFont
  ```
  `derived_size`: `v = size + derived[role]`; if `snap`: `v = floor(v / snap + 0.5) * snap`; `v = max(floor, v)`; if `cap`: `v = min(max(cap, size), v)` (the cap applies to roles `title` and `heading` only). `open_font` applies Roboto axes by axis name from `get_variation_axes()` in fvar order (full list), after any `font_variant`.
- Also produces `frontend/fonts/SHA256SUMS` (`<sha>  <file>` lines) and the pinned-source constants in `tools/build_fonts.py` (Roboto Flex as in Global Constraints; Tiny5: same commit, path `ofl/tiny5/Tiny5[BLED,JITT,ROND,wdth,wght].ttf`, SHA-256 `588626f2c1b164f55927c0d8123f4bc134651aad7d257da68fc19b0824b0d8fa`).

- [ ] **Step 1: Tests (red) `tests/test_fonts.py`** (pure, pytest):
  - `test_font_table_matches_the_spec_values`: literal comparison of `sizes`, `width`, `weight`, `derived` for both fonts to the Global Constraints values.
  - `test_derived_size_table` parametrized, literals: Roboto `(24,title)=30, (24,heading)=38, (24,footer)=20, (48,title)=54, (60,heading)=64, (96,title)=96, (96,heading)=96, (96,footer)=92, (12,footer)=12`; Tiny5 `(8,title)=16, (8,heading)=24, (8,footer)=8, (16,title)=24, (16,footer)=16, (24,title)=32, (24,heading)=40, (24,footer)=24, (32,title)=40, (40,footer)=40, (48,title)=56, (48,heading)=64`.
  - `test_font_entry_is_a_closed_enum`: `"../../etc/passwd"`, `"Roboto Flex"`, `""`, `None`, `5` -> `ValueError`.
  - `test_shipped_files_match_sha256sums_and_the_table`: every `FONTS[*].file` exists, its hash equals the `SHA256SUMS` line, no extra font files, both `OFL-*.txt` present and contain `SIL OPEN FONT LICENSE`.
  - `test_roboto_flex_exposes_only_width_and_weight_axes`: `open_font("roboto_flex", 24).get_variation_axes()` has exactly two axes with ranges 25..151 and 100..1000.
  - `test_roboto_flex_covers_the_promised_scripts`: `open_font("roboto_flex", 24)`: for `Ё ё Ж ӿ ԯ № ₽ € Ł ž ÿ –` the rendered mask is non-empty and differs from the mask of an unmapped code point (`"\ue000"`); Tiny5: `Ж ж Привет` likewise.
  - `test_width_axis_is_not_confused_with_weight_axis`: `open_font(..., width=151).getlength("Привет") > 1.3 * open_font(..., width=25).getlength("Привет")` and mask ink for weight 900 > weight 200 (fails if axes are passed in the wrong order).
- [ ] **Step 2:** `PY tests/test_fonts.py` -> FAIL.
- [ ] **Step 3:** Write `tools/build_fonts.py` (header comment pins `fonttools==<version you use>`; `--check` rebuilds into a temp dir and fails on any hash difference): download pinned sources and verify their SHA-256; Roboto Flex: `fontTools.varLib.instancer` pins every axis except `wdth` (25-151) and `wght` (100-1000), `opsz` = 14; subset with layout features kept (`*`) to U+0000-024F, U+0400-052F, U+2010-2027, U+2030-205E, U+2116, U+20AC, U+20BD; Tiny5: instance at its defaults (static), same subset; write `OFL-*.txt` from the upstream `OFL.txt`, `SHA256SUMS`, print hashes. Run it twice; the second run's hashes must equal the first (reproducible), then `--check`. Commit the outputs. Write `fonts.py` per Interfaces. If Tiny5 lacks Cyrillic after subsetting (the spike measured none missing) STOP and report.
- [ ] **Step 4:** Add `"fonts": FONTS` to `printer_info`; add `test_printer_info_serves_the_font_table` in `tests/test_ha_studio.py` (`result["fonts"]["tiny5"]["sizes"]["values"] == [8,16,24,32,40,48]`, `list(result["fonts"]) == ["roboto_flex", "tiny5"]`). Add `tests/test_fonts.py` to `validate.yml`. Run `PY tests/test_fonts.py`, `HA test_ha_studio.py`, `LINT`.
- [ ] **Step 5: Commit** `feat: bundle Roboto Flex and Tiny5 with a reproducible build (Refs #25)`.

### Task 7: `studio_render.py` fonts, option bounds and default font

**Files:**
- Modify: `custom_components/tinyprint/studio_render.py` (`FONT_CANDIDATES`, `DEFAULTS`, `normalize_options`, `_font`, `_text_block`, `_label`), `tests/test_studio_render.py` (loader and tests), `.github/workflows/validate.yml` (drop the `fonts-dejavu-core` apt step once tests pass without it)

**Interfaces:**
- Consumes: Task 6 `fonts.py`. Produces `normalize_options` keys `font` (default `"roboto_flex"`), `font_width` (100), `font_weight` (400), `columns` (1); `font_size` range follows the font (Roboto 12-96, Tiny5 values). Error messages name the key (`"font_width must be between 25 and 151"`). Pillow-only rejections: `columns` != 1 -> `ValueError("columns 2 and 3 are available only in the Studio editor")`; Tiny5 `font_width` 200 -> `ValueError("font_width 200 is available only in the Studio editor")`; Tiny5 `font_weight` != 400 -> `ValueError`. With a configured `font_path` the file is opened as before (axes ignored); otherwise `open_font(options["font"], size, options["font_width"], options["font_weight"])`. Derived sizes come from `fonts.derived_size`; `_label` uses it for title/heading/footer.

- [ ] **Step 1:** Switch `tests/test_studio_render.py` to `from shared_loader import load; r = load("studio_render")` (relative imports now needed; `sys.path` gets `tests/` automatically under pytest). Add tests (red):
  - `test_default_font_is_bundled_roboto_flex_not_a_system_font`: monkeypatch `PIL.ImageFont.truetype` to record paths; render a template; every recorded path is inside `frontend/fonts/`; `normalize_options({})["font"] == "roboto_flex"`.
  - `test_font_path_pinned_to_the_bundled_file_matches_the_default_render`: bitmap with `font_path=<bundled RobotoFlex.ttf>` equals the default bitmap.
  - `test_pillow_axes_follow_fvar_order`: widths 25 vs 151 and weights 100 vs 1000 render distinct bitmaps; wide > narrow ink width; heavy ink > light ink.
  - `test_font_axes_validate_before_pillow_clamps`: `font_width` in `24, 152, True, 100.5, "100"` and `font_weight` in `99, 1001, True, 400.5` raise `ValueError`; `25, 151, 100` and `100, 1000, 400` pass and render.
  - `test_font_is_a_closed_enum_in_options`: bad names raise; both bundled names render.
  - `test_font_size_bounds_follow_the_font`: Roboto 11/12/96/97; Tiny5 12 (reject), 8/16/48 (accept), 56 (reject), non-int rejects.
  - `test_columns_only_one_is_accepted`: `0, 2, 3, True, "1", 1.0` rejected, `1` accepted.
  - `test_tiny5_pillow_limits`: `font_width: 200` rejected, `font_weight: 700` rejected, `font_width: 100` renders.
  - Existing tests referencing the old `font_size` 48 ceiling or DejaVu are updated, not deleted.
- [ ] **Step 2:** `PY tests/test_studio_render.py` -> FAIL.
- [ ] **Step 3:** Implement. Remove `FONT_CANDIDATES` and the system-font search (clean cutover); one font object per `_text_block` call (no sharing across threads).
- [ ] **Step 4:** Remove the `apt-get install fonts-dejavu-core` step from `validate.yml`; `PY tests/test_studio_render.py tests/test_fonts.py`, `HA test_ha_studio.py`, `LINT` -> PASS.
- [ ] **Step 5: Commit** `feat: bundled default font, font/width/weight/columns options in studio_render (Refs #25)`.

### Task 8: `render.py` (legacy text/QR/image) defaults to the bundled font

**Files:** Modify `custom_components/tinyprint/render.py` (`FONT_CANDIDATES`, `_font`), `tests/test_core.py` (loader), `tests/test_ha*.py` only if they reference system fonts.

**Interfaces:** `render_text(text, font_size=24, font_path=None)` unchanged signature; size range 12-64 kept; default font = `open_font("roboto_flex", size)`; `font_path` overrides.

- [ ] **Step 1:** Switch `tests/test_core.py` `module(...)` to `shared_loader.load` for `protocol` and `render`. Add `test_render_text_never_opens_a_system_font` (monkeypatched `ImageFont.truetype` records paths; all inside `frontend/fonts/` when `font_path=None`; equal to the explicit path when given) and `test_render_text_size_range_is_still_12_to_64` (11 and 65 raise, 12 and 64 render).
- [ ] **Step 2:** `PY tests/test_core.py` -> FAIL (red on the first test).
- [ ] **Step 3:** Implement; delete `FONT_CANDIDATES`.
- [ ] **Step 4:** `PY tests/test_core.py tests/test_studio_render.py`, `HA test_ha.py`, `LINT` -> PASS.
- [ ] **Step 5: Commit** `feat: legacy renderer defaults to the bundled font (Refs #25)`.

### Task 9: Python layout plan, golden fixture, parity test

**Files:**
- Modify: `custom_components/tinyprint/studio_render.py` (split `_text_block`/`_label`/`_qr` into plan + draw), `tests/test_studio_render.py`
- Create: `tools/gen_layout_cases.py`, `tests/fixtures/layout_cases.json`, `tests/test_ha_layout_parity.py`

**Interfaces:**
- Produces `studio_render.layout_blocks(document: dict, font_path: str | None = None) -> dict` for `kind` template/text/qr: `{"rows": int, "blocks": [Block]}`; `Block = {"field": str, "kind": "text" | "qr", "x": int, "y": int, "w": int, "h": int, "text_y": int, "step": int, "size": int, "ascent": int, "descent": int, "lines": [str]}`. `y`/`h` = the part band (first block `y == 8`, bands contiguous, `sum(h) + 16 == rows`); text bands include 8 rows padding above and below; `text_y = y + 8`, `h = len(lines) * step + 16`; `w` = wrap width (`384 - inset - 10`); QR band has `h == image height`, `lines == []`. Spool rows give one Block per present row field (`field` = key, `x` = `inset + advance("Name: ")` rounded, lines of that row's wrapped text); other fields as named. Rendering uses the same plan (no second code path).
- Produces fixture `tests/fixtures/layout_cases.json`: `{"pillow": "12.3.0", "metrics": [{"font","size","width","weight","ascent","descent"}], "derived": [{"font","size","role","value"}], "cases": [{"name", "document", "near_edge": bool, "expect": {"rows", "blocks": [Block...]}}]}`.
  Cases (all `columns = 1`): each of the 6 templates with the catalog examples at size 24 and 96 (cap rule); `note` with a tab, an emoji (`😀`), combining marks, a blank-line paragraph, a 600-char unbroken token, and `\r\n` endings; centered and right aligned; `border: true`; `checklist` with long wrapping items; Tiny5 `note` at sizes 8/16/24; one `kind: text`; lines constructed to end within 1 px of the wrap width (`near_edge: true`). `metrics` cover sizes 8, 12, 16, 24, 32, 48, 64, 96 per valid font/width/weight combination used.

- [ ] **Step 1: Red tests in `tests/test_studio_render.py`:**
  - `test_layout_blocks_describe_the_rendered_label`: for every template: `rows == image.height`, `blocks[0].y == 8`, `blocks[i+1].y == blocks[i].y + blocks[i].h`, `sum(h) + 16 == rows`.
  - `test_every_ink_row_lies_inside_a_block_band_or_the_border`: no ink outside `[8, rows - 8)` when `border` is false.
  - `test_title_is_never_smaller_than_body_text_at_size_96`: title block `size >= 96`, `heading` template likewise; at size 24 title `size == 30`, heading `38`, footer `20`.
- [ ] **Step 2:** FAIL; implement `layout_blocks` and refactor drawing to consume the plan (bitmaps for existing tests must stay identical: run `PY tests/test_studio_render.py` before/after on the golden hashes your earlier run printed).
- [ ] **Step 3:** Write `tools/gen_layout_cases.py` (asserts `features.check_feature("raqm")`, loads `studio_render` through a synthetic package like `tests/shared_loader.py`, exposes `build_cases() -> dict`, writes the fixture with `--write`, exits 1 on drift with `--check`). Generate the fixture and commit it.
- [ ] **Step 4:** `tests/test_ha_layout_parity.py` (`unittest.TestCase`): `test_raqm_is_available` (`features.check_feature("raqm")`), `test_python_layout_matches_the_committed_fixture` (`build_cases() == json.load(...)`, failing message names the first differing case). Run `HA test_ha_layout_parity.py`. **Stop rule:** if raqm is unavailable in the HA image, STOP and report (the spec requires the assertion; do not weaken it).
- [ ] **Step 5:** `PY tests/test_studio_render.py`, `LINT` -> PASS. **Commit** `test: golden layout fixture and Python layout plan (Refs #25)`.

### Task 10: Browser harness, font registry, packer, CI browser job

**Files:**
- Create: `tests/browser_harness.py`, `tests/browser_renderer.py`, `custom_components/tinyprint/frontend/renderer/fonts.js`, `custom_components/tinyprint/frontend/renderer/pack.js`
- Modify: `.github/workflows/validate.yml` (`browser` job)

**Interfaces:**
- `browser_harness.py`: `async def open_page(playwright, browser_name: str, *, font_delay_ms: int = 0) -> Page` (routes `https://ha.test/` to a blank HTML page and `https://ha.test/tinyprint_static/t/**` to files of `frontend/`, records every request URL in `page.requests`), `def browsers() -> list[str]`, `def font_table() -> dict` (the Python `FONTS` via `shared_loader`).
- `pack.js`: `WIDTH = 384`, `ROW_BYTES = 48`, `MAX_ROWS = 2048`, `ALPHA_INK_MIN = 96`; `packAlpha(rgba: Uint8ClampedArray, width: number, rows: number): Uint8Array`; `packInk(ink: Uint8Array, rows: number): Uint8Array` (one 0/1 byte per pixel); `unpack(bitmap: Uint8Array, rows: number): ImageData` (black ink, opaque white background); `inkPercent(bitmap: Uint8Array): number`.
- `fonts.js`: `class FontError extends Error { code: 'font_missing' | 'font_load_failed' }`; `class FaceRegistry { constructor(baseUrl: URL, limit = 8); face(entry, width: number, weight: number): Promise<Face>; dispose(): void }`; `Face = { family: string, css(size: number): string, ascent(size: number): number, descent(size: number): number, scaleX: number }`. Family name `tp-<key>-w<width>-W<weight>` (Tiny5: one face, `scaleX = width / 100`). `ascent/descent` = `ceil(hhea.ascender * size / unitsPerEm)` and `ceil(-hhea.descender * size / unitsPerEm)` parsed from the shared `ArrayBuffer`.

- [ ] **Step 1: Red tests in `tests/browser_renderer.py`** (each run for every browser; `assert` style like the existing browser script, collecting `checks`):
  - `pack: bit order and alpha threshold`: alpha row with ink only at x=0 -> byte 0 is `0x80`; only x=383 -> byte 47 is `0x01`; alpha 95 -> 0 bit, 96 -> ink; `packInk` agrees.
  - `pack: unpack(pack(b)) == b` for a seeded random 40-row bitmap; `inkPercent` of one all-ink row is 100.
  - `fonts: width and weight axes change advance widths`: advance of `"Привет"` at (25,400) < (100,400) < (151,400) strictly and (100,1000) > (100,100).
  - `fonts: LRU keeps 8 faces and removes evicted ones from document.fonts`: nine distinct (width, weight) -> exactly 8 `tp-` faces in `document.fonts`, the first is absent; requesting it again works.
  - `fonts: missing file fails loudly`: route returns 404 for `fonts/RobotoFlex.ttf` -> `face()` rejects with `code === 'font_missing'` and no `tp-` face remains registered.
- [ ] **Step 2:** Locally: `pip install playwright opencv-python-headless numpy && python -m playwright install chromium firefox`; run `python tests/browser_renderer.py` -> FAIL (modules missing).
- [ ] **Step 3:** Implement `pack.js`, `fonts.js` (one `fetch` per file cached as `ArrayBuffer`; `new FontFace(family, buffer, {stretch: `${w}%`, weight: String(W)})`; assert `face.status === 'loaded' && document.fonts.has(face)`, else `FontError`), harness.
- [ ] **Step 4:** `validate.yml` `browser` job: `pip install Pillow==12.3.0 qrcode==8.2 playwright opencv-python-headless numpy`, `python -m playwright install --with-deps chromium firefox`, steps `python tests/browser_renderer.py` then `python tests/browser_studio.py`. Run `python tests/browser_renderer.py` -> PASS (browser_studio.py is rewritten in Task 17; until then it may stay as is, do not run it in this task).
- [ ] **Step 5: Commit** `feat: browser font registry, bitmap packer and browser test harness (Refs #25)`.

### Task 11: Layout engine (single column) matching the golden fixture

**Files:** Create `custom_components/tinyprint/frontend/renderer/layout.js`; modify `tests/browser_renderer.py`.

**Interfaces:**
- `class RenderError extends Error { code: 'too_tall' | 'blank_qr' | 'qr_modules_too_small' | 'image_too_large' | 'image_unreadable' | 'bad_document' | 'font_missing' | 'font_load_failed' }` (exported from `layout.js`, re-exported by `index.js`).
- `derivedSize(entry: FontEntry, role: 'title' | 'heading' | 'footer', size: number): number` (same rule as `fonts.derived_size`).
- `neededSizes(document, entry): number[]`; `wrapText(measure: (s: string) => number, text: string, maxWidth: number, opts?: { checklist?: boolean }): { text: string, check: boolean }[]` (same algorithm as `_text_block`: `expandtabs(4)` per line position, `\n` split, `rstrip("\r")`, binary search on a code-point array bounded to 512, back off to the last space, `lstrip(" ")`, empty paragraph keeps one empty line, 2048-row cap -> `RenderError('too_tall')`).
- `planLabel(document, faces: Map<number, Face>, ctx: CanvasRenderingContext2D, templates): LabelPlan`; `LabelPlan = { rows: number, border: boolean, blocks: Block[], slots: Slot[] }`. `Block` = the Task 9 shape plus `align`, `checklist`, `face`, optionally `columns` (Task 12). `Slot = { field, x, y, w, h }` for every field of the template that is blank: position where its first line would be drawn if it held one character, computed by planning a probe document with that field set to `"x"` (so a slot's `y` equals the eventual block's `text_y` by construction).
- `drawPlan(ctx, plan): void`: baseline `text_y + i * step + ascent`, x rounded to integers (center = `max(inset, round((384 - w) / 2))`, right = `max(inset, 384 - 10 - round(w))` exactly as Python's clamps), checkbox = four `fillRect` bars equivalent to PIL `rectangle((10, y+7, 24, y+21), outline=0, width=2)`, border = bars equivalent to `rectangle((2, 2, 381, rows-3), outline=0, width=2)`.

- [ ] **Step 1: Red tests (`tests/browser_renderer.py`)** reading `tests/fixtures/layout_cases.json`:
  - `layout: golden metrics`: for every `metrics` entry `face.ascent(size) == ascent` and `face.descent(size) == descent` exactly (all sizes, including 96 and Tiny5 8/16/24).
  - `layout: derived sizes equal the Python table` for all `derived` entries (exact).
  - `layout: golden cases`: for every case not `near_edge`: `rows` equal; block `field`/`kind`/`lines` equal; `x`, `y`, `w`, `h`, `text_y` within 1 of the fixture.
  - `layout: near-edge cases`: for `near_edge` cases the concatenation of lines (spaces collapsed) equals the fixture's, and every JS line measures `<= w` (breaks at the edge may differ by one word).
  - `layout: emoji, tab, combining marks and a 600-char token do not crash` (covered by cases; additionally assert the emoji stays in one piece: no lone surrogate in any line).
  - `layout: more than 2048 rows raises too_tall` (3000 newlines -> `RenderError.code === 'too_tall'`).
  - `layout: checkbox and border pixels follow the Python rectangles`: draw a checklist item and a border into a canvas, assert ink at `(10, y+7), (24, y+21), (11, y+8), (23, y+20)` and none at `(13, y+10)`; border ink at `(2,2), (3,3), (381, rows-3)` and none at `(4,4)`.
  - `layout: a blank field has a slot whose y equals the block it will become`: plan `note` with title only, read the footer slot, then plan with footer `"x"`; slot `y == footer.text_y`.
- [ ] **Step 2:** FAIL. **Step 3:** Implement `layout.js` (measure with `ctx.font = face.css(size)`, `ctx.measureText`; Tiny5 measure is multiplied by `face.scaleX`). **Step 4:** `python tests/browser_renderer.py` -> PASS in all browsers.
- [ ] **Step 5: Commit** `feat: browser layout engine with golden parity (Refs #25)`.

### Task 12: Columns 2 and 3

**Files:** Modify `renderer/layout.js`, `tests/browser_renderer.py`.

**Interfaces:** `Block.columns?: { x: number, w: number, lines: string[], widths: number[], items: number[] }[]` (present when `options.columns > 1` and the block is the main `text` field): `items` = line counts of the items in that column (a non-checklist item is one line; a checklist item is all wrapped lines of one paragraph); `widths` = advance widths. `flowColumns(items: number[], n: number): number[][]` (minimise the tallest column over contiguous partitions; ties: fill earlier columns first). Column `i` x = `10 + i * (colW + 8)`; wrap width = `colW` (checklist `colW - 24`, text and checkbox offset 24 from the column edge); block `h = tallest * step + 16`; alignment inside the column (`left` = column x, `center` = column x + round((colW - w) / 2), `right` = column x + colW - round(w), never left of the column x). Spool rows, title, location, footer, QR unchanged.

- [ ] **Step 1: Red tests:**
  - `columns: widths and positions`: columns 2 -> `w == 178`, x `[10, 196]`; columns 3 -> `w == 116`, x `[10, 134, 258]`.
  - `columns: no line is wider than its column` (advance `widths[i] <= w`) for note/checklist/heading/storage_label texts incl. a long unbroken token, columns 2 and 3.
  - `columns: tallest column is minimal`: Python brute-forces every contiguous n-partition of the item list (flattened from `columns[*].items`) and compares the minimum tallest height to the JS result.
  - `columns: reading order preserved and checklist items atomic`: concatenating `columns[*].lines` equals the single-column wrap of the same text at column width; `columns[*].items` concatenated equals the item list and each column's `items` sum equals its line count.
  - `columns: full-width blocks are unchanged`: title/footer/QR blocks equal their `columns = 1` plan.
  - `columns: checklist indent is 24 relative to the column edge`: text x = column x + 24, wrap width 154 (two columns).
  - `columns: a single short line with 3 columns yields one non-empty column` and `columns: 1 equals the golden fixture`.
- [ ] **Step 2-4:** FAIL, implement, `python tests/browser_renderer.py` PASS. **Step 5: Commit** `feat: newspaper columns in the browser layout (Refs #25)`.

### Task 13: QR encoder

**Files:** Create `custom_components/tinyprint/frontend/vendor/qrcodegen.js` (single-file Nayuki QR Code generator, MIT license header kept; change only the tail to `export default qrcodegen`; note upstream tag/commit in the header), `custom_components/tinyprint/frontend/renderer/qr.js`; modify `tests/browser_renderer.py`.

**Interfaces:** `qrLayer(text: string, ecc: 'M' | 'Q' | 'H', qrSize: number): { ink: Uint8Array, width: 384, rows: number }`: `ECC` as requested (no automatic boost), 4-module quiet zone included in the module count, `scale = floor(qrSize / (modules + 8))`, `scale < 3` -> `RenderError('qr_modules_too_small')`, blank text -> `RenderError('blank_qr')`, image centred at `floor((384 - w) / 2)`, `rows = modules * scale`. `layout.js` consumes it for QR blocks (`field: 'qr'`, band height = `rows`); extend `planLabel`/`drawPlan` accordingly.

- [ ] **Step 1: Red tests:**
  - `qr: decodes with OpenCV`: render `kind: qr` blocks for `https://example.com/spool/42`, `Катушка №42 — PETG`, and a 120-char string at `qr_size` 320 and ecc M/Q/H; Python builds a PIL image from the packed bytes and `cv2.QRCodeDetector().detectAndDecode` returns the original text.
  - `qr: integer module scaling and quiet zone`: rows is a multiple of the scale, the first and last `4 * scale` columns/rows of the code image are blank, scale >= 3.
  - `qr: too small modules raise`: 500-character data at `qr_size` 96 -> `qr_modules_too_small`; blank/whitespace data -> `blank_qr`.
- [ ] **Step 2-4:** FAIL, implement, PASS. **Step 5: Commit** `feat: browser QR encoder (Refs #25)`.

### Task 14: Image pipeline

**Files:** Create `custom_components/tinyprint/frontend/renderer/image.js`; modify `tests/browser_renderer.py`.

**Interfaces:**
- `decodeUpload(blob: Blob): Promise<{ id: number, bitmap: ImageBitmap }>`: `createImageBitmap(blob, { imageOrientation: 'from-image' })`; `width * height > 4_000_000` -> `RenderError('image_too_large')`; unreadable -> `RenderError('image_unreadable')`.
- `processImage(source, options): { ink: Uint8Array, width: 384, rows: number, layer: 'hit' | 'miss' }` where `options` = resolved `rotate, width, fit, brightness, contrast, sharpness, invert, method, threshold`. Order as `_image`: composite on white, rotate, resize (`imageSmoothingQuality = 'high'`) to `width` (`fit: original` -> `min(width, w)`), height `max(1, round(h * width / w))` (> 2048 -> `RenderError('too_tall')`), `ImageEnhance` semantics for brightness (multiply), contrast (blend with `floor(mean + 0.5)` gray), sharpness (blend with 3x3 `[[1,1,1],[1,5,1],[1,1,1]]/13`, border pixels copied), invert, then `threshold` (`p >= t` white), `floyd_steinberg` (on `clamp(p + 128 - t)`), `ordered` (4x4 Bayer, limit `(m + 0.5) * 16 + t - 128`); centred on white. The resized gray layer is cached by `(source.id, rotate, width, fit)` (spec's key listed `preset/invert/dither` which would collide when brightness changes; all remaining steps are cheap and always re-run).

- [ ] **Step 1: Red tests** (fixtures generated in Python with Pillow and passed as bytes: 360x240 gradient, flat gray 128 square, fully transparent PNG, a JPEG with EXIF orientation 6, an all-white 2001x2001 PNG, a 50x1000 PNG):
  - `image: flat mid-gray dithers to about half ink` for `floyd_steinberg` and `ordered` (brightness = contrast = sharpness = 1, threshold 128): ink between 45% and 55%.
  - `image: threshold ink is non-decreasing in the threshold` (gradient, thresholds 32..224 step 32).
  - `image: transparent pixels become white` (ink 0). `image: EXIF orientation is applied` (100x50 with orientation 6 -> `rows == 768`). `image: rotate 90 swaps dimensions`. `image: invert turns a black square white`.
  - `image: over 4 million pixels is rejected` (`image_too_large`); `image: 50x1000 at width 384 is rejected` (`too_tall`); `image: unreadable bytes -> image_unreadable`.
  - `image: resized layer is cached` (second call with a different threshold is `'hit'`, a changed width is `'miss'`).
  - `image: presets change the bitmap` (photo vs line_art option sets from Python `PRESETS` differ on the gradient).
- [ ] **Step 2-4:** FAIL, implement, PASS. **Step 5: Commit** `feat: browser image preparation (Refs #25)`.

### Task 15: `Renderer`, ghost/approximate renders, `selfTest`

**Files:** Create `custom_components/tinyprint/frontend/renderer/index.js`; modify `tests/browser_renderer.py`.

**Interfaces (consumed by the panel and by Plans 2/3):**
```js
export const DEFAULT_OPTIONS;                       // same keys/values as studio_render.DEFAULTS
export { RenderError } from './layout.js';
export class Renderer {
  constructor({ baseUrl: URL, fonts: FontTable, templates: object });
  render(input: RenderInput): Promise<RenderResult>;
  selfTest(): Promise<SelfTestReport>;
  faceFor(options): Promise<Face>;                  // overlay CSS uses the same family/size
  dispose(): void;
}
// RenderInput = { document, image: {id, bitmap}|null, ghost: boolean, approximate: boolean, generation: number }
// document = {kind:'template',template,fields,options} | {kind:'text',text,options} | {kind:'qr',text,options} | {kind:'image',options}
// RenderResult = { generation, exact: boolean, ghost: boolean, rows, bitmap: Uint8Array|null,   // null when ghost
//                  pixels: ImageData, blocks: Block[], slots: Slot[], inkPercent: number, face: Face|null }
// SelfTestReport = { ok: boolean, engine: { userAgent, offscreenCanvas: boolean },
//                    checks: { name: string, ok: boolean, detail: string }[] }
```
`approximate: true` snaps width to multiples of 5 and weight to multiples of 50 (nearest, clamped to the font's range) and sets `exact` to whether the snapped values equal the requested ones; `ghost: true` renders the document as given (the panel passes the example fields) and returns `bitmap: null`, `pixels` from the canvas (never packed). Canvas: `OffscreenCanvas(384, rows)` else a detached `HTMLCanvasElement`; context `{ alpha: true, willReadFrequently: true }`, `textRendering = 'geometricPrecision'`; rows > 2048 -> `RenderError('too_tall')`. `pixels` for non-ghost results are `unpack(bitmap, rows)`. `selfTest` checks, in order: `faces_loaded`, `width_axis_changes_glyph_widths`, `weight_axis_changes_glyph_widths`, `render_is_deterministic`, `pack_roundtrip_matches_display`; a font failure yields `ok: false` with a failing check, never a throw. It uses no `crypto.subtle` (insecure contexts).

- [ ] **Step 1: Red tests:**
  - `renderer: two renders are byte-identical and pixels equal unpack(bitmap)`; `renderer: every document kind renders` (template, text, qr, image); `renderer: unknown template -> bad_document`.
  - `renderer: ghost has no bitmap but has pixels`.
  - `renderer: approximate render equals the exact render at the snapped values and is flagged inexact`: (37, 480) approximate bytes == exact (35, 500) bytes, `exact === false`; exact render of (37, 480) differs from both and has `exact === true`; (35, 500) approximate is `exact === true`.
  - `renderer: sliders change the strip across their full ranges`: width 25 / 100 / 151 and weight 100 / 1000 give pairwise different bitmaps and ink at weight 1000 > ink at weight 100.
  - `renderer: Tiny5 has no intermediate alpha at sizes 8, 16, 24`: draw `"Привет"` with a Tiny5 `Face` on an `OffscreenCanvas`; every alpha is 0 or 255; ink run lengths are multiples of `size / 8`.
  - `renderer: font failure fails loudly` (404 for the font file -> `render` rejects with `font_missing`; no bitmap).
  - `renderer: DEFAULT_OPTIONS equals studio_render.DEFAULTS` (keys and values; Python side imported in the test).
  - `renderer: selfTest passes` (all five checks `ok`, `report.ok === true`); with a 404 font `selfTest` returns `ok: false` and does not throw.
- [ ] **Step 2-4:** FAIL, implement, PASS in Chromium and Firefox. **Step 5: Commit** `feat: browser Renderer with ghost, approximate renders and selfTest (Refs #25)`.

### Task 16: Pure editor modules (blockers, placeholders, status, option rules, scheduler)

**Files:** Create `frontend/studio/state.js`, `frontend/studio/status.js`, `frontend/studio/options.js`, `frontend/studio/live.js`; modify `tests/browser_renderer.py` (these modules are DOM-free, tested with `page.evaluate`).

**Interfaces:**
```js
// state.js
export const BLOCKERS = ['no_device','no_input','required_blank','render_pending','render_failed','render_approximate','no_ink','job_in_flight'];
export const BLOCKER_TEXT: Record<string, string>;     // Russian tooltip text per code; required_blank gets the captions appended by the caller
export function isBlank(value: unknown): boolean;      // non-string or whitespace-only
export function hasUserInput(s: { mode: 'template'|'qr'|'image', fields: Record<string,string>, qrText: string, hasImage: boolean }): boolean;
export function missingRequired(s, templates): string[];           // field keys, template mode only
export function effectiveDocument(s, templates, options): { document: object | null, ghost: boolean };
//   no input -> ghost document built from the template's `example` (or null for qr/image modes); else only non-blank fields
export function printBlockers(s: { deviceId: string, hasInput: boolean, missing: string[], renderPending: boolean,
                                   renderOk: boolean, renderExact: boolean, hasInk: boolean, inFlight: boolean }): string[];  // BLOCKERS order
// status.js
export const STATUS_TEXT: Record<string, string>;      // rendering/connecting/printing -> Russian label; Plan 3 adds waiting_for_printer
export function jobInFlight(stateObject: {state: string} | undefined, inFlightStates: string[]): boolean;
export function statusText(stateObject): string | null;
// options.js
export function snapToGrid(value: number, step: number, min: number, max: number): number;
export function closingCounterHint(font: string, size: number, width: number, weight: number): string | null;
export function coerceForFont(options, fonts: FontTable, fontName: string): object;   // nearest allowed size, width/weight to defaults
export function usesBrowserOnlyOptions(options): boolean;                              // columns > 1 or Tiny5 font_width 200
// live.js
export class RenderScheduler {
  constructor(run: (input, generation: number) => Promise<RenderResult>, onResult: (r: RenderResult) => void,
              onError: (e: Error, generation: number) => void, raf = requestAnimationFrame);
  request(buildInput: () => RenderInput): number;      // new generation; one run per frame, latest wins
  get pending(): boolean; get generation(): number; dispose(): void;
}
```
- [ ] **Step 1: Red tests (exact assertions):**
  - `state: all blank -> ghost and no_input`: `effectiveDocument` returns the example document with `ghost: true`; `printBlockers` for that state contains `'no_input'`.
  - `state: one character removes every example`: with `title: "a"` the document's `fields` equals `{title: "a"}` exactly (no example text anywhere, blank fields absent), `ghost: false`.
  - `state: whitespace-only counts as blank` (including `"\n\t "`), an uploaded image or QR text counts as input.
  - `state: required field named`: `note` with only a title -> `missingRequired == ['text']`, blockers `['required_blank']` (when everything else is fine).
  - `state: every single failing condition yields exactly its code and an all-good state yields []`; order equals `BLOCKERS`; `render_approximate` and `render_failed` and `no_ink` are independent.
  - `state: switching templates keeps each draft` (pure draft map helper used by the panel: write note, switch to heading and back, note intact).
  - `status: unknown, unavailable, undefined, idle, sent, error are never in flight; rendering, connecting, printing are`; `statusText` is null for settled states.
  - `options: closingCounterHint`: `(Roboto, 24, 100, 701)` hint, `(…, 24, 100, 700)` none, `(…, 25, 100, 701)` none, `(…, 71, 99, 1000)` hint, `(…, 72, 99, 1000)` none, `(…, 71, 100, 1000)` none, `(…, 30, 49, 801)` hint, `(…, 30, 49, 800)` none; Tiny5 never hints.
  - `options: snapToGrid(37, 5, 25, 151) == 35`, `(480, 50, 100, 1000) == 500`, `(1000, 50, …) == 1000`, clamps at the ends; `coerceForFont` moves size 20 to the nearest Tiny5 size (16), resets width/weight when switching font, keeps `columns`.
  - `live: 50 requests inside one frame run once with the latest input`; `live: a stale result is discarded and only the newest generation is delivered`; `live: pending is true until the current generation's result arrives`; `live: a stale rejection is ignored, a current rejection reaches onError` (fake `raf` passed in).
- [ ] **Step 2-4:** FAIL, implement, PASS. **Step 5: Commit** `feat: editor state, blockers, status, option rules and frame scheduler (Refs #25)`.

### Task 17: Panel shell, live preview, `print_bitmap` flow, versioned static path

**Files:**
- Modify: `custom_components/tinyprint/frontend/tinyprint-panel.js` (shell), `custom_components/tinyprint/studio.py` (`async_setup_panel`), `tests/test_ha_studio.py`, `tests/browser_studio.py` (rewritten)
- Uses: Tasks 15-16 modules.

**Interfaces (panel shell; Plans 2/3 plug in here):**
- `TinyPrintPanel` methods: `call(service, data, returnResponse = true)`, `notice(text, error = false)`, `loadInfo()`, `jobState(deviceId): { entityId: string | null, state: string | undefined, inFlight: boolean }`, `getDocument(): object | null` (current print_document-shaped document of the visible mode, image mode -> `null`; only options relevant to the mode), `renderNow(): void` (re-render with the current draft), property `renderer: Renderer`, `lastResult: RenderResult | null`.
- DOM extension points in `mount()`: the `.devicebar` toolbar row (Plan 2 adds the history button there), the `.layout` grid (Plan 2 appends the drawer after it), element `#job-status` (Plan 3 extends `STATUS_TEXT` in `studio/status.js`; the shell writes `statusText(...)` into it from the `hass` setter), `#notice`.
- Static path: `StaticPathConfig(f"/tinyprint_static/{PANEL_VERSION}", frontend_dir, False)` and `module_url=f"/tinyprint_static/{PANEL_VERSION}/tinyprint-panel.js"` (no `?v=`).

Behaviour: the entry file defines the element synchronously and loads its modules with `import()` relative to `import.meta.url`. Removed: the `Preview` button, the hint "После изменения макета обновите предпросмотр.", "refresh preview" notices, `makePreview`, `invalidate`, `signature`. Drafts start empty; each field's `placeholder` = catalog example. Live preview: every `input`/`change` calls `scheduler.request(...)`; the result is drawn into `<canvas id="raster">` from `result.pixels` (ghost drawn at reduced opacity); `#dimensions`/`#ink` captions from the result. Print: one click -> `print_bitmap {device_id, rows, bitmap_b64}` built from `lastResult` (same generation as the displayed strip), sets `pendingCall` (blocker `job_in_flight`), then `loadInfo()`; button `title` = the blockers' texts. `hass` setter: `printers[].entity_id` + `printer_info.in_flight_states` -> `jobInFlight(this.hass.states[entityId], ...)`; a missing entity or state never locks. The "no printer" option text no longer promises a preview. The "file is not stored on the server" hints (old `:85`, `:225`) are reworded: the file never leaves the browser.

- [ ] **Step 1: Python red test `test_panel_is_served_from_a_version_prefixed_path`** in `tests/test_ha_studio.py` (extend `test_panel_registration_is_idempotent`): the static config URL equals `/tinyprint_static/{PANEL_VERSION}` and `module_url == f"/tinyprint_static/{PANEL_VERSION}/tinyprint-panel.js"`.
- [ ] **Step 2: Rewrite `tests/browser_studio.py`** against the harness (mock services: `printer_info` with `entity_id`, `in_flight_states`, `fonts`, `templates`, `presets`; `print_bitmap` returning `phase`, `bytes_written` and a server-side `bitmap_sha256`; `diagnose`, `cancel`; no `preview`/`print_document`). Port every check of the old script that does not mention preview (mobile 390 px no overflow, bridge/native wording, diagnose never prints, injection safety, image upload). New tests:
  - `no #preview anywhere`: `locator("#preview").count() == 0`, no `preview`/`print_document` call ever, only `printer_info` on load.
  - `all blank shows a ghost and blocks Print`: `#print` disabled, `title` contains the `no_input` text, the canvas has ink, and `calls` has no `print_bitmap`.
  - `one character removes the ghost`: type into the required `text`, the sent bitmap equals `Renderer.render` of exactly the typed fields (no example text present); with only a title typed, `#print` stays disabled and its tooltip names the caption of `text`.
  - `sent bytes equal the displayed pixels` (acceptance 3): decode `bitmap_b64` in Python; pack the displayed `#raster` canvas pixels (ink = dark) in Python; bytes equal; `rows * 48 == len(bytes)`.
  - `one click sends once` (double click -> exactly one `print_bitmap`); `nothing prints on load, edit, upload or template switch`.
  - `Print follows the job sensor`: set `hass.states[entity_id] = {state: 'printing'}` -> `#print` disabled with the in-flight text and `#job-status` shows the label; `'unavailable'`, `'unknown'` and a missing entity leave Print enabled.
  - `a render that is still pending blocks Print`: delay the font fetch (`font_delay_ms`), type, click -> no `print_bitmap`; after the render lands Print works and the sent bitmap matches the final text.
  - `a font load failure blocks Print with a notice` (404 font file).
  - `template drafts are kept per template`; `hidden controls are not part of getDocument()` (template mode contains no image options, image mode contains no `font_*`/`align`).
- [ ] **Step 3:** Run `HA test_ha_studio.py` and `python tests/browser_studio.py` -> FAIL. **Step 4:** Implement the shell split, versioned path, live flow. Keep the Russian UI copy of untouched controls.
- [ ] **Step 5:** `HA test_ha_studio.py`, `BR`, `LINT` -> PASS. **Commit** `feat: live WYSIWYG preview and print_bitmap flow, versioned panel path (Refs #25)`.

### Task 18: Paper strip, in-place overlays, docked inputs

**Files:** Create `frontend/studio/strip.js`; modify `tinyprint-panel.js`, `tests/browser_studio.py`.

**Interfaces:**
```js
export const EDIT_POLICY = 'overlay';   // set to the outcome of Task 2: 'overlay' | 'docked-all'
export function fieldEditMode(field: string, ctx: { columns: number, policy: string }): 'overlay' | 'docked';
//   'docked-all' -> docked; field 'qr' -> docked; field 'text' with columns > 1 -> docked; otherwise overlay
export class PaperStrip { constructor(container: HTMLElement);
  show(result: RenderResult, zoom: 1 | 2): void;
  setFields(fields: { key, caption, multiline, value, placeholder }[], mode: (key) => 'overlay'|'docked', onInput: (key, value) => void): void;
  focusField(key: string): void; dispose(): void; }
```
Strip = the dimmed/real canvas plus overlay `input`/`textarea` elements positioned from `result.blocks[].{x,text_y,w,h}` (and `result.slots` for blank fields, so the same DOM element is repositioned, never recreated, while the first character is typed); overlay text colour transparent, `caret-color` visible, `font` = `face.css(size)` of the renderer's face, line height = block `step`, padding/border 0, `aria-label` = caption; blank unfocused overlays are `pointer-events: none`; a chip row above the strip (`+ <caption>`) focuses blank fields; ghost placeholders in overlays are transparent (the ghost is drawn on the canvas), docked inputs show the example as a normal placeholder. 1x/2x toggle `#zoom`; the inner 384-wide box is scaled with CSS `transform` to fit narrow containers (overlays are children of that box). Spool row overlays start at `block.x` (after the caption prefix). The `qr` field and QR/image modes use docked inputs.

- [ ] **Step 1: Red tests:**
  - `edit mode table`: `('title', {columns:1, policy:'overlay'})` overlay; `('text', columns 1)` overlay; `('text', columns 2)` docked; `('footer', columns 3)` overlay; `('qr', any)` docked; any field with `policy: 'docked-all'` docked.
  - `overlay geometry equals block geometry`: each overlay's rect (divided by the strip scale) is within 1 px of `x, text_y, w, h`; at zoom 2 and at a 390 px viewport the same holds.
  - `overlay uses the renderer's font`: computed `font-family` contains `tp-rf-`, `font-size` equals the block size, `color` is transparent, `caret-color` is not.
  - `typing in an overlay updates the strip within a frame` (canvas hash changes after two animation frames; no service call).
  - `a blank field stays reachable and keeps focus`: type a title, click the footer chip, `document.activeElement` is the footer overlay, type `x` -> footer block appears at the slot's `y` and `activeElement` is the same element.
  - `columns 2 docks the main text but not the title`: docked textarea visible with visible text colour; title overlay present.
  - `ghost state`: overlays exist on the ghost blocks; docked textarea placeholder equals the template example.
  - `policy docked-all` (set via the module's exported setter used only by tests... instead run `fieldEditMode` unit table; the panel test runs with the committed policy).
- [ ] **Step 2-4:** FAIL, implement (set `EDIT_POLICY` from Task 2), `BR` PASS, `LINT`. **Step 5: Commit** `feat: paper strip editor with in-place and docked inputs (Refs #25)`.

### Task 19: Option bar (fonts, size, width, weight, columns) and slider behaviour

**Files:** Modify `tinyprint-panel.js`, `studio/options.js` consumers, `tests/browser_studio.py`, `tests/browser_renderer.py`.

**Interfaces:** Controls generated from `printer_info.fonts`: font select; size (Roboto `number` 12-96, Tiny5 `select` of the 6 values); Roboto width slider 25-151 and weight slider 100-1000 (step 1); Tiny5 width `select` 100/200 (labels `1x`/`2x`), no weight control; `columns` select 1/2/3 shown only when the template has a `text` field; unchanged align, border, QR options. `input` on a width/weight slider requests an approximate render; `change` requests the exact render. `getDocument().options` contains `font`, `font_size`, `font_width`, `font_weight`, `align`, `border`, `columns`, `qr_size`, `qr_correction` in template mode (subset for qr mode, image options in image mode). `closingCounterHint` text goes to a non-blocking `#font-hint` line. When `usesBrowserOnlyOptions(options)` the automation-action block shows a notice instead of an action Pillow would reject.

- [ ] **Step 1: Red tests:**
  - `slider drag blocks Print until release`: dispatch `input` only on the weight slider -> `#print` disabled with the `render_approximate` text and no recipe-bearing call; dispatch `change` -> exact render, Print enabled.
  - `switching to Tiny5 restricts the controls`: size select lists exactly 8,16,24,32,40,48 (size 20 becomes 16), weight control hidden, width select offers 100 and 200; switching back restores defaults 100/400.
  - `hint never blocks`: weight 801, size 24 shows `#font-hint`, Print still enabled when otherwise valid.
  - `columns select is hidden for qr_label and visible for note`.
  - `option names are valid Python options`: every key of `getDocument().options` in each mode is in `studio_render.DEFAULTS`, and a Python `normalize_options` accepts them when `columns` is 1 and the font is Roboto.
  - `browser-only options replace the automation action with a notice` (columns 2, Tiny5 width 200).
  - `full-range sliders visibly change the strip` (bitmap hashes differ at width 25/100/151).
- [ ] **Step 2-4:** FAIL, implement, `BR` PASS, `LINT`. **Step 5: Commit** `feat: font, size, width, weight and columns controls (Refs #25)`.

### Task 20: Renderer diagnostics action

**Files:** Modify `tinyprint-panel.js` (`details#diagnostics`), `tests/browser_studio.py`.

**Interfaces:** button `#renderer-check` ("Проверить рендерер") inside `details#diagnostics`; click -> `this.renderer.selfTest()` -> `#diagnostic-data` gets the pretty-printed `SelfTestReport`, the details open, `notice` reports pass or the first failing check (error style).

- [ ] **Step 1: Red tests:** `renderer check passes in this browser` (click; `#diagnostic-data` parses to JSON with `ok === true` and five checks); `renderer check reports a font failure instead of throwing` (404 font -> `ok === false`, notice has the error class, panel still responsive); `renderer check never calls a service` (`calls` unchanged).
- [ ] **Step 2-4:** FAIL, implement, `BR` PASS. **Step 5: Commit** `feat: renderer self-check in the panel diagnostics (Refs #25)`.

### Task 21: Documentation and release notes

**Files:** Modify `docs/studio.md`, `README.md`, `custom_components/tinyprint/frontend/tinyprint-panel.js` (header comment `:1`), `custom_components/tinyprint/studio_render.py` (`preview_result` docstring); create `docs/CHANGELOG.md` (the repo has none).

- [ ] **Step 1:** Rewrite `docs/studio.md` (English): editor (paper strip, live preview, no Preview button, ghost/placeholders, columns, fonts and the options table with `font`, `font_width`, `font_weight`, `columns`, new `font_size` ranges), `print_bitmap` (fields, errors, `bitmap_sha256`, an automation example), `printer_info` additions (`entity_id`, `in_flight_states`, `fonts`), Pillow vs browser differences (automations stay on Pillow, not pixel-identical; columns 2-3 and Tiny5 width 200 are browser-only and rejected by the Pillow actions; pinning `font_path` restores the old output), the browser-only/phone requirements and the renderer self-check, "No fonts are bundled" paragraph replaced (Roboto Flex and Tiny5, OFL). Rename the existing "Logs, privacy and hardware verification" section to exactly `## Privacy and persistence`, keep its logging paragraph, and leave the persistence text to Plan 2; remove every statement that a preview exists, that examples are pre-filled, and the stale `0.2.1` title version. Update the header comment of `tinyprint-panel.js` and the `preview_result` docstring. README: mention the editor in one sentence and link to `docs/studio.md`.
- [ ] **Step 2:** `docs/CHANGELOG.md` with a `0.3.0` section: Added (live editor, columns, bundled fonts, `print_bitmap`), Changed ("default font is now bundled Roboto Flex: output and `expected_sha256` of existing automations change; pin `font_path` to keep the old output"; `font_size` range 12-96; title/heading cap `max(64, size)`), Notes (browser-only options, phone support gate).
- [ ] **Step 3:** `grep -rn "Предпросмотр\|preview" custom_components/tinyprint/frontend docs/studio.md` shows only the `preview` service documentation. `LINT`. **Commit** `docs: rewrite Studio documentation for the WYSIWYG editor (Refs #25)`.

### Task 22: MANUAL (needs the user) - phone and paper acceptance

**Files:** none. Evidence goes into the PR description.

- [ ] **Step 1: MANUAL (needs the user), phone.** Install this branch's integration on the user's HA, restart, open the TinyPrint panel in the phone browser and in the Companion app. Do: tap the diagnostics action "Проверить рендерер" -> pass condition `ok: true`; type into a title overlay -> pass: strip updates, caret on the glyphs, the page does not zoom or jump; drag the weight slider -> pass: strip changes smoothly, Print re-enables after release. Stop rule: any failed check, or typing lag over about half a second on a 384x645 label: stop, record the numbers, ask the user how to proceed.
- [ ] **Step 2: MANUAL (needs the user), paper.** With the printer on and no other client connected, the user prints: one Cyrillic label in Roboto Flex, one in Tiny5 (size 16, `1x`), and a 2-column label. Pass condition: three photographs, text legible, no missing glyphs, Tiny5 pixels uniform, columns not overlapping; software "sent" is never reported as paper output. Stop rule: any illegible or missing-glyph label: do not close the task; report the photo and the label settings.

### Task 23: Version bump to 0.3.0 and final gate

**Files:** Modify `custom_components/tinyprint/studio.py` (`PANEL_VERSION`, `:21`), `manifest.json` (`:32`), `diagnostics.py` (`:15`), `README.md`, `tests/browser_studio.py` (mock `printer_info` `"version"`, `:47`), `tests/test_ha_bridge.py` (`:400-406`).

- [ ] **Step 1:** Change `0.2.2` to `0.3.0` at all six sites. `README.md:15` is the label `## Direct local LE (0.2.2)` (when that feature shipped, not the current version): make it `## Direct local LE (since 0.2.2)` and add one line `Current version: 0.3.0.` under the README title; Plans 2 and 3 bump that line (0.3.1, 0.3.2).
- [ ] **Step 2:** `grep -rn "0\.2\.2" . --include=* -I` returns only `docs/CHANGELOG.md`, the README "since" label and historical specs/plans.
- [ ] **Step 3:** Full local gate: `PY tests/test_core.py tests/test_transport.py tests/test_studio_render.py tests/test_frame_counter.py tests/test_bridge_wire.py tests/test_att.py tests/test_bridge_config.py tests/test_bridge_link.py tests/test_bridge_jobs.py tests/test_bridge_server.py tests/test_bridge_installer.py tests/test_bridge_client.py tests/test_local_le.py tests/test_bitmap.py tests/test_fonts.py`; `HA` for every `tests/test_ha*.py` (discover with `-p "test_ha*.py"`); `BR`; `LINT`; `python tools/gen_layout_cases.py --check`; `python tools/build_fonts.py --check`. Expected: all pass; CI jobs covering each: `tests` (pytest list), `home-assistant` (`test_ha*.py` incl. layout parity), `browser` (both browser scripts), hassfest/HACS (manifest and services).
- [ ] **Step 4: Commit** `chore: bump to 0.3.0 (Refs #25)`.

---

## Self-review

**Spec coverage** (spec section -> task): Problem 1-4 -> 15-19; Architecture C1/C2/C3, panel stops calling preview/print_document, `entity_id`/`in_flight_states` -> 3, 4, 5, 17; Browser renderer points 1-6 -> 10-15; Fonts (bundle, build tool + hash CI, font table, Python defaults/options/bounds) -> 6-8; Editor surface, placeholders, Print rule, generations -> 16-19; Columns -> 12, 19; phone gate and `selfTest` -> 1, 15, 20, 22; docs -> 21; tests listed in the spec's "Documentation, tests, versioning" (except history items) -> 3-20; version bump -> 23. Print history, its tests, recorder text -> Plan 2; `waiting_for_printer` -> Plan 3.

**Consciously not covered:** History drawer, recipe/title/source fields, `history_*` services and their tests/auth tests (Plan 2). The spec's `recipe` hand-off: this plan only exposes `getDocument()`; Plan 2 builds `{v: 1, ...document}`. "Open in editor" (`setDocument`) belongs to Plan 2.

**Spec gaps decided here (flagged to the orchestrator):** (1) blank fields draw nothing, so they get "slots" and a chip row to stay reachable (Task 11/18); (2) the spec's image-layer cache key would collide when brightness changes, so the cache is keyed by `(source, rotate, width, fit)` (Task 14); (3) `README.md:15` is a feature-introduction label, a `Current version:` line is added (Task 23); (4) static path versioning replaces `?v=` because sub-modules cannot carry it (Task 17); (5) `crypto.subtle` is absent on plain-http HA, so the panel never hashes; (6) the `qr` field is always docked (no glyphs under it); (7) Tiny5 upstream is variable (BLED/JITT/ROND/wdth/wght), so the build instantiates it statically.

**Proportion check:** the plan is a task list with signatures, exact test names and values; bodies appear nowhere except `derived_size`'s rule. 
