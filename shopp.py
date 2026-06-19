#!/usr/bin/env python3

from utils import Cache, Time, get_logger, leagues, network
from datetime import datetime
from urllib.parse import quote, urljoin
import re
import asyncio
from bs4 import BeautifulSoup

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


async def get_events_from_main_page() -> list[dict]:
    """Scrape the main page for event cards and extract event info"""
    events = []
    
    try:
        if not (r := await network.request(MAIN_URL, headers=SCRAPE_HEADERS, log=log)):
            return events
            
        soup = BeautifulSoup(r.text, 'html.parser')
        
        # Find all event cards
        event_cards = soup.find_all('a', class_='event-card')
        
        for card in event_cards:
            try:
                # Extract event name from h3
                h3 = card.find('h3')
                if not h3:
                    continue
                    
                event_name = h3.get_text(strip=True)
                
                # Get event link
                event_url = card.get('href')
                if not event_url:
                    continue
                    
                # Make full URL
                full_url = urljoin(BASE_URL, event_url)
                
                # Get start and end times from data attributes
                start_time = card.get('data-start')
                end_time = card.get('data-end')
                
                events.append({
                    'name': event_name,
                    'url': full_url,
                    'start': start_time,
                    'end': end_time,
                    'sport': 'Live Event'
                })
                
            except Exception as e:
                log.debug(f"Error parsing event card: {e}")
                continue
                
    except Exception as e:
        log.error(f"Error scraping main page: {e}")
        
    return events


async def extract_streams_from_event_page(event_url: str) -> list[str]:
    """Extract m3u8 stream URLs from an event page"""
    streams = []
    
    try:
        if not (r := await network.request(event_url, headers=SCRAPE_HEADERS, log=log)):
            return streams
            
        soup = BeautifulSoup(r.text, 'html.parser')
        
        # Look for m3u8 URLs in the page
        # Method 1: Find all script tags that might contain stream URLs
        scripts = soup.find_all('script')
        for script in scripts:
            if script.string:
                # Look for m3u8 patterns
                m3u8_patterns = re.findall(r'https?://[^\s"\']+\.m3u8[^\s"\']*', script.string)
                streams.extend(m3u8_patterns)
                
                # Look for streamxyz.shop patterns
                stream_patterns = re.findall(r'https?://streamxyz\.shop/[^/\s"\']+/index\.m3u8[^\s"\']*', script.string)
                streams.extend(stream_patterns)
        
        # Method 2: Look for select options with stream URLs
        selects = soup.find_all('select', class_='feed-select')
        for select in selects:
            options = select.find_all('option')
            for option in options:
                value = option.get('value', '')
                if '.m3u8' in value:
                    streams.append(value)
        
        # Method 3: Look for direct stream links
        links = soup.find_all('a', href=True)
        for link in links:
            href = link.get('href', '')
            if '.m3u8' in href:
                full_url = urljoin(BASE_URL, href)
                streams.append(full_url)
        
        # Method 4: Look in the page HTML for stream URLs
        html_content = r.text
        m3u8_urls = re.findall(r'https?://[^\s"\']+\.m3u8[^\s"\']*', html_content)
        streams.extend(m3u8_urls)
        
        # Clean and deduplicate streams
        unique_streams = []
        seen = set()
        for stream in streams:
            if stream not in seen:
                seen.add(stream)
                unique_streams.append(stream)
                
        log.info(f"Found {len(unique_streams)} streams on {event_url}")
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
            
            log.info(f"Processing event: {event_name}")
            
            # Extract streams from event page
            streams = await extract_streams_from_event_page(event_url)
            
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
