"""Pinned-proxy connection choice against fake scanners, with no radio or network."""

import asyncio
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


def slow_failing_client(timeouts: list[float]):
    """A BleakClient stand-in whose connect uses its whole timeout before failing."""
    from bleak.exc import BleakError

    class Client:
        def __init__(self, device, backend, timeout):
            timeouts.append(timeout)
            self.timeout = timeout

        async def connect(self):
            await asyncio.sleep(self.timeout)
            raise BleakError("no")

        async def disconnect(self):
            pass

    return Client


def short_budget():
    return (
        patch.object(proxy_client, "CONNECT_TIMEOUT", 0.3),
        patch.object(proxy_client, "CONNECT_BUDGET", 0.5),
        patch.object(proxy_client, "MIN_ATTEMPT_TIMEOUT", 0.1),
        patch.object(proxy_client, "RETRY_DELAY", 0.05),
    )


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

    async def test_retry_budget_fits_inside_the_caller_deadline(self):
        from custom_components.tinyprint import connection

        self.assertLess(proxy_client.CONNECT_BUDGET, connection.CONNECT_DEADLINE)
        self.assertLessEqual(proxy_client.CONNECT_TIMEOUT, proxy_client.CONNECT_BUDGET)

    async def test_last_attempt_shrinks_and_retrying_stops_inside_the_budget(self):
        from bleak.exc import BleakError

        timeouts: list[float] = []
        devices = [scanner_device("AA:BB:CC:DD:EE:FF", MagicMock())]
        loop = asyncio.get_running_loop()
        timeout, budget, minimum, delay = short_budget()
        with (
            patch.object(
                proxy_client.bluetooth, "async_scanner_devices_by_address", return_value=devices
            ),
            patch.object(proxy_client, "BleakClient", slow_failing_client(timeouts)),
            timeout,
            budget,
            minimum,
            delay,
        ):
            started = loop.time()
            with self.assertRaises(BleakError):
                await proxy_client.connect_via_proxy(None, ADDRESS, "X6h", "AA:BB:CC:DD:EE:FF")
            elapsed = loop.time() - started
        self.assertEqual(len(timeouts), 2)
        self.assertEqual(timeouts[0], 0.3)
        self.assertTrue(0.1 <= timeouts[1] < 0.3)
        self.assertLess(elapsed, 0.5 + 0.2)

    async def test_caller_gets_the_connect_error_not_its_own_deadline(self):
        from bleak.exc import BleakError

        from custom_components.tinyprint import connection

        timeouts: list[float] = []
        devices = [scanner_device("AA:BB:CC:DD:EE:FF", MagicMock())]
        hass = SimpleNamespace(
            async_create_task=lambda coro, name: asyncio.get_running_loop().create_task(coro)
        )
        timeout, budget, minimum, delay = short_budget()
        with (
            patch.object(
                proxy_client.bluetooth, "async_scanner_devices_by_address", return_value=devices
            ),
            patch.object(proxy_client, "BleakClient", slow_failing_client(timeouts)),
            patch.object(connection, "CONNECT_DEADLINE", 0.7),
            timeout,
            budget,
            minimum,
            delay,
        ):
            with self.assertRaises(BleakError):
                async with connection.connected_client(
                    hass,
                    proxy_client.connect_via_proxy(hass, ADDRESS, "X6h", "AA:BB:CC:DD:EE:FF"),
                ):
                    self.fail("the printer never connected")
        self.assertEqual(len(timeouts), 2)


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

    async def test_options_form_falls_back_to_the_imported_proxy_source(self):
        def suggested(form):
            marker = next(key for key in form["data_schema"].schema if key.schema == "proxy_source")
            return marker.description["suggested_value"]

        imported = self.entry()
        flow = TinyPrintOptionsFlow(imported)
        flow.hass = self.hass
        form = await flow.async_step_init()
        self.assertEqual(suggested(form), PROXY)
        saved = await flow.async_step_init({"transport": "proxy", "proxy_source": suggested(form)})
        self.assertEqual(saved["data"]["proxy_source"], PROXY)

        overridden = self.entry()
        overridden.options = {"transport": "proxy", "proxy_source": "66:55:44:33:22:11"}
        flow = TinyPrintOptionsFlow(overridden)
        flow.hass = self.hass
        self.assertEqual(suggested(await flow.async_step_init()), "66:55:44:33:22:11")

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
