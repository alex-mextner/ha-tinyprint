"""Diagnostics intentionally omit print content, paths and hardware addresses."""

from .const import DOMAIN


def transport_info(printer) -> dict:
    """Secret-free transport summary; printers from before the bridge existed are native."""
    describe = getattr(printer, "transport_info", None)
    return describe() if describe else {"mode": "native", "bridge_configured": False}


async def async_get_config_entry_diagnostics(hass, entry):
    printer = hass.data[DOMAIN][entry.entry_id]
    return {
        "version": "0.2.1",
        "transport": transport_info(printer),
        "status": printer.status,
        "last_attempt": printer.last_attempt,
        "last_result": printer.last_result,
        "font_configured": bool(printer.font_path),
        "physical_confirmation_available": False,
    }
