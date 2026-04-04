import asyncio
import os
import urllib.parse

from utils import Cache, Time, get_logger, leagues, network

log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}

TAG = "LTSPRETA-CH"

CACHE_FILE = Cache(TAG, exp=19_800)
API_CACHE = Cache(f"{TAG}-api", exp=19_800)

# API
API_URL = os.environ.get("LTSPRETA_CH_API_URL")
if API_URL and not API_URL.startswith(("http://", "https://")):
    API_URL = f"https://{API_URL}"

# OUNTRY FILTER
ALLOWED_COUNTRIES = {
    "US", "IN", "RS", "GB", "GR", "PL", 
    "BG", "AR", "MX", "RU", "CA"
}

# OUTPUT FILES
VLC_OUTPUT_FILE = "ltspreta_ch_vlc.m3u8"
TIVIMATE_OUTPUT_FILE = "ltspreta_ch_tivimate.m3u8"

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

def encode_user_agent(user_agent: str) -> str:
    return urllib.parse.quote(user_agent)


# =========================
# STREAM EXTRACTION
# =========================
async def process_event(url: str, url_num: int):
    event_id = url.split("id=")[-1]
    if not event_id:
        return None, None

    token_req = await network.request(
        "https://lista-preta-tv.site/generate_token.php",
        params={"id": event_id},
        log=log,
    )
    if not token_req:
        return None, None

    data = token_req.json()
    token = data.get("token")
    exp = data.get("exp")

    if not token or not exp:
        return None, None

    referer = f"https://lista-preta-tv.site/player-all.html?id={event_id}"

    m3u8_req = await network.request(
        "https://lista-preta-tv.site/m3u8.php",
        params={"id": event_id, "token": token, "exp": exp},
        headers={"Referer": referer},
        follow_redirects=True,
        log=log,
    )

    if not m3u8_req:
        return None, None

    final_url = str(m3u8_req.url)
    return final_url, referer


# =========================
# OUTPUT FILES
# =========================
def generate_output_files():
    if not urls:
        log.info("No URLs to write")
        return

    vlc = "#EXTM3U\n"
    tivimate = "#EXTM3U\n"

    sorted_urls = sorted(urls.items(), key=lambda x: x[1].get("timestamp", 0))

    chno = 1
    for key, data in sorted_urls:
        url = data.get("url")
        if not url:
            continue

        logo = data.get("logo", "")
        tvg_id = data.get("id", "Live.TV")
        group = data.get("group", "LIVE")
        referer = data.get("referer", "")

        extinf = f'#EXTINF:-1 tvg-chno="{chno}" tvg-id="{tvg_id}" tvg-logo="{logo}" group-title="{group}",{key}\n'

        # VLC
        vlc += extinf
        vlc += f"#EXTVLCOPT:http-referrer={referer}\n"
        vlc += f"#EXTVLCOPT:http-origin={referer}\n"
        vlc += f"#EXTVLCOPT:http-user-agent={USER_AGENT}\n"
        vlc += f"{url}\n\n"

        # TiviMate
        encoded = encode_user_agent(USER_AGENT)
        tivimate_url = f"{url}|referer={referer}|origin={referer}|user-agent={encoded}"

        tivimate += extinf
        tivimate += f"{tivimate_url}\n\n"

        chno += 1

    with open(VLC_OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(vlc)

    with open(TIVIMATE_OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(tivimate)

    log.info(f"Generated {chno-1} channels")


# =========================
# GET CHANNELS
# =========================
async def get_events(cached_keys):
    now = Time.now()
    events = []

    api_data = API_CACHE.load(per_entry=False)

    if not api_data:
        if not API_URL:
            log.error("Missing API URL")
            return events

        r = await network.request(API_URL, log=log)
        if not r:
            return events

        api_data = r.json()
        API_CACHE.write(api_data)

    if not isinstance(api_data, list):
        log.error("Invalid API format")
        return events

    log.info(f"Processing {len(api_data)} channels")

    for ch in api_data:
        try:
            name = ch.get("name")
            link = ch.get("link")
            country = ch.get("country", "").upper()
            image = ch.get("image", "")
            ch_id = ch.get("id")

            if not (name and link and ch_id):
                continue

            # COUNTRY FILTER
            if country not in ALLOWED_COUNTRIES:
                continue

            key = f"[{country}] {name} ({TAG})"

            if key in cached_keys:
                continue

            events.append({
                "name": name,
                "link": link,
                "logo": image,
                "id": str(ch_id),
                "country": country,
                "timestamp": now.timestamp(),
            })

            log.info(f"Added channel: {key}")

        except Exception as e:
            log.error(f"Error parsing channel: {e}")

    return events


# =========================
# MAIN SCRAPER
# =========================
async def scrape():
    cached = CACHE_FILE.load() or {}
    urls.update(cached)

    events = await get_events(list(cached.keys()))

    for i, ev in enumerate(events, start=1):
        m3u8, ref = await process_event(ev["link"], i)

        if not m3u8:
            continue

        key = f"[{ev['country']}] {ev['name']} ({TAG})"

        urls[key] = cached[key] = {
            "url": m3u8,
            "logo": ev["logo"],
            "timestamp": ev["timestamp"],
            "id": ev["id"],
            "group": ev["country"],
            "referer": ref,
        }

        log.info(f"Added stream: {key}")

    CACHE_FILE.write(cached)
    generate_output_files()


# =========================
# ENTRY
# =========================
async def main():
    log.info("Starting LTSPRETA-CH updater")

    if not API_URL:
        log.error("API URL missing")
        return

    await scrape()


def run():
    asyncio.run(main())


if __name__ == "__main__":
    run()
