#!/usr/bin/env python3
import os
import json
import re
from urllib.parse import quote_plus

# ============================================================
# CONFIG
# ============================================================

BASE_URL = os.getenv("BASE_URL_JSON", "").strip()
if not BASE_URL:
    raise RuntimeError("BASE_URL_JSON secret is not set or empty")

OUTPUT_M3U = "perre.m3u8"
DEFAULT_TVG_LOGO = "https://i.postimg.cc/L4cgDpLB/thumb-1920-392729.jpg"
GROUP_TITLE = "PR"
TAG = "PR"

# User agent for TiviMate headers
UA_RAW = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) HeadlessChrome/147.0.0.0 Safari/537.36"
)
UA_ENC = quote_plus(UA_RAW)

# ============================================================
# LOGGING
# ============================================================

def log(*args):
    print(*args, flush=True)

# ============================================================
# CLEAN HELPERS
# ============================================================

def clean_channel_name(name: str) -> str:
    """Clean channel name by removing emojis and extra whitespace."""
    # Remove emojis (basic Unicode emoji range)
    emoji_pattern = re.compile(
        "["
        "\U0001F600-\U0001F64F"  # emoticons
        "\U0001F300-\U0001F5FF"  # symbols & pictographs
        "\U0001F680-\U0001F6FF"  # transport & map symbols
        "\U0001F1E0-\U0001F1FF"  # flags (iOS)
        "\U00002702-\U000027B0"
        "\U000024C2-\U0001F251"
        "\U0001F900-\U0001F9FF"  # supplemental symbols
        "\U0001FA70-\U0001FAFF"  # additional symbols
        "\U00002300-\U000023FF"  # misc symbols
        "\U00002600-\U000027FF"  # misc symbols
        "\U00002900-\U000029FF"  # arrows
        "\U00002B00-\U00002BFF"  # arrows
        "]+",
        flags=re.UNICODE,
    )
    name = emoji_pattern.sub("", name).strip()
    
    # Remove any remaining non-ASCII characters
    name = re.sub(r'[^\x00-\x7F]+', '', name).strip()
    
    # Clean up multiple spaces
    name = re.sub(r'\s+', ' ', name).strip()
    
    return name

# ============================================================
# FETCH JSON
# ============================================================

def fetch_json_data(url: str):
    """Fetch JSON data from the given URL."""
    import requests
    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        log(f"Error fetching JSON data: {e}")
        return None

# ============================================================
# BUILD M3U8
# ============================================================

def build_m3u8(data):
    """Build the M3U8 playlist from JSON data."""
    if not data:
        log("No data to process")
        return None
    
    lines = ["#EXTM3U"]
    channel_counter = 1
    
    for item in data:
        # Get channel details
        tvg_id = item.get("nanoid", "")
        name = item.get("name", "")
        
        # Skip if no name or no streams
        if not name:
            continue
        
        sources = item.get("sources", {})
        streams = sources.get("streams", [])
        
        if not streams:
            continue
        
        # Use the first stream URL
        stream_url = streams[0]
        
        # Clean the channel name
        clean_name = clean_channel_name(name)
        
        # Build the EXTINF line
        extinf = (
            f'#EXTINF:-1 '
            f'tvg-id="{tvg_id}" '
            f'tvg-name="{clean_name}" '
            f'tvg-logo="{DEFAULT_TVG_LOGO}" '
            f'group-title="{GROUP_TITLE}", '
            f'{clean_name} ({TAG})'
        )
        
        # Build the stream URL with TiviMate headers
        url_with_params = (
            f"{stream_url}"
            f"|user-agent={UA_ENC}"
        )
        
        lines.append(extinf)
        lines.append(url_with_params)
        
        channel_counter += 1
    
    return "\n".join(lines) + "\n"

# ============================================================
# MAIN
# ============================================================

def main():
    log("Starting PR JSON to M3U8 converter...")
    log(f"Fetching data from: {BASE_URL}")
    
    # Fetch JSON data
    data = fetch_json_data(BASE_URL)
    
    if not data:
        log("Failed to fetch data. Exiting.")
        return
    
    log(f"Found {len(data)} channels")
    
    # Build M3U8
    m3u8_content = build_m3u8(data)
    
    if not m3u8_content:
        log("No channels to write. Exiting.")
        return
    
    # Write output file
    with open(OUTPUT_M3U, "w", encoding="utf-8") as f:
        f.write(m3u8_content)
    
    log(f"Playlist saved: {OUTPUT_M3U}")
    log("Done.")

# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    main()
