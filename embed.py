#!/usr/bin/env python3

import asyncio
from functools import partial
from urllib.parse import urljoin, quote
import os
from pathlib import Path

from playwright.async_api import async_playwright, Browser

from utils import Cache, Time, get_logger, leagues, network

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
    return " ".join(x.capitalize() for x in s.split()) if len(s) > 5 else s.upper()


# ================= EVENTS =================

async def get_events(cached_keys: list[str]) -> list[dict[str, str]]:
    now = Time.clean(Time.now())

    if not (api_data := API_FILE.load(per_entry=False)):
        log.info("Refreshing API cache")

        api_data = {"timestamp": now.timestamp()}

        if r := await network.request(urljoin(BASE_URL, "api-event.php"), log=log):
            api_data: dict = r.json()
            api_data["timestamp"] = now.timestamp()

        API_FILE.write(api_data)

    events = []

    start_dt = now.delta(hours=-3)
    end_dt = now.delta(minutes=30)

    for info in api_data.get("days", []):
        for event in info["items"]:
            if (event_league := event["league"]) == "channel tv":
                continue

            event_dt = Time.from_str(event["when_et"], timezone="ET")

            if not start_dt <= event_dt <= end_dt:
                continue

            sport = fix_league(event_league)
            event_name = event["title"]

            if f"[{sport}] {event_name} ({TAG})" in cached_keys:
                continue

            if not (streams := event.get("streams")):
                continue

            if not (link := streams[0].get("link")):
                continue

            events.append(
                {
                    "sport": sport,
                    "event": event_name,
                    "link": link,
                    "timestamp": now.timestamp(),
                }
            )

    return events


# ================= UPDATER =================

async def scrape(browser: Browser) -> None:
    cached_urls = CACHE_FILE.load()

    valid_urls = {k: v for k, v in cached_urls.items() if v["url"]}
    urls.update(valid_urls)

    log.info(f"Loaded {len(valid_urls)} event(s) from cache")
    log.info(f'Scraping from "{BASE_URL}"')

    if events := await get_events(cached_urls.keys()):
        log.info(f"Processing {len(events)} new URL(s)")

        async with network.event_context(browser) as context:
            for i, ev in enumerate(events, start=1):
                async with network.event_page(context) as page:
                    handler = partial(
                        network.process_event,
                        url=(link := ev["link"]),
                        url_num=i,
                        page=page,
                        log=log,
                    )

                    stream_url = await network.safe_process(
                        handler,
                        url_num=i,
                        semaphore=network.PW_S,
                        log=log,
                    )

                    sport, event, ts = ev["sport"], ev["event"], ev["timestamp"]

                    tvg_id, logo = leagues.get_tvg_info(sport, event)

                    key = f"[{sport}] {event} ({TAG})"

                    entry = {
                        "url": stream_url,
                        "logo": logo,
                        "base": REFERER,
                        "timestamp": ts,
                        "id": tvg_id or "Live.Event.us",
                        "link": link,
                    }

                    cached_urls[key] = entry

                    if stream_url:
                        urls[key] = entry

        log.info("Scraping completed")

    else:
        log.info("No new events found")

    CACHE_FILE.write(cached_urls)


# ================= M3U GENERATOR =================

def write_outputs():
    if not urls:
        log.warning("No URLs to write")
        return

    # VLC
    with open(OUT_VLC, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for i, (name, e) in enumerate(urls.items(), 1):
            f.write(
                f'#EXTINF:-1 tvg-chno="{i}" tvg-id="{e["id"]}" '
                f'tvg-name="{name}" tvg-logo="{e["logo"]}" '
                f'group-title="Live Events",{name}\n'
            )
            f.write(f"#EXTVLCOPT:http-referrer={REFERER}\n")
            f.write(f"#EXTVLCOPT:http-origin={ORIGIN}\n")
            f.write(f"#EXTVLCOPT:http-user-agent={USER_AGENT}\n")
            f.write(f"{e['url']}\n\n")

    # TiviMate
    with open(OUT_TIVI, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for i, (name, e) in enumerate(urls.items(), 1):
            f.write(
                f'#EXTINF:-1 tvg-chno="{i}" tvg-id="{e["id"]}" '
                f'tvg-name="{name}" tvg-logo="{e["logo"]}" '
                f'group-title="Live Events",{name}\n'
            )
            f.write(
                f"{e['url']}|referer={REFERER}|origin={ORIGIN}|user-agent={UA_ENC}\n"
            )

    log.info("M3U playlists generated")


# ================= MAIN =================

async def main():
    log.info("Starting EMBED updater...")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)

        await scrape(browser)

        await browser.close()

    write_outputs()


if __name__ == "__main__":
    asyncio.run(main())
