#!/usr/bin/env python3

import asyncio
from urllib.parse import urljoin, quote
from datetime import datetime
import re

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
from selectolax.parser import HTMLParser

from utils import Cache, Time, get_logger, leagues, network

log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}

TAG = "SPFIT"

CACHE_FILE = Cache(TAG, exp=28_800)

BASE_URL = "https://streamseast.biz/"

# Output files
VLC_OUTPUT = "spfit_vlc.m3u8"
TIVIMATE_OUTPUT = "spfit_tivimate.m3u8"

# Headers
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
REFERER = "https://sportspass.fit/"
ORIGIN = "https://sportspass.fit"

# Sport categories from the website
SPORT_CATEGORIES = {
    "Soccer": "/soccer",
    "NBA": "/nba",
    # "NFL": "/nfl",
    "NHL": "/nhl",
    "MLB": "/mlb",
    "MMA": "/mma",
    "Boxing": "/boxing",
    "F1": "/f1",
}


def clean_event_name(event_name: str) -> str:
    """Clean event name by removing commas and extra spaces"""
    if not event_name:
        return event_name
    
    # Remove commas
    cleaned = event_name.replace(",", "")
    
    # Remove extra spaces
    cleaned = re.sub(r'\s+', ' ', cleaned)
    
    # Remove common suffixes
    cleaned = re.sub(r'\s*-\s*(?:Live|Stream|Watch|SPFIT)\s*$', '', cleaned, flags=re.IGNORECASE)
    
    return cleaned.strip()


async def get_event_links_from_category(page, category_url: str) -> list[tuple[str, str]]:
    """Extract event links from category page"""
    events = []
    
    try:
        await page.goto(category_url, wait_until="networkidle", timeout=15000)
        
        # Wait for content to load
        await page.wait_for_timeout(2000)
        
        # Find all event links - try multiple selectors
        selectors = [
            'a.matches',
            'a.match-link', 
            'a[href*="/soccer/"]',
            'a[href*="/nba/"]',
            'a[href*="/nfl/"]',
            'a[href*="/nhl/"]',
            'a[href*="/mlb/"]',
            'a[href*="/mma/"]',
            'a[href*="/boxing/"]',
            'a[href*="/f1/"]',
            'a[href*="/event/"]',
            'a[href*="/stream/"]'
        ]
        
        for selector in selectors:
            links = await page.query_selector_all(selector)
            for link in links:
                href = await link.get_attribute('href')
                if href and ('/soccer/' in href or '/nba/' in href or '/nfl/' in href or 
                           '/nhl/' in href or '/mlb/' in href or '/mma/' in href or
                           '/boxing/' in href or '/f1/' in href):
                    
                    # Get full URL
                    if href.startswith('/'):
                        full_url = urljoin(BASE_URL, href)
                    else:
                        full_url = href
                    
                    # Get event name
                    event_name = await link.inner_text()
                    event_name = clean_event_name(event_name)
                    
                    if full_url not in [e[1] for e in events]:
                        events.append((event_name, full_url))
        
        # Remove duplicates
        events = list(dict.fromkeys(events))
        
    except Exception as e:
        log.error(f"Error getting events from {category_url}: {e}")
    
    return events


async def extract_m3u8_from_event(page, event_url: str, event_name: str, url_num: int) -> str | None:
    """Navigate to event page and extract m3u8 stream URL"""
    
    try:
        log.info(f"URL {url_num}) Processing: {event_name}")
        
        # Navigate to event page
        await page.goto(event_url, wait_until="domcontentloaded", timeout=15000)
        
        # Wait for page to load
        await page.wait_for_timeout(3000)
        
        # Try to find and click play button if exists
        play_selectors = [
            'button.play-btn',
            'button.vjs-big-play-button',
            '.play-button',
            '.video-js .vjs-big-play-button',
            'button:has-text("Play")',
            '.play-btn'
        ]
        
        for selector in play_selectors:
            try:
                play_button = await page.query_selector(selector)
                if play_button:
                    await play_button.click()
                    await page.wait_for_timeout(2000)
                    break
            except:
                pass
        
        # Set up network request monitoring
        m3u8_url = None
        
        def handle_request(request):
            nonlocal m3u8_url
            if '.m3u8' in request.url and not m3u8_url:
                m3u8_url = request.url
                log.info(f"URL {url_num}) Found m3u8: {m3u8_url}")
        
        page.on('request', handle_request)
        
        # Wait for m3u8 to load (check multiple times)
        for attempt in range(10):
            if m3u8_url:
                break
            await page.wait_for_timeout(2000)
            
            # Check if there's an iframe to navigate into
            iframes = await page.query_selector_all('iframe')
            for iframe in iframes:
                try:
                    src = await iframe.get_attribute('src')
                    if src and src.startswith('http'):
                        # Navigate to iframe
                        await page.goto(src, wait_until="domcontentloaded", timeout=10000)
                        await page.wait_for_timeout(3000)
                        
                        # Check for play button in iframe
                        for selector in play_selectors:
                            try:
                                play_button = await page.query_selector(selector)
                                if play_button:
                                    await play_button.click()
                                    await page.wait_for_timeout(2000)
                                    break
                            except:
                                pass
                except:
                    pass
            
            # Check page content for m3u8
            content = await page.content()
            m3u8_pattern = r'https?://[^\s"\']+\.m3u8[^\s"\']*'
            matches = re.findall(m3u8_pattern, content)
            if matches and not m3u8_url:
                m3u8_url = matches[0]
                log.info(f"URL {url_num}) Found m3u8 in page source")
                break
        
        page.remove_listener('request', handle_request)
        
        if m3u8_url:
            return m3u8_url
        else:
            log.warning(f"URL {url_num}) No m3u8 found for {event_name}")
            return None
            
    except Exception as e:
        log.error(f"URL {url_num}) Error processing {event_name}: {e}")
        return None


async def scrape() -> None:
    """Main scraping function"""
    cached_urls = CACHE_FILE.load() or {}
    
    # Load cached URLs
    valid_urls = {k: v for k, v in cached_urls.items() if v.get("url")}
    urls.update(valid_urls)
    log.info(f"Loaded {len(valid_urls)} event(s) from cache")
    
    # Get all event links from categories
    all_events = []
    
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=['--disable-blink-features=AutomationControlled'])
        context = await browser.new_context(
            viewport={'width': 1920, 'height': 1080},
            user_agent=USER_AGENT
        )
        page = await context.new_page()
        
        try:
            for sport, category_path in SPORT_CATEGORIES.items():
                category_url = urljoin(BASE_URL, category_path)
                log.info(f"Scanning {sport} - {category_url}")
                
                events = await get_event_links_from_category(page, category_url)
                log.info(f"Found {len(events)} events in {sport}")
                
                for event_name, event_url in events:
                    # Check if already in cache
                    key = f"[{sport}] {event_name} ({TAG})"
                    if key in cached_urls and cached_urls[key].get("url"):
                        log.info(f"Skipping cached event: {event_name}")
                        continue
                    
                    all_events.append({
                        "sport": sport,
                        "name": event_name,
                        "url": event_url,
                        "key": key
                    })
        finally:
            await browser.close()
    
    log.info(f"Total new events to process: {len(all_events)}")
    
    # Process each event
    now = Time.clean(Time.now())
    new_count = 0
    
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=['--disable-blink-features=AutomationControlled'])
        
        for idx, event in enumerate(all_events, 1):
            page = await browser.new_page()
            try:
                log.info(f"Processing {idx}/{len(all_events)}: {event['name']}")
                
                # Set up request interception
                m3u8_url = None
                
                def handle_request(request):
                    nonlocal m3u8_url
                    if '.m3u8' in request.url and not m3u8_url:
                        m3u8_url = request.url
                        log.info(f"Captured m3u8: {m3u8_url}")
                
                page.on('request', handle_request)
                
                # Navigate to event page
                await page.goto(event['url'], wait_until="domcontentloaded", timeout=15000)
                await page.wait_for_timeout(5000)
                
                # Try to find iframe and navigate
                iframes = await page.query_selector_all('iframe')
                for iframe in iframes:
                    try:
                        src = await iframe.get_attribute('src')
                        if src and ('player' in src or 'embed' in src or 'stream' in src):
                            await page.goto(src, wait_until="domcontentloaded", timeout=10000)
                            await page.wait_for_timeout(5000)
                            
                            # Try to click play button
                            play_buttons = await page.query_selector_all('button')
                            for btn in play_buttons:
                                btn_text = await btn.inner_text()
                                if 'play' in btn_text.lower():
                                    await btn.click()
                                    await page.wait_for_timeout(2000)
                                    break
                    except:
                        pass
                
                # Wait for m3u8 to load
                for attempt in range(15):
                    if m3u8_url:
                        break
                    await page.wait_for_timeout(2000)
                    
                    # Check page source
                    content = await page.content()
                    m3u8_pattern = r'https?://[^\s"\']+\.m3u8[^\s"\']*'
                    matches = re.findall(m3u8_pattern, content)
                    if matches:
                        m3u8_url = matches[0]
                        log.info(f"Found m3u8 in page source")
                        break
                
                page.remove_listener('request', handle_request)
                
                if m3u8_url:
                    tvg_id, logo = leagues.get_tvg_info(event['sport'], event['name'])
                    
                    entry = {
                        "url": m3u8_url,
                        "logo": logo,
                        "base": event['url'],
                        "timestamp": now.timestamp(),
                        "id": tvg_id or "Live.Event.us",
                        "link": event['url'],
                    }
                    
                    cached_urls[event['key']] = entry
                    urls[event['key']] = entry
                    new_count += 1
                    log.info(f"✓ Successfully captured stream for {event['name']}")
                else:
                    log.warning(f"✗ No stream found for {event['name']}")
                    
            except Exception as e:
                log.error(f"Error processing {event['name']}: {e}")
            finally:
                await page.close()
        
        await browser.close()
    
    # Save to cache
    CACHE_FILE.write(cached_urls)
    log.info(f"Collected {new_count} new streams, total: {len(urls)}")


def generate_playlists() -> None:
    """Generate VLC and TiviMate playlist files from collected events"""
    if not urls:
        log.warning("No events to generate playlists")
        # Create empty playlists
        with open(VLC_OUTPUT, "w", encoding="utf-8") as f:
            f.write("#EXTM3U\n# No events available\n")
        with open(TIVIMATE_OUTPUT, "w", encoding="utf-8") as f:
            f.write("#EXTM3U\n# No events available\n")
        return

    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    header = f'#EXTM3U x-tvg-url="https://epgshare01.online/epgshare01/epg_ripper_ALL_SOURCES1.xml.gz"\n# Last Updated: {ts}\n# Total Streams: {len(urls)}\n\n'

    # Generate VLC playlist
    try:
        with open(VLC_OUTPUT, "w", encoding="utf-8") as f:
            f.write(header)
            
            ch_no = 1
            for event_name, event_data in urls.items():
                url = event_data.get("url")
                logo = event_data.get("logo", "https://i.gyazo.com/1c4aa937f5ea01b0f29bb27adb59884c.png")
                tvg_id = event_data.get("id", "Live.Event.us")
                
                if not url:
                    continue
                
                # Clean event name
                clean_name = clean_event_name(event_name)
                
                # Write VLC format
                f.write(f'#EXTINF:-1 tvg-chno="{ch_no}" tvg-id="{tvg_id}" tvg-name="{clean_name}" tvg-logo="{logo}" group-title="Live Events",{clean_name}\n')
                f.write(f'#EXTVLCOPT:http-referrer={REFERER}\n')
                f.write(f'#EXTVLCOPT:http-origin={ORIGIN}\n')
                f.write(f'#EXTVLCOPT:http-user-agent={USER_AGENT}\n')
                f.write(f'{url}\n\n')
                
                ch_no += 1
        
        log.info(f"Generated VLC playlist: {VLC_OUTPUT} with {ch_no - 1} streams")
    except Exception as e:
        log.error(f"Error generating VLC playlist: {e}")

    # Generate TiviMate playlist
    try:
        ua_enc = quote(USER_AGENT, safe="")
        referer_enc = quote(REFERER, safe="")
        origin_enc = quote(ORIGIN, safe="")
        
        with open(TIVIMATE_OUTPUT, "w", encoding="utf-8") as f:
            f.write(header)
            
            ch_no = 1
            for event_name, event_data in urls.items():
                url = event_data.get("url")
                logo = event_data.get("logo", "https://i.gyazo.com/1c4aa937f5ea01b0f29bb27adb59884c.png")
                tvg_id = event_data.get("id", "Live.Event.us")
                
                if not url:
                    continue
                
                # Clean event name
                clean_name = clean_event_name(event_name)
                
                # Write TiviMate format
                f.write(f'#EXTINF:-1 tvg-chno="{ch_no}" tvg-id="{tvg_id}" tvg-name="{clean_name}" tvg-logo="{logo}" group-title="Live Events",{clean_name}\n')
                f.write(f'{url}|referer={referer_enc}|origin={origin_enc}|user-agent={ua_enc}\n\n')
                
                ch_no += 1
        
        log.info(f"Generated TiviMate playlist: {TIVIMATE_OUTPUT} with {ch_no - 1} streams")
    except Exception as e:
        log.error(f"Error generating TiviMate playlist: {e}")


async def main() -> None:
    """Main function to run the scraper and generate playlists"""
    log.info("Starting SPFIT playlist generator")
    
    try:
        # Scrape events
        await scrape()
        
        # Generate playlists
        generate_playlists()
        
        log.info("Playlist generation completed")
        print(f"\n SPFIT Playlists generated successfully!")
        print(f"    VLC: {VLC_OUTPUT}")
        print(f"    TiviMate: {TIVIMATE_OUTPUT}")
        print(f"    Total streams: {len(urls)}")
    except Exception as e:
        log.error(f"Error in main execution: {e}")
        print(f"\n Error: {e}")
        raise


if __name__ == "__main__":
    asyncio.run(main())
