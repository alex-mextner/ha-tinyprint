"""Protocol vectors and rendering boundaries, independent of Home Assistant."""

from pathlib import Path

import pytest
from PIL import Image
from shared_loader import load

p = load("protocol")
r = load("render")


@pytest.mark.parametrize(
    "data,expected", [(b"\x35", 0x8B), (b"\x10", 0x70), (b"\x40\x01", 0x5C), (b"", 0)]
)
def test_captured_crc(data, expected):
    assert p.crc8(data) == expected


def test_captured_frame():
    assert p.frame(0xA4, b"\x35").hex() == "5178a4000100358bff"


def test_bitmap_bit_order_blank_lead_and_last_row():
    bitmap = b"\x80" + bytes(47) + bytes(47) + b"\x01"
    frames = list(p.print_frames(bitmap, 2, feed=64, tail_rows=0))
    rows = [f for f in frames if f[2] == 0xA2]
    assert len(rows) == 3
    assert rows[0][6:54] == bytes(48)
    assert rows[1][6] == 1
    assert rows[-1][53] == 128
    assert frames[-2] == p.frame(0xA1, b"\x10\x00")
    assert frames[-3] == p.frame(0xA1, b"\x30\x00")
    assert frames[-1] == p.get_state_frame()


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


def _raster_rows(frames):
    return [f[6:54] for f in frames if f[2] == 0xA2]


def test_default_tail_follows_last_ink_row_before_feed_and_state_request():
    bitmap = b"\x80" + bytes(47) + bytes(47) + b"\x01"
    frames = list(p.print_frames(bitmap, 2))
    rows = _raster_rows(frames)
    assert p.DEFAULT_TAIL_ROWS == 96
    assert len(rows) == 1 + 2 + p.DEFAULT_TAIL_ROWS  # blank lead, image, tail
    assert rows[2][47] == 128 and all(row == bytes(48) for row in rows[3:])
    last_row = max(i for i, f in enumerate(frames) if f[2] == 0xA2)
    assert [f[2] for f in frames[last_row + 1 :]] == [0xBD, 0xA1, 0xA1, 0xA3]


def test_tail_zero_disables_and_custom_length_is_exact():
    bitmap = b"\xff" * 48
    assert len(_raster_rows(p.print_frames(bitmap, 1, tail_rows=0))) == 2
    assert len(_raster_rows(p.print_frames(bitmap, 1, tail_rows=7))) == 2 + 7
    assert len(_raster_rows(p.print_frames(bitmap, 1, tail_rows=512))) == 2 + 512


def test_existing_blank_bottom_margin_counts_toward_tail_without_double_padding():
    bitmap = b"\xff" * 48 + bytes(48 * 40)
    assert len(_raster_rows(p.print_frames(bitmap, 41, tail_rows=96))) == 1 + 41 + 56
    # A margin at least as long as the tail adds nothing.
    assert len(_raster_rows(p.print_frames(bitmap, 41, tail_rows=40))) == 1 + 41
    assert len(_raster_rows(p.print_frames(bitmap, 41, tail_rows=10))) == 1 + 41


def test_tail_is_not_added_twice_by_reusing_the_output_bitmap():
    # A bitmap that already carries a full tail stays untouched.
    bitmap = b"\xff" * 48 + bytes(48 * p.DEFAULT_TAIL_ROWS)
    rows = _raster_rows(p.print_frames(bitmap, 1 + p.DEFAULT_TAIL_ROWS))
    assert len(rows) == 1 + 1 + p.DEFAULT_TAIL_ROWS


@pytest.mark.parametrize("tail", [-1, 513, 1.5])
def test_tail_limits(tail):
    with pytest.raises((ValueError, TypeError)):
        list(p.print_frames(bytes(48), 1, tail_rows=tail))


@pytest.mark.parametrize("value", [0, 96, 512, 96.0])
def test_normalize_tail_rows_accepts_whole_rows(value):
    assert p.normalize_tail_rows(value) == int(value)


@pytest.mark.parametrize("value", [-1, 513, 1.5, "96", None, True])
def test_normalize_tail_rows_rejects_the_rest(value):
    with pytest.raises(ValueError):
        p.normalize_tail_rows(value)


def test_default_setup_within_documented_values():
    frames = list(p.print_frames(bytes(48), 1, feed=0))
    # Sequence and values of the X6h job that prints on hardware: quality, energy 8000,
    # text mode, speed 10, rows, feed speed 25, then the state request.
    assert frames[:4] == [
        p.frame(0xA4, b"\x33"),
        p.frame(0xAF, (8000).to_bytes(2, "little")),
        p.frame(0xBE, b"\x01"),
        p.frame(0xBD, b"\x0a"),
    ]
    assert frames[-2:] == [p.frame(0xBD, b"\x19"), p.get_state_frame()]
    assert p.get_state_frame().hex() == "5178a30001000000ff"


def test_speed_is_resent_every_200_rows_and_feed_is_split_in_48_dot_steps():
    frames = list(p.print_frames(bytes(48 * 400), 400, feed=100))
    assert [f for f in frames if f[2] == 0xBD] == [p.frame(0xBD, b"\x0a")] * 3 + [
        p.frame(0xBD, b"\x19")
    ]
    feeds = [int.from_bytes(f[6:8], "little") for f in frames if f[2] == 0xA1]
    assert feeds == [48, 48, 4]


def test_state_reply_decoding_and_frame_parsing():
    parser = p.NotificationParser()
    payload = bytes([0x80, 0, 0x27])
    reply = b"\x51\x78\xa3\x01\x03\x00" + payload + bytes([p.crc8(payload), 0xFF])
    assert parser.packets(reply) == [(0xA3, payload)]
    assert p.decode_state(bytes([0x85])) == ("out_of_paper", "overheating", "printing")
    assert p.decode_state(b"") == ()


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


def test_render_text_never_opens_a_system_font(recorded_font_paths):
    bundled = Path(r.__file__).parent / "frontend" / "fonts"
    r.render_text("Привет, мир", 24)
    assert recorded_font_paths
    assert all(Path(path).parent == bundled for path in recorded_font_paths)
    recorded_font_paths.clear()
    explicit = str(bundled / "Tiny5-Regular.ttf")
    r.render_text("Привет, мир", 24, font_path=explicit)
    assert recorded_font_paths == [explicit]


def test_render_text_size_range_is_still_12_to_64():
    for size in (11, 65):
        with pytest.raises(ValueError):
            r.render_text("x", size)
    for size in (12, 64):
        assert r.render_text("x", size).width == r.WIDTH
