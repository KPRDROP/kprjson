import asyncio
import json
import re
from functools import partial
from urllib.parse import parse_qsl, urlsplit, quote

from selectolax.parser import HTMLParser

from utils import Cache, Time, get_logger, leagues, network

log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}

TAG = "VIVA"

CACHE_FILE = Cache(TAG, exp=19_800)

BASE_URL = "https://vivatops.cyou"

# Headers for requests
HEADERS = {
    "Referer": "https://edher.lockedherhe.site/",
    "Origin": "https://edher.lockedherhe.site",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
}

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"


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
        base = data.get("base", "https://edher.lockedherhe.site/")

        if not url:
            continue

        # Sanitize name for playlist
        safe_name = name.replace('"', '').replace("'", "")

        extinf = (
            f'#EXTINF:-1 tvg-chno="{chno}" tvg-id="{tvg_id}" '
            f'tvg-name="{safe_name}" tvg-logo="{logo}" group-title="Live Events",{safe_name}'
        )

        # VLC (no pipe encoding needed)
        vlc_lines.append(extinf)
        vlc_lines.append(f"#EXTVLCOPT:http-referrer={base}")
        vlc_lines.append(f"#EXTVLCOPT:http-origin={base}")
        vlc_lines.append(f"#EXTVLCOPT:http-user-agent={USER_AGENT}")
        vlc_lines.append(url)

        # TiviMate (pipe format with encoded user agent)
        tivimate_lines.append(extinf)

        # Build the pipe-formatted URL
        tiv_url = (
            f"{url}"
            f"|referer={base}"
            f"|origin={base}"
            f"|user-agent={ua_encoded}"
        )

        tivimate_lines.append(tiv_url)

    # Write VLC playlist
    with open("viva_vlc.m3u8", "w", encoding="utf8") as f:
        f.write("\n".join(vlc_lines))

    # Write TiviMate playlist
    with open("viva_tivimate.m3u8", "w", encoding="utf8") as f:
        f.write("\n".join(tivimate_lines))

    log.info(f"Playlists generated: {len(urls)} streams -> viva_vlc.m3u8 / viva_tivimate.m3u8")


async def process_event(channel_id: str, url_num: int) -> tuple[str | None, str | None]:
    """
    Process a single event/channel to extract m3u8 URL.
    """
    nones = None, None

    ifr_url = f"https://edher.lockedherhe.site/player_stateless/channel{channel_id}"
    ref_url = f"{BASE_URL}/vivo/?ch={channel_id}"

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

    # Pattern to find streamUrl in the response
    valid_m3u8 = re.compile(r'streamUrl\s+=\s+"([^"]*)"', re.I)

    if not (match := valid_m3u8.search(html_data.text)):
        log.warning(f"URL {url_num}) No M3U8 found")
        return nones

    # Parse the captured URL (handles escaped characters)
    stream_url = json.loads(f'"{match[1]}"')
    log.info(f"URL {url_num}) Captured M3U8: {stream_url[:100]}...")

    return stream_url, ref_url


async def get_events() -> list[dict[str, str]]:
    """
    Fetch and parse events from the main page.
    """
    now = Time.clean(Time.now())
    events = []

    log.info(f"Fetching events from {BASE_URL}")

    if not (html_data := await network.request(BASE_URL, log=log)):
        log.error("Failed to fetch main page")
        return events

    soup = HTMLParser(html_data.content)

    # Check if page is up to date
    if not (last_update := soup.css_first("h2.update")):
        log.warning("No update date found on page")
        return events

    update_text = last_update.text(strip=True)
    update_date = update_text.split(":")[-1].split()[0] if ":" in update_text else update_text.split()[0]

    if now.strftime("%d-%m-%y") != update_date:
        log.info(f"Page not updated today. Update date: {update_date}")
        return events

    sport = "Live Event"

    for matches in soup.css(".match"):
        if not (a_elem := matches.css_first("a")) or not (
            href := a_elem.attributes.get("href")
        ):
            continue

        params = dict(parse_qsl(urlsplit(href).query, keep_blank_values=True))

        if not (channel_id := params.get("ch")):
            continue

        for event in matches.css("strong"):
            splits = event.text(strip=True).split()

            event_name = (
                " ".join(splits[: splits.index("-")])
                if "-" in splits
                else " ".join(splits)
            )

            events.append(
                {
                    "sport": sport,
                    "event": event_name,
                    "channel-id": channel_id,
                    "timestamp": now.timestamp(),
                }
            )

            log.info(f"Found event: {event_name} (Channel: {channel_id})")

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

    log.info(f'Scraping from "{BASE_URL}"')

    if events := await get_events():
        log.info(f"Processing {len(events)} new URL(s)")

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
                "base": "https://edher.lockedherhe.site/",
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

        log.info(f"Collected and cached {len(urls)} new event(s)")

    else:
        log.info("No events found")

    CACHE_FILE.write(cached_urls)
    generate_playlists()


async def main():
    """
    Main async entry point.
    """
    log.info("=" * 50)
    log.info("Starting VIVA Streams Updater")
    log.info(f"Base URL: {BASE_URL}")
    log.info("=" * 50)

    try:
        await scrape()
    except Exception as e:
        log.error(f"Scraping failed: {e}")
        raise

    log.info("VIVA Streams Updater finished")


if __name__ == "__main__":
    asyncio.run(main())
