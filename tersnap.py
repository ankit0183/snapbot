#!/usr/bin/env python3
"""
Snapchat Story Downloader - Working Solution (December 2024)
Uses Selenium to handle JavaScript-rendered content
"""

import os
import sys
import json
import re
import time
import requests
from datetime import datetime
from pathlib import Path
import argparse
from typing import Optional, List, Dict
from urllib.parse import urlparse, parse_qs
import base64

# Import Selenium components
try:
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.chrome.options import Options
    from selenium.common.exceptions import TimeoutException, NoSuchElementException
    SELENIUM_AVAILABLE = True
except ImportError:
    SELENIUM_AVAILABLE = False
    print("Selenium not installed. Install with: pip install selenium webdriver-manager")

# Color output
class Color:
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    WHITE = '\033[97m'
    BOLD = '\033[1m'
    END = '\033[0m'

class SnapchatDownloader2024:
    """Updated downloader that handles JavaScript-rendered content"""
    
    def __init__(self, headless: bool = True):
        self.headless = headless
        self.driver = None
        self.session = requests.Session()
        self.download_folder = "snapchat_stories_2024"
        Path(self.download_folder).mkdir(exist_ok=True)
        
        # Updated headers
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate, br',
            'DNT': '1',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'none',
            'Sec-Fetch-User': '?1',
            'Cache-Control': 'max-age=0',
        }
        
        self.session.headers.update(self.headers)
    
    def print_status(self, message: str, msg_type: str = "info"):
        """Print status with color"""
        colors = {
            "info": Color.BLUE,
            "success": Color.GREEN,
            "warning": Color.YELLOW,
            "error": Color.RED,
            "debug": Color.CYAN
        }
        
        prefixes = {
            "info": "[*]",
            "success": "[+]",
            "warning": "[!]",
            "error": "[-]",
            "debug": "[D]"
        }
        
        color = colors.get(msg_type, Color.WHITE)
        prefix = prefixes.get(msg_type, "[*]")
        print(f"{color}{prefix} {message}{Color.END}")
    
    def setup_selenium(self):
        """Setup Selenium WebDriver"""
        if not SELENIUM_AVAILABLE:
            self.print_status("Selenium not available. Install: pip install selenium webdriver-manager", "error")
            return False
        
        try:
            # Try to use webdriver-manager for automatic driver management
            try:
                from webdriver_manager.chrome import ChromeDriverManager
                from selenium.webdriver.chrome.service import Service
                
                chrome_options = Options()
                if self.headless:
                    chrome_options.add_argument('--headless=new')
                
                # Additional options to mimic real browser
                chrome_options.add_argument('--no-sandbox')
                chrome_options.add_argument('--disable-dev-shm-usage')
                chrome_options.add_argument('--disable-blink-features=AutomationControlled')
                chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
                chrome_options.add_experimental_option('useAutomationExtension', False)
                chrome_options.add_argument('--disable-gpu')
                chrome_options.add_argument('--window-size=1920,1080')
                chrome_options.add_argument('--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')
                
                # Disable automation flags
                chrome_options.add_argument('--disable-blink-features=AutomationControlled')
                
                service = Service(ChromeDriverManager().install())
                self.driver = webdriver.Chrome(service=service, options=chrome_options)
                
                # Execute CDP commands to avoid detection
                self.driver.execute_cdp_cmd('Network.setUserAgentOverride', {
                    "userAgent": 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
                })
                self.driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
                
            except ImportError:
                # Fallback to local Chrome driver
                self.print_status("webdriver-manager not found, trying local Chrome", "warning")
                chrome_options = Options()
                if self.headless:
                    chrome_options.add_argument('--headless')
                chrome_options.add_argument('--no-sandbox')
                chrome_options.add_argument('--disable-dev-shm-usage')
                chrome_options.add_argument('--disable-blink-features=AutomationControlled')
                chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
                chrome_options.add_experimental_option('useAutomationExtension', False)
                
                self.driver = webdriver.Chrome(options=chrome_options)
            
            self.print_status("Selenium WebDriver initialized successfully", "success")
            return True
            
        except Exception as e:
            self.print_status(f"Failed to initialize Selenium: {str(e)}", "error")
            return False
    
    def extract_media_urls_from_network(self, username: str) -> List[Dict]:
        """Extract media URLs by monitoring network traffic"""
        self.print_status(f"Extracting media from network traffic for @{username}...", "info")
        
        try:
            # Navigate to the profile
            url = f"https://story.snapchat.com/@{username}"
            self.driver.get(url)
            
            # Wait for page to load
            time.sleep(5)
            
            # Get page source to see what's loaded
            page_source = self.driver.page_source
            
            # Method 1: Look for video elements in the page
            video_urls = []
            try:
                video_elements = self.driver.find_elements(By.TAG_NAME, "video")
                for video in video_elements:
                    src = video.get_attribute("src")
                    if src and "snapchat.com" in src:
                        video_urls.append({
                            "url": src,
                            "type": "video",
                            "source": "video_tag"
                        })
            except:
                pass
            
            # Method 2: Look for source tags
            source_urls = []
            try:
                source_elements = self.driver.find_elements(By.TAG_NAME, "source")
                for source in source_elements:
                    src = source.get_attribute("src")
                    if src and "snapchat.com" in src:
                        source_urls.append({
                            "url": src,
                            "type": "video" if ".mp4" in src else "image",
                            "source": "source_tag"
                        })
            except:
                pass
            
            # Method 3: Look for data URLs in scripts
            script_urls = []
            try:
                scripts = self.driver.find_elements(By.TAG_NAME, "script")
                for script in scripts:
                    script_content = script.get_attribute("innerHTML")
                    if script_content:
                        # Look for URLs in scripts
                        url_patterns = [
                            r'https://cf-st\.sc-cdn\.net/[^"\']+\.mp4[^"\']*',
                            r'https://cf-st\.sc-cdn\.net/[^"\']+\.jpg[^"\']*',
                            r'https://cf-st\.sc-cdn\.net/[^"\']+\.png[^"\']*',
                            r'mediaUrl["\']?\s*:\s*["\']([^"\']+)["\']',
                            r'hdUrl["\']?\s*:\s*["\']([^"\']+)["\']',
                            r'snapUrls["\']?[^}]+mediaUrl["\']?\s*:\s*["\']([^"\']+)["\']'
                        ]
                        
                        for pattern in url_patterns:
                            matches = re.findall(pattern, script_content)
                            for match in matches:
                                if "snapchat.com" in match or "sc-cdn.net" in match:
                                    script_urls.append({
                                        "url": match,
                                        "type": "video" if ".mp4" in match else "image",
                                        "source": "script"
                                    })
            except:
                pass
            
            # Combine all URLs
            all_urls = video_urls + source_urls + script_urls
            
            # Remove duplicates
            unique_urls = []
            seen = set()
            for item in all_urls:
                if item["url"] not in seen:
                    seen.add(item["url"])
                    unique_urls.append(item)
            
            return unique_urls
            
        except Exception as e:
            self.print_status(f"Error extracting media: {str(e)}", "error")
            return []
    
    def extract_from_page_source(self, username: str) -> List[Dict]:
        """Extract media information from page source using regex patterns"""
        self.print_status(f"Analyzing page source for @{username}...", "info")
        
        try:
            url = f"https://story.snapchat.com/@{username}"
            response = self.session.get(url, timeout=30)
            
            if response.status_code != 200:
                self.print_status(f"Failed to fetch page: HTTP {response.status_code}", "error")
                return []
            
            html = response.text
            
            # Save for debugging
            debug_file = f"debug_{username}.html"
            with open(debug_file, "w", encoding="utf-8") as f:
                f.write(html)
            self.print_status(f"Page source saved to {debug_file}", "debug")
            
            # Look for __NEXT_DATA__ script
            next_data_pattern = r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>'
            next_data_matches = re.findall(next_data_pattern, html, re.DOTALL)
            
            media_items = []
            
            if next_data_matches:
                try:
                    data = json.loads(next_data_matches[0])
                    
                    # Save JSON for debugging
                    json_file = f"debug_{username}_next_data.json"
                    with open(json_file, "w", encoding="utf-8") as f:
                        json.dump(data, f, indent=2, ensure_ascii=False)
                    self.print_status(f"JSON data saved to {json_file}", "debug")
                    
                    # Try to extract from various possible structures
                    def extract_from_dict(obj, path=""):
                        results = []
                        if isinstance(obj, dict):
                            # Check for media URLs
                            for key, value in obj.items():
                                if isinstance(value, str) and ("snapchat.com" in value or "sc-cdn.net" in value):
                                    media_type = "video" if ".mp4" in value.lower() else "image"
                                    results.append({
                                        "url": value,
                                        "type": media_type,
                                        "source": f"dict:{path}.{key}",
                                        "data_path": f"{path}.{key}"
                                    })
                                elif isinstance(value, (dict, list)):
                                    results.extend(extract_from_dict(value, f"{path}.{key}"))
                        elif isinstance(obj, list):
                            for i, item in enumerate(obj):
                                results.extend(extract_from_dict(item, f"{path}[{i}]"))
                        return results
                    
                    media_items = extract_from_dict(data)
                    
                except json.JSONDecodeError as e:
                    self.print_status(f"Failed to parse JSON: {str(e)}", "error")
            
            # Also search directly in HTML
            direct_patterns = [
                r'https://cf-st\.sc-cdn\.net/[^"\'\s]+\.mp4[^"\'\s]*',
                r'https://cf-st\.sc-cdn\.net/[^"\'\s]+\.jpg[^"\'\s]*',
                r'https://cf-st\.sc-cdn\.net/[^"\'\s]+\.png[^"\'\s]*',
                r'src="(https://[^"]+\.snapchat\.com[^"]+)"',
                r'data-src="(https://[^"]+)"',
                r'mediaUrl["\']?\s*:\s*["\']([^"\']+)["\']',
            ]
            
            for pattern in direct_patterns:
                matches = re.findall(pattern, html)
                for match in matches:
                    if "snapchat.com" in match or "sc-cdn.net" in match:
                        media_type = "video" if ".mp4" in match else "image"
                        media_items.append({
                            "url": match,
                            "type": media_type,
                            "source": "direct_regex"
                        })
            
            # Remove duplicates
            unique_items = []
            seen_urls = set()
            for item in media_items:
                if item["url"] not in seen_urls:
                    seen_urls.add(item["url"])
                    unique_items.append(item)
            
            return unique_items
            
        except Exception as e:
            self.print_status(f"Error analyzing page: {str(e)}", "error")
            return []
    
    def get_story_data(self, username: str) -> List[Dict]:
        """Get story data using multiple methods"""
        all_media = []
        
        # Method 1: Selenium extraction
        if SELENIUM_AVAILABLE and self.setup_selenium():
            selenium_media = self.extract_media_urls_from_network(username)
            all_media.extend(selenium_media)
            self.driver.quit()
        
        # Method 2: Page source analysis
        page_media = self.extract_from_page_source(username)
        all_media.extend(page_media)
        
        # Method 3: Try alternative endpoints
        alternative_media = self.try_alternative_endpoints(username)
        all_media.extend(alternative_media)
        
        # Remove duplicates and format
        unique_media = []
        seen = set()
        
        for item in all_media:
            url = item["url"]
            if url not in seen:
                seen.add(url)
                # Clean and validate URL
                if self.validate_url(url):
                    unique_media.append({
                        "id": self.generate_id(url),
                        "media_url": url,
                        "media_type": item["type"],
                        "username": username,
                        "timestamp": int(time.time()),
                        "source": item.get("source", "unknown")
                    })
        
        return unique_media
    
    def try_alternative_endpoints(self, username: str) -> List[Dict]:
        """Try alternative endpoints and data sources"""
        media_items = []
        
        endpoints = [
            f"https://www.snapchat.com/add/{username}",
            f"https://snapchat.com/add/{username}",
            f"https://www.snapchat.com/@{username}",
        ]
        
        for endpoint in endpoints:
            try:
                response = self.session.get(endpoint, timeout=15)
                if response.status_code == 200:
                    # Look for JSON-LD or other structured data
                    json_ld_pattern = r'<script type="application/ld\+json">(.*?)</script>'
                    matches = re.findall(json_ld_pattern, response.text, re.DOTALL)
                    
                    for match in matches:
                        try:
                            data = json.loads(match)
                            # Extract potential media URLs
                            if isinstance(data, dict):
                                for key, value in data.items():
                                    if isinstance(value, str) and ("http" in value and ("video" in value or "image" in value)):
                                        media_type = "video" if "video" in value.lower() else "image"
                                        media_items.append({
                                            "url": value,
                                            "type": media_type,
                                            "source": f"json_ld:{endpoint}"
                                        })
                        except:
                            pass
            except:
                continue
        
        return media_items
    
    def validate_url(self, url: str) -> bool:
        """Validate if URL is a valid media URL"""
        valid_domains = ["snapchat.com", "sc-cdn.net", "cf-st.sc-cdn.net"]
        valid_extensions = [".mp4", ".jpg", ".jpeg", ".png", ".gif", ".webm", ".mov"]
        
        if not any(domain in url for domain in valid_domains):
            return False
        
        if not any(ext in url.lower() for ext in valid_extensions):
            # Check if it's a data URL or API endpoint that might redirect
            if "media" in url.lower() or "video" in url.lower() or "image" in url.lower():
                return True
        
        return True
    
    def generate_id(self, url: str) -> str:
        """Generate a unique ID from URL"""
        import hashlib
        return hashlib.md5(url.encode()).hexdigest()[:8]
    
    def download_media(self, media_item: Dict, folder: str, index: int, total: int) -> bool:
        """Download a single media item"""
        try:
            url = media_item["media_url"]
            media_type = media_item["media_type"]
            username = media_item["username"]
            
            # Create filename
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            file_id = media_item["id"]
            
            if media_type == "video":
                extension = ".mp4"
            else:
                # Try to determine extension from URL
                if ".jpg" in url.lower() or ".jpeg" in url.lower():
                    extension = ".jpg"
                elif ".png" in url.lower():
                    extension = ".png"
                elif ".gif" in url.lower():
                    extension = ".gif"
                else:
                    extension = ".mp4"  # Default to mp4
            
            filename = f"{username}_{timestamp}_{file_id}{extension}"
            filepath = os.path.join(folder, filename)
            
            # Create directory if it doesn't exist
            os.makedirs(os.path.dirname(filepath), exist_ok=True)
            
            self.print_status(f"Downloading ({index}/{total}): {filename}", "info")
            
            # Download with headers
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': '*/*',
                'Accept-Language': 'en-US,en;q=0.9',
                'Referer': 'https://story.snapchat.com/',
                'Origin': 'https://story.snapchat.com',
                'Sec-Fetch-Dest': 'video' if media_type == "video" else 'image',
                'Sec-Fetch-Mode': 'cors',
                'Sec-Fetch-Site': 'cross-site',
            }
            
            response = self.session.get(url, headers=headers, stream=True, timeout=60)
            
            if response.status_code != 200:
                self.print_status(f"Failed to download: HTTP {response.status_code}", "error")
                return False
            
            # Get file size
            total_size = int(response.headers.get('content-length', 0))
            
            # Download with progress
            downloaded = 0
            with open(filepath, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        
                        if total_size > 0:
                            percent = (downloaded / total_size) * 100
                            sys.stdout.write(f'\r    Progress: {downloaded/1024:.1f}KB / {total_size/1024:.1f}KB ({percent:.1f}%)')
                            sys.stdout.flush()
            
            if total_size > 0:
                print()
            
            file_size = os.path.getsize(filepath)
            if file_size > 0:
                self.print_status(f"✓ Downloaded: {filename} ({file_size/1024:.1f}KB)", "success")
                return True
            else:
                os.remove(filepath)
                self.print_status(f"✗ Empty file: {filename}", "error")
                return False
            
        except Exception as e:
            self.print_status(f"✗ Download failed: {str(e)}", "error")
            return False
    
    def run(self, username: str, output_dir: Optional[str] = None, use_selenium: bool = True):
        """Main execution method"""
        print(f"\n{Color.CYAN}{'='*60}{Color.END}")
        print(f"{Color.GREEN}Snapchat Story Downloader 2024{Color.END}")
        print(f"{Color.YELLOW}Advanced JavaScript-aware downloader{Color.END}")
        print(f"{Color.CYAN}{'='*60}{Color.END}\n")
        
        self.print_status(f"Target: @{username}", "info")
        
        # Get media data
        self.print_status("Collecting media information...", "info")
        
        if use_selenium and not SELENIUM_AVAILABLE:
            self.print_status("Selenium not available, using fallback methods", "warning")
        
        media_items = self.get_story_data(username)
        
        if not media_items:
            self.print_status("No media found. Possible reasons:", "error")
            self.print_status("1. User has no public stories", "info")
            self.print_status("2. Stories are geoblocked in your region", "info")
            self.print_status("3. Snapchat changed their structure again", "info")
            self.print_status("4. Try running with a VPN", "info")
            return False
        
        self.print_status(f"Found {len(media_items)} media items", "success")
        
        # Display found media
        print(f"\n{Color.YELLOW}{'='*80}{Color.END}")
        for i, item in enumerate(media_items, 1):
            url_preview = item['media_url'][:70] + "..." if len(item['media_url']) > 70 else item['media_url']
            print(f"{Color.GREEN}{i:3}. {item['media_type'].upper()}{Color.END} - {Color.BLUE}{url_preview}{Color.END}")
        print(f"{Color.YELLOW}{'='*80}{Color.END}")
        
        # Set output directory
        if output_dir:
            download_folder = output_dir
        else:
            download_folder = os.path.join(self.download_folder, username)
        
        Path(download_folder).mkdir(parents=True, exist_ok=True)
        
        # Ask for confirmation
        choice = input(f"\n{Color.YELLOW}Download {len(media_items)} items? (y/n): {Color.END}").strip().lower()
        if choice not in ['y', 'yes']:
            self.print_status("Download cancelled", "info")
            return True
        
        self.print_status(f"Downloading to: {download_folder}", "info")
        
        # Download all media
        successful = 0
        failed = 0
        
        for i, item in enumerate(media_items, 1):
            if self.download_media(item, download_folder, i, len(media_items)):
                successful += 1
            else:
                failed += 1
        
        # Summary
        print(f"\n{Color.CYAN}{'='*60}{Color.END}")
        print(f"{Color.GREEN}SUMMARY{Color.END}")
        print(f"{Color.CYAN}{'='*60}{Color.END}")
        
        if successful > 0:
            self.print_status(f"Successfully downloaded: {successful}/{len(media_items)}", "success")
            self.print_status(f"Location: {download_folder}", "info")
        
        if failed > 0:
            self.print_status(f"Failed: {failed}/{len(media_items)}", "error")
        
        return successful > 0

def main():
    """Main function"""
    parser = argparse.ArgumentParser(description='Snapchat Story Downloader 2024')
    parser.add_argument('username', help='Snapchat username')
    parser.add_argument('-o', '--output', help='Output directory')
    parser.add_argument('--no-selenium', action='store_true', help='Disable Selenium')
    
    args = parser.parse_args()
    
    # Install requirements if needed
    if not SELENIUM_AVAILABLE:
        print("\n⚠️  Selenium is not installed. Some features will be limited.")
        print("Install with: pip install selenium webdriver-manager")
        print("Continue with basic methods? (y/n): ", end="")
        if input().lower() != 'y':
            return
    
    downloader = SnapchatDownloader2024(headless=True)
    
    try:
        downloader.run(
            username=args.username,
            output_dir=args.output,
            use_selenium=not args.no_selenium
        )
    except KeyboardInterrupt:
        print(f"\n{Color.YELLOW}[!] Interrupted by user{Color.END}")
    except Exception as e:
        print(f"{Color.RED}[-] Error: {str(e)}{Color.END}")

if __name__ == "__main__":
    main()