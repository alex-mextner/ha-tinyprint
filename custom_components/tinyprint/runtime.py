"""Home Assistant device routing and per-printer job lifecycle."""

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bleak_retry_connector import BleakClientWithServiceCache, establish_connection
from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .bridge_client import BridgeSettings
from .connection import connected_client
from .const import TRANSPORT_BRIDGE, TRANSPORT_NATIVE, TRANSPORT_PROXY, UPDATE_SIGNAL
from .job import run_owned_job
from .protocol import DEFAULT_TAIL_ROWS
from .render import render_image, render_qr, render_text, to_bitmap
from .transport import send_bitmap

_LOGGER = logging.getLogger(__name__)


class TinyPrintPrinter:
    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        address: str,
        name: str,
        font_path: str | None,
        *,
        bridge: BridgeSettings | None = None,
        bridge_error: str | None = None,
        local_adapter: str | None = None,
        proxy_source: str | None = None,
        tail_rows: int = DEFAULT_TAIL_ROWS,
    ) -> None:
        self.hass = hass
        self.entry_id = entry_id
        self.address = address
        self.name = name
        self.font_path = font_path
        self.lock = asyncio.Lock()
        self.status = "idle"
        self.last_error: str | None = None
        self.last_result: dict[str, Any] = {}
        self.last_attempt: str | None = None
        self.task: asyncio.Task | None = None
        # A selected-but-broken bridge stays "bridge" so printing fails closed, never to BLE.
        self.bridge = bridge
        self.bridge_error = bridge_error
        self.adapter_mac = local_adapter
        self.proxy_source = proxy_source
        self.tail_rows = tail_rows
        self.transport = (
            "local_le"
            if local_adapter is not None
            else TRANSPORT_PROXY
            if proxy_source is not None
            else TRANSPORT_BRIDGE
            if bridge or bridge_error
            else TRANSPORT_NATIVE
        )
        self.bridge_state = (
            "native"
            if self.transport in (TRANSPORT_NATIVE, TRANSPORT_PROXY)
            else "misconfigured"
            if bridge_error
            else "unchecked"
        )
        self.bridge_checked_at: str | None = None

    def transport_info(self) -> dict[str, Any]:
        """Secret-free transport summary: no URL, host or token."""
        return {
            "mode": self.transport,
            "local_adapter_configured": self.transport == "local_le" and bool(self.adapter_mac),
            "bridge_configured": self.bridge is not None,
            "bridge_state": self.bridge_state,
            "bridge_scheme": self.bridge.scheme if self.bridge else None,
            "bridge_checked_at": self.bridge_checked_at,
        }

    def update(self, state: str) -> None:
        self.status = state
        async_dispatcher_send(self.hass, UPDATE_SIGNAL.format(self.entry_id))

    def _render(self, kind: str, payload: str, font_size: int) -> tuple[bytes, int]:
        if kind == "text":
            if self.font_path and (
                Path(self.font_path).suffix.lower() not in {".ttf", ".otf"}
                or not self.hass.config.is_allowed_path(self.font_path)
            ):
                raise ServiceValidationError(
                    "Font must be a TTF/OTF file in an allowed Home Assistant path"
                )
            image = render_text(payload, font_size, self.font_path)
        elif kind == "qr":
            image = render_qr(payload)
        elif kind == "image":
            if not self.hass.config.is_allowed_path(payload):
                raise ServiceValidationError("Image is outside Home Assistant's allowed paths")
            image = render_image(payload)
        else:
            raise ServiceValidationError("Unsupported print operation")
        return to_bitmap(image)

    async def async_print(self, kind: str, payload: str, font_size: int = 24) -> dict[str, Any]:
        if self.lock.locked():
            raise ServiceValidationError("Printer is busy; wait for the current job")
        async with self.lock:
            return await run_owned_job(self, self._async_print_job(kind, payload, font_size))

    async def _async_print_job(self, kind: str, payload: str, font_size: int) -> dict[str, Any]:
        self.last_error = None
        self.last_result = {}
        self.last_attempt = datetime.now(timezone.utc).isoformat()
        self.update("rendering")
        try:
            bitmap, height = await self.hass.async_add_executor_job(
                self._render, kind, payload, font_size
            )
            self.update("connecting")
            device = bluetooth.async_ble_device_from_address(
                self.hass, self.address, connectable=True
            )
            if device is None:
                raise HomeAssistantError(
                    "Printer is not visible. Turn it on, keep it near a connectable Bluetooth adapter and close Tiny Print on the phone"
                )
            # Retry only the connection: never replay data after printing starts.
            async with connected_client(
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
            ) as client:
                self.update("printing")
                self.last_result = await send_bitmap(
                    client, bitmap, height, tail_rows=self.tail_rows
                )
            self.update("sent")
            _LOGGER.info(
                "TinyPrint job sent: rows=%s bytes=%s confirmation=transport_only",
                height,
                self.last_result["bytes_written"],
            )
            return self.last_result
        except asyncio.CancelledError:
            self.last_error = "cancelled"
            self.update("error")
            raise
        except (ServiceValidationError, HomeAssistantError) as error:
            self.last_error = str(error)
            self.update("error")
            raise
        except ValueError as error:
            self.last_error = str(error)
            self.update("error")
            raise ServiceValidationError(str(error)) from error
        except Exception as error:
            # No payload, image path or Bluetooth packet data is logged.
            self.last_error = type(error).__name__
            self.update("error")
            _LOGGER.warning("TinyPrint job failed (%s); not replayed", self.last_error)
            raise HomeAssistantError(
                f"Print failed ({self.last_error}); check printer power, paper and Bluetooth. A partial job was not replayed"
            ) from error

    async def async_cancel(self) -> None:
        if self.task and self.task is not asyncio.current_task():
            self.task.cancel()
            await asyncio.wait({self.task})
