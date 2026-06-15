import asyncio
import base64
import re
from functools import partial
from urllib.parse import urlsplit, quote

import cloudscraper

from utils import Cache, Time, get_logger, leagues, network

log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}

TAG = "DLSPORTZ"

CACHE_FILE = Cache(TAG, exp=3_600)

API_FILE = Cache(f"{TAG}-api", exp=28_800)

API_URL = "https://streameast.mov/api/events"

# Headers for requests
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36 Edg/134.0.0.0"

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
    if not urls:
        log.warning("No streams to generate playlists")
        return
        
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


def fetch_api_with_cloudscraper() -> dict | None:
    """
    Fetch API data using cloudscraper to bypass Cloudflare protection.
    """
    try:
        scraper = cloudscraper.create_scraper(
            browser={
                'browser': 'chrome',
                'platform': 'windows',
                'desktop': True,
                'mobile': False
            }
        )
        
        response = scraper.get(
            API_URL,
            headers=API_HEADERS,
            timeout=30
        )
        
        if response.status_code == 200:
            return response.json()
        else:
            log.error(f"Cloudscraper request failed with status: {response.status_code}")
            return None
            
    except Exception as e:
        log.error(f"Cloudscraper error: {e}")
        return None


async def get_events() -> list[dict[str, str]]:
    """
    Fetch and parse events from the API using cloudscraper.
    """
    now = Time.clean(Time.now())
    events = []

    log.info(f"Fetching events from API: {API_URL}")

    # Try to load from cache first
    api_data = API_FILE.load(per_entry=False, index=-1)
    
    api_data_valid = False
    
    # If cache exists, check if it's recent and has today's date
    if api_data and len(api_data) > 0:
        last_timestamp = api_data[-1].get("timestamp", 0) if isinstance(api_data, list) else 0
        current_time = now.timestamp()
        
        # Check if cache is within 8 hours
        if current_time - last_timestamp < 28_800:
            # Check if the cached data has today's date
            first_item = api_data[0]
            if date := first_item.get("day"):
                try:
                    date_part = date.split("-")[0].strip()
                    api_date = re.sub(r"(?<=\d)(st|nd|rd|th)", "", date_part, flags=re.I)
                    current_date = f"{now:%A} {now.day} {now:%B} {now:%Y}"
                    
                    if api_date == current_date:
                        log.info("Using cached API data (today's date)")
                        api_data_valid = True
                    else:
                        log.info(f"Cached API data is from {api_date}, not today. Will refresh.")
                except Exception:
                    pass

    if not api_data_valid:
        log.info("Refreshing API cache using cloudscraper")
        
        # Use cloudscraper to fetch API data
        api_data = None
        for attempt in range(3):
            log.info(f"API request attempt {attempt + 1}/3")
            api_data = await asyncio.to_thread(fetch_api_with_cloudscraper)
            
            if api_data and isinstance(api_data, list) and len(api_data) > 0:
                # Add timestamp to the last element
                if isinstance(api_data[-1], dict):
                    api_data[-1]["timestamp"] = now.timestamp()
                API_FILE.write(api_data)
                log.info(f"API cache updated with {len(api_data)} items")
                api_data_valid = True
                break
            await asyncio.sleep(2)

    if not api_data_valid or not api_data or len(api_data) == 0:
        log.warning("No valid API data available after cloudscraper attempts")
        return events

    # Get the first item which contains the date and categories
    first_item = api_data[0]
    
    if not (date := first_item.get("day")):
        log.warning("No date found in API response")
        return events

    # Parse and compare date (extract the date part before the dash)
    try:
        date_part = date.split("-")[0].strip()
        api_date = re.sub(r"(?<=\d)(st|nd|rd|th)", "", date_part, flags=re.I)
        current_date = f"{now:%A} {now.day} {now:%B} {now:%Y}"
        
        if api_date != current_date:
            log.info(f"API date mismatch. API: {api_date}, Current: {current_date}")
            log.info("API not yet updated for today. No new events will be processed.")
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
        
        # Still check for new events from API
        log.info("Checking for new events from API...")
    else:
        log.info('Scraping from "streameast.mov"')

    events = await get_events()
    
    if events:
        log.info(f"Processing {len(events)} new URL(s)")

        # Initialize cached_urls as dict if needed
        if not isinstance(cached_urls, dict):
            cached_urls = {}

        new_streams_count = 0
        
        for i, ev in enumerate(events, start=1):
            # Check if event already exists in cache
            key = f"[{ev['sport']}] {ev['event']} ({TAG})"
            if key in cached_urls and cached_urls[key].get("url"):
                log.info(f"--- [{i}/{len(events)}]: {ev['event']} (already cached, skipping) ---")
                continue
                
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
                new_streams_count += 1
                log.info(f"✓ [{i}] New stream captured: {url[:100]}...")
            else:
                log.warning(f"✗ [{i}] No stream captured")

            # Small delay between requests
            await asyncio.sleep(1)

        if new_streams_count > 0:
            log.info(f"Collected and cached {new_streams_count} new event(s)")
            CACHE_FILE.write(cached_urls)
        else:
            log.info("No new streams found")
    else:
        log.info("No new events found from API")
        if valid_urls:
            log.info("Using cached streams from previous runs")
        else:
            log.info("No streams available")

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
