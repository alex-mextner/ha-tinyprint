"""Own the connection result even when cancellation races with its return."""

import asyncio
import logging
from contextlib import asynccontextmanager

_LOGGER = logging.getLogger(__name__)


async def _close(client):
    if not getattr(client, "is_connected", False):
        return
    try:
        async with asyncio.timeout(5):
            await client.disconnect()
    except Exception as error:
        _LOGGER.debug("Connection cleanup failed: %s", type(error).__name__)


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
