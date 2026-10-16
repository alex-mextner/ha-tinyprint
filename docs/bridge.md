# TinyPrint LE print bridge

An optional, authenticated HTTP service that prints a bitmap on the X6h over an explicit
Bluetooth LE link. Home Assistant keeps rendering labels as before; only the last hop (talking to
the printer) moves to a small Linux host near it. Without a bridge URL configured, TinyPrint
behaves exactly as before (native BLE / ESPHome proxy).

## Why it exists

* The X6h is a dual-mode chip. BlueZ `Device1.Connect` (used by Bleak and Home Assistant) picks
  BR/EDR for it and fails with `ProfileUnavailable` or an SDP timeout; no LE GATT link is formed.
  The bridge opens a kernel L2CAP socket on the ATT fixed channel (CID 4, `BDADDR_LE_PUBLIC`),
  which can only form an LE link, from a normal user, without changing BlueZ settings.
* The Home Assistant host's Bluetooth adapter may simply not hear the printer. The bridge runs on
  a host that does.

```
HA (renders, owns the lock and UI)  --HTTP + bearer-->  bridge  --LE ATT-->  printer
```

## Install (on the host near the printer)

Requirements: Linux with BlueZ, Python 3.14 (the installer checks), `systemd --user`. No root
needed. Install from a checkout that stays on disk, so that you can update by pulling and
re-running the installer; the installed copy under `app/` does not depend on the checkout path.

```sh
git clone https://github.com/alex-mextner/ha-tinyprint ~/.local/share/tinyprint-bridge/source
cd ~/.local/share/tinyprint-bridge/source && git checkout <exact reviewed commit>
bridge/install.sh --printer-mac AA:BB:CC:DD:EE:FF --adapter-mac 00:11:22:33:44:55 \
    --bind 100.74.26.95 --port 8129   # example: the host's Tailscale IP and a free port
```

Omit `--bind`/`--port` to listen on `127.0.0.1:8765`. The checkout may live anywhere; keeping it
inside the data directory (as above) is safe because the installer only replaces `app/`, `venv/`
and its own config. Do not run `rm -r` on the data directory (see Uninstall) unless you mean to
delete that checkout too.

Find the printer address with `bluetoothctl devices` or from the TinyPrint advertisement
(service `AF30`), and the adapter address with `bluetoothctl list`.

The installer is idempotent. It copies the code to `~/.local/share/tinyprint-bridge/app`
(`bridge/` plus the four shared modules `protocol.py`, `transport.py`, `att.py`,
`bridge_wire.py` under `custom_components/tinyprint/`; keep that layout or set
`TINYPRINT_SHARED_DIR`), creates a venv with `aiohttp`, writes the unit to
`~/.config/systemd/user/`, and starts it. Options: `--no-systemd`, `--no-venv`, `--prefix`,
`--config-dir`, `--bind`, `--port`; `--prefix` and `--config-dir` must be absolute paths of plain
characters (no spaces). `--bind`/`--port` only apply when `bridge.env` is first created. Re-running
replaces code only: an existing `bridge.env` and `token` are never overwritten, the token is never
printed, and an installed `aiohttp` is not re-downloaded (re-runs work offline). The code tree
`<prefix>/app` is only replaced when it carries the installer's marker file, so a wrong `--prefix`
cannot delete someone else's `app/` directory. `--no-venv` on a fresh install needs
`--no-systemd` (the unit points at `<prefix>/venv`).

To keep the user service running when you are logged out: `loginctl enable-linger $USER`.

## Configuration

`~/.config/tinyprint-bridge/bridge.env` (mode 600), loaded by the unit:

| Variable | Meaning |
| --- | --- |
| `TINYPRINT_BRIDGE_PRINTER_MAC` | The only printer the bridge will ever contact. Required. |
| `TINYPRINT_BRIDGE_ADAPTER_MAC` | Local adapter to bind the LE socket to. Required. |
| `TINYPRINT_BRIDGE_TOKEN_FILE` | File with the bearer token (mode 600, owned by the service user, at most 4 KiB). |
| `TINYPRINT_BRIDGE_TOKEN` | Alternative to the file (visible in the process environment; prefer the file). |
| `TINYPRINT_BRIDGE_BIND` | IP address to listen on. Default `127.0.0.1`. Hostnames are refused. |
| `TINYPRINT_BRIDGE_PORT` | Default `8765`. |

`--bind`/`--port` flags override the environment. The bridge refuses to start (exit code 2, with
an actionable message that never contains the token) when a MAC is missing or malformed, the token
is missing, shorter than 32 characters or contains whitespace/non-ASCII, or the token file is a
symlink, not a regular file, group/world accessible, or not owned by the service user. Binding to
`0.0.0.0` or `::` logs a warning.

No request can choose an address, path, URL or command.

### Token

The installer generates a 64-character token in `~/.config/tinyprint-bridge/token` (mode 600).
Read it on the bridge host with `cat ~/.config/tinyprint-bridge/token` and paste it into the
TinyPrint options in Home Assistant (password field). Do not type it into a shell command line.
To rotate: write a new value to the token file, `systemctl --user restart tinyprint-bridge`, update
Home Assistant.

### Tailscale

Bind to the host's Tailscale IP (`tailscale ip -4`) so the service is reachable only over the
tailnet, then use `http://<that ip>:<port>` (for example `http://100.74.26.95:8129`) as the bridge URL in Home Assistant. Do not expose the
port to the internet. Traffic is plain HTTP; the tailnet provides the encryption.

## HTTP API (v1)

Every route, including unknown paths and wrong methods, answers `401` with
`WWW-Authenticate: Bearer` unless `Authorization: Bearer <token>` matches (constant-time compare of
SHA-256 digests). Bodies are JSON (`Content-Type: application/json`), at most 256 KiB.
Errors: `{"error": {"code": "...", "message": "..."}}` with codes `unauthorized` 401,
`bad_request` 400 (also 408 when an authenticated body does not arrive within 15 s), `payload_too_large` 413, `unsupported_media_type` 415, `busy` 409,
`duplicate_job` 409, `not_found` 404, `method_not_allowed` 405.

| Route | Purpose |
| --- | --- |
| `GET /v1/status` | Cheap health. Never touches the radio. |
| `POST /v1/jobs` | Start a print. `{"job_id", "rows", "bitmap_b64"}` -> 202 and the job view. |
| `POST /v1/diagnose` | Start a diagnose job (connect + GATT survey, no print). `{"job_id"}` -> 202. |
| `GET /v1/jobs/{job_id}` | Poll the job view. |
| `POST /v1/jobs/{job_id}/cancel` | Cancel. Answers only after the worker and socket are cleaned up (bounded 15 s). |

Print body: `job_id` matches `[A-Za-z0-9_-]{8,64}`; `rows` is an integer (not boolean) from 1 to
2048; `bitmap_b64` is strict base64 of exactly `rows * 48` bytes (384-dot rows, MSB first, black =
1). Unknown keys are rejected, and everything is validated before the radio is used.

Job view: `job_id`, `kind` (`print`|`diagnose`), `state`
(`accepted -> connecting -> discovering -> transmitting -> sent | failed | cancelled`),
`terminal`, counters (`bytes_written`, `frames_written`, `writes`, `notifications`,
`flow_pauses`), `delivery_uncertain`, `confirmation`, `physical_print_confirmed` (always `false`),
`elapsed_seconds`, `error` (`{code, message}` or null), `diagnosis` (diagnose jobs only). A
successful diagnose job also ends in state `sent`, with `confirmation: "connection_only"`.

```sh
TOKEN_FILE=~/.config/tinyprint-bridge/token
curl -s -H "Authorization: Bearer $(cat "$TOKEN_FILE")" http://127.0.0.1:8765/v1/status
curl -s -X POST -H "Authorization: Bearer $(cat "$TOKEN_FILE")" \
     -H 'Content-Type: application/json' -d '{"job_id": "diag-0001"}' \
     http://127.0.0.1:8765/v1/diagnose
curl -s -H "Authorization: Bearer $(cat "$TOKEN_FILE")" http://127.0.0.1:8765/v1/jobs/diag-0001
```

Never paste the token into the command line itself (shell history); read it from the file as above.

### Semantics

* One hardware job at a time, no queue: a second start while one is active gets `409 busy`.
* A `job_id` that was used before (running, finished, or evicted from the retained results while
  still in the bounded recent-ids memory) gets `409 duplicate_job`. The bridge never replays a job.
* `202` means accepted, not printed. `sent` means all ATT writes returned and the settle delay
  passed. The printer sends no "print finished" signal, so `physical_print_confirmed` stays
  `false` and `confirmation` is `transport_only`.
* `delivery_uncertain` is true when a write raised or was cancelled, or when the job failed or was
  cancelled after bytes were written. Counters are preserved. Do not retry such a job
  automatically: part of the raster may already be on the printer.
* Results are kept in a ring of 32; recent ids are remembered for 4096 jobs.
* If a cancel cannot finish within its bound the response has `terminal: false`, and the slot
  stays busy until the worker really ends.

## Home Assistant

Settings -> Devices & services -> TinyPrint -> Configure: choose transport `bridge`, enter the
bridge URL and the token. Saving validates with `GET /v1/status` (`cannot_connect`,
`invalid_auth`). Choose `native` to go back (this clears the stored bridge URL and token). In
bridge mode Home Assistant needs no Bluetooth adapter or advertisement; it fails closed, with no
silent fallback to native BLE, when the bridge is unreachable. A network timeout after a print was
submitted is reported as `delivery_uncertain` and is never retried. The bridge URL is admin-only
and is never accepted from a printable document or an ordinary user.

## Troubleshooting

```sh
systemctl --user status tinyprint-bridge
journalctl --user -u tinyprint-bridge -n 100
```

* Exit code 2 / "configuration error": read the message, fix `bridge.env` or the token file
  (mode 600 and owned by you), `systemctl --user restart tinyprint-bridge`.
* `status` is healthy but prints fail: run a diagnose job. `status` never touches Bluetooth.
* `connect_timeout` / `connect_failed`: the printer is off, out of range, connected to another
  device (phone app), or `ADAPTER_MAC` is wrong (`bluetoothctl list`). Only one device can hold the
  LE link.
* `profile_unsupported`: the device at that address is not an X6h (no AE30/AE01/AE02 with a CCCD).
  The diagnose job's `diagnosis.profile_problem` says what is missing.
* `att_protocol_error`: malformed or missing ATT answers; `diagnosis.stray_pdus` counts unexpected
  PDUs.
* `link_lost`, `write_failed`, `flow_pause_timeout`, `transfer_timeout`: the job stopped part-way;
  see `delivery_uncertain` before printing again.
* `Failed with result 'exit-code'` with status 218 or 226 in the journal: your user manager cannot
  apply the sandbox lines. Add a drop-in (`systemctl --user edit tinyprint-bridge`) overriding
  `ProtectSystem=` / `ProtectHome=` rather than editing the shipped unit; keep `NoNewPrivileges`.
* Do not change the global Bluetooth mode or restart BlueZ for this; neither is needed.

## Security notes

* Authentication precedes body reading on every route; unauthenticated callers learn nothing
  about routes. There is no rate limiting: keep the port on a private interface.
* Secrets stay out of the repository, argv, logs and responses. Logs carry only job id, phase,
  counters and error code (never bodies, bitmaps, tokens, headers or addresses).
* The unit sets `NoNewPrivileges`, `PrivateTmp`, `ProtectSystem=strict`, `ProtectHome=read-only`
  and restricts address families to inet, unix and Bluetooth; it has no capabilities.
* Request limits: 256 KiB body, 2048 rows, strict base64 length checked before decoding.

## Uninstall

```sh
systemctl --user disable --now tinyprint-bridge
rm ~/.config/systemd/user/tinyprint-bridge.service && systemctl --user daemon-reload
rm -r ~/.local/share/tinyprint-bridge ~/.config/tinyprint-bridge   # also deletes the token and a checkout kept in source/
```

## Limitations

No physical confirmation (the printer has none), one job at a time, X6h only (single configured
printer), plain HTTP (use a private network), LE public address only.
