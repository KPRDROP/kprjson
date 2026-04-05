#!/usr/bin/env python3
"""
Playlist Transformer Script
Fetches playlist, transforms URLs, and pushes to GitHub
"""

import re
import os
import base64
import requests
from pathlib import Path
from typing import List, Dict, Optional
import json
from datetime import datetime

class PlaylistTransformer:
    def __init__(self, github_token: str = None, github_repo: str = None):
        """
        Initialize the transformer with GitHub credentials
        
        Args:
            github_token: GitHub personal access token (or set env var GITHUB_TOKEN)
            github_repo: GitHub repo in format "username/repo" (or set env var GITHUB_REPO)
        """
        self.github_token = github_token or os.getenv('GITHUB_TOKEN')
        self.github_repo = github_repo or os.getenv('GITHUB_REPO')
        self.base_url = "https://raw.githubusercontent.com/Metroid2023/DaddyLiveHD/refs/heads/main/playlist.m3u8"
        self.output_file = "space.m3u8"
        
    def fetch_playlist(self) -> str:
        """Fetch the playlist from the URL"""
        try:
            response = requests.get(self.base_url, timeout=30)
            response.raise_for_status()
            print(f"✓ Fetched playlist from {self.base_url}")
            return response.text
        except requests.RequestException as e:
            print(f"✗ Error fetching playlist: {e}")
            raise
    
    def extract_premium_id(self, url: str) -> Optional[str]:
        """
        Extract premium ID from URL pattern /premium{N}/
        
        Args:
            url: Stream URL
            
        Returns:
            Premium ID or None if not found
        """
        # Pattern to match /premium51/ or /premium123/
        pattern = r'/premium(\d+)/'
        match = re.search(pattern, url)
        if match:
            return match.group(1)
        return None
    
    def transform_url(self, url: str) -> str:
        """
        Transform URL to new format
        
        Args:
            url: Original stream URL
            
        Returns:
            Transformed URL
        """
        premium_id = self.extract_premium_id(url)
        if premium_id:
            new_url = f"https://my-event-worker.per-405.workers.dev/{premium_id}"
            print(f"  ⟳ Transformed: {premium_id} -> {new_url}")
            return new_url
        print(f"  ⚠ No premium ID found in: {url}")
        return url
    
    def process_playlist(self, content: str) -> str:
        """
        Process playlist content and transform URLs
        
        Args:
            content: Raw playlist content
            
        Returns:
            Transformed playlist content
        """
        lines = content.split('\n')
        new_lines = []
        current_extinf = None
        line_count = 0
        transformed_count = 0
        
        for line in lines:
            # Skip the #EXTM3U line with url-tvg parameter
            if line.startswith('#EXTM3U'):
                new_lines.append('#EXTM3U')
                print("✓ Added #EXTM3U header")
                continue
            
            # Handle #EXTINF lines (keep as-is)
            if line.startswith('#EXTINF'):
                new_lines.append(line)
                current_extinf = line
                line_count += 1
                continue
            
            # Handle stream URLs
            if line and not line.startswith('#'):
                # Clean the URL (remove any CSS or extra parameters)
                url = line.strip()
                
                # Transform the URL
                transformed_url = self.transform_url(url)
                if transformed_url != url:
                    transformed_count += 1
                
                new_lines.append(transformed_url)
                line_count += 1
                current_extinf = None
                continue
            
            # Keep any other lines (comments, etc.)
            if line:
                new_lines.append(line)
        
        print(f"\n✓ Processed {line_count} entries")
        print(f"✓ Transformed {transformed_count} URLs")
        
        return '\n'.join(new_lines)
    
    def save_playlist(self, content: str, filename: str = None) -> str:
        """
        Save playlist to local file
        
        Args:
            content: Playlist content
            filename: Output filename (default: space.m3u8)
            
        Returns:
            Path to saved file
        """
        if filename is None:
            filename = self.output_file
            
        with open(filename, 'w', encoding='utf-8') as f:
            f.write(content)
        
        print(f"✓ Saved playlist to {filename}")
        return filename
    
    def push_to_github(self, filename: str, commit_message: str = None) -> Dict:
        """
        Push file to GitHub repository
        
        Args:
            filename: File to push
            commit_message: Commit message
            
        Returns:
            GitHub API response
        """
        if not self.github_token or not self.github_repo:
            raise ValueError("GitHub token and repo are required. Set GITHUB_TOKEN and GITHUB_REPO env vars")
        
        if commit_message is None:
            commit_message = f"Update {filename} - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        
        # Read file content
        with open(filename, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Encode content to base64
        content_base64 = base64.b64encode(content.encode('utf-8')).decode('utf-8')
        
        # GitHub API endpoint
        api_url = f"https://api.github.com/repos/{self.github_repo}/contents/{filename}"
        
        # First, try to get the current file SHA if it exists
        headers = {
            'Authorization': f'token {self.github_token}',
            'Accept': 'application/vnd.github.v3+json'
        }
        
        sha = None
        try:
            response = requests.get(api_url, headers=headers)
            if response.status_code == 200:
                sha = response.json().get('sha')
                print(f"✓ Found existing file {filename} with SHA: {sha}")
        except:
            pass
        
        # Prepare payload
        payload = {
            'message': commit_message,
            'content': content_base64,
            'branch': 'main'  # or 'master' depending on your repo
        }
        
        if sha:
            payload['sha'] = sha
        
        # Push to GitHub
        response = requests.put(api_url, headers=headers, json=payload)
        
        if response.status_code in [200, 201]:
            print(f"✓ Successfully pushed {filename} to {self.github_repo}")
            return response.json()
        else:
            print(f"✗ Failed to push to GitHub: {response.status_code}")
            print(f"Response: {response.text}")
            raise Exception(f"GitHub push failed: {response.text}")
    
    def run(self, push_to_github: bool = True, output_filename: str = "space.m3u8"):
        """
        Main execution flow
        
        Args:
            push_to_github: Whether to push to GitHub
            output_filename: Output filename
        """
        print("=" * 60)
        print("Playlist Transformer Started")
        print("=" * 60)
        
        try:
            # Step 1: Fetch playlist
            print("\n📥 Step 1: Fetching playlist...")
            raw_content = self.fetch_playlist()
            
            # Step 2: Process and transform
            print("\n🔄 Step 2: Processing playlist...")
            transformed_content = self.process_playlist(raw_content)
            
            # Step 3: Save locally
            print("\n💾 Step 3: Saving playlist...")
            local_file = self.save_playlist(transformed_content, output_filename)
            
            # Step 4: Push to GitHub (optional)
            if push_to_github:
                print("\n📤 Step 4: Pushing to GitHub...")
                result = self.push_to_github(local_file)
                print(f"✓ Commit URL: {result.get('commit', {}).get('html_url', 'N/A')}")
            else:
                print("\n⚠ Skipping GitHub push (disabled)")
            
            print("\n" + "=" * 60)
            print("✅ Playlist transformation completed successfully!")
            print("=" * 60)
            
        except Exception as e:
            print(f"\n❌ Error: {e}")
            raise

def create_workflow_file():
    """Create GitHub Actions workflow file"""
    workflow_content = GITHUB_ACTIONS_YAML.strip()
    
    # Create .github/workflows directory if it doesn't exist
    workflow_dir = Path('.github/workflows')
    workflow_dir.mkdir(parents=True, exist_ok=True)
    
    # Write workflow file
    workflow_file = workflow_dir / 'update_playlist.yml'
    with open(workflow_file, 'w') as f:
        f.write(workflow_content)
    
    print(f"✓ Created GitHub Actions workflow: {workflow_file}")
    return workflow_file

if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description='Transform and push playlist to GitHub')
    parser.add_argument('--no-push', action='store_true', help='Skip pushing to GitHub')
    parser.add_argument('--output', default='space.m3u8', help='Output filename (default: space.m3u8)')
    parser.add_argument('--create-workflow', action='store_true', help='Create GitHub Actions workflow file')
    
    args = parser.parse_args()
    
    # Create workflow file if requested
    if args.create_workflow:
        create_workflow_file()
    
    # Initialize transformer with environment variables
    transformer = PlaylistTransformer()
    
    # Run the transformation
    transformer.run(push_to_github=not args.no_push, output_filename=args.output)
