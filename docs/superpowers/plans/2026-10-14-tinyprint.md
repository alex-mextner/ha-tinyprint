# Implementation and verification plan

1. Inspect target advertisement, deployed HA/Python/Bleak and authoritative protocol.
2. Write failing protocol/render tests; implement bounded frame encoding and rendering.
3. Write failing BLE transport tests; implement pacing, flow control, cleanup, no replay.
4. Add HA configuration, import/discovery/options, device entities and actions.
5. Test against real HA 2026.9.3 APIs; distinguish real APIs from mocked Bluetooth.
6. Audit privacy, perform independent code review, run lint/tests, publish clean source.
7. Install isolated integration/submodule with only printer-specific HA configuration.
8. Validate config, restart safely, check live entry/entities and actual service dispatch.
9. Attempt one hardware check when printer advertises; never claim paper success otherwise.
10. Record HACS readiness and remaining upstream/hardware requirements.

Progress and raw environment-specific evidence stay in git-ignored verification/.
