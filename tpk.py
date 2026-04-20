import json
import re
import asyncio
from urllib.parse import urljoin

from selectolax.parser import HTMLParser

from utils import Cache, Time, get_logger, network

log = get_logger(__name__)

urls = {}

CACHE_FILE = Cache("TPK", exp=28_800)

BASE_URL = "https://live.totalsportek.fyi"

USER_AGENT = "Mozilla/5.0"


# =========================
# 🔥 IMPROVED EXTRACTOR
# =========================
M3U8_REGEX = re.compile(r"https?://[^\s\"']+\.m3u8[^\s\"']*", re.I)


async def extract_m3u8(url: str, depth=0, referer=None):
    if depth > 5:
        return None

    headers = {"User-Agent": USER_AGENT}
    if referer:
        headers["Referer"] = referer

    res = await network.request(url, headers=headers, log=log)
    if not res:
        return None

    text = res.text

    # ✅ direct m3u8
    if m := M3U8_REGEX.search(text):
        log.info(f"FOUND m3u8 (direct)")
        return m.group(0)

    # ✅ multiple patterns
    patterns = [
        r'source\s*:\s*"([^"]+)"',
        r'file\s*:\s*"([^"]+)"',
        r'hls\s*:\s*"([^"]+)"',
        r'"(https://[^"]+\.m3u8[^"]*)"',
    ]

    for p in patterns:
        if m := re.search(p, text):
            if ".m3u8" in m.group(1):
                log.info(f"FOUND m3u8 (pattern)")
                return m.group(1)

    # 🔁 recursive iframe scan
    soup = HTMLParser(res.content)

    for iframe in soup.css("iframe"):
        src = iframe.attributes.get("src")
        if not src:
            continue

        src = urljoin(url, src)

        result = await extract_m3u8(src, depth + 1, referer=url)
        if result:
            return result

    log.warning(f"NO STREAM FOUND: {url}")
    return None


# =========================
# EVENTS
# =========================
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

        event_name = " vs ".join(teams)

        events.append({
            "event": event_name,
            "link": urljoin(BASE_URL, href)
        })

    return events


# =========================
# PROCESS
# =========================
async def handle_event(ev):
    name = ev["event"]
    link = ev["link"]

    log.info(f"Processing: {name}")

    stream = await extract_m3u8(link)

    urls[f"[Live Event] {name} (TPK)"] = {
        "url": stream,
        "link": link,
        "id": "Live.Event.us",
        "logo": "https://i.gyazo.com/4a5e9fa2525808ee4b65002b56d3450e.png",
        "timestamp": Time.now().timestamp(),
    }


async def process_all(events):
    await asyncio.gather(*(handle_event(ev) for ev in events))


# =========================
# OUTPUT (FIXED)
# =========================
def write_outputs():
    with open("tpk_vlc.m3u8", "w", encoding="utf-8") as f1, \
         open("tpk_tivimate.m3u8", "w", encoding="utf-8") as f2:

        f1.write("#EXTM3U\n")
        f2.write("#EXTM3U\n")

        for i, (name, data) in enumerate(urls.items(), start=200):

            url = data["url"] or "http://invalid/stream"  # 🔥 NEVER EMPTY

            # VLC
            f1.write(
                f'#EXTINF:-1 tvg-chno="{i}" tvg-id="{data["id"]}" tvg-name="{name}" '
                f'tvg-logo="{data["logo"]}" group-title="Live Events",{name}\n'
            )
            f1.write(f'#EXTVLCOPT:http-referrer={data["link"]}\n')
            f1.write(f'#EXTVLCOPT:http-origin={data["link"]}\n')
            f1.write(f'#EXTVLCOPT:http-user-agent={USER_AGENT}\n')
            f1.write(f"{url}\n")

            # TiviMate
            ua = USER_AGENT.replace(" ", "%20").replace("/", "%2F")

            f2.write(
                f'#EXTINF:-1 tvg-chno="{i}" tvg-id="{data["id"]}" tvg-name="{name}" '
                f'tvg-logo="{data["logo"]}" group-title="Live Events",{name}\n'
            )
            f2.write(
                f"{url}|referer={data['link']}|origin={data['link']}|user-agent={ua}\n"
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
