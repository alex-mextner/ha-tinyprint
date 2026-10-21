"""Direct Linux LE/ATT jobs on the Home Assistant host; no HTTP or second host."""

import asyncio
import logging
import re
import sys
from typing import Any

from . import att
from .le_link import L2capLeLink
from .protocol import DEFAULT_TAIL_ROWS
from .transport import send_bitmap

_LOGGER = logging.getLogger(__name__)
CLOSE_TIMEOUT = 5.0


def normalize_adapter_mac(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("Choose a local Bluetooth adapter MAC for local LE")
    value = value.strip().upper()
    if not re.fullmatch(r"(?:[0-9A-F]{2}:){5}[0-9A-F]{2}", value) or value == "00:00:00:00:00:00":
        raise ValueError("Local LE requires a valid nonzero Bluetooth adapter MAC")
    return value


def _retrieve(task: asyncio.Task) -> None:
    if not task.cancelled():
        task.exception()


async def _close(client: Any, channel: Any) -> None:
    # Closing first synchronously aborts both a pending kernel connect and a stuck reader.
    # Never leave the radio owned while waiting for cancellable Python cleanup.
    if channel is not None:
        channel.close()
    if client is None:
        return
    cleanup = asyncio.create_task(client.aclose())
    cleanup.add_done_callback(_retrieve)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + CLOSE_TIMEOUT
    cancelled = None
    while not cleanup.done():
        remaining = deadline - loop.time()
        if remaining <= 0:
            cleanup.cancel()
            _LOGGER.debug("Local LE reader cleanup reached its deadline")
            break
        try:
            await asyncio.wait({cleanup}, timeout=remaining)
        except asyncio.CancelledError as error:
            cancelled = cancelled or error
    if cancelled is not None:
        raise cancelled


async def run_local_job(
    printer: Any,
    kind: str,
    bitmap: bytes | None = None,
    rows: int | None = None,
    *,
    link: Any = None,
    request_timeout: float = 10.0,
    pacing: dict[str, float] | None = None,
    tail_rows: int = DEFAULT_TAIL_ROWS,
) -> dict[str, Any]:
    """Run under the existing printer lock. Every resource is released before return.

    Injection points are Python-only test seams, not service or printable-document inputs.
    No scanner, HTTP client, pairing or BlueZ Device1.Connect is used here.
    """
    adapter = normalize_adapter_mac(printer.adapter_mac)
    address = normalize_adapter_mac(printer.address)
    if kind not in ("print", "diagnose"):
        raise ValueError("Unsupported local LE operation")
    if kind == "print" and (
        type(rows) is not int
        or not 1 <= rows <= 2048
        or not isinstance(bitmap, bytes)
        or len(bitmap) != rows * 48
    ):
        raise ValueError("Local LE bitmap must contain exactly 48 bytes per row")
    if link is None:
        if sys.platform != "linux":
            raise ValueError("Direct local LE requires Linux; use native BLE or a proxy here")
        link = L2capLeLink(adapter, address)
    result = printer.last_result
    result.update(
        {
            "bytes_written": 0,
            "frames_written": 0,
            "writes": 0,
            "notifications": 0,
            "flow_pauses": 0,
            "delivery_uncertain": False,
            "confirmation": "none",
            "physical_print_confirmed": False,
        }
    )
    channel = client = gatt = None
    try:
        printer.phase("connecting_le", "connecting")
        async with asyncio.timeout(35):
            channel = await link.connect()
        client = att.AttClient(channel, request_timeout=request_timeout, send_timeout=5)
        await client.start()
        printer.phase("discovering_gatt", "connecting")
        if kind == "diagnose":
            async with asyncio.timeout(30):
                survey = await att.discover_all(client)
            result.update(
                {
                    "services": survey["services"],
                    "supported_profile": survey["profile_supported"],
                    "profile_problem": survey["profile_problem"],
                    "confirmation": "connection_only",
                }
            )
        else:
            async with asyncio.timeout(30):
                profile = await att.discover_profile(client)
            gatt = att.AttGattClient(client, profile)
            printer.phase("transmitting", "printing")
            stats = await send_bitmap(gatt, bitmap, rows, tail_rows=tail_rows, **(pacing or {}))
            result.update(stats)
    finally:
        if gatt is not None:
            result.update(gatt.counters)
            # Failure after any completed write can leave a partial label on paper.
            if sys.exc_info()[0] is not None and result["bytes_written"]:
                result["delivery_uncertain"] = True
        try:
            await _close(client, channel)
        except BaseException:
            if result["bytes_written"]:
                result["delivery_uncertain"] = True
            result["confirmation"] = "none"
            raise
    return dict(result)
