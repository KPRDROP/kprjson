#!/usr/bin/env python3

import asyncio
from functools import partial
from urllib.parse import urljoin, quote
from datetime import datetime
import re

from playwright.async_api import async_playwright, Browser, Page, TimeoutError
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
USER_AGENT = "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Mobile Safari/537.36"
REFERER = "https://sportspass.fit/"
ORIGIN = "https://sportspass.fit"

# Sport categories from the website
SPORT_CATEGORIES = {
    "Soccer": "/soccer",
    "NBA": "/nba",
    "NFL": "/nfl",
    "NHL": "/nhl",
    "MLB": "/mlb",
    "MMA": "/mma",
    "Boxing": "/boxing",
    "F1": "/f1",
}

SPORT_URLS = {sport: urljoin(BASE_URL, path) for sport, path in SPORT_CATEGORIES.items()}


def clean_event_name(event_name: str) -> str:
    """Remove commas from event names"""
    if not event_name:
        return event_name
    
    # Remove commas
    cleaned = event_name.replace(",", "")
    
    # Remove extra spaces
    cleaned = re.sub(r'\s+', ' ', cleaned)
    
    return cleaned.strip()


async def extract_m3u8_from_page(page: Page, url_num: int) -> tuple[str | None, str | None]:
    """Extract m3u8 URL from page by monitoring network requests"""
    captured: list[str] = []
    got_one = asyncio.Event()
    
    def handle_request(request):
        url = request.url
        if '.m3u8' in url and url not in captured:
            captured.append(url)
            got_one.set()
    
    page.on("request", handle_request)
    
    try:
        # Navigate to the page
        await page.goto(url, wait_until="domcontentloaded", timeout=10000)
        
        # Wait for m3u8 request
        try:
            await asyncio.wait_for(got_one.wait(), timeout=15)
            if captured:
                return captured[0], None
        except asyncio.TimeoutError:
            pass
        
        # Try to find iframe
        iframes = await page.query_selector_all('iframe')
        for iframe in iframes:
            src = await iframe.get_attribute('src')
            if src and src.startswith('http'):
                # Navigate to iframe
                await page.goto(src, wait_until="domcontentloaded", timeout=10000)
                try:
                    await asyncio.wait_for(got_one.wait(), timeout=10)
                    if captured:
                        return captured[0], src
                except asyncio.TimeoutError:
                    pass
        
        # Try to find video source
        video_sources = await page.query_selector_all('video source')
        for source in video_sources:
            src = await source.get_attribute('src')
            if src and '.m3u8' in src:
                return src, None
        
        # Search page content for m3u8
        content = await page.content()
        m3u8_pattern = r'https?://[^\s"\']+\.m3u8[^\s"\']*'
        matches = re.findall(m3u8_pattern, content)
        if matches:
            return matches[0], None
            
    except Exception as e:
        log.warning(f"URL {url_num}) Error: {e}")
    finally:
        page.remove_listener("request", handle_request)
    
    return None, None


async def process_event(
    url: str,
    url_num: int,
    page: Page,
) -> tuple[str | None, str | None, str | None]:

    event_name = "Sporting Event"
    
    try:
        # Navigate to event page
        resp = await page.goto(url, wait_until="domcontentloaded", timeout=10000)
        
        if not resp or resp.status != 200:
            log.warning(f"URL {url_num}) Status Code: {resp.status if resp else 'None'}")
            return (event_name, None, None)
        
        # Try to get event name from different selectors
        selectors = ['h1.match-head', 'h1', '.event-title', '.match-title', 'title']
        for selector in selectors:
            try:
                elem = page.locator(selector).first
                if await elem.count() > 0:
                    event_name = await elem.inner_text(timeout=2000)
                    event_name = clean_event_name(event_name)
                    if event_name and len(event_name) > 3:
                        break
            except:
                continue
        
        if not event_name or event_name == "Sporting Event":
            # Extract from URL
            event_name = url.split('/')[-1].replace('.html', '').replace('-', ' ').title()
            event_name = clean_event_name(event_name)
        
        # Extract m3u8 URL
        m3u8_url, iframe_src = await extract_m3u8_from_page(page, url_num)
        
        if m3u8_url:
            log.info(f"URL {url_num}) Captured M3U8: {event_name}")
            return event_name, iframe_src or url, m3u8_url
        else:
            log.warning(f"URL {url_num}) No m3u8 found for {event_name}")
            
    except Exception as e:
        log.warning(f"URL {url_num}) Error: {e}")
    
    return (event_name, None, None)


async def get_category_events(browser: Browser, category: str, category_url: str, cached_links: set[str]) -> list[dict[str, str]]:
    """Get events from a specific category page"""
    events = []
    page = None
    
    try:
        page = await browser.new_page()
        await page.goto(category_url, wait_until="domcontentloaded", timeout=15000)
        
        # Find all event links
        event_links = await page.query_selector_all('a.matches, a.match-link, a.event-link, a[href*="/stream/"], a[href*="/event/"]')
        
        for link in event_links:
            href = await link.get_attribute('href')
            if not href:
                continue
            
            # Get full URL
            if href.startswith('/'):
                full_url = urljoin(BASE_URL, href)
            elif href.startswith('http'):
                full_url = href
            else:
                full_url = urljoin(category_url, href)
            
            # Skip if already processed
            if full_url in cached_links:
                continue
            
            # Get event text
            event_text = await link.inner_text()
            event_text = clean_event_name(event_text.strip())
            
            events.append({
                "sport": category,
                "link": full_url,
                "name": event_text
            })
        
        await page.close()
        
    except Exception as e:
        log.warning(f"Error processing category {category}: {e}")
        if page:
            await page.close()
    
    return events


async def get_events(browser: Browser, cached_links: set[str]) -> list[dict[str, str]]:
    """Get all events from all categories"""
    all_events = []
    
    for sport, url in SPORT_URLS.items():
        log.info(f"Scanning category: {sport}")
        events = await get_category_events(browser, sport, url, cached_links)
        all_events.extend(events)
        log.info(f"Found {len(events)} events in {sport}")
    
    return all_events


async def scrape(browser: Browser) -> None:
    cached_urls = CACHE_FILE.load() or {}
    
    cached_links = {entry.get("link", "") for entry in cached_urls.values() if entry.get("link")}
    
    valid_urls = {k: v for k, v in cached_urls.items() if v.get("url")}
    
    valid_count = cached_count = len(valid_urls)
    
    urls.update(valid_urls)
    
    log.info(f"Loaded {cached_count} event(s) from cache")
    log.info(f'Scraping from "{BASE_URL}"')
    
    events = await get_events(browser, cached_links)
    
    if events:
        log.info(f"Processing {len(events)} new URL(s)")
        
        now = Time.clean(Time.now())
        
        for i, ev in enumerate(events, start=1):
            page = await browser.new_page()
            try:
                event_name, iframe_src, m3u8_url = await process_event(
                    ev["link"], 
                    i, 
                    page
                )
                
                if m3u8_url:
                    tvg_id, logo = leagues.get_tvg_info(ev["sport"], event_name)
                    
                    key = f"[{ev['sport']}] {event_name} ({TAG})"
                    
                    entry = {
                        "url": m3u8_url,
                        "logo": logo,
                        "base": iframe_src or ev["link"],
                        "timestamp": now.timestamp(),
                        "id": tvg_id or "Live.Event.us",
                        "link": ev["link"],
                    }
                    
                    cached_urls[key] = entry
                    valid_count += 1
                    urls[key] = entry
                    log.info(f"Collected: {key}")
                    
            except Exception as e:
                log.warning(f"Error processing event {i}: {e}")
            finally:
                await page.close()
        
        log.info(f"Collected and cached {valid_count - cached_count} new event(s)")
    else:
        log.info("No new events found")
    
    CACHE_FILE.write(cached_urls)


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
                base_url = event_data.get("base", REFERER)
                
                if not url:
                    continue
                
                # Clean event name (remove commas)
                clean_name = clean_event_name(event_name)
                
                # Write VLC format with EXTVLCOPT lines
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
                
                # Clean event name (remove commas)
                clean_name = clean_event_name(event_name)
                
                # Write TiviMate format with pipe-separated headers
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
        # Launch playwright browser
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                await scrape(browser)
            finally:
                await browser.close()
        
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
