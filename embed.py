#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import time
from datetime import datetime
from pathlib import Path
from functools import partial
from urllib.parse import urljoin, quote

from playwright.async_api import Browser, async_playwright

from utils import Cache, Time, get_logger, leagues, network

log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}

TAG = "EMBED"

CACHE_FILE = Cache(TAG, exp=5_400)

API_FILE = Cache(f"{TAG}-api", exp=28_800)

# Get BASE_URL from environment variable or use default
BASE_URL = os.getenv("EMBED_BASE_URL")

# Forced headers for streams
FORCED_REFERER = "https://exposestrat.com/"
FORCED_ORIGIN = "https://exposestrat.com"

# Output files
REPO_DIR = Path(__file__).parent if "__file__" in dir() else Path.cwd()
VLC_FILE = REPO_DIR / "embed_vlc.m3u8"
TIVIMATE_FILE = REPO_DIR / "embed_tivimate.m3u8"

# Default user agent
DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"


def fix_league(s: str) -> str:
    return " ".join(x.capitalize() for x in s.split()) if len(s) > 5 else s.upper()


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

            if not (event_streams := event["streams"]):
                continue

            elif not (event_link := event_streams[0].get("link")):
                continue

            events.append(
                {
                    "sport": sport,
                    "event": event_name,
                    "link": event_link,
                    "timestamp": now.timestamp(),
                }
            )

    return events


async def scrape(browser: Browser) -> None:
    cached_urls = CACHE_FILE.load()

    valid_urls = {k: v for k, v in cached_urls.items() if v["url"]}

    valid_count = cached_count = len(valid_urls)

    urls.update(valid_urls)

    log.info(f"Loaded {cached_count} event(s) from cache")

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

                    url = await network.safe_process(
                        handler,
                        url_num=i,
                        semaphore=network.PW_S,
                        log=log,
                    )

                    sport, event, ts = (
                        ev["sport"],
                        ev["event"],
                        ev["timestamp"],
                    )

                    tvg_id, logo = leagues.get_tvg_info(sport, event)

                    key = f"[{sport}] {event} ({TAG})"

                    entry = {
                        "url": url,
                        "logo": logo,
                        "base": FORCED_REFERER,
                        "timestamp": ts,
                        "id": tvg_id or "Live.Event.us",
                        "link": link,
                    }

                    cached_urls[key] = entry

                    if url:
                        valid_count += 1

                        urls[key] = entry

        log.info(f"Collected and cached {valid_count - cached_count} new event(s)")

    else:
        log.info("No new events found")

    CACHE_FILE.write(cached_urls)


def vlc_headers(stream_url: str) -> list[str]:
    """Format headers for VLC"""
    return [
        f'#EXTVLCOPT:http-referrer={FORCED_REFERER}',
        f'#EXTVLCOPT:http-origin={FORCED_ORIGIN}',
        f'#EXTVLCOPT:http-user-agent={DEFAULT_USER_AGENT}',
    ]


def tivimate_format(stream_url: str) -> str:
    """Format URL with pipe headers for Tivimate"""
    params = [
        f"referer={FORCED_REFERER}",
        f"origin={FORCED_ORIGIN}",
        f"user-agent={quote(DEFAULT_USER_AGENT)}",
    ]
    return f"{stream_url}|{'|'.join(params)}"


def generate_playlists():
    """Generate VLC and Tivimate playlist files"""
    if not urls:
        log.warning("No URLs to generate playlists")
        return False

    vlc_entries = ["#EXTM3U"]
    tivimate_entries = ["#EXTM3U"]

    for key, info in urls.items():
        if not info.get("url"):
            continue

        # Extract sport and event from key
        # Key format: "[SPORT] Event Name (EMBED)"
        try:
            sport = key.split("]")[0].strip("[")
            event_name = key.split("] ")[1].split(" (EMBED)")[0]
        except:
            sport = "Sports"
            event_name = key

        stream_url = info["url"]
        tvg_id = info.get("id", "")
        logo = info.get("logo", "")

        # Build EXTINF line
        extinf_parts = [
            f'#EXTINF:-1 group-title="{sport}"',
        ]
        
        if tvg_id:
            extinf_parts.append(f'tvg-id="{tvg_id}"')
        if logo:
            extinf_parts.append(f'tvg-logo="{logo}"')
        
        extinf_parts.append(f",{event_name}")
        extinf_line = " ".join(extinf_parts)

        # VLC format
        vlc_entries.append(extinf_line)
        vlc_entries.extend(vlc_headers(stream_url))
        vlc_entries.append(stream_url)

        # Tivimate format
        tivimate_entries.append(extinf_line)
        tivimate_entries.append(tivimate_format(stream_url))

    # Write files
    try:
        VLC_FILE.write_text("\n".join(vlc_entries) + "\n", encoding="utf-8")
        TIVIMATE_FILE.write_text("\n".join(tivimate_entries) + "\n", encoding="utf-8")
        log.info(f"Generated {VLC_FILE.name} with {len(vlc_entries) - 1} streams")
        log.info(f"Generated {TIVIMATE_FILE.name} with {len(tivimate_entries) - 1} streams")
        return True
    except Exception as e:
        log.error(f"Error writing playlist files: {e}")
        return False


def push_to_github():
    """Push generated files to GitHub repository"""
    # Skip git push if running in GitHub Actions (workflow handles it)
    if os.getenv("GITHUB_ACTIONS") == "true":
        log.info("Running in GitHub Actions - skipping Python git push")
        return True
    
    try:
        from git import Repo
        import subprocess
        
        repo = Repo(REPO_DIR)
        
        # Check if files have changed
        changed = False
        
        # Add files
        for file in [VLC_FILE, TIVIMATE_FILE]:
            if file.exists():
                repo.git.add(str(file.relative_to(REPO_DIR)))
                changed = True
        
        # Also add caches directory if it exists
        caches_dir = REPO_DIR / "caches"
        if caches_dir.exists():
            repo.git.add(str(caches_dir.relative_to(REPO_DIR)))
            changed = True
        
        if not changed:
            log.info("No changes to commit")
            return True
        
        # Commit
        commit_message = f"Update EMBED playlists {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        repo.index.commit(commit_message)
        
        # Try to push using different methods
        try:
            # Method 1: Try using token from environment
            token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
            if token:
                remote_url = repo.remote("origin").url
                if remote_url.startswith("https://"):
                    # Insert token into URL
                    authenticated_url = remote_url.replace(
                        "https://",
                        f"https://x-access-token:{token}@"
                    )
                    repo.remote("origin").set_url(authenticated_url)
                    log.info("Using token authentication for git push")
            
            # Push
            repo.remote("origin").push()
            log.info("✓ Successfully pushed to GitHub")
            return True
            
        except Exception as push_error:
            log.warning(f"Push with token failed: {push_error}")
            
            # Method 2: Try using subprocess
            try:
                result = subprocess.run(
                    ["git", "push", "origin", "HEAD"],
                    cwd=REPO_DIR,
                    capture_output=True,
                    text=True,
                    timeout=30
                )
                if result.returncode == 0:
                    log.info("✓ Successfully pushed using subprocess")
                    return True
                else:
                    log.error(f"Git push failed: {result.stderr}")
            except Exception as sub_error:
                log.error(f"Subprocess push failed: {sub_error}")
            
            return False
        
    except ImportError:
        log.warning("GitPython not installed. Skipping git push.")
        log.info("Files generated locally:")
        log.info(f"  - {VLC_FILE}")
        log.info(f"  - {TIVIMATE_FILE}")
        return False
    except Exception as e:
        log.error(f"Git push error: {e}")
        return False


async def main():
    """Main function to run the scraper and generate outputs"""
    log.info("=" * 60)
    log.info(f"EMBED STREAM UPDATER - {TAG}")
    log.info(f"Base URL: {BASE_URL}")
    log.info(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log.info("=" * 60)
    
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    '--no-sandbox',
                    '--disable-setuid-sandbox',
                    '--disable-dev-shm-usage',
                    '--disable-gpu',
                ]
            )
            
            try:
                # Run the scraper
                await scrape(browser)
                
                # Generate playlist files
                if generate_playlists():
                    log.info("Playlists generated successfully")
                    
                    # Push to GitHub (only if not in GitHub Actions)
                    push_to_github()
                else:
                    log.warning("No playlists generated")
                    
            finally:
                await browser.close()
                
    except Exception as e:
        log.error(f"Fatal error: {e}")
        raise


if __name__ == "__main__":
    import asyncio
    
    # Check for required environment variable
    if not os.getenv("EMBED_BASE_URL"):
        log.warning(f"EMBED_BASE_URL not set. Using default: {BASE_URL}")
    
    asyncio.run(main())
