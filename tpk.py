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


# ───────── STREAM EXTRACTION (MULTIPLE METHODS) ─────────

async def process_ts1(ifr_src: str, url_num: int) -> str | None:
    """Process iframe with hex encoded M3U8"""
    data = await network.request(ifr_src, log=log)
    if not data:
        return None

    # Try multiple patterns for hex encoded data
    patterns = [
        r'(var|const)\s+(\w+)\s*=\s*"([^"]*)"',
        r'(var|const)\s+(\w+)\s*=\s*\'([^\']*)\'',
        r'(\w+)\s*=\s*"([^"]*)"',
    ]
    
    for pattern in patterns:
        match = re.search(pattern, data.text, re.I)
        if match:
            encoded = match.group(2) if len(match.groups()) > 2 else match.group(1)
            if len(encoded) < 20 and len(match.groups()) > 2:
                encoded = match.group(3)
            
            try:
                decoded = bytes.fromhex(encoded).decode("utf-8")
                if '.m3u8' in decoded or 'http' in decoded or 'cloudfront' in decoded:
                    log.info(f"URL {url_num}) Captured M3U8 from hex")
                    return decoded
            except:
                continue
    
    return None


async def process_ts2(ifr_src: str, url_num: int) -> str | None:
    """Process iframe with direct stream URL"""
    data = await network.request(ifr_src, log=log)
    if not data:
        return None
    
    # Look for direct stream URLs
    patterns = [
        r'(https?://[^\s"\']+\.m3u8[^\s"\']*)',
        r'(https?://[^\s"\']+\.woff2[^\s"\']*)',
        r'(https?://[^\s"\']+cloudfront[^\s"\']+\.(?:m3u8|woff2)[^\s"\']*)',
        r'(https?://[^\s"\']+\.js[^\s"\']*?stream[^\s"\']*)',
    ]
    
    for pattern in patterns:
        match = re.search(pattern, data.text, re.I)
        if match:
            log.info(f"URL {url_num}) Captured stream direct")
            return match.group(1)
    
    return None


async def process_ts3(url: str, url_num: int) -> str | None:
    """Process event page with nested iframes"""
    data = await network.request(url, log=log)
    if not data:
        return None

    soup = HTMLParser(data.content)
    
    # Find all iframes
    iframes = soup.css("iframe")
    for iframe in iframes:
        src = iframe.attributes.get("src")
        if not src:
            continue
        
        if not src.startswith("http"):
            src = urljoin(url, src)
        
        # Try to get stream from iframe
        for method in [process_ts1, process_ts2]:
            result = await method(src, url_num)
            if result:
                return result
    
    # Look for player scripts
    scripts = soup.css("script")
    for script in scripts:
        script_text = script.text()
        if script_text:
            patterns = [
                r'file\s*:\s*"([^"]+\.m3u8[^"]*)"',
                r'source\s*:\s*"([^"]+\.m3u8[^"]*)"',
                r'url\s*:\s*"([^"]+\.m3u8[^"]*)"',
                r'(https?://[^\s"\']+cloudfront[^\s"\']+\.woff2[^\s"\']*)',
            ]
            for pattern in patterns:
                match = re.search(pattern, script_text, re.I)
                if match:
                    log.info(f"URL {url_num}) Captured from script")
                    return match.group(1)
    
    return None


async def process_event(url: str, url_num: int, tag: str) -> str | None:
    """Process event page to extract stream URL using multiple methods"""
    # Try nested iframe method first
    result = await process_ts3(url, url_num)
    if result:
        return result
    
    # Try direct page scan for any stream URL
    data = await network.request(url, log=log)
    if data:
        patterns = [
            r'(https?://[^\s"\']+cloudfront[^\s"\']+\.woff2[^\s"\']*)',
            r'(https?://[^\s"\']+\.m3u8[^\s"\']*)',
            r'(https?://[^\s"\']+\.js[^\s"\']*stream[^\s"\']*)',
        ]
        for pattern in patterns:
            match = re.search(pattern, data.text, re.I)
            if match:
                log.info(f"URL {url_num}) Captured from page scan")
                return match.group(1)
    
    log.warning(f"URL {url_num}) No stream found")
    return None


# ───────── EVENTS EXTRACTION ─────────

async def get_events(cached_keys):
    events = []

    base = BASES["TPK"]
    html = await network.request(base, log=log)
    if not html:
        return events

    soup = HTMLParser(html.content)
    
    # Method 1: Look for event links with specific patterns
    for link in soup.css('a[href*="/event/"], a[href*="/match/"], a[href*="/stream/"]'):
        href = link.attributes.get("href")
        if not href:
            continue
        
        # Get link text
        link_text = link.text(strip=True)
        if not link_text or len(link_text) < 3:
            continue
        
        # Skip navigation links
        if any(skip in link_text.lower() for skip in ['home', 'login', 'register', 'contact', 'about', 'disclaimer']):
            continue
        
        # Build full URL
        if href.startswith("http"):
            event_url = href
        else:
            event_url = urljoin(base, href)
        
        # Extract event name from URL or text
        if " vs " in link_text:
            event_name = fix_txt(link_text)
        else:
            # Try to extract from URL
            url_path = urlparse(event_url).path
            parts = url_path.split('/')
            if len(parts) > 2:
                name_part = parts[-1].replace('-', ' ')
                event_name = fix_txt(name_part)
            else:
                event_name = fix_txt(link_text)
        
        # Determine sport
        sport = "Live Event"
        sport_keywords = {
            'F1': 'F1', 'NASCAR': 'NASCAR', 'WWE': 'WWE',
            'Tennis': 'Tennis', 'Golf': 'Golf', 'NBA': 'NBA',
            'MLB': 'MLB', 'NHL': 'NHL', 'UFC': 'UFC',
            'Boxing': 'BOXING', 'Soccer': 'SOCCER', 'Football': 'FOOTBALL'
        }
        for key, value in sport_keywords.items():
            if key.lower() in link_text.lower() or key.lower() in event_name.lower():
                sport = value
                break
        
        key = f"[{sport}] {event_name} (TPK)"
        
        if key in cached_keys:
            continue
        
        events.append({
            "sport": sport,
            "event": event_name,
            "tag": "TPK",
            "link": event_url,
        })
    
    # Method 2: Look for text patterns "Match Started"
    for node in soup.css('*'):
        node_text = node.text(strip=True)
        if node_text and 'Match Started' in node_text:
            parent = node.parent
            if parent:
                for link in parent.css('a'):
                    href = link.attributes.get("href")
                    if href and ('/event/' in href or '/match/' in href):
                        event_url = urljoin(base, href)
                        event_name = fix_txt(node_text.replace('Match Started', '').strip())
                        if event_name and len(event_name) > 3:
                            sport = "Live Event"
                            key = f"[{sport}] {event_name} (TPK)"
                            if key not in cached_keys and not any(e['link'] == event_url for e in events):
                                events.append({
                                    "sport": sport,
                                    "event": event_name,
                                    "tag": "TPK",
                                    "link": event_url,
                                })
    
    # Remove duplicates
    seen = set()
    unique_events = []
    for event in events:
        if event["link"] not in seen:
            seen.add(event["link"])
            unique_events.append(event)
    
    log.info(f"Found {len(unique_events)} events")
    return unique_events


# ───────── PLAYLIST BUILD ─────────

def build_vlc(entry):
    headers = []
    ref = entry.get("link", BASES["TPK"])

    headers.append(f'#EXTVLCOPT:http-referrer={ref}')
    headers.append(f'#EXTVLCOPT:http-origin={ref}')
    headers.append('#EXTVLCOPT:http-user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36')

    return headers


def build_tivi(entry):
    ref = entry.get("link", BASES["TPK"])
    ua = quote("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36", safe="")
    return f'{entry["url"]}|referer={ref}|origin={ref}|user-agent={ua}'


def generate_playlists():
    vlc = ["#EXTM3U"]
    tivi = ["#EXTM3U"]
    vlc.append("# Playlist generated by TPK Scraper")
    tivi.append("# Playlist generated by TPK Scraper")
    vlc.append("")
    tivi.append("")

    chno = 200

    for name, entry in urls.items():
        if not entry.get("url"):
            continue

        ext = f'#EXTINF:-1 tvg-chno="{chno}" tvg-id="{entry.get("id","Live.Event.us")}" tvg-name="{name}" tvg-logo="{entry.get("logo","https://i.gyazo.com/4a5e9fa2525808ee4b65002b56d3450e.png")}" group-title="Live Events",{name}'

        vlc.append(ext)
        vlc.extend(build_vlc(entry))
        vlc.append(entry["url"])
        vlc.append("")

        tivi.append(ext)
        tivi.append(build_tivi(entry))
        tivi.append("")

        chno += 1

    (REPO_DIR / VLC_FILE).write_text("\n".join(vlc))
    (REPO_DIR / TIVIMATE_FILE).write_text("\n".join(tivi))

    log.info(f"Playlists written: {len(urls)} events")


# ───────── SCRAPE ─────────

async def scrape():
    global urls
    cache = CACHE_FILE.load() or {}
    urls = {k: v for k, v in cache.items() if v.get("url")}

    log.info(f"Loaded {len(urls)} cached events")

    events = await get_events(cache.keys())
    log.info(f"Found {len(events)} new events")

    if not events:
        log.info("No new events to process")
        generate_playlists()
        return

    now = Time.clean(Time.now())

    for i, ev in enumerate(events, 1):
        log.info(f"Processing {i}/{len(events)}: {ev['event']}")
        
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
            log.info(f"✓ Captured: {ev['event']}")
        else:
            log.warning(f"✗ Failed: {ev['event']}")

    CACHE_FILE.write(cache)
    generate_playlists()


# ───────── MAIN ─────────

async def main():
    log.info("Starting TPK scraper")
    await scrape()
    
    # Git push
    try:
        repo = Repo(REPO_DIR)
        repo.git.add(A=True)
        if repo.index.diff("HEAD"):
            repo.index.commit(f"TPK auto update {time.strftime('%Y-%m-%d %H:%M:%S')}", skip_hooks=True)
            repo.remote().push()
            log.info("Changes pushed to repository")
        else:
            log.info("No changes to push")
    except Exception as e:
        log.error(f"Git error: {e}")
    
    log.info("TPK scraper completed")


if __name__ == "__main__":
    asyncio.run(main())
