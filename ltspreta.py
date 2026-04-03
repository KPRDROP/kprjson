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

TAG = "LTSPRETA"

CACHE_FILE = Cache(TAG, exp=19_800)
API_CACHE = Cache(f"{TAG}-api", exp=19_800)

API_URL = os.environ.get("LTSPRETA_API_URL")
if API_URL and not API_URL.startswith(('http://', 'https://')):
    API_URL = f"https://{API_URL}"

VLC_OUTPUT_FILE = "ltspreta_vlc.m3u8"
TIVIMATE_OUTPUT_FILE = "ltspreta_tivimate.m3u8"

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"


def encode_user_agent(user_agent: str) -> str:
    return urllib.parse.quote(user_agent)


async def process_event(url: str, url_num: int) -> tuple[str | None, str | None]:
    nones = None, None

    event_id = url.split("id=")[-1]
    if not event_id:
        log.warning(f"URL {url_num}) Could not extract ID")
        return nones

    if not (
        token_req := await network.request(
            "https://lista-preta-tv.site/generate_token.php",
            params={"id": event_id},
            log=log,
        )
    ):
        return nones

    token_data = token_req.json()
    token = token_data.get("token")
    exp = token_data.get("exp")

    if not token or not exp:
        return nones

    ref = f"https://lista-preta-tv.site/player-all.html?id={event_id}"

    if not (
        m3u8_req := await network.request(
            "https://lista-preta-tv.site/m3u8.php",
            headers={"Referer": ref},
            params={"id": event_id, "token": token, "exp": exp},
            follow_redirects=True,
            log=log,
        )
    ):
        return nones

    m3u8 = str(m3u8_req.url) if hasattr(m3u8_req, 'url') else None

    if not m3u8:
        location = m3u8_req.headers.get("Location")
        if location:
            m3u8 = str(location)

    if not m3u8:
        return nones

    log.info(f"URL {url_num}) Captured M3U8: {m3u8}")
    return m3u8, ref


def generate_output_files():
    if not urls:
        log.info("No URLs to write")
        return

    log.info(f"Generating output files with {len(urls)} events")

    vlc_content = "#EXTM3U\n"
    tivimate_content = "#EXTM3U\n"

    sorted_urls = sorted(urls.items(), key=lambda x: x[1].get("timestamp", 0))

    chno = 1
    for key, data in sorted_urls:
        if not data.get("url"):
            continue

        sport = key.split("[")[1].split("]")[0] if "[" in key else "Live"
        event_name = key.split("]")[-1].replace(f"({TAG})", "").strip()

        logo = data.get("logo", "")
        tvg_id = data.get("id", "Live.Event")
        url = data.get("url")
        referer = data.get("referer_url", "https://lista-preta-tv.site/")

        extinf = f'#EXTINF:-1 tvg-chno="{chno}" tvg-id="{tvg_id}" tvg-name="{key}" tvg-logo="{logo}" group-title="{sport}",{event_name}\n'

        # VLC
        vlc_content += extinf
        vlc_content += f"#EXTVLCOPT:http-referrer={referer}\n"
        vlc_content += f"#EXTVLCOPT:http-origin={referer}\n"
        vlc_content += f"#EXTVLCOPT:http-user-agent={USER_AGENT}\n"
        vlc_content += f"{url}\n\n"

        # TiviMate
        encoded_ua = encode_user_agent(USER_AGENT)
        tivimate_content += extinf
        tivimate_content += f"{url}|referer={referer}|origin={referer}|user-agent={encoded_ua}\n\n"

        chno += 1

    with open(VLC_OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(vlc_content)

    with open(TIVIMATE_OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(tivimate_content)

    log.info(f"Files generated: {VLC_OUTPUT_FILE}, {TIVIMATE_OUTPUT_FILE}")


async def get_events(cached_keys):
    now = Time.clean(Time.now())
    events = []

    api_data = API_CACHE.load(per_entry=False)

    if not api_data:
        if not API_URL:
            return events

        if r := await network.request(API_URL, log=log, headers={"User-Agent": USER_AGENT}):
            api_data = r.json()

        API_CACHE.write(api_data or [])

    if not api_data:
        return events

    for event in api_data:
        try:
            if event.get("status", "").lower() != "live":
                continue

            home = event.get("home")
            away = event.get("away")
            sport = event.get("sport")

            if not (home and away and sport):
                continue

            channels = event.get("channels", [])
            if not channels:
                continue

            player_url = channels[0].get("url")
            if not player_url:
                continue

            key = f"[{sport}] {home} vs {away} ({TAG})"

            if key in cached_keys:
                continue

            events.append({
                "sport": sport,
                "event": f"{home} vs {away}",
                "link": player_url,
                "timestamp": now.timestamp(),
                "logo": channels[0].get("image", ""),
                "event_id": player_url.split("id=")[-1]
            })

        except:
            continue

    return events


async def scrape(browser=None):
    cached_urls = CACHE_FILE.load() or {}
    urls.update(cached_urls)

    if events := await get_events(list(cached_urls.keys())):
        for i, ev in enumerate(events, start=1):
            m3u8, ref = await process_event(ev["link"], i)

            if m3u8:
                key = f"[{ev['sport']}] {ev['event']} ({TAG})"

                urls[key] = cached_urls[key] = {
                    "url": m3u8,
                    "logo": ev["logo"],
                    "timestamp": ev["timestamp"],
                    "id": f"{ev['sport']}.event",
                    "referer_url": ref
                }

    CACHE_FILE.write(cached_urls)

    generate_output_files()


async def main():
    log.info("Starting LTSPRETA updater")

    if not API_URL:
        log.error("Missing API URL")
        return

    await scrape()


def run():
    asyncio.run(main())


if __name__ == "__main__":
    run()
