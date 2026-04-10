import asyncio
import re
import json
from functools import partial
from pathlib import Path
from urllib.parse import quote, quote_plus, urljoin

from utils import Cache, Time, get_logger, leagues, network
from selectolax.parser import HTMLParser

log = get_logger(__name__)

TAG = "PITS"
BASE_URL = "https://pitsport.live"
LIVE_NOW_URL = f"{BASE_URL}/live-now"

CACHE_FILE = Cache(f"{TAG.lower()}.json", exp=10_800)
OUTPUT_FILE = Path("pits.m3u8")

# Encoded User-Agent for TiViMate pipe
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:146.0) "
    "Gecko/20100101 Firefox/146.0"
)
UA_ENC = quote_plus(UA)


# -------------------------------------------------
# Extract image URL from srcset
# -------------------------------------------------
def extract_image_url(srcset: str) -> str:
    """Extract full image URL from srcset attribute"""
    if not srcset:
        return "https://i.gyazo.com/4a5e9fa2525808ee4b65002b56d3450e.png"
    
    # Pattern to match URL in srcset
    pattern = r'(https?://[^\s]+\.(?:jpg|jpeg|png|webp))'
    match = re.search(pattern, srcset)
    if match:
        return match.group(1)
    
    return "https://i.gyazo.com/4a5e9fa2525808ee4b65002b56d3450e.png"


# -------------------------------------------------
# Extract M3U8 from event watch page
# -------------------------------------------------
async def extract_stream_from_watch_page(watch_url: str, url_num: int) -> str | None:
    """Extract M3U8 stream URL from watch page"""
    r = await network.request(watch_url, log=log)
    if not r:
        log.warning(f"URL {url_num}) failed to load watch page")
        return None
    
    soup = HTMLParser(r.content)
    
    # Look for iframe with embed
    iframe = soup.css_first('iframe[src*="embed"], iframe[src*="stream"], iframe[src*="player"]')
    if iframe:
        iframe_src = iframe.attributes.get("src")
        if iframe_src:
            # Fetch iframe content
            iframe_r = await network.request(iframe_src, headers={"Referer": watch_url}, log=log)
            if iframe_r:
                # Look for M3U8 in iframe content
                m3u8_pattern = r'(https?://[^\s"\']+\.m3u8[^\s"\']*)'
                match = re.search(m3u8_pattern, iframe_r.text, re.IGNORECASE)
                if match:
                    stream = match.group(1)
                    log.info(f"URL {url_num}) captured M3U8 from iframe -> {stream[:80]}...")
                    return stream
    
    # Look for direct M3U8 in page
    m3u8_pattern = r'(https?://[^\s"\']+\.(?:m3u8|html)[^\s"\']*)'
    matches = re.findall(m3u8_pattern, r.text, re.IGNORECASE)
    
    # Filter for actual stream URLs
    for match in matches:
        if 'serveplay' in match or 'm3u8' in match:
            log.info(f"URL {url_num}) captured M3U8 -> {match[:80]}...")
            return match
    
    # Look for JavaScript variables containing stream URL
    script_pattern = r'(?:var|const|let)\s+(?:url|src|source|stream|file|video|hls)\s*=\s*["\']([^"\']+\.(?:m3u8|html)[^"\']*)["\']'
    match = re.search(script_pattern, r.text, re.IGNORECASE)
    if match:
        stream = match.group(1)
        log.info(f"URL {url_num}) captured M3U8 from JS -> {stream[:80]}...")
        return stream
    
    # Look for HTML file that might contain the stream
    html_pattern = r'(https?://[^\s"\']+\.html[^\s"\']*)'
    matches = re.findall(html_pattern, r.text, re.IGNORECASE)
    for match in matches:
        if 'serveplay' in match:
            log.info(f"URL {url_num}) captured HTML stream -> {match[:80]}...")
            return match
    
    log.warning(f"URL {url_num}) no stream found on watch page")
    return None


# -------------------------------------------------
# Parse events from main homepage HTML
# -------------------------------------------------
async def get_events(cached_hrefs: set[str]) -> list[dict[str, str]]:
    events = []
    
    r = await network.request(LIVE_NOW_URL, log=log)
    if not r:
        log.error("Failed to fetch live-now page")
        return events
    
    soup = HTMLParser(r.content)
    
    # Find all event cards - look for elements with the structure
    # Looking for parent elements that contain both category and event name
    event_cards = soup.css('div.flex.flex-col')
    
    for card in event_cards:
        # Get category (race name) from p tag
        category_elem = card.css_first('p.text-gray-500.text-xs.font-semibold.uppercase.tracking-wider.mb-1')
        if not category_elem:
            continue
        
        category = category_elem.text(strip=True)
        
        # Get event name from h1 tag
        name_elem = card.css_first('h1.text-white.font-bold')
        if not name_elem:
            continue
        
        event_name = name_elem.text(strip=True)
        
        # Get date/time from h2 tag
        datetime_elem = card.css_first('h2.text-gray-400.min-h-\\[1\\.5em\\]')
        if not datetime_elem:
            continue
        
        event_datetime = datetime_elem.text(strip=True)
        # Replace comma with dash
        event_datetime = event_datetime.replace(',', ' -')
        
        # Get image URL from img tag
        img_elem = card.css_first('img')
        img_url = ""
        if img_elem:
            srcset = img_elem.attributes.get('srcset', '')
            img_url = extract_image_url(srcset)
            if not img_url:
                img_url = img_elem.attributes.get('src', '')
        
        # Get watch link from a tag
        link_elem = card.css_first('a.block')
        if not link_elem:
            continue
        
        watch_path = link_elem.attributes.get('href', '')
        if not watch_path:
            continue
        
        watch_url = urljoin(BASE_URL, watch_path)
        
        # Create unique href identifier
        href_id = watch_path
        
        if href_id in cached_hrefs:
            continue
        
        # Combine category and event name with datetime
        full_event_name = f"{category} - {event_name} ({event_datetime})"
        
        events.append({
            "sport": category.upper().replace(' ', '_'),
            "category": category,
            "event": event_name,
            "datetime": event_datetime,
            "full_name": full_event_name,
            "link": watch_url,
            "href": href_id,
            "logo": img_url,
        })
    
    log.info(f"Found {len(events)} events on live-now page")
    return events


# -------------------------------------------------
# Build the final playlist text
# -------------------------------------------------
def build_playlist(data: dict[str, dict]) -> str:
    lines = ["#EXTM3U"]
    chno = 1
    
    for title, info in data.items():
        # Get referer and origin from stream URL or use default
        stream_url = info["url"]
        
        # Extract domain for referer/origin
        if 'serveplay' in stream_url:
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
        chno += 1
    
    return "\n".join(lines) + "\n"


# -------------------------------------------------
# Process event to get stream URL
# -------------------------------------------------
async def process_event(url: str, url_num: int) -> str | None:
    """Process event page to extract stream URL"""
    stream = await extract_stream_from_watch_page(url, url_num)
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
    
    if not events:
        log.info("No new events to process")
        # Still write playlist with cached events
        if urls:
            out = build_playlist(urls)
            OUTPUT_FILE.write_text(out, encoding="utf-8")
            log.info(f"Successfully wrote {len(urls)} entries to pits.m3u8 (from cache)")
        return
    
    now_ts = Time.clean(Time.now()).timestamp()
    
    for i, ev in enumerate(events, start=1):
        log.info(f"Processing event {i}/{len(events)}: {ev['full_name']}")
        
        handler = partial(process_event, ev["link"], i)
        
        stream = await network.safe_process(
            handler,
            url_num=i,
            semaphore=network.HTTP_S,
            log=log,
        )
        
        if not stream:
            log.warning(f"Event {i}) No stream found for: {ev['full_name']}")
            continue
        
        # Create title with datetime for uniqueness
        title = f"[{ev['sport']}] {ev['category']} - {ev['event']} ({ev['datetime']}) ({TAG})"
        
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
            "datetime": ev["datetime"],
        }
        
        log.info(f"Event {i}) ✓ Added: {ev['full_name']}")
    
    CACHE_FILE.write(urls)
    
    # Write playlist
    out = build_playlist(urls)
    OUTPUT_FILE.write_text(out, encoding="utf-8")
    log.info(f"Successfully wrote {len(urls)} entries to pits.m3u8")


# -------------------------------------------------
# Run scraper
# -------------------------------------------------
async def main():
    log.info("Starting PITS scraper")
    await scrape()
    log.info("PITS scraper completed")


if __name__ == "__main__":
    asyncio.run(main())
