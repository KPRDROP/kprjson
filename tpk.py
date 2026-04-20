import json
import re
import asyncio
from functools import partial
from urllib.parse import urljoin, urlparse

from selectolax.parser import HTMLParser

from utils import Cache, Time, get_logger, leagues, network

log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}

CACHE_FILE = Cache("TPK", exp=28_800)

BASE_URL = {
    "TPK": "https://live.totalsportek.fyi"
}

# 🚫 ignore junk iframes
BAD_IFRAME = (
    "youtube", "youtu.be", "atletifan", "facebook", "twitter"
)


def fix_txt(s: str) -> str:
    s = " ".join(s.split())
    return s.upper() if s.islower() else s


# 🔥 UNIVERSAL M3U8 EXTRACTOR
async def extract_m3u8(url: str, referer: str, url_num: int) -> str | None:
    if any(b in url for b in BAD_IFRAME):
        return None

    data = await network.request(url, headers={"Referer": referer}, log=log)
    if not data:
        return None

    text = data.text

    # ✅ pattern 1: direct m3u8
    m = re.search(r'https?://[^"\']+\.m3u8[^"\']*', text)
    if m:
        log.info(f"URL {url_num}) Direct m3u8 found")
        return m.group(0)

    # ✅ pattern 2: currentStreamUrl
    m = re.search(r'currentStreamUrl\s*=\s*"([^"]+)"', text)
    if m:
        log.info(f"URL {url_num}) currentStreamUrl found")
        return json.loads(f'"{m.group(1)}"')

    # ✅ pattern 3: hex encoded
    m = re.search(r'(?:var|const)\s+\w+\s*=\s*"([0-9a-fA-F]+)"', text)
    if m:
        try:
            decoded = bytes.fromhex(m.group(1)).decode()
            if ".m3u8" in decoded:
                log.info(f"URL {url_num}) Hex decoded m3u8")
                return decoded
        except:
            pass

    # ✅ pattern 4: sources: [{file: "..."}]
    m = re.search(r'file\s*:\s*"([^"]+\.m3u8[^"]*)"', text)
    if m:
        log.info(f"URL {url_num}) JWPlayer source found")
        return m.group(1)

    return None


async def process_event(url: str, url_num: int, tag: str) -> str | None:
    event_data = await network.request(url, log=log)
    if not event_data:
        log.warning(f"URL {url_num}) Failed to load page")
        return

    soup = HTMLParser(event_data.content)

    # 🔥 FIX: scan ALL iframes
    iframes = soup.css("iframe")

    for ifr in iframes:
        src = ifr.attributes.get("src")
        if not src:
            continue

        stream = await extract_m3u8(src, url, url_num)
        if stream:
            return stream

    log.warning(f"NO STREAM FOUND: {url}")
    return None


async def get_events(cached_keys: list[str]) -> list[dict[str, str]]:
    events = []

    html_data = await network.request(BASE_URL["TPK"], log=log)
    if not html_data:
        return events

    soup = HTMLParser(html_data.content)

    sport = "Live Event"

    for tag, base in BASE_URL.items():
        for node in soup.css("a"):
            if not node.attributes.get("class"):
                continue

            if (parent := node.parent) and "my-1" in parent.attributes.get("class", ""):
                if span := node.css_first("span"):
                    sport = span.text(strip=True)

            sport = fix_txt(sport)

            teams = [t.text(strip=True) for t in node.css(".col-7 .col-12")]
            if not teams:
                continue

            href = node.attributes.get("href")
            if not href:
                continue

            href = urlparse(href).path if href.startswith("http") else href

            time_node = node.css_first(".col-3 span")
            if not time_node:
                continue

            if time_node.text(strip=True).lower() != "matchstarted":
                continue

            event_name = fix_txt(" vs ".join(teams))
            key = f"[{sport}] {event_name} ({tag})"

            if key in cached_keys:
                continue

            events.append({
                "sport": sport,
                "event": event_name,
                "tag": tag,
                "link": urljoin(base, href),
            })

    return events


# 🔥 OUTPUT GENERATORS
def write_outputs(data: dict):
    ua = "Mozilla/5.0"

    vlc = ["#EXTM3U"]
    tivimate = ["#EXTM3U"]

    ch = 200

    for name, d in data.items():
        if not d.get("url"):
            continue

        url = d["url"]
        link = d["link"]

        tvg = d.get("id", "Live.Event.us")
        logo = d.get("logo", "")

        # VLC
        vlc.append(
            f'#EXTINF:-1 tvg-chno="{ch}" tvg-id="{tvg}" tvg-name="{name}" tvg-logo="{logo}" group-title="Live Events",{name}'
        )
        vlc.append(f"#EXTVLCOPT:http-referrer={link}")
        vlc.append(f"#EXTVLCOPT:http-origin={link}")
        vlc.append(f"#EXTVLCOPT:http-user-agent={ua}")
        vlc.append(url)

        # Tivimate
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
    cached = CACHE_FILE.load()

    log.info("Fetching events...")
    events = await get_events(cached.keys())
    log.info(f"Found {len(events)} events")

    now = Time.clean(Time.now())

    tasks = []
    for i, ev in enumerate(events, 1):
        log.info(f"Processing: {ev['event']}")
        tasks.append(process_event(ev["link"], i, ev["tag"]))

    results = await asyncio.gather(*tasks)

    for ev, stream in zip(events, results):
        key = f"[{ev['sport']}] {ev['event']} ({ev['tag']})"
        tvg_id, logo = leagues.get_tvg_info(ev["sport"], ev["event"])

        cached[key] = {
            "url": stream,
            "logo": logo,
            "link": ev["link"],
            "id": tvg_id or "Live.Event.us",
            "timestamp": now.timestamp(),
        }

    CACHE_FILE.write(cached)

    write_outputs(cached)


if __name__ == "__main__":
    asyncio.run(scrape())
