"""Protocol vectors and rendering boundaries, independent of Home Assistant."""

import importlib.util
import sys
from pathlib import Path

import pytest
from PIL import Image

ROOT = Path(__file__).parents[1] / "custom_components/tinyprint"


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"{name}.py")
    obj = importlib.util.module_from_spec(spec)
    sys.modules[name] = obj
    spec.loader.exec_module(obj)
    return obj


p = module("protocol")
r = module("render")


@pytest.mark.parametrize(
    "data,expected", [(b"\x35", 0x8B), (b"\x10", 0x70), (b"\x40\x01", 0x5C), (b"", 0)]
)
def test_captured_crc(data, expected):
    assert p.crc8(data) == expected


def test_captured_frame():
    assert p.frame(0xA4, b"\x35").hex() == "5178a4000100358bff"


def test_bitmap_bit_order_blank_lead_and_last_row():
    bitmap = b"\x80" + bytes(47) + bytes(47) + b"\x01"
    frames = list(p.print_frames(bitmap, 2, feed=64))
    rows = [f for f in frames if f[2] == 0xA2]
    assert len(rows) == 3
    assert rows[0][6:54] == bytes(48)
    assert rows[1][6] == 1
    assert rows[-1][53] == 128
    assert frames[-1] == p.frame(0xA1, b"\x40\x00")


@pytest.mark.parametrize(
    "bitmap,height,feed",
    [
        (b"", 0, 64),
        (b"1", 1, 64),
        (bytes(48), 1, -1),
        (bytes(48), 1, 10000),
        (bytes(48 * 2049), 2049, 64),
    ],
)
def test_protocol_limits(bitmap, height, feed):
    with pytest.raises(ValueError):
        list(p.print_frames(bitmap, height, feed=feed))


def test_default_setup_within_documented_values():
    frames = list(p.print_frames(bytes(48), 1, feed=0))
    assert frames[:3] == [
        p.frame(0xBD, b"\x10"),
        p.frame(0xAF, (24000).to_bytes(2, "little")),
        p.frame(0xBE, b"\x01"),
    ]
    assert frames[-1][2] == 0xA2


def test_notification_crc_fragmentation_and_noise():
    parser = p.NotificationParser()
    msg = bytes.fromhex("5178ae0101001070ff")
    assert parser.feed(b"noise" + msg[:4]) == []
    assert parser.feed(msg[4:] + bytes.fromhex("5178ae0101000000ff")) == [False, True]
    assert parser.feed(bytes.fromhex("5178ae0101001000ff")) == []


def test_notification_bogus_length_does_not_wedge():
    parser = p.NotificationParser()
    assert parser.feed(b"\x51\x78\xae\x01\xff\xff" + bytes.fromhex("5178ae0101000000ff")) == [True]


def test_unicode_text_and_long_word_wrap():
    image = r.render_text("Привет, Алекс!\n" + "Ш" * 80, 24)
    assert image.size[0] == 384
    assert 40 < image.height < 2048
    assert image.getextrema()[0] == 0
    bitmap, height = r.to_bitmap(image)
    assert len(bitmap) == height * 48
    assert any(bitmap)


def test_text_preserves_explicit_empty_lines():
    assert r.render_text("A\n\nB", 24).height > r.render_text("A\nB", 24).height


@pytest.mark.parametrize("text", ["", "  ", "a" * 3001, "\n" * 2000])
def test_text_rejects_empty_or_huge(text):
    with pytest.raises(ValueError):
        r.render_text(text, 24)


def test_qr_is_binary_with_quiet_zone():
    image = r.render_qr("https://example.com/spool/123")
    assert image.width == 384
    assert all(count == 0 for index, count in enumerate(image.histogram()) if index not in (0, 255))
    assert all(image.getpixel((x, 0)) == 255 for x in range(384))


@pytest.mark.parametrize("text", ["", "a" * 501])
def test_qr_limits(text):
    with pytest.raises(ValueError):
        r.render_qr(text)


def test_image_alpha_flattens_on_white(tmp_path):
    path = tmp_path / "alpha.png"
    Image.new("RGBA", (20, 20), (0, 0, 0, 0)).save(path)
    image = r.render_image(str(path))
    assert image.getextrema() == (255, 255)


def test_image_height_limit(tmp_path):
    path = tmp_path / "tall.png"
    Image.new("L", (1, 2049), 255).save(path)
    with pytest.raises(ValueError):
        r.render_image(str(path))


def test_output_bitmap_black_is_one():
    image = Image.new("L", (384, 1), 255)
    image.putpixel((0, 0), 0)
    bitmap, height = r.to_bitmap(image)
    assert height == 1 and bitmap == b"\x80" + bytes(47)


def test_non_image_is_validation_error(tmp_path):
    path = tmp_path / "not-image.png"
    path.write_text("not an image")
    with pytest.raises(ValueError, match="readable image"):
        r.render_image(str(path))


@pytest.mark.parametrize("failure", [SyntaxError("invalid PNG"), EOFError("truncated frame")])
def test_corrupt_image_decoding_is_validation_error(tmp_path, monkeypatch, failure):
    path = tmp_path / "bad.png"
    path.write_bytes(b"not decoded yet")

    def broken_open(*args, **kwargs):
        raise failure

    monkeypatch.setattr(r.Image, "open", broken_open)
    with pytest.raises(ValueError, match="readable image"):
        r.render_image(str(path))
