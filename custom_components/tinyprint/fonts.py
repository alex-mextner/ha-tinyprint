"""Bundled fonts: per-font table served to the panel, derived sizes and Pillow loading."""

import math
from pathlib import Path

from PIL import ImageFont

DEFAULT_FONT = "roboto_flex"

FONT_DIR = Path(__file__).parent / "frontend" / "fonts"

FONTS: dict[str, dict] = {
    "roboto_flex": {
        "label": "Roboto Flex",
        "file": "RobotoFlex.ttf",
        "sizes": {"min": 12, "max": 96},
        "width": {"kind": "axis", "min": 25, "max": 151, "default": 100},
        "weight": {"kind": "axis", "min": 100, "max": 1000, "default": 400},
        "derived": {
            "title": 6,
            "heading": 14,
            "footer": -4,
            "cap": 64,
            "floor": 12,
            "snap": None,
        },
    },
    "tiny5": {
        "label": "Tiny5",
        "file": "Tiny5-Regular.ttf",
        "sizes": {"values": [8, 16, 24, 32, 40, 48]},
        "width": {"kind": "scale", "values": [100, 200], "default": 100},
        "weight": {"kind": "fixed", "value": 400},
        "derived": {
            "title": 6,
            "heading": 14,
            "footer": -4,
            "cap": None,
            "floor": 8,
            "snap": 8,
        },
    },
}

_CAPPED_ROLES = ("title", "heading")
_ROLES = (*_CAPPED_ROLES, "footer")


def font_entry(name: object) -> dict:
    if not isinstance(name, str) or name not in FONTS:
        raise ValueError(f"font must be one of {', '.join(FONTS)}")
    return FONTS[name]


def bundled_font_path(name: str) -> Path:
    return FONT_DIR / font_entry(name)["file"]


def derived_size(name: str, role: str, size: int) -> int:
    rule = font_entry(name)["derived"]
    if role not in _ROLES:
        raise ValueError(f"role must be one of {', '.join(_ROLES)}")
    value = size + rule[role]
    if rule["snap"]:
        value = math.floor(value / rule["snap"] + 0.5) * rule["snap"]
    value = max(rule["floor"], value)
    if rule["cap"] and role in _CAPPED_ROLES:
        value = min(max(rule["cap"], size), value)
    return value


def open_font(name: str, size: int, width: int = 100, weight: int = 400) -> ImageFont.FreeTypeFont:
    font = ImageFont.truetype(str(bundled_font_path(name)), size)
    if name == "roboto_flex":
        # By fvar position (the subsetter prunes names); the instanced font keeps [wght, wdth].
        font.set_variation_by_axes([weight, width])
    return font
