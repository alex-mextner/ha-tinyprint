"""Exercise the bundled panel against a clearly labelled local mock-HA harness.

The renderer is real; BLE/HA service routing is simulated. No hardware claim.
Run: python tests/browser_studio.py
"""

import asyncio
import importlib.util
import json
import os
import sys
from pathlib import Path

from PIL import Image
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "studio_render", ROOT / "custom_components/tinyprint/studio_render.py"
)
renderer = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = renderer
spec.loader.exec_module(renderer)

HTML = """<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>TinyPrint | Test harness</title><style>body{margin:0;font-family:system-ui}.test-banner{font-size:11px;text-align:center;padding:5px;background:#fff2cc;color:#4c3900}tinyprint-panel{height:calc(100vh - 28px)}</style><div class="test-banner">TEST HARNESS · интерфейс и рендерер настоящие · не рабочий HA и не проверка бумажной печати</div><tinyprint-panel></tinyprint-panel></html>"""


async def service(request):
    name = request["name"]
    data = request["data"]
    if name == "printer_info":
        result = {
            "printers": [
                {
                    "device_id": "test-printer",
                    "name": "X6h · тестовый стенд",
                    "status": "idle",
                    "visible": False,
                    "rssi_raw": None,
                    "last_error": None,
                    "last_result": {},
                }
            ],
            "templates": renderer.TEMPLATES,
            "presets": renderer.PRESETS,
            "version": "0.2.0",
        }
    elif name == "preview":
        image = await asyncio.to_thread(renderer.render_document, data["document"])
        result = renderer.preview_result(image)
    elif name == "print_document":
        image = renderer.render_document(data["document"])
        result = renderer.preview_result(image)
        if result["bitmap_sha256"] != data["expected_sha256"]:
            raise ValueError("Hash mismatch")
        result = {
            "confirmation": "transport_only",
            "mock_transport": True,
            "bytes_written": len(renderer.bitmap(image)[0]),
        }
    elif name == "diagnose":
        result = {
            "supported_profile": True,
            "confirmation": "connection_only",
            "mock_transport": True,
            "bytes_written": 0,
        }
    elif name == "cancel":
        result = {"cancelled": True, "mock_transport": True}
    else:
        raise ValueError("Unknown mock service")
    return result


async def main():
    out = ROOT / "verification"
    out.mkdir(exist_ok=True)
    gradient = Image.new("RGB", (360, 240))
    gradient.putdata(
        [
            (x * 255 // 359, y * 255 // 239, (x + y) * 255 // 598)
            for y in range(240)
            for x in range(360)
        ]
    )
    image_file = out / "browser-image.png"
    gradient.save(image_file)
    errors = []
    checks = []
    async with async_playwright() as p:
        executable = os.environ.get("CHROMIUM_PATH", "/usr/bin/chromium")
        browser = await p.chromium.launch(
            executable_path=executable if Path(executable).exists() else None,
            headless=True,
            args=["--no-sandbox"],
        )
        page = await browser.new_page(
            viewport={"width": 1360, "height": 1080}, device_scale_factor=1
        )
        page.on("pageerror", lambda error: errors.append(str(error)))
        await page.expose_function(
            "studioService", lambda name, data: service({"name": name, "data": data})
        )
        await page.set_content(HTML)
        await page.add_script_tag(
            path=str(ROOT / "custom_components/tinyprint/frontend/tinyprint-panel.js")
        )
        await page.evaluate(
            """() => {window.calls=[]; window.previewDelay=0; window.printDelay=0; document.querySelector('tinyprint-panel').hass={callWS:async(request)=>{window.calls.push(structuredClone(request)); const delay=request.service==='preview'?window.previewDelay:request.service==='print_document'?window.printDelay:0; if(delay) await new Promise(r=>setTimeout(r,delay)); return {response: await window.studioService(request.service,request.service_data)};}};}"""
        )
        await page.wait_for_function("document.querySelector('tinyprint-panel').loaded")
        assert await page.locator("#template option").count() == 6
        assert await page.evaluate("calls.every(c=>c.service==='printer_info')")
        checks.append("initial load: six templates; no preview or print")
        await page.locator("#preview").click()
        await page.wait_for_function("!!document.querySelector('tinyprint-panel').signature")
        assert await page.locator("#raster").is_visible()
        assert await page.evaluate("!calls.some(c=>c.service==='print_document')")
        checks.append("real-renderer preview makes no print call")
        await page.screenshot(path=str(out / "studio-desktop.png"), full_page=True)
        await page.locator("#field-text").fill("Изменено после предпросмотра")
        assert await page.locator("#print").is_disabled()
        checks.append("editing invalidates the print authorization")
        await page.evaluate("previewDelay=150")
        await page.locator("#preview").click()
        await page.locator("#field-text").fill("Новый текст во время подготовки")
        await page.wait_for_function("!document.querySelector('tinyprint-panel').busy")
        assert await page.locator("#print").is_disabled()
        checks.append("stale async preview is ignored")
        await page.evaluate("previewDelay=0")
        await page.locator("#template").select_option("qr_label")
        await page.locator("#field-title").fill("Катушка PLA")
        await page.locator("#field-qr").fill("https://example.org/spool/42")
        await page.locator("#preview").click()
        await page.wait_for_function("!!document.querySelector('tinyprint-panel').signature")
        await page.set_viewport_size({"width": 390, "height": 844})
        await page.screenshot(path=str(out / "studio-mobile.png"), full_page=True)
        await page.locator("#raster").scroll_into_view_if_needed()
        await page.screenshot(path=str(out / "studio-mobile-preview.png"), full_page=True)
        assert await page.evaluate(
            "document.querySelector('tinyprint-panel').shadowRoot.querySelector('main').scrollWidth<=390"
        )
        checks.append("mobile 390px: no horizontal overflow")
        await page.evaluate("printDelay=150")
        await page.locator("#print").click()
        assert await page.locator("#print").is_disabled()
        await page.wait_for_function("!document.querySelector('tinyprint-panel').busy")
        assert await page.evaluate(
            "calls.filter(c=>c.service==='print_document').length===1 && calls.find(c=>c.service==='print_document').service_data.expected_sha256.length===64"
        )
        checks.append("one explicit print call; preview hash supplied; no double send")
        await page.locator('[data-mode="image"]').click()
        await page.locator("#image-file").set_input_files(str(image_file))
        await page.wait_for_function(
            "!!document.querySelector('tinyprint-panel').imageData", timeout=3000
        )
        checks.append("real file input/FileReader upload works")
        await page.locator("#preview").click()
        await page.wait_for_function("!!document.querySelector('tinyprint-panel').signature")
        first = await page.evaluate(
            "document.querySelector('tinyprint-panel').preview.bitmap_sha256"
        )
        await page.locator("#preset").select_option("line_art")
        assert await page.locator("#method").input_value() == "threshold"
        await page.locator("#preview").click()
        await page.wait_for_function("!!document.querySelector('tinyprint-panel').signature")
        second = await page.evaluate(
            "document.querySelector('tinyprint-panel').preview.bitmap_sha256"
        )
        assert first != second
        checks.append("image preset changes actual final bitmap")
        await page.set_viewport_size({"width": 1360, "height": 1080})
        await page.evaluate("document.querySelector('tinyprint-panel').scrollTop=0")
        await page.screenshot(path=str(out / "studio-image.png"), full_page=True)
        await page.locator("#diagnose").click()
        await page.wait_for_function("!document.querySelector('tinyprint-panel').busy")
        assert await page.evaluate(
            "calls.filter(c=>c.service==='print_document').length===1 && calls.filter(c=>c.service==='diagnose').length===1"
        )
        checks.append("diagnosis never invokes a print service")
        await page.locator('[data-mode="template"]').click()
        await page.locator("#field-title").fill('<img src=x onerror="window.pwned=true">')
        await page.locator("#automation").evaluate("(e)=>e.open=true")
        await page.wait_for_timeout(30)
        assert await page.evaluate("window.pwned===undefined")
        assert await page.locator("#action-data img").count() == 0
        checks.append("untrusted template content rendered as text, not HTML")
        assert not errors, errors
        checks.append("zero browser JavaScript exceptions")
        await browser.close()
    report = {
        "checks": checks,
        "count": len(checks),
        "browser_errors": errors,
        "scope": "Bundled component + real renderer; mock HA/BLE, no hardware",
    }
    (out / "browser-results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
