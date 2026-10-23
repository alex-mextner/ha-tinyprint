"""Browser checks for the renderer modules, run once per browser in TINYPRINT_BROWSERS.

Run: python tests/browser_renderer.py
"""

import asyncio
import json
import random
import sys
from pathlib import Path

from playwright.async_api import async_playwright

sys.path.insert(0, str(Path(__file__).parent))
from browser_harness import ROOT, STATIC, browsers, font_table, open_page  # noqa: E402

FONTS = font_table()
METRICS = json.loads((ROOT / "tests" / "fixtures" / "layout_cases.json").read_text())["metrics"]
PACK = f"{STATIC}renderer/pack.js"
FONTS_JS = f"{STATIC}renderer/fonts.js"


def seeded(n: int, seed: int = 7) -> list[int]:
    rng = random.Random(seed)
    return [rng.randrange(256) for _ in range(n)]


async def check_pack_bits(page):
    result = await page.evaluate(
        """async (url) => {
          const p = await import(url);
          const rgba = (xs, a) => { const r = new Uint8ClampedArray(384 * 4); for (const x of xs) r[x * 4 + 3] = a; return r; };
          const first = p.packAlpha(rgba([0], 255), 384, 1);
          const last = p.packAlpha(rgba([383], 255), 384, 1);
          const below = p.packAlpha(rgba([5], 95), 384, 1);
          const at = p.packAlpha(rgba([5], 96), 384, 1);
          const ink = new Uint8Array(384); ink[0] = 1;
          const inkLast = new Uint8Array(384); inkLast[383] = 1;
          return {
            first: [first[0], first.length], last: [last[47], last[0]],
            below: Array.from(below).every((b) => b === 0), at: at[0],
            ink: p.packInk(ink, 1)[0], inkLast: p.packInk(inkLast, 1)[47],
          };
        }""",
        PACK,
    )
    assert result["first"] == [0x80, 48], result
    assert result["last"] == [0x01, 0], result
    assert result["below"] is True and result["at"] == 0x04, result
    assert result["ink"] == 0x80 and result["inkLast"] == 0x01, result


async def check_base64(page):
    big = seeded(98304)
    result = await page.evaluate(
        """async ([url, big]) => {
          const p = await import(url);
          const same = (bytes) => {
            const back = Uint8Array.from(atob(p.bytesToBase64(bytes)), (c) => c.charCodeAt(0));
            return back.length === bytes.length && back.every((b, i) => b === bytes[i]);
          };
          return [same(Uint8Array.from(big)), ...[0, 1, 2, 3].map((n) => same(Uint8Array.from(big.slice(0, n))))];
        }""",
        [PACK, big],
    )
    assert result == [True] * 5, result


async def check_roundtrip(page):
    data = seeded(48 * 40, seed=11)
    result = await page.evaluate(
        """async ([url, data]) => {
          const p = await import(url);
          const bitmap = Uint8Array.from(data);
          const image = p.unpack(bitmap, 40);
          const alpha = new Uint8ClampedArray(384 * 40 * 4);
          let opaque = true;
          for (let i = 0; i < 384 * 40; i += 1) {
            if (image.data[i * 4 + 3] !== 255) opaque = false;
            alpha[i * 4 + 3] = image.data[i * 4] === 0 ? 255 : 0;
          }
          const repacked = p.packAlpha(alpha, 384, 40);
          const full = new Uint8Array(48).fill(255);
          const target = new ImageData(384, 1);
          const returned = p.unpack(Uint8Array.from([0x80, ...new Array(47).fill(0)]), 1, target);
          return {
            roundtrip: repacked.every((b, i) => b === bitmap[i]) && repacked.length === bitmap.length,
            opaque, percent: p.inkPercent(full), blank: p.inkPercent(new Uint8Array(48)),
            same: returned === target, ink: Array.from(target.data.slice(0, 4)), white: Array.from(target.data.slice(4, 8)),
          };
        }""",
        [PACK, data],
    )
    assert result["roundtrip"] and result["opaque"], result
    assert result["percent"] == 100 and result["blank"] == 0, result
    assert (
        result["same"]
        and result["ink"] == [0, 0, 0, 255]
        and result["white"] == [255, 255, 255, 255]
    ), result


MEASURE = """async ([url, base, fonts, cases]) => {
  const { FaceRegistry } = await import(url);
  const registry = new FaceRegistry(new URL(base));
  const ctx = document.createElement('canvas').getContext('2d');
  const out = [];
  for (const [width, weight] of cases) {
    const face = await registry.face('roboto_flex', fonts.roboto_flex, width, weight);
    ctx.font = face.css(48);
    out.push(ctx.measureText('Привет').width);
  }
  registry.dispose();
  return out;
}"""


async def check_axes(page):
    cases = [[25, 400], [50, 400], [75, 400], [100, 400], [151, 400], [100, 100], [100, 1000]]
    w = await page.evaluate(MEASURE, [FONTS_JS, STATIC, FONTS, cases])
    assert w[0] < w[1] < w[2] < w[3] < w[4], w
    assert w[6] > w[5], w


async def check_metrics_and_scale(page):
    result = await page.evaluate(
        """async ([url, base, fonts, metrics]) => {
          const { FaceRegistry } = await import(url);
          const registry = new FaceRegistry(new URL(base));
          const bad = [];
          for (const m of metrics) {
            const face = await registry.face(m.font, fonts[m.font], m.width, m.weight);
            if (face.ascent(m.size) !== m.ascent || face.descent(m.size) !== m.descent) {
              bad.push([m, face.ascent(m.size), face.descent(m.size)]);
            }
          }
          const tiny = await registry.face('tiny5', fonts.tiny5, 200, 400);
          const roboto = await registry.face('roboto_flex', fonts.roboto_flex, 50, 400);
          registry.dispose();
          return { bad, count: metrics.length, tiny: tiny.scaleX, roboto: roboto.scaleX, family: tiny.family };
        }""",
        [FONTS_JS, STATIC, FONTS, METRICS],
    )
    assert result["count"] > 0 and not result["bad"], result["bad"][:3]
    assert result["tiny"] == 2 and result["roboto"] == 1, result
    assert result["family"] == "tp-tiny5-w100-W400", result


async def check_lru(page):
    result = await page.evaluate(
        """async ([url, base, fonts]) => {
          const { FaceRegistry } = await import(url);
          const registry = new FaceRegistry(new URL(base));
          const tp = () => [...document.fonts].filter((f) => f.family.replaceAll('"', '').startsWith('tp-')).map((f) => f.family.replaceAll('"', ''));
          const first = await registry.face('roboto_flex', fonts.roboto_flex, 20, 400);
          for (let w = 30; w <= 90; w += 10) await registry.face('roboto_flex', fonts.roboto_flex, w, 400);
          const eight = tp();
          await registry.face('roboto_flex', fonts.roboto_flex, 100, 400);
          const nine = tp();
          const again = await registry.face('roboto_flex', fonts.roboto_flex, 20, 400);
          const afterAgain = tp();
          registry.dispose();
          const burst = new FaceRegistry(new URL(base));
          const faces = await Promise.all([20, 30, 40, 50, 60, 70, 80, 90, 100].map((w) => burst.face('roboto_flex', fonts.roboto_flex, w, 400)));
          const burstCount = tp().length;
          burst.dispose();
          return { first: first.family, eight, nine, again: again.family, afterAgain, left: tp(), burstCount, burstFaces: faces.length };
        }""",
        [FONTS_JS, STATIC, FONTS],
    )
    first = result["first"]
    assert len(result["eight"]) == 8 and first in result["eight"], result
    assert len(result["nine"]) == 8 and first not in result["nine"], result
    assert (
        result["again"] == first
        and first in result["afterAgain"]
        and len(result["afterAgain"]) == 8
    ), result
    assert result["left"] == [], result
    assert result["burstFaces"] == 9 and result["burstCount"] == 8, result


async def check_missing(page):
    async def not_found(route):
        await route.fulfill(status=404, body="nope")

    await page.route(f"**/fonts/{FONTS['roboto_flex']['file']}", not_found)
    result = await page.evaluate(
        """async ([url, base, fonts]) => {
          const { FaceRegistry } = await import(url);
          const registry = new FaceRegistry(new URL(base));
          let code = null;
          try { await registry.face('roboto_flex', fonts.roboto_flex, 100, 400); } catch (e) { code = e.code; }
          const left = [...document.fonts].filter((f) => f.family.replaceAll('"', '').startsWith('tp-')).length;
          return { code, left };
        }""",
        [FONTS_JS, STATIC, FONTS],
    )
    assert result == {"code": "font_missing", "left": 0}, result


CHECKS = [
    ("pack: bit order and alpha threshold", check_pack_bits),
    ("pack: bytesToBase64 round-trips 98,304 bytes without a RangeError", check_base64),
    ("pack: unpack(pack(b)) == b, inkPercent and target reuse", check_roundtrip),
    ("fonts: width and weight axes change advance widths", check_axes),
    ("fonts: hhea metrics match the Pillow golden and Tiny5 scales", check_metrics_and_scale),
    ("fonts: LRU keeps 8 faces and removes evicted ones from document.fonts", check_lru),
    ("fonts: missing file fails loudly", check_missing),
]


async def main() -> int:
    failures = 0
    async with async_playwright() as p:
        for name in browsers():
            for title, check in CHECKS:
                page = await open_page(p, name)
                try:
                    await check(page)
                    print(f"ok   [{name}] {title}")
                except Exception as error:  # noqa: BLE001 - report every failing check
                    failures += 1
                    print(f"FAIL [{name}] {title}: {error}")
                finally:
                    await page.context.browser.close()
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
