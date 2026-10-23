"""Browser checks for the renderer modules, run once per browser in TINYPRINT_BROWSERS.

Run: python tests/browser_renderer.py
"""

import asyncio
import base64
import functools
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
RENDERER_JS = f"{STATIC}renderer/index.js"
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


RENDERER_PRELUDE = """
const r = await import(args.index);
const pack = await import(args.pack);
const renderer = new r.Renderer({ baseUrl: new URL(args.base), fonts: args.fonts_table, templates: args.templates });
const go = (document, extra = {}) =>
  renderer.render({ document, image: null, ghost: false, approximate: false, generation: 1, ...extra });
const code = async (fn) => {
  try { await fn(); return null; } catch (e) { return e instanceof r.RenderError ? e.code : 'NOT_RENDER_ERROR:' + e; }
};
const same = (a, b) => a.length === b.length && a.every((v, i) => v === b[i]);
const opt = (o) => ({ kind: 'text', text: args.sample, options: o });
"""
SAMPLE = "Привет, мир! Широкий текст для проверки ползунков 0123456789"


def renderer_args(**extra):
    return {
        "index": RENDERER_JS,
        "pack": PACK,
        "base": STATIC,
        "fonts_table": FONTS,
        "templates": STUDIO.TEMPLATES,
        "sample": SAMPLE,
        **extra,
    }


async def run_renderer(page, body: str, **extra):
    script = (
        "async (args) => {"
        + RENDERER_PRELUDE
        + "try {"
        + body
        + "} finally { renderer.dispose(); }}"
    )
    return await page.evaluate(script, renderer_args(**extra))


NOTE = {
    "kind": "template",
    "template": "note",
    "fields": {"title": "Заголовок", "text": "Привет, мир\nвторая строка", "footer": "низ"},
    "options": {},
}


async def check_renderer_deterministic(page):
    result = await run_renderer(
        page,
        """
        const a = await go(args.note);
        const b = await go(args.note);
        const view = pack.unpack(a.bitmap, a.rows);
        return {
          same: same(a.bitmap, b.bitmap),
          pixels: same(a.pixels.data, view.data),
          size: [a.pixels.width, a.pixels.height, a.rows, a.bitmap.length],
          exact: [a.exact, a.ghost], gen: a.generation, face: a.face.family,
          ink: a.inkPercent, doc: a.document.options.columns,
        };
        """,
        note=NOTE,
    )
    assert result["same"] and result["pixels"], result
    assert result["size"][0] == 384 and result["size"][1] == result["size"][2], result
    assert result["size"][3] == 48 * result["size"][2], result
    assert result["exact"] == [True, False] and result["gen"] == 1, result
    assert result["ink"] > 0 and result["doc"] == 1, result


async def check_renderer_kinds(page):
    png = png_bytes(gradient_image(120, 80))
    result = await run_renderer(
        page,
        """
        const image = await (await import(args.image_js)).decodeUpload(new Blob([Uint8Array.from(args.png)]));
        const docs = {
          template: args.note,
          text: opt({}),
          qr: { kind: 'qr', text: 'https://example.com', options: { qr_size: 200 } },
          image: { kind: 'image', options: { fit: 'width', method: 'threshold' } },
        };
        const out = {};
        for (const [name, doc] of Object.entries(docs)) {
          const res = await go(doc, { image: { id: image.id, bitmap: image.bitmap } });
          out[name] = [res.rows, res.bitmap.length === 48 * res.rows, res.inkPercent > 0,
                       res.face === null, res.blocks.length, res.document.kind];
        }
        out.noImage = await code(() => go({ kind: 'image', options: {} }));
        out.noId = await code(() => go(docs.image, { image: { bitmap: image.bitmap } }));
        const dim = await go(docs.image, { image: { id: image.id, bitmap: image.bitmap }, ghost: true });
        const levels = new Set();
        for (let i = 0; i < dim.pixels.data.length; i += 4) levels.add(dim.pixels.data[i] + ':' + dim.pixels.data[i + 3]);
        out.imageGhost = [dim.bitmap, dim.document, dim.ghost, dim.exact, [...levels].sort()];
        return out;
        """,
        note=NOTE,
        png=png,
        image_js=IMAGE_JS,
    )
    for name in ("template", "text", "qr", "image"):
        rows, sized, inked, _, _, kind = result[name]
        assert rows > 0 and sized and inked and kind == name, (name, result[name])
    assert result["template"][3] is False and result["template"][4] == 3, result
    assert result["qr"][3] and result["image"][3] and result["image"][4] == 0, result
    assert result["noImage"] == result["noId"] == "bad_document", result
    bitmap, document, ghost, exact, levels = result["imageGhost"]
    assert bitmap is None and document is None and ghost and not exact, result
    assert levels and all(level.endswith(":255") for level in levels), levels
    assert len(levels) == 2 and "255:255" in levels, levels


async def check_renderer_unknown_template(page):
    result = await run_renderer(
        page,
        """
        return [
          await code(() => go({ kind: 'template', template: 'nope', fields: {}, options: {} })),
          await code(() => go({ kind: 'template', template: '__proto__', fields: {}, options: {} })),
        ];
        """,
    )
    assert result == ["bad_document", "bad_document"], result


async def check_renderer_ghost(page):
    example = STUDIO.TEMPLATES["note"]["example"]
    result = await run_renderer(
        page,
        """
        const doc = { kind: 'template', template: 'note', fields: args.example, options: {} };
        const g = await go(doc, { ghost: true });
        const a = new Set();
        for (let i = 0; i < g.pixels.data.length; i += 4) {
          if (g.pixels.data[i + 3] !== 255) return { opaque: false };
          a.add(g.pixels.data[i]);
        }
        const real = await go(doc);
        const again = await go(doc);
        return {
          bitmap: g.bitmap, ghost: g.ghost, exact: g.exact, doc: g.document, rows: g.rows,
          height: g.pixels.height, keys: Object.keys(g), grays: [...a].sort((x, y) => x - y),
          slots: g.slots.length, blocks: g.blocks.length, realRows: real.rows, afterGhost: same(real.bitmap, again.bitmap) && real.inkPercent > 1,
        };
        """,
        example=example,
    )
    assert result["bitmap"] is None and result["ghost"] is True and result["exact"] is False, result
    assert result["doc"] is None and result["height"] == result["rows"] == result["realRows"], (
        result
    )
    assert "bytes" not in result["keys"] and "packed" not in result["keys"], result
    # Dimmed example text: opaque white page, a gray that is neither pure ink nor paper.
    assert 255 in result["grays"] and 0 not in result["grays"], result
    assert any(0 < g < 255 for g in result["grays"]), result
    assert result["blocks"] == 3 and result["afterGhost"], result


async def check_renderer_ghost_wins_over_approximate(page):
    example = STUDIO.TEMPLATES["note"]["example"]
    result = await run_renderer(
        page,
        """
        const doc = { kind: 'template', template: 'note', fields: args.example,
                      options: { font_width: 37, font_weight: 480 } };
        const g = await go(doc, { ghost: true, approximate: true });
        return { bitmap: g.bitmap, ghost: g.ghost, exact: g.exact, family: g.face.family, keys: Object.keys(g) };
        """,
        example=example,
    )
    assert result["bitmap"] is None and result["ghost"] and result["exact"] is False, result
    # Ghost ignores `approximate`: the document is drawn as given, not snapped.
    assert result["family"] == "tp-roboto_flex-w37-W480", result
    assert "bytes" not in result["keys"] and "packed" not in result["keys"], result


async def check_renderer_approximate(page):
    result = await run_renderer(
        page,
        """
        const at = (w, wt) => opt({ font_width: w, font_weight: wt });
        const approx = await go(at(37, 480), { approximate: true });
        const snapped = await go(at(35, 500));
        const exact = await go(at(37, 480));
        const onGrid = await go(at(35, 500), { approximate: true });
        return {
          equal: same(approx.bitmap, snapped.bitmap),
          differs: !same(exact.bitmap, snapped.bitmap),
          flags: [approx.exact, snapped.exact, exact.exact, onGrid.exact],
          used: [approx.snapped.width, approx.snapped.weight],
          onGridUsed: [onGrid.snapped.width, onGrid.snapped.weight],
          approxDocuments: [approx.document, onGrid.document],
          exactSnapped: [exact.snapped, snapped.snapped],
          requested: [exact.document.options.font_width, exact.document.options.font_weight],
          bitmaps: [approx.bitmap instanceof Uint8Array, onGrid.bitmap instanceof Uint8Array],
          keys: Object.keys(approx),
          snap: [r.snapToGrid(475, 50, 100, 1000), r.snapToGrid(474, 50, 100, 1000),
                 r.snapToGrid(0, 5, 25, 151), r.snapToGrid(999, 50, 100, 1000),
                 r.snapToGrid(151, 5, 25, 151), r.snapToGrid(26, 5, 25, 151), r.snapToGrid(37, 5, 25, 151)],
          steps: [r.APPROX_WIDTH_STEP, r.APPROX_WEIGHT_STEP],
        };
        """,
    )
    assert result["equal"] and result["differs"], result
    assert result["flags"] == [False, True, True, False], result
    assert result["used"] == [35, 500] and result["requested"] == [37, 480], result
    assert result["onGridUsed"] == [35, 500] and result["bitmaps"] == [True, True], result
    # R15: nothing a recipe could be built from leaves an inexact render.
    assert result["approxDocuments"] == [None, None], result
    assert result["exactSnapped"] == [None, None], result
    assert "bytes" not in result["keys"] and "packed" not in result["keys"], result
    assert result["snap"] == [500, 450, 25, 1000, 150, 25, 35], result
    assert result["steps"] == [5, 50], result


async def check_renderer_sliders(page):
    result = await run_renderer(
        page,
        """
        const cases = [[25, 400], [100, 400], [151, 400], [100, 100], [100, 1000]];
        const out = [];
        for (const [w, wt] of cases) out.push(await go(opt({ font_width: w, font_weight: wt })));
        const pairs = [];
        for (let i = 0; i < out.length; i += 1) {
          for (let j = i + 1; j < out.length; j += 1) {
            if (out[i].rows === out[j].rows && same(out[i].bitmap, out[j].bitmap)) pairs.push([i, j]);
          }
        }
        return { pairs, light: out[3].inkPercent, heavy: out[4].inkPercent };
        """,
    )
    assert result["pairs"] == [], result
    assert result["heavy"] > result["light"], result


async def check_renderer_tiny5_crisp(page):
    result = await run_renderer(
        page,
        """
        const runs = (data, w, h) => {
          const lengths = new Set();
          let bad = 0;
          for (let y = 0; y < h; y += 1) {
            let run = 0;
            for (let x = 0; x <= w; x += 1) {
              const a = x < w ? data[(y * w + x) * 4 + 3] : 0;
              if (a !== 0 && a !== 255) bad += 1;
              if (a > 0) run += 1;
              else if (run) { lengths.add(run); run = 0; }
            }
          }
          return { lengths: [...lengths], bad };
        };
        const out = {};
        for (const size of [8, 16, 24]) {
          const face = await renderer.faceFor({ font: 'tiny5', font_size: size, font_width: 100, font_weight: 400 });
          const canvas = new OffscreenCanvas(384, 80);
          const ctx = canvas.getContext('2d', { alpha: true, willReadFrequently: true });
          ctx.textRendering = 'geometricPrecision';
          ctx.font = face.css(size);
          ctx.fillText('Привет', 4, face.ascent(size));
          const found = runs(ctx.getImageData(0, 0, 384, 80).data, 384, 80);
          out[size] = { ...found, ink: found.lengths.length > 0 };
        }
        const wide = await go({ kind: 'text', text: 'Привет', options: { font: 'tiny5', font_size: 16, font_width: 200 } });
        const canvas = new OffscreenCanvas(384, wide.rows);
        const ctx = canvas.getContext('2d', { alpha: true, willReadFrequently: true });
        ctx.textRendering = 'geometricPrecision';
        (await import(args.layout)).drawPlan(ctx, { blocks: wide.blocks, border: false, rows: wide.rows });
        const alpha = runs(ctx.getImageData(0, 0, 384, wide.rows).data, 384, wide.rows);
        const shown = runs(wide.pixels.data.map((v, i) => (i % 4 === 3 ? 255 - wide.pixels.data[i - 3] : v)), 384, wide.rows);
        out.wide = { scale: wide.face.scaleX, alpha, shown };
        return out;
        """,
        layout=LAYOUT_JS,
    )
    for size in (8, 16, 24):
        found = result[str(size)]
        assert found["ink"] and found["bad"] == 0, (size, found)
        assert all(n % (size // 8) == 0 for n in found["lengths"]), (size, found)
    wide = result["wide"]
    assert wide["scale"] == 2 and wide["alpha"]["bad"] == 0, wide
    assert wide["alpha"]["lengths"] and all(n % 4 == 0 for n in wide["alpha"]["lengths"]), wide
    assert all(n % 4 == 0 for n in wide["shown"]["lengths"]), wide


async def check_renderer_no_offscreen(page):
    bare = await page.sibling(no_offscreen=True)
    try:
        body = """
        const res = await go(args.note);
        const report = await renderer.selfTest();
        return { bytes: Array.from(res.bitmap), ok: report.ok, engine: report.engine,
                 offscreen: typeof OffscreenCanvas, details: report.checks.filter((c) => !c.ok) };
        """
        plain = await run_renderer(page, body, note=NOTE)
        without = await run_renderer(bare, body, note=NOTE)
    finally:
        await bare.context.browser.close()
    assert plain["offscreen"] == "function" and without["offscreen"] == "undefined", (
        plain,
        without,
    )
    assert without["bytes"] == plain["bytes"], "bitmap differs without OffscreenCanvas"
    assert without["ok"] is True and without["engine"]["offscreenCanvas"] is False, without
    assert plain["engine"]["offscreenCanvas"] is True, plain


async def check_renderer_font_failure(page):
    file = FONTS["roboto_flex"]["file"]
    await page.route(f"**/fonts/{file}", lambda route: route.fulfill(status=404, body="gone"))
    result = await run_renderer(
        page,
        """
        let res = null;
        const failure = await code(async () => { res = await go(args.note); });
        return { failure, res: res === null };
        """,
        note=NOTE,
    )
    assert result == {"failure": "font_missing", "res": True}, result


async def check_renderer_defaults(page):
    result = await run_renderer(page, "return r.DEFAULT_OPTIONS;")
    assert result == STUDIO.DEFAULTS, (result, STUDIO.DEFAULTS)
    # Preset names are validated against a literal list in index.js.
    assert set(STUDIO.PRESETS) == {"photo", "line_art", "logo", "soft"}, STUDIO.PRESETS
    accepted = await run_renderer(
        page,
        """
        const out = {};
        for (const name of ['photo', 'line_art', 'logo', 'soft', 'nope']) {
          out[name] = await code(() => go(opt({ preset: name })));
        }
        return out;
        """,
    )
    assert accepted == {
        "photo": None,
        "line_art": None,
        "logo": None,
        "soft": None,
        "nope": "bad_document",
    }, accepted


SELF_TEST_NAMES = [
    "faces_loaded",
    "width_axis_changes_glyph_widths",
    "weight_axis_changes_glyph_widths",
    "render_is_deterministic",
    "pack_roundtrip_matches_display",
]


async def check_renderer_self_test(page):
    report = await run_renderer(page, "return await renderer.selfTest();")
    assert [c["name"] for c in report["checks"]] == SELF_TEST_NAMES, report
    assert all(c["ok"] for c in report["checks"]) and report["ok"] is True, report
    assert report["engine"]["offscreenCanvas"] is True and report["engine"]["userAgent"], report
    file = FONTS["roboto_flex"]["file"]
    await page.route(f"**/fonts/{file}", lambda route: route.fulfill(status=404, body="gone"))
    broken = await run_renderer(page, "return await renderer.selfTest();")
    assert broken["ok"] is False and not broken["checks"][0]["ok"], broken
    assert [c["name"] for c in broken["checks"]] == SELF_TEST_NAMES, broken
    assert "font_missing" in broken["checks"][0]["detail"], broken


async def check_renderer_evicted_face(page):
    """R12: an evicted face is reloaded for the render, never replaced by a fallback font."""
    result = await run_renderer(
        page,
        """
        const doc = opt({ font_width: 60 });
        const before = await go(doc);
        for (const w of [30, 40, 45, 50, 55, 65, 70, 75, 80]) {
          await renderer.faceFor({ font: 'roboto_flex', font_width: w });
        }
        await new Promise((resolve) => setTimeout(resolve, 20)); // the registry evicts after the task
        const family = 'tp-roboto_flex-w60-W400';
        const names = () => [...document.fonts].map((f) => f.family.replaceAll('"', ''));
        const evicted = !names().includes(family);
        const after = await go(doc);
        return { evicted, same: same(before.bitmap, after.bitmap), back: names().includes(family) };
        """,
    )
    assert result == {"evicted": True, "same": True, "back": True}, result


async def check_renderer_stale_face_fails_loudly(page):
    """R12: a face the registry still caches but the page no longer has is an error, not a fallback."""
    result = await run_renderer(
        page,
        """
        await go(opt({ font_width: 60 }));
        for (const f of [...document.fonts]) {
          if (f.family.replaceAll('"', '') === 'tp-roboto_flex-w60-W400') document.fonts.delete(f);
        }
        let res = null;
        const failure = await code(async () => { res = await go(opt({ font_width: 60 })); });
        const other = await code(() => go(opt({ font_width: 70 })));
        return { failure, res: res === null, other };
        """,
    )
    assert result == {"failure": "font_load_failed", "res": True, "other": None}, result


async def check_renderer_pixels_reuse(page):
    """The reused ImageData is rewritten in full: a same-height render with less ink leaves no dots."""
    result = await run_renderer(
        page,
        """
        const heavy = await go({ kind: 'text', text: 'ШШШШШШ', options: {} });
        const light = await go({ kind: 'text', text: 'iiiiii', options: {} });
        const expect = pack.unpack(light.bitmap, light.rows).data;
        return { rows: [heavy.rows, light.rows], reused: light.pixels === heavy.pixels,
                 exact: same(expect, light.pixels.data), differ: !same(heavy.bitmap, light.bitmap) };
        """,
    )
    assert result["rows"][0] == result["rows"][1], result
    assert result["reused"] and result["exact"] and result["differ"], result


async def check_renderer_self_test_keeps_results(page):
    """selfTest on the live Renderer must not rewrite the pixels of a result already handed out."""
    result = await run_renderer(
        page,
        """
        // Same row count as the self-test label but other ink: reuse would overwrite it.
        const probe = await go({ kind: 'text', text: args.selftext, options: {} });
        const held = await go({ kind: 'text', text: 'ЫЫЫЫЫЫ', options: {} });
        const shot = Array.from(held.pixels.data);
        const report = await renderer.selfTest();
        return { ok: report.ok, kept: same(shot, held.pixels.data), rows: held.rows === probe.rows };
        """,
        selftext="Привет, мир 0123 Wxyz",
    )
    assert result == {"ok": True, "kept": True, "rows": True}, result


async def check_renderer_dispose_in_flight(page):
    """A render still loading its face when dispose() runs fails and leaves no FontFace behind."""
    result = await run_renderer(
        page,
        """
        const pending = code(() => go(opt({ font_width: 91 })));
        renderer.dispose();
        const failure = await pending;
        await new Promise((resolve) => setTimeout(resolve, 50));
        const left = [...document.fonts].filter((f) => f.family.replaceAll('"', '').startsWith('tp-')).length;
        return { failure, left };
        """,
    )
    assert result == {"failure": "font_load_failed", "left": 0}, result
    after = await run_renderer(
        page,
        """
        renderer.dispose();
        const text = await code(() => go(opt({ font_width: 92 })));
        const qr = await code(() => go({ kind: 'qr', text: 'x', options: {} }));
        const face = await code(() => renderer.faceFor({ font_width: 93 }));
        const left = [...document.fonts].filter((f) => f.family.replaceAll('"', '').startsWith('tp-')).length;
        return { text, qr, face, left };
        """,
    )
    assert after == {
        "text": "font_load_failed",
        "qr": "font_load_failed",
        "face": "font_load_failed",
        "left": 0,
    }, after


async def check_renderer_overlapping_renders(page):
    """Unawaited renders interleave at the face awaits: each keeps its generation and its bits."""
    example = STUDIO.TEMPLATES["note"]["example"]
    result = await run_renderer(
        page,
        """
        const docs = [
          opt({ font_width: 60 }),                       // uncached face: resolves last
          opt({}),
          args.note,
          { kind: 'qr', text: 'https://example.com', options: { qr_size: 200 } },
          { kind: 'template', template: 'note', fields: args.example, options: {} },
        ];
        const modes = [{}, {}, {}, {}, { ghost: true }];
        const batch = (n) => docs.slice(0, n).map((doc, i) => go(doc, { generation: 100 + i, ...modes[i] }));
        const out = {};
        for (const n of [2, 5]) {
          const results = await Promise.all(batch(n));
          // Independent copies: bitmaps are fresh arrays, taken before anything renders again.
          const bits = results.map((r) => (r.bitmap ? Array.from(r.bitmap) : null));
          const buffers = new Set(results.filter((r) => r.bitmap).map((r) => r.bitmap.buffer));
          const sequential = [];
          for (let i = 0; i < n; i += 1) {
            const r = await go(docs[i], { generation: 7, ...modes[i] });
            sequential.push(r.bitmap ? Array.from(r.bitmap) : null);
          }
          out[n] = {
            generations: results.map((r) => r.generation),
            ghosts: results.map((r) => r.ghost),
            equal: bits.map((b, i) => (b === null ? sequential[i] === null : same(b, sequential[i]))),
            distinctBuffers: buffers.size === results.filter((r) => r.bitmap).length,
            ink: results.map((r) => r.inkPercent > 0 || r.ghost),
          };
        }
        // Documented reuse: same-height exact results share one ImageData, only the newest is valid.
        const line = (text) => ({ kind: 'text', text, options: {} });
        const [a, b] = await Promise.all([go(line('ЫЫЫЫЫЫ')), go(line('iiiiii'))]);
        out.reuse = {
          sameHeight: a.rows === b.rows,
          shared: a.pixels === b.pixels,
          newestValid: same(b.pixels.data, pack.unpack(b.bitmap, b.rows).data),
          bitmapsIndependent: a.bitmap.buffer !== b.bitmap.buffer && !same(a.bitmap, b.bitmap),
        };
        return out;
        """,
        example=example,
        note=NOTE,
    )
    for n in ("2", "5"):
        out = result[n]
        assert out["generations"] == [100 + i for i in range(int(n))], result
        assert all(out["equal"]) and out["distinctBuffers"] and all(out["ink"]), (n, result)
    assert result["5"]["ghosts"] == [False, False, False, False, True], result
    reuse = result["reuse"]
    assert reuse["sameHeight"] and reuse["shared"] and reuse["newestValid"], reuse
    assert reuse["bitmapsIndependent"], reuse


async def check_renderer_impostor_face(page):
    """R12: liveness is the Renderer's own FontFace instance, not any FontFace of the same family.

    Known limit: the page picks by family name, so an impostor registered while the Renderer's own
    face is still live can be the one the canvas uses; only a family name nobody else can produce
    would exclude that.
    """
    result = await run_renderer(
        page,
        """
        const family = 'tp-roboto_flex-w60-W400';
        const names = () => [...document.fonts].filter((f) => f.family.replaceAll('"', '') === family);
        const impostor = async () => {
          const font = new FontFace(family, await (await fetch(new URL('fonts/RobotoFlex.ttf', args.base))).arrayBuffer());
          await font.load();
          document.fonts.add(font);
          return font;
        };
        const doc = opt({ font_width: 60 });
        const first = await go(doc);
        const own = first.face.fontFace;
        // 1. The registry still caches the face, the page lost it, an impostor took its place.
        document.fonts.delete(own);
        const fake = await impostor();
        let res = null;
        const stale = await code(async () => { res = await go(doc); });
        // 2. The registry evicted the face for real: the render reloads and draws with its own copy.
        document.fonts.delete(fake);
        for (const w of [30, 40, 45, 50, 55, 65, 70, 75, 80]) await renderer.faceFor({ font: 'roboto_flex', font_width: w });
        await new Promise((resolve) => setTimeout(resolve, 20));
        const fake2 = await impostor();
        const again = await go(doc);
        // Same family name: the page itself may prefer the impostor, so compare once it is gone.
        document.fonts.delete(fake2);
        const clean = await go(doc);
        return {
          stale, staleResult: res === null,
          reloaded: again.face.fontFace !== fake2 && again.face.fontFace !== own,
          registered: document.fonts.has(again.face.fontFace) && again.face.fontFace.status === 'loaded',
          same: same(first.bitmap, clean.bitmap) && clean.face.fontFace === again.face.fontFace,
        };
        """,
    )
    assert result == {
        "stale": "font_load_failed",
        "staleResult": True,
        "reloaded": True,
        "registered": True,
        "same": True,
    }, result


async def check_renderer_face_for(page):
    result = await run_renderer(
        page,
        """
        const face = await renderer.faceFor({ font: 'roboto_flex', font_size: 30, font_width: 80, font_weight: 700 });
        const res = await go(opt({ font_size: 30, font_width: 80, font_weight: 700 }));
        return [face.family, res.face.family, face.css(30), await code(() => renderer.faceFor({ font: 'nope' }))];
        """,
    )
    assert result[0] == result[1] == "tp-roboto_flex-w80-W700", result
    assert result[2] == '30px "tp-roboto_flex-w80-W700"', result
    assert result[3] == "bad_document", result


def option_cases() -> list[tuple[str, dict]]:
    bad: dict[str, list] = {
        "fit": ["cover", "Width", 1, None],
        "rotate": [45, 360, -90, "90", None],
        "width": [95, 385, 200.5, "384", None],
        "threshold": [0, 255, 128.5, "128", None],
        "brightness": [0.24, 2.01, "1", None],
        "contrast": [0.24, 3.01, "1", None],
        "sharpness": [-0.01, 3.01, "1", None],
        "columns": [0, 4, 1.5, "2", True, None],
        "font": ["comic", "", 1, None],
        "font_size": [11, 97, 24.5, "24", None],
        "qr_size": [95, 361, 200.5, None],
        "qr_correction": ["L", "m", 1, None],
        "method": ["sobel", None],
        "align": ["justify", None],
        "invert": [1, "yes"],
        "border": [0, "no"],
        "font_width": [24, 152, 99.5, None],
        "font_weight": [99, 1001, 400.5, None],
        "preset": ["nope", None],
        "unknown_key": [1],
    }
    cases = [(f"{key}={value!r}", {key: value}) for key, values in bad.items() for value in values]
    tiny = {"font": "tiny5", "font_size": 16}
    cases += [
        ("tiny5 size 12", {**tiny, "font_size": 12}),
        ("tiny5 size 56", {**tiny, "font_size": 56}),
        ("tiny5 width 150", {**tiny, "font_width": 150}),
        ("tiny5 width 50", {**tiny, "font_width": 50}),
        ("tiny5 weight 500", {**tiny, "font_weight": 500}),
        ("tiny5 size 20", {**tiny, "font_size": 20}),
    ]
    return cases


def good_options() -> list[dict]:
    tiny = {"font": "tiny5", "font_weight": 400}
    return [
        {"fit": "width"},
        {"fit": "original"},
        *({"rotate": r} for r in (0, 90, 180, 270)),
        {"width": 96},
        {"width": 384},
        {"threshold": 1},
        {"threshold": 254},
        {"brightness": 0.25},
        {"brightness": 2},
        {"contrast": 0.25},
        {"contrast": 3},
        {"sharpness": 0},
        {"sharpness": 3},
        {"font_size": 12},
        {"font_size": 96},
        {"font_width": 25},
        {"font_width": 151},
        {"font_weight": 100},
        {"font_weight": 1000},
        {"qr_size": 96},
        {"qr_size": 360},
        *({"qr_correction": q} for q in "MQH"),
        *({"columns": c} for c in (1, 2, 3)),
        *({**tiny, "font_size": s} for s in (8, 16, 24, 32, 40, 48)),
        {**tiny, "font_size": 16, "font_width": 200},
    ]


async def check_renderer_option_validation(page):
    result = await run_renderer(
        page,
        """
        const wrong = [];
        for (const [label, options] of args.bad) {
          const got = await code(() => go(opt(options)));
          if (got !== 'bad_document') wrong.push([label, got]);
        }
        const refused = [];
        for (const options of args.good) {
          const got = await code(() => go(opt(options)));
          if (got !== null) refused.push([options, got]);
        }
        // `columns` is required by layout: an explicit undefined must not fall through to it.
        const missing = await code(() => go(opt({ columns: undefined })));
        const noOptions = await code(() => go({ kind: 'text', text: 'x' }));
        const badOptions = await code(() => go({ kind: 'text', text: 'x', options: [] }));
        return { wrong, refused, missing, noOptions, badOptions };
        """,
        bad=option_cases(),
        good=good_options(),
    )
    assert result["wrong"] == [], result["wrong"]
    assert result["refused"] == [], result["refused"]
    assert result["missing"] == "bad_document" and result["badOptions"] == "bad_document", result
    assert result["noOptions"] is None, result


async def check_renderer_field_validation(page):
    big = "я" * 3001
    result = await run_renderer(
        page,
        """
        const t = (fields, template = 'note') => ({ kind: 'template', template, fields, options: {} });
        const cases = {
          nonString: t({ text: 123 }),
          nullText: t({ text: null }),
          longTitle: t({ title: args.big, text: 'x' }),
          longQr: t({ qr: 'q'.repeat(501), title: 'x' }, 'qr_label'),
          combined: t({ title: 'a'.repeat(1600), text: 'b'.repeat(1600) }),
          blankRequired: t({ text: '   ', title: 'x' }),
          missingRequired: t({ title: 'x' }),
          unknownField: t({ text: 'x', nope: 'y' }),
          fieldsArray: t(['x']),
          fieldsString: t('x'),
          nonStringOptional: t({ text: 'x', footer: 5 }),
          nullOptional: t({ text: 'x', footer: null }),
          fieldsNull: t(null),
          pythonBlank: t({ text: '\x1f\x85 ' }),
          kindArray: { kind: ['text'], text: 'x', options: {} },
          kindConstructor: { kind: 'constructor', options: {} },
          blankTextKind: { kind: 'text', text: ' ', options: {} },
          numberTextKind: { kind: 'text', text: 7, options: {} },
          longTextKind: { kind: 'text', text: args.big, options: {} },
          longQrKind: { kind: 'qr', text: 'q'.repeat(501), options: {} },
          nonStringQrKind: { kind: 'qr', text: 5, options: {} },
          unknownKind: { kind: 'bogus', options: {} },
          unknownKey: { kind: 'text', text: 'x', options: {}, extra: 1 },
          nullDoc: null,
        };
        const out = {};
        for (const [name, doc] of Object.entries(cases)) out[name] = await code(() => go(doc));
        out.blankQrKind = await code(() => go({ kind: 'qr', text: '  ', options: {} }));
        out.pythonBlankQr = await code(() => go({ kind: 'qr', text: '\x1f', options: {} }));
        out.nullOptions = await code(() => go({ kind: 'text', text: 'x', options: null }));
        out.limit = await code(() => go(t({ title: 'a'.repeat(1500), text: 'b'.repeat(1500) })));
        out.astral = await code(() => go({ kind: 'text', text: '😀'.repeat(1000), options: {} }));
        return out;
        """,
        big=big,
    )
    bad = [
        "nonString nullText longTitle longQr combined blankRequired missingRequired unknownField",
        "fieldsArray fieldsString nonStringOptional nullOptional fieldsNull pythonBlank blankTextKind",
        "numberTextKind longTextKind longQrKind nonStringQrKind unknownKind unknownKey nullDoc",
        "kindArray kindConstructor",
    ]
    expected = {name: "bad_document" for line in bad for name in line.split()}
    expected |= {"blankQrKind": "blank_qr", "pythonBlankQr": "blank_qr", "nullOptions": None}
    # 1000 emoji are within the limit (code points, not UTF-16 units); the row cap depends on the
    # fallback emoji font, so only a validation error would be wrong.
    assert result.pop("astral") in (None, "too_tall"), result
    expected |= {"limit": "too_tall"}
    assert result == expected, result


async def check_renderer_row_cap(page):
    result = await run_renderer(
        page,
        """
        const doc = { kind: 'text', text: 'Ш'.repeat(3000), options: {} };
        return [await code(() => go(doc)), await code(() => go(doc, { approximate: true })),
                await code(() => go(doc, { ghost: true }))];
        """,
    )
    assert result == ["too_tall"] * 3, result


async def check_renderer_qr_500(page):
    result = await run_renderer(
        page,
        """
        const text = Array.from({ length: 500 }, (_, i) => String.fromCharCode(97 + (i * 7) % 26)).join('');
        const big = await go({ kind: 'qr', text, options: { qr_size: 360, qr_correction: 'M' } });
        const small = await code(() => go({ kind: 'qr', text, options: { qr_size: 96 } }));
        const high = await code(() => go({ kind: 'qr', text, options: { qr_size: 360, qr_correction: 'H' } }));
        const label = await go({ kind: 'template', template: 'qr_label', fields: { qr: 'https://example.com' }, options: {} });
        return { rows: big.rows, ink: big.inkPercent > 0, small, high, qrBlock: label.blocks.some((b) => b.kind === 'qr') };
        """,
    )
    assert result["rows"] > 96 and result["ink"], result
    assert result["small"] == "qr_modules_too_small", result
    assert result["high"] == "qr_modules_too_small", result
    assert result["qrBlock"], result


STATE_JS = f"{STATIC}studio/state.js"
STATUS_JS = f"{STATIC}studio/status.js"
OPTIONS_JS = f"{STATIC}studio/options.js"
LIVE_JS = f"{STATIC}studio/live.js"
ALL_GOOD = {
    "deviceId": "dev1",
    "hasInput": True,
    "missing": [],
    "textTooLong": False,
    "renderPending": False,
    "renderOk": True,
    "renderExact": True,
    "hasInk": True,
    "inFlight": False,
}
BLOCKER_FLAGS = {
    "no_device": {"deviceId": ""},
    "no_input": {"hasInput": False},
    "required_blank": {"missing": ["text"]},
    "text_too_long": {"textTooLong": True},
    "render_pending": {"renderPending": True},
    "render_failed": {"renderOk": False},
    "render_approximate": {"renderExact": False},
    "no_ink": {"hasInk": False},
    "job_in_flight": {"inFlight": True},
}


async def run_module(page, url, body, arg=None):
    return await page.evaluate(
        "async ([url, body, arg]) => { const m = await import(url);"
        " return await (new Function('m', 'arg', `return (async () => { ${body} })()`))(m, arg); }",
        [url, body, arg],
    )


def state_arg(**extra):
    return {"templates": STUDIO.TEMPLATES, **extra}


async def check_state_ghost(page):
    r = await run_module(
        page,
        STATE_JS,
        """const s = { mode: 'template', template: 'note', fields: {}, qrText: '', hasImage: false };
        const doc = m.effectiveDocument(s, arg.templates, {});
        return { doc, input: m.hasUserInput(s),
          blockers: m.printBlockers({ ...arg.good, hasInput: false }),
          qr: m.effectiveDocument({ ...s, mode: 'qr' }, arg.templates, {}),
          image: m.effectiveDocument({ ...s, mode: 'image' }, arg.templates, {}) };""",
        state_arg(good=ALL_GOOD),
    )
    note = STUDIO.TEMPLATES["note"]["example"]
    assert r["doc"]["ghost"] is True and r["doc"]["document"]["fields"] == note, r
    assert r["doc"]["document"]["kind"] == "template" and r["doc"]["document"]["template"] == "note"
    assert r["input"] is False and "no_input" in r["blockers"], r
    assert r["qr"] == {"document": None, "ghost": False} or r["qr"]["document"] is None, r
    assert r["image"]["document"] is None, r


async def check_state_one_char(page):
    r = await run_module(
        page,
        STATE_JS,
        """const s = { mode: 'template', template: 'note', fields: { title: 'a', text: '  ', footer: '' },
          qrText: '', hasImage: false };
        return m.effectiveDocument(s, arg.templates, { font_size: 30 });""",
        state_arg(),
    )
    assert r["ghost"] is False, r
    assert r["document"]["fields"] == {"title": "a"}, r
    assert r["document"]["options"] == {"font_size": 30}, r


async def check_state_too_long(page):
    r = await run_module(
        page,
        STATE_JS,
        """const t = arg.templates;
        const note = (fields) => ({ mode: 'template', template: 'note', fields, qrText: '', hasImage: false });
        const qr = (text) => ({ mode: 'qr', template: 'note', fields: {}, qrText: text, hasImage: false });
        const spool = (qrText) => ({ mode: 'template', template: 'qr_label', fields: { qr: qrText }, qrText: '', hasImage: false });
        return {
          field3001: m.textTooLong(note({ text: 'x'.repeat(3001) }), t),
          field3000: m.textTooLong(note({ text: 'x'.repeat(3000) }), t),
          qr501: m.textTooLong(qr('x'.repeat(501)), t),
          qr500: m.textTooLong(qr('x'.repeat(500)), t),
          tplQr501: m.textTooLong(spool('x'.repeat(501)), t),
          tplQr500: m.textTooLong(spool('x'.repeat(500)), t),
          two1600: m.textTooLong(note({ title: 'x'.repeat(1600), text: 'y'.repeat(1600) }), t),
          two1500: m.textTooLong(note({ title: 'x'.repeat(1500), text: 'y'.repeat(1500) }), t),
          emoji: m.textTooLong(note({ text: '\\u{1F600}'.repeat(2000) }), t),
          qrNotCombined: m.textTooLong({ mode: 'template', template: 'storage_label',
            fields: { title: 'x'.repeat(3000), qr: 'y'.repeat(500) }, qrText: '', hasImage: false }, t),
          blockers: m.printBlockers({ ...arg.good, textTooLong: true }),
        };""",
        state_arg(good=ALL_GOOD),
    )
    assert r.pop("blockers") == ["text_too_long"]
    expected = {k: k in {"field3001", "qr501", "tplQr501", "two1600"} for k in r}
    assert r == expected, r


async def check_state_blank(page):
    r = await run_module(
        page,
        STATE_JS,
        """const base = { mode: 'template', template: 'note', fields: {}, qrText: '', hasImage: false };
        return {
          blank: ['', ' ', '\\n\\t ', '\\u00a0\\u3000', '\\u001c', null, undefined, 5].map((v) => m.isBlank(v)),
          nonblank: ['a', ' a '].map((v) => m.isBlank(v)),
          ws: m.hasUserInput({ ...base, fields: { title: '\\n\\t ' } }),
          one: m.hasUserInput({ ...base, fields: { footer: 'x' } }),
          image: m.hasUserInput({ ...base, mode: 'image', hasImage: true }),
          noImage: m.hasUserInput({ ...base, mode: 'image' }),
          qr: m.hasUserInput({ ...base, mode: 'qr', qrText: 'https://x' }),
          qrBlank: m.hasUserInput({ ...base, mode: 'qr', qrText: '  ' }),
          qrIgnoresStaleFields: m.hasUserInput({ ...base, mode: 'qr', fields: { title: 'a' } }),
        };""",
    )
    assert r["blank"] == [True] * 7 + [True], r
    assert r["nonblank"] == [False, False], r
    assert (r["ws"], r["one"], r["image"], r["noImage"], r["qr"], r["qrBlank"]) == (
        False,
        True,
        True,
        False,
        True,
        False,
    ), r
    assert r["qrIgnoresStaleFields"] is False, r


async def check_state_required(page):
    r = await run_module(
        page,
        STATE_JS,
        """const s = { mode: 'template', template: 'note', fields: { title: 'Hi' }, qrText: '', hasImage: false };
        const missing = m.missingRequired(s, arg.templates);
        return { missing,
          ok: m.missingRequired({ ...s, fields: { text: 'x' } }, arg.templates),
          qrMode: m.missingRequired({ ...s, mode: 'qr' }, arg.templates),
          multi: m.missingRequired({ ...s, template: 'qr_label', fields: {} }, arg.templates),
          blockers: m.printBlockers({ ...arg.good, missing }),
          text: m.BLOCKER_TEXT };""",
        state_arg(good=ALL_GOOD),
    )
    assert r["missing"] == ["text"] and r["ok"] == [] and r["qrMode"] == [], r
    assert r["multi"] == ["qr"], r
    assert r["blockers"] == ["required_blank"], r
    assert set(r["text"]) == set(BLOCKER_FLAGS), r
    assert all(
        isinstance(v, str) and any("а" <= c.lower() <= "я" for c in v) for v in r["text"].values()
    )


async def check_state_blockers(page):
    r = await run_module(
        page,
        STATE_JS,
        """const out = { all: m.printBlockers(arg.good), order: m.BLOCKERS };
        out.single = {};
        for (const [code, flags] of Object.entries(arg.flags)) out.single[code] = m.printBlockers({ ...arg.good, ...flags });
        out.nullDevice = m.printBlockers({ ...arg.good, deviceId: null });
        out.everything = m.printBlockers({ deviceId: '', hasInput: false, missing: ['a'], textTooLong: true,
          renderPending: true, renderOk: false, renderExact: false, hasInk: false, inFlight: true });
        out.pair = m.printBlockers({ ...arg.good, renderOk: false, renderExact: true });
        out.pair2 = m.printBlockers({ ...arg.good, renderOk: true, renderExact: false, hasInk: false });
        return out;""",
        state_arg(good=ALL_GOOD, flags=BLOCKER_FLAGS),
    )
    order = list(BLOCKER_FLAGS)
    assert r["order"] == order and r["all"] == [], r
    for code in order:
        assert r["single"][code] == [code], (code, r["single"][code])
    assert r["nullDevice"] == ["no_device"], r
    assert r["everything"] == order, r
    assert r["pair"] == ["render_failed"] and r["pair2"] == ["render_approximate", "no_ink"], r


async def check_state_drafts(page):
    r = await run_module(
        page,
        STATE_JS,
        """let d = m.emptyDrafts();
        d = m.withDraftField(d, 'note', 'text', 'milk');
        const heading = m.draftFor(d, 'heading');
        d = m.withDraftField(d, 'heading', 'title', 'BIG');
        const before = JSON.stringify(d);
        const frozen = m.withDraftField(d, 'note', 'title', 'x');
        return { heading, note: m.draftFor(d, 'note'), again: m.draftFor(d, 'heading'),
          untouched: JSON.stringify(d) === before, frozen: m.draftFor(frozen, 'note'),
          fresh: m.draftFor(m.emptyDrafts(), 'note') };""",
    )
    assert r["heading"] == {} and r["note"] == {"text": "milk"}, r
    assert r["again"] == {"title": "BIG"} and r["untouched"] is True, r
    assert r["frozen"] == {"text": "milk", "title": "x"} and r["fresh"] == {}, r


async def check_state_printable(page):
    r = await run_module(
        page,
        STATE_JS,
        """const bitmap = new Uint8Array(48 * 2); bitmap[5] = 1;
        const exact = { generation: 4, exact: true, ghost: false, document: { kind: 'text' }, bitmap, rows: 2 };
        const view = (result, generation, pending) => m.printableResult(result, { generation, pending });
        const pixels = new ImageData(new Uint8ClampedArray(384 * 2 * 4).fill(7), 384, 2);
        const kept = m.keepResult({ ...exact, pixels });
        pixels.data.fill(0); bitmap[5] = 0;
        return {
          ok: view(exact, 4, false) === exact,
          older: view(exact, 5, false), inFlight: view(exact, 4, true), none: view(null, 4, false),
          approx: view({ ...exact, exact: false, document: null }, 4, false),
          noDoc: view({ ...exact, document: null }, 4, false),
          ghost: view({ ...exact, ghost: true }, 4, false),
          keptPixels: kept.pixels, keptRows: kept.rows, keptBit: kept.bitmap[5],
          inkYes: m.bitmapHasInk(new Uint8Array([0, 0, 8])), inkNo: m.bitmapHasInk(new Uint8Array(4)),
          inkNull: m.bitmapHasInk(null),
        };""",
    )
    assert r["ok"] is True and r["older"] is None and r["inFlight"] is None and r["none"] is None, r
    assert r["approx"] is None and r["noDoc"] is None and r["ghost"] is None, r
    assert r["keptPixels"] is None and r["keptBit"] == 1 and r["keptRows"] == 2, r
    assert (r["inkYes"], r["inkNo"], r["inkNull"]) == (True, False, False), r


async def check_status(page):
    r = await run_module(
        page,
        STATUS_JS,
        """const flight = ['rendering', 'connecting', 'printing'];
        const o = (state) => ({ state });
        const states = ['unknown', 'unavailable', 'idle', 'sent', 'error', 'rendering', 'connecting', 'printing'];
        return {
          flags: Object.fromEntries(states.map((s) => [s, m.jobInFlight(o(s), flight)])),
          missing: m.jobInFlight(undefined, flight), nul: m.jobInFlight(null, flight),
          text: Object.fromEntries(states.map((s) => [s, m.statusText(o(s))])),
          undef: m.statusText(undefined),
          later: m.statusText(o('waiting_for_printer')),
          laterFlight: m.jobInFlight(o('waiting_for_printer'), [...flight, 'waiting_for_printer']),
          table: m.STATUS_TEXT,
        };""",
    )
    inflight = {"rendering", "connecting", "printing"}
    assert all(v == (k in inflight) for k, v in r["flags"].items()), r["flags"]
    assert r["missing"] is False and r["nul"] is False and r["laterFlight"] is True, r
    for k, v in r["text"].items():
        assert (v is not None) == (k in inflight), r["text"]
    assert r["undef"] is None, r
    assert set(r["table"]) >= inflight and all(isinstance(v, str) for v in r["table"].values()), r
    assert isinstance(r["later"], str) and r["later"], r


async def check_options_hint(page):
    cases = [
        (24, 100, 701, True), (23, 100, 701, True), (24, 100, 700, False), (25, 100, 701, False),
        (71, 99, 1000, True), (72, 99, 1000, False), (71, 100, 1000, False), (71, 99, 999, False),
        (30, 49, 801, True), (30, 49, 800, False), (30, 50, 801, False),
    ]  # fmt: skip
    r = await run_module(
        page,
        OPTIONS_JS,
        """return { roboto: arg.map(([s, w, wt]) => m.closingCounterHint('roboto_flex', s, w, wt)),
          tiny: [[8, 25, 1000], [24, 100, 900]].map(([s, w, wt]) => m.closingCounterHint('tiny5', s, w, wt)) };""",
        [c[:3] for c in cases],
    )
    for case, got in zip(cases, r["roboto"], strict=True):
        assert (isinstance(got, str) and bool(got)) == case[3] and (got is None or got), (case, got)
        assert case[3] or got is None, (case, got)
    assert r["tiny"] == [None, None], r


async def check_options_snap_and_coerce(page):
    r = await run_module(
        page,
        OPTIONS_JS,
        """const fonts = arg.fonts;
        const base = { font: 'roboto_flex', font_size: 20, font_width: 50, font_weight: 900, columns: 2, threshold: 99 };
        return {
          snaps: [m.snapToGrid(37, 5, 25, 151), m.snapToGrid(480, 50, 100, 1000), m.snapToGrid(475, 50, 100, 1000),
            m.snapToGrid(1000, 50, 100, 1000), m.snapToGrid(5000, 50, 100, 1000), m.snapToGrid(1, 50, 100, 1000)],
          steps: [m.APPROX_WIDTH_STEP, m.APPROX_WEIGHT_STEP],
          tiny: m.coerceForFont(base, fonts, 'tiny5'),
          tie28: m.coerceForFont({ ...base, font: 'tiny5', font_size: 24, font_width: 100, font_weight: 400 }, fonts, 'tiny5'),
          tie12: m.coerceForFont({ ...base, font_size: 12 }, fonts, 'tiny5').font_size,
          tie36: m.coerceForFont({ ...base, font_size: 36 }, fonts, 'tiny5').font_size,
          big: m.coerceForFont({ ...base, font_size: 90 }, fonts, 'tiny5').font_size,
          back: m.coerceForFont({ ...base, font: 'tiny5', font_size: 8, font_width: 200, font_weight: 400 }, fonts, 'roboto_flex'),
          sameKept: m.coerceForFont(base, fonts, 'roboto_flex'),
          sameSize: m.coerceForFont({ ...base, font_size: 8 }, fonts, 'roboto_flex').font_size,
          input: base,
        };""",
        {"fonts": FONTS},
    )
    assert r["snaps"] == [35, 500, 500, 1000, 1000, 100], r
    assert r["steps"] == [5, 50], r
    assert r["tiny"] == {
        "font": "tiny5", "font_size": 16, "font_width": 100, "font_weight": 400, "columns": 2, "threshold": 99,
    }, r  # fmt: skip
    assert r["tie28"]["font_size"] == 24, r
    assert (r["tie12"], r["tie36"], r["big"]) == (8, 32, 48), r
    assert r["back"]["font_width"] == 100 and r["back"]["font_weight"] == 400, r
    assert r["back"]["font_size"] == 12 and r["back"]["columns"] == 2, r
    assert r["sameKept"] == r["input"] and r["sameSize"] == 12, r


async def check_options_preset_and_browser_only(page):
    r = await run_module(
        page,
        OPTIONS_JS,
        """const o = { preset: 'photo', method: 'floyd_steinberg', contrast: 1.25, brightness: 1.1, sharpness: 1.2,
          threshold: 77, columns: 1 };
        const applied = m.applyPreset(o, arg.presets, 'line_art');
        let error = null;
        try { m.applyPreset(o, arg.presets, 'nope'); } catch (e) { error = String(e); }
        return { applied, input: o, error,
          flags: [{ columns: 1, font: 'roboto_flex', font_width: 100 }, { columns: 2 }, { columns: 3, font: 'tiny5' },
            { columns: 1, font: 'tiny5', font_width: 200 }, { columns: 1, font: 'tiny5', font_width: 100 },
            { columns: 1, font: 'roboto_flex', font_width: 200 }, {}].map((x) => m.usesBrowserOnlyOptions(x)) };""",
        {"presets": STUDIO.PRESETS},
    )
    line = {k: v for k, v in STUDIO.PRESETS["line_art"].items() if k != "name"}
    assert r["applied"] == {
        "preset": "line_art", **line, "threshold": 77, "columns": 1,
    }, r  # fmt: skip
    assert r["input"]["preset"] == "photo" and r["error"], r
    assert r["flags"] == [False, True, True, True, False, False, False], r


LIVE_PRELUDE = """
const frames = [];
const raf = (cb) => { frames.push(cb); return frames.length; };
const frame = () => { const batch = frames.splice(0); for (const cb of batch) cb(0); };
const tick = () => new Promise((resolve) => setTimeout(resolve, 0));
const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return { promise, resolve, reject }; };
const results = [], errors = [], runs = [];
const make = (run) => new m.RenderScheduler(run, (r) => results.push(r), (e, g) => errors.push([String(e.message), g]), raf);
"""


async def check_live_coalesce(page):
    r = await run_module(
        page,
        LIVE_JS,
        LIVE_PRELUDE
        + """const s = make(async (input, g) => { runs.push([input, g]); return { generation: g, tag: input }; });
        const built = [];
        let last;
        for (let i = 0; i < 50; i += 1) last = s.request(() => { built.push(i); return `in${i}`; });
        const before = { runs: runs.length, pending: s.pending, frames: frames.length };
        frame(); await tick();
        return { before, runs, built, last, generation: s.generation, results, pending: s.pending, frames: frames.length };""",
    )
    assert r["before"] == {"runs": 0, "pending": True, "frames": 1}, r
    assert r["runs"] == [["in49", 50]] and r["built"] == [49], r
    assert r["last"] == 50 and r["generation"] == 50, r
    assert r["results"] == [{"generation": 50, "tag": "in49"}] and r["pending"] is False, r
    assert r["frames"] == 0, r


async def check_live_no_overlap(page):
    r = await run_module(
        page,
        LIVE_JS,
        LIVE_PRELUDE
        + """const gates = [];
        let active = 0, maxActive = 0;
        const s = make(async (input, g) => {
          runs.push([input, g]); active += 1; maxActive = Math.max(maxActive, active);
          const d = deferred(); gates.push(d); const r = await d.promise; active -= 1; return r;
        });
        s.request(() => 'a'); frame(); await tick();
        const afterFirst = runs.length;
        s.request(() => 'b'); s.request(() => 'c'); s.request(() => 'd');
        frame(); await tick(); frame(); await tick();
        const duringFirst = runs.length;
        gates[0].resolve({ generation: 1 }); await tick();
        const framesAfterSettle = frames.length;
        frame(); await tick();
        const secondStarted = runs.length;
        gates[1].resolve({ generation: 4 }); await tick();
        frame(); await tick();
        return { afterFirst, duringFirst, framesAfterSettle, secondStarted, runs, maxActive, results, pending: s.pending,
          runsAtEnd: runs.length };""",
    )
    assert (r["afterFirst"], r["duringFirst"]) == (1, 1), r
    assert r["framesAfterSettle"] == 1 and r["secondStarted"] == 2, r
    assert r["runs"] == [["a", 1], ["d", 4]] and r["maxActive"] == 1, r
    assert r["results"] == [{"generation": 4}] and r["pending"] is False and r["runsAtEnd"] == 2, r


async def check_live_last_edit_kept(page):
    """The edit made while a render is in flight is never dropped (the old `if (busy) return` bug)."""
    r = await run_module(
        page,
        LIVE_JS,
        LIVE_PRELUDE
        + """const gates = [];
        const s = make(async (input, g) => { runs.push(input); const d = deferred(); gates.push(d); await d.promise; return { generation: g, input }; });
        s.request(() => 'first'); frame(); await tick();
        s.request(() => 'last-edit');
        gates[0].resolve(); await tick(); frame(); await tick();
        gates[1].resolve(); await tick();
        return { runs, results };""",
    )
    assert r["runs"] == ["first", "last-edit"], r
    assert r["results"] == [{"generation": 2, "input": "last-edit"}], r


async def check_live_stale_discarded(page):
    r = await run_module(
        page,
        LIVE_JS,
        LIVE_PRELUDE
        + """const gates = [];
        const s = make(async (input, g) => { const d = deferred(); gates.push(d); const v = await d.promise; return { generation: g, v }; });
        s.request(() => 1); frame(); await tick();
        const g2 = s.request(() => 2);
        gates[0].resolve('old'); await tick();
        const afterStale = { results: results.slice(), pending: s.pending };
        frame(); await tick(); gates[1].resolve('new'); await tick();
        return { afterStale, results, g2, pending: s.pending };""",
    )
    assert r["afterStale"] == {"results": [], "pending": True}, r
    assert r["results"] == [{"generation": 2, "v": "new"}] and r["pending"] is False, r


async def check_live_pending(page):
    r = await run_module(
        page,
        LIVE_JS,
        LIVE_PRELUDE
        + """const d = deferred();
        const s = make(async () => d.promise);
        const p0 = s.pending;
        s.request(() => 1);
        const p1 = s.pending; frame(); await tick();
        const p2 = s.pending; d.resolve({ generation: 1 }); await tick();
        return [p0, p1, p2, s.pending];""",
    )
    assert r == [False, True, True, False], r


async def check_live_errors(page):
    r = await run_module(
        page,
        LIVE_JS,
        LIVE_PRELUDE
        + """const gates = [];
        const s = make(async (input) => { if (input === 'sync') throw new Error('sync boom'); const d = deferred(); gates.push(d); return d.promise; });
        s.request(() => 'a'); frame(); await tick();
        s.request(() => 'b');
        gates[0].reject(new Error('stale boom')); await tick();
        const afterStale = { errors: errors.slice(), pending: s.pending };
        frame(); await tick(); gates[1].reject(new Error('current boom')); await tick();
        const afterCurrent = { errors: errors.slice(), pending: s.pending };
        s.request(() => 'sync'); frame(); await tick();
        s.request(() => { throw new Error('build boom'); }); frame(); await tick();
        return { afterStale, afterCurrent, errors, pending: s.pending, results };""",
    )
    assert r["afterStale"] == {"errors": [], "pending": True}, r
    assert r["afterCurrent"] == {"errors": [["current boom", 2]], "pending": False}, r
    assert r["errors"] == [["current boom", 2], ["sync boom", 3], ["build boom", 4]], r
    assert r["pending"] is False and r["results"] == [], r


async def check_live_dispose(page):
    r = await run_module(
        page,
        LIVE_JS,
        LIVE_PRELUDE
        + """const d = deferred();
        const s = make(async () => d.promise);
        s.request(() => 1); frame(); await tick();
        s.dispose(); d.resolve({ generation: 1 }); await tick();
        s.request(() => 2); frame(); await tick();
        return { results, errors, runs: runs.length };""",
    )
    assert r == {"results": [], "errors": [], "runs": 0}, r


async def check_live_real_raf(page):
    r = await run_module(
        page,
        LIVE_JS,
        """const results = [];
        const s = new m.RenderScheduler(async (i, g) => ({ generation: g, i }), (x) => results.push(x), () => {});
        for (let i = 0; i < 5; i += 1) s.request(() => i);
        await new Promise((resolve) => setTimeout(resolve, 300));
        return results;""",
    )
    assert r == [{"generation": 5, "i": 4}], r


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
    (
        "renderer: two renders are byte-identical and pixels equal unpack(bitmap)",
        check_renderer_deterministic,
    ),
    ("renderer: every document kind renders", check_renderer_kinds),
    ("renderer: unknown template -> bad_document", check_renderer_unknown_template),
    ("renderer: ghost has no bitmap but has pixels", check_renderer_ghost),
    (
        "renderer: ghost with approximate returns the ghost result",
        check_renderer_ghost_wins_over_approximate,
    ),
    (
        "renderer: approximate render equals the exact render at the snapped values and is flagged inexact",
        check_renderer_approximate,
    ),
    ("renderer: sliders change the strip across their full ranges", check_renderer_sliders),
    ("renderer: Tiny5 has no intermediate alpha at sizes 8, 16, 24", check_renderer_tiny5_crisp),
    ("renderer: works without OffscreenCanvas", check_renderer_no_offscreen),
    ("renderer: font failure fails loudly", check_renderer_font_failure),
    ("renderer: DEFAULT_OPTIONS equals studio_render.DEFAULTS", check_renderer_defaults),
    ("renderer: selfTest passes", check_renderer_self_test),
    ("renderer: an evicted face is reloaded for the render", check_renderer_evicted_face),
    (
        "renderer: a stale face fails loudly, never a fallback font",
        check_renderer_stale_face_fails_loudly,
    ),
    (
        "renderer: reused pixels equal unpack(bitmap) after different content",
        check_renderer_pixels_reuse,
    ),
    ("renderer: selfTest leaves a held result untouched", check_renderer_self_test_keeps_results),
    ("renderer: dispose during a render leaves no FontFace", check_renderer_dispose_in_flight),
    (
        "renderer: overlapping renders keep their generation and their own bits",
        check_renderer_overlapping_renders,
    ),
    (
        "renderer: a FontFace of the same family is not mistaken for the Renderer's own",
        check_renderer_impostor_face,
    ),
    ("renderer: faceFor shares the family and size of the render", check_renderer_face_for),
    (
        "renderer: every option range, enum and font constraint is validated",
        check_renderer_option_validation,
    ),
    (
        "renderer: field types and limits are validated before layout",
        check_renderer_field_validation,
    ),
    ("renderer: the 2048 row cap gives too_tall on every path", check_renderer_row_cap),
    ("renderer: a 500-character QR does not crash", check_renderer_qr_500),
    ("state: all blank gives a ghost and no_input", check_state_ghost),
    ("state: one character removes every example", check_state_one_char),
    ("state: text over the Python limits is blocked", check_state_too_long),
    ("state: whitespace-only counts as blank, image and QR text as input", check_state_blank),
    ("state: the missing required field is named", check_state_required),
    (
        "state: each failing condition yields exactly its code, in BLOCKERS order",
        check_state_blockers,
    ),
    ("state: switching templates keeps each draft", check_state_drafts),
    (
        "state: only a current exact result is printable, pixels are copied",
        check_state_printable,
    ),
    ("status: only the in-flight states are in flight and labelled", check_status),
    ("options: closingCounterHint follows the spec rule", check_options_hint),
    ("options: snapToGrid and coerceForFont", check_options_snap_and_coerce),
    (
        "options: applyPreset copies the preset values; browser-only options",
        check_options_preset_and_browser_only,
    ),
    ("live: 50 requests inside one frame run once with the latest input", check_live_coalesce),
    ("live: runs never overlap, only the latest queued request runs", check_live_no_overlap),
    ("live: the last edit made during a render is never dropped", check_live_last_edit_kept),
    ("live: a stale result is discarded", check_live_stale_discarded),
    ("live: pending is true until the current result arrives", check_live_pending),
    ("live: stale rejections are ignored, current ones reach onError", check_live_errors),
    ("live: dispose silences the scheduler", check_live_dispose),
    ("live: default frame callback coalesces", check_live_real_raf),
]


async def main() -> int:
    failures = 0
    async with async_playwright() as p:
        for name in browsers():
            for title, check in CHECKS:
                page = await open_page(p, name)
                page.sibling = functools.partial(open_page, p, name)
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
