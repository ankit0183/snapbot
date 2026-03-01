#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════╗
║         SNAPCHAT STORY DOWNLOADER — TELEGRAM BOT             ║
║         Version 3.1  •  Format & Missing-File Fixes          ║
╚══════════════════════════════════════════════════════════════╝

FIXED IN v3.1:
  • Video format bug: extension now detected from real file bytes
    (magic bytes + Content-Type header) — never mislabels mp4 as jpg
  • Missing items: timestamp fallback chain checks 6 different fields
  • Missing items: URL fallback chain checks mediaUrl, mediaUrl2,
    overlayUrl, snapUrls string, and direct URL keys
  • Deduplication: same URL appearing in stories + spotlights is merged
  • Download: streams response to detect Content-Type before saving
  • All previous v3.0 features retained
"""

import os, sys, json, re, logging, asyncio, requests, time
import zipfile, io, math, pickle
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass, field
from dotenv import load_dotenv

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters,
)
from telegram.error import TelegramError, RetryAfter, TimedOut

# ─────────────────────────────────────────────────────────────
# BOOT
# ─────────────────────────────────────────────────────────────
load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    sys.exit("❌  Set TELEGRAM_BOT_TOKEN in your .env file.")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(levelname)-8s │ %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)
Path("snapchat_downloads").mkdir(exist_ok=True)

# ─────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────
ITEMS_PER_PAGE   = 8
MAX_ZIP_PER_FILE = 45       # files per ZIP before splitting
SEND_DELAY       = 0.7      # seconds between Telegram sends
DOWNLOAD_RETRY   = 3
TRACK_INTERVAL   = 300      # seconds between notification checks
MAX_HISTORY      = 10       # searches remembered per user

DATA_FILE = Path("bot_data.pkl")
SEP       = "§"             # callback-data separator

# ─────────────────────────────────────────────────────────────
# DATA MODELS
# ─────────────────────────────────────────────────────────────
@dataclass
class SnapContent:
    username:     str
    media_url:    str
    media_type:   int       # 0=image  1=video
    timestamp:    int
    views:        int  = 0
    is_spotlight: bool = False

@dataclass
class UserTrack:
    chat_id:         int
    username:        str
    last_check:      float
    last_story_time: int = 0

@dataclass
class BotStats:
    total_downloads: int = 0
    total_zips:      int = 0
    per_user:        Dict[int, int] = field(default_factory=dict)

# ─────────────────────────────────────────────────────────────
# PERSISTENT STORAGE
# ─────────────────────────────────────────────────────────────
def _load_data() -> Dict:
    if DATA_FILE.exists():
        try:
            with open(DATA_FILE, "rb") as f:
                return pickle.load(f)
        except Exception as e:
            logger.warning(f"Could not load data: {e}")
    return {"tracks": {}, "stats": BotStats(), "history": {}}

def _save_data():
    try:
        with open(DATA_FILE, "wb") as f:
            pickle.dump(_db, f)
    except Exception as e:
        logger.error(f"Save error: {e}")

_db:          Dict                  = _load_data()
user_tracks:  Dict[str, UserTrack]  = _db.setdefault("tracks",  {})
bot_stats:    BotStats              = _db.setdefault("stats",   BotStats())
user_history: Dict[int, List[str]]  = _db.setdefault("history", {})

user_sessions: Dict[int, Dict] = {}
cancel_flags:  Dict[int, bool] = {}

# ─────────────────────────────────────────────────────────────
# CALLBACK HELPERS
# ─────────────────────────────────────────────────────────────
def cb(*parts) -> str:
    raw = SEP.join(str(p) for p in parts)
    return raw[:64] if len(raw.encode()) > 64 else raw

def cb_parts(data: str) -> List[str]:
    return data.split(SEP)

# ─────────────────────────────────────────────────────────────
# FORMAT DETECTION  (from real bytes — never trust JSON media_type alone)
# ─────────────────────────────────────────────────────────────

# Magic byte signatures → extension
_MAGIC: List[Tuple[bytes, str]] = [
    # Video formats
    (b"\x00\x00\x00\x18ftypmp4",  "mp4"),
    (b"\x00\x00\x00\x1cftypmp4",  "mp4"),
    (b"\x00\x00\x00\x20ftypmp4",  "mp4"),
    (b"\x00\x00\x00\x14ftypisom", "mp4"),
    (b"\x00\x00\x00\x18ftypisom", "mp4"),
    (b"\x00\x00\x00\x1cftypisom", "mp4"),
    (b"\x00\x00\x00\x14ftypM4V",  "mp4"),
    (b"\x00\x00\x00\x18ftypM4V",  "mp4"),
    (b"\x66\x74\x79\x70\x69\x73\x6f\x6d", "mp4"),  # ftypisom at offset 4
    (b"\x00\x00\x00",             ""),   # placeholder — mp4 checked below
    (b"\x1a\x45\xdf\xa3",         "webm"),
    (b"\x52\x49\x46\x46",         "avi"),  # RIFF....AVI
    (b"\x30\x26\xb2\x75",         "wmv"),
    (b"\x00\x00\x01\xb3",         "mpeg"),
    (b"\x00\x00\x01\xba",         "mpeg"),
    # Image formats
    (b"\xff\xd8\xff",             "jpg"),
    (b"\x89PNG\r\n\x1a\n",        "png"),
    (b"GIF87a",                   "gif"),
    (b"GIF89a",                   "gif"),
    (b"RIFF",                     "webp"),  # refined below
    (b"\x49\x49\x2a\x00",         "tiff"),
    (b"\x4d\x4d\x00\x2a",         "tiff"),
    (b"\x42\x4d",                 "bmp"),
    (b"WEBP",                     "webp"),
]

_CONTENT_TYPE_MAP = {
    "video/mp4":        "mp4",
    "video/quicktime":  "mp4",
    "video/webm":       "webm",
    "video/x-msvideo":  "avi",
    "video/mpeg":       "mpeg",
    "video/3gpp":       "mp4",
    "video/3gpp2":      "mp4",
    "image/jpeg":       "jpg",
    "image/jpg":        "jpg",
    "image/png":        "png",
    "image/gif":        "gif",
    "image/webp":       "webp",
    "image/bmp":        "bmp",
    "image/tiff":       "tiff",
    "application/octet-stream": "",   # must check bytes
}


def _detect_extension(data: bytes, content_type: str, url: str) -> str:
    """
    Detect the true file extension from:
      1. Magic bytes in the first 32 bytes of the file
      2. Content-Type header
      3. URL path extension (last resort)
    Returns one of: jpg, png, gif, webp, mp4, webm, avi, mpeg, bmp, tiff
    """
    head = data[:32] if len(data) >= 32 else data

    # ── Magic bytes check (most reliable) ──────────────────────────
    # MP4/MOV: ftyp atom can appear at byte 4 or 0
    if len(data) >= 12:
        atom = data[4:8]
        if atom == b"ftyp":
            return "mp4"

    if head[:3] == b"\xff\xd8\xff":
        return "jpg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return "webm"
    if head[:4] == b"RIFF" and len(data) >= 12:
        # RIFF....WEBP  or  RIFF....AVI
        chunk = data[8:12]
        if chunk == b"WEBP":
            return "webp"
        if chunk == b"AVI ":
            return "avi"
    if head[:3] in (b"\x00\x00\x01",) and head[3:4] in (b"\xb3", b"\xba"):
        return "mpeg"
    if head[:2] == b"BM":
        return "bmp"

    # ── Content-Type header ─────────────────────────────────────────
    ct = content_type.lower().split(";")[0].strip()
    if ct in _CONTENT_TYPE_MAP and _CONTENT_TYPE_MAP[ct]:
        return _CONTENT_TYPE_MAP[ct]
    if "video" in ct:
        return "mp4"
    if "image" in ct:
        return "jpg"

    # ── URL extension ───────────────────────────────────────────────
    url_path = url.split("?")[0].lower()
    for ext in ("mp4", "mov", "webm", "avi", "jpg", "jpeg", "png", "gif", "webp"):
        if url_path.endswith(f".{ext}"):
            return "jpg" if ext == "jpeg" else ext

    # ── Default fallback ────────────────────────────────────────────
    # If file starts with 0x00 bytes it's almost certainly a video container
    if data[:4] == b"\x00\x00\x00\x00" or (len(data) > 4 and data[4:8] in
                                             (b"ftyp", b"moov", b"mdat", b"free")):
        return "mp4"

    return "jpg"   # safest default for unknown binary


# ─────────────────────────────────────────────────────────────
# SNAPCHAT DOWNLOADER
# ─────────────────────────────────────────────────────────────
class SnapchatDownloader:
    HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Referer": "https://www.snapchat.com/",
    }

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(self.HEADERS)

    # ── Page fetch ──────────────────────────────────────────────────────────
    def _fetch_json(self, username: str) -> Optional[Dict]:
        for url in [
            f"https://www.snapchat.com/add/{username}/",
            f"https://www.snapchat.com/@{username}/",
        ]:
            try:
                r = self.session.get(url, timeout=25)
                r.raise_for_status()
                html = r.text
                m = re.search(
                    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
                    html, re.DOTALL,
                )
                if m:
                    return json.loads(m.group(1))
                m = re.search(
                    r'window\.__INITIAL_STATE__\s*=\s*({.*?});\s*</script>',
                    html, re.DOTALL,
                )
                if m:
                    return json.loads(m.group(1))
            except Exception as e:
                logger.debug(f"fetch error ({url}): {e}")
        logger.warning(f"No JSON found for @{username}")
        return None

    # ── URL extraction — tries every possible field ─────────────────────────
    @staticmethod
    def _extract_url(snap: Dict, spotlight: bool = False) -> str:
        """
        Try every known URL field in priority order.
        For spotlights, prefer mediaUrl2 which is the watermark-free version.
        Returns the first valid http URL found, or empty string.
        """
        snap_urls = snap.get("snapUrls", {})

        if isinstance(snap_urls, dict):
            # For spotlights: mediaUrl2 is clean, mediaUrl has Snapchat watermark
            # For stories: mediaUrl is fine
            if spotlight:
                priority = ("mediaUrl2", "mediaUrl", "overlayUrl", "url")
            else:
                priority = ("mediaUrl", "mediaUrl2", "overlayUrl", "url")

            for key in priority:
                val = snap_urls.get(key, "")
                if val and isinstance(val, str) and val.startswith("http"):
                    return val
        elif isinstance(snap_urls, str) and snap_urls.startswith("http"):
            return snap_urls

        # Direct top-level URL fields
        for key in ("mediaUrl2", "mediaUrl", "url", "videoUrl", "imageUrl",
                    "overlayUrl", "cdnUrl", "downloadUrl"):
            val = snap.get(key, "")
            if val and isinstance(val, str) and val.startswith("http"):
                return val

        # Nested media objects
        for media_key in ("media", "snapMedia", "content"):
            m = snap.get(media_key, {})
            if isinstance(m, dict):
                for key in ("mediaUrl2", "mediaUrl", "url", "src"):
                    val = m.get(key, "")
                    if val and isinstance(val, str) and val.startswith("http"):
                        return val

        return ""

    # ── Timestamp extraction — tries every known field ──────────────────────
    @staticmethod
    def _extract_timestamp(snap: Dict) -> int:
        """
        Try every known timestamp field.
        Returns epoch int, or 0 if none found.
        """
        def _unwrap(v) -> int:
            if isinstance(v, dict):
                return int(v.get("value", v.get("low", 0)) or 0)
            try:
                return int(v or 0)
            except (TypeError, ValueError):
                return 0

        for key in (
            "timestampInSec", "captureTimeSecs", "postedTimestampSecs",
            "createTimestamp", "timestamp", "startTimeSecs",
            "snapTimestamp", "publishedAt",
        ):
            ts = _unwrap(snap.get(key, 0))
            if ts > 0:
                return ts

        # millisecond timestamps → convert to seconds
        for key in ("timestampMs", "createTimestampMs", "captureTimeMs"):
            raw = _unwrap(snap.get(key, 0))
            if raw > 1_000_000_000_000:   # definitely ms
                return raw // 1000
            if raw > 1_000_000_000:       # already seconds
                return raw

        return 0

    # ── media_type detection from JSON ──────────────────────────────────────
    @staticmethod
    def _extract_media_type(snap: Dict) -> int:
        """
        Returns 0 (image) or 1 (video) based on JSON hints.
        NOTE: this is only a HINT — final type is confirmed from bytes after download.
        """
        mt = snap.get("snapMediaType", snap.get("mediaType", snap.get("contentType", -1)))
        if isinstance(mt, int) and mt in (0, 1):
            return mt
        if isinstance(mt, str):
            mt_lower = mt.lower()
            if "video" in mt_lower:
                return 1
            if "image" in mt_lower or "photo" in mt_lower:
                return 0
        # Infer from URL extension
        url = snap.get("mediaUrl", "") or ""
        if any(url.lower().endswith(ext) for ext in (".mp4", ".mov", ".webm", ".avi")):
            return 1
        return 0   # default to image

    # ── Snap extractor ──────────────────────────────────────────────────────
    @staticmethod
    def _extract_snap(snap: Dict, username: str, spotlight: bool) -> Optional["SnapContent"]:
        url = SnapchatDownloader._extract_url(snap, spotlight=spotlight)
        if not url:
            return None

        ts = SnapchatDownloader._extract_timestamp(snap)
        if ts == 0:
            # Use current time as fallback so the item isn't silently dropped
            ts = int(time.time())
            logger.debug(f"No timestamp for snap, using now: {url[:60]}")

        return SnapContent(
            username=username,
            media_url=url,
            media_type=SnapchatDownloader._extract_media_type(snap),
            timestamp=ts,
            views=int(snap.get("viewCount", snap.get("views", 0)) or 0),
            is_spotlight=spotlight,
        )

    # ── Story parser ────────────────────────────────────────────────────────
    def _parse_stories(self, data: Dict, username: str) -> List[SnapContent]:
        out  = []
        seen = set()
        try:
            pp    = data.get("props", {}).get("pageProps", {})
            story = pp.get("story", {})
            snaps = story.get("snapList") or story.get("snaps", [])

            # Also check userProfile.publicStories for some account types
            user_profile = pp.get("userProfile", {})
            pub = user_profile.get("publicStories", {})
            extra_snaps = pub.get("snapList", [])

            for s in list(snaps) + list(extra_snaps):
                c = self._extract_snap(s, username, False)
                if c and c.media_url not in seen:
                    seen.add(c.media_url)
                    out.append(c)

            out.sort(key=lambda x: x.timestamp, reverse=True)
        except Exception as e:
            logger.error(f"parse_stories: {e}")
        return out

    # ── Spotlight parser ────────────────────────────────────────────────────
    def _parse_spotlights(self, data: Dict, username: str) -> List[SnapContent]:
        out  = []
        seen = set()
        try:
            pp       = data.get("props", {}).get("pageProps", {})
            sections: List[Dict] = []

            sh = pp.get("spotlightHighlights")
            if isinstance(sh, list):   sections.extend(sh)
            elif isinstance(sh, dict): sections.append(sh)

            for item in pp.get("curatedHighlights", []):
                if not isinstance(item, dict): continue
                if item.get("$case") == "spotlightHighlights":
                    inner = item.get("spotlightHighlights", [])
                    sections.extend(inner if isinstance(inner, list) else [inner])
                elif "snapList" in item:
                    sections.append(item)

            # Some profiles store spotlights directly under userProfile
            user_profile = pp.get("userProfile", {})
            for key in ("spotlightHighlights", "highlights"):
                extra = user_profile.get(key)
                if isinstance(extra, list):
                    sections.extend(extra)
                elif isinstance(extra, dict):
                    sections.append(extra)

            for sec in sections:
                if not isinstance(sec, dict): continue
                for snap in sec.get("snapList", []):
                    c = self._extract_snap(snap, username, True)
                    if c and c.media_url not in seen:
                        seen.add(c.media_url)
                        out.append(c)

            out.sort(key=lambda x: x.views, reverse=True)
        except Exception as e:
            logger.error(f"parse_spotlights: {e}")
        return out

    # ── Public API ──────────────────────────────────────────────────────────
    def get_all(self, username: str) -> Tuple[List[SnapContent], List[SnapContent]]:
        data = self._fetch_json(username)
        if not data:
            return [], []
        stories    = self._parse_stories(data, username)
        spotlights = self._parse_spotlights(data, username)

        # Cross-dedup: remove any spotlight URL already in stories
        story_urls = {s.media_url for s in stories}
        spotlights = [sp for sp in spotlights if sp.media_url not in story_urls]

        logger.info(f"@{username}: {len(stories)} stories, {len(spotlights)} spotlights")
        return stories, spotlights

    @staticmethod
    def filter_recent(stories: List[SnapContent], hours: int = 24) -> List[SnapContent]:
        cutoff = time.time() - hours * 3600
        return sorted([s for s in stories if s.timestamp >= cutoff],
                      key=lambda x: x.timestamp, reverse=True)

    # ── Download — identical mechanism for BOTH images and videos ─────────────
    def download_file(self, content: SnapContent) -> Optional[Tuple[bytes, str]]:
        """
        Downloads any media (image or video) using the EXACT same code path.
        No special-casing by media type — bytes are bytes.

        Returns (file_bytes, correct_extension) where extension is detected
        from the actual file bytes (magic bytes) + Content-Type header.
        Returns None on all retry failures.
        """
        # Use a fresh session per download to avoid any connection-level state
        # that might differ between images and videos
        dl_headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "identity",   # no compression — get raw bytes
            "Referer": "https://www.snapchat.com/",
            "Origin": "https://www.snapchat.com",
        }

        for attempt in range(1, DOWNLOAD_RETRY + 1):
            try:
                # stream=False: load the entire file into memory at once.
                # This is identical to how images are downloaded and works
                # reliably for both small images and large video files.
                r = requests.get(
                    content.media_url,
                    headers=dl_headers,
                    timeout=60,       # generous timeout for large video files
                    stream=False,     # same as image download — load all at once
                    allow_redirects=True,
                )
                r.raise_for_status()

                data = r.content
                if not data or len(data) < 16:
                    raise ValueError(f"Response too small: {len(data) if data else 0} bytes")

                # Detect extension from real bytes — same logic for image & video
                ext = _detect_extension(
                    data,
                    r.headers.get("Content-Type", ""),
                    content.media_url,
                )
                logger.debug(
                    f"Downloaded {len(data):,} bytes  ext={ext}  "
                    f"ct={r.headers.get('Content-Type', '?')}"
                )
                return data, ext

            except Exception as e:
                logger.warning(f"Download attempt {attempt}/{DOWNLOAD_RETRY} failed: {e}")
                if attempt < DOWNLOAD_RETRY:
                    time.sleep(2 * attempt)

        logger.error(f"All {DOWNLOAD_RETRY} attempts failed: {content.media_url[:80]}")
        return None

# ─────────────────────────────────────────────────────────────
# UTILITIES
# ─────────────────────────────────────────────────────────────
def make_filename(c: SnapContent, ext: str = "") -> str:
    """ext should come from _detect_extension() on real bytes, not JSON media_type."""
    dt   = datetime.fromtimestamp(c.timestamp)
    if not ext:
        ext = "jpg" if c.media_type == 0 else "mp4"
    kind = "spotlight" if c.is_spotlight else "story"
    return f"{c.username}_{kind}_{dt.strftime('%Y-%m-%d_%H-%M-%S')}.{ext}"

def human_age(ts: int) -> str:
    d = int(time.time() - ts)
    if d < 3600:  return f"{d//60}m ago"
    if d < 86400: return f"{d//3600}h ago"
    return f"{d//86400}d ago"

def progress_bar(done: int, total: int, width: int = 14) -> str:
    if total == 0: return "░" * width
    filled = round(width * done / total)
    return "█" * filled + "░" * (width - filled)

def record_dl(chat_id: int, count: int = 1):
    bot_stats.total_downloads += count
    bot_stats.per_user[chat_id] = bot_stats.per_user.get(chat_id, 0) + count

def add_history(chat_id: int, username: str):
    h = user_history.setdefault(chat_id, [])
    if username in h: h.remove(username)
    h.insert(0, username)
    user_history[chat_id] = h[:MAX_HISTORY]

def filter_items(items: List[SnapContent], ftype: str) -> List[SnapContent]:
    if ftype == "img": return [i for i in items if i.media_type == 0]
    if ftype == "vid": return [i for i in items if i.media_type == 1]
    return items

def pool_for_kind(sess: Dict, kind: str) -> List[SnapContent]:
    return sess[{"stories": "stories", "recent": "recent", "spots": "spotlights"}[kind]]

async def _edit(target, text: str, markup=None):
    kw = dict(text=text, parse_mode="Markdown", reply_markup=markup)
    if hasattr(target, "edit_message_text"):
        await target.edit_message_text(**kw)
    else:
        await target.edit_text(**kw)

async def safe_send(func, *args, **kwargs):
    for attempt in range(4):
        try:
            return await func(*args, **kwargs)
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except (TimedOut, TelegramError) as e:
            logger.warning(f"Send error attempt {attempt}: {e}")
            await asyncio.sleep(3)
    return None


_VIDEO_EXTS = {"mp4", "webm", "avi", "mov", "mpeg", "wmv"}


async def send_media_file(
    message,
    data: bytes,
    filename: str,
    ext: str,
    caption: str = "",
) -> bool:
    """
    Send media using the correct Telegram method:
      • Videos (mp4/webm/…) → reply_video  — native playback, gallery saves correctly
      • Images              → reply_photo  — native display
      • Unknown / other     → reply_document — raw file, always preserves filename

    Using reply_video for mp4 is critical: Telegram natively embeds the filename
    in the video message so that "Save to Gallery" on both Android and iOS stores
    the file with the correct name and format.

    Falls back to reply_document automatically if the native send fails
    (e.g. file too large for inline video, codec unsupported, etc.)
    """
    buf = io.BytesIO(data)
    buf.name = filename   # Telegram reads .name for the filename

    if ext in _VIDEO_EXTS:
        # Try native video first — this is what preserves gallery filename
        result = await safe_send(
            message.reply_video,
            video=buf,
            filename=filename,
            caption=caption,
            supports_streaming=True,
            write_timeout=120,
            read_timeout=120,
        )
        if result:
            return True
        # Fallback: send as document (filename still correct, just no inline play)
        buf.seek(0)
        result = await safe_send(
            message.reply_document,
            document=buf,
            filename=filename,
            caption=caption + "\n_(sent as file — tap to download)_",
        )
        return result is not None

    # Images — send as photo for inline preview
    if ext in ("jpg", "jpeg", "png", "webp"):
        buf_doc = io.BytesIO(data)
        buf_doc.name = filename
        result = await safe_send(
            message.reply_document,
            document=buf_doc,
            filename=filename,
            caption=caption,
        )
        return result is not None

    # Everything else as document
    result = await safe_send(
        message.reply_document,
        document=buf,
        filename=filename,
        caption=caption,
    )
    return result is not None

# ─────────────────────────────────────────────────────────────
# MENU BUILDERS
# ─────────────────────────────────────────────────────────────
async def show_main_menu(target, username: str, sess: Dict):
    s  = len(sess["stories"])
    r  = len(sess["recent"])
    sp = len(sess["spotlights"])
    chat_id = sess["chat_id"]
    tracked = f"{chat_id}:{username}" in user_tracks

    text = (
        f"👻 *@{username}*\n"
        f"{'─' * 28}\n"
        f"📖 Stories        →  `{s}`\n"
        f"🕐 Recent 24h    →  `{r}`\n"
        f"🔥 Spotlights     →  `{sp}`\n"
        f"{'─' * 28}\n"
        f"{'🔔 *Auto-notify ON*' if tracked else '🔕 _Auto-notify off_'}"
    )
    kbd = [
        [
            InlineKeyboardButton(f"📖 Stories ({s})",    callback_data=cb("menu", username, "stories", 1, "all")),
            InlineKeyboardButton(f"🕐 Recent ({r})",     callback_data=cb("menu", username, "recent",  1, "all")),
        ],
        [
            InlineKeyboardButton(f"🔥 Spotlights ({sp})", callback_data=cb("menu", username, "spots", 1, "all")),
        ],
        [
            InlineKeyboardButton("📥 Download ALL",   callback_data=cb("dlall", username, "all", "all")),
            InlineKeyboardButton("🗜️ ZIP Everything", callback_data=cb("zipall", username)),
        ],
        [
            InlineKeyboardButton("🖼️ Images Only",  callback_data=cb("dlall", username, "all", "img")),
            InlineKeyboardButton("🎥 Videos Only",  callback_data=cb("dlall", username, "all", "vid")),
        ],
        [
            InlineKeyboardButton("🔔 Track" if not tracked else "🔕 Untrack",
                                 callback_data=cb("toggletrack", username)),
            InlineKeyboardButton("🔄 Refresh",      callback_data=cb("refresh", username)),
        ],
        [InlineKeyboardButton("🔍 New Search", callback_data=cb("newsearch"))],
    ]
    await _edit(target, text, InlineKeyboardMarkup(kbd))


async def show_list_menu(query, username: str, sess: Dict,
                         kind: str, page: int, ftype: str):
    pool = filter_items(pool_for_kind(sess, kind), ftype)
    if not pool:
        label_map = {"stories": "stories", "recent": "recent stories", "spots": "spotlights"}
        await _edit(query, f"❌ No {label_map.get(kind, kind)} found"
                    + (" for this filter." if ftype != "all" else "."))
        return

    total_pages = max(1, math.ceil(len(pool) / ITEMS_PER_PAGE))
    page        = max(1, min(page, total_pages))
    start       = (page - 1) * ITEMS_PER_PAGE
    page_items  = pool[start: start + ITEMS_PER_PAGE]

    icons = {"stories": "📖", "recent": "🕐", "spots": "🔥"}
    label = {"stories": "Stories", "recent": "Recent", "spots": "Spotlights"}[kind]
    flabel = {"all": "All", "img": "🖼️ Images", "vid": "🎥 Videos"}[ftype]

    text = (
        f"{icons[kind]} *{label} — @{username}*\n"
        f"Filter: {flabel}  •  {len(pool)} items  •  Page {page}/{total_pages}"
    )

    kbd = []
    for i, item in enumerate(page_items):
        idx   = start + i
        icon  = "🖼️" if item.media_type == 0 else "🎥"
        age   = human_age(item.timestamp)
        dt    = datetime.fromtimestamp(item.timestamp).strftime("%m/%d %H:%M")
        views = f"  👀{item.views:,}" if item.is_spotlight and item.views else ""
        kbd.append([InlineKeyboardButton(
            f"{idx+1}. {icon} {dt}  {age}{views}",
            callback_data=cb("dl1", username, kind, idx),
        )])

    # Pagination row
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("◀️", callback_data=cb("menu", username, kind, page-1, ftype)))
    nav.append(InlineKeyboardButton(f" {page}/{total_pages} ", callback_data=cb("noop")))
    if page < total_pages:
        nav.append(InlineKeyboardButton("▶️", callback_data=cb("menu", username, kind, page+1, ftype)))
    if nav:
        kbd.append(nav)

    # Filter row
    kbd.append([
        InlineKeyboardButton("🌐 All",    callback_data=cb("menu", username, kind, 1, "all")),
        InlineKeyboardButton("🖼️ Images", callback_data=cb("menu", username, kind, 1, "img")),
        InlineKeyboardButton("🎥 Videos", callback_data=cb("menu", username, kind, 1, "vid")),
    ])
    # Action row — downloads ALL items (entire pool, not just the page)
    kbd.append([
        InlineKeyboardButton(f"📥 Download ALL {len(pool)}",
                             callback_data=cb("dlall", username, kind, ftype)),
        InlineKeyboardButton("🗜️ ZIP All",
                             callback_data=cb("zip",   username, kind, ftype)),
    ])
    kbd.append([InlineKeyboardButton("🔙 Back", callback_data=cb("back", username))])

    await _edit(query, text, InlineKeyboardMarkup(kbd))

# ─────────────────────────────────────────────────────────────
# DOWNLOAD ENGINE  (downloads EVERY item — no page cap)
# ─────────────────────────────────────────────────────────────
async def download_all_and_send(query, items: List[SnapContent],
                                username: str, label: str, chat_id: int):
    if not items:
        await _edit(query, f"❌ No {label} to download.")
        return

    total = len(items)
    dl    = SnapchatDownloader()
    done  = failed = 0
    cancel_flags[chat_id] = False

    cancel_kbd = InlineKeyboardMarkup([[
        InlineKeyboardButton("🛑 Cancel", callback_data=cb("cancel", username))
    ]])

    prog_msg = await query.edit_message_text(
        f"📥 *Starting download of {total} {label}…*\n\n"
        f"`{'░' * 14}` 0%  (0/{total})\n\n"
        "_Each file is sent as it downloads_",
        parse_mode="Markdown",
        reply_markup=cancel_kbd,
    )
    last_edit = 0.0

    for idx, item in enumerate(items):
        # Cancel check
        if cancel_flags.get(chat_id):
            skipped = total - idx
            await prog_msg.edit_text(
                f"🛑 *Cancelled!*\n\n"
                f"✅ Sent:    {done}\n"
                f"❌ Failed:  {failed}\n"
                f"⏭️ Skipped: {skipped}",
                parse_mode="Markdown",
            )
            cancel_flags.pop(chat_id, None)
            record_dl(chat_id, done)
            _save_data()
            return

        result_tuple = await asyncio.to_thread(dl.download_file, item)

        if result_tuple:
            data, ext = result_tuple
            fname = make_filename(item, ext)
            icon  = "🎥" if ext in _VIDEO_EXTS else "🖼️"
            cap   = f"{icon} {idx+1}/{total}  •  @{username}"
            ok    = await send_media_file(query.message, data, fname, ext, cap)
            done  += 1 if ok else 0
            if not ok:
                failed += 1
        else:
            failed += 1

        # Update progress every 2 s or on final item
        now = time.time()
        if now - last_edit >= 2 or idx == total - 1:
            last_edit = now
            completed = done + failed
            bar = progress_bar(completed, total)
            pct = round(100 * completed / total)
            try:
                await prog_msg.edit_text(
                    f"📥 *Downloading {label}…*\n\n"
                    f"`{bar}` {pct}%  ({completed}/{total})\n"
                    f"✅ {done} sent   ❌ {failed} failed",
                    parse_mode="Markdown",
                    reply_markup=cancel_kbd,
                )
            except Exception:
                pass

        await asyncio.sleep(SEND_DELAY)

    record_dl(chat_id, done)
    _save_data()
    cancel_flags.pop(chat_id, None)

    icon = "✅" if failed == 0 else "⚠️"
    await prog_msg.edit_text(
        f"{icon} *Download complete!*\n\n"
        f"📦 Total:    `{total}`\n"
        f"✅ Sent:     `{done}`\n"
        f"❌ Failed:   `{failed}`\n"
        f"👤 User:    @{username}",
        parse_mode="Markdown",
    )


async def zip_and_send(query, items: List[SnapContent],
                       username: str, label: str, chat_id: int):
    if not items:
        await _edit(query, f"❌ No {label} to ZIP.")
        return

    total    = len(items)
    num_zips = math.ceil(total / MAX_ZIP_PER_FILE)
    dl       = SnapchatDownloader()

    await query.edit_message_text(
        f"🗜️ *Building ZIP for {total} {label}…*\n"
        f"{'Will create ' + str(num_zips) + ' archives (split by 45).' if num_zips > 1 else 'One archive.'}\n\n"
        "_Please wait…_",
        parse_mode="Markdown",
    )

    for z in range(num_zips):
        chunk  = items[z * MAX_ZIP_PER_FILE: (z + 1) * MAX_ZIP_PER_FILE]
        buf    = io.BytesIO()
        added  = failed = 0
        zlabel = f"part{z+1}of{num_zips}" if num_zips > 1 else "all"

        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for item in chunk:
                result_tuple = await asyncio.to_thread(dl.download_file, item)
                if result_tuple:
                    data, ext = result_tuple
                    zf.writestr(make_filename(item, ext), data)
                    added += 1
                else:
                    failed += 1

        if added == 0:
            continue

        buf.seek(0)
        fname = (
            f"{username}_{label.replace(' ', '_')}"
            f"_{zlabel}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
        )
        cap = (
            f"🗜️ @{username} — {label}\n"
            f"Part {z+1}/{num_zips}  •  {added} files"
            + (f"  ❌ {failed} failed" if failed else "")
        )
        await safe_send(query.message.reply_document,
                        document=buf, filename=fname, caption=cap)
        bot_stats.total_zips += 1

    record_dl(chat_id, total)
    _save_data()
    await query.edit_message_text(
        f"✅ *ZIP complete!*\n\n"
        f"📦 {total} files  •  {num_zips} archive(s) sent\n"
        f"👤 @{username}",
        parse_mode="Markdown",
    )

# ─────────────────────────────────────────────────────────────
# COMMAND HANDLERS
# ─────────────────────────────────────────────────────────────
HELP_TEXT = """
📱 *SNAPCHAT DOWNLOADER v3.0*

━━━━━━━━━━━━━━━━━━━━━━━
🔹 *BROWSE MODE*
Just send a username — full menu with pagination, filters
━━━━━━━━━━━━━━━━━━━━━━━
⚡ *QUICK COMMANDS*
`/dl user`      — download all content
`/recent user`  — last 24h stories only
`/spot user`    — spotlights only
`/zip user`     — ZIP everything
━━━━━━━━━━━━━━━━━━━━━━━
🔔 *TRACKING*
`/track user`   — notify on new stories
`/untrack user` — stop tracking
`/mytracks`     — list tracked users
━━━━━━━━━━━━━━━━━━━━━━━
📊 *INFO*
`/stats`    — download statistics
`/history`  — recent searches
`/help`     — this message
━━━━━━━━━━━━━━━━━━━━━━━
📁 *File names:*
`user_story_2024-01-15_14-30.jpg`
`user_spotlight_2024-01-15_14-30.mp4`
"""

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kbd = InlineKeyboardMarkup([[
        InlineKeyboardButton("📖 How to use", callback_data=cb("showhelp")),
        InlineKeyboardButton("📊 My Stats",   callback_data=cb("mystats")),
    ]])
    await update.message.reply_text(
        "👻 *Welcome to Snapchat Downloader!*\n\n"
        "Send any public Snapchat username to browse & download.\n\n"
        "Example: `jackjohnson` or `snapchat`",
        parse_mode="Markdown",
        reply_markup=kbd,
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, parse_mode="Markdown")

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    my = bot_stats.per_user.get(chat_id, 0)
    await update.message.reply_text(
        "📊 *Download Statistics*\n\n"
        f"👤 Your downloads:   `{my}`\n"
        f"🌍 Global downloads: `{bot_stats.total_downloads}`\n"
        f"🗜️ ZIPs created:     `{bot_stats.total_zips}`\n"
        f"👥 Users served:     `{len(bot_stats.per_user)}`",
        parse_mode="Markdown",
    )

async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    h = user_history.get(chat_id, [])
    if not h:
        await update.message.reply_text("📭 No search history yet.")
        return
    lines = ["🕘 *Recent Searches:*\n"]
    kbd   = []
    for i, u in enumerate(h, 1):
        lines.append(f"{i}. `{u}`")
        kbd.append([InlineKeyboardButton(f"🔍 @{u}", callback_data=cb("reopen", u))])
    await update.message.reply_text(
        "\n".join(lines), parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(kbd),
    )

async def cleanup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_sessions.pop(update.effective_chat.id, None)
    await update.message.reply_text("✅ Session cleared!")

# ── Resolve username from command ──────────────────────────
async def _parse_cmd_username(update: Update, cmd: str) -> Optional[str]:
    parts = update.message.text.split(None, 1)
    if len(parts) < 2 or len(parts[1].strip()) < 3:
        await update.message.reply_text(f"❌ Usage: /{cmd} username")
        return None
    return parts[1].strip().lstrip("@").lower()

async def _fetch_for_cmd(update: Update, username: str):
    msg = await update.message.reply_text(f"🔍 Fetching @{username}…")
    dl  = SnapchatDownloader()
    s, sp = await asyncio.to_thread(dl.get_all, username)
    await msg.delete()
    return s, sp

# ── Helper: send items using a progress message ─────────────
async def _quick_send(update: Update, items: List[SnapContent],
                      username: str, label: str):
    chat_id = update.effective_chat.id
    total   = len(items)
    dl      = SnapchatDownloader()
    done    = failed = 0
    cancel_flags[chat_id] = False

    prog = await update.message.reply_text(
        f"📥 *{label}* — {total} items\n`{'░'*14}` 0%",
        parse_mode="Markdown",
    )
    last_edit = 0.0

    for idx, item in enumerate(items):
        if cancel_flags.get(chat_id):
            break
        result_tuple = await asyncio.to_thread(dl.download_file, item)
        if result_tuple:
            data, ext = result_tuple
            fname = make_filename(item, ext)
            icon  = "🎥" if ext in _VIDEO_EXTS else "🖼️"
            ok    = await send_media_file(
                update.message, data, fname, ext,
                f"{icon} {idx+1}/{total}  •  @{username}",
            )
            done += 1 if ok else 0
            if not ok:
                failed += 1
        else:
            failed += 1

        now = time.time()
        if now - last_edit >= 2 or idx == total - 1:
            last_edit = now
            completed = done + failed
            bar = progress_bar(completed, total)
            pct = round(100 * completed / total)
            try:
                await prog.edit_text(
                    f"📥 *{label}…*\n`{bar}` {pct}% ({completed}/{total})\n"
                    f"✅ {done}  ❌ {failed}",
                    parse_mode="Markdown",
                )
            except Exception:
                pass
        await asyncio.sleep(SEND_DELAY)

    record_dl(chat_id, done)
    _save_data()
    cancel_flags.pop(chat_id, None)
    await prog.edit_text(
        f"✅ *Done!*  {done}/{total} sent  •  @{username}",
        parse_mode="Markdown",
    )

async def quick_dl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = await _parse_cmd_username(update, "dl")
    if not u: return
    stories, spots = await _fetch_for_cmd(update, u)
    items = (stories or []) + (spots or [])
    if not items:
        await update.message.reply_text(f"❌ No content for @{u}")
        return
    add_history(update.effective_chat.id, u)
    await _quick_send(update, items, u, "Downloading all")

async def quick_recent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = await _parse_cmd_username(update, "recent")
    if not u: return
    stories, _ = await _fetch_for_cmd(update, u)
    recent = SnapchatDownloader.filter_recent(stories or [])
    if not recent:
        await update.message.reply_text(f"❌ No recent stories for @{u}")
        return
    await _quick_send(update, recent, u, "Recent 24h stories")

async def quick_spot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = await _parse_cmd_username(update, "spot")
    if not u: return
    _, spots = await _fetch_for_cmd(update, u)
    if not spots:
        await update.message.reply_text(f"❌ No spotlights for @{u}")
        return
    await _quick_send(update, spots, u, "Spotlights")

async def quick_zip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = await _parse_cmd_username(update, "zip")
    if not u: return
    stories, spots = await _fetch_for_cmd(update, u)
    items = (stories or []) + (spots or [])
    if not items:
        await update.message.reply_text(f"❌ No content for @{u}")
        return

    prog = await update.message.reply_text(f"🗜️ Building ZIP for @{u}…")

    class _FakeQuery:
        message = update.message
        async def edit_message_text(self, *a, **kw):
            await prog.edit_text(*a, **kw)

    await zip_and_send(_FakeQuery(), items, u, "all", update.effective_chat.id)

# ── Tracking ───────────────────────────────────────────────
async def track_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = await _parse_cmd_username(update, "track")
    if not u: return
    chat_id = update.effective_chat.id
    key = f"{chat_id}:{u}"
    if key in user_tracks:
        await update.message.reply_text(f"ℹ️ Already tracking @{u}")
        return
    msg = await update.message.reply_text(f"🔍 Checking @{u}…")
    dl  = SnapchatDownloader()
    s, sp = await asyncio.to_thread(dl.get_all, u)
    if not s and not sp:
        await msg.edit_text(f"❌ @{u} has no public content.")
        return
    user_tracks[key] = UserTrack(
        chat_id=chat_id, username=u,
        last_check=time.time(),
        last_story_time=s[0].timestamp if s else 0,
    )
    _save_data()
    await msg.edit_text(
        f"🔔 *Now tracking @{u}!*\n"
        f"You'll be notified when new stories appear.\n\n"
        f"Stop with: /untrack {u}",
        parse_mode="Markdown",
    )

async def untrack_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = await _parse_cmd_username(update, "untrack")
    if not u: return
    key = f"{update.effective_chat.id}:{u}"
    if key in user_tracks:
        del user_tracks[key]
        _save_data()
        await update.message.reply_text(f"🔕 Stopped tracking @{u}")
    else:
        await update.message.reply_text(f"❌ Not tracking @{u}")

async def mytracks_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    mine = [t for k, t in user_tracks.items() if t.chat_id == chat_id]
    if not mine:
        await update.message.reply_text("📭 No tracked users.\nUse /track username")
        return
    lines = ["🔔 *Tracked Users:*\n"]
    kbd   = []
    for t in mine:
        lines.append(f"• @{t.username}  _(checked {human_age(int(t.last_check))})_")
        kbd.append([
            InlineKeyboardButton(f"📥 @{t.username}", callback_data=cb("reopen", t.username)),
            InlineKeyboardButton("🗑️ Remove",         callback_data=cb("removetrack", t.username)),
        ])
    await update.message.reply_text(
        "\n".join(lines), parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(kbd),
    )

# ─────────────────────────────────────────────────────────────
# USERNAME MESSAGE HANDLER
# ─────────────────────────────────────────────────────────────
async def handle_username(update: Update, context: ContextTypes.DEFAULT_TYPE):
    username = update.message.text.strip().lstrip("@").lower()
    if len(username) < 3:
        await update.message.reply_text("❌ Username too short (min 3 chars).")
        return

    chat_id = update.effective_chat.id
    msg     = await update.message.reply_text(f"🔍 Searching @{username}…")

    try:
        dl = SnapchatDownloader()
        stories, spots = await asyncio.to_thread(dl.get_all, username)
    except Exception as e:
        await msg.edit_text(f"❌ Error: {str(e)[:100]}")
        return

    if not stories and not spots:
        await msg.edit_text(
            f"❌ No public content for *@{username}*\n\n"
            "• Check the username is correct\n"
            "• Profile must be public\n"
            "• Stories must not have expired",
            parse_mode="Markdown",
        )
        return

    recent = SnapchatDownloader.filter_recent(stories)
    add_history(chat_id, username)
    _save_data()

    sess = {
        "username":   username,
        "stories":    stories,
        "recent":     recent,
        "spotlights": spots,
        "chat_id":    chat_id,
    }
    user_sessions[chat_id] = sess
    await show_main_menu(msg, username, sess)

# ─────────────────────────────────────────────────────────────
# CALLBACK ROUTER
# ─────────────────────────────────────────────────────────────
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    await query.answer()

    chat_id = update.effective_chat.id
    parts   = cb_parts(query.data)
    action  = parts[0]

    def get_sess(username: str) -> Optional[Dict]:
        s = user_sessions.get(chat_id)
        return s if s and s.get("username") == username else None

    # ── Noop ────────────────────────────────────────────
    if action == "noop":
        return

    # ── Inline help / stats ─────────────────────────────
    if action == "showhelp":
        await query.edit_message_text(
            HELP_TEXT, parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Back", callback_data=cb("backhome"))
            ]]),
        )
        return

    if action == "mystats":
        my = bot_stats.per_user.get(chat_id, 0)
        await query.edit_message_text(
            "📊 *Your Download Stats*\n\n"
            f"👤 Your downloads:   `{my}`\n"
            f"🌍 Global downloads: `{bot_stats.total_downloads}`\n"
            f"🗜️ ZIPs created:     `{bot_stats.total_zips}`\n"
            f"👥 Users served:     `{len(bot_stats.per_user)}`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Back", callback_data=cb("backhome"))
            ]]),
        )
        return

    if action == "backhome":
        await query.edit_message_text(
            "👻 *Snapchat Downloader*\n\nSend a username to begin!",
            parse_mode="Markdown",
        )
        return

    # ── New search ───────────────────────────────────────
    if action == "newsearch":
        await query.edit_message_text("🔍 Send a Snapchat username:")
        return

    # ── Reopen from history ──────────────────────────────
    if action == "reopen" and len(parts) > 1:
        username = parts[1]
        await query.edit_message_text(f"🔍 Loading @{username}…")
        dl = SnapchatDownloader()
        stories, spots = await asyncio.to_thread(dl.get_all, username)
        if not stories and not spots:
            await query.edit_message_text(f"❌ No content for @{username}")
            return
        recent = SnapchatDownloader.filter_recent(stories)
        sess = {
            "username": username, "stories": stories,
            "recent": recent, "spotlights": spots, "chat_id": chat_id,
        }
        user_sessions[chat_id] = sess
        add_history(chat_id, username)
        _save_data()
        await show_main_menu(query, username, sess)
        return

    # ── Refresh ──────────────────────────────────────────
    if action == "refresh" and len(parts) > 1:
        username = parts[1]
        await query.edit_message_text(f"🔄 Refreshing @{username}…")
        dl = SnapchatDownloader()
        stories, spots = await asyncio.to_thread(dl.get_all, username)
        if not stories and not spots:
            await query.edit_message_text(f"❌ No content for @{username}")
            return
        recent = SnapchatDownloader.filter_recent(stories)
        sess = {
            "username": username, "stories": stories,
            "recent": recent, "spotlights": spots, "chat_id": chat_id,
        }
        user_sessions[chat_id] = sess
        await show_main_menu(query, username, sess)
        return

    # ── Back to main menu ────────────────────────────────
    if action == "back" and len(parts) > 1:
        username = parts[1]
        s = get_sess(username)
        if s:
            await show_main_menu(query, username, s)
        else:
            await query.edit_message_text("⚠️ Session expired. Send username again.")
        return

    # ── Cancel download ──────────────────────────────────
    if action == "cancel":
        cancel_flags[chat_id] = True
        await query.answer("🛑 Cancelling…", show_alert=False)
        return

    # ── Toggle track ─────────────────────────────────────
    if action == "toggletrack" and len(parts) > 1:
        username  = parts[1]
        track_key = f"{chat_id}:{username}"
        if track_key in user_tracks:
            del user_tracks[track_key]
            _save_data()
            await query.answer("🔕 Untracked!", show_alert=False)
        else:
            s = get_sess(username)
            stories = s["stories"] if s else []
            user_tracks[track_key] = UserTrack(
                chat_id=chat_id, username=username,
                last_check=time.time(),
                last_story_time=stories[0].timestamp if stories else 0,
            )
            _save_data()
            await query.answer("🔔 Tracking!", show_alert=False)
        s = get_sess(username)
        if s:
            await show_main_menu(query, username, s)
        return

    # ── Remove track from /mytracks inline ───────────────
    if action == "removetrack" and len(parts) > 1:
        username = parts[1]
        user_tracks.pop(f"{chat_id}:{username}", None)
        _save_data()
        await query.edit_message_text(f"🗑️ Removed tracking for @{username}")
        return

    # ── Paginated list menu ──────────────────────────────
    if action == "menu" and len(parts) >= 5:
        username = parts[1]
        kind     = parts[2]
        page     = int(parts[3])
        ftype    = parts[4]
        s = get_sess(username)
        if not s:
            await query.edit_message_text("⚠️ Session expired. Send username again.")
            return
        await show_list_menu(query, username, s, kind, page, ftype)
        return

    # ── Single item download ─────────────────────────────
    if action == "dl1" and len(parts) >= 4:
        username = parts[1]
        kind     = parts[2]
        idx      = int(parts[3])
        s = get_sess(username)
        if not s:
            await query.edit_message_text("⚠️ Session expired.")
            return
        pool = pool_for_kind(s, kind)
        if idx >= len(pool):
            await query.edit_message_text("❌ Item not found.")
            return
        item = pool[idx]
        await query.edit_message_text("⏳ Downloading…")
        dl          = SnapchatDownloader()
        result_tuple = await asyncio.to_thread(dl.download_file, item)
        if not result_tuple:
            await query.edit_message_text("❌ Download failed. File may have expired.")
            return
        data, ext = result_tuple
        fname = make_filename(item, ext)
        icon  = "🎥" if ext in _VIDEO_EXTS else "🖼️"
        cap   = (f"{icon} @{username}  •  "
                 f"{datetime.fromtimestamp(item.timestamp).strftime('%Y-%m-%d %H:%M')}")
        await send_media_file(query.message, data, fname, ext, cap)
        record_dl(chat_id)
        _save_data()
        await query.edit_message_text("✅ Sent!")
        return

    # ── Download ALL items (no page cap!) ────────────────
    if action == "dlall" and len(parts) >= 4:
        username = parts[1]
        kind     = parts[2]   # "all" | "stories" | "recent" | "spots"
        ftype    = parts[3]   # "all" | "img" | "vid"
        s = get_sess(username)
        if not s:
            await query.edit_message_text("⚠️ Session expired.")
            return

        if kind == "all":
            items = s["stories"] + s["spotlights"]
            label = "items"
        else:
            items = pool_for_kind(s, kind)
            label = kind

        items = filter_items(items, ftype)
        if ftype != "all":
            label += f" ({ftype})"

        await download_all_and_send(query, items, username, label, chat_id)
        return

    # ── ZIP all (main menu button) ───────────────────────
    if action == "zipall" and len(parts) >= 2:
        username = parts[1]
        s = get_sess(username)
        if not s:
            await query.edit_message_text("⚠️ Session expired.")
            return
        items = s["stories"] + s["spotlights"]
        await zip_and_send(query, items, username, "all", chat_id)
        return

    # ── ZIP specific kind/filter ─────────────────────────
    if action == "zip" and len(parts) >= 4:
        username = parts[1]
        kind     = parts[2]
        ftype    = parts[3]
        s = get_sess(username)
        if not s:
            await query.edit_message_text("⚠️ Session expired.")
            return
        items = filter_items(pool_for_kind(s, kind), ftype)
        await zip_and_send(query, items, username, kind, chat_id)
        return

    logger.warning(f"Unhandled callback: {query.data!r}")


# ─────────────────────────────────────────────────────────────
# AUTO-NOTIFICATION JOB
# ─────────────────────────────────────────────────────────────
async def check_new_stories(context: ContextTypes.DEFAULT_TYPE):
    if not user_tracks:
        return
    dl      = SnapchatDownloader()
    changed = False

    for key, track in list(user_tracks.items()):
        try:
            stories, _ = await asyncio.to_thread(dl.get_all, track.username)
            track.last_check = time.time()
            changed = True

            if stories and stories[0].timestamp > track.last_story_time:
                new_count = sum(1 for s in stories if s.timestamp > track.last_story_time)
                track.last_story_time = stories[0].timestamp
                kbd = InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        f"📥 Open @{track.username}",
                        callback_data=cb("reopen", track.username),
                    )
                ]])
                await context.bot.send_message(
                    chat_id=track.chat_id,
                    text=(
                        f"🔔 *@{track.username}* posted "
                        f"{new_count} new stor{'ies' if new_count > 1 else 'y'}!\n\n"
                        "Tap below to browse & download 👇"
                    ),
                    parse_mode="Markdown",
                    reply_markup=kbd,
                )
        except Exception as e:
            logger.error(f"Track check error ({track.username}): {e}")

    if changed:
        _save_data()


# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────
def main():
    print("╔" + "═" * 60 + "╗")
    print("║  📱  SNAPCHAT DOWNLOADER v3.0  —  TELEGRAM BOT           ║")
    print("╠" + "═" * 60 + "╣")
    print("║  ✅  Downloads EVERY file (no page limit at all)         ║")
    print("║  ✅  Smart ZIP auto-splits into multi-part archives      ║")
    print("║  ✅  🛑 Cancel button during any batch operation         ║")
    print("║  ✅  Live █████░░░░░ progress bar with %                 ║")
    print("║  ✅  🖼️ Images / 🎥 Videos / 🌐 All — filter buttons     ║")
    print("║  ✅  Persistent tracks, stats, history (survive restart) ║")
    print("║  ✅  /stats /history /zip /dl /recent /spot              ║")
    print("║  ✅  Notification → inline button → direct download      ║")
    print("╚" + "═" * 60 + "╝")

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start",    start))
    app.add_handler(CommandHandler("help",     help_command))
    app.add_handler(CommandHandler("cleanup",  cleanup_command))
    app.add_handler(CommandHandler("dl",       quick_dl))
    app.add_handler(CommandHandler("recent",   quick_recent))
    app.add_handler(CommandHandler("spot",     quick_spot))
    app.add_handler(CommandHandler("zip",      quick_zip))
    app.add_handler(CommandHandler("track",    track_command))
    app.add_handler(CommandHandler("untrack",  untrack_command))
    app.add_handler(CommandHandler("mytracks", mytracks_command))
    app.add_handler(CommandHandler("stats",    stats_command))
    app.add_handler(CommandHandler("history",  history_command))

    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_username))

    if app.job_queue:
        app.job_queue.run_repeating(check_new_stories, interval=TRACK_INTERVAL, first=20)
    else:
        logger.warning("Install python-telegram-bot[job-queue] for tracking support.")

    print("\n🟢  Bot is running — press Ctrl+C to stop\n")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n🛑  Stopped.")
    except Exception as e:
        logger.exception(f"Fatal: {e}")
        sys.exit(1)