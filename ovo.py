import re
from functools import partial
from urllib.parse import urljoin, quote
from pathlib import Path

from selectolax.parser import HTMLParser

from utils import Cache, Time, get_logger, leagues, network

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

    # Look for iframe in the page
    iframe = soup.css_first("iframe")
    
    if iframe and (iframe_src := iframe.attributes.get("src")):
        log.info(f"URL {url_num}) Found iframe: {iframe_src}")
        
        iframe_data = await network.request(
            iframe_src,
            headers={"Referer": url},
            log=log,
        )
        
        if iframe_data:
            # Look for stream URL patterns
            patterns = [
                r'(https?://[^\s"\']+\.php[^\s"\']*)',
                r'(https?://[^\s"\']+\.m3u8[^\s"\']*)',
                r'(https?://[^\s"\']+\.js[^\s"\']*)',
                r'(https?://[^\s"\']+soccerball\.st/[^\s"\']+)',
            ]
            
            for pattern in patterns:
                match = re.search(pattern, iframe_data.text, re.I)
                if match:
                    stream_url = match.group(1)
                    log.info(f"URL {url_num}) Captured stream: {stream_url}")
                    return stream_url, iframe_src
    
    # Look for direct stream links in scripts
    scripts = soup.css("script")
    for script in scripts:
        script_text = script.text()
        if script_text:
            patterns = [
                r'(https?://[^\s"\']+\.php[^\s"\']*)',
                r'(https?://[^\s"\']+soccerball\.st/[^\s"\']+)',
                r'url\s*:\s*"([^"]+)"',
                r'src\s*:\s*"([^"]+)"',
            ]
            for pattern in patterns:
                match = re.search(pattern, script_text, re.I)
                if match:
                    stream_url = match.group(1)
                    log.info(f"URL {url_num}) Captured stream from script: {stream_url}")
                    return stream_url, None
    
    log.warning(f"URL {url_num}) No stream found")
    return nones


async def refresh_html_cache(now: Time) -> dict[str, dict[str, str | float]]:
    """Extract events from the main page"""
    events = {}

    if not (html_data := await network.request(BASE_URL, log=log)):
        return events

    soup = HTMLParser(html_data.content)
    
    # Find all match cards
    for card in soup.css("a.match-card"):
        href = card.attributes.get("href")
        if not href or href == "#" or len(href) < 5:
            continue
        
        # Skip if not a valid event link
        if not href.startswith("/update2026/"):
            continue
        
        # Extract team names
        home_team_elem = card.css_first(".match-team.home .team-full-name")
        away_team_elem = card.css_first(".match-team.away .team-full-name")
        
        if not home_team_elem or not away_team_elem:
            continue
        
        home_team = home_team_elem.text(strip=True)
        away_team = away_team_elem.text(strip=True)
        
        # Get match time
        time_elem = card.css_first(".match-time")
        match_time = time_elem.text(strip=True) if time_elem else ""
        
        # Get group info
        group_elem = card.css_first(".match-group-tag")
        group = group_elem.text(strip=True) if group_elem else "World Cup"
        
        # Extract date from match time
        match_date = None
        if match_time:
            # Parse date like "Jun 15 — 16:00 PM"
            date_match = re.search(r'(\w{3}\s+\d+)', match_time)
            if date_match:
                match_date = date_match.group(1)
        
        event_name = f"{home_team} vs {away_team}"
        sport = "World Cup 2026"
        
        # Determine if match is finished or upcoming
        badge_elem = card.css_first(".match-badge")
        if badge_elem:
            badge_text = badge_elem.text(strip=True)
            if badge_text == "Finished":
                # Skip finished matches or keep them
                pass
        
        # Build event URL
        event_url = urljoin(BASE_URL, href)
        
        # Parse event time
        event_dt = now
        if match_date:
            try:
                # Try to parse the date
                from datetime import datetime
                year = now.year
                date_str = f"{match_date} {year}"
                event_dt = Time.from_str(date_str, "%b %d %Y", timezone="UTC")
            except:
                event_dt = now
        
        key = f"[{sport}] {event_name} ({TAG})"
        
        events[key] = {
            "sport": sport,
            "event": event_name,
            "group": group,
            "home_team": home_team,
            "away_team": away_team,
            "match_time": match_time,
            "link": event_url,
            "event_ts": event_dt.timestamp(),
            "timestamp": now.timestamp(),
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

        referer = entry.get("base", BASE_URL)
        tvg_id = entry.get("id", "Live.Event.us")
        tvg_logo = entry.get("logo", "https://i.gyazo.com/4a5e9fa2525808ee4b65002b56d3450e.png")
        group_title = entry.get("sport", "Live Events")
        
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

        referer = entry.get("base", BASE_URL)
        tvg_id = entry.get("id", "Live.Event.us")
        tvg_logo = entry.get("logo", "https://i.gyazo.com/4a5e9fa2525808ee4b65002b56d3450e.png")
        group_title = entry.get("sport", "Live Events")
        
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

        now = Time.clean(Time.now())

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

            sport = ev["sport"]
            event = ev["event"]

            key = f"[{sport}] {event} ({TAG})"

            tvg_id, logo = leagues.get_tvg_info(sport, event)

            entry = {
                "url": url,
                "logo": logo,
                "base": iframe or BASE_URL,
                "timestamp": now.timestamp(),
                "id": tvg_id or "Live.Event.us",
                "link": link,
                "sport": sport,
            }

            cached_urls[key] = entry

            if url:
                valid_count += 1
                urls[key] = entry
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
        # Create empty playlists
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
