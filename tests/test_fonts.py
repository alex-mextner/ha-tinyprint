"""Bundled fonts: table, derived sizes, shipped files and subset coverage."""

import hashlib
from pathlib import Path

import pytest
from fontTools.ttLib import TTFont
from PIL import Image, ImageDraw
from shared_loader import load

fonts = load("fonts")
DEFAULT_FONT = fonts.DEFAULT_FONT
FONTS = fonts.FONTS
bundled_font_path = fonts.bundled_font_path
derived_size = fonts.derived_size
font_entry = fonts.font_entry
open_font = fonts.open_font

FONT_DIR = Path(__file__).parents[1] / "custom_components/tinyprint/frontend/fonts"


def _mask(font, text):
    image = Image.new("L", (200, 160), 0)
    ImageDraw.Draw(image).text((10, 10), text, font=font, fill=255)
    return image.tobytes()


def _ink(font, text):
    return sum(_mask(font, text))


def test_font_table_matches_the_spec_values():
    assert DEFAULT_FONT == "roboto_flex"
    assert list(FONTS) == ["roboto_flex", "tiny5"]
    roboto = FONTS["roboto_flex"]
    assert roboto["label"] == "Roboto Flex"
    assert roboto["file"] == "RobotoFlex.ttf"
    assert roboto["sizes"] == {"min": 12, "max": 96}
    assert roboto["width"] == {"kind": "axis", "min": 25, "max": 151, "default": 100}
    assert roboto["weight"] == {"kind": "axis", "min": 100, "max": 1000, "default": 400}
    assert roboto["derived"] == {
        "title": 6,
        "heading": 14,
        "footer": -4,
        "cap": 64,
        "floor": 12,
        "snap": None,
    }
    tiny = FONTS["tiny5"]
    assert tiny["label"] == "Tiny5"
    assert tiny["file"] == "Tiny5-Regular.ttf"
    assert tiny["sizes"] == {"values": [8, 16, 24, 32, 40, 48]}
    assert tiny["width"] == {"kind": "scale", "values": [100, 200], "default": 100}
    assert tiny["weight"] == {"kind": "fixed", "value": 400}
    assert tiny["derived"] == {
        "title": 6,
        "heading": 14,
        "footer": -4,
        "cap": None,
        "floor": 8,
        "snap": 8,
    }


@pytest.mark.parametrize(
    ("font", "size", "role", "expected"),
    [
        ("roboto_flex", 24, "title", 30),
        ("roboto_flex", 24, "heading", 38),
        ("roboto_flex", 24, "footer", 20),
        ("roboto_flex", 48, "title", 54),
        ("roboto_flex", 60, "heading", 64),
        ("roboto_flex", 96, "title", 96),
        ("roboto_flex", 96, "heading", 96),
        ("roboto_flex", 96, "footer", 92),
        ("roboto_flex", 12, "footer", 12),
        ("tiny5", 8, "title", 16),
        ("tiny5", 8, "heading", 24),
        ("tiny5", 8, "footer", 8),
        ("tiny5", 16, "title", 24),
        ("tiny5", 16, "footer", 16),
        ("tiny5", 24, "title", 32),
        ("tiny5", 24, "heading", 40),
        ("tiny5", 24, "footer", 24),
        ("tiny5", 32, "title", 40),
        ("tiny5", 40, "footer", 40),
        ("tiny5", 48, "title", 56),
        ("tiny5", 48, "heading", 64),
    ],
)
def test_derived_size_table(font, size, role, expected):
    assert derived_size(font, role, size) == expected


@pytest.mark.parametrize("name", ["../../etc/passwd", "Roboto Flex", "", None, 5])
def test_font_entry_is_a_closed_enum(name):
    with pytest.raises(ValueError):
        font_entry(name)


def test_font_entry_returns_the_table_row():
    assert font_entry("tiny5") is FONTS["tiny5"]
    assert bundled_font_path("tiny5") == FONT_DIR / "Tiny5-Regular.ttf"


def test_shipped_files_match_sha256sums_and_the_table():
    sums = {}
    for line in (FONT_DIR / "SHA256SUMS").read_text().splitlines():
        digest, name = line.split("  ")
        sums[name] = digest
    assert sorted(sums) == sorted(entry["file"] for entry in FONTS.values())
    for entry in FONTS.values():
        data = (FONT_DIR / entry["file"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == sums[entry["file"]]
    assert sorted(p.name for p in FONT_DIR.glob("*.ttf")) == sorted(sums)
    for licence in ("OFL-RobotoFlex.txt", "OFL-Tiny5.txt"):
        assert "SIL OPEN FONT LICENSE" in (FONT_DIR / licence).read_text()


def test_roboto_flex_exposes_only_width_and_weight_axes():
    axes = open_font("roboto_flex", 24).get_variation_axes()
    assert len(axes) == 2
    ranges = {(a["minimum"], a["maximum"]) for a in axes}
    assert ranges == {(25, 151), (100, 1000)}


@pytest.mark.parametrize(
    ("font", "samples"),
    [
        ("roboto_flex", ["Ё", "ё", "Ж", "ӿ", "№", "₽", "€", "Ł", "ž", "ÿ", "–"]),
        (
            "roboto_flex",
            ["А", "Я", "а", "я", "Є", "І", "Ї", "Ґ", "Ђ", "Ј", "Љ", "Њ", "Ћ", "Џ", "є", "ґ", "Ӂ"],
        ),
        ("tiny5", ["Ж", "ж", "Привет"]),
    ],
)
def test_fonts_cover_the_promised_scripts(font, samples):
    face = open_font(font, 24)
    unmapped = _mask(face, "\ue000")
    for sample in samples:
        mask = _mask(face, sample)
        assert any(mask), sample
        assert mask != unmapped, sample


def test_roboto_flex_fvar_order_is_wght_wdth():
    # The instancer preserves upstream fvar order; open_font passes axis values by this position.
    tags = [a.axisTag for a in TTFont(FONT_DIR / "RobotoFlex.ttf")["fvar"].axes]
    assert tags == ["wght", "wdth"]


@pytest.mark.parametrize("size", [12, 24, 48, 96])
def test_vertical_metrics_do_not_vary_with_axes(size):
    narrow = open_font("roboto_flex", size, 25, 100).getmetrics()
    wide = open_font("roboto_flex", size, 151, 1000).getmetrics()
    assert narrow == wide


def test_width_axis_is_not_confused_with_weight_axis():
    wide = open_font("roboto_flex", 24, width=151).getlength("Привет")
    narrow = open_font("roboto_flex", 24, width=25).getlength("Привет")
    assert wide > 1.15 * narrow
    lengths = [open_font("roboto_flex", 24, width=w).getlength("Привет") for w in (25, 50, 75, 100)]
    assert lengths == sorted(set(lengths))
    bold = _ink(open_font("roboto_flex", 24, weight=900), "Привет")
    thin = _ink(open_font("roboto_flex", 24, weight=200), "Привет")
    assert bold > thin
