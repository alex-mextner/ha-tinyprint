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
    """R9: unpacked 1-bit bitmap, pixelated, integer scale; x3 screenshot has only black and white."""
    async with panel(
        p, browser, viewport={"width": 1360, "height": 1080}, device_scale_factor=3
    ) as page:
        await fill_text(page, "Привет, мир\nвторая строка")
        await settle(page)
        style = await page.evaluate(
            f"""() => {{
              const c = {PANEL}.shadowRoot.querySelector('#raster'); const s = getComputedStyle(c);
              return [s.imageRendering, parseFloat(s.width), c.width, window.devicePixelRatio, c.tagName];
            }}"""
        )
        assert style[0] == "pixelated" and style[4] == "CANVAS", style
        assert style[3] == 3 and style[1] % style[2] == 0, style
        await page.locator("#raster").scroll_into_view_if_needed()
        shot = OUT / "strip-x3.png"
        await page.locator("#raster").screenshot(path=str(shot))
        # The element box is rounded out to whole device pixels: crop the rim (page background and
        # shadow) so only the canvas pixels are judged.
        image = Image.open(shot).convert("RGB")
        image = image.crop((3, 3, image.width - 3, image.height - 3))
        assert image.width >= 384 * 3 - 6, image.size
        colors = {color for _, color in image.getcolors(maxcolors=image.width * image.height)}
        assert colors <= {(0, 0, 0), (255, 255, 255)}, sorted(colors)[:6]
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
        # The panel has no weight control yet: the options hook stands in for it.
        await page.evaluate(
            f"""() => {{ const p = {PANEL}; const read = p.readOptions.bind(p); window.weight = 900; p.readOptions = () => ({{ ...read(), font_weight: window.weight }}); }}"""
        )
        await fill_text(page, "Первый")
        await page.evaluate("() => { window.weight = 800; }")
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


async def check_injection(p, browser):
    async with panel(p, browser) as page:
        await fill_text(page, '<img src=x onerror="window.pwned=true">', "title")
        await fill_text(page, "<b>x</b>")
        await page.locator("#automation").evaluate("(e) => { e.open = true; }")
        await settle(page)
        await page.wait_for_timeout(30)
        assert await page.evaluate("() => window.pwned === undefined")
        assert await page.locator("#action-data img").count() == 0
        assert await page.locator("#fields img").count() == 0
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
    ("untrusted template content is text, not HTML", check_injection, False),
    ("transport wording: native, bridge, local LE, no printer", check_wording, "shoot"),
    ("mobile 390px has no horizontal overflow; screenshots", check_mobile_and_screens, "shoot+tmp"),
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
    passed, failed = [], []
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
                except Exception as error:  # noqa: BLE001 - report every failing check
                    line = traceback.extract_tb(error.__traceback__)[-1].lineno
                    failed.append(f"[{name}] {title}: {error!r} (line {line})")
                    print(f"FAIL [{name}] {title}: {error!r} (line {line})")
    report = {
        "checks": passed,
        "count": len(passed),
        "failures": failed,
        "scope": "Bundled component + real renderer; mock HA/BLE, no hardware",
    }
    (OUT / "browser-results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
