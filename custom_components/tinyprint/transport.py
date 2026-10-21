"""Bounded X6h BLE writes with notification flow control; never replays a job."""

import asyncio
import itertools
import logging
from time import monotonic
from typing import Any

from .protocol import (
    CMD_GET_STATE,
    FAULTS,
    NOTIFY_UUID,
    WRITE_UUID,
    NotificationParser,
    decode_state,
    frame,
    get_state_frame,
    print_frames,
)

_LOGGER = logging.getLogger(__name__)


class PrinterFaultError(Exception):
    """The printer answered the job's state request with a paper, cover or heat fault."""


def transfer_timeout(height: int) -> float:
    """Allow conservative proxy round trips while bounding every job."""
    return min(600.0, max(60.0, 30.0 + 0.25 * (height + 1)))


async def probe_printer(client: Any, wait: float = 3.0) -> list[dict[str, Any]]:
    """Subscribe to every notifying characteristic, ask for state/info/battery, log what comes.

    Diagnostic only: prints nothing. Returns the received frames as hex with their
    characteristic and time, so printer behaviour can be read from the HA response and log.
    """
    received: list[dict[str, Any]] = []
    started = monotonic()
    subscribed = []

    def collect(short: str):
        def handler(_characteristic: Any, data: bytearray) -> None:
            item = {"char": short, "t": round(monotonic() - started, 3), "hex": bytes(data).hex()}
            _LOGGER.debug("PROBE RX %s", item)
            received.append(item)

        return handler

    try:
        for service in client.services:
            for characteristic in service.characteristics:
                if "notify" in characteristic.properties or "indicate" in characteristic.properties:
                    try:
                        async with asyncio.timeout(5):
                            await client.start_notify(
                                characteristic, collect(characteristic.uuid[4:8].upper())
                            )
                        subscribed.append(characteristic)
                    except Exception as error:
                        _LOGGER.debug(
                            "Probe subscribe %s failed: %s",
                            characteristic.uuid,
                            type(error).__name__,
                        )
        for command in (0xA3, 0xA8, 0xBA):
            await client.write_gatt_char(WRITE_UUID, frame(command, b"\x00"), response=False)
            await asyncio.sleep(0.3)
        await asyncio.sleep(wait)
    finally:
        for characteristic in subscribed:
            try:
                async with asyncio.timeout(5):
                    await client.stop_notify(characteristic)
            except Exception as error:
                _LOGGER.debug("Probe unsubscribe failed: %s", type(error).__name__)
    return received


async def _await_job_done(
    client: Any, state_reply: asyncio.Event, conditions: Any, poll: float
) -> None:
    """Wait for an A3 reply without `printing`; re-ask while the printer is silent or printing.

    A silent printer is not asked in a tight loop: one request per `poll` seconds.
    """
    while True:
        try:
            await asyncio.wait_for(state_reply.wait(), poll)
        except TimeoutError:
            pass
        else:
            current = conditions()
            if "printing" not in current or not FAULTS.isdisjoint(current):
                return
            await asyncio.sleep(poll)
        state_reply.clear()
        await client.write_gatt_char(WRITE_UUID, get_state_frame(), response=False)


async def send_bitmap(
    client: Any,
    bitmap: bytes,
    height: int,
    *,
    feed: int = 64,
    packet_size: int = 20,
    chunk_delay: float = 0.006,
    state_poll: float = 3.0,
    precheck_timeout: float = 3.0,
    pause_timeout: float = 15,
    total_timeout: float | None = None,
) -> dict[str, Any]:
    """Send an already rendered image on a connected client, always disconnect.

    The link is held until the printer answers the trailing A3 state request without its
    `printing` bit: the printer drops a job whose link closes while it is still printing.
    That reply says the buffer was consumed, not that paper came out.
    Connection retry, when needed, belongs before calling this function.
    """
    ready = asyncio.Event()
    ready.set()
    state_reply = asyncio.Event()
    conditions: tuple[str, ...] = ()
    parser = NotificationParser()
    stats: dict[str, Any] = {
        "rows": height,
        "bytes_written": 0,
        "frames_written": 0,
        "notifications": 0,
        "flow_pauses": 0,
        "confirmation": "transport_only",
        "printer_state": [],
        "printer_done": False,
    }
    notified = False
    frames_done = 0
    writes = 0
    started = monotonic()

    def notification(_characteristic: Any, data: bytearray) -> None:
        nonlocal conditions
        stats["notifications"] += 1
        _LOGGER.debug("RX %s +%.3fs", bytes(data).hex(), monotonic() - started)
        for command, payload in parser.packets(bytes(data)):
            if command == 0xAE and payload[:1] in (b"\x00", b"\x10"):
                if payload[0] == 0:
                    ready.set()
                else:
                    if ready.is_set():
                        stats["flow_pauses"] += 1
                    ready.clear()
            elif command == CMD_GET_STATE:
                conditions = decode_state(payload)
                stats["printer_state"] = list(conditions)
                state_reply.set()

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
            frames = await asyncio.to_thread(lambda: list(print_frames(bitmap, height, feed=feed)))
            await client.start_notify(NOTIFY_UUID, notification)
            notified = True
            # A fault now would only make the printer discard the raster: fail before it.
            await client.write_gatt_char(WRITE_UUID, get_state_frame(), response=False)
            stats["bytes_written"] += len(get_state_frame())
            try:
                await asyncio.wait_for(state_reply.wait(), precheck_timeout)
            except TimeoutError:
                _LOGGER.debug("No state reply before the job; sending anyway")
            else:
                if not FAULTS.isdisjoint(conditions):
                    fault = ",".join(sorted(FAULTS.intersection(conditions)))
                    _LOGGER.warning("Printer reported a fault before the job: %s", fault)
                    raise PrinterFaultError(fault)
            # One continuous stream, as the vendor app and the other X6h drivers send it: a
            # printer that runs dry between slow writes stops its motor mid-image, which
            # prints the following rows squeezed together.
            # The closing state request is a separate write so that its reply can only mean
            # "everything before it was consumed".
            *body, state_request = frames
            stream = b"".join(body)
            frame_ends = list(itertools.accumulate(map(len, body)))
            for offset in range(0, len(stream), packet_size):
                await asyncio.wait_for(ready.wait(), pause_timeout)
                if not client.is_connected:
                    raise ConnectionError("Printer disconnected during the job; not replayed")
                chunk = stream[offset : offset + packet_size]
                await client.write_gatt_char(WRITE_UUID, chunk, response=False)
                stats["bytes_written"] += len(chunk)
                while frames_done < len(frame_ends) and frame_ends[frames_done] <= (
                    offset + len(chunk)
                ):
                    frames_done += 1
                stats["frames_written"] = frames_done
                writes += 1
                if writes % 25 == 0:
                    _LOGGER.debug(
                        "TX %s/%s bytes in %s writes, %.2fs",
                        stats["bytes_written"],
                        len(stream),
                        writes,
                        monotonic() - started,
                    )
                await asyncio.sleep(chunk_delay)
            await asyncio.wait_for(ready.wait(), pause_timeout)
            state_reply.clear()  # only the reply to this job's own A3 counts
            await client.write_gatt_char(WRITE_UUID, state_request, response=False)
            stats["bytes_written"] += len(state_request)
            stats["frames_written"] = frames_done + 1
            await _await_job_done(client, state_reply, lambda: conditions, state_poll)
            if not FAULTS.isdisjoint(conditions):
                fault = ",".join(sorted(FAULTS.intersection(conditions)))
                _LOGGER.warning("Printer reported a fault after the job: %s", fault)
                raise PrinterFaultError(fault)
            _LOGGER.debug("Printer state after the job: %s", ",".join(conditions) or "idle")
            stats["printer_done"] = True
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
