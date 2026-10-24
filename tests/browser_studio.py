"""Exercise the bundled panel (real shell, real renderer) against a labelled mock-HA harness.

BLE and HA service routing are simulated; no hardware claim. The panel is served from
`https://ha.test/tinyprint_static/t/` exactly like the versioned path under Home Assistant.
Run: python tests/browser_studio.py   (browsers: env TINYPRINT_BROWSERS, default chromium,firefox)
"""

import asyncio
import base64
import contextlib
import hashlib
import json
import re
import sys
import traceback
from pathlib import Path

from PIL import Image
from playwright.async_api import async_playwright

sys.path.insert(0, str(Path(__file__).parent))
from browser_harness import ROOT, STATIC, browsers, font_table, open_page  # noqa: E402
from shared_loader import load  # noqa: E402

renderer = load("studio_render")
FONTS = font_table()
OUT = ROOT / "verification"
ENTITY = "sensor.x6h_job_status"
IN_FLIGHT = ["rendering", "connecting", "printing"]
PANEL_VERSION = re.search(
    r'^PANEL_VERSION = "([^"]+)"',
    (ROOT / "custom_components/tinyprint/studio.py").read_text(),
    re.M,
).group(1)
STATE_JS = f"{STATIC}studio/state.js"
STATUS_JS = f"{STATIC}studio/status.js"
PANEL = "document.querySelector('tinyprint-panel')"
IMAGE_ONLY = {"preset", "method", "rotate", "fit", "width", "invert"} | {
    "brightness",
    "contrast",
    "sharpness",
    "threshold",
}
BRIDGE = {
    "mode": "bridge",
    "bridge_configured": True,
    "bridge_state": "reachable",
    "bridge_scheme": "http",
    "bridge_checked_at": "2026-10-16T00:00:00+00:00",
}


def printer(device_id="test-printer", entity_id=ENTITY, **extra):
    return {
        "device_id": device_id,
        "entity_id": entity_id,
        "name": f"X6h · {device_id}",
        "status": "idle",
        "visible": False,
        "rssi_raw": None,
        "last_error": None,
        "last_result": {},
        **extra,
    }


class Mock:
    """Mock HA services: no preview/print_document at all, Print arrives as `print_bitmap`."""

    def __init__(self, printers):
        self.printers = printers
        self.print_fail = False
        self.sent = []

    async def service(self, name, data):
        if name == "printer_info":
            return {
                "printers": self.printers,
                "templates": renderer.TEMPLATES,
                "presets": renderer.PRESETS,
                "version": PANEL_VERSION,
                "in_flight_states": IN_FLIGHT,
                "fonts": FONTS,
            }
        if name == "print_bitmap":
            if self.print_fail:
                raise ValueError("printer refused the job")
            bitmap = base64.b64decode(data["bitmap_b64"], validate=True)
            assert len(bitmap) == data["rows"] * 48, "bitmap size does not match rows"
            self.sent.append(bitmap)
            return {
                "phase": "sent",
                "confirmation": "transport_only",
                "mock_transport": True,
                "bytes_written": len(bitmap),
                "bitmap_sha256": hashlib.sha256(bitmap).hexdigest(),
            }
        if name == "diagnose":
            return {
                "supported_profile": True,
                "confirmation": "connection_only",
                "mock_transport": True,
                "bytes_written": 0,
            }
        if name == "cancel":
            return {"cancelled": True, "mock_transport": True}
        raise ValueError(f"Unknown mock service {name}")


SETUP = """() => {
  window.calls = []; window.printDelay = 0;
  window.hass = { states: {}, callWS: async (request) => {
    window.calls.push(structuredClone(request));
    if (request.service === 'print_bitmap' && window.printDelay) await new Promise((r) => setTimeout(r, window.printDelay));
    return { response: await window.studioService(request.service, request.service_data) };
  } };
}"""
MOUNT = """(withHass) => {
  const style = document.createElement('style');
  style.textContent = 'body{margin:0}tinyprint-panel{height:100vh}';
  document.head.append(style);
  const panel = document.createElement('tinyprint-panel');
  document.body.append(panel);
  if (withHass) panel.hass = window.hass;
}"""
SETTLED = f"""() => {{ const p = {PANEL}; return p && p.ready && p.loaded && p.scheduler && !p.scheduler.pending && Boolean(p.lastResult || p.failure); }}"""


async def open_panel(
    p, browser, *, printers=None, delay_modules=0, mount=True, missing_font=False, **options
):
    page = await open_page(p, browser, **options)
    page.errors = []
    page.on("pageerror", lambda error: page.errors.append(str(error)))
    page.mock = Mock(printers if printers is not None else [printer()])
    await page.expose_function("studioService", page.mock.service)
    await page.evaluate(SETUP)
    if delay_modules:

        async def slow(route):
            await asyncio.sleep(delay_modules / 1000)
            await route.fallback()

        await page.route(STATE_JS, slow)
    page.font_missing = missing_font
    if missing_font:

        async def missing(route):
            if page.font_missing:
                await route.fulfill(status=404, body="not found")
            else:
                await route.fallback()

        await page.route(f"{STATIC}fonts/RobotoFlex.ttf", missing)
    await page.add_script_tag(url=f"{STATIC}tinyprint-panel.js", type="module")
    if mount:
        await page.evaluate(MOUNT, True)
    return page


class Skipped(Exception):
    """A check that cannot run in this browser: reported as skipped, neither ok nor failed."""


@contextlib.asynccontextmanager
async def panel(p, browser, settle=True, **options):
    page = await open_panel(p, browser, **options)
    try:
        if settle:
            await page.wait_for_function(SETTLED)
        yield page
        assert not page.errors, page.errors
        services = {c["service"] for c in await calls(page)}
        assert not services & {"preview", "print_document"}, services
    finally:
        await page.context.browser.close()


async def calls(page, service=None):
    found = await page.evaluate("() => window.calls")
    return [c for c in found if service is None or c["service"] == service]


async def settle(page):
    await page.wait_for_function(SETTLED)


async def texts(page):
    return await page.evaluate("async (url) => (await import(url)).BLOCKER_TEXT", STATE_JS)


async def set_state(page, state, entity=ENTITY):
    """Replace `hass` the way Home Assistant does on every update."""
    await page.evaluate(
        """([entity, state]) => {
          const states = { ...window.hass.states };
          if (state === null) delete states[entity]; else states[entity] = { state };
          window.hass = { ...window.hass, states };
          document.querySelector('tinyprint-panel').hass = window.hass;
        }""",
        [entity, state],
    )


async def fill_text(page, value, field="text"):
    await page.locator(f"#field-{field}").fill(value)


async def reference(page, document):
    """`Renderer.render` of exactly this document: (rows, packed bytes)."""
    rows, encoded = await page.evaluate(
        f"""async (doc) => {{
          const p = {PANEL};
          const r = await p.renderer.render({{ document: doc, image: null, ghost: false, approximate: false, generation: 0 }});
          return [r.rows, p.modules.pack.bytesToBase64(r.bitmap)];
        }}""",
        document,
    )
    return rows, base64.b64decode(encoded)


def note(**fields):
    return {"kind": "template", "template": "note", "fields": fields, "options": {}}


async def canvas_bytes(page):
    """Pack the displayed `#raster` pixels (ink = dark) the way the bitmap contract does."""
    rows, red = await page.evaluate(
        f"""() => {{
          const canvas = {PANEL}.shadowRoot.querySelector('#raster');
          const data = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data;
          const red = new Uint8Array(canvas.width * canvas.height);
          for (let i = 0; i < red.length; i += 1) red[i] = data[i * 4];
          let binary = '';
          for (let i = 0; i < red.length; i += 0x8000) binary += String.fromCharCode.apply(null, red.subarray(i, i + 0x8000));
          return [canvas.height, btoa(binary)];
        }}"""
    )
    gray = Image.frombytes("L", (384, rows), base64.b64decode(red))
    ink = gray.point(lambda value: 255 if value < 128 else 0).convert("1", dither=Image.Dither.NONE)
    return rows, ink.tobytes()


async def click_print(page):
    before = len(await calls(page, "print_bitmap"))
    await page.locator("#print").click()
    await page.wait_for_function(
        f"() => window.calls.filter((c) => c.service === 'print_bitmap').length > {before} && !{PANEL}.pendingCall"
    )
    return (await calls(page, "print_bitmap"))[-1]["service_data"]


# --------------------------------------------------------------------------------- checks


async def check_no_preview_and_load(p, browser):
    async with panel(p, browser) as page:
        assert await page.locator("#preview").count() == 0
        assert [c["service"] for c in await calls(page)] == ["printer_info"]
        assert await page.locator("#template option").count() == 6


async def check_initial_blockers(p, browser):
    """Before the first result only render_pending (and the missing input) speak."""
    async with panel(p, browser, settle=False, font_delay_ms=400) as page:
        await page.wait_for_function(f"() => {PANEL}.loaded")
        codes = await page.evaluate(f"() => {PANEL}.evaluate().codes")
        assert codes == ["no_input", "required_blank", "render_pending"], codes
        await settle(page)
        codes = await page.evaluate(f"() => {PANEL}.evaluate().codes")
        assert codes == ["no_input", "required_blank"], codes
        await fill_text(page, "Привет")
        await settle(page)
        assert await page.evaluate(f"() => {PANEL}.evaluate().codes") == []
        # An inexact result blocks, and Print() is then a no-op even when the button is clicked.
        await page.evaluate(
            f"""() => {{ const p = {PANEL}; p.lastResult = {{ ...p.lastResult, exact: false, document: null }}; p.updateButtons(); }}"""
        )
        assert await page.evaluate(f"() => {PANEL}.evaluate().codes") == ["render_approximate"]
        assert await page.locator("#print").is_disabled()
        await page.locator("#print").dispatch_event("click")
        await page.wait_for_timeout(50)
        assert not await calls(page, "print_bitmap")


async def check_ghost_blocks_print(p, browser):
    async with panel(p, browser) as page:
        assert await page.locator("#print").is_disabled()
        assert (await texts(page))["no_input"] in await page.locator("#print").get_attribute(
            "title"
        )
        assert await page.evaluate(f"() => {PANEL}.lastResult.ghost === true")
        opacity = await page.evaluate(
            f"() => Number(getComputedStyle({PANEL}.shadowRoot.querySelector('#raster')).opacity)"
        )
        assert opacity < 1, opacity
        await fill_text(page, "Ж")
        await settle(page)
        opacity = await page.evaluate(
            f"() => Number(getComputedStyle({PANEL}.shadowRoot.querySelector('#raster')).opacity)"
        )
        assert opacity == 1, opacity
        await fill_text(page, "")
        await settle(page)
        assert await page.evaluate(f"() => {PANEL}.lastResult.ghost === true")
        ink = await page.evaluate(
            f"""() => {{
              const c = {PANEL}.shadowRoot.querySelector('#raster');
              const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
              let n = 0; for (let i = 0; i < d.length; i += 4) if (d[i] !== 255) n += 1; return n;
            }}"""
        )
        assert ink > 0
        assert await page.evaluate(f"() => {PANEL}.getDocument()") is None
        await page.locator("#print").dispatch_event("click")
        await page.wait_for_timeout(50)
        assert not await calls(page, "print_bitmap")
        # Examples are placeholders, never values.
        assert await page.locator("#field-text").input_value() == ""
        assert await page.locator("#field-text").get_attribute("placeholder")


async def check_kept_result_has_only_packed_bytes(p, browser):
    async with panel(p, browser) as page:
        await fill_text(page, "Лента")
        await settle(page)
        kept = await page.evaluate(
            f"() => {{ const r = {PANEL}.lastResult; return [r.pixels, r.bitmap.length, r.rows]; }}"
        )
        assert kept[0] is None and kept[1] == kept[2] * 48, kept


async def check_job_state_before_mount(p, browser):
    page = await open_panel(p, browser, mount=False, delay_modules=400)
    try:
        await page.evaluate(MOUNT, False)
        unmounted, job = await page.evaluate(
            f"() => [{PANEL}.modules === null, {PANEL}.jobState('test-printer')]"
        )
        assert unmounted, "the modules loaded first: the check proves nothing"
        assert job["entityId"] is None and job["inFlight"] is False and job.get("state") is None
        assert not page.errors, page.errors
    finally:
        await page.context.browser.close()


async def check_one_character(p, browser):
    async with panel(p, browser) as page:
        await fill_text(page, "Я", "title")
        await settle(page)
        assert await page.locator("#print").is_disabled()
        title = await page.locator("#print").get_attribute("title")
        assert (await texts(page))["required_blank"] in title and "Текст" in title, title
        # What was typed stays on the strip while the required field is still blank: no error.
        assert await page.locator("#raster").is_visible()
        assert await page.locator("#notice").is_hidden()
        assert await page.evaluate(f"() => {PANEL}.lastResult.partial === true")
        assert await page.evaluate(f"() => {PANEL}.getDocument()") is None
        assert await page.evaluate(f"() => {PANEL}.evaluate().codes") == ["required_blank"]
        await page.locator("#template").select_option("qr_label")
        await fill_text(page, "Катушка", "title")
        await settle(page)
        # A label without its QR data draws nothing (not even a QR of an invisible character).
        assert (
            await page.locator("#raster").is_hidden() and await page.locator("#notice").is_hidden()
        )
        assert "QR" in await page.locator("#empty-text").inner_text()
        assert "QR" in await page.locator("#print").get_attribute("title")
        await page.locator("#template").select_option("note")
        await settle(page)
        await fill_text(page, "Ж")
        await fill_text(page, "", "title")
        await settle(page)
        assert await page.locator("#print").is_enabled()
        sent = await click_print(page)
        expected_rows, expected = await reference(page, note(text="Ж"))
        assert sent["rows"] == expected_rows
        assert base64.b64decode(sent["bitmap_b64"]) == expected
        assert set(sent) == {"device_id", "rows", "bitmap_b64"}


async def check_sent_bytes_equal_pixels(p, browser):
    async with panel(p, browser) as page:
        await fill_text(page, "Привет, мир\nвторая строка")
        await settle(page)
        sent = await click_print(page)
        rows, packed = await canvas_bytes(page)
        data = base64.b64decode(sent["bitmap_b64"])
        assert data == packed and sent["rows"] == rows and rows * 48 == len(data)
        shown = await page.locator("#diagnostic-data").text_content()
        assert hashlib.sha256(data).hexdigest() in shown
        assert await page.evaluate(
            f"() => {PANEL}.lastResult.generation === {PANEL}.scheduler.generation"
        )
    async with panel(p, browser) as page:
        rows = await page.evaluate(
            f"""async () => {{
              const p = {PANEL};
              const rowsOf = async (n) => {{
                try {{
                  return (await p.renderer.render({{ document: {{ kind: 'template', template: 'note', fields: {{ text: 'Строка\\n'.repeat(n) }}, options: {{}} }}, image: null, ghost: false, approximate: false, generation: 0 }})).rows;
                }} catch (error) {{
                  if (error.code === 'too_tall') return Infinity;
                  throw error;
                }}
              }};
              let n = 40;
              while ((await rowsOf(n + 1)) <= 2048) n += 1;
              return n;
            }}"""
        )
        await fill_text(page, "Строка\n" * rows)
        await settle(page)
        sent = await click_print(page)
        shown_rows, packed = await canvas_bytes(page)
        data = base64.b64decode(sent["bitmap_b64"])
        assert 1900 <= shown_rows <= 2048, shown_rows
        assert data == packed and sent["rows"] == shown_rows and shown_rows * 48 == len(data)


async def check_strip_is_crisp(p, browser):
    """R9: unpacked 1-bit bitmap, pixelated, integer scale; x3 screenshots (1x and 2x, overlays in place) are pure black and white."""
    async with panel(
        p, browser, viewport={"width": 1360, "height": 1080}, device_scale_factor=3
    ) as page:
        await fill_text(page, "Привет, мир\nвторая строка")
        await settle(page)
        # The inputs sit on the strip (their text is transparent): the bitmap under them is what is seen.
        assert await page.locator(".box .overlay").count() == 3
        for zoom in (1, 2):
            await set_zoom(page, zoom)
            if zoom == 2:
                # Let the whole doubled strip show: an element screenshot is clipped by the scroll box.
                await page.evaluate(
                    f"() => {{ const c = {PANEL}.shadowRoot.querySelector('.canvas'); c.style.overflow = 'visible'; c.style.maxHeight = 'none'; }}"
                )
            style = await page.evaluate(
                f"""() => {{
                  const c = {PANEL}.shadowRoot.querySelector('#raster'); const s = getComputedStyle(c);
                  return [s.imageRendering, parseFloat(s.width), c.width, window.devicePixelRatio, c.tagName, c.getBoundingClientRect().width];
                }}"""
            )
            assert style[0] == "pixelated" and style[4] == "CANVAS", style
            assert style[3] == 3 and style[1] % style[2] == 0 and style[5] == 384 * zoom, style
            # Blur the field: the caret and the focus ring are interface, not bitmap.
            await page.evaluate(f"() => {PANEL}.shadowRoot.activeElement?.blur()")
            await page.locator("#raster").scroll_into_view_if_needed()
            shot = OUT / f"strip-x3-{zoom}x.png"
            await page.locator("#raster").screenshot(path=str(shot))
            # The element box is rounded out to whole device pixels: crop the rim (page background and
            # shadow) so only the canvas pixels are judged.
            image = Image.open(shot).convert("RGB")
            image = image.crop((3, 3, image.width - 3, image.height - 3))
            assert image.width >= 384 * 3 * zoom - 6, image.size
            colors = {color for _, color in image.getcolors(maxcolors=image.width * image.height)}
            assert colors <= {(0, 0, 0), (255, 255, 255)}, (zoom, sorted(colors)[:6])
            assert (0, 0, 0) in colors
            shot.unlink()


async def check_one_click_sends_once(p, browser):
    async with panel(p, browser, printers=[printer(), printer("second")]) as page:
        await fill_text(page, "Раз")
        await settle(page)
        await page.evaluate("() => { window.printDelay = 100; }")
        await page.evaluate(
            f"() => {{ const b = {PANEL}.shadowRoot.querySelector('#print'); b.click(); b.click(); }}"
        )
        await page.wait_for_function(f"() => !{PANEL}.pendingCall")
        assert len(await calls(page, "print_bitmap")) == 1
        await page.locator("#print").dblclick()
        await page.wait_for_function(f"() => !{PANEL}.pendingCall")
        assert len(await calls(page, "print_bitmap")) == 2


async def check_nothing_prints_by_itself(p, browser, image_file):
    async with panel(p, browser, printers=[printer(), printer("second")]) as page:
        await fill_text(page, "Текст")
        await page.locator("#template").select_option("heading")
        await page.locator("#template").select_option("note")
        await page.locator("#device").select_option("second")
        await page.locator("#refresh").click()
        await page.locator('[data-mode="image"]').click()
        await page.locator("#image-file").set_input_files(str(image_file))
        await page.wait_for_function(f"() => {PANEL}.image")
        await settle(page)
        await page.locator('[data-mode="qr"]').click()
        await page.locator("#qr-data").fill("https://example.org")
        await settle(page)
        assert not await calls(page, "print_bitmap")


async def check_job_sensor(p, browser):
    async with panel(p, browser) as page:
        await fill_text(page, "Привет")
        await settle(page)
        t = await texts(page)
        labels = await page.evaluate("async (u) => (await import(u)).STATUS_TEXT", STATUS_JS)
        assert await page.locator("#print").is_enabled()
        for state in IN_FLIGHT:
            await set_state(page, state)
            assert await page.locator("#print").is_disabled(), state
            assert t["job_in_flight"] in await page.locator("#print").get_attribute("title")
            assert await page.locator("#job-status").inner_text() == labels[state]
        for state in ("unavailable", "unknown", "idle", "sent", "error", None):
            await set_state(page, state)
            assert await page.locator("#print").is_enabled(), state
            assert await page.locator("#job-status").is_hidden(), state
        # hass is read at every evaluation: mutate the live object without a setter call.
        await set_state(page, "idle")
        await page.evaluate("(e) => { window.hass.states[e] = { state: 'printing' }; }", ENTITY)
        await page.locator("#print").dispatch_event("click")
        await page.wait_for_timeout(50)
        assert not await calls(page, "print_bitmap")
        await page.evaluate("(e) => { window.hass.states[e] = { state: 'sent' }; }", ENTITY)
        await click_print(page)
    # Null entity_id: Print stays usable and only the pending call counts.
    async with panel(p, browser, printers=[printer(entity_id=None)]) as page:
        await fill_text(page, "Привет")
        await settle(page)
        job = await page.evaluate(f"() => {PANEL}.jobState('test-printer')")
        assert job["entityId"] is None and job["inFlight"] is False and job.get("state") is None
        assert await page.locator("#print").is_enabled()
        await page.evaluate("() => { window.printDelay = 150; }")
        await page.evaluate(
            f"() => {{ const b = {PANEL}.shadowRoot.querySelector('#print'); b.click(); b.click(); }}"
        )
        assert await page.locator("#print").is_disabled()
        await page.wait_for_function(f"() => !{PANEL}.pendingCall")
        assert len(await calls(page, "print_bitmap")) == 1
        assert await page.locator("#print").is_enabled()


async def check_device_switch(p, browser):
    async with panel(p, browser, printers=[printer("first"), printer("second", None)]) as page:
        await fill_text(page, "Привет")
        await settle(page)
        await page.locator("#automation").evaluate("(e) => { e.open = true; }")
        await click_print(page)
        await page.wait_for_function(f"() => !{PANEL}.loading")
        assert "bitmap_sha256" in await page.locator("#diagnostic-data").text_content()
        assert '"device_id": "first"' in await page.locator("#action-data").text_content()
        await page.locator("#device").select_option("second")
        assert '"device_id": "second"' in await page.locator("#action-data").text_content()
        # The answer to the first printer's job is not shown under the second printer.
        assert "bitmap_sha256" not in await page.locator("#diagnostic-data").text_content()


async def check_in_flight_list_from_server(p, browser):
    async with panel(p, browser) as page:
        await fill_text(page, "Привет")
        await settle(page)
        await page.evaluate(f"() => {{ {PANEL}.inFlightStates = ['sent']; }}")
        await set_state(page, "sent")
        assert await page.locator("#print").is_disabled()
        await set_state(page, "printing")
        assert await page.locator("#print").is_enabled()


async def check_overlapping_renders(p, browser):
    async with panel(p, browser, settle=False, font_delay_ms=300) as page:
        await page.wait_for_function(f"() => {PANEL}.loaded")
        await set_range(page, "font_weight", 900)
        await fill_text(page, "Первый")
        await set_range(page, "font_weight", 800)
        await fill_text(page, "Второй")
        await settle(page)
        assert await page.evaluate(
            f"() => {PANEL}.lastResult.generation === {PANEL}.scheduler.generation"
        )
        rows, expected = await reference(
            page, {**note(text="Второй"), "options": {"font_weight": 800}}
        )
        shown_rows, packed = await canvas_bytes(page)
        assert packed == expected and shown_rows == rows
        sent = await click_print(page)
        assert base64.b64decode(sent["bitmap_b64"]) == expected


async def check_hass_before_modules(p, browser):
    page = await open_panel(p, browser, mount=False, delay_modules=400)
    try:
        await page.evaluate(MOUNT, False)
        await page.evaluate(
            f"""(e) => {{
              window.hass.states[e] = {{ state: 'printing' }};
              const panel = {PANEL};
              panel.hass = window.hass;
              window.readyAtSet = Boolean(panel.ready);
            }}""",
            ENTITY,
        )
        assert await page.evaluate("() => window.readyAtSet") is False
        await page.wait_for_function(SETTLED)
        assert await page.locator("#device option").count() == 1
        assert await page.locator("#job-status").inner_text() == "Печать"
        assert not page.errors, page.errors
    finally:
        await page.context.browser.close()


async def check_hass_after_modules(p, browser):
    page = await open_panel(p, browser, mount=False)
    try:
        await page.evaluate(MOUNT, False)
        await page.wait_for_function(f"() => {PANEL}.ready")
        assert await page.locator("#notice").is_hidden()
        await page.evaluate(f"() => {{ {PANEL}.hass = window.hass; }}")
        await page.wait_for_function(SETTLED)
        assert await page.locator("#device option").count() == 1
        assert not page.errors, page.errors
    finally:
        await page.context.browser.close()


async def check_reconnect_releases_fonts(p, browser):
    async with panel(p, browser) as page:
        await page.locator('[data-mode="qr"]').click()
        await page.locator("#qr-data").fill("https://example.org")
        await settle(page)
        faces = await page.evaluate("() => document.fonts.size")
        await page.evaluate(f"() => {{ window.panelEl = {PANEL}; window.panelEl.remove(); }}")
        assert await page.evaluate("() => document.fonts.size") < faces or faces == 0
        await page.evaluate("() => { document.body.append(window.panelEl); }")
        await settle(page)
        # The mount starts over: template tab, empty drafts, nothing left of the QR mode.
        assert await page.evaluate(f"() => {PANEL}.mode") == "template"
        assert await page.locator("#template-form").is_visible()
        await fill_text(page, "Снова")
        await settle(page)
        assert await page.locator("#print").is_enabled()
        await click_print(page)


async def check_print_finishing_while_detached(p, browser):
    async with panel(p, browser) as page:
        await fill_text(page, "Уходим")
        await settle(page)
        await page.evaluate(
            f"() => {{ window.printDelay = 300; window.panelEl = {PANEL}; {PANEL}.shadowRoot.querySelector('#print').click(); window.panelEl.remove(); }}"
        )
        await page.wait_for_timeout(600)
        await page.evaluate("() => { document.body.append(window.panelEl); }")
        await settle(page)
        assert await page.locator("#device option").count() == 1
        await fill_text(page, "Вернулись")
        await settle(page)
        assert await page.locator("#print").is_enabled()


async def check_edit_after_last_render(p, browser):
    async with panel(p, browser) as page:
        await fill_text(page, "Один")
        await settle(page)
        await page.evaluate(
            f"""() => {{
              const p = {PANEL}; const field = p.shadowRoot.querySelector('#field-text');
              field.value = 'Два'; field.dispatchEvent(new Event('input', {{ bubbles: true }}));
              p.shadowRoot.querySelector('#print').click();
            }}"""
        )
        assert not await calls(page, "print_bitmap")
        await settle(page)
        sent = await click_print(page)
        _, expected = await reference(page, note(text="Два"))
        assert base64.b64decode(sent["bitmap_b64"]) == expected


async def check_pending_blocks_print(p, browser):
    async with panel(p, browser, settle=False, font_delay_ms=300) as page:
        await page.wait_for_function(f"() => {PANEL}.loaded")
        await fill_text(page, "Дождись")
        assert await page.locator("#print").is_disabled()
        assert (await texts(page))["render_pending"] in await page.locator("#print").get_attribute(
            "title"
        )
        await page.locator("#print").dispatch_event("click")
        await page.wait_for_timeout(50)
        assert not await calls(page, "print_bitmap")
        await settle(page)
        sent = await click_print(page)
        _, expected = await reference(page, note(text="Дождись"))
        assert base64.b64decode(sent["bitmap_b64"]) == expected


async def assert_direct_print_refused(page, code, *, edit=None, second_guard=False):
    """`print()` itself, not the (disabled, so inert) button, must refuse while `code` blocks.

    `edit` types into the text field first. With `second_guard` the blocker re-check is replaced by
    "nothing blocks" for a second call, so a refusal can only come from the printable-result check.
    All of it runs in one page task: the state read and the calls cannot be separated by a render.
    """
    codes = await page.evaluate(
        f"""async ([edit, secondGuard]) => {{
          const p = {PANEL};
          if (edit) {{
            const field = p.shadowRoot.querySelector('#field-text');
            field.value = edit;
            field.dispatchEvent(new Event('input', {{ bubbles: true }}));
          }}
          const codes = p.evaluate().codes;
          await p.print();
          if (secondGuard) {{
            const real = p.evaluate;
            p.evaluate = () => ({{ codes: [], missing: [], template: null }});
            try {{ await p.print(); }} finally {{ p.evaluate = real; }}
          }}
          return codes;
        }}""",
        [edit, second_guard],
    )
    assert code in codes, (code, codes)
    assert not await calls(page, "print_bitmap"), (code, second_guard)


async def check_direct_print_respects_blockers(p, browser):
    """`print()` sends nothing in any blocker state, whatever calls it.

    The blocker re-check is exercised in every state. Where the render leaves no printable result
    (pending, an older result while an edit renders, approximate, ghost, partial, failed) the
    result check behind it is exercised alone as well.
    """
    async with panel(p, browser, settle=False, font_delay_ms=300) as page:
        await page.wait_for_function(f"() => {PANEL}.loaded")
        await fill_text(page, "Дождись")
        await assert_direct_print_refused(page, "render_pending", second_guard=True)
    async with panel(p, browser) as page:
        await fill_text(page, "Привет")
        await settle(page)
        await assert_direct_print_refused(page, "render_pending", edit="Привет!", second_guard=True)
    async with panel(p, browser) as page:
        await type_title_and_text(page)
        await set_range(page, "font_weight", 500, ("input",))
        await settle(page)
        await assert_direct_print_refused(page, "render_approximate", second_guard=True)
    async with panel(p, browser) as page:
        await assert_direct_print_refused(page, "no_input", second_guard=True)  # the ghost
    async with panel(p, browser) as page:
        await fill_text(page, "Я", "title")
        await settle(page)
        await assert_direct_print_refused(page, "required_blank", second_guard=True)
    async with panel(p, browser, settle=False, missing_font=True) as page:
        await page.wait_for_function(SETTLED)
        await fill_text(page, "Шрифт")
        await page.wait_for_function(f"() => !{PANEL}.scheduler.pending")
        await assert_direct_print_refused(page, "render_failed", second_guard=True)
    async with panel(p, browser) as page:
        await fill_text(page, "Привет")
        await settle(page)
        await set_state(page, "printing")
        await assert_direct_print_refused(page, "job_in_flight")
    async with panel(p, browser, printers=[]) as page:
        await fill_text(page, "Привет")
        await settle(page)
        await assert_direct_print_refused(page, "no_device")
    async with panel(p, browser) as page:
        await fill_text(page, "\u200b")
        await settle(page)
        await assert_direct_print_refused(page, "no_ink")
    async with panel(p, browser) as page:
        await fill_text(page, "а" * 1600, "title")
        await fill_text(page, "б" * 1600)
        await settle(page)
        await assert_direct_print_refused(page, "text_too_long")


async def check_font_failure(p, browser):
    async with panel(p, browser, settle=False, missing_font=True) as page:
        await page.wait_for_function(SETTLED)
        await fill_text(page, "Шрифт")
        await page.wait_for_function(f"() => !{PANEL}.scheduler.pending")
        assert await page.locator("#print").is_disabled()
        assert (await texts(page))["render_failed"] in await page.locator("#print").get_attribute(
            "title"
        )
        assert await page.evaluate(f"() => {PANEL}.evaluate().codes") == ["render_failed"]
        assert await page.locator("#notice").is_visible()
        assert "error" in (await page.locator("#notice").get_attribute("class") or "")
        assert await page.locator("#raster").is_hidden()
        await page.locator("#print").dispatch_event("click")
        await page.wait_for_timeout(50)
        assert not await calls(page, "print_bitmap")
        # A transient failure is retried by Refresh without editing the label.
        page.font_missing = False
        await page.locator("#refresh").click()
        await settle(page)
        assert await page.locator("#print").is_enabled()
        assert await page.locator("#raster").is_visible()


async def check_vanished_printer(p, browser):
    async with panel(p, browser, printers=[printer("first"), printer("second", None)]) as page:
        await fill_text(page, "Привет")
        await settle(page)
        assert await page.locator("#device").input_value() == "first"
        page.mock.printers = [printer("second", None)]
        await page.locator("#refresh").click()
        await page.wait_for_function(f"() => !{PANEL}.loading")
        assert await page.locator("#device").input_value() == ""
        # A second Refresh must not pick the remaining printer either.
        await page.locator("#refresh").click()
        await page.wait_for_function(f"() => !{PANEL}.loading")
        assert await page.locator("#device").input_value() == ""
        assert await page.locator("#print").is_disabled()
        assert (await texts(page))["no_device"] in await page.locator("#print").get_attribute(
            "title"
        )
        await page.locator("#print").dispatch_event("click")
        await page.wait_for_timeout(50)
        assert not await calls(page, "print_bitmap")
        await page.locator("#device").select_option("second")
        assert await page.locator("#print").is_enabled()
        # The list going through empty (a reload) must not hide the vanish either.
        page.mock.printers = [printer("first"), printer("second", None)]
        await page.locator("#refresh").click()
        await page.wait_for_function(f"() => !{PANEL}.loading")
        await page.locator("#device").select_option("second")
        page.mock.printers = []
        await page.locator("#refresh").click()
        await page.wait_for_function(f"() => !{PANEL}.loading")
        page.mock.printers = [printer("first"), printer("second", None)]
        await page.locator("#refresh").click()
        await page.wait_for_function(f"() => !{PANEL}.loading")
        assert await page.locator("#device").input_value() == ""
        assert await page.locator("#print").is_disabled()


async def check_over_limit(p, browser):
    async with panel(p, browser) as page:
        await fill_text(page, "а" * 1600, "title")
        await fill_text(page, "б" * 1600)
        await settle(page)
        assert await page.locator("#print").is_disabled()
        assert (await texts(page))["text_too_long"] in await page.locator("#print").get_attribute(
            "title"
        )
        assert await page.locator("#field-text").get_attribute("maxlength") == "3000"
        await page.locator("#template").select_option("qr_label")
        assert await page.locator("#field-qr").get_attribute("maxlength") == "500"
        await page.locator('[data-mode="qr"]').click()
        assert await page.locator("#qr-data").get_attribute("maxlength") == "500"


async def check_drafts_and_documents(p, browser, image_file):
    async with panel(p, browser) as page:
        seen = (
            """() => { const p = %s; window.seen = []; const render = p.renderer.render.bind(p.renderer);
          p.renderer.render = (input) => { window.seen.push(Object.keys(input.document?.options ?? {})); return render(input); }; }"""
            % PANEL
        )
        await page.evaluate(seen)
        await fill_text(page, "Заметка")
        await page.locator("#template").select_option("qr_label")
        assert await page.locator("#field-title").input_value() == ""
        await fill_text(page, "Катушка", "title")
        await page.locator("#template").select_option("note")
        assert await page.locator("#field-text").input_value() == "Заметка"
        await page.locator("#template").select_option("qr_label")
        assert await page.locator("#field-title").input_value() == "Катушка"
        await fill_text(page, "https://example.org/spool/42", "qr")
        await settle(page)
        sent = await page.evaluate("() => window.seen.at(-1)")
        assert not IMAGE_ONLY & set(sent), sent
        assert {"font_size", "qr_size"} <= set(sent), sent
        document = await page.evaluate(f"() => {PANEL}.getDocument()")
        assert document["kind"] == "template" and document["template"] == "qr_label"
        assert not IMAGE_ONLY & set(document["options"]), document
        assert document["fields"] == {"title": "Катушка", "qr": "https://example.org/spool/42"}
        await page.locator('[data-mode="qr"]').click()
        await page.locator("#qr-data").fill("https://example.org")
        await settle(page)
        document = await page.evaluate(f"() => {PANEL}.getDocument()")
        assert document == {
            "kind": "qr",
            "text": "https://example.org",
            "options": {"border": False, "qr_size": 320, "qr_correction": "M"},
        }
        await page.locator('[data-mode="image"]').click()
        await page.locator("#image-file").set_input_files(str(image_file))
        await page.wait_for_function(f"() => {PANEL}.image")
        await settle(page)
        assert await page.evaluate(f"() => {PANEL}.getDocument()") is None
        first = await page.evaluate(f"() => {PANEL}.lastResult.bitmap.join(',')")
        await page.locator("#preset").select_option("line_art")
        assert await page.locator("#method").input_value() == "threshold"
        assert await page.locator("#contrast").input_value() == "1.6"
        await settle(page)
        second = await page.evaluate(f"() => {PANEL}.lastResult.bitmap.join(',')")
        assert first != second
        options = await page.evaluate(f"() => {PANEL}.lastResult.document.options")
        assert options["preset"] == "line_art" and options["method"] == "threshold"
        assert options["contrast"] == 1.6 and options["brightness"] == 1.0
        sent = await click_print(page)
        assert set(sent) == {"device_id", "rows", "bitmap_b64"}


async def check_empty_modes(p, browser, image_file):
    async with panel(p, browser) as page:
        t = await texts(page)
        await fill_text(page, "Лента")
        await settle(page)
        assert await page.locator("#raster").is_visible()
        for mode in ("qr", "image"):
            await page.locator(f'[data-mode="{mode}"]').click()
            await settle(page)
            assert await page.locator("#raster").is_hidden(), mode
            assert await page.locator("#empty").is_visible(), mode
            assert await page.evaluate(f"() => {PANEL}.lastResult.empty === true")
            assert await page.locator("#print").is_disabled()
            assert t["no_input"] in await page.locator("#print").get_attribute("title")
            assert not await page.evaluate(f"() => {PANEL}.lastResult.ghost")
        await page.locator('[data-mode="template"]').click()
        await settle(page)
        assert await page.locator("#raster").is_visible()


async def check_upload_limit_and_rejected_print(p, browser):
    async with panel(p, browser) as page:
        await page.locator('[data-mode="image"]').click()
        await page.locator("#image-file").set_input_files(
            files=[
                {
                    "name": "big.png",
                    "mimeType": "image/png",
                    "buffer": b"\0" * (2 * 1024 * 1024 + 1),
                }
            ]
        )
        await page.wait_for_function(f"() => !{PANEL}.shadowRoot.querySelector('#notice').hidden")
        assert "2 МиБ" in await page.locator("#notice").inner_text()
        assert await page.locator("#print").is_disabled()
        assert not await calls(page, "print_bitmap")
    async with panel(p, browser) as page:
        page.mock.print_fail = True
        await fill_text(page, "Отказ")
        await settle(page)
        await page.locator("#print").click()
        await page.wait_for_function(f"() => !{PANEL}.pendingCall && window.calls.length > 1")
        assert "printer refused" in await page.locator("#notice").inner_text()
        assert await page.locator("#print").is_enabled()
        assert await page.evaluate(f"() => {PANEL}.pendingCall") is False


async def check_no_ink_blocks(p, browser):
    async with panel(p, browser) as page:
        await fill_text(page, "\u200b")
        await settle(page)
        assert await page.locator("#print").is_disabled()
        assert (await texts(page))["no_ink"] in await page.locator("#print").get_attribute("title")


async def check_diagnose_never_prints(p, browser):
    async with panel(p, browser) as page:
        await page.locator("#diagnose").click()
        await page.wait_for_function(f"() => !{PANEL}.pendingCall")
        assert len(await calls(page, "diagnose")) == 1
        assert not await calls(page, "print_bitmap")


async def run_renderer_check(page):
    if not await page.locator("#renderer-check").is_visible():
        await page.locator("#diagnostics > summary").click()
    await page.locator("#renderer-check").click()
    await page.wait_for_function(
        "() => /SELFTEST (PASS|FAIL)/.test("
        f"{PANEL}.shadowRoot.querySelector('#renderer-check-data').textContent)"
    )
    return await page.locator("#renderer-check-data").text_content()


def verdict(output):
    last = output.rstrip("\n").split("\n")[-1]
    assert last.startswith("SELFTEST "), output
    return last


async def panel_snapshot(page):
    return await page.evaluate(
        f"""() => {{
          const p = {PANEL};
          const canvas = p.shadowRoot.querySelector('#raster');
          return {{
            document: JSON.stringify(p.getDocument()),
            print: p.shadowRoot.querySelector('#print').disabled,
            generation: p.scheduler.generation,
            raster: canvas.toDataURL(),
          }};
        }}"""
    )


async def check_renderer_check_passes(p, browser):
    async with panel(p, browser) as page:
        await fill_text(page, "Проверка")
        await settle(page)
        before = await panel_snapshot(page)
        sent = len(await calls(page))
        first = await run_renderer_check(page)
        assert verdict(first) == "SELFTEST PASS", first
        assert first.count("PASS") >= 5 and "FAIL" not in first.replace("SELFTEST", ""), first
        assert "devicePixelRatio" in first and "OffscreenCanvas" in first, first
        assert "Проверка" not in first and "test-printer" not in first, first
        # Pasting it elsewhere must give the same text the second time.
        assert await run_renderer_check(page) == first
        assert await panel_snapshot(page) == before
        assert len(await calls(page)) == sent  # no service, no print
        await fill_text(page, "Другое")
        await settle(page)
        assert await page.locator("#print").is_enabled()
        assert (await panel_snapshot(page))["raster"] != before["raster"]


async def check_renderer_check_without_clipboard(p, browser):
    async with panel(p, browser, no_offscreen=True) as page:
        await page.evaluate(
            "() => Object.defineProperty(navigator, 'clipboard', { value: undefined, configurable: true })"
        )
        output = await run_renderer_check(page)
        assert verdict(output) == "SELFTEST PASS", output
        assert "HTMLCanvasElement" in output, output
        assert await page.locator("#renderer-check-copy").is_hidden()
        assert await run_renderer_check(page) == output
        assert await page.locator("#renderer-check").is_enabled()


async def check_renderer_check_copies(p, browser):
    async with panel(p, browser) as page:
        await page.evaluate(
            "() => Object.defineProperty(navigator, 'clipboard', "
            "{ value: { writeText: async (text) => { window.copied = text; } }, configurable: true })"
        )
        output = await run_renderer_check(page)
        await page.locator("#renderer-check-copy").click()
        await page.wait_for_function("() => window.copied !== undefined")
        assert await page.evaluate("() => window.copied") == output


STATUS = "#renderer-check-status"
VANISHED = "Выбранный принтер больше недоступен. Выберите принтер заново."


async def check_renderer_check_reports_failure(p, browser):
    async with panel(p, browser, settle=False, missing_font=True) as page:
        await page.wait_for_function(SETTLED)
        await fill_text(page, "Шрифт")
        await page.wait_for_function(f"() => !{PANEL}.scheduler.pending")
        render_error = await page.locator("#notice").text_content()
        sent = len(await calls(page))
        output = await run_renderer_check(page)
        assert verdict(output) == "SELFTEST FAIL faces_loaded", output
        assert "FAIL faces_loaded" in output and "Traceback" not in output and " at " not in output
        assert "error" in (await page.locator(STATUS).get_attribute("class") or "")
        assert "faces_loaded" in await page.locator(STATUS).text_content()
        # The check reports in its own line: the render error in the panel notice is untouched.
        assert await page.locator("#notice").text_content() == render_error
        assert len(await calls(page)) == sent
        # The panel keeps working: the font comes back and a normal render succeeds.
        page.font_missing = False
        again = await run_renderer_check(page)
        assert verdict(again) == "SELFTEST PASS", again
        assert "faces_loaded" not in await page.locator(STATUS).text_content()
        assert "error" not in (await page.locator(STATUS).get_attribute("class") or "")
        await page.locator("#refresh").click()
        await settle(page)
        assert await page.locator("#raster").is_visible()
        assert await page.locator("#print").is_enabled()


async def check_renderer_check_keeps_other_notices(p, browser):
    async with panel(p, browser, settle=False, missing_font=True) as page:
        await page.wait_for_function(SETTLED)
        await page.evaluate(f"(text) => {PANEL}.notice(text, true)", VANISHED)
        assert verdict(await run_renderer_check(page)) == "SELFTEST FAIL faces_loaded"
        assert await page.locator("#notice").text_content() == VANISHED
        assert "error" in (await page.locator("#notice").get_attribute("class") or "")
        page.font_missing = False
        assert verdict(await run_renderer_check(page)) == "SELFTEST PASS"
        assert await page.locator("#notice").text_content() == VANISHED
        # The notice lives by its own lifecycle: a later notice replaces it as before.
        await page.evaluate(f"() => {PANEL}.notice('Данные отправлены.')")
        assert await page.locator("#notice").text_content() == "Данные отправлены."


async def check_renderer_check_clipboard_without_write_text(p, browser):
    async with panel(p, browser) as page:
        await page.evaluate(
            "() => Object.defineProperty(navigator, 'clipboard', { value: {}, configurable: true })"
        )
        output = await run_renderer_check(page)
        assert verdict(output) == "SELFTEST PASS", output
        assert await page.locator("#renderer-check-copy").is_hidden()


async def check_renderer_check_copy_rejected(p, browser):
    async with panel(p, browser) as page:
        await page.evaluate(
            "() => Object.defineProperty(navigator, 'clipboard', { value: "
            "{ writeText: async () => { throw new DOMException('denied', 'NotAllowedError'); } }, "
            "configurable: true })"
        )
        output = await run_renderer_check(page)
        await page.locator("#renderer-check-copy").click()
        await page.wait_for_function(
            f"() => {PANEL}.shadowRoot.querySelector('{STATUS}').textContent.includes('Буфер обмена')"
        )
        assert "error" in (await page.locator(STATUS).get_attribute("class") or "")
        assert await page.locator("#renderer-check-data").text_content() == output
        # page.errors (pageerror, unhandled rejections) is asserted when the panel closes.
        assert verdict(await run_renderer_check(page)) == "SELFTEST PASS"


async def check_renderer_check_during_renders(p, browser):
    async with panel(p, browser, viewport={"width": 1400, "height": 1000}) as page:
        await type_title_and_text(page)
        await settle(page)
        # A render is pending when the check runs: the newest generation still wins.
        await page.locator("#diagnostics > summary").click()
        pending = await page.evaluate(
            f"""() => {{
              const p = {PANEL};
              const field = p.shadowRoot.querySelector('#field-text');
              field.value = 'Второй';
              field.dispatchEvent(new Event('input', {{ bubbles: true }}));
              p.shadowRoot.querySelector('#renderer-check').click();
              return p.scheduler.pending;
            }}"""
        )
        assert pending is True  # the check started while that render was still queued
        await page.wait_for_function(
            "() => /SELFTEST (PASS|FAIL)/.test("
            f"{PANEL}.shadowRoot.querySelector('#renderer-check-data').textContent)"
        )
        assert "SELFTEST PASS" in await page.locator("#renderer-check-data").text_content()
        await settle(page)
        last = await page.evaluate(
            f"() => {PANEL}.lastResult.generation === {PANEL}.scheduler.generation"
        )
        assert last is True
        assert await page.locator("#print").is_enabled()
        sent = await click_print(page)
        _, expected = await reference(page, await page.evaluate(f"() => {PANEL}.getDocument()"))
        assert base64.b64decode(sent["bitmap_b64"]) == expected
        # A width/weight drag is in flight (approximate render newest, Print blocked): the check
        # neither corrupts the strip nor ends the drag nor unblocks Print.
        slider = page.locator("#font_weight")
        box = await slider.bounding_box()
        y = box["y"] + box["height"] / 2
        await page.mouse.move(box["x"] + box["width"] * 0.5, y)
        await page.mouse.down()
        x = box["x"] + box["width"] * 0.62
        await page.mouse.move(x, y, steps=4)
        for _ in range(40):
            if int(await slider.input_value()) % 50:
                break
            x += 1
            await page.mouse.move(x, y)
        value = int(await slider.input_value())
        await settle(page)
        during = await page.evaluate(LAST)
        assert during["exact"] is False and await page.locator("#print").is_disabled()
        raster = (await panel_snapshot(page))["raster"]
        generation = await page.evaluate(f"() => {PANEL}.scheduler.generation")
        # Click through the DOM: a real pointer click would end the drag by itself.
        output = await page.evaluate(
            f"""async () => {{
              const p = {PANEL};
              p.shadowRoot.querySelector('#renderer-check').click();
              while (!/SELFTEST (PASS|FAIL)/.test(p.shadowRoot.querySelector('#renderer-check-data').textContent)) {{
                await new Promise((r) => setTimeout(r, 20));
              }}
              return p.shadowRoot.querySelector('#renderer-check-data').textContent;
            }}"""
        )
        assert verdict(output) == "SELFTEST PASS", output
        assert await page.evaluate(f"() => {PANEL}.scheduler.generation") == generation
        assert await page.evaluate(LAST) == during
        assert (await panel_snapshot(page))["raster"] == raster
        assert await page.locator("#print").is_disabled()
        assert int(await slider.input_value()) == value
        await page.mouse.up()
        await settle(page)
        after = await page.evaluate(LAST)
        assert after["exact"] is True and after["family"] == f"tp-roboto_flex-w100-W{value}", after
        assert await page.locator("#print").is_enabled()
        assert await page.evaluate(
            f"() => {PANEL}.lastResult.generation === {PANEL}.scheduler.generation"
        )


FACE_OF_RESULT = f"() => {PANEL}.lastResult.face.fontFace"
# Faces the self-test does not use: loading them fills the registry (limit 8) without
# touching the face of the document on screen.
OTHER_FACES = [(30, 500), (40, 500), (80, 500), (90, 500), (110, 500), (120, 500)]


async def check_renderer_check_evicts_and_redraws_face(p, browser):
    async with panel(p, browser) as page:
        await set_range(page, "font_width", 60)
        await set_range(page, "font_weight", 700)
        await fill_text(page, "Широкий жирный")
        await settle(page)
        before = await canvas_bytes(page)
        old = await page.evaluate_handle(FACE_OF_RESULT)
        family = await page.evaluate(f"() => {PANEL}.lastResult.face.family")
        # Fill the registry to its limit of 8 with faces newer than the one on screen.
        await page.evaluate(
            f"""async (faces) => {{
              for (const [width, weight] of faces) {{
                await {PANEL}.renderer.faceFor({{ font: 'roboto_flex', font_width: width, font_weight: weight }});
              }}
            }}""",
            OTHER_FACES,
        )
        assert await page.evaluate("(f) => document.fonts.has(f)", old), "premise: face loaded"
        assert await page.evaluate("() => document.fonts.size") == 8
        assert verdict(await run_renderer_check(page)) == "SELFTEST PASS"
        # The premise: the check's own faces pushed the displayed one out of the page.
        assert not await page.evaluate("(f) => document.fonts.has(f)", old), (
            "premise: the self-test evicted the displayed face"
        )
        # Until the next render the overlay inputs must still have a face of that family.
        assert await page.evaluate(
            """(family) => [...document.fonts].some(
              (f) => f.family.replace(/["']/g, '') === family && f.status === 'loaded')""",
            family,
        ), "the displayed family is gone from the page"
        assert await page.evaluate(OVERLAY_FAMILY) == family
        # Drawn again, the label comes from a NEW FontFace of the same family, with the same pixels.
        await fill_text(page, "Другое")
        await settle(page)
        await fill_text(page, "Широкий жирный")
        await settle(page)
        new = await page.evaluate_handle(FACE_OF_RESULT)
        assert await page.evaluate("([a, b]) => a !== b", [old, new]), "same stale FontFace"
        assert await page.evaluate("(f) => document.fonts.has(f)", new)
        assert await page.evaluate(f"() => {PANEL}.lastResult.face.family") == family
        assert await page.evaluate(OVERLAY_FAMILY) == family
        assert await canvas_bytes(page) == before
        document = await page.evaluate(f"() => {PANEL}.getDocument()")
        assert await canvas_bytes(page) == await reference(page, document)


async def check_renderer_check_keeps_tiny5_face(p, browser):
    async with panel(p, browser) as page:
        await page.locator("#font").select_option("tiny5")
        await fill_text(page, "Ёж Ґ Є")
        await settle(page)
        before = await canvas_bytes(page)
        family = await page.evaluate(f"() => {PANEL}.lastResult.face.family")
        assert verdict(await run_renderer_check(page)) == "SELFTEST PASS"
        assert await page.evaluate(f"() => document.fonts.has({PANEL}.lastResult.face.fontFace)")
        assert await page.evaluate(OVERLAY_FAMILY) == family
        await fill_text(page, "Ёж Ґ Є 2")
        await settle(page)
        await fill_text(page, "Ёж Ґ Є")
        await settle(page)
        assert await canvas_bytes(page) == before


async def check_injection(p, browser):
    async with panel(p, browser) as page:
        await fill_text(page, '<img src=x onerror="window.pwned=true">', "title")
        await fill_text(page, "<b>x</b>")
        await page.locator("#automation").evaluate("(e) => { e.open = true; }")
        await settle(page)
        await page.wait_for_timeout(30)
        assert await page.evaluate("() => window.pwned === undefined")
        assert await page.locator("#action-data img").count() == 0
        assert await page.locator("#paper img").count() == 0
        assert "<img" in await page.locator("#action-data").inner_text()


async def check_wording(p, browser, shoot):
    async with panel(p, browser, printers=[printer()]) as page:
        assert await page.locator("#device-status").inner_text() == "Не виден по Bluetooth"
        assert await page.locator("#diagnose").inner_text() == "Проверить Bluetooth"
    bridge = printer(transport=BRIDGE, local_scan_only=True, visible=False, rssi_raw=-97)
    async with panel(p, browser, printers=[bridge]) as page:
        assert await page.locator("#device-status").inner_text() == "Мост доступен"
        assert await page.locator("#diagnose").inner_text() == "Проверить через мост"
        diagnostic = await page.locator("#diagnostic-data").text_content()
        assert "local_rssi_raw" in diagnostic and '"rssi_raw"' not in diagnostic
        assert "token" not in diagnostic.lower() and "http://" not in diagnostic
        await fill_text(page, "Через мост")
        await settle(page)
        assert await page.locator("#print").is_enabled()
        await page.locator("#diagnostics").evaluate("(e) => { e.open = true; }")
        await shoot(page, "bridge-panel.png")
    local = printer(transport={"mode": "local_le", "local_adapter_configured": True}, visible=True)
    async with panel(p, browser, printers=[local]) as page:
        assert await page.locator("#device-status").inner_text() == "Принтер обнаружен"
        assert await page.locator("#diagnose").inner_text() == "Проверить подключение"
        await shoot(page, "local-le-panel.png")
    async with panel(p, browser, printers=[]) as page:
        option = await page.locator("#device option").first.inner_text()
        assert "предпросмотр" not in option, option


async def check_mobile_and_screens(p, browser, image_file, shoot):
    async with panel(p, browser, viewport={"width": 1360, "height": 1080}) as page:
        await fill_text(page, "Забрать посылку\nКупить корм")
        await settle(page)
        await shoot(page, "studio-desktop.png")
        await page.locator("#template").select_option("qr_label")
        await fill_text(page, "Катушка PLA", "title")
        await fill_text(page, "https://example.org/spool/42", "qr")
        await settle(page)
        await page.set_viewport_size({"width": 390, "height": 844})
        await shoot(page, "studio-mobile.png")
        await page.locator("#raster").scroll_into_view_if_needed()
        await shoot(page, "studio-mobile-preview.png")
        assert await page.evaluate(
            f"() => {PANEL}.shadowRoot.querySelector('main').scrollWidth <= 390"
        )
        await page.set_viewport_size({"width": 1360, "height": 1080})
        await page.locator('[data-mode="image"]').click()
        await page.locator("#image-file").set_input_files(str(image_file))
        await page.wait_for_function(f"() => {PANEL}.image")
        await settle(page)
        await page.evaluate(f"() => {{ {PANEL}.scrollTop = 0; }}")
        await shoot(page, "studio-image.png")


# ------------------------------------------------------- Task 18: paper strip and overlays

STRIP_JS = f"{STATIC}studio/strip.js"
# The line box centres a glyph row in `step`, which is ascent + descent + 5 rows: the overlay box sits
# half of those 5 rows higher so its baseline is the bitmap's (asserted below as step - ascent - descent == 5).
HALF_LEADING = -2.5
OVERLAY_RECT = f"""(key) => {{
  const root = {PANEL}.shadowRoot;
  const box = root.querySelector('.box').getBoundingClientRect();
  const r = root.querySelector('#field-' + key).getBoundingClientRect();
  const scale = box.width / 384;
  return {{ x: (r.left - box.left) / scale, y: (r.top - box.top) / scale, w: r.width / scale, h: r.height / scale }};
}}"""
PLAN_BLOCK = f"""(key) => {{
  const b = {PANEL}.lastResult.blocks.find((block) => block.field === key);
  return b && {{ x: b.x, y: b.text_y, w: b.w, h: b.h, step: b.step, ascent: b.ascent, descent: b.descent, size: b.size, align: b.align, lines: b.lines.length, family: b.face.family }};
}}"""
ACTIVE_ID = f"() => {PANEL}.shadowRoot.activeElement?.id ?? null"
TWO_FRAMES = "() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)))"
CANVAS_HASH = f"""() => {{
  const c = {PANEL}.shadowRoot.querySelector('#raster');
  const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
  let h = 0; for (let i = 0; i < d.length; i += 1) h = (h * 31 + d[i]) | 0;
  return [h, c.height];
}}"""


async def fill_note(page):
    await fill_text(page, "Заголовок", "title")
    await fill_text(page, "Первая строка\nВторая строка", "text")
    await fill_text(page, "Низ", "footer")
    await settle(page)


async def assert_rect(page, key, want, context):
    got = await page.evaluate(OVERLAY_RECT, key)
    for axis in "xywh":
        assert abs(got[axis] - want[axis]) <= 1, (context, key, axis, got, want)


async def assert_overlays_on_blocks(page, keys, context):
    for key in keys:
        block = await page.evaluate(PLAN_BLOCK, key)
        assert block["step"] - block["ascent"] - block["descent"] == 5, block
        want = {
            "x": block["x"],
            "y": block["y"] + HALF_LEADING,
            "w": block["w"],
            "h": block["h"] - 16,
        }
        await assert_rect(page, key, want, context)


async def check_edit_mode_table(p, browser):
    async with panel(p, browser) as page:
        table = await page.evaluate(
            """async (url) => {
              const { fieldEditMode, autoZoom } = await import(url);
              const mode = (field, columns, policy, font, width) => fieldEditMode(field, { columns, policy, font, width });
              return [
                mode('title', 1, 'overlay', 'roboto_flex', 100),
                mode('title', 1, 'overlay', 'tiny5', 200),
                mode('text', 1, 'overlay', 'roboto_flex', 100),
                mode('text', 2, 'overlay', 'roboto_flex', 100),
                mode('footer', 3, 'overlay', 'roboto_flex', 100),
                mode('qr', 1, 'overlay', 'roboto_flex', 100),
                mode('title', 1, 'docked-all', 'roboto_flex', 100),
                mode('title', 1, 'overlay', 'tiny5', 100),
                [autoZoom(767), autoZoom(768)],
              ];
            }""",
            STRIP_JS,
        )
        expected = [
            "overlay",
            "docked",
            "overlay",
            "docked",
            "overlay",
            "docked",
            "docked",
            "overlay",
        ]
        assert table == [*expected, [1, 2]], table
        assert (
            await page.evaluate("async (url) => (await import(url)).EDIT_POLICY", STRIP_JS)
            == "overlay"
        )


async def check_overlay_geometry(p, browser):
    """Overlay rect = block rect (x, text_y + half leading, w, h - 16) at 1x, 2x and on a phone."""
    async with panel(p, browser, viewport={"width": 1360, "height": 1080}) as page:
        await fill_note(page)
        await set_zoom(page, 1)
        await assert_overlays_on_blocks(page, ["title", "text", "footer"], "1x")
        await set_zoom(page, 2)
        matrix = await page.evaluate(
            f"() => getComputedStyle({PANEL}.shadowRoot.querySelector('.box')).transform"
        )
        assert matrix == "matrix(2, 0, 0, 2, 0, 0)", (
            matrix
        )  # an integer scale: the 1-bit bitmap never resamples
        await assert_overlays_on_blocks(page, ["title", "text", "footer"], "2x")
        await page.set_viewport_size({"width": 390, "height": 844})
        await assert_overlays_on_blocks(page, ["title", "text", "footer"], "2x at 390px")
        await set_zoom(page, 1)
        await assert_overlays_on_blocks(page, ["title", "text", "footer"], "1x at 390px")
        scrolls = await page.evaluate(
            f"() => {{ const c = {PANEL}.shadowRoot.querySelector('.canvas'); return [c.scrollWidth, c.clientWidth]; }}"
        )
        assert scrolls[0] > scrolls[1], scrolls  # narrow card: the strip scrolls, it is not shrunk


async def check_zoom_rule(p, browser):
    """2x iff the strip's content box is >= 768 CSS px; #zoom overrides it both ways."""
    for viewport, scale in ((1400, 2), (1000, 2), (390, 1)):
        async with panel(p, browser, viewport={"width": viewport, "height": 900}) as page:
            await page.wait_for_function(f"() => {PANEL}.zoom === {scale}")
            room = await page.evaluate(f"() => {PANEL}.strip.availableWidth()")
            assert (room >= 768) == (scale == 2), (viewport, room)
            stage = await page.evaluate(
                f"() => {PANEL}.shadowRoot.querySelector('.stage').getBoundingClientRect().width"
            )
            assert stage == 384 * scale, (viewport, stage)
            assert await page.locator("#zoom").inner_text() == f"{scale}×"
            other = 3 - scale
            await page.locator("#zoom").click()
            assert await page.evaluate(f"() => {PANEL}.zoom") == other
            assert (
                await page.locator("#zoom").get_attribute("aria-pressed") == str(other == 2).lower()
            )
            # A user choice sticks: a resize no longer changes it.
            await page.set_viewport_size({"width": 1400 if viewport == 390 else 390, "height": 900})
            await page.wait_for_timeout(100)
            assert await page.evaluate(f"() => {PANEL}.zoom") == other
            stage = await page.evaluate(
                f"() => {PANEL}.shadowRoot.querySelector('.stage').getBoundingClientRect().width"
            )
            assert stage == 384 * other, (viewport, stage)
    async with panel(p, browser, viewport={"width": 1400, "height": 900}) as page:
        # Without a user choice a resize re-picks the scale.
        await page.wait_for_function(f"() => {PANEL}.zoom === 2")
        await page.set_viewport_size({"width": 390, "height": 900})
        await page.wait_for_function(f"() => {PANEL}.zoom === 1")
        await page.set_viewport_size({"width": 1400, "height": 900})
        await page.wait_for_function(f"() => {PANEL}.zoom === 2")


async def set_zoom(page, zoom):
    if await page.evaluate(f"() => {PANEL}.zoom") != zoom:
        await page.locator("#zoom").click()
    assert await page.evaluate(f"() => {PANEL}.zoom") == zoom


async def check_overlay_font(p, browser):
    async with panel(p, browser) as page:
        await fill_note(page)
        for key in ("title", "text", "footer"):
            info = await page.evaluate(
                f"""(key) => {{
                  const el = {PANEL}.shadowRoot.querySelector('#field-' + key); const s = getComputedStyle(el);
                  return {{ tag: el.tagName, family: s.fontFamily, size: s.fontSize, color: s.color, caret: s.caretColor, align: s.textAlign, lineHeight: s.lineHeight, face: {PANEL}.lastResult.face.family, aria: el.getAttribute('aria-label') }};
                }}""",
                key,
            )
            block = await page.evaluate(PLAN_BLOCK, key)
            family = info["family"].strip("\"'")
            assert re.fullmatch(r"tp-[a-z0-9_]+-w\d+-W\d+", family), info
            assert family == info["face"] == block["family"], info
            assert info["tag"] == "TEXTAREA" and info["aria"], info
            assert (
                info["size"] == f"{block['size']}px" and info["lineHeight"] == f"{block['step']}px"
            ), (info, block)
            assert info["color"] in ("rgba(0, 0, 0, 0)", "transparent"), info
            assert info["caret"] not in ("rgba(0, 0, 0, 0)", "transparent", "auto"), info
            assert info["align"] == block["align"], (info, block)
        title = await page.evaluate(PLAN_BLOCK, "title")
        assert title["align"] == "center"
        await page.locator("#align").select_option("right")
        await page.locator("#font_size").fill("36")
        await settle(page)
        for key, align in (("text", "right"), ("title", "center"), ("footer", "center")):
            block = await page.evaluate(PLAN_BLOCK, key)
            style = await page.evaluate(
                f"(key) => {{ const s = getComputedStyle({PANEL}.shadowRoot.querySelector('#field-' + key)); return [s.fontSize, s.textAlign]; }}",
                key,
            )
            assert style == [f"{block['size']}px", align] and block["align"] == align, (
                key,
                style,
                block,
            )
        assert (await page.evaluate(PLAN_BLOCK, "text"))["size"] == 36
        await assert_overlays_on_blocks(page, ["title", "text", "footer"], "size 36, right")


def ink_rows(shot, top, bottom, threshold):
    image = Image.open(shot).convert("L")
    rows = [
        y
        for y in range(top, min(bottom, image.height))
        if any(image.getpixel((x, y)) < threshold for x in range(image.width))
    ]
    return rows[0], rows[-1]


async def baseline_shift(page, key, shot):
    """First/last ink rows of the bitmap vs those of the overlay text made visible, relative to text_y."""
    await set_zoom(page, 1)  # the screenshot is read in paper dots
    block = await page.evaluate(PLAN_BLOCK, key)
    top, bottom = block["y"], block["y"] + block["lines"] * block["step"]
    await page.evaluate(f"() => {PANEL}.shadowRoot.activeElement?.blur()")
    box = page.locator(".box")
    await box.screenshot(path=str(shot))
    drawn = ink_rows(shot, top, bottom, 128)
    await page.evaluate(
        f"""(key) => {{
          const root = {PANEL}.shadowRoot;
          root.querySelector('#raster').style.visibility = 'hidden';
          const el = root.querySelector('#field-' + key); el.style.color = '#000'; el.style.caretColor = 'transparent';
        }}""",
        key,
    )
    await box.screenshot(path=str(shot))
    typed = ink_rows(shot, top, bottom, 160)
    await page.evaluate(
        f"""(key) => {{
          const root = {PANEL}.shadowRoot;
          root.querySelector('#raster').style.visibility = '';
          const el = root.querySelector('#field-' + key); el.style.color = 'transparent';
        }}""",
        key,
    )
    return drawn, typed


async def check_overlay_ignores_page_direction(p, browser):
    """An RTL Home Assistant locale must not flip the bidi base of the overlays: the bitmap is LTR."""
    async with panel(p, browser) as page:
        await page.evaluate("() => { document.documentElement.dir = 'rtl'; }")
        await fill_note(page)
        for key in ("title", "text", "footer"):
            direction = await page.evaluate(
                f"(key) => getComputedStyle({PANEL}.shadowRoot.querySelector('#field-' + key)).direction",
                key,
            )
            assert direction == "ltr", (key, direction)


async def check_overlay_baseline(p, browser):
    """With the overlay text made visible its first and last ink rows equal the bitmap's within 1 dot."""
    shot = OUT / "baseline.png"
    async with panel(p, browser, viewport={"width": 1360, "height": 1080}) as page:
        await fill_text(page, "H", "title")
        await settle(page)
        drawn, typed = await baseline_shift(page, "title", shot)
        assert abs(drawn[0] - typed[0]) <= 1 and abs(drawn[1] - typed[1]) <= 1, (drawn, typed)
        await page.locator("#template").select_option("heading")
        await page.locator("#font_size").fill("48")
        await fill_text(page, "HHHH HHHH", "title")
        await settle(page)
        block = await page.evaluate(PLAN_BLOCK, "title")
        assert block["lines"] > 1 and block["size"] >= 60, block  # a wrapped heading
        drawn, typed = await baseline_shift(page, "title", shot)
        assert abs(drawn[0] - typed[0]) <= 1 and abs(drawn[1] - typed[1]) <= 1, (drawn, typed)
        # The largest size: a wrapping heading at 96 px. The overflow:hidden textarea must wrap
        # exactly like the renderer, or the caret would leave the drawn line (measured, not assumed).
        await page.locator("#font_size").fill("96")
        await settle(page)
        block = await page.evaluate(PLAN_BLOCK, "title")
        assert block["lines"] > 1 and block["size"] == 96, block
        drawn, typed = await baseline_shift(page, "title", shot)
        assert abs(drawn[0] - typed[0]) <= 1 and abs(drawn[1] - typed[1]) <= 1, (drawn, typed)
        fit = await page.evaluate(
            f"""() => {{ const e = {PANEL}.shadowRoot.querySelector('#field-title'); return [e.scrollHeight, e.clientHeight]; }}"""
        )
        assert fit[0] == fit[1] == block["lines"] * block["step"], (fit, block)
    shot.unlink(missing_ok=True)


async def check_typing_updates_strip(p, browser):
    """Acceptance 1: with the fonts loaded one keystroke changes the strip by the end of the next frame, no network."""
    async with panel(p, browser) as page:
        await fill_text(page, "Я")
        await settle(page)
        before = await page.evaluate(CANVAS_HASH)
        sent = len(await calls(page))
        requested = len(page.requests)
        # Armed in the capture phase, so its frame callback is queued before the panel's own and
        # the timeout runs after every callback of that frame (and the microtasks they start).
        await page.evaluate(
            """() => document.addEventListener('input', () => {
              window.afterNextFrame = new Promise((r) => requestAnimationFrame(() => setTimeout(r, 0)));
            }, { capture: true, once: true })"""
        )
        await page.keyboard.type("Ж")
        await page.evaluate("() => window.afterNextFrame")
        assert await page.evaluate(CANVAS_HASH) != before
        assert len(await calls(page)) == sent  # editing never calls a service
        assert len(page.requests) == requested, page.requests[requested:]  # nor loads anything


async def check_blank_field_keeps_focus(p, browser):
    """The first characters of a blank field re-render the strip; the element, focus and caret survive (1x and 2x)."""
    async with panel(p, browser) as page:
        for zoom in (1, 2):
            await set_zoom(page, zoom)
            await fill_text(page, "", "footer")
            await fill_text(page, "Заголовок", "title")
            await settle(page)
            chips = await page.locator("#chips button").all_inner_texts()
            assert chips == ["+ Текст", "+ Подпись снизу"], chips
            slot = await page.evaluate(
                f"() => {PANEL}.lastResult.slots.find((s) => s.field === 'footer')"
            )
            assert slot and not await page.evaluate(PLAN_BLOCK, "footer")
            await page.locator('#chips button[data-field="footer"]').click()
            assert await page.evaluate(ACTIVE_ID) == "field-footer"
            await page.evaluate(
                f"() => {{ window.keptFooter = {PANEL}.shadowRoot.querySelector('#field-footer'); }}"
            )
            for count, char in enumerate("abc", start=1):
                await page.keyboard.type(char)
                same, caret, value = await page.evaluate(
                    f"""() => {{ const el = {PANEL}.shadowRoot.querySelector('#field-footer');
                      return [el === window.keptFooter && {PANEL}.shadowRoot.activeElement === el, el.selectionStart, el.value]; }}"""
                )
                assert same and caret == count and value == "abc"[:count], (
                    zoom,
                    count,
                    same,
                    caret,
                    value,
                )
                if count == 1:
                    await settle(
                        page
                    )  # the first render (slot -> block) lands between two keystrokes
            await settle(page)
            same, caret, value = await page.evaluate(
                f"""() => {{ const el = {PANEL}.shadowRoot.querySelector('#field-footer');
                  return [el === window.keptFooter && {PANEL}.shadowRoot.activeElement === el, el.selectionStart, el.value]; }}"""
            )
            assert same and caret == 3 and value == "abc", (zoom, same, caret, value)
            block = await page.evaluate(PLAN_BLOCK, "footer")
            assert block["y"] == slot["y"], (
                block,
                slot,
            )  # the footer appears exactly where its slot was
            await assert_overlays_on_blocks(
                page, ["footer"], f"after the first characters at {zoom}x"
            )
            assert await page.locator("#chips button").all_inner_texts() == ["+ Текст"]
            assert await page.locator("#field-footer").count() == 1


STYLE_AND_RECT = f"""(key) => {{
  const el = {PANEL}.shadowRoot.querySelector('#field-' + key); const s = getComputedStyle(el);
  return [s.fontFamily, s.fontSize, s.textAlign, s.lineHeight];
}}"""


async def check_slot_overlay_lands_on_block(p, browser):
    """Every blank field of every template, at every alignment: the overlay does not move or restyle when its first character appears."""
    async with panel(p, browser) as page:
        await set_zoom(page, 1)
        templates = await page.evaluate(f"() => {PANEL}.templates")
        checked = 0
        for name, spec in templates.items():
            await page.locator("#template").select_option(name)
            for align in ("left", "center", "right"):
                for blank in spec["fields"]:
                    if blank == "qr":
                        continue  # always docked: no overlay to compare
                    for field in spec["fields"]:
                        await fill_text(page, "", field)
                    await page.locator("#align").select_option(align)
                    filled = [f for f in spec["required"] if f != blank]
                    if not filled:
                        filled = [next(f for f in spec["fields"] if f not in (blank, "qr"))]
                    for field in filled:
                        await fill_text(
                            page,
                            "https://example.org" if field == "qr" else f"т {field}",
                            field,
                        )
                    await settle(page)
                    context = (name, align, blank)
                    before = (
                        await page.evaluate(STYLE_AND_RECT, blank),
                        await page.evaluate(OVERLAY_RECT, blank),
                    )
                    await page.locator(f'#chips button[data-field="{blank}"]').click()
                    await page.keyboard.type("x")
                    await settle(page)
                    after = (
                        await page.evaluate(STYLE_AND_RECT, blank),
                        await page.evaluate(OVERLAY_RECT, blank),
                    )
                    assert before[0] == after[0], (context, before, after)
                    for axis in "xywh":
                        assert abs(before[1][axis] - after[1][axis]) <= 1, (context, before, after)
                    checked += 1
        assert checked == 3 * 19, checked


async def check_overlay_focus_indicator(p, browser):
    """Tabbing into an overlay shows a visible focus ring (the overlay text is transparent: the ring is the cue)."""
    async with panel(p, browser) as page:
        await fill_note(page)
        await page.locator("#field-title").focus()
        await page.keyboard.press("Tab")
        assert await page.evaluate(ACTIVE_ID) == "field-text"
        ring = await page.evaluate(
            f"""() => {{ const s = getComputedStyle({PANEL}.shadowRoot.activeElement);
              return [s.outlineStyle, parseFloat(s.outlineWidth), s.outlineColor]; }}"""
        )
        assert ring[0] != "none" and ring[1] >= 2 and ring[2] != "rgba(0, 0, 0, 0)", ring


async def check_unplaced_fields_stay_editable(p, browser):
    """qr_label with a blank QR draws nothing: the typed title must stay visible, focused and editable."""
    async with panel(p, browser) as page:
        await page.locator("#template").select_option("qr_label")
        await settle(page)
        await page.locator("#field-title").focus()
        await page.keyboard.type("Кат")
        await settle(page)
        assert await page.locator("#raster").is_hidden()
        assert await page.evaluate(ACTIVE_ID) == "field-title"
        info = await page.evaluate(
            f"""() => {{ const el = {PANEL}.shadowRoot.querySelector('#field-title'); const s = getComputedStyle(el);
              return [el.classList.contains('loose'), s.color, el.selectionStart, el.getBoundingClientRect().height]; }}"""
        )
        assert (
            info[0]
            and info[1] not in ("rgba(0, 0, 0, 0)", "transparent")
            and info[2] == 3
            and info[3] > 20
        ), info
        await page.keyboard.type("а")
        await fill_text(page, "https://example.org/x", "qr")
        await settle(page)
        assert await page.locator("#raster").is_visible()
        assert not await page.evaluate(
            f"() => {PANEL}.shadowRoot.querySelector('#field-title').classList.contains('loose')"
        )
        await assert_overlays_on_blocks(page, ["title"], "back on the strip")
        assert await page.locator("#field-title").input_value() == "Ката"


async def check_qr_and_columns_are_docked(p, browser):
    async with panel(p, browser) as page:
        await page.locator("#template").select_option("qr_label")
        await settle(page)
        qr = page.locator("#field-qr")
        assert await qr.evaluate("(e) => e.closest('#docked') !== null") and await qr.is_visible()
        assert await page.locator('label[for="field-qr"]').inner_text() == "Ссылка или данные QR"
        assert await page.locator("#chips button").all_inner_texts() == [
            "+ Заголовок",
            "+ Подпись снизу",
        ]
        assert await page.locator("#field-title").evaluate("(e) => e.closest('.box') !== null")
        await page.locator("#template").select_option("note")
        await fill_text(page, "Верх", "title")
        await fill_text(page, "один\nдва\nтри\nчетыре", "text")
        await settle(page)
        await page.locator("#columns").select_option("2")
        await settle(page)
        text = page.locator("#field-text")
        assert (
            await text.evaluate("(e) => e.closest('#docked') !== null") and await text.is_visible()
        )
        assert await text.input_value() == "один\nдва\nтри\nчетыре"
        color = await text.evaluate("(e) => getComputedStyle(e).color")
        assert color not in ("rgba(0, 0, 0, 0)", "transparent"), color
        assert await page.locator("#field-title").evaluate("(e) => e.closest('.box') !== null")
        assert await page.evaluate(
            f"() => getComputedStyle({PANEL}.shadowRoot.querySelector('#field-title')).color"
        ) in ("rgba(0, 0, 0, 0)", "transparent")
        assert await page.locator("#chips button").all_inner_texts() == ["+ Подпись снизу"]
        await assert_overlays_on_blocks(page, ["title"], "columns 2")
        # Typing in the docked textarea still reaches the strip.
        before = await page.evaluate(CANVAS_HASH)
        await text.fill("один\nдва")
        await settle(page)
        assert await page.evaluate(CANVAS_HASH) != before


async def check_ghost_overlays(p, browser):
    async with panel(p, browser) as page:
        assert await page.evaluate(f"() => {PANEL}.lastResult.ghost === true")
        for key in ("title", "text", "footer"):
            assert await page.evaluate(PLAN_BLOCK, key), key
        await assert_overlays_on_blocks(page, ["title", "text", "footer"], "ghost")
        for key in ("title", "text", "footer"):
            el = page.locator(f"#field-{key}")
            assert await el.input_value() == ""
            assert await el.get_attribute("placeholder")
            placeholder = await el.evaluate("(e) => getComputedStyle(e, '::placeholder').color")
            assert placeholder in ("rgba(0, 0, 0, 0)", "transparent"), placeholder
            assert await el.evaluate("(e) => getComputedStyle(e).pointerEvents") == "auto"
        await page.locator("#template").select_option("qr_label")
        await settle(page)
        example = await page.evaluate(f"() => {PANEL}.templates.qr_label.example.qr")
        qr = page.locator("#field-qr")
        assert await qr.get_attribute("placeholder") == example
        placeholder = await qr.evaluate("(e) => getComputedStyle(e, '::placeholder').color")
        assert placeholder not in ("rgba(0, 0, 0, 0)", "transparent"), placeholder
        assert await qr.evaluate("(e) => e.closest('#docked') !== null")


async def check_spool_anchors(p, browser):
    """Spool rows: the overlay starts where the plan's anchor says the value starts (left, centre, right)."""
    async with panel(p, browser) as page:
        await page.locator("#template").select_option("spool_label")
        await fill_text(page, "Катушка", "title")
        await fill_text(page, "PETG", "material")
        await fill_text(page, "Чёрный", "color")
        await fill_text(page, "750 г", "weight")
        starts = {}
        for align in ("left", "center", "right"):
            await page.locator("#align").select_option(align)
            await settle(page)
            anchors = await page.evaluate(
                f"() => {PANEL}.lastResult.blocks.find((b) => b.anchors).anchors"
            )
            assert [a["field"] for a in anchors] == ["material", "color", "weight"]
            for anchor in anchors:
                want = {
                    "x": anchor["x"],
                    "y": anchor["text_y"] + HALF_LEADING,
                    "w": anchor["w"],
                    "h": anchor["h"],
                }
                await assert_rect(page, anchor["field"], want, align)
            starts[align] = anchors[0]["x"]
            # The blank row (nozzle) waits in its slot, where its value would start.
            slot = await page.evaluate(
                f"() => {PANEL}.lastResult.slots.find((s) => s.field === 'nozzle')"
            )
            await assert_rect(
                page,
                "nozzle",
                {"x": slot["x"], "y": slot["y"] + HALF_LEADING, "w": slot["w"], "h": slot["h"]},
                f"{align} slot",
            )
            assert await page.evaluate(
                f"() => {PANEL}.shadowRoot.querySelector('#field-nozzle').classList.contains('slot')"
            )
        # Not vacuous: the three alignments really start the first value at three different places.
        assert starts["left"] < starts["center"] < starts["right"], starts
        order = await page.evaluate(
            f"() => [...{PANEL}.shadowRoot.querySelectorAll('.overlay')].map((e) => e.id)"
        )
        assert order == [
            "field-title",
            "field-material",
            "field-color",
            "field-weight",
            "field-nozzle",
        ], order


async def check_composition(p, browser):
    """An IME composition survives a re-render and reaches the draft only when it ends (Chromium CDP)."""
    if browser != "chromium":
        raise Skipped("Playwright can only drive a real composition through the Chromium protocol")
    async with panel(p, browser) as page:
        await fill_text(page, "Верх", "title")
        await settle(page)
        await page.locator("#field-text").focus()
        await page.evaluate(
            f"""() => {{ window.ime = [];
              const el = {PANEL}.shadowRoot.querySelector('#field-text');
              for (const type of ['compositionstart', 'compositionend']) el.addEventListener(type, () => window.ime.push(type)); }}"""
        )
        cdp = await page.context.new_cdp_session(page)
        generation = await page.evaluate(f"() => {PANEL}.scheduler.generation")
        await cdp.send(
            "Input.imeSetComposition", {"text": "п", "selectionStart": 1, "selectionEnd": 1}
        )
        await page.evaluate(TWO_FRAMES)
        assert await page.locator("#field-text").input_value() == "п"
        assert await page.evaluate(f"() => {PANEL}.scheduler.generation") == generation
        assert await page.evaluate("() => window.ime") == ["compositionstart"]
        # A render that starts for another reason must not cancel the composition.
        await page.evaluate(f"() => {PANEL}.renderNow()")
        await settle(page)
        # While composing, the draft has no text yet: a partial render, nothing printable.
        assert await page.evaluate(f"() => {PANEL}.lastResult.partial === true")
        assert await page.evaluate(f"() => {PANEL}.lastResult.document") is None
        await page.evaluate(TWO_FRAMES)
        assert await page.evaluate(ACTIVE_ID) == "field-text"
        assert await page.evaluate("() => window.ime") == ["compositionstart"]
        await cdp.send(
            "Input.imeSetComposition", {"text": "пр", "selectionStart": 2, "selectionEnd": 2}
        )
        assert await page.locator("#field-text").input_value() == "пр"
        await cdp.send("Input.insertText", {"text": "пр"})
        await settle(page)
        assert await page.locator("#field-text").input_value() == "пр"
        assert "compositionend" in await page.evaluate("() => window.ime")
        doc = await page.evaluate(f"() => {PANEL}.lastResult.document")
        assert doc["fields"] == {"title": "Верх", "text": "пр"}, doc
        draft = await page.evaluate(f"() => {PANEL}.getDocument()")
        assert draft["fields"] == {"title": "Верх", "text": "пр"}, draft


async def check_keyboard_and_names(p, browser):
    async with panel(p, browser) as page:
        await fill_note(page)
        captions = await page.evaluate(
            f"() => [...{PANEL}.shadowRoot.querySelectorAll('.overlay')].map((e) => [e.id, e.getAttribute('aria-label')])"
        )
        assert captions == [
            ["field-title", "Заголовок"],
            ["field-text", "Текст"],
            ["field-footer", "Подпись снизу"],
        ], captions
        await page.locator("#field-title").focus()
        order, ys = ["field-title"], []
        for _ in range(2):
            await page.keyboard.press("Tab")
            order.append(await page.evaluate(ACTIVE_ID))
        assert order == ["field-title", "field-text", "field-footer"], (
            order
        )  # Tab walks the strip top down
        for key in ("title", "text", "footer"):
            ys.append((await page.evaluate(OVERLAY_RECT, key))["y"])
        assert ys == sorted(ys), ys


async def check_overlay_text_is_not_html(p, browser):
    async with panel(p, browser) as page:
        await fill_text(page, '<img src=x onerror="window.pwned=true">', "title")
        await settle(page)
        assert await page.locator("#paper img").count() == 0
        assert await page.evaluate("() => window.pwned === undefined")
        assert (
            await page.locator("#field-title").input_value()
            == '<img src=x onerror="window.pwned=true">'
        )


async def check_single_line_fields_refuse_line_breaks(p, browser):
    async with panel(p, browser) as page:
        await fill_text(page, "Раз", "title")
        await page.locator("#field-title").focus()
        await page.keyboard.press("End")
        await page.keyboard.press("Enter")
        assert await page.locator("#field-title").input_value() == "Раз"
        await page.locator("#field-text").focus()
        await page.keyboard.type("а")
        await page.keyboard.press("Enter")
        await page.keyboard.type("б")
        assert await page.locator("#field-text").input_value() == "а\nб"


async def set_range(page, key, value, events=("input", "change")):
    """Set a slider the way the user's final value looks, firing only the events asked for."""
    await page.locator(f"#{key}").evaluate(
        "(e, [v, names]) => { e.value = v; for (const n of names) e.dispatchEvent(new Event(n, { bubbles: true })); }",
        [str(value), list(events)],
    )


LAST = f"""() => {{ const r = {PANEL}.lastResult; return {{ exact: r.exact, ghost: Boolean(r.ghost), document: r.document, snapped: r.snapped, family: r.face?.family ?? null }}; }}"""
OVERLAY_FAMILY = f"""() => getComputedStyle({PANEL}.shadowRoot.querySelector('#field-text')).fontFamily.replace(/["']/g, '')"""


async def type_title_and_text(page):
    await fill_text(page, "Заголовок", "title")
    await fill_text(page, "Широкий текст", "text")
    await settle(page)


async def check_slider_drag_blocks_print(p, browser):
    async with panel(p, browser) as page:
        await type_title_and_text(page)
        blocked = (await texts(page))["render_approximate"]
        await set_range(page, "font_weight", 500, ("input",))
        await settle(page)
        last = await page.evaluate(LAST)
        assert last["exact"] is False and last["document"] is None, (
            last
        )  # approximate even on the grid
        assert last["snapped"]["weight"] == 500, last
        assert await page.locator("#print").is_disabled()
        assert blocked in await page.locator("#print").get_attribute("title")
        await page.locator("#print").dispatch_event("click")
        await page.wait_for_timeout(100)
        assert await calls(page, "print_bitmap") == []
        await set_range(page, "font_weight", 500, ("change",))
        await settle(page)
        assert (await page.evaluate(LAST))["exact"] is True
        assert await page.locator("#print").is_enabled()
        # A keyboard step fires input and change together: the exact render still lands.
        await page.locator("#font_weight").focus()
        await page.keyboard.press("ArrowRight")
        await settle(page)
        assert (await page.evaluate(LAST))["exact"] is True
        assert await page.locator("#print").is_enabled()


async def check_slider_release_paths(p, browser):
    """A drag that ends without `change` (pointer cancelled or lost) still renders the exact value."""
    for event in ("pointercancel", "pointerup"):
        async with panel(p, browser) as page:
            await type_title_and_text(page)
            await set_range(page, "font_width", 87, ("input",))
            await settle(page)
            assert (await page.evaluate(LAST))["exact"] is False
            await page.evaluate(
                f"() => window.dispatchEvent(new PointerEvent('{event}', {{ bubbles: true }}))"
            )
            await settle(page)
            assert (await page.evaluate(LAST))["exact"] is True, event
            assert await page.locator("#print").is_enabled(), event


async def check_slider_drag_uses_approximate_face(p, browser):
    async with panel(p, browser, viewport={"width": 1400, "height": 1000}) as page:
        await type_title_and_text(page)
        slider = page.locator("#font_weight")
        box = await slider.bounding_box()
        y = box["y"] + box["height"] / 2
        await page.mouse.move(box["x"] + box["width"] * 0.5, y)
        await page.mouse.down()
        x = box["x"] + box["width"] * 0.62
        await page.mouse.move(x, y, steps=4)
        for _ in range(40):  # leave the grid: the approximate and exact weights must differ
            if int(await slider.input_value()) % 50:
                break
            x += 1
            await page.mouse.move(x, y)
        value = int(await slider.input_value())
        await settle(page)
        last = await page.evaluate(LAST)
        assert last["exact"] is False and last["document"] is None, last
        snapped = last["snapped"]["weight"]
        assert snapped != value and snapped % 50 == 0, (snapped, value)
        family = await page.evaluate(OVERLAY_FAMILY)
        assert family == last["family"] == f"tp-roboto_flex-w100-W{snapped}", (family, last)
        assert await page.locator("#print").is_disabled()
        await page.mouse.up()
        await settle(page)
        after = await page.evaluate(LAST)
        assert after["exact"] is True, after
        assert (
            await page.evaluate(OVERLAY_FAMILY)
            == after["family"]
            == f"tp-roboto_flex-w100-W{value}"
        )
        assert after["family"] != family
        assert await page.locator("#print").is_enabled()


async def check_slider_drag_in_blank_state(p, browser):
    async with panel(p, browser) as page:
        await set_range(page, "font_weight", 700, ("input",))
        await settle(page)
        assert (await page.evaluate(LAST))["ghost"] is True
        assert await page.locator("#raster").evaluate("(e) => e.classList.contains('ghost')")
        assert not await page.locator("#notice.error").count()
        assert await page.locator("#print").is_disabled()
        assert (await texts(page))["no_input"] in await page.locator("#print").get_attribute(
            "title"
        )


async def check_tiny5_restricts_controls(p, browser):
    async with panel(p, browser) as page:
        await page.locator("#font_size").fill("20")
        await set_range(page, "font_width", 60)
        await page.locator("#font").select_option("tiny5")
        await settle(page)
        size = page.locator("#font_size")
        assert await size.evaluate("(e) => e.tagName") == "SELECT"
        sizes = await size.locator("option").evaluate_all("(o) => o.map((x) => x.value)")
        assert sizes == ["8", "16", "24", "32", "40", "48"], sizes
        assert await size.input_value() == "16"
        assert not await page.locator("#font_weight").is_visible()
        width = page.locator("#font_width")
        assert await width.evaluate("(e) => e.tagName") == "SELECT"
        offered = await width.locator("option").evaluate_all(
            "(o) => o.map((x) => [x.value, x.textContent])"
        )
        assert offered == [["100", "1x"], ["200", "2x"]], offered
        assert await width.input_value() == "100"
        await page.locator("#font").select_option("roboto_flex")
        await settle(page)
        assert await page.locator("#font_width").input_value() == "100"
        assert await page.locator("#font_weight").input_value() == "400"
        assert await page.locator("#font_size").input_value() == "16"


async def check_hint_never_blocks(p, browser):
    async with panel(p, browser) as page:
        await type_title_and_text(page)
        assert not await page.locator("#font-hint").is_visible()
        await set_range(page, "font_weight", 801)
        await settle(page)
        want = await page.evaluate(
            "async (url) => (await import(url)).closingCounterHint('roboto_flex', 24, 100, 801)",
            f"{STATIC}studio/options.js",
        )
        assert want and await page.locator("#font-hint").inner_text() == want
        assert await page.locator("#print").is_enabled()
        await set_range(page, "font_weight", 400)
        await settle(page)
        assert not await page.locator("#font-hint").is_visible()


async def check_columns_visibility(p, browser):
    async with panel(p, browser) as page:
        assert await page.locator("#columns").is_visible()
        values = await page.locator("#columns option").evaluate_all("(o) => o.map((x) => x.value)")
        assert values == ["1", "2", "3"], values
        await page.locator("#template").select_option("qr_label")
        await settle(page)
        assert not await page.locator("#columns").is_visible()
        await page.locator("#template").select_option("note")
        assert await page.locator("#columns").is_visible()


async def check_option_names_are_valid_options(p, browser):
    template_keys = {
        "font",
        "font_size",
        "font_width",
        "font_weight",
        "align",
        "border",
        "columns",
        "qr_size",
        "qr_correction",
    }
    async with panel(p, browser) as page:
        await type_title_and_text(page)
        options = (await page.evaluate(f"() => {PANEL}.getDocument()"))["options"]
        assert set(options) == template_keys, options
        assert set(options) <= set(renderer.DEFAULTS)
        renderer.normalize_options(options)
        await page.locator('[data-mode="qr"]').click()
        await page.locator("#qr-data").fill("https://example.org")
        await settle(page)
        options = (await page.evaluate(f"() => {PANEL}.getDocument()"))["options"]
        assert set(options) == {"border", "qr_size", "qr_correction"}, options
        renderer.normalize_options(options)


async def check_browser_only_options_notice(p, browser):
    async with panel(p, browser) as page:
        await type_title_and_text(page)
        await page.locator("#automation summary").click()
        await page.wait_for_function(
            f"() => {PANEL}.shadowRoot.querySelector('#action-data').textContent !== ''"
        )
        assert "tinyprint.print_document" in await page.locator("#action-data").inner_text()
        await page.locator("#columns").select_option("2")
        await settle(page)
        shown = await page.locator("#action-data").inner_text()
        assert "только в панели" in shown and "print_document" not in shown, shown
        await page.locator("#columns").select_option("1")
        await settle(page)
        assert "tinyprint.print_document" in await page.locator("#action-data").inner_text()
        await page.locator("#font").select_option("tiny5")
        await page.locator("#font_width").select_option("200")
        await settle(page)
        shown = await page.locator("#action-data").inner_text()
        assert "только в панели" in shown and "print_document" not in shown, shown


async def check_full_range_sliders_change_strip(p, browser):
    async with panel(p, browser) as page:
        await type_title_and_text(page)
        for key, values in (
            ("font_width", (25, 50, 75, 100, 151)),
            ("font_weight", (100, 400, 700, 1000)),
        ):
            seen = []
            for value in values:
                await set_range(page, key, value)
                await settle(page)
                seen.append(tuple(await page.evaluate(CANVAS_HASH)))
            assert len(set(seen)) == len(values), (key, seen)
        await set_range(page, "font_width", 100)
        await set_range(page, "font_weight", 400)
        await settle(page)
        expected = await reference(
            page, {**note(title="Заголовок", text="Широкий текст"), "options": {}}
        )
        assert await canvas_bytes(page) == expected


async def check_image_mode_shows_only_image_options(p, browser, image_file):
    async with panel(p, browser) as page:
        await page.locator('[data-mode="image"]').click()
        for key in ("font", "font_size", "font_width", "font_weight", "align", "columns", "border"):
            assert not await page.locator(f"#{key}").is_visible(), key
        await page.locator("#preset").scroll_into_view_if_needed()
        assert await page.locator("#preset").is_visible()
        assert not await page.locator("#image-more").evaluate(
            "(e) => e.open"
        )  # the rest is collapsed
        await page.locator("#image-more summary").click()
        for key in ("method", "brightness", "threshold", "invert"):
            assert await page.locator(f"#{key}").is_visible(), key
        assert await page.evaluate(f"() => {PANEL}.getDocument()") is None
        await page.locator("#image-file").set_input_files(str(image_file))
        await page.wait_for_function(f"() => {PANEL}.image")
        await settle(page)
        assert await page.evaluate(f"() => {PANEL}.getDocument()") is None


async def check_option_controls_have_names(p, browser):
    keys = ("font", "font_size", "font_width", "font_weight", "columns", "align", "border")
    async with panel(p, browser) as page:
        for key in (*keys, "qr_size", "qr_correction"):
            name = await page.evaluate(
                f"""(key) => {{ const root = {PANEL}.shadowRoot; const e = root.querySelector('#' + key); const l = root.querySelector('label[for=' + key + ']') ?? e.closest('label'); return e.getAttribute('aria-label') || (l ? l.textContent.trim() : ''); }}""",
                key,
            )
            assert re.search("[А-Яа-я]", name), (key, name)


async def check_option_bar_stays_visible(p, browser):
    """Wide: the bar and the strip stay on screen while the page scrolls (sticky)."""
    async with panel(p, browser, viewport={"width": 1400, "height": 1000}) as page:
        await fill_text(page, "\n".join(f"строка {n}" for n in range(30)), "text")
        await settle(page)
        await page.wait_for_function(f"() => {PANEL}.shadowRoot.querySelector('.live.pinned')")
        position = await page.evaluate(
            f"() => getComputedStyle({PANEL}.shadowRoot.querySelector('.live')).position"
        )
        assert position == "sticky", position
        await page.evaluate(f"() => {{ {PANEL}.scrollTop = {PANEL}.scrollHeight; }}")
        await page.wait_for_timeout(100)
        for selector in ("#font_weight", "#font_width", "#font", ".canvas"):
            rect = await page.evaluate(
                f"(s) => {{ const r = {PANEL}.shadowRoot.querySelector(s).getBoundingClientRect(); return [r.top, r.bottom, innerHeight]; }}",
                selector,
            )
            assert rect[0] >= 0 and rect[1] <= rect[2] and rect[1] > rect[0], (selector, rect)


STRIP_VISIBLE = f"""() => {{
  const root = {PANEL}.shadowRoot;
  const clip = root.querySelector('.canvas').getBoundingClientRect();
  const stage = root.querySelector('.stage').getBoundingClientRect();
  const live = root.querySelector('.live');
  const top = Math.max(stage.top, clip.top, 0), bottom = Math.min(stage.bottom, clip.bottom, innerHeight);
  return {{ visible: Math.max(0, bottom - top), stage: stage.height, position: getComputedStyle(live).position,
    live: live.getBoundingClientRect().height, viewport: innerHeight, scrolls: {PANEL}.scrollHeight > {PANEL}.clientHeight }};
}}"""
LAYOUT_VIEWPORTS = ((1400, 900), (1366, 768), (390, 844))


SWEEP = f"""async (selector) => {{
  const measure = ({STRIP_VISIBLE});
  const host = {PANEL}, el = host.shadowRoot.querySelector(selector);
  const frames = () => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
  const max = host.scrollHeight - host.clientHeight;
  let worst = Infinity, positions = 0, last = null;
  for (let top = 0; top <= max + 40; top += 40) {{
    host.scrollTop = Math.min(top, max);
    await frames();
    const r = el.getBoundingClientRect();
    if (r.top < 0 || r.bottom > innerHeight) continue; // only where the slider itself can be used
    last = measure(); positions += 1; worst = Math.min(worst, last.visible);
  }}
  return {{ ...last, worst, positions }};
}}"""


LAYOUT_LOG = []  # (viewport, slider, least visible paper px, card height, position) per measured sweep


async def assert_strip_visible_at(page, slider, where):
    """Wherever the page is scrolled with the slider on screen, 240 px of the strip are on screen too."""
    await page.wait_for_timeout(150)  # the layout observers run
    seen = await page.evaluate(SWEEP, slider)
    LAYOUT_LOG.append((where, slider, round(seen["worst"]), round(seen["live"]), seen["position"]))
    assert seen["positions"] > 0, (where, slider, seen)
    assert seen["worst"] >= min(240, seen["stage"]) - 0.5, (where, slider, seen)
    # A pinned block must leave the rest of the page reachable: it never fills the viewport.
    assert seen["position"] != "sticky" or seen["viewport"] - seen["live"] >= 150, (where, seen)
    assert seen["position"] == "sticky" or seen["scrolls"], (where, seen)


async def check_strip_stays_visible_with_options(p, browser, image_file):
    """R17/R18: with a slider in use, 240 CSS px of the strip (or all of it) are on screen, unscrolled."""
    for width, height in LAYOUT_VIEWPORTS:
        where = f"{browser} {width}x{height}"
        async with panel(p, browser, viewport={"width": width, "height": height}) as page:
            await fill_text(page, "Заголовок", "title")
            await fill_text(page, "\n".join(f"строка {n}" for n in range(12)), "text")
            await settle(page)
            for slider in ("#font_weight", "#font_width"):
                await assert_strip_visible_at(page, slider, where)
            await page.locator('[data-mode="image"]').click()
            await page.locator("#image-file").set_input_files(str(image_file))
            await page.wait_for_function(f"() => {PANEL}.image")
            await settle(page)
            await page.locator("#image-more summary").click()
            for slider in ("#brightness", "#threshold"):
                await assert_strip_visible_at(page, slider, f"{where} image")


async def check_short_viewport_does_not_pin(p, browser):
    """A landscape phone has no room for a pinned strip: the card scrolls and the page stays reachable."""
    async with panel(p, browser, viewport={"width": 844, "height": 390}) as page:
        await fill_text(page, "Заголовок", "title")
        await settle(page)
        await page.wait_for_timeout(150)
        seen = await page.evaluate(STRIP_VISIBLE)
        assert seen["position"] != "sticky" and seen["scrolls"], seen
        await page.locator("#print").scroll_into_view_if_needed()
        rect = await page.locator("#print").bounding_box()
        assert 0 <= rect["y"] and rect["y"] + rect["height"] <= 390, rect


async def check_notice_keeps_pin(p, browser):
    """A notice (print result, error) never unpins the card, and a huge one cannot swallow the page."""
    async with panel(p, browser, viewport={"width": 1366, "height": 768}) as page:
        await type_title_and_text(page)
        live = f"{PANEL}.shadowRoot.querySelector('.live')"
        await page.wait_for_function(f"() => {live}.classList.contains('pinned')")
        long_text = "Не удалось выполнить задание: " + "очень длинное сообщение об ошибке " * 60
        await page.evaluate(f"(text) => {PANEL}.notice(text, true)", long_text)
        await page.wait_for_timeout(150)
        assert await page.evaluate(f"() => {live}.classList.contains('pinned')")
        sizes = await page.evaluate(
            f"""() => {{ const n = {PANEL}.shadowRoot.querySelector('#notice');
              return [{live}.offsetHeight, {PANEL}.clientHeight, n.scrollHeight > n.clientHeight]; }}"""
        )
        assert sizes[0] <= sizes[1] - 50 and sizes[2], sizes  # the notice scrolls inside its cap
        seen = await page.evaluate(SWEEP, "#font_weight")
        assert seen["worst"] >= 240, seen
        # Re-fitting the card must not move the strip's own scroll position.
        await fill_text(page, "\n".join(f"строка {n}" for n in range(40)), "text")
        await settle(page)
        canvas = f"{PANEL}.shadowRoot.querySelector('.canvas')"
        before = await page.evaluate(f"() => ({canvas}.scrollTop = 150, {canvas}.scrollTop)")
        assert before > 0, before
        await page.evaluate(f"() => {PANEL}.notice('Готово')")
        await page.wait_for_timeout(150)
        assert await page.evaluate(f"() => {canvas}.scrollTop") == before


async def check_focus_clears_pinned_card(p, browser):
    """A control below the pinned card that takes focus is moved out from behind it."""
    async with panel(p, browser, viewport={"width": 1366, "height": 768}) as page:
        await type_title_and_text(page)
        live = f"{PANEL}.shadowRoot.querySelector('.live')"
        await page.wait_for_function(f"() => {live}.classList.contains('pinned')")
        await page.evaluate(
            f"() => {{ {PANEL}.shadowRoot.querySelector('#more-card').style.minHeight = '1600px'; }}"
        )
        await page.evaluate(
            f"""() => {{ const host = {PANEL}, t = host.shadowRoot.querySelector('#template');
              host.scrollTop = 600; host.scrollTop += t.getBoundingClientRect().top - 100; t.focus({{ preventScroll: true }}); }}"""
        )
        await page.wait_for_timeout(100)
        gap = await page.evaluate(
            f"""() => {{ const t = {PANEL}.shadowRoot.querySelector('#template');
              return t.getBoundingClientRect().top - {live}.getBoundingClientRect().bottom; }}"""
        )
        assert gap >= 0, gap


async def check_tab_into_editor_clears_pinned_card(p, browser):
    """Tabbing out of the strip card into the editor card: the browser's own focus scroll leaves the control visible."""
    async with panel(p, browser, viewport={"width": 1366, "height": 768}) as page:
        await type_title_and_text(page)
        live = f"{PANEL}.shadowRoot.querySelector('.live')"
        await page.wait_for_function(f"() => {live}.classList.contains('pinned')")
        await page.locator("#font").focus()
        for _ in range(60):
            await page.keyboard.press("Tab")
            inside = await page.evaluate(
                f"() => Boolean({PANEL}.shadowRoot.activeElement?.closest('#editor-card'))"
            )
            if inside:
                break
        assert inside
        await page.wait_for_timeout(150)
        gap = await page.evaluate(
            f"""() => {{ const e = {PANEL}.shadowRoot.activeElement;
              return e.getBoundingClientRect().top - {live}.getBoundingClientRect().bottom; }}"""
        )
        assert gap >= 0, gap


async def check_restored_focus_does_not_scroll(p, browser):
    """Coming back to the window re-fires focus on the same control: the page the user scrolled stays put."""
    async with panel(p, browser, viewport={"width": 1366, "height": 768}) as page:
        await type_title_and_text(page)
        live = f"{PANEL}.shadowRoot.querySelector('.live')"
        await page.wait_for_function(f"() => {live}.classList.contains('pinned')")
        await page.evaluate(
            f"() => {{ {PANEL}.shadowRoot.querySelector('#more-card').style.minHeight = '1600px'; }}"
        )
        await page.locator("#template").focus()
        await page.evaluate(TWO_FRAMES)
        await page.wait_for_timeout(700)  # past the press window
        await page.evaluate(f"() => {{ {PANEL}.scrollTop = {PANEL}.scrollTop + 300; }}")
        before = await page.evaluate(f"() => {PANEL}.scrollTop")
        await page.evaluate(
            f"() => {{ const t = {PANEL}.shadowRoot.querySelector('#template'); t.dispatchEvent(new FocusEvent('focusin', {{ bubbles: true, composed: true }})); }}"
        )
        await page.evaluate(TWO_FRAMES)
        assert await page.evaluate(f"() => {PANEL}.scrollTop") == before


async def check_returning_focus_after_the_strip_is_a_move(p, browser):
    """Tab from the strip card back to the control that was focused before is corrected, not mistaken for a restore."""
    async with panel(p, browser, viewport={"width": 1366, "height": 768}) as page:
        await type_title_and_text(page)
        live = f"{PANEL}.shadowRoot.querySelector('.live')"
        await page.wait_for_function(f"() => {live}.classList.contains('pinned')")
        await page.evaluate(
            f"() => {{ {PANEL}.shadowRoot.querySelector('#more-card').style.minHeight = '1600px'; }}"
        )
        tab = page.locator('[data-mode="template"]')
        await tab.focus()
        await page.wait_for_timeout(700)
        await page.locator("#font_weight").focus()
        await page.evaluate(f"() => {{ {PANEL}.scrollTop = 700; }}")
        await page.evaluate(TWO_FRAMES)
        await page.locator("#font_weight").focus()
        for _ in range(60):
            await page.keyboard.press("Tab")
            if await page.evaluate(
                f"() => {PANEL}.shadowRoot.activeElement?.dataset?.mode === 'template'"
            ):
                break
        await page.wait_for_timeout(150)
        gap = await page.evaluate(
            f"""() => {{ const e = {PANEL}.shadowRoot.activeElement;
              return e.getBoundingClientRect().top - {live}.getBoundingClientRect().bottom; }}"""
        )
        assert gap >= 0, gap


async def check_unpinning_follows_with_a_button_focused(p, browser):
    """A focused button (after Print, say) does not stop the card from following when it unpins."""
    async with panel(p, browser, viewport={"width": 1400, "height": 900}) as page:
        live = f"{PANEL}.shadowRoot.querySelector('.live')"
        await page.wait_for_function(f"() => {live}.classList.contains('pinned')")
        await page.evaluate(
            f"() => {{ {PANEL}.shadowRoot.querySelector('#refresh').focus(); {PANEL}.scrollTop = {PANEL}.scrollHeight; }}"
        )
        await page.set_viewport_size({"width": 1400, "height": 500})
        await page.wait_for_function(f"() => !{live}.classList.contains('pinned')")
        top = await page.evaluate(f"() => {live}.getBoundingClientRect().top")
        assert -1 <= top <= 1, top


async def check_pointer_press_is_not_scrolled_away(p, browser):
    """Focus caused by a press must not move the page: the click would miss its target."""
    async with panel(p, browser, viewport={"width": 1366, "height": 768}) as page:
        await type_title_and_text(page)
        live = f"{PANEL}.shadowRoot.querySelector('.live')"
        await page.wait_for_function(f"() => {live}.classList.contains('pinned')")
        await page.evaluate(  # room to scroll the editor card up under the pinned card
            f"() => {{ {PANEL}.shadowRoot.querySelector('#more-card').style.minHeight = '1600px'; }}"
        )
        spot = await page.evaluate(
            f"""() => {{ const host = {PANEL}, t = host.shadowRoot.querySelector('#template');
              host.scrollTop = 600; // the card is stuck now
              host.scrollTop += t.getBoundingClientRect().top - ({live}.getBoundingClientRect().bottom - 10);
              const r = t.getBoundingClientRect();
              return [r.left + r.width / 2, {live}.getBoundingClientRect().bottom + 3, host.scrollTop]; }}"""
        )
        await page.mouse.click(spot[0], spot[1])
        await page.evaluate(TWO_FRAMES)  # keepFocusClear runs a frame after the focus
        await page.wait_for_timeout(50)
        assert await page.evaluate(f"() => {PANEL}.scrollTop") == spot[2]
        await page.keyboard.press("Escape")


async def check_unpinning_keeps_the_card_in_view(p, browser):
    """A card that stops fitting while the page is scrolled past it comes back to the top of the view."""
    async with panel(p, browser, viewport={"width": 1400, "height": 900}) as page:
        live = f"{PANEL}.shadowRoot.querySelector('.live')"
        await page.wait_for_function(f"() => {live}.classList.contains('pinned')")
        await page.evaluate(f"() => {{ {PANEL}.scrollTop = {PANEL}.scrollHeight; }}")
        await page.set_viewport_size({"width": 1400, "height": 500})
        await page.wait_for_function(f"() => !{live}.classList.contains('pinned')")
        top = await page.evaluate(f"() => {live}.getBoundingClientRect().top")
        assert -1 <= top <= 1, top


async def check_pin_follows_panel_height(p, browser):
    """Only the panel height changes: the card unpins on a short panel and pins again on a tall one."""
    async with panel(p, browser, viewport={"width": 1400, "height": 900}) as page:
        live = f"{PANEL}.shadowRoot.querySelector('.live')"
        await page.wait_for_function(f"() => {live}.classList.contains('pinned')")
        await page.set_viewport_size({"width": 1400, "height": 390})
        await page.wait_for_function(f"() => !{live}.classList.contains('pinned')")
        canvas = await page.evaluate(
            f"() => {{ const c = {PANEL}.shadowRoot.querySelector('.canvas').style; return [c.minHeight, c.maxHeight]; }}"
        )
        assert canvas == ["", ""], canvas
        await page.set_viewport_size({"width": 1400, "height": 900})
        await page.wait_for_function(f"() => {live}.classList.contains('pinned')")


async def check_printed_bytes_after_drag(p, browser):
    """A real drag to an off-grid weight: nothing prints from the approximate render, the exact bytes go."""
    async with panel(p, browser, viewport={"width": 1400, "height": 1000}) as page:
        await type_title_and_text(page)
        slider = page.locator("#font_weight")
        box = await slider.bounding_box()
        y = box["y"] + box["height"] / 2
        x = box["x"] + box["width"] * 0.6
        await page.mouse.move(box["x"] + box["width"] * 0.5, y)
        await page.mouse.down()
        await page.mouse.move(x, y, steps=4)
        for _ in range(40):
            if int(await slider.input_value()) % 50:
                break
            x += 1
            await page.mouse.move(x, y)
        value = int(await slider.input_value())
        assert value % 50, value
        await settle(page)
        assert (await page.evaluate(LAST))["exact"] is False
        assert await page.locator("#print").is_disabled()
        await page.locator("#print").dispatch_event("click")
        assert await calls(page, "print_bitmap") == []
        await page.mouse.up()
        await page.wait_for_function(
            f"() => {PANEL}.lastResult?.exact === true && !{PANEL}.scheduler.pending"
        )
        assert await calls(page, "print_bitmap") == []
        sent = await click_print(page)
        _, expected = await reference(
            page,
            {**note(title="Заголовок", text="Широкий текст"), "options": {"font_weight": value}},
        )
        assert base64.b64decode(sent["bitmap_b64"]) == expected
        assert len(await calls(page, "print_bitmap")) == 1


async def check_blurred_window_releases_drag(p, browser):
    """Alt-tab or a hidden tab mid-drag fires no pointer event: the exact render still lands."""
    for event, hide in (("blur", False), ("visibilitychange", True)):
        async with panel(p, browser) as page:
            await type_title_and_text(page)
            await set_range(page, "font_weight", 537, ("input",))
            await settle(page)
            assert (await page.evaluate(LAST))["exact"] is False
            assert await page.locator("#print").is_disabled()
            if hide:
                await page.evaluate(
                    "() => { Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'hidden' }); document.dispatchEvent(new Event('visibilitychange')); }"
                )
            else:
                await page.evaluate("() => window.dispatchEvent(new Event('blur'))")
            # The request is made at once; a really hidden tab only renders it when frames resume.
            assert await page.evaluate(f"() => {PANEL}.approximateRequested") is False, event
            await settle(page)
            assert (await page.evaluate(LAST))["exact"] is True, event
            assert await page.locator("#print").is_enabled(), event


CHECKS = [
    ("no #preview anywhere; only printer_info on load", check_no_preview_and_load, False),
    (
        "initial state: render_pending alone, no failed/approximate noise",
        check_initial_blockers,
        False,
    ),
    (
        "kept result holds the packed bytes, not the pixels",
        check_kept_result_has_only_packed_bytes,
        False,
    ),
    ("jobState works before the modules are mounted", check_job_state_before_mount, False),
    ("all blank shows a ghost and blocks Print", check_ghost_blocks_print, False),
    ("one character removes the ghost; required caption in tooltip", check_one_character, False),
    (
        "sent bytes equal the displayed pixels (incl. 2048-row text)",
        check_sent_bytes_equal_pixels,
        False,
    ),
    (
        "strip is pixelated at an integer scale; x3 screenshot is pure black/white",
        check_strip_is_crisp,
        False,
    ),
    ("one click sends once", check_one_click_sends_once, False),
    (
        "nothing prints on load, edit, upload, device or template switch",
        check_nothing_prints_by_itself,
        True,
    ),
    ("Print follows the job sensor; null entity; lazy hass", check_job_sensor, False),
    ("device switch refreshes the action and the job answer", check_device_switch, False),
    ("in-flight list comes from printer_info", check_in_flight_list_from_server, False),
    ("overlapping renders show the latest generation", check_overlapping_renders, False),
    ("hass before modules: no throw, replayed", check_hass_before_modules, False),
    ("hass after modules: still loads", check_hass_after_modules, False),
    (
        "leaving the panel releases fonts; coming back mounts again",
        check_reconnect_releases_fonts,
        False,
    ),
    ("a print finishing while the panel is detached", check_print_finishing_while_detached, False),
    (
        "an edit after the last render never changes what is stored",
        check_edit_after_last_render,
        False,
    ),
    ("a render that is still pending blocks Print", check_pending_blocks_print, False),
    ("a font load failure blocks Print with a notice", check_font_failure, False),
    ("a vanished printer is never replaced silently", check_vanished_printer, False),
    ("over-limit text is blocked by the blocker", check_over_limit, False),
    (
        "drafts per template; hidden controls not in getDocument; image print sends bytes only",
        check_drafts_and_documents,
        True,
    ),
    ("qr and image modes with no input show an empty strip", check_empty_modes, True),
    (
        "2 MiB upload limit; rejected print clears pendingCall",
        check_upload_limit_and_rejected_print,
        False,
    ),
    ("a label without ink blocks Print", check_no_ink_blocks, False),
    ("diagnosis never prints", check_diagnose_never_prints, False),
    ("renderer check passes and leaves the panel unchanged", check_renderer_check_passes, False),
    (
        "renderer check without Clipboard API or OffscreenCanvas",
        check_renderer_check_without_clipboard,
        False,
    ),
    ("renderer check output can be copied", check_renderer_check_copies, False),
    ("renderer check keeps other notices", check_renderer_check_keeps_other_notices, False),
    (
        "renderer check hides copy without writeText",
        check_renderer_check_clipboard_without_write_text,
        False,
    ),
    ("renderer check copy rejection shows a message", check_renderer_check_copy_rejected, False),
    (
        "renderer check during a render and a slider drag",
        check_renderer_check_during_renders,
        False,
    ),
    ("renderer check keeps the Tiny5 face", check_renderer_check_keeps_tiny5_face, False),
    (
        "renderer check evicts a face and the next render redraws it",
        check_renderer_check_evicts_and_redraws_face,
        False,
    ),
    (
        "renderer check reports a failing check and the panel keeps working",
        check_renderer_check_reports_failure,
        False,
    ),
    ("untrusted template content is text, not HTML", check_injection, False),
    ("transport wording: native, bridge, local LE, no printer", check_wording, "shoot"),
    ("mobile 390px has no horizontal overflow; screenshots", check_mobile_and_screens, "shoot+tmp"),
    ("edit mode table; integer zoom rule", check_edit_mode_table, False),
    ("overlay geometry equals block geometry at 1x, 2x and 390px", check_overlay_geometry, False),
    ("zoom is an integer 1x/2x toggle; narrow cards scroll", check_zoom_rule, False),
    ("overlay uses the renderer's font and the block's alignment", check_overlay_font, False),
    ("overlays ignore the page direction", check_overlay_ignores_page_direction, False),
    ("overlay baseline equals the bitmap baseline", check_overlay_baseline, False),
    ("typing in an overlay updates the strip within a frame", check_typing_updates_strip, False),
    ("print() itself refuses in the blocker states", check_direct_print_respects_blockers, False),
    (
        "a blank field stays reachable and keeps focus and caret",
        check_blank_field_keeps_focus,
        False,
    ),
    (
        "fields without a place on the strip stay editable",
        check_unplaced_fields_stay_editable,
        False,
    ),
    (
        "qr and the main text of a multi-column label are docked",
        check_qr_and_columns_are_docked,
        False,
    ),
    ("ghost state: overlays on ghost blocks, example placeholders", check_ghost_overlays, False),
    ("spool rows follow the plan anchors (left, centre, right)", check_spool_anchors, False),
    ("an IME composition survives a re-render", check_composition, False),
    (
        "a slot overlay lands on its block for every blank field, template and alignment",
        check_slot_overlay_lands_on_block,
        False,
    ),
    ("a focused overlay has a visible focus ring", check_overlay_focus_indicator, False),
    ("overlays have names and Tab follows the visual order", check_keyboard_and_names, False),
    ("overlay text is text, not HTML", check_overlay_text_is_not_html, False),
    ("single-line fields refuse line breaks", check_single_line_fields_refuse_line_breaks, False),
    ("slider drag blocks Print until release", check_slider_drag_blocks_print, False),
    ("a drag ending without change still renders exactly", check_slider_release_paths, False),
    (
        "slider drag shows the approximate face; release shows the exact one",
        check_slider_drag_uses_approximate_face,
        False,
    ),
    ("slider drag in the blank state keeps the ghost", check_slider_drag_in_blank_state, False),
    ("switching to Tiny5 restricts the controls", check_tiny5_restricts_controls, False),
    ("the closing-counter hint never blocks", check_hint_never_blocks, False),
    ("columns is hidden for qr_label and visible for note", check_columns_visibility, False),
    ("option names are valid Python options", check_option_names_are_valid_options, False),
    (
        "browser-only options replace the automation action with a notice",
        check_browser_only_options_notice,
        False,
    ),
    (
        "full-range sliders visibly change the strip",
        check_full_range_sliders_change_strip,
        False,
    ),
    ("image mode shows only image options", check_image_mode_shows_only_image_options, True),
    ("option controls have Russian accessible names", check_option_controls_have_names, False),
    ("option bar and strip stay visible while scrolling", check_option_bar_stays_visible, False),
    (
        "the strip stays visible while option controls are used",
        check_strip_stays_visible_with_options,
        True,
    ),
    ("a short viewport does not pin the strip", check_short_viewport_does_not_pin, False),
    (
        "the strip is pinned only while the panel height allows",
        check_pin_follows_panel_height,
        False,
    ),
    ("a notice never unpins the strip", check_notice_keeps_pin, False),
    ("focus is moved out from behind the pinned strip", check_focus_clears_pinned_card, False),
    (
        "a press is not scrolled away from under the pointer",
        check_pointer_press_is_not_scrolled_away,
        False,
    ),
    (
        "returning focus after the strip card is corrected",
        check_returning_focus_after_the_strip_is_a_move,
        False,
    ),
    (
        "a focused button does not stop the card following",
        check_unpinning_follows_with_a_button_focused,
        False,
    ),
    ("restored focus does not scroll the page", check_restored_focus_does_not_scroll, False),
    (
        "tabbing into the editor card clears the pinned strip",
        check_tab_into_editor_clears_pinned_card,
        False,
    ),
    ("an unpinned card stays in view", check_unpinning_keeps_the_card_in_view, False),
    (
        "printed bytes after a real slider drag equal the exact render",
        check_printed_bytes_after_drag,
        False,
    ),
    (
        "a blurred window or hidden tab releases a slider drag",
        check_blurred_window_releases_drag,
        False,
    ),
]


async def main() -> int:
    OUT.mkdir(exist_ok=True)
    gradient = Image.new("RGB", (360, 240))
    gradient.putdata(
        [
            (x * 255 // 359, y * 255 // 239, (x + y) * 255 // 598)
            for y in range(240)
            for x in range(360)
        ]
    )
    image_file = OUT / "browser-image.png"
    gradient.save(image_file)
    passed, skipped, failed = [], [], []
    first = browsers()[0]
    async with async_playwright() as p:
        for name in browsers():

            async def shoot(page, filename, _name=name):
                # One set of artifacts: the first configured browser writes them.
                if _name == first:
                    await page.screenshot(path=str(OUT / filename), full_page=True)

            for title, check, extra in CHECKS:
                args = {
                    True: (image_file,),
                    False: (),
                    "shoot": (shoot,),
                    "shoot+tmp": (image_file, shoot),
                }[extra]
                try:
                    await check(p, name, *args)
                    passed.append(f"[{name}] {title}")
                    print(f"ok   [{name}] {title}")
                except Skipped as reason:
                    skipped.append(f"[{name}] {title}: {reason}")
                    print(f"skip [{name}] {title}: {reason}")
                except Exception as error:  # noqa: BLE001 - report every failing check
                    line = traceback.extract_tb(error.__traceback__)[-1].lineno
                    failed.append(f"[{name}] {title}: {error!r} (line {line})")
                    print(f"FAIL [{name}] {title}: {error!r} (line {line})")
    for row in LAYOUT_LOG:
        print("layout", *row)
    report = {
        "checks": passed,
        "count": len(passed),
        "skipped": skipped,
        "failures": failed,
        "scope": "Bundled component + real renderer; mock HA/BLE, no hardware",
    }
    (OUT / "browser-results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"{len(passed)} ok, {len(skipped)} skipped, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
