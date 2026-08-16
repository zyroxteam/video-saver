"""
All-in-One Video Downloader Website (VideoSaver)
=================================================
Supports YouTube, Instagram, Facebook, Twitter/X, Pinterest, TikTok, Reddit
and 1000+ sites via yt-dlp. When a platform blocks guest downloads
(e.g. Instagram, X without cookies) the backend automatically falls back to
the Telegram bot @allsaverbot (if TELEGRAM_SESSION_STRING is configured).

Backends:
  - ytdlp     : direct (fast, default)
  - telegram  : always route through the bot
  - auto      : try yt-dlp, fall back to bot on failure (default if .env exists)
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
# If .env has telegram credentials and backend not explicitly set, use "auto"
if BACKEND == "ytdlp" and os.environ.get("TELEGRAM_SESSION_STRING") and HAS_PYROGRAM:
    BACKEND = "auto"

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024  # 500 MB

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
    "extractor_args": {"youtube": {"player_client": ["android", "web"]}},
}

def _ytdlp_download(url: str, outtmpl: str, format_id=None, fetch_only=False):
    opts = dict(YTDL_COMMON)
    opts["outtmpl"] = outtmpl
    if fetch_only:
        opts["skip_download"] = True
        opts["socket_timeout"] = 25
    if format_id:
        opts["format"] = format_id
    else:
        opts["format"] = "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b"
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
    try:
        outtmpl = str(DOWNLOAD_DIR / f"{job_id}.%(ext)s")
        info, fn = _ytdlp_download(url, outtmpl, format_id=format_id)
        with jobs_lock:
            jobs[job_id].update(status="done", title=info.get("title","Video"),
                                filename=fn.name, filesize=fn.stat().st_size)
    except Exception as e:
        with jobs_lock:
            jobs[job_id]["error"] = str(e)[:300]
        raise

# ------------- Telegram bot backend (@allsaverbot) -------------
def download_via_telegram(job_id, url):
    if not HAS_PYROGRAM:
        raise RuntimeError("Pyrogram/tgcrypto install nahi hai (`pip install pyrogram tgcrypto`)")
    make_event_loop()

    api_id = int(os.environ.get("TELEGRAM_API_ID","0"))
    api_hash = os.environ.get("TELEGRAM_API_HASH","")
    session = os.environ.get("TELEGRAM_SESSION_STRING","")
    bot = os.environ.get("BOT_USERNAME","allsaverbot")
    if not api_id or not api_hash or not session:
        raise RuntimeError("Telegram credentials missing.")

    with jobs_lock:
        jobs[job_id]["status"] = "contacting_bot"
        jobs[job_id]["backend"] = "telegram @"+bot
        jobs[job_id]["bot"] = "@"+bot.lstrip("@")

    async def pick_btn(msg):
        rm = msg.reply_markup
        if not rm or not getattr(rm,"inline_keyboard",None):
            return None
        btns = []
        for row in rm.inline_keyboard:
            for b in row:
                t = (b.text or "").lower()
                if not b.callback_data: continue
                pref = -1
                if "1080" in t: pref=50
                elif "720" in t: pref=40
                elif "480" in t: pref=30
                elif "360" in t: pref=20
                elif "240" in t: pref=10
                elif "mp3" in t or "audio" in t: pref=5
                if pref>0: btns.append((pref,b))
        if not btns: return None
        btns.sort(key=lambda x:-x[0])
        return btns[0][1]

    async def _run():
        c = PyroClient(f"job_{job_id}", api_id=api_id, api_hash=api_hash,
                       session_string=session, in_memory=True, no_updates=True)
        async with c:
            try:
                await c.send_message(bot, "/start")
                await asyncio.sleep(1.5)
            except Exception: pass
            await c.send_message(bot, url)
            t0 = time.time()
            deadline = t0 + 90
            qmsg, qbtn = None, None
            while time.time() < deadline:
                await asyncio.sleep(1.2)
                async for m in c.get_chat_history(bot, limit=8):
                    if m.outgoing: continue
                    if m.date and m.date.timestamp() < t0-2: continue
                    b = await pick_btn(m)
                    if b:
                        qmsg, qbtn = m, b; break
                if qbtn: break
            if qbtn:
                with jobs_lock:
                    jobs[job_id]["status"] = "selecting_quality"
                    jobs[job_id]["title"] = qmsg.caption or "Video"
                try:
                    await c.request_callback_answer(chat_id=qmsg.chat.id,
                                                    message_id=qmsg.id,
                                                    callback_data=qbtn.callback_data,
                                                    timeout=30)
                except Exception: pass
                deadline = time.time() + 180
            else:
                deadline = time.time() + 60

            while time.time() < deadline:
                await asyncio.sleep(2)
                async for m in c.get_chat_history(bot, limit=10):
                    if m.outgoing: continue
                    if m.date and m.date.timestamp() < t0-2: continue
                    if qmsg and m.id == qmsg.id: continue
                    fname = None; media = None
                    if m.video:
                        media = m.video; fname = m.video.file_name or f"{job_id}.mp4"
                    elif m.document:
                        if m.document.mime_type and m.document.mime_type.startswith("image/"):
                            continue
                        media = m.document; fname = m.document.file_name or f"{job_id}.bin"
                    elif m.audio:
                        media = m.audio; fname = m.audio.file_name or f"{job_id}.mp3"
                    elif m.photo or m.voice or m.video_note or m.animation:
                        continue
                    else:
                        if m.text and m.date and m.date.timestamp() > t0-2:
                            tl = m.text.lower()
                            if any(k in tl for k in ("error","failed","not found","invalid",
                                                     "nahi mila","limit","blocked","try later")):
                                raise RuntimeError(f"Bot: {m.text[:200]}")
                        continue
                    with jobs_lock:
                        jobs[job_id]["status"] = "downloading_from_bot"
                        jobs[job_id]["title"] = (m.caption or (qmsg.caption if qmsg else "") or "Video")
                    out = DOWNLOAD_DIR / f"{job_id}_{sanitize(fname)}"
                    dl = await c.download_media(m, file_name=str(out))
                    return Path(dl).name, (m.caption or (qmsg.caption if qmsg else "") or "Video")
                with jobs_lock:
                    jobs[job_id]["wait_sec"] = int(time.time()-t0)
            raise TimeoutError("Bot ne 3 minute me video nahi bheji.")

    fn, title = asyncio.run(_run())
    with jobs_lock:
        jobs[job_id].update(status="done", title=title, filename=fn,
                            filesize=(DOWNLOAD_DIR/fn).stat().st_size)

# ------------- Smart wrapper: yt-dlp → telegram fallback -------------
def download_auto(job_id, url, format_id=None):
    """Try yt-dlp; if it fails AND telegram creds exist, fall back to @allsaverbot."""
    tele_ok = bool(HAS_PYROGRAM and os.environ.get("TELEGRAM_SESSION_STRING")
                   and os.environ.get("TELEGRAM_API_ID"))
    # Try yt-dlp first
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
            download_via_telegram(job_id, url)
        except Exception as e2:
            with jobs_lock:
                jobs[job_id]["status"] = "error"
                jobs[job_id]["error"] = f"yt-dlp: {err1} | Bot: {str(e2)[:200]}"

# ------------- Routes -------------
@app.route("/")
def home():
    return render_template("index.html", backend=BACKEND)

@app.route("/api/fetch", methods=["POST"])
def api_fetch():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url or not URL_REGEX.match(url):
        return jsonify({"ok": False, "error": "Kripya ek valid URL daalein."}), 400

    # In ytdlp/auto mode, try to fetch formats; on failure, proceed directly to bot
    if BACKEND in ("ytdlp","auto"):
        try:
            info = fetch_info_ytdlp(url)
            return jsonify({"ok": True, "info": info})
        except Exception as e:
            if BACKEND == "auto" and HAS_PYROGRAM and os.environ.get("TELEGRAM_SESSION_STRING"):
                # skip format selection, go straight to bot download
                job_id = uuid.uuid4().hex[:12]
                with jobs_lock:
                    jobs[job_id] = {"status":"queued","url":url,"created":time.time()}
                threading.Thread(target=download_via_telegram, args=(job_id,url), daemon=True).start()
                return jsonify({"ok": True, "job_id": job_id, "direct_to_bot": True})
            return jsonify({"ok": False, "error": f"Fetch nahi ho paya: {e}"}), 400
    else:
        job_id = uuid.uuid4().hex[:12]
        with jobs_lock:
            jobs[job_id] = {"status":"queued","url":url,"created":time.time()}
        threading.Thread(target=download_via_telegram, args=(job_id,url), daemon=True).start()
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
        t = threading.Thread(target=download_via_telegram, args=(job_id,url), daemon=True)
    else:  # auto
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

# ------------- start -------------
if __name__ == "__main__":
    print(f"🎬 VideoSaver starting — backend: {BACKEND}")
    print(f"   Telegram fallback: {'ON' if HAS_PYROGRAM and os.environ.get('TELEGRAM_SESSION_STRING') else 'OFF'}")
    app.run(host="0.0.0.0", port=5000, debug=False)
