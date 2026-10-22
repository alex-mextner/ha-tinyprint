"""Real Home Assistant API tests; run in the pinned HA image, not a fake HA."""

import asyncio
import base64
import io
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from PIL import Image

try:
    from homeassistant.core import HomeAssistant
    from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
except ImportError as error:
    raise unittest.SkipTest("Run inside the Home Assistant test image") from error

from custom_components.tinyprint import async_setup
from custom_components.tinyprint.sensor import (
    IN_FLIGHT_STATES,
    SETTLED_STATES,
    TinyPrintJobSensor,
)


class StudioAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self.tmp.name)
        await async_setup(self.hass, {})

    async def asyncTearDown(self):
        await self.hass.async_stop()
        self.tmp.cleanup()

    async def test_new_services_always_register(self):
        for action in (
            "preview",
            "print_document",
            "print_template",
            "printer_info",
            "diagnose",
            "cancel",
            "print_bitmap",
        ):
            self.assertTrue(self.hass.services.has_service("tinyprint", action), action)

    async def test_preview_without_printer_or_bluetooth(self):
        result = await self.hass.services.async_call(
            "tinyprint",
            "preview",
            {"document": {"kind": "qr", "text": "https://example.com"}},
            blocking=True,
            return_response=True,
        )
        image = Image.open(io.BytesIO(base64.b64decode(result["png_base64"])))
        self.assertEqual(image.width, 384)
        self.assertEqual(result["confirmation"], "preview_only")
        self.assertFalse(self.hass.data["tinyprint"])

    async def test_repeated_preview_cancel_holds_slot_until_render_finishes(self):
        renders = []
        release = threading.Event()

        def render(*args):
            renders.append(args)
            release.wait(5)
            return {"confirmation": "preview_only"}

        def call():
            return asyncio.create_task(
                self.hass.services.async_call(
                    "tinyprint",
                    "preview",
                    {"document": {"kind": "qr", "text": "x"}},
                    blocking=True,
                    return_response=True,
                )
            )

        async def renders_started(count):
            while len(renders) < count:
                await asyncio.sleep(0.01)

        try:
            with patch("custom_components.tinyprint.studio._preview", new=render):
                first, second = call(), call()
                await asyncio.wait_for(renders_started(2), 3)
                first.cancel()
                await asyncio.sleep(0.2)
                first.cancel()
                await asyncio.sleep(0.2)
                self.assertFalse(first.done())
                with self.assertRaises(ServiceValidationError):
                    await asyncio.wait_for(call(), 2)
                self.assertEqual(len(renders), 2)
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(first, 3)
                self.assertEqual(
                    await asyncio.wait_for(second, 3), {"confirmation": "preview_only"}
                )
                self.assertEqual(
                    await asyncio.wait_for(call(), 3), {"confirmation": "preview_only"}
                )
        finally:
            release.set()

    async def test_printer_info_preserves_raw_rssi(self):
        self.hass.data["tinyprint"] = {
            "one": SimpleNamespace(
                address="AA:BB:CC:DD:EE:FF",
                name="Test",
                status="idle",
                last_error=None,
                last_result={},
            )
        }
        registry = MagicMock()
        registry.async_get_device.return_value = SimpleNamespace(id="device")
        with (
            patch("custom_components.tinyprint.studio.dr.async_get", return_value=registry),
            patch(
                "custom_components.tinyprint.studio.er.async_get",
                return_value=MagicMock(async_get_entity_id=MagicMock(return_value=None)),
            ),
            patch(
                "custom_components.tinyprint.studio.bluetooth.async_last_service_info",
                return_value=SimpleNamespace(rssi=57),
            ),
            patch(
                "custom_components.tinyprint.studio.bluetooth.async_address_present",
                return_value=True,
            ),
        ):
            result = await self.hass.services.async_call(
                "tinyprint", "printer_info", {}, blocking=True, return_response=True
            )
        self.assertEqual(result["printers"][0]["rssi_raw"], 57)
        self.assertTrue(result["printers"][0]["visible"])
        self.assertEqual(len(result["templates"]), 6)
        self.assertNotIn("AA:BB:CC:DD:EE:FF", str(result))

    def _info_with_entity_registry(self, entity_id):
        self.hass.data["tinyprint"] = {
            "one": SimpleNamespace(
                address="AA:BB:CC:DD:EE:FF",
                name="Test",
                status="idle",
                last_error=None,
                last_result={},
            )
        }
        dev_registry = MagicMock()
        dev_registry.async_get_device.return_value = SimpleNamespace(id="device")
        ent_registry = MagicMock()
        ent_registry.async_get_entity_id.return_value = entity_id
        return dev_registry, ent_registry

    async def _call_printer_info(self, dev_registry, ent_registry):
        with (
            patch("custom_components.tinyprint.studio.dr.async_get", return_value=dev_registry),
            patch("custom_components.tinyprint.studio.er.async_get", return_value=ent_registry),
            patch(
                "custom_components.tinyprint.studio.bluetooth.async_last_service_info",
                return_value=None,
            ),
            patch(
                "custom_components.tinyprint.studio.bluetooth.async_address_present",
                return_value=True,
            ),
        ):
            return await self.hass.services.async_call(
                "tinyprint", "printer_info", {}, blocking=True, return_response=True
            )

    async def test_sensor_options_are_exactly_in_flight_plus_settled(self):
        sensor = TinyPrintJobSensor(SimpleNamespace(address="AA:BB:CC:DD:EE:FF", name="Test"))
        self.assertEqual(set(sensor.options), IN_FLIGHT_STATES | SETTLED_STATES)
        self.assertTrue(IN_FLIGHT_STATES.isdisjoint(SETTLED_STATES))

    async def test_printer_info_returns_job_sensor_entity_id_and_in_flight_states(self):
        dev_registry, ent_registry = self._info_with_entity_registry("sensor.test_print_job")
        result = await self._call_printer_info(dev_registry, ent_registry)
        sensor = TinyPrintJobSensor(SimpleNamespace(address="AA:BB:CC:DD:EE:FF", name="Test"))
        ent_registry.async_get_entity_id.assert_called_once_with(
            "sensor", "tinyprint", sensor.unique_id
        )
        self.assertEqual(result["printers"][0]["entity_id"], "sensor.test_print_job")
        self.assertEqual(result["in_flight_states"], sorted(IN_FLIGHT_STATES))
        for state in ("connecting", "printing", "rendering"):
            self.assertIn(state, result["in_flight_states"])
        for state in ("unknown", "unavailable"):
            self.assertNotIn(state, result["in_flight_states"])

    async def test_printer_info_entity_id_is_none_when_the_sensor_is_not_registered(self):
        dev_registry, ent_registry = self._info_with_entity_registry(None)
        result = await self._call_printer_info(dev_registry, ent_registry)
        self.assertIsNone(result["printers"][0]["entity_id"])

    async def test_printer_info_serves_the_font_table(self):
        from custom_components.tinyprint import fonts

        dev_registry, ent_registry = self._info_with_entity_registry(None)
        result = await self._call_printer_info(dev_registry, ent_registry)
        self.assertEqual(list(result["fonts"]), ["roboto_flex", "tiny5"])
        self.assertEqual(result["fonts"]["tiny5"]["sizes"]["values"], [8, 16, 24, 32, 40, 48])
        result["fonts"]["tiny5"]["sizes"]["values"].append(56)
        result["fonts"]["roboto_flex"]["derived"]["cap"] = 1
        self.assertEqual(fonts.FONTS["tiny5"]["sizes"]["values"], [8, 16, 24, 32, 40, 48])
        self.assertEqual(fonts.FONTS["roboto_flex"]["derived"]["cap"], 64)

    async def test_diagnostic_version_matches_manifest(self):
        import json
        from pathlib import Path

        from custom_components.tinyprint.diagnostics import async_get_config_entry_diagnostics

        self.hass.data["tinyprint"] = {
            "one": SimpleNamespace(status="idle", last_attempt=None, last_result={}, font_path=None)
        }
        report = await async_get_config_entry_diagnostics(
            self.hass, SimpleNamespace(entry_id="one")
        )
        manifest = Path(__file__).parents[1] / "custom_components/tinyprint/manifest.json"
        self.assertEqual(report["version"], json.loads(manifest.read_text())["version"])
        self.assertFalse(report["physical_confirmation_available"])

    async def test_unknown_options_are_service_validation_errors(self):
        with self.assertRaises(ServiceValidationError):
            await self.hass.services.async_call(
                "tinyprint",
                "preview",
                {"document": {"kind": "qr", "text": "x", "options": {"evil": "value"}}},
                blocking=True,
                return_response=True,
            )

    async def test_explicit_device_routes_only_matching_printer(self):
        one = SimpleNamespace(
            async_print=AsyncMock(return_value={"confirmation": "transport_only"})
        )
        other = SimpleNamespace(async_print=AsyncMock())
        self.hass.data["tinyprint"] = {"one": one, "two": other}
        registry = MagicMock()
        registry.async_get.return_value = SimpleNamespace(config_entries={"one"})
        document = {"kind": "qr", "text": "x"}
        with patch("custom_components.tinyprint.studio.dr.async_get", return_value=registry):
            result = await self.hass.services.async_call(
                "tinyprint",
                "print_document",
                {"device_id": "chosen", "document": document},
                blocking=True,
                return_response=True,
            )
        one.async_print.assert_awaited_once_with(
            "document", {"document": document, "expected_sha256": None}, 24
        )
        other.async_print.assert_not_awaited()
        self.assertEqual(result["confirmation"], "transport_only")

    async def test_unknown_device_never_falls_back(self):
        registry = MagicMock()
        registry.async_get.return_value = None
        self.hass.data["tinyprint"] = {"one": SimpleNamespace(async_print=AsyncMock())}
        with patch("custom_components.tinyprint.studio.dr.async_get", return_value=registry):
            with self.assertRaises(ServiceValidationError):
                await self.hass.services.async_call(
                    "tinyprint",
                    "print_document",
                    {"device_id": "missing", "document": {"kind": "qr", "text": "x"}},
                    blocking=True,
                )
        self.hass.data["tinyprint"]["one"].async_print.assert_not_awaited()

    async def test_panel_registration_is_idempotent(self):
        from custom_components.tinyprint.studio import async_setup_panel

        self.hass.http = SimpleNamespace(async_register_static_paths=AsyncMock())
        with patch(
            "custom_components.tinyprint.studio.panel_custom.async_register_panel", new=AsyncMock()
        ) as register:
            await asyncio.gather(async_setup_panel(self.hass), async_setup_panel(self.hass))
            register.assert_awaited_once()
            self.assertTrue(register.call_args.kwargs["require_admin"])
            self.assertEqual(register.call_args.kwargs["frontend_url_path"], "tinyprint")
        self.hass.http.async_register_static_paths.assert_awaited_once()
        self.hass.http = None

    async def test_loaded_entry_uses_studio_runtime_and_registers_panel(self):
        from custom_components.tinyprint import async_setup_entry
        from custom_components.tinyprint.studio_runtime import StudioPrinter

        entry = MagicMock(
            entry_id="entry", title="Test", data={"address": "AA:BB:CC:DD:EE:FF"}, options={}
        )
        entries = SimpleNamespace(async_forward_entry_setups=AsyncMock())
        with (
            patch.object(self.hass, "config_entries", entries),
            patch("custom_components.tinyprint.async_setup_panel", new=AsyncMock()) as panel,
        ):
            self.assertTrue(await async_setup_entry(self.hass, entry))
            panel.assert_awaited_once_with(self.hass)
        self.assertIsInstance(self.hass.data["tinyprint"]["entry"], StudioPrinter)

    async def test_nonadmin_cannot_use_studio_actions(self):
        from homeassistant.core import Context
        from homeassistant.exceptions import Unauthorized

        auth = SimpleNamespace(
            async_get_user=AsyncMock(return_value=SimpleNamespace(is_admin=False))
        )
        with patch.object(self.hass, "auth", auth, create=True):
            with self.assertRaises(Unauthorized):
                await self.hass.services.async_call(
                    "tinyprint",
                    "preview",
                    {"document": {"kind": "qr", "text": "private"}},
                    blocking=True,
                    return_response=True,
                    context=Context(user_id="nonadmin"),
                )

    async def test_runtime_failure_records_phase_without_secret_text(self):
        from custom_components.tinyprint.studio_runtime import StudioPrinter

        printer = StudioPrinter(self.hass, "one", "AA:BB:CC:DD:EE:FF", "Test", None)
        with (
            patch.object(printer, "_render", return_value=(bytes(48), 1)),
            patch(
                "custom_components.tinyprint.studio_runtime.bluetooth.async_ble_device_from_address",
                return_value=object(),
            ),
            patch(
                "custom_components.tinyprint.studio_runtime.establish_connection",
                new=AsyncMock(side_effect=TimeoutError("PRIVATE CONTENT")),
            ),
        ):
            with self.assertLogs(
                "custom_components.tinyprint.studio_runtime", level="INFO"
            ) as logs:
                with self.assertRaises(HomeAssistantError):
                    await printer.async_print("text", "PRIVATE CONTENT")
        self.assertEqual(printer.last_result["failure_phase"], "connecting_gatt")
        self.assertEqual(printer.last_result["bytes_written"], 0)
        self.assertNotIn("PRIVATE CONTENT", "\n".join(logs.output))
        self.assertEqual(printer.status, "error")

    async def _call_print_bitmap(self, printer, data):
        self.hass.data["tinyprint"] = {"one": printer}
        registry = MagicMock()
        registry.async_get.return_value = SimpleNamespace(config_entries={"one"})
        with patch("custom_components.tinyprint.studio.dr.async_get", return_value=registry):
            return await self.hass.services.async_call(
                "tinyprint",
                "print_bitmap",
                {"device_id": "chosen", **data},
                blocking=True,
                return_response=True,
            )

    async def test_print_bitmap_rejects_each_invalid_input_before_printing(self):
        from custom_components.tinyprint.bitmap import MAX_BITMAP_B64_LENGTH

        def b64(size):
            return base64.b64encode(bytes(size)).decode()

        cases = {
            "rows true": {"rows": True, "bitmap_b64": b64(48)},
            "rows zero": {"rows": 0, "bitmap_b64": b64(48)},
            "rows too large": {"rows": 2049, "bitmap_b64": b64(48)},
            "rows string": {"rows": "1", "bitmap_b64": b64(48)},
            "invalid base64": {"rows": 1, "bitmap_b64": "!!!!"},
            "oversized base64": {"rows": 1, "bitmap_b64": "A" * (MAX_BITMAP_B64_LENGTH + 4)},
            "too short": {"rows": 1, "bitmap_b64": b64(47)},
            "too long": {"rows": 1, "bitmap_b64": b64(49)},
        }
        printer = SimpleNamespace(async_print=AsyncMock())
        for name, data in cases.items():
            with self.subTest(name):
                with self.assertRaises(ServiceValidationError):
                    await self._call_print_bitmap(printer, data)
                printer.async_print.assert_not_awaited()

    async def test_print_bitmap_prints_exact_bytes_and_returns_server_hash(self):
        import hashlib

        data = bytes(range(96))
        printer = SimpleNamespace(async_print=AsyncMock(return_value={"phase": "sent"}))
        result = await self._call_print_bitmap(
            printer, {"rows": 2, "bitmap_b64": base64.b64encode(data).decode()}
        )
        printer.async_print.assert_awaited_once_with("bitmap", {"rows": 2, "bitmap": data}, 24)
        self.assertEqual(result["phase"], "sent")
        self.assertEqual(result["bitmap_sha256"], hashlib.sha256(data).hexdigest())

    async def test_print_bitmap_requires_admin(self):
        from homeassistant.core import Context
        from homeassistant.exceptions import Unauthorized

        printer = SimpleNamespace(async_print=AsyncMock())
        self.hass.data["tinyprint"] = {"one": printer}
        auth = SimpleNamespace(
            async_get_user=AsyncMock(return_value=SimpleNamespace(is_admin=False))
        )
        with patch.object(self.hass, "auth", auth, create=True):
            with self.assertRaises(Unauthorized):
                await self.hass.services.async_call(
                    "tinyprint",
                    "print_bitmap",
                    {
                        "device_id": "chosen",
                        "rows": 1,
                        "bitmap_b64": base64.b64encode(bytes(48)).decode(),
                    },
                    blocking=True,
                    return_response=True,
                    context=Context(user_id="nonadmin"),
                )
        printer.async_print.assert_not_awaited()

    async def test_studio_printer_render_bitmap_returns_payload_unchanged(self):
        from custom_components.tinyprint.studio_runtime import StudioPrinter

        printer = StudioPrinter(self.hass, "one", "AA:BB:CC:DD:EE:FF", "Test", None)
        data = bytes(range(144))
        self.assertEqual(printer._render("bitmap", {"rows": 3, "bitmap": data}, 24), (data, 3))

    async def test_print_bitmap_bytes_reach_the_transport_unchanged(self):
        from custom_components.tinyprint.studio_runtime import StudioPrinter

        printer = StudioPrinter(self.hass, "one", "AA:BB:CC:DD:EE:FF", "Test", None)
        data = bytes(range(96))
        client = SimpleNamespace(is_connected=True, disconnect=AsyncMock())
        transmit = AsyncMock(return_value={"confirmation": "transport_only"})
        with (
            patch(
                "custom_components.tinyprint.studio_runtime.bluetooth.async_ble_device_from_address",
                return_value=object(),
            ),
            patch(
                "custom_components.tinyprint.studio_runtime.establish_connection",
                new=AsyncMock(return_value=client),
            ),
            patch("custom_components.tinyprint.studio_runtime.send_bitmap", new=transmit),
        ):
            await printer.async_print("bitmap", {"rows": 2, "bitmap": data}, 24)
        self.assertEqual(transmit.await_args.args[1:3], (data, 2))

    async def test_configured_tail_rows_reach_the_transport(self):
        from custom_components.tinyprint.studio_runtime import StudioPrinter

        printer = StudioPrinter(self.hass, "one", "AA:BB:CC:DD:EE:FF", "Test", None, tail_rows=40)
        default = StudioPrinter(self.hass, "two", "AA:BB:CC:DD:EE:FF", "Test", None)
        client = SimpleNamespace(is_connected=True, disconnect=AsyncMock())
        for target, expected in ((printer, 40), (default, 96)):
            transmit = AsyncMock(return_value={"confirmation": "transport_only"})
            with (
                patch.object(target, "_render", return_value=(bytes(48), 1)),
                patch(
                    "custom_components.tinyprint.studio_runtime.bluetooth.async_ble_device_from_address",
                    return_value=object(),
                ),
                patch(
                    "custom_components.tinyprint.studio_runtime.establish_connection",
                    new=AsyncMock(return_value=client),
                ),
                patch("custom_components.tinyprint.studio_runtime.send_bitmap", new=transmit),
            ):
                await target.async_print("text", "Hello")
            self.assertEqual(transmit.await_args.kwargs["tail_rows"], expected)

    async def test_user_cancel_reports_neutral_reason_and_releases_connection(self):
        from custom_components.tinyprint.studio_runtime import StudioPrinter

        printer = StudioPrinter(self.hass, "one", "AA:BB:CC:DD:EE:FF", "Test", None)
        entered = asyncio.Event()
        client = SimpleNamespace(is_connected=True, disconnect=AsyncMock())

        async def transmit(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        with (
            patch.object(printer, "_render", return_value=(bytes(48), 1)),
            patch(
                "custom_components.tinyprint.studio_runtime.bluetooth.async_ble_device_from_address",
                return_value=object(),
            ),
            patch(
                "custom_components.tinyprint.studio_runtime.establish_connection",
                new=AsyncMock(return_value=client),
            ),
            patch("custom_components.tinyprint.studio_runtime.send_bitmap", new=transmit),
        ):
            caller = asyncio.create_task(printer.async_print("text", "Hello"))
            await asyncio.wait_for(entered.wait(), 2)
            await printer.async_cancel()
            with self.assertRaises(HomeAssistantError) as raised:
                await caller
        self.assertNotIn("unloaded", str(raised.exception))
        self.assertIn("cannot be recalled", str(raised.exception))
        self.assertFalse(caller.cancelled())
        self.assertIsNone(printer.task)
        self.assertEqual(printer.status, "error")
        self.assertTrue(printer.last_result.get("failure_phase"))
        client.disconnect.assert_awaited()

    async def test_cancel_during_render_holds_printer_until_worker_finishes(self):
        await self._assert_cancel_waits_for_render(cancels=1)

    async def test_repeated_cancel_during_render_still_holds_printer(self):
        await self._assert_cancel_waits_for_render(cancels=2)

    async def test_repeated_caller_cancel_during_render_still_holds_printer(self):
        await self._assert_cancel_waits_for_render(cancels=0, caller_cancels=2)

    async def _assert_cancel_waits_for_render(self, cancels, caller_cancels=0):
        from custom_components.tinyprint.studio_runtime import StudioPrinter

        printer = StudioPrinter(self.hass, "one", "AA:BB:CC:DD:EE:FF", "Test", None)
        started = threading.Event()
        release = threading.Event()

        def render(*args):
            started.set()
            release.wait(5)
            return bytes(48), 1

        try:
            with (
                patch.object(printer, "_render", new=render),
                patch(
                    "custom_components.tinyprint.studio_runtime.establish_connection",
                    new=AsyncMock(),
                ) as connect,
            ):
                caller = asyncio.create_task(printer.async_print("text", "Hello"))
                await asyncio.wait_for(asyncio.to_thread(started.wait, 2), 3)
                cancel_tasks = []
                for _ in range(cancels):
                    cancel_tasks.append(asyncio.create_task(printer.async_cancel()))
                    await asyncio.sleep(0.2)
                for _ in range(caller_cancels):
                    caller.cancel()
                    await asyncio.sleep(0.2)
                self.assertTrue(printer.lock.locked())
                with self.assertRaises(ServiceValidationError):
                    await printer.async_print("text", "Second")
                self.assertFalse(caller.done())
                self.assertFalse(any(task.done() for task in cancel_tasks))
                release.set()
                await asyncio.wait_for(asyncio.gather(*cancel_tasks), 3)
                if caller_cancels:
                    with self.assertRaises(asyncio.CancelledError):
                        await asyncio.wait_for(caller, 3)
                else:
                    with self.assertRaises(HomeAssistantError) as raised:
                        await asyncio.wait_for(caller, 3)
                    self.assertIn("cannot be recalled", str(raised.exception))
        finally:
            release.set()
        self.assertFalse(printer.lock.locked())
        self.assertIsNone(printer.task)
        connect.assert_not_called()

    async def test_diagnose_cancel_says_no_print_was_sent(self):
        from custom_components.tinyprint.studio_runtime import StudioPrinter

        printer = StudioPrinter(self.hass, "one", "AA:BB:CC:DD:EE:FF", "Test", None)
        entered = asyncio.Event()

        async def connect(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        with (
            patch(
                "custom_components.tinyprint.studio_runtime.bluetooth.async_ble_device_from_address",
                return_value=object(),
            ),
            patch("custom_components.tinyprint.studio_runtime.establish_connection", new=connect),
        ):
            caller = asyncio.create_task(printer.async_diagnose())
            await asyncio.wait_for(entered.wait(), 2)
            await printer.async_cancel()
            with self.assertRaises(HomeAssistantError) as raised:
                await caller
        self.assertIn("no print was sent", str(raised.exception))
        self.assertNotIn("recalled", str(raised.exception))
        self.assertIsNone(printer.task)

    async def _assert_invisible_printer_message_kept(self, start):
        from custom_components.tinyprint.studio_runtime import StudioPrinter

        printer = StudioPrinter(self.hass, "one", "AA:BB:CC:DD:EE:FF", "Test", None)
        with (
            patch.object(printer, "_render", return_value=(bytes(48), 1)),
            patch(
                "custom_components.tinyprint.studio_runtime.bluetooth.async_ble_device_from_address",
                return_value=None,
            ),
            patch(
                "custom_components.tinyprint.studio_runtime.establish_connection", new=AsyncMock()
            ) as connect,
        ):
            with self.assertRaises(HomeAssistantError) as raised:
                await start(printer)
        self.assertIn("not visible", str(raised.exception))
        self.assertEqual(printer.status, "error")
        self.assertEqual(printer.last_result["failure_phase"], "connecting_gatt")
        self.assertEqual(printer.last_result["bytes_written"], 0)
        connect.assert_not_called()

    async def test_print_keeps_invisible_printer_message(self):
        await self._assert_invisible_printer_message_kept(
            lambda printer: printer.async_print("text", "Hello")
        )

    async def test_diagnose_keeps_invisible_printer_message(self):
        await self._assert_invisible_printer_message_kept(lambda printer: printer.async_diagnose())

    async def test_preview_hash_mismatch_fails_before_bluetooth(self):
        from custom_components.tinyprint.studio_runtime import StudioPrinter

        printer = StudioPrinter(self.hass, "one", "AA:BB:CC:DD:EE:FF", "Test", None)
        with patch(
            "custom_components.tinyprint.studio_runtime.establish_connection", new=AsyncMock()
        ) as connect:
            with self.assertRaises(ServiceValidationError):
                await printer.async_print(
                    "document",
                    {"document": {"kind": "qr", "text": "x"}, "expected_sha256": "0" * 64},
                )
        connect.assert_not_awaited()

    async def test_partial_write_counters_survive_failure(self):
        from custom_components.tinyprint.studio_runtime import StudioPrinter, TrackedClient

        printer = StudioPrinter(self.hass, "one", "AA:BB:CC:DD:EE:FF", "Test", None)
        printer.begin()
        client = TrackedClient(
            SimpleNamespace(
                write_gatt_char=AsyncMock(side_effect=[None, RuntimeError("PRIVATE CONTENT")])
            ),
            printer,
        )
        await client.write_gatt_char("test", b"abc", response=False)
        with self.assertRaises(RuntimeError):
            await client.write_gatt_char("test", b"xyz", response=False)
        self.assertEqual(printer.last_result["bytes_written"], 3)
        self.assertTrue(printer.last_result["delivery_uncertain"])

    async def test_diagnose_never_sends_raster(self):
        from custom_components.tinyprint.protocol import NOTIFY_UUID, WRITE_UUID
        from custom_components.tinyprint.studio_runtime import StudioPrinter

        printer = StudioPrinter(self.hass, "one", "AA:BB:CC:DD:EE:FF", "Test", None)
        chars = {
            WRITE_UUID: SimpleNamespace(uuid=WRITE_UUID, properties=["write-without-response"]),
            NOTIFY_UUID: SimpleNamespace(uuid=NOTIFY_UUID, properties=["notify"]),
        }

        class Services:
            def __iter__(self):
                return iter([SimpleNamespace(uuid="service", characteristics=list(chars.values()))])

            def get_characteristic(self, uuid):
                return chars.get(uuid)

        client = SimpleNamespace(
            services=Services(),
            is_connected=True,
            disconnect=AsyncMock(),
            write_gatt_char=AsyncMock(),
            start_notify=AsyncMock(),
            stop_notify=AsyncMock(),
        )
        with (
            patch(
                "custom_components.tinyprint.studio_runtime.bluetooth.async_ble_device_from_address",
                return_value=object(),
            ),
            patch(
                "custom_components.tinyprint.studio_runtime.establish_connection",
                new=AsyncMock(return_value=client),
            ),
            patch("custom_components.tinyprint.transport.asyncio.sleep", new=AsyncMock()),
        ):
            result = await printer.async_diagnose()
        # Only the A3 state, A8 info and BA battery queries; never a raster row (A2) or feed.
        written = [call.args[1] for call in client.write_gatt_char.await_args_list]
        self.assertEqual([data[2] for data in written], [0xA3, 0xA8, 0xBA])
        self.assertEqual(result["probe"], [])
        client.disconnect.assert_awaited()
        self.assertEqual(result["confirmation"], "connection_only")
        self.assertTrue(result["supported_profile"])

    async def test_diagnose_reports_an_unsupported_profile_instead_of_failing(self):
        from custom_components.tinyprint.studio_runtime import StudioPrinter

        printer = StudioPrinter(self.hass, "one", "AA:BB:CC:DD:EE:FF", "Test", None)
        chars = {"2a37": SimpleNamespace(uuid="2a37", properties=["notify"])}

        class Services:
            def __iter__(self):
                return iter([SimpleNamespace(uuid="service", characteristics=list(chars.values()))])

            def get_characteristic(self, uuid):
                return chars.get(uuid)

        client = SimpleNamespace(
            services=Services(),
            is_connected=True,
            disconnect=AsyncMock(),
            write_gatt_char=AsyncMock(side_effect=RuntimeError("characteristic not found")),
            start_notify=AsyncMock(),
            stop_notify=AsyncMock(),
        )
        with (
            patch(
                "custom_components.tinyprint.studio_runtime.bluetooth.async_ble_device_from_address",
                return_value=object(),
            ),
            patch(
                "custom_components.tinyprint.studio_runtime.establish_connection",
                new=AsyncMock(return_value=client),
            ),
            patch("custom_components.tinyprint.transport.asyncio.sleep", new=AsyncMock()),
        ):
            result = await printer.async_diagnose()
        client.write_gatt_char.assert_not_awaited()
        self.assertFalse(result["supported_profile"])
        self.assertEqual(result["confirmation"], "connection_only")
        client.disconnect.assert_awaited()


if __name__ == "__main__":
    unittest.main()
