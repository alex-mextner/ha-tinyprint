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


def print_frames(bitmap: bytes, height: int, *, feed: int = 64) -> Iterator[bytes]:
    """Convert MSB-first, black=1 bitmap to bounded raw-raster print frames."""
    if not 1 <= height <= MAX_ROWS or len(bitmap) != height * ROW_BYTES:
        raise ValueError("Bitmap must contain 1..2048 rows of 384 dots")
    if not 0 <= feed <= 512:
        raise ValueError("Feed must be between 0 and 512 rows")
    yield frame(0xBD, b"\x10")  # conservative feed divisor 16
    yield frame(0xAF, struct.pack("<H", 24000))
    yield frame(0xBE, b"\x01")
    yield frame(0xA2, bytes(ROW_BYTES))  # blank lead prevents first-row artifacts
    for offset in range(0, len(bitmap), ROW_BYTES):
        yield frame(0xA2, bitmap[offset : offset + ROW_BYTES].translate(_REVERSE))
    if feed:
        yield frame(0xA1, struct.pack("<H", feed))


class NotificationParser:
    """Stream parser: accept only CRC-valid ready/buffer-full notifications."""

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, data: bytes) -> list[bool]:
        self._buffer.extend(data)
        result: list[bool] = []
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
            if packet[2:6] == b"\xae\x01\x01\x00" and packet[6] in (0, 16):
                result.append(packet[6] == 0)
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
