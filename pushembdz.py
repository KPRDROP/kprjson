import asyncio
import re
from pathlib import Path
from urllib.parse import quote_plus

from playwright.async_api import async_playwright
from utils import Cache, Time, get_logger, leagues, network

log = get_logger(__name__)

TAG = "PUSHEMBDZ"

BASE_URL = "https://pushembdz.store/"
WATCH_BASE = f"{BASE_URL}/embed"

CACHE_FILE = Cache(f"{TAG.lower()}.json", exp=10_800)
OUTPUT_FILE = Path("pushembdz.m3u8")

# Encoded User-Agent for TiViMate pipe
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:146.0) "
    "Gecko/20100101 Firefox/146.0"
)
UA_ENC = quote_plus(UA)


# -------------------------------------------------
# Extract playable stream from embed URL (UNCHANGED)
# -------------------------------------------------
async def extract_playable_stream(embed_url: str, url_num: int) -> str | None:
    try:
        r = await network.request(embed_url, log=log)
        if r:
            content = r.text

            css_matches = re.findall(r'(https?://[^\s"\']+\.css[^\s"\']*)', content, re.IGNORECASE)

            for css_url in css_matches:
                if 'serveplay' in css_url:
                    css_response = await network.request(css_url, headers={"Referer": embed_url}, log=log)
                    if css_response:
                        css_content = css_response.text

                        patterns = [
                            r'(https?://[^\s"\']+\.js[^\s"\']*)',
                            r'(https?://[^\s"\']+\.m3u8[^\s"\']*)',
                        ]

                        for pattern in patterns:
                            matches = re.findall(pattern, css_content, re.IGNORECASE)
                            for url in matches:
                                if 'serveplay' in url:
                                    return url

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()

            stream_url = None

            async def handle_request(request):
                nonlocal stream_url
                if 'serveplay' in request.url:
                    stream_url = request.url

            page.on('request', handle_request)

            try:
                await page.goto(embed_url, wait_until='networkidle')
                await asyncio.sleep(3)
            finally:
                await browser.close()

            return stream_url

    except Exception as e:
        log.error(f"URL {url_num}) error extracting stream: {e}")

    return None


# -------------------------------------------------
# NEW: Extract Events from homepage
# -------------------------------------------------
async def get_events(cached_hrefs: set[str]) -> list[dict]:
    events = []

    r = await network.request(BASE_URL, log=log)
    if not r:
        return events

    content = r.text

    # Find all event blocks
    pattern = re.findall(
        r'<h3[^>]*title="([^"]+)"[^>]*>.*?</h3>.*?<code[^>]*>(https://pushembdz\.store/embed/[a-z0-9\-]+)</code>',
        content,
        re.DOTALL | re.IGNORECASE
    )

    for title, embed_url in pattern:
        event_id = embed_url.split("/")[-1]

        if event_id in cached_hrefs:
            continue

        events.append({
            "event": title.strip(),
            "embed": embed_url.strip(),
            "href": event_id,
            "logo": "https://i.gyazo.com/4a5e9fa2525808ee4b65002b56d3450e.png"
        })

    log.info(f"Extracted {len(events)} events from homepage")
    return events


# -------------------------------------------------
# Build playlist (UNCHANGED FORMAT)
# -------------------------------------------------
def build_playlist(data: dict[str, dict]) -> str:
    lines = ["#EXTM3U"]
    chno = 1

    for title, info in data.items():
        lines.append(
            f'#EXTINF:-1 tvg-chno="{chno}" '
            f'tvg-id="{info["id"]}" '
            f'tvg-name="{title}" '
            f'tvg-logo="{info["logo"]}" '
            f'group-title="Live Events",{title}'
        )

        lines.append(
            f'{info["url"]}'
            f'|referer={BASE_URL}'
            f'|origin={BASE_URL}'
            f'|user-agent={UA_ENC}'
        )

        chno += 1

    return "\n".join(lines) + "\n"


# -------------------------------------------------
# Main scrape
# -------------------------------------------------
async def scrape():
    cached = CACHE_FILE.load() or {}
    urls = dict(cached)

    cached_hrefs = {v.get("href") for v in urls.values()}

    log.info(f"Loaded {len(urls)} cached events")

    events = await get_events(cached_hrefs)

    now_ts = Time.clean(Time.now()).timestamp()

    for i, ev in enumerate(events, start=1):
        log.info(f"Processing {i}/{len(events)}: {ev['event']}")

        stream = await extract_playable_stream(ev["embed"], i)

        if not stream:
            continue

        title = f"[EVENT] {ev['event']} ({TAG})"

        tvg_id, logo_lookup = leagues.get_tvg_info("EVENT", ev["event"])

        urls[title] = {
            "url": stream,
            "logo": ev["logo"] or logo_lookup,
            "timestamp": now_ts,
            "id": tvg_id or "Live.Event.us",
            "href": ev["href"],
        }

        await asyncio.sleep(1)

    if urls:
        CACHE_FILE.write(urls)

        playlist = build_playlist(urls)
        OUTPUT_FILE.write_text(playlist, encoding="utf-8")

        log.info(f"Playlist written: {OUTPUT_FILE}")
    else:
        log.warning("No events found")


# -------------------------------------------------
# Run
# -------------------------------------------------
async def main():
    log.info("Starting PUSHEMBDZ scraper")
    await scrape()


if __name__ == "__main__":
    asyncio.run(main())
