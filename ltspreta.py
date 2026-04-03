import asyncio
import os
import urllib.parse
import re
from functools import partial
from urllib.parse import urljoin, urlparse, parse_qs

from playwright.async_api import Browser

from utils import Cache, Time, get_logger, leagues, network

log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}

TAG = "LISTA"

CACHE_FILE = Cache(TAG, exp=10_800)

API_CACHE = Cache(f"{TAG}-api", exp=19_800)

# Get API_URL from environment variable (secret) with validation
API_URL = os.environ.get("LTSPRETA_API_URL")
# Ensure URL has protocol
if API_URL and not API_URL.startswith(('http://', 'https://')):
    API_URL = f"https://{API_URL}"

# Constants for output files
VLC_OUTPUT_FILE = "ltspreta_vlc.m3u8"
TIVIMATE_OUTPUT_FILE = "ltspreta_tivimate.m3u8"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"

def encode_user_agent(user_agent: str) -> str:
    """Encode user agent for TiviMate format"""
    return urllib.parse.quote(user_agent)

def extract_id_from_url(url: str) -> str:
    """Extract ID from player.php URL"""
    try:
        # Parse the URL
        parsed = urlparse(url)
        # Get query parameters
        params = parse_qs(parsed.query)
        # Extract id parameter
        if 'id' in params:
            return params['id'][0]
        
        # Try regex as fallback
        match = re.search(r'[?&]id=([^&]+)', url)
        if match:
            return match.group(1)
    except Exception as e:
        log.debug(f"Error extracting ID from {url}: {e}")
    return None

async def process_event(url: str, url_num: int) -> tuple[str | None, str | None]:
    """Process event URL to get M3U8 stream"""
    nones = None, None
    
    # Extract event ID from URL
    event_id = extract_id_from_url(url)
    if not event_id:
        log.warning(f"URL {url_num}) Could not extract ID from URL: {url}")
        return nones
    
    log.debug(f"URL {url_num}) Extracted event ID: {event_id}")
    
    # Get token from generate_token.php
    if not (
        token_req := await network.request(
            "https://lista-preta-tv.site/generate_token.php",
            params={"id": event_id},
            log=log,
        )
    ):
        log.warning(f"URL {url_num}) Failed to load token data.")
        return nones
    
    if not (token_data := token_req.json()):
        log.warning(f"URL {url_num}) No token data available.")
        return nones
    
    token = token_data.get("token")
    exp = token_data.get("exp")
    
    if not token or not exp:
        log.warning(f"URL {url_num}) Missing token or expiration data.")
        return nones
    
    log.debug(f"URL {url_num}) Got token: {token}, exp: {exp}")
    
    # Construct referer URL
    ref = f"https://lista-preta-tv.site/player-all.html?id={event_id}"
    
    # Get M3U8 stream
    if not (
        m3u8_req := await network.request(
            "https://lista-preta-tv.site/m3u8.php",
            headers={"Referer": ref},
            params={"id": event_id, "token": token, "exp": exp},
            follow_redirects=False,
            log=log,
        )
    ):
        log.warning(f"URL {url_num}) Unable to fetch M3U8 request.")
        return nones
    
    # Get the Location header which contains the M3U8 URL
    m3u8 = m3u8_req.headers.get("Location")
    if not m3u8:
        log.warning(f"URL {url_num}) No Location header in response.")
        return nones
    
    log.info(f"URL {url_num}) Captured M3U8: {m3u8}")
    
    return m3u8, ref

def generate_output_files():
    """Generate both VLC and TiviMate M3U8 files"""
    if not urls:
        log.info("No URLs to write to output files")
        return
    
    log.info(f"Generating output files with {len(urls)} events")
    
    # Generate VLC format
    vlc_content = "#EXTM3U\n"
    tivimate_content = "#EXTM3U\n"
    
    # Sort by timestamp to maintain order
    sorted_urls = sorted(urls.items(), key=lambda x: x[1].get("timestamp", 0))
    
    chno = 1  # Start channel number from 1
    for key, data in sorted_urls:
        if not data.get("url"):
            continue
            
        # Extract data
        sport_match = key.split("[")[1].split("]")[0] if "[" in key else "Live Events"
        sport = sport_match
        event_name = key.split("]")[-1].strip().replace(f"({TAG})", "").strip() if "]" in key else key
        logo = data.get("logo", "")
        tvg_id = data.get("id", "Live.Event.us")
        url = data.get("url", "")
        referer_url = data.get("referer_url", "")
        
        # Keep the full URL with token parameters
        full_url = url
        
        # Skip if no URL
        if not full_url:
            continue
        
        # For VLC referer, use the constructed referer URL
        vlc_referer = referer_url if referer_url else "https://lista-preta-tv.site/"
        
        # EXTINF line (same for both formats)
        extinf = f'#EXTINF:-1 tvg-chno="{chno}" tvg-id="{tvg_id}" tvg-name="{key}" tvg-logo="{logo}" group-title="{sport}",{event_name}\n'
        
        # VLC format
        vlc_content += extinf
        vlc_content += f"#EXTVLCOPT:http-referrer={vlc_referer}\n"
        vlc_content += f"#EXTVLCOPT:http-origin={vlc_referer}\n"
        vlc_content += f"#EXTVLCOPT:http-user-agent={USER_AGENT}\n"
        vlc_content += f"{full_url}\n\n"
        
        # TiviMate format (with pipe and encoded user agent)
        encoded_ua = encode_user_agent(USER_AGENT)
        tivimate_url = f"{full_url}|referer={vlc_referer}|origin={vlc_referer}|user-agent={encoded_ua}"
        
        tivimate_content += extinf
        tivimate_content += f"{tivimate_url}\n\n"
        
        chno += 1
    
    # Write VLC file
    try:
        with open(VLC_OUTPUT_FILE, "w", encoding="utf-8") as f:
            f.write(vlc_content)
        log.info(f"Successfully wrote {VLC_OUTPUT_FILE} with {chno-1} events")
    except Exception as e:
        log.error(f"Error writing {VLC_OUTPUT_FILE}: {e}")
    
    # Write TiviMate file
    try:
        with open(TIVIMATE_OUTPUT_FILE, "w", encoding="utf-8") as f:
            f.write(tivimate_content)
        log.info(f"Successfully wrote {TIVIMATE_OUTPUT_FILE} with {chno-1} events")
    except Exception as e:
        log.error(f"Error writing {TIVIMATE_OUTPUT_FILE}: {e}")

async def get_events(cached_keys: list[str]) -> list[dict[str, str]]:
    now = Time.clean(Time.now())
    
    events = []
    
    api_data = API_CACHE.load(per_entry=False)
    
    if not api_data:
        log.info("Refreshing API cache")
        
        # Validate API_URL is set
        if not API_URL:
            log.error("LTSPRETA_API_URL environment variable is not set")
            return events
        
        api_url = API_URL
        log.info(f"Fetching from API: {api_url}")
        
        if r := await network.request(
            api_url,
            log=log,
            headers={
                "User-Agent": USER_AGENT
            }
        ):
            try:
                api_data = r.json()
                
                # Handle different response formats
                if isinstance(api_data, dict):
                    # Check if it's a list wrapped in a dict
                    if "events" in api_data:
                        api_data = api_data.get("events", [])
                    elif "data" in api_data:
                        api_data = api_data.get("data", [])
                    elif "games" in api_data:
                        api_data = api_data.get("games", [])
                elif not isinstance(api_data, list):
                    log.error(f"Unexpected API response format: {type(api_data)}")
                    api_data = []
                
                if api_data and isinstance(api_data, list):
                    log.info(f"API returned {len(api_data)} events")
                else:
                    log.warning("API returned empty data or invalid format")
                    api_data = []
                    
            except Exception as e:
                log.error(f"Error parsing API response: {e}")
                api_data = []
        
        if not api_data:
            log.error("Failed to fetch from API or empty response")
            api_data = []
        
        # Cache the raw API data
        API_CACHE.write(api_data)
    
    # If no data, return empty list
    if not api_data:
        log.warning("No API data available")
        return events
    
    # Extended time window to capture more events (4 hours before to 4 hours after)
    start_dt = now.delta(minutes=-240)
    end_dt = now.delta(minutes=240)
    
    log.info(f"Processing {len(api_data)} events from API (time window: -4h to +4h)")
    
    for event in api_data:
        try:
            # Extract event information from new API format
            sport = event.get("sport", "")
            if not sport:
                continue
            
            # Get teams
            home_team = event.get("home", "")
            away_team = event.get("away", "")
            
            if not (home_team and away_team):
                continue
            
            # Format event name
            event_name = f"{home_team} vs {away_team}"
            
            # Get tournament/league
            tournament = event.get("tournament", sport)
            
            # Get status (only process live events)
            status = event.get("status", "").lower()
            if status != "live":
                log.debug(f"Event {event_name} status is '{status}', skipping")
                continue
            
            # Get channels
            channels = event.get("channels", [])
            if not channels:
                log.debug(f"No channels for event: {event_name}")
                continue
            
            # Process channels to get stream URL and referer
            for channel in channels:
                try:
                    # Get player URL from channel
                    player_url = channel.get("url", "")
                    if not player_url:
                        continue
                    
                    # Extract event ID from player URL
                    event_id = extract_id_from_url(player_url)
                    if not event_id:
                        log.debug(f"Could not extract ID from player URL: {player_url}")
                        continue
                    
                    # Get channel name and code
                    channel_name = channel.get("channel_name", "")
                    channel_code = channel.get("channel_code", "")
                    
                    # Parse event time
                    event_time_str = event.get("start", "")
                    timestamp = now.timestamp()
                    
                    # Construct datetime from start time if available
                    if event_time_str:
                        try:
                            event_dt = Time.from_str(event_time_str, timezone="UTC")
                            timestamp = event_dt.timestamp()
                            
                            # Check if event is within our time window
                            if not (start_dt <= event_dt <= end_dt):
                                log.debug(f"Event outside time window: {event_name} at {event_dt}")
                                continue
                                
                        except Exception as e:
                            log.debug(f"Could not parse time for {event_name}: {e}")
                            # Use current time as fallback
                    
                    # Create key with tournament and event name
                    key = f"[{tournament}] {event_name} ({TAG})"
                    
                    if key in cached_keys:
                        log.debug(f"Event already in cache: {key}")
                        continue
                    
                    # Get logo from channel or event
                    logo = channel.get("image", "")
                    if not logo:
                        logo = event.get("homeIMG", "")
                    
                    events.append({
                        "sport": tournament,
                        "event": event_name,
                        "link": player_url,  # Store player URL for processing
                        "timestamp": timestamp,
                        "tournament": tournament,
                        "sport_type": sport,
                        "home_team": home_team,
                        "away_team": away_team,
                        "logo": logo,
                        "channel_name": channel_name,
                        "channel_code": channel_code,
                        "event_id": event_id
                    })
                    
                    log.info(f"Found new event: {key} at {event_time_str if event_time_str else 'current time'} (player_url: {player_url})")
                    break  # Use first valid channel
                    
                except Exception as e:
                    log.error(f"Error processing channel for event {event_name}: {e}")
                    continue
            
        except Exception as e:
            log.error(f"Error processing event: {e}")
            continue
    
    log.info(f"Total new events found: {len(events)}")
    return events

async def scrape(browser: Browser) -> None:
    """Main scraping function"""
    # Load cached URLs
    cached_urls = CACHE_FILE.load() or {}
    
    cached_count = len(cached_urls)
    
    # Update global urls with cached ones
    urls.update(cached_urls)
    
    log.info(f"Loaded {cached_count} event(s) from cache")
    log.info(f'Scraping from "{API_URL}"')
    
    if events := await get_events(list(cached_urls.keys())):
        log.info(f"Processing {len(events)} new URL(s)")
        
        # Use a semaphore to limit concurrent processing
        semaphore = asyncio.Semaphore(3)
        
        async def process_single_event(i, ev):
            async with semaphore:
                async with network.event_context(browser) as context:
                    async with network.event_page(context) as page:
                        log.info(f"Processing event {i}/{len(events)}: {ev['sport']} - {ev['event']}")
                        
                        # Use the process_event function to get M3U8
                        m3u8_url, referer = await process_event(ev["link"], i)
                        
                        if m3u8_url:
                            sport, event, ts = (
                                ev["sport"],
                                ev["event"],
                                ev["timestamp"],
                            )
                            
                            key = f"[{sport}] {event} ({TAG})"
                            
                            tvg_id, logo = leagues.get_tvg_info(sport, event)
                            
                            # Use logo from API if available
                            final_logo = ev.get("logo", logo) if ev.get("logo") else logo
                            final_id = tvg_id or f"{sport.replace(' ', '.')}.event"
                            
                            entry = {
                                "url": m3u8_url,
                                "logo": final_logo,
                                "base": referer if referer else "https://lista-preta-tv.site/",
                                "timestamp": ts,
                                "id": final_id,
                                "link": ev["link"],
                                "referer_url": referer if referer else f"https://lista-preta-tv.site/player-all.html?id={ev.get('event_id', '')}",
                            }
                            
                            urls[key] = cached_urls[key] = entry
                            log.info(f"Successfully added URL for: {key} - M3U8: {m3u8_url}")
                        else:
                            log.warning(f"Failed to get M3U8 for event: {ev['sport']} - {ev['event']}")
        
        # Process events concurrently
        tasks = []
        for i, ev in enumerate(events, start=1):
            tasks.append(process_single_event(i, ev))
        
        await asyncio.gather(*tasks)
        
        log.info(f"Collected and cached {len(cached_urls) - cached_count} new event(s)")
    
    else:
        log.info("No new events found")
    
    # Save updated cache
    CACHE_FILE.write(cached_urls)
    
    # Generate output files
    generate_output_files()

async def main():
    """Main function to run the updater"""
    log.info("Starting LTSPRETA updater")
    
    # Validate API_URL
    if not API_URL or API_URL == "None":
        log.error("LTSPRETA_API_URL environment variable is not set correctly")
        return
    
    log.info(f"Using API URL: {API_URL}")
    
    from playwright.async_api import async_playwright
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            await scrape(browser)
        finally:
            await browser.close()
    
    log.info("LTSPRETA updater completed")

def run():
    """Synchronous entry point for the updater"""
    asyncio.run(main())

if __name__ == "__main__":
    run()
