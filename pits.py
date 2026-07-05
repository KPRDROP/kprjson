import asyncio
import re
import json
from pathlib import Path
from urllib.parse import quote_plus

from playwright.async_api import async_playwright
from utils import Cache, Event, Time, get_logger, leagues, network

log = get_logger(__name__)

TAG = "PITS"
BASE_URL = "https://pitsport.live"
API_URL = "https://api.pitsport.live/v1/streams"
WATCH_BASE = f"{BASE_URL}/watch"

CACHE_FILE = Cache(f"{TAG.lower()}.json", exp=10_800)
OUTPUT_FILE = Path("pits.m3u8")

# -------------------------------------------------
# User-Agent
# -------------------------------------------------
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:146.0) "
    "Gecko/20100101 Firefox/146.0"
)

UA_ENC = quote_plus(UA)

# -------------------------------------------------
# Valid stream domains/extensions
# -------------------------------------------------
VALID_STREAM_DOMAINS = {
    "ossfeed.store",
    "sense-scramble-bay.xyz",
    "sadhoofiton.shop",
    "serveplay",
    "ev01-prod",
    "cloudfront",
}

VALID_STREAM_EXT = {
    ".m3u8",
    ".css",
    ".js",
}


# -------------------------------------------------
# Clean stream URL
# -------------------------------------------------
def clean_stream_url(url: str) -> str:
    if not url:
        return url
    url = url.strip().strip('"').strip("'")
    url = url.replace("\\/", "/")
    return url


# -------------------------------------------------
# Check if URL is valid stream
# -------------------------------------------------
def is_stream_url(url: str) -> bool:
    if not url:
        return False

    url = clean_stream_url(url).lower()

    if "/out/v2/" in url:
        return True

    if any(host in url for host in VALID_STREAM_DOMAINS):
        return True

    if any(ext in url for ext in VALID_STREAM_EXT):
        return True

    return False


# -------------------------------------------------
# Extract __NEXT_DATA__ from page
# -------------------------------------------------
async def extract_next_data(page) -> dict:
    """Extract __NEXT_DATA__ from the page"""
    try:
        data = await page.evaluate("() => window.__NEXT_DATA__")
        if data:
            return data
    except:
        pass

    # Fallback: extract from HTML
    try:
        content = await page.content()
        pattern = r'<script[^>]*id="__NEXT_DATA__"[^>]*>([\s\S]*?)</script>'
        match = re.search(pattern, content, re.I)
        if match:
            return json.loads(match.group(1))
    except:
        pass

    return {}


# -------------------------------------------------
# Get watch content using Playwright
# -------------------------------------------------
async def get_watch_content_with_playwright(watch_id: str, url_num: int) -> list[dict]:
    """Get watch page content using Playwright"""
    watch_url = f"{WATCH_BASE}/{watch_id}"
    content_data = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )

        context = await browser.new_context(
            user_agent=UA,
            viewport={"width": 1920, "height": 1080},
        )

        page = await context.new_page()

        # Capture XHR responses for embed streams
        embed_streams = {}

        async def handle_response(response):
            url = response.url

            # Look for embed responses
            if "pushembdz.store/embed" in url:
                try:
                    body = await response.text()
                    data = json.loads(body)

                    # Extract stream link
                    stream = data.get("stream")
                    if stream:
                        link = stream.get("link")
                        if is_stream_url(link):
                            embed_id = url.split("/")[-1]
                            embed_streams[embed_id] = {
                                "url": clean_stream_url(link),
                                "title": stream.get("title", ""),
                            }
                            log.info(f"URL {url_num}) Captured stream from embed: {link[:80]}...")

                    # Check for streams (plural)
                    streams = data.get("streams")
                    if streams and isinstance(streams, list):
                        for s in streams:
                            link = s.get("link")
                            if is_stream_url(link):
                                embed_id = url.split("/")[-1]
                                embed_streams[embed_id] = {
                                    "url": clean_stream_url(link),
                                    "title": s.get("title", ""),
                                }
                                log.info(f"URL {url_num}) Captured stream from embed: {link[:80]}...")

                except:
                    pass

            # Look for API responses
            if "api.pushembdz.store" in url:
                try:
                    body = await response.text()
                    data = json.loads(body)
                    if "stream" in data and "link" in data["stream"]:
                        link = data["stream"]["link"]
                        if is_stream_url(link):
                            # This is likely the embed response
                            pass
                except:
                    pass

        page.on("response", handle_response)

        try:
            log.info(f"URL {url_num}) Loading watch page: {watch_url}")
            await page.goto(watch_url, wait_until="domcontentloaded", timeout=30000)

            # Wait for content to load
            await asyncio.sleep(5)

            # Extract __NEXT_DATA__
            next_data = await extract_next_data(page)

            # Get content from __NEXT_DATA__
            if next_data:
                try:
                    if "props" in next_data and "pageProps" in next_data["props"]:
                        page_props = next_data["props"]["pageProps"]
                        if "content" in page_props:
                            content_data = page_props["content"]
                            log.info(f"URL {url_num}) Found {len(content_data)} embeds from __NEXT_DATA__")
                except:
                    pass

            # If no content from __NEXT_DATA__, try to find iframes directly
            if not content_data:
                # Look for iframe elements
                iframes = await page.query_selector_all('iframe')
                for iframe in iframes:
                    try:
                        src = await iframe.get_attribute('src')
                        if src and "pushembdz.store/embed" in src:
                            # Extract embed ID
                            uuid_match = re.search(r'/embed/([0-9a-f\-]{36})', src, re.I)
                            if uuid_match:
                                content_data.append({
                                    "iframe": src,
                                    "customText": None,
                                })
                    except:
                        pass

                log.info(f"URL {url_num}) Found {len(content_data)} embeds from iframes")

            # Wait a bit more for XHR responses
            await asyncio.sleep(3)

            # If we have embed streams from XHR, add them to content_data
            if embed_streams:
                for item in content_data:
                    iframe = item.get("iframe", "")
                    uuid_match = re.search(r'/embed/([0-9a-f\-]{36})', iframe, re.I)
                    if uuid_match:
                        embed_id = uuid_match.group(1)
                        if embed_id in embed_streams:
                            item["_stream"] = embed_streams[embed_id]

        except Exception as e:
            log.error(f"URL {url_num}) Playwright error: {e}")

        finally:
            await browser.close()

    return content_data


# -------------------------------------------------
# Get events from API
# -------------------------------------------------
async def get_events_from_api(cached_hrefs: set[str]) -> list[dict[str, str]]:
    """Get events directly from the API"""
    events = []

    response = await network.request(
        API_URL,
        headers={
            "User-Agent": UA,
            "Accept": "application/json",
        },
        log=log,
    )

    if not response:
        log.error("Failed to fetch API")
        return events

    try:
        data = json.loads(response.text)

        if not data.get("success"):
            log.error("API returned success=false")
            return events

        categories = data.get("categories", [])

        for category_data in categories:
            category = category_data.get("category", "LIVE")
            streams = category_data.get("streams", [])

            for stream in streams:
                uri = stream.get("uri")
                if not uri:
                    continue

                # Extract watch ID from URI
                watch_match = re.search(r'/watch/([a-z0-9\-]+)', uri, re.I)
                if not watch_match:
                    continue

                watch_id = watch_match.group(1)

                if watch_id in cached_hrefs:
                    continue

                title = stream.get("title", "")
                watch_url = f"{WATCH_BASE}/{watch_id}"
                thumbnail = stream.get("thumbnail", "")

                # Build full name
                full_name = f"{category} - {title}"

                events.append({
                    "sport": category,
                    "category": category,
                    "event": title,
                    "full_name": full_name,
                    "link": watch_url,
                    "href": watch_id,
                    "logo": thumbnail or "https://i.gyazo.com/4a5e9fa2525808ee4b65002b56d3450e.png",
                    "is_live": False,
                })

    except json.JSONDecodeError as e:
        log.error(f"Failed to parse API response: {e}")
    except Exception as e:
        log.error(f"Error processing API: {e}")

    log.info(f"Found {len(events)} events from API")
    return events


# -------------------------------------------------
# Process event to extract streams
# -------------------------------------------------
async def process_event(watch_id: str, url: str, url_num: int) -> list[dict]:
    """Process event to extract all stream URLs"""
    streams = []

    # Step 1: Get watch content using Playwright
    content = await get_watch_content_with_playwright(watch_id, url_num)

    if not content:
        log.warning(f"URL {url_num}) No content found")
        return streams

    # Step 2: Extract streams from content
    for item in content:
        iframe = item.get("iframe")
        custom_text = item.get("customText")

        if not iframe:
            continue

        # Check if we already have a stream from XHR
        if "_stream" in item:
            stream_data = item["_stream"]
            stream_url = stream_data.get("url")
            stream_title = stream_data.get("title")

            if stream_url:
                if custom_text:
                    if stream_title:
                        stream_title = f"{stream_title} ({custom_text})"
                    else:
                        stream_title = custom_text

                streams.append({
                    "url": stream_url,
                    "title": stream_title,
                })
            continue

        # Extract embed ID from iframe URL
        uuid_match = re.search(r'/embed/([0-9a-f\-]{36})', iframe, re.I)
        if not uuid_match:
            continue

        embed_id = uuid_match.group(1)

        # If no stream from XHR, try to load the embed page directly
        embed_url = f"https://pushembdz.store/embed/{embed_id}"

        try:
            # Use network request as fallback
            response = await network.request(
                embed_url,
                headers={
                    "User-Agent": UA,
                    "Referer": "https://pushembdz.store/",
                    "Origin": "https://pushembdz.store",
                    "Accept": "application/json",
                },
                log=log,
            )

            if response:
                data = json.loads(response.text)
                stream = data.get("stream")
                if stream:
                    stream_url = stream.get("link")
                    if is_stream_url(stream_url):
                        stream_title = stream.get("title", "")
                        if custom_text:
                            if stream_title:
                                stream_title = f"{stream_title} ({custom_text})"
                            else:
                                stream_title = custom_text

                        streams.append({
                            "url": clean_stream_url(stream_url),
                            "title": stream_title,
                        })
        except:
            pass

    if streams:
        log.info(f"URL {url_num}) Found {len(streams)} streams")

    return streams


# -------------------------------------------------
# Build playlist
# -------------------------------------------------
def build_playlist(data: dict[str, dict]) -> str:
    lines = ["#EXTM3U"]
    lines.append(
        f"# Playlist generated by {TAG} Scraper - "
        f"{Time.clean(Time.now()).strftime('%Y-%m-%d %H:%M:%S')}"
    )
    lines.append("")

    chno = 1

    for title, info in data.items():
        stream_url = info["url"]

        referer = "https://pushembdz.store/"
        origin = "https://pushembdz.store"

        lines.append(
            f'#EXTINF:-1 tvg-chno="{chno}" '
            f'tvg-id="{info["id"]}" '
            f'tvg-name="{title}" '
            f'tvg-logo="{info["logo"]}" '
            f'group-title="Live Events",{title}'
        )

        lines.append(
            f"{stream_url}"
            f"|referer={referer}"
            f"|origin={origin}"
            f"|user-agent={UA_ENC}"
        )

        lines.append("")
        chno += 1

    return "\n".join(lines) + "\n"


# -------------------------------------------------
# Main scraper
# -------------------------------------------------
async def scrape() -> None:
    cached = CACHE_FILE.load() or {}
    urls: dict[str, dict] = dict(cached)
    cached_hrefs = {v.get("href", "") for v in urls.values()}

    log.info(f"Loaded {len(urls)} cached events")

    # Get events from API
    events = await get_events_from_api(cached_hrefs)
    log.info(f"Found {len(events)} event(s)")

    if not events and not urls:
        log.info("No events found and no cached events")
        return

    now_ts = Time.clean(Time.now()).timestamp()
    new_events_count = 0

    for i, ev in enumerate(events, start=1):
        log.info(f"Processing event {i}/{len(events)}: {ev['full_name'][:80]}...")

        stream_results = await process_event(ev["href"], ev["link"], i)

        if not stream_results:
            log.warning(f"Event {i}) No streams found for: {ev['full_name'][:60]}...")
            continue

        for stream_data in stream_results:
            stream_url = stream_data.get("url")
            if not stream_url:
                continue

            stream_title = stream_data.get("title", "")

            # Build title with suffix if available
            if stream_title:
                title = f"[{ev['sport']}] {ev['event']} - {stream_title} ({TAG})"
            else:
                title = f"[{ev['sport']}] {ev['event']} ({TAG})"

            tvg_id, _logo_lookup = leagues.get_tvg_info(ev["sport"], ev["event"])

            # Generate unique key for multiple streams per event
            key = title
            existing_titles = [k for k in urls.keys() if k.startswith(f"[{ev['sport']}] {ev['event']}")]
            if existing_titles:
                stream_count = len(existing_titles)
                key = f"{title} [{stream_count + 1}]"

            urls[key] = {
                "url": stream_url,
                "logo": ev["logo"] or _logo_lookup,
                "base": BASE_URL,
                "timestamp": now_ts,
                "id": tvg_id or "Live.Event.us",
                "href": ev["href"],
                "category": ev["category"],
                "event": ev["event"],
            }

            new_events_count += 1
            log.info(f"Event {i}) ✓ Added stream: {stream_url[:80]}...")

        await asyncio.sleep(1)

    if new_events_count > 0:
        CACHE_FILE.write(urls)
        log.info(f"Added {new_events_count} streams to cache")

    # Write playlist
    if urls:
        out = build_playlist(urls)
        OUTPUT_FILE.write_text(out, encoding="utf-8")
        log.info(f"Successfully wrote {len(urls)} entries to pits.m3u8")
    else:
        OUTPUT_FILE.write_text("#EXTM3U\n# No events available\n", encoding="utf-8")
        log.warning("No events written")


# -------------------------------------------------
# Main
# -------------------------------------------------
async def main():
    log.info("Starting PITS scraper")
    await scrape()
    log.info("PITS scraper completed")


if __name__ == "__main__":
    asyncio.run(main())
