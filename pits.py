import asyncio
import re
import json
from pathlib import Path
from urllib.parse import quote_plus, urljoin

from playwright.async_api import async_playwright
from utils import Cache, Time, get_logger, leagues, network

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
# Extract all possible UUIDs
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

    # unique
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
    api_url = f"https://pushembdz.store/api/stream/{embed_id}"

    try:
        log.info(f"URL {url_num}) API request: {api_url}")

        response = await network.request(
            api_url,
            headers={
                "User-Agent": UA,
                "Referer": BASE_URL,
                "Origin": BASE_URL,
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

            possible_keys = [
                "link",
                "url",
                "stream",
                "src",
                "file",
            ]

            for key in possible_keys:
                stream = data.get(key)

                if stream:
                    stream = clean_stream_url(stream)

                    if is_stream_url(stream):
                        log.info(
                            f"URL {url_num}) ✓ Stream from API: {stream[:120]}"
                        )
                        return stream

        except Exception:
            pass

        # Raw fallback regex
        regex = r'https?://[^\s"\']+\.(?:m3u8|css|js)[^\s"\']*'

        matches = re.findall(regex, text, re.I)

        for match in matches:
            match = clean_stream_url(match)

            if is_stream_url(match):
                log.info(
                    f"URL {url_num}) ✓ Stream regex API: {match[:120]}"
                )
                return match

    except Exception as e:
        log.error(f"URL {url_num}) API error: {e}")

    return None


# -------------------------------------------------
# Extract embed/API UUID from watch page
# -------------------------------------------------
async def extract_embed_ids(content: str, url_num: int) -> list[str]:
    ids = []

    # Direct embed URLs
    patterns = [
        r'pushembdz\.store/embed/([0-9a-f\-]{36})',
        r'pushembdz\.store/api/stream/([0-9a-f\-]{36})',
        r'"streamId":"([0-9a-f\-]{36})"',
        r"'streamId':'([0-9a-f\-]{36})'",
        r'"embedId":"([0-9a-f\-]{36})"',
        r"'embedId':'([0-9a-f\-]{36})'",
    ]

    for pattern in patterns:
        matches = re.findall(pattern, content, re.I)

        for match in matches:
            if match not in ids:
                ids.append(match)

    # Generic UUID extraction fallback
    generic = extract_uuids(content)

    for item in generic:
        if item not in ids:
            ids.append(item)

    log.info(f"URL {url_num}) Found {len(ids)} possible embed IDs")

    return ids


# -------------------------------------------------
# Extract direct stream URLs from page content
# -------------------------------------------------
def extract_streams_from_content(content: str, url_num: int) -> list[str]:
    streams = []

    patterns = [
        r'https?://[^\s"\']+\.m3u8[^\s"\']*',
        r'https?://[^\s"\']+master\.css[^\s"\']*',
        r'https?://[^\s"\']+stream\.js[^\s"\']*',
        r'https?://[^\s"\']+\.(?:css|js)[^\s"\']*',
    ]

    for pattern in patterns:
        matches = re.findall(pattern, content, re.I)

        for match in matches:
            match = clean_stream_url(match)

            if is_stream_url(match):
                if match not in streams:
                    streams.append(match)

    if streams:
        log.info(f"URL {url_num}) Found {len(streams)} direct streams")

    return streams


# -------------------------------------------------
# Main extraction from watch page
# -------------------------------------------------
async def extract_stream_from_page(
    watch_url: str,
    watch_id: str,
    url_num: int,
) -> str | None:

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
            return None

        content = response.text

        # -------------------------------------------------
        # 1) Direct stream inside page
        # -------------------------------------------------
        direct_streams = extract_streams_from_content(content, url_num)

        if direct_streams:
            return direct_streams[0]

        # -------------------------------------------------
        # 2) Extract embed IDs
        # -------------------------------------------------
        embed_ids = await extract_embed_ids(content, url_num)

        # Try every UUID through API
        for embed_id in embed_ids:
            stream = await extract_stream_from_api(embed_id, url_num)

            if stream:
                return stream

        # -------------------------------------------------
        # 3) Search inside JS chunks
        # -------------------------------------------------
        js_files = re.findall(
            r'src="([^"]+\.js)"',
            content,
            re.I,
        )

        for js in js_files[:10]:

            try:
                js_url = urljoin(BASE_URL, js)

                log.info(f"URL {url_num}) Checking JS: {js_url}")

                js_response = await network.request(
                    js_url,
                    headers={"User-Agent": UA},
                    log=log,
                )

                if not js_response:
                    continue

                js_content = js_response.text

                # Direct stream in JS
                streams = extract_streams_from_content(
                    js_content,
                    url_num,
                )

                if streams:
                    return streams[0]

                # UUIDs inside JS
                js_ids = await extract_embed_ids(
                    js_content,
                    url_num,
                )

                for js_id in js_ids:
                    stream = await extract_stream_from_api(
                        js_id,
                        url_num,
                    )

                    if stream:
                        return stream

            except Exception as e:
                log.debug(f"URL {url_num}) JS parse error: {e}")

    except Exception as e:
        log.error(f"URL {url_num}) Page extraction error: {e}")

    return None


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

        # -------------------------------------------------
        # Capture requests
        # -------------------------------------------------
        async def handle_request(request):
            nonlocal stream_url

            url = request.url

            if is_stream_url(url):
                stream_url = clean_stream_url(url)

                log.info(
                    f"URL {url_num}) ✓ Captured request stream: "
                    f"{stream_url[:120]}"
                )

        # -------------------------------------------------
        # Capture responses
        # -------------------------------------------------
        async def handle_response(response):
            nonlocal stream_url

            url = response.url

            if is_stream_url(url):
                stream_url = clean_stream_url(url)

                log.info(
                    f"URL {url_num}) ✓ Captured response stream: "
                    f"{stream_url[:120]}"
                )

                return

            # API responses
            if "api/stream/" in url:

                try:
                    body = await response.text()

                    regex = (
                        r'https?://[^\s"\']+'
                        r'\.(?:m3u8|css|js)[^\s"\']*'
                    )

                    matches = re.findall(regex, body, re.I)

                    for match in matches:

                        match = clean_stream_url(match)

                        if is_stream_url(match):
                            stream_url = match

                            log.info(
                                f"URL {url_num}) ✓ API response stream: "
                                f"{match[:120]}"
                            )

                            return

                except Exception:
                    pass

        page.on("request", handle_request)
        page.on("response", handle_response)

        try:
            log.info(f"URL {url_num}) Opening browser page")

            await page.goto(
                watch_url,
                wait_until="domcontentloaded",
                timeout=45000,
            )

            await asyncio.sleep(8)

            # Click buttons if exist
            buttons = await page.query_selector_all(
                'button, .play, .vjs-big-play-button'
            )

            for button in buttons[:3]:
                try:
                    await button.click(timeout=2000)
                    await asyncio.sleep(2)
                except Exception:
                    pass

            # iframe scan
            frames = page.frames

            for frame in frames:

                try:
                    frame_content = await frame.content()

                    streams = extract_streams_from_content(
                        frame_content,
                        url_num,
                    )

                    if streams:
                        stream_url = streams[0]
                        break

                except Exception:
                    pass

            await asyncio.sleep(5)

        except Exception as e:
            log.error(f"URL {url_num}) Playwright error: {e}")

        finally:
            await browser.close()

    return stream_url


# -------------------------------------------------
# Extract events from schedule page
# -------------------------------------------------
async def get_events_from_schedule(
    cached_hrefs: set[str]
) -> list[dict[str, str]]:

    events = []

    response = await network.request(SCHEDULE_URL, log=log)

    if not response:
        log.error("Failed to fetch schedule page")
        return events

    content = response.text

    watch_pattern = r'href=["\']/watch/([a-z0-9\-]+)["\']'

    watch_matches = re.findall(
        watch_pattern,
        content,
        re.I,
    )

    # -------------------------------------------------
    # Improved sports detection
    # -------------------------------------------------
    sport_keywords = {
        "F1": "F1",
        "Formula E": "FORMULA_E",
        "Formula 2": "F2",
        "F2": "F2",
        "Formula 3": "F3",
        "F3": "F3",
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

        title_pattern = (
            rf'href=["\']/watch/{watch_id}["\'][^>]*>'
            rf'.*?<h1[^>]*>([^<]+)</h1>'
        )

        title_match = re.search(
            title_pattern,
            content,
            re.S | re.I,
        )

        title = (
            title_match.group(1).strip()
            if title_match
            else f"Event {watch_id[:8]}"
        )

        category = "LIVE"

        for key, value in sport_keywords.items():

            if key.lower() in title.lower():
                category = value
                break

        full_name = f"{category} - {title}"

        events.append({
            "sport": category,
            "category": category.replace("_", " "),
            "event": title,
            "full_name": full_name,
            "link": watch_url,
            "href": watch_id,
            "logo": "https://i.gyazo.com/4a5e9fa2525808ee4b65002b56d3450e.png",
            "is_live": False,
        })

    # remove duplicates
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
# Process event
# -------------------------------------------------
async def process_event(
    watch_id: str,
    url: str,
    url_num: int,
) -> str | None:

    # Fast extraction first
    stream = await extract_stream_from_page(
        url,
        watch_id,
        url_num,
    )

    if stream:
        return stream

    # Playwright fallback
    log.info(f"URL {url_num}) Trying Playwright fallback")

    stream = await extract_stream_with_playwright(
        url,
        url_num,
    )

    return stream


# -------------------------------------------------
# Main scraper
# -------------------------------------------------
async def scrape() -> None:

    cached = CACHE_FILE.load() or {}

    urls: dict[str, dict] = dict(cached)

    cached_hrefs = {
        v.get("href", "")
        for v in urls.values()
    }

    log.info(f"Loaded {len(urls)} cached events")

    events = await get_events(cached_hrefs)

    log.info(f"Found {len(events)} event(s)")

    if not events and not urls:
        log.info("No events found")
        return

    now_ts = Time.clean(Time.now()).timestamp()

    new_events_count = 0

    for i, ev in enumerate(events, start=1):

        log.info(
            f"Processing event {i}/{len(events)}: "
            f"{ev['full_name'][:80]}..."
        )

        stream = await process_event(
            ev["href"],
            ev["link"],
            i,
        )

        if not stream:

            log.warning(
                f"Event {i}) No stream found for: "
                f"{ev['full_name'][:60]}"
            )

            continue

        title = (
            f"[{ev['sport']}] "
            f"{ev['event']} "
            f"({TAG})"
        )

        tvg_id, _logo_lookup = leagues.get_tvg_info(
            ev["sport"],
            ev["event"],
        )

        urls[title] = {
            "url": stream,
            "logo": ev["logo"] or _logo_lookup,
            "base": BASE_URL,
            "timestamp": now_ts,
            "id": tvg_id or "Live.Event.us",
            "href": ev["href"],
            "category": ev["category"],
            "event": ev["event"],
        }

        new_events_count += 1

        log.info(
            f"Event {i}) ✓ Added stream: "
            f"{stream[:100]}"
        )

        await asyncio.sleep(1)

    # Save cache
    if new_events_count > 0:
        CACHE_FILE.write(urls)

        log.info(
            f"Added {new_events_count} events to cache"
        )

    # Write playlist
    if urls:

        out = build_playlist(urls)

        OUTPUT_FILE.write_text(
            out,
            encoding="utf-8",
        )

        log.info(
            f"Successfully wrote {len(urls)} entries "
            f"to pits.m3u8"
        )

    else:

        OUTPUT_FILE.write_text(
            "#EXTM3U\n# No events available\n",
            encoding="utf-8",
        )

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
