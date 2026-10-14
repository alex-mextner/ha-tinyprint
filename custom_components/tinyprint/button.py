"""User-operated test print button; no automatic prints on setup/reload."""

from datetime import datetime, timezone

from homeassistant.components.button import ButtonEntity

from .const import DOMAIN
from .entity import TinyPrintEntity


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([TinyPrintTestButton(hass.data[DOMAIN][entry.entry_id])])


class TinyPrintTestButton(TinyPrintEntity, ButtonEntity):
    _attr_translation_key = "test_print"
    _attr_icon = "mdi:printer-check"

    def __init__(self, printer) -> None:
        super().__init__(printer)
        self._attr_unique_id = f"{printer.address}_test_print"

    async def async_press(self) -> None:
        timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        await self.printer.async_print(
            "text",
            f"Home Assistant\nTinyPrint X6h\nПроверка печати\n{timestamp}\n0123456789\nABCDEFGHIJKLMN",
            24,
        )
