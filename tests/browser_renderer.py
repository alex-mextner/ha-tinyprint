"""Browser checks for the renderer modules, run once per browser in TINYPRINT_BROWSERS.

Run: python tests/browser_renderer.py
"""

import asyncio
import base64
import io
import itertools
import json
import random
import struct
import sys
from pathlib import Path

import qrcode
from PIL import Image
from playwright.async_api import async_playwright

sys.path.insert(0, str(Path(__file__).parent))
from browser_harness import ROOT, STATIC, browsers, font_table, open_page  # noqa: E402
from shared_loader import load  # noqa: E402

FONTS = font_table()
GOLDEN = json.loads((ROOT / "tests" / "fixtures" / "layout_cases.json").read_text())
METRICS = GOLDEN["metrics"]
PACK = f"{STATIC}renderer/pack.js"
FONTS_JS = f"{STATIC}renderer/fonts.js"
LAYOUT_JS = f"{STATIC}renderer/layout.js"
QR_JS = f"{STATIC}renderer/qr.js"
IMAGE_JS = f"{STATIC}renderer/image.js"
VENDOR_QR_JS = f"{STATIC}vendor/qrcodegen.js"
STUDIO = load("studio_render")


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
          // Faces settled in the current task are protected from eviction until the task ends.
          const settle = () => new Promise((resolve) => setTimeout(resolve, 0));
          const first = await registry.face('roboto_flex', fonts.roboto_flex, 20, 400);
          for (let w = 30; w <= 90; w += 10) await registry.face('roboto_flex', fonts.roboto_flex, w, 400);
          const eight = tp();
          await registry.face('roboto_flex', fonts.roboto_flex, 100, 400);
          await settle();
          const nine = tp();
          const again = await registry.face('roboto_flex', fonts.roboto_flex, 20, 400);
          await settle();
          const afterAgain = tp();
          registry.dispose();
          const burst = new FaceRegistry(new URL(base));
          const faces = await Promise.all([20, 30, 40, 50, 60, 70, 80, 90, 100].map((w) => burst.face('roboto_flex', fonts.roboto_flex, w, 400)));
          await settle();
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


def truncated_hhea_woff(ttf: bytes) -> bytes:
    """A WOFF the browser loads but whose sfnt table directory the registry cannot read."""
    count = struct.unpack(">H", ttf[4:6])[0]
    records = [struct.unpack(">4sIII", ttf[12 + 16 * i : 28 + 16 * i]) for i in range(count)]
    tables = [
        (tag, checksum, ttf[offset : offset + length]) for tag, checksum, offset, length in records
    ]
    sfnt_size = 12 + 16 * count + sum((len(data) + 3) & ~3 for _, _, data in tables)
    offset = 44 + 20 * count
    directory, body = b"", b""
    for tag, checksum, data in tables:
        directory += struct.pack(">4sIIII", tag, offset + len(body), len(data), len(data), checksum)
        body += data + b"\0" * (-len(data) % 4)
    header = struct.pack(
        ">4sIIHHIHHIIIII",
        b"wOFF",
        0x00010000,
        44 + 20 * count + len(body),
        count,
        0,
        sfnt_size,
        1,
        0,
        0,
        0,
        0,
        0,
        0,
    )
    return header + directory + body


async def check_corrupt_font(page):
    good = (
        ROOT
        / "custom_components"
        / "tinyprint"
        / "frontend"
        / "fonts"
        / FONTS["roboto_flex"]["file"]
    ).read_bytes()
    served = {"bad": True}

    async def sometimes_corrupt(route):
        if served["bad"]:
            await route.fulfill(status=200, content_type="font/ttf", body=truncated_hhea_woff(good))
        else:
            await route.fallback()

    file = FONTS["roboto_flex"]["file"]
    await page.route(f"**/fonts/{file}", sometimes_corrupt)
    probe = """async ([url, base, fonts]) => {
      const { FaceRegistry } = await import(url);
      window.registry ??= new FaceRegistry(new URL(base));
      let code = null;
      try { await window.registry.face('roboto_flex', fonts.roboto_flex, 100, 400); } catch (e) { code = e.code; }
      const left = [...document.fonts].filter((f) => f.family.replaceAll('"', '').startsWith('tp-')).length;
      return { code, left };
    }"""
    args = [FONTS_JS, STATIC, FONTS]
    first = await page.evaluate(probe, args)
    assert first == {"code": "font_load_failed", "left": 0}, first
    served["bad"] = False
    second = await page.evaluate(probe, args)
    assert second == {"code": None, "left": 1}, second
    fetches = [u for u in page.requests if u.endswith(f"fonts/{file}")]
    assert len(fetches) == 2, fetches


async def check_concurrent_settle_order(page):
    tiny_file = FONTS["tiny5"]["file"]

    async def slow(route):
        await asyncio.sleep(1.0)
        await route.fallback()

    await page.route(f"**/fonts/{tiny_file}", slow)
    result = await page.evaluate(
        """async ([url, base, fonts]) => {
          const { FaceRegistry } = await import(url);
          const registry = new FaceRegistry(new URL(base));
          const registered = (face) => [...document.fonts].some((f) => f.family.replaceAll('"', '') === face.family && f.status === 'loaded');
          const order = [];
          const requests = [registry.face('tiny5', fonts.tiny5, 100, 400)];
          for (const w of [20, 30, 40, 50, 60, 70, 80, 90]) requests.push(registry.face('roboto_flex', fonts.roboto_flex, w, 400));
          const checked = await Promise.all(requests.map(async (request, i) => {
            const face = await request;
            order.push(i);
            return registered(face);
          }));
          const count = [...document.fonts].filter((f) => f.family.replaceAll('"', '').startsWith('tp-')).length;
          registry.dispose();
          return { checked, count, lastSettled: order[order.length - 1] };
        }""",
        [FONTS_JS, STATIC, FONTS],
    )
    assert result["lastSettled"] == 0, result
    assert all(result["checked"]), result
    assert result["count"] == 8, result


# The page side of every layout check: one registry, one measuring context, the same recipe the
# Renderer uses (OffscreenCanvas, geometricPrecision). `args.cases` carry normalized options.
LAYOUT_PRELUDE = """
const { FaceRegistry } = await import(args.fonts);
const layout = await import(args.layout);
const { RenderError } = await import(args.errors);
const registry = new FaceRegistry(new URL(args.base));
const canvas = new OffscreenCanvas(384, 2048);
const ctx = canvas.getContext('2d', { alpha: true, willReadFrequently: true });
ctx.textRendering = 'geometricPrecision';
const faceOf = (o) => registry.face(o.font, args.fonts_table[o.font], o.font_width, o.font_weight);
const plan = async (doc, modules) => {
  const face = await faceOf(doc.options);
  return layout.planLabel(doc, face, ctx, args.templates, modules === undefined ? undefined : () => modules);
};
const modulesOf = (c) => c.expect.blocks.find((b) => b.kind === 'qr')?.modules;
const near = (a, b) => Math.abs(a - b) <= 1;
"""


def layout_args(**extra):
    return {
        "fonts": FONTS_JS,
        "layout": LAYOUT_JS,
        "errors": f"{STATIC}renderer/errors.js",
        "base": STATIC,
        "fonts_table": FONTS,
        "templates": STUDIO.TEMPLATES,
        **extra,
    }


def normalized(document: dict) -> dict:
    return {**document, "options": STUDIO.normalize_options(document.get("options"))}


def layout_case(name: str) -> dict:
    return next(c for c in GOLDEN["cases"] if c["name"] == name)


async def run_layout(page, body: str, **extra):
    script = "async (args) => {" + LAYOUT_PRELUDE + body + "\n  registry.dispose();\n}"
    return await page.evaluate(script, layout_args(**extra))


CASES = [{**c, "document": normalized(c["document"])} for c in GOLDEN["cases"]]


def has_astral(case: dict) -> bool:
    document = case["document"]
    text = document.get("fields", {}).get("text") or document.get("text") or ""
    return any(ord(char) > 0xFFFF for char in text)


# Roboto Flex has no emoji: Pillow draws its .notdef box, a browser falls back to a system emoji
# font of another advance, so the Python line breaks of a WRAPPING astral run cannot be reproduced
# (platform-dependent). Those cases keep property checks (whole code points, nothing lost) and
# are left out of the line-for-line comparison; combining-mark cases are compared in full.
ASTRAL_WRAP_CASES = {c["name"] for c in CASES if c["name"].startswith("wrap-") and has_astral(c)}

GOLDEN_BODY = """
const out = [];
for (const c of args.cases) {
  if (c.near_edge !== args.near_edge) continue;
  const p = await plan(c.document, modulesOf(c));
  const e = c.expect;
  const bad = (what) => out.push(`${c.name}: ${what}`);
  if (p.blocks.length !== e.blocks.length) { bad(`blocks ${p.blocks.length} != ${e.blocks.length}`); continue; }
  if (!args.near_edge && p.rows !== e.rows) bad(`rows ${p.rows} != ${e.rows}`);
  e.blocks.forEach((x, i) => {
    const b = p.blocks[i];
    if (b.field !== x.field || b.kind !== x.kind) bad(`block ${i} ${b.field}/${b.kind} != ${x.field}/${x.kind}`);
    if (args.near_edge) {
      const flat = (l) => l.join('').replaceAll(' ', '');
      if (flat(b.lines) !== flat(x.lines)) bad(`block ${i} text differs: ${JSON.stringify(b.lines)} vs ${JSON.stringify(x.lines)}`);
      b.lines.forEach((line, k) => {
        if (b.widths[k] > b.w) bad(`block ${i} line ${k} is ${b.widths[k]} wide, limit ${b.w}`);
      });
      return;
    }
    if (JSON.stringify(b.lines) !== JSON.stringify(x.lines)) bad(`block ${i} lines ${JSON.stringify(b.lines)} != ${JSON.stringify(x.lines)}`);
    for (const k of ['x', 'y', 'w', 'h', 'text_y']) if (!near(b[k], x[k])) bad(`block ${i} ${k} ${b[k]} != ${x[k]}`);
    for (const k of ['step', 'size', 'ascent', 'descent']) if (b[k] !== x[k]) bad(`block ${i} ${k} ${b[k]} != ${x[k]}`);
    const ja = b.anchors ?? [];
    const xa = x.anchors ?? [];
    if (ja.length !== xa.length) bad(`block ${i} anchors ${ja.length} != ${xa.length}`);
    xa.forEach((a, k) => {
      const m = ja[k];
      if (!m || m.field !== a.field) { bad(`block ${i} anchor ${k} field`); return; }
      for (const key of ['x', 'text_y', 'w', 'h']) if (!near(m[key], a[key])) bad(`anchor ${a.field} ${key} ${m[key]} != ${a[key]}`);
    });
  });
}
return out;
"""


async def check_golden_cases(page):
    cases = [c for c in CASES if not c["near_edge"] and c["name"] not in ASTRAL_WRAP_CASES]
    assert len(cases) > 30, len(cases)
    bad = await run_layout(page, GOLDEN_BODY, cases=cases, near_edge=False)
    assert not bad, bad[:8]


async def check_near_edge_cases(page):
    cases = [c for c in CASES if c["near_edge"]]
    assert len(cases) >= 8, len(cases)
    bad = await run_layout(page, GOLDEN_BODY, cases=cases, near_edge=True)
    assert not bad, bad[:8]


async def check_derived_sizes(page):
    bad = await run_layout(
        page,
        """
        const out = [];
        for (const d of args.derived) {
          const got = layout.derivedSize(args.fonts_table[d.font], d.role, d.size);
          if (got !== d.value) out.push([d, got]);
        }
        return { bad: out, count: args.derived.length };
        """,
        derived=GOLDEN["derived"],
    )
    assert bad["count"] > 30 and not bad["bad"], bad["bad"][:3]


async def check_unusual_text(page):
    names = ["note-tab", "note-emoji", "note-combining", "note-long-token", "note-crlf"]
    result = await run_layout(
        page,
        """
        const out = {};
        for (const c of args.cases) {
          const p = await plan(c.document);
          const lines = p.blocks.flatMap((b) => b.lines);
          const lone = /[\\ud800-\\udbff](?![\\udc00-\\udfff])|(?<![\\ud800-\\udbff])[\\udc00-\\udfff]/;
          out[c.name] = lines.some((line) => lone.test(line)) ? ['LONE SURROGATE'] : lines;
        }
        return out;
        """,
        cases=[c for c in CASES if c["name"] in names],
    )
    for name, lines in result.items():
        assert lines and lines != ["LONE SURROGATE"], (name, lines)
    assert "".join(result["note-emoji"]).count("\U0001f600") == 3, result["note-emoji"]
    assert all(len(line) <= 600 for line in result["note-long-token"])
    assert "".join(result["note-long-token"]) == "x" * 600


async def check_too_tall(page):
    result = await run_layout(
        page,
        """
        const doc = args.doc;
        const face = await faceOf(doc.options);
        try {
          layout.planLabel(doc, face, ctx, args.templates);
          return { code: null };
        } catch (error) {
          return { code: error.code, isRenderError: error instanceof RenderError };
        }
        """,
        doc=normalized({"kind": "template", "template": "note", "fields": {"text": "\n" * 3000}}),
    )
    assert result == {"code": "too_tall", "isRenderError": True}, result


WRAP_BODY = """
const lone = /[\\ud800-\\udbff](?![\\udc00-\\udfff])|(?<![\\ud800-\\udbff])[\\udc00-\\udfff]/;
const combining = /^[\\u0300-\\u036f]/;
const out = [];
for (const c of args.cases) {
  const p = await plan(c.document);
  const text = c.document.fields?.text ?? c.document.text;
  const expected = text.replace(/[\\r\\n ]/g, '');
  const lines = p.blocks.flatMap((b) => b.lines);
  const bad = (what) => out.push(`${c.name}: ${what}`);
  if (lines.length < 2 && !args.astral.includes(c.name)) bad('did not wrap');
  if (lines.some((line) => lone.test(line))) bad('a line holds a lone surrogate');
  if (lines.some((line) => combining.test(line))) bad('a line starts with a combining mark');
  if (lines.join('').replace(/ /g, '') !== expected) bad('joined lines differ from the input');
  const golden = c.expect.blocks.flatMap((b) => b.lines);
  if (!args.astral.includes(c.name) && JSON.stringify(lines) !== JSON.stringify(golden)) {
    bad(`lines ${JSON.stringify(lines)} != ${JSON.stringify(golden)}`);
  }
}
return out;
"""


async def check_wrap_boundaries(page):
    cases = [c for c in CASES if c["name"].startswith("wrap-")]
    assert len(cases) == 11 and len(ASTRAL_WRAP_CASES) == 9, [c["name"] for c in cases]
    bad = await run_layout(page, WRAP_BODY, cases=cases, astral=sorted(ASTRAL_WRAP_CASES))
    assert not bad, bad[:6]


def rows_of(lines: int, size: int) -> int | None:
    """Python's own height of `lines` note lines at `size`, None when Python refuses it."""
    document = {
        "kind": "template",
        "template": "note",
        "fields": {"text": "\n".join(["строка"] * lines)},
        "options": {"font_size": size},
    }
    try:
        return STUDIO.layout_blocks(normalized(document))["rows"]
    except ValueError:
        return None


def row_cap_boundary() -> tuple[int, int]:
    """(size, lines): a note whose Python plan is exactly 2048 rows, one more line is refused."""
    for size in range(12, 97):
        step = sum(FONT_METRICS(size)) + 5
        lines = (2048 - 32) // step
        if rows_of(lines, size) == 2048 and rows_of(lines + 1, size) is None:
            return size, lines
    raise AssertionError("no font size lands exactly on 2048 rows")


def FONT_METRICS(size: int) -> tuple[int, int]:  # noqa: N802 - reads like a table lookup
    return load("fonts").open_font("roboto_flex", size).getmetrics()


async def check_row_cap_boundary(page):
    size, lines = row_cap_boundary()
    result = await run_layout(
        page,
        """
        const outcome = async (count) => {
          const doc = { ...args.doc, fields: { text: Array(count).fill('строка').join('\\n') } };
          try {
            return { rows: (await plan(doc)).rows };
          } catch (error) {
            return { code: error.code };
          }
        };
        return [await outcome(args.lines), await outcome(args.lines + 1)];
        """,
        doc=normalized(
            {
                "kind": "template",
                "template": "note",
                "fields": {"text": "x"},
                "options": {"font_size": size},
            }
        ),
        lines=lines,
    )
    assert result == [{"rows": 2048}, {"code": "too_tall"}], (size, lines, result)


async def check_pixels(page):
    result = await run_layout(
        page,
        """
        const read = (x, y) => ctx.getImageData(x, y, 1, 1).data[3];
        const boxed = layout.planLabel(args.checklist, await faceOf(args.checklist.options), ctx, args.templates);
        ctx.clearRect(0, 0, 384, 2048);
        layout.drawPlan(ctx, boxed);
        const y = boxed.blocks[0].text_y;
        const box = [read(10, y + 7), read(24, y + 21), read(11, y + 8), read(23, y + 20), read(13, y + 10)];
        const framed = layout.planLabel(args.bordered, await faceOf(args.bordered.options), ctx, args.templates);
        ctx.clearRect(0, 0, 384, 2048);
        layout.drawPlan(ctx, framed);
        const rows = framed.rows;
        const border = [read(2, 2), read(3, 3), read(381, rows - 3), read(4, 4), read(380, rows - 4)];
        return { box, border };
        """,
        checklist=normalized(
            {"kind": "template", "template": "checklist", "fields": {"text": "один\nдва"}}
        ),
        bordered=normalized(
            {
                "kind": "template",
                "template": "note",
                "fields": {"text": "рамка"},
                "options": {"border": True},
            }
        ),
    )
    assert result["box"][:4] == [255] * 4 and result["box"][4] == 0, result
    assert result["border"][:3] == [255] * 3 and result["border"][3] == 0, result
    assert result["border"][4] == 255, result


async def check_tiny5_scale(page):
    def doc(width):
        # Width 200 is Studio-only (Python rejects it), so it is set after normalizing.
        base = normalized(
            {
                "kind": "template",
                "template": "note",
                "fields": {"text": "Привет мир"},
                "options": {"font": "tiny5", "font_size": 16},
            }
        )
        return {**base, "options": {**base["options"], "font_width": width}}

    result = await run_layout(
        page,
        """
        const one = await plan(args.narrow);
        const two = await plan(args.wide);
        return {
          scale: (await faceOf(args.wide.options)).scaleX,
          narrow: one.blocks[0].widths,
          wide: two.blocks[0].widths,
          lines: [one.blocks[0].lines, two.blocks[0].lines],
        };
        """,
        narrow=doc(100),
        wide=doc(200),
    )
    assert result["scale"] == 2, result
    assert result["lines"][0] == result["lines"][1] and len(result["narrow"]) == 1, result
    assert result["wide"] == [2 * w for w in result["narrow"]], result


async def check_probe_overflow(page):
    result = await run_layout(
        page,
        """
        const doc = args.doc;
        const face = await faceOf(doc.options);
        const step = face.ascent(doc.options.font_size) + face.descent(doc.options.font_size) + 5;
        // The most lines that still fit: the leftover is smaller than any extra band.
        const lines = Math.floor((2048 - 32) / step);
        doc.fields.text = Array(lines).fill('строка').join('\\n');
        const p = layout.planLabel(doc, face, ctx, args.templates);
        const alone = layout.planLabel({ ...doc, fields: { text: doc.fields.text } }, face, ctx, args.templates);
        return {
          rows: p.rows,
          expected: lines * step + 32,
          slots: p.slots.map((s) => s.field),
          same: JSON.stringify(p.blocks.map((b) => [b.y, b.h, b.lines])) === JSON.stringify(alone.blocks.map((b) => [b.y, b.h, b.lines])),
          blocks: p.blocks.length,
        };
        """,
        doc=normalized({"kind": "template", "template": "note", "fields": {"text": "x"}}),
    )
    assert result["rows"] == result["expected"] <= 2048, result
    assert result["rows"] > 2048 - 60, result
    assert result["slots"] == [] and result["same"] and result["blocks"] == 1, result


async def check_blank_slot(page):
    result = await run_layout(
        page,
        """
        const titled = await plan(args.titled);
        const slot = titled.slots.find((s) => s.field === 'footer');
        const grown = await plan(args.grown);
        const footer = grown.blocks.find((b) => b.field === 'footer');
        return {
          slot,
          footerY: footer.text_y,
          fields: titled.slots.map((s) => s.field),
          title: titled.blocks.map((b) => b.field),
        };
        """,
        titled=normalized(
            {"kind": "template", "template": "note", "fields": {"title": "Т", "text": "t"}}
        ),
        grown=normalized(
            {
                "kind": "template",
                "template": "note",
                "fields": {"title": "Т", "text": "t", "footer": "x"},
            }
        ),
    )
    assert result["slot"]["y"] == result["footerY"], result
    assert result["fields"] == ["footer"] and result["title"] == ["title", "text"], result


async def check_spool_anchor_heuristic(page):
    # In the 96 px spool label the weight value starts on the line ': 750 г' (the colon counts).
    result = await run_layout(
        page,
        """
        const c = args.case;
        const p = await plan(c.document, modulesOf(c));
        const rows = p.blocks.find((b) => b.field === 'spool_rows');
        const weight = rows.anchors.find((a) => a.field === 'weight');
        return { line: rows.lines[(weight.text_y - rows.text_y) / rows.step], x: weight.x };
        """,
        case=next(c for c in CASES if c["name"] == "spool_label-96"),
    )
    assert result == {"line": ": 750 г", "x": 10}, result


async def check_slot_kinds(page):
    result = await run_layout(
        page,
        """
        const fixed = await plan(args.spool, 21);
        const noEncoder = await plan(args.spool);
        const plain = await plan(args.text);
        const slot = (p, f) => p.slots.find((s) => s.field === f);
        const grown = await plan({ ...args.spool, fields: { ...args.spool.fields, qr: 'x' } }, 21);
        const nozzled = await plan({ ...args.spool, fields: { ...args.spool.fields, nozzle: 'x' } }, 21);
        return {
          qr: slot(fixed, 'qr'),
          qrY: grown.blocks.find((b) => b.field === 'qr').text_y,
          rows: fixed.slots.map((s) => s.field),
          noEncoder: noEncoder.slots.map((s) => s.field),
          plain: plain.slots,
          nozzle: slot(fixed, 'nozzle'),
          nozzleBlock: nozzled.blocks.find((b) => b.field === 'spool_rows').anchors.at(-1),
        };
        """,
        spool=normalized(
            {"kind": "template", "template": "spool_label", "fields": {"title": "T", "color": "c"}}
        ),
        text=normalized({"kind": "text", "text": "строка"}),
    )
    assert result["qr"]["y"] == result["qrY"], result
    assert result["rows"] == ["material", "weight", "nozzle", "qr"], result
    assert result["noEncoder"] == ["material", "weight", "nozzle"], result
    assert result["plain"] == [], result
    assert result["nozzle"]["x"] == result["nozzleBlock"]["x"], result


# --- Columns 2 and 3 are Studio-only (Python rejects them), so these checks hold invariants. ---

COLUMN_HELPERS = """
const measureOf = async (doc) => {
  const face = await faceOf(doc.options);
  ctx.font = face.css(doc.options.font_size);
  return (s) => ctx.measureText(s).width * face.scaleX;
};
const mainOf = (p) => p.blocks.find((b) => b.field === 'text');
const textOf = (doc) => doc.fields?.text ?? doc.text;
const indentOf = (block) => (block.checklist ? 24 : 0);
const ink = (x0, x1, y0, y1) => {
  const { data } = ctx.getImageData(x0, y0, x1 - x0, y1 - y0);
  const xs = [];
  for (let i = 3; i < data.length; i += 4) if (data[i] > 0) xs.push(x0 + ((i - 3) / 4) % (x1 - x0));
  return xs.length === 0 ? null : [Math.min(...xs), Math.max(...xs)];
};
"""


def set_columns(document: dict, columns: int) -> dict:
    """Python refuses columns != 1, so the option is set after normalizing."""
    return {**document, "options": {**document["options"], "columns": columns}}


def column_doc(
    columns: int,
    text: str,
    *,
    template: str = "note",
    font: str = "roboto_flex",
    size: int = 24,
    width: int = 100,
    align: str = "left",
) -> dict:
    options = {"font": font, "font_size": size, "font_width": width, "align": align}
    if template == "text":
        document = {"kind": "text", "text": text, "options": options}
    else:
        fields = {"text": text}
        if template in ("heading", "storage_label"):
            fields["title"] = "Заголовок"
        document = {"kind": "template", "template": template, "fields": fields, "options": options}
    return set_columns(normalized(document), columns)


COLUMN_TEXTS = [
    "Короткая строка\nВторая строка немного длиннее первой\nТретья",
    "Unbroken " + "W" * 16 + " tail and more words to wrap across lines",
    "ПриветмирПриветмирПриветмирПриветмирПриветмир\nа б в г д е ж з и к",
]
COLUMN_TEMPLATES = ["note", "checklist", "heading", "storage_label", "text"]
COLUMN_FACES = [("roboto_flex", size) for size in (12, 24, 48)] + [
    ("tiny5", size) for size in (8, 24, 48)
]


async def check_columns_geometry(page):
    result = await run_layout(
        page,
        COLUMN_HELPERS
        + """
        const out = {};
        for (const doc of args.docs) {
          const p = await plan(doc);
          const block = mainOf(p);
          out[doc.options.columns] = {
            w: block.columns.map((c) => c.w),
            x: block.columns.map((c) => c.x),
            tallest: Math.max(...block.columns.map((c) => c.lines.length)),
            h: block.h,
            step: block.step,
            box: [block.x, block.w, block.text_y - block.y],
            rows: p.rows,
          };
        }
        return out;
        """,
        docs=[column_doc(n, "\n".join(f"строка {i}" for i in range(7)), size=12) for n in (2, 3)],
    )
    assert result["2"]["w"] == [178, 178] and result["2"]["x"] == [10, 196], result
    assert result["3"]["w"] == [116] * 3 and result["3"]["x"] == [10, 134, 258], result
    for n, tallest_lines in (("2", 4), ("3", 3)):
        got = result[n]
        assert got["tallest"] == tallest_lines, got
        assert got["h"] == tallest_lines * got["step"] + 16, got
        assert got["box"] == [10, 364, 8] and got["rows"] == got["h"] + 16, got


async def check_columns_no_line_too_wide(page):
    docs = [
        column_doc(n, text, template=template, font=font, size=size)
        for font, size in COLUMN_FACES
        for template in COLUMN_TEMPLATES
        for n in (2, 3)
        for text in COLUMN_TEXTS
    ]
    result = await run_layout(
        page,
        COLUMN_HELPERS
        + """
        const bad = [];
        let lines = 0;
        for (const doc of args.docs) {
          const block = mainOf(await plan(doc));
          const measure = await measureOf(doc);
          const tag = `${doc.options.font}/${doc.options.font_size}/${doc.options.columns}/${block.checklist}`;
          for (const col of block.columns) {
            const limit = col.w - indentOf(block);
            col.lines.forEach((line, i) => {
              lines += 1;
              // The wrap always cuts at least one code point, so a lone glyph may exceed the column.
              const lone = Array.from(line).length === 1;
              if (col.widths[i] !== measure(line)) bad.push(`${tag}: width ${i} is not the advance width`);
              if (col.widths[i] > limit && !lone) bad.push(`${tag}: ${JSON.stringify(line)} is ${col.widths[i]} > ${limit}`);
            });
          }
        }
        return { bad, lines };
        """,
        docs=docs,
    )
    assert result["lines"] > 1000 and not result["bad"], result["bad"][:5]


async def check_columns_extreme_glyphs(page):
    result = await run_layout(
        page,
        COLUMN_HELPERS
        + """
        const out = [];
        for (const doc of args.docs) {
          const block = mainOf(await plan(doc));
          const lines = block.columns.flatMap((c) => c.lines);
          out.push({
            counts: lines.map((line) => Array.from(line).length),
            joined: lines.join(''),
            checklist: block.checklist,
            over: block.columns.some((c) => c.widths.some((w) => w > c.w - indentOf(block))),
          });
        }
        return out;
        """,
        docs=[
            column_doc(3, "WWWW MMMM\nWWW", size=96, width=151),
            column_doc(3, "WWWW MMMM\nWWW", template="checklist", size=96, width=151),
        ],
    )
    for case in result:
        assert set(case["counts"]) == {1}, case
        # A glyph is 93-96 wide: it fits a 116 column but overflows the 92 left beside a checkbox.
        assert case["joined"] == "WWWWMMMMWWW" and case["over"] == case["checklist"], case


def brute_partitions(items: list[int], n: int):
    """Every contiguous split of `items` into `n` (possibly empty) groups."""
    for cuts in itertools.combinations_with_replacement(range(len(items) + 1), n - 1):
        bounds = (0, *cuts, len(items))
        yield [items[bounds[i] : bounds[i + 1]] for i in range(n)]


def brute_flow(items: list[int], n: int) -> list[list[int]]:
    """Minimal tallest column; among those the largest line counts read left to right."""

    def key(partition):
        sums = [sum(column) for column in partition]
        return (max(sums), [-s for s in sums])

    return min(brute_partitions(items, n), key=key)


def tallest(partition: list[list[int]]) -> int:
    return max(sum(column) for column in partition)


async def check_columns_tallest_minimal(page):
    docs = [
        column_doc(n, text, template=template, size=size)
        for n in (2, 3)
        for template, text, size in [
            ("note", "\n".join("слово " * (1 + i % 4) for i in range(11)), 24),
            ("checklist", "\n".join("слово " * (1 + (i * 7) % 9) + str(i) for i in range(9)), 24),
            ("checklist", "ПриветмирПриветмирПриветмирПривет\nа\nб\nв\nдлинный пункт\n" * 2, 12),
            ("text", "один два три четыре пять шесть семь восемь девять десять " * 3, 36),
        ]
    ]
    result = await run_layout(
        page,
        COLUMN_HELPERS
        + """
        const out = [];
        for (const doc of args.docs) {
          const block = mainOf(await plan(doc));
          out.push({ n: doc.options.columns, items: block.columns.map((c) => c.items) });
        }
        return out;
        """,
        docs=docs,
    )
    for case in result:
        flat = [count for column in case["items"] for count in column]
        assert len(case["items"]) == case["n"] and len(flat) > case["n"], case
        best = min(tallest(p) for p in brute_partitions(flat, case["n"]))
        assert tallest(case["items"]) == best, (case, best)
    # flowColumns on many seeded item lists, brute-forced.
    rng = random.Random(3)
    lists = [[rng.randint(1, 7) for _ in range(rng.randint(0, 11))] for _ in range(250)]
    flowed = await run_layout(
        page,
        "return args.lists.flatMap((items) => [2, 3].map((n) => layout.flowColumns(items, n)));",
        lists=lists,
    )
    cases = [(items, n) for items in lists for n in (2, 3)]
    assert len(flowed) == len(cases)
    for (items, n), got in zip(cases, flowed, strict=True):
        assert got == brute_flow(items, n), (items, n, got)


async def check_columns_ties(page):
    cases = [
        ([1, 1, 1, 1], 3),
        ([1, 1, 1, 1, 1], 3),
        ([1, 1], 3),
        ([1], 3),
        ([2, 1, 1, 2], 2),
        ([3, 1, 1, 1, 1, 1], 3),
        ([2, 2, 1, 1, 1], 3),
        ([], 3),
    ]
    flowed = await run_layout(
        page, "return args.cases.map(([items, n]) => layout.flowColumns(items, n));", cases=cases
    )
    for (items, n), got in zip(cases, flowed, strict=True):
        assert got == brute_flow(items, n), (items, n, got)
    assert flowed[0] == [[1, 1], [1, 1], []], flowed[0]
    # The same rule seen through a plan: four one-line items in three columns.
    plan_items = await run_layout(
        page,
        COLUMN_HELPERS + "return mainOf(await plan(args.doc)).columns.map((c) => c.items);",
        doc=column_doc(3, "а\nб\nв\nг"),
    )
    assert plan_items == [[1, 1], [1, 1], []], plan_items


async def check_columns_reading_order(page):
    checklist_text = "\n".join(
        ["Высушить пластик и очистить стол перед печатью", "Короткий", "", "Ещё"] * 3
    )
    docs = [
        column_doc(n, text, template=template, size=size)
        for n in (2, 3)
        for template, text, size in [
            ("note", COLUMN_TEXTS[0] + "\n" + COLUMN_TEXTS[1], 24),
            ("checklist", checklist_text, 24),
            ("checklist", checklist_text, 12),
        ]
    ]
    result = await run_layout(
        page,
        COLUMN_HELPERS
        + """
        const out = [];
        for (const doc of args.docs) {
          const block = mainOf(await plan(doc));
          const measure = await measureOf(doc);
          const width = block.columns[0].w - indentOf(block);
          const wrapped = layout.wrapText(measure, textOf(doc), width, { checklist: block.checklist });
          const expectedItems = [];
          for (const line of wrapped) {
            if (block.checklist && !line.head) expectedItems[expectedItems.length - 1] += 1;
            else expectedItems.push(1);
          }
          out.push({
            checklist: block.checklist,
            lines: block.columns.flatMap((c) => c.lines),
            expectedLines: wrapped.map((l) => l.text),
            blockLines: block.lines,
            checks: block.columns.flatMap((c) => c.checks),
            expectedChecks: wrapped.map((l) => l.check),
            items: block.columns.flatMap((c) => c.items),
            expectedItems,
            sums: block.columns.map((c) => [c.items.reduce((a, b) => a + b, 0), c.lines.length, c.widths.length]),
            paragraphs: textOf(doc).split('\\n').length,
          });
        }
        return out;
        """,
        docs=docs,
    )
    for case in result:
        assert case["lines"] == case["expectedLines"] == case["blockLines"], case
        assert case["checks"] == case["expectedChecks"], case
        assert case["items"] == case["expectedItems"], case
        assert all(a == b == c for a, b, c in case["sums"]), case
        if case["checklist"]:
            assert len(case["items"]) == case["paragraphs"], case
            assert max(case["items"]) > 1, case  # an item wraps, so atomicity is exercised
        else:
            assert set(case["items"]) == {1}, case


async def check_columns_full_width_blocks(page):
    storage = normalized(
        {
            "kind": "template",
            "template": "storage_label",
            "fields": {
                "title": "Заголовок",
                "location": "Полка 3",
                "text": "Строка один\nСтрока два\nСтрока три\nСтрока четыре",
                "qr": "https://example.com",
            },
            "options": {"align": "right"},
        }
    )
    short = normalized(
        {
            "kind": "template",
            "template": "note",
            "fields": {"title": "Т", "text": "коротко", "footer": "Подвал"},
        }
    )
    qr = normalized({"kind": "qr", "text": "https://example.com"})
    result = await run_layout(
        page,
        """
        const strip = (b) => { const { face, columns, ...rest } = b; return rest; };
        const out = {};
        for (const [name, doc] of Object.entries(args.docs)) {
          const one = await plan(doc.one, 21);
          const many = await plan(doc.many, 21);
          out[name] = {
            fields: many.blocks.map((b) => b.field),
            // Every block except the main text (its height may differ) is identical up to y.
            sameFullWidth: many.blocks.every((b, i) => b.field === 'text'
              || JSON.stringify({ ...strip(b), y: 0, text_y: 0 }) === JSON.stringify({ ...strip(one.blocks[i]), y: 0, text_y: 0 })),
            exact: JSON.stringify(many.blocks.map(strip)) === JSON.stringify(one.blocks.map(strip)),
            rowsDelta: many.rows - one.rows,
          };
        }
        return out;
        """,
        docs={
            "storage": {"one": set_columns(storage, 1), "many": set_columns(storage, 3)},
            "short": {"one": set_columns(short, 1), "many": set_columns(short, 3)},
            "qr": {"one": set_columns(qr, 1), "many": set_columns(qr, 3)},
        },
    )
    assert all(case["sameFullWidth"] for case in result.values()), result
    assert result["storage"]["fields"] == ["title", "location", "text", "qr"], result
    assert result["storage"]["rowsDelta"] < 0, result  # the text is shorter in three columns
    # One short line is one row either way: the whole plan, y included, is unchanged.
    assert result["short"]["exact"] and result["qr"]["exact"], result


async def check_columns_checklist_indent(page):
    text = "\n".join(["Высушить пластик перед печатью и очистить стол", "Один", "Два", "Три"] * 2)
    result = await run_layout(
        page,
        COLUMN_HELPERS
        + """
        const read = (x, y) => ctx.getImageData(x, y, 1, 1).data[3];
        const out = {};
        for (const doc of args.docs) {
          const p = await plan(doc);
          const block = mainOf(p);
          const measure = await measureOf(doc);
          ctx.clearRect(0, 0, 384, 2048);
          layout.drawPlan(ctx, p);
          const n = doc.options.columns;
          const xs = n > 1 ? block.columns.map((c) => c.x) : [10];
          const y = block.text_y;
          out[n] = {
            box: [block.x, block.w],
            xs,
            lines: n > 1 ? block.columns.flatMap((c) => c.lines) : block.lines,
            expected: layout.wrapText(measure, textOf(doc), n > 1 ? 154 : 340, { checklist: true }).map((l) => l.text),
            boxes: xs.map((x) => {
              const column = (from, to) => {
                let most = 0;
                for (let gx = x + from; gx < x + to; gx += 1) for (let gy = y; gy < y + block.step; gy += 1) most = Math.max(most, read(gx, gy));
                return most;
              };
              return { corners: [read(x, y + 7), read(x + 14, y + 21)], gap: column(16, 24), text: column(24, 44) };
            }),
          };
        }
        return out;
        """,
        docs=[column_doc(n, text, template="checklist") for n in (1, 2)],
    )
    one, two = result["1"], result["2"]
    assert one["box"] == [34, 340] and one["xs"] == [10], one
    assert two["xs"] == [10, 196], two
    for case in (one, two):
        assert case["lines"] == case["expected"] and len(case["lines"]) > 4, case
        for box in case["boxes"]:
            assert box["corners"] == [255, 255] and box["gap"] == 0 and box["text"] > 0, case


async def check_columns_short_text(page):
    result = await run_layout(
        page,
        COLUMN_HELPERS
        + """
        const block = mainOf(await plan(args.doc));
        return {
          lines: block.columns.map((c) => c.lines),
          items: block.columns.map((c) => c.items),
          x: block.columns.map((c) => c.x),
          h: block.h - block.step,
        };
        """,
        doc=column_doc(3, "один"),
    )
    assert result["lines"] == [["один"], [], []], result
    assert result["items"] == [[1], [], []] and result["x"] == [10, 134, 258], result
    assert result["h"] == 16, result


async def check_columns_one_is_golden(page):
    assert all(case["document"]["options"]["columns"] == 1 for case in CASES)
    cases = [c for c in CASES if not c["near_edge"] and c["name"] not in ASTRAL_WRAP_CASES]
    bad = await run_layout(page, GOLDEN_BODY, cases=cases, near_edge=False)
    assert not bad, bad[:8]
    columned = await run_layout(
        page,
        """
        let seen = 0;
        for (const c of args.cases) {
          const p = await plan(c.document, modulesOf(c));
          seen += p.blocks.filter((b) => 'columns' in b).length;
        }
        return seen;
        """,
        cases=cases,
    )
    assert columned == 0, columned


async def check_columns_alignment_and_gutter(page):
    aligned = [column_doc(2, "HI\nHI", align=align) for align in ("left", "center", "right")]
    packed = column_doc(3, "\n".join(["WWWW WWWW WWWW"] * 6))
    result = await run_layout(
        page,
        COLUMN_HELPERS
        + """
        const out = { aligned: [], gutters: [] };
        for (const doc of args.aligned) {
          const p = await plan(doc);
          const block = mainOf(p);
          ctx.clearRect(0, 0, 384, 2048);
          layout.drawPlan(ctx, p);
          for (const col of block.columns) {
            const w = col.widths[0];
            const start = { left: col.x, center: col.x + Math.round((col.w - w) / 2), right: col.x + col.w - Math.round(w) }[doc.options.align];
            out.aligned.push({ align: doc.options.align, start, col: [col.x, col.w], ink: ink(col.x, col.x + col.w + 4, block.text_y, block.text_y + block.step) });
          }
        }
        const p = await plan(args.packed);
        const block = mainOf(p);
        ctx.clearRect(0, 0, 384, 2048);
        layout.drawPlan(ctx, p);
        const tallest = Math.max(...block.columns.map((c) => c.lines.length));
        block.columns.forEach((col, i) => {
          const next = block.columns[i + 1];
          out.gutters.push({
            ink: ink(col.x, col.x + col.w + 1, block.text_y, block.text_y + tallest * block.step),
            gutter: next ? ink(col.x + col.w + 2, next.x - 2, block.text_y, block.text_y + tallest * block.step) : null,
            beyond: next ? null : ink(col.x + col.w + 2, 384, block.text_y, block.text_y + tallest * block.step),
          });
        });
        return out;
        """,
        aligned=aligned,
        packed=packed,
    )
    assert len(result["aligned"]) == 6, result
    for item in result["aligned"]:
        x, w = item["col"]
        left, right = item["ink"]
        assert abs(left - item["start"]) <= 4 and x <= left and right <= x + w + 2, item
    starts = {item["align"]: item["start"] - item["col"][0] for item in result["aligned"]}
    assert starts["left"] == 0 < starts["center"] < starts["right"], starts
    assert all(g["ink"] is not None and g["gutter"] is None for g in result["gutters"]), result
    assert result["gutters"][2]["beyond"] is None, result


async def check_columns_blank_slot(page):
    heading = normalized(
        {"kind": "template", "template": "heading", "fields": {"title": "Заголовок"}}
    )
    result = await run_layout(
        page,
        COLUMN_HELPERS
        + """
        const slot = (await plan(args.blank)).slots.find((s) => s.field === 'text');
        const block = mainOf(await plan(args.filled));
        return { slot, y: block.text_y, column: [block.columns[0].x, block.columns[0].w] };
        """,
        blank=set_columns(heading, 2),
        filled=set_columns({**heading, "fields": {**heading["fields"], "text": "x"}}, 2),
    )
    slot = result["slot"]
    assert [slot["x"], slot["w"]] == result["column"] == [10, 178], result
    assert slot["y"] == result["y"], result


# JSON cannot carry NaN or undefined: those two travel as labels and are built page-side.
COLUMN_VALUES = [
    ("1", 1),
    ("2", 2),
    ("3", 3),
    ("4", 4),
    ("0", 0),
    ("-1", -1),
    ("'2'", "2"),
    ("2.5", 2.5),
    ("null", None),
    ("true", True),
    ("NaN", None),
    ("undefined", None),
]


async def check_columns_invalid_count(page):
    result = await run_layout(
        page,
        COLUMN_HELPERS
        + """
        const outcomes = {};
        for (const [label, value] of args.values) {
          const doc = structuredClone(args.doc);
          if (label === 'NaN') doc.options.columns = NaN;
          else if (label === 'undefined') delete doc.options.columns;
          else doc.options.columns = value;
          try {
            const p = await plan(doc);
            outcomes[label] = { columns: mainOf(p).columns?.length ?? 1 };
          } catch (error) {
            outcomes[label] = { code: error.code, isRenderError: error instanceof RenderError };
          }
        }
        return outcomes;
        """,
        values=COLUMN_VALUES,
        doc=column_doc(1, "один\nдва\nтри\nчетыре"),
    )
    for count in (1, 2, 3):
        assert result[str(count)] == {"columns": count}, (count, result)
    for label, _ in COLUMN_VALUES[3:]:
        assert result[label] == {"code": "bad_document", "isRenderError": True}, (label, result)


async def check_columns_row_cap(page):
    size, lines = row_cap_boundary()
    result = await run_layout(
        page,
        COLUMN_HELPERS
        + """
        const outcome = async (columns, count) => {
          const doc = { ...args.doc, options: { ...args.doc.options, columns } };
          doc.fields = { text: Array(count).fill('строка').join('\\n') };
          try {
            const p = await plan(doc);
            const block = mainOf(p);
            return { rows: p.rows, tallest: Math.max(...(block.columns ?? [block]).map((c) => c.lines.length)) };
          } catch (error) {
            return { code: error.code };
          }
        };
        const out = {};
        for (const n of [2, 3]) out[n] = [await outcome(n, n * args.lines), await outcome(n, n * args.lines + 1)];
        out.single = await outcome(1, 3 * args.lines);
        return out;
        """,
        doc=normalized(
            {
                "kind": "template",
                "template": "note",
                "fields": {"text": "x"},
                "options": {"font_size": size},
            }
        ),
        lines=lines,
    )
    # The tallest column sets the height: n * lines items fill n columns to exactly 2048 rows, one
    # more makes the tallest column a line too tall, and the same items in one column are too tall.
    for n in ("2", "3"):
        assert result[n] == [{"rows": 2048, "tallest": lines}, {"code": "too_tall"}], (n, result)
    assert result["single"] == {"code": "too_tall"}, result


QR_LEVELS = {
    "M": qrcode.constants.ERROR_CORRECT_M,
    "Q": qrcode.constants.ERROR_CORRECT_Q,
    "H": qrcode.constants.ERROR_CORRECT_H,
}
# Page glue shared by the QR checks: the real encoder is the module-count callback of planLabel.
QR_PRELUDE = """
const qr = await import(args.qr);
const planQr = async (doc) => layout.planLabel(doc, await faceOf(doc.options), ctx, args.templates, qr.qrModules);
"""


def qr_doc(text: str, size: int = 320, ecc: str = "M") -> dict:
    return normalized(
        {"kind": "qr", "text": text, "options": {"qr_size": size, "qr_correction": ecc}}
    )


async def run_qr(page, body: str, **extra):
    return await run_layout(page, QR_PRELUDE + body, qr=QR_JS, vendor=VENDOR_QR_JS, **extra)


async def check_qr_golden(page):
    cases = [c for c in CASES if c["qr"]]
    assert len(cases) >= 12, len(cases)
    bad = await run_qr(
        page,
        """
        const out = [];
        for (const c of args.cases) {
          const p = await planQr(c.document);
          const blocks = p.blocks.filter((b) => b.kind === 'qr');
          const want = c.expect.blocks.filter((b) => b.kind === 'qr');
          if (p.rows !== c.expect.rows) out.push(`${c.name}: rows ${p.rows} != ${c.expect.rows}`);
          if (blocks.length !== want.length) { out.push(`${c.name}: qr blocks`); continue; }
          blocks.forEach((b, i) => {
            const e = want[i];
            if (b.modules !== e.modules) out.push(`${c.name}: modules ${b.modules} != ${e.modules}`);
            for (const k of ['x', 'y', 'w', 'h', 'text_y']) {
              if (b[k] !== e[k]) out.push(`${c.name}: ${k} ${b[k]} != ${e[k]}`);
            }
            const layer = qr.qrLayer(b.data, b.ecc, c.document.options.qr_size);
            if (layer.rows !== b.h || layer.modules !== b.modules) out.push(`${c.name}: layer ${layer.rows}/${layer.modules}`);
          });
        }
        return out;
        """,
        cases=cases,
    )
    assert not bad, bad[:8]


# Strings of the spike. Python `qrcode` splits mixed text into numeric/alphanumeric/byte segments,
# Nayuki's makeSegments picks one segment for the whole string, so these two pack different bit
# streams and cannot match module for module. Python's smaller version may not even hold the single
# segment, so the forced encode can raise "Data too long"; the code stays valid and decodable.
QR_MODULE_STRINGS = [
    "HELLO WORLD",
    "https://example.com/a?b=c&d=e",
    "Привет, мир!",
    "WIFI:T:WPA;S:HomeNet;P:s3cret-pass;;",
    "1234567890123456",
    "https://example.com/tinyprint?id=12345&x=Привет",
    "A" * 120,
    "BEGIN:VCARD\nVERSION:3.0\nFN:Иван Петров\nTEL:+79001234567\nEND:VCARD",
    "http://homeassistant.local:8123/api/webhook/abcdefghijklmnop",
    "ТЕСТ 123",
    "ABC123abc",
    "01234567890123456789012345678901234567890123456789",
    "ORDER 12345678901234567890123456789012345678901234567890 THANKS FOR SHOPPING",
    "ID:12345678901234567890123456789012345678901234567890 abc",
]
QR_SEGMENT_SPLIT = {s for s in QR_MODULE_STRINGS if s.startswith(("ORDER ", "ID:"))}


def python_symbol(text: str, ecc: str, version=None, mask=None) -> qrcode.QRCode:
    code = qrcode.QRCode(
        version=version,
        error_correction=QR_LEVELS[ecc],
        box_size=1,
        border=0,
        mask_pattern=mask,
    )
    code.add_data(text)
    code.make(fit=version is None)
    return code


async def check_qr_modules_equal_python(page):
    cases = []
    for text in QR_MODULE_STRINGS:
        for ecc in QR_LEVELS:
            auto = python_symbol(text, ecc)
            # qrcode does not report the mask it chose: find the forced mask that reproduces it.
            mask = next(
                m
                for m in range(8)
                if python_symbol(text, ecc, auto.version, m).modules == auto.modules
            )
            cases.append(
                {
                    "text": text,
                    "ecc": ecc,
                    "version": auto.version,
                    "mask": mask,
                    "segments": len(auto.data_list),
                    "rows": [[int(cell) for cell in row] for row in auto.modules],
                }
            )
    # The split strings really are multi-segment in Python, the others are not.
    for case in cases:
        assert (case["segments"] > 1) == (case["text"] in QR_SEGMENT_SPLIT), case["text"]
    result = await run_qr(
        page,
        """
        const { default: qrcodegen } = await import(args.vendor);
        const { QrCode, QrSegment } = qrcodegen;
        const levels = { M: QrCode.Ecc.MEDIUM, Q: QrCode.Ecc.QUARTILE, H: QrCode.Ecc.HIGH };
        return args.cases.map((c) => {
          let code;
          try {
            code = QrCode.encodeSegments(
              QrSegment.makeSegments(c.text), levels[c.ecc], c.version, c.version, c.mask, false);
          } catch (e) {
            return { error: e.message };
          }
          const rows = [];
          for (let y = 0; y < code.size; y += 1) {
            rows.push(Array.from({ length: code.size }, (_, x) => (code.getModule(x, y) ? 1 : 0)));
          }
          return { same: JSON.stringify(rows) === JSON.stringify(c.rows), version: code.version };
        });
        """,
        cases=cases,
    )
    for case, got in zip(cases, result, strict=True):
        if case["text"] in QR_SEGMENT_SPLIT:
            continue
        assert "error" not in got, (case["text"], got)
        assert got["same"], (case["text"], case["ecc"], case["version"], case["mask"])
    compared = sum(1 for case in cases if case["text"] not in QR_SEGMENT_SPLIT)
    assert len(QR_SEGMENT_SPLIT) == 2 and compared == 12 * len(QR_LEVELS)


async def check_qr_cache(page):
    result = await run_qr(
        page,
        """
        const a = qr.qrLayer('https://example.com/a', 'M', 320);
        const same = qr.qrLayer('https://example.com/a', 'M', 320);
        const text = qr.qrLayer('https://example.com/b', 'M', 320);
        const ecc = qr.qrLayer('https://example.com/a', 'Q', 320);
        const size = qr.qrLayer('https://example.com/a', 'M', 300);
        return { same: a === same, text: a !== text, ecc: a !== ecc, size: a !== size };
        """,
    )
    assert result == {"same": True, "text": True, "ecc": True, "size": True}, result


async def check_qr_decodes(page):
    import cv2  # noqa: PLC0415 - only this check needs OpenCV
    import numpy as np  # noqa: PLC0415
    from PIL import Image  # noqa: PLC0415

    texts = [
        "https://example.com/spool/42",
        "Катушка №42 — PETG",
        "".join(f"{n:03d}-" for n in range(30))[:120],
    ]
    docs = [qr_doc(text, 320, ecc) for text in texts for ecc in QR_LEVELS]
    result = await run_qr(
        page,
        """
        const pack = await import(args.pack);
        return Promise.all(args.docs.map(async (doc) => {
          const plan = await planQr(doc);
          ctx.clearRect(0, 0, 384, 2048);
          layout.drawPlan(ctx, plan);
          const { data } = ctx.getImageData(0, 0, 384, plan.rows);
          return { rows: plan.rows, bytes: Array.from(pack.packAlpha(data, 384, plan.rows)) };
        }));
        """,
        docs=docs,
        pack=PACK,
    )
    # The classic detector misses some valid symbols that differ only in white margin (qrcode's own
    # version-7 Q image fails at one padding and decodes at the next), so the ArUco-based detector
    # of the same OpenCV module is the second opinion; both read the packed bytes, not the canvas.
    detectors = [cv2.QRCodeDetector(), cv2.QRCodeDetectorAruco()]
    for doc, shot in zip(docs, result, strict=True):
        bits = np.unpackbits(
            np.array(shot["bytes"], dtype=np.uint8).reshape(shot["rows"], 48), axis=1
        )
        image = np.array(Image.fromarray(np.where(bits == 1, 0, 255).astype(np.uint8)))
        decoded = next((text for d in detectors if (text := d.detectAndDecode(image)[0])), "")
        assert decoded == doc["text"], (doc["text"][:20], doc["options"]["qr_correction"], decoded)


async def check_qr_scaling(page):
    result = await run_qr(
        page,
        """
        const pack = await import(args.pack);
        const out = [];
        for (const [text, size, ecc] of args.cases) {
          const layer = qr.qrLayer(text, ecc, size);
          const scale = layer.rows / (layer.modules + 8);
          const left = Math.floor((384 - layer.rows) / 2);
          let margin = 0;
          const at = (x, y) => layer.ink[y * 384 + x];
          for (let i = 0; i < layer.rows; i += 1) {
            for (let j = 0; j < 4 * scale; j += 1) {
              margin += at(left + j, i) + at(left + layer.rows - 1 - j, i)
                + at(left + i, j) + at(left + i, layer.rows - 1 - j);
            }
          }
          let outside = 0;
          for (let y = 0; y < layer.rows; y += 1) {
            for (let x = 0; x < 384; x += 1) {
              if ((x < left || x >= left + layer.rows) && at(x, y)) outside += 1;
            }
          }
          const plan = await planQr({ ...args.doc, text, options: { ...args.doc.options, qr_size: size, qr_correction: ecc } });
          ctx.clearRect(0, 0, 384, 2048);
          layout.drawPlan(ctx, plan);
          const { data } = ctx.getImageData(0, plan.blocks[0].y, 384, layer.rows);
          const drawn = pack.packAlpha(data, 384, layer.rows);
          const expected = pack.packInk(layer.ink, layer.rows);
          out.push({
            scale, integer: Number.isInteger(scale), rows: layer.rows, modules: layer.modules,
            width: layer.width, margin, outside, drawnEqual: drawn.every((v, i) => v === expected[i]),
          });
        }
        return out;
        """,
        cases=[["https://example.com/spool/42", 320, "M"], ["x" * 120, 360, "H"], ["a", 96, "Q"]],
        doc=qr_doc("x"),
        pack=PACK,
    )
    for got in result:
        assert got["integer"] and got["scale"] >= 3, got
        assert got["rows"] == (got["modules"] + 8) * got["scale"] and got["width"] == 384, got
        assert got["margin"] == 0 and got["outside"] == 0 and got["drawnEqual"], got


async def check_qr_too_long(page):
    result = await run_qr(
        page,
        """
        const caught = [];
        for (const run of [
          () => qr.qrLayer(args.text, 'H', 360),
          () => qr.qrModules(args.text, 'H'),
          () => layout.planLabel(args.doc, null, ctx, args.templates, qr.qrModules),
        ]) {
          try { run(); caught.push('no error'); } catch (e) { caught.push([e.name, e.code]); }
        }
        const long = qr.qrModules('я'.repeat(500), 'H');
        return { caught, long };
        """,
        text="x" * 3000,
        doc=qr_doc("x" * 3000, 360, "H"),
    )
    assert result["caught"] == [["RenderError", "qr_too_long"]] * 3, result
    assert result["long"] > 100, result


async def check_qr_too_small(page):
    result = await run_qr(
        page,
        """
        const code = (run) => { try { run(); return null; } catch (e) { return e.code; } };
        return [
          code(() => qr.qrLayer('x'.repeat(500), 'M', 96)),
          code(() => layout.planLabel(args.big, null, ctx, args.templates, qr.qrModules)),
          code(() => qr.qrLayer('   ', 'M', 320)),
          code(() => qr.qrLayer('', 'M', 320)),
          code(() => qr.qrModules('\\n\\t ', 'M')),
          code(() => qr.qrModules('\\ud83d', 'M')),
          code(() => qr.qrModules('ab\\udc00', 'M')),
          code(() => qr.qrModules('a', 'constructor')),
          code(() => qr.qrLayer('a', 'M', 1000)),
          code(() => qr.qrLayer('a', 'M', undefined)),
          code(() => layout.planLabel(args.huge, null, ctx, args.templates, qr.qrModules)),
        ];
        """,
        big=qr_doc("x" * 500, 96, "M"),
        # Studio-only size: Python's normalize_options would reject it, the browser must not draw it.
        huge={**qr_doc("a"), "options": {**qr_doc("a")["options"], "qr_size": 1000}},
    )
    assert result == [
        "qr_modules_too_small",
        "qr_modules_too_small",
        "blank_qr",
        "blank_qr",
        "blank_qr",
        "bad_document",
        "bad_document",
        "bad_document",
        "bad_document",
        "qr_modules_too_small",
        "bad_document",
    ], result


def png_bytes(image: Image.Image) -> list[int]:
    out = io.BytesIO()
    image.save(out, "PNG")
    return list(out.getvalue())


def gradient_image(width: int, height: int) -> Image.Image:
    rng = random.Random(5)
    image = Image.new("L", (width, height))
    image.putdata(
        [
            min(255, max(0, x * 255 // (width - 1) + rng.randrange(-20, 21)))
            for _ in range(height)
            for x in range(width)
        ]
    )
    return image


def image_options(**overrides) -> dict:
    return {
        "rotate": 0,
        "width": 384,
        "fit": "width",
        "brightness": 1,
        "contrast": 1,
        "sharpness": 1,
        "invert": False,
        "method": "threshold",
        "threshold": 128,
    } | overrides


IMAGE_PRELUDE = """
const m = await import(args.url);
const pack = await import(args.pack);
const bytes = (list) => new Blob([Uint8Array.from(list)]);
const decode = async (list) => m.decodeUpload(bytes(list));
const run = async (list, opts) => m.processImage(await decode(list), opts);
const inkCount = (r) => r.ink.reduce((a, b) => a + b, 0);
const code = async (fn) => { try { await fn(); return null; } catch (e) { return e.code ?? String(e); } };
"""


async def run_image(page, body: str, **extra):
    return await page.evaluate(
        "async (args) => {" + IMAGE_PRELUDE + body + "}",
        {"url": IMAGE_JS, "pack": PACK, **extra},
    )


async def check_image_dither_exact(page):
    """No resize happens (fit original, width == source width): Pillow's bits must be reproduced."""
    source = gradient_image(100, 60)
    data = png_bytes(source)
    plain = {"brightness": 1.0, "contrast": 1.0, "sharpness": 1.0}
    cases = [
        {**plain, "method": "threshold", "threshold": 100},
        {**plain, "method": "threshold", "threshold": 180},
        {**plain, "method": "floyd_steinberg", "threshold": 90},
        {**plain, "method": "floyd_steinberg", "threshold": 128},
        {**plain, "method": "ordered", "threshold": 140},
        {**plain, "method": "ordered", "threshold": 128},
        {**plain, "method": "ordered", "threshold": 128, "invert": True},
        {**plain, "method": "floyd_steinberg", "threshold": 128, "invert": True},
    ]
    for preset in ("photo", "line_art", "logo", "soft"):
        cases.append(
            {
                k: v
                for k, v in STUDIO.normalize_options({"preset": preset}).items()
                if k in image_options()
            }
            | {"width": 100, "fit": "original", "rotate": 0}
        )
    b64 = base64.b64encode(bytes(data)).decode()
    expected = []
    for case in cases:
        options = image_options(**case) | {"width": 100, "fit": "original"}
        bits, rows = STUDIO.bitmap(
            STUDIO.render_document({"kind": "image", "image_base64": b64, "options": options})
        )
        expected.append((list(bits), rows))
    result = await run_image(
        page,
        """
        const out = [];
        const src = await decode(args.data);
        for (const c of args.cases) {
          const r = m.processImage(src, c);
          out.push([Array.from(pack.packInk(r.ink, r.rows)), r.rows]);
        }
        return out;
        """,
        data=data,
        cases=[image_options(**c) | {"width": 100, "fit": "original"} for c in cases],
    )
    for i, (got, want) in enumerate(zip(result, expected, strict=True)):
        assert got[1] == want[1], (i, cases[i], got[1], want[1])
        diff = sum(a != b for a, b in zip(got[0], want[0], strict=True))
        assert diff == 0, (i, cases[i], diff)


async def check_image_flat_gray(page):
    data = png_bytes(Image.new("L", (100, 100), 128))
    result = await run_image(
        page,
        """
        const out = {};
        for (const method of ['floyd_steinberg', 'ordered']) {
          const r = await run(args.data, { ...args.opts, method, width: 100, fit: 'original' });
          out[method] = inkCount(r) / (100 * 100);
        }
        return out;
        """,
        data=data,
        opts=image_options(),
    )
    for method, share in result.items():
        assert 0.45 <= share <= 0.55, (method, share)


async def check_image_threshold_monotonic(page):
    data = png_bytes(gradient_image(360, 240))
    result = await run_image(
        page,
        """
        const src = await decode(args.data);
        const out = [];
        for (let t = 32; t <= 224; t += 32) out.push(inkCount(m.processImage(src, { ...args.opts, threshold: t })));
        return out;
        """,
        data=data,
        opts=image_options(),
    )
    assert result == sorted(result) and result[0] < result[-1], result


async def check_image_transparent(page):
    data = png_bytes(Image.new("RGBA", (64, 64), (0, 0, 0, 0)))
    result = await run_image(
        page, "return inkCount(await run(args.data, args.opts));", data=data, opts=image_options()
    )
    assert result == 0, result


def exif_jpeg() -> list[int]:
    image = Image.new("RGB", (100, 50), (0, 0, 0))
    exif = Image.Exif()
    exif[0x0112] = 6
    out = io.BytesIO()
    image.save(out, "JPEG", exif=exif, quality=95)
    return list(out.getvalue())


async def check_image_exif(page):
    data = exif_jpeg()
    result = await run_image(
        page,
        """
        const columns = (r) => {
          const used = new Set();
          for (let i = 0; i < r.ink.length; i += 1) if (r.ink[i]) used.add(i % 384);
          return [Math.min(...used), Math.max(...used)];
        };
        const fit = await run(args.data, args.opts);
        const original = await run(args.data, { ...args.opts, fit: 'original' });
        return { fit: fit.rows, original: original.rows, span: columns(original) };
        """,
        data=data,
        opts=image_options(),
    )
    assert result["fit"] == 768, result
    # The stored 100x50 is shown as 50x100: 100 rows, 50 columns centred on the 384 dots.
    assert result["original"] == 100 and result["span"] == [167, 216], result


async def check_image_rotation(page):
    image = Image.new("RGB", (40, 20), "white")
    image.putpixel((0, 0), (0, 0, 0))
    data = png_bytes(image)
    result = await run_image(
        page,
        """
        const where = (r) => {
          const i = r.ink.indexOf(1);
          return [i % 384, Math.floor(i / 384), inkCount(r), r.rows];
        };
        const out = {};
        for (const rotate of [0, 90, 180, 270]) {
          out[rotate] = where(await run(args.data, { ...args.opts, rotate, fit: 'original' }));
        }
        return out;
        """,
        data=data,
        opts=image_options(),
    )
    # 40x20 is centred at x0 = 172, 20x40 at x0 = 182.
    assert result["0"] == [172, 0, 1, 20], result
    assert result["90"] == [182 + 19, 0, 1, 40], result
    assert result["180"] == [172 + 39, 19, 1, 20], result
    assert result["270"] == [182, 39, 1, 40], result


async def check_image_invert(page):
    data = png_bytes(Image.new("L", (64, 64), 0))
    result = await run_image(
        page,
        """
        return [inkCount(await run(args.data, args.opts)), inkCount(await run(args.data, { ...args.opts, invert: true }))];
        """,
        data=data,
        opts=image_options(fit="original"),
    )
    assert result == [64 * 64, 0], result


async def check_image_upload_cap(page):
    result = await run_image(
        page,
        """
        let calls = 0;
        const real = window.createImageBitmap;
        window.createImageBitmap = (...a) => { calls += 1; return real(...a); };
        const big = new Blob([new Uint8Array(2 * 1024 * 1024 + 1)]);
        const first = await code(() => m.decodeUpload(big));
        const countAfterBig = calls;
        window.createImageBitmap = real;
        return [first, countAfterBig];
        """,
    )
    assert result == ["image_too_large", 0], result


async def check_image_limits(page):
    result = await run_image(
        page,
        """
        return [
          await code(() => decode(args.huge)),
          await code(() => run(args.tall, args.opts)),
          await code(() => decode(args.junk)),
          await code(() => decode([])),
        ];
        """,
        huge=png_bytes(Image.new("L", (2001, 2001), 255)),
        tall=png_bytes(Image.new("L", (50, 1000), 255)),
        junk=list(b"this is not an image" * 8),
        opts=image_options(),
    )
    assert result == ["image_too_large", "too_tall", "image_unreadable", "image_unreadable"], result


async def check_image_cache(page):
    data = png_bytes(gradient_image(200, 100))
    result = await run_image(
        page,
        """
        const src = await decode(args.data);
        const layer = (o) => m.processImage(src, { ...args.opts, ...o }).layer;
        const out = [];
        out.push(layer({}));
        out.push(layer({ threshold: 90 }));
        out.push(layer({ method: 'ordered', brightness: 1.3, invert: true }));
        out.push(layer({ width: 300 }));
        out.push(layer({ rotate: 90 }));
        out.push(layer({ fit: 'original' }));
        out.push(layer({}));
        // Bounded: more distinct layers than the LRU holds evicts the oldest.
        for (const width of [101, 102, 103, 104, 105, 106]) layer({ width });
        out.push(layer({ width: 101 }));
        out.push(layer({ width: 106 }));
        return out;
        """,
        data=data,
        opts=image_options(),
    )
    assert result == ["miss", "hit", "hit", "miss", "miss", "miss", "hit", "miss", "hit"], result


async def check_image_presets(page):
    data = png_bytes(gradient_image(360, 240))
    keys = image_options().keys()
    sets = {
        name: {k: v for k, v in STUDIO.normalize_options({"preset": name}).items() if k in keys}
        for name in ("photo", "line_art")
    }
    result = await run_image(
        page,
        """
        const src = await decode(args.data);
        const bits = (o) => { const r = m.processImage(src, o); return Array.from(pack.packInk(r.ink, r.rows)).join(); };
        return [bits(args.photo), bits(args.line_art)];
        """,
        data=data,
        photo=sets["photo"],
        line_art=sets["line_art"],
    )
    assert result[0] != result[1], "photo and line_art produced the same bitmap"


CHECKS = [
    ("pack: bit order and alpha threshold", check_pack_bits),
    ("pack: bytesToBase64 round-trips 98,304 bytes without a RangeError", check_base64),
    ("pack: unpack(pack(b)) == b, inkPercent and target reuse", check_roundtrip),
    ("fonts: width and weight axes change advance widths", check_axes),
    ("fonts: hhea metrics match the Pillow golden and Tiny5 scales", check_metrics_and_scale),
    ("fonts: LRU keeps 8 faces and removes evicted ones from document.fonts", check_lru),
    ("fonts: missing file fails loudly", check_missing),
    ("fonts: unreadable font leaves no face and is refetched", check_corrupt_font),
    (
        "fonts: a face that settles last under concurrency stays registered",
        check_concurrent_settle_order,
    ),
    ("layout: derived sizes equal the Python table", check_derived_sizes),
    ("layout: golden cases", check_golden_cases),
    ("layout: near-edge cases", check_near_edge_cases),
    (
        "layout: emoji, tab, combining marks and a 600-char token do not crash",
        check_unusual_text,
    ),
    (
        "layout: emoji and combining marks wrap whole at the line boundary",
        check_wrap_boundaries,
    ),
    ("layout: 2048 rows plan and 2049 raises too_tall", check_row_cap_boundary),
    ("layout: more than 2048 rows raises too_tall", check_too_tall),
    ("layout: checkbox and border pixels follow the Python rectangles", check_pixels),
    ("layout: Tiny5 width 200 measures twice width 100", check_tiny5_scale),
    ("layout: a probe overflow drops the slot only", check_probe_overflow),
    (
        "layout: a blank field has a slot whose y equals the block it will become",
        check_blank_slot,
    ),
    (
        "layout: the spool anchor counts the colon of the wrapped value line",
        check_spool_anchor_heuristic,
    ),
    ("layout: slots exist for QR and spool rows, never for plain documents", check_slot_kinds),
    ("columns: widths and positions", check_columns_geometry),
    ("columns: no line is wider than its column", check_columns_no_line_too_wide),
    (
        "columns: one glyph per line when two glyphs do not fit a column",
        check_columns_extreme_glyphs,
    ),
    ("columns: tallest column is minimal", check_columns_tallest_minimal),
    ("columns: ties pick the lexicographically largest partition", check_columns_ties),
    (
        "columns: reading order preserved and checklist items atomic",
        check_columns_reading_order,
    ),
    ("columns: full-width blocks are unchanged", check_columns_full_width_blocks),
    ("columns: checklist indent is 24 relative to the column edge", check_columns_checklist_indent),
    (
        "columns: a single short line with 3 columns yields one non-empty column",
        check_columns_short_text,
    ),
    ("columns: 1 equals the golden fixture", check_columns_one_is_golden),
    (
        "columns: alignment is relative to the column and the gutters stay clear",
        check_columns_alignment_and_gutter,
    ),
    ("columns: a blank main text slot sits in the first column", check_columns_blank_slot),
    ("columns: only the integers 1, 2 and 3 are accepted", check_columns_invalid_count),
    ("columns: the row cap applies to the tallest column", check_columns_row_cap),
    ("qr: golden QR cases", check_qr_golden),
    (
        "qr: modules equal Python qrcode when version and mask are forced",
        check_qr_modules_equal_python,
    ),
    ("qr: unchanged data is not re-encoded", check_qr_cache),
    ("qr: decodes with OpenCV", check_qr_decodes),
    ("qr: integer module scaling and quiet zone", check_qr_scaling),
    ("qr: data beyond version-40 capacity raises qr_too_long", check_qr_too_long),
    ("qr: too small modules raise", check_qr_too_small),
    (
        "image: threshold, dithers and enhancement equal Pillow without resizing",
        check_image_dither_exact,
    ),
    ("image: flat mid-gray dithers to about half ink", check_image_flat_gray),
    ("image: threshold ink is non-decreasing in the threshold", check_image_threshold_monotonic),
    ("image: transparent pixels become white", check_image_transparent),
    ("image: EXIF orientation is applied", check_image_exif),
    ("image: rotation is clockwise", check_image_rotation),
    ("image: invert turns a black square white", check_image_invert),
    ("image: an upload over 2 MiB is rejected before decoding", check_image_upload_cap),
    ("image: pixel, height and decode limits", check_image_limits),
    ("image: resized layer is cached and the cache is bounded", check_image_cache),
    ("image: presets change the bitmap", check_image_presets),
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
