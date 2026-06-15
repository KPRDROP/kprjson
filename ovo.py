import re
from functools import partial
from urllib.parse import urljoin, quote
from pathlib import Path

from selectolax.parser import HTMLParser

from .utils import Cache, Time, get_logger, leagues, network

log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}

TAG = "OVO"

CACHE_FILE = Cache(TAG, exp=10_800)

HTML_FILE = Cache(f"{TAG}-html", exp=28_800)

BASE_URL = "https://ovogoalz.st"

# User Agent for playlists
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/149.0.0.0 Safari/537.36"
)
UA_ENC = quote(USER_AGENT)

OUTPUT_VLC = Path("ovo_vlc.m3u8")
OUTPUT_TIVIMATE = Path("ovo_tivimate.m3u8")


async def process_event(url: str, url_num: int) -> tuple[str | None, str | None]:
    """Process event page to extract stream URL"""
    nones = None, None

    if not (html_data := await network.request(url, log=log)):
        log.warning(f"URL {url_num}) Failed to load url.")
        return nones

    soup = HTMLParser(html_data.content)

    iframe = soup.css_first("iframe")

    if not iframe or not (iframe_src := iframe.attributes.get("src")):
        log.warning(f"URL {url_num}) No iframe element found.")
        return nones

    log.info(f"URL {url_num}) Found iframe: {iframe_src}")

    # Check if the iframe src is already a stream URL (soccerball.st)
    if "soccerball.st" in iframe_src:
        log.info(f"URL {url_num}) Stream URL found directly in iframe")
        return iframe_src, iframe_src

    # If it's a YouTube embed, skip (not our target)
    if "youtube.com" in iframe_src or "youtu.be" in iframe_src:
        log.warning(f"URL {url_num}) YouTube iframe, skipping")
        return nones

    # Try to fetch iframe content to find stream URL
    iframe_data = await network.request(
        iframe_src,
        headers={"Referer": url},
        log=log,
    )

    if iframe_data:
        # Look for stream URL patterns
        patterns = [
            r'(https?://[^\s"\']+soccerball\.st/[^\s"\']+)',
            r'(https?://[^\s"\']+\.php[^\s"\']*)',
            r'(https?://[^\s"\']+\.m3u8[^\s"\']*)',
            r'(https?://[^\s"\']+\.js[^\s"\']*)',
        ]

        for pattern in patterns:
            match = re.search(pattern, iframe_data.text, re.I)
            if match:
                stream_url = match.group(1)
                log.info(f"URL {url_num}) Captured stream: {stream_url}")
                return stream_url, iframe_src

    log.warning(f"URL {url_num}) No stream found")
    return nones


async def refresh_html_cache(now: Time) -> dict[str, dict[str, str | float]]:
    """Extract events from the main page"""
    events = {}

    if not (html_data := await network.request(BASE_URL, log=log)):
        return events

    soup = HTMLParser(html_data.content)

    sport = "World Cup 2026"

    for card in soup.css("a.match-card"):
        href = card.attributes.get("href")
        if not href or href == "#" or len(href) < 5:
            continue

        # Extract team names
        home_elem = card.css_first(".match-team.home .team-full-name")
        away_elem = card.css_first(".match-team.away .team-full-name")

        if not home_elem or not away_elem:
            continue

        home_team = home_elem.text(strip=True)
        away_team = away_elem.text(strip=True)

        event_name = f"{home_team} vs {away_team}"

        # Get match time
        time_elem = card.css_first(".match-time")
        event_time = time_elem.text(strip=True) if time_elem else ""

        # Build event URL
        event_url = urljoin(BASE_URL, href)

        # Parse event time
        event_dt = now
        if event_time:
            try:
                # Extract date like "Jun 11" from "Jun 11 — 21:00 PM"
                date_match = re.search(r'(\w{3}\s+\d+)', event_time)
                if date_match:
                    date_str = f"{date_match.group(1)} {now.year}"
                    event_dt = Time.from_str(date_str, "%b %d %Y", timezone="UTC")
            except:
                event_dt = now

        key = f"[{sport}] {event_name} ({TAG})"

        events[key] = {
            "sport": sport,
            "event": event_name,
            "link": event_url,
            "event_ts": event_dt.timestamp(),
            "timestamp": now.timestamp(),
            "home_team": home_team,
            "away_team": away_team,
        }

    log.info(f"Refreshed HTML cache with {len(events)} events")
    return events


async def get_events(cached_keys: list[str]) -> list[dict[str, str | float]]:
    now = Time.clean(Time.now())

    if not (events := HTML_FILE.load()):
        log.info("Refreshing HTML cache")
        events = await refresh_html_cache(now)
        HTML_FILE.write(events)

    # Return all events (not time-filtered for now)
    return [v for k, v in events.items() if k not in cached_keys]


def generate_vlc_playlist(data: dict[str, dict]) -> int:
    """Generate VLC-compatible playlist"""
    lines = ["#EXTM3U"]
    lines.append(f"# Playlist generated by {TAG} Scraper - {Time.clean(Time.now()).strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("")
    count = 0
    chno = 1

    for name, entry in sorted(data.items()):
        url = entry.get("url")
        if not url:
            continue

        referer = entry.get("base", "https://soccerball.st/rampages/unoair1/")
        tvg_id = entry.get("id", "Live.Event.us")
        tvg_logo = entry.get("logo", "https://i.gyazo.com/4a5e9fa2525808ee4b65002b56d3450e.png")
        group_title = entry.get("sport", "World Cup 2026")
        
        lines.append(f'#EXTINF:-1 tvg-chno="{chno}" tvg-id="{tvg_id}" tvg-logo="{tvg_logo}" group-title="{group_title}",{name}')
        lines.append(f"#EXTVLCOPT:http-referrer={referer}")
        lines.append(f"#EXTVLCOPT:http-origin={referer}")
        lines.append(f"#EXTVLCOPT:http-user-agent={USER_AGENT}")
        lines.append(url)
        lines.append("")
        count += 1
        chno += 1

    with open(OUTPUT_VLC, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    log.info(f"Generated {OUTPUT_VLC} with {count} events")
    return count


def generate_tivimate_playlist(data: dict[str, dict]) -> int:
    """Generate TiviMate-compatible playlist with pipe format"""
    lines = ["#EXTM3U"]
    lines.append(f"# Playlist generated by {TAG} Scraper - {Time.clean(Time.now()).strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("")
    count = 0
    chno = 1

    for name, entry in sorted(data.items()):
        url = entry.get("url")
        if not url:
            continue

        referer = entry.get("base", "https://soccerball.st/rampages/unoair1/")
        tvg_id = entry.get("id", "Live.Event.us")
        tvg_logo = entry.get("logo", "https://i.gyazo.com/4a5e9fa2525808ee4b65002b56d3450e.png")
        group_title = entry.get("sport", "World Cup 2026")
        
        lines.append(f'#EXTINF:-1 tvg-chno="{chno}" tvg-id="{tvg_id}" tvg-logo="{tvg_logo}" group-title="{group_title}",{name}')
        lines.append(f"{url}|referer={referer}|origin={referer}|user-agent={UA_ENC}")
        lines.append("")
        count += 1
        chno += 1

    with open(OUTPUT_TIVIMATE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    log.info(f"Generated {OUTPUT_TIVIMATE} with {count} events")
    return count


async def scrape() -> None:
    cached_urls = CACHE_FILE.load() or {}

    valid_urls = {k: v for k, v in cached_urls.items() if v.get("url")}
    valid_count = cached_count = len(valid_urls)

    urls.update(valid_urls)

    log.info(f"Loaded {cached_count} event(s) from cache")

    log.info(f'Scraping from "{BASE_URL}"')

    events = await get_events(list(cached_urls.keys()))
    
    if events:
        log.info(f"Processing {len(events)} new URL(s)")

        for i, ev in enumerate(events, start=1):
            log.info(f"Processing {i}/{len(events)}: {ev['event']}")
            
            handler = partial(
                process_event,
                url=(link := ev["link"]),
                url_num=i,
            )

            url, iframe = await network.safe_process(
                handler,
                url_num=i,
                semaphore=network.HTTP_S,
                log=log,
            )

            if not url:
                log.warning(f"Event {i}) No stream found for: {ev['event']}")
                continue

            sport = ev["sport"]
            event = ev["event"]

            key = f"[{sport}] {event} ({TAG})"

            tvg_id, logo = leagues.get_tvg_info(sport, event)

            # Use the iframe as referer if available, otherwise use default
            referer = iframe if iframe else "https://soccerball.st/rampages/unoair1/"

            entry = {
                "url": url,
                "logo": logo,
                "base": referer,
                "timestamp": ev["event_ts"],
                "id": tvg_id or "Live.Event.us",
                "link": link,
                "sport": sport,
            }

            cached_urls[key] = entry
            urls[key] = entry
            valid_count += 1
            log.info(f"Event {i}) ✓ Captured: {event}")

        log.info(f"Collected and cached {valid_count - cached_count} new event(s)")

    else:
        log.info("No new events found")

    # Save cache
    CACHE_FILE.write(cached_urls)
    
    # Generate playlists only with valid URLs
    valid_events = {k: v for k, v in urls.items() if v.get("url")}
    
    if valid_events:
        vlc_count = generate_vlc_playlist(valid_events)
        tivimate_count = generate_tivimate_playlist(valid_events)
        log.info(f"Final playlist size: {len(valid_events)} events")
        log.info(f"Total written: {vlc_count + tivimate_count}")
    else:
        log.warning("No valid events to generate playlists")
        with open(OUTPUT_VLC, "w") as f:
            f.write("#EXTM3U\n# No events available\n")
        with open(OUTPUT_TIVIMATE, "w") as f:
            f.write("#EXTM3U\n# No events available\n")


async def main():
    """Main entry point"""
    log.info(f"Starting {TAG} updater")
    await scrape()
    log.info(f"{TAG} updater completed")


def run():
    """Run the updater"""
    import asyncio
    asyncio.run(main())


if __name__ == "__main__":
    run()
