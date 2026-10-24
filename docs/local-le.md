# Direct LE printing on the Home Assistant host

`local_le` sends data from the Linux machine running Home Assistant directly to the X6h.
It does not use an HTTP bridge, `home`, a second server, a token, or BlueZ `Device1.Connect`.
Native HA Bluetooth/proxy mode remains the default; the remote bridge remains optional.

Choose **Direct LE on the Home Assistant host** in TinyPrint's configuration and enter the
local Bluetooth controller MAC from `bluetoothctl list`. This is NOT the printer MAC.
Local LE needs Linux Bluetooth socket access in the HA container. It does not work through
ESPHome proxies; use native mode for those. No pairing or system-wide Bluetooth mode change
is performed. A configured but unavailable adapter fails rather than falling back elsewhere.

An existing YAML-imported printer can instead be configured without an options-flow step:

```yaml
tinyprint:
  printers:
    - address: !secret tinyprint_ble_address
      name: "Desk printer"
      font_path: "/config/tinyprint_fonts/DejaVuSans.ttf"
      transport: local_le
      adapter_mac: "00:11:22:33:44:55"
```

Use the actual local adapter address. The existing printer entry and entity/device IDs are
preserved through HA's public import update API. Unchanged imports do not force reloads.
Explicit UI options override imported data. Bridge credentials are not accepted in YAML.
With `font_path` set, its directory still needs the existing root `homeassistant.allowlist_external_dirs`
entry. Without `font_path` no entry is needed: `font_path` is only an override, and Roboto Flex and
Tiny5 are bundled (see `studio.md`).

All existing print actions, the test button, Studio templates and the sidebar use the selected
transport. Preview remains offline. Local diagnosis reads the real GATT table without sending
raster data. Job phases distinguish `local_config`, `connecting_le`, `discovering_gatt` and
`transmitting`; partial-write counters survive failure/cancellation. Cleanup closes the socket
before waiting for the reader task, is bounded, and resists repeated cancellation. Failed or
partially sent jobs are never replayed automatically.

`sent` / `transport_only` means the transfer finished, not that paper or the QR was inspected.
`physical_print_confirmed` stays false. A connection by itself is not print acceptance.

## Adapter troubleshooting

A printer advertisement is not proof of a working data link. During an Odroid investigation,
the controller reported an LE connection and sent the first ATT request, but returned no ACL
data. Its kernel log identified an unbranded CSR clone. A scoped suspend/resume experiment did
not fix that observed failure. Such hardware/driver issues are separate from selecting LE
instead of Classic Bluetooth and are not fixed merely by selecting `local_le`.

Check the targeted HCI exchange and received ACL counts before blaming the renderer, paper or
QR payload. Do not negate a positive raw RSSI to make it look valid. No automatic adapter resets
or repeated print attempts are part of this integration.

## Verification

`tests/test_local_le.py` exercises the real ATT codec against an in-memory GATT peer and tests
resource cleanup and error counters. `tests/test_ha_local_le.py` checks HA routing, settings and
imports without real radio/network use. The browser harness checks the local-mode label using
the packaged frontend and a simulated HA connection. Those tests are not physical acceptance.
