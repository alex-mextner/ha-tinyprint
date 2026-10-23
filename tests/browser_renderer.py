"""Browser checks for the renderer modules, run once per browser in TINYPRINT_BROWSERS.

Run: python tests/browser_renderer.py
"""

import asyncio
import json
import random
import struct
import sys
from pathlib import Path

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
    cases = [c for c in CASES if not c["near_edge"]]
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
          out[c.name] = p.blocks.flatMap((b) => b.lines);
        }
        return out;
        """,
        cases=[c for c in CASES if c["name"] in names],
    )
    for name, lines in result.items():
        assert lines, name
        assert not any(0xD800 <= ord(char) <= 0xDFFF for line in lines for char in line), lines
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
