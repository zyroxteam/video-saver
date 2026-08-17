"""
ZYROX DOWNLOADER — All-in-One Video Saver
==========================================
Telegram bot-chain based downloader (PRIMARY — works on every hosting).
yt-dlp is last-resort fallback for local/dev.

Single shared asyncio loop + ONE persistent Pyrogram client to avoid
Telegram session flooding. Jobs are queued (1 at a time).
"""
import os, re, uuid, time, threading, urllib.parse, asyncio, queue as _q
from pathlib import Path
from functools import wraps

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
_default_dl = Path("/tmp/videosaver") if os.environ.get("PORT") else (APP_ROOT / "downloads")
DOWNLOAD_DIR = Path(os.environ.get("DOWNLOAD_DIR", str(_default_dl)))
try:
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
except Exception:
    DOWNLOAD_DIR = APP_ROOT / "downloads"
    DOWNLOAD_DIR.mkdir(exist_ok=True)

BACKEND = os.environ.get("DOWNLOAD_BACKEND", "auto").lower()

BOTS_RAW = os.environ.get("BOT_USERNAME", "YTfinderbot,allsaverbot,FacebookDl_RoBot")
BOT_LIST = [b.strip().lstrip("@") for b in BOTS_RAW.split(",") if b.strip()]
_extra = ["FacebookDl_RoBot"]
_seen = set(b.lower() for b in BOT_LIST)
for _b in _extra:
    if _b.lower() not in _seen:
        BOT_LIST.append(_b)
        _seen.add(_b.lower())

PLATFORM_BOTS = {
    "Facebook": ["FacebookDl_RoBot", "allsaverbot", "YTfinderbot"],
    "YouTube": ["YTfinderbot", "allsaverbot"],
    "TikTok": ["YTfinderbot", "allsaverbot"],
}

def _get_bot_order(url):
    platform = detect_platform(url) if False else _detect_platform(url)
    preferred = PLATFORM_BOTS.get(platform, [])
    ordered, seen = [], set()
    for b in list(preferred) + list(BOT_LIST):
        bl = b.lower().lstrip("@")
        if bl not in seen:
            seen.add(bl); ordered.append(bl)
    return ordered

# No branding / credits / operator labels shown to the user (per user request: no credit, no admin)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024

jobs = {}
jobs_lock = threading.Lock()

URL_REGEX = re.compile(
    r"https?://(?:www\.)?[-\w@:%._+~#=]{1,256}\.[a-zA-Z0-9()]{1,6}\b[-\w@:%_+.~#?&/=]*",
    re.IGNORECASE,
)

# ------------- helpers -------------
def sanitize(name):
    return re.sub(r"[\\/:*?\"<>|]", "_", name).strip(". ")[:120] or "video"

def _detect_platform(url):
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
detect_platform = _detect_platform

def human_size(n):
    for u in ("B","KB","MB","GB"):
        if n < 1024: return f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} TB"

def _clean_user_text(t):
    """Strip bot usernames, 'Downloaded via @...' credits, t.me links, subscribe prompts from user-visible strings."""
    if not t: return ""
    s = str(t)
    import re as _re
    # Strip zero-width / invisible characters
    s = _re.sub(r"[\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff]", "", s)
    # Remove t.me/... links
    s = _re.sub(r"t\.me/\S+", "", s, flags=_re.I)
    # Remove @usernames
    s = _re.sub(r"@[A-Za-z0-9_]{2,}", "", s)
    # Remove full URLs
    s = _re.sub(r"https?://\S+", "", s, flags=_re.I)
    # Remove credit / subscribe lines (case-insensitive, multi-language)
    credit_patterns = [
        r"downloaded\s*(?:via|by|from)?[^\n]*",
        r"powered\s*by[^\n]*",
        r"join\s+(?:our|my|the)?\s*(?:channel|group|chat|update)[^\n]*",
        r"subscribe\s*(?:to|our|for)[^\n]*",
        r"🤖[^\n]*", r"📥[^\n]*", r"⬇[️]?[^\n]*", r"👉[^\n]*", r"🔗[^\n]*",
    ]
    for pat in credit_patterns:
        s = _re.sub(pat, "", s, flags=_re.I)
    # Collapse leading emoji that were next to removed credits
    s = _re.sub(r"^[\s\W_]*📹[\s\W_]*", "", s)
    s = _re.sub(r"^[\s\W_]+", "", s)
    # Collapse whitespace
    s = _re.sub(r"[|\-–_:•·]{2,}", " ", s)
    s = _re.sub(r"\s{2,}", " ", s).strip(" -–_:|•·\n\r\t")
    return s if len(s) >= 3 else "Video"

# ------------- Bot config -------------
BOT_VIDEO_KEYWORDS = {
    "allsaverbot": ["1080","720","480","360","240","hd","video","mp4","🎞","download"],
    "ytfinderbot": ["video","🎞","mp4","hd","download","get"],
    "facebookdl_robot": ["video","mp4","hd","sd","download","quality"],
}
# Buttons to click BEFORE quality selection (subscribe gates / start prompts)
GATE_KEYWORDS = ["confirm","continue","subscribed","✅","start","get started","i have joined","begin"]
FATAL_ERROR_KW = [
    "not found","doesn't support","does not support","unable to",
    "couldn","could not","unfortunately","invalid link","link is invalid",
    "add to group","guruhga","music was not found","try another link","no video",
    "failed to","cannot download","can't download","can't process","can't find",
    "скачать не удалось","не удалось","download failed","hello! i can download",
]
def _sg(o, a, d=None):
    try: return getattr(o, a, d)
    except Exception: return d
def _st(m, a="text"):
    try: return str(_sg(m, a) or "")
    except Exception: return ""
def _ha(m, a):
    try: return bool(_sg(m, a))
    except Exception: return False

def _pick_button(bot_name, msg, stage="quality"):
    """Pick a button to click. stage='gate' returns gate/confirm buttons;
    stage='quality' returns best video-quality button."""
    rm = _sg(msg, "reply_markup")
    if not rm: return None
    try: kb = getattr(rm, "inline_keyboard", None) or []
    except Exception: return None
    btns = [b for row in kb for b in row if _sg(b, "callback_data")]
    if not btns: return None

    if stage == "gate":
        # Click any "Confirm / Continue / Subscribed" gate button
        for b in btns:
            t = (_sg(b, "text") or "").lower()
            if any(k in t for k in GATE_KEYWORDS):
                return b
        return None

    quality_order = ["2160","1440","1080","hd","720","480","360","240","mp4","video","🎞","download","get","save"]
    best = None; best_score = -1
    for b in btns:
        t = (_sg(b, "text") or "").lower()
        if "audio" in t or "mp3" in t or "🎧" in t or "add to group" in t or "guruhga" in t:
            continue
        # Avoid re-clicking "confirm/subscribe" buttons as quality
        if any(k in t for k in GATE_KEYWORDS):
            continue
        for i, k in enumerate(quality_order):
            if k in t:
                sc = len(quality_order) - i
                if sc > best_score:
                    best_score = sc; best = b
                break
    if best: return best
    for b in btns:
        t = (_sg(b, "text") or "").lower()
        if "audio" not in t and "mp3" not in t and "🎧" not in t \
           and not any(k in t for k in GATE_KEYWORDS) \
           and "add to group" not in t and "guruhga" not in t:
            return b
    return None

# ------------- SHARED ASYNC ENGINE -------------
_tg_loop = None
_tg_client = None
_tg_started = asyncio.Event()
_tg_ready = threading.Event()
_job_queue = asyncio.Queue()

async def _client_keepalive():
    """Maintain one persistent Pyrogram client across all jobs."""
    global _tg_client
    api_id = int(os.environ.get("TELEGRAM_API_ID", "0"))
    api_hash = os.environ.get("TELEGRAM_API_HASH", "")
    session = os.environ.get("TELEGRAM_SESSION_STRING", "")
    if not (api_id and api_hash and session and HAS_PYROGRAM):
        print("[tg] credentials missing, engine disabled", flush=True)
        return
    client = PyroClient(
        "zyrox_main", api_id=api_id, api_hash=api_hash,
        session_string=session, in_memory=False,
        workers=2, max_concurrent_transmissions=2,
    )
    _tg_client = client
    print(f"[tg] starting client...", flush=True)
    backoff = 2
    while True:
        try:
            await client.start()
            print("[tg] client CONNECTED ✓", flush=True)
            _tg_started.set()
            _tg_ready.set()
            # Keep alive; if disconnected, reconnect
            while client.is_connected:
                await asyncio.sleep(5)
            _tg_started.clear()
            print("[tg] disconnected, will reconnect...", flush=True)
        except Exception as e:
            print(f"[tg] client error: {type(e).__name__}: {e}", flush=True)
        try:
            try: await client.stop()
            except Exception: pass
        except Exception: pass
        _tg_started.clear()
        print(f"[tg] retrying in {backoff}s...", flush=True)
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 30)

async def _bot_download_one(bot_name, url, job_id):
    """Download via a specific bot using the SHARED client."""
    client = _tg_client
    if not client or not client.is_connected:
        raise RuntimeError("Telegram client ready nahi hai")
    bot_uname = bot_name.lower().lstrip("@")
    bot_chat = "@" + bot_uname

    video_future = asyncio.Future()
    error_future = asyncio.Future()
    clicked_flag = {"v": False, "gate": 0}  # gate click counter (max 3)
    title_box = {"t": "Video"}
    sent_ts = [0.0]
    handler_installed = [False]
    latest_seen_id = [0]
    _HANDLER_GROUP = -99  # Dedicated group so add/remove are idempotent

    def _set_err(msg):
        if not error_future.done():
            error_future.set_result(msg)

    async def _process_message(c, m):
        try:
            if not clicked_flag.get("_ready", False):
                return
            if _sg(m, "outgoing"):
                return
            try:
                md = _sg(m, "date")
                if md and sent_ts[0] and md.timestamp() < sent_ts[0] - 3:
                    return
            except Exception:
                pass
            v = _sg(m, "video")
            d = _sg(m, "document")
            is_v = bool(v)
            is_vd = False
            if d:
                try: is_vd = (_sg(d, "mime_type") or "").startswith("video/")
                except Exception: is_vd = False
            if is_v or is_vd:
                if not video_future.done():
                    cap = _st(m, "caption")
                    if cap: title_box["t"] = cap[:120]
                    video_future.set_result(m)
                return
            if _ha(m, "audio"):
                return
            if _ha(m, "photo") or _ha(m, "animation") or _ha(m, "video_note"):
                cap = _st(m, "caption")
                if cap and (not title_box["t"] or title_box["t"] == "Video"):
                    title_box["t"] = cap[:120]
            mtxt = _st(m, "text") or _st(m, "caption")
            if mtxt:
                tl = mtxt.lower()
                if not (mtxt.strip().startswith("🔍") or mtxt.strip() == "/cancel"):
                    if not (("\n" in mtxt) and re.search(r'(^|\n)\s*\d+\.\s+\S+', mtxt) and not clicked_flag["v"]):
                        for kw in FATAL_ERROR_KW:
                            if kw in tl:
                                if len(mtxt) < 260 or "not support" in tl or "guruhga" in tl:
                                    _set_err(f"@{bot_uname} ye URL support nahi karta")
                                    return
            if not video_future.done() and not error_future.done():
                rm = _sg(m, "reply_markup")
                # 1) First click any gate/confirm button (subscribe prompt)
                if rm and not clicked_flag["v"] and clicked_flag["gate"] < 3:
                    gate = _pick_button(bot_uname, m, stage="gate")
                    if gate:
                        clicked_flag["gate"] += 1
                        print(f"[bot {bot_uname}] gate click #{clicked_flag['gate']} ({(gate.text or '')[:20]!r})", flush=True)
                        async def _do_gate(msg=m, b=gate):
                            try:
                                await c.request_callback_answer(msg.chat.id, msg.id, b.callback_data, timeout=30)
                            except Exception:
                                pass
                        asyncio.create_task(_do_gate())
                        return
                # 2) Then click quality button
                if rm and not clicked_flag["v"]:
                    btn = _pick_button(bot_uname, m, stage="quality")
                    if btn:
                        clicked_flag["v"] = True
                        cap = _st(m, "caption")
                        if cap and (not title_box["t"] or title_box["t"] == "Video"):
                            title_box["t"] = cap[:120]
                        with jobs_lock:
                            jobs[job_id]["status"] = "selecting_quality"
                        print(f"[bot {bot_uname}] quality click ({(btn.text or '')[:20]!r})", flush=True)
                        async def _do_click(msg=m, b=btn):
                            try:
                                await c.request_callback_answer(msg.chat.id, msg.id, b.callback_data, timeout=60)
                            except Exception:
                                pass
                        asyncio.create_task(_do_click())
        except Exception as e:
            print(f"[bot {bot_uname}] handler err: {type(e).__name__}: {e}", flush=True)

    async def message_handler(c, m):
        try:
            ch = _sg(m, "chat")
            if ch:
                cu = (_sg(ch, "username") or "").lower()
                if cu and cu != bot_uname:
                    return
            await _process_message(c, m)
        except Exception as e:
            print(f"[bot {bot_uname}] outer handler err: {e}", flush=True)

    # Install handler in a DEDICATED group so remove_handler works
    grp = handlers.MessageHandler(message_handler, filters.chat(bot_chat))
    try:
        client.remove_handler(grp, _HANDLER_GROUP)
    except Exception:
        pass
    client.add_handler(grp, group=_HANDLER_GROUP)
    try:
        # DRAIN
        clicked_flag["_ready"] = False
        try:
            await client.send_message(bot_chat, "/cancel")
        except Exception:
            pass
        await asyncio.sleep(1.2)
        try:
            async for _ in client.get_chat_history(bot_chat, limit=15):
                pass
        except Exception:
            pass
        await asyncio.sleep(0.6)
        clicked_flag["_ready"] = True

        # SEND URL
        await client.send_message(bot_chat, url)
        sent_ts[0] = time.time()
        t0 = sent_ts[0]
        with jobs_lock:
            jobs[job_id]["status"] = "contacting_bot"

        total_deadline = t0 + 90
        got_extend = False
        last_poll = 0.0
        while time.time() < total_deadline:
            if not clicked_flag["v"]:
                remaining = min(25, total_deadline - time.time())
            else:
                if not got_extend:
                    total_deadline = time.time() + 120
                    got_extend = True
                remaining = total_deadline - time.time()
            if remaining <= 0: break
            done, _ = await asyncio.wait(
                [video_future, error_future],
                timeout=min(2.0, remaining),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not video_future.done() and not error_future.done() and time.time() - last_poll > 3.0:
                last_poll = time.time()
                try:
                    async for hm in client.get_chat_history(bot_chat, limit=12):
                        try:
                            mid = _sg(hm, "id") or 0
                            if mid and mid <= latest_seen_id[0]: break
                            if mid > latest_seen_id[0]: latest_seen_id[0] = mid
                            if _sg(hm, "outgoing"): continue
                            md = _sg(hm, "date")
                            if md and md.timestamp() < sent_ts[0] - 3: break
                            await _process_message(client, hm)
                            if video_future.done() or error_future.done(): break
                        except Exception: continue
                except Exception: pass
            with jobs_lock:
                jobs[job_id]["wait_sec"] = int(time.time() - t0)
                if video_future.done() or clicked_flag["v"]:
                    jobs[job_id]["status"] = "downloading_from_bot"
                else:
                    jobs[job_id]["status"] = "contacting_bot"
            if error_future.done():
                raise RuntimeError(error_future.result())
            if video_future.done(): break
        if not video_future.done():
            raise RuntimeError(f"@{bot_uname} ne time me video nahi bheji")

        media_msg = video_future.result()
        v = _sg(media_msg, "video")
        d = _sg(media_msg, "document")
        media = v if v else d
        vname = "video.mp4"; fsize = 0
        try:
            cand = _sg(media, "file_name")
            if cand: vname = cand
            fsize = _sg(media, "file_size") or 0
        except Exception: pass
        vname = sanitize(vname) or "video.mp4"
        out_path = DOWNLOAD_DIR / f"{job_id}_{bot_uname}_{vname}"
        print(f"[bot {bot_uname}] media received size={fsize} name={vname}", flush=True)
        last_chunk_t = [time.time()]
        def _prog(cur, tot):
            last_chunk_t[0] = time.time()
        with jobs_lock:
            jobs[job_id]["status"] = "downloading_from_bot"
            if fsize: jobs[job_id]["filesize"] = fsize
        dl = None
        try:
            dl = await asyncio.wait_for(
                client.download_media(media_msg, file_name=str(out_path), progress=_prog),
                timeout=240,
            )
            if time.time() - last_chunk_t[0] > 90:
                raise RuntimeError(f"@{bot_uname} se file download stalled")
        except asyncio.TimeoutError:
            if out_path.exists() and out_path.stat().st_size > 1024*100:
                dl = str(out_path)
            else:
                raise RuntimeError(f"@{bot_uname} se file download time-out")
        except Exception as e:
            raise RuntimeError(f"@{bot_uname} download error: {e}")
        if not dl or not Path(dl).exists():
            cands = list(DOWNLOAD_DIR.glob(f"{job_id}_{bot_uname}_*"))
            cands = [p for p in cands if p.stat().st_size > 1024]
            if cands:
                cands.sort(key=lambda p: p.stat().st_mtime, reverse=True)
                dl = str(cands[0])
            else:
                raise RuntimeError(f"@{bot_uname} se file nahi mili")
        fp = Path(dl)
        if fp.stat().st_size < 1024:
            raise RuntimeError("File khaali hai")
        print(f"[bot {bot_uname}] saved {fp.name} ({fp.stat().st_size} bytes)", flush=True)
        return fp.name, _clean_user_text(title_box["t"])
    finally:
        try: client.remove_handler(grp, _HANDLER_GROUP)
        except Exception: pass


async def _process_job(job_id, url):
    """Run a job against all bots until one succeeds."""
    errors = []
    bot_order = _get_bot_order(url)
    print(f"[job {job_id}] start platform={detect_platform(url)} order={bot_order}", flush=True)
    # Wait for client to be ready
    for _ in range(50):
        if _tg_client and _tg_client.is_connected: break
        await asyncio.sleep(1)
    else:
        with jobs_lock:
            jobs[job_id]["status"] = "error"
            jobs[job_id]["error"] = "Telegram client ready nahi hai"
        return
    for bot_name in bot_order:
        try:
            fn, title = await _bot_download_one(bot_name, url, job_id)
            fp = DOWNLOAD_DIR / fn
            if fp.exists() and fp.stat().st_size > 1024:
                with jobs_lock:
                    jobs[job_id].update(
                        status="done", title=title, filename=fn,
                        filesize=fp.stat().st_size,
                    )
                print(f"[job {job_id}] ✅ @{bot_name} done {fn} ({human_size(fp.stat().st_size)})", flush=True)
                return
        except Exception as e:
            msg = str(e)[:200]
            errors.append(f"@{bot_name}: {msg}")
            print(f"[job {job_id}] ❌ @{bot_name} fail: {e}", flush=True)
    # Bots failed -> try yt-dlp last resort for ALL platforms
    if HAS_YTDLP:
        try:
            fn, title, sz = await _ytdlp_download(job_id, url)
            with jobs_lock:
                jobs[job_id].update(status="done", title=title, filename=fn,
                                    filesize=sz)
            print(f"[job {job_id}] ✅ yt-dlp fallback done ({human_size(sz)})", flush=True)
            return
        except Exception as e:
            errors.append(f"yt-dlp: {str(e)[:120]}")
            print(f"[job {job_id}] ❌ yt-dlp fail: {str(e)[:120]}", flush=True)
    err = " | ".join(errors) or "Saare bots fail"
    with jobs_lock:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = err


async def _ytdlp_download(job_id, url):
    """yt-dlp fallback (run in thread)."""
    def _run():
        outtmpl = str(DOWNLOAD_DIR / f"{job_id}_ytdlp.%(ext)s")
        opts = {
            "quiet": True, "no_warnings": True, "noplaylist": True,
            "geo_bypass": True, "socket_timeout": 25, "retries": 3,
            "fragment_retries": 3, "concurrent_fragment_downloads": 4,
            "nocheckcertificate": True, "prefer_ffmpeg": True,
            "merge_output_format": "mp4", "outtmpl": outtmpl,
            "format": "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/best",
            "extractor_args": {"youtube": {"player_client": ["android","web"]}},
            "http_headers": {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            },
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if info.get("_type") == "playlist":
                info = info["entries"][0]
            fn = Path(ydl.prepare_filename(info))
            if not fn.exists():
                for ext in ("mp4","mkv","webm","m4a"):
                    c = fn.with_suffix(f".{ext}")
                    if c.exists(): fn = c; break
            return info, fn
    loop = asyncio.get_running_loop()
    with jobs_lock:
        jobs[job_id]["status"] = "downloading"
    info, fn = await asyncio.wait_for(loop.run_in_executor(None, _run), timeout=150)
    return fn.name, _clean_user_text(info.get("title","Video")), fn.stat().st_size


async def _queue_worker():
    """Worker that processes queued jobs one at a time on the shared client."""
    await _tg_started.wait()
    print("[tg] queue worker ready", flush=True)
    while True:
        job_id, url = await _job_queue.get()
        try:
            await _process_job(job_id, url)
        except Exception as e:
            print(f"[job {job_id}] uncaught: {e}", flush=True)
            with jobs_lock:
                if jobs.get(job_id, {}).get("status") not in ("done",):
                    jobs[job_id]["status"] = "error"
                    jobs[job_id]["error"] = str(e)[:200]
        finally:
            _job_queue.task_done()


def _start_async_loop():
    global _tg_loop
    loop = asyncio.new_event_loop()
    _tg_loop = loop
    asyncio.set_event_loop(loop)
    try:
        loop.create_task(_client_keepalive())
        loop.create_task(_queue_worker())
        loop.run_forever()
    except Exception as e:
        print(f"[tg] loop died: {e}", flush=True)

_tg_thread = threading.Thread(target=_start_async_loop, daemon=True)
_tg_thread.start()
# Wait briefly for client to start (don't block requests)
_tg_ready.wait(timeout=15)

def enqueue_download(job_id, url):
    """Submit a job to the async engine from a Flask thread."""
    with jobs_lock:
        jobs[job_id]["status"] = "queued"
    _tg_loop.call_soon_threadsafe(asyncio.ensure_future, _job_queue.put((job_id, url)))

# ------------- Routes -------------
@app.route("/")
def home():
    return render_template("index.html")

@app.route("/api/health")
def health():
    connected = bool(_tg_client and _tg_client.is_connected)
    return jsonify({"ok": True, "tg_connected": connected, "brand": "ZYROX DOWNLOADER"})

@app.route("/api/download", methods=["POST"])
def api_download():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"ok": False, "error": "URL chahiye."}), 400
    if not URL_REGEX.match(url):
        return jsonify({"ok": False, "error": "Valid URL daalein."}), 400
    job_id = uuid.uuid4().hex[:12]
    with jobs_lock:
        jobs[job_id] = {"status":"queued","url":url,"created":time.time(),
                        "platform":detect_platform(url)}
    enqueue_download(job_id, url)
    return jsonify({"ok": True, "job_id": job_id})

# /api/fetch alias (for compatibility)
@app.route("/api/fetch", methods=["POST"])
def api_fetch():
    return api_download()

@app.route("/api/status/<job_id>")
def api_status(job_id):
    job = jobs.get(job_id)
    if not job: return jsonify({"ok": False, "error": "Job nahi mila."}), 404
    resp = {}
    for k in ("status", "title", "filename", "filesize", "filesize_human", "platform", "wait_sec", "error"):
        if k in job:
            resp[k] = job[k]
    if job.get("filesize"):
        resp["filesize_human"] = human_size(job["filesize"])
    return jsonify({"ok": True, "job": resp})

@app.route("/files/<path:name>")
def serve_file(name):
    if "/" in name or ".." in name: return ("Bad path", 400)
    return send_from_directory(DOWNLOAD_DIR, name, as_attachment=True)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    print(f"🎬 ZYROX DOWNLOADER — port={port} bots={BOT_LIST}", flush=True)
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
