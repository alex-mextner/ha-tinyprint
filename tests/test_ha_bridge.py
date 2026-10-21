"""Bridge transport inside real Home Assistant (run in the pinned HA image, no real BLE).

A scripted aiohttp fake bridge stands in for the nearby Linux host. Every test also proves the
local Bluetooth stack is never consulted in bridge mode.
"""

import asyncio
import json
import socket
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from bridge_fake import LEAK, TOKEN, FakeBridge

try:
    from homeassistant.core import HomeAssistant
    from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
except ImportError as error:
    raise unittest.SkipTest("Run inside the Home Assistant test image") from error

from aiohttp import ClientSession
from aiohttp.test_utils import TestServer

from custom_components.tinyprint import async_setup, async_setup_entry
from custom_components.tinyprint import bridge_client as bc
from custom_components.tinyprint.button import TinyPrintTestButton
from custom_components.tinyprint.config_flow import TinyPrintOptionsFlow
from custom_components.tinyprint.const import DOMAIN
from custom_components.tinyprint.diagnostics import async_get_config_entry_diagnostics
from custom_components.tinyprint.studio_runtime import StudioPrinter

ADDRESS = "AA:BB:CC:DD:EE:FF"
BLE = "custom_components.tinyprint.studio_runtime.bluetooth.async_ble_device_from_address"
CONNECT = "custom_components.tinyprint.studio_runtime.establish_connection"
SENT = {
    "state": "sent",
    "bytes_written": 160,
    "frames_written": 4,
    "writes": 8,
    "notifications": 2,
    "flow_pauses": 1,
    "confirmation": "transport_only",
}


class BridgeCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self.tmp.name)
        await async_setup(self.hass, {})
        self.bridge = FakeBridge()
        self.server = TestServer(self.bridge.app())
        await self.server.start_server()
        self.url = str(self.server.make_url("")).rstrip("/")
        self.settings = bc.BridgeSettings(self.url, TOKEN)
        # A bare HomeAssistant has no zeroconf/network stack, which HA's shared session needs.
        self.session = ClientSession()
        for target in (
            "custom_components.tinyprint.bridge_runtime.async_get_clientsession",
            "custom_components.tinyprint.config_flow.async_get_clientsession",
        ):
            session_patch = patch(target, return_value=self.session)
            session_patch.start()
            self.addCleanup(session_patch.stop)
        patcher = patch.object(bc, "POLL_INTERVAL", 0.01)
        patcher.start()
        self.addCleanup(patcher.stop)
        ble = patch(BLE, side_effect=AssertionError("local BLE must not be used in bridge mode"))
        self.ble = ble.start()
        self.addCleanup(ble.stop)
        connect = patch(CONNECT, new=AsyncMock(side_effect=AssertionError("native connect")))
        self.connect = connect.start()
        self.addCleanup(connect.stop)

    async def asyncTearDown(self):
        await self.session.close()
        await self.server.close()
        await self.hass.async_stop()
        self.tmp.cleanup()

    def printer(self, **kwargs):
        kwargs.setdefault("bridge", self.settings)
        printer = StudioPrinter(self.hass, "entry", ADDRESS, "Test", None, **kwargs)
        self.hass.data[DOMAIN]["entry"] = printer
        return printer

    def registry(self):
        registry = MagicMock()
        registry.async_get.return_value = SimpleNamespace(config_entries={"entry"})
        registry.async_get_device.return_value = SimpleNamespace(id="device")
        return patch("custom_components.tinyprint.studio.dr.async_get", return_value=registry)

    def assert_no_ble(self):
        self.ble.assert_not_called()
        self.connect.assert_not_called()


class RoutingTests(BridgeCase):
    async def test_old_print_text_action_prints_through_bridge(self):
        printer = self.printer()
        self.bridge.polls = [SENT]
        with patch.object(printer, "_render", return_value=(bytes(48 * 3), 3)):
            result = await self.hass.services.async_call(
                DOMAIN, "print_text", {"text": "hello"}, blocking=True, return_response=True
            )
        self.assertEqual(result["confirmation"], "transport_only")
        self.assertEqual(result["bytes_written"], 160)
        self.assertFalse(result["physical_print_confirmed"])
        self.assertEqual(printer.status, "sent")
        self.assertEqual(self.bridge.count("POST", "/v1/jobs"), 1)
        body = self.bridge.bodies[0]
        self.assertEqual(body["rows"], 3)
        self.assertEqual(set(body), {"job_id", "rows", "bitmap_b64"})
        self.assert_no_ble()

    async def test_studio_print_document_prints_through_bridge(self):
        printer = self.printer()
        self.bridge.polls = [SENT]
        with self.registry():
            result = await self.hass.services.async_call(
                DOMAIN,
                "print_document",
                {"device_id": "device", "document": {"kind": "qr", "text": "https://example.com"}},
                blocking=True,
                return_response=True,
            )
        self.assertEqual(result["confirmation"], "transport_only")
        self.assertEqual(printer.status, "sent")
        self.assertEqual(self.bridge.bodies[0]["rows"] % 1, 0)
        self.assert_no_ble()

    async def test_template_action_prints_through_bridge(self):
        self.printer()
        self.bridge.polls = [SENT]
        with self.registry():
            await self.hass.services.async_call(
                DOMAIN,
                "print_template",
                {
                    "device_id": "device",
                    "template": "qr_label",
                    "fields": {"qr": "https://example.com"},
                },
                blocking=True,
                return_response=True,
            )
        self.assertEqual(self.bridge.count("POST", "/v1/jobs"), 1)
        self.assert_no_ble()

    async def test_button_press_uses_bridge(self):
        printer = self.printer()
        self.bridge.polls = [SENT]
        with patch.object(printer, "_render", return_value=(bytes(48 * 3), 3)):
            await TinyPrintTestButton(printer).async_press()
        self.assertEqual(self.bridge.count("POST", "/v1/jobs"), 1)
        self.assert_no_ble()

    async def test_diagnose_runs_through_bridge_without_printing(self):
        self.printer()
        self.bridge.polls = [
            {
                "state": "sent",
                "kind": "diagnose",
                "confirmation": "connection_only",
                "diagnosis": {
                    "profile_supported": True,
                    "profile": {"write": 8, "notify": 10},
                    "services": ["0000ae30-0000-1000-8000-00805f9b34fb"],
                    "att_mtu": 23,
                },
            }
        ]
        with self.registry():
            result = await self.hass.services.async_call(
                DOMAIN, "diagnose", {"device_id": "device"}, blocking=True, return_response=True
            )
        self.assertEqual(result["confirmation"], "connection_only")
        self.assertEqual(result["transport"], "bridge")
        self.assertTrue(result["supported_profile"])
        self.assertEqual(self.bridge.count("POST", "/v1/diagnose"), 1)
        self.assertEqual(self.bridge.count("POST", "/v1/jobs"), 0)
        self.assert_no_ble()

    async def test_second_call_is_busy_across_old_and_studio_actions(self):
        printer = self.printer()
        self.bridge.default_poll = {"state": "transmitting", "bytes_written": 20}
        with patch.object(printer, "_render", return_value=(bytes(48 * 3), 3)):
            first = asyncio.create_task(printer.async_print("text", "hello"))
            for _ in range(200):
                if self.bridge.job_id:
                    break
                await asyncio.sleep(0.01)
            with self.assertRaises(ServiceValidationError):
                await printer.async_print("document", {"document": {"kind": "qr", "text": "x"}})
            with self.assertRaises(ServiceValidationError):
                await printer.async_diagnose()
            await printer.async_cancel()
            with self.assertRaises(HomeAssistantError):
                await first
        self.assertEqual(self.bridge.count("POST", "/v1/jobs"), 1)

    async def test_cancel_posts_bridge_cancel_and_waits(self):
        printer = self.printer()
        self.bridge.default_poll = {"state": "transmitting", "bytes_written": 40}
        with patch.object(printer, "_render", return_value=(bytes(48 * 3), 3)):
            caller = asyncio.create_task(printer.async_print("text", "hello"))
            for _ in range(200):
                if self.bridge.job_id:
                    break
                await asyncio.sleep(0.01)
            for _ in range(200):
                if printer.last_result["bytes_written"]:
                    break
                await asyncio.sleep(0.01)
            await printer.async_cancel()
            self.assertEqual(self.bridge.count("POST", "/cancel"), 1)
            with self.assertRaises(HomeAssistantError) as raised:
                await caller
        self.assertIn("cannot be recalled", str(raised.exception))
        self.assertIsNone(printer.task)
        self.assertEqual(printer.last_result["bridge_cancel"], "acknowledged")
        self.assertTrue(printer.last_result["delivery_uncertain"])
        self.assertEqual(printer.status, "error")
        self.assert_no_ble()

    async def test_repeated_cancels_wait_for_bridge_cancel_before_releasing_the_lock(self):
        printer = self.printer()
        self.bridge.default_poll = {"state": "transmitting", "bytes_written": 40}
        self.bridge.cancel_delay = 0.3
        with patch.object(printer, "_render", return_value=(bytes(48 * 3), 3)):
            caller = asyncio.create_task(printer.async_print("text", "hello"))
            for _ in range(200):
                if printer.last_result.get("bytes_written"):
                    break
                await asyncio.sleep(0.01)
            caller.cancel()
            for _ in range(200):
                if self.bridge.count("POST", "/cancel") or self.bridge.cancel_finished:
                    break
                await asyncio.sleep(0.01)
            # Further cancels (service caller again, cancel action) while the bridge cancel runs.
            task = printer.task
            self.assertIsNotNone(task)
            task.cancel()
            caller.cancel()
            self.assertTrue(printer.lock.locked())
            self.assertFalse(self.bridge.cancel_finished)
            with self.assertRaises(asyncio.CancelledError):
                await caller
            self.assertTrue(self.bridge.cancel_finished)
            self.assertFalse(printer.lock.locked())
        self.assertEqual(printer.last_result["bridge_cancel"], "acknowledged")
        self.assertEqual(self.bridge.count("POST", "/cancel"), 1)

    async def test_cancel_action_cancels_orphan_bridge_job(self):
        printer = self.printer()
        self.bridge.job_id = "a" * 32
        self.bridge.status_body = {
            **self.bridge.status_body,
            "busy": True,
            "active_job_id": "a" * 32,
        }
        with self.registry():
            result = await self.hass.services.async_call(
                DOMAIN, "cancel", {"device_id": "device"}, blocking=True, return_response=True
            )
        self.assertEqual(self.bridge.count("POST", "/cancel"), 1)
        self.assertEqual(result["bridge_cancel"], "acknowledged")
        self.assertEqual(printer.status, "idle")

    async def test_cancel_action_with_idle_bridge_posts_nothing(self):
        self.printer()
        with self.registry():
            result = await self.hass.services.async_call(
                DOMAIN, "cancel", {"device_id": "device"}, blocking=True, return_response=True
            )
        self.assertEqual(self.bridge.count("POST", "/cancel"), 0)
        self.assertEqual(result["bridge_cancel"], "none_active")

    async def test_misconfigured_bridge_fails_closed(self):
        printer = self.printer(bridge=None, bridge_error="misconfigured")
        self.assertEqual(printer.transport, "bridge")
        self.assertEqual(printer.bridge_state, "misconfigured")
        with patch.object(printer, "_render", return_value=(bytes(48 * 3), 3)):
            with self.assertRaises(ServiceValidationError) as raised:
                await printer.async_print("text", "hello")
        self.assertIn("options", str(raised.exception))
        with self.assertRaises(ServiceValidationError):
            await printer.async_diagnose()
        self.assertEqual(self.bridge.requests, [])
        self.assert_no_ble()

    async def test_unreachable_bridge_never_falls_back_to_native(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        printer = self.printer(bridge=bc.BridgeSettings(f"http://127.0.0.1:{port}", TOKEN))
        with patch.object(printer, "_render", return_value=(bytes(48 * 3), 3)):
            with self.assertRaises(HomeAssistantError) as raised:
                await printer.async_print("text", "hello")
        self.assertIn("No raster writes were attempted", str(raised.exception))
        self.assertFalse(printer.last_result["delivery_uncertain"])
        self.assertEqual(printer.bridge_state, "unreachable")
        self.assert_no_ble()

    async def test_lost_answer_after_start_is_uncertain_and_not_replayed(self):
        printer = self.printer()
        self.bridge.start_mode = "drop"
        with patch.object(printer, "_render", return_value=(bytes(48 * 3), 3)):
            with self.assertRaises(HomeAssistantError) as raised:
                await printer.async_print("text", "hello")
        self.assertIn("may have accepted", str(raised.exception))
        self.assertTrue(printer.last_result["delivery_uncertain"])
        self.assertEqual(self.bridge.count("POST", "/v1/jobs"), 1)
        # The possibly running job is cancelled instead of being left without an owner.
        self.assertEqual(self.bridge.count("POST", "/cancel"), 1)
        self.assertEqual(printer.status, "error")
        self.assert_no_ble()

    async def test_failed_job_keeps_partial_counters(self):
        printer = self.printer()
        self.bridge.polls = [
            {
                "state": "failed",
                "bytes_written": 60,
                "writes": 3,
                "delivery_uncertain": True,
                "error": {"code": "link_lost", "message": "Link lost"},
            }
        ]
        with patch.object(printer, "_render", return_value=(bytes(48 * 3), 3)):
            with self.assertRaises(HomeAssistantError) as raised:
                await printer.async_print("text", "hello")
        self.assertIn("Partial delivery is possible", str(raised.exception))
        self.assertEqual(printer.last_result["bytes_written"], 60)
        self.assertTrue(printer.last_result["delivery_uncertain"])
        self.assertEqual(printer.last_result["failure_phase"], "bridge_accepted")
        self.assertEqual(printer.status, "error")

    async def test_native_default_unchanged(self):
        printer = StudioPrinter(self.hass, "entry", ADDRESS, "Test", None)
        self.assertIsNone(printer.bridge)
        self.assertEqual(printer.transport, "native")
        self.assertEqual(printer.bridge_state, "native")
        self.assertIsNone(await printer.async_bridge_status())
        with (
            patch.object(printer, "_render", return_value=(bytes(48 * 3), 3)),
            patch(BLE, return_value=None) as ble,
        ):
            with self.assertRaises(HomeAssistantError) as raised:
                await printer.async_print("text", "hello")
        self.assertIn("not visible", str(raised.exception))
        ble.assert_called()
        self.assertEqual(self.bridge.requests, [])


class SetupTests(BridgeCase):
    async def load(self, options):
        entry = MagicMock(
            entry_id="entry", title="Test", data={"address": ADDRESS}, options=options
        )
        entries = SimpleNamespace(async_forward_entry_setups=AsyncMock())
        with (
            patch.object(self.hass, "config_entries", entries),
            patch("custom_components.tinyprint.async_setup_panel", new=AsyncMock()),
        ):
            self.assertTrue(await async_setup_entry(self.hass, entry))
        return self.hass.data[DOMAIN]["entry"]

    async def test_entry_without_transport_keys_is_native(self):
        printer = await self.load({})
        self.assertEqual(
            (printer.transport, printer.bridge, printer.bridge_error), ("native", None, None)
        )
        self.assertEqual(printer.address, ADDRESS)

    async def test_entry_with_bridge_options_uses_bridge(self):
        printer = await self.load(
            {"transport": "bridge", "bridge_url": self.url + "/", "bridge_token": TOKEN}
        )
        self.assertEqual(printer.transport, "bridge")
        self.assertEqual(printer.bridge.url, self.url)
        self.assertEqual(printer.bridge_state, "unchecked")

    async def test_entry_with_bad_bridge_options_is_misconfigured_not_native(self):
        printer = await self.load(
            {"transport": "bridge", "bridge_url": "nope", "bridge_token": TOKEN}
        )
        self.assertEqual(printer.transport, "bridge")
        self.assertIsNone(printer.bridge)
        self.assertEqual(printer.bridge_state, "misconfigured")

    async def test_component_versions_match_release(self):
        from custom_components.tinyprint.studio import PANEL_VERSION

        manifest = Path(__file__).parents[1] / "custom_components/tinyprint/manifest.json"
        self.assertEqual(json.loads(manifest.read_text())["version"], "0.2.2")
        self.assertEqual(PANEL_VERSION, "0.2.2")
        printer = self.printer()
        report = await async_get_config_entry_diagnostics(
            self.hass, SimpleNamespace(entry_id="entry")
        )
        self.assertEqual(report["version"], "0.2.2")
        self.assertIsNotNone(printer)


class DiagnosticsTests(BridgeCase):
    async def test_printer_info_labels_local_rssi_and_reports_bridge_state(self):
        self.printer()
        self.bridge.status_body = {
            **self.bridge.status_body,
            "busy": True,
            "active_job_id": "a" * 32,
        }
        with (
            self.registry(),
            patch(
                "custom_components.tinyprint.studio.bluetooth.async_last_service_info",
                return_value=SimpleNamespace(rssi=57),
            ),
            patch(
                "custom_components.tinyprint.studio.bluetooth.async_address_present",
                return_value=False,
            ),
        ):
            result = await self.hass.services.async_call(
                DOMAIN, "printer_info", {}, blocking=True, return_response=True
            )
        info = result["printers"][0]
        self.assertEqual(info["transport"]["mode"], "bridge")
        self.assertEqual(info["transport"]["bridge_state"], "busy")
        self.assertTrue(info["transport"]["bridge_configured"])
        self.assertEqual(info["transport"]["bridge_scheme"], "http")
        self.assertTrue(info["local_scan_only"])
        self.assertEqual(info["rssi_raw"], 57)
        self.assertEqual(self.bridge.count("GET", "/v1/status"), 1)
        self.assertEqual(self.bridge.count("POST", ""), 0)
        # A second call inside the cache window does not poll the bridge again.
        with (
            self.registry(),
            patch(
                "custom_components.tinyprint.studio.bluetooth.async_last_service_info",
                return_value=None,
            ),
            patch(
                "custom_components.tinyprint.studio.bluetooth.async_address_present",
                return_value=False,
            ),
        ):
            await self.hass.services.async_call(
                DOMAIN, "printer_info", {}, blocking=True, return_response=True
            )
        self.assertEqual(self.bridge.count("GET", "/v1/status"), 1)

    async def test_printer_info_reports_auth_failure_and_unreachable(self):
        printer = self.printer()
        self.bridge.token = "x" * 40
        await printer.async_bridge_status(max_age=0)
        self.assertEqual(printer.bridge_state, "auth_failed")
        await self.server.close()
        await printer.async_bridge_status(max_age=0)
        self.assertEqual(printer.bridge_state, "unreachable")
        self.assertIsNotNone(printer.bridge_checked_at)

    async def test_native_printer_info_keeps_shape(self):
        StudioPrinter(self.hass, "entry", ADDRESS, "Test", None)
        self.hass.data[DOMAIN]["entry"] = StudioPrinter(self.hass, "entry", ADDRESS, "Test", None)
        with (
            self.registry(),
            patch(
                "custom_components.tinyprint.studio.bluetooth.async_last_service_info",
                return_value=None,
            ),
            patch(
                "custom_components.tinyprint.studio.bluetooth.async_address_present",
                return_value=True,
            ),
        ):
            result = await self.hass.services.async_call(
                DOMAIN, "printer_info", {}, blocking=True, return_response=True
            )
        info = result["printers"][0]
        self.assertEqual(info["transport"]["mode"], "native")
        self.assertFalse(info["local_scan_only"])
        self.assertTrue(info["visible"])

    async def test_secrets_are_redacted_everywhere(self):
        printer = self.printer()
        self.bridge.extra_key = True
        self.bridge.polls = [SENT]
        collected = []
        with (
            self.assertLogs("custom_components.tinyprint", level="DEBUG") as logs,
            patch.object(printer, "_render", return_value=(bytes(48 * 3), 3)),
        ):
            await printer.async_print("text", "hello")
            self.bridge.polls = ["fail", "fail", "fail"]
            with self.assertRaises(HomeAssistantError) as raised:
                await printer.async_print("text", "hello")
            collected.append(str(raised.exception))
            self.bridge.token = "q" * 40
            with self.assertRaises(HomeAssistantError) as raised:
                await printer.async_print("text", "hello")
            collected.append(str(raised.exception))
            await printer.async_bridge_status(max_age=0)
        report = await async_get_config_entry_diagnostics(
            self.hass, SimpleNamespace(entry_id="entry")
        )
        with (
            self.registry(),
            patch(
                "custom_components.tinyprint.studio.bluetooth.async_last_service_info",
                return_value=None,
            ),
            patch(
                "custom_components.tinyprint.studio.bluetooth.async_address_present",
                return_value=False,
            ),
        ):
            info = await self.hass.services.async_call(
                DOMAIN, "printer_info", {}, blocking=True, return_response=True
            )
        self.assertEqual(report["transport"]["mode"], "bridge")
        blob = "\n".join(
            [
                json.dumps(report, default=str),
                json.dumps(info, default=str),
                json.dumps(printer.last_result, default=str),
                repr(printer.bridge),
                str(printer.bridge),
                repr(printer.bridge_client()),
                printer.last_error or "",
                *collected,
                *logs.output,
            ]
        )
        for secret in (TOKEN, "q" * 40, LEAK, self.url, f":{self.server.port}", "127.0.0.1"):
            self.assertNotIn(secret, blob, secret)
        self.assertNotIn("bitmap_b64", blob)
        self.assertNotIn(ADDRESS, json.dumps(report, default=str))


class OptionsFlowTests(BridgeCase):
    def flow(self, options=None, data=None):
        entry = SimpleNamespace(
            options=options or {}, data=data or {"address": ADDRESS, "font_path": "/f.ttf"}
        )
        flow = TinyPrintOptionsFlow(entry)
        flow.hass = self.hass
        return flow

    def fields(self, result):
        return {
            marker.schema: validator for marker, validator in result["data_schema"].schema.items()
        }

    async def test_form_offers_transport_and_password_token(self):
        result = await self.flow().async_step_init()
        fields = self.fields(result)
        self.assertEqual(
            set(fields), {"transport", "bridge_url", "bridge_token", "font_path", "adapter_mac"}
        )
        self.assertEqual(fields["bridge_token"].config["type"], "password")
        self.assertEqual(
            {option["value"] for option in fields["transport"].config["options"]}
            if isinstance(fields["transport"].config["options"][0], dict)
            else set(fields["transport"].config["options"]),
            {"native", "bridge", "local_le"},
        )

    async def test_form_never_prefills_the_stored_token(self):
        flow = self.flow({"transport": "bridge", "bridge_url": self.url, "bridge_token": TOKEN})
        result = await flow.async_step_init()
        for marker in result["data_schema"].schema:
            self.assertNotIn(TOKEN, repr(marker.default) + repr(marker.description))
        self.assertNotIn(TOKEN, repr(result))

    async def test_native_choice_clears_bridge_fields(self):
        flow = self.flow({"transport": "bridge", "bridge_url": self.url, "bridge_token": TOKEN})
        result = await flow.async_step_init({"transport": "native", "font_path": ""})
        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(result["data"], {"transport": "native", "font_path": ""})
        self.assertEqual(self.bridge.requests, [])

    async def test_bridge_choice_validates_with_status_call_and_stores_normalized(self):
        result = await self.flow().async_step_init(
            {
                "transport": "bridge",
                "bridge_url": self.url + "/",
                "bridge_token": TOKEN,
                "font_path": "",
            }
        )
        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(
            result["data"],
            {"transport": "bridge", "bridge_url": self.url, "bridge_token": TOKEN, "font_path": ""},
        )
        self.assertEqual(self.bridge.count("GET", "/v1/status"), 1)
        self.assertEqual(self.bridge.count("POST", ""), 0)

    async def test_blank_token_keeps_stored_token(self):
        flow = self.flow({"transport": "bridge", "bridge_url": self.url, "bridge_token": TOKEN})
        result = await flow.async_step_init(
            {"transport": "bridge", "bridge_url": self.url, "font_path": "x.ttf"}
        )
        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(result["data"]["bridge_token"], TOKEN)
        self.assertEqual(result["data"]["font_path"], "x.ttf")

    async def test_changing_url_requires_retyping_the_token(self):
        flow = self.flow(
            {"transport": "bridge", "bridge_url": "http://old.example:1", "bridge_token": TOKEN}
        )
        result = await flow.async_step_init({"transport": "bridge", "bridge_url": self.url})
        self.assertEqual(result["type"], "form")
        self.assertEqual(result["errors"], {"bridge_token": "token_required"})
        self.assertEqual(self.bridge.requests, [])

    async def test_invalid_input_matrix(self):
        cases = [
            ({"transport": "bridge", "bridge_token": TOKEN}, {"bridge_url": "invalid_url"}),
            (
                {"transport": "bridge", "bridge_url": "http://u:p@h", "bridge_token": TOKEN},
                {"bridge_url": "invalid_url"},
            ),
            (
                {"transport": "bridge", "bridge_url": self.url, "bridge_token": "short"},
                {"bridge_token": "invalid_token"},
            ),
            ({"transport": "bridge", "bridge_url": self.url}, {"bridge_token": "token_required"}),
        ]
        for user_input, errors in cases:
            result = await self.flow().async_step_init(user_input)
            self.assertEqual(result["type"], "form", user_input)
            self.assertEqual(result["errors"], errors, user_input)
            self.assertNotIn(TOKEN, repr(result))
        self.assertEqual(self.bridge.requests, [])

    async def test_connectivity_failure_blocks_saving(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        result = await self.flow().async_step_init(
            {"transport": "bridge", "bridge_url": f"http://127.0.0.1:{port}", "bridge_token": TOKEN}
        )
        self.assertEqual(result["type"], "form")
        self.assertEqual(result["errors"], {"base": "cannot_connect"})

    async def test_wrong_token_blocks_saving(self):
        self.bridge.token = "z" * 40
        result = await self.flow().async_step_init(
            {"transport": "bridge", "bridge_url": self.url, "bridge_token": TOKEN}
        )
        self.assertEqual(result["type"], "form")
        self.assertEqual(result["errors"], {"base": "invalid_auth"})
        self.assertNotIn(TOKEN, repr(result))

    async def test_existing_font_only_options_still_save(self):
        result = await self.flow({"font_path": "/f.ttf"}).async_step_init(
            {"transport": "native", "font_path": "/g.ttf"}
        )
        self.assertEqual(result["data"], {"transport": "native", "font_path": "/g.ttf"})


class StringsTests(unittest.TestCase):
    def test_option_strings_cover_all_fields_and_errors_in_every_language(self):
        base = Path(__file__).parents[1] / "custom_components/tinyprint"
        fields = {"transport", "bridge_url", "bridge_token", "font_path", "adapter_mac"}
        errors = {
            "invalid_url",
            "invalid_token",
            "token_required",
            "cannot_connect",
            "invalid_auth",
        }
        for name in ("strings.json", "translations/en.json", "translations/ru.json"):
            data = json.loads((base / name).read_text(encoding="utf-8"))
            step = data["options"]["step"]["init"]
            self.assertTrue(fields <= set(step["data"]), name)
            self.assertTrue(
                {"transport", "bridge_url", "bridge_token"} <= set(step["data_description"]), name
            )
            self.assertTrue(errors <= set(data["options"]["error"]), name)
            self.assertEqual(
                set(data["selector"]["transport"]["options"]),
                {"native", "bridge", "local_le"},
                name,
            )
        self.assertEqual(
            (base / "strings.json").read_text(), (base / "translations/en.json").read_text()
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
