"""
All-in-One Video Downloader Website (VideoSaver)
=================================================
Supports 1000+ sites via Telegram bot chain (PRIMARY — works on every hosting).
yt-dlp is last-resort fallback for local/dev.

Bot chain:
  @allsaverbot  →  @YTfinderbot
(allsaverbot supports YouTube, Instagram, Facebook, Twitter/X, Pinterest, TikTok,
Likee, Snapchat, Dailymotion, Vimeo, Reddit and 40+ more sites.)
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
    from pyrogram import handlers, filters
    HAS_PYROGRAM = True
except ImportError:
    PyroClient = None  # type: ignore
    handlers = filters = None
    HAS_PYROGRAM = False

# ------------- Config -------------
APP_ROOT = Path(__file__).resolve().parent
# Use /tmp on server (writable, fast, ephemeral OK for short-lived downloads)
_default_dl = Path("/tmp/videosaver") if os.environ.get("PORT") else (APP_ROOT / "downloads")
DOWNLOAD_DIR = Path(os.environ.get("DOWNLOAD_DIR", str(_default_dl)))
try:
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
except Exception:
    DOWNLOAD_DIR = APP_ROOT / "downloads"
    DOWNLOAD_DIR.mkdir(exist_ok=True)

BACKEND = os.environ.get("DOWNLOAD_BACKEND", "auto").lower()

# Primary bot = @allsaverbot (all platforms), secondary = @YTfinderbot (fast YT/IG)
BOTS_RAW = os.environ.get("BOT_USERNAME", "allsaverbot,YTfinderbot")
BOT_LIST = [b.strip().lstrip("@") for b in BOTS_RAW.split(",") if b.strip()]

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
    if "snap" in h: return "Snapchat"
    if "dailymotion" in h or "dai.ly" in h: return "Dailymotion"
    if "vimeo" in h: return "Vimeo"
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

# ------------- Bot config -------------
# Per-bot button keyword preferences
BOT_VIDEO_KEYWORDS = {
    "allsaverbot": ["1080", "720", "480", "360", "240", "hd", "video", "mp4", "🎞", "download"],
    "ytfinderbot": ["video", "🎞", "mp4", "hd", "download", "get"],
}

# Phrases that indicate the bot rejected the URL (don't wait for video)
FATAL_ERROR_KW = [
    "not found", "doesn't support", "does not support", "unable to",
    "couldn", "could not", "unfortunately", "invalid link", "link is invalid",
    "add to group", "guruhga", "group ga", "music was not found",
    "try another link", "no video", "failed to", "cannot download",
    "can't download", "can't process", "can't find",
]

def _safe_getattr(obj, attr, default=None):
    try:
        return getattr(obj, attr, default)
    except Exception:
        return default

def _safe_text(m, attr="text"):
    """Safely get text/caption from a Pyrogram message (surrogate chars crash otherwise)."""
    try:
        v = getattr(m, attr, None) or ""
        return str(v)
    except Exception:
        return ""

def _has_attr(m, attr):
    try:
        v = getattr(m, attr, None)
        return bool(v)
    except Exception:
        return False

def _pick_button(bot_name, msg):
    """Pick the best video/quality button."""
    rm = _safe_getattr(msg, "reply_markup")
    if not rm:
        return None
    try:
        kb = getattr(rm, "inline_keyboard", None) or []
    except Exception:
        return None
    btns = [b for row in kb for b in row if _safe_getattr(b, "callback_data")]
    if not btns:
        return None
    kws = BOT_VIDEO_KEYWORDS.get(bot_name.lower(),
                                 ["1080","720","480","360","240","mp4","video","hd","🎞","download"])
    # Score each button: prefer higher quality
    quality_order = ["2160","1440","1080","hd","720","480","360","240","mp4","video","🎞","download"]
    best = None; best_score = -1
    for b in btns:
        t = (_safe_getattr(b, "text") or "").lower()
        if "audio" in t or "mp3" in t or "🎧" in t or "add to group" in t or "guruhga" in t:
            continue
        for i, k in enumerate(quality_order):
            if k in t:
                score = len(quality_order) - i
                if score > best_score:
                    best_score = score; best = b
                break
    if best:
        return best
    # Fallback: any non-audio button
    for b in btns:
        t = (_safe_getattr(b, "text") or "").lower()
        if "audio" in t or "mp3" in t or "🎧" in t:
            continue
        return b
    return None

# ------------- Telegram bot engine (event-driven for reliability) -------------
async def _bot_download_one(bot_name, url, job_id, api_id, api_hash, session_str):
    """Connect to one Telegram bot, send URL, auto-click button, download video.
    Uses Pyrogram's update handlers (not polling get_chat_history) — most reliable."""
    from pyrogram import enums

    bot_uname = bot_name.lower().lstrip("@")
    bot_chat = "@" + bot_uname

    client = PyroClient(
        f"jb_{job_id}_{bot_uname}_{uuid.uuid4().hex[:5]}",
        api_id=api_id, api_hash=api_hash,
        session_string=session_str, in_memory=True,
        workers=1,
    )

    video_future = asyncio.Future()     # resolved with the media Message
    error_future = asyncio.Future()     # resolved with error string
    clicked_flag = {"v": False}
    title_box = {"t": "Video"}
    sent_ts = [0.0]

    def _set_err(msg):
        if not error_future.done():
            error_future.set_result(msg)

    async def message_handler(c, m):
        if clicked_flag["_drained"] is False:
            return  # still draining, ignore
        # Only process msgs from the bot that arrived after we sent the URL
        try:
            chat_uname = ""
            if m.chat:
                chat_uname = (m.chat.username or "").lower()
            if chat_uname and chat_uname != bot_uname:
                return
            if getattr(m, "outgoing", False):
                return
            if m.date and sent_ts[0] and m.date.timestamp() < sent_ts[0] - 3:
                return
        except Exception:
            return

        # 1. Video?
        v = _safe_getattr(m, "video")
        d = _safe_getattr(m, "document")
        d_mime = (_safe_getattr(d, "mime_type") or "") if d else ""
        if v or (d and d_mime.startswith("video/")):
            if not video_future.done():
                cap = _safe_text(m, "caption")
                if cap: title_box["t"] = cap[:120]
                video_future.set_result(m)
            return

        # 2. Skip audio
        if _has_attr(m, "audio"):
            return

        # 3. Photo/animation thumbnail — extract caption
        if _has_attr(m, "photo") or _has_attr(m, "animation") or _has_attr(m, "video_note"):
            cap = _safe_text(m, "caption")
            if cap and (not title_box["t"] or title_box["t"] == "Video"):
                title_box["t"] = cap[:120]
            # fall through to button/error check (photo msgs often have buttons attached)

        # 4. Text error detection
        mtxt = _safe_text(m, "text") or _safe_text(m, "caption")
        if mtxt:
            tl = mtxt.lower()
            # Skip /cancel echo / search results
            if mtxt.strip().startswith("🔍") or mtxt.strip() in ("/cancel",):
                pass
            elif not clicked_flag["v"] and "\n" in mtxt and re.search(r'(^|\n)\s*\d+\.\s+\S+', mtxt):
                # Numbered list = search results; pick #1 or wait for buttons
                pass
            else:
                for kw in FATAL_ERROR_KW:
                    if kw in tl:
                        # "Failed" / "error" alone isn't enough; require a short message
                        if len(mtxt) < 250 or "not support" in tl or "guruhga" in tl:
                            _set_err(f"@{bot_uname} ye URL support nahi karta")
                            return

        # 5. Auto-click a button (only once, before video arrives)
        if not video_future.done() and not error_future.done():
            rm = _safe_getattr(m, "reply_markup")
            btn = _pick_button(bot_uname, m) if rm else None
            if btn and not clicked_flag["v"]:
                clicked_flag["v"] = True
                cap = _safe_text(m, "caption")
                if cap and (not title_box["t"] or title_box["t"] == "Video"):
                    title_box["t"] = cap[:120]
                with jobs_lock:
                    jobs[job_id]["status"] = "selecting_quality"
                try:
                    await c.request_callback_answer(m.chat.id, m.id, btn.callback_data, timeout=20)
                except Exception:
                    pass

    clicked_flag["_drained"] = False

    async with client:
        client.add_handler(handlers.MessageHandler(message_handler, filters.chat(bot_chat)))

        # ----- DRAIN: reset prior state -----
        try:
            await client.send_message(bot_chat, "/cancel")
        except Exception:
            pass
        # Wait for cancel to take effect, then eat any leftover messages
        await asyncio.sleep(1.5)
        try:
            async for _ in client.get_chat_history(bot_chat, limit=20):
                pass
        except Exception:
            pass
        await asyncio.sleep(0.8)
        clicked_flag["_drained"] = True

        # ----- SEND URL -----
        await client.send_message(bot_chat, url)
        sent_ts[0] = time.time()
        t0 = sent_ts[0]

        with jobs_lock:
            jobs[job_id]["status"] = "contacting_bot"

        # ----- WAIT LOOP -----
        # Total budget: 25s for the bot to acknowledge & (if needed) show buttons
        # + 90s after click for video to arrive
        total_deadline = t0 + 150
        got_click_extend = False

        while time.time() < total_deadline:
            # Determine wait window
            if not clicked_flag["v"]:
                wait_window = 30  # 30s to get a button or direct video
                remaining = min(wait_window, total_deadline - time.time())
            else:
                if not got_click_extend:
                    total_deadline = time.time() + 90  # 90s after click
                    got_click_extend = True
                remaining = total_deadline - time.time()
            if remaining <= 0:
                break
            # Wait for either future
            done, _ = await asyncio.wait(
                [video_future, error_future],
                timeout=min(2.0, remaining),
                return_when=asyncio.FIRST_COMPLETED,
            )
            # Heartbeat status
            with jobs_lock:
                jobs[job_id]["wait_sec"] = int(time.time() - t0)
                if video_future.done():
                    jobs[job_id]["status"] = "downloading_from_bot"
                elif clicked_flag["v"]:
                    jobs[job_id]["status"] = "downloading_from_bot"
                else:
                    jobs[job_id]["status"] = "contacting_bot"

            if error_future.done():
                raise RuntimeError(error_future.result())
            if video_future.done():
                break

        if not video_future.done():
            raise RuntimeError(f"@{bot_uname} ne time me video nahi bheji (timeout)")

        # ----- DOWNLOAD THE MEDIA FILE -----
        media_msg = video_future.result()
        v = _safe_getattr(media_msg, "video")
        d = _safe_getattr(media_msg, "document")
        media = v if v else d
        vname = "video.mp4"
        fsize = 0
        try:
            cand = _safe_getattr(media, "file_name")
            if cand: vname = cand
            fsize = _safe_getattr(media, "file_size") or 0
        except Exception:
            pass
        # Sanitize filename
        vname = sanitize(vname) or "video.mp4"
        out_path = DOWNLOAD_DIR / f"{job_id}_{bot_uname}_{vname}"
        print(f"[bot {bot_uname}] media received size={fsize} name={vname} -> {out_path}", flush=True)
        last_chunk_t = [time.time()]
        def _progress(current, total):
            last_chunk_t[0] = time.time()
        with jobs_lock:
            jobs[job_id]["status"] = "downloading_from_bot"
            if fsize:
                jobs[job_id]["filesize"] = fsize
        try:
            dl = await asyncio.wait_for(
                client.download_media(media_msg, file_name=str(out_path), progress=_progress),
                timeout=180,
            )
            if time.time() - last_chunk_t[0] > 120:
                raise RuntimeError(f"@{bot_uname} se file download stalled")
        except asyncio.TimeoutError:
            # Check if partial file exists and is big enough
            if out_path.exists() and out_path.stat().st_size > 1024*100:
                dl = str(out_path)
            else:
                raise RuntimeError(f"@{bot_uname} se file download time-out")
        except Exception as e:
            raise RuntimeError(f"@{bot_uname} download error: {e}")
        if not dl or not Path(dl).exists():
            # Try finding file by glob (Pyrogram sometimes changes extension)
            import glob
            cands = list(DOWNLOAD_DIR.glob(f"{job_id}_{bot_uname}_*"))
            cands = [p for p in cands if p.stat().st_size > 1024]
            if cands:
                cands.sort(key=lambda p: p.stat().st_mtime, reverse=True)
                dl = str(cands[0])
            else:
                raise RuntimeError(f"@{bot_uname} se download fail (file nahi mili)")
        fp = Path(dl)
        if fp.stat().st_size < 1024:
            raise RuntimeError(f"@{bot_uname} se download fail (khaali file)")
        print(f"[bot {bot_uname}] saved {fp.name} ({fp.stat().st_size} bytes)", flush=True)
        return fp.name, title_box["t"]


def download_via_bots(job_id, url):
    """Try each configured bot in order; first success wins."""
    if not HAS_PYROGRAM:
        raise RuntimeError("Pyrogram install nahi hai — pip install pyrogram tgcrypto")
    make_event_loop()
    api_id = int(os.environ.get("TELEGRAM_API_ID", "0"))
    api_hash = os.environ.get("TELEGRAM_API_HASH", "")
    session = os.environ.get("TELEGRAM_SESSION_STRING", "")
    if not api_id or not api_hash or not session:
        raise RuntimeError("Telegram credentials env me nahi hain.")

    errors = []
    for bot_name in BOT_LIST:
        with jobs_lock:
            jobs[job_id]["backend"] = f"telegram @{bot_name}"
            jobs[job_id]["bot"] = "@" + bot_name
        try:
            fn, title = asyncio.run(_bot_download_one(bot_name, url, job_id, api_id, api_hash, session))
            fp = DOWNLOAD_DIR / fn
            if fp.exists() and fp.stat().st_size > 1024:
                with jobs_lock:
                    jobs[job_id].update(
                        status="done", title=title, filename=fn,
                        filesize=fp.stat().st_size,
                    )
                print(f"[job {job_id}] ✅ @{bot_name} se download hua: {fn} ({human_size(fp.stat().st_size)})")
                return
        except Exception as e:
            msg = f"@{bot_name}: {str(e)[:150]}"
            errors.append(msg)
            print(f"[job {job_id}] ❌ bot @{bot_name} fail: {e}")
    raise RuntimeError(" | ".join(errors) or "Saare bots fail ho gaye")


def download_via_telegram(job_id, url):
    return download_via_bots(job_id, url)

# ------------- yt-dlp (fallback, for local/dev) -------------
YTDL_COMMON = {
    "quiet": True, "no_warnings": True, "noplaylist": True,
    "geo_bypass": True, "socket_timeout": 25,
    "retries": 2, "fragment_retries": 2,
    "concurrent_fragment_downloads": 4,
    "nocheckcertificate": True, "prefer_ffmpeg": True,
    "merge_output_format": "mp4",
    "extractor_args": {
        "youtube": {"player_client": ["mweb","android","ios"], "player_skip": ["js","webpage"]},
    },
    "skip_unavailable_fragments": True,
    "no_color": True,
}

def _ytdlp_run(url, outtmpl, format_id=None, fetch_only=False):
    opts = dict(YTDL_COMMON)
    opts["outtmpl"] = outtmpl
    for fp in ("/usr/bin/ffmpeg","/usr/local/bin/ffmpeg"):
        if Path(fp).exists():
            opts["ffmpeg_location"] = fp; break
    if fetch_only:
        opts["skip_download"] = True
    if format_id and format_id not in ("1","best","0","mp4",""):
        opts["format"] = f"({format_id})/18/bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/best"
    else:
        opts["format"] = "18/bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/best"
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
                if cand.exists(): fn = cand; break
        return info, fn

def download_ytdlp(job_id, url, format_id, time_budget=120):
    with jobs_lock:
        if jobs[job_id].get("status") in ("done","error"): return
        jobs[job_id]["status"] = "downloading"
        jobs[job_id]["backend"] = "yt-dlp"
    done_evt = threading.Event(); err_box = []
    def _run():
        try:
            outtmpl = str(DOWNLOAD_DIR / f"{job_id}.%(ext)s")
            info, fn = _ytdlp_run(url, outtmpl, format_id=format_id)
            with jobs_lock:
                if jobs[job_id].get("status") in ("done","error"): return
                jobs[job_id].update(status="done", title=info.get("title","Video"),
                                    filename=fn.name, filesize=fn.stat().st_size)
        except Exception as e:
            err_box.append(e)
        finally:
            done_evt.set()
    t = threading.Thread(target=_run, daemon=True); t.start()
    done_evt.wait(timeout=time_budget)
    if not done_evt.is_set():
        raise TimeoutError(f"yt-dlp {time_budget}s me complete nahi hua")
    if err_box:
        raise err_box[0]

# ------------- Smart wrapper: BOTS first → yt-dlp fallback -------------
def download_auto(job_id, url, format_id=None):
    """Bots first (works everywhere, fast). yt-dlp only when bots unavailable or fail."""
    tele_ok = bool(HAS_PYROGRAM and os.environ.get("TELEGRAM_SESSION_STRING")
                   and os.environ.get("TELEGRAM_API_ID") and BOT_LIST)
    if tele_ok:
        with jobs_lock:
            jobs[job_id]["status"] = "queued"
        try:
            download_via_bots(job_id, url)
            return
        except Exception as e1:
            err1 = str(e1)[:300]
    else:
        err1 = "Telegram configured nahi hai"

    # Bots fail gaye → yt-dlp last resort
    if HAS_YTDLP:
        try:
            download_ytdlp(job_id, url, format_id)
            return
        except Exception as e2:
            err_final = f"Telegram: {err1} | yt-dlp: {str(e2)[:200]}"
    else:
        err_final = err1
    with jobs_lock:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = err_final

# ------------- Routes -------------
@app.route("/")
def home():
    return render_template("index.html", backend=BACKEND, bots=BOT_LIST,
                           primary_bot=BOT_LIST[0] if BOT_LIST else "")

@app.route("/api/health")
def health():
    return jsonify({"ok": True, "backend": BACKEND, "bots": BOT_LIST,
                    "primary": BOT_LIST[0] if BOT_LIST else None})

@app.route("/api/fetch", methods=["POST"])
def api_fetch():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url or not URL_REGEX.match(url):
        return jsonify({"ok": False, "error": "Valid URL daalein."}), 400
    # In bot-first mode, skip fetch_info (it's slow / blocked on cloud) and go straight to job
    if BACKEND in ("telegram","auto") and BOT_LIST:
        job_id = uuid.uuid4().hex[:12]
        with jobs_lock:
            jobs[job_id] = {"status":"queued","url":url,"created":time.time(),"platform":detect_platform(url)}
        threading.Thread(target=download_auto, args=(job_id,url,None), daemon=True).start()
        return jsonify({"ok": True, "job_id": job_id, "direct": True,
                        "platform": detect_platform(url)})
    if HAS_YTDLP:
        try:
            # Best-effort info fetch (might fail on cloud)
            info = None
            def _run(box):
                try: box.append(_ytdlp_run(url, "", fetch_only=True))
                except Exception as e: box.append(e)
            box=[]
            th = threading.Thread(target=_run, args=(box,), daemon=True); th.start()
            th.join(timeout=20)
            if box and not isinstance(box[0], Exception):
                info = box[0]
            if info:
                fmts = []
                for f in info.get("formats",[]):
                    if f.get("vcodec")=="none" and f.get("acodec")=="none": continue
                    if f.get("ext") not in ("mp4","webm","m4v","mov","mkv","mp3","m4a"): continue
                    sz = f.get("filesize") or f.get("filesize_approx") or 0
                    fmts.append({"format_id":f["format_id"],"ext":f.get("ext","mp4"),
                                 "resolution":f.get("resolution") or f.get("quality") or "best",
                                 "has_video":f.get("vcodec")!="none","has_audio":f.get("acodec")!="none",
                                 "filesize":sz,"filesize_human":human_size(sz) if sz else "—"})
                seen=set(); uniq=[]
                for f in sorted(fmts, key=lambda x:(x["has_video"],x["filesize"] or 0), reverse=True):
                    k=(f["resolution"],f["ext"])
                    if k in seen: continue
                    seen.add(k); uniq.append(f)
                    if len(uniq)>=6: break
                return jsonify({"ok":True,"info":{
                    "title":info.get("title","Video"),"thumbnail":info.get("thumbnail") or "",
                    "duration":info.get("duration") or 0,"platform":detect_platform(url),
                    "uploader":info.get("uploader") or info.get("channel") or "","formats":uniq}})
        except Exception:
            pass
    # Fall through to direct job
    job_id = uuid.uuid4().hex[:12]
    with jobs_lock:
        jobs[job_id] = {"status":"queued","url":url,"created":time.time(),"platform":detect_platform(url)}
    threading.Thread(target=download_auto, args=(job_id,url,None), daemon=True).start()
    return jsonify({"ok": True, "job_id": job_id, "direct": True, "platform": detect_platform(url)})

@app.route("/api/download", methods=["POST"])
def api_download():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    format_id = data.get("format_id") or None
    if not url:
        return jsonify({"ok":False,"error":"URL chahiye."}),400
    job_id = uuid.uuid4().hex[:12]
    with jobs_lock:
        jobs[job_id] = {"status":"queued","url":url,"created":time.time(),"platform":detect_platform(url)}
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

if __name__ == "__main__":
    port = int(os.environ.get("PORT","5000"))
    debug = os.environ.get("FLASK_DEBUG","0")=="1"
    print(f"🎬 VideoSaver — backend={BACKEND}, bots={BOT_LIST} (primary=@{BOT_LIST[0] if BOT_LIST else 'none'}), port={port}")
    app.run(host="0.0.0.0", port=port, debug=debug)
