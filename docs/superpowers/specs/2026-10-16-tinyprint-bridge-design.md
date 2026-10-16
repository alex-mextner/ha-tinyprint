# TinyPrint LE print bridge: design (ticket #6)

## Problem (proven on hardware, 2026-10-16)

1. The X6h is a dual-mode JieLi chip. BlueZ `Device1.Connect` (what Bleak, bleak-retry-connector
   and Home Assistant use) picks BR/EDR for it and fails with `ProfileUnavailable` or an SDP
   timeout. It never forms an LE GATT link. A kernel L2CAP socket on the ATT fixed channel
   (CID 4, `BDADDR_LE_PUBLIC`) is LE-only by construction and printed a label from a normal user
   without touching BlueZ configuration.
2. The Odroid HA host's CSR adapter hears 0 printer adverts in 20 s; a host in the same flat
   hears 14. Local reach, not only the bearer, is broken.

A bridge on the nearby Linux host fixes both without relocating HA or buying hardware. Native
BLE/ESPHome mode stays the default for everyone else.

## Components

```
HA (renders, owns lock/UI)  --HTTP+bearer-->  bridge (nearby Linux host)  --LE ATT-->  printer
```

| Piece | Location | Notes |
| --- | --- | --- |
| `protocol.py`, `transport.py` | `custom_components/tinyprint/` | Existing pure codec and flow-controlled sender. Reused by both sides, never copied. |
| `att.py` | same | New pure ATT client (PDU codec, dynamic discovery, reader task). No HA, no sockets. |
| `bridge_wire.py` | same | New pure wire contract: validators and constants shared by bridge and HA client. |
| `bridge_client.py` | same | HA-side aiohttp client plus job runner. Only module that talks to the bridge. |
| `tinyprint_bridge/` | `bridge/` | Standalone server: config, auth, job manager, L2CAP LE link, CLI. |
| installer, unit | `bridge/` | Idempotent user-level install. |

The bridge process loads the shared pure modules through a synthetic package (no execution of
the HA-importing package `__init__`). Same pattern the tests already use.

## Bridge HTTP API (v1)

All routes, including unknown paths, require `Authorization: Bearer <token>` (constant-time
compare of SHA-256 digests) before any body is read. Token is mandatory and at least 32 chars.
Bodies are JSON only, at most 256 KiB. Errors are `{"error": {"code": "...", "message": "..."}}`.

| Route | Purpose |
| --- | --- |
| `GET /v1/status` | Cheap health. Never touches the radio. Version, busy flag, active job id, retained count. |
| `POST /v1/jobs` | Start a print job. Returns 202 immediately. |
| `POST /v1/diagnose` | Start a diagnose job (explicit connect, GATT discovery, no print). |
| `GET /v1/jobs/{job_id}` | Poll. Phase, counters, outcome. |
| `POST /v1/jobs/{job_id}/cancel` | Cancel; returns only after worker and socket cleanup finished (bounded). |

Print body (strict, unknown keys rejected): `job_id` (`[A-Za-z0-9_-]{8,64}`), `rows` (int, 1..2048,
`bool` rejected), `bitmap_b64` (strict base64 that decodes to exactly `rows * 48` bytes; encoded
length is bounded before decoding). Validation precedes any radio activity.

Rules: one hardware job at a time, no queue (second start -> 409 `busy`); a `job_id` seen
before is 409 `duplicate_job` (bounded memory of ids); the bridge never replays; results retained
in a bounded ring.

Job state: `accepted -> connecting -> discovering -> transmitting -> sent | failed | cancelled`.
Terminal result carries `bytes_written`, `frames_written`, `writes`, `notifications`,
`flow_pauses`, `delivery_uncertain`, `error{code,message}` and always
`physical_print_confirmed=false`, `confirmation="transport_only"`. `sent` is set only after all
ATT writes returned and the settle delay passed.

## ATT client rules

* Dynamic discovery: primary services (0x2800) to find AE30, characteristics (0x2803) inside its
  range for AE01 and AE02, Find Information (0x04) bounded by the AE02 characteristic end for the
  CCCD (0x2902). No hardcoded handles.
* Reject: short or malformed PDUs, zero entry length, entry length not 6/20, 7/21, 4/18, handles
  outside the requested range, non-increasing handles (non-progress pagination), error responses
  for another opcode, response opcode mismatch. Pagination is capped.
* Single reader task. Responses go to the one outstanding request; notifications pause/resume the
  sender through `NotificationParser`; indications are confirmed (0x1E); peer MTU requests are
  answered with the default 23; other peer requests get Request Not Supported; stray responses
  are counted and dropped.
* Writes are Write Commands of at most 20 bytes; pacing is `transport.send_bitmap`'s.
* Every send, receive, connect and cleanup step has a timeout. After an ATT transaction timeout
  the channel is considered dead.
* Cleanup on failure or cancel: stop notifications (CCCD 0), stop the reader, close the socket,
  each bounded.

## Config (bridge)

Printer MAC, adapter MAC, bind, port from an env file outside the repo; token from `TOKEN_FILE`
(mode must not be group or world readable) or env. Default bind is `127.0.0.1`; the deployment
sets the Tailscale IP explicitly. The request can never choose an address.

## HA side

Options flow only (admin UI): `transport` (`native` default, `bridge`), `bridge_url`,
`bridge_token` (password selector; blank keeps the stored value). Saving `native` clears the
bridge fields. `bridge` is validated with a status call (`cannot_connect`, `invalid_auth`).
`StudioPrinter` routes print, diagnose and cancel through `bridge_client` when a bridge is
configured; it never needs a local BLEDevice. A bridge misconfiguration fails closed (no silent
fallback to native BLE). Network timeouts after submit are reported as
`delivery_uncertain`, never retried. HA cancel posts bridge cancel (bounded) before returning.
`printer_info` and diagnostics expose transport mode and bridge state with the URL and token
redacted; local RSSI is labelled as local only.

## Out of scope

Physical acceptance, deployment and live HA changes are owned by the coordinator.
