import json
import re
import asyncio
from urllib.parse import urljoin
from pathlib import Path

from selectolax.parser import HTMLParser
from playwright.async_api import async_playwright

from utils import Cache, Time, get_logger, network

log = get_logger(__name__)

urls = {}

CACHE_FILE = Cache("TPK", exp=28_800)

BASE_URL = "https://live.totalsportek.fyi"

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36"

OUTPUT_VLC = Path("tpk_vlc.m3u8")
OUTPUT_TIVIMATE = Path("tpk_tivimate.m3u8")


# =========================
# PLAYWRIGHT STREAM EXTRACTOR
# =========================
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
            
            # Look for stream URLs
            if any(x in req_url for x in ['.m3u8', '.woff2', 'cloudfront', 'serveplay']):
                if 'invalid' not in req_url and 'google' not in req_url:
                    stream_url = req_url
                    log.info(f"URL {url_num}) Captured stream: {req_url[:100]}...")
        
        page.on('request', handle_request)
        
        try:
            # Navigate to the event page
            await page.goto(url, wait_until='networkidle', timeout=30000)
            
            # Wait for streams to load
            await asyncio.sleep(5)
            
            # Also check for iframes
            iframes = await page.query_selector_all('iframe')
            for iframe in iframes:
                src = await iframe.get_attribute('src')
                if src:
                    # Navigate to iframe
                    try:
                        await page.goto(src, wait_until='networkidle', timeout=15000)
                        await asyncio.sleep(3)
                    except:
                        pass
            
            # Check page content for stream URLs
            content = await page.content()
            patterns = [
                r'(https?://[^\s"\']+\.m3u8[^\s"\']*)',
                r'(https?://[^\s"\']+\.woff2[^\s"\']*)',
                r'(https?://[^\s"\']+cloudfront[^\s"\']+\.(?:m3u8|woff2)[^\s"\']*)',
            ]
            for pattern in patterns:
                matches = re.findall(pattern, content, re.I)
                for match in matches:
                    if 'invalid' not in match and 'google' not in match:
                        stream_url = match
                        log.info(f"URL {url_num}) Captured from page: {match[:100]}...")
                        break
                if stream_url:
                    break
                    
        except Exception as e:
            log.error(f"URL {url_num}) Playwright error: {e}")
        finally:
            await browser.close()
    
    return stream_url


# =========================
# DIRECT HTTP EXTRACTOR (FALLBACK)
# =========================
async def extract_stream_direct(url: str, url_num: int) -> str | None:
    """Extract stream URL using direct HTTP requests"""
    res = await network.request(url, log=log)
    if not res:
        return None
    
    text = res.text
    
    # Look for stream URLs
    patterns = [
        r'(https?://[^\s"\']+\.m3u8[^\s"\']*)',
        r'(https?://[^\s"\']+\.woff2[^\s"\']*)',
        r'(https?://[^\s"\']+cloudfront[^\s"\']+\.(?:m3u8|woff2)[^\s"\']*)',
        r'source\s*:\s*"([^"]+\.m3u8[^"]*)"',
        r'file\s*:\s*"([^"]+\.m3u8[^"]*)"',
    ]
    
    for pattern in patterns:
        matches = re.findall(pattern, text, re.I)
        for match in matches:
            if 'invalid' not in match and 'google' not in match:
                log.info(f"URL {url_num}) Captured direct: {match[:100]}...")
                return match
    
    # Check for iframes recursively
    soup = HTMLParser(res.content)
    for iframe in soup.css("iframe"):
        src = iframe.attributes.get("src")
        if src:
            src = urljoin(url, src)
            result = await extract_stream_direct(src, url_num)
            if result:
                return result
    
    return None


# =========================
# MAIN EXTRACTOR
# =========================
async def extract_stream(url: str, url_num: int) -> str | None:
    """Extract stream URL using multiple methods"""
    
    # Try direct HTTP first (faster)
    stream = await extract_stream_direct(url, url_num)
    if stream:
        return stream
    
    # Try Playwright (handles dynamic content)
    log.info(f"URL {url_num}) Trying Playwright...")
    stream = await extract_stream_with_playwright(url, url_num)
    if stream:
        return stream
    
    log.warning(f"URL {url_num}) NO STREAM FOUND")
    return None


# =========================
# EVENTS
# =========================
async def get_events():
    events = []

    res = await network.request(BASE_URL, log=log)
    if not res:
        return events

    soup = HTMLParser(res.content)

    for node in soup.css("a"):
        if not node.attributes.get("class"):
            continue

        teams = [t.text(strip=True) for t in node.css(".col-7 .col-12")]
        if not teams:
            continue

        href = node.attributes.get("href")
        if not href:
            continue

        time_node = node.css_first(".col-3 span")
        if not time_node or time_node.text(strip=True).lower() != "matchstarted":
            continue

        event_name = " vs ".join(teams)
        event_url = urljoin(BASE_URL, href)

        events.append({
            "event": event_name,
            "link": event_url
        })

    log.info(f"Found {len(events)} events")
    return events


# =========================
# PROCESS EVENTS
# =========================
async def process_event(ev, index, total):
    name = ev["event"]
    link = ev["link"]
    
    log.info(f"[{index}/{total}] Processing: {name}")
    
    stream = await extract_stream(link, index)
    
    if stream:
        urls[f"[Live Event] {name} (TPK)"] = {
            "url": stream,
            "link": link,
            "id": "Live.Event.us",
            "logo": "https://i.gyazo.com/4a5e9fa2525808ee4b65002b56d3450e.png",
            "timestamp": Time.now().timestamp(),
        }
        log.info(f"[{index}/{total}] ✓ Captured: {name[:50]}...")
    else:
        log.warning(f"[{index}/{total}] ✗ Failed: {name[:50]}...")


# =========================
# OUTPUT
# =========================
def write_outputs():
    vlc_lines = ["#EXTM3U"]
    tivi_lines = ["#EXTM3U"]
    vlc_lines.append("# Playlist generated by TPK Scraper")
    tivi_lines.append("# Playlist generated by TPK Scraper")
    vlc_lines.append("")
    tivi_lines.append("")
    
    chno = 200
    
    for name, data in urls.items():
        url = data["url"]
        if not url or "invalid" in url:
            continue
            
        # VLC format
        vlc_lines.append(
            f'#EXTINF:-1 tvg-chno="{chno}" tvg-id="{data["id"]}" tvg-name="{name}" '
            f'tvg-logo="{data["logo"]}" group-title="Live Events",{name}'
        )
        vlc_lines.append(f'#EXTVLCOPT:http-referrer={data["link"]}')
        vlc_lines.append(f'#EXTVLCOPT:http-origin={data["link"]}')
        vlc_lines.append(f'#EXTVLCOPT:http-user-agent={USER_AGENT}')
        vlc_lines.append(url)
        vlc_lines.append("")
        
        # TiviMate format
        ua_encoded = USER_AGENT.replace(" ", "%20").replace("/", "%2F")
        tivi_lines.append(
            f'#EXTINF:-1 tvg-chno="{chno}" tvg-id="{data["id"]}" tvg-name="{name}" '
            f'tvg-logo="{data["logo"]}" group-title="Live Events",{name}'
        )
        tivi_lines.append(
            f'{url}|referer={data["link"]}|origin={data["link"]}|user-agent={ua_encoded}'
        )
        tivi_lines.append("")
        
        chno += 1
    
    # Write files
    OUTPUT_VLC.write_text("\n".join(vlc_lines), encoding="utf-8")
    OUTPUT_TIVIMATE.write_text("\n".join(tivi_lines), encoding="utf-8")
    
    log.info(f"Playlists written: {len(urls)} streams")


# =========================
# MAIN
# =========================
async def scrape():
    log.info("Starting TPK scraper")
    
    # Load cache
    cache = CACHE_FILE.load() or {}
    for k, v in cache.items():
        if v.get("url") and "invalid" not in v["url"]:
            urls[k] = v
    
    log.info(f"Loaded {len(urls)} cached events")
    
    # Get new events
    events = await get_events()
    
    # Filter out already processed events
    new_events = []
    for ev in events:
        key = f"[Live Event] {ev['event']} (TPK)"
        if key not in urls:
            new_events.append(ev)
    
    log.info(f"Processing {len(new_events)} new events")
    
    # Process events sequentially (to avoid overwhelming)
    for i, ev in enumerate(new_events, 1):
        await process_event(ev, i, len(new_events))
        # Small delay between requests
        await asyncio.sleep(1)
    
    # Save cache
    CACHE_FILE.write(urls)
    
    # Write outputs
    write_outputs()
    
    log.info(f"TPK scraper completed - {len(urls)} total streams")


if __name__ == "__main__":
    asyncio.run(scrape())
