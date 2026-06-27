from functools import partial
from urllib.parse import parse_qsl, urljoin, urlsplit
from pathlib import Path
import os

from selectolax.parser import HTMLParser

from utils import Cache, Time, get_logger, leagues, network

log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}

TAG = "OBSHUB"

CACHE_FILE = Cache(TAG, exp=10_800)

BASE_URL = "https://streamhub.pro"

# Constants for output files
REFERER = "https://getembed.live/"
ORIGIN = "https://getembed.live"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36 Edg/134.0.0.0"
USER_AGENT_ENCODED = "Mozilla%2F5.0%20(Windows%20NT%2010.0%3B%20Win64%3B%20x64)%20AppleWebKit%2F537.36%20(KHTML%2C%20like%20Gecko)%20Chrome%2F149.0.0.0%20Safari%2F537.36"


async def process_event(url: str, url_num: int) -> tuple[str | None, str | None]:
    nones = None, None

    if not (event_data := await network.request(url, log=log)):
        log.warning(f"URL {url_num}) Failed to load url.")
        return nones

    soup_1 = HTMLParser(event_data.content)

    ifr_1 = soup_1.css_first("iframe#playerIframe")

    if not ifr_1 or not (ifr_1_src := ifr_1.attributes.get("src")):
        log.warning(f"URL {url_num}) No iframe element/src found. (IFR1)")
        return nones

    elif not (ifr_1_src_data := await network.request(ifr_1_src, log=log)):
        log.warning(f"URL {url_num}) Failed to load iframe source. (IFR1)")
        return nones

    soup_2 = HTMLParser(ifr_1_src_data.content)

    ifr_2 = soup_2.css_first("iframe")

    if not ifr_2 or not (ifr_2_src := ifr_2.attributes.get("src")):
        log.warning(f"URL {url_num}) No iframe element/src found. (IFR2)")
        return nones

    params = dict(parse_qsl(urlsplit(ifr_2_src).query))

    if not (stream_key := params.get("stream")):
        log.warning(f"URL {url_num}) No stream key found.")
        return nones

    log.info(f"URL {url_num}) Captured M3U8")

    return f"https://obstreamx.click/live/{stream_key}.m3u8", ifr_2_src


async def get_events(cached_keys: list[str]) -> list[dict[str, str]]:
    events = []

    if not (html_data := await network.request(BASE_URL, log=log)):
        return events

    soup = HTMLParser(html_data.content)
    
    # Track events by slug to avoid duplicates
    seen_events = set()
    
    # Scrape from "LIVE NOW" section (.live-card elements)
    for card in soup.css(".live-card"):
        if not (href := card.attributes.get("href")):
            continue

        # Extract sport from live-league
        league_elem = card.css_first(".live-league")
        sport = ""
        if league_elem:
            sport = "".join(
                x for x in league_elem.text(strip=True) if x.isascii()
            ).lstrip()
        
        # Extract team names
        team_elems = card.css(".live-team-name")
        if not team_elems:
            continue
            
        event_name = (
            "".join(x.text(strip=True) for x in team_elems)
            if len(team_elems) == 1
            else " vs ".join(x.text(strip=True) for x in team_elems)
        )
        
        # Create unique key for deduplication
        event_key = f"{sport}|{event_name}"
        if event_key in seen_events:
            continue
        seen_events.add(event_key)

        if f"[{sport}] {event_name} ({TAG})" in cached_keys:
            continue

        events.append(
            {
                "sport": sport or "Live",
                "event": event_name,
                "link": urljoin(f"{html_data.url}", href),
            }
        )
    
    # Scrape from "TODAY'S MATCHES" section (.match-row elements)
    for match in soup.css(".match-row"):
        # Get the watch link
        watch_link = match.css_first("a.watch-live")
        if not watch_link or not (href := watch_link.attributes.get("href")):
            continue
        
        # Get sport from upcoming-sport-head (parent section)
        sport = "Unknown"
        parent = match.parent
        while parent:
            sport_head = parent.css_first(".upcoming-sport-head")
            if sport_head:
                sport_text = sport_head.text(strip=True)
                # Extract sport name (first line before the count)
                sport = sport_text.split("\n")[0].strip() if "\n" in sport_text else sport_text.strip()
                break
            parent = parent.parent
        
        # Extract team names from .team elements
        team_elems = match.css(".team .team-name")
        if len(team_elems) == 2:
            event_name = f"{team_elems[0].text(strip=True)} vs {team_elems[1].text(strip=True)}"
        elif team_elems:
            event_name = " vs ".join(x.text(strip=True) for x in team_elems)
        else:
            continue
        
        # Create unique key for deduplication
        event_key = f"{sport}|{event_name}"
        if event_key in seen_events:
            continue
        seen_events.add(event_key)
        
        if f"[{sport}] {event_name} ({TAG})" in cached_keys:
            continue

        events.append(
            {
                "sport": sport,
                "event": event_name,
                "link": urljoin(f"{html_data.url}", href),
            }
        )

    return events


def generate_m3u8_files(events_data: dict[str, dict]) -> None:
    """Generate VLC and TiviMate M3U8 files from event data"""
    
    # Sort events by sport for better organization
    sorted_events = sorted(
        [(k, v) for k, v in events_data.items() if v.get("url")],
        key=lambda x: (x[1].get("sport", ""), x[0])
    )
    
    vlc_lines = []
    tivimate_lines = []
    valid_streams = 0
    
    for idx, (key, data) in enumerate(sorted_events, start=1):
        if not data.get("url"):
            continue
            
        valid_streams += 1
        
        # Extract event info from key
        # Key format: "[Sport] Event Name (TAG)"
        key_clean = key.replace(f" ({TAG})", "")
        sport_part = key_clean.split("] ", 1)
        sport = sport_part[0].strip("[")
        event_name = sport_part[1] if len(sport_part) > 1 else key_clean
        
        tvg_id = data.get("id", "Live.Event.us")
        logo = data.get("logo", "")
        stream_url = data["url"]
        
        # VLC format
        vlc_lines.append(f'#EXTINF:-1 tvg-chno="{idx}" tvg-id="{tvg_id}" tvg-name="{event_name}" tvg-logo="{logo}" group-title="{sport}",{event_name}')
        vlc_lines.append(f'#EXTVLCOPT:http-referrer={REFERER}')
        vlc_lines.append(f'#EXTVLCOPT:http-origin={ORIGIN}')
        vlc_lines.append(f'#EXTVLCOPT:http-user-agent={USER_AGENT}')
        vlc_lines.append(stream_url)
        vlc_lines.append("")  # Empty line for separation
        
        # TiviMate format (pipe-separated with encoded user agent)
        tivimate_lines.append(f'#EXTINF:-1 tvg-chno="{idx}" tvg-id="{tvg_id}" tvg-name="{event_name}" tvg-logo="{logo}" group-title="{sport}",{event_name}')
        tivimate_line = f"{stream_url}|referer={REFERER}|origin={ORIGIN}|user-agent={USER_AGENT_ENCODED}"
        tivimate_lines.append(tivimate_line)
        tivimate_lines.append("")  # Empty line for separation
    
    # Write VLC file
    vlc_output_path = Path("obshub_vlc.m3u8")
    with open(vlc_output_path, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        f.write("\n".join(vlc_lines))
    
    # Write TiviMate file
    tivimate_output_path = Path("obshub_tivimate.m3u8")
    with open(tivimate_output_path, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        f.write("\n".join(tivimate_lines))
    
    log.info(f"Generated {vlc_output_path} with {valid_streams} streams")
    log.info(f"Generated {tivimate_output_path} with {valid_streams} streams")
    
    # Verify files were created
    if vlc_output_path.exists():
        log.info(f" {vlc_output_path} exists ({vlc_output_path.stat().st_size} bytes)")
    else:
        log.error(f" {vlc_output_path} was not created!")
        
    if tivimate_output_path.exists():
        log.info(f" {tivimate_output_path} exists ({tivimate_output_path.stat().st_size} bytes)")
    else:
        log.error(f" {tivimate_output_path} was not created!")


async def scrape() -> None:
    cached_urls = CACHE_FILE.load()

    valid_urls = {k: v for k, v in cached_urls.items() if v["url"]}

    valid_count = cached_count = len(valid_urls)

    urls.update(valid_urls)

    log.info(f"Loaded {cached_count} event(s) from cache")

    log.info(f'Scraping from "{BASE_URL}"')

    if events := await get_events(cached_urls.keys()):
        log.info(f"Processing {len(events)} new URL(s)")

        now = Time.clean(Time.now())

        for i, ev in enumerate(events, start=1):
            handler = partial(
                process_event,
                url=(link := ev["link"]),
                url_num=i,
            )

            url, iframe = await network.safe_process(
                handler,
                url_num=i,
                timeout_return=(None, None),
                semaphore=network.HTTP_S,
                log=log,
            )

            sport, event = ev["sport"], ev["event"]

            key = f"[{sport}] {event} ({TAG})"

            tvg_id, logo = leagues.get_tvg_info(sport, event)

            entry = {
                "url": url,
                "logo": logo,
                "base": iframe,
                "timestamp": now.timestamp(),
                "id": tvg_id or "Live.Event.us",
                "link": link,
                "sport": sport,  # Store sport for sorting
            }

            cached_urls[key] = entry

            if url:
                valid_count += 1
                urls[key] = entry

        log.info(f"Collected and cached {valid_count - cached_count} new event(s)")

    else:
        log.info("No new events found")

    CACHE_FILE.write(cached_urls)
    
    # Generate M3U8 files after updating cache
    generate_m3u8_files(cached_urls)


async def main() -> None:
    """Main function to run the updater"""
    log.info("Starting OBSHUB updater...")
    await scrape()
    log.info("OBSHUB updater completed successfully")


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
