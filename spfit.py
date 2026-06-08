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

SPORT_URLS = {
    sport: urljoin(BASE_URL, sport.lower())
    for sport in [
        "Boxing",
        "F1",
        "MLB",
        "MMA",
        "NBA",
        "NFL",
        "NHL",
        "Soccer",
    ]
}


def clean_event_name(event_name: str) -> str:
    """Remove commas from event names"""
    if not event_name:
        return event_name
    
    # Remove commas
    cleaned = event_name.replace(",", "")
    
    # Remove extra spaces
    cleaned = re.sub(r'\s+', ' ', cleaned)
    
    return cleaned.strip()


async def process_event(
    url: str,
    url_num: int,
    page: Page,
) -> tuple[str | None, str | None, str | None]:

    nones = None, None

    captured: list[str] = []

    got_one = asyncio.Event()

    handler = partial(
        network.capture_req,
        captured=captured,
        got_one=got_one,
    )

    page.on("request", handler)

    event_name = "Sporting Event"

    try:
        resp = await page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=6_000,
        )

        if not resp or resp.status != 200:
            log.warning(
                f"URL {url_num}) Status Code: {resp.status if resp else 'None'}"
            )
            return (event_name, *nones)

        event_name_elem = page.locator("h1.match-head")

        event_name = await event_name_elem.inner_text(timeout=1_250)
        
        # Clean event name by removing commas
        event_name = clean_event_name(event_name)

        try:
            ifr = page.locator("iframe.embed-responsive-item")

            await ifr.wait_for(timeout=1_250)

            ifr_src = await ifr.get_attribute("src")
        except TimeoutError:
            log.warning(f"URL {url_num}) No iframe found.")
            return (event_name, *nones)

        await page.goto(
            ifr_src,
            wait_until="domcontentloaded",
            timeout=2_250,
        )

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
            log.info(f"URL {url_num}) Captured M3U8")

            return event_name, ifr_src, captured[0]
    except Exception as e:
        log.warning(f"URL {url_num}) {e}")
        return (event_name, *nones)

    finally:
        page.remove_listener("request", handler)


async def get_events(cached_links: set[str]) -> list[dict[str, str]]:
    tasks = [network.request(url, log=log) for url in SPORT_URLS.values()]

    results = await asyncio.gather(*tasks)

    events = []

    if not (
        soups := [(HTMLParser(html.content), html.url) for html in results if html]
    ):
        return events

    for soup, url in soups:
        sport = next((k for k, v in SPORT_URLS.items() if v == url), "Live Event")

        for event in soup.css("a.matches"):
            if not (href := event.attributes.get("href")):
                continue

            elif cached_links & {link := urljoin(BASE_URL, href)}:
                continue

            events.append({"sport": sport, "link": link})

    return events


async def scrape(browser: Browser) -> None:
    cached_urls = CACHE_FILE.load()

    cached_links = {entry["link"] for entry in cached_urls.values()}

    valid_urls = {k: v for k, v in cached_urls.items() if v["url"]}

    valid_count = cached_count = len(valid_urls)

    urls.update(valid_urls)

    log.info(f"Loaded {cached_count} event(s) from cache")

    log.info(f'Scraping from "{BASE_URL}"')

    if events := await get_events(cached_links):
        log.info(f"Processing {len(events)} URL(s)")

        now = Time.clean(Time.now())

        # Use browser context from the passed browser
        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        
        for i, ev in enumerate(events, start=1):
            page = await context.new_page()
            try:
                handler = partial(
                    process_event,
                    url=(link := ev["link"]),
                    url_num=i,
                    page=page,
                )

                event, ifr_src, url = await network.safe_process(
                    handler,
                    url_num=i,
                    semaphore=network.PW_S,
                    log=log,
                )

                tvg_id, logo = leagues.get_tvg_info((sport := ev["sport"]), event)

                key = f"[{sport}] {event} ({TAG})"

                entry = {
                    "url": url,
                    "logo": logo,
                    "base": ifr_src,
                    "timestamp": now.timestamp(),
                    "id": tvg_id or "Live.Event.us",
                    "link": link,
                }

                cached_urls[key] = entry

                if url:
                    valid_count += 1
                    urls[key] = entry
            finally:
                await page.close()

        log.info(f"Collected and cached {valid_count - cached_count} event(s)")

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
                base_url = event_data.get("base", BASE_URL)
                
                if not url:
                    continue
                
                # Clean event name (remove commas)
                clean_name = clean_event_name(event_name)
                
                # Write VLC format with EXTVLCOPT lines
                f.write(f'#EXTINF:-1 tvg-chno="{ch_no}" tvg-id="{tvg_id}" tvg-name="{clean_name}" tvg-logo="{logo}" group-title="Live Events",{clean_name}\n')
                f.write(f'#EXTVLCOPT:http-referrer={base_url}\n')
                f.write(f'#EXTVLCOPT:http-origin={base_url}\n')
                f.write(f'#EXTVLCOPT:http-user-agent={USER_AGENT}\n')
                f.write(f'{url}\n\n')
                
                ch_no += 1
        
        log.info(f"Generated VLC playlist: {VLC_OUTPUT} with {ch_no - 1} streams")
    except Exception as e:
        log.error(f"Error generating VLC playlist: {e}")

    # Generate TiviMate playlist
    try:
        ua_enc = quote(USER_AGENT, safe="")
        
        with open(TIVIMATE_OUTPUT, "w", encoding="utf-8") as f:
            f.write(header)
            
            ch_no = 1
            for event_name, event_data in urls.items():
                url = event_data.get("url")
                logo = event_data.get("logo", "https://i.gyazo.com/1c4aa937f5ea01b0f29bb27adb59884c.png")
                tvg_id = event_data.get("id", "Live.Event.us")
                base_url = event_data.get("base", BASE_URL)
                
                if not url:
                    continue
                
                # Clean event name (remove commas)
                clean_name = clean_event_name(event_name)
                
                # Encode headers for TiviMate
                referer_enc = quote(base_url, safe="")
                origin_enc = quote(base_url, safe="")
                
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
