#!/usr/bin/env python3
"""Build the bundled fonts from pinned upstream sources.

Requires: fonttools==4.67.0

    python tools/build_fonts.py          # rebuild custom_components/tinyprint/frontend/fonts/
    python tools/build_fonts.py --check  # rebuild into a temp dir, fail on any hash difference

Upstream files are downloaded from the pinned google/fonts commit, verified by SHA-256 and cached in
~/.cache/tinyprint-research/fonts-cache/. The shipped binaries are the committed build output; the
SHA-256 of each is written to SHA256SUMS. fontTools output depends on its version, hence the pin.
"""

import argparse
import hashlib
import io
import sys
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

from fontTools import subset
from fontTools.ttLib import TTFont
from fontTools.varLib import instancer

COMMIT = "45e3d5269e4a17d823f6d84470bc084dc5ae46f6"
RAW_URL = "https://raw.githubusercontent.com/google/fonts/" + COMMIT + "/{path}"

ROBOTO_FLEX = {
    "path": "ofl/robotoflex/RobotoFlex[GRAD,XOPQ,XTRA,YOPQ,YTAS,YTDE,YTFI,YTLC,YTUC,opsz,slnt,wdth,wght].ttf",
    "sha256": "9b523f7d82593df0107173849ebb8c817471a1df4b4fb2c3cbf40cfd810c8281",
    "licence_path": "ofl/robotoflex/OFL.txt",
    "output": "RobotoFlex.ttf",
    "licence": "OFL-RobotoFlex.txt",
}
TINY5 = {
    "path": "ofl/tiny5/Tiny5[BLED,JITT,ROND,wdth,wght].ttf",
    "sha256": "588626f2c1b164f55927c0d8123f4bc134651aad7d257da68fc19b0824b0d8fa",
    "licence_path": "ofl/tiny5/OFL.txt",
    "output": "Tiny5-Regular.ttf",
    "licence": "OFL-Tiny5.txt",
}

# Roboto Flex keeps wdth and wght free and pins every other axis (opsz at 14, the rest at default).
ROBOTO_PINS = {
    "opsz": 14,
    "GRAD": 0,
    "slnt": 0,
    "XOPQ": 96,
    "YOPQ": 79,
    "XTRA": 468,
    "YTUC": 712,
    "YTLC": 514,
    "YTAS": 750,
    "YTDE": -203,
    "YTFI": 738,
}
TINY5_PINS = {"wght": 400, "wdth": 100, "ROND": 0, "BLED": 0, "JITT": 0}
KEPT_AXES = ["wght", "wdth"]

UNICODES = (
    list(range(0x0000, 0x024F + 1))
    + list(range(0x0400, 0x052F + 1))
    + list(range(0x2010, 0x2027 + 1))
    + list(range(0x2030, 0x205E + 1))
    + [0x2116, 0x20AC, 0x20BD]
)

REPO = Path(__file__).resolve().parents[1]
OUTPUT_DIR = REPO / "custom_components" / "tinyprint" / "frontend" / "fonts"
CACHE_DIR = Path.home() / ".cache" / "tinyprint-research" / "fonts-cache"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fetch(path: str, expected: str | None) -> bytes:
    cached = CACHE_DIR / sha256(path.encode())[:16]
    if cached.exists():
        data = cached.read_bytes()
    else:
        url = RAW_URL.format(path=urllib.parse.quote(path, safe="/"))
        with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310 - pinned https URL
            data = response.read()
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cached.write_bytes(data)
    if expected is not None and sha256(data) != expected:
        cached.unlink(missing_ok=True)
        raise SystemExit(f"{path}: SHA-256 {sha256(data)} != pinned {expected}")
    return data


def build_font(source: Path, pins: dict[str, float], target: Path) -> None:
    font = TTFont(source, recalcTimestamp=False)
    font = instancer.instantiateVariableFont(font, pins, inplace=False)
    # Reload: the instanced font keeps lazy glyph tables the subsetter cannot index.
    buffer = io.BytesIO()
    font.save(buffer)
    buffer.seek(0)
    font = TTFont(buffer, recalcTimestamp=False)
    options = subset.Options()
    options.layout_features = ["*"]
    options.notdef_outline = True
    options.recalc_timestamp = False
    options.drop_tables = [*options.drop_tables, "meta"]
    subsetter = subset.Subsetter(options)
    subsetter.populate(unicodes=UNICODES)
    subsetter.subset(font)
    font.save(target)


def build(output: Path) -> dict[str, str]:
    sums: dict[str, str] = {}
    with tempfile.TemporaryDirectory(dir=CACHE_DIR.parent) as work:
        for spec, pins in ((ROBOTO_FLEX, ROBOTO_PINS), (TINY5, TINY5_PINS)):
            source = Path(work) / "source.ttf"
            source.write_bytes(fetch(spec["path"], spec["sha256"]))
            build_font(source, pins, output / spec["output"])
            licence = fetch(spec["licence_path"], None).replace(b"\r\n", b"\n")
            (output / spec["licence"]).write_bytes(licence)
            sums[spec["output"]] = sha256((output / spec["output"]).read_bytes())
    lines = "".join(f"{digest}  {name}\n" for name, digest in sorted(sums.items()))
    (output / "SHA256SUMS").write_text(lines)
    return sums


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail if a rebuild differs")
    args = parser.parse_args()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if not args.check:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        for name, digest in build(OUTPUT_DIR).items():
            print(f"{digest}  {name}")
        return 0
    with tempfile.TemporaryDirectory(dir=CACHE_DIR.parent) as temp:
        rebuilt = build(Path(temp))
        shipped = (OUTPUT_DIR / "SHA256SUMS").read_text()
        wanted = "".join(f"{digest}  {name}\n" for name, digest in sorted(rebuilt.items()))
        if shipped != wanted:
            print("fonts differ from a fresh build:\n" + wanted, file=sys.stderr)
            return 1
    print("fonts match a fresh build")
    return 0


if __name__ == "__main__":
    sys.exit(main())
