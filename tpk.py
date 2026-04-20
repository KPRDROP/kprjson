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

urls: dict[str, dict[str, str | float]] = {}

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
            
            # Look for stream URLs
            if any(x in req_url for x in ['.m3u8', '.woff2', 'cloudfront', 'serveplay', 'm3u8']):
                if 'invalid' not in req_url and 'google' not in req_url and 'chat' not in req_url:
                    stream_url = req_url
                    log.info(f"URL {url_num}) Captured stream via network: {req_url[:100]}...")
        
        page.on('request', handle_request)
        
        try:
            # Navigate to the event page
            await page.goto(url, wait_until='networkidle', timeout=30000)
            
            # Wait for page to load
            await asyncio.sleep(5)
            
            # Look for player links and click them
            player_links = await page.query_selector_all('a[href*="crazyvidup"], a[href*="player"], a[href*="embed"]')
            for link in player_links:
                href = await link.get_attribute('href')
                if href:
                    log.info(f"URL {url_num}) Found player link: {href[:100]}...")
                    await page.goto(href, wait_until='networkidle', timeout=30000)
                    await asyncio.sleep(5)
            
            # Also check for iframes
            iframes = await page.query_selector_all('iframe')
            for iframe in iframes:
                src = await iframe.get_attribute('src')
                if src:
                    log.info(f"URL {url_num}) Found iframe: {src[:100]}...")
                    await page.goto(src, wait_until='networkidle', timeout=30000)
                    await asyncio.sleep(5)
            
            # Check page content for stream URLs
            content = await page.content()
            patterns = [
                r'(https?://[^\s"\']+\.m3u8[^\s"\']*)',
                r'(https?://[^\s"\']+\.woff2[^\s"\']*)',
                r'(https?://[^\s"\']+cloudfront[^\s"\']+\.(?:m3u8|woff2)[^\s"\']*)',
                r'(https?://[^\s"\']+serveplay[^\s"\']+\.(?:css|js)[^\s"\']*)',
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


async def extract_stream_from_page(url: str, url_num: int) -> str | None:
    """Extract stream URL from event page by finding iframes and scripts"""
    if not (event_data := await network.request(url, log=log)):
        log.warning(f"URL {url_num}) Failed to load url.")
        return None

    soup = HTMLParser(event_data.content)
    
    # Look for player links first
    player_links = []
    for link in soup.css("a[href*='crazyvidup'], a[href*='player'], a[href*='embed']"):
        href = link.attributes.get("href")
        if href:
            player_links.append(href)
    
    # Follow player links
    for player_url in player_links:
        if not player_url.startswith("http"):
            player_url = urljoin(url, player_url)
        
        log.info(f"URL {url_num}) Following player link: {player_url[:100]}...")
        
        player_data = await network.request(player_url, headers={"Referer": url}, log=log)
        if player_data:
            # Look for stream URLs in player page
            patterns = [
                r'(https?://[^\s"\']+\.m3u8[^\s"\']*)',
                r'(https?://[^\s"\']+\.woff2[^\s"\']*)',
                r'(https?://[^\s"\']+cloudfront[^\s"\']+\.(?:m3u8|woff2)[^\s"\']*)',
                r'(https?://[^\s"\']+serveplay[^\s"\']+\.(?:css|js)[^\s"\']*)',
                r'source\s*:\s*"([^"]+)"',
                r'file\s*:\s*"([^"]+)"',
            ]
            for pattern in patterns:
                match = re.search(pattern, player_data.text, re.I)
                if match:
                    stream = match.group(1)
                    if '.m3u8' in stream or 'cloudfront' in stream:
                        log.info(f"URL {url_num}) Captured stream from player link")
                        return stream
    
    # Look for iframes
    iframes = soup.css("iframe")
    for iframe in iframes:
        src = iframe.attributes.get("src")
        if not src:
            continue
        
        if not src.startswith("http"):
            src = urljoin(url, src)
        
        # Skip chat iframes
        if "chat" in src.lower():
            continue
        
        log.info(f"URL {url_num}) Found iframe: {src[:100]}...")
        
        # Fetch iframe content
        iframe_data = await network.request(src, headers={"Referer": url}, log=log)
        if iframe_data:
            # Look for stream URLs in iframe
            patterns = [
                r'(https?://[^\s"\']+\.m3u8[^\s"\']*)',
                r'(https?://[^\s"\']+\.woff2[^\s"\']*)',
                r'(https?://[^\s"\']+cloudfront[^\s"\']+\.(?:m3u8|woff2)[^\s"\']*)',
                r'(https?://[^\s"\']+serveplay[^\s"\']+\.(?:css|js)[^\s"\']*)',
            ]
            for pattern in patterns:
                match = re.search(pattern, iframe_data.text, re.I)
                if match:
                    log.info(f"URL {url_num}) Captured stream from iframe")
                    return match.group(1)
    
    # Look for stream URLs in scripts
    scripts = soup.css("script")
    for script in scripts:
        script_text = script.text()
        if script_text:
            patterns = [
                r'(https?://[^\s"\']+\.m3u8[^\s"\']*)',
                r'(https?://[^\s"\']+\.woff2[^\s"\']*)',
                r'(https?://[^\s"\']+cloudfront[^\s"\']+\.(?:m3u8|woff2)[^\s"\']*)',
                r'file\s*:\s*"([^"]+\.m3u8[^"]*)"',
                r'source\s*:\s*"([^"]+\.m3u8[^"]*)"',
            ]
            for pattern in patterns:
                match = re.search(pattern, script_text, re.I)
                if match:
                    log.info(f"URL {url_num}) Captured stream from script")
                    return match.group(1)
    
    return None


async def process_event(url: str, url_num: int, tag: str) -> str | None:
    """Process event page to extract stream URL"""
    # Try direct HTTP first
    stream = await extract_stream_from_page(url, url_num)
    if stream:
        return stream
    
    # Try Playwright for dynamic content
    log.info(f"URL {url_num}) Trying Playwright...")
    stream = await extract_stream_with_playwright(url, url_num)
    if stream:
        return stream
    
    return None


async def get_events(cached_keys: list[str]) -> list[dict[str, str]]:
    """Extract events from the main page"""
    events = []

    if not (html_data := await network.request(BASE_URL, log=log)):
        return events

    soup = HTMLParser(html_data.content)
    
    # Find all event links
    for link in soup.css("a[href*='watchfootballhighlights'], a[href*='/event/'], a[href*='/match/']"):
        href = link.attributes.get("href")
        if not href:
            continue
        
        # Skip non-event links
        if any(skip in href.lower() for skip in ['boxing', 'blog', 'date', 'images', 'css', 'disclaimer']):
            continue
        
        # Get the event name
        event_name = None
        
        # Try to get from parent elements
        parent = link.parent
        for _ in range(5):
            if parent:
                # Look for team names
                text = parent.text(strip=True)
                if ' vs ' in text:
                    parts = text.split(' vs ')
                    if len(parts) >= 2:
                        event_name = parts[0].strip() + ' vs ' + parts[1].strip().split()[0]
                        break
                parent = parent.parent
            else:
                break
        
        if not event_name:
            # Extract from URL
            href_parts = href.split('/')
            if len(href_parts) > 1:
                event_name = href_parts[-1].replace('-', ' ').title()
            else:
                continue
        
        # Determine sport
        sport = "Live Event"
        sport_keywords = {
            'F1': 'F1', 'NASCAR': 'NASCAR', 'WWE': 'WWE',
            'Tennis': 'Tennis', 'Golf': 'Golf', 'NBA': 'NBA',
            'MLB': 'MLB', 'NHL': 'NHL', 'Boxing': 'BOXING',
        }
        for key, value in sport_keywords.items():
            if key.lower() in event_name.lower():
                sport = value
                break
        
        event_url = href if href.startswith('http') else urljoin(BASE_URL, href)
        key = f"[{sport}] {event_name} ({TAG})"
        
        if key in cached_keys:
            continue
        
        events.append({
            "sport": sport,
            "event": event_name,
            "tag": TAG,
            "link": event_url,
        })
    
    # Remove duplicates
    seen = set()
    unique_events = []
    for event in events:
        if event["link"] not in seen:
            seen.add(event["link"])
            unique_events.append(event)
    
    log.info(f"Found {len(unique_events)} events")
    return unique_events


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
            log.info(f"Processing {i}/{len(events)}: {ev['event']}")
            
            handler = partial(
                process_event,
                url=ev["link"],
                url_num=i,
                tag=ev["tag"],
            )

            stream_url = await network.safe_process(
                handler,
                url_num=i,
                semaphore=network.HTTP_S,
                log=log,
            )

            if not stream_url:
                log.warning(f"Event {i}) No stream found for: {ev['event']}")
                continue

            key = f"[{ev['sport']}] {ev['event']} ({ev['tag']})"
            tvg_id, logo = leagues.get_tvg_info(ev["sport"], ev["event"])

            entry = {
                "url": stream_url,
                "logo": logo,
                "base": ev["link"],
                "timestamp": now.timestamp(),
                "id": tvg_id or "Live.Event.us",
                "link": ev["link"],
                "sport": ev["sport"],
            }

            cached_urls[key] = entry
            urls[key] = entry
            valid_count += 1
            log.info(f"Event {i}) ✓ Captured: {ev['event']}")

        log.info(f"Collected and cached {valid_count - cached_count} new event(s)")

    else:
        log.info("No new events found")

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
    log.info("Starting TPK scraper")
    await scrape()
    log.info("TPK scraper completed")


if __name__ == "__main__":
    asyncio.run(main())
