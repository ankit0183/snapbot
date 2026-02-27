#!/usr/bin/env python3
"""
Snapchat Story Downloader Telegram Bot
FIXED: Back Button + Pagination for 10+ items
"""

import os
import sys
import json
import re
import logging
import asyncio
import requests
import time
import zipfile
import io
import math
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Optional, Tuple, Set
from dataclasses import dataclass, asdict
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
from telegram.error import TelegramError

# ============================================================================
# CONFIGURATION
# ============================================================================

load_dotenv()
TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')

if not TOKEN:
    print("=" * 70)
    print("❌ ERROR: No Telegram bot token found!")
    print("Create .env file with: TELEGRAM_BOT_TOKEN=your_token_here")
    print("=" * 70)
    sys.exit(1)

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Create directories
DOWNLOADS_DIR = Path("snapchat_downloads")
DOWNLOADS_DIR.mkdir(exist_ok=True)

# ============================================================================
# DATA MODELS
# ============================================================================

@dataclass
class SnapContent:
    username: str
    media_url: str
    media_type: int  # 0=image, 1=video
    timestamp: int
    views: int = 0
    is_spotlight: bool = False
    quality: str = "original"

@dataclass
class UserTrack:
    chat_id: int
    username: str
    last_check: datetime
    last_story_time: int = 0

# Store user sessions
user_sessions = {}
user_tracks = {}  # For auto-notifications
download_queue = {}  # For batch downloads

# ============================================================================
# ENHANCED SNAPCHAT DOWNLOADER
# ============================================================================

class EnhancedSnapchatDownloader:
    """Enhanced Downloader with all features"""
    
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })
    
    def get_user_content(self, username: str) -> Tuple[List[SnapContent], List[SnapContent]]:
        """Get both stories and spotlights for username"""
        try:
            url = f"https://www.snapchat.com/add/{username}/"
            response = self.session.get(url, timeout=10)
            html = response.text
            
            json_match = re.search(
                r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
                html, re.DOTALL
            )
            
            if not json_match:
                json_match = re.search(r'window\.__INITIAL_STATE__\s*=\s*({.*?});', html)
                if not json_match:
                    return [], []
            
            data = json.loads(json_match.group(1))
            
            stories = self.parse_stories(data, username)
            spotlights = self.parse_spotlights(data, username)
            
            return stories, spotlights
            
        except Exception as e:
            logger.error(f"Error for {username}: {e}")
            return [], []
    
    def parse_stories(self, data: Dict, username: str) -> List[SnapContent]:
        """Parse all stories from data"""
        stories = []
        try:
            page_props = data.get('props', {}).get('pageProps', {})
            story_data = page_props.get('story', {})
            
            snap_list = story_data.get('snapList', [])
            if not snap_list:
                snap_list = story_data.get('snaps', [])
            
            for snap in snap_list:
                try:
                    snap_urls = snap.get('snapUrls', {})
                    media_url = ''
                    
                    if isinstance(snap_urls, dict):
                        media_url = snap_urls.get('mediaUrl', '')
                    elif isinstance(snap_urls, str):
                        media_url = snap_urls
                    
                    if not media_url or 'http' not in media_url:
                        continue
                    
                    timestamp_data = snap.get('timestampInSec', {})
                    timestamp = 0
                    
                    if isinstance(timestamp_data, dict):
                        timestamp = timestamp_data.get('value', 0)
                    elif isinstance(timestamp_data, (int, float)):
                        timestamp = timestamp_data
                    
                    if timestamp == 0:
                        continue
                    
                    story = SnapContent(
                        username=username,
                        media_url=media_url,
                        media_type=snap.get('snapMediaType', 0),
                        timestamp=int(timestamp),
                        is_spotlight=False
                    )
                    stories.append(story)
                    
                except Exception as e:
                    continue
            
            stories.sort(key=lambda x: x.timestamp, reverse=True)
            
        except Exception as e:
            logger.error(f"Error parsing stories: {e}")
        
        return stories
    
    def get_recent_stories(self, stories: List[SnapContent], hours: int = 24) -> List[SnapContent]:
        """Get stories from last N hours"""
        current_time = time.time()
        cutoff_time = current_time - (hours * 3600)
        
        recent = [
            story for story in stories 
            if story.timestamp >= cutoff_time
        ]
        
        recent.sort(key=lambda x: x.timestamp, reverse=True)
        return recent
    
    def parse_spotlights(self, data: Dict, username: str) -> List[SnapContent]:
        """Parse spotlights from data"""
        spotlights = []
        try:
            page_props = data.get('props', {}).get('pageProps', {})
            
            spotlight_data = []
            
            if 'spotlightHighlights' in page_props:
                content = page_props['spotlightHighlights']
                if isinstance(content, list):
                    spotlight_data.extend(content)
                elif isinstance(content, dict):
                    spotlight_data.append(content)
            
            if 'curatedHighlights' in page_props:
                for item in page_props['curatedHighlights']:
                    if isinstance(item, dict):
                        if item.get('$case') == 'spotlightHighlights':
                            spotlight_data.extend(item.get('spotlightHighlights', []))
                        elif 'snapList' in item:
                            spotlight_data.append(item)
            
            for section in spotlight_data:
                snaps = section.get('snapList', []) if isinstance(section, dict) else []
                for snap in snaps:
                    try:
                        snap_urls = snap.get('snapUrls', {})
                        media_url = ''
                        
                        if isinstance(snap_urls, dict):
                            media_url = snap_urls.get('mediaUrl', '')
                        elif isinstance(snap_urls, str):
                            media_url = snap_urls
                        
                        if not media_url or 'http' not in media_url:
                            continue
                        
                        timestamp_data = snap.get('timestampInSec', {})
                        timestamp = 0
                        
                        if isinstance(timestamp_data, dict):
                            timestamp = timestamp_data.get('value', 0)
                        elif isinstance(timestamp_data, (int, float)):
                            timestamp = timestamp_data
                        
                        if timestamp == 0:
                            continue
                        
                        views = snap.get('viewCount', 0)
                        
                        spotlight = SnapContent(
                            username=username,
                            media_url=media_url,
                            media_type=snap.get('snapMediaType', 0),
                            timestamp=int(timestamp),
                            views=views,
                            is_spotlight=True
                        )
                        spotlights.append(spotlight)
                    except Exception as e:
                        continue
            
            spotlights.sort(key=lambda x: x.views, reverse=True)
            
        except Exception as e:
            logger.error(f"Error parsing spotlights: {e}")
        
        return spotlights
    
    def download_file(self, content: SnapContent, quality: str = "original") -> Optional[bytes]:
        """Download file with quality option"""
        try:
            url = content.media_url
            if quality == "high" and "low" in url:
                url = url.replace("low", "high")
            
            response = requests.get(url, timeout=30)
            response.raise_for_status()
            return response.content
        except Exception as e:
            logger.error(f"Download error: {e}")
            return None

# ============================================================================
# PAGINATION HANDLER
# ============================================================================

class PaginationManager:
    """Handle pagination for large lists"""
    
    ITEMS_PER_PAGE = 8  # Show 8 items per page
    
    @staticmethod
    def get_total_pages(total_items: int) -> int:
        return math.ceil(total_items / PaginationManager.ITEMS_PER_PAGE)
    
    @staticmethod
    def get_page_items(items: List, page: int) -> List:
        start = (page - 1) * PaginationManager.ITEMS_PER_PAGE
        end = start + PaginationManager.ITEMS_PER_PAGE
        return items[start:end]
    
    @staticmethod
    def create_pagination_keyboard(current_page: int, total_pages: int, base_callback: str, 
                                  content_type: str, username: str, total_items: int) -> List:
        """Create pagination navigation buttons"""
        keyboard = []
        
        # Page navigation
        nav_row = []
        
        if current_page > 1:
            nav_row.append(InlineKeyboardButton(
                "◀️ PREV", 
                callback_data=f"page_{base_callback}_{content_type}_{current_page-1}_{username}"
            ))
        
        nav_row.append(InlineKeyboardButton(
            f"📄 {current_page}/{total_pages}", 
            callback_data="noop"
        ))
        
        if current_page < total_pages:
            nav_row.append(InlineKeyboardButton(
                "NEXT ▶️", 
                callback_data=f"page_{base_callback}_{content_type}_{current_page+1}_{username}"
            ))
        
        if nav_row:
            keyboard.append(nav_row)
        
        # Download options
        download_row = [
            InlineKeyboardButton(
                f"📥 DOWNLOAD ALL ({total_items})", 
                callback_data=f"download_all_{content_type}_{username}"
            )
        ]
        keyboard.append(download_row)
        
        # Back button
        keyboard.append([
            InlineKeyboardButton("🔙 BACK TO MAIN", callback_data="back_to_main")
        ])
        
        return keyboard

# ============================================================================
# MULTIPLE USER SEARCH FEATURE
# ============================================================================

async def handle_multiple_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle multiple usernames separated by commas"""
    text = update.message.text.strip()
    usernames = [u.strip().replace('@', '').lower() for u in text.split(',')]
    
    if len(usernames) < 2:
        return False  # Not multiple users
    
    chat_id = update.effective_chat.id
    msg = await update.message.reply_text(f"🔍 Searching for {len(usernames)} users...")
    
    found_users = []
    all_stories = []
    all_spotlights = []
    
    downloader = EnhancedSnapchatDownloader()
    
    for i, username in enumerate(usernames):
        await msg.edit_text(f"🔍 Searching {i+1}/{len(usernames)}: @{username}")
        
        stories, spotlights = await asyncio.to_thread(downloader.get_user_content, username)
        
        if stories or spotlights:
            found_users.append(username)
            all_stories.extend(stories)
            all_spotlights.extend(spotlights)
    
    if not found_users:
        await msg.edit_text("❌ No content found for any user!")
        return True
    
    # Store in session
    user_sessions[chat_id] = {
        'multiple': True,
        'usernames': found_users,
        'stories': all_stories,
        'spotlights': all_spotlights,
        'current_menu': 'multiple_main',
        'story_page': 1,
        'spotlight_page': 1
    }
    
    # Show multiple users menu
    text = f"👥 *Found {len(found_users)} users*\n\n"
    text += f"📖 Total Stories: {len(all_stories)}\n"
    text += f"🔥 Total Spotlights: {len(all_spotlights)}\n\n"
    text += "👇 *Choose action:*"
    
    keyboard = [
        [
            InlineKeyboardButton("📥 DOWNLOAD ALL", callback_data="multi_download_all"),
            InlineKeyboardButton("📦 EXPORT AS ZIP", callback_data="multi_export_zip")
        ],
        [
            InlineKeyboardButton("📖 STORIES ONLY", callback_data="multi_stories"),
            InlineKeyboardButton("🔥 SPOTLIGHTS ONLY", callback_data="multi_spotlights")
        ],
        [
            InlineKeyboardButton("📊 SHOW USERS", callback_data="multi_show_users"),
            InlineKeyboardButton("🔄 NEW SEARCH", callback_data="menu_new_search")
        ]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await msg.edit_text(text, parse_mode='Markdown', reply_markup=reply_markup)
    
    return True

# ============================================================================
# BATCH DOWNLOAD WITH PROGRESS BAR
# ============================================================================

async def batch_download_with_progress(query, items: List[SnapContent], username: str, item_type: str, quality: str = "original"):
    """Download multiple items with visual progress bar"""
    total = len(items)
    chat_id = query.message.chat_id
    
    # Create progress message
    progress_msg = await query.message.reply_text(
        f"📥 Preparing batch download of {total} {item_type}...\n\n"
        f"Quality: {quality.upper()}\n"
        f"[░░░░░░░░░░░░░░░░░░░░] 0%"
    )
    
    downloaded_files = []
    downloader = EnhancedSnapchatDownloader()
    
    for i, item in enumerate(items):
        try:
            # Update progress bar
            percent = int((i + 1) / total * 100)
            bar = "█" * (percent // 5) + "░" * (20 - percent // 5)
            
            current_item = f"{item_type} {i+1}/{total}"
            if item.is_spotlight:
                current_item = f"Spotlight {i+1}/{total}"
            
            await progress_msg.edit_text(
                f"📥 Batch Download Progress\n\n"
                f"Quality: {quality.upper()}\n"
                f"[{bar}] {percent}%\n"
                f"📁 {current_item}\n"
                f"⏳ ETA: {((total - i - 1) * 2)} seconds"
            )
            
            # Download file
            file_bytes = downloader.download_file(item, quality)
            if file_bytes:
                downloaded_files.append((file_bytes, item))
                
                # Send individual file
                await send_downloaded_file(query, file_bytes, item, username, quality)
            
            await asyncio.sleep(1)  # Rate limit
            
        except Exception as e:
            logger.error(f"Batch download error: {e}")
    
    # Complete
    await progress_msg.edit_text(
        f"✅ *Batch Download Complete!*\n\n"
        f"📥 Downloaded: {len(downloaded_files)}/{total} files\n"
        f"📁 Quality: {quality.upper()}\n"
        f"✨ Files sent individually above!",
        parse_mode='Markdown'
    )
    
    return downloaded_files

# ============================================================================
# AUTO-NOTIFICATION FOR NEW STORIES
# ============================================================================

async def setup_auto_notification(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Setup auto-notification for a user"""
    query = update.callback_query
    await query.answer()
    
    chat_id = update.effective_chat.id
    data = query.data
    
    if data.startswith("track_"):
        username = data[6:]
        
        # Store tracking info
        track_key = f"{chat_id}_{username}"
        user_tracks[track_key] = UserTrack(
            chat_id=chat_id,
            username=username,
            last_check=datetime.now(),
            last_story_time=0
        )
        
        await query.edit_message_text(
            f"🔔 *Auto-Notification Enabled!*\n\n"
            f"Now tracking @{username}\n"
            f"I'll notify you when new stories are posted!\n\n"
            f"To stop: /untrack {username}",
            parse_mode='Markdown'
        )

async def check_for_new_stories(context: ContextTypes.DEFAULT_TYPE):
    """Background task to check for new stories"""
    downloader = EnhancedSnapchatDownloader()
    
    for track_key, track in list(user_tracks.items()):
        try:
            # Get current stories
            stories, _ = await asyncio.to_thread(downloader.get_user_content, track.username)
            
            if stories:
                latest_time = stories[0].timestamp
                
                # Check if new stories
                if latest_time > track.last_story_time:
                    new_stories = [s for s in stories if s.timestamp > track.last_story_time]
                    
                    if new_stories:
                        # Send notification
                        await context.bot.send_message(
                            chat_id=track.chat_id,
                            text=f"🔔 *New Stories from @{track.username}!*\n\n"
                                 f"📸 {len(new_stories)} new stories posted\n"
                                 f"🕐 Latest: {datetime.fromtimestamp(new_stories[0].timestamp).strftime('%H:%M')}\n\n"
                                 f"Use /dl {track.username} to download!",
                            parse_mode='Markdown'
                        )
                        
                        # Update last check
                        track.last_story_time = latest_time
                        track.last_check = datetime.now()
            
        except Exception as e:
            logger.error(f"Auto-notify error for {track.username}: {e}")

# ============================================================================
# QUALITY OPTIONS
# ============================================================================

async def show_quality_options(query, session):
    """Show quality selection menu"""
    text = "🎚️ *Select Download Quality*\n\n"
    text += "• ORIGINAL: Best quality, larger file\n"
    text += "• HIGH: Good quality, smaller file\n\n"
    text += f"Current: {session.get('quality', 'original').upper()}"
    
    keyboard = [
        [
            InlineKeyboardButton("🖼️ ORIGINAL", callback_data="quality_original"),
            InlineKeyboardButton("📱 HIGH", callback_data="quality_high")
        ],
        [InlineKeyboardButton("🔙 BACK", callback_data="back_to_main")]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(text, parse_mode='Markdown', reply_markup=reply_markup)

async def set_quality(query, session, data):
    """Set quality preference"""
    quality = data.split("_")[1]
    session['quality'] = quality
    
    await query.edit_message_text(f"✅ Quality set to {quality.upper()}!\n\nUse this for all future downloads.")
    await asyncio.sleep(1)
    await show_main_menu(query, session['username'], session['stories'], 
                        session['recent_stories'], session['spotlights'])

# ============================================================================
# EXPORT AS ZIP
# ============================================================================

async def export_as_zip(query, items: List[SnapContent], username: str, content_type: str, quality: str = "original"):
    """Export multiple files as ZIP"""
    await query.edit_message_text(f"📦 Creating ZIP archive for {len(items)} files...")
    
    downloader = EnhancedSnapchatDownloader()
    zip_buffer = io.BytesIO()
    
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
        for i, item in enumerate(items):
            try:
                # Show progress
                if i % 5 == 0:
                    await query.edit_message_text(f"📦 Zipping files... {i+1}/{len(items)}")
                
                # Download file
                file_bytes = downloader.download_file(item, quality)
                if file_bytes:
                    # Create filename
                    timestamp = datetime.fromtimestamp(item.timestamp)
                    date_str = timestamp.strftime("%Y-%m-%d")
                    time_str = timestamp.strftime("%H-%M-%S")
                    ext = "jpg" if item.media_type == 0 else "mp4"
                    
                    if item.is_spotlight:
                        filename = f"{username}_spotlight_{date_str}_{time_str}.{ext}"
                    else:
                        filename = f"{username}_{date_str}_{time_str}.{ext}"
                    
                    # Add to zip
                    zip_file.writestr(filename, file_bytes)
                    
            except Exception as e:
                logger.error(f"ZIP error: {e}")
    
    zip_buffer.seek(0)
    
    # Send ZIP file
    zip_filename = f"{username}_{content_type}_{datetime.now().strftime('%Y%m%d_%H%M')}.zip"
    
    await query.message.reply_document(
        document=zip_buffer,
        filename=zip_filename,
        caption=f"📦 *ZIP Archive Ready!*\n\n"
                f"👤 User: @{username}\n"
                f"📁 Content: {content_type}\n"
                f"📄 Files: {len(items)}\n"
                f"💾 Size: {zip_buffer.getbuffer().nbytes / 1024 / 1024:.1f} MB\n\n"
                f"⬇️ *Click to download*",
        parse_mode='Markdown'
    )
    
    await query.edit_message_text(f"✅ ZIP file sent! Check above.")

# ============================================================================
# QUICK COMMANDS
# ============================================================================

async def handle_quick_commands(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle quick commands like /dl, /recent, etc."""
    text = update.message.text
    chat_id = update.effective_chat.id
    
    # /dl username - Quick download all
    if text.startswith('/dl '):
        username = text[4:].strip()
        await quick_download(update, username, 'all')
        return True
    
    # /recent username - Recent stories only
    elif text.startswith('/recent '):
        username = text[8:].strip()
        await quick_download(update, username, 'recent')
        return True
    
    # /spot username - Spotlights only
    elif text.startswith('/spot '):
        username = text[6:].strip()
        await quick_download(update, username, 'spotlights')
        return True
    
    # /track username - Auto-notify
    elif text.startswith('/track '):
        username = text[7:].strip()
        await setup_quick_track(update, username)
        return True
    
    # /untrack username - Stop tracking
    elif text.startswith('/untrack '):
        username = text[9:].strip()
        await stop_tracking(update, username)
        return True
    
    # /quality username - Set quality
    elif text.startswith('/quality '):
        parts = text[9:].strip().split()
        if len(parts) == 2:
            quality, username = parts
            context.user_data['quality'] = quality.lower()
            await update.message.reply_text(f"✅ Quality set to {quality.upper()} for next downloads!")
        return True
    
    return False

async def quick_download(update: Update, username: str, mode: str):
    """Quick download handler"""
    msg = await update.message.reply_text(f"⚡ Quick download for @{username}...")
    
    downloader = EnhancedSnapchatDownloader()
    stories, spotlights = await asyncio.to_thread(downloader.get_user_content, username)
    
    if mode == 'all':
        items = stories + spotlights
        content_type = "all content"
    elif mode == 'recent':
        recent = downloader.get_recent_stories(stories)
        items = recent
        content_type = "recent stories"
    else:  # spotlights
        items = spotlights
        content_type = "spotlights"
    
    if not items:
        await msg.edit_text(f"❌ No {content_type} found for @{username}")
        return
    
    await msg.edit_text(f"📥 Downloading {len(items)} {content_type}...")
    
    quality = update.effective_user.id in user_sessions and user_sessions[update.effective_user.id].get('quality') or 'original'
    
    for i, item in enumerate(items[:10]):  # Limit to 10 for quick download
        try:
            file_bytes = downloader.download_file(item, quality)
            if file_bytes:
                await send_downloaded_file_to_chat(update, file_bytes, item, username, quality)
            await asyncio.sleep(1)
        except Exception as e:
            logger.error(f"Quick download error: {e}")
    
    await msg.edit_text(f"✅ Quick download complete! Sent {min(10, len(items))} files.")

async def setup_quick_track(update: Update, username: str):
    """Quick track setup"""
    chat_id = update.effective_chat.id
    track_key = f"{chat_id}_{username}"
    
    user_tracks[track_key] = UserTrack(
        chat_id=chat_id,
        username=username,
        last_check=datetime.now(),
        last_story_time=0
    )
    
    await update.message.reply_text(
        f"🔔 *Now tracking @{username}!*\n\n"
        f"I'll notify you when new stories are posted.",
        parse_mode='Markdown'
    )

async def stop_tracking(update: Update, username: str):
    """Stop tracking user"""
    chat_id = update.effective_chat.id
    track_key = f"{chat_id}_{username}"
    
    if track_key in user_tracks:
        del user_tracks[track_key]
        await update.message.reply_text(f"✅ Stopped tracking @{username}")
    else:
        await update.message.reply_text(f"❌ Not tracking @{username}")

# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

async def send_downloaded_file(query, file_bytes: bytes, content: SnapContent, username: str, quality: str):
    """Send downloaded file to chat"""
    timestamp = datetime.fromtimestamp(content.timestamp)
    date_str = timestamp.strftime("%Y-%m-%d")
    time_str = timestamp.strftime("%H-%M-%S")
    ext = "jpg" if content.media_type == 0 else "mp4"
    
    if content.is_spotlight:
        filename = f"{username}_spotlight_{date_str}_{time_str}_{quality}.{ext}"
        icon = "🔥"
    else:
        filename = f"{username}_{date_str}_{time_str}_{quality}.{ext}"
        icon = "📸"
    
    caption = f"{icon} @{username}\n📅 {date_str} at {time_str}\n🎚️ Quality: {quality.upper()}"
    
    await query.message.reply_document(
        document=file_bytes,
        filename=filename,
        caption=caption
    )

async def send_downloaded_file_to_chat(update, file_bytes: bytes, content: SnapContent, username: str, quality: str):
    """Send file directly to chat (for quick commands)"""
    timestamp = datetime.fromtimestamp(content.timestamp)
    date_str = timestamp.strftime("%Y-%m-%d")
    time_str = timestamp.strftime("%H-%M-%S")
    ext = "jpg" if content.media_type == 0 else "mp4"
    
    if content.is_spotlight:
        filename = f"{username}_spotlight_{date_str}_{time_str}_{quality}.{ext}"
        icon = "🔥"
    else:
        filename = f"{username}_{date_str}_{time_str}_{quality}.{ext}"
        icon = "📸"
    
    caption = f"{icon} @{username} - {date_str} {time_str} ({quality})"
    
    await update.message.reply_document(
        document=file_bytes,
        filename=filename,
        caption=caption
    )

# ============================================================================
# MAIN BOT HANDLERS
# ============================================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start command with all features"""
    text = """
📱 *SNAPCHAT DOWNLOADER - FIXED VERSION*

✨ *NEW FEATURES ADDED:*
• 👥 Multiple User Search (comma separated)
• 📊 Batch Download with Progress Bar
• 🔔 Auto-Notification for New Stories
• 🎚️ Quality Options (Original/High)
• ⚡ Quick Commands (/dl, /recent, /spot)
• 📦 Export as ZIP
• 📄 Pagination for 10+ items
• 🔙 Fixed Back Button Navigation

📁 *File Naming:*
• Stories: `username_YYYY-MM-DD_HH-MM-SS_quality.jpg`
• Spotlights: `username_spotlight_YYYY-MM-DD_HH-MM-SS_quality.jpg`

🚀 *QUICK COMMANDS:*
• `/dl username` - Download all
• `/recent username` - Last 24h only
• `/spot username` - Spotlights only
• `/track username` - Auto-notify
• `/quality high username` - Set quality

💡 *Example:* Send `pri_boo, snapchat, djkhaled` for multiple users!
"""
    await update.message.reply_text(text, parse_mode='Markdown')

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle all text messages"""
    # Check for quick commands first
    if await handle_quick_commands(update, context):
        return
    
    # Check for multiple users
    if await handle_multiple_users(update, context):
        return
    
    # Single user
    await handle_single_user(update, context)

async def handle_single_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle single username"""
    username = update.message.text.strip().replace('@', '').lower()
    chat_id = update.effective_chat.id
    
    if len(username) < 3:
        await update.message.reply_text("❌ Username must be at least 3 characters!")
        return
    
    msg = await update.message.reply_text(f"🔍 Searching for @{username}...")
    
    try:
        downloader = EnhancedSnapchatDownloader()
        stories, spotlights = await asyncio.to_thread(downloader.get_user_content, username)
        
        if not stories and not spotlights:
            await msg.edit_text(f"❌ No content found for @{username}")
            return
        
        recent_stories = downloader.get_recent_stories(stories) if stories else []
        
        # Store session with pagination pages
        user_sessions[chat_id] = {
            'username': username,
            'stories': stories,
            'recent_stories': recent_stories,
            'spotlights': spotlights,
            'current_menu': 'main',
            'quality': context.user_data.get('quality', 'original'),
            'story_page': 1,
            'recent_page': 1,
            'spotlight_page': 1
        }
        
        await show_main_menu(msg, username, stories, recent_stories, spotlights)
        
    except Exception as e:
        await msg.edit_text(f"❌ Error: {str(e)[:100]}")

async def show_main_menu(msg, username: str, stories: List, recent: List, spotlights: List):
    """Show main menu with all options"""
    quality = user_sessions.get(msg.chat.id, {}).get('quality', 'original')
    
    summary = f"👤 *@{username}*\n\n"
    summary += f"📖 All Stories: {len(stories)}\n"
    summary += f"🕐 Recent (24h): {len(recent)}\n"
    summary += f"🔥 Spotlights: {len(spotlights)}\n"
    summary += f"🎚️ Current Quality: {quality.upper()}\n\n"
    summary += "👇 *Choose an option:*"
    
    keyboard = [
        [
            InlineKeyboardButton(f"📖 ALL STORIES ({len(stories)})", callback_data="menu_all_stories"),
            InlineKeyboardButton(f"🕐 RECENT ({len(recent)})", callback_data="menu_recent_stories")
        ],
        [
            InlineKeyboardButton(f"🔥 SPOTLIGHTS ({len(spotlights)})", callback_data="menu_spotlights")
        ],
        [
            InlineKeyboardButton("📥 BATCH DOWNLOAD", callback_data="menu_batch"),
            InlineKeyboardButton("📦 EXPORT ZIP", callback_data="menu_export")
        ],
        [
            InlineKeyboardButton("🎚️ QUALITY", callback_data="menu_quality"),
            InlineKeyboardButton("🔔 AUTO-NOTIFY", callback_data=f"track_{username}")
        ],
        [
            InlineKeyboardButton("🔄 NEW SEARCH", callback_data="menu_new_search"),
            InlineKeyboardButton("🏠 RESTART", callback_data="menu_restart")
        ]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await msg.edit_text(summary, parse_mode='Markdown', reply_markup=reply_markup)

# ============================================================================
# PAGINATED MENUS
# ============================================================================

async def show_paginated_stories(query, username: str, stories: List, story_type: str, page: int = 1):
    """Show stories with pagination"""
    if not stories:
        await query.edit_message_text("❌ No stories available!")
        return
    
    total_pages = PaginationManager.get_total_pages(len(stories))
    page = max(1, min(page, total_pages))
    
    # Get items for current page
    page_items = PaginationManager.get_page_items(stories, page)
    start_idx = (page - 1) * PaginationManager.ITEMS_PER_PAGE + 1
    
    title = f"📖 *ALL STORIES*" if story_type == "all" else f"🕐 *RECENT STORIES*"
    text = f"{title} for @{username}\n\n"
    text += f"📁 Page {page}/{total_pages} | Total: {len(stories)}\n\n"
    text += "👇 *Click to download:*\n"
    
    keyboard = []
    
    # Add story buttons for current page
    for i, story in enumerate(page_items):
        item_num = start_idx + i
        date_str = datetime.fromtimestamp(story.timestamp).strftime("%m/%d %H:%M")
        type_icon = "🖼️" if story.media_type == 0 else "🎥"
        btn_text = f"{item_num}. {type_icon} {date_str}"
        
        if story_type == "all":
            callback = f"story_all_{(page-1)*PaginationManager.ITEMS_PER_PAGE + i}"
        else:
            callback = f"story_recent_{(page-1)*PaginationManager.ITEMS_PER_PAGE + i}"
        
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=callback)])
    
    # Add pagination and download buttons
    pagination_keyboard = PaginationManager.create_pagination_keyboard(
        page, total_pages, "story", story_type, username, len(stories)
    )
    keyboard.extend(pagination_keyboard)
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(text, parse_mode='Markdown', reply_markup=reply_markup)

async def show_paginated_spotlights(query, username: str, spotlights: List, page: int = 1):
    """Show spotlights with pagination"""
    if not spotlights:
        await query.edit_message_text("❌ No spotlights available!")
        return
    
    total_pages = PaginationManager.get_total_pages(len(spotlights))
    page = max(1, min(page, total_pages))
    
    # Get items for current page
    page_items = PaginationManager.get_page_items(spotlights, page)
    start_idx = (page - 1) * PaginationManager.ITEMS_PER_PAGE + 1
    
    text = f"🔥 *SPOTLIGHTS for @{username}*\n\n"
    text += f"📁 Page {page}/{total_pages} | Total: {len(spotlights)}\n\n"
    text += "👇 *Click to download:*\n"
    
    keyboard = []
    
    # Add spotlight buttons for current page
    for i, spotlight in enumerate(page_items):
        item_num = start_idx + i
        date_str = datetime.fromtimestamp(spotlight.timestamp).strftime("%m/%d %H:%M")
        type_icon = "🖼️" if spotlight.media_type == 0 else "🎥"
        views = f" 👀 {spotlight.views:,}" if spotlight.views > 0 else ""
        btn_text = f"{item_num}. {type_icon} {date_str}{views}"
        
        callback = f"spotlight_{(page-1)*PaginationManager.ITEMS_PER_PAGE + i}"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=callback)])
    
    # Add pagination and download buttons
    pagination_keyboard = PaginationManager.create_pagination_keyboard(
        page, total_pages, "spot", "spotlights", username, len(spotlights)
    )
    keyboard.extend(pagination_keyboard)
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(text, parse_mode='Markdown', reply_markup=reply_markup)

# ============================================================================
# CALLBACK HANDLER
# ============================================================================

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle all button clicks"""
    query = update.callback_query
    await query.answer()
    
    chat_id = update.effective_chat.id
    data = query.data
    
    # Handle no-op buttons
    if data == "noop":
        return
    
    session = user_sessions.get(chat_id)
    if not session and not data.startswith(('track_', 'menu_new_search', 'menu_restart')):
        await query.edit_message_text("❌ Session expired! Send username again.")
        return
    
    # Handle tracking
    if data.startswith("track_"):
        await setup_auto_notification(update, context)
        return
    
    # Handle pagination
    if data.startswith("page_"):
        await handle_pagination(query, session, data)
        return
    
    # Handle download all
    if data.startswith("download_all_"):
        await handle_download_all(query, session, data)
        return
    
    # Handle quality menu
    if data == "menu_quality":
        await show_quality_options(query, session)
        return
    
    if data.startswith("quality_"):
        await set_quality(query, session, data)
        return
    
    # Handle batch download
    if data == "menu_batch":
        await show_batch_menu(query, session)
        return
    
    if data.startswith("batch_"):
        await handle_batch_download(query, session, data)
        return
    
    # Handle export
    if data == "menu_export":
        await show_export_menu(query, session)
        return
    
    if data.startswith("export_"):
        await handle_export(query, session, data)
        return
    
    # Handle multiple users
    if data.startswith("multi_"):
        await handle_multi_callback(query, session, data)
        return
    
    # Handle regular navigation
    await handle_regular_callback(query, session, data)

async def handle_pagination(query, session, data):
    """Handle pagination navigation"""
    parts = data.split('_')
    # format: page_{type}_{content_type}_{page}_{username}
    if len(parts) >= 5:
        _, _, content_type, page, username = parts[:5]
        page = int(page)
        
        if content_type == "story":
            story_type = parts[3]  # all or recent
            if story_type == "all":
                await show_paginated_stories(query, username, session['stories'], "all", page)
            else:
                await show_paginated_stories(query, username, session['recent_stories'], "recent", page)
        elif content_type == "spot":
            await show_paginated_spotlights(query, username, session['spotlights'], page)

async def handle_download_all(query, session, data):
    """Handle download all for a content type"""
    parts = data.split('_')
    # format: download_all_{content_type}_{username}
    if len(parts) >= 4:
        content_type = parts[2]
        username = parts[3]
        quality = session.get('quality', 'original')
        
        if content_type == "all":
            items = session['stories']
            name = "All Stories"
        elif content_type == "recent":
            items = session['recent_stories']
            name = "Recent Stories"
        elif content_type == "spotlights":
            items = session['spotlights']
            name = "Spotlights"
        else:
            return
        
        if not items:
            await query.edit_message_text(f"❌ No {name} available!")
            return
        
        await batch_download_with_progress(query, items, username, name, quality)

async def show_batch_menu(query, session):
    """Show batch download menu"""
    text = "📥 *Batch Download Options*\n\n"
    text += f"📖 Stories: {len(session['stories'])}\n"
    text += f"🕐 Recent: {len(session['recent_stories'])}\n"
    text += f"🔥 Spotlights: {len(session['spotlights'])}\n\n"
    text += "Choose what to download in batch:"
    
    keyboard = [
        [
            InlineKeyboardButton("📖 ALL STORIES", callback_data="batch_stories"),
            InlineKeyboardButton("🕐 RECENT", callback_data="batch_recent")
        ],
        [
            InlineKeyboardButton("🔥 SPOTLIGHTS", callback_data="batch_spotlights"),
            InlineKeyboardButton("📦 EVERYTHING", callback_data="batch_all")
        ],
        [InlineKeyboardButton("🔙 BACK", callback_data="back_to_main")]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(text, parse_mode='Markdown', reply_markup=reply_markup)

async def handle_batch_download(query, session, data):
    """Handle batch download selection"""
    content_type = data.split("_")[1]
    quality = session.get('quality', 'original')
    
    if content_type == "stories":
        items = session['stories']
        name = "Stories"
    elif content_type == "recent":
        items = session['recent_stories']
        name = "Recent Stories"
    elif content_type == "spotlights":
        items = session['spotlights']
        name = "Spotlights"
    else:  # all
        items = session['stories'] + session['spotlights']
        name = "All Content"
    
    if not items:
        await query.edit_message_text(f"❌ No {name} available!")
        return
    
    await batch_download_with_progress(query, items, session['username'], name, quality)

async def show_export_menu(query, session):
    """Show export menu"""
    text = "📦 *Export as ZIP*\n\n"
    text += f"📖 Stories: {len(session['stories'])}\n"
    text += f"🕐 Recent: {len(session['recent_stories'])}\n"
    text += f"🔥 Spotlights: {len(session['spotlights'])}\n\n"
    text += "Choose what to export:"
    
    keyboard = [
        [
            InlineKeyboardButton("📖 STORIES", callback_data="export_stories"),
            InlineKeyboardButton("🕐 RECENT", callback_data="export_recent")
        ],
        [
            InlineKeyboardButton("🔥 SPOTLIGHTS", callback_data="export_spotlights"),
            InlineKeyboardButton("📦 ALL", callback_data="export_all")
        ],
        [InlineKeyboardButton("🔙 BACK", callback_data="back_to_main")]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(text, parse_mode='Markdown', reply_markup=reply_markup)

async def handle_export(query, session, data):
    """Handle export selection"""
    content_type = data.split("_")[1]
    quality = session.get('quality', 'original')
    
    if content_type == "stories":
        items = session['stories']
        name = "stories"
    elif content_type == "recent":
        items = session['recent_stories']
        name = "recent"
    elif content_type == "spotlights":
        items = session['spotlights']
        name = "spotlights"
    else:  # all
        items = session['stories'] + session['spotlights']
        name = "all"
    
    if not items:
        await query.edit_message_text(f"❌ No {name} available!")
        return
    
    await export_as_zip(query, items, session['username'], name, quality)

async def handle_multi_callback(query, session, data):
    """Handle multiple users callbacks"""
    if data == "multi_download_all":
        items = session['stories'] + session['spotlights']
        await batch_download_with_progress(query, items, "multiple_users", "All Content", "original")
    
    elif data == "multi_export_zip":
        items = session['stories'] + session['spotlights']
        await export_as_zip(query, items, "multiple_users", "all", "original")
    
    elif data == "multi_stories":
        await show_paginated_stories(query, "multiple_users", session['stories'], "all", 1)
    
    elif data == "multi_spotlights":
        await show_paginated_spotlights(query, "multiple_users", session['spotlights'], 1)
    
    elif data == "multi_show_users":
        users = session.get('usernames', [])
        text = "👥 *Users Found:*\n\n"
        for i, user in enumerate(users, 1):
            text += f"{i}. @{user}\n"
        
        keyboard = [[InlineKeyboardButton("🔙 BACK", callback_data="back_to_main")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(text, parse_mode='Markdown', reply_markup=reply_markup)

async def handle_regular_callback(query, session, data):
    """Handle regular navigation callbacks"""
    username = session['username']
    stories = session['stories']
    recent = session['recent_stories']
    spotlights = session['spotlights']
    
    if data == "back_to_main":
        await show_main_menu(query, username, stories, recent, spotlights)
        return
    
    if data == "menu_new_search":
        await query.edit_message_text("🔄 Send another username:")
        return
    
    if data == "menu_restart":
        await start(query, None)
        return
    
    if data == "menu_all_stories":
        await show_paginated_stories(query, username, stories, "all", 1)
        return
    
    if data == "menu_recent_stories":
        await show_paginated_stories(query, username, recent, "recent", 1)
        return
    
    if data == "menu_spotlights":
        await show_paginated_spotlights(query, username, spotlights, 1)
        return
    
    # Handle individual story downloads
    if data.startswith("story_all_"):
        index = int(data.split("_")[2])
        if index < len(stories):
            story = stories[index]
            quality = session.get('quality', 'original')
            downloader = EnhancedSnapchatDownloader()
            file_bytes = downloader.download_file(story, quality)
            if file_bytes:
                await send_downloaded_file(query, file_bytes, story, username, quality)
        return
    
    if data.startswith("story_recent_"):
        index = int(data.split("_")[2])
        if index < len(recent):
            story = recent[index]
            quality = session.get('quality', 'original')
            downloader = EnhancedSnapchatDownloader()
            file_bytes = downloader.download_file(story, quality)
            if file_bytes:
                await send_downloaded_file(query, file_bytes, story, username, quality)
        return
    
    if data.startswith("spotlight_"):
        index = int(data.split("_")[1])
        if index < len(spotlights):
            spotlight = spotlights[index]
            quality = session.get('quality', 'original')
            downloader = EnhancedSnapchatDownloader()
            file_bytes = downloader.download_file(spotlight, quality)
            if file_bytes:
                await send_downloaded_file(query, file_bytes, spotlight, username, quality)
        return

# ============================================================================
# HELP AND CLEANUP
# ============================================================================

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Help command"""
    text = """
🆘 *COMPLETE HELP GUIDE*

📱 *NEW FEATURES:*

👥 *MULTIPLE USER SEARCH*
• Send usernames separated by commas
• Example: `pri_boo, snapchat, djkhaled`

📊 *BATCH DOWNLOAD WITH PROGRESS*
• Download multiple files at once
• Shows progress bar and ETA

🔔 *AUTO-NOTIFICATION*
• `/track username` - Get notified of new stories
• `/untrack username` - Stop notifications

🎚️ *QUALITY OPTIONS*
• Original - Best quality
• High - Good quality, smaller file

⚡ *QUICK COMMANDS*
• `/dl username` - Download all
• `/recent username` - Last 24h only
• `/spot username` - Spotlights only
• `/track username` - Auto-notify
• `/quality high username` - Set quality

📦 *EXPORT AS ZIP*
• Download multiple files as ZIP archive
• Perfect for sharing!

📄 *PAGINATION*
• Shows 8 items per page
• Next/Prev buttons for navigation
• Download All option for each type

🛠️ *BASIC COMMANDS*
/start - Show welcome
/help - This guide
/cleanup - Clear session
"""
    await update.message.reply_text(text, parse_mode='Markdown')

async def cleanup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Clear session"""
    chat_id = update.effective_chat.id
    if chat_id in user_sessions:
        del user_sessions[chat_id]
    await update.message.reply_text("✅ Session cleared! Send a username to start.")

# ============================================================================
# MAIN
# ============================================================================

def main():
    print("=" * 80)
    print("📱 SNAPCHAT DOWNLOADER - FIXED VERSION")
    print("=" * 80)
    print("✅ FIXES APPLIED:")
    print("  • 🔙 Back button now works everywhere")
    print("  • 📄 Pagination for 10+ items")
    print("  • 📥 Download All option for each page")
    print("  • ◀️ NEXT/PREV buttons for navigation")
    print("=" * 80)
    print("✅ ALL FEATURES:")
    print("  • 👥 Multiple User Search")
    print("  • 📊 Batch Download with Progress")
    print("  • 🔔 Auto-Notification System")
    print("  • 🎚️ Quality Options (Original/High)")
    print("  • ⚡ Quick Commands (/dl, /recent, /spot)")
    print("  • 📦 Export as ZIP")
    print("=" * 80)
    
    app = Application.builder().token(TOKEN).build()
    
    # Add handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("cleanup", cleanup_command))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    # Add background task for auto-notifications
    if app.job_queue:
        app.job_queue.run_repeating(check_for_new_stories, interval=300, first=10)  # Check every 5 minutes
    else:
        print("⚠️ WARNING: JobQueue is not available. Auto-notifications will be disabled.")
        print("To enable them, run: pip install \\\"python-telegram-bot[job-queue]\\\"")
    
    print("🟢 Bot is running with all features...")
    print("📱 Send a username to start!")
    print("💡 Try: `pri_boo, snapchat, djkhaled` for multiple users!")
    print("=" * 80)
    
    app.run_polling()

if __name__ == "__main__":
    main()