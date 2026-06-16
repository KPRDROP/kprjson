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
CACHE_FILE = Cache(f"{TAG.lower()}.json", exp=10_800)
OUTPUT_FILE = Path("pushembdz.m3u8")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:146.0) "
    "Gecko/20100101 Firefox/146.0"
)
UA_ENC = quote_plus(UA)


# -------------------------------------------------
# Extract stream from embed URL using Playwright
# -------------------------------------------------
async def extract_stream_from_embed(embed_url: str, url_num: int) -> str | None:
    """Extract stream URL from embed page by waiting for dynamic content"""
    stream_url = None
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        
        # Set up context with proper headers
        context = await browser.new_context(
            user_agent=UA,
            extra_http_headers={
                "Referer": BASE_URL,
                "Origin": BASE_URL,
            }
        )
        page = await context.new_page()
        
        # Set up request interception to capture API responses
        async def handle_response(response):
            nonlocal stream_url
            url = response.url
            
            # Look for the stream data in the page content
            try:
                body = await response.text()
                # Check if the response contains the stream JSON
                if '"link"' in body and ('m3u8' in body or 'css' in body):
                    # Try to extract the stream URL from the body
                    json_patterns = [
                        r'"link"\s*:\s*"([^"]+\.m3u8[^"]*)"',
                        r'"link"\s*:\s*"([^"]+\.css[^"]*)"',
                        r'{"success":\s*true,\s*"timestamp":\s*\d+,\s*"stream":\s*{[^}]+"link":\s*"([^"]+\.m3u8[^"]*)"[^}]*}}',
                    ]
                    for pattern in json_patterns:
                        match = re.search(pattern, body, re.IGNORECASE)
                        if match:
                            stream_url = match.group(1)
                            log.info(f"URL {url_num}) Captured stream from response: {stream_url[:100]}...")
                            return
            except:
                pass
        
        page.on('response', handle_response)
        
        try:
            log.info(f"URL {url_num}) Navigating to embed: {embed_url}")
            await page.goto(embed_url, wait_until='networkidle', timeout=30000)
            
            # Wait for dynamic content to load
            await asyncio.sleep(8)
            
            # Get the page content and look for stream data
            content = await page.content()
            
            # Look for the stream URL in the page content
            # The data is embedded in the Next.js page as JSON
            json_patterns = [
                r'"link"\s*:\s*"([^"]+\.m3u8[^"]*)"',
                r'"link"\s*:\s*"([^"]+\.css[^"]*)"',
                r'"success":\s*true,\s*"timestamp":\s*\d+,\s*"stream":\s*{[^}]*"link":\s*"([^"]+\.m3u8[^"]*)"[^}]*}',
                r'(https?://[^\s"\']+ossfeed\.store/out/v2/[a-f0-9]+/index\.m3u8[^\s"\']*)',
                r'(https?://[^\s"\']+ossfeed\.store/out/v2/[a-f0-9]+/master\.css[^\s"\']*)',
                r'(https?://[^\s"\']+serveplay[^\s"\']+\.css[^\s"\']*)',
                r'(https?://[^\s"\']+serveplay[^\s"\']+\.js[^\s"\']*)',
                r'(https?://[^\s"\']+ev01-prod[^\s"\']+\.css[^\s"\']*)',
                r'(https?://[^\s"\']+ev01-prod[^\s"\']+\.js[^\s"\']*)',
            ]
            
            for pattern in json_patterns:
                matches = re.findall(pattern, content, re.IGNORECASE)
                for match in matches:
                    if 'pushembdz.store' not in match and 'api.pushembdz.store' not in match:
                        if '.m3u8' in match or '.css' in match or '.js' in match:
                            stream_url = match
                            log.info(f"URL {url_num}) Found stream in page: {stream_url[:100]}...")
                            return stream_url
            
            # Also look for script tags containing JSON data
            script_pattern = r'<script[^>]*>([\s\S]*?)</script>'
            scripts = re.findall(script_pattern, content, re.IGNORECASE)
            
            for script in scripts:
                if '"link"' in script and ('m3u8' in script or 'css' in script):
                    # Look for the link in the script
                    link_pattern = r'"link"\s*:\s*"([^"]+\.m3u8[^"]*)"'
                    match = re.search(link_pattern, script, re.IGNORECASE)
                    if match:
                        stream_url = match.group(1)
                        log.info(f"URL {url_num}) Found stream in script: {stream_url[:100]}...")
                        return stream_url
                    
                    link_pattern = r'"link"\s*:\s*"([^"]+\.css[^"]*)"'
                    match = re.search(link_pattern, script, re.IGNORECASE)
                    if match:
                        stream_url = match.group(1)
                        log.info(f"URL {url_num}) Found stream in script: {stream_url[:100]}...")
                        return stream_url
            
            # Try to extract from JSON data in the page
            json_data_pattern = r'({[^{]*"link"[^}]*})'
            json_matches = re.findall(json_data_pattern, content, re.IGNORECASE)
            for json_str in json_matches:
                try:
                    data = json.loads(json_str)
                    if data.get("link") and ('m3u8' in data["link"] or 'css' in data["link"]):
                        stream_url = data["link"]
                        log.info(f"URL {url_num}) Captured stream from JSON: {stream_url[:100]}...")
                        return stream_url
                except:
                    pass
            
            # If no stream found, check if there's a redirect to the stream
            redirect_pattern = r'window\.location\s*=\s*["\']([^"\']+)["\']'
            redirect_match = re.search(redirect_pattern, content, re.IGNORECASE)
            if redirect_match:
                redirect_url = redirect_match.group(1)
                log.info(f"URL {url_num}) Found redirect: {redirect_url}")
                # Follow the redirect
                redirect_response = await network.request(redirect_url, headers={"Referer": embed_url}, log=log)
                if redirect_response:
                    for pattern in json_patterns:
                        match = re.search(pattern, redirect_response.text, re.IGNORECASE)
                        if match:
                            stream_url = match.group(1) if isinstance(match, re.Match) and len(match.groups()) > 0 else None
                            if stream_url:
                                log.info(f"URL {url_num}) Captured stream from redirect: {stream_url[:100]}...")
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
        context = await browser.new_context(user_agent=UA)
        page = await context.new_page()

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
        
        # Extract stream from embed using Playwright
        stream = await extract_stream_from_embed(ev["embed"], i)
        
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
