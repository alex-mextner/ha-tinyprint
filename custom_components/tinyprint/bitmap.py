"""Print-bitmap validation for every caller that accepts a base64 bitmap (bridge wire contract first).

Pure: stdlib plus `.protocol` only, so the bridge process can load it without Home Assistant
(through a synthetic package, see bridge/tinyprint_bridge/_shared.py).

Invariants: errors never echo the input (it may carry a print bitmap); the encoded size is bounded
before any decoding; base64 is decoded strictly (`validate=True`), so urlsafe characters, stray
whitespace and misplaced padding are rejected instead of silently skipped.
"""

import base64
import binascii

from .protocol import MAX_ROWS, ROW_BYTES

MAX_BITMAP_BYTES = MAX_ROWS * ROW_BYTES
MAX_BITMAP_B64_LENGTH = 4 * ((MAX_BITMAP_BYTES + 2) // 3)


class BitmapError(ValueError):
    """A bitmap or its row count broke the contract. Messages are generic by design."""


def check_rows(value: object) -> int:
    # bool is an int subclass; True must not become one row.
    if type(value) is not int or not 1 <= value <= MAX_ROWS:
        raise BitmapError(f"rows must be an integer from 1 to {MAX_ROWS}")
    return value


def decode_bitmap_b64(rows: int, encoded: object) -> bytes:
    """Validate `rows`, then strictly decode `encoded` into exactly `rows * ROW_BYTES` bytes."""
    check_rows(rows)
    if not isinstance(encoded, str) or len(encoded) > MAX_BITMAP_B64_LENGTH:
        raise BitmapError("bitmap_b64 must be a base64 string within the size limit")
    try:
        bitmap = base64.b64decode(encoded, validate=True)
    except binascii.Error, ValueError:
        raise BitmapError("bitmap_b64 is not valid base64") from None
    if len(bitmap) != rows * ROW_BYTES:
        raise BitmapError("bitmap size does not match rows")
    return bitmap
