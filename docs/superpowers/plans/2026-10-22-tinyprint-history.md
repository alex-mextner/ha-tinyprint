# TinyPrint Print History Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Record every print job from every entry point in a persistent, capped history that the Studio panel can list, reopen in the editor and reprint byte-exactly.

**Architecture:** A pure ring buffer with a deduplicated blob map (`history.py`) is wrapped by one domain-level `HistoryStore` over HA's `Store` (`history_store.py`). `StudioPrinter._async_print_job` becomes a thin wrapper that records each finished job inside the owned job task from `last_result`. Five admin services and a panel drawer sit on top. Plan 1 (Studio core) lands first and provides `bitmap.py`, `print_bitmap`, the `"bitmap"` render kind and the panel modules; Plan 3 (transport) lands after and consumes `last_result["timings"]`.

**Tech Stack:** Python 3.14, Home Assistant 2026.9.3 (`helpers.storage.Store`), Pillow 12.3.0, voluptuous, vanilla ES-module panel (no build step), pytest, `unittest` in the HA image, Playwright (Chromium and Firefox).

**Spec:** `docs/superpowers/specs/2026-10-22-tinyprint-studio-wysiwyg-design.md` section "Print history" (and the "Architecture" bullets for `print_bitmap`, "Documentation, tests, versioning"). Transport spec `2026-10-22-tinyprint-transport-reliability-design.md` section 1 defines `timings`. Tracked in GitHub issue #25; commits use `Refs #25`. Line numbers below are hints as of commit 28e8940 and shift after Plan 1 lands: find code by symbol.

## Global Constraints

Exact values below; the rest is in the spec section "Print history". Line numbers in tasks are hints as of commit 28e8940: find code by symbol.

- Commands used by every task: `TESTS` = the exact `python -m pytest ...` file list of the `tests` job in `.github/workflows/validate.yml`; `HA_TESTS <pattern>` = `docker run --rm --entrypoint /bin/sh -v "$PWD:/workspace" -w /workspace ghcr.io/home-assistant/home-assistant:2026.9.3 -c 'python -m pip install --target /tmp/test-deps --no-deps qrcode==8.2 && PYTHONPATH=/workspace:/workspace/bridge:/tmp/test-deps python -m unittest discover -s tests -p "<pattern>" -v'`; `LINT` = `ruff check . && ruff format --check .`. CI: the `tests` job runs an explicit file list WITHOUT Home Assistant or voluptuous (so `history.py` imports neither); `home-assistant` job picks up new `test_ha_*.py` automatically; `browser` job runs `python tests/browser_studio.py`.
- Store: `homeassistant.helpers.storage.Store`, key `tinyprint.history`, version 1, `private=True` (owner-only file permissions: the label content must not be world-readable), `serialize_in_event_loop=False` (in HA 2026.9.3 `data_func` then runs in a separate thread: it only returns an already-built immutable snapshot), `async_delay_save` 30 s. Created once per `hass` in `async_setup`, never recreated on entry reload; loaded before any print service or button is usable; mutations on the event loop. Kept at `hass.data["tinyprint_history"]`, NOT under `hass.data["tinyprint"]` (tests assert that dict is empty and it maps entry ids to printers).
- Ring caps: 200 entries and 5 MiB serialised (entries plus blobs), sizes computed once at insert, byte cap wins. Every size, and the recipe cap, is `_json_size`: UTF-8 bytes of compact JSON with `ensure_ascii=False` (the default `ensure_ascii=True` would count Cyrillic as 6 bytes per character). Each bitmap (`bitmap_z`, `thumb`) is stored once by `bitmap_sha256` and dropped with its last entry.
- Entry fields: `id` (`uuid4().hex`, validated on every input), `created` (UTC ISO-8601), `device_id`, `source` (`service`|`studio`|`button`|`history`), `status` (`sent`|`failed`|`delivery_uncertain`|`cancelled`), `phase`, `rows`, `ink_percent`, `bitmap_sha256`, `recipe`, `title` (at most 80 chars), `job_id`, `timings` (object or null). `bitmap_z` = zlib + base64 of the packed bitmap; `thumb` = base64 PNG at most 96 dots wide, made inline in the job task.
- Status: one boundary, the first raster write call. No raster write: `cancelled` if cancelled, else `failed`. Any failure, cancel or fault after it: `delivery_uncertain`. Success: `sent`. On the bridge path `delivery_uncertain` itself counts as raster started (a lost answer to the start request leaves `bytes_written` 0 while the bridge may be printing; spec amended). A failed or cancelled-before-raster job from a legacy action (`print_text`, `print_qr`, `print_image`, `source` `service`) is NOT recorded; the same from `print_bitmap`, `print_document`/`print_template`, reprints and the button is.
- A history write failure is logged without payload and never changes the job result. Logs and diagnostics stay payload-free.
- `print_bitmap` new optional fields: `recipe` (JSON object, at most 64 KiB of UTF-8 compact JSON, never interpreted by the server), `title` (at most 80 chars), `source` (`studio`|`service`, default `service`). The server builds `recipe` through `normalize_options` (fully resolved options, `v: 1`) for text, template and QR; the panel builds it for Studio; `null` for images and recipe-less `print_bitmap`.
- Five admin services (`async_register_admin_service`): `history_list`, `history_get`, `history_delete`, `history_clear`, `history_reprint`. Reprint decompresses with an output bound of `2048 * 48 + 1` bytes and re-validates rows and decoded length through `bitmap.py` before any radio activity; it never re-renders.
- Privacy docs (Task 10) must state: content persists in `/config/.storage/tinyprint.history` (private to the HA user) and HA backups; `call_service` events carry the full `service_data` into the recorder, which drops event data above 32768 bytes with a logged error (`print_bitmap` calls of roughly 500 rows or fewer WITH AN EMPTY RECIPE are stored, larger ones are not; the recipe counts too); the `recorder: exclude: event_types: [call_service]` snippet; `history_clear` does not purge backups or recorder rows; history of a removed printer is not deleted automatically; non-admin users can create entries through the legacy actions and evict older ones (accepted).
- Version: 0.3.0 at start, 0.3.1 at end.

## Review Focus

1. Two jobs for one printer: the second is rejected as busy and must leave no history entry and not disturb the first one's entry (Task 5).
2. A hand-edited or truncated `.storage/tinyprint.history` (malformed entries, bool rows, entry whose blob is gone or invalid, a missing key, oversize or future-version file): HA still starts, bad entries are dropped, nothing crashes; an entry with a missing or invalid blob stays listed but not reprintable (Tasks 3, 4).
3. Reprint of an entry whose printer device is unknown or not loaded, and reprint while another job runs: a clear error, nothing printed, no new entry for an unknown device (Task 8).
4. Hostile or awkward titles: newlines, very long text, markup, RTL marks. The stored title is one line, at most 80 characters; markup stays inert text everywhere (Tasks 3, 9).
5. History storage failing (store raises, disk full): the print result, `last_result` and the sensor state are unchanged and the log line carries no label text (Task 5).

---

### Task 1: The first-raster-write boundary (`raster_started`)

**Files:**
- Modify: `custom_components/tinyprint/transport.py` (`send_bitmap`), `custom_components/tinyprint/studio_runtime.py` (`StudioPrinter.begin`, the `send_bitmap` call in `_async_print_job`; Task 5 later renames that body to `_print_job`), `custom_components/tinyprint/local_le.py` (`run_local_job`), `custom_components/tinyprint/bridge_client.py` (`run_bridge_job`)
- Test: `tests/test_transport.py`, `tests/test_local_le.py`, `tests/test_bridge_client.py`

**Interfaces:**
- Produces (consumed by Task 5 and by Plan 3):
  `send_bitmap(client, bitmap, height, *, ..., progress: dict[str, Any] | None = None) -> dict`. `progress` is a caller-owned mutable dict: `send_bitmap` sets `progress["raster_started"] = True` and `stats["raster_started"] = True` immediately before the first write of the job stream in the packet loop (the `for offset in range(0, len(stream), packet_size)` loop; the leading `get_state_frame()` precheck write and `start_notify` do not count; a write that raises still counts). `stats["raster_started"]` is `False` until then. Because the caller owns `progress`, the flag survives an exception.
  After this task every path leaves `last_result["raster_started"]` as a `bool`: `StudioPrinter.begin()` initialises it `False`; native/proxy pass `progress=self.last_result`; `run_local_job` passes `progress=result` and resets `"raster_started": False` in its initial `result.update({...})`; for the bridge, `run_bridge_job` sets `result["raster_started"] = (result.get("bytes_written") or 0) > len(get_state_frame()) or bool(result.get("delivery_uncertain"))` in a `finally` around the whole job (every exit, including `CancelledError`), for `kind == "print"` only. 9 bytes is the leading state request and the wire format is unchanged. `delivery_uncertain` counts because three paths set it with `bytes_written` still 0 while the bridge may be printing: a lost answer to the start request (`error.uncertain`), an unacknowledged cancel (`_cancel_and_flag`) and a `sent` view with zero bytes; `_failure` already treats `bytes_written or delivery_uncertain` as partial delivery, so the two stay consistent. Do not put this in `_merge`: those paths set the flag without calling it.

  **Decision (two out-parameters):** Plan 3 adds a second optional keyword `timeline: JobTimeline | None` to `send_bitmap` and keeps `progress` as it is. They stay separate: `progress` is a plain dict that must work before Plan 3 exists and on the `local_le` path (`run_local_job` passes its result dict and has no timeline); the timeline is optional instrumentation of the native/proxy path that only Plan 3's marks (notify end, transmit, printer wait, flow pauses, printer bits) need. Merging them would make Plan 2 define a class Plan 3 owns, or make Plan 3 rewrite Plan 2's call sites and tests. The `raster_started` flag is assigned only here; Plan 3 puts its `enter("transmit")` call on the next line and never sets the flag.

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


def test_a_fault_before_the_job_means_no_raster_started():
    progress = {}
    client = FakeClient(replies=(0x01,))  # out_of_paper in the precheck reply
    with pytest.raises(t.PrinterFaultError, match="out_of_paper"):
        asyncio.run(t.send_bitmap(client, bytes(48), 1, progress=progress, **KW))
    assert progress.get("raster_started") is not True
    assert client.writes == [GET_STATE]  # a raising call returns no stats; the wire shows no raster


def test_successful_send_reports_raster_started_in_stats():
    progress = {}
    stats = asyncio.run(t.send_bitmap(FakeClient(), bytes(48), 1, progress=progress, **KW))
    assert stats["raster_started"] is True and progress["raster_started"] is True
```
In `tests/test_local_le.py` (models: the failing-at-discovery test near `:75-80` and the failing-write test near `:91-100`): `test_raster_started_false_when_the_link_fails_before_any_write` asserts `p.last_result["raster_started"] is False`; `test_raster_started_true_when_a_stream_write_fails` asserts `True`.
In `tests/test_bridge_client.py` (use its scripted fake bridge; `run(bridge)` returns the printer and the result or error): `test_raster_started_is_derived_from_bytes_beyond_the_state_request` over terminal views, expected `raster_started`: `failed` with `bytes_written` 0 -> `False`, 9 (state request only) -> `False`, 10 -> `True`; `sent` with 0 -> `True` (rejected as `bridge_bad_response` with `delivery_uncertain`), 9 -> `False` (accepted; the flag follows the bytes), 10 -> `True`. `test_a_lost_start_answer_counts_as_raster_started`: the start request fails with `BridgeError("timeout", uncertain=True)` (the code `test_timeout_after_send_is_uncertain` asserts) and the follow-up state read reports `bytes_written` 0 -> `last_result["delivery_uncertain"] is True` and `last_result["raster_started"] is True`. `test_a_start_refused_before_any_send_is_not_raster_started`: a non-uncertain start error (see `test_start_error_classification`) -> `raster_started is False`. `test_raster_started_is_set_when_the_job_is_cancelled`: `CancelledError` during polling with `bytes_written` 0 and a cancel the bridge does not acknowledge -> `True`; acknowledged -> `False`.

- [ ] **Step 2: Run to verify they fail:** `TESTS` restricted to the three files. Expected: FAIL (`TypeError: unexpected keyword argument 'progress'`, `KeyError: 'raster_started'`); the `TypeError` is an acceptable red for the transport tests, the local and bridge tests must fail on the missing key.

- [ ] **Step 3: Implement** the Interfaces block above in the four files. In `send_bitmap` set the flag on the line before `await client.write_gatt_char(WRITE_UUID, chunk, response=False)` in the packet loop.

- [ ] **Step 4: Run** `TESTS`, `HA_TESTS "test_ha*.py"` (Task 1 changes `StudioPrinter.begin` and the `send_bitmap` call, so fakes with a fixed signature break in the HA tests now, not later) and `LINT`. Expected: all pass.
- [ ] **Step 5: Commit:** `git add custom_components/tinyprint tests && git commit -m "feat: expose the first raster write as last_result.raster_started (Refs #25)"`

---

### Task 2: Thumbnail and ink helpers

**Files:**
- Modify: `custom_components/tinyprint/studio_render.py`
- Test: `tests/test_studio_render.py`

**Interfaces:**
- Produces: `ink_percent(data: bytes, rows: int) -> float` (same formula as `preview_result`: `round(100 * ones / (384 * rows), 2)`; `preview_result` now calls it); `thumbnail_png(data: bytes, rows: int) -> bytes` (PNG of the packed bitmap, ink = black, area-averaged down by 4 so it is 96 dots wide and `ceil(rows / 4)` tall).

- [ ] **Step 1: Write the failing tests** in `tests/test_studio_render.py`:
```python
def test_ink_percent_is_computed_from_the_packed_bits():
    assert r.ink_percent(b"\xff" * 48, 1) == 100.0
    assert r.ink_percent(bytes(48), 1) == 0.0
    assert r.ink_percent(b"\x80" + bytes(95), 2) == round(100 / 768, 2)  # one dot in 2 rows


def test_thumbnail_is_96_wide_and_keeps_the_ink():
    data = bytes(48 * 10) + b"\xff" * (48 * 10)  # 10 blank rows, then 10 solid rows
    png = Image.open(io.BytesIO(r.thumbnail_png(data, 20)))
    assert png.size == (96, 5)
    assert png.convert("L").getpixel((10, 0)) == 255  # blank rows stay white
    assert png.convert("L").getpixel((10, 4)) == 0  # solid ink stays black


def test_thumbnail_of_a_tall_bitmap_keeps_the_aspect_ratio():
    assert Image.open(io.BytesIO(r.thumbnail_png(bytes(48 * 2048), 2048))).size == (96, 512)
```
- [ ] **Step 2:** `python -m pytest tests/test_studio_render.py -q` — expected FAIL (`AttributeError: module ... has no attribute 'ink_percent'`; the test module reaches the module as `r`).
- [ ] **Step 3: Implement** both functions: `Image.frombytes("1", (384, rows), data.translate(_INVERT))` (the module's existing ink-inversion table), `.convert("L").reduce(4)`, `save(format="PNG", optimize=True)`.
- [ ] **Step 4:** same command, expected PASS; `LINT`.
- [ ] **Step 5: Commit:** `git add custom_components/tinyprint/studio_render.py tests/test_studio_render.py && git commit -m "feat: ink percent and thumbnail helpers for history (Refs #25)"`

---

### Task 3: Pure history core (`history.py`)

**Files:**
- Create: `custom_components/tinyprint/history.py`, `tests/test_history.py`, `tests/fixtures/title_cases.json`
- Modify: `.github/workflows/validate.yml` (append `tests/test_history.py` to the `tests` job pytest list)

**Interfaces:**
- Consumes: `bitmap.ROW_BYTES`, `bitmap.MAX_BITMAP_BYTES`, `bitmap.MAX_BITMAP_B64_LENGTH`, `bitmap.check_rows`, `bitmap.BitmapError` (Plan 1, C1); `studio_render.normalize_options`.
- Imports only the standard library, `.bitmap`, `.studio_render` (no `homeassistant`, no `voluptuous`).
- Produces (Tasks 4-8 rely on these exact names):
```python
MAX_ENTRIES = 200; MAX_BYTES = 5 * 1024 * 1024; SAVE_DELAY = 30; MAX_TITLE = 80; MAX_RECIPE_BYTES = 64 * 1024; MAX_TIMINGS_BYTES = 16 * 1024
MAX_BLOB_Z_LENGTH = bitmap.MAX_BITMAP_B64_LENGTH + 1024   # 132096: base64 of the worst-case zlib output for 98304 bytes is about 131148
MAX_THUMB_LENGTH = 128 * 1024
SOURCES = ("service", "studio", "button", "history")
STATUSES = ("sent", "failed", "delivery_uncertain", "cancelled")
ENTRY_ID_RE, SHA_RE, THUMB_RE = (compiled) r"[0-9a-f]{32}\Z", r"[0-9a-f]{64}\Z", r"[A-Za-z0-9+/=]+\Z"   # `\Z`, never `$` (it accepts a trailing newline; `vol.Match` uses `re.match`)
def valid_entry_id(value: object) -> bool              # str and ENTRY_ID_RE.match; the one id check for from_data and every service
class HistoryError(ValueError)
def _json_size(obj: object) -> int                      # len(json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode()); maps TypeError, RecursionError and OverflowError (non-JSON service data such as a `date`, a 10k-deep dict) to ValueError (a lone surrogate already raises UnicodeEncodeError, a ValueError)
def job_status(*, succeeded: bool, cancelled: bool, raster_started: bool) -> str
def title_for(kind: str, payload: Any) -> str          # kind in "text","qr","image","document","bitmap"; payload as passed to async_print (for "document": {"document": {...}, ...}); every source goes through one_line
def recipe_for(kind: str, payload: Any, font_size: int) -> dict[str, Any] | None
def check_recipe(value: object) -> dict[str, Any]       # raises ValueError unless a dict with _json_size(value) <= MAX_RECIPE_BYTES (non-JSON content included)
def clean_recipe(value: object) -> dict[str, Any] | None   # check_recipe(value), or None when it raises ValueError; shared by from_data and HistoryStore.record
def clean_timings(value: object) -> dict[str, Any] | None  # the dict when it is a dict and _json_size <= MAX_TIMINGS_BYTES (no ValueError), else None
def pack_bitmap(data: bytes) -> str                     # base64(zlib(data))
def unpack_bitmap(entry: Mapping, blob: Mapping | None) -> tuple[int, bytes]   # raises HistoryError only (wraps ValueError, which covers bitmap.BitmapError, binascii.Error and the plain ValueError b64decode raises for non-ASCII text, and zlib.error)
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
Algorithms the tests do not determine: `add` appends the entry, increments its blob reference count, then evicts the oldest entries while `len > max_entries or total_bytes > max_bytes` and more than one entry remains (the new entry is never evicted by its own insert; a blob goes when its count reaches 0). Sizes are `_json_size(x)` computed once at insert. The snapshot object is rebuilt eagerly (shallow copies of the entry list and blob map) after every mutation and `snapshot()` only returns it, so a thread reading it never sees a half-mutated state; entries and blobs are never mutated after insert. `unpack_bitmap` order: `check_rows(entry["rows"])`; strict `b64decode(validate=True)`; `zlib.decompressobj().decompress(raw, MAX_BITMAP_BYTES + 1)` and require `eof`, no `unused_data`, length at most `MAX_BITMAP_BYTES` and exactly `rows * ROW_BYTES`; finally `sha256(data) == entry["bitmap_sha256"]`. It catches `ValueError` and `zlib.error`, re-raises each as `HistoryError`, and raises `HistoryError` for its own failures (Task 8 maps only `HistoryError`). `summary` = `id, created, device_id, source, status, phase, rows, ink_percent, bitmap_sha256, title, job_id, timings` plus `thumb` (or `None`), `has_recipe` (`recipe is not None`), `reprintable` (blob present); `detail` = the whole entry plus `bitmap_z` and `thumb` (each `None` when the blob is missing). `from_data` rebuilds each kept entry from exactly the entry fields and keeps an item only if: it is a dict, `valid_entry_id(id)` and no earlier kept entry has that id, `status` and `source` are in the enums, `rows` is an int (not bool) in 1..2048, `bitmap_sha256` matches `SHA_RE`, `title` is a str of at most 80 chars, `created` is a str. Optional keys fall back to a default instead of dropping the entry: `recipe` -> `clean_recipe`, `timings` -> `clean_timings`; `job_id` and `phase` -> `""` unless str; `device_id` -> `None` unless str; `ink_percent` -> `0.0` unless a non-bool number. A blob is kept only if it is a dict with a str `bitmap_z` of at most `MAX_BLOB_Z_LENGTH` and a str `thumb` of at most `MAX_THUMB_LENGTH` matching `THUMB_RE` (a hand-edited file cannot pin huge strings); an invalid blob is dropped like an unreferenced one, and its entries stay, not reprintable, with `thumb` `None`. `dropped_on_load` counts dropped entries only. Caps are enforced after loading. `title_for`: one rule `one_line(text, default)`: normalise `\r\n` and `\r` to `\n`; take the first line with a non-whitespace character; collapse runs of `[ \t\r\n\f\v]+` to one space and trim them (an explicit ASCII class: Python `str.split()` and JS `\s` disagree on `\x1c`-`\x1f`, `\x85`, `\ufeff`, `\u2028`, `\u2029`, which stay ordinary characters); `text[:79] + "…"` when longer than 80 code points; `default` when blank or not a str. Sources: `text` -> the payload, default `"Print"`; `document` -> `payload["document"]`: kind text -> its `text`, template -> `fields["title"]`, falling through when that is blank to `fields["text"]`, then to `f"Template {template}"` (the template key, e.g. `Template note`), QR -> `"QR code"`, image -> `"Image"` (never a path); `qr` -> `"QR code"`; `image` -> `"Image"`; `bitmap` -> `payload.get("title")`, default `"Bitmap"`. `recipe_for`: `text` -> `{"v": 1, "kind": "text", "text": payload, "options": normalize_options({"font_size": font_size})}`; `qr` -> `{"v": 1, "kind": "qr", "text": payload, "options": normalize_options()}`; `document` of kind text/qr/template -> `{**payload["document"], "options": normalize_options(payload["document"].get("options")), "v": 1}` (`v` last, so a document key `v` cannot override it); `image` kinds and `ValueError` from `normalize_options` -> `None`; `bitmap` -> `payload.get("recipe")` unchanged.
Invariants: `add` computes every size it needs (`_json_size` of the entry and of the blob) BEFORE it mutates anything, so a failing `add` (ValueError, e.g. a lone surrogate in a title) leaves the ring untouched. Blob reference counts are built from the ENTRIES (`from_data` counts the entries per sha, not the loaded blobs), so an entry whose blob is missing or was neutralised can be deleted, evicted and re-added with the same sha without an exception, a negative count or a lost blob. "Blank" in `one_line` means "only characters of the explicit class" (`re.fullmatch(r"[ \t\r\n\f\v]*", line)`, never `str.strip()` or `isspace()`; JS `/^[ \t\r\n\f\v]*$/`).

- [ ] **Step 1: Write the failing tests** in `tests/test_history.py` (load modules with the stub-package pattern at the top of `tests/test_bridge_client.py`; build entries with a helper `entry(i, sha=None, status="sent")`):
  - `test_ring_keeps_the_newest_200_entries`: add 250 small entries; `len == 200`, oldest id gone, `page(5, 0)` is newest first, total 200.
  - `test_byte_cap_wins_over_the_entry_cap`: 200 entries each with its own ~40 KB blob; `len(ring) < 200`, `total_bytes <= MAX_BYTES`, newest entry present.
  - `test_a_shared_bitmap_is_stored_once`: three entries with one sha, blob passed once; `total_bytes == sum(entry sizes) + blob size` and `snapshot()["blobs"]` has one key.
  - `test_blob_outlives_the_eviction_of_one_of_its_entries` (`max_entries=2`): A(X), B(Y), C(X); adding C evicts A, `has_blob(X)` true; adding D(Z) evicts B, `has_blob(Y)` false, `has_blob(X)` true.
  - `test_delete_and_clear_release_blobs_and_bytes`: `delete` returns False for an unknown id; deleting the last referencing entry drops the blob; `clear()` returns the count and leaves `total_bytes == 0`.
  - `test_snapshot_round_trips_through_from_data`.
  - `test_from_data_drops_malformed_entries_and_orphan_blobs`: a non-dict item, bad id, an id with a trailing `"\n"`, a duplicate id (the later one is dropped), unknown status, `rows=True`, `rows=2049`, a title of 81 chars are dropped and `dropped_on_load` counts exactly these entries; an unreferenced blob disappears; `from_data(None)` and `from_data([])` are empty.
  - `test_from_data_keeps_entries_with_a_missing_or_invalid_blob_as_not_reprintable`: blob missing, `thumb` failing `THUMB_RE` (also one with a trailing `"\n"`), a non-dict blob, a blob without `bitmap_z`, a `bitmap_z` of `MAX_BLOB_Z_LENGTH + 1` characters, a `thumb` of `MAX_THUMB_LENGTH + 1` characters: the entry is kept, `summary(...)["reprintable"] is False`, `summary(...)["thumb"] is None`, `detail(...)` does not raise, `dropped_on_load` unchanged, and the oversized strings are absent from `snapshot()`.
  - `test_from_data_fills_defaults_for_missing_or_mistyped_optional_keys`: an entry without `recipe`, `timings`, `job_id`, `phase`, `device_id`, `ink_percent` is kept as `None, None, "", "", None, 0.0`; a list `recipe`, a 65 KiB `recipe`, a str `timings`, `timings` above `MAX_TIMINGS_BYTES` and a bool `ink_percent` fall back the same way; `summary` and `detail` work for each (no `KeyError`); an extra key does not survive.
  - `test_ids_with_a_trailing_newline_are_invalid`: `valid_entry_id(uuid4().hex)` True; `valid_entry_id(uuid4().hex + "\n")`, a 31-char id and a non-str are False.
  - `test_from_data_enforces_the_caps_on_an_oversized_file`.
  - `test_a_snapshot_is_unaffected_by_later_mutations`: take `snapshot()`, add and delete entries, the old object is unchanged.
  - `test_add_is_atomic_when_a_size_cannot_be_computed`: `add` of an entry with `"\ud800"` in its title raises `ValueError`; `len`, `total_bytes`, `snapshot()` and `has_blob` are as before.
  - `test_blobless_entries_loaded_from_disk_can_be_deleted_evicted_and_readded`: load entries with a missing blob and with an invalid blob (`max_entries=2`); `delete` one, evict one by adding two more, then `add` a new entry with the same sha and a blob: no exception, `has_blob(sha)` true, `snapshot()["blobs"]` holds it exactly once, evicting every referencing entry leaves no blob and `total_bytes` equals the sum of the remaining sizes.
  - `test_json_size_maps_non_json_data_to_value_error`: `check_recipe({"d": datetime.date(2026, 1, 1)})` and a 10000-deep nested dict raise `ValueError` (not `TypeError`/`RecursionError`); `clean_recipe` and `clean_timings` return `None` for them.
  - `test_blank_means_only_the_explicit_whitespace_class`: vectors added to `tests/fixtures/title_cases.json`: `"\x85\nb"` -> `"\x85"` and `"\x1c\nb"` -> `"\x1c"` (non-blank under the explicit class; Python and JS agree).
  - `test_job_status_matrix`, parametrised over the 6 reachable combinations (`succeeded and cancelled` cannot occur: the wrapper derives both from one outcome): succeeded with and without raster -> `sent`; failed without raster -> `failed`, with raster -> `delivery_uncertain`; cancelled without raster -> `cancelled`, with raster -> `delivery_uncertain`.
  - `test_summary_has_no_bitmap_or_recipe_and_flags_the_actions`: keys exclude `bitmap_z` and `recipe`; `has_recipe` False for `recipe=None`; `detail` has `recipe` and `bitmap_z`.
  - `test_title_is_one_clipped_line`: parametrised over `tests/fixtures/title_cases.json`, a list of `{"document": ..., "title": ...}` checked as `title_for("document", {"document": v["document"]}) == v["title"]`; Task 9's browser test feeds the same vectors to `documentTitle`, so Python and JS cannot drift. Vectors: `"  a\n\nb "` -> `"a"`; `"a\r\nb"`, `"a\rb"` -> `"a"`; 81 chars and 81 emoji -> 79 chars + `…`; blank text -> `"Print"`; `"<img src=x onerror=alert(1)>"` unchanged; `"\u202eabc\n"` -> `"\u202eabc"`; `"a\u2028b"` and `"a\x85b"` unchanged (not whitespace under the explicit class); `"Привет\nмир"` -> `"Привет"`; QR -> `"QR code"`; image with a path -> `"Image"`; template `note` with `fields.title`, with a blank `fields.title` and `fields.text` `"T"` -> `"T"`, with only `fields.text`, and with neither -> `"Template note"`. Direct cases in `tests/test_history.py`: `("text", "Hello\nworld")` -> `"Hello"`, `("qr", "x")`, `("image", ...)`, and `bitmap`: title `"a\nb"` -> `"a"`, `"   "` and `None` -> `"Bitmap"`, 200 chars -> 80 chars ending `…`.
  - `test_recipe_for_resolves_options_and_marks_the_version`: text recipe `["options"] == normalize_options({"font_size": 24})`, `["v"] == 1`; a template document keeps `template` and `fields`; a document with a key `"v": 99` still yields `["v"] == 1`; image -> `None`; `font_size=9999` -> `None`; bitmap passes the supplied dict through.
  - `test_check_recipe_boundary`: a dict whose `_json_size` is exactly `64 * 1024` bytes passes, one byte more raises `ValueError`; Cyrillic: `{"t": "я" * 32764}` (exactly 64 KiB in UTF-8) passes and `"я" * 32765` raises (the `ensure_ascii` default would reject both); a list, a str and `None` raise.
  - `test_unpack_round_trips_and_rejects_bad_blobs`: round trip returns `(rows, data)`; each of these raises exactly `HistoryError` (`pytest.raises(HistoryError)`: no `BitmapError`, `binascii.Error`, `zlib.error` or plain `ValueError` escapes): `rows=True`, `rows=2049`, `rows` not matching the decoded length, a `zlib.compress(bytes(MAX_BITMAP_BYTES + 1))` blob (oversized output), valid blob plus trailing bytes, invalid base64 (`"!!!"`), non-ASCII `bitmap_z` (`"я"`: `b64decode(validate=True)` raises a plain `ValueError`), valid base64 of non-zlib bytes, one flipped bit in the decompressed data (sha mismatch), `blob=None`.

- [ ] **Step 2: Run to verify they fail:** `python -m pytest tests/test_history.py -q`. Expected: FAIL (`ModuleNotFoundError: ... history`).
- [ ] **Step 3: Implement `history.py`** to the Interfaces block; append `tests/test_history.py` to the `tests` job file list in `.github/workflows/validate.yml`.
- [ ] **Step 4: Run:** `python -m pytest tests/test_history.py -q && ruff check . && ruff format --check .`. Expected: PASS.
- [ ] **Step 5: Commit:** `git add custom_components/tinyprint/history.py tests/test_history.py tests/fixtures/title_cases.json .github/workflows/validate.yml && git commit -m "feat: pure print history ring with deduplicated blobs (Refs #25)"`

---

### Task 4: `HistoryStore` over HA `Store`, created once per hass

**Files:**
- Create: `custom_components/tinyprint/history_store.py`, `tests/test_ha_history.py`
- Modify: `custom_components/tinyprint/studio.py` (`async_register_studio_services`: create and load the store before registering anything), `custom_components/tinyprint/__init__.py` (`async_setup_entry`: pass `history=hass.data.get(HISTORY_KEY)` to the printer), `custom_components/tinyprint/studio_runtime.py` (`StudioPrinter.__init__` takes the `history` kwarg here, so every entry-loading HA test stays green between this task and Task 5), `tests/test_ha_studio.py` (one assertion)

**Interfaces:**
- Consumes: `history.HistoryRing`, `history.SAVE_DELAY`, `history.pack_bitmap`, `history.clean_recipe`, `history.clean_timings`, `studio_render.thumbnail_png`, `studio_render.ink_percent`.
- Produces:
```python
HISTORY_KEY = "tinyprint_history"
class HistoryStore:
    def __init__(self, hass: HomeAssistant) -> None   # Store(hass, 1, "tinyprint.history", private=True, serialize_in_event_loop=False)
    async def async_load(self) -> None
    def record(self, *, device_id: str | None, source: str, status: str, phase: str, bitmap: bytes,
               rows: int, recipe: dict | None, title: str, job_id: str, timings: dict | None) -> str  # returns the new entry id
    def page(self, limit: int, offset: int) -> dict[str, Any]   # {"entries": [summary...], "total": int}
    def get(self, entry_id: str) -> dict | None                 # history.detail
    def blob_for(self, entry: Mapping) -> Mapping | None
    async def delete(self, entry_id: str) -> bool               # persists with await store.async_save(ring.snapshot())
    async def clear(self) -> int                                # same
    @property
    def ring(self) -> HistoryRing                               # read-only; tests read total_bytes and snapshot()
async def async_setup_history(hass: HomeAssistant) -> HistoryStore   # idempotent; stores at hass.data[HISTORY_KEY]
```
`StudioPrinter.__init__(self, *args, history: HistoryStore | None = None, **kwargs) -> None` (`studio_runtime.py`): `super().__init__(*args, **kwargs)`, then `self.history = history` and `self._job_bitmap: tuple[bytes, int] | None = None` (Task 5 fills it); `history=None` means no recording (printers built directly in tests).
`record` computes `bitmap_sha256 = sha256(bitmap)`; builds the blob (`pack_bitmap`, base64 of `thumbnail_png`) only if the ring lacks that sha; `ink_percent` from `ink_percent`; `created = datetime.now(timezone.utc).isoformat(timespec="seconds")`; `id = uuid4().hex`; passes `recipe` through `clean_recipe` and `timings` through `clean_timings` (the load limits apply to a live record too: a legacy text recipe over 64 KiB is stored as `None`, never as an oversize entry), then copies both with `json.loads(json.dumps(x))` so later mutation of `last_result["timings"]` or of the caller's recipe cannot change an inserted entry after its size was computed (the thread-safe snapshot depends on entries never changing); then `ring.add` and `store.async_delay_save(ring.snapshot, SAVE_DELAY)` (the bound method, no lambda: `data_func` runs in a thread and only returns the prebuilt snapshot). `delete` and `clear` are coroutines that persist with `await store.async_save(ring.snapshot())` (an explicit user act; `async_delay_save` reschedules on every call, so a `record` in the same tick would push the purge back 30 s). `async_load` runs `store.async_load()` and `HistoryRing.from_data` inside `except Exception`: on any exception it logs the exception type name once (never the message or content), keeps an empty ring in memory and DISABLES SAVING for this session (`record` still works in memory; no `async_delay_save`/`async_save` call), so a transient `OSError` or a file written by a newer version (`NotImplementedError`) is never overwritten. `Store.async_load()` returning `None` (no file, or a truncated one that HA renamed to `tinyprint.history.corrupt.<isoformat>`) is not a failure: empty ring, saving on. On success it logs the number of entries dropped (count only).

- [ ] **Step 1: Write the failing tests** in `tests/test_ha_history.py` (`unittest.IsolatedAsyncioTestCase` on a real `HomeAssistant(tmpdir)`, same skeleton as `tests/test_ha_studio.py`; helper writes `<tmp>/.storage/tinyprint.history` as `{"version": 1, "minor_version": 1, "key": "tinyprint.history", "data": {...}}`):
  - `test_history_is_created_once_and_survives_a_second_setup`: call `async_setup` twice; `hass.data["tinyprint_history"]` is the same object.
  - `test_nothing_is_usable_until_history_is_loaded`: patch `Store.async_load` to wait on an event; start `async_setup`; while blocked `has_service("tinyprint", "print_text")` and `has_service("tinyprint", "print_bitmap")` are False; after release both are True. (Order, verified: `__init__.async_setup` awaits `async_register_studio_services` before it registers `print_text`/`print_qr`/`print_image`, and the load happens inside `async_register_studio_services`. `history_list` does not exist until Task 7, which extends this test.)
  - `test_the_first_record_does_not_overwrite_stored_history`: pre-write a file with two valid entries; set up; `record(...)` once; `page(50, 0)["total"] == 3`.
  - `test_a_recorded_entry_survives_a_restart`: record, then flush with `hass.bus.async_fire(EVENT_HOMEASSISTANT_FINAL_WRITE)` and `await hass.async_block_till_done()` (the one flush every test in this file uses; `async_stop()` of a bare `HomeAssistant` that never started returns at once); read the file with `json`: it has the entry and its blob, and its mode has no group or other bits (`private=True`); start a second `HomeAssistant` on the same directory, `async_setup`, `page(...)["total"] == 1`.
  - `test_corrupt_stored_data_starts_empty_without_crashing`: file whose `data` is `{"entries": "x", "blobs": 5}` -> empty store, setup succeeds.
  - `test_a_truncated_store_file_starts_empty_and_saves`: file cut mid-JSON (`Store.async_load` returns `None`; HA renames it to `tinyprint.history.corrupt.<isoformat>`) -> empty ring, setup succeeds, `glob("tinyprint.history.corrupt*")` finds one file, and a later `record` flushed as above is written to `tinyprint.history`. If HA's corrupt-file recovery needs the issue registry, load it in the test skeleton; otherwise the load would fail for the wrong reason and disable saving.
  - `test_an_unreadable_or_future_version_file_is_never_overwritten`: parametrised over a `"version": 2` file (`Store.async_load` raises `NotImplementedError`) and a valid file with `Store.async_load` patched to raise `OSError("PRIVATE")`: empty ring, setup succeeds, `record` works in memory; with `Store.async_save` and `Store.async_delay_save` patched, flush as above: neither was called and the file on disk is byte-identical; `assertLogs` output contains the exception type and neither `PRIVATE` nor any title.
  - `test_record_copies_timings_and_recipe`: record with `timings=t`, `recipe=r`, then mutate `t` and `r`; `get(id)` still returns the original values.
  - `test_record_applies_the_load_limits`: a text recipe `{"v": 1, "kind": "text", "text": "a" * 70000}` is stored as `None` (`has_recipe` False) and `store.ring.total_bytes` stays below `MAX_RECIPE_BYTES` plus the bitmap; `timings` over `MAX_TIMINGS_BYTES` and a str `timings` are stored as `None`.
  - In `tests/test_ha_studio.py`, extend `test_loaded_entry_uses_studio_runtime_and_registers_panel`: the printer's `.history is self.hass.data["tinyprint_history"]`; a directly built `StudioPrinter` has `history is None`.
  - `test_data_func_snapshot_is_not_mutated_by_later_records`: capture the callable passed to `async_delay_save` (patch it; it is `ring.snapshot`), call it, then `record` again; the first result is unchanged.
  - `test_a_repeated_bitmap_adds_no_blob_bytes`: record the same bytes twice; the second record grows `store.ring.total_bytes` by exactly the entry size.
  - `test_delete_and_clear_persist_immediately`: after `await store.delete(id)` and after `await store.clear()` the file on disk, read with `json`, no longer holds that entry or its blobs; a `record` made in the same tick before `clear` does not defer it.

- [ ] **Step 2: Run to verify they fail:** `HA_TESTS test_ha_history.py`. Expected: FAIL (`ModuleNotFoundError: ... history_store`). Then `HA_TESTS "test_ha*.py"`: the `test_ha_studio.py` extension fails with `AttributeError: ... no attribute 'history'` (only this pattern runs that file). The CI `home-assistant` job covers it; no workflow change.
- [ ] **Step 3: Implement** `history_store.py`; in `async_register_studio_services` (after the `DATA_KEY` guard, before the `async_register_admin_service` calls, and so before `async_setup` registers `print_text`/`print_qr`/`print_image`) `await async_setup_history(hass)`; add the `history` kwarg to `StudioPrinter` and pass `history=hass.data.get(HISTORY_KEY)` in `async_setup_entry`.
- [ ] **Step 4: Run** `HA_TESTS test_ha_history.py`, then `HA_TESTS "test_ha*.py"` for regressions. Expected: PASS.
- [ ] **Step 5: Commit:** `git add custom_components/tinyprint tests && git commit -m "feat: persistent HistoryStore created once per hass (Refs #25)"`

---

### Task 5: Record every job inside the owned job task

**Files:**
- Modify: `custom_components/tinyprint/studio_runtime.py`, `custom_components/tinyprint/button.py`
- Test: `tests/test_ha_history.py`

**Interfaces:**
- Consumes: Task 1 (`last_result["raster_started"]`), Tasks 3-4.
- Produces (Task 6-8 and Plan 3 rely on it):
  - The `history=` kwarg and `self.history` exist since Task 4; this task adds the `source` argument and the recording.
  - `StudioPrinter.async_print(self, kind: str, payload: Any, font_size: int = 24, *, source: str = "service") -> dict` overriding the base (same busy check `"Printer is busy; wait for the current job"`, same `run_owned_job`). The base `TinyPrintPrinter` is untouched (it is still used by `tests/test_ha.py`).
  - `StudioPrinter._async_print_job(self, kind, payload, font_size, source)` is a thin wrapper: the previous body is renamed `_print_job(kind, payload, font_size)` and additionally stores the rendered `(data, rows)` in `self._job_bitmap` right after the render. The wrapper awaits `_print_job` and derives the outcome: `succeeded` = it returned; `cancelled` = it raised `asyncio.CancelledError` (the existing body already runs `failed()` for it and re-raises, and `runtime.async_cancel()` awaits the ended task, so the entry exists when `async_cancel()` returns); any other `Exception` = failed. It calls `self._record_job(kind, payload, font_size, source, succeeded=True, cancelled=False)` after a return, `succeeded=False, cancelled=True` after `CancelledError`, `succeeded=False, cancelled=False` after any other `Exception` (synchronous, never raises), lets the result return or the original exception propagate unchanged, and clears `_job_bitmap` in a `finally`. It therefore runs after `phase("sent")` / `failed()`, inside the task created by `run_owned_job`.
  - `_record_job(self, kind, payload, font_size, source, *, succeeded: bool, cancelled: bool) -> None`: the whole body (registry lookup, `recipe_for`, `title_for`, `last_result` reads via `.get`, `history.record`) runs inside one `try/except Exception` that logs `"TinyPrint history write failed (%s); job result unchanged"` with the exception type name only, so an error there never replaces or chains the job's real exception and never turns success into an error. Inside: return without recording when `history is None` or `_job_bitmap is None` (a job that failed while rendering has no bitmap and is not recorded), or when the status is `failed` or `cancelled`, `source == "service"` and `kind in ("text", "qr", "image")`. Otherwise `history.record(device_id=<id of dr.async_get(hass).async_get_device(identifiers={(DOMAIN, self.address)}), or None>, source=source, status=job_status(succeeded=succeeded, cancelled=cancelled, raster_started=bool(last_result.get("raster_started"))), phase=last_result.get("failure_phase") or last_result.get("phase") or "", bitmap=data, rows=rows, recipe=recipe_for(kind, payload, font_size), title=title_for(kind, payload), job_id=last_result.get("job_id") or "", timings=<last_result.get("timings") if a dict, else None>)` (`or ""` so a live entry equals the one reloaded from disk). `recipe_for`, `title_for` and `job_status` are imported by name into `studio_runtime`.
  - `button.py` passes `source="button"`. `button.py`, `history_reprint` and the legacy handlers always call a `StudioPrinter` (VERIFIED: `__init__.py` imports `StudioPrinter as TinyPrintPrinter` and `async_setup_entry` instantiates it); the base `TinyPrintPrinter` in `runtime.py` is used only by `tests/test_ha.py` and never receives `source`. Legacy handlers in `__init__.py` and `print_document` keep the default `"service"` (the existing `assert_awaited_once_with("document", {...}, 24)` test must stay green).
  - Plan 3 contract: `last_result["timings"]` must be final before the wrapper records, which holds when Plan 3 finalises it in `phase()` / `failed()`. Plan 3 Task 4 updates the two tests below that depend on it (`test_entry_carries_the_label_data`, `test_timings_are_stored_only_when_an_object`).

- [ ] **Step 1: Write the failing tests** in `tests/test_ha_history.py` (`StudioPrinter(hass, "one", ADDRESS, "Test", None, history=store)`; patch `_render`, `bluetooth.async_ble_device_from_address`, `establish_connection` and `send_bitmap` as `tests/test_ha_studio.py` does; patch `custom_components.tinyprint.studio_runtime.dr.async_get`; the fake `send_bitmap(client, bitmap, rows, *, progress, **kw)` sets `progress["raster_started"] = True` before raising/returning, which is what the real one does):
  - `test_status_follows_the_raster_boundary`: success -> `sent`; connect failure -> `failed`; failure after `raster_started` -> `delivery_uncertain`; cancel before raster -> `cancelled`; cancel after raster -> `delivery_uncertain`. Source for these: `print_document`-style `("document", ...)` call so pre-raster outcomes are recorded.
  - `test_legacy_action_failing_before_raster_is_not_recorded_but_after_raster_is`: `async_print("text", ...)` with a connect failure -> `page(...)["total"] == 0` and the call still raised `HomeAssistantError`; the same call cancelled before raster -> 0; failing after raster -> 1 entry `delivery_uncertain`; `("text", ..., source="button")` failing before raster -> recorded as `failed`; `("bitmap", {...})` with default source failing before raster -> recorded.
  - `test_a_bridge_job_whose_start_answer_was_lost_is_recorded_delivery_uncertain`: real `StudioPrinter` on the bridge transport with a scripted bridge client whose start request raises `BridgeError(..., uncertain=True)` and whose state read reports 0 bytes: entry `status == "delivery_uncertain"` (not `failed`).
  - `test_entry_carries_the_label_data`: success of `async_print("text", "Hello\nworld")`: `title == "Hello"`, `recipe["v"] == 1` and `recipe["options"] == normalize_options({"font_size": 24})`, `rows`, `bitmap_sha256 == sha256(bitmap)`, `ink_percent`, `job_id == last_result["job_id"]`, `source == "service"`, `device_id` from the patched registry, `timings is None`; a QR job has `title "QR code"`; an image job has `recipe None`.
  - `test_device_id_comes_from_the_real_device_registry`: the only test that does not patch `dr.async_get`: `await dr.async_load(hass)`, create the device with `dr.async_get(hass).async_get_or_create(config_entry_id=<a ConfigEntry of domain DOMAIN registered with hass.config_entries>, identifiers={(DOMAIN, ADDRESS)})`, run a job; `entry["device_id"] == device.id` (a wrong identifier tuple in `_record_job` would pass every patched test; `entity.py` and `studio.py` use `(DOMAIN, address)`).
  - `test_timings_are_stored_only_when_an_object`: a fake `send_bitmap` that puts `last_result["timings"] = {"total": 1.5}` -> stored as is; `"timings": "x"` -> `None`.
  - `test_a_busy_rejection_records_nothing`: hold job 1 open, job 2 raises `ServiceValidationError`; exactly one entry after job 1 ends.
  - `test_history_failure_never_changes_the_job_result`: parametrised over `store.record` raising `OSError("PRIVATE CONTENT")` and `studio_runtime.title_for` raising `RuntimeError("PRIVATE CONTENT")`, each for a successful job and a failing job that IS recorded (a `("document", ...)` job failing at connect, or a legacy `"text"` job failing after `raster_started`; a legacy job failing before raster returns early and never reaches the history code): the successful job returns its normal result and `printer.status == "sent"`; the failing job raises its own `HomeAssistantError` (not the history error) and `printer.status == "error"`; `assertLogs` output contains the warning and not `PRIVATE CONTENT`.
  - `test_a_cancelled_job_is_recorded_inside_the_task`: cancel through `printer.async_cancel()` after the fake raster write; the entry exists when `async_cancel` returns.
  - `test_a_reprinted_bitmap_shares_its_blob`: two jobs with identical bytes -> `snapshot()["blobs"]` has one key and two entries.

- [ ] **Step 2: Run to verify they fail:** `HA_TESTS test_ha_history.py`. Expected: FAIL (`TypeError: ... unexpected keyword argument 'source'`, missing entries). The tests that patch `studio_runtime.dr.async_get` or `studio_runtime.title_for` fail with `AttributeError` (no such attribute) until Step 3 imports them.
- [ ] **Step 3: Implement** per Interfaces (`from homeassistant.helpers import device_registry as dr`, `from .history import job_status, recipe_for, title_for`, `from .const import DOMAIN` where missing).
- [ ] **Step 4: Run** `HA_TESTS "test_ha*.py"` and `TESTS`. Expected: PASS, existing tests untouched.
- [ ] **Step 5: Commit:** `git add custom_components/tinyprint tests/test_ha_history.py && git commit -m "feat: record every print job in history from the owned job task (Refs #25)"`

---

### Task 6: `print_bitmap` stores `recipe`, `title`, `source`

**Files:**
- Modify: `custom_components/tinyprint/studio.py` (`print_bitmap` schema and handler), `custom_components/tinyprint/services.yaml`, `custom_components/tinyprint/strings.json`, `custom_components/tinyprint/translations/en.json`, `custom_components/tinyprint/translations/ru.json`
- Test: `tests/test_ha_history.py`

**Interfaces:**
- Consumes: Plan 1's `print_bitmap` (fields `device_id`, `rows`, `bitmap_b64`; handler calls `printer.async_print("bitmap", {"rows": int, "bitmap": bytes}, 24)`); `history.check_recipe`, `history.MAX_TITLE`. Plan 1's browser assertion "the image-mode print sends only `{device_id, rows, bitmap_b64}`" is updated in Task 9.
- Produces: schema additions `vol.Optional("recipe"): check_recipe`, `vol.Optional("title"): vol.All(str, vol.Length(max=80))`, `vol.Optional("source", default="service"): vol.In(["studio", "service"])`. The handler now calls `printer.async_print("bitmap", {"rows": rows, "bitmap": data, "recipe": call.data.get("recipe"), "title": call.data.get("title")}, 24, source=call.data["source"])`. The `"bitmap"` payload therefore has optional `recipe` and `title` keys (Plan 1's `_render` branch ignores them). The result shape (including `bitmap_sha256`) is unchanged.

- [ ] **Step 1: Write the failing tests** (patch the printer with an `AsyncMock`, as `test_explicit_device_routes_only_matching_printer` does):
  - `test_print_bitmap_forwards_recipe_title_and_source`: `source="studio"`, `title="Заметка"`, `recipe={"v": 1, "kind": "text", "text": "x"}` reach `async_print` as the payload keys and `source="studio"`; without the optional fields `source == "service"` and `recipe`/`title` are `None`.
  - `test_print_bitmap_optional_field_boundaries`: title of 80 chars accepted, 81 rejected; `source="button"` and `source="history"` rejected; a recipe of exactly 64 KiB compact JSON (`{"t": "a" * 65528}`) accepted, one byte more rejected; Cyrillic `{"t": "я" * 32764}` accepted, `"я" * 32765` rejected (UTF-8 bytes); a list/str recipe rejected; every rejection raises before `async_print` is awaited.
  - `test_print_bitmap_end_to_end_records_the_panel_recipe`: real `StudioPrinter` with `history=store`, patched transport; entry has `source == "studio"`, `title`, the exact recipe dict, and `status == "sent"`; a call without recipe stores `recipe None` and `title "Bitmap"`.
  - Update Plan 1's `test_print_bitmap_prints_exact_bytes_and_returns_server_hash` in `tests/test_ha_studio.py`: it asserts `async_print` awaited with `("bitmap", {"rows": 2, "bitmap": ...}, 24)`; the handler now also passes `recipe`, `title` and `source`, so that assertion changes to the new call shape in this task (it would otherwise go red).
- [ ] **Step 2:** `HA_TESTS test_ha_history.py`. Expected: FAIL (`extra keys not allowed @ data['recipe']`).
- [ ] **Step 3: Implement.** Add the three fields to the `print_bitmap` entries in `services.yaml` (selector `object` for `recipe`, `text` for `title`, `select` with `studio`/`service` for `source`), `strings.json`, `translations/en.json`, `translations/ru.json` (name + description each; `ru` in Russian).
- [ ] **Step 4:** `HA_TESTS "test_ha*.py"`; PASS (hassfest in CI validates the metadata).
- [ ] **Step 5: Commit:** `git add custom_components/tinyprint tests && git commit -m "feat: print_bitmap accepts recipe, title and source for history (Refs #25)"`

---

### Task 7: Admin services `history_list`, `history_get`, `history_delete`, `history_clear`

**Files:**
- Create: `custom_components/tinyprint/studio_history.py`
- Modify: `custom_components/tinyprint/studio.py` (call the registration), `services.yaml`, `strings.json`, `translations/{en,ru}.json`
- Test: `tests/test_ha_history.py`

**Interfaces:**
- Produces: `async def async_register_history_services(hass: HomeAssistant, store: HistoryStore, resolve_printer: Callable[[HomeAssistant, str], Any]) -> None`, called at the end of `async_register_studio_services` with `store=hass.data[HISTORY_KEY]` and `resolve_printer=_printer` (avoids a circular import; Task 8 uses `resolve_printer`). All five services are registered with `async_register_admin_service`.
  - `history_list`: schema `{vol.Optional("limit", default=50): vol.All(int, vol.Range(min=1, max=200)), vol.Optional("offset", default=0): vol.All(int, vol.Range(min=0))}` with a leading validator that raises `vol.Invalid` for `bool` (bool is an `int`) and MUST return the value it was given (voluptuous replaces the value with the validator's return, so a bare `raise`-or-fall-through function would turn `limit` into `None`), `SupportsResponse.ONLY`, returns `store.page(limit, offset)`.
  - `history_get`: `{vol.Required("id"): vol.Match(ENTRY_ID_RE)}` (`ENTRY_ID_RE` from `history.py`), `ONLY`, returns `store.get(id)` or raises `ServiceValidationError("History entry not found")`.
  - `history_delete`: same schema, `SupportsResponse.NONE`, awaits `store.delete(id)`; an unknown id raises `ServiceValidationError`.
  - `history_clear`: empty schema, `NONE`, awaits `store.clear()`.

- [ ] **Step 1: Write the failing tests:**
  - `test_history_list_hides_payload_and_pages_newest_first`: 3 entries (one with `recipe None`, one whose blob was removed via a hand-edited store file); response `total == 3`; no entry has `bitmap_z` or `recipe` keys; `has_recipe` and `reprintable` booleans are right; `limit=1, offset=1` returns the middle entry; `limit=0`, `limit=201`, `offset=-1`, `limit=True` and `offset=True` raise (the schema rejects bool explicitly, `vol.All(int, ...)` alone accepts it).
  - `test_history_get_returns_recipe_and_bitmap_z_and_validates_the_id`: full entry; `id="../../x"`, a 31-char id, a valid id plus `"\n"` and an unknown valid id raise.
  - `test_history_delete_removes_one_entry_and_clear_removes_all`: a second delete of the same id raises (so does a valid id plus `"\n"`); clear empties the list and the blob map.
  - `test_nothing_is_usable_until_history_is_loaded` (Task 4) also asserts `has_service("tinyprint", "history_list")` is False while the load is blocked and True after.
- [ ] **Step 2:** `HA_TESTS test_ha_history.py`. Expected: FAIL (`ServiceNotFound: history_list`).
- [ ] **Step 3: Implement.** Metadata for all four in `services.yaml` (Russian name/description; `limit`, `offset` as number boxes, `id` as text), `strings.json`, `translations/{en,ru}.json`.
- [ ] **Step 4:** `HA_TESTS "test_ha*.py"`; PASS.
- [ ] **Step 5: Commit:** `git add custom_components/tinyprint tests/test_ha_history.py && git commit -m "feat: admin history list, get, delete and clear services (Refs #25)"`

---

### Task 8: `history_reprint` and the authorisation matrix

**Files:**
- Modify: `custom_components/tinyprint/studio_history.py`, `services.yaml`, `strings.json`, `translations/{en,ru}.json`
- Test: `tests/test_ha_history.py`

**Interfaces:**
- Consumes: `history.unpack_bitmap`, `HistoryStore.get`, `HistoryStore.blob_for`, `resolve_printer`.
- Produces: `history_reprint` with schema `{vol.Required("device_id"): str, vol.Required("id"): vol.Match(ENTRY_ID_RE)}`, `SupportsResponse.OPTIONAL`. Order: resolve the printer, `entry = store.get(id)` (unknown -> `ServiceValidationError("History entry not found")`), `rows, data = unpack_bitmap(entry, store.blob_for(entry))` (the detail's own `bitmap_z` is not used for reprint; `unpack_bitmap` wraps every decode failure as `HistoryError`; any `HistoryError` -> `ServiceValidationError`, message without payload), then `printer.async_print("bitmap", {"rows": rows, "bitmap": data, "recipe": entry["recipe"], "title": entry["title"]}, 24, source="history")` and return its result when `call.return_response`. The reprint is itself recorded (same blob, so no new blob bytes).

- [ ] **Step 1: Write the failing tests:**
  - `test_reprint_sends_exactly_the_stored_bytes`: record a bitmap, reprint; the patched `async_print` receives `kind "bitmap"`, `bitmap == original bytes`, `rows`, `source == "history"`, the stored `recipe` and `title`.
  - `test_reprint_with_a_real_printer_records_a_history_entry_sharing_the_blob`: two entries, one blob; the patched `send_bitmap` receives bytes identical to the stored ones.
  - `test_reprint_rejects_bad_stored_data_before_any_radio_activity`: for each of a flipped bit (sha mismatch), a zip bomb (`zlib.compress(bytes(MAX_BITMAP_BYTES + 1))`), a missing blob and valid data with trailing bytes (seed the store file before setup, every blob with a valid `thumb`, and first assert `summary(...)["reprintable"] is True` for every case except the missing blob: an invalid blob would otherwise be dropped at load and become a "missing blob", and the `unpack_bitmap` checks would never run): raises `ServiceValidationError`, `async_print` not awaited, `bluetooth.async_ble_device_from_address` not called. A hand-edited `rows` of `True` or `2049` never reaches reprint (dropped at load, Task 3; `unpack_bitmap` unit cases there).
  - `test_reprint_for_an_unknown_device_or_entry_prints_nothing`: an unknown device id, a registered device whose config entry is not loaded (absent from `hass.data["tinyprint"]`), an unknown entry id and a valid id plus `"\n"` all raise `ServiceValidationError`; no new entry appears.
  - `test_reprint_while_the_printer_is_busy_is_refused`: with a held job, raises `ServiceValidationError("Printer is busy; ...")`; entry count unchanged.
  - `test_every_new_service_rejects_a_non_admin_caller` (patch `hass.auth` so `async_get_user` returns a non-admin user, exactly as `test_nonadmin_cannot_use_studio_actions` does; an unknown user id alone also raises `Unauthorized` and would prove nothing about the admin check): for `print_bitmap`, `history_list`, `history_get`, `history_delete`, `history_clear`, `history_reprint`, each with schema-valid data and `Context(user_id="nonadmin")` raises `Unauthorized`; call with `blocking=True` and, for every service registered with a response, `return_response=True` (a `SupportsResponse.ONLY` service called without it raises `ServiceValidationError` before the admin check); afterwards the store content and the printer mock are unchanged.
  - `test_hostile_markup_is_stored_as_inert_text_for_every_entry_point`: markup `<img src=x onerror=alert(1)>` through `print_text` (admin-free legacy handler, real `StudioPrinter`, fake transport failing after raster), through `print_bitmap` `title` and `recipe` strings: `history_get` returns the strings byte-for-byte (the server does not sanitise; the panel is the defence, Task 9).
- [ ] **Step 2:** `HA_TESTS test_ha_history.py`. Expected: FAIL (`ServiceNotFound: history_reprint`).
- [ ] **Step 3: Implement.** Metadata in `services.yaml`, `strings.json`, `translations/{en,ru}.json`.
- [ ] **Step 4:** `HA_TESTS "test_ha*.py"`, `TESTS`, `LINT`. PASS.
- [ ] **Step 5: Commit:** `git add custom_components/tinyprint tests/test_ha_history.py && git commit -m "feat: history_reprint prints the stored bitmap after re-validation (Refs #25)"`

---

### Task 9: Panel history drawer, recipe and title on Print, Open in editor

**Files:**
- Create: `custom_components/tinyprint/frontend/studio/history.js`, `custom_components/tinyprint/frontend/studio/title.js`
- Modify: `custom_components/tinyprint/frontend/tinyprint-panel.js` (toolbar button and drawer container in `mount()`; the `print_bitmap` call), `tests/browser_studio.py`

**Interfaces:**
- Consumes (Plan 1): shell methods `call(service, data, returnResponse)`, `notice(text, error)` (renders with `textContent`; every notice below passes through it), `loadInfo()`, `jobState(deviceId) -> {entityId, state, inFlight}`; `panel.lastResult.document` (`RenderResult.document`: the print_document-shaped document that produced the displayed and printed strip; `null` for ghost and image renders); `unpack(bitmap: Uint8Array, rows: number): ImageData` from `renderer/pack.js`.
- Produces: `documentTitle(document) -> string` (`studio/title.js`; the same rule and the same vectors as `history.title_for`: text kind -> `text`; template -> `fields.title`, falling through when blank to `fields.text`, then `Template <template key>`; `\r\n`/`\r` normalised to `\n`, first line that is not blank, where blank means only characters of `[ \t\r\n\f\v]` (an explicit class, not `\s`), runs of that class collapsed to one space and trimmed, clipped by code points (`Array.from`) to 79 chars + `…` when longer than 80; blank -> `"Print"`; `"QR code"` for QR, `"Image"` for image kinds); `panel.setDocument(document) -> string[]` (shell; fills the editor from a print_document-shaped document, returns notices; an unknown template, font or option falls back to the default and yields a visible notice that echoes the name as text only; `kind: "text"`, a legacy `print_text` recipe, maps to the `note` template with `fields.text` set to the text and the recipe's options applied; for QR recipes an unknown option VALUE gives a notice while an option key the panel does not show is ignored silently); the Print call builds its extras from `lastResult.document` (the document of the printed strip, not `getDocument()` at click time) and always sends `source: "studio"` and `title` (`documentTitle(document)`, or `"Image"` when `document` is `null`, i.e. image mode), and `recipe: {...document, v: 1}` only when `document` is non-null and `new TextEncoder().encode(JSON.stringify(recipe)).length <= 64 * 1024` (UTF-8 bytes, the server's measure); image mode sends no recipe. `studio/history.js` exports `class HistoryDrawer` mounted under a toolbar button `#history-toggle`.
  Drawer DOM (the browser test selects these): container `#history`, list rows `.history-row[data-id]` each with `img.history-thumb`, `.history-title`, `.history-time`, `.history-status` (class `status-<status>`), buttons `.history-open` and `.history-reprint`, and `#history-more`.
  Behaviour:
  - Opening the drawer calls only `history_list` (`limit` 50). The next page uses `offset` = the number of rows already fetched (not distinct ids). `#history-more` is visible while the last page returned `limit` rows and fetched rows `< total`; a page with fewer than `limit` rows ends paging (this also stops it when `total` can no longer be reached, e.g. at the 200-entry cap). Rows are deduplicated by `data-id` (a print between two pages shifts the offsets, so a page may repeat an id: it is shown once). The list reloads after every Print or Reprint settles.
  - Text: title and time via `textContent`; `.history-title` has `dir="auto"` and CSS `unicode-bidi: isolate` so an RTL override such as U+202E cannot reorder `.history-time` or `.history-status`; time = `toLocaleString()` of `created`, `"—"` when the date is invalid. Status badge text comes from a fixed Russian map (`sent`, `failed`, `delivery_uncertain`, `cancelled`); any other value shows `"?"`.
  - Thumbnail: `img.src = "data:image/png;base64," + thumb` only when `thumb` matches `^[A-Za-z0-9+/=]+$`; otherwise no `img` source (placeholder box).
  - `.history-open` is disabled when `has_recipe` is false. Click calls `history_get`, then `setDocument(entry.recipe)` (strip the `v` key) and shows any returned notices (text only: font, template and option names come from the stored recipe). The comparison canvas is shown only when `bitmap_z` is a string, decoded like this: `atob` inside try/catch (bad base64 throws); `new DecompressionStream("deflate")`; write the compressed bytes with `writer.write(...)`/`writer.close()` WITHOUT awaiting either before reading (an awaited write of a bomb stalls on backpressure and deadlocks), attach a no-op `.catch` to both promises, and read chunks from `readable.getReader()`, summing lengths and calling `reader.cancel()` (and `writer.abort()`) as soon as the sum exceeds `2048 * 48` (the stream has no output limit of its own); require exactly `rows * 48` bytes, unpack with `unpack`. Bad base64, non-zlib bytes (the stream errors), a bomb, a wrong length and a null or absent `bitmap_z` (blob gone) all skip the comparison canvas with one notice, the editor is still filled, and nothing leaves an unhandled rejection. A recipe that is not an object or has a missing/unknown `kind` shows a notice and changes nothing.
  - `.history-reprint` is disabled when `reprintable` is false, no printer is selected, or `jobState(device).inFlight`. For `delivery_uncertain` it first calls `window.confirm` with a warning that the first copy may have printed; cancelling makes no service call. It calls `history_reprint {device_id, id}` and shows the result or the error as a notice.
  - Nothing in the drawer prints on load, scroll or open.

- [ ] **Step 1: Write the failing browser test** in `tests/browser_studio.py` (extend the mock HA harness `service()` with `history_list`, `history_get` and `history_reprint`; run on Chromium and Firefox like Plan 1's tests; every test below fails on any `pageerror` or unhandled rejection): `test_history_drawer_renders_hostile_entries_as_text` (entries with title `<img src=x onerror=window.__pwned=1>`, status `<b>x</b>`, thumb `"><script>1</script>`, a valid thumb, a 3000-char title, and a title `"\u202eabc"`: `window.__pwned` undefined, `.history-title` `textContent` equals the hostile string, no `script` or `img[onerror]` inside `#history`, bad status shows `?`, only the valid thumb has a `data:image/png;base64,` src; `.history-title` has `dir="auto"` and computed `unicode-bidi` `isolate`, and for the `\u202eabc` row the `.history-time` and `.history-status` boxes do not overlap the title box); `test_drawer_buttons_follow_has_recipe_reprintable_and_inflight` (the three disabled conditions, and a sensor state in flight disables every Reprint); `test_reprint_of_an_uncertain_entry_asks_first` (dialog dismissed -> zero `history_reprint` calls; accepted -> one call with exactly `{device_id, id}`); `test_open_in_editor_loads_the_recipe_and_shows_the_stored_bitmap` (editor field values equal the recipe; the comparison canvas pixels read with `ctx.getImageData` equal `unpack(expected_bytes, rows)` pixels for the mock's packed bytes, since a canvas holds RGBA, not the packed bitmap; a recipe with `options.font = "Nope"` shows the fallback notice, and `options.font = '<img src=x onerror=window.__pwned=1>'` shows it as text: `window.__pwned` undefined, no `img` inside the notice element); `test_open_maps_text_and_qr_recipes` (a `kind: "text"` recipe fills the `note` template with `fields.text` equal to the text; a QR recipe with `options.qr_correction = "Z"` shows a notice, one with an extra key the panel does not show, such as `threshold`, shows none); `test_a_decompression_bomb_in_a_stored_bitmap_is_refused` (mock `bitmap_z` = base64 of `zlib.compress(bytes(10 * 1024 * 1024))`, about 10 KB: `ReadableStreamDefaultReader.prototype.cancel` is called, no comparison canvas, a notice, the editor still filled, the page stays responsive); `test_bad_base64_and_non_zlib_bitmap_z_show_a_notice` (`bitmap_z` `"!!!"` and valid base64 of non-zlib bytes: one notice each, no comparison canvas, editor filled); `test_open_works_for_an_entry_whose_bitmap_is_gone` (`has_recipe` true, `bitmap_z: null`: editor filled, no comparison canvas, a notice); `test_drawer_pages_and_never_prints_on_open` (second page uses `offset: 50`; a second page that repeats one id of the first shows it once and the third page still uses `offset: 100`; with `total: 300` and pages of 50, 50 and 20 rows `#history-more` is hidden after the short page and no further `history_list` call is made; the mock records no `print_*` or `history_reprint` call while only opening, scrolling and loading more); `test_studio_print_sends_recipe_title_and_source` (replaces Plan 1's image-mode assertion in `hidden controls are not part of getDocument()` that the image-mode print sends only `{device_id, rows, bitmap_b64}`: the `print_bitmap` payload has `source == "studio"`, `title`, `recipe.v == 1`, built from `lastResult.document` (change the draft after the render and before the click: the payload still follows the printed strip); an oversized document omits the recipe but still sends `source` and `title`, including a `lastResult.document` text document of 40000 `я`, which is below 64 KiB in `.length` and above it in UTF-8 bytes; image mode, `lastResult.document` `null`, sends `source: "studio"`, `title: "Image"` and no `recipe`); `test_document_title_matches_the_shared_vectors` (`documentTitle` over every vector of `tests/fixtures/title_cases.json`, loaded by the Python test and evaluated in the page).

- [ ] **Step 2: Run to verify it fails:** `python tests/browser_studio.py`. Expected: FAIL (`#history-toggle` not found).
- [ ] **Step 3: Implement** the modules and shell changes.
- [ ] **Step 4: Run** `python tests/browser_studio.py` (PASS on both browsers) and `LINT`.
- [ ] **Step 5: Commit:** `git add custom_components/tinyprint/frontend tests/browser_studio.py && git commit -m "feat: panel history drawer with reprint and open in editor (Refs #25)"`

---

### Task 10: Documentation and changelog

**Files:**
- Modify: `docs/studio.md` (add `### History` under `## Privacy and persistence`, the heading Plan 1 leaves; add service reference rows for `print_bitmap` `recipe`/`title`/`source` and the five history services), `docs/CHANGELOG.md` (created by Plan 1; create it with a `# Changelog` heading if absent), `README.md` (one line listing history)

- [ ] **Step 1:** Write the `### History` section: what is stored, the statuses and the raster-write boundary, that Reprint sends the stored bitmap while Open in editor re-renders in the current browser (bits may differ), the 200 entries / 5 MiB caps with byte-cap precedence, that the file is private to the HA user, that a store file that cannot be read at startup or was written by a newer version is left untouched and history is not saved until the next restart (a downgrade does not overwrite it), and every privacy statement from Global Constraints. Add the `0.3.1` changelog entry: history and its services, `print_bitmap` optional fields, `last_result.raster_started`.
- [ ] **Step 2: Verify:** `for p in '.storage/tinyprint.history' 'private to the HA user' '32768' 'roughly 500 rows or fewer with an empty recipe' 'event_types: [call_service]' 'does not purge backups or recorder rows' 'removed printer' 'non-admin' '200 entries' '5 MiB' 'left untouched'; do printf '%s: ' "$p"; grep -cF -- "$p" docs/studio.md; done`. Expected: every count is at least 1 (write the sentences with these exact phrases); the numbers equal `MAX_ENTRIES`, `MAX_BYTES`.
- [ ] **Step 3: Commit:** `git add docs README.md && git commit -m "docs: print history, privacy and changelog (Refs #25)"`

---

### Task 11: MANUAL (needs the user): history on the real printer

**Needs:** the user, the deployed integration (copy `custom_components/tinyprint` of this branch to the Home Assistant host and restart HA), the physical X6h with paper, a phone or desktop browser on the panel.

**Do:**
1. Press the printer's **test print** button entity. Open Studio, then the history drawer. Photograph the paper.
2. In Studio print one short Cyrillic label. Photograph the paper. In the drawer press **Reprint** on that entry. Photograph the second copy.
3. Switch the printer off, print once from Studio, switch it on again. Note the transport in use (Settings > Devices > TinyPrint > Configure).
4. Press **Open in editor** on the Cyrillic entry; check the editor fields and the stored-bitmap comparison.
5. Restart Home Assistant (Settings > System > Restart). Reopen the drawer.
6. Check `/config/.storage/tinyprint.history` exists on the HA host.

**Pass:** step 1: a `button` entry with status Sent and a thumbnail matching the paper; step 2: a `studio` entry, then a `history` entry, both Sent, and the two paper copies are visually identical; step 3: no paper, and the entry is `failed` on native, proxy and local_le; on bridge it is `failed` when the bridge reports the failure and `delivery_uncertain` when the bridge's answer to the start request timed out (that is correct behaviour, not a stop-rule failure); step 4: fields equal what was typed; step 5: all entries still listed; step 6: the file is present.
**Stop rule:** if any status is wrong (judged against the transport expectation in step 3), an entry is missing, a reprint differs from the first copy, or the drawer shows markup instead of text, stop, do not proceed to the version bump, and report the HA log lines containing `tinyprint` plus the photographs. Software test results are never reported as paper output.

---

### Task 12: Bump the version to 0.3.1

**Files:**
- Modify (0.3.0 -> 0.3.1): `PANEL_VERSION` in `custom_components/tinyprint/studio.py`, `custom_components/tinyprint/manifest.json` (`version`), `custom_components/tinyprint/diagnostics.py` (`"version"`), `README.md` (the `Current version:` line under the title that Plan 1 added; the `## Direct local LE (since 0.2.2)` heading is a feature label and stays), `tests/browser_studio.py` (mock `printer_info` `"version"`), `tests/test_ha_bridge.py` (`test_component_versions_match_release`, three assertions)

- [ ] **Step 1:** Update every site, then check exactly the version-bearing files listed above: `grep -n "0\.3\.0" custom_components/tinyprint/studio.py custom_components/tinyprint/manifest.json custom_components/tinyprint/diagnostics.py README.md tests/browser_studio.py tests/test_ha_bridge.py`. Expected: no output (the README `(0.2.2)` and `(0.2.1)` headings are feature labels and do not match; `docs/CHANGELOG.md` and `docs/superpowers/` are not checked, their 0.3.0 mentions are history).
- [ ] **Step 2: Run everything:** `TESTS`, `LINT`, `HA_TESTS "test_ha*.py"`, `python tests/browser_studio.py`. Expected: all pass.
- [ ] **Step 3: Commit:** `git add -A custom_components tests README.md && git commit -m "chore: release 0.3.1 (Refs #25)"`

---

## Self-Review

Not covered by design: the drawer has no delete or clear buttons (the spec lists only Open in editor and Reprint; the services exist); a job that fails before a bitmap exists (render failure, `_require_bridge`, a validation error) has no bitmap, so no entry is written (the schema requires `bitmap_sha256`).
