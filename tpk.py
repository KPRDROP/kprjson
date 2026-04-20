import json
import re
import asyncio
from urllib.parse import urljoin, urlparse, quote
from pathlib import Path

from selectolax.parser import HTMLParser

from utils import Cache, Time, get_logger, leagues, network

log = get_logger(__name__)

TAG = "TPK"
CACHE_FILE = Cache(TAG, exp=28_800)

BASE_URL = "https://live.totalsportek.fyi"

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36"

OUTPUT_VLC = Path("tpk_vlc.m3u8")
OUTPUT_TIVIMATE = Path("tpk_tivimate.m3u8")


def fix_txt(s: str) -> str:
    s = " ".join(s.split())
    return s.upper() if s.islower() else s


async def extract_stream_from_page(url: str, url_num: int) -> str | None:
    """Extract stream URL from event page using direct HTTP requests"""
    try:
        log.info(f"URL {url_num}) Fetching {url[:80]}...")
        
        # Fetch the event page
        event_data = await network.request(url, log=log)
        if not event_data:
            log.warning(f"URL {url_num}) Failed to load page")
            return None
        
        content = event_data.text
        
        # Look for iframe with stream
        iframe_pattern = r'<iframe[^>]+src=["\']([^"\']+\.php[^"\']*|/player/[^"\']*|https?://[^"\']+player[^"\']*)[^>]*>'
        iframes = re.findall(iframe_pattern, content, re.I)
        
        for iframe_src in iframes:
            if not iframe_src.startswith('http'):
                iframe_src = urljoin(url, iframe_src)
            
            log.info(f"URL {url_num}) Found iframe, fetching...")
            
            # Fetch iframe content
            iframe_data = await network.request(iframe_src, headers={"Referer": url}, log=log)
            if iframe_data:
                # Look for cloudfront URLs
                cloudfront_pattern = r'(https?://[^\s"\']+cloudfront[^\s"\']+\.woff2[^\s"\']*)'
                match = re.search(cloudfront_pattern, iframe_data.text, re.I)
                if match:
                    stream_url = match.group(1)
                    log.info(f"URL {url_num}) Found stream in iframe: {stream_url[:80]}...")
                    return stream_url
                
                # Look for m3u8 URLs
                m3u8_pattern = r'(https?://[^\s"\']+\.m3u8[^\s"\']*)'
                match = re.search(m3u8_pattern, iframe_data.text, re.I)
                if match:
                    stream_url = match.group(1)
                    log.info(f"URL {url_num}) Found m3u8 in iframe: {stream_url[:80]}...")
                    return stream_url
        
        # Look for direct cloudfront URLs in page
        cloudfront_pattern = r'(https?://[^\s"\']+cloudfront[^\s"\']+\.woff2[^\s"\']*)'
        match = re.search(cloudfront_pattern, content, re.I)
        if match:
            stream_url = match.group(1)
            log.info(f"URL {url_num}) Found cloudfront URL: {stream_url[:80]}...")
            return stream_url
        
        # Look for m3u8 URLs in page
        m3u8_pattern = r'(https?://[^\s"\']+\.m3u8[^\s"\']*)'
        match = re.search(m3u8_pattern, content, re.I)
        if match:
            stream_url = match.group(1)
            log.info(f"URL {url_num}) Found m3u8 URL: {stream_url[:80]}...")
            return stream_url
        
        # Look for player links to follow
        player_pattern = r'href=["\'](https?://[^"\']+crazyvidup[^"\']+)["\']'
        player_links = re.findall(player_pattern, content, re.I)
        
        for player_url in player_links:
            log.info(f"URL {url_num}) Following player link: {player_url[:80]}...")
            player_data = await network.request(player_url, headers={"Referer": url}, log=log)
            if player_data:
                # Look for stream in player page
                match = re.search(cloudfront_pattern, player_data.text, re.I)
                if match:
                    stream_url = match.group(1)
                    log.info(f"URL {url_num}) Found stream in player: {stream_url[:80]}...")
                    return stream_url
                
                match = re.search(m3u8_pattern, player_data.text, re.I)
                if match:
                    stream_url = match.group(1)
                    log.info(f"URL {url_num}) Found m3u8 in player: {stream_url[:80]}...")
                    return stream_url
        
        log.warning(f"URL {url_num}) No stream found in page")
        return None
        
    except Exception as e:
        log.error(f"URL {url_num}) Error: {e}")
        return None


async def process_event(url: str, url_num: int) -> str | None:
    """Process event page to extract stream URL"""
    return await extract_stream_from_page(url, url_num)


def load_events_from_cache() -> list[dict[str, str]]:
    """Load events from the JSON cache file"""
    events = []
    
    # Load the cache file
    cached_urls = CACHE_FILE.load() or {}
    
    for key, entry in cached_urls.items():
        # Skip if already has a stream URL
        if entry.get("url") and entry["url"] not in [None, "null", ""]:
            log.debug(f"Skipping {key} - already has stream")
            continue
        
        # Extract event name from key format: "[Live Event] WWE vs Monday Night RAW (TPK)"
        event_name = key.replace(f" ({TAG})", "").replace("[Live Event] ", "")
        
        # Get the event link
        link = entry.get("link")
        if not link:
            continue
        
        # Determine sport from event name
        sport = "Live Event"
        sport_keywords = {
            'F1': 'F1', 'NASCAR': 'NASCAR', 'WWE': 'WWE',
            'Tennis': 'Tennis', 'Golf': 'Golf', 'NBA': 'NBA',
            'MLB': 'MLB', 'NHL': 'NHL', 'Boxing': 'BOXING',
        }
        for key_word, sport_name in sport_keywords.items():
            if key_word.lower() in event_name.lower():
                sport = sport_name
                break
        
        # Get logo and ID
        logo = entry.get("logo", "https://i.gyazo.com/4a5e9fa2525808ee4b65002b56d3450e.png")
        tvg_id = entry.get("id", "Live.Event.us")
        
        events.append({
            "sport": sport,
            "event": event_name,
            "tag": TAG,
            "link": link,
            "logo": logo,
            "id": tvg_id,
            "key": key,
        })
    
    log.info(f"Loaded {len(events)} events from cache (without streams)")
    return events


def generate_vlc_playlist(data: dict[str, dict]) -> int:
    """Generate VLC-compatible playlist"""
    lines = ["#EXTM3U"]
    lines.append(f"# Playlist generated by {TAG} Scraper - {Time.clean(Time.now()).strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("")
    count = 0

    for name, entry in data.items():
        url = entry.get("url")
        if not url:
            continue

        referer = entry.get("link", BASE_URL)
        tvg_id = entry.get("id", "Live.Event.us")
        tvg_logo = entry.get("logo", "https://i.gyazo.com/4a5e9fa2525808ee4b65002b56d3450e.png")
        group_title = entry.get("sport", "Live Events")
        
        lines.append(f'#EXTINF:-1 tvg-id="{tvg_id}" tvg-logo="{tvg_logo}" group-title="{group_title}",{name}')
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
    ua_encoded = USER_AGENT.replace(" ", "%20").replace("/", "%2F")
    lines = ["#EXTM3U"]
    lines.append(f"# Playlist generated by {TAG} Scraper - {Time.clean(Time.now()).strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("")
    count = 0

    for name, entry in data.items():
        url = entry.get("url")
        if not url:
            continue

        referer = entry.get("link", BASE_URL)
        tvg_id = entry.get("id", "Live.Event.us")
        tvg_logo = entry.get("logo", "https://i.gyazo.com/4a5e9fa2525808ee4b65002b56d3450e.png")
        group_title = entry.get("sport", "Live Events")
        
        lines.append(f'#EXTINF:-1 tvg-id="{tvg_id}" tvg-logo="{tvg_logo}" group-title="{group_title}",{name}')
        lines.append(f"{url}|referer={referer}|origin={referer}|user-agent={ua_encoded}")
        lines.append("")
        count += 1

    with open(OUTPUT_TIVIMATE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    log.info(f"Generated {OUTPUT_TIVIMATE} with {count} events")
    return count


async def scrape() -> None:
    # Load events from cache that don't have streams yet
    events = load_events_from_cache()
    
    if not events:
        log.warning("No events found in cache without streams")
        # Still try to generate playlists from existing streams
        cached_urls = CACHE_FILE.load() or {}
        existing_streams = {k: v for k, v in cached_urls.items() if v.get("url") and v["url"] not in [None, "null", ""]}
        if existing_streams:
            log.info(f"Found {len(existing_streams)} existing streams in cache")
            generate_vlc_playlist(existing_streams)
            generate_tivimate_playlist(existing_streams)
        return
    
    log.info(f"Processing {len(events)} events from cache")
    
    # Load existing cache
    existing_cache = CACHE_FILE.load() or {}
    updated_urls = {}
    now = Time.clean(Time.now())
    
    for i, ev in enumerate(events, start=1):
        log.info(f"Processing {i}/{len(events)}: {ev['event'][:60]}...")
        
        stream_url = await process_event(ev["link"], i)
        
        if not stream_url:
            log.warning(f"Event {i}) No stream found for: {ev['event'][:50]}...")
            continue
        
        # Create key in the same format as cache
        key = ev["key"]
        
        # Update the entry with stream URL
        updated_entry = {
            "url": stream_url,
            "logo": ev["logo"],
            "link": ev["link"],
            "id": ev["id"],
            "timestamp": now.timestamp(),
            "sport": ev["sport"],
        }
        
        updated_urls[key] = updated_entry
        existing_cache[key] = updated_entry
        
        log.info(f"Event {i}) ✓ Captured stream: {stream_url[:80]}...")
        
        # Small delay between requests
        await asyncio.sleep(2)
    
    # Save updated cache
    if updated_urls:
        CACHE_FILE.write(existing_cache)
        log.info(f"Updated {len(updated_urls)} events in cache")
        
        # Generate playlists with all streams (including existing ones)
        all_streams = {k: v for k, v in existing_cache.items() if v.get("url") and v["url"] not in [None, "null", ""]}
        vlc_count = generate_vlc_playlist(all_streams)
        tivimate_count = generate_tivimate_playlist(all_streams)
        log.info(f"Final playlist size: {len(all_streams)} events")
        log.info(f"Total written: {vlc_count + tivimate_count}")
    else:
        log.warning("No valid streams found")
        # Try to generate playlists from existing streams
        existing_streams = {k: v for k, v in existing_cache.items() if v.get("url") and v["url"] not in [None, "null", ""]}
        if existing_streams:
            generate_vlc_playlist(existing_streams)
            generate_tivimate_playlist(existing_streams)
        else:
            with open(OUTPUT_VLC, "w") as f:
                f.write("#EXTM3U\n# No streams available\n")
            with open(OUTPUT_TIVIMATE, "w") as f:
                f.write("#EXTM3U\n# No streams available\n")


async def main():
    log.info("Starting TPK scraper")
    await scrape()
    log.info("TPK scraper completed")


if __name__ == "__main__":
    asyncio.run(main())
