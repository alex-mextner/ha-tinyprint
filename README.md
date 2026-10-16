# TinyPrint X6h for Home Assistant

Local Bluetooth printing of text (including Cyrillic), QR codes and local images.
Designed for lowercase-h **X6h** devices advertising AF30, with AE01 write and
AE02 notification characteristics. This is **not** a generic X6H/MXW01/Phomemo driver.

## Verification status

Protocol/rendering/transport tests pass, as do 16 tests using real Home Assistant
2026.9.3 APIs. Bluetooth is mocked in these software tests. Physical printing on
the target unit is **not yet confirmed**: the device stopped advertising before
the hardware check. A successful service result means **sent**, never verified
paper output. Treat this initial version as an experimental integration.

## Direct local LE (0.2.2)

For an X6h on a Linux HA host, the opt-in `local_le` transport bypasses Classic
Bluetooth bearer selection without a second host, HTTP bridge or token. All
existing actions and Studio use it. See [configuration and hardware limitations](docs/local-le.md).
A successful software test is not physical print acceptance.

## Install

Add `https://github.com/alex-mextner/ha-tinyprint` in HACS > Custom repositories,
category Integration, download, and restart Home Assistant. This repository is
not yet included in the default HACS catalog. Alternatively copy only
`custom_components/tinyprint` into your HA config's `custom_components` directory.
HA 2026.9+ and a connectable Bluetooth adapter/proxy are required.

Add TinyPrint X6h under Settings > Devices & services. Discovery recognizes the
`X6h-*` / `x6h-*` name plus AF30 advertisement; manual entry accepts a Bluetooth
MAC address. Turn the printer on and close any phone app holding its connection.

For deployment without UI, this optional YAML creates a normal config entry:

```yaml
tinyprint:
  printers:
    - address: "AA:BB:CC:DD:EE:FF"
      name: "Desk printer"
      font_path: "/config/tinyprint_fonts/DejaVuSans.ttf"
```

No font binaries are bundled. For text, provide a TrueType font installed by you,
including Cyrillic glyphs when required. Explicit font paths and image paths
must be allowed by HA's `homeassistant.allowlist_external_dirs` setting. Use a
narrow directory, not the filesystem root. QR and image printing need no font.

## Actions

```yaml
action: tinyprint.print_text
data:
  text: "Hello from Home Assistant"
  font_size: 24
```

```yaml
action: tinyprint.print_qr
data:
  text: "https://example.com/spool/123"
```

```yaml
action: tinyprint.print_image
data:
  path: "/config/www/labels/spool.png"
```

With more than one configured printer, add `device_id` to select exactly one.
Each device also has a Test print button and Job status sensor. Actions can
return transport statistics through `response_variable` in HA scripts.

Only one job per printer is accepted; concurrent requests fail instead of
silently overlapping. A partially transmitted job is **never automatically
replayed**, to avoid duplicate labels. Switching off the printer causes a clear
failure. Connections and notification handlers are released on failure or
cancellation. There is no background polling that keeps a sleeping printer awake.

Print width: 384 dots. Maximum height: 2048 rows; text: 3000 characters; QR: 500
characters (dense codes are rejected if modules would be too small); images:
4 MiB and 4 million pixels. Rendering runs outside the HA event loop. BLE chunks
are at most 20 bytes with conservative pacing and bounded buffer-flow waits.
The total transfer budget scales with image height (60 to 600 seconds maximum),
so a proxy round trip is not confused with a stalled printer.

## Optional LE print bridge (0.2.1)

BlueZ connects dual-mode printers such as the X6h over BR/EDR, which fails, and the HA
host's adapter may simply be too far away. A small bridge service on a nearby Linux host
can talk to the printer over an explicit LE link instead (see `docs/bridge.md`). Home
Assistant still renders everything and sends only a validated 384-wide 1-bit bitmap.

Open **Settings > Devices & services > TinyPrint > Configure** as an administrator, choose
**TinyPrint bridge**, then enter the bridge URL and access token. Saving checks the bridge
before storing anything (`cannot connect` / `access token rejected` are shown in the form).
A blank token keeps the saved one for the same URL; picking native Bluetooth again removes
the bridge address and token. Without these options nothing changes: the native Bluetooth
and ESPHome path stays the default.

In bridge mode the old actions, Studio actions, test button and sidebar all use the same
printer lock and the bridge. Printing and diagnosing never use HA's own Bluetooth (its scan
data in `printer_info` is only labelled as local), so a printer that HA cannot see still prints. A broken bridge configuration, an unreachable bridge or a lost
answer fails closed: there is no fallback to local Bluetooth and no automatic replay, and a
request whose answer was lost is reported as `delivery_uncertain`. The sidebar and
`printer_info` show the transport and the bridge's HTTP state (reachable, busy, token
rejected, unreachable); a reachable bridge does not prove the printer link, run **Diagnose**
for that. Bridge URL and token never appear in diagnostics or action responses.

## Privacy and limitations

Print content, MAC addresses and font/file paths are excluded from integration
diagnostics. The integration logs job metadata, not the printed text. Home
Assistant's own automation traces can still contain action inputs.

No battery percentage, paper state or physical print-completion acknowledgement
is claimed. The sensor exposes `physical_print_confirmed: false`; `sent` only
means that transmission completed without a reported transport error.

The raw A2 monochrome protocol was implemented independently from public
protocol descriptions, particularly
https://github.com/cicloid/printable/blob/main/docs/PROTOCOL.md .
The X6h-specific reference https://github.com/nazarovmi/tinyprint-x6h helped
identify AF30 advertising, but this integration does not depend on its old
Bleak or python-lzo versions. Other printer families are deliberately excluded.

## Development and HACS

Run `pytest tests/test_core.py tests/test_transport.py` and `ruff check .`.
`tests/test_ha.py` runs via unittest inside the actual HA 2026.9.3 image, with
qrcode 8.2 installed to an isolated test directory. CI additionally runs
Hassfest and HACS validation. A passing action is not a hardware test.

HACS default-list submission additionally needs successful validation, a full
GitHub release after checks pass, and review of a PR to `hacs/default`. Local
brand assets are provided under `custom_components/tinyprint/brand`. Catalog
acceptance is separate from installation as a custom repository.

MIT licensed. No cloud service, vendor application or account required.
