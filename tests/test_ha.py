"""Run with stdlib unittest inside the actual HA image; no test stubs for HA APIs."""

import asyncio
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

try:
    from homeassistant.core import HomeAssistant
    from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
except ImportError as error:
    raise unittest.SkipTest("Execute in the deployed Home Assistant Python environment") from error

from custom_components.tinyprint import async_setup
from custom_components.tinyprint.config_flow import TinyPrintConfigFlow, normalize_address
from custom_components.tinyprint.connection import connected_client
from custom_components.tinyprint.const import DOMAIN
from custom_components.tinyprint.runtime import TinyPrintPrinter


class HomeAssistantAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self.tmp.name)
        await async_setup(self.hass, {})

    async def asyncTearDown(self):
        await self.hass.async_stop()
        self.tmp.cleanup()

    async def test_services_register_on_real_ha(self):
        for name in ("print_text", "print_qr", "print_image"):
            self.assertTrue(self.hass.services.has_service(DOMAIN, name))

    async def test_actual_ha_service_dispatch(self):
        printer = SimpleNamespace(
            async_print=AsyncMock(return_value={"confirmation": "transport_only"})
        )
        self.hass.data[DOMAIN]["one"] = printer
        result = await self.hass.services.async_call(
            DOMAIN, "print_text", {"text": "Привет"}, blocking=True, return_response=True
        )
        self.assertEqual(result["confirmation"], "transport_only")
        printer.async_print.assert_awaited_once_with("text", "Привет", 24)

    async def test_multiple_printers_require_selection(self):
        self.hass.data[DOMAIN].update({"one": None, "two": None})
        with self.assertRaises(ServiceValidationError):
            await self.hass.services.async_call(
                DOMAIN, "print_text", {"text": "hello"}, blocking=True
            )

    async def test_no_printer_reports_validation_error(self):
        with self.assertRaises(ServiceValidationError):
            await self.hass.services.async_call(
                DOMAIN, "print_text", {"text": "hello"}, blocking=True
            )

    async def test_invalid_mac_form(self):
        flow = TinyPrintConfigFlow()
        flow.hass = self.hass
        result = await flow.async_step_user(
            {"address": "not a MAC", "name": "Printer", "font_path": ""}
        )
        self.assertEqual(result["type"], "form")
        self.assertEqual(result["errors"], {"address": "invalid_address"})

    async def test_address_normalization(self):
        self.assertEqual(normalize_address(" aa:bb:cc:dd:ee:ff "), "AA:BB:CC:DD:EE:FF")
        for value in ("", "foo", "11:22:33:44:55:zz"):
            with self.assertRaises(ValueError):
                normalize_address(value)

    async def test_busy_job_rejected(self):
        printer = TinyPrintPrinter(self.hass, "entry", "AA:BB:CC:DD:EE:FF", "Test", None)
        async with printer.lock:
            with self.assertRaises(ServiceValidationError):
                await printer.async_print("text", "Hello")

    async def test_image_outside_allowed_path_rejected_before_read(self):
        printer = TinyPrintPrinter(self.hass, "entry", "AA:BB:CC:DD:EE:FF", "Test", None)
        with patch.object(self.hass.config, "is_allowed_path", return_value=False):
            with self.assertRaises(ServiceValidationError):
                await printer.async_print("image", "/etc/passwd")
        self.assertEqual(printer.status, "error")

    async def test_offline_printer_fails_without_connection(self):
        printer = TinyPrintPrinter(self.hass, "entry", "AA:BB:CC:DD:EE:FF", "Test", None)
        with (
            patch.object(printer, "_render", return_value=(bytes(48), 1)),
            patch(
                "custom_components.tinyprint.runtime.bluetooth.async_ble_device_from_address",
                return_value=None,
            ),
        ):
            with self.assertRaises(HomeAssistantError):
                await printer.async_print("text", "Hello")
        self.assertEqual(printer.status, "error")

    async def test_transport_failure_marks_error_not_sent(self):
        printer = TinyPrintPrinter(self.hass, "entry", "AA:BB:CC:DD:EE:FF", "Test", None)
        with (
            patch.object(printer, "_render", return_value=(bytes(48), 1)),
            patch(
                "custom_components.tinyprint.runtime.bluetooth.async_ble_device_from_address",
                return_value=object(),
            ),
            patch(
                "custom_components.tinyprint.runtime.establish_connection",
                new=AsyncMock(return_value=object()),
            ),
            patch(
                "custom_components.tinyprint.runtime.send_bitmap",
                new=AsyncMock(side_effect=TimeoutError),
            ),
        ):
            with self.assertRaises(HomeAssistantError):
                await printer.async_print("text", "Hello")
        self.assertEqual(printer.status, "error")

    async def test_success_marks_only_sent(self):
        printer = TinyPrintPrinter(self.hass, "entry", "AA:BB:CC:DD:EE:FF", "Test", None)
        stats = {"confirmation": "transport_only", "rows": 1, "bytes_written": 160}
        with (
            patch.object(printer, "_render", return_value=(bytes(48), 1)),
            patch(
                "custom_components.tinyprint.runtime.bluetooth.async_ble_device_from_address",
                return_value=object(),
            ),
            patch(
                "custom_components.tinyprint.runtime.establish_connection",
                new=AsyncMock(return_value=object()),
            ),
            patch(
                "custom_components.tinyprint.runtime.send_bitmap", new=AsyncMock(return_value=stats)
            ),
        ):
            result = await printer.async_print("text", "Hello")
        self.assertEqual(printer.status, "sent")
        self.assertEqual(result, stats)
        self.assertNotIn("text", printer.last_result)

    async def test_invalid_import_aborts_instead_of_dangling_form(self):
        flow = TinyPrintConfigFlow()
        flow.hass = self.hass
        result = await flow.async_step_import({"address": "invalid"})
        self.assertEqual(result["type"], "abort")
        self.assertEqual(result["reason"], "invalid_address")

    async def test_unload_cancels_only_owned_job_not_service_caller(self):
        printer = TinyPrintPrinter(self.hass, "entry", "AA:BB:CC:DD:EE:FF", "Test", None)
        entered = asyncio.Event()
        cleanup = asyncio.Event()

        async def transmit(*args, **kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleanup.set()

        with (
            patch.object(printer, "_render", return_value=(bytes(48), 1)),
            patch(
                "custom_components.tinyprint.runtime.bluetooth.async_ble_device_from_address",
                return_value=object(),
            ),
            patch(
                "custom_components.tinyprint.runtime.establish_connection",
                new=AsyncMock(
                    return_value=SimpleNamespace(is_connected=True, disconnect=AsyncMock())
                ),
            ),
            patch("custom_components.tinyprint.runtime.send_bitmap", new=transmit),
        ):
            caller = asyncio.create_task(printer.async_print("text", "Hello"))
            await asyncio.wait_for(entered.wait(), 2)
            await printer.async_cancel()
            with self.assertRaises(HomeAssistantError):
                await caller
        self.assertFalse(caller.cancelled())
        self.assertTrue(cleanup.is_set())
        self.assertIsNone(printer.task)

    async def test_cancelled_caller_cleans_up_owned_job(self):
        printer = TinyPrintPrinter(self.hass, "entry", "AA:BB:CC:DD:EE:FF", "Test", None)
        entered = asyncio.Event()
        cleanup = asyncio.Event()

        async def transmit(*args, **kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleanup.set()

        with (
            patch.object(printer, "_render", return_value=(bytes(48), 1)),
            patch(
                "custom_components.tinyprint.runtime.bluetooth.async_ble_device_from_address",
                return_value=object(),
            ),
            patch(
                "custom_components.tinyprint.runtime.establish_connection",
                new=AsyncMock(
                    return_value=SimpleNamespace(is_connected=True, disconnect=AsyncMock())
                ),
            ),
            patch("custom_components.tinyprint.runtime.send_bitmap", new=transmit),
        ):
            caller = asyncio.create_task(printer.async_print("text", "Hello"))
            await asyncio.wait_for(entered.wait(), 2)
            caller.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await caller
        self.assertTrue(cleanup.is_set())
        self.assertIsNone(printer.task)

    async def test_connected_scope_releases_client_before_transmission_failure(self):
        client = SimpleNamespace(is_connected=True, disconnect=AsyncMock())
        with self.assertRaises(RuntimeError):
            async with connected_client(self.hass, AsyncMock(return_value=client)()):
                raise RuntimeError("before sender")
        client.disconnect.assert_awaited_once()

    async def test_late_connection_result_is_released_after_cancellation(self):
        entered = asyncio.Event()
        released = asyncio.Event()
        client = SimpleNamespace(is_connected=True, disconnect=AsyncMock(side_effect=released.set))

        async def connect():
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return client

        async def use():
            async with connected_client(self.hass, connect()):
                self.fail("Cancelled connection must never start a print")

        task = asyncio.create_task(use())
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(released.wait(), 2)
        client.disconnect.assert_awaited_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
