# TinyPrint transport reliability: measured timeline and waiting for the printer

Status: design approved in conversation 2026-10-22; written spec awaiting review. Implemented **after**
`2026-10-22-tinyprint-studio-wysiwyg-design.md` (the user chose that order).

## Problem

The user reports the mini printer is slow to be found, prints slowly and sometimes is not found at all.
Deployment (verified in the odroid HA log, 2026-10-22): `proxy` transport through ESPHome proxy
`btproxy-kitchen` (30:C9:22:D1:A5:72), which also serves the Xiaomi kettle integration. The
`ha-tinyprint-config/packages/tinyprint.yaml` copy on the laptop still says `local_le` and is stale.

### Evidence

| Time | Outcome | Bytes | Connect | Notify | Transmit | Total |
| --- | --- | --- | --- | --- | --- | --- |
| 10-21 08:03 | sent | 18,376 | 9.4 s | 1.3 s | 13.6 s | 24.6 s |
| 10-22 12:19:46 | sent | 8,007 | 19.3 s | 2.8 s | 10.9 s | 33.0 s |
| 10-22 12:20:34 | failed `connecting_gatt` (BleakError) | 0 | gave up | - | - | 39.3 s |
| 10-22 12:50:38 | sent | 8,007 | 2.0 s | 0.15 s | 11.1 s | 13.2 s |
| 10-21 05:31, 05:37 | failed "not visible to the selected proxy" | 0 | - | - | - | 0.175 s, 0.018 s |

- Earlier proxy connects (10-21 00:00-07:11, 11 jobs): median 2.3 s, range 0.7-3.5 s. Today's range
  is 2.0-19.3 s then a 39 s failure.
- "Not found" is an instant failure, not a long search: `proxy_client.py` (~44-64) reads HA's advertisement
  cache with `async_scanner_devices_by_address(connectable=True)`, filters on the pinned proxy source and
  raises at once if the printer was not advertised. It never waits for an advertisement.
- Whether the X6h powers itself off or merely stops advertising is **undocumented** in every source checked
  (repo, cicloid PROTOCOL.md, nazarovmi/tinyprint-x6h, Cat-Printer). The earlier claim "it auto-powers-off"
  came from an agent summary, not a source. No BLE command to read or set an auto-off timer was found.
  Advertising interval: unknown.
- Paced BLE writes are a minor part of job time. For a 384x300 label: 397 row frames x 56 B = about
  22.3 KB, at 100 B / 20 ms (`studio_runtime.py:32`) = about 4.7 s of writes. The `Transmit` column above
  is longer than that because it also contains the printer consuming rows. The rest is connect and that
  consumption: implied 25-41 rows/s from the A3 reply timing [INFERENCE: the A3 reply is deferred until
  the buffer is consumed; the logged byte totals are not multiples of 56, so the plan reconciles bytes to
  rows before quoting a rate]. Typical total 15-25 s.
- The 12:20:34 failure and the 12:19:46 slow connect both followed kettle activity on the same proxy
  (kettle reconnect 12:21:17-22; kettle write 12:19:00) [INFERENCE: contention, unproven]. The proxy has 3
  connection slots; no slot exhaustion was observed.
- Printer RSSI at the pinned proxy: -85 dBm (raw, 12:34:57). The user says the printer was possibly in another
  room at that time and has been next to the proxy since; the value is not representative until re-measured
  with the printer on and in place.
- The proxy firmware scans actively with interval 320 ms and window 300 ms (94% duty cycle) on a single
  Wi-Fi/BLE radio (`kettle-xiaomi/esphome/d32/base.yaml`). Coexistence impact: [INFERENCE only].
- Today's HA log is INFO-only: no per-write progress or timing breakdown exists after the fact.

## Goals

1. A powered-off or silent printer yields a clear, patient behaviour instead of an instant error.
2. Every job leaves a payload-free timing breakdown, so each later change is judged by numbers.
3. Evidence-based tuning only: no constant changes without a measurement protocol result.

## Non-goals

- Waking a printer that is not advertising (no mechanism exists).
- Changing `PROXY_PACING` (100 B / 20 ms is an empirical ESP32 limit recorded at `studio_runtime.py:29-32`; 177 B / 4 ms drops
  writes), `SPEED`, `TEXT_ENERGY` or `DEFAULT_TAIL_ROWS` without Experiment results below. An earlier session saw squeezed
  lines at energy 24000 / BD=16.
- Promising a print-speed improvement. The printer's own consumption rate bounds it.
- Flashing the proxy firmware without the user's explicit approval.

## Design

### 1. Job timeline

Each job records phase durations in seconds: `lock_wait` (time waiting for the per-printer lock, 0 when
free), `visible_wait` (time spent waiting for the printer to appear, 0 if already visible), `connect`
(lookup to connected), `notify` (connected to CCCD write done), `transmit` (first write to last write),
`printer_wait` (last write to idle A3 reply) and `total`, plus counts: `rows`, `bytes`, `writes`,
`flow_pauses`, `connect_attempts`, and the proxy name (`proxy`; never the MAC, matching how
`proxy_client.py` logs the proxy). The new `transmit` excludes the printer's consumption time, so E0
numbers are not comparable to the `Transmit` column of the evidence table. The object goes to
`last_result`, the history entry (`timings`, defined here and referenced by the Studio spec) and one INFO
log line per job (`tinyprint job timings ...`, metadata only). Failures keep the partial timeline and the
failing phase.

### 2. Waiting for the printer

- New phase `waiting_for_printer`, entered when the pinned proxy has no *fresh* advertisement for the
  printer. HA's connectable advertisement cache keeps a silent device listed for up to 195 s
  (`CONNECTABLE_FALLBACK_MAXIMUM_STALE_ADVERTISEMENT_SECONDS`, habluetooth 6.26.11), so "listed" is not
  "present". Freshness is `time.monotonic() - scanner.discovered_device_timestamps[address]` below
  `ADVERT_FRESH_SECONDS` (30 s to start; the plan adjusts it from the measured advertising interval) on
  the pinned scanner, the same per-scanner view `proxy_client.py` already uses
  (`async_scanner_devices_by_address`). The wait polls that view once per second.
  `async_process_advertisements` is not used: the Bluetooth manager delivers an advertisement to callbacks
  only from the source it currently prefers for that address, so a stronger second scanner would hide the
  pinned proxy and the wait would time out with the printer on.
  If all connect attempts fail and no advertisement newer than the start of the connect arrived, the job
  enters `waiting_for_printer` for the remaining wait seconds instead of failing as `connecting_gatt`.
  [INFERENCE: `discovered_device_timestamps` semantics and the 195 s window are read from habluetooth
  source, not exercised on the device; the plan verifies both on the odroid with the printer switched off.]
- Option `wait_for_printer_seconds` (same name in the options flow and in YAML), whole seconds, 0-300,
  default 60. `0` keeps today's fail-fast. Configured in the options flow next to `proxy_source`; the YAML
  import maps it like `proxy_source`. Raising the default from today's instant failure means a job for an
  absent printer holds the printer lock for up to 60 s, during which other jobs get "Printer is busy".
- The wait holds the existing per-printer job lock (one job per printer is already the rule). The wait runs
  as a task that the `cancel` action cancels, and it ends without connecting if the integration unloads.
  The `print_bitmap` / `print_document` call stays blocking until the job ends, as it is today; the panel
  disables Print while a job for that device is in flight and shows the waiting status from the sensor.
- After the printer is fresh, the normal connect budget applies unchanged (3 attempts of 20 s each, inside
  one 40 s total budget, `proxy_client.py:21-26`).
- Timeout result: failed, phase `waiting_for_printer`, error code `printer_not_seen`, message names the
  waited seconds and says to switch the printer on or move it closer. It is not `delivery_uncertain`; no
  bytes were sent.
- Status is pushed, not polled. The runtime writes `phase`, `wait_seconds` and `waiting_until` (UTC
  ISO-8601) into `last_result` on every phase transition and updates the job sensor state; the sensor
  already exposes `last_result` as attributes (`sensor.py:27-34`) and its status reaches the entity by
  dispatcher (`runtime.py:87-89`). The panel has no entity subscription today (it polls `printer_info`),
  so it gains one (`hass.connection.subscribeEntities`) and shows "Printer not seen: switch it on,
  printing starts automatically (N s left)", counting down locally from the moment the state arrives using
  `wait_seconds` (no comparison of browser and server clocks). Automations get the same state for free.
  `TinyPrintJobSensor` has a fixed enum (`_attr_options`, `sensor.py:16`); add `waiting_for_printer` to it
  and to the translations.
- Scope: the `proxy` transport (the deployed one). Native, bridge and `local_le` keep their behaviour
  unless the plan finds the same helper applies without extra risk.

### 3. Measurement protocol (needs the user and a powered-on printer; runs after 1 and 2 ship)

Reference bitmap: a fixed packed fixture (`tests/fixtures/reference_247.bin`, 247 rows, SHA-256 recorded in
the experiment doc) rendered once from the label of the 10-21 08:03 job (the original bytes were not
kept), printed through `print_bitmap` in every run. Each experiment: at least 5 runs, one variable
changed, medians from the timeline, every output photographed; a change is adopted only if the median total
improves and the print has no visible defect (squeezed or dropped rows, swallowed last line).

| ID | Variable | Risk |
| --- | --- | --- |
| E0 | Baseline, kettle integration active | none |
| E1 | Same, kettle integration paused (is connect time contention?) | none |
| E2 | Completion poll interval 3 s to 0.5 s (`transport.py:123` `state_poll`; the tests pass explicit values, so none asserts the default) | none expected |
| E3 | Tail rows 96 / 64 / 32 (the tail exists because the last lines were swallowed) | last line lost |
| E4 | `SPEED` 10 vs one step either side | squeezed lines |
| E5 | Pacing 100 B / 20 ms vs 120 B / 20 ms and 100 B / 15 ms | dropped writes |
| E6 | Proxy scan window 300/320 ms to 100/320 ms (firmware change, user approval required) | other proxy users |
| E7 | Dedicated proxy for the printer vs shared with the kettle (user has a spare ESP32-S3) | hardware |

E1 decides whether E7 is needed. Also read the printer RSSI at the proxy at the start of each session and
record it with the results.

### 4. Printer status bits (log only)

Read the A3 status byte already returned by `probe_printer` and the closing A3 and put the decoded bits
(`out_of_paper`, `cover_open`, `overheating`, `low_battery`, `charging`, `printing`) into the timeline.
No gating and no UI claim: the bit meanings come from cat-printer lore and are unverified on the X6h
(README states no battery or paper state is claimed). Promotion to UI requires captured real replies.

## Documentation and tests

- `docs/esphome-proxy.md`: the waiting behaviour, the option, and the `printer_not_seen` error.
- `docs/studio.md`: the waiting status line and `last_result.phase` list.
- Fake-clock tests for `proxy_client` / job layer: printer appears after N s (job proceeds), never appears
  (`printer_not_seen`, no bytes, lock released), listed but stale advertisement enters the wait, cancel
  during wait, a fresh advertisement on the pinned scanner while a stronger second scanner owns the address,
  `wait_for_printer_seconds: 0` fails fast, timeline fields present on success and on failure, YAML import
  of `wait_for_printer_seconds`.
- Existing pacing tests keep encoding the defaults they test: the 6 ms per-write estimate at
  `tests/test_transport.py:215-217` and the 20 B packet limit at `:53`.

## Acceptance

1. With the printer switched off, a print waits, shows the waiting status, and starts by itself within the
   job's time once the printer is switched on; with nothing switched on it fails after the configured seconds
   with `printer_not_seen`, sends nothing and frees the lock.
2. Every job has a timeline in `last_result`, history and one INFO log line.
3. E0-E2 results (median connect, transmit, printer_wait, total over at least 5 jobs each) are written to
   `docs/` with raw numbers; any adopted tuning change cites its experiment.
4. A physical print confirms the waiting flow end to end and is photographed.
