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
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36 Edg/134.0.0.0"


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
            headers={"Referer": ref_url},
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
    m3u8 = base64.b64decode(match[2]).decode("utf-8")

    log.info(f"URL {url_num}) Captured M3U8: {m3u8[:100]}...")

    return m3u8, ref_url


async def get_events() -> list[dict[str, str]]:
    """
    Fetch and parse events from the API.
    """
    now = Time.clean(Time.now())
    events = []

    log.info(f"Fetching events from API: {API_URL}")

    if not (api_data := API_FILE.load(per_entry=False, index=-1)):
        log.info("Refreshing API cache")

        api_data = [{"timestamp": now.timestamp()}]

        if r := await network.request(API_URL, log=log):
            api_data = r.json()

            api_data[-1]["timestamp"] = now.timestamp()

        API_FILE.write(api_data)

    if not (date := api_data[0].get("day")):
        log.warning("No date found in API response")
        return events

    # Parse and compare date
    api_date = re.sub(
        r"(?<=\d)(st|nd|rd|th)",
        "",
        date.split("-")[0].strip(),
        flags=re.I,
    )

    current_date = f"{now:%A} {now.day} {now:%B} {now:%Y}"
    
    if api_date != current_date:
        log.info(f"API date mismatch. API: {api_date}, Current: {current_date}")
        return events

    for category in api_data[0].get("categories", {}):
        # Skip unwanted categories
        if category.lower() in ["popular live events", "tv shows"]:
            continue

        category_info = api_data[0]["categories"][category]

        for event_info in category_info:
            if event_info.get("source") != "tv":
                continue

            if not (channels := event_info.get("channels")):
                continue

            name: str = event_info["event"]
            channel_id: str = channels[0]["channel_id"]

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

    log.info('Scraping from "strmeast"')

    if events := await get_events():
        log.info(f"Processing {len(events)} URL(s)")

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

    else:
        log.info("No events found")

    CACHE_FILE.write(cached_urls)
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
