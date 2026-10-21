"""TinyPrint X6h native Home Assistant integration."""

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr

from .bridge_client import BridgeConfigError, bridge_settings
from .const import CONF_PROXY_SOURCE, CONF_TAIL_FEED_LINES, DOMAIN, TRANSPORT_PROXY
from .protocol import DEFAULT_TAIL_ROWS, normalize_tail_rows
from .studio import async_register_studio_services, async_setup_panel
from .studio_runtime import StudioPrinter as TinyPrintPrinter

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.BUTTON, Platform.SENSOR]
PRINTER_SCHEMA = vol.Schema(
    {
        vol.Required("address"): cv.string,
        vol.Optional("name", default="TinyPrint X6h"): cv.string,
        vol.Optional("font_path", default=""): cv.string,
        vol.Optional("transport"): vol.In(("native", "local_le", TRANSPORT_PROXY)),
        vol.Optional("adapter_mac"): cv.string,
        vol.Optional(CONF_PROXY_SOURCE): cv.string,
    }
)
CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {vol.Optional("printers", default=[]): vol.All(cv.ensure_list, [PRINTER_SCHEMA])}
        )
    },
    extra=vol.ALLOW_EXTRA,
)


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    hass.data.setdefault(DOMAIN, {})
    await async_register_studio_services(hass)

    async def handle(call: ServiceCall) -> dict[str, Any]:
        printers = hass.data[DOMAIN]
        if device_id := call.data.get("device_id"):
            device = dr.async_get(hass).async_get(device_id)
            matches = [
                printer
                for key, printer in printers.items()
                if device and key in device.config_entries
            ]
        else:
            matches = list(printers.values())
        if len(matches) != 1:
            raise ServiceValidationError("Select exactly one configured TinyPrint device")
        kind = call.service.removeprefix("print_")
        key = "path" if kind == "image" else "text"
        return await matches[0].async_print(kind, call.data[key], call.data.get("font_size", 24))

    for operation, payload, limit in (
        ("text", "text", 3000),
        ("qr", "text", 500),
        ("image", "path", 1024),
    ):
        fields = {
            vol.Required(payload): vol.All(cv.string, vol.Length(min=1, max=limit)),
            vol.Optional("device_id"): cv.string,
        }
        if operation == "text":
            fields[vol.Optional("font_size", default=24)] = vol.All(
                vol.Coerce(int), vol.Range(min=12, max=64)
            )
        hass.services.async_register(
            DOMAIN,
            f"print_{operation}",
            handle,
            schema=vol.Schema(fields),
            supports_response=SupportsResponse.OPTIONAL,
        )
    for printer in config.get(DOMAIN, {}).get("printers", []):
        hass.async_create_task(
            hass.config_entries.flow.async_init(
                DOMAIN, context={"source": SOURCE_IMPORT}, data=printer
            )
        )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if not hass.services.has_service(DOMAIN, "print_text"):
        await async_setup(hass, {})
    bridge = bridge_error = None
    try:
        bridge = bridge_settings(entry.options)
    except BridgeConfigError:
        bridge_error = "misconfigured"
        _LOGGER.error(
            "TinyPrint bridge transport is selected but its options are invalid; "
            "printing stays disabled (no Bluetooth fallback) until the options are fixed"
        )
    effective = dict(entry.data) | dict(entry.options)
    local_adapter = (
        (effective.get("adapter_mac") or "") if effective.get("transport") == "local_le" else None
    )
    proxy_source = (
        (effective.get(CONF_PROXY_SOURCE) or "")
        if effective.get("transport") == TRANSPORT_PROXY
        else None
    )
    try:
        tail_rows = normalize_tail_rows(effective.get(CONF_TAIL_FEED_LINES, DEFAULT_TAIL_ROWS))
    except ValueError:
        tail_rows = DEFAULT_TAIL_ROWS
        _LOGGER.warning("Invalid tail_feed_lines option; using %s rows", DEFAULT_TAIL_ROWS)
    runtime = TinyPrintPrinter(
        hass,
        entry.entry_id,
        entry.data["address"],
        entry.title,
        entry.options.get("font_path", entry.data.get("font_path")) or None,
        bridge=bridge,
        bridge_error=bridge_error,
        local_adapter=local_adapter,
        proxy_source=proxy_source,
        tail_rows=tail_rows,
    )
    hass.data[DOMAIN][entry.entry_id] = runtime
    try:
        await async_setup_panel(hass)
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        raise
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    return True


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    runtime = hass.data[DOMAIN].get(entry.entry_id)
    if runtime:
        await runtime.async_cancel()
    if await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id, None)
        return True
    return False
