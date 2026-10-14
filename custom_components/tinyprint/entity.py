"""Shared non-polling entity lifecycle."""

from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo, Entity

from .const import DOMAIN, UPDATE_SIGNAL


class TinyPrintEntity(Entity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, printer) -> None:
        self.printer = printer
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, printer.address)},
            name=printer.name,
            manufacturer="Shenzhen Seven Star Technology",
            model="X6h",
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, UPDATE_SIGNAL.format(self.printer.entry_id), self._updated
            )
        )

    @callback
    def _updated(self) -> None:
        self.async_write_ha_state()
