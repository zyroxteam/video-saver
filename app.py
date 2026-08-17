"""
ZYROX DOWNLOADER — All-in-One Video Saver
==========================================
Telegram bot-chain based downloader (PRIMARY). yt-dlp is fast fallback.

- MULTIPLE concurrent jobs supported (per-job handlers, concurrency semaphore).
- Per-job handler groups so messages don't leak across jobs.
- @allsaverbot is universal primary (covers YouTube, IG, FB, TT, X, Reddit, Pinterest, etc.).
"""
import os, re, uuid, time, threading, urllib.parse, asyncio
from pathlib import Path

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
    PyroClient = None
    handlers = filters = None
    HAS_PYROGRAM = False

APP_ROOT = Path(__file__).resolve().parent
_default_dl = Path("/tmp/videosaver") if os.environ.get("PORT") else (APP_ROOT / "downloads")
DOWNLOAD_DIR = Path(os.environ.get("DOWNLOAD_DIR", str(_default_dl)))
try:
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
except Exception:
    DOWNLOAD_DIR = APP_ROOT / "downloads"
    DOWNLOAD_DIR.mkdir(exist_ok=True)

# Bots — @allsaverbot supports nearly ALL platforms (YT, IG, FB, TT, X, Reddit, Pinterest, Likee, etc.)
BOTS_RAW = os.environ.get("BOT_USERNAME", "allsaverbot,YTfinderbot,FacebookDl_RoBot,ironwood_downbot,downloader_tiktok_bot")
BOT_LIST = [b.strip().lstrip("@") for b in BOTS_RAW.split(",") if b.strip()]
_seen = set(b.lower() for b in BOT_LIST)
for _extra in ("allsaverbot","YTfinderbot","FacebookDl_RoBot"):
    if _extra.lower() not in _seen:
        BOT_LIST.append(_extra); _seen.add(_extra.lower())

# ALL platforms prefer @allsaverbot first — it truly supports 40+ sites.
PLATFORM_BOTS = {
    "YouTube":   ["allsaverbot", "YTfinderbot"],
    "Facebook":  ["FacebookDl_RoBot", "allsaverbot"],
    "TikTok":    ["allsaverbot", "YTfinderbot"],
    "Instagram": ["allsaverbot", "ironwood_downbot"],
    "Twitter/X": ["allsaverbot", "ironwood_downbot"],
    "Reddit":    ["allsaverbot"],
    "Pinterest": ["allsaverbot"],
    "Likee":     ["allsaverbot"],
    "Snapchat":  ["allsaverbot"],
    "Dailymotion":["allsaverbot"],
    "Vimeo":     ["allsaverbot"],
}

def _get_bot_order(url):
    platform = _detect_platform(url)
    preferred = PLATFORM_BOTS.get(platform, [])
    ordered, seen = [], set()
    # Universal primary: allsaverbot FIRST for any unknown platform
    if "allsaverbot" not in [b.lower() for b in preferred]:
        preferred = ["allsaverbot"] + list(preferred)
    for b in list(preferred) + list(BOT_LIST):
        bl = b.lower().lstrip("@")
        if bl not in seen:
            seen.add(bl); ordered.append(bl)
    return ordered

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024

jobs = {}
jobs_lock = threading.Lock()

URL_REGEX = re.compile(
    r"https?://(?:www\.)?[-\w@:%._+~#=]{1,256}\.[a-zA-Z0-9()]{1,6}\b[-\w@:%_+.~#?&/=]*",
    re.IGNORECASE,
)

def sanitize(name):
    return re.sub(r"[\\/:*?\"<>|]", "_", name).strip(". ")[:120] or "video"

def _detect_platform(url):
    h = urllib.parse.urlparse(url).netloc.lower()
    if any(x in h for x in ("youtu.be","youtube")): return "YouTube"
    if "instagram" in h: return "Instagram"
    if any(x in h for x in ("facebook","fb.watch","fb.com")): return "Facebook"
    if "tiktok" in h: return "TikTok"
    if any(x in h for x in ("twitter","x.com")): return "Twitter/X"
    if any(x in h for x in ("reddit","redd.it")): return "Reddit"
    if any(x in h for x in ("pinterest","pin.it")): return "Pinterest"
    if "likee" in h: return "Likee"
    if "snap" in h: return "Snapchat"
    if any(x in h for x in ("dailymotion","dai.ly")): return "Dailymotion"
    if "vimeo" in h: return "Vimeo"
    return h

def human_size(n):
    for u in ("B","KB","MB","GB"):
        if n < 1024: return f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} TB"

def _clean_user_text(t):
    if not t: return ""
    s = str(t)
    import re as _re
    s = _re.sub(r"[\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff]", "", s)
    s = _re.sub(r"t\.me/\S+", "", s, flags=_re.I)
    s = _re.sub(r"@[A-Za-z0-9_]{2,}", "", s)
    s = _re.sub(r"https?://\S+", "", s, flags=_re.I)
    credit_patterns = [
        r"downloaded\s*(?:via|by|from)?[^\n]*",
        r"powered\s*by[^\n]*",
        r"join\s+(?:our|my|the)?\s*(?:channel|group|chat|update)[^\n]*",
        r"subscribe\s*(?:to|our|for)[^\n]*",
        r"🤖[^\n]*", r"📥[^\n]*", r"⬇[️]?[^\n]*", r"👉[^\n]*", r"🔗[^\n]*",
        r"📲[^\n]*", r"✨[^\n]*", r"🔥[^\n]*", r"💜[^\n]*", r"👇[^\n]*",
    ]
    for pat in credit_patterns:
        s = _re.sub(pat, "", s, flags=_re.I)
    s = _re.sub(r"^[\s\W_]*📹[\s\W_]*", "", s)
    s = _re.sub(r"^[\s\W_]+", "", s)
    s = _re.sub(r"[|\-–_:•·]{2,}", " ", s)
    s = _re.sub(r"\s{2,}", " ", s).strip(" -–_:|•·\n\r\t")
    return s if len(s) >= 3 else "Video"

GATE_KEYWORDS = [
    "confirm","continue","subscribed","✅","i have joined","begin",
    "start","get started","next","proceed","yes","i'm in","okay","ok",
    "download","get video","get link","send video","mp4","tap here",
]
FATAL_ERROR_KW = [
    "not found","doesn't support","does not support","unable to",
    "couldn't","could not","unfortunately","invalid link","link is invalid",
    "add to group","guruhga","music was not found","try another link","no video",
    "failed to","cannot download","can't download","can't process","can't find",
    "скачать не удалось","не удалось","download failed","hello! i can download",
    "not available","private","copyright","blocked","age restricted","login required",
    "is not supported","not supported","unsupported","error occurred",
    "this link","link is not","broken","removed","deleted","video is not",
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
    rm = _sg(msg, "reply_markup")
    if not rm: return None
    try: kb = getattr(rm, "inline_keyboard", None) or []
    except Exception: return None
    btns = [b for row in kb for b in row if _sg(b, "callback_data")]
    if not btns: return None

    if stage == "gate":
        for b in btns:
            t = (_sg(b, "text") or "").lower().strip()
            if any(k in t for k in GATE_KEYWORDS):
                return b
        return None

    quality_order = ["2160","1440","1080","hd","720","480","360","240","mp4","video","🎞","download","get","save","proceed"]
    best = None; best_score = -1
    for b in btns:
        t = (_sg(b, "text") or "").lower().strip()
        skip_markers = ("audio","mp3","🎧","add to group","guruhga","vip","premium","coin","pay","subscribe to join")
        if any(x in t for x in skip_markers):
            continue
        if any(k in t for k in ("subscribe","join channel","join our","must join","join first")):
            continue
        for i, k in enumerate(quality_order):
            if k in t:
                sc = len(quality_order) - i
                if sc > best_score:
                    best_score = sc; best = b
                break
    if best: return best
    for b in btns:
        t = (_sg(b, "text") or "").lower().strip()
        skip_markers = ("audio","mp3","🎧","add to group","guruhga","vip","premium","coin","pay","subscribe to join","join channel")
        if not any(x in t for x in skip_markers):
            return b
    return None

# ------------- ASYNC ENGINE -------------
_tg_loop = None
_tg_client = None
_tg_started = asyncio.Event()
_tg_ready = threading.Event()
_job_sem = None              # concurrency limiter
_active_jobs = {}            # job_id -> state dict (for routing messages)
_next_group = [-1000]        # per-job handler group allocator
_group_owners = {}           # group_id -> job_id

async def _client_keepalive():
    global _tg_client, _job_sem
    api_id = int(os.environ.get("TELEGRAM_API_ID", "0"))
    api_hash = os.environ.get("TELEGRAM_API_HASH", "")
    session = os.environ.get("TELEGRAM_SESSION_STRING", "")
    if not (api_id and api_hash and session and HAS_PYROGRAM):
        print("[tg] credentials missing, engine disabled", flush=True)
        return
    CONC = int(os.environ.get("TG_CONCURRENCY", "3"))
    _job_sem = asyncio.Semaphore(CONC)
    client = PyroClient(
        "zyrox_main", api_id=api_id, api_hash=api_hash,
        session_string=session, in_memory=False,
        workers=4, max_concurrent_transmissions=4,
    )
    _tg_client = client
    print(f"[tg] starting client (concurrency={CONC})...", flush=True)
    backoff = 2
    while True:
        try:
            await client.start()
            print("[tg] client CONNECTED ✓", flush=True)
            _tg_started.set()
            _tg_ready.set()
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
    client = _tg_client
    if not client or not client.is_connected:
        raise RuntimeError("Telegram client ready nahi hai")
    bot_uname = bot_name.lower().lstrip("@")
    bot_chat = "@" + bot_uname

    video_future = asyncio.Future()
    error_future = asyncio.Future()
    clicked_flag = {"v": False, "gate": 0}
    title_box = {"t": "Video"}
    sent_ts = [0.0]
    latest_seen_id = [0]
    # Allocate a UNIQUE handler group per (bot,job) so concurrent jobs don't collide
    grp_id = None
    while True:
        grp_id = _next_group[0]; _next_group[0] -= 1
        if grp_id not in _group_owners: break

    state = {
        "bot_uname": bot_uname, "video_future": video_future, "error_future": error_future,
        "clicked_flag": clicked_flag, "title_box": title_box, "sent_ts": sent_ts,
        "latest_seen_id": latest_seen_id, "job_id": job_id, "ready": False,
    }
    _active_jobs[job_id] = state
    _group_owners[grp_id] = job_id

    def _set_err(msg):
        if not error_future.done():
            error_future.set_result(msg)

    async def _process_message(c, m):
        try:
            if not state["ready"]: return
            if _sg(m, "outgoing"): return
            try:
                md = _sg(m, "date")
                if md and sent_ts[0] and md.timestamp() < sent_ts[0] - 3: return
            except Exception: pass
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
                    if cap: title_box["t"] = cap[:160]
                    video_future.set_result(m)
                return
            if _ha(m, "audio"): return
            if _ha(m, "photo") or _ha(m, "animation") or _ha(m, "video_note"):
                cap = _st(m, "caption")
                if cap and (not title_box["t"] or title_box["t"] == "Video"):
                    title_box["t"] = cap[:160]
            mtxt = _st(m, "text") or _st(m, "caption")
            if mtxt:
                tl = mtxt.lower()
                if not (mtxt.strip().startswith("🔍") or mtxt.strip() == "/cancel"):
                    # Menu list message from allsaverbot: don't treat as error
                    if not (("\n" in mtxt) and re.search(r'(^|\n)\s*\d+[\.\)]\s+\S+', mtxt) and not clicked_flag["v"]):
                        for kw in FATAL_ERROR_KW:
                            if kw in tl:
                                if len(mtxt) < 300 or "not support" in tl or "guruhga" in tl or "not found" in tl or "invalid" in tl:
                                    _set_err(f"@{bot_uname} ye URL support nahi karta")
                                    return
            if not video_future.done() and not error_future.done():
                rm = _sg(m, "reply_markup")
                if rm and not clicked_flag["v"] and clicked_flag["gate"] < 4:
                    gate = _pick_button(bot_uname, m, stage="gate")
                    if gate:
                        clicked_flag["gate"] += 1
                        print(f"[{job_id} @{bot_uname}] gate click #{clicked_flag['gate']} ({(gate.text or '')[:24]!r})", flush=True)
                        async def _do_gate(msg=m, b=gate):
                            try:
                                await c.request_callback_answer(msg.chat.id, msg.id, b.callback_data, timeout=30)
                            except Exception: pass
                        asyncio.create_task(_do_gate())
                        return
                if rm and not clicked_flag["v"]:
                    btn = _pick_button(bot_uname, m, stage="quality")
                    if btn:
                        clicked_flag["v"] = True
                        cap = _st(m, "caption")
                        if cap and (not title_box["t"] or title_box["t"] == "Video"):
                            title_box["t"] = cap[:160]
                        with jobs_lock:
                            jobs[job_id]["status"] = "selecting_quality"
                        print(f"[{job_id} @{bot_uname}] quality click ({(btn.text or '')[:24]!r})", flush=True)
                        async def _do_click(msg=m, b=btn):
                            try:
                                await c.request_callback_answer(msg.chat.id, msg.id, b.callback_data, timeout=60)
                            except Exception: pass
                        asyncio.create_task(_do_click())
        except Exception as e:
            print(f"[{job_id} @{bot_uname}] handler err: {type(e).__name__}: {e}", flush=True)

    async def message_handler(c, m):
        try:
            ch = _sg(m, "chat")
            if ch:
                cu = (_sg(ch, "username") or "").lower()
                if cu and cu != bot_uname: return
            await _process_message(c, m)
        except Exception as e:
            print(f"[{job_id} @{bot_uname}] outer: {e}", flush=True)

    grp = handlers.MessageHandler(message_handler, filters.chat(bot_chat))
    client.add_handler(grp, group=grp_id)
    try:
        # DRAIN
        state["ready"] = False
        try: await client.send_message(bot_chat, "/cancel")
        except Exception: pass
        await asyncio.sleep(0.8)
        try:
            async for _ in client.get_chat_history(bot_chat, limit=10):
                pass
        except Exception: pass
        await asyncio.sleep(0.4)
        state["ready"] = True

        await client.send_message(bot_chat, url)
        sent_ts[0] = time.time()
        t0 = sent_ts[0]
        with jobs_lock:
            jobs[job_id]["status"] = "contacting_bot"

        total_deadline = t0 + 55          # wait for quality selection max 55s
        got_extend = False
        last_poll = 0.0
        while time.time() < total_deadline:
            if not clicked_flag["v"]:
                remaining = min(20, total_deadline - time.time())
            else:
                if not got_extend:
                    total_deadline = time.time() + 180  # download up to 3 min
                    got_extend = True
                remaining = total_deadline - time.time()
            if remaining <= 0: break
            done, _ = await asyncio.wait(
                [video_future, error_future],
                timeout=min(2.0, remaining),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not video_future.done() and not error_future.done() and time.time() - last_poll > 2.5:
                last_poll = time.time()
                try:
                    async for hm in client.get_chat_history(bot_chat, limit=10):
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
        print(f"[{job_id} @{bot_uname}] media size={fsize} name={vname}", flush=True)
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
                timeout=300,
            )
            if time.time() - last_chunk_t[0] > 120:
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
        print(f"[{job_id} @{bot_uname}] saved {fp.name} ({fp.stat().st_size} bytes)", flush=True)
        return fp.name, _clean_user_text(title_box["t"])
    finally:
        try: client.remove_handler(grp, grp_id)
        except Exception: pass
        _active_jobs.pop(job_id, None)
        _group_owners.pop(grp_id, None)


async def _process_job(job_id, url):
    errors = []
    bot_order = _get_bot_order(url)
    print(f"[job {job_id}] start platform={_detect_platform(url)} order={bot_order}", flush=True)
    for _ in range(60):
        if _tg_client and _tg_client.is_connected: break
        await asyncio.sleep(1)
    else:
        with jobs_lock:
            jobs[job_id]["status"] = "error"
            jobs[job_id]["error"] = "Telegram client ready nahi hai"
        return

    # Acquire concurrency slot
    if _job_sem:
        with jobs_lock:
            jobs[job_id]["status"] = "queued"
        await _job_sem.acquire()
    try:
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
        # yt-dlp fast fallback
        if HAS_YTDLP:
            try:
                fn, title, sz = await _ytdlp_download(job_id, url)
                with jobs_lock:
                    jobs[job_id].update(status="done", title=title, filename=fn, filesize=sz)
                print(f"[job {job_id}] ✅ yt-dlp fallback done ({human_size(sz)})", flush=True)
                return
            except Exception as e:
                errors.append(f"yt-dlp: {str(e)[:120]}")
                print(f"[job {job_id}] ❌ yt-dlp fail: {str(e)[:120]}", flush=True)
        err = " | ".join(errors[-2:]) or "Saare bots fail"
        with jobs_lock:
            jobs[job_id]["status"] = "error"
            jobs[job_id]["error"] = err
    finally:
        if _job_sem:
            _job_sem.release()


async def _ytdlp_download(job_id, url):
    def _run():
        outtmpl = str(DOWNLOAD_DIR / f"{job_id}_ytdlp.%(ext)s")
        opts = {
            "quiet": True, "no_warnings": True, "noplaylist": True,
            "geo_bypass": True, "socket_timeout": 25, "retries": 3,
            "fragment_retries": 3, "concurrent_fragment_downloads": 6,
            "nocheckcertificate": True, "prefer_ffmpeg": True,
            "merge_output_format": "mp4", "outtmpl": outtmpl,
            "format": "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/best",
            "extractor_args": {"youtube": {"player_client": ["android","web"]}},
            "http_headers": {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36",
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
    info, fn = await asyncio.wait_for(loop.run_in_executor(None, _run), timeout=180)
    return fn.name, _clean_user_text(info.get("title","Video")), fn.stat().st_size


async def _queue_worker():
    await _tg_started.wait()
    print("[tg] queue worker ready", flush=True)
    while True:
        job_id, url = await _job_queue.get()
        asyncio.create_task(_process_job(job_id, url))
        _job_queue.task_done()

_job_queue = asyncio.Queue()

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
_tg_ready.wait(timeout=20)

def enqueue_download(job_id, url):
    with jobs_lock:
        jobs[job_id]["status"] = "queued"
    _tg_loop.call_soon_threadsafe(asyncio.ensure_future, _job_queue.put((job_id, url)))

# ------------- Cleanup old files (15 min) -------------
def _cleanup_loop():
    while True:
        time.sleep(300)
        try:
            cutoff = time.time() - 15*60
            for p in DOWNLOAD_DIR.glob("*"):
                try:
                    if p.is_file() and p.stat().st_mtime < cutoff:
                        p.unlink()
                except Exception: pass
        except Exception: pass
threading.Thread(target=_cleanup_loop, daemon=True).start()

# ------------- Routes -------------
@app.route("/")
def home():
    return render_template("index.html")

@app.route("/api/health")
def health():
    connected = bool(_tg_client and _tg_client.is_connected)
    queued = sum(1 for j in jobs.values() if j.get("status") in ("queued","contacting_bot","selecting_quality","downloading_from_bot","downloading"))
    return jsonify({"ok": True, "tg_connected": connected, "brand": "ZYROX DOWNLOADER", "active_jobs": queued})

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
                        "platform":_detect_platform(url)}
    enqueue_download(job_id, url)
    return jsonify({"ok": True, "job_id": job_id})

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
