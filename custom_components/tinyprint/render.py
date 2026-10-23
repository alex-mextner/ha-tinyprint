"""Bounded raster rendering. Call in HA's executor, not its event loop."""

from pathlib import Path

import qrcode
from PIL import Image, ImageDraw, ImageFont, ImageOps

from .fonts import DEFAULT_FONT, open_font

WIDTH = 384
MAX_ROWS = 2048
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_SOURCE_PIXELS = 4_000_000


def _font(size: int, path: str | None) -> ImageFont.FreeTypeFont:
    if not 12 <= size <= 64:
        raise ValueError("Font size must be between 12 and 64")
    if path:
        try:
            return ImageFont.truetype(path, size)
        except OSError as err:
            raise ValueError(f"font_path is not a readable TTF font: {path}") from err
    return open_font(DEFAULT_FONT, size)


def render_text(text: str, font_size: int = 24, font_path: str | None = None) -> Image.Image:
    """Wrap explicitly and preserve blank lines. All text is rasterized locally."""
    if not isinstance(text, str) or not text.strip() or len(text) > 3000:
        raise ValueError("Text must contain 1..3000 characters and cannot be blank")
    font = _font(font_size, font_path)
    lines: list[str] = []
    # Prefer word boundaries; split long unbroken tokens only as necessary.
    for source in text.expandtabs(4).split("\n"):
        pending = source.rstrip("\r")
        if not pending:
            lines.append("")
            continue
        while pending:
            low, high = 1, len(pending)
            while low < high:
                middle = (low + high + 1) // 2
                if font.getlength(pending[:middle]) <= WIDTH - 16:
                    low = middle
                else:
                    high = middle - 1
            cut = low
            if cut < len(pending):
                space = pending.rfind(" ", 0, cut + 1)
                if space > 0:
                    cut = space
            lines.append(pending[:cut])
            pending = pending[cut:].lstrip(" ")
    ascent, descent = font.getmetrics()
    line_height = ascent + descent + 4
    height = len(lines) * line_height + 16
    if height > MAX_ROWS:
        raise ValueError("Rendered text exceeds 2048 rows; split it into smaller jobs")
    image = Image.new("L", (WIDTH, height), 255)
    draw = ImageDraw.Draw(image)
    for index, line in enumerate(lines):
        draw.text((8, 8 + index * line_height), line, font=font, fill=0)
    return image


def render_qr(text: str) -> Image.Image:
    """Integer module scaling with four-module quiet zone; no antialiasing."""
    if not isinstance(text, str) or not text.strip() or len(text) > 500:
        raise ValueError("QR payload must contain 1..500 characters")
    code = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=1, border=4)
    code.add_data(text)
    code.make(fit=True)
    image = code.make_image(fill_color="black", back_color="white").convert("L")
    scale = WIDTH // image.width
    if scale < 3:
        raise ValueError("QR is too dense for reliable printing at 384 dots")
    image = image.resize((image.width * scale, image.height * scale), Image.Resampling.NEAREST)
    canvas = Image.new("L", (WIDTH, image.height), 255)
    canvas.paste(image, ((WIDTH - image.width) // 2, 0))
    return canvas


def render_image(path: str) -> Image.Image:
    """Load a local image after HA has authorized the path; reject oversized input."""
    file = Path(path)
    if not file.is_file() or file.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("Image must be a local file no larger than 4 MiB")
    try:
        with Image.open(file) as source:
            if source.width * source.height > MAX_SOURCE_PIXELS:
                raise ValueError("Source image exceeds 4 million pixels")
            if (
                not source.width
                or not source.height
                or source.height > MAX_ROWS
                and source.width <= WIDTH
            ):
                raise ValueError("Image exceeds output height limit")
            image = ImageOps.exif_transpose(source).convert("RGBA")
    except (
        OSError,
        SyntaxError,
        EOFError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as error:
        raise ValueError("Not a readable image or source dimensions are unsafe") from error
    if image.width > WIDTH:
        new_height = round(image.height * WIDTH / image.width)
        if new_height > MAX_ROWS:
            raise ValueError("Scaled image exceeds 2048 rows")
        image = image.resize((WIDTH, max(new_height, 1)), Image.Resampling.LANCZOS)
    if image.height > MAX_ROWS:
        raise ValueError("Image exceeds 2048 rows")
    canvas = Image.new("RGBA", (WIDTH, image.height), "white")
    canvas.alpha_composite(image, ((WIDTH - image.width) // 2, 0))
    return canvas.convert("L")


def to_bitmap(image: Image.Image) -> tuple[bytes, int]:
    """Pillow uses white=1; invert after dithering to obtain ink=1."""
    if image.width != WIDTH or not 1 <= image.height <= MAX_ROWS:
        raise ValueError("Output must be 384 dots wide and 1..2048 rows tall")
    data = image.convert("1").tobytes()
    return bytes(value ^ 255 for value in data), image.height
