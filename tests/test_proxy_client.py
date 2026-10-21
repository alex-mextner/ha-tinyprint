"""Pinned-proxy connection choice against fake scanners, with no radio or network."""

import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

try:
    from homeassistant.core import HomeAssistant
    from homeassistant.exceptions import HomeAssistantError
except ImportError as error:
    raise unittest.SkipTest("Run in the Home Assistant image") from error

from custom_components.tinyprint import CONFIG_SCHEMA, async_setup, async_setup_entry, proxy_client
from custom_components.tinyprint.config_flow import TinyPrintConfigFlow, TinyPrintOptionsFlow
from custom_components.tinyprint.studio_runtime import StudioPrinter

ADDRESS = "AA:BB:CC:DD:EE:FF"
PROXY = "11:22:33:44:55:66"


def scanner_device(source: str, client_factory: MagicMock | None):
    connector = None if client_factory is None else SimpleNamespace(client=client_factory)
    return SimpleNamespace(
        scanner=SimpleNamespace(source=source, name=source, connector=connector),
        ble_device=SimpleNamespace(address=ADDRESS),
    )


class ProxyClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_connects_through_requested_source_only(self):
        local, proxy = MagicMock(), MagicMock()
        devices = [
            scanner_device("00:11:22:33:44:55", local),
            scanner_device("AA:BB:CC:DD:EE:FF", proxy),
        ]
        with (
            patch.object(
                proxy_client.bluetooth, "async_scanner_devices_by_address", return_value=devices
            ),
            patch.object(proxy_client, "BleakClient") as bleak,
        ):
            bleak.return_value.connect = AsyncMock()
            client = await proxy_client.connect_via_proxy(None, ADDRESS, "X6h", "AA:BB:CC:DD:EE:FF")
        self.assertIs(client, bleak.return_value)
        backend = bleak.call_args.kwargs["backend"]
        backend("device")
        proxy.assert_called_once_with("device")
        local.assert_not_called()

    async def test_missing_proxy_fails_without_fallback(self):
        devices = [scanner_device("00:11:22:33:44:55", MagicMock())]
        with (
            patch.object(
                proxy_client.bluetooth, "async_scanner_devices_by_address", return_value=devices
            ),
            patch.object(proxy_client, "BleakClient") as bleak,
        ):
            with self.assertRaises(HomeAssistantError):
                await proxy_client.connect_via_proxy(None, ADDRESS, "X6h", "AA:BB:CC:DD:EE:FF")
        bleak.assert_not_called()

    async def test_connect_retries_then_raises_last_error(self):
        from bleak.exc import BleakError

        devices = [scanner_device("AA:BB:CC:DD:EE:FF", MagicMock())]
        with (
            patch.object(
                proxy_client.bluetooth, "async_scanner_devices_by_address", return_value=devices
            ),
            patch.object(proxy_client, "BleakClient") as bleak,
            patch.object(proxy_client, "RETRY_DELAY", 0),
        ):
            bleak.return_value.connect = AsyncMock(side_effect=BleakError("no"))
            bleak.return_value.disconnect = AsyncMock()
            with self.assertRaises(BleakError):
                await proxy_client.connect_via_proxy(None, ADDRESS, "X6h", "AA:BB:CC:DD:EE:FF")
        self.assertEqual(bleak.return_value.connect.await_count, proxy_client.CONNECT_ATTEMPTS)


class ProxyConfigTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self.tmp.name)
        await async_setup(self.hass, {})

    async def asyncTearDown(self):
        await self.hass.async_stop()
        self.tmp.cleanup()

    def entry(self):
        entry = MagicMock()
        entry.entry_id = "entry"
        entry.title = "Proxy"
        entry.data = {"address": ADDRESS, "transport": "proxy", "proxy_source": PROXY}
        entry.options = {}
        return entry

    async def test_yaml_accepts_proxy(self):
        validated = CONFIG_SCHEMA(
            {
                "tinyprint": {
                    "printers": [{"address": ADDRESS, "transport": "proxy", "proxy_source": PROXY}]
                }
            }
        )
        self.assertEqual(validated["tinyprint"]["printers"][0]["proxy_source"], PROXY)

    async def test_setup_selects_proxy_transport(self):
        with (
            patch("custom_components.tinyprint.async_setup_panel", new=AsyncMock()),
            patch.object(
                self.hass, "config_entries", SimpleNamespace(async_forward_entry_setups=AsyncMock())
            ),
        ):
            await async_setup_entry(self.hass, self.entry())
        printer = self.hass.data["tinyprint"]["entry"]
        self.assertEqual(printer.transport, "proxy")
        self.assertEqual(printer.proxy_source, PROXY)
        self.assertEqual(printer.bridge_state, "native")

    async def test_options_store_normalized_proxy_and_reject_bad_mac(self):
        flow = TinyPrintOptionsFlow(self.entry())
        flow.hass = self.hass
        result = await flow.async_step_init({"transport": "proxy", "proxy_source": PROXY.lower()})
        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(result["data"]["proxy_source"], PROXY)
        bad = await flow.async_step_init({"transport": "proxy", "proxy_source": "bad"})
        self.assertEqual(bad["type"], "form")
        self.assertIn("proxy_source", bad["errors"])

    async def test_import_proxy_and_invalid_proxy(self):
        flow = TinyPrintConfigFlow()
        flow.hass = self.hass
        flow.context = {"source": "import"}
        with (
            patch.object(flow, "async_set_unique_id", new=AsyncMock()),
            patch.object(flow, "_abort_if_unique_id_configured"),
        ):
            result = await flow.async_step_import(
                {"address": ADDRESS, "transport": "proxy", "proxy_source": PROXY}
            )
            bad = await flow.async_step_import({"address": ADDRESS, "transport": "proxy"})
        self.assertEqual(result["data"]["proxy_source"], PROXY)
        self.assertEqual(bad["type"], "abort")

    async def test_connection_uses_pinned_proxy_never_ha_routing(self):
        printer = StudioPrinter(self.hass, "entry", ADDRESS, "Proxy", None, proxy_source=PROXY)
        printer.begin()
        with (
            patch(
                "custom_components.tinyprint.studio_runtime.connect_via_proxy", new=MagicMock()
            ) as pinned,
            patch(
                "custom_components.tinyprint.studio_runtime.bluetooth.async_ble_device_from_address",
                side_effect=AssertionError("HA routing must not be used"),
            ),
            patch("custom_components.tinyprint.studio_runtime.connected_client") as wrapped,
        ):
            printer.connection()
        pinned.assert_called_once_with(self.hass, ADDRESS, "Proxy", PROXY)
        wrapped.assert_called_once()


if __name__ == "__main__":
    unittest.main()
