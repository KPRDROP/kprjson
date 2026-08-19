#!/usr/bin/env python3

import asyncio
from collections.abc import KeysView
from functools import partial
from urllib.parse import urljoin, quote
import os
from pathlib import Path

from playwright.async_api import async_playwright, Browser, Page

from utils import Cache, Event, Time, get_logger, leagues, network

log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}

# ================= CONFIG =================

TAG = "EMBED"

CACHE_FILE = Cache(TAG, exp=5_400)
API_FILE = Cache(f"{TAG}-api", exp=28_800)

BASE_URL = os.environ.get("EMBED_BASE_URL")
if not BASE_URL:
    raise RuntimeError("Missing EMBED_BASE_URL secret")

REFERER = "https://exposestrat.com/"
ORIGIN = "https://exposestrat.com"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

UA_ENC = quote(USER_AGENT, safe="")

OUT_VLC = Path("embed_vlc.m3u8")
OUT_TIVI = Path("embed_tivimate.m3u8")

# ================= HELPERS =================

def fix_league(s: str) -> str:
    """Fix league name formatting."""
    splits = s.split()
    if not splits:
        return s
    
    i = splits[0]
    return f"{i.upper() if len(i) < 4 else i.capitalize()} {' '.join(x.capitalize() for x in splits[1:])}".strip()


def clean_display_name(name: str) -> str:
    """
    Clean display name by removing commas and extra spaces.
    
    Args:
        name: Display name
        
    Returns:
        Cleaned display name
    """
    if not name:
        return ""
    # Remove commas but keep the text around them
    import re
    cleaned = re.sub(r',\s*', ' ', name)
    # Remove extra spaces
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned


def encode_user_agent(user_agent: str) -> str:
    """
    Encode the user agent for URL parameters.
    
    Args:
        user_agent: User agent string
        
    Returns:
        URL-encoded user agent
    """
    # URL encode the user agent
    encoded = user_agent.replace(' ', '%20')
    encoded = encoded.replace('(', '%28')
    encoded = encoded.replace(')', '%29')
    encoded = encoded.replace(';', '%3B')
    encoded = encoded.replace(',', '%2C')
    return encoded

# ================= EVENT PROCESSOR =================

async def process_event(
    url: str,
    url_num: int,
    page: Page,
) -> str | None:
    """Process a single event URL and capture M3U8 stream."""
    
    captured: list[str] = []
    got_one = asyncio.Event()
    
    handler = partial(
        network.capture_req,
        captured=captured,
        got_one=got_one,
    )
    
    page.on("request", handler)
    
    try:
        resp = await page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=6_000,
            referer=BASE_URL,
        )
        
        if not resp or resp.status != 200:
            log.warning(f"URL {url_num}) Status Code: {resp.status if resp else 'None'}")
            return None
        
        wait_task = asyncio.create_task(got_one.wait())
        
        try:
            await asyncio.wait_for(wait_task, timeout=6)
        except asyncio.TimeoutError:
            log.warning(f"URL {url_num}) Timed out waiting for M3U8.")
            return None
        finally:
            if not wait_task.done():
                wait_task.cancel()
                try:
                    await wait_task
                except asyncio.CancelledError:
                    pass
        
        if captured:
            log.info(f"URL {url_num}) Captured M3U8")
            return captured[0]
        
    except Exception as e:
        log.warning(f"URL {url_num}) {e}")
        return None
    finally:
        page.remove_listener("request", handler)
    
    return None


# ================= EVENTS =================

async def get_events(cached_keys: KeysView[str]) -> list[Event]:
    """Fetch and filter events from API."""
    now = Time.rn()  # Use Time.rn() like original code
    
    if not (api_data := API_FILE.load(per_entry=False)):
        log.info("Refreshing API cache")
        
        api_data = {"timestamp": now.timestamp()}
        
        if r := await network.request(urljoin(BASE_URL, "api-event.php"), log=log):
            api_data: dict = r.json()
            api_data["timestamp"] = now.timestamp()
        
        API_FILE.write(api_data)
    
    events: list[Event] = []
    
    # Expanded time window to get more events
    start_dt = now.delta(hours=-6)  # Expanded from -3 to -6
    end_dt = now.delta(minutes=60)   # Expanded from 30 to 60
    
    for info in api_data.get("days", []):
        for event in info["items"]:
            if (event_league := event["league"]) == "channel tv":
                continue
            
            event_dt = Time.from_ts(event["ts_et"])
            
            if not start_dt <= event_dt <= end_dt:
                continue
            
            sport = fix_league(event_league)
            event_name = event["title"]
            
            if f"[{sport}] {event_name} ({TAG})" in cached_keys:
                continue
            
            if not (event_streams := event.get("streams")):
                continue
            
            elif not (event_link := event_streams[0].get("link")):
                continue
            
            events.append(
                Event(
                    sport=sport,
                    name=event_name,
                    link=event_link,
                    timestamp=now.timestamp(),
                )
            )
    
    return events


# ================= UPDATER =================

async def scrape(browser: Browser) -> None:
    """Scrape events and capture stream URLs."""
    cached_urls = CACHE_FILE.load()
    
    valid_urls = {k: v for k, v in cached_urls.items() if v.get("source")}
    valid_count = cached_count = len(valid_urls)
    
    urls.update(valid_urls)
    
    log.info(f"Loaded {cached_count} event(s) from cache")
    log.info(f'Scraping from "{BASE_URL}"')
    
    if events := await get_events(cached_urls.keys()):
        log.info(f"Processing {len(events)} new URL(s)")
        
        async with network.event_context(browser) as context:
            for i, ev in enumerate(events, start=1):
                async with network.event_page(context) as page:
                    handler = partial(
                        process_event,
                        url=ev.link,
                        url_num=i,
                        page=page,
                    )
                    
                    source = await network.safe_process(
                        handler,
                        url_num=i,
                        semaphore=network.PW_S,
                        log=log,
                    )
                    
                    tvg_id, logo = leagues.get_tvg_info(ev.sport, ev.name)
                    
                    key = f"[{ev.sport}] {ev.name} ({TAG})"
                    
                    entry = {
                        "source": source,
                        "logo": logo,
                        "refer": REFERER,
                        "timestamp": ev.timestamp,
                        "tvg-id": tvg_id or "Live.Event.us",
                        "link": ev.link,
                        "sport": ev.sport,
                        "name": ev.name,
                    }
                    
                    cached_urls[key] = entry
                    
                    if source:
                        valid_count += 1
                        urls[key] = entry
        
        log.info(f"Collected and cached {valid_count - cached_count} new event(s)")
    else:
        log.info("No new events found")
    
    CACHE_FILE.write(cached_urls)


# ================= M3U GENERATOR =================

async def generate_m3u8_files() -> None:
    """Generate M3U8 playlists for VLC and TiviMate."""
    # Filter out channels without source
    valid_channels = {k: v for k, v in urls.items() if v.get("source")}
    
    if not valid_channels:
        log.warning("No valid channels found to generate M3U8 files")
        # Create empty files with headers
        with open(OUT_VLC, 'w', encoding='utf-8') as f:
            f.write("#EXTM3U\n")
        with open(OUT_TIVI, 'w', encoding='utf-8') as f:
            f.write("#EXTM3U\n")
        OUT_VLC.chmod(0o644)
        OUT_TIVI.chmod(0o644)
        return
    
    # VLC format
    with open(OUT_VLC, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        chno = 1
        for key, channel in valid_channels.items():
            # Extract channel info
            sport = channel.get("sport", "Live Event")
            name = channel.get("name", key)
            
            # Clean display name
            display_name = clean_display_name(key.replace(f" ({TAG})", ""))
            
            # VLC format
            tvg_name = f"[{sport}] {name} ({TAG})"
            
            f.write(
                f'#EXTINF:-1 tvg-chno="{chno}" '
                f'tvg-id="{channel.get("tvg-id", "Live.Event.us")}" '
                f'tvg-name="{tvg_name}" '
                f'tvg-logo="{channel.get("logo", "")}" '
                f'group-title="{sport}",{display_name}\n'
            )
            f.write(f"#EXTVLCOPT:http-referrer={REFERER}\n")
            f.write(f"#EXTVLCOPT:http-origin={ORIGIN}\n")
            f.write(f"#EXTVLCOPT:http-user-agent={USER_AGENT}\n")
            f.write(f"{channel['source']}\n\n")
            chno += 1
    
    # TiviMate format
    with open(OUT_TIVI, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        chno = 1
        for key, channel in valid_channels.items():
            # Extract channel info
            sport = channel.get("sport", "Live Event")
            name = channel.get("name", key)
            
            # Clean display name for Tivimate
            display_name = clean_display_name(key.replace(f" ({TAG})", f" ({TAG}TV)"))
            
            # Tivimate format
            tvg_name = f"[{sport}] {name} ({TAG}TV)"
            
            # Encode the user agent for Tivimate
            encoded_user_agent = encode_user_agent(USER_AGENT)
            
            f.write(
                f'#EXTINF:-1 tvg-chno="{chno}" '
                f'tvg-id="{channel.get("tvg-id", "Live.Event.us")}" '
                f'tvg-name="{tvg_name}" '
                f'tvg-logo="{channel.get("logo", "")}" '
                f'group-title="{sport}",{display_name}\n'
            )
            f.write(
                f"{channel['source']}|referer={REFERER}|origin={ORIGIN}|user-agent={encoded_user_agent}\n\n"
            )
            chno += 1
    
    # Set write permissions (read/write for owner, read for others)
    OUT_VLC.chmod(0o644)
    OUT_TIVI.chmod(0o644)
    
    log.info(f"M3U playlists generated: {OUT_VLC}, {OUT_TIVI} with {chno-1} channel(s)")


# ================= MAIN =================

async def main():
    """Main entry point."""
    log.info("Starting EMBED scraper...")
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=['--no-sandbox', '--disable-setuid-sandbox']
        )
        
        await scrape(browser)
        
        await browser.close()
    
    # Generate M3U8 files after scraping
    await generate_m3u8_files()
    
    log.info("EMBED scraper completed")


if __name__ == "__main__":
    asyncio.run(main())
