import asyncio
import re
import json
from pathlib import Path
from urllib.parse import quote, quote_plus, urljoin

from playwright.async_api import async_playwright
from utils import Cache, Time, get_logger, leagues, network

log = get_logger(__name__)

TAG = "PITS"
BASE_URL = "https://pitsport.live"
SCHEDULE_URL = f"{BASE_URL}/schedule"
WATCH_BASE = f"{BASE_URL}/watch"

CACHE_FILE = Cache(f"{TAG.lower()}.json", exp=10_800)
OUTPUT_FILE = Path("pits.m3u8")

# Encoded User-Agent for TiViMate pipe
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:146.0) "
    "Gecko/20100101 Firefox/146.0"
)
UA_ENC = quote_plus(UA)


# -------------------------------------------------
# Extract stream from API endpoint
# -------------------------------------------------
async def extract_stream_from_api(watch_id: str, url_num: int) -> str | None:
    """Extract stream URL from the API endpoint"""
    api_url = f"https://pushembdz.store/api/stream/{watch_id}"
    
    try:
        log.info(f"URL {url_num}) Trying API: {api_url}")
        response = await network.request(api_url, log=log)
        
        if response:
            try:
                data = json.loads(response.text)
                stream_url = data.get("link")
                if stream_url and ('.css' in stream_url or '.js' in stream_url or '.m3u8' in stream_url):
                    log.info(f"URL {url_num}) Captured stream from API: {stream_url[:100]}...")
                    return stream_url
            except json.JSONDecodeError:
                log.debug(f"URL {url_num}) API response not JSON")
        
        return None
    except Exception as e:
        log.debug(f"URL {url_num}) API error: {e}")
        return None


# -------------------------------------------------
# Extract stream from watch page
# -------------------------------------------------
async def extract_stream_from_page(watch_url: str, watch_id: str, url_num: int) -> str | None:
    """Extract stream URL from watch page by finding API calls"""
    try:
        # First try the API endpoint directly
        stream = await extract_stream_from_api(watch_id, url_num)
        if stream:
            return stream
        
        # If API fails, try fetching the page
        response = await network.request(watch_url, log=log)
        if not response:
            return None
        
        content = response.text
        
        # Look for API URLs in the page
        api_pattern = r'https?://pushembdz\.store/api/stream/[a-f0-9-]+'
        api_matches = re.findall(api_pattern, content, re.IGNORECASE)
        
        for api_url in api_matches:
            log.info(f"URL {url_num}) Found API URL: {api_url}")
            api_response = await network.request(api_url, log=log)
            if api_response:
                try:
                    data = json.loads(api_response.text)
                    stream_url = data.get("link")
                    if stream_url and ('.css' in stream_url or '.js' in stream_url):
                        log.info(f"URL {url_num}) Captured stream from page API: {stream_url[:100]}...")
                        return stream_url
                except:
                    pass
        
        # Look for direct stream URLs
        stream_patterns = [
            r'(https?://[^\s"\']+serveplay[^\s"\']+\.css[^\s"\']*)',
            r'(https?://[^\s"\']+serveplay[^\s"\']+\.js[^\s"\']*)',
            r'(https?://[^\s"\']+ev01-prod[^\s"\']+\.css[^\s"\']*)',
            r'(https?://[^\s"\']+ev01-prod[^\s"\']+\.js[^\s"\']*)',
            r'(https?://[^\s"\']+cloudfront[^\s"\']+\.(?:css|js)[^\s"\']*)',
        ]
        
        for pattern in stream_patterns:
            matches = re.findall(pattern, content, re.IGNORECASE)
            for match in matches:
                log.info(f"URL {url_num}) Found stream URL: {match[:100]}...")
                return match
        
        return None
        
    except Exception as e:
        log.error(f"URL {url_num}) Error: {e}")
        return None


# -------------------------------------------------
# Extract stream using Playwright (fallback)
# -------------------------------------------------
async def extract_stream_with_playwright(watch_url: str, url_num: int) -> str | None:
    """Extract stream URL using Playwright to capture network requests"""
    stream_url = None
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        page = await browser.new_page()
        
        async def handle_response(response):
            nonlocal stream_url
            url = response.url
            
            # Look for API responses
            if 'pushembdz.store/api/stream' in url:
                try:
                    body = await response.text()
                    data = json.loads(body)
                    if data.get("link"):
                        stream_url = data["link"]
                        log.info(f"URL {url_num}) Captured from API response: {stream_url[:100]}...")
                except:
                    pass
            
            # Look for stream URLs in responses
            if any(x in url for x in ['.css', '.js', '.m3u8']) and any(x in url for x in ['serveplay', 'ev01-prod', 'cloudfront']):
                stream_url = url
                log.info(f"URL {url_num}) Captured stream from response: {url[:100]}...")
        
        page.on('response', handle_response)
        
        try:
            await page.goto(watch_url, wait_until='networkidle', timeout=30000)
            await asyncio.sleep(5)
            
            # Check for iframes
            iframes = await page.query_selector_all('iframe')
            for iframe in iframes:
                src = await iframe.get_attribute('src')
                if src and 'pushembdz.store' in src:
                    await page.goto(src, wait_until='networkidle', timeout=30000)
                    await asyncio.sleep(3)
            
        except Exception as e:
            log.error(f"URL {url_num}) Playwright error: {e}")
        finally:
            await browser.close()
    
    return stream_url


# -------------------------------------------------
# Extract events from schedule page
# -------------------------------------------------
async def get_events_from_schedule(cached_hrefs: set[str]) -> list[dict[str, str]]:
    """Extract events from the schedule page"""
    events = []
    
    response = await network.request(SCHEDULE_URL, log=log)
    if not response:
        log.error("Failed to fetch schedule page")
        return events
    
    content = response.text
    
    # Find all event links
    # Pattern: href="/watch/{uuid}"
    watch_pattern = r'href=["\']/watch/([a-f0-9-]+)["\']'
    watch_matches = re.findall(watch_pattern, content, re.IGNORECASE)
    
    # Also look for the newer structure with blocks
    for match in watch_matches:
        watch_id = match
        event_url = f"{WATCH_BASE}/{watch_id}"
        
        if watch_id in cached_hrefs:
            continue
        
        # Find the event title near this watch ID
        title_pattern = rf'href=["\']/watch/{watch_id}["\'][^>]*>.*?<h1[^>]*>([^<]+)</h1>'
        title_match = re.search(title_pattern, content, re.DOTALL | re.IGNORECASE)
        
        if title_match:
            title = title_match.group(1).strip()
        else:
            # Try to find title in the surrounding context
            context_pattern = rf'/watch/{watch_id}[^>]*>.*?(?:<h1[^>]*>|<div[^>]*class="[^"]*title[^"]*"[^>]*>)([^<]+)'
            context_match = re.search(context_pattern, content, re.DOTALL | re.IGNORECASE)
            title = context_match.group(1).strip() if context_match else f"Event {watch_id[:8]}"
        
        # Find the category/sport
        category = "Unknown"
        sport_keywords = {
            'NASCAR': 'NASCAR',
            'World Rally': 'WRC',
            'Rally': 'WRC',
            'MotoGP': 'MotoGP',
            'Moto2': 'Moto2',
            'Moto3': 'Moto3',
            'Super Formula': 'Super Formula',
            'Basketball': 'Basketball',
        }
        for keyword, sport_name in sport_keywords.items():
            if keyword.lower() in title.lower():
                category = sport_name
                break
        
        # Build full event name
        full_name = f"{category} - {title}" if category != "Unknown" else title
        
        events.append({
            "sport": category.upper().replace(' ', '_'),
            "category": category,
            "event": title,
            "full_name": full_name,
            "link": event_url,
            "href": watch_id,
            "logo": "https://i.gyazo.com/4a5e9fa2525808ee4b65002b56d3450e.png",
            "is_live": False,
        })
    
    log.info(f"Found {len(events)} events from schedule page")
    return events


# -------------------------------------------------
# Get events (cached and new)
# -------------------------------------------------
async def get_events(cached_hrefs: set[str]) -> list[dict[str, str]]:
    """Get events from schedule page"""
    events = await get_events_from_schedule(cached_hrefs)
    return events


# -------------------------------------------------
# Build the final playlist text
# -------------------------------------------------
def build_playlist(data: dict[str, dict]) -> str:
    lines = ["#EXTM3U"]
    lines.append(f"# Playlist generated by {TAG} Scraper - {Time.clean(Time.now()).strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("")
    chno = 1
    
    for title, info in data.items():
        stream_url = info["url"]
        
        # Extract domain for referer/origin
        if 'serveplay' in stream_url or 'ev01-prod' in stream_url:
            referer = "https://pushembdz.store/"
            origin = "https://pushembdz.store"
        else:
            referer = BASE_URL
            origin = BASE_URL
        
        lines.append(
            f'#EXTINF:-1 tvg-chno="{chno}" '
            f'tvg-id="{info["id"]}" '
            f'tvg-name="{title}" '
            f'tvg-logo="{info["logo"]}" '
            f'group-title="Live Events",{title}'
        )
        lines.append(
            f'{stream_url}'
            f'|referer={referer}'
            f'|origin={origin}'
            f'|user-agent={UA_ENC}'
        )
        lines.append("")
        chno += 1
    
    return "\n".join(lines) + "\n"


# -------------------------------------------------
# Process event to get stream URL
# -------------------------------------------------
async def process_event(watch_id: str, url: str, url_num: int) -> str | None:
    """Process event to extract stream URL"""
    # First try direct API call
    stream = await extract_stream_from_api(watch_id, url_num)
    if stream:
        return stream
    
    # Then try fetching the page
    stream = await extract_stream_from_page(url, watch_id, url_num)
    if stream:
        return stream
    
    # Finally try Playwright
    log.debug(f"URL {url_num}) Trying Playwright...")
    stream = await extract_stream_with_playwright(url, url_num)
    
    return stream


# -------------------------------------------------
# Main scrape function
# -------------------------------------------------
async def scrape() -> None:
    cached = CACHE_FILE.load() or {}
    urls: dict[str, dict] = dict(cached)
    cached_hrefs = {v.get("href", "") for v in urls.values()}
    
    log.info(f"Loaded {len(urls)} cached events")
    
    events = await get_events(cached_hrefs)
    log.info(f"Found {len(events)} event(s)")
    
    if not events and not urls:
        log.info("No events found and no cached events")
        return
    
    now_ts = Time.clean(Time.now()).timestamp()
    new_events_count = 0
    
    for i, ev in enumerate(events, start=1):
        log.info(f"Processing event {i}/{len(events)}: {ev['full_name'][:80]}...")
        
        stream = await process_event(ev["href"], ev["link"], i)
        
        if not stream:
            log.warning(f"Event {i}) No stream found for: {ev['full_name'][:50]}...")
            continue
        
        # Create title
        title = f"[{ev['sport']}] {ev['category']} - {ev['event']} ({TAG})"
        
        tvg_id, _logo_lookup = leagues.get_tvg_info(ev["sport"], ev["event"])
        
        urls[title] = {
            "url": stream,
            "logo": ev["logo"] or _logo_lookup,
            "base": BASE_URL,
            "timestamp": now_ts,
            "id": tvg_id or "Live.Event.us",
            "href": ev["href"],
            "category": ev["category"],
            "event": ev["event"],
        }
        new_events_count += 1
        log.info(f"Event {i}) ✓ Added: {ev['full_name'][:60]}... -> {stream[:80]}...")
        
        # Small delay between requests
        await asyncio.sleep(1)
    
    if new_events_count > 0:
        CACHE_FILE.write(urls)
        log.info(f"Added {new_events_count} new events to cache")
    
    # Write playlist
    if urls:
        out = build_playlist(urls)
        OUTPUT_FILE.write_text(out, encoding="utf-8")
        log.info(f"Successfully wrote {len(urls)} entries to pits.m3u8")
    else:
        log.warning("No events to write to playlist")
        OUTPUT_FILE.write_text("#EXTM3U\n# No events available\n", encoding="utf-8")


# -------------------------------------------------
# Run scraper
# -------------------------------------------------
async def main():
    log.info("Starting PITS scraper")
    await scrape()
    log.info("PITS scraper completed")


if __name__ == "__main__":
    asyncio.run(main())
