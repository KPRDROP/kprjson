import asyncio
import json
import re
from pathlib import Path
from urllib.parse import quote_plus, urljoin

from playwright.async_api import async_playwright
from utils import Cache, Time, get_logger, leagues, network

log = get_logger(__name__)

TAG = "PUSHEMBDZ"

BASE_URL = "https://pushembdz.store"
API_BASE_URL = "https://api.pushembdz.store"
CACHE_FILE = Cache(f"{TAG.lower()}.json", exp=10_800)
OUTPUT_FILE = Path("pushembdz.m3u8")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:146.0) "
    "Gecko/20100101 Firefox/146.0"
)
UA_ENC = quote_plus(UA)


# -------------------------------------------------
# Extract stream from embed URL
# -------------------------------------------------
async def extract_stream_from_embed(embed_url: str, url_num: int) -> str | None:
    """Extract stream URL from embed page by finding the API response"""
    try:
        # Extract the UUID from the embed URL
        uuid_match = re.search(r'/embed/([a-f0-9-]+)', embed_url)
        if not uuid_match:
            log.warning(f"URL {url_num}) Could not extract UUID from embed URL")
            return None
        
        uuid = uuid_match.group(1)
        
        # First try to fetch the embed page content directly
        log.info(f"URL {url_num}) Fetching embed: {embed_url}")
        response = await network.request(embed_url, log=log)
        
        if response:
            content = response.text
            
            # Look for the stream URL in the page content
            # The page contains JSON-like data with the stream link
            stream_patterns = [
                r'"link"\s*:\s*"([^"]+\.m3u8[^"]*)"',
                r'"link"\s*:\s*"([^"]+\.css[^"]*)"',
                r'(https?://[^\s"\']+\.m3u8[^\s"\']*)',
                r'(https?://[^\s"\']+ossfeed\.store/out/v2/[a-f0-9]+/index\.m3u8[^\s"\']*)',
                r'(https?://[^\s"\']+ossfeed\.store/out/v2/[a-f0-9]+/master\.css[^\s"\']*)',
                r'(https?://[^\s"\']+serveplay[^\s"\']+\.css[^\s"\']*)',
                r'(https?://[^\s"\']+serveplay[^\s"\']+\.js[^\s"\']*)',
                r'(https?://[^\s"\']+ev01-prod[^\s"\']+\.css[^\s"\']*)',
                r'(https?://[^\s"\']+ev01-prod[^\s"\']+\.js[^\s"\']*)',
            ]
            
            for pattern in stream_patterns:
                matches = re.findall(pattern, content, re.IGNORECASE)
                for match in matches:
                    # Filter out the embed URL itself (only return actual streams)
                    if 'pushembdz.store/embed' not in match and 'api.pushembdz.store' not in match:
                        if '.m3u8' in match or '.css' in match or '.js' in match:
                            log.info(f"URL {url_num}) Found stream URL: {match[:100]}...")
                            return match
            
            # Look for JSON data in the page (the page is a Next.js app with embedded data)
            # The stream data is embedded in the page as JSON
            json_patterns = [
                r'{"link"\s*:\s*"([^"]+\.m3u8[^"]*)"',
                r'{"link"\s*:\s*"([^"]+\.css[^"]*)"',
                r'{"success":\s*true,\s*"timestamp":\s*\d+,\s*"stream":\s*{[^}]+"link":\s*"([^"]+\.m3u8[^"]*)"[^}]*}}',
            ]
            
            for pattern in json_patterns:
                match = re.search(pattern, content, re.IGNORECASE)
                if match:
                    stream_url = match.group(1)
                    if 'pushembdz.store/embed' not in stream_url and 'api.pushembdz.store' not in stream_url:
                        log.info(f"URL {url_num}) Captured stream from JSON: {stream_url[:100]}...")
                        return stream_url
        
        # Try the API endpoint as fallback
        # Use the correct API URL: https://api.pushembdz.store/v1/streams
        api_url = f"{API_BASE_URL}/v1/streams"
        log.info(f"URL {url_num}) Trying API: {api_url}")
        
        api_response = await network.request(api_url, log=log)
        if api_response:
            try:
                data = api_response.json()
                if isinstance(data, list):
                    for item in data:
                        if item.get("id") == uuid:
                            stream_url = item.get("link")
                            if stream_url and ('m3u8' in stream_url or 'css' in stream_url):
                                # Convert from API embed URL to actual embed URL if needed
                                if 'api.pushembdz.store/embed' in stream_url:
                                    stream_url = stream_url.replace('api.pushembdz.store', 'pushembdz.store')
                                log.info(f"URL {url_num}) Captured stream from API: {stream_url[:100]}...")
                                return stream_url
                elif isinstance(data, dict):
                    # Check if it's a dict with a "data" key or directly the stream data
                    items = data.get("data", []) if "data" in data else [data] if data.get("id") else []
                    for item in items if isinstance(items, list) else []:
                        if item.get("id") == uuid:
                            stream_url = item.get("link")
                            if stream_url and ('m3u8' in stream_url or 'css' in stream_url):
                                if 'api.pushembdz.store/embed' in stream_url:
                                    stream_url = stream_url.replace('api.pushembdz.store', 'pushembdz.store')
                                log.info(f"URL {url_num}) Captured stream from API: {stream_url[:100]}...")
                                return stream_url
            except json.JSONDecodeError:
                log.debug(f"URL {url_num}) API response not JSON")
        
        return None
        
    except Exception as e:
        log.error(f"URL {url_num}) Error extracting stream: {e}")
        return None


# -------------------------------------------------
# Extract stream using Playwright (fallback)
# -------------------------------------------------
async def extract_stream_with_playwright(embed_url: str, url_num: int) -> str | None:
    """Extract stream URL using Playwright to capture API responses"""
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
            if 'api.pushembdz.store/v1/streams' in url:
                try:
                    body = await response.text()
                    data = json.loads(body)
                    if isinstance(data, list):
                        for item in data:
                            if item.get("link") and ('m3u8' in item["link"] or 'css' in item["link"]):
                                stream_url = item["link"]
                                if 'api.pushembdz.store/embed' in stream_url:
                                    stream_url = stream_url.replace('api.pushembdz.store', 'pushembdz.store')
                                log.info(f"URL {url_num}) Captured from API response: {stream_url[:100]}...")
                    elif isinstance(data, dict):
                        if data.get("link") and ('m3u8' in data["link"] or 'css' in data["link"]):
                            stream_url = data["link"]
                            if 'api.pushembdz.store/embed' in stream_url:
                                stream_url = stream_url.replace('api.pushembdz.store', 'pushembdz.store')
                            log.info(f"URL {url_num}) Captured from API response: {stream_url[:100]}...")
                except:
                    pass
            
            # Look for stream URLs in responses
            if any(x in url for x in ['.css', '.js', '.m3u8']) and any(x in url for x in ['ossfeed', 'serveplay', 'ev01-prod']):
                stream_url = url
                log.info(f"URL {url_num}) Captured stream from response: {url[:100]}...")
        
        page.on('response', handle_response)
        
        try:
            await page.goto(embed_url, wait_until='networkidle', timeout=30000)
            await asyncio.sleep(5)
            
            # Check page content for the stream URL
            content = await page.content()
            
            # Look for stream URLs in the rendered content
            stream_patterns = [
                r'"link"\s*:\s*"([^"]+\.m3u8[^"]*)"',
                r'"link"\s*:\s*"([^"]+\.css[^"]*)"',
                r'(https?://[^\s"\']+\.m3u8[^\s"\']*)',
            ]
            
            for pattern in stream_patterns:
                matches = re.findall(pattern, content, re.IGNORECASE)
                for match in matches:
                    if 'pushembdz.store/embed' not in match and 'api.pushembdz.store' not in match:
                        stream_url = match
                        log.info(f"URL {url_num}) Found stream in page: {stream_url[:100]}...")
                        return stream_url
            
        except Exception as e:
            log.error(f"URL {url_num}) Playwright error: {e}")
        finally:
            await browser.close()
    
    return stream_url


# -------------------------------------------------
# Extract events from homepage using Playwright
# -------------------------------------------------
async def get_events(cached_hrefs: set[str]) -> list[dict]:
    """Extract events from the homepage using Playwright"""
    events = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        page = await browser.new_page()

        try:
            await page.goto(BASE_URL, wait_until="networkidle", timeout=60000)
            
            # Wait for content to render
            await asyncio.sleep(5)
            
            # Find all event cards - look for h3 elements with titles
            cards = await page.query_selector_all("h3")
            
            for card in cards:
                title = await card.get_attribute("title")
                if not title:
                    title = await card.inner_text()
                
                if not title or not title.strip():
                    continue
                
                title = title.strip()
                
                # Find the code element containing the embed URL
                parent = await card.evaluate_handle("el => el.closest('div')")
                code_el = await parent.query_selector("code")
                
                if not code_el:
                    siblings = await page.evaluate_handle("""
                        (el) => {
                            const parent = el.parentElement;
                            if (parent) {
                                const code = parent.querySelector('code');
                                return code ? code.innerText : null;
                            }
                            return null;
                        }
                    """, card)
                    
                    if siblings:
                        embed_url = str(siblings).strip()
                    else:
                        continue
                else:
                    embed_url = await code_el.inner_text()
                    embed_url = embed_url.strip()
                
                if "pushembdz.store/embed" not in embed_url:
                    continue
                
                event_id = embed_url.split("/")[-1]
                
                if event_id in cached_hrefs:
                    continue
                
                events.append({
                    "event": title,
                    "embed": embed_url,
                    "href": event_id,
                    "logo": "https://i.gyazo.com/4a5e9fa2525808ee4b65002b56d3450e.png"
                })
                
                log.info(f"Found event: {title[:50]}... -> {embed_url[:60]}...")

        except Exception as e:
            log.error(f"Error extracting events: {e}")
        finally:
            await browser.close()

    log.info(f"Extracted {len(events)} events from homepage")
    return events


# -------------------------------------------------
# Build playlist
# -------------------------------------------------
def build_playlist(data: dict[str, dict]) -> str:
    lines = ["#EXTM3U"]
    lines.append(f"# Playlist generated by {TAG} Updater - {Time.clean(Time.now()).strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("")
    chno = 1

    for title, info in data.items():
        stream_url = info["url"]
        
        # Extract domain for referer/origin
        if 'serveplay' in stream_url or 'ev01-prod' in stream_url or 'ossfeed' in stream_url:
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
# Main scrape
# -------------------------------------------------
async def scrape():
    cached = CACHE_FILE.load() or {}
    urls = dict(cached)

    cached_hrefs = {v.get("href") for v in urls.values() if v.get("href")}

    log.info(f"Loaded {len(urls)} cached events")

    events = await get_events(cached_hrefs)

    if not events:
        log.warning("No events found")
        if urls:
            playlist = build_playlist(urls)
            OUTPUT_FILE.write_text(playlist, encoding="utf-8")
            log.info(f"Playlist written from cache: {OUTPUT_FILE}")
        return

    now_ts = Time.clean(Time.now()).timestamp()
    new_events_count = 0

    for i, ev in enumerate(events, start=1):
        log.info(f"Processing {i}/{len(events)}: {ev['event'][:60]}...")
        
        # Try direct API call first
        stream = await extract_stream_from_embed(ev["embed"], i)
        
        # If direct fails, try Playwright
        if not stream:
            log.debug(f"Event {i}) Trying Playwright...")
            stream = await extract_stream_with_playwright(ev["embed"], i)
        
        if not stream:
            log.warning(f"Event {i}) No stream found for: {ev['event'][:50]}...")
            continue

        title = f"[EVENT] {ev['event']} ({TAG})"

        tvg_id, logo_lookup = leagues.get_tvg_info("EVENT", ev["event"])

        urls[title] = {
            "url": stream,
            "logo": ev["logo"] or logo_lookup,
            "timestamp": now_ts,
            "id": tvg_id or "Live.Event.us",
            "href": ev["href"],
        }
        new_events_count += 1
        log.info(f"Event {i}) ✓ Captured: {ev['event'][:50]}... -> {stream[:80]}...")

        await asyncio.sleep(1)

    if new_events_count > 0:
        CACHE_FILE.write(urls)
        log.info(f"Added {new_events_count} new events to cache")

    # Write playlist
    if urls:
        playlist = build_playlist(urls)
        OUTPUT_FILE.write_text(playlist, encoding="utf-8")
        log.info(f"Playlist written: {OUTPUT_FILE} with {len(urls)} events")
    else:
        log.warning("No events to write to playlist")
        OUTPUT_FILE.write_text("#EXTM3U\n# No events available\n", encoding="utf-8")


# -------------------------------------------------
# Run
# -------------------------------------------------
async def main():
    log.info("Starting PUSHEMBDZ updater")
    await scrape()
    log.info("PUSHEMBDZ updater completed")


if __name__ == "__main__":
    asyncio.run(main())
