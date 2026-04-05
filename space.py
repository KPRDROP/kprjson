#!/usr/bin/env python3
import re
import os
import base64
import requests
from datetime import datetime

class PlaylistTransformer:
    def __init__(self):
        self.github_token = os.getenv('GITHUB_TOKEN')
        self.github_repo = os.getenv('GITHUB_REPO')
        self.base_url = "https://raw.githubusercontent.com/Metroid2023/DaddyLiveHD/refs/heads/main/playlist.m3u8"
        self.output_file = "space.m3u8"
        
    def fetch_playlist(self):
        """Fetch the playlist from the URL"""
        print(f"📥 Fetching: {self.base_url}")
        response = requests.get(self.base_url, timeout=30)
        response.raise_for_status()
        print("✓ Playlist fetched successfully")
        return response.text
    
    def extract_premium_id(self, url):
        """Extract premium ID from URL pattern /premium{N}/"""
        pattern = r'/premium(\d+)/'
        match = re.search(pattern, url)
        return match.group(1) if match else None
    
    def transform_url(self, url):
        """Transform URL to new worker format"""
        premium_id = self.extract_premium_id(url)
        if premium_id:
            new_url = f"https://my-event-worker.per-405.workers.dev/{premium_id}"
            print(f"  ⟳ {premium_id} -> {new_url}")
            return new_url
        return url
    
    def process_playlist(self, content):
        """Process playlist and transform all URLs"""
        lines = content.split('\n')
        new_lines = []
        transformed = 0
        
        for line in lines:
            if line.startswith('#EXTM3U'):
                new_lines.append('#EXTM3U')
            elif line.startswith('#EXTINF'):
                new_lines.append(line)
            elif line and not line.startswith('#'):
                new_url = self.transform_url(line.strip())
                if new_url != line.strip():
                    transformed += 1
                new_lines.append(new_url)
            elif line:
                new_lines.append(line)
        
        print(f"✓ Transformed {transformed} URLs")
        return '\n'.join(new_lines)
    
    def save_local(self, content):
        """Save playlist to local file"""
        with open(self.output_file, 'w', encoding='utf-8') as f:
            f.write(content)
        print(f"✓ Saved to {self.output_file}")
        return self.output_file
    
    def push_to_github(self, filename):
        """Push file to GitHub using token"""
        if not self.github_token or not self.github_repo:
            print("⚠ No GitHub credentials found, skipping push")
            return None
        
        # Read file
        with open(filename, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Encode to base64
        content_base64 = base64.b64encode(content.encode('utf-8')).decode('utf-8')
        
        # GitHub API
        api_url = f"https://api.github.com/repos/{self.github_repo}/contents/{filename}"
        headers = {
            'Authorization': f'token {self.github_token}',
            'Accept': 'application/vnd.github.v3+json'
        }
        
        # Get existing file SHA if any
        sha = None
        try:
            response = requests.get(api_url, headers=headers)
            if response.status_code == 200:
                sha = response.json().get('sha')
        except:
            pass
        
        # Prepare commit
        payload = {
            'message': f'Update {filename} - {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}',
            'content': content_base64,
            'branch': 'main'
        }
        if sha:
            payload['sha'] = sha
        
        # Push to GitHub
        response = requests.put(api_url, headers=headers, json=payload)
        
        if response.status_code in [200, 201]:
            print(f"✓ Pushed to GitHub: {self.github_repo}/{filename}")
            return response.json()
        else:
            print(f"✗ GitHub push failed: {response.status_code}")
            return None
    
    def run(self):
        """Main execution"""
        print("=" * 50)
        print("Playlist Transformer Started")
        print("=" * 50)
        
        try:
            # Fetch
            raw = self.fetch_playlist()
            
            # Process
            print("\nProcessing URLs...")
            transformed = self.process_playlist(raw)
            
            # Save
            print("\nSaving locally...")
            local_file = self.save_local(transformed)
            
            # Push to GitHub
            print("\nPushing to GitHub...")
            self.push_to_github(local_file)
            
            print("\n" + "=" * 50)
            print("Complete! Check space.m3u8")
            print("=" * 50)
            
        except Exception as e:
            print(f"\n Error: {e}")
            raise

if __name__ == "__main__":
    transformer = PlaylistTransformer()
    transformer.run()
