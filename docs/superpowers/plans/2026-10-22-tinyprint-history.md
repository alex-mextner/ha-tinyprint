# TinyPrint Print History Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Record every print job from every entry point in a persistent, capped history that the Studio panel can list, reopen in the editor and reprint byte-exactly.

**Architecture:** A pure ring buffer with a deduplicated blob map (`history.py`) is wrapped by one domain-level `HistoryStore` over HA's `Store` (`history_store.py`). `StudioPrinter._async_print_job` becomes a thin wrapper that records each finished job inside the owned job task from `last_result`. Five admin services and a panel drawer sit on top. Plan 1 (Studio core) lands first and provides `bitmap.py`, `print_bitmap`, the `"bitmap"` render kind and the panel modules; Plan 3 (transport) lands after and consumes `last_result["timings"]`.

**Tech Stack:** Python 3.14, Home Assistant 2026.9.3 (`helpers.storage.Store`), Pillow 12.3.0, voluptuous, vanilla ES-module panel (no build step), pytest, `unittest` in the HA image, Playwright Chromium.

**Spec:** `docs/superpowers/specs/2026-10-22-tinyprint-studio-wysiwyg-design.md` section "Print history" (and the "Architecture" bullets for `print_bitmap`, "Documentation, tests, versioning"). Transport spec `2026-10-22-tinyprint-transport-reliability-design.md` section 1 defines `timings`. Tracked in GitHub issue #25; commits use `Refs #25`. Line numbers below are hints as of commit 28e8940 and shift after Plan 1 lands: find code by symbol.

## Global Constraints

- Store: `homeassistant.helpers.storage.Store`, key `tinyprint.history`, version 1, created once per `hass` in `async_setup`, never recreated on entry reload; `async_delay_save` with a 30 s delay; `data_func` returns a snapshot of immutable entries; `Store(..., serialize_in_event_loop=False)`. In HA 2026.9.3 that flag means `data_func` itself runs in a separate thread, so it must only read an already-built immutable snapshot.
- One domain-level `HistoryStore`, loaded before any print service or button is usable. All mutations run on the event loop. It is NOT stored under `hass.data["tinyprint"]` (tests assert that dict is empty and it maps entry ids to printers); use `hass.data["tinyprint_history"]`.
- Ring caps: 200 entries and 5 MiB of serialised data (entries plus blobs); sizes computed once at insert, running total kept; the byte cap takes precedence. Each bitmap (`bitmap_z`, `thumb`) is stored once, keyed by `bitmap_sha256`, dropped with its last referencing entry.
- Entry fields: `id` (`uuid4().hex`, validated on every input), `created` (UTC ISO-8601), `device_id`, `source` (`service`|`studio`|`button`|`history`), `status` (`sent`|`failed`|`delivery_uncertain`|`cancelled`), `phase`, `rows`, `ink_percent`, `bitmap_sha256`, `recipe`, `title` (at most 80 chars), `job_id`, `timings` (object or null). `bitmap_z` = zlib + base64 of the packed bitmap; `thumb` = base64 PNG at most 96 dots wide, made inline in the job task.
- Status precedence (one boundary, the "first raster write call"): no raster write and cancelled is `cancelled`; no raster write otherwise is `failed`; any failure, cancel or fault after the first raster write call is `delivery_uncertain`; `sent` when the job ended successfully. A job from a legacy (`print_text`, `print_qr`, `print_image`; `source` `service`) action that failed or was cancelled before the first raster write is NOT recorded. Failures before it from `print_bitmap`, `print_document`/`print_template`, reprints and the button are recorded.
- A history write failure is logged without payload and never changes the job result. Logs and diagnostics stay payload-free.
- `print_bitmap` new optional fields: `recipe` (JSON object, at most 64 KiB, never interpreted by the server), `title` (at most 80 chars), `source` (`studio`|`service`, default `service`). `recipe` is built by the server through the shared `normalize_options` for text, template and QR actions, with fully resolved options and `v: 1`; by the panel for Studio; `null` for images and recipe-less `print_bitmap` calls.
- Admin-only services via `async_register_admin_service`: `history_list` (`limit` default 50, `offset`; response only), `history_get` (response only), `history_delete`, `history_clear`, `history_reprint` (`device_id`, `id`). `history_reprint` decompresses with an output bound of `2048 * 48 + 1` bytes and re-validates rows and decoded length through `bitmap.py` before any radio activity; it never re-renders.
- Panel: every history field is written with `textContent` or property assignment, never `innerHTML`; `thumb` is used only if it matches `^[A-Za-z0-9+/=]+$` and goes into a `data:image/png;base64,` URL built by the panel. Reprint of a `delivery_uncertain` entry warns that the first copy may have printed.
- Privacy documentation must state: content persists in `/config/.storage/tinyprint.history` and HA backups; `call_service` events carry the full `service_data` into the recorder, which drops event data above 32768 bytes with a logged error (small `print_bitmap` calls, roughly 500 rows or fewer, are stored; larger ones are not, at the cost of an error log line); the `recorder: exclude: event_types: [call_service]` snippet; `history_clear` does not purge backups or recorder rows; history of a removed printer is not deleted automatically; non-admin users can create entries through the legacy actions and can evict older ones (accepted).
- CI facts: the `tests` job runs an EXPLICIT pytest file list without Home Assistant or voluptuous installed (Pillow 12.3.0, qrcode 8.2, aiohttp only), so `history.py` must import neither; the `home-assistant` job runs `tests/test_ha*.py` with `unittest` discover in the 2026.9.3 image (new `test_ha_*.py` files are picked up automatically); the `browser` job runs `python tests/browser_studio.py`; also `ruff check . && ruff format --check .`, hassfest, HACS.
- Version at start of this plan: 0.3.0 (Plan 1). This plan ends at 0.3.1.

## Review Focus

1. Two jobs for one printer: the second is rejected as busy and must leave no history entry and not disturb the first one's entry (Task 5).
2. A hand-edited or truncated `.storage/tinyprint.history` (malformed entries, bool rows, entry whose blob is gone, oversize file): HA still starts, bad entries are dropped, nothing crashes; an entry with a missing blob stays listed but not reprintable (Tasks 3, 4).
3. Reprint of an entry whose printer device is unknown or not loaded, and reprint while another job runs: a clear error, nothing printed, no new entry for an unknown device (Task 8).
4. Hostile or awkward titles: newlines, very long text, markup, RTL marks. The stored title is one line, at most 80 characters; markup stays inert text everywhere (Tasks 3, 9).
5. History storage failing (store raises, disk full): the print result, `last_result` and the sensor state are unchanged and the log line carries no label text (Task 5).

---

## File Structure

| File | Responsibility |
| --- | --- |
| `custom_components/tinyprint/transport.py` | `send_bitmap(progress=...)` flags the first raster write (Task 1) |
| `custom_components/tinyprint/studio_render.py` | add `ink_percent`, `thumbnail_png` (Task 2) |
| `custom_components/tinyprint/history.py` (new) | pure: `HistoryRing`, `job_status`, `title_for`, `recipe_for`, `check_recipe`, `pack_bitmap`, `unpack_bitmap`, `summary`, `detail` (Task 3) |
| `custom_components/tinyprint/history_store.py` (new) | `HistoryStore` over `Store`, `async_setup_history`, `HISTORY_KEY` (Task 4) |
| `custom_components/tinyprint/studio_runtime.py` | `async_print(source=)`, recording wrapper, `history=` ctor kwarg (Tasks 1, 5) |
| `custom_components/tinyprint/studio.py` | `print_bitmap` optional fields (Task 6); calls history service registration |
| `custom_components/tinyprint/studio_history.py` (new) | the five admin services (Tasks 7, 8) |
| `frontend/studio/history.js`, `frontend/studio/title.js` (new), `frontend/tinyprint-panel.js` | drawer, title derivation, `setDocument` (Task 9) |
| `tests/test_history.py` (new), `tests/test_ha_history.py` (new), `tests/browser_studio.py` | tests |

---

### Task 1: The first-raster-write boundary (`raster_started`)

**Files:**
- Modify: `custom_components/tinyprint/transport.py` (`send_bitmap`), `custom_components/tinyprint/studio_runtime.py` (`StudioPrinter.begin`, the `send_bitmap` call in `_async_print_job`), `custom_components/tinyprint/local_le.py` (`run_local_job`), `custom_components/tinyprint/bridge_client.py` (`_merge`)
- Test: `tests/test_transport.py`, `tests/test_local_le.py`, `tests/test_bridge_client.py`

**Interfaces:**
- Produces (consumed by Task 5 and by Plan 3):
  `send_bitmap(client, bitmap, height, *, ..., progress: dict[str, Any] | None = None) -> dict`. `progress` is a caller-owned mutable dict: `send_bitmap` sets `progress["raster_started"] = True` and `stats["raster_started"] = True` immediately before the first write of the job stream in the packet loop (the `for offset in range(0, len(stream), packet_size)` loop; the leading `get_state_frame()` precheck write and `start_notify` do not count; a write that raises still counts). `stats["raster_started"]` is `False` until then. Because the caller owns `progress`, the flag survives an exception. Plan 3 may put its own timeline marks into the same dict.
  After this task every path leaves `last_result["raster_started"]` as a `bool`: `StudioPrinter.begin()` initialises it `False`; native/proxy pass `progress=self.last_result`; `run_local_job` passes `progress=result` and resets `"raster_started": False` in its initial `result.update({...})`; for the bridge `_merge` sets `result["raster_started"] = result["bytes_written"] > len(get_state_frame())` (9 bytes; the wire format is unchanged).

- [ ] **Step 1: Write the failing tests**

In `tests/test_transport.py` (use the existing `FakeClient`/`KW`; `fail_at=N` raises on the N-th write, so `fail_at=0` fails the precheck state request and `fail_at=1` fails the first stream write):
```python
def test_raster_started_stays_false_when_the_precheck_write_fails():
    progress = {}
    with pytest.raises(RuntimeError):
        asyncio.run(t.send_bitmap(FakeClient(fail_at=0), bytes(48), 1, progress=progress, **KW))
    assert progress.get("raster_started", False) is False

def test_raster_started_is_true_when_the_first_stream_write_raises():
    progress = {}
    with pytest.raises(RuntimeError):
        asyncio.run(t.send_bitmap(FakeClient(fail_at=1), bytes(48), 1, progress=progress, **KW))
    assert progress["raster_started"] is True

def test_a_fault_before_the_job_means_no_raster_started():  # model on :239 fault test
    # printer reports a FAULTS flag in the precheck reply -> PrinterFaultError
    assert progress.get("raster_started", False) is False
    assert not any(w != GET_STATE for w in client.writes)

def test_successful_send_reports_raster_started_in_stats():
    assert stats["raster_started"] is True and progress["raster_started"] is True
```
In `tests/test_local_le.py` (models: the failing-at-discovery test near `:75-80` and the failing-write test near `:91-100`): `test_raster_started_false_when_the_link_fails_before_any_write` asserts `p.last_result["raster_started"] is False`; `test_raster_started_true_when_a_stream_write_fails` asserts `True`.
In `tests/test_bridge_client.py` (use its scripted fake bridge): `test_raster_started_is_derived_from_bytes_beyond_the_state_request` with terminal views whose `bytes_written` is `0`, `9` (state request only) and `10`: expected `False`, `False`, `True`, on both a sent and a failed job.

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_transport.py tests/test_local_le.py tests/test_bridge_client.py -q`
Expected: FAIL (`TypeError: unexpected keyword argument 'progress'`, `KeyError: 'raster_started'`). The `TypeError` is an acceptable red for the transport tests; the local and bridge tests must fail on the missing key.

- [ ] **Step 3: Implement** the Interfaces block above in the four files. In `send_bitmap` set the flag on the line before `await client.write_gatt_char(WRITE_UUID, chunk, response=False)` in the packet loop.

- [ ] **Step 4: Run the full `tests` job list**

Run: the exact `python -m pytest ...` list from `.github/workflows/validate.yml` (`tests` job), then `ruff check . && ruff format --check .`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add custom_components/tinyprint tests
git commit -m "feat: expose the first raster write as last_result.raster_started (Refs #25)"
```

---

### Task 2: Thumbnail and ink helpers

**Files:**
- Modify: `custom_components/tinyprint/studio_render.py`
- Test: `tests/test_studio_render.py`

**Interfaces:**
- Produces: `ink_percent(data: bytes, rows: int) -> float` (same formula as `preview_result`: `round(100 * ones / (384 * rows), 2)`; `preview_result` now calls it); `thumbnail_png(data: bytes, rows: int) -> bytes` (PNG of the packed bitmap, ink = black, area-averaged down by 4 so it is 96 dots wide and `ceil(rows / 4)` tall).

- [ ] **Step 1: Write the failing tests** in `tests/test_studio_render.py`:
```python
def test_ink_percent_matches_the_preview_value():
    image = render_document({"kind": "qr", "text": "x"})
    data, rows = bitmap(image)
    assert ink_percent(data, rows) == preview_result(image)["ink_percent"]

def test_thumbnail_is_96_wide_and_keeps_the_ink():
    data = bytes(48 * 10) + b"\xff" * (48 * 10)       # 10 blank rows, then 10 solid rows
    png = Image.open(io.BytesIO(thumbnail_png(data, 20)))
    assert png.size == (96, 5)
    assert png.convert("L").getpixel((10, 0)) == 255     # blank rows stay white
    assert png.convert("L").getpixel((10, 4)) == 0       # solid ink stays black

def test_thumbnail_of_a_tall_bitmap_keeps_the_aspect_ratio():
    assert Image.open(io.BytesIO(thumbnail_png(bytes(48 * 2048), 2048))).size == (96, 512)
```
- [ ] **Step 2:** `python -m pytest tests/test_studio_render.py -q` — expected FAIL (`ImportError`).
- [ ] **Step 3: Implement** both functions: `Image.frombytes("1", (384, rows), data.translate(_INVERT))` (the module's existing ink-inversion table), `.convert("L").reduce(4)`, `save(format="PNG", optimize=True)`.
- [ ] **Step 4:** same command — expected PASS; `ruff check . && ruff format --check .`
- [ ] **Step 5: Commit**

```bash
git add custom_components/tinyprint/studio_render.py tests/test_studio_render.py
git commit -m "feat: ink percent and thumbnail helpers for history (Refs #25)"
```

---

### Task 3: Pure history core (`history.py`)

**Files:**
- Create: `custom_components/tinyprint/history.py`, `tests/test_history.py`
- Modify: `.github/workflows/validate.yml` (append `tests/test_history.py` to the `tests` job pytest list)

**Interfaces:**
- Consumes: `bitmap.ROW_BYTES`, `bitmap.MAX_BITMAP_BYTES`, `bitmap.check_rows`, `bitmap.BitmapError` (Plan 1, C1); `studio_render.normalize_options`.
- Imports only the standard library, `.bitmap`, `.studio_render` (no `homeassistant`, no `voluptuous`).
- Produces (Tasks 4-8 rely on these exact names):
```python
MAX_ENTRIES = 200; MAX_BYTES = 5 * 1024 * 1024; SAVE_DELAY = 30; MAX_TITLE = 80; MAX_RECIPE_BYTES = 64 * 1024
SOURCES = ("service", "studio", "button", "history")
STATUSES = ("sent", "failed", "delivery_uncertain", "cancelled")
class HistoryError(ValueError)
def job_status(*, succeeded: bool, cancelled: bool, raster_started: bool) -> str
def title_for(kind: str, payload: Any) -> str          # kind in "text","qr","image","document","bitmap"; payload as passed to async_print
def recipe_for(kind: str, payload: Any, font_size: int) -> dict[str, Any] | None
def check_recipe(value: object) -> dict[str, Any]       # raises ValueError unless a dict whose compact JSON is <= MAX_RECIPE_BYTES
def pack_bitmap(data: bytes) -> str                     # base64(zlib(data))
def unpack_bitmap(entry: Mapping, blob: Mapping | None) -> tuple[int, bytes]   # raises HistoryError
def summary(entry, blob) -> dict; def detail(entry, blob) -> dict
class HistoryRing:
    def __init__(self, max_entries: int = MAX_ENTRIES, max_bytes: int = MAX_BYTES) -> None
    @classmethod
    def from_data(cls, data: object, *, max_entries=MAX_ENTRIES, max_bytes=MAX_BYTES) -> "HistoryRing"
    dropped_on_load: int
    def has_blob(self, sha: str) -> bool
    def add(self, entry: dict[str, Any], blob: dict[str, str] | None) -> None   # blob None only if has_blob(entry["bitmap_sha256"])
    def get(self, entry_id: str) -> dict | None
    def blob(self, sha: str) -> dict[str, str] | None
    def page(self, limit: int, offset: int) -> tuple[list[dict], int]           # newest first, plus total
    def delete(self, entry_id: str) -> bool
    def clear(self) -> int
    def snapshot(self) -> dict[str, Any]     # {"entries": [...oldest first], "blobs": {sha: {"bitmap_z", "thumb"}}}
    total_bytes: int
    def __len__(self) -> int
```
Algorithms the tests do not determine: `add` appends the entry, increments its blob reference count, then evicts the oldest entries while `len > max_entries or total_bytes > max_bytes` and more than one entry remains (the new entry is never evicted by its own insert; a blob goes when its count reaches 0). Sizes are `len(json.dumps(x, separators=(",", ":")).encode())` computed once at insert. The snapshot object is rebuilt eagerly (shallow copies of the entry list and blob map) after every mutation and `snapshot()` only returns it, so a thread reading it never sees a half-mutated state; entries and blobs are never mutated after insert. `unpack_bitmap` order: `check_rows(entry["rows"])`; strict `b64decode(validate=True)`; `zlib.decompressobj().decompress(raw, MAX_BITMAP_BYTES + 1)` and require `eof`, no `unused_data`, length at most `MAX_BITMAP_BYTES` and exactly `rows * ROW_BYTES`; finally `sha256(data) == entry["bitmap_sha256"]`. `summary` = `id, created, device_id, source, status, phase, rows, ink_percent, bitmap_sha256, title, job_id, timings` plus `thumb` (or `None`), `has_recipe` (`recipe is not None`), `reprintable` (blob present); `detail` = the whole entry plus `bitmap_z` and `thumb`. `from_data` keeps an entry only if: `id` matches `^[0-9a-f]{32}$`, `status` and `source` are in the enums, `rows` is an int (not bool) in 1..2048, `bitmap_sha256` matches `^[0-9a-f]{64}$`, `title` is a str of at most 80 chars, `created` is a str; blobs must be dicts with str `bitmap_z` and a `thumb` matching `^[A-Za-z0-9+/=]+$`, unreferenced blobs are dropped, an entry whose blob is missing stays; caps are enforced after loading. `title_for`: first non-blank line of the text (for `document`: text field, or template `fields["title"]`, else `fields["text"]`, else `f"Template {name}"`), `" ".join(text.split())`, clipped to 80 chars with a trailing `…`; `"QR code"` for qr, `"Image"` for image (never a path), `payload.get("title")` or `"Bitmap"` for `bitmap`, `"Print"` otherwise or when the text is blank. `recipe_for`: `text` -> `{"v": 1, "kind": "text", "text": payload, "options": normalize_options({"font_size": font_size})}`; `qr` -> `{"v": 1, "kind": "qr", "text": payload, "options": normalize_options()}`; `document` of kind text/qr/template -> `{"v": 1, **document-without-options, "options": normalize_options(document.get("options"))}`; `image` kinds and `ValueError` from `normalize_options` -> `None`; `bitmap` -> `payload.get("recipe")` unchanged.

- [ ] **Step 1: Write the failing tests** in `tests/test_history.py` (load modules with the stub-package pattern at the top of `tests/test_bridge_client.py`; build entries with a helper `entry(i, sha=None, status="sent")`):
  - `test_ring_keeps_the_newest_200_entries`: add 250 small entries; `len == 200`, oldest id gone, `page(5, 0)` is newest first, total 200.
  - `test_byte_cap_wins_over_the_entry_cap`: 200 entries each with its own ~40 KB blob; `len(ring) < 200`, `total_bytes <= MAX_BYTES`, newest entry present.
  - `test_a_shared_bitmap_is_stored_once`: three entries with one sha, blob passed once; `total_bytes == sum(entry sizes) + blob size` and `snapshot()["blobs"]` has one key.
  - `test_blob_outlives_the_eviction_of_one_of_its_entries` (`max_entries=2`): A(X), B(Y), C(X); adding C evicts A, `has_blob(X)` true; adding D(Z) evicts B, `has_blob(Y)` false, `has_blob(X)` true.
  - `test_delete_and_clear_release_blobs_and_bytes`: `delete` returns False for an unknown id; deleting the last referencing entry drops the blob; `clear()` returns the count and leaves `total_bytes == 0`.
  - `test_snapshot_round_trips_through_from_data`.
  - `test_from_data_drops_malformed_entries_and_orphan_blobs`: bad id, unknown status, `rows=True`, `rows=2049`, bad `thumb` regex, title of 81 chars are dropped (`dropped_on_load` counts them); an entry whose blob is missing is kept with `summary(...)["reprintable"] is False`; an unreferenced blob disappears; `from_data(None)` and `from_data([])` are empty.
  - `test_from_data_enforces_the_caps_on_an_oversized_file`.
  - `test_a_snapshot_is_unaffected_by_later_mutations`: take `snapshot()`, add and delete entries, the old object is unchanged.
  - `test_job_status_matrix`, parametrised over all 8 combinations: succeeded -> `sent`; failed/cancelled without raster -> `failed` / `cancelled`; any non-success with raster -> `delivery_uncertain` (cancelled or not).
  - `test_summary_has_no_bitmap_or_recipe_and_flags_the_actions`: keys exclude `bitmap_z` and `recipe`; `has_recipe` False for `recipe=None`; `detail` has `recipe` and `bitmap_z`.
  - `test_title_is_one_clipped_line`: parametrised: `"  a\n\nb "` -> `"a"` (first non-blank line); 200 chars -> 80 chars ending `…`; `"<img src=x onerror=alert(1)>"` unchanged; `"\u202eabc\n"` one line; QR -> `"QR code"`; image document with a path -> `"Image"`; template without title or text -> `"Template note"`; `bitmap` with and without title.
  - `test_recipe_for_resolves_options_and_marks_the_version`: text recipe `["options"] == normalize_options({"font_size": 24})`, `["v"] == 1`; a template document keeps `template` and `fields`; image -> `None`; `font_size=9999` -> `None`; bitmap passes the supplied dict through.
  - `test_check_recipe_boundary`: a dict whose compact JSON is exactly `64 * 1024` bytes passes, one byte more raises `ValueError`; a list, a str and `None` raise.
  - `test_unpack_round_trips_and_rejects_bad_blobs`: round trip returns `(rows, data)`; each of these raises `HistoryError`: `rows=True`, `rows=2049`, `rows` not matching the decoded length, a `zlib.compress(bytes(98305))` blob (oversized output), valid blob plus trailing bytes, invalid base64 (`"!!!"`), one flipped bit in the decompressed data (sha mismatch), `blob=None`.

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_history.py -q`
Expected: FAIL (`ModuleNotFoundError: ... history`).

- [ ] **Step 3: Implement `history.py`** to the Interfaces block. Add `tests/test_history.py` to the pytest file list in the `tests` job of `.github/workflows/validate.yml`.

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/test_history.py -q && ruff check . && ruff format --check .`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add custom_components/tinyprint/history.py tests/test_history.py .github/workflows/validate.yml
git commit -m "feat: pure print history ring with deduplicated blobs (Refs #25)"
```

---

### Task 4: `HistoryStore` over HA `Store`, created once per hass

**Files:**
- Create: `custom_components/tinyprint/history_store.py`, `tests/test_ha_history.py`
- Modify: `custom_components/tinyprint/studio.py` (`async_register_studio_services`: create and load the store before registering anything), `custom_components/tinyprint/__init__.py` (`async_setup_entry`: pass `history=` to the printer; the constructor kwarg itself arrives in Task 5)

**Interfaces:**
- Consumes: `history.HistoryRing`, `history.SAVE_DELAY`, `history.pack_bitmap`, `studio_render.thumbnail_png`, `studio_render.ink_percent`.
- Produces:
```python
HISTORY_KEY = "tinyprint_history"
class HistoryStore:
    def __init__(self, hass: HomeAssistant) -> None   # Store(hass, 1, "tinyprint.history", serialize_in_event_loop=False)
    async def async_load(self) -> None
    def record(self, *, device_id: str | None, source: str, status: str, phase: str, bitmap: bytes,
               rows: int, recipe: dict | None, title: str, job_id: str, timings: dict | None) -> str  # returns the new entry id
    def page(self, limit: int, offset: int) -> dict[str, Any]   # {"entries": [summary...], "total": int}
    def get(self, entry_id: str) -> dict | None                 # history.detail
    def blob_for(self, entry: Mapping) -> Mapping | None
    def delete(self, entry_id: str) -> bool
    def clear(self) -> int
async def async_setup_history(hass: HomeAssistant) -> HistoryStore   # idempotent; stores at hass.data[HISTORY_KEY]
```
`record` computes `bitmap_sha256 = sha256(bitmap)`; builds the blob (`pack_bitmap`, base64 of `thumbnail_png`) only if the ring lacks that sha; `ink_percent` from `ink_percent`; `created = datetime.now(timezone.utc).isoformat(timespec="seconds")`; `id = uuid4().hex`; then `ring.add` and `store.async_delay_save(lambda: ring.snapshot(), SAVE_DELAY)`. `delete` and `clear` save with delay 0 (an explicit user act). `async_load` logs the number of entries dropped (count only).

- [ ] **Step 1: Write the failing tests** in `tests/test_ha_history.py` (`unittest.IsolatedAsyncioTestCase` on a real `HomeAssistant(tmpdir)`, same skeleton as `tests/test_ha_studio.py`; helper writes `<tmp>/.storage/tinyprint.history` as `{"version": 1, "minor_version": 1, "key": "tinyprint.history", "data": {...}}`):
  - `test_history_is_created_once_and_survives_a_second_setup`: call `async_setup` twice; `hass.data["tinyprint_history"]` is the same object.
  - `test_nothing_is_usable_until_history_is_loaded`: patch `Store.async_load` to wait on an event; start `async_setup`; while blocked `has_service("tinyprint", "print_text")` is False and so is `history_list`; after release both are True.
  - `test_the_first_record_does_not_overwrite_stored_history`: pre-write a file with two valid entries; set up; `record(...)` once; `page(50, 0)["total"] == 3`.
  - `test_a_recorded_entry_survives_a_restart`: record, then stop hass (`await hass.async_stop()` fires the final write; if that is awkward fire `EVENT_HOMEASSISTANT_FINAL_WRITE` and `async_block_till_done()`); read the file with `json`: it has the entry and its blob; start a second `HomeAssistant` on the same directory, `async_setup`, `page(...)["total"] == 1`.
  - `test_corrupt_stored_data_starts_empty_without_crashing`: file whose `data` is `{"entries": "x", "blobs": 5}` -> empty store, setup succeeds.
  - `test_data_func_snapshot_is_not_mutated_by_later_records`: capture the callable passed to `async_delay_save` (patch it), call it, then `record` again; the first result is unchanged.
  - `test_a_repeated_bitmap_adds_no_blob_bytes`: record the same bytes twice; the second record grows `ring.total_bytes` by exactly the entry size.
  - `test_delete_and_clear_save_immediately`: `async_delay_save` is called with delay `0`.

- [ ] **Step 2: Run to verify they fail**

Run: `docker run --rm --entrypoint /bin/sh -v "$PWD:/workspace" -w /workspace ghcr.io/home-assistant/home-assistant:2026.9.3 -c 'python -m pip install --target /tmp/test-deps --no-deps qrcode==8.2 && PYTHONPATH=/workspace:/workspace/bridge:/tmp/test-deps python -m unittest discover -s tests -p "test_ha_history.py" -v'`
Expected: FAIL (`ModuleNotFoundError: ... history_store`). Covered in CI by the `home-assistant` job (no workflow change needed).

- [ ] **Step 3: Implement** `history_store.py`; in `async_register_studio_services` (after the `DATA_KEY` guard, before `async_register_admin_service` calls) `await async_setup_history(hass)`; in `async_setup_entry` pass `history=hass.data[HISTORY_KEY]`.

- [ ] **Step 4: Run to verify they pass**

Run: the Step 2 command, then the `tests/test_ha*.py` run for regressions: same command with `-p "test_ha*.py"`.
Expected: PASS. (`async_setup_entry` will fail on the unknown `history` kwarg until Task 5; if the full discovery run is red only for that reason, make Task 5's constructor kwarg land in the same commit.)

- [ ] **Step 5: Commit**

```bash
git add custom_components/tinyprint tests/test_ha_history.py
git commit -m "feat: persistent HistoryStore created once per hass (Refs #25)"
```

---

### Task 5: Record every job inside the owned job task

**Files:**
- Modify: `custom_components/tinyprint/studio_runtime.py`, `custom_components/tinyprint/button.py`
- Test: `tests/test_ha_history.py`

**Interfaces:**
- Consumes: Task 1 (`last_result["raster_started"]`), Tasks 3-4.
- Produces (Task 6-8 and Plan 3 rely on it):
  - `StudioPrinter.__init__(self, *args, history: HistoryStore | None = None, **kwargs)`; `None` means no recording (printers built directly in tests).
  - `StudioPrinter.async_print(self, kind: str, payload: Any, font_size: int = 24, *, source: str = "service") -> dict` overriding the base (same busy check `"Printer is busy; wait for the current job"`, same `run_owned_job`). The base `TinyPrintPrinter` is untouched (it is still used by `tests/test_ha.py`).
  - `StudioPrinter._async_print_job(self, kind, payload, font_size, source)` is a thin wrapper: the previous body is renamed `_print_job(kind, payload, font_size)` and additionally stores the rendered `(data, rows)` in `self._job_bitmap` right after the render; the wrapper runs `_print_job`, and on success or on any `BaseException` (including `CancelledError`, re-raised afterwards) calls `self._record_job(kind, payload, font_size, source, succeeded=..., cancelled=...)`, then clears `_job_bitmap`. It therefore runs after `phase("sent")` / `failed()`, inside the task created by `run_owned_job`.
  - `_record_job`: returns without recording when `history is None` or `_job_bitmap is None` (a job that failed while rendering has no bitmap and is not recorded), or when the status is `failed` or `cancelled`, `source == "service"` and `kind in ("text", "qr", "image")`. Otherwise `history.record(device_id=<registry lookup: dr.async_get(hass).async_get_device(identifiers={(DOMAIN, self.address)}) id or None>, source, status=job_status(...), phase=last_result.get("failure_phase") or last_result["phase"], bitmap, rows, recipe=recipe_for(kind, payload, font_size), title=title_for(kind, payload), job_id=last_result["job_id"], timings=last_result.get("timings") if it is a dict else None)`. Any `Exception` from it is caught and logged as `"TinyPrint history write failed (%s); job result unchanged"` with the exception type name only.
  - `button.py` passes `source="button"`. Legacy handlers in `__init__.py` and `print_document` keep the default `"service"` (the existing `assert_awaited_once_with("document", {...}, 24)` test must stay green).
  - Plan 3 contract: `last_result["timings"]` must be final before the wrapper records, which holds when Plan 3 finalises it in `phase()` / `failed()`.

- [ ] **Step 1: Write the failing tests** in `tests/test_ha_history.py` (`StudioPrinter(hass, "one", ADDRESS, "Test", None, history=store)`; patch `_render`, `bluetooth.async_ble_device_from_address`, `establish_connection` and `send_bitmap` as `tests/test_ha_studio.py` does; patch `custom_components.tinyprint.studio_runtime.dr.async_get`; the fake `send_bitmap(client, bitmap, rows, *, progress, **kw)` sets `progress["raster_started"] = True` before raising/returning, which is what the real one does):
  - `test_status_follows_the_raster_boundary`: success -> `sent`; connect failure -> `failed`; failure after `raster_started` -> `delivery_uncertain`; cancel before raster -> `cancelled`; cancel after raster -> `delivery_uncertain`. Source for these: `print_document`-style `("document", ...)` call so pre-raster outcomes are recorded.
  - `test_legacy_action_failing_before_raster_is_not_recorded_but_after_raster_is`: `async_print("text", ...)` with a connect failure -> `page(...)["total"] == 0` and the call still raised `HomeAssistantError`; the same call cancelled before raster -> 0; failing after raster -> 1 entry `delivery_uncertain`; `("text", ..., source="button")` failing before raster -> recorded as `failed`; `("bitmap", {...})` with default source failing before raster -> recorded.
  - `test_entry_carries_the_label_data`: success of `async_print("text", "Hello\nworld")`: `title == "Hello"`, `recipe["v"] == 1` and `recipe["options"] == normalize_options({"font_size": 24})`, `rows`, `bitmap_sha256 == sha256(bitmap)`, `ink_percent`, `job_id == last_result["job_id"]`, `source == "service"`, `device_id` from the patched registry, `timings is None`; a QR job has `title "QR code"`; an image job has `recipe None`.
  - `test_timings_are_stored_only_when_an_object`: a fake `send_bitmap` that puts `last_result["timings"] = {"total": 1.5}` -> stored as is; `"timings": "x"` -> `None`.
  - `test_a_busy_rejection_records_nothing`: hold job 1 open, job 2 raises `ServiceValidationError`; exactly one entry after job 1 ends.
  - `test_history_failure_never_changes_the_job_result`: `store.record` patched to raise `OSError("PRIVATE CONTENT")`; the job returns its normal result, `printer.status == "sent"`, and `assertLogs` output contains the warning and not `PRIVATE CONTENT`.
  - `test_a_cancelled_job_is_recorded_inside_the_task`: cancel through `printer.async_cancel()` after the fake raster write; the entry exists when `async_cancel` returns.
  - `test_a_reprinted_bitmap_shares_its_blob`: two jobs with identical bytes -> `snapshot()["blobs"]` has one key and two entries.

- [ ] **Step 2: Run to verify they fail** — Step 2 command of Task 4. Expected: FAIL (`TypeError: unexpected keyword argument 'history'` / `'source'`).

- [ ] **Step 3: Implement** per Interfaces; add `from homeassistant.helpers import device_registry as dr` and `from .const import DOMAIN` imports where missing.

- [ ] **Step 4: Run** the HA discovery (`-p "test_ha*.py"`) and the `tests` job list. Expected: PASS, existing tests untouched.

- [ ] **Step 5: Commit**

```bash
git add custom_components/tinyprint tests/test_ha_history.py
git commit -m "feat: record every print job in history from the owned job task (Refs #25)"
```

---

### Task 6: `print_bitmap` stores `recipe`, `title`, `source`

**Files:**
- Modify: `custom_components/tinyprint/studio.py` (`print_bitmap` schema and handler), `custom_components/tinyprint/services.yaml`, `custom_components/tinyprint/strings.json`, `custom_components/tinyprint/translations/en.json`, `custom_components/tinyprint/translations/ru.json`
- Test: `tests/test_ha_history.py`

**Interfaces:**
- Consumes: Plan 1's `print_bitmap` (fields `device_id`, `rows`, `bitmap_b64`; handler calls `printer.async_print("bitmap", {"rows": int, "bitmap": bytes})`); `history.check_recipe`, `history.MAX_TITLE`.
- Produces: schema additions `vol.Optional("recipe"): check_recipe`, `vol.Optional("title"): vol.All(str, vol.Length(max=80))`, `vol.Optional("source", default="service"): vol.In(["studio", "service"])`. The handler now calls `printer.async_print("bitmap", {"rows": rows, "bitmap": data, "recipe": call.data.get("recipe"), "title": call.data.get("title")}, 24, source=call.data["source"])`. The `"bitmap"` payload therefore has optional `recipe` and `title` keys (Plan 1's `_render` branch ignores them). The result shape (including `bitmap_sha256`) is unchanged.

- [ ] **Step 1: Write the failing tests** (patch the printer with an `AsyncMock`, as `test_explicit_device_routes_only_matching_printer` does):
  - `test_print_bitmap_forwards_recipe_title_and_source`: `source="studio"`, `title="Заметка"`, `recipe={"v": 1, "kind": "text", "text": "x"}` reach `async_print` as the payload keys and `source="studio"`; without the optional fields `source == "service"` and `recipe`/`title` are `None`.
  - `test_print_bitmap_optional_field_boundaries`: title of 80 chars accepted, 81 rejected; `source="button"` and `source="history"` rejected; a recipe of exactly 64 KiB compact JSON accepted, one byte more rejected, a list/str recipe rejected; every rejection raises before `async_print` is awaited.
  - `test_print_bitmap_end_to_end_records_the_panel_recipe`: real `StudioPrinter` with `history=store`, patched transport; entry has `source == "studio"`, `title`, the exact recipe dict, and `status == "sent"`; a call without recipe stores `recipe None` and `title "Bitmap"`.
- [ ] **Step 2:** run Task 4's Step 2 command. Expected: FAIL (`extra keys not allowed @ data['recipe']`).
- [ ] **Step 3: Implement.** Add the three fields to the `print_bitmap` entries in `services.yaml` (selector `object` for `recipe`, `text` for `title`, `select` with `studio`/`service` for `source`), `strings.json`, `translations/en.json`, `translations/ru.json` (name + description each; `ru` in Russian).
- [ ] **Step 4:** run `-p "test_ha*.py"`; PASS. hassfest in CI validates the metadata.
- [ ] **Step 5: Commit**

```bash
git add custom_components/tinyprint tests/test_ha_history.py
git commit -m "feat: print_bitmap accepts recipe, title and source for history (Refs #25)"
```

---

### Task 7: Admin services `history_list`, `history_get`, `history_delete`, `history_clear`

**Files:**
- Create: `custom_components/tinyprint/studio_history.py`
- Modify: `custom_components/tinyprint/studio.py` (call the registration), `services.yaml`, `strings.json`, `translations/{en,ru}.json`
- Test: `tests/test_ha_history.py`

**Interfaces:**
- Produces: `async def async_register_history_services(hass: HomeAssistant, store: HistoryStore, resolve_printer: Callable[[HomeAssistant, str], Any]) -> None`, called at the end of `async_register_studio_services` with `store=hass.data[HISTORY_KEY]` and `resolve_printer=_printer` (avoids a circular import; Task 8 uses `resolve_printer`). All five services are registered with `async_register_admin_service`.
  - `history_list`: schema `{vol.Optional("limit", default=50): vol.All(int, vol.Range(min=1, max=200)), vol.Optional("offset", default=0): vol.All(int, vol.Range(min=0))}` with a leading validator that raises `vol.Invalid` for `bool` (bool is an `int`), `SupportsResponse.ONLY`, returns `store.page(limit, offset)`.
  - `history_get`: `{vol.Required("id"): vol.Match(r"^[0-9a-f]{32}$")}`, `ONLY`, returns `store.get(id)` or raises `ServiceValidationError("History entry not found")`.
  - `history_delete`: same schema, `SupportsResponse.NONE`, unknown id raises `ServiceValidationError`.
  - `history_clear`: empty schema, `NONE`.

- [ ] **Step 1: Write the failing tests:**
  - `test_history_list_hides_payload_and_pages_newest_first`: 3 entries (one with `recipe None`, one whose blob was removed via a hand-edited store file); response `total == 3`; no entry has `bitmap_z` or `recipe` keys; `has_recipe` and `reprintable` booleans are right; `limit=1, offset=1` returns the middle entry; `limit=0`, `limit=201`, `offset=-1`, `limit=True` and `offset=True` raise (the schema rejects bool explicitly, `vol.All(int, ...)` alone accepts it).
  - `test_history_get_returns_recipe_and_bitmap_z_and_validates_the_id`: full entry; `id="../../x"`, a 31-char id and an unknown valid id raise.
  - `test_history_delete_removes_one_entry_and_clear_removes_all`: a second delete of the same id raises; clear empties the list and the blob map.
- [ ] **Step 2:** run Task 4's Step 2 command. Expected: FAIL (`ServiceNotFound: history_list`).
- [ ] **Step 3: Implement.** Metadata for all four in `services.yaml` (Russian name/description, `history_list` fields `limit` and `offset` as number boxes, `id` as text), `strings.json`, `translations/{en,ru}.json`.
- [ ] **Step 4:** `-p "test_ha*.py"`; PASS.
- [ ] **Step 5: Commit**

```bash
git add custom_components/tinyprint tests/test_ha_history.py
git commit -m "feat: admin history list, get, delete and clear services (Refs #25)"
```

---

### Task 8: `history_reprint` and the authorisation matrix

**Files:**
- Modify: `custom_components/tinyprint/studio_history.py`, `services.yaml`, `strings.json`, `translations/{en,ru}.json`
- Test: `tests/test_ha_history.py`

**Interfaces:**
- Consumes: `history.unpack_bitmap`, `HistoryStore.get`, `HistoryStore.blob_for`, `resolve_printer`.
- Produces: `history_reprint` with schema `{vol.Required("device_id"): str, vol.Required("id"): vol.Match(r"^[0-9a-f]{32}$")}`, `SupportsResponse.OPTIONAL`. Order: resolve the printer, load the entry (unknown -> `ServiceValidationError("History entry not found")`), `unpack_bitmap` (any `HistoryError` -> `ServiceValidationError`, message without payload), then `printer.async_print("bitmap", {"rows": rows, "bitmap": data, "recipe": entry["recipe"], "title": entry["title"]}, 24, source="history")` and return its result when `call.return_response`. The reprint is itself recorded (same blob, so no new blob bytes).

- [ ] **Step 1: Write the failing tests:**
  - `test_reprint_sends_exactly_the_stored_bytes`: record a bitmap, reprint; the patched `async_print` receives `kind "bitmap"`, `bitmap == original bytes`, `rows`, `source == "history"`, the stored `recipe` and `title`; the printer's `_render` is never called.
  - `test_reprint_with_a_real_printer_records_a_history_entry_sharing_the_blob`: two entries, one blob.
  - `test_reprint_rejects_bad_stored_data_before_any_radio_activity`: for each of a flipped bit (sha mismatch), a `zlib.compress(bytes(98305))` blob, `rows` hand-edited to `True`, `rows` 2049, a missing blob and trailing bytes: raises `ServiceValidationError`, `async_print` not awaited, `bluetooth.async_ble_device_from_address` not called.
  - `test_reprint_for_an_unknown_device_or_entry_prints_nothing`: unknown device id and unknown entry id both raise `ServiceValidationError`; no new entry appears.
  - `test_reprint_while_the_printer_is_busy_is_refused`: with a held job, raises `ServiceValidationError("Printer is busy; ...")`; entry count unchanged.
  - `test_every_new_service_rejects_a_non_admin_caller` (models `test_nonadmin_cannot_use_studio_actions`): for `print_bitmap`, `history_list`, `history_get`, `history_delete`, `history_clear`, `history_reprint`, each with schema-valid data and `Context(user_id="nonadmin")` raises `Unauthorized`; afterwards the store content and the printer mock are unchanged.
  - `test_hostile_markup_is_stored_as_inert_text_for_every_entry_point`: markup `<img src=x onerror=alert(1)>` through `print_text` (admin-free legacy handler, real `StudioPrinter`, fake transport failing after raster), through `print_bitmap` `title` and `recipe` strings: `history_get` returns the strings byte-for-byte (the server does not sanitise; the panel is the defence, Task 9).
- [ ] **Step 2:** run Task 4's Step 2 command. Expected: FAIL (`ServiceNotFound: history_reprint`).
- [ ] **Step 3: Implement.** Metadata in `services.yaml`, `strings.json`, `translations/{en,ru}.json`.
- [ ] **Step 4:** `-p "test_ha*.py"`, then the `tests` job list and ruff. PASS.
- [ ] **Step 5: Commit**

```bash
git add custom_components/tinyprint tests/test_ha_history.py
git commit -m "feat: history_reprint prints the stored bitmap after re-validation (Refs #25)"
```

---

### Task 9: Panel history drawer, recipe and title on Print, Open in editor

**Files:**
- Create: `custom_components/tinyprint/frontend/studio/history.js`, `custom_components/tinyprint/frontend/studio/title.js`
- Modify: `custom_components/tinyprint/frontend/tinyprint-panel.js` (toolbar button and drawer container in `mount()`; the `print_bitmap` call), `tests/browser_studio.py`

**Interfaces:**
- Consumes (Plan 1): shell methods `call(service, data, returnResponse)`, `notice(text, error)`, `loadInfo()`, `jobState(deviceId) -> {entityId, state, inFlight}`; `panel.getDocument()` (print_document-shaped document, `null` in image mode); the renderer's unpack-to-image function from `renderer/pack.js` (use the exported name Plan 1 defines for turning packed bytes into `ImageData`).
- Produces: `documentTitle(document) -> string` (`studio/title.js`; same rule as `history.title_for`: first non-blank line, whitespace collapsed, at most 80 chars with `…`, `"QR code"` for QR, `"Image"` otherwise); `panel.setDocument(document) -> string[]` (shell; fills the editor from a print_document-shaped document, returns notices; an unknown template, font or option falls back to the default and yields a visible notice); the Print call now sends `recipe: {v: 1, ...panel.getDocument()}` when a document exists and its JSON is at most 64 KiB (else omitted), `title: documentTitle(document)` and `source: "studio"`; image mode sends none of the three. `studio/history.js` exports `class HistoryDrawer` mounted under a toolbar button `#history-toggle`.
  Drawer DOM (the browser test selects these): container `#history`, list rows `.history-row[data-id]` each with `img.history-thumb`, `.history-title`, `.history-time`, `.history-status` (class `status-<status>`), buttons `.history-open` and `.history-reprint`, and `#history-more`.
  Behaviour:
  - Opening the drawer calls only `history_list` (`limit` 50). `#history-more` is visible while loaded rows `< total` and calls with `offset`. The list reloads after every Print or Reprint settles.
  - Text: title and time via `textContent`; time = `toLocaleString()` of `created`, `"—"` when the date is invalid. Status badge text comes from a fixed Russian map (`sent`, `failed`, `delivery_uncertain`, `cancelled`); any other value shows `"?"`.
  - Thumbnail: `img.src = "data:image/png;base64," + thumb` only when `thumb` matches `^[A-Za-z0-9+/=]+$`; otherwise no `img` source (placeholder box).
  - `.history-open` is disabled when `has_recipe` is false. Click calls `history_get`, then `setDocument(entry.recipe)` (strip the `v` key), shows any returned notices, and shows the stored bitmap in a comparison canvas: base64 decode `bitmap_z`, inflate with `new DecompressionStream("deflate")` capped at `2048 * 48` bytes, require `rows * 48` bytes, unpack through the renderer's function. A recipe that is not an object or has a missing/unknown `kind` shows a notice and changes nothing.
  - `.history-reprint` is disabled when `reprintable` is false, no printer is selected, or `jobState(device).inFlight`. For `delivery_uncertain` it first calls `window.confirm` with a warning that the first copy may have printed; cancelling makes no service call. It calls `history_reprint {device_id, id}` and shows the result or the error as a notice.
  - Nothing in the drawer prints on load, scroll or open.

- [ ] **Step 1: Write the failing browser test** in `tests/browser_studio.py` (extend the mock HA harness `service()` with `history_list`, `history_get` and `history_reprint`; run on Chromium and Firefox like Plan 1's tests): `test_history_drawer_renders_hostile_entries_as_text` (entries with title `<img src=x onerror=window.__pwned=1>`, status `<b>x</b>`, thumb `"><script>1</script>`, a valid thumb, a 3000-char title: `window.__pwned` undefined, `.history-title` `textContent` equals the hostile string, no `script` or `img[onerror]` inside `#history`, bad status shows `?`, only the valid thumb has a `data:image/png;base64,` src); `test_drawer_buttons_follow_has_recipe_reprintable_and_inflight` (the three disabled conditions, and a sensor state in flight disables every Reprint); `test_reprint_of_an_uncertain_entry_asks_first` (dialog dismissed -> zero `history_reprint` calls; accepted -> one call with exactly `{device_id, id}`); `test_open_in_editor_loads_the_recipe_and_shows_the_stored_bitmap` (editor field values equal the recipe; the comparison canvas bytes hash to the mock's `bitmap_sha256`; a recipe with `options.font = "Nope"` shows the fallback notice); `test_drawer_pages_and_never_prints_on_open` (second page uses `offset: 50`; the mock records no `print_*` or `history_reprint` call while only opening, scrolling and loading more); `test_studio_print_sends_recipe_title_and_source` (the `print_bitmap` payload has `source == "studio"`, `title`, `recipe.v == 1`; an oversized document omits the recipe; image mode sends none of the three).

- [ ] **Step 2: Run to verify it fails**

Run: `python tests/browser_studio.py`
Expected: FAIL (`#history-toggle` not found). Covered by the CI `browser` job.

- [ ] **Step 3: Implement** the modules and shell changes.

- [ ] **Step 4: Run** `python tests/browser_studio.py` — expected PASS on both browsers; then `ruff check . && ruff format --check .`.

- [ ] **Step 5: Commit**

```bash
git add custom_components/tinyprint/frontend tests/browser_studio.py
git commit -m "feat: panel history drawer with reprint and open in editor (Refs #25)"
```

---

### Task 10: Documentation and changelog

**Files:**
- Modify: `docs/studio.md` (add `### History` under `## Privacy and persistence`, the heading Plan 1 leaves; add service reference rows for `print_bitmap` `recipe`/`title`/`source` and the five history services), `docs/CHANGELOG.md` (created by Plan 1; create it with a `# Changelog` heading if absent), `README.md` (one line listing history)

- [ ] **Step 1:** Write the `### History` section: what is stored, the entry statuses and the raster-write boundary, that Reprint sends the stored bitmap and Open in editor re-renders in the current browser (bits may differ), the 200 entries / 5 MiB caps with byte-cap precedence, and every privacy statement from Global Constraints (storage path, backups, `call_service` events and the 32768-byte drop, the `recorder: exclude: event_types: [call_service]` YAML snippet, `history_clear` limits, removed printers, non-admin eviction). Add the `0.3.1` changelog entry: history and its services, `print_bitmap` optional fields, `last_result.raster_started`.
- [ ] **Step 2: Verify the text matches the code**

Run: `grep -n "32768\|call_service\|history_clear\|200\|5 MiB" docs/studio.md`
Expected: each statement present once; the numbers equal `MAX_ENTRIES`, `MAX_BYTES`.
- [ ] **Step 3: Commit**

```bash
git add docs README.md
git commit -m "docs: print history, privacy and changelog (Refs #25)"
```

---

### Task 11: MANUAL (needs the user): history on the real printer

**Needs:** the user, the deployed integration (copy `custom_components/tinyprint` of this branch to the Home Assistant host and restart HA), the physical X6h with paper, a phone or desktop browser on the panel.

**Do:**
1. Press the printer's **test print** button entity. Open Studio, then the history drawer. Photograph the paper.
2. In Studio print one short Cyrillic label. Photograph the paper. In the drawer press **Reprint** on that entry. Photograph the second copy.
3. Switch the printer off, print once from Studio (it fails before any data is sent), switch it on again.
4. Press **Open in editor** on the Cyrillic entry; check the editor fields and the stored-bitmap comparison.
5. Restart Home Assistant (Settings > System > Restart). Reopen the drawer.
6. Check `/config/.storage/tinyprint.history` exists on the HA host.

**Pass:** step 1: a `button` entry with status Sent and a thumbnail matching the paper; step 2: a `studio` entry, then a `history` entry, both Sent, and the two paper copies are visually identical; step 3: a `failed` entry, no paper; step 4: fields equal what was typed; step 5: all entries still listed; step 6: the file is present.
**Stop rule:** if any status is wrong, an entry is missing, a reprint differs from the first copy, or the drawer shows markup instead of text, stop, do not proceed to the version bump, and report the HA log lines containing `tinyprint` plus the photographs. Software test results are never reported as paper output.

---

### Task 12: Bump the version to 0.3.1

**Files:**
- Modify (0.3.0 -> 0.3.1): `PANEL_VERSION` in `custom_components/tinyprint/studio.py`, `custom_components/tinyprint/manifest.json` (`version`), `custom_components/tinyprint/diagnostics.py` (`"version"`), `README.md` (the version in the local-LE heading), `tests/browser_studio.py` (mock `printer_info` `"version"`), `tests/test_ha_bridge.py` (`test_component_versions_match_release`, three assertions)

- [ ] **Step 1:** Update every site; confirm none is missed:

Run: `grep -rn "0\.3\.0" --include=*.py --include=*.json --include=*.md . | grep -v "docs/superpowers\|CHANGELOG"`
Expected: no output.
- [ ] **Step 2: Run everything**

Run: the `tests` job pytest list and `ruff check . && ruff format --check .`; the Task 4 Docker command with `-p "test_ha*.py"`; `python tests/browser_studio.py`.
Expected: all pass.
- [ ] **Step 3: Commit**

```bash
git add -A custom_components tests README.md
git commit -m "chore: release 0.3.1 (Refs #25)"
```

---

## Self-Review

1. **Spec coverage:** Store/blob/caps/schedule: Tasks 3-4. Entry schema and status precedence with `raster_started`: Tasks 1, 3, 5. Recording in the owned task, legacy pre-raster exclusion, write failure isolation: Task 5. `print_bitmap` extension and recipe construction (server in Tasks 3/5, panel in Task 9): Tasks 3, 5, 6, 9. Five admin services, bounded decompression, revalidation: Tasks 7-8. Drawer, injection, disabled states, `thumb` regex, delivery-uncertain warning, Open in editor and comparison: Task 9. Metadata for new services: Tasks 6-8. Privacy docs: Task 10. Injection and authorisation tests: Tasks 8, 9. Version and workflow wiring: Tasks 3, 12. Physical verification: Task 11 (MANUAL).
2. **Consciously not covered:** the drawer has no delete or clear buttons (the spec lists only Open in editor and Reprint; delete and clear exist as services); a job that fails while rendering has no bitmap, so no entry is written (the entry schema requires `bitmap_sha256`); a bridge start whose answer is lost sets `delivery_uncertain` but, per the spec's derivation `bytes_written > state request`, records `failed` (proposed fix: treat `delivery_uncertain` as raster-started on the bridge path).
3. **Names:** `raster_started`, `progress`, `HistoryRing`, `HistoryStore`, `HISTORY_KEY`, `job_status`, `title_for`, `recipe_for`, `check_recipe`, `unpack_bitmap`, `summary`, `detail`, `async_register_history_services(hass, store, resolve_printer)` are used identically in every task that mentions them.
4. **Proportion:** the plan is about 1.8x the spec's size in bytes, mostly test names with their assertions; code blocks are tests and signatures only.
