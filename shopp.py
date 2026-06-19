#!/usr/bin/env python3

from utils import Cache, Time, get_logger, leagues, network
from datetime import datetime
from urllib.parse import quote
import json

log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}

TAG = "SHOPP"

CACHE_FILE = Cache(TAG, exp=28_800)

BASE_URL = "https://xyzstreams.shop/"
API_URL = "https://api.streamxyz.shop:2053/api/scoreboard"
EMBED_API_URL = "https://xyzstreams.shop/embedapi.json"

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

# Browser-like headers for API requests
API_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Encoding": "gzip, deflate, br",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
    "Host": "api.streamxyz.shop:2053",
    "Origin": "https://xyzstreams.shop",
    "Referer": "https://xyzstreams.shop/",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "cross-site",
    "User-Agent": USER_AGENT,
}


async def get_events() -> dict[str, dict[str, str | float]]:
    """Fetch events from API with proper headers and fallback to embed API"""
    events = {}
    
    # Try primary API first
    try:
        if r := await network.request(
            API_URL,
            headers=API_HEADERS,
            log=log,
        ):
            api_data = r.json()
            events = parse_api_data(api_data)
            if events:
                log.info(f"Successfully fetched {len(events)} events from primary API")
                return events
    except Exception as e:
        log.warning(f"Primary API failed: {e}, trying fallback...")
    
    # Fallback to embed API
    try:
        if r := await network.request(
            EMBED_API_URL,
            headers={"Referer": BASE_URL, "User-Agent": USER_AGENT},
            log=log,
        ):
            embed_data = r.json()
            events = parse_embed_data(embed_data)
            if events:
                log.info(f"Successfully fetched {len(events)} events from embed API")
                return events
    except Exception as e:
        log.error(f"Embed API also failed: {e}")
    
    return events


def parse_api_data(api_data: list[dict]) -> dict[str, dict[str, str | float]]:
    """Parse data from primary API endpoint"""
    events = {}
    now = Time.clean(Time.now())
    sport = "Live Event"
    
    for event_info in api_data:
        try:
            away_team = event_info.get("away", {}).get("name")
            home_team = event_info.get("home", {}).get("name")
            event_date = event_info.get("gameDate")
            
            if not (event_date and away_team and home_team):
                continue
                
            event_dt = Time.fromisoformat(event_date)
            if event_dt.date() != now.date():
                continue
                
            feeds = event_info.get("feeds")
            if not feeds:
                continue
                
            event_name = f"{away_team} vs {home_team}"
            
            for i, feed in enumerate(feeds.values(), start=1):
                key = f"[{sport}] {event_name} {i} ({TAG})"
                tvg_id, logo = leagues.get_tvg_info(sport, event_name)
                
                events[key] = {
                    "url": feed,
                    "logo": logo,
                    "base": BASE_URL,
                    "timestamp": now.timestamp(),
                    "id": tvg_id or "Live.Event.us",
                }
        except Exception as e:
            log.debug(f"Error parsing event: {e}")
            continue
            
    return events


def parse_embed_data(embed_data: dict) -> dict[str, dict[str, str | float]]:
    """Parse data from embed API as fallback"""
    events = {}
    now = Time.clean(Time.now())
    
    try:
        # Look for event data in embed structure
        for sport_category, events_list in embed_data.items():
            if not isinstance(events_list, list):
                continue
                
            for event in events_list:
                try:
                    # Extract team names and IDs from embed data structure
                    if isinstance(event, dict):
                        # Try different possible field names
                        away_team = event.get("away", event.get("awayTeam", event.get("team2", "")))
                        home_team = event.get("home", event.get("homeTeam", event.get("team1", "")))
                        clean_id = event.get("id", event.get("cleanId", ""))
                        
                        if not (away_team and home_team and clean_id):
                            continue
                            
                        # Create m3u8 URL from cleanId
                        m3u8_url = f"https://streamxyz.shop/{clean_id}/index.m3u8"
                        event_name = f"{away_team} vs {home_team}"
                        sport = event.get("sport", "Live Event")
                        
                        key = f"[{sport}] {event_name} ({TAG})"
                        tvg_id, logo = leagues.get_tvg_info(sport, event_name)
                        
                        events[key] = {
                            "url": m3u8_url,
                            "logo": logo,
                            "base": BASE_URL,
                            "timestamp": now.timestamp(),
                            "id": tvg_id or clean_id,
                        }
                except Exception as e:
                    log.debug(f"Error parsing embed event: {e}")
                    continue
                    
    except Exception as e:
        log.error(f"Error parsing embed data: {e}")
        
    return events


async def scrape() -> None:
    """Scrape events from all available sources"""
    if cached := CACHE_FILE.load():
        urls.update(cached)
        log.info(f"Loaded {len(urls)} event(s) from cache")
        return
    
    log.info('Updating from "xyzstreams"')
    
    # Try all sources
    events = await get_events()
    
    if events:
        urls.update(events)
        log.info(f"Collected and cached {len(urls)} new event(s)")
        CACHE_FILE.write(urls)
    else:
        log.warning("No events found from any source")


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
