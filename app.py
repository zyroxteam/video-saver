"""
All-in-One Video Downloader Website (VideoSaver)
=================================================
Supports YouTube, Instagram, Facebook, Twitter/X, Pinterest, TikTok, Reddit
and 1000+ sites via yt-dlp. Falls back through multiple Telegram bots if
direct yt-dlp access is blocked (common on cloud/datacenter IPs).

Backends (auto mode):
  yt-dlp (direct)  →  @YTfinderbot  →  @allsaverbot
"""
import os, re, uuid, time, threading, urllib.parse, asyncio
from pathlib import Path

# ------------- auto-load .env -------------
ENV_PATH = Path(__file__).resolve().parent / ".env"
if ENV_PATH.exists():
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

from flask import Flask, render_template, request, jsonify, send_from_directory
try:
    import yt_dlp
    HAS_YTDLP = True
except ImportError:
    HAS_YTDLP = False

try:
    from pyrogram import Client as PyroClient
    HAS_PYROGRAM = True
except ImportError:
    PyroClient = None  # type: ignore
    HAS_PYROGRAM = False

# ------------- Config -------------
APP_ROOT = Path(__file__).resolve().parent
DOWNLOAD_DIR = APP_ROOT / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)

BACKEND = os.environ.get("DOWNLOAD_BACKEND", "ytdlp").lower()
if BACKEND == "ytdlp" and os.environ.get("TELEGRAM_SESSION_STRING") and HAS_PYROGRAM:
    BACKEND = "auto"

# Comma-separated bot list. @YTfinderbot first (fastest for YT/IG/Pin), @allsaverbot second (all platforms).
BOTS_RAW = os.environ.get("BOT_USERNAME", "YTfinderbot,allsaverbot")
BOT_LIST = [b.strip().lstrip("@") for b in BOTS_RAW.split(",") if b.strip()]
PRIMARY_BOT = BOT_LIST[0] if BOT_LIST else "allsaverbot"

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024

jobs = {}
jobs_lock = threading.Lock()

URL_REGEX = re.compile(
    r"https?://(?:www\.)?[-\w@:%._+~#=]{1,256}\.[a-zA-Z0-9()]{1,6}\b[-\w@:%_+.~#?&/=]*",
    re.IGNORECASE,
)

# ------------- helpers -------------
def sanitize(name: str) -> str:
    return re.sub(r"[\\/:*?\"<>|]", "_", name).strip(". ")[:120] or "video"

def detect_platform(url: str) -> str:
    h = urllib.parse.urlparse(url).netloc.lower()
    if "youtu" in h: return "YouTube"
    if "instagram" in h: return "Instagram"
    if "facebook" in h or "fb.watch" in h or "fb.com" in h: return "Facebook"
    if "tiktok" in h: return "TikTok"
    if "twitter" in h or "x.com" in h: return "Twitter/X"
    if "reddit" in h or "redd.it" in h: return "Reddit"
    if "pinterest" in h or "pin.it" in h: return "Pinterest"
    if "likee" in h: return "Likee"
    return h

def human_size(n):
    for u in ("B","KB","MB","GB"):
        if n < 1024: return f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} TB"

def make_event_loop():
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

# ------------- yt-dlp core -------------
YTDL_COMMON = {
    "quiet": True, "no_warnings": True, "noplaylist": True,
    "geo_bypass": True, "socket_timeout": 60,
    "retries": 5, "fragment_retries": 5,
    "concurrent_fragment_downloads": 4,
    "nocheckcertificate": True, "prefer_ffmpeg": True,
    "merge_output_format": "mp4",
    "extractor_args": {
        "youtube": {"player_client": ["mweb", "android", "ios"]},
    },
    "extractor_retries": 3,
    "file_access_retries": 3,
}

def _ytdlp_download(url, outtmpl, format_id=None, fetch_only=False):
    opts = dict(YTDL_COMMON)
    opts["outtmpl"] = outtmpl
    for fp in ("/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg"):
        if Path(fp).exists():
            opts["ffmpeg_location"] = fp; break
    if fetch_only:
        opts["skip_download"] = True
        opts["socket_timeout"] = 25
    if format_id and format_id not in ("1","best","0","mp4",""):
        opts["format"] = f"({format_id})/18/bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b/best"
    else:
        opts["format"] = "18/bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b/best"
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=not fetch_only)
        if info.get("_type") == "playlist":
            info = info["entries"][0]
        if fetch_only:
            return info
        fn = Path(ydl.prepare_filename(info))
        if not fn.exists():
            for ext in ("mp4","mkv","webm","m4a","mp3","mov"):
                cand = fn.with_suffix(f".{ext}")
                if cand.exists():
                    fn = cand; break
        return info, fn

def fetch_info_ytdlp(url):
    if not HAS_YTDLP:
        raise RuntimeError("yt-dlp is not installed.")
    info = _ytdlp_download(url, "", fetch_only=True)
    formats = []
    for f in info.get("formats", []):
        if f.get("vcodec") == "none" and f.get("acodec") == "none": continue
        if f.get("ext") not in ("mp4","webm","m4v","mov","mkv","mp3","m4a"): continue
        sz = f.get("filesize") or f.get("filesize_approx") or 0
        formats.append({
            "format_id": f["format_id"],
            "ext": f.get("ext","mp4"),
            "resolution": f.get("resolution") or f.get("quality") or "best",
            "has_video": f.get("vcodec") != "none",
            "has_audio": f.get("acodec") != "none",
            "filesize": sz,
            "filesize_human": human_size(sz) if sz else "—",
        })
    seen = set(); uniq = []
    for f in sorted(formats, key=lambda x:(x["has_video"], x["filesize"] or 0), reverse=True):
        k = (f["resolution"], f["ext"])
        if k in seen: continue
        seen.add(k); uniq.append(f)
        if len(uniq) >= 8: break
    return {
        "title": info.get("title","Video"),
        "thumbnail": info.get("thumbnail") or "",
        "duration": info.get("duration") or 0,
        "uploader": info.get("uploader") or info.get("channel") or "",
        "platform": detect_platform(url),
        "formats": uniq,
    }

def download_ytdlp(job_id, url, format_id):
    with jobs_lock:
        jobs[job_id]["status"] = "downloading"
        jobs[job_id]["backend"] = "yt-dlp"
    outtmpl = str(DOWNLOAD_DIR / f"{job_id}.%(ext)s")
    info, fn = _ytdlp_download(url, outtmpl, format_id=format_id)
    with jobs_lock:
        jobs[job_id].update(status="done", title=info.get("title","Video"),
                            filename=fn.name, filesize=fn.stat().st_size)

# ------------- Generic Telegram-bot engine (tries multiple bots) -------------
# Per-bot button keyword preferences — what caption/text on a button means
# "give me the video file" in that bot's UX.
BOT_VIDEO_KEYWORDS = {
    "ytfinderbot": ["video", "🎞", "mp4", "hd", "download", "get"],
    "allsaverbot": ["1080", "720", "480", "360", "240"],
}
# Phrases that mean "the bot failed"
ERROR_KEYWORDS = ("error","failed","not found","invalid","limit","blocked",
                  "try later","can't","sorry","unfortunately","nahi mila",
                  "music was not found")

def _pick_button(bot_name, msg, quality_pref=None):
    """Pick the best quality/video button from a bot message. Returns button or None."""
    rm = msg.reply_markup
    if not rm or not getattr(rm,"inline_keyboard",None):
        return None
    btns = [b for row in rm.inline_keyboard for b in row if b.callback_data]
    if not btns:
        return None
    kws = BOT_VIDEO_KEYWORDS.get(bot_name.lower(), ["video","mp4","hd","1080","720","480","360","download","🎞"])

    # First pass: look for explicit quality preference (e.g. 720/1080)
    if quality_pref:
        for b in btns:
            if quality_pref.lower() in (b.text or "").lower():
                return b

    # Second pass: highest-quality video button among video-type keywords
    quality_order = ["2160","1440","1080","hd","720","480","360","240","mp4","video","🎞","download"]
    best = None; best_score = -1
    for b in btns:
        t = (b.text or "").lower()
        for i, k in enumerate(quality_order):
            if k in t:
                # Earlier in list = higher priority
                score = len(quality_order) - i
                if score > best_score:
                    best_score = score; best = b
                break
    if best:
        return best

    # Last resort: any button that isn't audio/mp3
    for b in btns:
        t = (b.text or "").lower()
        if "audio" in t or "mp3" in t or "🎧" in t or "add to group" in t:
            continue
        return b
    return None

async def _bot_download_one(bot_name, url, job_id, api_id, api_hash, session):
    """Try one bot. Returns (filename, title) on success, raises on failure."""
    bot = "@" + bot_name
    c = PyroClient(f"jb_{job_id}_{bot_name}_{uuid.uuid4().hex[:6]}",
                   api_id=api_id, api_hash=api_hash,
                   session_string=session, in_memory=True, no_updates=True)
    async with c:
        # Drain — cancel any prior state
        try:
            await c.send_message(bot, "/cancel"); await asyncio.sleep(0.8)
        except Exception: pass
        try:
            async for _ in c.get_chat_history(bot, limit=15):
                pass
        except Exception: pass
        await asyncio.sleep(0.5)

        await c.send_message(bot, url)
        t0 = time.time()
        deadline = t0 + 90
        clicked = False
        quality_msg_id = None
        received_video = False
        title = "Video"
        wait_after_click = 0

        while time.time() < deadline:
            await asyncio.sleep(1.5)
            async for m in c.get_chat_history(bot, limit=12):
                if m.outgoing: continue
                if m.date and m.date.timestamp() < t0-1: continue
                if m.id == quality_msg_id: continue

                # — Got media?
                if m.video:
                    with jobs_lock:
                        jobs[job_id]["status"] = "downloading_from_bot"
                        jobs[job_id]["wait_sec"] = int(time.time()-t0)
                        if m.caption: title = m.caption[:120]
                    out = DOWNLOAD_DIR / f"{job_id}_{bot_name}_{sanitize(m.video.file_name or f'video.mp4')}"
                    dl = await c.download_media(m, file_name=str(out))
                    return Path(dl).name, title

                if m.document and m.document.mime_type and "video" in m.document.mime_type:
                    with jobs_lock:
                        jobs[job_id]["status"] = "downloading_from_bot"
                        if m.caption: title = m.caption[:120]
                    out = DOWNLOAD_DIR / f"{job_id}_{bot_name}_{sanitize(m.document.file_name or f'video.bin')}"
                    dl = await c.download_media(m, file_name=str(out))
                    return Path(dl).name, title

                if m.audio:
                    # Some bots return audio for music links; skip for video flow
                    continue
                if m.photo or m.voice or m.video_note or m.animation:
                    # Photo with caption is often the thumbnail/format menu
                    pass

                # — Error text?
                if m.text:
                    tl = m.text.lower()
                    if any(k in tl for k in ERROR_KEYWORDS):
                        if "add to group" in tl:
                            # Bot telling us to add to group = unsupported platform
                            raise RuntimeError(f"@{bot_name} doesn't support this link")
                        raise RuntimeError(f"@{bot_name}: {m.text[:150]}")

                # — Buttons? Click a video/quality button once
                if not clicked and m.reply_markup and getattr(m.reply_markup, "inline_keyboard", None):
                    btn = _pick_button(bot_name, m)
                    if btn:
                        with jobs_lock:
                            jobs[job_id]["status"] = "selecting_quality"
                            if m.caption: title = m.caption[:120]
                        try:
                            await c.request_callback_answer(m.chat.id, m.id, btn.callback_data, timeout=30)
                        except Exception:
                            pass
                        clicked = True
                        quality_msg_id = m.id
                        deadline = time.time() + 180  # give more time after click
                        wait_after_click = time.time()
            # status updates while waiting
            with jobs_lock:
                elapsed = int(time.time()-t0)
                jobs[job_id]["wait_sec"] = elapsed
                if clicked:
                    jobs[job_id]["status"] = "downloading_from_bot"
                else:
                    jobs[job_id]["status"] = "contacting_bot"
        raise TimeoutError(f"@{bot_name} ne time me video nahi bheji")

def download_via_bots(job_id, url):
    """Try each configured Telegram bot in order; first success wins."""
    if not HAS_PYROGRAM:
        raise RuntimeError("Pyrogram/tgcrypto install nahi hai")
    make_event_loop()
    api_id = int(os.environ.get("TELEGRAM_API_ID","0"))
    api_hash = os.environ.get("TELEGRAM_API_HASH","")
    session = os.environ.get("TELEGRAM_SESSION_STRING","")
    if not api_id or not api_hash or not session:
        raise RuntimeError("Telegram credentials missing.")

    errors = []
    for bot_name in BOT_LIST:
        with jobs_lock:
            jobs[job_id]["backend"] = f"telegram @{bot_name}"
            jobs[job_id]["bot"] = "@" + bot_name
        try:
            fn, title = asyncio.run(_bot_download_one(bot_name, url, job_id, api_id, api_hash, session))
            with jobs_lock:
                jobs[job_id].update(status="done", title=title, filename=fn,
                                    filesize=(DOWNLOAD_DIR/fn).stat().st_size)
            return
        except Exception as e:
            msg = f"@{bot_name}: {str(e)[:150]}"
            errors.append(msg)
            print(f"[job {job_id}] bot {bot_name} failed: {e}")
    # All bots failed
    raise RuntimeError(" | ".join(errors))

# Backwards compat alias (used in older routes/frontend)
def download_via_telegram(job_id, url):
    return download_via_bots(job_id, url)

# ------------- Smart wrapper: yt-dlp → Telegram bots -------------
def download_auto(job_id, url, format_id=None):
    """Try yt-dlp first; on failure fall back through configured Telegram bots."""
    tele_ok = bool(HAS_PYROGRAM and os.environ.get("TELEGRAM_SESSION_STRING")
                   and os.environ.get("TELEGRAM_API_ID"))
    try:
        download_ytdlp(job_id, url, format_id)
        return
    except Exception as e1:
        err1 = jobs[job_id].get("error", str(e1)[:200])

    if not tele_ok:
        with jobs_lock:
            jobs[job_id]["status"] = "error"
            jobs[job_id]["error"] = err1
        return
    with jobs_lock:
        jobs[job_id]["status"] = "fallback_bot"
        jobs[job_id]["yt_error"] = err1
    try:
        download_via_bots(job_id, url)
    except Exception as e2:
        with jobs_lock:
            jobs[job_id]["status"] = "error"
            jobs[job_id]["error"] = f"yt-dlp: {err1} | {str(e2)[:300]}"

# ------------- Routes -------------
@app.route("/")
def home():
    return render_template("index.html", backend=BACKEND, bots=BOT_LIST)

@app.route("/api/fetch", methods=["POST"])
def api_fetch():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url or not URL_REGEX.match(url):
        return jsonify({"ok": False, "error": "Kripya ek valid URL daalein."}), 400
    if BACKEND in ("ytdlp","auto"):
        try:
            info = fetch_info_ytdlp(url)
            return jsonify({"ok": True, "info": info})
        except Exception as e:
            if BACKEND == "auto" and HAS_PYROGRAM and os.environ.get("TELEGRAM_SESSION_STRING"):
                job_id = uuid.uuid4().hex[:12]
                with jobs_lock:
                    jobs[job_id] = {"status":"queued","url":url,"created":time.time()}
                threading.Thread(target=download_via_bots, args=(job_id,url), daemon=True).start()
                return jsonify({"ok": True, "job_id": job_id, "direct_to_bot": True})
            return jsonify({"ok": False, "error": f"Fetch nahi ho paya: {e}"}), 400
    else:
        job_id = uuid.uuid4().hex[:12]
        with jobs_lock:
            jobs[job_id] = {"status":"queued","url":url,"created":time.time()}
        threading.Thread(target=download_via_bots, args=(job_id,url), daemon=True).start()
        return jsonify({"ok": True, "job_id": job_id})

@app.route("/api/download", methods=["POST"])
def api_download():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    format_id = data.get("format_id") or None
    if not url:
        return jsonify({"ok":False,"error":"URL chahiye."}),400
    job_id = uuid.uuid4().hex[:12]
    with jobs_lock:
        jobs[job_id] = {"status":"queued","url":url,"created":time.time()}
    if BACKEND == "ytdlp":
        t = threading.Thread(target=download_ytdlp, args=(job_id,url,format_id), daemon=True)
    elif BACKEND == "telegram":
        t = threading.Thread(target=download_via_bots, args=(job_id,url), daemon=True)
    else:
        t = threading.Thread(target=download_auto, args=(job_id,url,format_id), daemon=True)
    t.start()
    return jsonify({"ok": True, "job_id": job_id})

@app.route("/api/status/<job_id>")
def api_status(job_id):
    job = jobs.get(job_id)
    if not job: return jsonify({"ok":False,"error":"Job nahi mila."}),404
    resp = dict(job)
    if job.get("filesize"): resp["filesize_human"] = human_size(job["filesize"])
    return jsonify({"ok":True,"job":resp})

@app.route("/files/<path:name>")
def serve_file(name):
    if "/" in name or ".." in name: return ("Bad path",400)
    return send_from_directory(DOWNLOAD_DIR, name, as_attachment=True)

@app.route("/api/health")
def health():
    return jsonify({"ok": True, "backend": BACKEND, "bots": BOT_LIST})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    print(f"🎬 VideoSaver — backend={BACKEND}, bots={BOT_LIST}, port={port}")
    app.run(host="0.0.0.0", port=port, debug=debug)
