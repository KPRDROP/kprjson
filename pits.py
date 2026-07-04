import asyncio
import re
import json
from pathlib import Path
from urllib.parse import quote_plus, urljoin

from playwright.async_api import async_playwright
from selectolax.parser import HTMLParser
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
VALID_STREAM_DOMAINS = {
    "ossfeed.store",
    "sense-scramble-bay.xyz",
    "serveplay",
    "ev01-prod",
    "cloudfront",
}

VALID_STREAM_EXT = {
    ".m3u8",
    ".css",
    ".js",
}

API_HOSTS = (
    "https://api.pushembdz.store/v1/stream/",
    "https://pushembdz.store/api/stream/",
)

MAX_CONCURRENT_API = 8

# UUID regex
UUID_RE = re.compile(
    r"[0-9a-f]{8}-"
    r"[0-9a-f]{4}-"
    r"[0-9a-f]{4}-"
    r"[0-9a-f]{4}-"
    r"[0-9a-f]{12}",
    re.I,
)


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
# Extract UUIDs from text
# -------------------------------------------------
def extract_uuids(text: str) -> list[str]:
    seen = set()
    out = []

    for uuid in UUID_RE.findall(text):
        if uuid not in seen:
            seen.add(uuid)
            out.append(uuid)

    return out


# -------------------------------------------------
# Recursively collect embed IDs from JSON
# -------------------------------------------------
def collect_embed_ids(obj, out: set[str]):
    if isinstance(obj, dict):
        iframe = obj.get("iframe")
        if isinstance(iframe, str):
            m = UUID_RE.search(iframe)
            if m:
                out.add(m.group())

        embed = obj.get("embed")
        if isinstance(embed, str):
            m = UUID_RE.search(embed)
            if m:
                out.add(m.group())

        stream = obj.get("stream")
        if isinstance(stream, str):
            m = UUID_RE.search(stream)
            if m:
                out.add(m.group())

        for value in obj.values():
            collect_embed_ids(value, out)

    elif isinstance(obj, list):
        for item in obj:
            collect_embed_ids(item, out)

    elif isinstance(obj, str):
        m = UUID_RE.search(obj)
        if m:
            out.add(m.group())


# -------------------------------------------------
# Fetch stream from API
# -------------------------------------------------
async def fetch_api_stream(embed_id: str):
    headers = {
        "User-Agent": UA,
        "Referer": "https://pushembdz.store/",
        "Origin": "https://pushembdz.store",
        "Accept": "application/json",
    }

    for api in API_HOSTS:
        try:
            r = await network.request(
                api + embed_id,
                headers=headers,
                log=log,
            )

            if not r:
                continue

            data = json.loads(r.text)

            if isinstance(data, dict):
                if "stream" in data:
                    stream = data["stream"]
                    if isinstance(stream, dict):
                        link = stream.get("link")
                        if is_stream_url(link):
                            yield {
                                "title": stream.get("title", ""),
                                "url": clean_stream_url(link),
                            }

                if "content" in data:
                    for item in data["content"]:
                        if "link" not in item:
                            continue
                        if is_stream_url(item["link"]):
                            yield {
                                "title": item.get("title", ""),
                                "url": clean_stream_url(item["link"]),
                            }

                if "link" in data:
                    if is_stream_url(data["link"]):
                        yield {
                            "title": "",
                            "url": clean_stream_url(data["link"]),
                        }

        except Exception:
            pass


# -------------------------------------------------
# Extract embed IDs from watch page
# -------------------------------------------------
async def extract_embed_ids_from_page(watch_url: str, url_num: int) -> list[str]:
    ids = set()

    r = await network.request(
        watch_url,
        headers={
            "User-Agent": UA,
            "Referer": BASE_URL,
        },
        log=log,
    )

    if not r:
        return []

    html = r.text
    tree = HTMLParser(html)

    # 1) JSON scripts
    for node in tree.css("script"):
        text = node.text()
        if not text:
            continue

        if text.startswith("{"):
            try:
                data = json.loads(text)
                collect_embed_ids(data, ids)
            except:
                pass

    # 2) __NEXT_DATA__
    next_script = tree.css_first('script#__NEXT_DATA__')
    if next_script:
        try:
            data = json.loads(next_script.text())
            collect_embed_ids(data, ids)
        except:
            pass

    # 3) Fallback
    ids.update(extract_uuids(html))

    ids = sorted(ids)

    log.info(f"URL {url_num}) Found {len(ids)} embed IDs")

    return ids


# -------------------------------------------------
# Extract events from schedule page
# -------------------------------------------------
async def get_events_from_schedule(cached_hrefs: set[str]) -> list[dict[str, str]]:
    events = []

    response = await network.request(SCHEDULE_URL, log=log)

    if not response:
        log.error("Failed to fetch schedule page")
        return events

    content = response.text
    tree = HTMLParser(content)

    # Find all event links - look for /watch/ URLs in href
    watch_links = tree.css('a[href*="/watch/"]')

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

    seen_hrefs = set()

    for link in watch_links:
        href = link.attributes.get("href", "")

        # Extract watch ID
        watch_match = re.search(r'/watch/([a-z0-9\-]+)', href, re.I)
        if not watch_match:
            continue

        watch_id = watch_match.group(1)

        if watch_id in cached_hrefs or watch_id in seen_hrefs:
            continue

        seen_hrefs.add(watch_id)

        watch_url = f"{WATCH_BASE}/{watch_id}"

        # Get the parent card or container
        parent = link.parent
        card_content = ""

        # Traverse up to find the card container
        for _ in range(5):
            if parent:
                card_content = parent.html
                parent = parent.parent
            else:
                break

        # Extract title from h1 within the card
        title_match = re.search(r'<h1[^>]*>([^<]+)</h1>', card_content, re.I)
        if title_match:
            title = title_match.group(1).strip()
        else:
            # Try broader search
            title_pattern = rf'href=["\']/watch/{watch_id}["\'][^>]*>.*?<h1[^>]*>([^<]+)</h1>'
            title_match = re.search(title_pattern, content, re.S | re.I)
            title = title_match.group(1).strip() if title_match else f"Event {watch_id[:8]}"

        # Extract date from h2 within the card
        date_match = re.search(r'<h2[^>]*>([^<]+)</h2>', card_content, re.I)
        if date_match:
            event_date = date_match.group(1).strip()
            event_date = event_date.replace(',', '')
        else:
            # Try broader search
            date_pattern = rf'href=["\']/watch/{watch_id}["\'][^>]*>.*?<h2[^>]*>([^<]+)</h2>'
            date_match = re.search(date_pattern, content, re.S | re.I)
            event_date = date_match.group(1).strip().replace(',', '') if date_match else ""

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

    log.info(f"Found {len(events)} events from schedule page")
    return events


# -------------------------------------------------
# Get events
# -------------------------------------------------
async def get_events(cached_hrefs: set[str]) -> list[dict[str, str]]:
    return await get_events_from_schedule(cached_hrefs)


# -------------------------------------------------
# Extract stream with Playwright (fallback)
# -------------------------------------------------
async def extract_stream_with_playwright(watch_url: str, url_num: int) -> str | None:
    stream = None

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
            nonlocal stream
            if stream:
                return

            url = clean_stream_url(response.url)

            if is_stream_url(url):
                stream = url
                return

            if "application/json" not in response.headers.get("content-type", ""):
                return

            try:
                body = await response.text()
                for m in re.findall(r'https?://[^"\']+', body, re.I):
                    if is_stream_url(m):
                        stream = clean_stream_url(m)
                        return
            except:
                pass

        page.on("response", handle_response)

        # Block unnecessary resources
        await page.route(
            "**/*",
            lambda route: (
                route.abort()
                if route.request.resource_type in ("image", "font", "media")
                else route.continue_()
            ),
        )

        try:
            await page.goto(
                watch_url,
                wait_until="domcontentloaded",
                timeout=25000,
            )

            # Wait for stream to be captured
            for _ in range(6):
                if stream:
                    break
                await asyncio.sleep(1)

        except Exception as e:
            log.error(f"URL {url_num}) Playwright error: {e}")

        finally:
            await page.close()
            await browser.close()

    return stream


# -------------------------------------------------
# Process event to extract streams
# -------------------------------------------------
async def process_event(watch_id: str, url: str, url_num: int) -> list[dict]:
    """Process event to extract all stream URLs"""
    streams = []

    # Step 1: Extract embed IDs from the watch page
    embed_ids = await extract_embed_ids_from_page(url, url_num)

    if not embed_ids:
        log.warning(f"URL {url_num}) No embed IDs found")
        return streams

    # Step 2: Parallel API requests
    sem = asyncio.Semaphore(MAX_CONCURRENT_API)

    async def worker(embed_id):
        async with sem:
            results = []
            async for stream in fetch_api_stream(embed_id):
                results.append(stream)
            return results

    tasks = [worker(e) for e in embed_ids]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    for result in results:
        if isinstance(result, Exception):
            continue
        streams.extend(result)

    if streams:
        log.info(f"URL {url_num}) Found {len(streams)} streams via API")
        return streams

    # Step 3: Playwright fallback
    log.info(f"URL {url_num}) Trying Playwright fallback")
    stream_url = await extract_stream_with_playwright(url, url_num)

    if stream_url:
        streams.append({"title": "", "url": stream_url})

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

    events = await get_events(cached_hrefs)
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

            if stream_title:
                title = f"[{ev['sport']}] {ev['event']} - {stream_title} ({TAG})"
            else:
                title = f"[{ev['sport']}] {ev['event']}"
                if ev.get('date'):
                    title += f" ({ev['date']})"
                title += f" ({TAG})"

            tvg_id, _logo_lookup = leagues.get_tvg_info(ev["sport"], ev["event"])

            # Generate unique key for multiple streams per event
            key = title
            if title in urls:
                key = f"{title} [{len([k for k in urls.keys() if k.startswith(title[:50])]) + 1}]"

            urls[key] = {
                "url": stream_url,
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
            log.info(f"Event {i}) ✓ Added stream: {stream_url[:100]}...")

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
