#!/usr/bin/env python3
"""Capture Google Maps network traffic used to render an area outline."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from playwright.async_api import async_playwright


DEFAULT_URL = (
    "https://www.google.com/maps/place/Bengaluru,+Karnataka/"
    "@12.9879249,77.4568178,11z/data=!3m1!4b1!4m6!3m5!"
    "1s0x3bae1670c9b44e6d:0xf8dfc3e8517e4fe0!8m2!3d12.9628957!"
    "4d77.57754!16zL20vMDljMTc?entry=ttu"
)


async def capture(url: str, output: Path, wait_seconds: float, show: bool) -> None:
    output.mkdir(parents=True, exist_ok=True)
    har_path = output / "maps.har"
    requests: list[dict[str, object]] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=not show, channel="chrome")
        context = await browser.new_context(
            viewport={"width": 1440, "height": 900},
            record_har_path=har_path,
            record_har_content="embed",
            record_har_mode="full",
            locale="en-IN",
        )
        page = await context.new_page()

        async def record(response) -> None:
            request = response.request
            headers = await response.all_headers()
            requests.append(
                {
                    "url": response.url,
                    "status": response.status,
                    "method": request.method,
                    "resource_type": request.resource_type,
                    "content_type": headers.get("content-type", ""),
                }
            )

        page.on("response", record)
        await page.goto(url, wait_until="domcontentloaded", timeout=90_000)
        await page.wait_for_timeout(int(wait_seconds * 1000))
        await page.screenshot(path=output / "maps.png", full_page=False)
        await context.close()
        await browser.close()

    (output / "requests.json").write_text(
        json.dumps(requests, indent=2), encoding="utf-8"
    )
    print(f"captured {len(requests)} responses in {output}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("url", nargs="?", default=DEFAULT_URL)
    parser.add_argument("--output", type=Path, default=Path("maps_boundary_capture"))
    parser.add_argument("--wait", type=float, default=12)
    parser.add_argument("--show", action="store_true")
    args = parser.parse_args()
    asyncio.run(capture(args.url, args.output, args.wait, args.show))


if __name__ == "__main__":
    main()
