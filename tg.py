import os
import logging
import requests
from datetime import datetime
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from dotenv import load_dotenv
import re

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Bot token from environment variable
TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')

# Snapchat API endpoints (Note: These are hypothetical and may not work without proper authorization)
SNAPCHAT_API_BASE = "https://storysharing.snapchat.com/v1/fetch"

class SnapchatStoryDownloader:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })
    
    def extract_username(self, url):
        """Extract username from Snapchat URL"""
        patterns = [
            r'snapchat\.com/add/([^/?]+)',
            r'snapchat\.com/([^/?]+)',
            r'@(\w+)'
        ]
        
        for pattern in patterns:
            match = re.search(pattern, url)
            if match:
                return match.group(1)
        return url.strip('@')
    
    def download_public_story(self, username):
        """
        Attempt to download public story.
        NOTE: This is a template. Actual Snapchat API requires proper authorization.
        """
        try:
            # This is a placeholder - actual implementation requires Snapchat API access
            # Snapchat doesn't provide a public API for downloading stories
            
            # For educational purposes only - you would need:
            # 1. Official Snapchat API access
            # 2. User consent
            # 3. Proper authentication
            
            logger.info(f"Attempting to download story for: {username}")
            
            # Return mock data for demonstration
            return {
                'success': False,
                'message': 'This functionality requires Snapchat API access which is not publicly available.',
                'username': username
            }
            
        except Exception as e:
            logger.error(f"Error downloading story: {e}")
            return {'success': False, 'error': str(e)}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send welcome message when /start is issued."""
    welcome_text = """
    👋 *Welcome to Snapchat Story Downloader Bot*
    
    ⚠️ *Disclaimer*:
    - This bot is for educational purposes only
    - Downloading Snapchat content may violate their Terms of Service
    - Only download content you have permission to access
    
    📝 *How to use*:
    Send a Snapchat username (with or without @)
    
    Example: `snapchatuser` or `@snapchatuser`
    
    🔒 *Privacy Note*:
    This bot respects privacy and should only be used for public content with proper authorization.
    """
    await update.message.reply_text(welcome_text, parse_mode='Markdown')

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle incoming messages with Snapchat usernames."""
    user_input = update.message.text.strip()
    
    # Show processing message
    processing_msg = await update.message.reply_text("🔍 Processing...")
    
    # Initialize downloader
    downloader = SnapchatStoryDownloader()
    
    # Extract username
    username = downloader.extract_username(user_input)
    
    # Attempt to get story info
    result = downloader.download_public_story(username)
    
    if result.get('success', False):
        # If download was successful (in a real implementation)
        await processing_msg.edit_text(
            f"✅ Found public content for @{username}\n\n"
            f"⚠️ Note: Actual downloading requires Snapchat API access."
        )
    else:
        await processing_msg.edit_text(
            f"❌ Could not access stories for @{username}\n\n"
            f"*Reasons this might fail:*\n"
            f"1. User has no public stories\n"
            f"2. Stories are private\n"
            f"3. Snapchat API restrictions\n"
            f"4. Legal/technical limitations\n\n"
            f"💡 *Remember:* Always respect privacy and terms of service.",
            parse_mode='Markdown'
        )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send help message."""
    help_text = """
    ℹ️ *Help Guide*
    
    *Available Commands:*
    /start - Start the bot
    /help - Show this help message
    /disclaimer - Show legal disclaimer
    
    *How to use:*
    1. Send a Snapchat username
    2. Bot will attempt to find public stories
    3. If found, you can download them
    
    *Limitations:*
    - Only works for public stories
    - Requires user's public profile
    - Subject to Snapchat's API limitations
    """
    await update.message.reply_text(help_text, parse_mode='Markdown')

async def disclaimer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show legal disclaimer."""
    disclaimer_text = """
    ⚖️ *LEGAL DISCLAIMER*
    
    *IMPORTANT:*
    1. This bot is for **EDUCATIONAL PURPOSES ONLY**
    2. Downloading Snapchat content may violate:
       - Snapchat's Terms of Service
       - Copyright laws
       - Privacy laws
    
    3. *DO NOT* use this bot to:
       - Download private content
       - Harass or stalk users
       - Violate anyone's privacy
    
    4. *ONLY* use for:
       - Your own content
       - Content you have permission to download
       - Public content where allowed by law
    
    The developer is not responsible for misuse of this bot.
    """
    await update.message.reply_text(disclaimer_text, parse_mode='Markdown')

def main():
    """Start the bot."""
    if not TOKEN:
        logger.error("No TELEGRAM_BOT_TOKEN found in environment variables!")
        return
    
    # Create Application
    application = Application.builder().token(TOKEN).build()
    
    # Add command handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("disclaimer", disclaimer))
    
    # Add message handler
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    # Start the bot
    logger.info("Bot starting...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()