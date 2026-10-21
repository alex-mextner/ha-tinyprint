"""Studio jobs and payload-free tracing around the established BLE transport."""

import asyncio
import contextlib
import hashlib
import logging
from time import monotonic
from typing import Any
from uuid import uuid4

from bleak_retry_connector import BleakClientWithServiceCache, establish_connection
from homeassistant.components import bluetooth
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from .connection import connected_client
from .job import run_owned_job
from .protocol import NOTIFY_UUID, WRITE_UUID
from .runtime import TinyPrintPrinter
from .studio_render import bitmap as encode_bitmap
from .studio_render import render_document
from .transport import send_bitmap

_LOGGER = logging.getLogger(__name__)


class TrackedClient:
    """Delegate BLE calls while retaining confirmed partial-write counters.

    No packet contents escape this short-lived object. A failed write is explicitly
    uncertain: it may have reached the device even if the await raised an error.
    """

    def __init__(self, client: Any, printer: Any) -> None:
        self.client = client
        self.printer = printer
        self.pending = bytearray()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.client, name)

    async def start_notify(self, characteristic: str, callback: Any) -> None:
        self.printer.phase("notification_setup", "printing")

        def notified(char: Any, data: bytearray) -> None:
            self.printer.last_result["notifications"] += 1
            callback(char, data)

        await self.client.start_notify(characteristic, notified)
        self.printer.phase("transmitting", "printing")

    async def write_gatt_char(self, characteristic: str, data: bytes, response: bool) -> None:
        try:
            await self.client.write_gatt_char(characteristic, data, response=response)
        except BaseException:
            self.printer.last_result["delivery_uncertain"] = True
            raise
        stats = self.printer.last_result
        stats["bytes_written"] += len(data)
        stats["writes"] += 1
        self.pending.extend(data)
        while len(self.pending) >= 6:
            if self.pending[:2] != b"\x51\x78":
                self.pending.clear()
                break
            length = int.from_bytes(self.pending[4:6], "little") + 8
            if length > 520:
                self.pending.clear()
                break
            if len(self.pending) < length:
                break
            stats["frames_written"] += 1
            del self.pending[:length]
        if stats["writes"] % 100 == 0:
            _LOGGER.debug(
                "TinyPrint job=%s phase=transmitting bytes=%s frames=%s writes=%s",
                stats["job_id"],
                stats["bytes_written"],
                stats["frames_written"],
                stats["writes"],
            )


class StudioPrinter(TinyPrintPrinter):
    """Preserve existing print API/locking, add studio rendering and diagnostics."""

    def begin(self) -> None:
        self.started = monotonic()
        self.last_error = None
        self.last_result = {
            "job_id": uuid4().hex[:12],
            "phase": "rendering",
            "bytes_written": 0,
            "frames_written": 0,
            "writes": 0,
            "notifications": 0,
            "delivery_uncertain": False,
            "confirmation": "none",
        }
        # Preserve the original timestamp format used by the sensor.
        from datetime import datetime, timezone

        self.last_attempt = datetime.now(timezone.utc).isoformat()

    def phase(self, phase: str, status: str) -> None:
        self.last_result["phase"] = phase
        self.last_result["elapsed_seconds"] = round(monotonic() - self.started, 3)
        self.update(status)
        _LOGGER.info(
            "TinyPrint job=%s phase=%s elapsed=%s bytes=%s frames=%s",
            self.last_result["job_id"],
            phase,
            self.last_result["elapsed_seconds"],
            self.last_result["bytes_written"],
            self.last_result["frames_written"],
        )

    def failed(self, error: BaseException) -> None:
        phase = self.last_result["phase"]
        self.last_result["failure_phase"] = phase
        self.last_result["exception_type"] = type(error).__name__
        self.last_result["elapsed_seconds"] = round(monotonic() - self.started, 3)
        self.last_error = f"{phase}: {type(error).__name__}"
        self.update("error")
        # Deliberately omit exception message/traceback: those may contain user data.
        _LOGGER.warning(
            "TinyPrint job=%s failed phase=%s type=%s elapsed=%s bytes=%s frames=%s uncertain=%s replay=false",
            self.last_result["job_id"],
            phase,
            type(error).__name__,
            self.last_result["elapsed_seconds"],
            self.last_result["bytes_written"],
            self.last_result["frames_written"],
            self.last_result["delivery_uncertain"],
        )

    def _render(self, kind: str, payload: Any, font_size: int) -> tuple[bytes, int]:
        if kind != "document":
            return super()._render(kind, payload, font_size)
        if not isinstance(payload, dict) or "document" not in payload:
            raise ValueError("Invalid studio print request")
        image = render_document(
            payload["document"], self.font_path, self.hass.config.is_allowed_path
        )
        data, height = encode_bitmap(image)
        expected = payload.get("expected_sha256")
        if expected is not None and hashlib.sha256(data).hexdigest() != expected:
            raise ValueError("Content changed since preview; make a new preview before printing")
        return data, height

    def connection(self) -> Any:
        self.phase("connecting_gatt", "connecting")
        device = bluetooth.async_ble_device_from_address(self.hass, self.address, connectable=True)
        if device is None:
            raise HomeAssistantError(
                "Printer is not visible to a connectable Bluetooth adapter; turn it on and close other printer clients"
            )
        return connected_client(
            self.hass,
            establish_connection(
                BleakClientWithServiceCache,
                device,
                self.name,
                max_attempts=3,
                ble_device_callback=lambda: bluetooth.async_ble_device_from_address(
                    self.hass, self.address, connectable=True
                ),
            ),
        )

    async def _async_print_job(self, kind: str, payload: Any, font_size: int) -> dict[str, Any]:
        self.begin()
        self.phase("rendering", "rendering")
        try:
            render = self.hass.async_add_executor_job(self._render, kind, payload, font_size)
            try:
                data, height = await asyncio.shield(render)
            except asyncio.CancelledError:
                # Keep the printer lock until the executor render actually finishes,
                # even if another cancel arrives while waiting.
                while not render.done():
                    with contextlib.suppress(asyncio.CancelledError):
                        await asyncio.wait({render})
                if not render.cancelled():
                    render.exception()
                raise
            async with self.connection() as client:
                self.phase("gatt_validation", "printing")
                stats = await send_bitmap(TrackedClient(client, self), data, height)
                self.last_result.update(stats)
            self.phase("sent", "sent")
            return dict(self.last_result)
        except asyncio.CancelledError as error:
            self.failed(error)
            raise
        except HomeAssistantError as error:
            # Already user-facing (e.g. printer not visible); keep the actionable text.
            self.failed(error)
            raise
        except Exception as error:
            failure_phase = self.last_result["phase"]
            self.failed(error)
            if failure_phase == "rendering" and isinstance(
                error, (ValueError, ServiceValidationError)
            ):
                raise ServiceValidationError(str(error)) from error
            partial = bool(
                self.last_result["bytes_written"] or self.last_result["delivery_uncertain"]
            )
            detail = (
                "Partial delivery is possible; no automatic replay."
                if partial
                else "No raster writes were attempted."
            )
            raise HomeAssistantError(
                f"Printer failed at {failure_phase} ({type(error).__name__}). {detail}"
            ) from error

    async def async_diagnose(self) -> dict[str, Any]:
        """Connect and inspect GATT, without printing or changing adapter settings."""
        if self.lock.locked():
            raise ServiceValidationError("Printer is busy; wait or cancel the current job")
        async with self.lock:
            return await run_owned_job(
                self, self._diagnose(), "Printer diagnostic was cancelled; no print was sent"
            )

    async def _diagnose(self) -> dict[str, Any]:
        self.begin()
        try:
            async with self.connection() as client:
                services = []
                for service in client.services:
                    services.append(
                        {
                            "uuid": service.uuid,
                            "characteristics": [
                                {"uuid": char.uuid, "properties": list(char.properties)}
                                for char in list(service.characteristics)[:64]
                            ],
                        }
                    )
                    if len(services) >= 32:
                        break
                write = client.services.get_characteristic(WRITE_UUID)
                notify = client.services.get_characteristic(NOTIFY_UUID)
                supported = bool(
                    write
                    and notify
                    and "write-without-response" in write.properties
                    and "notify" in notify.properties
                )
            self.last_result.update(
                {
                    "services": services,
                    "supported_profile": supported,
                    "confirmation": "connection_only",
                }
            )
            self.phase("diagnosed", "idle")
            return dict(self.last_result)
        except asyncio.CancelledError as error:
            self.failed(error)
            raise
        except HomeAssistantError as error:
            # Already user-facing (e.g. printer not visible); keep the actionable text.
            self.failed(error)
            raise
        except Exception as error:
            self.failed(error)
            raise HomeAssistantError(
                f"GATT diagnostic failed at {self.last_result['failure_phase']} ({type(error).__name__}); no print was sent"
            ) from error
