"""
All-in-One Video Downloader Website
===================================
Flask server with two pluggable backends:
  1. ytdlp (default) — downloads directly with yt-dlp
  2. telegram        — routes every link through @allsaverbot (or any other
                       Telegram downloader bot) via a Pyrogram user client.
Set DOWNLOAD_BACKEND=telegram plus TELEGRAM_API_ID/API_HASH/SESSION_STRING
in .env to use the Telegram backend.
"""

import os
import re
import uuid
import time
import threading
import urllib.parse
from pathlib import Path

# Load .env automatically (if present) so ./start_with_telegram.sh isn't required
ENV_PATH = Path(__file__).resolve().parent / ".env"
if ENV_PATH.exists():
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

from flask import (
    Flask, render_template, request, jsonify,
    send_from_directory, redirect, url_for,
)
try:
    import yt_dlp
    HAS_YTDLP = True
except ImportError:
    HAS_YTDLP = False

try:
    from pyrogram import Client
    HAS_PYROGRAM = True
except ImportError:
    Client = None  # type: ignore
    HAS_PYROGRAM = False

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
APP_ROOT = Path(__file__).resolve().parent
DOWNLOAD_DIR = APP_ROOT / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)

# Which backend to use: "ytdlp" (default) or "telegram" (requires session)
BACKEND = os.environ.get("DOWNLOAD_BACKEND", "ytdlp").lower()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB cap for uploads

# In-memory job store: job_id -> {status, url, title, filename, error, ...}
jobs = {}
jobs_lock = threading.Lock()

URL_REGEX = re.compile(
    r"https?://(?:www\.)?[-\w@:%._+~#=]{1,256}\.[a-zA-Z0-9()]{1,6}\b[-\w@:%_+.~#?&/=]*",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def sanitize_filename(name: str) -> str:
    name = re.sub(r"[\\/:*?\"<>|]", "_", name)
    return name.strip(". ")[:120] or "video"


def detect_platform(url: str) -> str:
    host = urllib.parse.urlparse(url).netloc.lower()
    if "youtube" in host or "youtu.be" in host:
        return "YouTube"
    if "instagram" in host:
        return "Instagram"
    if "facebook" in host or "fb.watch" in host:
        return "Facebook"
    if "tiktok" in host:
        return "TikTok"
    if "twitter" in host or "x.com" in host:
        return "Twitter/X"
    if "reddit" in host:
        return "Reddit"
    if "likee" in host:
        return "Likee"
    return host


# ---------------------------------------------------------------------------
# yt-dlp backend
# ---------------------------------------------------------------------------
def fetch_info_ytdlp(url: str):
    """Return metadata (title, thumbnail, duration, formats) without downloading."""
    if not HAS_YTDLP:
        raise RuntimeError("yt-dlp is not installed on the server.")

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "cookiefile": None,
        "geo_bypass": True,
        "socket_timeout": 30,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        if info.get("_type") == "playlist":
            info = info["entries"][0]

    # Sort formats: video+audio best first
    formats = []
    for f in info.get("formats", []):
        if f.get("vcodec") == "none" and f.get("acodec") == "none":
            continue
        if f.get("ext") not in ("mp4", "webm", "m4v", "mov", "mkv", "mp3", "m4a"):
            continue
        size = f.get("filesize") or f.get("filesize_approx") or 0
        formats.append({
            "format_id": f["format_id"],
            "ext": f.get("ext", "mp4"),
            "resolution": f.get("resolution") or f.get("quality") or "best",
            "has_video": f.get("vcodec") != "none",
            "has_audio": f.get("acodec") != "none",
            "filesize": size,
            "filesize_human": human_size(size) if size else "—",
        })

    # Dedupe by resolution+ext, keep highest quality unique entries
    seen = set()
    unique_formats = []
    for f in sorted(
        formats,
        key=lambda x: (x["has_video"], x["filesize"] or 0),
        reverse=True,
    ):
        key = (f["resolution"], f["ext"])
        if key in seen:
            continue
        seen.add(key)
        unique_formats.append(f)
        if len(unique_formats) >= 8:
            break

    return {
        "title": info.get("title", "Video"),
        "thumbnail": info.get("thumbnail") or "",
        "duration": info.get("duration") or 0,
        "uploader": info.get("uploader") or info.get("channel") or "",
        "platform": detect_platform(url),
        "formats": unique_formats,
    }


def download_ytdlp(job_id: str, url: str, format_id: str | None):
    try:
        with jobs_lock:
            jobs[job_id]["status"] = "downloading"

        outtmpl = str(DOWNLOAD_DIR / f"{job_id}.%(ext)s")
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "outtmpl": outtmpl,
            "geo_bypass": True,
            "socket_timeout": 60,
            "retries": 3,
            "concurrent_fragment_downloads": 4,
        }
        if format_id:
            ydl_opts["format"] = format_id
        else:
            ydl_opts["format"] = "bv*+ba/b"

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if info.get("_type") == "playlist":
                info = info["entries"][0]
            filename = Path(ydl.prepare_filename(info))
            # yt-dlp might change extension after merging
            if not filename.exists():
                # try common merged ext
                for ext in ("mp4", "mkv", "webm", "m4a", "mp3"):
                    cand = filename.with_suffix(f".{ext}")
                    if cand.exists():
                        filename = cand
                        break

        with jobs_lock:
            jobs[job_id]["status"] = "done"
            jobs[job_id]["title"] = info.get("title", "Video")
            jobs[job_id]["filename"] = filename.name
            jobs[job_id]["filesize"] = filename.stat().st_size

    except Exception as e:
        with jobs_lock:
            jobs[job_id]["status"] = "error"
            jobs[job_id]["error"] = str(e)[:300]


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


# ---------------------------------------------------------------------------
# Telegram bot backend (routes links through @allsaverbot or any similar bot)
# ---------------------------------------------------------------------------
def download_via_telegram_bot(job_id: str, url: str):
    """
    Sends the URL to the configured Telegram bot (default @allsaverbot) using
    the USER'S personal account (Pyrogram user client, because bots can't talk
    to other bots on Telegram), waits for the bot to reply with a
    video/document/audio message, downloads it to the server, and serves it
    to the website visitor.
    """
    import asyncio
    if not HAS_PYROGRAM:
        with jobs_lock:
            jobs[job_id]["status"] = "error"
            jobs[job_id]["error"] = "Pyrogram install nahi hai. `pip install pyrogram tgcrypto` karein."
        return

    # Python 3.12+ new threads don't have an event loop — create one (Pyrogram's sync layer needs it)
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    api_id = int(os.environ.get("TELEGRAM_API_ID", "0"))
    api_hash = os.environ.get("TELEGRAM_API_HASH", "")
    session = os.environ.get("TELEGRAM_SESSION_STRING", "")
    bot_username = os.environ.get("BOT_USERNAME", "allsaverbot")

    if not api_id or not api_hash or not session:
        with jobs_lock:
            jobs[job_id]["status"] = "error"
            jobs[job_id]["error"] = (
                "Telegram credentials missing. TELEGRAM_API_ID, TELEGRAM_API_HASH, "
                "TELEGRAM_SESSION_STRING set karein."
            )
        return

    try:
        with jobs_lock:
            jobs[job_id]["status"] = "contacting_bot"
            jobs[job_id]["bot"] = "@" + bot_username.lstrip("@")

        user_client = Client(
            f"job_{job_id}",
            api_id=api_id,
            api_hash=api_hash,
            session_string=session,
            in_memory=True,
            no_updates=True,
        )

        async def _pick_best_quality_keyboard(msg):
            """@allsaverbot-style inline keyboard me se best video quality button choose kare."""
            rm = msg.reply_markup
            if not rm or not getattr(rm, "inline_keyboard", None):
                return None
            all_btns = []
            for row in rm.inline_keyboard:
                for b in row:
                    t = (b.text or "").lower()
                    cb = b.callback_data
                    if not cb:
                        continue
                    # Prefer full video (not audio), highest resolution
                    pref = -1
                    if "1080" in t: pref = 50
                    elif "720" in t: pref = 40
                    elif "480" in t: pref = 30
                    elif "360" in t: pref = 20
                    elif "240" in t: pref = 10
                    elif "mp3" in t or "audio" in t: pref = 5
                    if pref > 0:
                        all_btns.append((pref, b))
            if not all_btns:
                return None
            all_btns.sort(key=lambda x: -x[0])
            return all_btns[0][1]

        async def _run():
            async with user_client:
                # Pehle bot se /start bhej dijiye taaki bot active rahe
                try:
                    await user_client.send_message(bot_username, "/start")
                    await asyncio.sleep(1.5)
                except Exception:
                    pass

                # Send the actual URL
                await user_client.send_message(bot_username, url)
                sent_time = time.time()

                # Wait up to 60s for an inline-keyboard (quality picker) message
                deadline = time.time() + 90
                quality_msg = None
                chosen_btn = None
                while time.time() < deadline:
                    await asyncio.sleep(1.5)
                    async for msg in user_client.get_chat_history(bot_username, limit=8):
                        if msg.outgoing:
                            continue
                        if msg.date and msg.date.timestamp() < sent_time - 2:
                            continue
                        btn = await _pick_best_quality_keyboard(msg)
                        if btn:
                            quality_msg = msg
                            chosen_btn = btn
                            break
                    if chosen_btn:
                        break

                if chosen_btn:
                    with jobs_lock:
                        jobs[job_id]["status"] = "selecting_quality"
                        jobs[job_id]["title"] = quality_msg.caption or "Video"
                    # Press the quality button
                    try:
                        await user_client.request_callback_answer(
                            chat_id=quality_msg.chat.id,
                            message_id=quality_msg.id,
                            callback_data=chosen_btn.callback_data,
                            timeout=30,
                        )
                    except Exception:
                        pass
                    # Now wait for the actual file (document/video/audio)
                    deadline = time.time() + 180
                else:
                    # Fall back: agar keyboard nahi mila (e.g. Instagram/TikTok direct file bhejta ho)
                    deadline = time.time() + 60

                last_seen_id = quality_msg.id if quality_msg else 0
                while time.time() < deadline:
                    await asyncio.sleep(2)
                    async for msg in user_client.get_chat_history(bot_username, limit=10):
                        if msg.outgoing:
                            continue
                        if msg.date and msg.date.timestamp() < sent_time - 2:
                            continue
                        # Skip the quality-picker photo itself
                        if quality_msg and msg.id == quality_msg.id:
                            continue
                        media_obj = None
                        ext_hint = "mp4"
                        if msg.video:
                            media_obj = msg.video
                            fname = msg.video.file_name or f"{job_id}.mp4"
                        elif msg.document:
                            media_obj = msg.document
                            fname = msg.document.file_name or f"{job_id}.bin"
                            # Ignore small / non-media documents like images
                            if msg.document.mime_type and msg.document.mime_type.startswith("image/"):
                                continue
                        elif msg.audio:
                            media_obj = msg.audio
                            fname = msg.audio.file_name or f"{job_id}.mp3"
                        elif msg.voice or msg.video_note:
                            # skip these
                            continue
                        elif msg.photo:
                            # photos ignore karo (thumbnail hote hain)
                            continue
                        else:
                            # Text message? check for error
                            if msg.text and msg.date and msg.date.timestamp() > sent_time - 2:
                                tl = msg.text.lower()
                                if any(k in tl for k in ("error", "failed", "not found", "invalid", "nahi mila", "asamarth", "limit", "blocked", "try later")):
                                    raise RuntimeError(f"Bot: {msg.text[:200]}")
                            continue

                        if not media_obj:
                            continue

                        # Found our media — download it
                        with jobs_lock:
                            jobs[job_id]["status"] = "downloading_from_bot"
                            jobs[job_id]["title"] = (msg.caption or quality_msg.caption if quality_msg else "") or "Video"

                        safe = sanitize_filename(fname)
                        out_path = DOWNLOAD_DIR / f"{job_id}_{safe}"
                        downloaded = await user_client.download_media(msg, file_name=str(out_path))
                        return Path(downloaded).name, (msg.caption or quality_msg.caption if quality_msg else "") or "Video"

                    with jobs_lock:
                        jobs[job_id]["wait_sec"] = int(time.time() - sent_time)

                raise TimeoutError("Bot ne 3 minute me video nahi bheji.")

        filename, title = asyncio.run(_run())

        with jobs_lock:
            jobs[job_id]["status"] = "done"
            jobs[job_id]["title"] = title
            jobs[job_id]["filename"] = filename
            jobs[job_id]["filesize"] = (DOWNLOAD_DIR / filename).stat().st_size

    except Exception as e:
        with jobs_lock:
            jobs[job_id]["status"] = "error"
            jobs[job_id]["error"] = f"Telegram bot: {e}"[:300]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/")
def home():
    return render_template("index.html", backend=BACKEND)


@app.route("/api/fetch", methods=["POST"])
def api_fetch():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url or not URL_REGEX.match(url):
        return jsonify({"ok": False, "error": "Kripya ek valid URL daalein."}), 400

    if BACKEND == "ytdlp":
        try:
            info = fetch_info_ytdlp(url)
            return jsonify({"ok": True, "info": info})
        except Exception as e:
            return jsonify({"ok": False, "error": f"Fetch nahi ho paya: {e}"}), 400
    else:
        # Telegram backend skips format selection — goes straight to queue
        job_id = uuid.uuid4().hex[:12]
        with jobs_lock:
            jobs[job_id] = {"status": "queued", "url": url, "created": time.time()}
        threading.Thread(target=download_via_telegram_bot, args=(job_id, url), daemon=True).start()
        return jsonify({"ok": True, "job_id": job_id})


@app.route("/api/download", methods=["POST"])
def api_download():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    format_id = (data.get("format_id") or None)
    if not url:
        return jsonify({"ok": False, "error": "URL chahiye."}), 400

    job_id = uuid.uuid4().hex[:12]
    with jobs_lock:
        jobs[job_id] = {"status": "queued", "url": url, "created": time.time()}

    if BACKEND == "ytdlp":
        t = threading.Thread(target=download_ytdlp, args=(job_id, url, format_id), daemon=True)
    else:
        t = threading.Thread(target=download_via_telegram_bot, args=(job_id, url), daemon=True)
    t.start()

    return jsonify({"ok": True, "job_id": job_id})


@app.route("/api/status/<job_id>")
def api_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"ok": False, "error": "Job nahi mila."}), 404
    resp = dict(job)
    if job.get("filesize"):
        resp["filesize_human"] = human_size(job["filesize"])
    return jsonify({"ok": True, "job": resp})


@app.route("/files/<path:name>")
def serve_file(name):
    # Whitelist: only our job files
    if "/" in name or ".." in name:
        return ("Bad path", 400)
    return send_from_directory(DOWNLOAD_DIR, name, as_attachment=True)


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print(f"Starting server with backend: {BACKEND}")
    app.run(host="0.0.0.0", port=5000, debug=False)
