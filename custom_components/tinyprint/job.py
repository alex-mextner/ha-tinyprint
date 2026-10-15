"""Run a cancellable print job without cancelling its HA service caller."""

import asyncio
from collections.abc import Coroutine
from typing import Any

from homeassistant.exceptions import HomeAssistantError

PRINT_CANCELLED = "Printer job was cancelled; any data already sent cannot be recalled"


async def run_owned_job(
    printer: Any, coroutine: Coroutine, cancelled_message: str = PRINT_CANCELLED
) -> dict[str, Any]:
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
        raise HomeAssistantError(cancelled_message) from None
    finally:
        printer.task = None
