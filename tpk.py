import asyncio
import json
import re
from urllib.parse import urlparse

from playwright.async_api import async_playwright

from utils import Cache, get_logger

log = get_logger(__name__)

CACHE_FILE = Cache("TPK", exp=28_800)

urls = {}


# ---------------------------
# PLAYWRIGHT STREAM CAPTURE
# ---------------------------
async def get_stream_playwright(page_url: str) -> str | None:
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
            )
            page = await context.new_page()

            m3u8_url = None

            def handle_response(response):
                nonlocal m3u8_url
                url = response.url
                if ".m3u8" in url:
                    m3u8_url = url

            page.on("response", handle_response)

            log.info(f"Opening page: {page_url}")

            await page.goto(page_url, timeout=60000)

            # Wait for iframe
            await page.wait_for_timeout(3000)

            # Try clicking play buttons / overlays
            try:
                await page.mouse.click(500, 400)
                await page.wait_for_timeout(2000)
                await page.mouse.click(500, 400)
            except:
                pass

            # Wait for network
            await page.wait_for_timeout(8000)

            await browser.close()

            if m3u8_url:
                log.info(f"Captured stream: {m3u8_url}")
                return m3u8_url

    except Exception as e:
        log.warning(f"Playwright error: {e}")

    return None


# ---------------------------
# LOAD JSON EVENTS
# ---------------------------
def load_events():
    data = CACHE_FILE.load()

    log.info(f"Loaded {len(data)} events from tpk.json")

    return data


# ---------------------------
# BUILD M3U OUTPUT
# ---------------------------
def build_outputs(entries):
    vlc_lines = ["#EXTM3U"]
    tivimate_lines = ["#EXTM3U"]

    for name, data in entries.items():
        url = data.get("url")
        if not url:
            continue

        logo = data.get("logo", "")
        tvg_id = data.get("id", "Live.Event.us")

        vlc_lines.append(
            f'#EXTINF:-1 tvg-id="{tvg_id}" tvg-logo="{logo}",{name}'
        )
        vlc_lines.append(url)

        tivimate_lines.append(
            f'#EXTINF:-1 tvg-id="{tvg_id}" tvg-logo="{logo}",{name}'
        )
        tivimate_lines.append(
            f'{url}|user-agent=Mozilla/5.0'
        )

    with open("tpk_vlc.m3u8", "w", encoding="utf-8") as f:
        f.write("\n".join(vlc_lines))

    with open("tpk.tivimate.m3u8", "w", encoding="utf-8") as f:
        f.write("\n".join(tivimate_lines))


# ---------------------------
# MAIN SCRAPER
# ---------------------------
async def scrape():
    events = load_events()

    updated = 0

    for i, (name, data) in enumerate(events.items(), start=1):
        if data.get("url"):
            continue

        link = data.get("link")
        if not link:
            continue

        log.info(f"Processing: {name}")

        stream = await get_stream_playwright(link)

        if stream:
            data["url"] = stream
            updated += 1
        else:
            log.warning(f"NO STREAM FOUND: {link}")

    CACHE_FILE.write(events)

    log.info(f"Updated {updated} streams")

    build_outputs(events)


# ---------------------------
# ENTRY
# ---------------------------
if __name__ == "__main__":
    asyncio.run(scrape())
