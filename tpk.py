import json
import re
import os
from functools import partial
from urllib.parse import urljoin, urlparse, quote
from pathlib import Path

from selectolax.parser import HTMLParser
from git import Repo

from utils import Cache, Time, get_logger, leagues, network

log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}

CACHE_FILE = Cache("TPK", exp=28_800)

# BASE URL FROM SECRET
BASES = {
    "TPK": os.getenv("TPK_BASE_URL")
}

REPO_DIR = Path(__file__).resolve().parent.parent
VLC_FILE = "tpk_vlc.m3u8"
TIVIMATE_FILE = "tpk_tivimate.m3u8"


def fix_txt(s: str) -> str:
    s = " ".join(s.split())
    return s.upper() if s.islower() else s


# ───────── STREAM PROCESSORS ─────────

async def process_ts1(ifr_src: str, url_num: int) -> str | None:
    if not (ifr_src_data := await network.request(ifr_src, log=log)):
        return

    match = re.search(r'(var|const)\s+(\w+)\s*=\s*"([^"]*)"', ifr_src_data.text)
    if not match:
        return

    encoded = match[2] if len(match[2]) > 20 else match[3]

    return bytes.fromhex(encoded).decode("utf-8")


async def process_ts3(ifr_src: str, url_num: int) -> str | None:
    if not (data := await network.request(ifr_src, log=log)):
        return

    soup = HTMLParser(data.content)
    ifr = soup.css_first("iframe")
    if not ifr:
        return

    src = ifr.attributes.get("src")
    if not src:
        return

    data2 = await network.request(src, headers={"Referer": ifr_src}, log=log)
    if not data2:
        return

    match = re.search(r'currentStreamUrl\s+=\s+"([^"]*)"', data2.text)
    if not match:
        return

    return json.loads(f'"{match[1]}"')


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

    return await process_ts1(src, url_num) if tag == "TPK" else await process_ts3(src, url_num)


# ───────── SCRAPER ─────────

async def get_events(cached_keys: list[str]) -> list[dict[str, str]]:
    events = []

    base = BASES["TPK"]

    html = await network.request(base, log=log)
    if not html:
        return events

    soup = HTMLParser(html.content)

    for node in soup.css("a"):
        if not node.attributes.get("class"):
            continue

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


# ───────── PLAYLIST BUILDERS ─────────

def build_vlc_headers(entry: dict) -> list[str]:
    headers = []
    if entry["link"]:
        headers.append(f'#EXTVLCOPT:http-referrer={entry["link"]}')
        headers.append(f'#EXTVLCOPT:http-origin={entry["link"]}')
    headers.append('#EXTVLCOPT:http-user-agent=Mozilla/5.0')
    return headers


def build_tivimate_url(entry: dict) -> str:
    base = entry["url"]
    ref = entry["link"]
    ua = quote("Mozilla/5.0", safe="")

    return f"{base}|referer={ref}|origin={ref}|user-agent={ua}"


def generate_playlists():
    vlc = ["#EXTM3U"]
    tivimate = ["#EXTM3U"]

    chno = 200

    for name, entry in urls.items():
        if not entry.get("url"):
            continue

        title = name
        logo = entry.get("logo") or ""
        tvg_id = entry.get("id") or "Live.Event.us"

        extinf = f'#EXTINF:-1 tvg-chno="{chno}" tvg-id="{tvg_id}" tvg-name="{title}" tvg-logo="{logo}" group-title="Live Events",{title}'

        # VLC
        vlc.append(extinf)
        vlc.extend(build_vlc_headers(entry))
        vlc.append(entry["url"])

        # TiviMate
        tivimate.append(extinf)
        tivimate.append(build_tivimate_url(entry))

        chno += 1

    (REPO_DIR / VLC_FILE).write_text("\n".join(vlc), encoding="utf-8")
    (REPO_DIR / TIVIMATE_FILE).write_text("\n".join(tivimate), encoding="utf-8")

    log.info("Playlists generated.")


# ───────── MAIN SCRAPER ─────────

async def scrape() -> None:
    cached_urls = CACHE_FILE.load()

    urls.update({k: v for k, v in cached_urls.items() if v["url"]})

    if events := await get_events(cached_urls.keys()):
        now = Time.clean(Time.now())

        for i, ev in enumerate(events, start=1):
            handler = partial(process_event, url=ev["link"], url_num=i, tag=ev["tag"])

            stream_url = await network.safe_process(
                handler,
                url_num=i,
                semaphore=network.HTTP_S,
                log=log,
            )

            key = f"[{ev['sport']}] {ev['event']} ({ev['tag']})"

            tvg_id, logo = leagues.get_tvg_info(ev["sport"], ev["event"])

            entry = {
                "url": stream_url,
                "logo": logo,
                "link": ev["link"],
                "id": tvg_id or "Live.Event.us",
                "timestamp": now.timestamp(),
            }

            cached_urls[key] = entry

            if stream_url:
                urls[key] = entry

    CACHE_FILE.write(cached_urls)


# ───────── MAIN RUNNER + PUSH ─────────

async def main():
    await scrape()
    generate_playlists()

    try:
        repo = Repo(REPO_DIR)
        repo.git.add(A=True)
        repo.index.commit(f"TPK auto-update {Time.now().timestamp()}", skip_hooks=True)
        repo.remote().push()
        log.info("Pushed to GitHub.")
    except Exception as e:
        log.error(f"Git push error: {e}")


# THIS WAS MISSING (CRITICAL)
if __name__ == "__main__":
    asyncio.run(main())
