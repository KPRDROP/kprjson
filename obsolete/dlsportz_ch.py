import asyncio
import base64
import re
from functools import partial
from urllib.parse import urlsplit, quote

import cloudscraper

from utils import Cache, Time, get_logger, leagues, network

log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}

TAG = "DLSPORTZ_CH"

CACHE_FILE = Cache(TAG, exp=3_600)

API_FILE = Cache(f"{TAG}-api", exp=28_800)

API_URL = "https://streameast.mov/api/channels"

# Headers for requests
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"

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

        base_url = base.split('?')[0] if base else ""
        safe_name = name.replace('"', '').replace("'", "")

        extinf = (
            f'#EXTINF:-1 tvg-chno="{chno}" tvg-id="{tvg_id}" '
            f'tvg-name="{safe_name}" tvg-logo="{logo}" group-title="Live Channels",{safe_name}'
        )

        vlc_lines.append(extinf)
        if base_url:
            vlc_lines.append(f"#EXTVLCOPT:http-referrer={base_url}")
            vlc_lines.append(f"#EXTVLCOPT:http-origin={base_url}")
        vlc_lines.append(f"#EXTVLCOPT:http-user-agent={USER_AGENT}")
        vlc_lines.append(url)

        tivimate_lines.append(extinf)

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

    with open("dlsportz_ch_vlc.m3u8", "w", encoding="utf8") as f:
        f.write("\n".join(vlc_lines))

    with open("dlsportz_ch_tivimate.m3u8", "w", encoding="utf8") as f:
        f.write("\n".join(tivimate_lines))

    log.info(f"Playlists generated: {len(urls)} streams -> dlsportz_ch_vlc.m3u8 / dlsportz_ch_tivimate.m3u8")


async def process_event(channel_id: str, url_num: int) -> tuple[str | None, str | None]:
    """
    Process a single channel to extract m3u8 URL.
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

    pattern = re.compile(r'source:\s+window\.atob\((\'|\")([^"]*)(\'|\")\)', re.I)

    if not (match := pattern.search(html_data.text)):
        log.warning(f"URL {url_num}) No M3U8 found")
        return nones

    try:
        m3u8 = base64.b64decode(match[2]).decode("utf-8")
        log.info(f"URL {url_num}) Captured M3U8: {m3u8[:100]}...")
        return m3u8, ref_url
    except Exception as e:
        log.error(f"URL {url_num}) Failed to decode base64: {e}")
        return nones


def fetch_api_with_cloudscraper() -> list | None:
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


async def get_channels() -> list[dict[str, str]]:
    """
    Fetch and parse channels from the API.
    The /api/channels endpoint returns a direct array of channel objects.
    """
    now = Time.clean(Time.now())
    channels = []

    log.info(f"Fetching channels from API: {API_URL}")

    # Try to load from cache first
    api_data = API_FILE.load(per_entry=False, index=-1)
    
    api_data_valid = False
    
    if api_data and isinstance(api_data, list) and len(api_data) > 0:
        last_timestamp = api_data[-1].get("timestamp", 0) if isinstance(api_data[-1], dict) else 0
        current_time = now.timestamp()
        
        if current_time - last_timestamp < 28_800:  # 8 hours
            log.info("Using cached API data")
            api_data_valid = True

    if not api_data_valid:
        log.info("Refreshing API cache using cloudscraper")
        
        api_data = None
        for attempt in range(3):
            log.info(f"API request attempt {attempt + 1}/3")
            api_data = await asyncio.to_thread(fetch_api_with_cloudscraper)
            
            if api_data and isinstance(api_data, list) and len(api_data) > 0:
                # Add timestamp as a separate metadata entry
                api_data.append({"timestamp": now.timestamp()})
                API_FILE.write(api_data)
                log.info(f"API cache updated with {len(api_data) - 1} channels")
                api_data_valid = True
                break
            await asyncio.sleep(2)

    if not api_data_valid or not api_data or len(api_data) == 0:
        log.warning("No valid API data available after cloudscraper attempts")
        return channels

    # Remove the timestamp entry from the end for processing
    channels_data = [item for item in api_data if "timestamp" not in item]

    log.info(f"Processing {len(channels_data)} channels from API")

    for channel in channels_data:
        channel_name = channel.get("channel_name", "")
        channel_id = channel.get("channel_id", "")
        
        if not channel_name or not channel_id:
            continue

        # Determine sport/category from channel name (heuristic)
        sport = "Channel"
        
        # Try to extract sport from channel name
        sport_keywords = {
            "Soccer": ["Soccer", "Football", "LaLiga", "Premier", "Bundesliga", "Serie A", "Ligue"],
            "Motorsport": ["F1", "Formula", "Racing", "MotoGP", "NASCAR"],
            "MMA": ["MMA", "UFC", "Fight", "Boxing", "Kickboxing"],
            "Basketball": ["Basketball", "NBA", "Euroleague"],
            "Tennis": ["Tennis", "WTA", "ATP"],
            "Hockey": ["Hockey", "NHL"],
            "Baseball": ["Baseball", "MLB"],
            "Cricket": ["Cricket"],
            "Rugby": ["Rugby"],
        }
        
        for sport_name, keywords in sport_keywords.items():
            for keyword in keywords:
                if keyword.lower() in channel_name.lower():
                    sport = sport_name
                    break
            if sport != "Channel":
                break

        key = f"[{sport}] {channel_name} ({TAG})"

        # Skip if already in cache
        cached_urls = CACHE_FILE.load()
        if key in cached_urls and cached_urls[key].get("url"):
            log.debug(f"Channel {channel_name} already cached, skipping")
            continue

        channels.append({
            "key": key,
            "sport": sport,
            "event": channel_name,
            "channel-id": channel_id,
            "timestamp": now.timestamp(),
        })

        log.info(f"Found channel: {channel_name} (ID: {channel_id})")

    return channels


async def scrape() -> None:
    """
    Main scraping function.
    """
    cached_urls = CACHE_FILE.load()
    valid_urls = {k: v for k, v in cached_urls.items() if v.get("url")}

    if valid_urls:
        urls.update(valid_urls)
        log.info(f"Loaded {len(valid_urls)} channel(s) from cache")
        generate_playlists()
        
        # Still check for new channels from API
        log.info("Checking for new channels from API...")
    else:
        log.info('Scraping from "streameast.mov"')

    channels = await get_channels()
    
    if channels:
        log.info(f"Processing {len(channels)} new channel(s)")

        if not isinstance(cached_urls, dict):
            cached_urls = {}

        new_streams_count = 0
        
        for i, ch in enumerate(channels, start=1):
            key = ch["key"]
            
            # Check if already cached
            if key in cached_urls and cached_urls[key].get("url"):
                log.info(f"--- [{i}/{len(channels)}]: {ch['event']} (already cached, skipping) ---")
                continue
                
            log.info(f"--- [{i}/{len(channels)}]: {ch['event']} ---")

            handler = partial(
                process_event,
                channel_id=ch["channel-id"],
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
                ch["sport"],
                ch["event"],
                ch["timestamp"],
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
                log.info(f"✓ [{i}] Stream captured: {url[:100]}...")
            else:
                log.warning(f"✗ [{i}] No stream captured")

            await asyncio.sleep(1)

        if new_streams_count > 0:
            log.info(f"Collected and cached {new_streams_count} new channel(s)")
            CACHE_FILE.write(cached_urls)
        else:
            log.info("No new streams found")
    else:
        log.info("No new channels found from API")
        if valid_urls:
            log.info("Using cached channels from previous runs")

    generate_playlists()


async def main():
    """
    Main async entry point.
    """
    log.info("=" * 50)
    log.info("Starting DLSPORTZ_CH Streams Updater")
    log.info(f"API URL: {API_URL}")
    log.info("=" * 50)

    try:
        await scrape()
    except Exception as e:
        log.error(f"Scraping failed: {e}")
        raise

    log.info("DLSPORTZ_CH Streams Updater finished")


if __name__ == "__main__":
    asyncio.run(main())
