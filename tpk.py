import json
import re
from functools import partial
from urllib.parse import urljoin, urlparse, quote
from pathlib import Path

from selectolax.parser import HTMLParser

from utils import Cache, Time, get_logger, leagues, network

log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}

TAG = "TPK"
CACHE_FILE = Cache(TAG, exp=28_800)

BASE_URL = "https://live.totalsportek.fyi"

# User Agent for playlists
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:146.0) "
    "Gecko/20100101 Firefox/146.0"
)
UA_ENC = quote(USER_AGENT)

OUTPUT_VLC = Path("tpk_vlc.m3u8")
OUTPUT_TIVIMATE = Path("tpk_tivimate.m3u8")


def fix_txt(s: str) -> str:
    s = " ".join(s.split())
    return s.upper() if s.islower() else s


def extract_sport_from_text(text: str) -> str:
    """Extract sport category from text"""
    sport_keywords = {
        'F1': 'F1',
        'NASCAR': 'NASCAR',
        'WWE': 'WWE',
        'Tennis': 'Tennis',
        'Golf': 'Golf',
        'NBA': 'NBA',
        'NFL': 'NFL',
        'MLB': 'MLB',
        'NHL': 'NHL',
        'UFC': 'UFC',
        'Boxing': 'BOXING',
        'Soccer': 'SOCCER',
        'Football': 'FOOTBALL',
        'Racing': 'RACING',
        'Rally': 'RALLY',
    }
    
    for key, value in sport_keywords.items():
        if key.lower() in text.lower():
            return value
    return "Live Event"


async def process_ts1(ifr_src: str, url_num: int) -> str | None:
    """Process iframe with hex encoded M3U8"""
    if not (ifr_src_data := await network.request(ifr_src, log=log)):
        log.info(f"URL {url_num}) Failed to load iframe source.")
        return None

    # Try multiple patterns for hex encoded data
    patterns = [
        r'(var|const)\s+(\w+)\s*=\s*"([^"]*)"',
        r'(var|const)\s+(\w+)\s*=\s*\'([^\']*)\'',
        r'(\w+)\s*=\s*"([^"]*)"',
    ]
    
    for pattern in patterns:
        if match := re.search(pattern, ifr_src_data.text, re.I):
            if len(match.group(2) if len(match.groups()) > 2 else match.group(1)) < 20:
                encoded = match.group(3) if len(match.groups()) > 2 else match.group(2)
            else:
                encoded = match.group(2) if len(match.groups()) > 2 else match.group(1)
            
            try:
                decoded = bytes.fromhex(encoded).decode("utf-8")
                if '.m3u8' in decoded or 'http' in decoded:
                    log.info(f"URL {url_num}) Captured M3U8 from hex")
                    return decoded
            except:
                continue
    
    return None


async def process_ts2(ifr_src: str, url_num: int) -> str | None:
    """Process iframe with direct M3U8 URL"""
    if not (ifr_src_data := await network.request(ifr_src, log=log)):
        log.info(f"URL {url_num}) Failed to load iframe source.")
        return None
    
    # Look for direct M3U8 URLs
    patterns = [
        r'(https?://[^\s"\']+\.m3u8[^\s"\']*)',
        r'(https?://[^\s"\']+\.m3u8(?:\?[^\s"\']*)?)',
        r'(https?://[^\s"\']+stream[^\s"\']*\.m3u8[^\s"\']*)',
        r'(https?://[^\s"\']+playlist[^\s"\']*\.m3u8[^\s"\']*)',
    ]
    
    for pattern in patterns:
        if match := re.search(pattern, ifr_src_data.text, re.I):
            log.info(f"URL {url_num}) Captured M3U8 direct")
            return match.group(1)
    
    return None


async def process_ts3(ifr_src: str, url_num: int) -> str | None:
    """Process iframe with nested iframe structure"""
    if not (ifr_1_src_data := await network.request(ifr_src, log=log)):
        log.warning(f"URL {url_num}) Failed to load iframe source. (IFR1)")
        return None

    soup_2 = HTMLParser(ifr_1_src_data.content)

    ifr_2 = soup_2.css_first("iframe")

    if not ifr_2 or not (ifr_2_src := ifr_2.attributes.get("src")):
        log.warning(f"URL {url_num}) No iframe element found. (IFR2)")
        return None

    if not (
        ifr_2_src_data := await network.request(
            ifr_2_src,
            headers={"Referer": ifr_src},
            log=log,
        )
    ):
        log.warning(f"URL {url_num}) Failed to load iframe source. (IFR2)")
        return None

    # Look for currentStreamUrl pattern
    patterns = [
        r'currentStreamUrl\s+=\s+"([^"]*)"',
        r'currentStreamUrl\s*=\s*\'([^\']*)\'',
        r'source\s*:\s*"([^"]*)"',
        r'file\s*:\s*"([^"]*)"',
        r'url\s*:\s*"([^"]*)"',
    ]
    
    for pattern in patterns:
        if match := re.search(pattern, ifr_2_src_data.text, re.I):
            try:
                url = json.loads(f'"{match.group(1)}"')
                if '.m3u8' in url or 'http' in url:
                    log.info(f"URL {url_num}) Captured M3U8 from nested iframe")
                    return url
            except:
                if '.m3u8' in match.group(1):
                    log.info(f"URL {url_num}) Captured M3U8 from nested iframe")
                    return match.group(1)
    
    return None


async def process_ts4(page_url: str, url_num: int) -> str | None:
    """Process page with embedded player script"""
    if not (event_data := await network.request(page_url, log=log)):
        log.warning(f"URL {url_num}) Failed to load url.")
        return None
    
    # Look for player scripts and embedded data
    patterns = [
        r'player\.setup\s*\(\s*\{\s*file\s*:\s*"([^"]+)"',
        r'sources\s*:\s*\[\s*\{\s*file\s*:\s*"([^"]+)"',
        r'video\s*:\s*"([^"]+\.m3u8)"',
        r'stream\s*:\s*"([^"]+\.m3u8)"',
    ]
    
    for pattern in patterns:
        if match := re.search(pattern, event_data.text, re.I):
            log.info(f"URL {url_num}) Captured M3U8 from player setup")
            return match.group(1)
    
    return None


async def process_event(url: str, url_num: int, tag: str) -> str | None:
    """Process event page to extract stream URL using multiple methods"""
    if not (event_data := await network.request(url, log=log)):
        log.warning(f"URL {url_num}) Failed to load url.")
        return None

    soup = HTMLParser(event_data.content)

    # Try to find iframe
    iframe = soup.css_first("iframe")
    
    if iframe and (iframe_src := iframe.attributes.get("src")):
        # Try different extraction methods for iframe
        for method in [process_ts1, process_ts2, process_ts3]:
            result = await method(iframe_src, url_num)
            if result:
                return result
    
    # If no iframe or iframe methods failed, try direct page extraction
    result = await process_ts4(url, url_num)
    if result:
        return result
    
    # Try to find any M3U8 URL in the page
    m3u8_pattern = r'(https?://[^\s"\']+\.m3u8[^\s"\']*)'
    if match := re.search(m3u8_pattern, event_data.text, re.I):
        log.info(f"URL {url_num}) Captured M3U8 from page")
        return match.group(1)
    
    log.warning(f"URL {url_num}) No valid stream source found.")
    return None


async def get_events(cached_keys: list[str]) -> list[dict[str, str]]:
    events = []

    if not (html_data := await network.request(BASE_URL, log=log)):
        return events

    soup = HTMLParser(html_data.content)
    
    # Find all event links - look for elements that contain match information
    # The page structure: each event is in a div or container with match info
    
    # Method 1: Look for links that might be event pages
    for link in soup.css('a[href*="/event/"], a[href*="/match/"], a[href*="/stream/"]'):
        href = link.attributes.get("href")
        if not href:
            continue
        
        # Get the text content of the link and its parent
        link_text = link.text(strip=True)
        if not link_text or len(link_text) < 3:
            continue
        
        # Skip navigation links
        if any(skip in link_text.lower() for skip in ['home', 'login', 'register', 'contact', 'about']):
            continue
        
        # Build full URL
        event_url = urljoin(BASE_URL, href)
        
        # Try to extract team names from the text
        teams = link_text.split(' vs ')
        if len(teams) == 2:
            event_name = fix_txt(f"{teams[0]} vs {teams[1]}")
        else:
            event_name = fix_txt(link_text)
        
        # Determine sport from context or link text
        sport = extract_sport_from_text(link_text)
        
        # Check parent elements for sport info
        parent = link.parent
        for _ in range(3):  # Check up to 3 levels up
            if parent:
                parent_text = parent.text(strip=True)
                sport_from_parent = extract_sport_from_text(parent_text)
                if sport_from_parent != "Live Event":
                    sport = sport_from_parent
                    break
                parent = parent.parent
        
        key = f"[{sport}] {event_name} ({TAG})"
        if key in cached_keys:
            continue
        
        events.append({
            "sport": sport,
            "event": event_name,
            "tag": TAG,
            "link": event_url,
        })
    
    # Method 2: Look for text patterns that indicate live events
    # Find elements containing "Match Started" or "Match Ended"
    for node in soup.css('*'):
        if not node.text():
            continue
        
        node_text = node.text(strip=True)
        if 'Match Started' in node_text or 'Match Ended' in node_text or 'Live' in node_text:
            # Look for nearby links
            parent_container = node.parent
            if parent_container:
                for link in parent_container.css('a'):
                    href = link.attributes.get("href")
                    if href and ('/event/' in href or '/match/' in href or '/stream/' in href):
                        event_url = urljoin(BASE_URL, href)
                        
                        # Get event name from sibling or parent text
                        event_name = node_text.replace('Match Started', '').replace('Match Ended', '').replace('Live', '').strip()
                        if not event_name or len(event_name) < 3:
                            event_name = link.text(strip=True)
                        
                        if event_name:
                            event_name = fix_txt(event_name)
                            sport = extract_sport_from_text(event_name)
                            
                            key = f"[{sport}] {event_name} ({TAG})"
                            if key not in cached_keys and key not in [e['event'] for e in events]:
                                events.append({
                                    "sport": sport,
                                    "event": event_name,
                                    "tag": TAG,
                                    "link": event_url,
                                })
    
    # Remove duplicates by link
    seen_links = set()
    unique_events = []
    for event in events:
        if event["link"] not in seen_links:
            seen_links.add(event["link"])
            unique_events.append(event)
    
    log.info(f"Found {len(unique_events)} events")
    return unique_events


def generate_vlc_playlist(data: dict[str, dict]) -> int:
    """Generate VLC-compatible playlist"""
    lines = ["#EXTM3U"]
    lines.append(f"# Playlist generated by {TAG} Scraper - {Time.clean(Time.now()).strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("")
    count = 0

    for key, entry in sorted(data.items()):
        url = entry.get("url")
        if not url:
            continue

        # Build referer from original link
        referer = entry.get("link", BASE_URL)
        
        tvg_id = entry.get("id", "Live.Event.us")
        tvg_logo = entry.get("logo", "https://i.gyazo.com/4a5e9fa2525808ee4b65002b56d3450e.png")
        group_title = entry.get("sport", "Live Events")
        
        lines.append(f'#EXTINF:-1 tvg-id="{tvg_id}" tvg-logo="{tvg_logo}" group-title="{group_title}",{key}')
        lines.append(f"#EXTVLCOPT:http-referrer={referer}")
        lines.append(f"#EXTVLCOPT:http-origin={referer}")
        lines.append(f"#EXTVLCOPT:http-user-agent={USER_AGENT}")
        lines.append(url)
        lines.append("")
        count += 1

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

    for key, entry in sorted(data.items()):
        url = entry.get("url")
        if not url:
            continue

        # Build referer from original link
        referer = entry.get("link", BASE_URL)
        
        tvg_id = entry.get("id", "Live.Event.us")
        tvg_logo = entry.get("logo", "https://i.gyazo.com/4a5e9fa2525808ee4b65002b56d3450e.png")
        group_title = entry.get("sport", "Live Events")
        
        lines.append(f'#EXTINF:-1 tvg-id="{tvg_id}" tvg-logo="{tvg_logo}" group-title="{group_title}",{key}')
        lines.append(f"{url}|referer={referer}|origin={referer}|user-agent={UA_ENC}")
        lines.append("")
        count += 1

    with open(OUTPUT_TIVIMATE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    log.info(f"Generated {OUTPUT_TIVIMATE} with {count} events")
    return count


async def scrape() -> None:
    """Main scrape function"""
    cached_urls = CACHE_FILE.load() or {}

    valid_urls = {k: v for k, v in cached_urls.items() if v.get("url")}
    valid_count = cached_count = len(valid_urls)

    urls.update(valid_urls)

    log.info(f"Loaded {cached_count} event(s) from cache")

    log.info(f'Scraping from "{BASE_URL}"')

    events = await get_events(cached_urls.keys())
    
    if events:
        log.info(f"Processing {len(events)} new URL(s)")

        now = Time.clean(Time.now())

        for i, ev in enumerate(events, start=1):
            handler = partial(
                process_event,
                url=(link := ev["link"]),
                url_num=i,
                tag=(tag := ev["tag"]),
            )

            stream_url = await network.safe_process(
                handler,
                url_num=i,
                semaphore=network.HTTP_S,
                log=log,
            )

            if not stream_url:
                log.warning(f"Event {i}) Failed to extract URL: {ev['event']}")
                continue

            sport, event = ev["sport"], ev["event"]
            key = f"[{sport}] {event} ({tag})"

            tvg_id, logo = leagues.get_tvg_info(sport, event)

            entry = {
                "url": stream_url,
                "logo": logo,
                "base": link,
                "timestamp": now.timestamp(),
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
        # Create empty playlists to avoid errors
        with open(OUTPUT_VLC, "w") as f:
            f.write("#EXTM3U\n# No events available\n")
        with open(OUTPUT_TIVIMATE, "w") as f:
            f.write("#EXTM3U\n# No events available\n")


async def main():
    """Main entry point"""
    log.info(f"Starting {TAG} scraper")
    await scrape()
    log.info(f"{TAG} scraper completed")


def run():
    """Run the scraper"""
    import asyncio
    asyncio.run(main())


if __name__ == "__main__":
    run()
