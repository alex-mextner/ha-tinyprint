"""Run a cancellable print job without cancelling its HA service caller."""

import asyncio
from collections.abc import Coroutine
from typing import Any

from homeassistant.exceptions import HomeAssistantError


async def run_owned_job(printer: Any, coroutine: Coroutine) -> dict[str, Any]:
    """Keep task ownership inside the printer and propagate caller cancellation."""
    task = printer.hass.async_create_task(coroutine, "TinyPrint print job")
    printer.task = task
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        if asyncio.current_task().cancelling():
            task.cancel()
            await asyncio.wait({task})
            raise
        raise HomeAssistantError("Print cancelled because the integration was unloaded") from None
    finally:
        printer.task = None
