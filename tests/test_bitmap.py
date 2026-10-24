"""Shared bitmap validation used by the bridge wire contract and the print_bitmap service."""

import base64

import pytest
from shared_loader import load

b = load("bitmap")


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


@pytest.mark.parametrize("value", [True, False, 0, -1, 2049, "5", 5.0, None])
def test_check_rows_rejects_bool_zero_overflow_and_non_int(value):
    with pytest.raises(b.BitmapError):
        b.check_rows(value)


@pytest.mark.parametrize("value", [1, 2048])
def test_check_rows_accepts_bounds(value):
    assert b.check_rows(value) == value


@pytest.mark.parametrize("rows", [True, "1"])
def test_decode_validates_rows_first(rows):
    # True * 48 == 48 would pass the length check, so rows must be rejected before it.
    with pytest.raises(b.BitmapError):
        b.decode_bitmap_b64(rows, b64(bytes(48)))


def test_decode_returns_exact_bytes():
    data = bytes(range(96))
    assert b.decode_bitmap_b64(2, b64(data)) == data


@pytest.mark.parametrize("encoded", [b"AAAA", None, 5])
def test_decode_rejects_non_string(encoded):
    with pytest.raises(b.BitmapError):
        b.decode_bitmap_b64(1, encoded)


_OK = b64(bytes(48))
assert len(_OK) == 64

# The lenient default b64decode drops characters outside the alphabet instead of failing, which is
# how these inputs would slip through without validate=True.
STRICT_ONLY_CASES = {
    "urlsafe_dash": _OK[:10] + "-" + _OK[11:],
    "urlsafe_underscore": _OK[:10] + "_" + _OK[11:],
    "foreign_character": _OK[:10] + "@" + _OK[11:],
    "embedded_newline": _OK[:10] + "\n" + _OK[10:],
    "misplaced_padding": _OK[:10] + "=" + _OK[11:],
}


@pytest.mark.parametrize("encoded", STRICT_ONLY_CASES.values(), ids=STRICT_ONLY_CASES.keys())
def test_decode_rejects_what_only_strict_validation_catches(encoded):
    with pytest.raises(b.BitmapError):
        b.decode_bitmap_b64(1, encoded)


def test_encoded_length_is_bounded_before_decoding():
    with pytest.raises(b.BitmapError, match="size limit"):
        b.decode_bitmap_b64(1, "!" * (b.MAX_BITMAP_B64_LENGTH + 1))


@pytest.mark.parametrize("size", [47 * 2, 49 * 2])
def test_decode_rejects_length_mismatch(size):
    with pytest.raises(b.BitmapError):
        b.decode_bitmap_b64(2, b64(bytes(size)))


@pytest.mark.parametrize(
    "call",
    [
        lambda: b.check_rows("SECRETMARKER"),
        lambda: b.decode_bitmap_b64(1, "SECRETMARKER!"),
        lambda: b.decode_bitmap_b64(1, b64(b"SECRETMARKER")),
        lambda: b.decode_bitmap_b64("SECRETMARKER", b64(bytes(48))),
    ],
)
def test_errors_do_not_echo_input(call):
    with pytest.raises(b.BitmapError) as caught:
        call()
    assert "SECRETMARKER" not in str(caught.value)


def test_constants():
    assert b.MAX_BITMAP_B64_LENGTH == 131072
    assert b.MAX_BITMAP_BYTES == 2048 * 48
    assert issubclass(b.BitmapError, ValueError)
