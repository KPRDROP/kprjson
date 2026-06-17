import asyncio
import base64
import re
from functools import partial
from urllib.parse import urlsplit, quote

from utils import Cache, Time, get_logger, leagues, network

log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}

TAG = "DLSPORTZ"

CACHE_FILE = Cache(TAG, exp=3_600)

API_FILE = Cache(f"{TAG}-api", exp=28_800)

API_URL = "https://streameast.mov/api/events"

# Headers for requests
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"

# Additional headers to avoid blocking - more comprehensive
API_HEADERS = {
    "Referer": "https://streameast.mov/",
    "Origin": "https://streameast.mov",
    "User-Agent": USER_AGENT,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9,es;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Ch-UA": '"Chromium";v="134", "Not:A-Brand";v="24", "Microsoft Edge";v="134"',
    "Sec-Ch-UA-Mobile": "?0",
    "Sec-Ch-UA-Platform": '"Windows"',
    "Connection": "keep-alive",
}


def generate_playlists():
    """
    Generate VLC and TiviMate M3U8 playlists from captured streams.
    """
    vlc_lines = ["#EXTM3U"]
    tivimate_lines = ["#EXTM3U"]

    ua_encoded = quote(USER_AGENT, safe="")

    for chno, (name, data) in enumerate(urls.items(), start=1):
        url = data.get("url")
        logo = data.get("logo") or ""
        tvg_id = data.get("id", "Live.Event.us")
        base = data.get("base", "")

        if not url:
            continue

        # Extract base URL for referer (remove query parameters)
        base_url = base.split('?')[0] if base else ""

        # Sanitize name for playlist
        safe_name = name.replace('"', '').replace("'", "")

        extinf = (
            f'#EXTINF:-1 tvg-chno="{chno}" tvg-id="{tvg_id}" '
            f'tvg-name="{safe_name}" tvg-logo="{logo}" group-title="Live Events",{safe_name}'
        )

        # VLC (no pipe encoding needed)
        vlc_lines.append(extinf)
        if base_url:
            vlc_lines.append(f"#EXTVLCOPT:http-referrer={base_url}")
            vlc_lines.append(f"#EXTVLCOPT:http-origin={base_url}")
        vlc_lines.append(f"#EXTVLCOPT:http-user-agent={USER_AGENT}")
        vlc_lines.append(url)

        # TiviMate (pipe format with encoded user agent)
        tivimate_lines.append(extinf)

        # Build the pipe-formatted URL
        if base_url:
            tiv_url = (
                f"{url}"
                f"|referer={base_url}"
                f"|origin={base_url}"
                f"|user-agent={ua_encoded}"
            )
        else:
            tiv_url = f"{url}|user-agent={ua_encoded}"

        tivimate_lines.append(tiv_url)

    # Write VLC playlist
    with open("dlsportz_vlc.m3u8", "w", encoding="utf8") as f:
        f.write("\n".join(vlc_lines))

    # Write TiviMate playlist
    with open("dlsportz_tivimate.m3u8", "w", encoding="utf8") as f:
        f.write("\n".join(tivimate_lines))

    log.info(f"Playlists generated: {len(urls)} streams -> dlsportz_vlc.m3u8 / dlsportz_tivimate.m3u8")


async def process_event(channel_id: str, url_num: int) -> tuple[str | None, str | None]:
    """
    Process a single event/channel to extract m3u8 URL.
    """
    nones = None, None

    ifr_url = f"https://donis.jimpenopisonline.online/premiumtv/resportz.php?id={channel_id}"
    ref_url = f"https://resportz.cfd/live/stream-{channel_id}.php"

    log.info(f"URL {url_num}) Processing channel {channel_id}")
    log.debug(f"URL {url_num}) Iframe URL: {ifr_url}")
    log.debug(f"URL {url_num}) Referer: {ref_url}")

    if not (
        html_data := await network.request(
            ifr_url,
            headers={"Referer": ref_url, "User-Agent": USER_AGENT},
            log=log,
        )
    ):
        log.warning(f"URL {url_num}) Failed to load url.")
        return nones

    # Pattern to find base64 encoded source
    pattern = re.compile(r'source:\s+window\.atob\((\'|\")([^"]*)(\'|\")\)', re.I)

    if not (match := pattern.search(html_data.text)):
        log.warning(f"URL {url_num}) No M3U8 found")
        return nones

    # Decode base64 to get m3u8 URL
    try:
        m3u8 = base64.b64decode(match[2]).decode("utf-8")
        log.info(f"URL {url_num}) Captured M3U8: {m3u8[:100]}...")
        return m3u8, ref_url
    except Exception as e:
        log.error(f"URL {url_num}) Failed to decode base64: {e}")
        return nones


async def get_events() -> list[dict[str, str]]:
    """
    Fetch and parse events from the API.
    """
    now = Time.clean(Time.now())
    events = []

    log.info(f"Fetching events from API: {API_URL}")

    # Try to load from cache first
    api_data = API_FILE.load(per_entry=False, index=-1)
    
    # If cache exists and is recent, use it
    if api_data and len(api_data) > 0:
        # Check if cache has timestamp and is not too old (within 8 hours)
        last_timestamp = api_data[-1].get("timestamp", 0) if isinstance(api_data, list) else 0
        current_time = now.timestamp()
        
        if current_time - last_timestamp < 28_800:  # 8 hours
            log.info("Using cached API data")
        else:
            api_data = None
            log.info("API cache expired, refreshing")

    if not api_data:
        log.info("Refreshing API cache")
        
        # Make request with proper headers
        r = await network.request(
            API_URL, 
            headers=API_HEADERS,
            log=log,
            timeout=30
        )
        
        if r and r.content:
            try:
                api_data = r.json()
                if isinstance(api_data, list) and len(api_data) > 0:
                    # Add timestamp to the last element or create a metadata object
                    if isinstance(api_data[-1], dict):
                        api_data[-1]["timestamp"] = now.timestamp()
                    API_FILE.write(api_data)
                    log.info(f"API cache updated with {len(api_data)} items")
                else:
                    log.warning("API returned invalid data format")
                    api_data = None
            except Exception as e:
                log.error(f"Failed to parse API response: {e}")
                api_data = None
        else:
            log.error("Failed to fetch API data")
            api_data = None

    if not api_data or len(api_data) == 0:
        log.warning("No API data available")
        return events

    # Get the first item which contains the date and categories
    first_item = api_data[0]
    
    if not (date := first_item.get("day")):
        log.warning("No date found in API response")
        log.debug(f"API response keys: {list(first_item.keys()) if isinstance(first_item, dict) else 'not a dict'}")
        return events

    # Parse and compare date (extract the date part before the dash)
    try:
        # The date format is like "Friday 12th June 2026 - Schedule Time UK GMT"
        date_part = date.split("-")[0].strip()
        # Remove ordinal indicators (st, nd, rd, th)
        api_date = re.sub(r"(?<=\d)(st|nd|rd|th)", "", date_part, flags=re.I)
        current_date = f"{now:%A} {now.day} {now:%B} {now:%Y}"
        
        if api_date != current_date:
            log.info(f"API date mismatch. API: {api_date}, Current: {current_date}")
            return events
    except Exception as e:
        log.error(f"Date parsing error: {e}")
        return events

    # Process categories
    categories = first_item.get("categories", {})
    if not categories:
        log.warning("No categories found in API response")
        return events

    for category, category_info in categories.items():
        # Skip unwanted categories
        if category.lower() in ["popular live events", "tv shows"]:
            continue

        # category_info should be a list of events
        if not isinstance(category_info, list):
            continue

        for event_info in category_info:
            if event_info.get("source") != "tv":
                continue

            channels = event_info.get("channels")
            if not channels or not isinstance(channels, list):
                continue

            # Get the first channel
            channel = channels[0]
            channel_id = channel.get("channel_id")
            
            if not channel_id:
                continue

            name = event_info.get("event", "")
            if not name:
                continue

            events.append(
                {
                    "sport": category,
                    "event": name,
                    "channel-id": channel_id,
                    "timestamp": now.timestamp(),
                }
            )

            log.info(f"Found event: {name} (Channel: {channel_id})")

    return events


async def scrape() -> None:
    """
    Main scraping function.
    """
    # Load cached URLs
    cached_urls = CACHE_FILE.load()
    valid_urls = {k: v for k, v in cached_urls.items() if v.get("url")}

    if valid_urls:
        urls.update(valid_urls)
        log.info(f"Loaded {len(valid_urls)} event(s) from cache")
        generate_playlists()
        return

    log.info('Scraping from "streameast.mov"')

    events = await get_events()
    
    if events:
        log.info(f"Processing {len(events)} URL(s)")

        # Initialize cached_urls as dict if needed
        if not isinstance(cached_urls, dict):
            cached_urls = {}

        for i, ev in enumerate(events, start=1):
            log.info(f"--- [{i}/{len(events)}]: {ev['event']} ---")

            handler = partial(
                process_event,
                channel_id=ev["channel-id"],
                url_num=i,
            )

            url, iframe = await network.safe_process(
                handler,
                url_num=i,
                semaphore=network.HTTP_S,
                timeout=30,
                log=log,
            )

            sport, event, ts = (
                ev["sport"],
                ev["event"],
                ev["timestamp"],
            )

            key = f"[{sport}] {event} ({TAG})"

            tvg_id, logo = leagues.get_tvg_info(sport, event)

            entry = {
                "url": url,
                "logo": logo,
                "base": iframe,
                "timestamp": ts,
                "id": tvg_id or "Live.Event.us",
                "link": iframe,
            }

            cached_urls[key] = entry

            if url:
                urls[key] = entry
                log.info(f"✓ [{i}] Stream captured: {url[:100]}...")
            else:
                log.warning(f"✗ [{i}] No stream captured")

            # Small delay between requests
            await asyncio.sleep(1)

        log.info(f"Collected and cached {len(urls)} event(s)")
        CACHE_FILE.write(cached_urls)
    else:
        log.info("No events found")
        # Still generate playlists with cached data if available
        if urls:
            generate_playlists()
        return

    generate_playlists()


async def main():
    """
    Main async entry point.
    """
    log.info("=" * 50)
    log.info("Starting DLSPORTZ Streams Updater")
    log.info(f"API URL: {API_URL}")
    log.info("=" * 50)

    try:
        await scrape()
    except Exception as e:
        log.error(f"Scraping failed: {e}")
        raise

    log.info("DLSPORTZ Streams Updater finished")


if __name__ == "__main__":
    asyncio.run(main())
