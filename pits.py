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

CACHE_FILE = Cache(f"{TAG.lower()}.json", exp=10_800)
OUTPUT_FILE = Path("pits.m3u8")

# Encoded User-Agent for TiViMate pipe
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:146.0) "
    "Gecko/20100101 Firefox/146.0"
)
UA_ENC = quote_plus(UA)


# -------------------------------------------------
# Extract image URL from Next.js image config
# -------------------------------------------------
def extract_image_url(image_config: dict) -> str:
    """Extract full image URL from Next.js image configuration"""
    if not image_config:
        return "https://i.gyazo.com/4a5e9fa2525808ee4b65002b56d3450e.png"
    
    # Check if it's a string (direct URL)
    if isinstance(image_config, str):
        if image_config.startswith('http'):
            return image_config
        elif image_config.startswith('/'):
            return urljoin(BASE_URL, image_config)
    
    # Check for src or url in dict
    if isinstance(image_config, dict):
        src = image_config.get('src') or image_config.get('url')
        if src:
            if src.startswith('http'):
                return src
            elif src.startswith('/'):
                return urljoin(BASE_URL, src)
    
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
    
    # Look for iframe with embed in the page
    iframe_pattern = r'<iframe[^>]*src=["\']([^"\']+)[^>]*>'
    iframes = re.findall(iframe_pattern, r.text, re.IGNORECASE)
    
    for iframe_src in iframes:
        if 'embed' in iframe_src or 'stream' in iframe_src or 'player' in iframe_src:
            # Fetch iframe content
            iframe_url = iframe_src if iframe_src.startswith('http') else urljoin(watch_url, iframe_src)
            iframe_r = await network.request(iframe_url, headers={"Referer": watch_url}, log=log)
            if iframe_r:
                # Look for M3U8 in iframe content
                m3u8_pattern = r'(https?://[^\s"\']+\.m3u8[^\s"\']*)'
                match = re.search(m3u8_pattern, iframe_r.text, re.IGNORECASE)
                if match:
                    stream = match.group(1)
                    log.info(f"URL {url_num}) captured M3U8 from iframe")
                    return stream
    
    # Look for direct M3U8 in page
    m3u8_pattern = r'(https?://[^\s"\']+\.(?:m3u8|html)[^\s"\']*)'
    matches = re.findall(m3u8_pattern, r.text, re.IGNORECASE)
    
    for match in matches:
        if 'serveplay' in match or 'm3u8' in match:
            log.info(f"URL {url_num}) captured M3U8")
            return match
    
    # Look for JavaScript variables
    script_pattern = r'(?:var|const|let)\s+(?:url|src|source|stream|file|video|hls)\s*=\s*["\']([^"\']+\.(?:m3u8|html)[^"\']*)["\']'
    match = re.search(script_pattern, r.text, re.IGNORECASE)
    if match:
        stream = match.group(1)
        log.info(f"URL {url_num}) captured M3U8 from JS")
        return stream
    
    # Look for embed URLs in Next.js data
    embed_pattern = r'https?://[^\s"\']*serveplay[^\s"\']*\.html[^\s"\']*'
    matches = re.findall(embed_pattern, r.text, re.IGNORECASE)
    for match in matches:
        log.info(f"URL {url_num}) captured HTML stream")
        return match
    
    log.warning(f"URL {url_num}) no stream found on watch page")
    return None


# -------------------------------------------------
# Parse events from Next.js page data
# -------------------------------------------------
async def get_events(cached_hrefs: set[str]) -> list[dict[str, str]]:
    events = []
    
    r = await network.request(LIVE_NOW_URL, log=log)
    if not r:
        log.error("Failed to fetch live-now page")
        return events
    
    content = r.text
    
    # Extract JSON data from Next.js script tags
    # Look for pattern: self.__next_f.push([...]) containing event data
    json_pattern = r'self\.__next_f\.push\(\[[^,]*,\s*"([^"]*)"\]\)'
    json_matches = re.findall(json_pattern, content)
    
    all_events_data = []
    
    for json_str in json_matches:
        # Unescape the JSON string
        json_str = json_str.replace('\\"', '"').replace('\\\\', '\\')
        json_str = json_str.replace('\\n', '').replace('\\t', '')
        
        # Try to find event data patterns
        event_pattern = r'category":"([^"]+)".*?title":"([^"]+)".*?href":"([^"]+)".*?imageUrl":"([^"]*)"'
        matches = re.findall(event_pattern, json_str, re.DOTALL)
        
        for category, title, href, image_url in matches:
            if href and href not in cached_hrefs:
                all_events_data.append({
                    'category': category,
                    'title': title,
                    'href': href,
                    'image_url': image_url
                })
    
    # Also try to find event data from the embedded props
    props_pattern = r'\"props\":\{[^}]*\"pageProps\":\{[^}]*\"events\":\[(.*?)\]'
    props_match = re.search(props_pattern, content, re.DOTALL)
    if props_match:
        events_json = props_match.group(1)
        # Extract individual events
        event_pattern = r'\{[^}]*"category":"([^"]+)"[^}]*"title":"([^"]+)"[^}]*"href":"([^"]+)"[^}]*"imageUrl":"([^"]*)"[^}]*\}'
        matches = re.findall(event_pattern, events_json, re.DOTALL)
        for category, title, href, image_url in matches:
            if href and href not in cached_hrefs:
                all_events_data.append({
                    'category': category,
                    'title': title,
                    'href': href,
                    'image_url': image_url
                })
    
    # Process all found events
    for event_data in all_events_data:
        category = event_data['category']
        title = event_data['title']
        href = event_data['href']
        image_url = event_data['image_url']
        
        if not href:
            continue
        
        # Extract date/time from title if present
        datetime_str = ""
        time_match = re.search(r'(\d{1,2}\s+\w+\s+\d{4},\s+\d{1,2}:\d{2}\s+(?:AM|PM))', title)
        if time_match:
            datetime_str = time_match.group(1)
            datetime_str = datetime_str.replace(',', ' -')
            # Remove time from title
            title = title.replace(time_match.group(0), '').strip()
        
        # Build full event URL
        event_url = urljoin(BASE_URL, href)
        
        # Create unique identifier
        href_id = href
        
        # Build full event name
        full_event_name = f"{category} - {title}"
        if datetime_str:
            full_event_name += f" ({datetime_str})"
        
        # Build image URL
        if image_url and not image_url.startswith('http'):
            image_url = urljoin(BASE_URL, image_url)
        
        events.append({
            "sport": category.upper().replace(' ', '_'),
            "category": category,
            "event": title,
            "datetime": datetime_str,
            "full_name": full_event_name,
            "link": event_url,
            "href": href_id,
            "logo": image_url,
        })
    
    # Remove duplicates by href
    seen = set()
    unique_events = []
    for event in events:
        if event['href'] not in seen:
            seen.add(event['href'])
            unique_events.append(event)
    
    log.info(f"Found {len(unique_events)} events on live-now page")
    return unique_events


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
        title = f"[{ev['sport']}] {ev['category']} - {ev['event']}"
        if ev['datetime']:
            title += f" ({ev['datetime']})"
        title += f" ({TAG})"
        
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
        new_events_count += 1
        log.info(f"Event {i}) ✓ Added: {ev['full_name'][:60]}...")
    
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
