# Printing through a chosen ESPHome Bluetooth proxy

`proxy` runs the job on the LE radio of one ESPHome Bluetooth proxy that you name, instead of
letting Home Assistant pick the connection path. Native mode picks by signal strength; when
the HA host's own adapter is nearer, BlueZ reaches the dual-mode X6h over BR/EDR and fails.
In `proxy` mode there is never a fallback to another adapter or proxy.

Choose **Through a chosen ESPHome Bluetooth proxy** in TinyPrint's configuration and enter the
proxy's Bluetooth MAC (the scanner `source` in the Bluetooth integration diagnostics, e.g.
`AA:BB:CC:DD:EE:FF`). The printer must be visible to that proxy and the proxy needs a free
connection slot. Or configure it in YAML:

```yaml
tinyprint:
  printers:
    - address: !secret tinyprint_ble_address
      name: "Desk printer"
      transport: proxy
      proxy_source: "AA:BB:CC:DD:EE:FF"
```

Only the connection is retried (3 attempts); a started job is never replayed. The result
remains `transport_only`: the printer has no print-completed acknowledgement.
