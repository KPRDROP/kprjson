#!/usr/bin/env python3

from utils import Cache, Time, get_logger, leagues, network
from datetime import datetime
from urllib.parse import quote

log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}

TAG = "SHOPP"

CACHE_FILE = Cache(TAG, exp=28_800)

API_URL = "https://blog.xyzstreams.shop:2053/api/scoreboard"

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


async def get_events() -> dict[str, dict[str, str | float]]:
    events = {}

    if not (r := await network.request(API_URL, log=log)):
        return events

    now = Time.clean(Time.now())

    api_data: list[dict[str, str | dict]] = r.json()

    sport = "Live Event"

    for event_info in api_data:
        away_team: str = event_info.get("away", {}).get("name")
        home_team: str = event_info.get("home", {}).get("name")
        event_date: str = event_info.get("gameDate")

        if not (event_date and away_team and home_team):
            continue

        event_dt = Time.fromisoformat(event_date)

        if event_dt.date() != now.date():
            continue

        if not (feeds := event_info.get("feeds")):
            continue

        event_name = f"{away_team} vs {home_team}"

        for i, feed in enumerate(feeds.values(), start=1):
            key = f"[{sport}] {event_name} {i} ({TAG})"

            tvg_id, logo = leagues.get_tvg_info(sport, event_name)

            events[key] = {
                "url": feed,
                "logo": logo,
                "base": "https://xyzstreams.shop",
                "timestamp": now.timestamp(),
                "id": tvg_id or "Live.Event.us",
            }

    return events


async def scrape() -> None:
    if cached := CACHE_FILE.load():
        urls.update(cached)

        log.info(f"Loaded {len(urls)} event(s) from cache")

        return

    log.info('Updating from "xyzstreams"')

    urls.update(await get_events() or {})

    log.info(f"Collected and cached {len(urls)} new event(s)")

    CACHE_FILE.write(urls)


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

    # =========================================================
    # VLC PLAYLIST
    # =========================================================

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

    log.info(
        f"Generated VLC playlist: {VLC_OUTPUT} "
        f"with {ch_no - 1} streams"
    )

    # =========================================================
    # TIVIMATE PLAYLIST
    # =========================================================

    # ONLY encode user-agent
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

            # Referer and origin in plain text
            f.write(
                f'{url}'
                f'|referer={REFERER}'
                f'|origin={ORIGIN}'
                f'|user-agent={ua_enc}\n\n'
            )

            ch_no += 1

    log.info(
        f"Generated TiviMate playlist: {TIVIMATE_OUTPUT} "
        f"with {ch_no - 1} streams"
    )


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
