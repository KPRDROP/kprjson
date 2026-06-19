#!/usr/bin/env python3

from utils import Cache, Time, get_logger, leagues, network
from datetime import datetime
from urllib.parse import quote, urljoin
import re
import asyncio
import cloudscraper

log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}

TAG = "SHOPP"

CACHE_FILE = Cache(TAG, exp=28_800)

BASE_URL = "https://xyzstreams.shop/"
MAIN_URL = "https://xyzstreams.shop/"

# Output files
VLC_OUTPUT = "shopp_vlc.m3u8"
TIVIMATE_OUTPUT = "shopp_tivimate.m3u8"

# Headers for streams
REFERER = "https://xyzstreams.shop"
ORIGIN = "https://xyzstreams.shop"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:151.0) "
    "Gecko/20100101 Firefox/151.0"
)

TIVIMATE_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:151.0) "
    "Gecko/20100101 Firefox/151.0"
)

# Browser headers for scraping
SCRAPE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
    "Host": "xyzstreams.shop",
    "Referer": "https://xyzstreams.shop/",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Upgrade-Insecure-Requests": "1",
    "User-Agent": USER_AGENT,
}


async def fetch_html_with_cloudscraper(url: str) -> str | None:
    """Fetch HTML using cloudscraper to bypass Cloudflare"""
    try:
        scraper = cloudscraper.create_scraper(
            browser={
                'browser': 'firefox',
                'platform': 'windows',
                'mobile': False
            }
        )
        response = scraper.get(url, headers=SCRAPE_HEADERS, timeout=30)
        if response.status_code == 200:
            return response.text
        else:
            log.error(f"Cloudscraper failed with status {response.status_code}")
            return None
    except Exception as e:
        log.error(f"Cloudscraper error: {e}")
        return None


async def get_events_from_main_page() -> list[dict]:
    """Scrape the main page for event cards and extract event info"""
    events = []
    
    try:
        # Try cloudscraper first
        html_content = await fetch_html_with_cloudscraper(MAIN_URL)
        if not html_content:
            # Fallback to network module
            if not (r := await network.request(MAIN_URL, headers=SCRAPE_HEADERS, log=log)):
                return events
            html_content = r.text
        
        # Use regex to find event cards since BeautifulSoup might miss dynamic content
        # Find all event cards with href and class
        event_pattern = r'<a\s+href="([^"]+)"\s+class="event-card"[^>]*data-start="([^"]*)"[^>]*data-end="([^"]*)"[^>]*>.*?<h3>([^<]+)</h3>'
        matches = re.findall(event_pattern, html_content, re.DOTALL)
        
        if not matches:
            # Try alternative pattern without data attributes
            event_pattern2 = r'<a\s+href="([^"]+)"\s+class="event-card"[^>]*>.*?<h3>([^<]+)</h3>'
            matches2 = re.findall(event_pattern2, html_content, re.DOTALL)
            for match in matches2:
                events.append({
                    'name': match[1].strip(),
                    'url': urljoin(BASE_URL, match[0]),
                    'start': None,
                    'end': None,
                    'sport': 'Live Event'
                })
        else:
            for match in matches:
                events.append({
                    'name': match[3].strip(),
                    'url': urljoin(BASE_URL, match[0]),
                    'start': match[1],
                    'end': match[2],
                    'sport': 'Live Event'
                })
        
        log.info(f"Found {len(events)} events on main page")
        return events
        
    except Exception as e:
        log.error(f"Error scraping main page: {e}")
        return []


async def extract_streams_from_event_page(event_url: str, event_name: str) -> list[str]:
    """Extract m3u8 stream URLs from an event page"""
    streams = []
    
    try:
        # Try cloudscraper first
        html_content = await fetch_html_with_cloudscraper(event_url)
        if not html_content:
            # Fallback to network module
            if not (r := await network.request(event_url, headers=SCRAPE_HEADERS, log=log)):
                return streams
            html_content = r.text
        
        # Method 1: Look for channel names and construct m3u8 URLs
        # Common channel patterns from the event page
        channel_patterns = [
            r'FOX\s*4K', r'FOX', r'BBC', r'TSN', r'Telemundo',
            r'beIN\s*Max', r'D\s*Sports'
        ]
        
        for pattern in channel_patterns:
            if re.search(pattern, html_content, re.IGNORECASE):
                # Construct m3u8 URL from channel name
                channel_name = re.search(pattern, html_content, re.IGNORECASE).group(0)
                channel_clean = re.sub(r'[^a-zA-Z0-9]', '', channel_name).upper()
                if channel_clean:
                    m3u8_url = f"https://streamxyz.shop/{channel_clean}/index.m3u8"
                    streams.append(m3u8_url)
        
        # Method 2: Look for direct m3u8 URLs
        m3u8_patterns = re.findall(r'https?://[^\s"\']+\.m3u8[^\s"\']*', html_content)
        streams.extend(m3u8_patterns)
        
        # Method 3: Look for streamxyz.shop patterns
        stream_patterns = re.findall(r'https?://streamxyz\.shop/[^/\s"\']+/index\.m3u8[^\s"\']*', html_content)
        streams.extend(stream_patterns)
        
        # Method 4: Look for select options with stream URLs
        select_pattern = r'<select[^>]*class="[^"]*feed-select[^"]*"[^>]*>.*?</select>'
        select_matches = re.findall(select_pattern, html_content, re.DOTALL)
        for select_html in select_matches:
            option_pattern = r'<option[^>]*value="([^"]+\.m3u8[^"]*)"[^>]*>'
            option_matches = re.findall(option_pattern, select_html)
            streams.extend(option_matches)
        
        # Method 5: Look for video source URLs
        source_pattern = r'<source[^>]+src="([^"]+\.m3u8[^"]*)"[^>]*>'
        source_matches = re.findall(source_pattern, html_content)
        streams.extend(source_matches)
        
        # Method 6: Look for data attributes containing stream URLs
        data_pattern = r'data-(?:src|url|stream|video)="([^"]+\.m3u8[^"]*)"'
        data_matches = re.findall(data_pattern, html_content)
        streams.extend(data_matches)
        
        # Clean and deduplicate streams
        unique_streams = []
        seen = set()
        for stream in streams:
            stream = stream.strip()
            if stream and stream not in seen:
                seen.add(stream)
                unique_streams.append(stream)
        
        if unique_streams:
            log.info(f"Found {len(unique_streams)} streams for {event_name}")
        else:
            # If no streams found, try to construct from common channel names
            log.warning(f"No streams found for {event_name}, trying fallback")
            # Try to extract from the page title or content
            title_match = re.search(r'<title>(.*?)</title>', html_content, re.IGNORECASE)
            if title_match:
                title = title_match.group(1)
                # Look for common channel names in title
                for channel in ['FOX', 'BBC', 'ESPN', 'TNT', 'NBC', 'CBS', 'ABC']:
                    if channel in title.upper():
                        fallback_url = f"https://streamxyz.shop/{channel}/index.m3u8"
                        if fallback_url not in unique_streams:
                            unique_streams.append(fallback_url)
        
        return unique_streams
        
    except Exception as e:
        log.error(f"Error extracting streams from {event_url}: {e}")
        return []


async def get_events() -> dict[str, dict[str, str | float]]:
    """Main function to get events and their streams"""
    events = {}
    
    # Step 1: Get events from main page
    main_events = await get_events_from_main_page()
    
    if not main_events:
        log.warning("No events found on main page")
        return events
    
    log.info(f"Found {len(main_events)} events on main page")
    
    # Step 2: For each event, extract streams
    for event_info in main_events:
        try:
            event_name = event_info['name']
            event_url = event_info['url']
            
            log.info(f"Processing event: {event_name} ({event_url})")
            
            # Extract streams from event page
            streams = await extract_streams_from_event_page(event_url, event_name)
            
            if not streams:
                log.warning(f"No streams found for {event_name}")
                continue
            
            # Get sport info for TVG ID and logo
            sport = event_info.get('sport', 'Live Event')
            tvg_id, logo = leagues.get_tvg_info(sport, event_name)
            
            # Add each stream as a separate entry
            for i, stream_url in enumerate(streams, start=1):
                key = f"[{sport}] {event_name} Stream {i} ({TAG})"
                
                events[key] = {
                    "url": stream_url,
                    "logo": logo,
                    "base": BASE_URL,
                    "timestamp": Time.now().timestamp(),
                    "id": tvg_id or f"stream_{i}",
                }
                
            log.info(f"Added {len(streams)} streams for {event_name}")
            
        except Exception as e:
            log.error(f"Error processing event {event_info.get('name', 'Unknown')}: {e}")
            continue
    
    return events


async def scrape() -> None:
    """Scrape events from website"""
    if cached := CACHE_FILE.load():
        urls.update(cached)
        log.info(f"Loaded {len(urls)} event(s) from cache")
        return
    
    log.info('Scraping from "xyzstreams"')
    
    events = await get_events()
    
    if events:
        urls.update(events)
        log.info(f"Collected and cached {len(urls)} new event(s)")
        CACHE_FILE.write(urls)
    else:
        log.warning("No events found")


def generate_playlists() -> None:
    """Generate VLC and TiviMate playlist files"""
    
    if not urls:
        log.warning("No events to generate playlists")
        return
        
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    
    header = (
        '#EXTM3U x-tvg-url="https://epgshare01.online/'
        'epgshare01/epg_ripper_ALL_SOURCES1.xml.gz"\n'
        f"# Last Updated: {ts}\n\n"
    )
    
    # VLC PLAYLIST
    with open(VLC_OUTPUT, "w", encoding="utf-8") as f:
        f.write(header)
        ch_no = 1
        
        for event_name, event_data in urls.items():
            url = event_data.get("url")
            logo = event_data.get(
                "logo",
                "https://i.gyazo.com/4a5e9fa2525808ee4b65002b56d3450e.png",
            )
            tvg_id = event_data.get("id", "Live.Event.us")
            
            if not url:
                continue
                
            f.write(
                f'#EXTINF:-1 tvg-chno="{ch_no}" '
                f'tvg-id="{tvg_id}" '
                f'tvg-name="{event_name}" '
                f'tvg-logo="{logo}" '
                f'group-title="Live Events",{event_name}\n'
            )
            f.write(f"#EXTVLCOPT:http-referrer={REFERER}\n")
            f.write(f"#EXTVLCOPT:http-origin={ORIGIN}\n")
            f.write(f"#EXTVLCOPT:http-user-agent={USER_AGENT}\n")
            f.write(f"{url}\n\n")
            ch_no += 1
            
    log.info(f"Generated VLC playlist: {VLC_OUTPUT} with {ch_no - 1} streams")
    
    # TIVIMATE PLAYLIST
    ua_enc = quote(TIVIMATE_USER_AGENT, safe="")
    
    with open(TIVIMATE_OUTPUT, "w", encoding="utf-8") as f:
        f.write(header)
        ch_no = 1
        
        for event_name, event_data in urls.items():
            url = event_data.get("url")
            logo = event_data.get(
                "logo",
                "https://i.gyazo.com/4a5e9fa2525808ee4b65002b56d3450e.png",
            )
            tvg_id = event_data.get("id", "Live.Event.us")
            
            if not url:
                continue
                
            f.write(
                f'#EXTINF:-1 tvg-chno="{ch_no}" '
                f'tvg-id="{tvg_id}" '
                f'tvg-name="{event_name}" '
                f'tvg-logo="{logo}" '
                f'group-title="Live Events",{event_name}\n'
            )
            f.write(
                f'{url}'
                f'|referer={REFERER}'
                f'|origin={ORIGIN}'
                f'|user-agent={ua_enc}\n\n'
            )
            ch_no += 1
            
    log.info(f"Generated TiviMate playlist: {TIVIMATE_OUTPUT} with {ch_no - 1} streams")


async def main() -> None:
    """Run updater and generate playlists"""
    log.info("Starting SHOPP playlist generator")
    await scrape()
    generate_playlists()
    log.info("Playlist generation completed")
    
    print("\nPlaylists generated successfully!")
    print(f"VLC: {VLC_OUTPUT}")
    print(f"TiviMate: {TIVIMATE_OUTPUT}")
    print(f"Total streams: {len(urls)}")


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
