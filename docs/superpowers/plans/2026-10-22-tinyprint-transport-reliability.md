# TinyPrint transport reliability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every print job leaves a payload-free timing breakdown, and a print for a switched-off or silent printer waits patiently (`waiting_for_printer`, then `printer_not_seen`) instead of failing at once.

**Architecture:** A pure `JobTimeline` (stdlib + `.protocol`) is fed by `send_bitmap`, `connect_via_proxy` and `StudioPrinter`, and lands in `last_result["timings"]` plus one INFO line. A pure `waiting.py` (fake-clock testable) holds freshness and the wait helper; `StudioPrinter` runs the wait before `self.connection()` on the proxy transport only, inside the owned job task and the existing printer lock. Measurement is manual and tool-assisted; no constant changes without a recorded experiment.

**Tech Stack:** Python 3.14, Home Assistant 2026.9.3 (habluetooth 6.26.11, bleak 3.0.2), pytest (pure) and unittest (HA image), vanilla-JS panel, Playwright, ruff.

**Spec:** `docs/superpowers/specs/2026-10-22-tinyprint-transport-reliability-design.md` (companion: `...studio-wysiwyg-design.md`). Implemented after Plan 1 (Studio core, 0.3.0) and Plan 2 (Print history, 0.3.1).

## Global Constraints

Exact values; reasoning is in the spec (sections 1-2).

- Only the `proxy` transport waits; native, bridge, `local_le` keep today's behaviour (partial timeline only).
- `wait_for_printer_seconds`: whole seconds 0-300, default 60, same name in options flow and YAML. `0` skips both wait entries and keeps today's behaviour exactly (listed check, fail-fast, same error and message, including a failed connect; tested).
- `ADVERT_FRESH_SECONDS = 30`; fresh = `monotonic() - scanner.discovered_device_timestamps[address]` below it, missing timestamp = not fresh; poll once per second; `async_process_advertisements` is NOT used.
- One deadline per job (`monotonic() + wait_for_printer_seconds`, at job start); at most two connect rounds; the wait runs before `self.connection()`, outside the 45 s `CONNECT_DEADLINE`; the connect budget (3 attempts of 20 s inside 40 s) is unchanged.
- A proxy scanner absent from the registry at job start fails at once with its own message; an absence beginning during a wait keeps waiting until the deadline.
- Timeout result: failed, phase `waiting_for_printer`, error code `printer_not_seen`, message names the waited seconds and says to switch the printer on, move it closer or close other printer clients (a printer connected to a phone stops advertising); NOT `delivery_uncertain` (boundary C4 = first raster write call).
- Timeline: seconds from unrounded monotonic values; `total` >= sum of phases; `proxy` is the scanner name, never the MAC; failures keep the partial timeline. Status bits (`out_of_paper`, `cover_open`, `overheating`, `low_battery`, `charging`, `printing`) are copied into the timeline only: no gating, no UI claim.
- No constant change (`PROXY_PACING`, `SPEED`, `TEXT_ENERGY`, `DEFAULT_TAIL_ROWS`, `state_poll`) without an experiment result in `docs/transport-experiments.md`. No proxy firmware flash and no E6/E7 without the user's explicit approval. Never present software success as paper output.
- Existing pacing tests keep encoding their defaults (6 ms per-write estimate `tests/test_transport.py` ~215-217, 20 B packet limit ~53).
- Commits: conventional, `Refs #26` (Studio is `#25`). `ruff check . && ruff format --check .` clean (line length 100).

## Shared contract (names fixed by the other plans; verify they landed, else find the role by grep, never re-create)

- Plan 1: `IN_FLIGHT_STATES`, `SETTLED_STATES` in `sensor.py`; `printer_info` per-printer `entity_id` and top-level `in_flight_states` (Task 9 adds `waiting_for_printer`); panel `studio/status.js` (`STATUS_TEXT`, `statusText(stateObj)`), shell `tinyprint-panel.js` (`jobState(deviceId)`, `#job-status`) (Task 11); `docs/CHANGELOG.md` (Task 17 appends 0.3.2).
- Plan 2: `send_bitmap(..., progress: dict | None = None)` and `last_result["raster_started"]`; `StudioPrinter._async_print_job(kind, payload, font_size, source)` = thin recording wrapper (records history after `phase("sent")`/`failed()`, reads `last_result.get("timings")`); `StudioPrinter._print_job(kind, payload, font_size)` = the former `_async_print_job` body (render, connect, `send_bitmap`, `phase`/`failed`). Tasks 3-4 and 10 edit `_print_job` only, never the wrapper. "The print body" below means `_print_job`; Task 4 finalises `timings` inside `phase()`/`failed()`.

**Decision (two out-parameters on `send_bitmap`):** Plan 2's `progress` dict (owns `raster_started`, also used by `local_le`, which has no timeline) and this plan's `timeline` are kept as two optional keywords. Merging would force Plan 2 to define a class this plan owns, or this plan to rewrite Plan 2's call sites and tests, for no behavioural gain. Rule: `raster_started` is assigned only by Plan 2's line; this plan's `enter("transmit")` goes on the next line and never sets the flag.

## Review Focus

1. An advertisement arriving on the very last poll (the deadline tick) must count; a printer switched on at second 59.9 of 60 prints (Task 5 test).
2. A configured address in another letter case than the registry's keys (`discovered_device_timestamps` keys are upper-case MACs) must still match (Task 5 test).
3. Cancel or unload while waiting, in either entry, leaves the lock free, no connection open, and sends nothing (Task 10 tests).
4. Printer on but every connect fails (connected to a phone, or contention): at most two connect rounds, then the plain connect error; never a third connect, never a wait past the single deadline (Task 10 tests).
5. A panel opened mid-wait, or with a browser clock ahead of or behind HA, must never show more seconds than are left (Task 11 browser test).

---

**Local commands** (repo root):
- Pure: `python -m pytest tests/test_core.py tests/test_transport.py tests/test_studio_render.py tests/test_timeline.py tests/test_waiting.py tests/test_tools.py tests/test_reference_fixture.py -q` (CI job `tests`; new pure test files are appended to its explicit list in `.github/workflows/validate.yml`).
- HA image (CI job `home-assistant`, runs `test_ha*.py`): `docker run --rm --entrypoint /bin/sh -v "$PWD:/workspace" -w /workspace ghcr.io/home-assistant/home-assistant:2026.9.3 -c 'python -m pip install --target /tmp/test-deps --no-deps qrcode==8.2 && PYTHONPATH=/workspace:/workspace/bridge:/tmp/test-deps python -m unittest discover -s tests -p "test_ha*.py" -v'`. One module: replace `discover ...` with `unittest tests.test_ha_proxy_wait -v`.
- Browser (CI job `browser`): `python tests/browser_studio.py`. Lint: `ruff check . && ruff format --check .`.
- "Run X" in a step means the matching command above.


### Task 1: Put the proxy tests under CI

`tests/test_proxy_client.py` is in neither CI list, so CI never runs it (it needs HA: the HA image job).

**Files:** Rename `tests/test_proxy_client.py` -> `tests/test_ha_proxy_client.py`.

- [ ] **Step 1:** `git mv tests/test_proxy_client.py tests/test_ha_proxy_client.py`.
- [ ] **Step 2:** Run the HA module `tests.test_ha_proxy_client`: all pass. A failure is a pre-existing break: fix minimally, say so in the commit body, never delete tests.
- [ ] **Step 3: Commit** `test: run the proxy client tests in the HA image job (Refs #26)`.

---

### Task 2: `JobTimeline`

**Files:** Create `custom_components/tinyprint/timeline.py`, `tests/test_timeline.py`; modify `.github/workflows/validate.yml` (append `tests/test_timeline.py` to the `tests` job's explicit pytest list).

**Interfaces:**
- Produces (C6):
```python
PHASES = ("visible_wait", "connect", "notify", "transmit", "printer_wait")
class JobTimeline:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None
    rows: int | None; bytes_written: int; writes: int; flow_pauses: int
    connect_attempts: int; proxy: str | None; printer_state: tuple[str, ...] | None
    def enter(self, phase: str | None) -> None      # close the open phase, open `phase`; None = no phase open; ValueError for unknown names
    def attempt(self, proxy: str) -> None           # connect_attempts += 1; proxy = proxy
    def finish(self) -> None                        # close the open phase and freeze total; idempotent; enter() after finish is a no-op
    def seconds(self) -> dict[str, float | None]    # unrounded; the 5 phases + "total"
    def as_dict(self) -> dict[str, object]
    def log_fields(self, job_id: str, outcome: str) -> str
```
- Imports only the standard library and `.protocol` (`STATUS_FLAGS` gives the six bit names).
- Internals: convert each clock reading to integer nanoseconds (`round(now * 1e9)`) so phase sums telescope exactly.
- `as_dict()` keys, in order: `visible_wait, connect, notify, transmit, printer_wait, total, rows, bytes, writes, flow_pauses, connect_attempts, proxy, printer_status`. Phases floor to whole milliseconds (3 decimals), `total` ceils, so `total >= sum(phases)` survives rounding. A phase never entered is `None` (`visible_wait` is `0.0` when never entered: "0 if already visible"). `printer_status` is `{bit: bool for the six names}` or `None` when no A3 reply was seen. Repeated `enter` of the same phase accumulates.
- `log_fields(job_id, outcome)`: `job=<id> outcome=<outcome> visible_wait=<v> connect=<v> notify=<v> transmit=<v> printer_wait=<v> total=<v> rows=<n> bytes=<n> writes=<n> flow_pauses=<n> connect_attempts=<n> proxy=<name>`; `None` renders as `none`; `proxy` is last (a name may contain spaces). Metadata only.

- [ ] **Step 1: Write failing tests** in `tests/test_timeline.py` (load via the `tests/shared_loader.py` pattern; fake clock = a list-backed callable):
  - `test_phases_accumulate_over_two_connect_rounds`: enter connect (2.0 s), visible_wait (5.0 s), connect (3.0 s) -> `connect == 5.0`, `visible_wait == 5.0`; `attempt("kitchen")` twice -> `connect_attempts == 2`.
  - `test_total_is_at_least_the_sum_of_phases_with_unrounded_durations`: five contiguous phases of 600_000 ns each, clock values `k * 0.0006`; `seconds()["total"] >= sum(phases)` AND `as_dict()["total"] >= sum` of its five phase values (round-to-nearest would fail this: 5 x 1 ms vs 3 ms).
  - `test_unreached_phases_are_none_and_visible_wait_is_zero`: only connect entered then `finish()` -> `notify/transmit/printer_wait is None`, `visible_wait == 0.0`.
  - `test_finish_closes_the_open_phase_and_freezes_total`: enter connect, advance clock, `finish()`, advance clock again -> `as_dict()["connect"]` and `total` unchanged by the second advance.
  - `test_printer_status_bits_decode_all_six_names`: `printer_state=("out_of_paper","printing")` -> exact six-key dict with those two `True`; `None` -> `printer_status is None`.
  - `test_log_fields_has_no_payload_and_renders_none`: string starts with `job=abc outcome=failed`, contains `transmit=none`, ends with `proxy=btproxy-kitchen`; unknown phase name -> `ValueError`.
- [ ] **Step 2:** `python -m pytest tests/test_timeline.py -q`: FAIL (module missing). **Step 3:** implement `timeline.py`. **Step 4:** PASS, then lint.
- [ ] **Step 5: Commit** `feat: add the job timeline model (Refs #26)`.

---

### Task 3: Feed the timeline from `send_bitmap` and `connect_via_proxy`

**Files:** `custom_components/tinyprint/transport.py` (`send_bitmap`, `notification` handler); `custom_components/tinyprint/proxy_client.py` (`connect_via_proxy`); tests `tests/test_transport.py` (pure), `tests/test_ha_proxy_client.py` (HA).

**Interfaces:**
- Consumes: `JobTimeline` (Task 2); Plan 2's `progress` keyword and its `raster_started` assignment (place the `enter("transmit")` call right after it; never set the flag here).
- Produces: `send_bitmap(..., progress=..., timeline: JobTimeline | None = None)`; `connect_via_proxy(hass, address, name, source, timeline: JobTimeline | None = None)`.
- Marks in `send_bitmap` (guarded by `timeline is not None`): after `start_notify` returns `enter(None)` (the precheck round trip is a gap, not a phase); before the first raster write `enter("transmit")`; right after the raster packet loop `enter("printer_wait")` (a trailing flow pause counts there); on success return `enter(None)`. In the notification handler mirror `timeline.flow_pauses = stats["flow_pauses"]` and, on every A3 reply, `timeline.printer_state = conditions`. In `connect_via_proxy`: `timeline.attempt(found.scanner.name)` before each `client.connect()` (never `source`).

- [ ] **Step 1: Write failing tests.** In `tests/test_transport.py` give `FakeClient` an optional fake clock that advances 0.5 s per raster write, 0.25 s in `start_notify`, 2.0 s when delivering a state reply; build `JobTimeline(clock=fake.now)`:
  - `test_send_bitmap_measures_notify_transmit_and_printer_wait`: `enter("notify")` before the call; for a 3-row bitmap with `chunk_delay=0`: `seconds()["notify"] == 0.25`; `transmit == 0.5 * raster_writes` where `raster_writes = len(client.writes) - 2` (the leading and closing GET_STATE are not raster); `printer_wait >= 2.0`; `connect is None`.
  - `test_a_fault_before_the_job_keeps_the_status_bits_and_never_starts_transmit`: reply flags `0x01` -> raises `PrinterFaultError`; `timeline.printer_state == ("out_of_paper",)`, `seconds()["transmit"] is None`.
  - `test_flow_pauses_reach_the_timeline_even_when_the_job_fails`: `FakeClient(hold=True)` (starts paused) with `write failed` at a later write -> `timeline.flow_pauses == 1` after the raise.
  In `tests/test_ha_proxy_client.py` (using `scanner_device(...)`/`slow_failing_client` helpers already there):
  - `test_attempts_accumulate_and_name_the_proxy_not_its_mac`: 3 failing attempts on a scanner named `btproxy-kitchen` with source MAC `AA:BB:CC:DD:EE:FF` -> `timeline.connect_attempts == 3`, `timeline.proxy == "btproxy-kitchen"`; a second `connect_via_proxy` call with the same timeline continues the count to 6.
- [ ] **Step 2:** `tests/test_transport.py` and HA module `test_ha_proxy_client`: FAIL (unexpected keyword `timeline`). **Step 3:** implement (the `test_connection_uses_pinned_proxy_never_ha_routing` update for the 5th positional arg `printer.timeline` belongs to Task 4). **Step 4:** both plus the full pure list: PASS.
- [ ] **Step 5: Commit** `feat: measure notify, transmit and printer wait in send_bitmap (Refs #26)`.

---

### Task 4: Timeline lifecycle, `timings` in `last_result`, the INFO line

**Files:** `custom_components/tinyprint/studio_runtime.py` (`begin`, `phase`, `failed`, `connection`, `_print_job` body, `_diagnose`; the `_async_print_job` recording wrapper is Plan 2's and stays untouched); test `tests/test_ha_proxy_wait.py` (create; HA); touch `tests/test_ha_proxy_client.py` as noted.

**Interfaces:**
- Produces: `StudioPrinter.timeline: JobTimeline` (created in `begin()` with `clock=self.clock.now` once Task 5/10 add `clock`; until then `time.monotonic`); `last_result["timings"]` (the dict of `as_dict()`) written exactly once per job by `_finish_timings(outcome: str)`; one INFO line `tinyprint job timings <log_fields>` on logger `custom_components.tinyprint.studio_runtime`.
- `_finish_timings` is called from `phase()` when `phase in ("sent", "diagnosed")` (outcome = that phase) and from `failed(error)` (outcome `cancelled` for `asyncio.CancelledError`, else `failed`); idempotent. Before `finish()` it copies `bytes_written`, `writes` from `last_result`; `rows` (set `self.timeline.rows = height` right after rendering); `flow_pauses` from the timeline (`last_result.get("flow_pauses")` for bridge/local_le); on success `printer_state` from `last_result["printer_state"]` when the timeline has none. It runs before Plan 2's history recording.
- `connection()` passes `self.timeline` as the 5th argument of `connect_via_proxy`. In `_print_job`: `self.timeline.enter("connect")` before `async with self.connection()` and `self.timeline.enter("notify")` as the first statement inside; `send_bitmap(..., timeline=self.timeline)`. `_diagnose` gets no phase marks (total only).

- [ ] **Step 1: Write failing tests** (HA; patch `_render`, `send_bitmap`/`establish_connection` the way `test_configured_tail_rows_reach_the_transport` does; native transport is enough):
  - `test_success_has_a_timeline_in_last_result_and_one_info_line`: `last_result["timings"]` has exactly the 13 keys of `as_dict()`, `rows == 1`, `bytes == last_result["bytes_written"]`, `total >= connect + notify`; `assertLogs(..., "INFO")` contains exactly one line starting `tinyprint job timings` containing `outcome=sent`.
  - `test_failure_keeps_the_partial_timeline_and_failing_phase`: `establish_connection` raises `TimeoutError("PRIVATE CONTENT")` -> `timings["connect"] is not None`, `timings["notify"] is None`, `timings["transmit"] is None`, `last_result["failure_phase"] == "connecting_gatt"`, one timings line with `outcome=failed`, `"PRIVATE CONTENT"` in no log line.
  - `test_cancel_still_logs_one_timings_line`: cancel during `send_bitmap` (pattern of `test_user_cancel_reports_neutral_reason...`) -> one line with `outcome=cancelled`.
  - `test_local_le_and_bridge_jobs_get_a_partial_timeline`: a patched `run_local_job` that fills `bytes_written=9, writes=3` -> `timings["bytes"] == 9`, `connect is None`, `visible_wait == 0.0`.
  - `test_printer_status_bits_are_copied_not_gated`: `send_bitmap` returns `printer_state: ["charging"]` and the job still ends `sent`; `timings["printer_status"]["charging"] is True`, others `False`.
  - `test_timings_are_payload_free_and_name_the_proxy_not_its_mac`: proxy transport with scanner `name="btproxy-kitchen"`, source `PROXY` MAC; `timings["proxy"] == "btproxy-kitchen"`, MAC absent from every log line and from `repr(timings)`.
- [ ] **Step 2:** HA module: FAIL. **Step 3:** implement. **Step 4:** whole HA suite PASS (existing `assertLogs` tests stay green; the INFO line shares their logger).
- [ ] **Step 5: Commit** `feat: record a payload-free timeline for every job (Refs #26)`.

---

### Task 5: Waiting primitives (pure)

**Files:** Create `custom_components/tinyprint/waiting.py`, `tests/wait_fake.py`, `tests/test_waiting.py`; modify `validate.yml` (add `tests/test_waiting.py` to the `tests` list).

**Interfaces:**
- Produces (`waiting.py`, stdlib only):
```python
ADVERT_FRESH_SECONDS = 30.0; POLL_SECONDS = 1.0
DEFAULT_WAIT_SECONDS = 60; MAX_WAIT_SECONDS = 300
class SystemClock: now() -> float (time.monotonic); async sleep(seconds) (asyncio.sleep)
SYSTEM_CLOCK = SystemClock()
def normalize_wait_seconds(value: object) -> int          # ValueError: bool, non-numeric, non-whole, <0, >300; whole floats (60.0) accepted
def is_fresh(scanner: object | None, address: str, newer_than: float) -> bool
    # scanner is not None and scanner.discovered_device_timestamps.get(address.upper(), -inf) > newer_than
async def wait_until_fresh(find_scanner: Callable[[], object | None], address: str,
                           newer_than: float, deadline: float, *, clock=SYSTEM_CLOCK) -> bool
def not_seen_message(seconds: int) -> str                  # contains f"{seconds} s", "switch", "closer", "phone"
```
- `wait_until_fresh`: check `is_fresh(find_scanner(), ...)` first, then the deadline, then `clock.sleep(min(POLL_SECONDS, remaining))`, so an advertisement on the last tick counts.
- **Spec amended** (commit `docs: transport spec passes a scanner resolver to wait_until_fresh`): the spec's `wait_until_fresh` now also takes a scanner *resolver*, called every poll. Reason: `bleak_esphome/connect.py:89` builds a new `ESPHomeScanner` on every proxy reconnect, so a held scanner object would go stale and never see the printer again; the resolver is also what makes "absent during a wait keeps waiting" true.
- Timestamps come from habluetooth's `monotonic_time_coarse()`; a few ms of skew against `time.monotonic()` is accepted.
- `tests/wait_fake.py` (test utility): `FakeClock(start=1000.0)` with `now()`, `async sleep(s)` (advances `t`, fires due scheduled actions in order, then `await asyncio.sleep(0)`), `at(offset, action)`; `FakeScanner(source, name="proxy", timestamps=None, connector=object())` whose `discovered_device_timestamps` property returns a copy of its dict.

- [ ] **Step 1: Write failing tests** in `tests/test_waiting.py` (use `asyncio.run` like `tests/test_transport.py`):
  - `test_normalize_wait_seconds_table`: `0, 60, 300, 60.0 -> int`; rejected: `-1, 301, 59.5, True, "60", None, float("nan")`.
  - `test_is_fresh_boundary_and_missing_timestamp`: timestamp exactly `newer_than` is not fresh, `+0.001` is; no timestamp, scanner `None` -> False.
  - `test_address_case_does_not_matter`: registry key `AA:BB:CC:DD:EE:FF`, configured `aa:bb:cc:dd:ee:ff` -> fresh.
  - `test_printer_appears_after_n_seconds`: advert at `+12` -> returns True, `clock.t - start` in `[12, 13]`.
  - `test_advert_on_the_deadline_tick_counts`: advert at exactly `+60` with deadline `+60` -> True.
  - `test_never_appears_returns_false_at_the_deadline`: result False, elapsed exactly 60.
  - `test_scanner_absent_during_the_wait_keeps_waiting_then_follows_the_new_scanner_object`: resolver returns `None` for 10 s, then a NEW `FakeScanner` with a fresh timestamp -> True (the old scanner object never advertises).
  - `test_only_the_pinned_scanner_counts_while_a_stronger_scanner_owns_the_address`: registry holds pinned (stale) and other (fresh every second); resolver returns pinned -> False at the deadline; same run with the pinned one advertising at `+5` -> True at about 5 s.
  - `test_not_seen_message_names_the_waited_seconds`.
- [ ] **Step 2:** `python -m pytest tests/test_waiting.py -q`: FAIL. **Step 3:** implement `waiting.py`, `wait_fake.py`. **Step 4:** PASS + lint.
- [ ] **Step 5: Commit** `feat: add the printer-freshness wait helper (Refs #26)`.

---

### Task 6: `tools/adv_watch.py` (read-only advertisement and scanner-age recorder)

**Files:** Create `tools/adv_watch.py`, `tests/test_tools.py`; modify `validate.yml` (add `tests/test_tools.py`).

**Interfaces:**
- `summarize_adverts(rows: list[tuple[float, str, str, int]], address: str, source: str) -> dict[str, float | int | None]` where rows are `(time_s, address, source, rssi)`; returns `{"count", "interval_median", "interval_p95", "interval_max", "rssi_median"}` for that address and source (`None` values when fewer than two adverts).
- `scanner_ages(diagnostics: dict, address: str) -> dict[str, float | None]`: per scanner source, `monotonic_time - discovered_device_timestamps[address]` or `None` when the address is no longer listed (reads the Bluetooth config-entry diagnostics shape: `data["scanners"][i]` with `source`, `monotonic_time`, `discovered_device_timestamps`; verify the shape on a real file in Task 7 and fix the key paths if they differ).
- CLI: `python tools/adv_watch.py --ha-url URL --address MAC --source MAC --seconds N [--csv out.csv] [--diagnostics-every S]`; token only from env `HA_TOKEN` (never an argument, never printed). Adverts: admin websocket `bluetooth/subscribe_advertisements` (`add` events with `address`, `source`, `rssi`, `time`); ages: `GET /api/diagnostics/config_entry/<bluetooth entry id>` (id from `GET /api/config/config_entries/entry?domain=bluetooth`). `aiohttp` imported inside `main()`. Read-only.
- Caveat for `--help` and the doc: the websocket delivers an advert only from the manager's currently preferred source, so interval statistics describe the pinned proxy only when it is preferred (printer next to it).

- [ ] **Step 1: Write failing tests** `test_summarize_adverts_intervals_and_rssi` (adverts at 0, 1, 2, 12 s from the pinned source plus noise from another source/address -> count 4, median 1.0, max 10.0, rssi median of the pinned rows only), `test_scanner_ages_none_once_the_address_expired` (two scanners, one without the address).
- [ ] **Steps 2-4:** red, implement, green + lint. **Step 5: Commit** `feat: add a read-only advertisement recorder for measurements (Refs #26)`.

---

### Task 7: MANUAL (needs the user): verify the spec's [INFERENCE] items before building on them

Read-only on the odroid HA (`ssh ultra@100.116.57.66`, container `homeassistant-homeassistant-1`); the user switches the printer. This is a gate for Task 10.

**Files:** create `docs/transport-experiments.md` (section "Inference checks"; the rest is filled by Tasks 12-14).

Ask the user for: a long-lived HA token exported as `HA_TOKEN` for the session (revoked afterwards), the printer MAC, the pinned proxy MAC, and to put the printer on, idle, next to the proxy.

- [ ] **Step 1 (advertising interval and RSSI):** `python tools/adv_watch.py --ha-url <url> --address <printer> --source <proxy> --seconds 120 --csv ~/.cache/tinyprint-research/PlanTransport/adv_on.csv`. Record count, median/p95/max interval and median RSSI in the doc. Rule: keep `ADVERT_FRESH_SECONDS = 30.0` if `interval_max <= 10 s`; otherwise set it to `ceil(3 * interval_max)` in `waiting.py` (change in Task 5's constant; commit `fix: size the freshness window from the measured advertising interval`).
- [ ] **Step 2 (timestamp ageing and the 195 s window):** run with `--diagnostics-every 10 --seconds 330`. At about t=30 s the user switches the printer OFF (power button) and leaves it off. Pass conditions, all three: (a) the printer's age on the pinned scanner grows by about 1 s per second after switch-off; (b) the address disappears from that scanner's `discovered_device_timestamps` between 195 and 230 s after the last advert (expiry sweep runs every 30 s); (c) after the user switches it ON again, the age returns to under `ADVERT_FRESH_SECONDS` within a few intervals. Save the raw output beside the doc entry.
  **Stop rule:** if (a) or (c) fails (age does not grow, or never resets), the freshness design is wrong: stop, report the raw numbers to the user, do not start Task 10.
- [ ] **Step 3 (does the proxy stop scanning while it connects):** run `adv_watch --csv` for 90 s with the printer on; at about t=20 s the user triggers one print (Developer tools -> Actions -> `tinyprint.print_text`, any short text). Using the job's `tinyprint job timings` INFO line (Task 4) and the CSV, compare the proxy's advert count per 5 s bin before, during `connect` and during `transmit`. Record "pauses" or "continues" with the bins. Also read ESPHome's `esp32_ble_tracker` source for the proxy firmware's version and note what it does on connect. Either outcome is acceptable; this only decides the wording in `docs/esphome-proxy.md` (Task 12). If the CSV shows no adverts at all from the source, record "not conclusive".
- [ ] **Step 4:** Update the doc's section with raw numbers; revoke the token. **Commit** `docs: record the verified advertisement and scanner behaviour (Refs #26)`.

---

### Task 8: Option `wait_for_printer_seconds`

**Files:** `const.py`, `runtime.py` (`TinyPrintPrinter.__init__`), `config_flow.py` (`async_step_import`, `TinyPrintOptionsFlow._schema` and `async_step_init`), `__init__.py` (`PRINTER_SCHEMA`, `async_setup_entry`), `strings.json`, `translations/en.json`, `translations/ru.json`; tests `tests/test_ha_proxy_wait.py`, `tests/test_ha_bridge.py` (field sets).

**Interfaces:**
- Consumes: `normalize_wait_seconds`, `DEFAULT_WAIT_SECONDS`, `MAX_WAIT_SECONDS` (Task 5).
- Produces: `const.CONF_WAIT_FOR_PRINTER_SECONDS = "wait_for_printer_seconds"`; `TinyPrintPrinter(..., wait_for_printer_seconds: int = DEFAULT_WAIT_SECONDS)` stored as `self.wait_for_printer_seconds`; options-flow error key `invalid_wait`; import abort reason `invalid_wait`.
- Behaviour: options flow stores `data[KEY] = normalize_wait_seconds(user_input[KEY])` when present, error `invalid_wait` otherwise; form default = `source.get(KEY, entry.data.get(KEY, 60))`, a `NumberSelector(min=0, max=300, step=1, mode=BOX)` next to `proxy_source`. YAML: `vol.Optional(KEY): int` in `PRINTER_SCHEMA`; the import step writes `data[KEY]` (60 when absent, as for `proxy_source`) and aborts `invalid_wait` on a bad value (bool). `async_setup_entry` reads `effective.get(KEY, 60)`; an invalid stored value falls back to 60 with a warning (pattern of `tail_rows`). Strings (all three files; EN copy, RU analogous): option label "Wait for the printer (seconds)", description "Proxy transport only. How long a print waits for a switched-off or silent printer to appear before failing with printer_not_seen. 0 fails at once. 0 to 300, default 60. While waiting, other jobs get Printer is busy."; error and abort `invalid_wait`: "Enter a whole number of seconds from 0 to 300."

- [ ] **Step 1: Write failing tests** in `tests/test_ha_proxy_wait.py` (class `WaitOptionTests`, setup like `ProxyConfigTests`): `test_options_store_the_wait_and_reject_out_of_range` (`60.0 -> 60` int stored; `301`, `-1`, `True` -> form with `errors == {"wait_for_printer_seconds": "invalid_wait"}`), `test_options_form_defaults_to_60_and_shows_the_stored_value`, `test_yaml_import_maps_the_wait_like_proxy_source` (`120` stored; absent -> `60` stored; `True` -> abort `invalid_wait`), `test_setup_passes_the_wait_to_the_printer_and_survives_garbage` (entry options `{"wait_for_printer_seconds": "soon"}` -> `printer.wait_for_printer_seconds == 60`, warning logged). Update the field-set assertions at `tests/test_ha_bridge.py` (~572 and ~715) to include the new field and the `invalid_wait` error.
- [ ] **Steps 2-4:** red, implement, whole HA suite green (the strings test checks all three language files). **Step 5: Commit** `feat: add the wait_for_printer_seconds option (Refs #26)`.

---

### Task 9: Sensor state, `printer_info` field, translations

**Files:** `sensor.py`, `studio.py` (`printer_info`), `strings.json`, `translations/en.json`, `translations/ru.json`; tests `tests/test_ha_proxy_wait.py`, `tests/test_ha_studio.py` (`test_printer_info_preserves_raw_rssi` builds a bare `SimpleNamespace` printer: give it `wait_for_printer_seconds=60`).

**Interfaces:** `waiting_for_printer` is added to `TinyPrintJobSensor._attr_options` and to `IN_FLIGHT_STATES` (Plan 1's constants; Plan 1's test `set(_attr_options) == IN_FLIGHT_STATES | SETTLED_STATES` must stay green). `printer_info` gains per printer `wait_for_printer_seconds: int` (the panel's clamp). Sensor state translations: EN "Waiting for the printer", RU "Ожидание принтера".

- [ ] **Step 1: Write failing tests:** `test_waiting_state_is_a_sensor_option_and_in_flight` (`"waiting_for_printer" in IN_FLIGHT_STATES`, in `_attr_options`), `test_every_job_state_has_a_translation_in_all_three_files` (for each option: `entity.sensor.job_status.state[option]` exists and is non-empty in `strings.json`, `translations/en.json`, `translations/ru.json`), `test_printer_info_serves_the_wait_seconds` (value equals the printer's attribute, int).
- [ ] **Steps 2-4:** red, implement, HA suite green. **Step 5: Commit** `feat: expose the waiting state and wait seconds (Refs #26)`.

---

### Task 10: Waiting for the printer in the job layer

**Files:** `studio_runtime.py` (`StudioPrinter`: `clock`, `begin`, `_print_connection`, wait helpers, `PrinterNotSeenError`, `_print_job`, `PROXY_ABSENT_MESSAGE`); tests `tests/test_ha_proxy_wait.py`.

**Interfaces:**
- Consumes: `waiting.py` (Task 5), `timeline` (Tasks 2-4), option (Task 8), `proxy_client` "not visible" error.
- Produces:
```python
class PrinterNotSeenError(HomeAssistantError): ...      # message = waiting.not_seen_message(seconds)
class StudioPrinter:
    clock = SYSTEM_CLOCK                                  # class attribute; tests assign an instance FakeClock
    wait_deadline: float                                  # set in begin(): clock.now() + wait_for_printer_seconds; begin() also switches the timeline to JobTimeline(clock=self.clock.now) (Task 4 used time.monotonic)
    def _proxy_scanner(self) -> object | None             # first of bluetooth.async_current_scanners(hass) with source.upper() == proxy_source and connector is not None
    @asynccontextmanager
    async def _print_connection(self) -> AsyncIterator[Any]   # yields the connected client
    async def _wait_for_printer(self, newer_than: float, original: Exception | None = None) -> None
```
- `last_result` keys: `waiting_until` (UTC ISO-8601, milliseconds, present only while waiting), `error_code = "printer_not_seen"`, `connect_error = type(original).__name__` (entry 2 only). `waiting_for_printer` reaches the sensor through the existing `update()` dispatcher.
- `_print_connection` (the print body uses it instead of `self.connection()`; `_diagnose` keeps `self.connection()` because it answers "is the printer visible now"; Task 12 documents this):
```
if transport != proxy or wait_for_printer_seconds == 0:
    timeline.enter("connect"); async with self.connection() as client: timeline.enter("notify"); yield client; return
async with AsyncExitStack() as stack:                  # body exceptions never retry a connect
    scanner = self._proxy_scanner()
    if scanner is None: self.phase("connecting_gatt","connecting"); raise HomeAssistantError(PROXY_ABSENT_MESSAGE)
    if not is_fresh(scanner, address, clock.now() - ADVERT_FRESH_SECONDS):
        await self._wait_for_printer(clock.now() - ADVERT_FRESH_SECONDS)        # entry 1
    for round in (1, 2):
        started = clock.now(); timeline.enter("connect")
        try: client = await stack.enter_async_context(self.connection()); break
        except (BleakError, TimeoutError, HomeAssistantError) as error:
            if round == 2: raise
            if not is_fresh(self._proxy_scanner(), address, started):
                await self._wait_for_printer(started, error)                    # entry 2
    timeline.enter("notify"); yield client
```
  `_wait_for_printer`: set `waiting_until = utcnow + (wait_deadline - clock.now())`, `phase("waiting_for_printer","waiting_for_printer")`, `timeline.enter("visible_wait")`, `ok = await wait_until_fresh(self._proxy_scanner, address, newer_than, wait_deadline, clock=self.clock)`, pop `waiting_until`; if not ok: set `error_code`/`connect_error`, `raise PrinterNotSeenError(...) from original`. A fresh advertisement after a failed round 1 goes straight to round 2 even when the deadline has passed (the deadline bounds waiting only). `PrinterNotSeenError` is a `HomeAssistantError`, so the existing `failed(error)` branch records `failure_phase == "waiting_for_printer"` with `delivery_uncertain` False. Cancel/unload need no new code (the wait lives in the owned job task) but are tested.

- [ ] **Step 1: Write failing tests** in `tests/test_ha_proxy_wait.py` (class `WaitingJobTests`; real `StudioPrinter` with `proxy_source=PROXY`, `printer.clock = FakeClock()`, `_render` patched to `(bytes(48), 1)`, `studio_runtime.bluetooth.async_current_scanners` patched to a mutable list of `FakeScanner`s, `studio_runtime.connect_via_proxy` patched with a scripted coroutine per round (raise `BleakError("x")` after `await clock.sleep(5)`, or return a fake client with `is_connected`, `disconnect`), `studio_runtime.send_bitmap` patched `AsyncMock(return_value={"confirmation": "transport_only", "bytes_written": 56})`; record every push via `UPDATE_SIGNAL.format("entry")` as `(printer.status, printer.last_result.get("waiting_until"))`):
  - `test_printer_appears_after_12_s_and_the_job_proceeds`: stale advert (age 100 s), advert scheduled at `+12`, wait 60 -> `send_bitmap` awaited once; pushed statuses in order `rendering, waiting_for_printer, connecting, printing, sent` with `waiting_until` set only on the waiting push (parse it: within 1 s of real-now + 60); `timings["visible_wait"]` in `[12, 13]`; `printer.lock` free afterwards.
  - `test_never_appears_fails_with_printer_not_seen_sends_nothing_and_frees_the_lock`: wait 60 -> `PrinterNotSeenError`, message contains `60 s`; `clock.t - start == 60`; `last_result["error_code"] == "printer_not_seen"`, `failure_phase == "waiting_for_printer"`, `bytes_written == 0`, `delivery_uncertain is False`, `send_bitmap` and `connect_via_proxy` not called, `printer.status == "error"`; a following `async_print` is accepted (not "busy").
  - `test_listed_but_stale_advert_enters_the_wait_and_a_fresh_one_does_not`: ages 31 s -> waits; 29 s -> `visible_wait == 0.0`, no waiting push.
  - `test_failed_connect_then_wait_then_one_more_connect_inside_one_deadline`: round 1 raises `BleakError` after 5 s, advert arrives at `+20`, round 2 succeeds -> `connect_via_proxy` called exactly twice, job `sent`, `timings["connect"] == 5.0 + round-2 duration`, `visible_wait == 15.0`, total fake time < 60.
  - `test_failed_connect_with_a_fresh_advert_retries_without_waiting`: advert timestamp after the round-1 start -> second round at once, `visible_wait == 0.0`.
  - `test_timeout_in_the_second_wait_reports_not_seen_with_the_original_connect_error`: round 1 takes 45 s then raises `BleakError`; no advert -> waits only the 15 s left, `PrinterNotSeenError.__cause__` is the `BleakError`, `last_result["connect_error"] == "BleakError"`, `clock.t - start == 60`.
  - `test_never_a_third_connect_round`: both rounds fail with a fresh advert present -> second error propagates as `Printer failed at connecting_gatt (BleakError)`, `connect_via_proxy` called exactly twice, no wait entered.
  - `test_proxy_scanner_absent_at_start_fails_at_once_but_absent_during_a_wait_keeps_waiting`: empty registry at start -> `HomeAssistantError`, `clock.t` unchanged, `failure_phase == "connecting_gatt"`; registry emptied at `+5`, a NEW scanner with a fresh advert at `+15` -> job proceeds.
  - `test_fresh_advert_on_the_pinned_scanner_wins_over_a_stronger_second_scanner`: registry `[pinned (stale), other (fresh every second)]`; pinned advertises at `+5` -> proceeds at about 5 s; without it -> `PrinterNotSeenError` (the other scanner never counts).
  - `test_wait_zero_keeps_todays_fail_fast_behaviour`: `wait_for_printer_seconds=0`: `async_current_scanners` patched to raise `AssertionError` if called; `connect_via_proxy` raising the exact `HomeAssistantError` text from `proxy_client` (import it from there) -> same message, `failure_phase == "connecting_gatt"`, `clock.t` unchanged, no `waiting_for_printer` push; and with a connect raising `BleakError` -> `Printer failed at connecting_gatt (BleakError)` with `connect_via_proxy` called once.
  - `test_cancel_during_the_wait_frees_the_lock_and_never_connects` (cancel in entry 1 and, in a second sub-case, in entry 2): `await printer.async_cancel()` -> caller gets the neutral cancelled `HomeAssistantError`, `printer.task is None`, lock free, `connect_via_proxy` not called after the cancel, `last_result["failure_phase"] == "waiting_for_printer"`, one timings line `outcome=cancelled`.
  - `test_unload_during_the_wait_ends_it_without_connecting`: `async_unload_entry(hass, entry)` (patch `hass.config_entries.async_unload_platforms` to return True) while waiting -> job task finished, `connect_via_proxy` never called, entry data popped.
  - `test_second_job_during_the_wait_is_rejected_busy_and_the_first_keeps_waiting`.
  - `test_body_failure_after_connecting_never_retries_the_connect`: connect succeeds, `send_bitmap` raises `RuntimeError` -> `connect_via_proxy` called once, client disconnected.
- [ ] **Steps 2-4:** red, implement per the algorithm, HA + pure suites + lint green. **Step 5: Commit** `feat: wait for a silent printer before failing (Refs #26)`.

---

### Task 11: Panel countdown label

**Files:** `custom_components/tinyprint/frontend/studio/status.js` (Plan 1 module), shell `frontend/tinyprint-panel.js`, `tests/browser_studio.py` (Plan 1's rewritten harness; add one section "waiting label").

**Interfaces:**
- Produces in `status.js`: `STATUS_TEXT.waiting_for_printer` and `waitingSecondsLeft(stateObj, arrivedAtMs, nowMs, maxSeconds) -> number` = `ceil(clamp(min(r1, r2), 0, maxSeconds))` with `r1 = (waitingUntil - lastUpdated)/1000 - (nowMs - arrivedAtMs)/1000` and `r2 = (waitingUntil - nowMs)/1000`, where `waitingUntil = Date.parse(stateObj.attributes.waiting_until)`, `lastUpdated = Date.parse(stateObj.last_updated)`. `statusText(stateObj)` for state `waiting_for_printer` returns the label with the remaining seconds. Label (match the surrounding language, currently Russian): "Принтер не найден: включите его, печать начнётся сама (осталось N с)" / EN "Printer not seen: switch it on, printing starts automatically (N s left)".
- The shell records `arrivedAtMs = Date.now()` when a state object with a new `last_updated` arrives through the `hass` setter, takes `maxSeconds` from `printer_info.wait_for_printer_seconds`, shows the label in `#job-status`, and re-renders it once per second only while the state is `waiting_for_printer` (clear the timer otherwise and in `disconnectedCallback`).

- [ ] **Step 1: Write the failing browser checks** (Playwright `page.clock.install(time=T0)`; set `panel.hass.states[entity_id]` objects, wait 60):
  - `waiting label counts down`: `waiting_until = T0+60 s`, `last_updated = T0`, arrives now -> label contains `60`; `page.clock.run_for(10_000)` -> `50`; state changes to `connecting` -> label gone and `window.__timers` (count of live intervals via `page.evaluate` on a wrapper installed before load) is 0.
  - `panel opened mid-wait never shows the full wait`: `last_updated = T0`, `waiting_until = T0+60 s`, state delivered at `T0+40 s` -> label shows `20` (the `Date.now()` estimate is the smaller one).
  - `browser clock ahead of HA only shortens the label`: browser time `T0+30 s`, same state delivered now -> `30`, never `60`.
  - `label is clamped`: `waiting_until` 10 minutes ahead with `maxSeconds` 60 -> `60`; `waiting_until` in the past -> `0`.
  - `print is blocked while waiting`: with `in_flight_states` containing `waiting_for_printer` and the state waiting, `#print` is disabled (Plan 1's blocker rule; assert, do not re-implement).
- [ ] **Steps 2-4:** red on the new section, implement, green on every browser Plan 1's harness runs. **Step 5: Commit** `feat: show a countdown while waiting for the printer (Refs #26)`.

---

### Task 12: Documentation

**Files:** `docs/esphome-proxy.md`, `docs/studio.md`, `README.md` (option mention only if the README lists options).

- [ ] **Step 1:** `docs/esphome-proxy.md`, new section "Waiting for the printer": what is fresh (`ADVERT_FRESH_SECONDS`), phase `waiting_for_printer`, the option (0-300, default 60, `0` = old behaviour, YAML key too), `printer_not_seen` (not `delivery_uncertain`), the printer lock held up to the wait plus two connect deadlines (other jobs get "Printer is busy"), `diagnose` does not wait, only `proxy` waits, the verified scanner facts from Task 7 (pause-while-connecting wording depends on its result).
- [ ] **Step 2:** `docs/studio.md`: add `waiting_for_printer` to the phase list (find the sentence beginning "Phase names include"), the status line, the `last_result` keys `phase`, `waiting_until`, `error_code`, `connect_error`, `timings` (field table of `as_dict()`), the `tinyprint job timings` INFO line; state that status bits are logged only and unverified on the X6h.
- [ ] **Step 3:** Check links. **Commit** `docs: document waiting for the printer and job timings (Refs #26)`.

---

### Task 13: Measurement tools, fixture and evidence reconciliation

**Files:** Create `tools/timings_report.py`, `tools/gen_reference_bitmap.py`, `tests/fixtures/reference_247.bin`, `tests/test_reference_fixture.py`; extend `tests/test_tools.py`; fill `docs/transport-experiments.md` sections "Evidence reconciliation" and "Reference bitmap"; modify `validate.yml` (add `tests/test_reference_fixture.py`).

**Interfaces:**
- `timings_report.parse_timings_lines(text: str) -> list[dict[str, object]]` parsing the Task 2 `log_fields` format from any text containing `tinyprint job timings ` lines (`none` -> `None`, numbers -> int/float, `proxy` = rest of line); `medians(rows, outcome="sent") -> dict[str, float]` for `connect, notify, transmit, printer_wait, total`; CLI reads stdin and prints a Markdown table of medians plus every raw row.
- `gen_reference_bitmap.py`: one-off; renders a fixed multi-line label with `studio_render.render_document`, crops to exactly 247 rows, packs with `studio_render.bitmap`, writes `tests/fixtures/reference_247.bin` (247 x 48 = 11,856 bytes) and prints the SHA-256. It is a stand-in with the same row count as the 10-21 08:03 job (original bytes were not kept), not comparable to the evidence rows.
- No `tools/reference_print_action.py`: Task 15 prints the action YAML with a one-liner.

- [ ] **Step 1: Write failing tests:** `test_parse_timings_round_trips_the_producer` (build a `JobTimeline`, call `log_fields`, parse it back: same values, `None` preserved, proxy with a space intact), `test_medians_ignore_failed_jobs_and_use_the_true_median` (rows with outcomes `sent, sent, sent, failed`; even count case), in `tests/test_reference_fixture.py`: `test_fixture_is_247_packed_rows_with_real_content` (length 11,856; ink fraction between 5% and 60%; no more than 100 consecutive all-zero rows), `test_experiment_doc_records_the_fixture_hash` (`hashlib.sha256` of the file appears in `docs/transport-experiments.md`).
- [ ] **Steps 2-4:** red; implement tools, generate the fixture, write its SHA-256 into the doc; green.
- [ ] **Step 5 (reconcile logged bytes to rows; the spec marks 10-21 08:03 unreconciled):** In a scratch script under `~/.cache/tinyprint-research/PlanTransport/`, compute `sum(len(f) for f in print_frames(bytes(48*h), h, feed=64, tail_rows=T))` for `h` in 1..700 and `T` in {0, 96}, plus 9 B per extra state write (`get_state_frame()` length), and find `(rows, state writes)` that give the logged totals 8,007 B / 152 frames (10-22 12:19 and 12:50 jobs), 18,376 B / 338 frames (10-21 08:03), 4,732 B / 91 frames, 8,428 B / 157 frames. Preliminary hint, verify do not trust: 140 rows + 4 state writes fits 8,007 and 325 rows + 4 state writes fits 18,376. Check which `tail_rows` was in force on each date with `git log -S"DEFAULT_TAIL_ROWS" -- custom_components/tinyprint/protocol.py`. Write the table (job, bytes, frames, derived rows, state writes, derived transmit rows/s from the spec's Transmit column) into the doc section "Evidence reconciliation", and strike or correct the spec's "13-24 rows/s" claim there. If no integer solution exists for a job, record it as unreconciled with the residue; do not quote a rate for it.
- [ ] **Step 6: Commit** `feat: add measurement tools and the 247-row reference bitmap (Refs #26)`.

---

### Task 14: MANUAL (needs the user): physical proof of the waiting flow (Acceptance 1 and 4)

Needs Tasks 1-13 deployed to the odroid HA (usual deployment, HA restart; confirm `printer_info.version`), a powered X6h with paper next to the pinned proxy, a phone for photographs. Proxy transport, `wait_for_printer_seconds` 60 (20 for the timeout case).

- [ ] **Step 1 (waits, then prints):** switch the printer OFF; in Developer tools -> Actions run `tinyprint.print_text` ("TinyPrint wait test") for the printer. Observable pass: the sensor `sensor.<printer>_print_job` shows `Waiting for the printer`, the Studio panel shows the countdown, and after the user switches the printer ON within the wait the job starts by itself and paper comes out with the text. Photograph the paper. Record the job's `timings` (`visible_wait` > 0) and the `tinyprint job timings` log line.
- [ ] **Step 2 (gives up):** set the option to 20, printer OFF, run a print, switch nothing on. Pass: the action fails after about 20 s with `printer_not_seen`, no paper moves, the next print is accepted immediately (lock free), the log line shows `outcome=failed`, `transmit=none`.
- [ ] **Step 3:** attach photographs and numbers to `docs/transport-experiments.md` under "Waiting flow". **Stop rule:** if the sensor never shows the waiting state, or a job prints twice, or the printer prints garbage, stop, keep the raw log, report to the user; do not continue to Task 15.
- [ ] **Step 4: Commit** `docs: record the physical waiting-flow proof (Refs #26)`.

---

### Task 15: MANUAL (needs the user): experiments E0-E2 (Acceptance 3)

Runs on the deployed build. Print the reference bitmap each time: a `python - <<'EOF'` one-liner printing YAML `action: tinyprint.print_bitmap` / `data: {device_id: <id>, rows: 247, bitmap_b64: <base64 of tests/fixtures/reference_247.bin>}` for Developer tools -> Actions (YAML mode; the response shows `timings`). Collect lines with `ssh ultra@100.116.57.66 'docker logs --since <ISO start> homeassistant-homeassistant-1 2>&1' | python tools/timings_report.py`.

Rules for every experiment (spec section 3): at least 5 runs per arm, one variable, interleave or repeat the baseline block after the variable block (A B A) so drift shows, medians from the timeline, every output photographed. Adopt a change only if the median `total` improves and no photograph shows squeezed or dropped rows or a swallowed last line.

- [ ] **Step 1 (session start):** `python tools/adv_watch.py ... --seconds 60`: record the printer RSSI at the pinned proxy, with the printer on and in its final place. Record it at the start of every session in the doc.
- [ ] **Step 2 (E0):** baseline, kettle integration active, 5+ runs. Raw numbers and medians of `connect, notify, transmit, printer_wait, total` into the doc. Note: the new `transmit` is first-to-last raster write only, so it is not comparable with the evidence table's old `Transmit` column.
- [ ] **Step 3 (E1):** ask the user to disable the Xiaomi kettle integration (Settings -> Devices & services -> the kettle entry -> Disable), run 5+ jobs, ask them to re-enable it. Record whether `connect` medians differ; the doc states the E1 verdict ("contention matters" / "does not") which decides whether E7 is worth proposing.
- [ ] **Step 4 (E2):** on a throwaway branch change only the default `state_poll` in `send_bitmap` from `3.0` to `0.5`, deploy, 5+ runs. Adoption per the rule above. If adopted: commit the constant change on the feature branch (`perf: poll the printer state every 0.5 s (E2, docs/transport-experiments.md)`), tests need no change (they pass explicit values; run the pure suite to confirm).
- [ ] **Step 5:** **Stop rule:** a squeezed/dropped-row photograph or two consecutive failed jobs with the printer on and paper loaded: revert the variable, record the photograph, end the session. **Commit** `docs: record E0-E2 transport measurements (Refs #26)`.

---

### Task 16: MANUAL (needs the user): optional experiments E3-E7

Each one only on the user's explicit go for that ID; a skipped one is written down as "not run". Same rules, runs, photographs and adoption rule as Task 15.

- [ ] **E3** tail rows 96 / 64 / 32: option `tail_feed_lines` in the options flow, no code change; risk: last line lost (stop rule: any swallowed last line -> back to 96 and record).
- [ ] **E4** `SPEED` 10 vs one step either side (`protocol.SPEED`, throwaway branch); risk: squeezed lines.
- [ ] **E5** `PROXY_PACING` 100 B / 20 ms vs 120 B / 20 ms and 100 B / 15 ms (`studio_runtime.PROXY_PACING`, throwaway branch); risk: dropped writes (stop rule: any dropped rows -> restore, record).
- [ ] **E6** (user approval required, other proxy users affected) proxy scan window 300/320 ms -> 100/320 ms: a firmware change on `btproxy-kitchen` that the user performs; the plan only specifies what to measure afterwards (E0 protocol).
- [ ] **E7** (hardware) a dedicated ESP32-S3 proxy for the printer vs shared with the kettle; only if E1 says contention matters; the user installs it, the pinned `proxy_source` changes, E0 protocol is rerun.
- [ ] Adopted changes are separate commits citing the experiment section; `docs/transport-experiments.md` holds raw numbers for every arm, adopted or not. **Commit** `docs: record optional transport experiments (Refs #26)`.

---

### Task 17: Version 0.3.2 and release notes

Before value: `0.3.1` (Plan 2); after: `0.3.2`.

**Files:** `studio.py` (`PANEL_VERSION`), `manifest.json`, `diagnostics.py`, `README.md` (the `Current version:` line under the title that Plan 1 added; the `Direct local LE (since 0.2.2)` heading is a feature label and stays), `tests/browser_studio.py` (mock `printer_info` `version`), `tests/test_ha_bridge.py` (`test_component_versions_match_release`: three assertions), `docs/CHANGELOG.md` (created by Plan 1; append).

- [ ] **Step 1:** change the existing assertions in `test_component_versions_match_release` and the browser mock to `0.3.2` first; run that test -> FAIL (red for the right reason).
- [ ] **Step 2:** bump all six sites. **Step 3:** CHANGELOG `0.3.2`: job timeline in `last_result.timings` and an INFO line; waiting for the printer (proxy only), option `wait_for_printer_seconds` default 60 (`0` restores the old fail-fast behaviour); new sensor state `waiting_for_printer` (automations that match sensor states need it; the sensor attribute `last_result` gains `waiting_until`, `error_code`, `timings`); new error `printer_not_seen`; a raised default wait means a job for an absent printer holds the printer lock for up to the wait plus connect time. Link `docs/transport-experiments.md` and state which of E0-E7 ran.
- [ ] **Step 4:** all local commands green. **Step 5: Commit** `chore: release 0.3.2 (Refs #26)`.

---

## Self-review

Scope notes: `diagnose` does not wait (decision, documented in Task 12); native/bridge/`local_le` do not wait (the spec allows it only when the helper applies without extra risk, and only the pinned proxy scanner has a registry-by-source and a `discovered_device_timestamps` view). `printer_info` gains `wait_for_printer_seconds` (Task 9); `wait_until_fresh` takes a resolver (Task 5).
