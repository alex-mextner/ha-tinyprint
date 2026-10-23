"""Golden fixtures for the Studio layout engines.

Pillow text output depends on the FreeType/HarfBuzz build, so the fixtures are generated AND
checked only inside the Home Assistant image (the pip-Pillow `tests` job never compares them):

    docker run --rm --user "$(id -u):$(id -g)" --entrypoint /bin/sh -v "$PWD:/workspace" \\
      -w /workspace ghcr.io/home-assistant/home-assistant:2026.9.3 -c \\
      'python -m pip install --target /tmp/test-deps --no-deps qrcode==8.2 && \\
       PYTHONPATH=/workspace:/workspace/bridge:/tmp/test-deps \\
       python tools/gen_layout_cases.py --write-hashes'

`--write-hashes` writes tests/fixtures/render_hashes.json, `--write` writes both fixtures and
`--check` exits 1 when either is out of date. Import this module freely: the CLI runs only
under `__main__`.
"""

import argparse
import hashlib
import itertools
import json
import sys
from pathlib import Path

import PIL
import qrcode
from PIL import features

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
HASHES_PATH = FIXTURES / "render_hashes.json"
CASES_PATH = FIXTURES / "layout_cases.json"

sys.path.insert(0, str(ROOT / "tests"))
from shared_loader import load  # noqa: E402

render = load("studio_render")
fonts = load("fonts")


def build_env() -> dict:
    return {
        "pillow": PIL.__version__,
        "freetype2": features.version("freetype2"),
        "harfbuzz": features.version("harfbuzz"),
        "fribidi": features.version("fribidi"),
        "raqm": features.check_feature("raqm"),
    }


def build_hashes() -> dict:
    """SHA-256 of the packed bitmap of every template example, per font and alignment."""
    out = {}
    for template, spec in render.TEMPLATES.items():
        for font in fonts.FONTS:
            for align in ("left", "center"):
                document = {
                    "kind": "template",
                    "template": template,
                    "fields": spec["example"],
                    "options": {"font": font, "align": align, "columns": 1},
                }
                data, _ = render.bitmap(render.render_document(document))
                out[f"{template}|{font}|{align}"] = hashlib.sha256(data).hexdigest()
    return out


METRIC_SIZES = (8, 12, 16, 24, 32, 48, 64, 96)
EDGE_FILLERS = "mnoeaxvuczrsiljt.,'"


def _size_allowed(font: str, size: int) -> bool:
    spec = fonts.FONTS[font]["sizes"]
    if "values" in spec:
        return size in spec["values"]
    return spec["min"] <= size <= spec["max"]


def _options(**overrides) -> dict:
    return {"columns": 1} | overrides


def _near_edge_token(font, limit: int, over: bool) -> str:
    """A space-free token whose width lies within 1 px of `limit` (at most, or just over it)."""
    low, high = (limit, limit + 1) if over else (limit - 1, limit)
    count = 0
    while font.getlength("m" * (count + 1)) <= limit - 24:
        count += 1
    for prefix in ("m" * k for k in range(count, max(count - 3, 0), -1)):
        for length in range(1, 5):
            for suffix in itertools.product(EDGE_FILLERS, repeat=length):
                token = prefix + "".join(suffix)
                width = font.getlength(token)
                if (low < width <= high) if over else (low <= width <= high):
                    return token
    raise AssertionError(f"no near-edge token for width {limit} (over={over})")


def _case_documents() -> list[tuple[str, dict, bool]]:
    """(name, document, near_edge) in a stable order."""
    cases: list[tuple[str, dict, bool]] = []

    def template(name: str, fields: dict, **options) -> dict:
        return {
            "kind": "template",
            "template": name,
            "fields": fields,
            "options": _options(**options),
        }

    for name, spec in render.TEMPLATES.items():
        for size in (24, 96):
            cases.append((f"{name}-{size}", template(name, spec["example"], font_size=size), False))
    # The spool anchors depend on the aligned start x of each line (R11).
    spool = render.TEMPLATES["spool_label"]["example"]
    for align in ("center", "right"):
        for size in (24, 96):
            doc = template("spool_label", spool, font_size=size, align=align)
            cases.append((f"spool_label-{size}-{align}", doc, False))
    notes = {
        "note-tab": "a\tb\tcol\n\tindented\tx",
        "note-emoji": "Привет 😀 мир 😀😀 конец",
        "note-combining": "e\u0301 a\u0308 Ё\u0306 n\u0303 x\u0323\u0307 слово",
        "note-blank-lines": "первый\n\nвторой\n\n\nтретий",
        "note-long-token": "x" * 600,
        "note-crlf": "строка один\r\nстрока два\r\n\r\nстрока четыре",
    }
    for name, text in notes.items():
        cases.append((name, template("note", {"text": text}), False))
    long_text = "Один из самых длинных пунктов списка не помещается в одну строку ширины бумаги"
    for align in ("center", "right"):
        fields = {"title": "Заголовок", "text": "Выровненный текст, который займёт несколько строк"}
        cases.append((f"note-{align}", template("note", fields, align=align), False))
    cases.append(
        ("note-border", template("note", {"title": "T", "text": "Рамка"}, border=True), False)
    )
    items = {"text": "\n".join([long_text, "Короткий", long_text + " " + long_text])}
    cases.append(("checklist-long", template("checklist", items), False))
    # R8: the Python checklist ignores `align`; the browser port must do the same.
    cases.append(("checklist-center", template("checklist", items, align="center"), False))
    cases.append(("checklist-right", template("checklist", items, align="right"), False))
    for size in (8, 16, 24):
        fields = {
            "title": "Tiny",
            "text": "Крошечный шрифт Tiny5 тоже переносит строки",
            "footer": "f",
        }
        cases.append(
            (f"tiny5-note-{size}", template("note", fields, font="tiny5", font_size=size), False)
        )
    cases.append(
        (
            "text-plain",
            {"kind": "text", "text": "Просто текст\nвторая строка", "options": _options()},
            False,
        )
    )
    cases.append(
        ("qr-plain", {"kind": "qr", "text": "https://example.com/a", "options": _options()}, False)
    )
    cases.append(
        (
            "qr-border",
            {
                "kind": "qr",
                "text": "tinyprint",
                "options": _options(border=True, qr_correction="H"),
            },
            False,
        )
    )
    for align in ("left", "center"):
        for font, size in (("roboto_flex", 24), ("tiny5", 16)):
            opts = _options(font=font, font_size=size, align=align)
            probe = fonts.open_font(font, size)
            # Tiny5 advances are whole pixels, so no token lands in the (limit, limit + 1) window.
            for over in (False, True) if font == "roboto_flex" else (False,):
                edge = _near_edge_token(probe, 364, over)
                suffix = "over" if over else "under"
                name = f"edge-{font}-{size}-{align}-{suffix}"
                doc = template("note", {"text": f"{edge} следующее слово"}, **opts)
                cases.append((name, doc, True))
    probe = fonts.open_font("roboto_flex", 24)
    for over in (False, True):
        edge = _near_edge_token(probe, 340, over)
        name = f"edge-checklist-{'over' if over else 'under'}"
        cases.append((name, template("checklist", {"text": f"{edge} дальше\nвторой пункт"}), True))
    return cases


def _qr_modules(document: dict, text: str) -> int:
    """Symbol size without the quiet zone."""
    levels = {
        "M": qrcode.constants.ERROR_CORRECT_M,
        "Q": qrcode.constants.ERROR_CORRECT_Q,
        "H": qrcode.constants.ERROR_CORRECT_H,
    }
    correction = render.normalize_options(document.get("options"))["qr_correction"]
    code = qrcode.QRCode(error_correction=levels[correction], box_size=1, border=0)
    code.add_data(text)
    code.make(fit=True)
    return code.modules_count


def build_cases() -> dict:
    cases = []
    used = set()
    for name, document, near_edge in _case_documents():
        options = render.normalize_options(document.get("options"))
        used.add((options["font"], options["font_width"], options["font_weight"]))
        expect = render.layout_blocks(document)
        qr = False
        for block in expect["blocks"]:
            if block["kind"] == "qr":
                qr = True
                text = document["text"] if document["kind"] == "qr" else document["fields"]["qr"]
                block["modules"] = _qr_modules(document, text)
        cases.append(
            {
                "name": name,
                "document": document,
                "near_edge": near_edge,
                "qr": qr,
                "expect": expect,
            }
        )
    metrics = []
    derived = []
    for font, width, weight in sorted(used):
        for size in METRIC_SIZES:
            if not _size_allowed(font, size):
                continue
            ascent, descent = fonts.open_font(font, size, width, weight).getmetrics()
            metrics.append(
                {
                    "font": font,
                    "size": size,
                    "width": width,
                    "weight": weight,
                    "ascent": ascent,
                    "descent": descent,
                }
            )
    for font in sorted({f for f, _, _ in used}):
        for size in METRIC_SIZES:
            if _size_allowed(font, size):
                for role in ("title", "heading", "footer"):
                    derived.append(
                        {
                            "font": font,
                            "size": size,
                            "role": role,
                            "value": fonts.derived_size(font, role, size),
                        }
                    )
    return {"env": build_env(), "metrics": metrics, "derived": derived, "cases": cases}


def _dump(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, indent=1, sort_keys=True) + "\n"


def hashes_fixture() -> dict:
    return {"env": build_env(), "hashes": build_hashes()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--write-hashes", action="store_true")
    group.add_argument("--write", action="store_true")
    group.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    if not features.check_feature("raqm"):
        print("Pillow without libraqm lays text out differently; run inside the HA image")
        return 2
    wanted = {HASHES_PATH: hashes_fixture}
    if not args.write_hashes:
        wanted[CASES_PATH] = build_cases
    if args.check:
        stale = [
            p
            for p, build in wanted.items()
            if not p.exists() or p.read_text(encoding="utf-8") != _dump(build())
        ]
        for path in stale:
            print(f"{path.relative_to(ROOT)} is out of date; regenerate with --write")
        return 1 if stale else 0
    FIXTURES.mkdir(exist_ok=True)
    for path, build in wanted.items():
        path.write_text(_dump(build()), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
