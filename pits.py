import asyncio
import re
import json
from pathlib import Path
from urllib.parse import quote_plus, urljoin

from playwright.async_api import async_playwright
from utils import Cache, Event, Time, get_logger, leagues, network

log = get_logger(__name__)

TAG = "PITS"
BASE_URL = "https://pitsport.live"
SCHEDULE_URL = f"{BASE_URL}/schedule"
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
VALID_STREAM_DOMAINS = (
    "ossfeed",
    "serveplay",
    "ev01-prod",
    "cloudfront",
    "sense-scramble-bay.xyz",
)

VALID_STREAM_EXT = (
    ".m3u8",
    ".css",
    ".js",
)

# -------------------------------------------------
# Check if URL is valid stream
# -------------------------------------------------
def is_stream_url(url: str) -> bool:
    if not url:
        return False

    url = url.lower()

    if not any(ext in url for ext in VALID_STREAM_EXT):
        return False

    if any(domain in url for domain in VALID_STREAM_DOMAINS):
        return True

    # Allow generic ossfeed style
    if "/out/v2/" in url:
        return True

    return False


# -------------------------------------------------
# Clean stream URL
# -------------------------------------------------
def clean_stream_url(url: str) -> str:
    if not url:
        return url

    url = url.strip().strip('"').strip("'")

    # Remove escaped slashes
    url = url.replace("\\/", "/")

    return url


# -------------------------------------------------
# Extract UUIDs from content
# -------------------------------------------------
def extract_uuids(content: str) -> list[str]:
    uuid_pattern = (
        r'[0-9a-f]{8}-'
        r'[0-9a-f]{4}-'
        r'[0-9a-f]{4}-'
        r'[0-9a-f]{4}-'
        r'[0-9a-f]{12}'
    )

    matches = re.findall(uuid_pattern, content, re.I)

    seen = set()
    out = []

    for m in matches:
        if m not in seen:
            seen.add(m)
            out.append(m)

    return out


# -------------------------------------------------
# Extract stream from API
# -------------------------------------------------
async def extract_stream_from_api(embed_id: str, url_num: int) -> str | None:
    api_url = f"https://api.pushembdz.store/v1/stream/{embed_id}"

    try:
        log.debug(f"URL {url_num}) API request: {api_url}")

        response = await network.request(
            api_url,
            headers={
                "User-Agent": UA,
                "Referer": "https://pushembdz.store/",
                "Origin": "https://pushembdz.store",
                "Accept": "application/json,text/plain,*/*",
            },
            log=log,
        )

        if not response:
            return None

        text = response.text

        # Try JSON parse
        try:
            data = json.loads(text)

            # Check for stream object
            if "stream" in data and "link" in data["stream"]:
                stream = clean_stream_url(data["stream"]["link"])
                if is_stream_url(stream):
                    log.info(f"URL {url_num}) ✓ Stream from API: {stream[:120]}...")
                    return stream

            # Direct link
            if "link" in data:
                stream = clean_stream_url(data["link"])
                if is_stream_url(stream):
                    log.info(f"URL {url_num}) ✓ Stream from API: {stream[:120]}...")
                    return stream

        except Exception:
            pass

        # Raw fallback regex
        regex = r'https?://[^\s"\']+\.(?:m3u8|css|js)[^\s"\']*'
        matches = re.findall(regex, text, re.I)

        for match in matches:
            match = clean_stream_url(match)
            if is_stream_url(match):
                log.info(f"URL {url_num}) ✓ Stream regex API: {match[:120]}...")
                return match

    except Exception as e:
        log.debug(f"URL {url_num}) API error: {e}")

    return None


# -------------------------------------------------
# Extract embed IDs from watch page JSON
# -------------------------------------------------
async def extract_embed_ids_from_page(watch_url: str, url_num: int) -> list[str]:
    """Extract embed IDs from the watch page JSON data"""
    embed_ids = []

    try:
        response = await network.request(
            watch_url,
            headers={
                "User-Agent": UA,
                "Referer": BASE_URL,
            },
            log=log,
        )

        if not response:
            return embed_ids

        content = response.text

        # Look for JSON data in the page
        # The page contains JSON with content array of iframe URLs
        json_pattern = r'<script[^>]*id="__NEXT_DATA__"[^>]*>([\s\S]*?)</script>'
        match = re.search(json_pattern, content, re.I)

        if match:
            try:
                data = json.loads(match.group(1))
                # Navigate to the content array
                # The structure varies, so try multiple paths
                if "props" in data and "pageProps" in data["props"]:
                    page_props = data["props"]["pageProps"]
                    if "content" in page_props:
                        for item in page_props["content"]:
                            if "iframe" in item:
                                embed_url = item["iframe"]
                                # Extract UUID from embed URL
                                uuid_match = re.search(
                                    r'/embed/([0-9a-f\-]{36})',
                                    embed_url,
                                    re.I
                                )
                                if uuid_match:
                                    embed_ids.append(uuid_match.group(1))
                    # Also check for streams data
                    if "streams" in page_props:
                        for stream in page_props["streams"]:
                            if "embed" in stream:
                                embed_url = stream["embed"]
                                uuid_match = re.search(
                                    r'/embed/([0-9a-f\-]{36})',
                                    embed_url,
                                    re.I
                                )
                                if uuid_match:
                                    embed_ids.append(uuid_match.group(1))
            except:
                pass

        # Fallback: look for any embed URLs
        embed_pattern = r'pushembdz\.store/embed/([0-9a-f\-]{36})'
        matches = re.findall(embed_pattern, content, re.I)

        for match in matches:
            if match not in embed_ids:
                embed_ids.append(match)

        # Also look for API URLs
        api_pattern = r'api\.pushembdz\.store/v1/stream/([0-9a-f\-]{36})'
        matches = re.findall(api_pattern, content, re.I)

        for match in matches:
            if match not in embed_ids:
                embed_ids.append(match)

        log.info(f"URL {url_num}) Found {len(embed_ids)} embed IDs")

    except Exception as e:
        log.error(f"URL {url_num}) Error extracting embed IDs: {e}")

    return embed_ids


# -------------------------------------------------
# Extract event info from schedule page
# -------------------------------------------------
async def get_events_from_schedule(cached_hrefs: set[str]) -> list[dict[str, str]]:
    events = []

    response = await network.request(SCHEDULE_URL, log=log)

    if not response:
        log.error("Failed to fetch schedule page")
        return events

    content = response.text

    # Find all event links
    watch_pattern = r'href=["\']/watch/([a-z0-9\-]+)["\']'
    watch_matches = re.findall(watch_pattern, content, re.I)

    # Sport keywords mapping
    sport_keywords = {
        "F1": "F1",
        "Formula E": "FORMULA_E",
        "Formula 2": "F2",
        "F2": "F2",
        "Formula 3": "F3",
        "F1 Academy": "F1_ACADEMY",
        "NASCAR Cup": "NASCAR",
        "NASCAR Truck": "NASCAR_TRUCK",
        "NASCAR O'Reilly": "NASCAR_XFINITY",
        "ARCA": "ARCA",
        "MotoGP": "MOTOGP",
        "Moto2": "MOTO2",
        "Moto3": "MOTO3",
        "IndyCar": "INDYCAR",
        "WRC": "WRC",
        "Rally": "RALLY",
        "WorldSBK": "WORLDSBK",
        "IMSA": "IMSA",
        "Super Formula": "SUPER_FORMULA",
        "Super GT": "SUPER_GT",
        "Le Mans": "LEMANS",
    }

    for watch_id in watch_matches:
        if watch_id in cached_hrefs:
            continue

        watch_url = f"{WATCH_BASE}/{watch_id}"

        # Extract title
        title_pattern = (
            rf'href=["\']/watch/{watch_id}["\'][^>]*>'
            rf'.*?<h1[^>]*>([^<]+)</h1>'
        )
        title_match = re.search(title_pattern, content, re.S | re.I)

        if title_match:
            title = title_match.group(1).strip()
        else:
            # Try alternative pattern for title
            alt_title_pattern = rf'/watch/{watch_id}[^>]*>.*?<h1[^>]*>([^<]+)</h1>'
            alt_match = re.search(alt_title_pattern, content, re.S | re.I)
            title = alt_match.group(1).strip() if alt_match else f"Event {watch_id[:8]}"

        # Extract date/time
        date_pattern = (
            rf'href=["\']/watch/{watch_id}["\'][^>]*>'
            rf'.*?<h2[^>]*>([^<]+)</h2>'
        )
        date_match = re.search(date_pattern, content, re.S | re.I)

        event_date = ""
        if date_match:
            event_date = date_match.group(1).strip()
            # Remove comma from date
            event_date = event_date.replace(',', '')

        # Determine sport category
        category = "LIVE"
        for key, value in sport_keywords.items():
            if key.lower() in title.lower():
                category = value
                break

        # Build full name with date
        full_name = f"{category} - {title}"
        if event_date:
            full_name += f" ({event_date})"

        events.append({
            "sport": category,
            "category": category.replace("_", " "),
            "event": title,
            "date": event_date,
            "full_name": full_name,
            "link": watch_url,
            "href": watch_id,
            "logo": "https://i.gyazo.com/4a5e9fa2525808ee4b65002b56d3450e.png",
            "is_live": False,
        })

    # Remove duplicates
    seen = set()
    unique_events = []

    for event in events:
        if event["href"] not in seen:
            seen.add(event["href"])
            unique_events.append(event)

    log.info(f"Found {len(unique_events)} events from schedule page")
    return unique_events


# -------------------------------------------------
# Get events
# -------------------------------------------------
async def get_events(cached_hrefs: set[str]) -> list[dict[str, str]]:
    return await get_events_from_schedule(cached_hrefs)


# -------------------------------------------------
# Process event to extract stream
# -------------------------------------------------
async def process_event(watch_id: str, url: str, url_num: int) -> str | None:
    """Process event to extract stream URL"""
    
    # Step 1: Extract embed IDs from the watch page
    embed_ids = await extract_embed_ids_from_page(url, url_num)
    
    if not embed_ids:
        log.warning(f"URL {url_num}) No embed IDs found")
        return None
    
    # Step 2: Try each embed ID through the API
    for embed_id in embed_ids:
        log.debug(f"URL {url_num}) Trying embed ID: {embed_id}")
        stream = await extract_stream_from_api(embed_id, url_num)
        if stream:
            log.info(f"URL {url_num}) ✓ Stream found via API")
            return stream
    
    # Step 3: Try Playwright fallback
    log.info(f"URL {url_num}) Trying Playwright fallback")
    stream = await extract_stream_with_playwright(url, url_num)
    
    return stream


# -------------------------------------------------
# Playwright fallback
# -------------------------------------------------
async def extract_stream_with_playwright(
    watch_url: str,
    url_num: int,
) -> str | None:

    stream_url = None

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

        async def handle_response(response):
            nonlocal stream_url
            url = response.url

            # Check if it's a stream URL
            if is_stream_url(url):
                stream_url = clean_stream_url(url)
                log.info(f"URL {url_num}) ✓ Captured stream: {stream_url[:120]}...")
                return

            # Check API responses
            if "api.pushembdz.store/v1/stream/" in url:
                try:
                    body = await response.text()
                    data = json.loads(body)
                    if "stream" in data and "link" in data["stream"]:
                        stream = clean_stream_url(data["stream"]["link"])
                        if is_stream_url(stream):
                            stream_url = stream
                            log.info(f"URL {url_num}) ✓ API stream: {stream[:120]}...")
                            return
                except:
                    pass

        page.on("response", handle_response)

        try:
            log.info(f"URL {url_num}) Opening browser page")
            await page.goto(watch_url, wait_until="domcontentloaded", timeout=45000)
            await asyncio.sleep(8)

            # Check for iframes and navigate to them
            iframes = await page.query_selector_all('iframe')
            for iframe in iframes:
                try:
                    src = await iframe.get_attribute('src')
                    if src and 'pushembdz.store/embed' in src:
                        log.info(f"URL {url_num}) Found embed iframe: {src[:100]}...")
                        await page.goto(src, wait_until="domcontentloaded", timeout=30000)
                        await asyncio.sleep(5)
                except:
                    pass

        except Exception as e:
            log.error(f"URL {url_num}) Playwright error: {e}")

        finally:
            await browser.close()

    return stream_url


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

    events = await get_events(cached_hrefs)
    log.info(f"Found {len(events)} event(s)")

    if not events and not urls:
        log.info("No events found and no cached events")
        return

    now_ts = Time.clean(Time.now()).timestamp()
    new_events_count = 0

    for i, ev in enumerate(events, start=1):
        log.info(f"Processing event {i}/{len(events)}: {ev['full_name'][:80]}...")

        stream = await process_event(ev["href"], ev["link"], i)

        if not stream:
            log.warning(f"Event {i}) No stream found for: {ev['full_name'][:60]}...")
            continue

        title = f"[{ev['sport']}] {ev['event']}"
        if ev.get('date'):
            title += f" ({ev['date']})"
        title += f" ({TAG})"

        tvg_id, _logo_lookup = leagues.get_tvg_info(ev["sport"], ev["event"])

        urls[title] = {
            "url": stream,
            "logo": ev["logo"] or _logo_lookup,
            "base": BASE_URL,
            "timestamp": now_ts,
            "id": tvg_id or "Live.Event.us",
            "href": ev["href"],
            "category": ev["category"],
            "event": ev["event"],
            "date": ev.get("date", ""),
        }

        new_events_count += 1
        log.info(f"Event {i}) ✓ Added stream: {stream[:100]}...")

        await asyncio.sleep(1)

    if new_events_count > 0:
        CACHE_FILE.write(urls)
        log.info(f"Added {new_events_count} events to cache")

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
