"""Own the connection result even when cancellation races with its return."""

import asyncio
import logging
from contextlib import asynccontextmanager

_LOGGER = logging.getLogger(__name__)

DISCONNECT_TIMEOUT = 5


async def _disconnect(client):
    try:
        await client.disconnect()
    except Exception as error:
        _LOGGER.debug("Connection cleanup failed: %s", type(error).__name__)


async def _close(client):
    if not getattr(client, "is_connected", False):
        return
    loop = asyncio.get_running_loop()
    deadline = loop.time() + DISCONNECT_TIMEOUT
    disconnect = loop.create_task(_disconnect(client))
    cancelled = None
    # A caller cancel must not release the printer while the client is still
    # connected; wait (bounded) for the single disconnect, then re-raise.
    while not disconnect.done():
        remaining = deadline - loop.time()
        if remaining <= 0:
            _LOGGER.debug("Connection cleanup timed out")
            disconnect.cancel()
            break
        try:
            await asyncio.wait({disconnect}, timeout=remaining)
        except asyncio.CancelledError as error:
            cancelled = cancelled or error
    if cancelled is not None:
        raise cancelled


@asynccontextmanager
async def connected_client(hass, connect):
    task = hass.async_create_task(connect, "TinyPrint connect")
    client = None

    def release_late_result(done):
        if not done.cancelled() and done.exception() is None:
            hass.async_create_task(_close(done.result()), "TinyPrint connection cleanup")

    try:
        async with asyncio.timeout(45):
            client = await asyncio.shield(task)
        yield client
    finally:
        if client is not None:
            await _close(client)
        elif task.done():
            if not task.cancelled() and task.exception() is None:
                await _close(task.result())
        else:
            task.add_done_callback(release_late_result)
            task.cancel()
