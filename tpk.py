import json
import re
import asyncio
from functools import partial
from urllib.parse import urlparse

from selectolax.parser import HTMLParser

from utils import Cache, Time, get_logger, leagues, network

log = get_logger(__name__)

CACHE_FILE = Cache("TPK", exp=28_800)


# 🔥 IMPROVED extractor
async def extract_stream(ifr_src: str, referer: str, url_num: int):
    if not ifr_src:
        return None

    data = await network.request(ifr_src, headers={"Referer": referer}, log=log)
    if not data:
        return None

    text = data.text

    # ✅ direct m3u8
    m = re.search(r'https?://[^"\']+\.m3u8[^"\']*', text)
    if m:
        log.info(f"{url_num}) direct m3u8")
        return m.group(0)

    # ✅ currentStreamUrl
    m = re.search(r'currentStreamUrl\s*=\s*"([^"]+)"', text)
    if m:
        log.info(f"{url_num}) currentStreamUrl")
        return json.loads(f'"{m.group(1)}"')

    # ✅ hex encoded
    m = re.search(r'(?:var|const)\s+\w+\s*=\s*"([0-9a-fA-F]+)"', text)
    if m:
        try:
            decoded = bytes.fromhex(m.group(1)).decode()
            if ".m3u8" in decoded:
                log.info(f"{url_num}) hex decoded")
                return decoded
        except:
            pass

    # ✅ jwplayer
    m = re.search(r'file\s*:\s*"([^"]+\.m3u8[^"]*)"', text)
    if m:
        log.info(f"{url_num}) jwplayer")
        return m.group(1)

    return None


# 🔥 FIX: scan ALL iframes
async def process_event(url: str, url_num: int):
    data = await network.request(url, log=log)
    if not data:
        log.warning(f"{url_num}) page failed")
        return None

    soup = HTMLParser(data.content)

    for ifr in soup.css("iframe"):
        src = ifr.attributes.get("src")
        if not src:
            continue

        stream = await extract_stream(src, url, url_num)
        if stream:
            return stream

    log.warning(f"NO STREAM FOUND: {url}")
    return None


# 🔥 LOAD FROM JSON INSTEAD OF WEBSITE
def load_events_from_cache():
    cached = CACHE_FILE.load()

    events = []
    for name, data in cached.items():
        if not data.get("link"):
            continue

        events.append({
            "name": name,
            "link": data["link"],
            "id": data.get("id", "Live.Event.us"),
            "logo": data.get("logo", ""),
        })

    return cached, events


# 🔥 OUTPUT FILES
def write_outputs(data):
    vlc = ["#EXTM3U"]
    tivimate = ["#EXTM3U"]

    ch = 200

    for name, d in data.items():
        if not d.get("url"):
            continue

        url = d["url"]
        link = d["link"]
        tvg = d["id"]
        logo = d["logo"]

        vlc.append(
            f'#EXTINF:-1 tvg-chno="{ch}" tvg-id="{tvg}" tvg-name="{name}" tvg-logo="{logo}" group-title="Live Events",{name}'
        )
        vlc.append(f"#EXTVLCOPT:http-referrer={link}")
        vlc.append(f"#EXTVLCOPT:http-origin={link}")
        vlc.append(f"#EXTVLCOPT:http-user-agent=Mozilla/5.0")
        vlc.append(url)

        tivimate.append(
            f'#EXTINF:-1 tvg-chno="{ch}" tvg-id="{tvg}" tvg-name="{name}" tvg-logo="{logo}" group-title="Live Events",{name}'
        )
        tivimate.append(
            f"{url}|referer={link}|origin={link}|user-agent=Mozilla%2F5.0"
        )

        ch += 1

    with open("tpk_vlc.m3u8", "w", encoding="utf-8") as f:
        f.write("\n".join(vlc))

    with open("tpk_tivimate.m3u8", "w", encoding="utf-8") as f:
        f.write("\n".join(tivimate))


async def scrape():
    cached, events = load_events_from_cache()

    log.info(f"Loaded {len(events)} events from tpk.json")

    tasks = []
    for i, ev in enumerate(events, 1):
        log.info(f"Processing: {ev['name']}")
        tasks.append(process_event(ev["link"], i))

    results = await asyncio.gather(*tasks)

    now = Time.clean(Time.now())

    for ev, stream in zip(events, results):
        cached[ev["name"]]["url"] = stream
        cached[ev["name"]]["timestamp"] = now.timestamp()

    CACHE_FILE.write(cached)

    write_outputs(cached)


if __name__ == "__main__":
    asyncio.run(scrape())
