import asyncio
import os
import urllib.parse
import re
from functools import partial
from urllib.parse import urljoin, urlparse, parse_qs

from utils import Cache, Time, get_logger, leagues, network

log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}

TAG = "LTSPRETA"

CACHE_FILE = Cache(TAG, exp=19_800)

API_CACHE = Cache(f"{TAG}-api", exp=19_800)

# Get API_URL from environment variable (secret) with validation
API_URL = os.environ.get("LTSPRETA_CH_API_URL")
# Ensure URL has protocol
if API_URL and not API_URL.startswith(('http://', 'https://')):
    API_URL = f"https://{API_URL}"

# Constants for output files
VLC_OUTPUT_FILE = "ltspreta_ch_vlc.m3u8"
TIVIMATE_OUTPUT_FILE = "ltspreta_ch_tivimate.m3u8"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"

# Country filter - only include channels from these countries
ALLOWED_COUNTRIES = {"US", "IN", "TR", "HR", "RS", "GB", "BR", "AU", "GR", "PL", "BG", "AR", "MX", "RU", "CA", "ZA"}

def encode_user_agent(user_agent: str) -> str:
    """Encode user agent for TiviMate format"""
    return urllib.parse.quote(user_agent)

async def process_event(url: str, url_num: int) -> tuple[str | None, str | None]:
    """Process channel URL to get M3U8 stream"""
    nones = None, None

    # Extract event ID from URL
    event_id = url.split("id=")[-1]
    if not event_id:
        log.warning(f"URL {url_num}) Could not extract ID from URL: {url}")
        return nones

    log.debug(f"URL {url_num}) Extracted channel ID: {event_id}")

    # Get token from generate_token.php
    if not (
        token_req := await network.request(
            "https://lista-preta-tv.site/generate_token.php",
            params={"id": event_id},
            log=log,
        )
    ):
        log.warning(f"URL {url_num}) Failed to load token data.")
        return nones

    if not (token_data := token_req.json()):
        log.warning(f"URL {url_num}) No token data available.")
        return nones

    elif not (token := token_data.get("token")) or not (exp := token_data.get("exp")):
        log.warning(f"URL {url_num}) No token data available.")
        return nones

    log.debug(f"URL {url_num}) Got token: {token}, exp: {exp}")

    # Construct referer URL
    ref = f"https://lista-preta-tv.site/player-all.html?id={event_id}"

    # Get M3U8 stream - follow redirects to get final URL
    if not (
        m3u8_req := await network.request(
            "https://lista-preta-tv.site/m3u8.php",
            headers={"Referer": ref},
            params={"id": event_id, "token": token, "exp": exp},
            follow_redirects=True,
            log=log,
        )
    ):
        log.warning(f"URL {url_num}) Unable to fetch M3U8 request.")
        return nones

    # Get the final URL after redirects and convert to string
    m3u8 = str(m3u8_req.url) if hasattr(m3u8_req, 'url') else None
    
    if not m3u8:
        # Try to get from Location header as fallback
        location = m3u8_req.headers.get("Location")
        if location:
            m3u8 = str(location)
    
    if not m3u8:
        log.warning(f"URL {url_num}) Unable to fetch M3U8 request.")
        return nones

    log.info(f"URL {url_num}) Captured M3U8: {m3u8}")

    return m3u8, ref

def generate_output_files():
    """Generate both VLC and TiviMate M3U8 files"""
    global urls
    
    log.info(f"Generating output files with {len(urls)} channels")
    
    # Generate VLC format
    vlc_content = "#EXTM3U\n"
    tivimate_content = "#EXTM3U\n"
    
    if urls:
        # Sort by timestamp to maintain order
        sorted_urls = sorted(urls.items(), key=lambda x: x[1].get("timestamp", 0))
        
        chno = 1  # Start channel number from 1
        for key, data in sorted_urls:
            if not data.get("url"):
                continue
                
            # Extract data
            sport_match = key.split("[")[1].split("]")[0] if "[" in key else "Channels"
            sport = sport_match
            channel_name = key.split("]")[-1].strip().replace(f"({TAG})", "").strip() if "]" in key else key
            logo = data.get("logo", "")
            tvg_id = data.get("id", "Channel.us")
            url = data.get("url", "")
            referer_url = data.get("referer_url", "")
            
            # Keep the full URL with token parameters
            full_url = url
            
            # Skip if no URL
            if not full_url:
                continue
            
            # For VLC referer, use the constructed referer URL
            vlc_referer = referer_url if referer_url else "https://lista-preta-tv.site/"
            
            # EXTINF line (same for both formats)
            extinf = f'#EXTINF:-1 tvg-chno="{chno}" tvg-id="{tvg_id}" tvg-name="{key}" tvg-logo="{logo}" group-title="{sport}",{channel_name}\n'
            
            # VLC format
            vlc_content += extinf
            vlc_content += f"#EXTVLCOPT:http-referrer={vlc_referer}\n"
            vlc_content += f"#EXTVLCOPT:http-origin={vlc_referer}\n"
            vlc_content += f"#EXTVLCOPT:http-user-agent={USER_AGENT}\n"
            vlc_content += f"{full_url}\n\n"
            
            # TiviMate format (with pipe and encoded user agent)
            encoded_ua = encode_user_agent(USER_AGENT)
            tivimate_url = f"{full_url}|referer={vlc_referer}|origin={vlc_referer}|user-agent={encoded_ua}"
            
            tivimate_content += extinf
            tivimate_content += f"{tivimate_url}\n\n"
            
            chno += 1
        
        log.info(f"Processed {chno-1} channels for output files")
    else:
        log.warning("No channels available to write to output files")
    
    # Write VLC file
    try:
        with open(VLC_OUTPUT_FILE, "w", encoding="utf-8") as f:
            f.write(vlc_content)
        log.info(f"Successfully wrote {VLC_OUTPUT_FILE}")
    except Exception as e:
        log.error(f"Error writing {VLC_OUTPUT_FILE}: {e}")
    
    # Write TiviMate file
    try:
        with open(TIVIMATE_OUTPUT_FILE, "w", encoding="utf-8") as f:
            f.write(tivimate_content)
        log.info(f"Successfully wrote {TIVIMATE_OUTPUT_FILE}")
    except Exception as e:
        log.error(f"Error writing {TIVIMATE_OUTPUT_FILE}: {e}")

async def get_channels(cached_keys: list[str]) -> list[dict[str, str]]:
    now = Time.clean(Time.now())
    
    channels_list = []
    
    api_data = API_CACHE.load(per_entry=False)
    
    if not api_data:
        log.info("Refreshing API cache")
        
        # Validate API_URL is set
        if not API_URL:
            log.error("LTSPRETA_CH_API_URL environment variable is not set")
            return channels_list
        
        api_url = API_URL
        log.info(f"Fetching from API: {api_url}")
        
        if r := await network.request(
            api_url,
            log=log,
            headers={
                "User-Agent": USER_AGENT
            }
        ):
            try:
                api_data = r.json()
                
                # Handle different response formats
                if isinstance(api_data, dict):
                    # Check if it's a list wrapped in a dict
                    if "channels" in api_data:
                        api_data = api_data.get("channels", [])
                    elif "data" in api_data:
                        api_data = api_data.get("data", [])
                elif not isinstance(api_data, list):
                    log.error(f"Unexpected API response format: {type(api_data)}")
                    api_data = []
                
                if api_data and isinstance(api_data, list):
                    log.info(f"API returned {len(api_data)} total channels")
                else:
                    log.warning("API returned empty data or invalid format")
                    api_data = []
                    
            except Exception as e:
                log.error(f"Error parsing API response: {e}")
                api_data = []
        
        if not api_data:
            log.error("Failed to fetch from API or empty response")
            api_data = []
        
        # Cache the raw API data
        API_CACHE.write(api_data)
    
    # If no data, return empty list
    if not api_data:
        log.warning("No API data available")
        return channels_list
    
    log.info(f"Processing {len(api_data)} channels from API")
    
    # Counter for filtered channels
    filtered_count = 0
    allowed_country_list = ", ".join(sorted(ALLOWED_COUNTRIES))
    log.info(f"Country filter enabled. Allowed countries: {allowed_country_list}")
    
    for channel in api_data:
        try:
            # Extract channel information
            channel_id = channel.get("id")
            channel_name = channel.get("name")
            channel_link = channel.get("link")
            country = channel.get("country", "").upper()
            image = channel.get("image", "")
            
            # Validate required fields
            if not all([channel_id, channel_name, channel_link]):
                log.debug(f"Skipping channel {channel_name}: Missing required fields")
                continue
            
            # Apply country filter
            if country not in ALLOWED_COUNTRIES:
                log.debug(f"Skipping channel {channel_name} from country {country} (not in allowed list)")
                filtered_count += 1
                continue
            
            # Create key with country and channel name
            key = f"[{country}] {channel_name} ({TAG})"
            
            if key in cached_keys:
                log.debug(f"Channel already in cache: {key}")
                continue
            
            # Get timestamp
            timestamp = now.timestamp()
            
            channels_list.append({
                "country": country,
                "channel_name": channel_name,
                "link": channel_link,
                "timestamp": timestamp,
                "logo": image,
                "channel_id": str(channel_id)
            })
            
            log.info(f"Found new channel: {key} from {country}")
            
        except Exception as e:
            log.error(f"Error processing channel: {e}")
            continue
    
    log.info(f"Total new channels found: {len(channels_list)} (Filtered out: {filtered_count} channels from disallowed countries)")
    return channels_list

async def scrape(browser=None) -> None:
    """Main scraping function"""
    global urls
    
    # Load cached URLs
    cached_urls = CACHE_FILE.load() or {}
    
    # Clear and reload urls from cache
    urls.clear()
    urls.update(cached_urls)
    
    cached_count = len(cached_urls)
    
    log.info(f"Loaded {cached_count} channel(s) from cache")
    log.info(f'Scraping from "{API_URL}"')
    log.info(f"Country filter: {', '.join(sorted(ALLOWED_COUNTRIES))}")
    
    if channels := await get_channels(list(cached_urls.keys())):
        log.info(f"Processing {len(channels)} new channel URL(s)")
        
        # Process channels sequentially to avoid overwhelming
        success_count = 0
        for i, ch in enumerate(channels, start=1):
            log.info(f"Processing channel {i}/{len(channels)}: {ch['country']} - {ch['channel_name']}")
            
            # Use the process_event function to get M3U8
            m3u8_url, referer = await process_event(ch["link"], i)
            
            if m3u8_url:
                country, channel_name, ts = (
                    ch["country"],
                    ch["channel_name"],
                    ch["timestamp"],
                )
                
                key = f"[{country}] {channel_name} ({TAG})"
                
                # Try to get tvg info from leagues helper, or use channel name
                tvg_id, logo = leagues.get_tvg_info(country, channel_name)
                
                # Use logo from API if available
                final_logo = ch.get("logo", logo) if ch.get("logo") else logo
                final_id = tvg_id or f"{country}.{channel_name.replace(' ', '.')}".lower()
                
                entry = {
                    "url": str(m3u8_url),  # Ensure URL is string
                    "logo": final_logo,
                    "base": referer if referer else "https://lista-preta-tv.site/",
                    "timestamp": ts,
                    "id": final_id,
                    "link": ch["link"],
                    "referer_url": referer if referer else f"https://lista-preta-tv.site/player-all.html?id={ch.get('channel_id', '')}",
                    "country": country
                }
                
                # Update both dictionaries
                cached_urls[key] = entry
                urls[key] = entry
                success_count += 1
                log.info(f"Successfully added URL for: {key}")
            else:
                log.warning(f"Failed to get M3U8 for channel: {ch['country']} - {ch['channel_name']}")
        
        log.info(f"Collected and cached {success_count} new channel(s) (Total in cache: {len(cached_urls)})")
    
    else:
        log.info("No new channels found")
    
    # Save updated cache
    if cached_urls:
        CACHE_FILE.write(cached_urls)
        log.info(f"Saved {len(cached_urls)} channels to cache")
    else:
        log.info("No channels to cache")
    
    # CRITICAL FIX: ALWAYS GENERATE OUTPUT FILES
    log.info("Generating output files...")
    generate_output_files()
    log.info(f"Final channels count: {len(urls)}")

async def main():
    """Main function to run the updater"""
    log.info("Starting LTSPRETA Channels updater")
    
    # Validate API_URL
    if not API_URL or API_URL == "None":
        log.error("LTSPRETA_CH_API_URL environment variable is not set correctly")
        return
    
    log.info(f"Using API URL: {API_URL}")
    log.info(f"Country filter enabled for {len(ALLOWED_COUNTRIES)} countries")
    
    # No browser needed for HTTP requests
    await scrape()

def run():
    """Synchronous entry point for the updater"""
    asyncio.run(main())

if __name__ == "__main__":
    run()
