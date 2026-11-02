"""Press the concept viewer's Save screenshot button in headless Chromium, as a user would.

Usage: python press_save.py URL VARIANT
Runs in cad-render's Playwright environment (`scriptenv.python_command(render.py, ...)`), whose
Chromium preflight `--warm` installs. Opens URL, clicks VARIANT in the variant list, clicks Save,
and prints one JSON object: the viewer's result line, the stage's CSS size, the device pixel
ratio, and every page error or console error.
"""

from __future__ import annotations

import json
import sys

from playwright.sync_api import TimeoutError as PlaywrightTimeout, sync_playwright

# SwiftShader WebGL, as cad-render launches it, so the viewer renders on hosts without a GPU.
CHROMIUM_ARGS = ["--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"]
SETTLED = ("() => { const r = document.getElementById('shot-result');"
           " return r.dataset.kind === 'error' || r.textContent.startsWith('Saved '); }")


def main(url: str, variant: str) -> int:
    errors: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=CHROMIUM_ARGS)
        try:
            page = browser.new_page(viewport={"width": 960, "height": 640}, device_scale_factor=2)
            page.set_default_timeout(60_000)
            page.on("pageerror", lambda error: errors.append(f"pageerror: {error}"))
            page.on("console", lambda message: errors.append(f"console: {message.text}")
                    if message.type == "error" else None)
            page.goto(url)
            page.wait_for_function("() => window.cadConcept && window.cadConcept.ready")
            page.evaluate("() => window.cadConcept.ready")
            page.click(f'button.variant[data-variant="{variant}"]')
            page.click("#screenshot")
            try:
                page.wait_for_function(SETTLED, timeout=30_000)
            except PlaywrightTimeout:
                pass  # report whatever the result line shows; the caller asserts on it
            report = {
                "result": page.text_content("#shot-result"),
                "status": page.text_content("#status"),
                "stage": page.evaluate("() => [document.getElementById('stage').clientWidth,"
                                       " document.getElementById('stage').clientHeight]"),
                "device_pixel_ratio": page.evaluate("() => window.devicePixelRatio"),
                "errors": errors,
            }
        finally:
            browser.close()
    print(json.dumps(report))
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:3]))
