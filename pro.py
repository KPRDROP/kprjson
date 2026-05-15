import os
from utils import Cache, Time, get_logger, leagues, network

log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}

TAG = "PRO"

CACHE_FILE = Cache(TAG, exp=28_800)

# API URL from environment variable
API_URL = os.environ.get("PRO_API_URL")
if not API_URL:
    raise RuntimeError("Missing PRO_API_URL secret")

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"

# Output files
VLC_OUTPUT = "pro_vlc.m3u8"
TIVIMATE_OUTPUT = "pro_tivimate.m3u8"


# ---------------------------------------------------------
# PLAYLIST GENERATOR
# ---------------------------------------------------------

def generate_playlists():
    """
    Generate VLC and Tivimate playlist files from captured streams
    """
    from urllib.parse import quote
    
    vlc_lines = ["#EXTM3U"]
    tivimate_lines = ["#EXTM3U"]
    
    ua_encoded = quote(USER_AGENT, safe="")
    valid_streams = 0
    
    for chno, (name, data) in enumerate(urls.items(), start=1):
        
        url = data.get("url")
        logo = data.get("logo") or ""
        tvg_id = data.get("id", "Live.Event.us")
        base = data.get("base")
        
        if not url:
            continue
        
        valid_streams += 1
        
        extinf = (
            f'#EXTINF:-1 tvg-chno="{chno}" tvg-id="{tvg_id}" '
            f'tvg-name="{name}" tvg-logo="{logo}" group-title="Live Events",{name}'
        )
        
        # VLC format (with #EXTVLCOPT headers)
        vlc_lines.append(extinf)
        vlc_lines.append(f"#EXTVLCOPT:http-referrer={base}")
        vlc_lines.append(f"#EXTVLCOPT:http-origin={base}")
        vlc_lines.append(f"#EXTVLCOPT:http-user-agent={USER_AGENT}")
        vlc_lines.append(url)
        vlc_lines.append("")  # Empty line for readability
        
        # Tivimate format (pipe-separated headers)
        tivimate_lines.append(extinf)
        tivimate_url = (
            f"{url}"
            f"|referer={base}"
            f"|origin={base}"
            f"|user-agent={ua_encoded}"
        )
        tivimate_lines.append(tivimate_url)
        tivimate_lines.append("")  # Empty line for readability
    
    # Write VLC playlist
    with open(VLC_OUTPUT, "w", encoding="utf8") as f:
        f.write("\n".join(vlc_lines))
    log.info(f"Generated {VLC_OUTPUT} with {valid_streams} streams")
    
    # Write Tivimate playlist
    with open(TIVIMATE_OUTPUT, "w", encoding="utf8") as f:
        f.write("\n".join(tivimate_lines))
    log.info(f"Generated {TIVIMATE_OUTPUT} with {valid_streams} streams")


# ---------------------------------------------------------
# EVENT DISCOVERY
# ---------------------------------------------------------

async def get_events() -> dict[str, dict[str, str | float]]:
    now = Time.clean(Time.now())

    events = {}

    if not (r := await network.request(API_URL, log=log)):
        return events

    api_data: dict[str, dict] = r.json()

    for stream_group in api_data.get("streams", []):
        sport = stream_group["category"]

        if sport == "24/7 Streams":
            continue

        for event in stream_group.get("streams", []):
            name = event.get("name")

            start_ts = event.get("starts_at")

            logo = event.get("poster")

            uri_name = event.get("uri_name")

            if not (name and start_ts and uri_name):
                continue

            event_dt = Time.from_ts(start_ts)

            if event_dt.date() != now.date():
                continue

            key = f"[{sport}] {name} ({TAG})"

            tvg_id, pic = leagues.get_tvg_info(sport, name)

            events[key] = {
                "url": f"https://dami-tv.pro/live-hls/channel/{uri_name}/playlist.m3u8",
                "logo": logo or pic,
                "base": f"https://dami-tv.pro/player/auto/?match={uri_name}",
                "timestamp": now.timestamp(),
                "id": tvg_id or "Live.Event.us",
            }

    return events


# ---------------------------------------------------------
# UPDATER
# ---------------------------------------------------------

async def scrape() -> None:
    if cached_urls := CACHE_FILE.load():
        urls.update(cached_urls)
        log.info(f"Loaded {len(urls)} event(s) from cache")
        
        # Generate playlists from cached data
        generate_playlists()
        return

    log.info(f'Scraping from "{API_URL}"')

    events = await get_events()

    urls.update(events)

    log.info(f"Collected and cached {len(urls)} event(s)")

    CACHE_FILE.write(urls)
    
    # Generate playlists after updating
    generate_playlists()


# ---------------------------------------------------------
# MAIN
# ---------------------------------------------------------

async def main():
    log.info("Starting PRO updater")
    
    try:
        await scrape()
    except Exception as e:
        log.error(f"Scraping failed: {e}")
        raise
    finally:
        await network.client.aclose()
    
    log.info("PRO updater finished")


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
