import asyncio
import re
import json
from functools import partial
from pathlib import Path
from urllib.parse import quote, quote_plus, urljoin

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
# Extract stream URL from watch page
# -------------------------------------------------
async def extract_stream_from_watch_page(watch_url: str, url_num: int) -> str | None:
    """Extract M3U8/stream URL from watch page"""
    r = await network.request(watch_url, log=log)
    if not r:
        log.warning(f"URL {url_num}) failed to load watch page")
        return None
    
    content = r.text
    
    # Look for iframe in the page
    iframe_pattern = r'<iframe[^>]*src=["\']([^"\']+)["\']'
    iframes = re.findall(iframe_pattern, content, re.IGNORECASE)
    
    for iframe_src in iframes:
        if 'embed' in iframe_src or 'stream' in iframe_src or 'player' in iframe_src:
            iframe_url = iframe_src if iframe_src.startswith('http') else urljoin(watch_url, iframe_src)
            iframe_r = await network.request(iframe_url, headers={"Referer": watch_url}, log=log)
            if iframe_r:
                # Look for M3U8 in iframe
                m3u8 = re.search(r'(https?://[^\s"\']+\.m3u8[^\s"\']*)', iframe_r.text, re.IGNORECASE)
                if m3u8:
                    stream = m3u8.group(1)
                    log.info(f"URL {url_num}) captured M3U8 from iframe")
                    return stream
                
                # Look for HTML stream
                html = re.search(r'(https?://[^\s"\']+\.html[^\s"\']*)', iframe_r.text, re.IGNORECASE)
                if html:
                    stream = html.group(1)
                    log.info(f"URL {url_num}) captured HTML stream from iframe")
                    return stream
    
    # Look for Next.js data containing stream URL
    # Pattern for the stream URL in Next.js props
    stream_patterns = [
        r'streamUrl["\']\s*:\s*["\']([^"\']+)["\']',
        r'stream_url["\']\s*:\s*["\']([^"\']+)["\']',
        r'url["\']\s*:\s*["\'](https?://[^\s"\']+serveplay[^\s"\']+\.html)',
        r'url["\']\s*:\s*["\'](https?://[^\s"\']+serveplay[^\s"\']+\.m3u8)',
        r'(https?://[^\s"\']+serveplay[^\s"\']+\.(?:m3u8|html))',
    ]
    
    for pattern in stream_patterns:
        match = re.search(pattern, content, re.IGNORECASE)
        if match:
            stream = match.group(1)
            log.info(f"URL {url_num}) captured stream from Next.js data")
            return stream
    
    # Look for embed URLs in the page
    embed_patterns = [
        r'(https?://[^\s"\']+serveplay[^\s"\']+\.html[^\s"\']*)',
        r'(https?://[^\s"\']+serveplay[^\s"\']+\.m3u8[^\s"\']*)',
        r'(https?://[^\s"\']+\.serveplay[^\s"\']+\.(?:m3u8|html))',
    ]
    
    for pattern in embed_patterns:
        matches = re.findall(pattern, content, re.IGNORECASE)
        for match in matches:
            if 'serveplay' in match:
                log.info(f"URL {url_num}) captured stream from embed pattern")
                return match
    
    log.warning(f"URL {url_num}) no stream found on watch page")
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
    # Pattern: href="/watch/{uuid}"
    watch_pattern = r'href=["\']/watch/([a-f0-9-]+)["\']'
    watch_ids = set(re.findall(watch_pattern, content, re.IGNORECASE))
    
    # Also look for Next.js data containing event info
    # Extract category and title from the page
    event_info_pattern = r'category":"([^"]+)".*?title":"([^"]+)"'
    info_matches = re.findall(event_info_pattern, content, re.DOTALL)
    
    # Create a mapping of categories and titles (order may not match)
    event_details = []
    for category, title in info_matches:
        event_details.append({
            'category': category,
            'title': title
        })
    
    # Match watch IDs with their details
    for i, watch_id in enumerate(watch_ids):
        event_url = f"{WATCH_BASE}/{watch_id}"
        
        # Try to get event details
        category = "Unknown"
        title = "Event"
        
        if i < len(event_details):
            category = event_details[i].get('category', 'Unknown')
            title = event_details[i].get('title', 'Event')
        else:
            # Try to extract from the page around this watch ID
            # Find the context around this watch ID
            context_pattern = rf'href=["\']/watch/{watch_id}["\'][^>]*>.*?<p[^>]*class="[^"]*text-gray-500[^"]*"[^>]*>([^<]+)</p>.*?<h1[^>]*>([^<]+)</h1>'
            context_match = re.search(context_pattern, content, re.DOTALL)
            if context_match:
                category = context_match.group(1).strip()
                title = context_match.group(2).strip()
        
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
        
        # Build full event name
        full_event_name = f"{category} - {title}"
        
        # Build sport name for group
        sport = category.upper().replace(' ', '_')
        
        events.append({
            "sport": sport,
            "category": category,
            "event": title,
            "datetime": "",
            "full_name": full_event_name,
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
    
    if not events and not urls:
        log.info("No events found and no cached events")
        return
    
    now_ts = Time.clean(Time.now()).timestamp()
    new_events_count = 0
    
    for i, ev in enumerate(events, start=1):
        log.info(f"Processing event {i}/{len(events)}: {ev['full_name'][:80]}...")
        
        handler = partial(process_event, ev["link"], i)
        
        stream = await network.safe_process(
            handler,
            url_num=i,
            semaphore=network.HTTP_S,
            log=log,
        )
        
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
