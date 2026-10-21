# TinyPrint bridge: implementation plan (ticket #6)

Spec: `docs/superpowers/specs/2026-10-16-tinyprint-bridge-design.md`. Each step is test first
(red, then green), unit tests use fakes and never touch real BLE.

1. `bridge_wire.py`: validators and job view contract. `tests/test_bridge_wire.py`.
2. `att.py` plus fake GATT peer: dynamic discovery, malformed and non-progress rejection, CCCD,
   notifications, indication ACK, peer MTU request, 20-byte write commands, cleanup.
   `tests/test_att.py`.
3. `bridge/tinyprint_bridge`: config, auth, job manager (busy, duplicate, cancel waits for
   cleanup, partial failure counters, bounded retention), L2CAP LE link, aiohttp server, CLI.
   `tests/test_bridge_*.py`.
4. `bridge_client.py` and HA routing: options flow, `StudioPrinter` remote path for print,
   diagnose, cancel; redaction; button; `tests/test_ha_bridge.py` (HA image).
5. Installer, systemd user unit, `docs/bridge.md`, README, CI, version 0.2.1.
6. Full checks (ruff, pure tests, HA-image tests), staged review, commit, push, dependent PR.
