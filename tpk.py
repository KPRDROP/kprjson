#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import re
import os
import asyncio
import time
from functools import partial
from urllib.parse import urljoin, urlparse, quote
from pathlib import Path

from selectolax.parser import HTMLParser
from git import Repo

# RELATIVE IMPORT
try:
    from utils import Cache, Time, get_logger, leagues, network
except:
    from .utils import Cache, Time, get_logger, leagues, network


log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}

CACHE_FILE = Cache("TPK", exp=28_800)

BASES = {
    "TPK": os.getenv("TPK_BASE_URL", "https://live.totalsportek.fyi")
}

REPO_DIR = Path(__file__).resolve().parent
VLC_FILE = "tpk_vlc.m3u8"
TIVIMATE_FILE = "tpk_tivimate.m3u8"


def fix_txt(s: str) -> str:
    s = " ".join(s.split())
    return s.upper() if s.islower() else s


# ───────── STREAM ─────────

async def process_ts1(ifr_src: str, url_num: int) -> str | None:
    data = await network.request(ifr_src, log=log)
    if not data:
        return

    match = re.search(r'(var|const)\s+(\w+)\s*=\s*"([^"]*)"', data.text)
    if not match:
        return

    encoded = match[2] if len(match[2]) > 20 else match[3]

    try:
        return bytes.fromhex(encoded).decode("utf-8")
    except:
        return None


async def process_event(url: str, url_num: int, tag: str) -> str | None:
    data = await network.request(url, log=log)
    if not data:
        return

    soup = HTMLParser(data.content)
    iframe = soup.css_first("iframe")
    if not iframe:
        return

    src = iframe.attributes.get("src")
    if not src:
        return

    return await process_ts1(src, url_num)


# ───────── EVENTS ─────────

async def get_events(cached_keys):
    events = []

    base = BASES["TPK"]
    html = await network.request(base, log=log)
    if not html:
        return events

    soup = HTMLParser(html.content)

    for node in soup.css("a"):
        teams = [t.text(strip=True) for t in node.css(".col-7 .col-12")]
        if not teams:
            continue

        href = node.attributes.get("href")
        if not href:
            continue

        href = urlparse(href).path if href.startswith("http") else href

        time_node = node.css_first(".col-3 span")
        if not time_node or time_node.text(strip=True).lower() != "matchstarted":
            continue

        sport = "Live Event"
        event_name = fix_txt(" vs ".join(teams))
        key = f"[{sport}] {event_name} (TPK)"

        if key in cached_keys:
            continue

        events.append({
            "sport": sport,
            "event": event_name,
            "tag": "TPK",
            "link": urljoin(base, href),
        })

    return events


# ───────── PLAYLIST BUILD ─────────

def build_vlc(entry):
    headers = []
    ref = entry["link"]

    headers.append(f'#EXTVLCOPT:http-referrer={ref}')
    headers.append(f'#EXTVLCOPT:http-origin={ref}')
    headers.append('#EXTVLCOPT:http-user-agent=Mozilla/5.0')

    return headers


def build_tivi(entry):
    ref = entry["link"]
    ua = quote("Mozilla/5.0", safe="")
    return f'{entry["url"]}|referer={ref}|origin={ref}|user-agent={ua}'


def generate_playlists():
    vlc = ["#EXTM3U"]
    tivi = ["#EXTM3U"]

    chno = 200

    for name, entry in urls.items():
        if not entry.get("url"):
            continue

        ext = f'#EXTINF:-1 tvg-chno="{chno}" tvg-id="{entry.get("id","Live.Event.us")}" tvg-name="{name}" group-title="Live Events",{name}'

        vlc.append(ext)
        vlc += build_vlc(entry)
        vlc.append(entry["url"])

        tivi.append(ext)
        tivi.append(build_tivi(entry))

        chno += 1

    # FORCE CHANGE EVERY RUN
    stamp = f"\n# updated {time.time()}"

    (REPO_DIR / VLC_FILE).write_text("\n".join(vlc) + stamp)
    (REPO_DIR / TIVIMATE_FILE).write_text("\n".join(tivi) + stamp)

    print("Playlists written.")


# ───────── SCRAPE ─────────

async def scrape():
    cache = CACHE_FILE.load()
    urls.update({k: v for k, v in cache.items() if v["url"]})

    events = await get_events(cache.keys())

    now = Time.clean(Time.now())

    for i, ev in enumerate(events, 1):
        handler = partial(process_event, url=ev["link"], url_num=i, tag="TPK")

        stream = await network.safe_process(
            handler,
            url_num=i,
            semaphore=network.HTTP_S,
            log=log,
        )

        key = f"[{ev['sport']}] {ev['event']} (TPK)"

        tvg_id, logo = leagues.get_tvg_info(ev["sport"], ev["event"])

        entry = {
            "url": stream,
            "logo": logo,
            "link": ev["link"],
            "id": tvg_id or "Live.Event.us",
            "timestamp": now.timestamp(),
        }

        cache[key] = entry

        if stream:
            urls[key] = entry

    CACHE_FILE.write(cache)


# ───────── MAIN ─────────

async def main():
    await scrape()
    generate_playlists()

    try:
        repo = Repo(REPO_DIR)
        repo.git.add(A=True)
        repo.index.commit(f"TPK auto {time.time()}", skip_hooks=True)
        repo.remote().push()
        print("Pushed.")
    except Exception as e:
        print("Git error:", e)


# THIS WAS MISSING (CRITICAL)
if __name__ == "__main__":
    asyncio.run(main())
