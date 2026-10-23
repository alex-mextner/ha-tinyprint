"""Shared Playwright harness for the renderer/panel browser scripts.

`https://ha.test/` is a blank page and `https://ha.test/tinyprint_static/t/**` serves the files of
`custom_components/tinyprint/frontend/`, so the modules load exactly as they do under Home Assistant.
"""

import asyncio
import mimetypes
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "custom_components" / "tinyprint" / "frontend"
ORIGIN = "https://ha.test"
STATIC = f"{ORIGIN}/tinyprint_static/t/"

sys.path.insert(0, str(Path(__file__).parent))
from shared_loader import load  # noqa: E402

BLANK = '<!doctype html><html lang="ru"><meta charset="utf-8"><title>TinyPrint harness</title><body></body></html>'
NO_OFFSCREEN = "delete window.OffscreenCanvas;"


def browsers() -> list[str]:
    return [
        b.strip()
        for b in os.environ.get("TINYPRINT_BROWSERS", "chromium,firefox").split(",")
        if b.strip()
    ]


def font_table() -> dict:
    return load("fonts").FONTS


async def open_page(
    playwright,
    browser_name: str,
    *,
    font_delay_ms: int = 0,
    no_offscreen: bool = False,
    **page_options,
):
    launcher = getattr(playwright, browser_name)
    options = {}
    if browser_name == "chromium" and os.environ.get("CHROMIUM_PATH"):
        options["executable_path"] = os.environ["CHROMIUM_PATH"]
    browser = await launcher.launch(**options)
    page = await browser.new_page(**page_options)
    page.requests = []
    page.on("request", lambda request: page.requests.append(request.url))
    if no_offscreen:
        await page.add_init_script(NO_OFFSCREEN)

    async def serve(route):
        url = route.request.url
        if url == ORIGIN + "/":
            await route.fulfill(status=200, content_type="text/html", body=BLANK)
            return
        relative = url.removeprefix(STATIC).split("?", 1)[0]
        path = (FRONTEND / relative).resolve()
        if not path.is_file() or FRONTEND not in path.parents:
            await route.fulfill(status=404, body="not found")
            return
        if font_delay_ms and path.suffix == ".ttf":
            await asyncio.sleep(font_delay_ms / 1000)
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if path.suffix == ".js":
            content_type = "text/javascript"
        await route.fulfill(status=200, content_type=content_type, body=path.read_bytes())

    await page.route(f"{ORIGIN}/**", serve)
    await page.goto(ORIGIN + "/")
    return page
