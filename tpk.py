import json
import re
import asyncio
from functools import partial
from urllib.parse import urljoin, urlparse, quote
from pathlib import Path

from selectolax.parser import HTMLParser
from playwright.async_api import async_playwright

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


async def extract_stream_with_playwright(url: str, url_num: int) -> str | None:
    """Extract stream URL using Playwright with network interception"""
    stream_url = None
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        page = await browser.new_page()
        
        # Set up request interception
        async def handle_request(request):
            nonlocal stream_url
            req_url = request.url
            
            # Look for stream URLs (cloudfront .woff2 files)
            if 'cloudfront' in req_url and '.woff2' in req_url:
                stream_url = req_url
                log.info(f"URL {url_num}) Captured stream: {req_url[:100]}...")
            elif '.m3u8' in req_url and 'cloudfront' in req_url:
                stream_url = req_url
                log.info(f"URL {url_num}) Captured m3u8: {req_url[:100]}...")
        
        page.on('request', handle_request)
        
        try:
            # Navigate to the event page
            log.info(f"URL {url_num}) Navigating to {url[:80]}...")
            await page.goto(url, wait_until='networkidle', timeout=30000)
            
            # Wait for page to load and for stream to initialize
            await asyncio.sleep(5)
            
            # Look for play buttons and click them
            play_buttons = await page.query_selector_all('button:has-text("Play"), button:has-text("play"), .play-button, .play-btn, [id*="play"], [class*="play"]')
            for button in play_buttons:
                try:
                    await button.click()
                    log.info(f"URL {url_num}) Clicked play button")
                    await asyncio.sleep(3)
                except:
                    pass
            
            # Look for iframes and navigate into them
            iframes = await page.query_selector_all('iframe')
            for iframe in iframes:
                src = await iframe.get_attribute('src')
                if src and 'crazyvidup' in src:
                    log.info(f"URL {url_num}) Found iframe: {src[:80]}...")
                    await page.goto(src, wait_until='networkidle', timeout=30000)
                    await asyncio.sleep(5)
                    
                    # Click play buttons in iframe
                    iframe_buttons = await page.query_selector_all('button:has-text("Play"), button:has-text("play")')
                    for button in iframe_buttons:
                        try:
                            await button.click()
                            log.info(f"URL {url_num}) Clicked play button in iframe")
                            await asyncio.sleep(3)
                        except:
                            pass
                    
                    # Check for stream URL in page content
                    content = await page.content()
                    cloudfront_pattern = r'(https?://[^\s"\']+cloudfront[^\s"\']+\.woff2[^\s"\']*)'
                    match = re.search(cloudfront_pattern, content, re.I)
                    if match:
                        stream_url = match.group(1)
                        log.info(f"URL {url_num}) Found stream in iframe content")
                        break
            
            # If still no stream, check page content
            if not stream_url:
                content = await page.content()
                patterns = [
                    r'(https?://[^\s"\']+cloudfront[^\s"\']+\.woff2[^\s"\']*)',
                    r'(https?://[^\s"\']+\.m3u8[^\s"\']*)',
                    r'(https?://[^\s"\']+serveplay[^\s"\']+\.(?:css|js)[^\s"\']*)',
                ]
                for pattern in patterns:
                    match = re.search(pattern, content, re.I)
                    if match:
                        stream_url = match.group(1)
                        log.info(f"URL {url_num}) Found stream in page content")
                        break
                    
        except Exception as e:
            log.error(f"URL {url_num}) Playwright error: {e}")
        finally:
            await browser.close()
    
    return stream_url


async def process_event(url: str, url_num: int) -> str | None:
    """Process event page to extract stream URL"""
    # Try Playwright for dynamic content
    stream = await extract_stream_with_playwright(url, url_num)
    if stream:
        return stream
    
    log.warning(f"URL {url_num}) No stream found")
    return None


def load_events_from_cache() -> list[dict[str, str]]:
    """Load events from the JSON cache file"""
    events = []
    
    # Load the cache file
    cached_urls = CACHE_FILE.load() or {}
    
    for key, entry in cached_urls.items():
        # Extract event name from key format: "[Live Event] WWE vs Monday Night RAW (TPK)"
        # Remove the tag from the end
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
        })
    
    log.info(f"Loaded {len(events)} events from cache")
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
    # Load events from cache file
    events = load_events_from_cache()
    
    if not events:
        log.warning("No events found in cache")
        return
    
    log.info(f"Processing {len(events)} events from cache")
    
    # Dictionary to store updated URLs
    updated_urls = {}
    now = Time.clean(Time.now())
    
    for i, ev in enumerate(events, start=1):
        log.info(f"Processing {i}/{len(events)}: {ev['event'][:60]}...")
        
        stream_url = await process_event(ev["link"], i)
        
        if not stream_url:
            log.warning(f"Event {i}) No stream found for: {ev['event'][:50]}...")
            continue
        
        # Create key in the same format as cache
        key = f"[Live Event] {ev['event']} ({TAG})"
        
        updated_urls[key] = {
            "url": stream_url,
            "logo": ev["logo"],
            "link": ev["link"],
            "id": ev["id"],
            "timestamp": now.timestamp(),
            "sport": ev["sport"],
        }
        
        log.info(f"Event {i}) ✓ Captured stream: {stream_url[:80]}...")
        
        # Small delay between requests
        await asyncio.sleep(1)
    
    # Update the cache with the new stream URLs
    if updated_urls:
        # Load existing cache
        existing_cache = CACHE_FILE.load() or {}
        
        # Update with new stream URLs
        for key, value in updated_urls.items():
            if key in existing_cache:
                existing_cache[key]["url"] = value["url"]
                existing_cache[key]["timestamp"] = value["timestamp"]
            else:
                existing_cache[key] = value
        
        # Save updated cache
        CACHE_FILE.write(existing_cache)
        log.info(f"Updated {len(updated_urls)} events in cache")
    
    # Generate playlists
    if updated_urls:
        vlc_count = generate_vlc_playlist(updated_urls)
        tivimate_count = generate_tivimate_playlist(updated_urls)
        log.info(f"Final playlist size: {len(updated_urls)} events")
        log.info(f"Total written: {vlc_count + tivimate_count}")
    else:
        log.warning("No valid streams found to generate playlists")
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
