import json
import re
import asyncio
from functools import partial
from urllib.parse import urljoin, urlparse

from selectolax.parser import HTMLParser

from utils import Cache, Time, get_logger, leagues, network

log = get_logger(__name__)

urls = {}

CACHE_FILE = Cache("TPK", exp=28_800)

BASE_URL = "https://live.totalsportek.fyi"

USER_AGENT = "Mozilla/5.0"


# =========================
#  UNIVERSAL M3U8 EXTRACTOR
# =========================
M3U8_REGEX = re.compile(r"https?://[^\s\"']+\.m3u8[^\s\"']*", re.I)


async def extract_m3u8(url: str, depth=0, referer=None):
    if depth > 4:
        return None

    headers = {"User-Agent": USER_AGENT}
    if referer:
        headers["Referer"] = referer

    res = await network.request(url, headers=headers, log=log)
    if not res:
        return None

    text = res.text

    #  Direct m3u8
    if m := M3U8_REGEX.search(text):
        return m.group(0)

    #  source / file pattern
    patterns = [
        r'source\s*:\s*"([^"]+)"',
        r'file\s*:\s*"([^"]+)"',
        r'hls\s*:\s*"([^"]+)"',
    ]

    for p in patterns:
        if m := re.search(p, text):
            if ".m3u8" in m.group(1):
                return m.group(1)

    #  Clappr style
    if m := re.search(r'Clappr\.Player.*?source\s*:\s*"([^"]+)"', text, re.S):
        return m.group(1)

    #  Follow iframes recursively
    soup = HTMLParser(res.content)

    for iframe in soup.css("iframe"):
        src = iframe.attributes.get("src")
        if not src:
            continue

        src = urljoin(url, src)

        result = await extract_m3u8(src, depth + 1, referer=url)
        if result:
            return result

    return None


# =========================
# EVENT SCRAPER (UNCHANGED CORE)
# =========================
def fix_txt(s: str) -> str:
    s = " ".join(s.split())
    return s.upper() if s.islower() else s


async def get_events():
    events = []

    res = await network.request(BASE_URL, log=log)
    if not res:
        return events

    soup = HTMLParser(res.content)

    for node in soup.css("a"):
        if not node.attributes.get("class"):
            continue

        teams = [t.text(strip=True) for t in node.css(".col-7 .col-12")]
        if not teams:
            continue

        href = node.attributes.get("href")
        if not href:
            continue

        time_node = node.css_first(".col-3 span")
        if not time_node or time_node.text(strip=True).lower() != "matchstarted":
            continue

        event_name = fix_txt(" vs ".join(teams))

        events.append({
            "event": event_name,
            "link": urljoin(BASE_URL, href)
        })

    return events


# =========================
#  PROCESS ALL EVENTS (PARALLEL)
# =========================
async def process_all(events):
    tasks = []

    for ev in events:
        tasks.append(handle_event(ev))

    await asyncio.gather(*tasks)


async def handle_event(ev):
    name = ev["event"]
    link = ev["link"]

    log.info(f"Processing: {name}")

    stream = await extract_m3u8(link)

    entry = {
        "url": stream,
        "logo": "https://i.gyazo.com/4a5e9fa2525808ee4b65002b56d3450e.png",
        "link": link,
        "id": "Live.Event.us",
        "timestamp": Time.now().timestamp(),
    }

    urls[f"[Live Event] {name} (TPK)"] = entry


# =========================
#  M3U GENERATOR
# =========================
def write_outputs():
    with open("tpk_vlc.m3u8", "w", encoding="utf-8") as f_vlc, \
         open("tpk_tivimate.m3u8", "w", encoding="utf-8") as f_tivi:

        f_vlc.write("#EXTM3U\n")
        f_tivi.write("#EXTM3U\n")

        for i, (name, data) in enumerate(urls.items(), start=200):
            if not data["url"]:
                continue

            url = data["url"]
            link = data["link"]

            # VLC
            f_vlc.write(
                f'#EXTINF:-1 tvg-chno="{i}" tvg-id="{data["id"]}" tvg-name="{name}" '
                f'tvg-logo="{data["logo"]}" group-title="Live Events",{name}\n'
            )
            f_vlc.write(f'#EXTVLCOPT:http-referrer={link}\n')
            f_vlc.write(f'#EXTVLCOPT:http-origin={link}\n')
            f_vlc.write(f'#EXTVLCOPT:http-user-agent={USER_AGENT}\n')
            f_vlc.write(f"{url}\n")

            # TiviMate
            f_tivi.write(
                f'#EXTINF:-1 tvg-chno="{i}" tvg-id="{data["id"]}" tvg-name="{name}" '
                f'tvg-logo="{data["logo"]}" group-title="Live Events",{name}\n'
            )

            encoded_ua = USER_AGENT.replace(" ", "%20").replace("/", "%2F")
            f_tivi.write(
                f"{url}|referer={link}|origin={link}|user-agent={encoded_ua}\n"
            )


# =========================
# MAIN
# =========================
async def scrape():
    log.info("Fetching events...")

    events = await get_events()

    log.info(f"Found {len(events)} events")

    await process_all(events)

    CACHE_FILE.write(urls)

    write_outputs()


if __name__ == "__main__":
    asyncio.run(scrape())
