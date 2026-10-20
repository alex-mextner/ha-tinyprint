"""X6h cat-printer wire codec. No I/O and no Home Assistant imports.

References and provenance are in docs/superpowers/specs/2026-10-14-tinyprint-design.md.
One-bit rows use least-significant-bit-first pixels, unlike Pillow's packed data.
"""

import struct
from collections.abc import Iterator

WIDTH = 384
ROW_BYTES = WIDTH // 8
MAX_ROWS = 2048
WRITE_UUID = "0000ae01-0000-1000-8000-00805f9b34fb"
NOTIFY_UUID = "0000ae02-0000-1000-8000-00805f9b34fb"
SERVICE_UUID = "0000ae30-0000-1000-8000-00805f9b34fb"
ADV_UUID = "0000af30-0000-1000-8000-00805f9b34fb"
_REVERSE = bytes(int(f"{value:08b}"[::-1], 2) for value in range(256))


def crc8(data: bytes) -> int:
    """CRC-8/SMBUS: polynomial 0x07, initial zero, no reflection/XOR."""
    value = 0
    for byte in data:
        value ^= byte
        for _ in range(8):
            value = ((value << 1) ^ (0x07 if value & 0x80 else 0)) & 0xFF
    return value


def frame(command: int, payload: bytes) -> bytes:
    """Build one host-to-printer frame."""
    if not 0 <= command <= 255 or len(payload) > 65535:
        raise ValueError("Invalid command or payload length")
    return (
        b"\x51\x78"
        + bytes((command, 0))
        + struct.pack("<H", len(payload))
        + payload
        + bytes((crc8(payload), 255))
    )


CMD_FEED = 0xA1
CMD_RAW_ROW = 0xA2
CMD_GET_STATE = 0xA3
CMD_QUALITY = 0xA4
CMD_ENERGY = 0xAF
CMD_SPEED = 0xBD
CMD_MODE = 0xBE
# Values that print on the X6h (firmware 3.0.5D) in the hardware-verified catprinter
# integration; 24000 energy and the missing quality/state commands never printed here.
QUALITY = 0x33
TEXT_ENERGY = 8000
SPEED = 10
FEED_SPEED = 25
SPEED_REFRESH_ROWS = 200
FEED_STEP = 48


def get_state_frame() -> bytes:
    """Ask for the A3 state; the printer answers once the earlier raster has been consumed."""
    return frame(CMD_GET_STATE, b"\x00")


def print_frames(bitmap: bytes, height: int, *, feed: int = 64) -> Iterator[bytes]:
    """Convert MSB-first, black=1 bitmap to bounded raw-raster print frames.

    The job ends with an A3 state request: its reply is the printer's job-consumed signal.
    """
    if not 1 <= height <= MAX_ROWS or len(bitmap) != height * ROW_BYTES:
        raise ValueError("Bitmap must contain 1..2048 rows of 384 dots")
    if not 0 <= feed <= 512:
        raise ValueError("Feed must be between 0 and 512 rows")
    speed = frame(CMD_SPEED, bytes((SPEED,)))
    yield frame(CMD_QUALITY, bytes((QUALITY,)))
    yield frame(CMD_ENERGY, struct.pack("<H", TEXT_ENERGY))
    yield frame(CMD_MODE, b"\x01")
    yield speed
    yield frame(CMD_RAW_ROW, bytes(ROW_BYTES))  # blank lead prevents first-row artifacts
    for index, offset in enumerate(range(0, len(bitmap), ROW_BYTES), start=1):
        yield frame(CMD_RAW_ROW, bitmap[offset : offset + ROW_BYTES].translate(_REVERSE))
        if index % SPEED_REFRESH_ROWS == 0:
            yield speed
    yield frame(CMD_SPEED, bytes((FEED_SPEED,)))
    while feed > 0:
        step = min(feed, FEED_STEP)
        yield frame(CMD_FEED, struct.pack("<H", step))
        feed -= step
    yield get_state_frame()


STATUS_FLAGS = (
    (0x01, "out_of_paper"),
    (0x02, "cover_open"),
    (0x04, "overheating"),
    (0x08, "low_battery"),
    (0x10, "charging"),
    (0x80, "printing"),
)
FAULTS = frozenset({"out_of_paper", "cover_open", "overheating"})


def decode_state(payload: bytes) -> tuple[str, ...]:
    """Names of the A3 status bits that are set (first payload byte)."""
    flags = payload[0] if payload else 0
    return tuple(name for bit, name in STATUS_FLAGS if flags & bit)


class NotificationParser:
    """Stream parser: accept only CRC-valid printer-to-host frames."""

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, data: bytes) -> list[bool]:
        """Flow-control events only: True = ready, False = buffer full."""
        result: list[bool] = []
        for command, payload in self.packets(data):
            if command == 0xAE and payload[:1] in (b"\x00", b"\x10"):
                result.append(payload[0] == 0)
        return result

    def packets(self, data: bytes) -> list[tuple[int, bytes]]:
        """CRC-valid printer-to-host frames as (command, payload)."""
        self._buffer.extend(data)
        result: list[tuple[int, bytes]] = []
        while len(self._buffer) >= 2:
            start = self._buffer.find(b"\x51\x78")
            if start == -1:
                self._buffer[:] = self._buffer[-1:] if self._buffer[-1] == 0x51 else b""
                break
            del self._buffer[:start]
            if len(self._buffer) < 6:
                break
            length = int.from_bytes(self._buffer[4:6], "little")
            if length > 512:  # garbage must not reserve an unbounded frame
                del self._buffer[0]
                continue
            total = length + 8
            if len(self._buffer) < total:
                break
            packet = bytes(self._buffer[:total])
            if packet[-1] != 255 or crc8(packet[6:-2]) != packet[-2]:
                del self._buffer[0]
                continue
            del self._buffer[:total]
            if packet[3] == 1:
                result.append((packet[2], packet[6:-2]))
        return result


class FrameCounter:
    """Count completed host frames from a stream of <=20-byte writes, with bounded memory.

    Garbage (wrong prefix or an impossible length) clears the buffer rather than guessing,
    so a corrupt stream under-counts instead of wedging.
    """

    def __init__(self) -> None:
        self._pending = bytearray()

    def feed(self, data: bytes) -> int:
        self._pending.extend(data)
        completed = 0
        while len(self._pending) >= 6:
            if self._pending[:2] != b"\x51\x78":
                self._pending.clear()
                break
            total = int.from_bytes(self._pending[4:6], "little") + 8
            if total > 520:
                self._pending.clear()
                break
            if len(self._pending) < total:
                break
            completed += 1
            del self._pending[:total]
        return completed
