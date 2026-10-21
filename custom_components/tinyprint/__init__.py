"""TinyPrint X6h native Home Assistant integration."""

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN
from .studio import async_register_studio_services, async_setup_panel
from .studio_runtime import StudioPrinter as TinyPrintPrinter

PLATFORMS = [Platform.BUTTON, Platform.SENSOR]
PRINTER_SCHEMA = vol.Schema(
    {
        vol.Required("address"): cv.string,
        vol.Optional("name", default="TinyPrint X6h"): cv.string,
        vol.Optional("font_path", default=""): cv.string,
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
    runtime = TinyPrintPrinter(
        hass,
        entry.entry_id,
        entry.data["address"],
        entry.title,
        entry.options.get("font_path", entry.data.get("font_path")) or None,
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
