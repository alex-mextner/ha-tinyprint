"""Direct-LE routing and configuration against real HA, with no radio or network."""

import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

try:
    from homeassistant.core import HomeAssistant
except ImportError as error:
    raise unittest.SkipTest("Run in the Home Assistant image") from error

from voluptuous import Invalid

from custom_components.tinyprint import CONFIG_SCHEMA, async_setup, async_setup_entry
from custom_components.tinyprint.button import TinyPrintTestButton
from custom_components.tinyprint.config_flow import TinyPrintConfigFlow, TinyPrintOptionsFlow
from custom_components.tinyprint.studio_runtime import StudioPrinter

ADAPTER = "00:11:22:33:44:55"
ADDRESS = "AA:BB:CC:DD:EE:FF"
SENT = {
    "bytes_written": 96,
    "frames_written": 7,
    "writes": 9,
    "confirmation": "transport_only",
    "physical_print_confirmed": False,
}


class LocalHA(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self.tmp.name)
        await async_setup(self.hass, {})

    async def asyncTearDown(self):
        await self.hass.async_stop()
        self.tmp.cleanup()

    def printer(self):
        p = StudioPrinter(self.hass, "entry", ADDRESS, "Direct", None)
        p.transport = "local_le"
        p.adapter_mac = ADAPTER
        self.hass.data["tinyprint"]["entry"] = p
        return p

    def entry(self):
        entry = MagicMock()
        entry.entry_id = "entry"
        entry.title = "Direct"
        entry.data = {"address": ADDRESS, "transport": "local_le", "adapter_mac": ADAPTER}
        entry.options = {}
        return entry

    async def test_yaml_accepts_local_adapter_without_bridge_secrets(self):
        config = {
            "tinyprint": {
                "printers": [{"address": ADDRESS, "transport": "local_le", "adapter_mac": ADAPTER}]
            }
        }
        try:
            validated = CONFIG_SCHEMA(config)
        except Invalid as error:
            self.fail(f"Direct LE YAML is rejected: {error}")
        self.assertEqual(validated["tinyprint"]["printers"][0]["transport"], "local_le")

    async def test_yaml_rejects_bridge_credentials(self):
        with self.assertRaises(Invalid):
            CONFIG_SCHEMA(
                {
                    "tinyprint": {
                        "printers": [
                            {
                                "address": ADDRESS,
                                "transport": "bridge",
                                "bridge_token": "not-allowed",
                            }
                        ]
                    }
                }
            )

    async def test_setup_preserves_explicit_local_mode_from_imported_data(self):
        entry = self.entry()
        with (
            patch("custom_components.tinyprint.async_setup_panel", new=AsyncMock()),
            patch.object(
                self.hass, "config_entries", SimpleNamespace(async_forward_entry_setups=AsyncMock())
            ),
        ):
            await async_setup_entry(self.hass, entry)
        p = self.hass.data["tinyprint"]["entry"]
        self.assertEqual(p.transport, "local_le")
        self.assertEqual(p.adapter_mac, ADAPTER)
        self.assertIsNone(p.bridge)

    async def test_ui_native_option_can_override_imported_local_data(self):
        entry = self.entry()
        entry.options = {"transport": "native"}
        with (
            patch("custom_components.tinyprint.async_setup_panel", new=AsyncMock()),
            patch.object(
                self.hass, "config_entries", SimpleNamespace(async_forward_entry_setups=AsyncMock())
            ),
        ):
            await async_setup_entry(self.hass, entry)
        self.assertEqual(self.hass.data["tinyprint"]["entry"].transport, "native")

    async def test_legacy_text_and_test_button_route_to_local_without_native_ble(self):
        p = self.printer()
        local = AsyncMock(return_value=SENT)
        with (
            patch.object(p, "_render", return_value=(bytes(48), 1)),
            patch("custom_components.tinyprint.studio_runtime.run_local_job", local, create=True),
            patch(
                "custom_components.tinyprint.studio_runtime.bluetooth.async_ble_device_from_address",
                side_effect=AssertionError("Must not use native BlueZ for local LE"),
            ),
        ):
            await self.hass.services.async_call(
                "tinyprint", "print_text", {"text": "direct"}, blocking=True
            )
            await TinyPrintTestButton(p).async_press()
        self.assertEqual(local.await_count, 2)
        self.assertEqual(p.status, "sent")
        self.assertIsNone(p.bridge)

    async def test_diagnose_routes_to_local_without_native_ble(self):
        p = self.printer()
        local = AsyncMock(
            return_value={
                "supported_profile": True,
                "bytes_written": 0,
                "confirmation": "connection_only",
            }
        )
        with (
            patch("custom_components.tinyprint.studio_runtime.run_local_job", local, create=True),
            patch(
                "custom_components.tinyprint.studio_runtime.bluetooth.async_ble_device_from_address",
                side_effect=AssertionError("Must not use native BlueZ"),
            ),
        ):
            result = await p.async_diagnose()
        self.assertEqual(local.await_args.args[1], "diagnose")
        self.assertEqual(result["bytes_written"], 0)

    async def test_options_store_local_mode_without_http_or_bridge_token(self):
        flow = TinyPrintOptionsFlow(self.entry())
        flow.hass = self.hass
        with patch(
            "custom_components.tinyprint.config_flow.async_get_clientsession",
            side_effect=AssertionError("Local settings need no network"),
        ):
            result = await flow.async_step_init({"transport": "local_le", "adapter_mac": ADAPTER})
        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(result["data"]["adapter_mac"], ADAPTER)
        self.assertNotIn("bridge_token", result["data"])

    async def test_invalid_adapter_keeps_options_form_and_never_connects(self):
        flow = TinyPrintOptionsFlow(self.entry())
        flow.hass = self.hass
        result = await flow.async_step_init({"transport": "local_le", "adapter_mac": "bad"})
        self.assertEqual(result["type"], "form")
        self.assertIn("adapter_mac", result["errors"])

    async def test_import_passes_local_data_to_public_existing_entry_update(self):
        flow = TinyPrintConfigFlow()
        flow.hass = self.hass
        flow.context = {"source": "import"}
        with (
            patch.object(flow, "async_set_unique_id", new=AsyncMock()),
            patch.object(flow, "_abort_if_unique_id_configured") as existing,
        ):
            result = await flow.async_step_import(
                {
                    "address": ADDRESS,
                    "name": "Direct",
                    "transport": "local_le",
                    "adapter_mac": ADAPTER,
                }
            )
        self.assertEqual(existing.call_args.kwargs["updates"]["adapter_mac"], ADAPTER)
        self.assertEqual(result["data"]["transport"], "local_le")

    async def test_invalid_imported_local_adapter_aborts(self):
        flow = TinyPrintConfigFlow()
        flow.hass = self.hass
        result = await flow.async_step_import(
            {"address": ADDRESS, "transport": "local_le", "adapter_mac": "bad"}
        )
        self.assertEqual(result["type"], "abort")

    async def test_real_ha_action_uses_local_att_codec_end_to_end(self):
        from att_fake import FakeGattServer, FakeLink

        from custom_components.tinyprint import local_le

        p = self.printer()
        server = FakeGattServer(first_handle=19, gap=1)
        link = FakeLink(server)
        with (
            patch.object(local_le, "L2capLeLink", return_value=link),
            patch.object(p, "_render", return_value=(bytes([0xAA]) * 48, 1)),
            patch(
                "custom_components.tinyprint.studio_runtime.establish_connection",
                side_effect=AssertionError("Native BlueZ must not be called"),
            ),
        ):
            result = await self.hass.services.async_call(
                "tinyprint", "print_qr", {"text": "test-only"}, blocking=True, return_response=True
            )
        self.assertEqual(p.status, "sent")
        self.assertEqual(link.connects, 1)
        self.assertTrue(server.closed)
        self.assertGreater(result["bytes_written"], 0)
        self.assertEqual(result["confirmation"], "transport_only")
        self.assertFalse(result["physical_print_confirmed"])
        self.assertFalse(p.lock.locked())

    async def test_import_does_not_schedule_duplicate_reload(self):
        flow = TinyPrintConfigFlow()
        flow.hass = self.hass
        flow.context = {"source": "import"}
        with (
            patch.object(flow, "async_set_unique_id", new=AsyncMock()),
            patch.object(flow, "_abort_if_unique_id_configured") as existing,
        ):
            await flow.async_step_import(
                {"address": ADDRESS, "transport": "local_le", "adapter_mac": ADAPTER}
            )
        self.assertFalse(existing.call_args.kwargs["reload_on_update"])

    async def test_omitted_yaml_transport_resets_existing_local_mode(self):
        existing_data = {"address": ADDRESS, "transport": "local_le", "adapter_mac": ADAPTER}
        flow = TinyPrintConfigFlow()
        flow.hass = self.hass
        flow.context = {"source": "import"}

        def merge_updates(*, updates, reload_on_update):
            existing_data.update(updates)

        with (
            patch.object(flow, "async_set_unique_id", new=AsyncMock()),
            patch.object(flow, "_abort_if_unique_id_configured", side_effect=merge_updates),
        ):
            await flow.async_step_import({"address": ADDRESS})
        self.assertEqual(existing_data["transport"], "native")
        self.assertEqual(existing_data["adapter_mac"], "")
