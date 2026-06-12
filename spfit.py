#!/usr/bin/env python3

import asyncio
import re
from functools import partial
from urllib.parse import urljoin
from datetime import datetime

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
from selectolax.parser import HTMLParser

from utils import Cache, Time, get_logger, leagues, network

log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}

TAG = "SPFIT"

CACHE_FILE = Cache(TAG, exp=28_800)

BASE_URL = "https://streamseast.is"

# Output files
VLC_OUTPUT = "spfit_vlc.m3u8"
TIVIMATE_OUTPUT = "spfit_tivimate.m3u8"

# Headers (NOT encoded for TiviMate)
USER_AGENT = "Mozilla%2F5.0%20(Windows%20NT%2010.0%3B%20Win64%3B%20x64)%20AppleWebKit%2F537.36%20(KHTML%2C%20like%20Gecko)%20Chrome%2F120.0.0.0%20Safari%2F537.36"
REFERER = "https://sportspass.fit/"
ORIGIN = "https://sportspass.fit"

# Sport categories (using working URLs from original code)
SPORT_CATEGORIES = {
    "Soccer": "/soccer",
    "NBA": "/nba",
    "NHL": "/nhl",
    "MLB": "/mlb",
    "MMA": "/mma",
    "Boxing": "/boxing",
    "F1": "/f1",
}

# Concurrency settings
MAX_CONCURRENT_PAGES = 3
PAGE_TIMEOUT = 6000  # 6 seconds timeout (from working code)
RETRY_ATTEMPTS = 2


def clean_event_name(event_name: str) -> str:
    """Clean event name by removing commas and extra spaces"""
    if not event_name:
        return event_name
    
    cleaned = event_name.replace(",", "")
    cleaned = re.sub(r'\s+', ' ', cleaned)
    cleaned = re.sub(r'\s*-\s*(?:Live|Stream|Watch|SPFIT)\s*$', '', cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.strip()
    
    return cleaned if cleaned else "Live Event"


async def process_event(
    url: str,
    url_num: int,
    page,
) -> tuple[str | None, str | None, str | None]:
    """Process a single event page to extract m3u8 URL"""
    nones = (None, None, None)
    captured: list[str] = []
    got_one = asyncio.Event()
    handler = partial(
        network.capture_req,
        captured=captured,
        got_one=got_one,
    )
    
    page.on("request", handler)
    event_name = "Sporting Event"
    iframe_src = None
    
    try:
        resp = await page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=PAGE_TIMEOUT,
        )
        
        if not resp or resp.status != 200:
            log.warning(f"URL {url_num}) Status Code: {resp.status if resp else 'None'}")
            return (event_name, *nones)
        
        # Get event name from h1.match-head element
        try:
            event_name_elem = page.locator("h1.match-head")
            event_name = await event_name_elem.inner_text(timeout=1250)
            event_name = clean_event_name(event_name)
        except:
            pass
        
        # Find iframe
        try:
            ifr = page.locator("iframe.embed-responsive-item")
            await ifr.wait_for(timeout=1250)
            iframe_src = await ifr.get_attribute("src")
        except PlaywrightTimeoutError:
            log.warning(f"URL {url_num}) No iframe found.")
            return (event_name, *nones)
        
        # Navigate to iframe
        await page.goto(
            iframe_src,
            wait_until="domcontentloaded",
            timeout=2250,
        )
        
        # Wait for m3u8 capture
        wait_task = asyncio.create_task(got_one.wait())
        
        try:
            await asyncio.wait_for(wait_task, timeout=5)
        except asyncio.TimeoutError:
            log.warning(f"URL {url_num}) Timed out waiting for M3U8.")
            return (event_name, *nones)
        finally:
            if not wait_task.done():
                wait_task.cancel()
                try:
                    await wait_task
                except asyncio.CancelledError:
                    pass
        
        if captured:
            log.info(f"✓ [{url_num}] {event_name[:50]}")
            return event_name, iframe_src, captured[0]
        
        return (event_name, *nones)
        
    except Exception as e:
        log.warning(f"URL {url_num}) Error: {e}")
        return (event_name, *nones)
    finally:
        page.remove_listener("request", handler)


async def get_events(cached_links: set[str]) -> list[dict[str, str]]:
    """Get events from category pages"""
    now = Time.clean(Time.now())
    events = []
    
    for sport, category_path in SPORT_CATEGORIES.items():
        category_url = urljoin(BASE_URL, category_path)
        log.info(f"Scanning {sport}...")
        
        try:
            html = await network.request(category_url, log=log)
            if not html:
                log.warning(f"  Failed to fetch {category_url}")
                continue
            
            soup = HTMLParser(html.content)
            
            # Find event links using the working selector from original code
            for event in soup.css("a.matches"):
                if not (href := event.attributes.get("href")):
                    continue
                
                link = urljoin(BASE_URL, href)
                
                if link in cached_links:
                    continue
                
                # Check if event has script with date or status badge
                if (scr_elem := event.css_first("script")) and (
                    match := re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}Z", scr_elem.text(strip=True))
                ):
                    event_dt = Time.fromisoformat(match[0]).to_tz("EST")
                elif event.css_first("span.status-badge.badge.bg-success"):
                    event_dt = now
                else:
                    continue
                
                # Only get events within time range
                start_dt = now.delta(hours=-3)
                end_dt = now.delta(minutes=5)
                
                if not start_dt <= event_dt <= end_dt:
                    continue
                
                # Get event name from the link text
                event_name = event.text(strip=True)
                event_name = clean_event_name(event_name)
                
                events.append({
                    "sport": sport,
                    "link": link,
                    "name": event_name,
                    "timestamp": event_dt.timestamp(),
                })
                
        except Exception as e:
            log.error(f"Error scanning {sport}: {e}")
            continue
    
    return events


async def scrape() -> None:
    """Main scraping function using the working approach"""
    cached_urls = CACHE_FILE.load() or {}
    
    # Get cached links
    cached_links = {entry.get("link") for entry in cached_urls.values() if entry.get("link")}
    
    # Load valid cached URLs
    valid_urls = {k: v for k, v in cached_urls.items() if v.get("url")}
    urls.update(valid_urls)
    log.info(f"Loaded {len(valid_urls)} event(s) from cache")
    
    # Get new events
    events = await get_events(cached_links)
    
    if not events:
        log.info("No new events to process")
        return
    
    log.info(f"Processing {len(events)} new event(s)")
    
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=['--disable-blink-features=AutomationControlled', '--no-sandbox']
        )
        context = await browser.new_context(
            viewport={'width': 1920, 'height': 1080},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        
        now = Time.clean(Time.now())
        new_count = 0
        
        for i, ev in enumerate(events, start=1):
            page = await context.new_page()
            
            try:
                event, iframe_src, stream_url = await process_event(
                    ev["link"],
                    i,
                    page
                )
                
                if stream_url:
                    tvg_id, logo = leagues.get_tvg_info(ev["sport"], event or ev["name"])
                    
                    key = f"[{ev['sport']}] {event or ev['name']} ({TAG})"
                    
                    entry = {
                        "url": stream_url,
                        "logo": logo,
                        "base": iframe_src,
                        "timestamp": ev["timestamp"],
                        "id": tvg_id or "Live.Event.us",
                        "link": ev["link"],
                    }
                    
                    cached_urls[key] = entry
                    urls[key] = entry
                    new_count += 1
                    
            except Exception as e:
                log.warning(f"Error processing event {i}: {e}")
            finally:
                await page.close()
            
            # Small delay between requests
            await asyncio.sleep(0.5)
        
        await browser.close()
    
    # Save to cache
    CACHE_FILE.write(cached_urls)
    log.info(f"Collected {new_count} new streams, total: {len(urls)}")


def generate_playlists() -> None:
    """Generate VLC and TiviMate playlist files"""
    if not urls:
        log.warning("No events to generate playlists")
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
                
                clean_name = clean_event_name(event_name)
                
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
        with open(TIVIMATE_OUTPUT, "w", encoding="utf-8") as f:
            f.write(header)
            
            ch_no = 1
            for event_name, event_data in urls.items():
                url = event_data.get("url")
                logo = event_data.get("logo", "https://i.gyazo.com/1c4aa937f5ea01b0f29bb27adb59884c.png")
                tvg_id = event_data.get("id", "Live.Event.us")
                
                if not url:
                    continue
                
                clean_name = clean_event_name(event_name)
                
                f.write(f'#EXTINF:-1 tvg-chno="{ch_no}" tvg-id="{tvg_id}" tvg-name="{clean_name}" tvg-logo="{logo}" group-title="Live Events",{clean_name}\n')
                f.write(f'{url}|referer={REFERER}|origin={ORIGIN}|user-agent={USER_AGENT}\n\n')
                
                ch_no += 1
        
        log.info(f"Generated TiviMate playlist: {TIVIMATE_OUTPUT} with {ch_no - 1} streams")
    except Exception as e:
        log.error(f"Error generating TiviMate playlist: {e}")


async def main() -> None:
    """Main function"""
    log.info("Starting SPFIT playlist generator")
    
    try:
        await scrape()
        generate_playlists()
        
        print(f"\n SPFIT Playlists generated successfully!")
        print(f"    VLC: {VLC_OUTPUT}")
        print(f"    TiviMate: {TIVIMATE_OUTPUT}")
        print(f"    Total streams: {len(urls)}")
    except Exception as e:
        log.error(f"Error in main execution: {e}")
        raise


if __name__ == "__main__":
    asyncio.run(main())
