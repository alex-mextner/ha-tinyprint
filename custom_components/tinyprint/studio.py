"""Authenticated Studio actions and an idempotent, packaged Home Assistant panel."""

import asyncio
from pathlib import Path
from typing import Any

import voluptuous as vol
from homeassistant.components import bluetooth, panel_custom
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.service import async_register_admin_service

from .const import DOMAIN
from .diagnostics import transport_info
from .studio_render import PRESETS, TEMPLATES, preview_result, render_document

DATA_KEY = "tinyprint_studio"
PANEL_VERSION = "0.2.1"


def _printer(hass: HomeAssistant, device_id: str) -> Any:
    device = dr.async_get(hass).async_get(device_id)
    matches = [
        printer
        for entry_id, printer in hass.data[DOMAIN].items()
        if device and entry_id in device.config_entries
    ]
    if len(matches) != 1:
        raise ServiceValidationError(
            "Select one loaded TinyPrint device; an unknown device never falls back to another printer"
        )
    return matches[0]


def _preview(
    hass: HomeAssistant, document: dict[str, Any], font_path: str | None
) -> dict[str, Any]:
    return preview_result(render_document(document, font_path, hass.config.is_allowed_path))


async def async_register_studio_services(hass: HomeAssistant) -> None:
    if DATA_KEY in hass.data:
        return
    state = hass.data[DATA_KEY] = {
        "preview_slots": asyncio.Semaphore(2),
        "panel_lock": asyncio.Lock(),
        "static": False,
        "panel": False,
    }

    async def preview(call: ServiceCall) -> dict[str, Any]:
        chosen = _printer(hass, call.data["device_id"]) if call.data.get("device_id") else None
        if chosen is None:
            printers = list(hass.data[DOMAIN].values())
            if len(printers) > 1:
                raise ServiceValidationError("Select a device for an unambiguous preview font")
            chosen = printers[0] if printers else None
        lock = state["preview_slots"]
        if lock.locked():
            raise ServiceValidationError("Preview is busy; try again when it finishes")
        async with lock:
            job = hass.async_add_executor_job(
                _preview, hass, call.data["document"], chosen.font_path if chosen else None
            )
            try:
                return await asyncio.shield(job)
            except asyncio.CancelledError:
                # Hold the slot until the bounded worker actually finishes.
                await asyncio.wait({job})
                if not job.cancelled():
                    job.exception()
                raise
            except (ValueError, OSError) as error:
                raise ServiceValidationError(str(error)) from error

    async def print_document(call: ServiceCall) -> dict[str, Any] | None:
        printer = _printer(hass, call.data["device_id"])
        document = call.data.get("document")
        if call.service == "print_template":
            document = {
                "kind": "template",
                "template": call.data["template"],
                "fields": call.data.get("fields", {}),
                "options": call.data.get("options", {}),
            }
        result = await printer.async_print(
            "document",
            {"document": document, "expected_sha256": call.data.get("expected_sha256")},
            24,
        )
        return result if call.return_response else None

    async def printer_info(call: ServiceCall) -> dict[str, Any]:
        registry = dr.async_get(hass)
        printers = []
        for printer in hass.data[DOMAIN].values():
            device = registry.async_get_device(identifiers={(DOMAIN, printer.address)})
            info = bluetooth.async_last_service_info(hass, printer.address, connectable=True)
            present = bluetooth.async_address_present(hass, printer.address, connectable=True)
            transport = transport_info(printer)
            if transport["mode"] == "bridge" and hasattr(printer, "async_bridge_status"):
                # HTTP status only (cached): never a radio connect from a diagnostics read.
                await printer.async_bridge_status(max_age=10)
                transport = transport_info(printer)
            printers.append(
                {
                    "device_id": device.id if device else None,
                    "name": printer.name,
                    "status": printer.status,
                    "transport": transport,
                    # In bridge mode these describe HA's own radio, not the printer's reachability.
                    "local_scan_only": transport["mode"] == "bridge",
                    "visible": present,
                    "rssi_raw": info.rssi if info else None,
                    "last_error": printer.last_error,
                    "last_result": dict(printer.last_result),
                }
            )
        return {
            "printers": printers,
            "templates": TEMPLATES,
            "presets": PRESETS,
            "version": PANEL_VERSION,
        }

    async def diagnose(call: ServiceCall) -> dict[str, Any] | None:
        result = await _printer(hass, call.data["device_id"]).async_diagnose()
        return result if call.return_response else None

    async def cancel(call: ServiceCall) -> dict[str, Any] | None:
        printer = _printer(hass, call.data["device_id"])
        owned = getattr(printer, "task", None) is not None
        await printer.async_cancel()
        response = {"status": printer.status}
        if not owned and getattr(printer, "bridge", None) is not None:
            response.update(await printer.async_cancel_bridge_orphan())
        return response if call.return_response else None

    target = {vol.Required("device_id"): str}
    digest = {vol.Optional("expected_sha256"): vol.Match(r"^[0-9a-f]{64}$")}
    definitions = (
        (
            "preview",
            preview,
            {vol.Required("document"): dict, vol.Optional("device_id"): str},
            SupportsResponse.ONLY,
        ),
        (
            "print_document",
            print_document,
            target | {vol.Required("document"): dict} | digest,
            SupportsResponse.OPTIONAL,
        ),
        (
            "print_template",
            print_document,
            target
            | {
                vol.Required("template"): vol.In(TEMPLATES),
                vol.Optional("fields", default={}): dict,
                vol.Optional("options", default={}): dict,
            }
            | digest,
            SupportsResponse.OPTIONAL,
        ),
        ("printer_info", printer_info, {}, SupportsResponse.ONLY),
        ("diagnose", diagnose, target, SupportsResponse.OPTIONAL),
        ("cancel", cancel, target, SupportsResponse.OPTIONAL),
    )
    # These actions can access configured local paths and control physical hardware.
    # Admin UI users and trusted HA automations only. No anonymous endpoints.
    for name, handler, schema, response in definitions:
        async_register_admin_service(
            hass, DOMAIN, name, handler, schema=vol.Schema(schema), supports_response=response
        )


async def async_setup_panel(hass: HomeAssistant) -> None:
    """Register once even when multiple printers load/reload concurrently."""
    state = hass.data[DATA_KEY]
    async with state["panel_lock"]:
        if not state["static"]:
            await hass.http.async_register_static_paths(
                [
                    StaticPathConfig(
                        "/tinyprint_static", str(Path(__file__).parent / "frontend"), False
                    )
                ]
            )
            state["static"] = True
        if not state["panel"]:
            await panel_custom.async_register_panel(
                hass,
                frontend_url_path="tinyprint",
                webcomponent_name="tinyprint-panel",
                sidebar_title="TinyPrint",
                sidebar_icon="mdi:printer",
                module_url=f"/tinyprint_static/tinyprint-panel.js?v={PANEL_VERSION}",
                require_admin=True,
            )
            state["panel"] = True
