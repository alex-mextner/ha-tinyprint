"""Connect to the printer through one pinned ESPHome Bluetooth proxy.

Home Assistant normally picks the connection path by signal strength. The X6h is a
dual-mode device that BlueZ on the HA host reaches over BR/EDR, so the job must run on
the proxy's LE radio. The proxy's own bleak backend is therefore used directly instead
of HA's scanner-agnostic client wrapper.
"""

import asyncio
import logging

from bleak import BleakClient
from bleak.exc import BleakError
from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .connection import CONNECT_DEADLINE

_LOGGER = logging.getLogger(__name__)
CONNECT_ATTEMPTS = 3
CONNECT_TIMEOUT = 20.0
RETRY_DELAY = 1.0
# connected_client cancels the connect at CONNECT_DEADLINE; retrying must end sooner so the
# caller gets the last connect error instead of a bare timeout, with room for cleanup.
CONNECT_BUDGET = CONNECT_DEADLINE - 5.0
MIN_ATTEMPT_TIMEOUT = 5.0


async def _quiet_disconnect(client: BleakClient) -> None:
    try:
        await client.disconnect()
    except Exception as error:
        _LOGGER.debug("Proxy connection cleanup failed: %s", type(error).__name__)


async def connect_via_proxy(
    hass: HomeAssistant, address: str, name: str, source: str
) -> BleakClient:
    """Connect through the proxy whose scanner source is `source`; never fall back.

    Only the connection is retried: nothing has been sent to the printer yet.
    """
    last_error: Exception | None = None
    loop = asyncio.get_running_loop()
    deadline = loop.time() + CONNECT_BUDGET
    for attempt in range(1, CONNECT_ATTEMPTS + 1):
        attempt_timeout = min(CONNECT_TIMEOUT, deadline - loop.time())
        found = next(
            (
                item
                for item in bluetooth.async_scanner_devices_by_address(
                    hass, address, connectable=True
                )
                if item.scanner.source.upper() == source and item.scanner.connector is not None
            ),
            None,
        )
        if found is None:
            raise HomeAssistantError(
                "Printer is not visible to the selected Bluetooth proxy; turn it on, keep it "
                "in range of the proxy and close other printer clients"
            )
        connector = found.scanner.connector

        def backend(*args, **kwargs):
            return connector.client(*args, **kwargs)

        client = BleakClient(found.ble_device, backend=backend, timeout=attempt_timeout)
        try:
            await client.connect()
        except (BleakError, TimeoutError) as error:
            last_error = error
            await _quiet_disconnect(client)
            _LOGGER.debug(
                "%s: proxy %s connect attempt %s failed: %s",
                name,
                found.scanner.name,
                attempt,
                type(error).__name__,
            )
            if (
                attempt == CONNECT_ATTEMPTS
                or deadline - loop.time() - RETRY_DELAY < MIN_ATTEMPT_TIMEOUT
            ):
                break
            await asyncio.sleep(RETRY_DELAY)
            continue
        except BaseException:
            await _quiet_disconnect(client)
            raise
        _LOGGER.info("%s: connected via proxy %s", name, found.scanner.name)
        return client
    raise last_error
