# TinyPrint X6h native integration design

## Goal and boundary
Local HA printing without a vendor application, with an independently reusable
BLE packet encoder/renderer and HACS-compatible package. Target only X6h AF30
advertisements. Other X6-labelled devices may be different and are not claimed.
Hardware proof requires both a successful real transmission and readable paper.
No software mock or sent status can establish the latter.

## Architecture
Config flow: Bluetooth discovery + explicit manual MAC + optional YAML import.
Runtime: fresh connectable HA BLEDevice lookup, connection-only retries,
per-printer exclusion lock, no replay after partial transmission, cancellation
and bounded cleanup. HA exposes text, QR, local image actions, test button and
last-job sensor. Pillow work runs in an executor, not the event loop.

Protocol: 384 dots, raw A2 monochrome, CRC8, blank lead row, setup BD/AF/BE,
AE01 write without response, AE02 flow-control notifications. 20-byte chunks,
conservative pacing, bounded whole-job and flow-control timeouts. Validate
required GATT characteristics before sending. No battery/paper/completion
signals are inferred from unrelated notification bytes.

## Security and resource limits
No remote image fetches. HA allowed-path checks apply before image/font reads.
No font binaries in the repository. Bounds: 3000 text chars, 500 QR chars,
4 MiB/4 million image pixels, 2048 output rows. Printed content and device IDs
are omitted from diagnostics; HA automation traces remain HA-owned behavior.

## Installation and validation
Deploy as its own submodule and custom_components symlink in the existing HA
configuration, preserving all unrelated dirty files. Add only a new package
for the target printer and narrow font allowlist. Validate configuration before
restart. Check normal config entry and entities afterwards.

Tests: vectors/limits, mocked BLE cleanup/backpressure and real HA API dispatch.
HACS custom-repository installation is separate from default catalog acceptance.
Default listing requires successful HACS/Hassfest validation, a full release and
upstream review. Do not claim upstream submission or hardware approval prematurely.
