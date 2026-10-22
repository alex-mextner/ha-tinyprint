#!/usr/bin/env python3
"""Build the bundled fonts from pinned upstream sources.

Requires: fonttools==4.67.0

    python tools/build_fonts.py          # rebuild custom_components/tinyprint/frontend/fonts/
    python tools/build_fonts.py --check  # rebuild into a temp dir, fail on any hash difference

Upstream files are downloaded from the pinned google/fonts commit, verified by SHA-256 and cached in
~/.cache/tinyprint-research/fonts-cache/. The shipped binaries are the committed build output; the
SHA-256 of each is written to SHA256SUMS. fontTools output depends on its version, hence the pin.

CI verifies the committed hashes through tests/test_fonts.py. `--check` is the maintainers'
reproducibility check (needs the network on a cold cache): run it when bumping a pin.
"""

import argparse
import hashlib
import io
import os
import shutil
import sys
import tempfile
import urllib.error
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
    "licence_sha256": "9cbaed04b20c853f99840efe5dc96956f6f6120ed83a0ade35f9281a2b63e5d0",
    "output": "RobotoFlex.ttf",
    "licence": "OFL-RobotoFlex.txt",
}
TINY5 = {
    "path": "ofl/tiny5/Tiny5[BLED,JITT,ROND,wdth,wght].ttf",
    "sha256": "588626f2c1b164f55927c0d8123f4bc134651aad7d257da68fc19b0824b0d8fa",
    "licence_path": "ofl/tiny5/OFL.txt",
    "licence_sha256": "690fda321d0ebce403249c8d61dadddcee09dc8813424cc5a8f982efcff9a9a3",
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


def fetch(path: str, expected: str) -> bytes:
    cached = CACHE_DIR / sha256(f"{COMMIT}/{path}".encode())[:16]
    if cached.exists():
        data = cached.read_bytes()
    else:
        url = RAW_URL.format(path=urllib.parse.quote(path, safe="/"))
        try:
            with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310 - pinned https URL
                data = response.read()
        except (urllib.error.URLError, TimeoutError) as error:
            raise SystemExit(f"download failed for {url}: {error}") from error
        if sha256(data) == expected:
            partial = cached.with_suffix(".part")
            partial.write_bytes(data)
            os.replace(partial, cached)
    if sha256(data) != expected:
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
    # OFL notice, licence and licence URL records (the default keeps ids 0-6 only).
    options.name_IDs = [0, 1, 2, 3, 4, 5, 6, 13, 14]
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
            licence = fetch(spec["licence_path"], spec["licence_sha256"]).replace(b"\r\n", b"\n")
            (output / spec["licence"]).write_bytes(licence)
            sums[spec["output"]] = sha256((output / spec["output"]).read_bytes())
    lines = "".join(f"{digest}  {name}\n" for name, digest in sorted(sums.items()))
    (output / "SHA256SUMS").write_text(lines)
    return sums


def licence_text(output: Path) -> dict[str, bytes]:
    return {
        spec["licence"]: (output / spec["licence"]).read_bytes() for spec in (ROBOTO_FLEX, TINY5)
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail if a rebuild differs")
    args = parser.parse_args()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=CACHE_DIR.parent) as temp:
        built = Path(temp)
        sums = build(built)
        if args.check:
            differing = [
                name
                for name in [*sums, *licence_text(built)]
                if not (OUTPUT_DIR / name).exists()
                or (OUTPUT_DIR / name).read_bytes() != (built / name).read_bytes()
            ]
            if (OUTPUT_DIR / "SHA256SUMS").read_bytes() != (built / "SHA256SUMS").read_bytes():
                differing.append("SHA256SUMS")
            if differing:
                print("differ from a fresh build: " + ", ".join(differing), file=sys.stderr)
                return 1
            print("fonts match a fresh build")
            return 0
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        for path in built.iterdir():
            shutil.copyfile(path, OUTPUT_DIR / (path.name + ".part"))
            os.replace(OUTPUT_DIR / (path.name + ".part"), OUTPUT_DIR / path.name)
        for name, digest in sums.items():
            print(f"{digest}  {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
