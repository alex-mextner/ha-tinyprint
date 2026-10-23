"""One bounded raster pipeline for the sidebar, HA actions and physical printing.

This module deliberately has no Home Assistant, Bluetooth or network dependencies.
Call it in an executor. Preview is the final one-bit raster, not a browser approximation.
"""

import base64
import binascii
import hashlib
import io
import math
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

import qrcode
from PIL import Image, ImageDraw, ImageEnhance, ImageFont, ImageOps

from .fonts import DEFAULT_FONT, derived_size, font_entry, open_font

WIDTH = 384
MAX_ROWS = 2048
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_SOURCE_PIXELS = 4_000_000
MAX_BASE64 = 4 * ((MAX_FILE_BYTES + 2) // 3)
TEMPLATES = {
    "note": {
        "name": "Заметка",
        "fields": ["title", "text", "footer"],
        "required": ["text"],
        "example": {
            "title": "Не забыть",
            "text": "Забрать посылку\nКупить корм",
            "footer": "Сегодня",
        },
    },
    "heading": {
        "name": "Крупная надпись",
        "fields": ["title", "text", "footer"],
        "required": ["title"],
        "example": {"title": "МАСТЕРСКАЯ", "text": "Инструменты — на место"},
    },
    "checklist": {
        "name": "Чек-лист",
        "fields": ["title", "text", "footer"],
        "required": ["text"],
        "example": {
            "title": "Перед печатью",
            "text": "Высушить пластик\nОчистить стол\nПроверить первый слой",
        },
    },
    "qr_label": {
        "name": "QR с подписью",
        "fields": ["title", "qr", "footer"],
        "required": ["qr"],
        "example": {
            "title": "Открыть инструкцию",
            "qr": "https://example.com",
            "footer": "Наведите камеру",
        },
    },
    "storage_label": {
        "name": "Ячейка хранения",
        "fields": ["title", "location", "text", "qr"],
        "required": ["title"],
        "example": {
            "title": "МЕТИЗЫ M4",
            "location": "Стеллаж А · ячейка 03",
            "text": "Винты и гайки",
            "qr": "https://example.com/box/03",
        },
    },
    "spool_label": {
        "name": "Катушка пластика",
        "fields": ["title", "material", "color", "weight", "nozzle", "qr"],
        "required": ["title"],
        "example": {
            "title": "Катушка 042",
            "material": "PETG",
            "color": "Чёрный",
            "weight": "750 г",
            "nozzle": "240 °C",
            "qr": "https://example.com/spool/42",
        },
    },
}
PRESETS = {
    "photo": {
        "name": "Фото",
        "method": "floyd_steinberg",
        "contrast": 1.25,
        "brightness": 1.10,
        "sharpness": 1.2,
    },
    "line_art": {
        "name": "Текст / чертёж",
        "method": "threshold",
        "contrast": 1.6,
        "brightness": 1.0,
        "sharpness": 1.5,
    },
    "logo": {
        "name": "Логотип",
        "method": "threshold",
        "contrast": 1.3,
        "brightness": 1.0,
        "sharpness": 1.0,
    },
    "soft": {
        "name": "Мягкие полутона",
        "method": "ordered",
        "contrast": 1.0,
        "brightness": 1.15,
        "sharpness": 1.0,
    },
}
DEFAULTS = {
    "preset": "photo",
    "method": "floyd_steinberg",
    "threshold": 128,
    "brightness": 1.1,
    "contrast": 1.25,
    "sharpness": 1.2,
    "rotate": 0,
    "invert": False,
    "width": 384,
    "fit": "width",
    "font": DEFAULT_FONT,
    "font_size": 24,
    "font_width": 100,
    "font_weight": 400,
    "columns": 1,
    "align": "left",
    "border": False,
    "qr_size": 320,
    "qr_correction": "M",
}
_INVERT = bytes(255 - x for x in range(256))
_BAYER = ((0, 8, 2, 10), (12, 4, 14, 6), (3, 11, 1, 9), (15, 7, 13, 5))


def normalize_options(raw: Any = None) -> dict[str, Any]:
    """Strictly validate user controls before resource allocation or connection."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict) or raw.keys() - DEFAULTS.keys():
        raise ValueError("Unknown rendering option")
    preset = raw.get("preset", "photo")
    if not isinstance(preset, str) or preset not in PRESETS:
        raise ValueError("Unknown image preset")
    out = DEFAULTS | {k: v for k, v in PRESETS[preset].items() if k != "name"} | raw
    ranges = {
        "width": (96, 384),
        "threshold": (1, 254),
        "qr_size": (96, 360),
        "brightness": (0.25, 2.0),
        "contrast": (0.25, 3.0),
        "sharpness": (0, 3.0),
    }
    for key, (low, high) in ranges.items():
        value = out[key]
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or not low <= value <= high
        ):
            raise ValueError(f"{key} must be between {low} and {high}")
        if key in {"width", "threshold", "qr_size"} and not isinstance(value, int):
            raise ValueError(f"{key} must be an integer")
    _check_font_options(out)
    choices = {
        "method": ("threshold", "floyd_steinberg", "ordered"),
        "rotate": (0, 90, 180, 270),
        "align": ("left", "center", "right"),
        "fit": ("width", "original"),
        "qr_correction": ("M", "Q", "H"),
    }
    for key, values in choices.items():
        if out[key] not in values or isinstance(out[key], bool):
            raise ValueError(f"Invalid {key}")
    for key in ("invert", "border"):
        if type(out[key]) is not bool:
            raise ValueError(f"{key} must be a boolean")
    return out


def _is_int(value: Any) -> bool:
    return type(value) is int


def _check_font_options(out: dict[str, Any]) -> None:
    """Validate font, size, axes and columns against the chosen font's own limits."""
    entry = font_entry(out["font"])
    size, width, weight = out["font_size"], out["font_width"], out["font_weight"]
    sizes = entry["sizes"]
    if "values" in sizes:
        if not _is_int(size) or size not in sizes["values"]:
            raise ValueError(f"font_size must be one of {', '.join(map(str, sizes['values']))}")
    elif not _is_int(size) or not sizes["min"] <= size <= sizes["max"]:
        raise ValueError(f"font_size must be between {sizes['min']} and {sizes['max']}")
    spec = entry["width"]
    if spec["kind"] == "axis":
        if not _is_int(width) or not spec["min"] <= width <= spec["max"]:
            raise ValueError(f"font_width must be between {spec['min']} and {spec['max']}")
    elif not _is_int(width) or width not in spec["values"]:
        raise ValueError(f"font_width must be one of {', '.join(map(str, spec['values']))}")
    elif width != spec["default"]:
        raise ValueError(f"font_width {width} is available only in the Studio editor")
    spec = entry["weight"]
    if spec["kind"] == "axis":
        if not _is_int(weight) or not spec["min"] <= weight <= spec["max"]:
            raise ValueError(f"font_weight must be between {spec['min']} and {spec['max']}")
    elif weight != spec["value"] or not _is_int(weight):
        raise ValueError(f"font_weight must be {spec['value']} for this font")
    columns = out["columns"]
    if not _is_int(columns) or columns not in (1, 2, 3):
        raise ValueError("columns must be 1, 2 or 3")
    if columns != 1:
        raise ValueError("columns 2 and 3 are available only in the Studio editor")


def _text(value: Any, limit: int = 3000, required: bool = False) -> str:
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        raise ValueError(
            f"Text must contain at most {limit} characters and required fields cannot be blank"
        )
    return value


def _font(size: int, options: dict[str, Any], path: str | None) -> ImageFont.FreeTypeFont:
    # A configured font file is opened as-is: font/width/weight options do not apply to it.
    if path:
        try:
            return ImageFont.truetype(path, size)
        except OSError as err:
            raise ValueError(f"Cannot open the configured font file {path}") from err
    return open_font(options["font"], size, options["font_width"], options["font_weight"])


@dataclass(frozen=True)
class _TextPart:
    """A laid-out text band. `lines` carries Pillow's own fractional start x per line (R4)."""

    block: dict[str, Any]
    font: ImageFont.FreeTypeFont
    lines: tuple[tuple[str, bool, float], ...]
    anchors: tuple[tuple[str, int, int, int, int], ...] = ()


@dataclass(frozen=True)
class _QrPart:
    block: dict[str, Any]
    image: Image.Image


def _half_up(value: float) -> int:
    # Same rounding as JavaScript's Math.round, so the browser port agrees on .5.
    return math.floor(value + 0.5)


def _wrap(
    font: ImageFont.FreeTypeFont, text: str, max_width: int, step: int, checklist: bool
) -> list[tuple[str, bool]]:
    lines: list[tuple[str, bool]] = []
    for paragraph in text.expandtabs(4).split("\n"):
        pending = paragraph.rstrip("\r")
        first = True
        while pending:
            low, high = 1, min(len(pending), 512)
            while low < high:
                middle = (low + high + 1) // 2
                if font.getlength(pending[:middle]) <= max_width:
                    low = middle
                else:
                    high = middle - 1
            cut = low
            if cut < len(pending):
                split = pending.rfind(" ", 0, cut + 1)
                if split > 0:
                    cut = split
            lines.append((pending[:cut], checklist and first))
            first = False
            pending = pending[cut:].lstrip(" ")
            _check_text_rows(len(lines), step)
        if first:
            lines.append(("", False))
            _check_text_rows(len(lines), step)
    return lines


def _check_text_rows(count: int, step: int) -> None:
    if count * step + 16 > MAX_ROWS:
        raise ValueError("Rendered text exceeds 2048 rows; split the job")


def _text_part(
    field: str,
    wrapped: list[tuple[str, bool]],
    size: int,
    align: str,
    font: ImageFont.FreeTypeFont,
    checklist: bool,
) -> _TextPart:
    ascent, descent = font.getmetrics()
    step = ascent + descent + 5
    inset = 34 if checklist else 10
    lines = []
    for line, check in wrapped:
        x = float(inset)
        if not checklist and align == "center":
            x = max(inset, (WIDTH - font.getlength(line)) / 2)
        elif not checklist and align == "right":
            x = max(inset, WIDTH - 10 - font.getlength(line))
        lines.append((line, check, x))
    rows = max(1, len(lines))
    block = {
        "field": field,
        "kind": "text",
        "x": inset,
        "w": WIDTH - inset - 10,
        "h": rows * step + 16,
        "step": step,
        "size": size,
        "ascent": ascent,
        "descent": descent,
        "lines": [line for line, _ in wrapped],
    }
    return _TextPart(block, font, tuple(lines))


def _text_block(
    field: str,
    text: str,
    size: int,
    align: str,
    options: dict[str, Any],
    font_path: str | None,
    checklist: bool = False,
) -> _TextPart:
    font = _font(size, options, font_path)
    ascent, descent = font.getmetrics()
    inset = 34 if checklist else 10
    wrapped = _wrap(font, text, WIDTH - inset - 10, ascent + descent + 5, checklist)
    return _text_part(field, wrapped, size, align, font, checklist)


def _spool_rows(
    rows: list[tuple[str, str, str]],
    size: int,
    align: str,
    options: dict[str, Any],
    path: str | None,
) -> _TextPart:
    """One text band of `Name: value` lines; anchors mark where each row's value starts."""
    font = _font(size, options, path)
    ascent, descent = font.getmetrics()
    step = ascent + descent + 5
    wrapped: list[tuple[str, bool]] = []
    spans = []
    for key, name, value in rows:
        first = len(wrapped)
        wrapped += _wrap(font, f"{name}: {value}", WIDTH - 20, step, False)
        _check_text_rows(len(wrapped), step)
        spans.append((key, name, first, len(wrapped) - first))
    part = _text_part("spool_rows", wrapped, size, align, font, False)
    anchors = []
    for key, name, first, count in spans:
        # The value starts on the first line holding more ink characters than "Name:" has;
        # a prefix that itself wraps (large sizes) pushes the value to a later line.
        prefix = len(f"{name}:")
        seen = 0
        for offset in range(count):
            seen += len(wrapped[first + offset][0].replace(" ", ""))
            if seen > prefix:
                break
        start = first + offset
        x = part.lines[start][2]
        if offset == 0:
            x += font.getlength(f"{name}: ")
        x = _half_up(x)
        anchors.append((key, x, WIDTH - 10 - x, start, count - offset))
    return replace(part, anchors=tuple(anchors))


def _qr_part(field: str, text: str, options: dict[str, Any]) -> _QrPart:
    _text(text, 500, required=True)
    levels = {
        "M": qrcode.constants.ERROR_CORRECT_M,
        "Q": qrcode.constants.ERROR_CORRECT_Q,
        "H": qrcode.constants.ERROR_CORRECT_H,
    }
    code = qrcode.QRCode(error_correction=levels[options["qr_correction"]], box_size=1, border=4)
    code.add_data(text)
    try:
        code.make(fit=True)
    except qrcode.exceptions.DataOverflowError as error:
        raise ValueError("QR data is too large") from error
    image = code.make_image(fill_color="black", back_color="white").convert("L")
    scale = options["qr_size"] // image.width
    if scale < 3:
        raise ValueError("QR modules would be too small; enlarge the QR or shorten its data")
    image = image.resize((image.width * scale, image.height * scale), Image.Resampling.NEAREST)
    block = {
        "field": field,
        "kind": "qr",
        "x": (WIDTH - image.width) // 2,
        "w": image.width,
        "h": image.height,
        "step": 0,
        "size": 0,
        "ascent": 0,
        "descent": 0,
        "lines": [],
    }
    return _QrPart(block, image)


def _draw(part: _TextPart | _QrPart) -> Image.Image:
    if isinstance(part, _QrPart):
        canvas = Image.new("L", (WIDTH, part.image.height), 255)
        canvas.paste(part.image, (part.block["x"], 0))
        return canvas
    step = part.block["step"]
    image = Image.new("L", (WIDTH, part.block["h"]), 255)
    draw = ImageDraw.Draw(image)
    for i, (line, check, x) in enumerate(part.lines):
        y = 8 + i * step
        if check:
            draw.rectangle((10, y + 7, 24, y + 21), outline=0, width=2)
        draw.text((x, y), line, font=part.font, fill=0)
    return image


def _plan(parts: list[_TextPart | _QrPart]) -> dict[str, Any]:
    """Place the bands one under another, 8 rows of paper margin above and below."""
    rows = sum(p.block["h"] for p in parts) + 16
    if not parts or rows > MAX_ROWS:
        raise ValueError("Label exceeds 2048 rows; split the job")
    blocks = []
    y = 8
    for part in parts:
        block = dict(part.block, y=y, text_y=y if isinstance(part, _QrPart) else y + 8)
        if isinstance(part, _TextPart) and part.anchors:
            block["anchors"] = [
                {
                    "field": key,
                    "x": x,
                    "text_y": block["text_y"] + first * block["step"],
                    "w": w,
                    "h": count * block["step"],
                }
                for key, x, w, first, count in part.anchors
            ]
        blocks.append(block)
        y += part.block["h"]
    return {"rows": rows, "blocks": blocks}


def _stack(parts: list[_TextPart | _QrPart], border: bool) -> Image.Image:
    plan = _plan(parts)
    rows = plan["rows"]
    image = Image.new("L", (WIDTH, rows), 255)
    for part, block in zip(parts, plan["blocks"]):
        image.paste(_draw(part), (0, block["y"]))
    if border:
        ImageDraw.Draw(image).rectangle((2, 2, WIDTH - 3, rows - 3), outline=0, width=2)
    # Text and QR are never inverted, rescaled or dithered using photo settings.
    return image.point(lambda p: 255 if p >= 160 else 0, mode="1")


def _label_parts(
    template: str, fields: Any, options: dict[str, Any], font_path: str | None
) -> list[_TextPart | _QrPart]:
    if not isinstance(template, str) or template not in TEMPLATES or not isinstance(fields, dict):
        raise ValueError("Unknown template or invalid fields")
    spec = TEMPLATES[template]
    if fields.keys() - set(spec["fields"]):
        raise ValueError("Unknown template field")
    for key in spec["fields"]:
        _text(fields.get(key, ""), 500 if key == "qr" else 3000, key in spec["required"])
    if sum(len(v) for k, v in fields.items() if k != "qr") > 3000:
        raise ValueError("Combined label text exceeds 3000 characters")
    size, face = options["font_size"], options["font"]
    parts: list[_TextPart | _QrPart] = []
    if title := fields.get("title"):
        role = "heading" if template == "heading" else "title"
        parts.append(
            _text_block(
                "title", title, derived_size(face, role, size), "center", options, font_path
            )
        )
    if location := fields.get("location"):
        parts.append(_text_block("location", location, size, options["align"], options, font_path))
    if template == "spool_label":
        names = {"material": "Материал", "color": "Цвет", "weight": "Остаток", "nozzle": "Сопло"}
        rows = [(key, name, fields[key]) for key, name in names.items() if fields.get(key)]
        if rows:
            parts.append(_spool_rows(rows, size, options["align"], options, font_path))
    if text := fields.get("text"):
        parts.append(
            _text_block(
                "text", text, size, options["align"], options, font_path, template == "checklist"
            )
        )
    if data := fields.get("qr"):
        parts.append(_qr_part("qr", data, options))
    if footer := fields.get("footer"):
        parts.append(
            _text_block(
                "footer", footer, derived_size(face, "footer", size), "center", options, font_path
            )
        )
    return parts


def _load_image(doc: dict[str, Any], allowed: Callable[[str], bool] | None) -> Image.Image:
    if ("path" in doc) == ("image_base64" in doc):
        raise ValueError("Supply exactly one image path or image_base64")
    if "path" in doc:
        path = _text(doc["path"], 1024, required=True)
        if allowed is None or not allowed(path):
            raise ValueError("Image is outside allowed Home Assistant paths")
        if not Path(path).is_file():
            raise ValueError("Image must be a regular file")
        # Bounded read remains bounded if a file grows between stat and read.
        try:
            with Path(path).open("rb") as handle:
                raw = handle.read(MAX_FILE_BYTES + 1)
        except OSError as error:
            raise ValueError("Not a readable image") from error
    else:
        value = doc["image_base64"]
        if not isinstance(value, str) or len(value) > MAX_BASE64 + 64:
            raise ValueError("Image exceeds 4 MiB")
        if value.startswith("data:"):
            header, sep, value = value.partition(",")
            if not sep or header not in {
                f"data:image/{fmt};base64" for fmt in ("png", "jpeg", "webp", "gif", "bmp")
            }:
                raise ValueError("Unsupported image data URL")
        if len(value) > MAX_BASE64:
            raise ValueError("Image exceeds 4 MiB")
        try:
            raw = base64.b64decode(value, validate=True)
        except (ValueError, binascii.Error) as error:
            raise ValueError("Invalid image_base64") from error
    if len(raw) > MAX_FILE_BYTES:
        raise ValueError("Image exceeds 4 MiB")
    try:
        with Image.open(io.BytesIO(raw)) as source:
            if source.width * source.height > MAX_SOURCE_PIXELS:
                raise ValueError("Source image exceeds 4 million pixels")
            image = ImageOps.exif_transpose(source).convert("RGBA")
    except (
        OSError,
        SyntaxError,
        EOFError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as error:
        raise ValueError("Not a readable image") from error
    canvas = Image.new("RGBA", image.size, "white")
    canvas.alpha_composite(image)
    return canvas.convert("L")


def _image(
    doc: dict[str, Any], options: dict[str, Any], allowed: Callable[[str], bool] | None
) -> Image.Image:
    image = _load_image(doc, allowed)
    if options["rotate"]:
        image = image.rotate(-options["rotate"], expand=True)
    width = options["width"] if options["fit"] == "width" else min(options["width"], image.width)
    height = max(1, round(image.height * width / image.width))
    if height > MAX_ROWS:
        raise ValueError("Scaled image exceeds 2048 rows")
    image = image.resize((width, height), Image.Resampling.LANCZOS)
    for cls, key in (
        (ImageEnhance.Brightness, "brightness"),
        (ImageEnhance.Contrast, "contrast"),
        (ImageEnhance.Sharpness, "sharpness"),
    ):
        image = cls(image).enhance(options[key])
    if options["invert"]:
        image = ImageOps.invert(image)
    threshold = options["threshold"]
    if options["method"] == "threshold":
        image = image.point(lambda p: 255 if p >= threshold else 0, mode="1")
    elif options["method"] == "floyd_steinberg":
        image = image.point(lambda p: max(0, min(255, p + 128 - threshold))).convert(
            "1", dither=Image.Dither.FLOYDSTEINBERG
        )
    else:
        source = image.tobytes()
        result = bytearray(len(source))
        for y in range(height):
            row = _BAYER[y % 4]
            for x in range(width):
                limit = (row[x % 4] + 0.5) * 16 + threshold - 128
                result[y * width + x] = 255 if source[y * width + x] >= limit else 0
        image = Image.frombytes("L", (width, height), bytes(result)).convert(
            "1", dither=Image.Dither.NONE
        )
    canvas = Image.new("1", (WIDTH, height), 1)
    canvas.paste(image, ((WIDTH - width) // 2, 0))
    return canvas


def _check_document(document: Any) -> tuple[str, dict[str, Any]]:
    if not isinstance(document, dict) or not isinstance(document.get("kind"), str):
        raise ValueError("Document must have a kind")
    kind = document["kind"]
    keys = {
        "text": {"text"},
        "qr": {"text"},
        "image": {"path", "image_base64"},
        "template": {"template", "fields"},
    }
    if kind not in keys or document.keys() - (keys[kind] | {"kind", "options"}):
        raise ValueError("Unknown document kind or field")
    return kind, normalize_options(document.get("options"))


def _document_parts(
    document: dict[str, Any], kind: str, options: dict[str, Any], font_path: str | None
) -> list[_TextPart | _QrPart]:
    if kind == "qr":
        return [_qr_part("qr", document.get("text"), options)]
    if kind == "text":
        text = _text(document.get("text"), required=True)
        return [
            _text_block("text", text, options["font_size"], options["align"], options, font_path)
        ]
    return _label_parts(document.get("template"), document.get("fields", {}), options, font_path)


def _check_font_path(kind: str, font_path: str | None, is_allowed: Callable[[str], bool] | None):
    # Authorization applies to explicit font files too, before ImageFont reads them.
    if (
        kind != "qr"
        and font_path
        and (
            Path(font_path).suffix.lower() not in {".ttf", ".otf"}
            or is_allowed is None
            or not is_allowed(font_path)
        )
    ):
        raise ValueError("Font must be a TTF/OTF file in an allowed Home Assistant path")


def layout_blocks(
    document: Any,
    font_path: str | None = None,
    is_allowed_path: Callable[[str], bool] | None = None,
) -> dict[str, Any]:
    """Return the label's band geometry exactly as `render_document` draws it."""
    kind, options = _check_document(document)
    if kind == "image":
        raise ValueError("Layout is available for template, text and qr documents only")
    _check_font_path(kind, font_path, is_allowed_path)
    return _plan(_document_parts(document, kind, options, font_path))


def render_document(
    document: Any,
    font_path: str | None = None,
    is_allowed_path: Callable[[str], bool] | None = None,
) -> Image.Image:
    """Render a validated structured document, never making a Bluetooth connection."""
    kind, options = _check_document(document)
    if kind == "image":
        return _image(document, options, is_allowed_path)
    _check_font_path(kind, font_path, is_allowed_path)
    return _stack(_document_parts(document, kind, options, font_path), options["border"])


def bitmap(image: Image.Image) -> tuple[bytes, int]:
    """Return ink=1, MSB-first rows, matching the original transport contract."""
    if image.width != WIDTH or not 1 <= image.height <= MAX_ROWS:
        raise ValueError("Output must be 384 dots wide and 1..2048 rows tall")
    return image.convert("1", dither=Image.Dither.NONE).tobytes().translate(_INVERT), image.height


def preview_result(image: Image.Image) -> dict[str, Any]:
    """Return an in-memory PNG; no public file, history or persistent image state."""
    data, height = bitmap(image)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return {
        "png_base64": base64.b64encode(output.getvalue()).decode("ascii"),
        "width": WIDTH,
        "height": height,
        "bitmap_sha256": hashlib.sha256(data).hexdigest(),
        "ink_percent": round(100 * sum(x.bit_count() for x in data) / (WIDTH * height), 2),
        "confirmation": "preview_only",
    }
