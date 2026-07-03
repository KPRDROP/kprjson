import re
import json
from functools import partial
from urllib.parse import urljoin, quote
from pathlib import Path

from selectolax.parser import HTMLParser

from utils import Cache, Event, Time, get_logger, leagues, network

log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}

TAG = "OVO"

CACHE_FILE = Cache(TAG, exp=10_800)

HTML_FILE = Cache(f"{TAG}-html", exp=28_800)

BASE_URL = "https://ovo-goal.st"

# User Agent for playlists
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/149.0.0.0 Safari/537.36"
)
UA_ENC = quote(USER_AGENT)

# Referer and origin
REFERER = "https://ziangel.com/"
ORIGIN = "https://ziangel.com"

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

    # Skip YouTube embeds
    if "youtube.com" in iframe_src or "youtu.be" in iframe_src:
        log.warning(f"URL {url_num}) YouTube iframe, skipping")
        return nones

    # Fetch the iframe content
    log.info(f"URL {url_num}) Fetching iframe content...")
    iframe_data = await network.request(
        iframe_src,
        headers={"Referer": url},
        log=log,
    )
    
    if not iframe_data:
        log.warning(f"URL {url_num}) Failed to load iframe source.")
        return nones

    content = iframe_data.text
    
    # Debug: Save iframe content for inspection
    log.debug(f"URL {url_num}) Iframe content length: {len(content)}")
    
    # Look for the actual stream URL in the iframe
    
    # Pattern 1: Look for m3u8 URL - Updated with more patterns
    m3u8_patterns = [
        r'(https?://[^\s"\']+\.m3u8[^\s"\']*)',
        r'(https?://[^\s"\']+\.m3u8\?[^\s"\']*)',
        r'(https?://[^\s"\']+/hls/[^\s"\']+\.m3u8[^\s"\']*)',
        r'(https?://[^\s"\']+azplay[^\s"\']+\.me/hls/[^\s"\']+\.m3u8[^\s"\']*)',
        r'(https?://[^\s"\']+azplay[^\s"\']+\.com/hls/[^\s"\']+\.m3u8[^\s"\']*)',
        r'(https?://[^\s"\']+stream[^\s"\']+\.m3u8[^\s"\']*)',
        r'(https?://[^\s"\']+[a-z0-9]+\.me/hls/[^\s"\']+\.m3u8[^\s"\']*)',
        r'(https?://[^\s"\']+[a-z0-9]+\.com/hls/[^\s"\']+\.m3u8[^\s"\']*)',
        r'(https?://[^\s"\']+[a-z0-9]+\.net/hls/[^\s"\']+\.m3u8[^\s"\']*)',
        r'(https?://[^\s"\']+ziangel\.[a-z]+/hls/[^\s"\']+\.m3u8[^\s"\']*)',
        r'(https?://[^\s"\']+[a-z0-9]+\.xyz/hls/[^\s"\']+\.m3u8[^\s"\']*)',
    ]
    
    for pattern in m3u8_patterns:
        matches = re.findall(pattern, content, re.IGNORECASE)
        for match in matches:
            if 'cdn.jsdelivr.net' not in match and 'clappr' not in match and 'jquery' not in match:
                log.info(f"URL {url_num}) Captured M3U8 stream: {match[:100]}...")
                return match, iframe_src
    
    # Pattern 2: Look for source URL in player configuration
    clappr_patterns = [
        r'source\s*:\s*["\']([^"\']+\.m3u8[^"\']*)["\']',
        r'source\s*:\s*["\']([^"\']+)["\']',
        r'file\s*:\s*["\']([^"\']+\.m3u8[^"\']*)["\']',
        r'url\s*:\s*["\']([^"\']+\.m3u8[^"\']*)["\']',
        r'playlist\s*:\s*["\']([^"\']+\.m3u8[^"\']*)["\']',
        r'"source"\s*:\s*"([^"]+\.m3u8[^"]*)"',
        r'"file"\s*:\s*"([^"]+\.m3u8[^"]*)"',
        r'"url"\s*:\s*"([^"]+\.m3u8[^"]*)"',
        r'currentStreamUrl\s*=\s*["\']([^"\']+\.m3u8[^"\']*)["\']',
        r'streamUrl\s*=\s*["\']([^"\']+\.m3u8[^"\']*)["\']',
        r'video\s*:\s*["\']([^"\']+\.m3u8[^"\']*)["\']',
        r'playlist\s*:\s*["\']([^"\']+\.m3u8[^"\']*)["\']',
    ]
    
    for pattern in clappr_patterns:
        match = re.search(pattern, content, re.IGNORECASE)
        if match:
            stream_url = match.group(1)
            if 'cdn.jsdelivr.net' not in stream_url and 'clappr' not in stream_url:
                log.info(f"URL {url_num}) Captured stream from config: {stream_url[:100]}...")
                return stream_url, iframe_src
    
    # Pattern 3: Look for any URL that might be a stream
    url_pattern = r'(https?://[^\s"\']+[^\s"\']+\.m3u8[^\s"\']*)'
    matches = re.findall(url_pattern, content, re.IGNORECASE)
    for match in matches:
        if 'cdn.jsdelivr.net' not in match and 'clappr' not in match:
            log.info(f"URL {url_num}) Found M3U8 URL: {match[:100]}...")
            return match, iframe_src
    
    # Pattern 4: Look for stream URL in script variables
    var_patterns = [
        r'(?:var|const|let)\s+(?:url|src|source|stream|file|video|hls|m3u8)\s*=\s*["\']([^"\']+\.m3u8[^"\']*)["\']',
        r'(?:var|const|let)\s+(?:url|src|source|stream|file|video|hls|m3u8)\s*=\s*["\']([^"\']+)["\']',
        r'(?:url|src|source|stream|file|video|hls|m3u8)\s*[:=]\s*["\']([^"\']+\.m3u8[^"\']*)["\']',
        r'(?:url|src|source|stream|file|video|hls|m3u8)\s*[:=]\s*["\']([^"\']+)["\']',
    ]
    
    for pattern in var_patterns:
        matches = re.findall(pattern, content, re.IGNORECASE)
        for match in matches:
            if '.m3u8' in match and 'cdn.jsdelivr.net' not in match and 'clappr' not in match:
                log.info(f"URL {url_num}) Captured stream from variable: {match[:100]}...")
                return match, iframe_src
    
    # Pattern 5: Look for base64 encoded URLs
    b64_patterns = [
        r'atob\(["\']([^"\']+)["\']\)',
        r'decodeURIComponent\(["\']([^"\']+)["\']\)',
        r'base64\.decode\(["\']([^"\']+)["\']\)',
        r'window\.atob\(["\']([^"\']+)["\']\)',
    ]
    
    for pattern in b64_patterns:
        matches = re.findall(pattern, content)
        for match in matches:
            try:
                import base64
                padding = 4 - (len(match) % 4)
                if padding != 4:
                    match += '=' * padding
                decoded = base64.b64decode(match).decode('utf-8')
                if '.m3u8' in decoded:
                    log.info(f"URL {url_num}) Captured stream from base64: {decoded[:100]}...")
                    return decoded, iframe_src
            except:
                pass
    
    # Pattern 6: Look for the stream URL in the iframe's parent page
    if 'window.location' in content:
        redirect_match = re.search(r'window\.location\s*=\s*["\']([^"\']+)["\']', content, re.I)
        if redirect_match:
            redirect_url = redirect_match.group(1)
            log.info(f"URL {url_num}) Found redirect: {redirect_url}")
            
            redirect_data = await network.request(redirect_url, headers={"Referer": iframe_src}, log=log)
            if redirect_data:
                for pattern in m3u8_patterns:
                    match = re.search(pattern, redirect_data.text, re.I)
                    if match:
                        stream_url = match.group(1)
                        if 'cdn.jsdelivr.net' not in stream_url:
                            log.info(f"URL {url_num}) Captured stream from redirect: {stream_url[:100]}...")
                            return stream_url, iframe_src
    
    # Pattern 7: Look for stream URL in the HTML content
    html_patterns = [
        r'data-stream=["\']([^"\']+\.m3u8[^"\']*)["\']',
        r'data-src=["\']([^"\']+\.m3u8[^"\']*)["\']',
        r'data-video=["\']([^"\']+\.m3u8[^"\']*)["\']',
        r'data-hls=["\']([^"\']+\.m3u8[^"\']*)["\']',
        r'data-source=["\']([^"\']+\.m3u8[^"\']*)["\']',
    ]
    
    for pattern in html_patterns:
        match = re.search(pattern, content, re.IGNORECASE)
        if match:
            stream_url = match.group(1)
            log.info(f"URL {url_num}) Captured stream from data attribute: {stream_url[:100]}...")
            return stream_url, iframe_src
    
    # Pattern 8: Look for the stream URL in script tags
    script_pattern = r'<script[^>]*>([\s\S]*?)</script>'
    scripts = re.findall(script_pattern, content, re.IGNORECASE)
    for script in scripts:
        if '.m3u8' in script:
            url_pattern = r'(https?://[^\s"\']+\.m3u8[^\s"\']*)'
            matches = re.findall(url_pattern, script, re.IGNORECASE)
            for match in matches:
                if 'cdn.jsdelivr.net' not in match and 'clappr' not in match:
                    log.info(f"URL {url_num}) Found M3U8 in script: {match[:100]}...")
                    return match, iframe_src
    
    # Pattern 9: Look for the stream URL in the page content using a more general approach
    general_pattern = r'(https?://[^\s"\']+[^\s"\']*\.m3u8[^\s"\']*)'
    matches = re.findall(general_pattern, content, re.IGNORECASE)
    for match in matches:
        if 'cdn.jsdelivr.net' not in match and 'clappr' not in match:
            log.info(f"URL {url_num}) Found M3U8 in general search: {match[:100]}...")
            return match, iframe_src
    
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
        
        if not href.startswith("/update2026/"):
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

        # Parse event time - ignore time filtering, always get events
        event_dt = now

        key = f"[{sport}] {event_name} ({TAG})"

        events[key] = {
            "sport": sport,
            "event": event_name,
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

    # Return all events regardless of time
    return [v for k, v in events.items() if k not in cached_keys]


def generate_vlc_playlist(data: dict[str, dict]) -> int:
    """Generate VLC-compatible playlist"""
    lines = ["#EXTM3U"]
    lines.append(f"# Playlist generated by {TAG} Updater - {Time.clean(Time.now()).strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("")
    count = 0
    chno = 1

    for name, entry in sorted(data.items()):
        url = entry.get("url")
        if not url:
            continue

        referer = entry.get("base", REFERER)
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
    lines.append(f"# Playlist generated by {TAG} Updater - {Time.clean(Time.now()).strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("")
    count = 0
    chno = 1

    for name, entry in sorted(data.items()):
        url = entry.get("url")
        if not url:
            continue

        referer = entry.get("base", REFERER)
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
                timeout_return=(None, None),
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

            referer = iframe if iframe else REFERER

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
