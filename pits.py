import asyncio
import re
import json
from pathlib import Path
from urllib.parse import quote_plus, urljoin

from utils import Cache, Event, Time, get_logger, leagues, network

log = get_logger(__name__)

TAG = "PITS"
BASE_URL = "https://pitsport.live"
API_URL = "https://api.pitsport.live/v1/streams"
WATCH_API_BASE = "https://api.pitsport.live/watch"
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
# Extract stream from embed API
# -------------------------------------------------
async def extract_stream_from_embed(embed_id: str, url_num: int) -> dict | None:
    """Extract stream from embed API"""
    api_url = f"https://api.pushembdz.store/v1/stream/{embed_id}"

    try:
        response = await network.request(
            api_url,
            headers={
                "User-Agent": UA,
                "Referer": "https://pushembdz.store/",
                "Origin": "https://pushembdz.store",
                "Accept": "application/json",
            },
            log=log,
        )

        if not response:
            return None

        data = json.loads(response.text)

        if "stream" in data and "link" in data["stream"]:
            link = clean_stream_url(data["stream"]["link"])
            if is_stream_url(link):
                return {
                    "url": link,
                    "title": data["stream"].get("title", ""),
                }

        if "link" in data:
            link = clean_stream_url(data["link"])
            if is_stream_url(link):
                return {
                    "url": link,
                    "title": "",
                }

    except Exception as e:
        log.debug(f"URL {url_num}) Embed API error: {e}")

    return None


# -------------------------------------------------
# Get watch page content from API
# -------------------------------------------------
async def get_watch_content(watch_id: str, url_num: int) -> list[dict]:
    """Get watch page content from the API"""
    watch_api_url = f"{WATCH_API_BASE}/{watch_id}"

    try:
        response = await network.request(
            watch_api_url,
            headers={
                "User-Agent": UA,
                "Referer": BASE_URL,
                "Accept": "application/json",
            },
            log=log,
        )

        if not response:
            return []

        data = json.loads(response.text)

        if not data.get("success"):
            log.debug(f"URL {url_num}) API returned success=false")
            return []

        content = data.get("content", [])
        log.info(f"URL {url_num}) Found {len(content)} embeds from API")

        return content

    except Exception as e:
        log.debug(f"URL {url_num}) Watch API error: {e}")

    return []


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

    # Step 1: Get watch content from API
    content = await get_watch_content(watch_id, url_num)

    if not content:
        log.warning(f"URL {url_num}) No content found from API")
        return streams

    # Step 2: Extract streams from each embed in content
    for item in content:
        iframe = item.get("iframe")
        custom_text = item.get("customText")

        if not iframe:
            continue

        # Extract embed ID from iframe URL
        uuid_match = re.search(r'/embed/([0-9a-f\-]{36})', iframe, re.I)
        if not uuid_match:
            continue

        embed_id = uuid_match.group(1)

        # Get stream from embed API
        stream_result = await extract_stream_from_embed(embed_id, url_num)

        if stream_result:
            stream_url = stream_result.get("url")
            stream_title = stream_result.get("title")

            # Use customText as title suffix if available
            if custom_text:
                if stream_title:
                    stream_title = f"{stream_title} ({custom_text})"
                else:
                    stream_title = custom_text

            streams.append({
                "url": stream_url,
                "title": stream_title,
            })

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
            # Check if this title already exists
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
