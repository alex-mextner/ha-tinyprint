"""StudioPrinter -> HTTP -> the real bridge app -> fake ATT printer, in the HA image.

Nothing is mocked between the printer runtime and the fake X6h except the Bluetooth radio:
real aiohttp client, real bridge server/job manager/ATT client, `FakeGattServer` as the link.
Proves the bytes reach the printer as 20-byte ATT writes and that HA never touches local BLE.
"""

import tempfile
import unittest
from unittest.mock import AsyncMock, patch

try:
    import att_fake as fake
    from homeassistant.core import HomeAssistant
    from homeassistant.exceptions import HomeAssistantError
    from tinyprint_bridge import config as bridge_config
    from tinyprint_bridge import jobs, server
except ImportError as error:
    raise unittest.SkipTest("Needs the HA image and bridge/ on PYTHONPATH") from error

from aiohttp import ClientSession
from aiohttp.test_utils import TestServer

from custom_components.tinyprint import async_setup
from custom_components.tinyprint import bridge_client as bc
from custom_components.tinyprint.const import DOMAIN
from custom_components.tinyprint.protocol import print_frames
from custom_components.tinyprint.studio_runtime import StudioPrinter

TOKEN = "E2e-" + "k" * 44
PRINTER_MAC = "4C:2A:EE:D3:3E:C2"
ADAPTER_MAC = "00:1A:7D:DA:71:13"
FAST = dict(chunk_delay=0, pause_timeout=0.5)
BITMAP = bytes(range(48)) * 3


class EndToEndTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self.tmp.name)
        await async_setup(self.hass, {})
        self.gatt = fake.FakeGattServer()
        self.link = fake.FakeLink(self.gatt)
        manager = jobs.JobManager(
            self.link,
            cancel_wait=2,
            connect_timeout=1,
            discovery_timeout=2,
            request_timeout=1,
            send_timeout=1,
            pacing=FAST,
        )
        config = bridge_config.BridgeConfig(PRINTER_MAC, ADAPTER_MAC, TOKEN)
        self.server = TestServer(server.create_app(config, manager))
        await self.server.start_server()
        self.session = ClientSession()
        url = str(self.server.make_url("")).rstrip("/")
        for target in (
            "custom_components.tinyprint.bridge_runtime.async_get_clientsession",
            "custom_components.tinyprint.config_flow.async_get_clientsession",
        ):
            session_patch = patch(target, return_value=self.session)
            session_patch.start()
            self.addCleanup(session_patch.stop)
        poll = patch.object(bc, "POLL_INTERVAL", 0.01)
        poll.start()
        self.addCleanup(poll.stop)
        ble = patch(
            "custom_components.tinyprint.studio_runtime.bluetooth.async_ble_device_from_address",
            side_effect=AssertionError("local BLE must not be used"),
        )
        self.ble = ble.start()
        self.addCleanup(ble.stop)
        connect = patch(
            "custom_components.tinyprint.studio_runtime.establish_connection",
            new=AsyncMock(side_effect=AssertionError("native connect")),
        )
        self.connect = connect.start()
        self.addCleanup(connect.stop)
        self.printer = StudioPrinter(
            self.hass,
            "entry",
            "AA:BB:CC:DD:EE:FF",
            "Test",
            None,
            bridge=bc.BridgeSettings(url, TOKEN),
        )
        self.hass.data[DOMAIN]["entry"] = self.printer

    async def asyncTearDown(self):
        await self.session.close()
        await self.server.close()
        await self.hass.async_stop()
        self.tmp.cleanup()

    async def test_print_reaches_the_fake_printer_in_20_byte_writes(self):
        with patch.object(self.printer, "_render", return_value=(BITMAP, 3)):
            result = await self.printer.async_print("text", "hello")
        sent = b"".join(value for _handle, value in self.gatt.commands)
        self.assertEqual(sent, b"".join(print_frames(BITMAP, 3)))
        self.assertTrue(all(len(value) <= 20 for _handle, value in self.gatt.commands))
        self.assertEqual(result["bytes_written"], len(sent))
        self.assertEqual(result["confirmation"], "transport_only")
        self.assertFalse(result["physical_print_confirmed"])
        self.assertEqual(self.printer.status, "sent")
        self.assertEqual(self.link.connects, 1)
        self.ble.assert_not_called()
        self.connect.assert_not_called()

    async def test_diagnose_discovers_profile_without_writing_raster(self):
        result = await self.printer.async_diagnose()
        self.assertEqual(result["confirmation"], "connection_only")
        self.assertTrue(result["supported_profile"])
        self.assertEqual(result["transport"], "bridge")
        self.assertEqual(self.gatt.commands, [])
        self.ble.assert_not_called()

    async def test_link_loss_midway_is_partial_and_not_replayed(self):
        self.gatt.fail_send_after_commands = 2
        with patch.object(self.printer, "_render", return_value=(BITMAP, 3)):
            with self.assertRaises(HomeAssistantError) as raised:
                await self.printer.async_print("text", "hello")
        self.assertIn("Partial delivery is possible", str(raised.exception))
        self.assertTrue(self.printer.last_result["delivery_uncertain"])
        self.assertEqual(self.link.connects, 1)
        self.assertEqual(self.printer.status, "error")
        self.ble.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
