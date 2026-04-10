import asyncio
import re
from pathlib import Path
from urllib.parse import quote, quote_plus, urljoin

from playwright.async_api import async_playwright
from utils import Cache, Time, get_logger, leagues, network

log = get_logger(__name__)

TAG = "PITS"
BASE_URL = "https://pitsport.live"
LIVE_NOW_URL = f"{BASE_URL}/live-now"
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
# Extract playable stream from embed URL
# -------------------------------------------------
async def extract_playable_stream(embed_url: str, url_num: int) -> str | None:
    """Extract the actual playable stream URL from embed page"""
    try:
        # First try direct request to get the embed content
        r = await network.request(embed_url, log=log)
        if r:
            content = r.text
            
            # Look for CSS file that contains the stream
            css_pattern = r'(https?://[^\s"\']+\.css[^\s"\']*)'
            css_matches = re.findall(css_pattern, content, re.IGNORECASE)
            
            for css_url in css_matches:
                if 'serveplay' in css_url:
                    log.info(f"URL {url_num}) found CSS stream: {css_url}")
                    
                    # Fetch the CSS file to get the actual stream
                    css_response = await network.request(css_url, headers={"Referer": embed_url}, log=log)
                    if css_response:
                        css_content = css_response.text
                        
                        # Look for the actual stream URL in CSS content
                        stream_patterns = [
                            r'(https?://[^\s"\']+\.js[^\s"\']*)',
                            r'(https?://[^\s"\']+\.m3u8[^\s"\']*)',
                            r'(https?://[^\s"\']+serveplay[^\s"\']+\.(?:js|m3u8)[^\s"\']*)',
                        ]
                        
                        for pattern in stream_patterns:
                            stream_matches = re.findall(pattern, css_content, re.IGNORECASE)
                            for stream_url in stream_matches:
                                if 'serveplay' in stream_url:
                                    log.info(f"URL {url_num}) extracted playable stream: {stream_url[:100]}...")
                                    return stream_url
        
        # If direct request fails, use Playwright to capture network requests
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            
            stream_url = None
            
            async def handle_request(request):
                nonlocal stream_url
                url = request.url
                # Look for CSS or JS files from serveplay domain
                if 'serveplay' in url and (url.endswith('.css') or url.endswith('.js') or url.endswith('.m3u8')):
                    stream_url = url
                    log.info(f"URL {url_num}) captured stream via network: {url[:100]}...")
            
            page.on('request', handle_request)
            
            try:
                await page.goto(embed_url, wait_until='networkidle', timeout=30000)
                await asyncio.sleep(3)  # Wait for dynamic content
                
                # Also check for iframes
                iframes = await page.query_selector_all('iframe')
                for iframe in iframes:
                    src = await iframe.get_attribute('src')
                    if src and 'serveplay' in src:
                        # Recursively extract from iframe
                        stream_url = await extract_playable_stream(src, url_num)
                        if stream_url:
                            break
                
            except Exception as e:
                log.error(f"URL {url_num}) Playwright error: {e}")
            finally:
                await browser.close()
            
            return stream_url
            
    except Exception as e:
        log.error(f"URL {url_num}) Error extracting playable stream: {e}")
    
    return None


# -------------------------------------------------
# Extract stream URL using Playwright
# -------------------------------------------------
async def extract_stream_with_playwright(watch_url: str, url_num: int) -> str | None:
    """Extract stream URL using Playwright to capture dynamic content"""
    stream_url = None
    embed_url = None
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        
        # Set up request interception to capture stream URLs
        async def handle_request(request):
            nonlocal stream_url, embed_url
            url = request.url
            
            # First capture the embed URL
            if 'pushembdz.store/embed' in url and not embed_url:
                embed_url = url
                log.info(f"URL {url_num}) captured embed URL: {url[:100]}...")
            
            # Look for playable streams (CSS/JS files from serveplay)
            if 'serveplay' in url and (url.endswith('.css') or url.endswith('.js') or url.endswith('.m3u8')):
                stream_url = url
                log.info(f"URL {url_num}) captured playable stream: {url[:100]}...")
        
        # Listen to all requests
        page.on('request', handle_request)
        
        try:
            # Navigate to the watch page
            log.debug(f"URL {url_num}) navigating to {watch_url}")
            await page.goto(watch_url, wait_until='networkidle', timeout=30000)
            
            # Wait a bit for dynamic content to load
            await asyncio.sleep(5)
            
            # If we found an embed URL but no stream yet, try to extract from embed
            if embed_url and not stream_url:
                log.info(f"URL {url_num}) extracting playable stream from embed...")
                stream_url = await extract_playable_stream(embed_url, url_num)
            
            # Also check page content for any stream URLs
            if not stream_url:
                content = await page.content()
                patterns = [
                    r'(https?://[^\s"\']+serveplay[^\s"\']+\.css[^\s"\']*)',
                    r'(https?://[^\s"\']+serveplay[^\s"\']+\.js[^\s"\']*)',
                    r'(https?://[^\s"\']+serveplay[^\s"\']+\.m3u8[^\s"\']*)',
                    r'(https?://[^\s"\']+dash\.serveplay[^\s"\']+\.css[^\s"\']*)',
                ]
                
                for pattern in patterns:
                    matches = re.findall(pattern, content, re.IGNORECASE)
                    for match in matches:
                        if 'serveplay' in match:
                            stream_url = match
                            log.info(f"URL {url_num}) captured stream from page content: {match[:100]}...")
                            break
                    if stream_url:
                        break
            
        except Exception as e:
            log.error(f"URL {url_num}) Playwright error: {e}")
        finally:
            await browser.close()
    
    return stream_url


# -------------------------------------------------
# Extract stream URL using direct requests
# -------------------------------------------------
async def extract_stream_direct(watch_url: str, url_num: int) -> str | None:
    """Extract stream URL using direct HTTP requests"""
    r = await network.request(watch_url, log=log)
    if not r:
        return None
    
    content = r.text
    
    # Look for embed URLs first
    embed_patterns = [
        r'(https?://[^\s"\']*pushembdz\.store/embed/[^\s"\']+)',
        r'(https?://[^\s"\']*serveplay[^\s"\']+\.css[^\s"\']*)',
        r'(https?://[^\s"\']*serveplay[^\s"\']+\.js[^\s"\']*)',
    ]
    
    for pattern in embed_patterns:
        matches = re.findall(pattern, content, re.IGNORECASE)
        for match in matches:
            if 'pushembdz.store/embed' in match:
                # Found embed URL, try to extract playable stream from it
                log.info(f"URL {url_num}) found embed URL, extracting playable stream...")
                stream = await extract_playable_stream(match, url_num)
                if stream:
                    return stream
            elif 'serveplay' in match and (match.endswith('.css') or match.endswith('.js')):
                log.info(f"URL {url_num}) captured playable stream directly: {match[:100]}...")
                return match
    
    return None


# -------------------------------------------------
# Extract event IDs from live-now page
# -------------------------------------------------
async def get_event_ids_from_page() -> list[dict]:
    """Extract event IDs and info from the live-now page"""
    events = []
    
    r = await network.request(LIVE_NOW_URL, log=log)
    if not r:
        log.error("Failed to fetch live-now page")
        return events
    
    content = r.text
    
    # Find all watch URLs in the page
    watch_pattern = r'href=["\']/watch/([a-f0-9-]+)["\']'
    watch_ids = set(re.findall(watch_pattern, content, re.IGNORECASE))
    
    # Extract event details from the page
    for watch_id in watch_ids:
        event_url = f"{WATCH_BASE}/{watch_id}"
        
        # Try to extract category and title from around this watch ID
        context_pattern = rf'href=["\']/watch/{watch_id}["\'][^>]*>.*?<p[^>]*class="[^"]*text-gray-500[^"]*"[^>]*>([^<]+)</p>.*?<h1[^>]*>([^<]+)</h1>'
        context_match = re.search(context_pattern, content, re.DOTALL)
        
        if context_match:
            category = context_match.group(1).strip()
            title = context_match.group(2).strip()
        else:
            # Try alternative pattern
            alt_pattern = rf'href=["\']/watch/{watch_id}["\'][^>]*>.*?<h1[^>]*>([^<]+)</h1>'
            alt_match = re.search(alt_pattern, content, re.DOTALL)
            if alt_match:
                title = alt_match.group(1).strip()
                category = "Unknown"
            else:
                category = "Unknown"
                title = f"Event {watch_id[:8]}"
        
        events.append({
            'id': watch_id,
            'url': event_url,
            'category': category,
            'title': title,
        })
    
    log.info(f"Found {len(events)} event IDs on live-now page")
    return events


# -------------------------------------------------
# Get events (cached and new)
# -------------------------------------------------
async def get_events(cached_hrefs: set[str]) -> list[dict[str, str]]:
    """Get events from live-now page"""
    events = []
    
    # Get all event IDs from the page
    event_ids = await get_event_ids_from_page()
    
    for event_data in event_ids:
        event_id = event_data['id']
        event_url = event_data['url']
        category = event_data['category']
        title = event_data['title']
        
        # Skip if already cached
        if event_id in cached_hrefs:
            continue
        
        # Build sport name for group
        sport = category.upper().replace(' ', '_')
        
        events.append({
            "sport": sport,
            "category": category,
            "event": title,
            "full_name": f"{category} - {title}",
            "link": event_url,
            "href": event_id,
            "logo": "https://i.gyazo.com/4a5e9fa2525808ee4b65002b56d3450e.png",
        })
    
    return events


# -------------------------------------------------
# Build the final playlist text
# -------------------------------------------------
def build_playlist(data: dict[str, dict]) -> str:
    lines = ["#EXTM3U"]
    chno = 1
    
    for title, info in data.items():
        stream_url = info["url"]
        
        # Extract domain for referer/origin
        if 'serveplay' in stream_url:
            referer = BASE_URL
            origin = BASE_URL
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
        chno += 1
    
    return "\n".join(lines) + "\n"


# -------------------------------------------------
# Process event to get stream URL
# -------------------------------------------------
async def process_event(url: str, url_num: int) -> str | None:
    """Process event page to extract stream URL"""
    # Try direct extraction first (faster)
    stream = await extract_stream_direct(url, url_num)
    
    # If direct extraction fails, try with Playwright
    if not stream:
        log.debug(f"URL {url_num}) direct extraction failed, trying Playwright...")
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
    log.info(f"Found {len(events)} new event(s)")
    
    if not events and not urls:
        log.info("No events found and no cached events")
        return
    
    now_ts = Time.clean(Time.now()).timestamp()
    new_events_count = 0
    
    for i, ev in enumerate(events, start=1):
        log.info(f"Processing event {i}/{len(events)}: {ev['full_name'][:80]}...")
        
        stream = await process_event(ev["link"], i)
        
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


# -------------------------------------------------
# Run scraper
# -------------------------------------------------
async def main():
    log.info("Starting PITS scraper")
    await scrape()
    log.info("PITS scraper completed")


if __name__ == "__main__":
    asyncio.run(main())
