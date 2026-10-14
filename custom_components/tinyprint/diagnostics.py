"""Diagnostics intentionally omit print content, paths and hardware addresses."""

from .const import DOMAIN


async def async_get_config_entry_diagnostics(hass, entry):
    printer = hass.data[DOMAIN][entry.entry_id]
    return {
        "version": "0.1.0",
        "status": printer.status,
        "last_attempt": printer.last_attempt,
        "last_result": printer.last_result,
        "font_configured": bool(printer.font_path),
        "physical_confirmation_available": False,
    }
