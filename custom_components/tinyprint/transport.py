"""Bounded X6h BLE writes with notification flow control; never replays a job."""

import asyncio
import logging
from time import monotonic
from typing import Any

from .protocol import NOTIFY_UUID, WRITE_UUID, NotificationParser, print_frames

_LOGGER = logging.getLogger(__name__)


def transfer_timeout(height: int) -> float:
    """Allow conservative proxy round trips while bounding every job."""
    return min(600.0, max(60.0, 30.0 + 0.25 * (height + 1)))


async def send_bitmap(
    client: Any,
    bitmap: bytes,
    height: int,
    *,
    feed: int = 64,
    chunk_delay: float = 0.006,
    row_delay: float = 0.012,
    settle_delay: float = 1.0,
    pause_timeout: float = 15,
    total_timeout: float | None = None,
) -> dict[str, Any]:
    """Send an already rendered image on a connected client, always disconnect.

    Return values describe the transport only. X6h has no known print-completed ACK.
    Connection retry, when needed, belongs before calling this function.
    """
    ready = asyncio.Event()
    ready.set()
    parser = NotificationParser()
    stats: dict[str, Any] = {
        "rows": height,
        "bytes_written": 0,
        "frames_written": 0,
        "notifications": 0,
        "flow_pauses": 0,
        "confirmation": "transport_only",
    }
    notified = False
    started = monotonic()

    def notification(_characteristic: Any, data: bytearray) -> None:
        stats["notifications"] += 1
        for can_send in parser.feed(bytes(data)):
            if can_send:
                ready.set()
            else:
                if ready.is_set():
                    stats["flow_pauses"] += 1
                ready.clear()

    try:
        async with asyncio.timeout(
            transfer_timeout(height) if total_timeout is None else total_timeout
        ):
            write = client.services.get_characteristic(WRITE_UUID)
            notify = client.services.get_characteristic(NOTIFY_UUID)
            if (
                write is None
                or notify is None
                or "write-without-response" not in write.properties
                or "notify" not in notify.properties
            ):
                raise ValueError("Unsupported GATT profile: AE01 write and AE02 notify required")
            # Validate geometry before any write (generator is evaluated on iteration).
            frames = list(print_frames(bitmap, height, feed=feed))
            await client.start_notify(NOTIFY_UUID, notification)
            notified = True
            for packet in frames:
                await asyncio.wait_for(ready.wait(), pause_timeout)
                if not client.is_connected:
                    raise ConnectionError("Printer disconnected during the job; not replayed")
                # 20-byte chunks also work on an ATT MTU of 23 and BLE proxies.
                # Complete the frame before honoring the next pause notification.
                for offset in range(0, len(packet), 20):
                    chunk = packet[offset : offset + 20]
                    await client.write_gatt_char(WRITE_UUID, chunk, response=False)
                    stats["bytes_written"] += len(chunk)
                    await asyncio.sleep(chunk_delay)
                stats["frames_written"] += 1
                if packet[2] == 0xA2:
                    await asyncio.sleep(row_delay)
            await asyncio.wait_for(ready.wait(), pause_timeout)
            await asyncio.sleep(settle_delay)
            stats["duration_seconds"] = round(monotonic() - started, 3)
            return stats
    finally:
        # A failed/cancelled job must never monopolize the Bluetooth connection.
        try:
            async with asyncio.timeout(5):
                if notified and client.is_connected:
                    await client.stop_notify(NOTIFY_UUID)
        except Exception as error:
            _LOGGER.debug("Notification cleanup failed: %s", type(error).__name__)
        try:
            async with asyncio.timeout(5):
                await client.disconnect()
        except Exception as error:
            _LOGGER.debug("Disconnect cleanup failed: %s", type(error).__name__)
