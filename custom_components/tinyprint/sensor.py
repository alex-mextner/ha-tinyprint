"""Last-job state, deliberately not a fabricated printer/battery status."""

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity

from .const import DOMAIN
from .entity import TinyPrintEntity

# Plan 3 adds waiting_for_printer to both IN_FLIGHT_STATES and TinyPrintJobSensor._attr_options.
IN_FLIGHT_STATES = frozenset({"rendering", "connecting", "printing"})
SETTLED_STATES = frozenset({"idle", "sent", "error"})


def job_sensor_unique_id(address: str) -> str:
    return f"{address}_job_status"


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([TinyPrintJobSensor(hass.data[DOMAIN][entry.entry_id])])


class TinyPrintJobSensor(TinyPrintEntity, SensorEntity):
    _attr_translation_key = "job_status"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["idle", "rendering", "connecting", "printing", "sent", "error"]
    _attr_icon = "mdi:printer"

    def __init__(self, printer) -> None:
        super().__init__(printer)
        self._attr_unique_id = job_sensor_unique_id(printer.address)

    @property
    def native_value(self):
        return self.printer.status

    @property
    def extra_state_attributes(self):
        return {
            "last_attempt": self.printer.last_attempt,
            "last_error": self.printer.last_error,
            "physical_print_confirmed": False,
            **self.printer.last_result,
        }
