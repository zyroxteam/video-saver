# 🎬 VideoSaver

**YouTube / Instagram / Facebook / TikTok / Twitter / Reddit** video downloader website. Backend uses [yt-dlp](https://github.com/yt-dlp/yt-dlp) OR routes every request through a Telegram bot such as `@allsaverbot` via a Pyrogram user client.

## ✨ Features
- 🌐 Hindi-first, glassmorphism UI
- ⚡ YouTube, Instagram, Facebook, TikTok, X, Reddit, Likee support
- 🎚 Quality selection (360p → 1080p + audio-only) in ytdlp mode
- 🤖 Telegram-bot backend support (pluggable)
- 📊 Real-time progress updates
- 🔌 Two backends: `ytdlp` (direct) or `telegram` (through any Telegram downloader bot)

## 🚀 Quick start (direct / yt-dlp mode — default & recommended)

```bash
cd video-downloader
pip install -r requirements.txt
python app.py
```

Then open http://localhost:5000

No Telegram login needed. Works out of the box for 1000+ sites yt-dlp supports.

## 🤖 Telegram bot mode (uses @allsaverbot)

To route downloads through a Telegram bot (as you originally asked), you need a personal Telegram account (bots can't talk to other bots — Telegram platform limitation).

1. Go to https://my.telegram.org → API development tools → create an app to get `api_id` and `api_hash`.
2. Generate a Pyrogram session string:
   ```bash
   python generate_session.py
   ```
3. Copy `.env.example` to `.env` and fill:
   ```
   DOWNLOAD_BACKEND=telegram
   TELEGRAM_API_ID=your_api_id
   TELEGRAM_API_HASH=your_api_hash
   TELEGRAM_SESSION_STRING=your_session_string
   BOT_USERNAME=allsaverbot
   ```
4. Start:
   ```bash
   ./start_with_telegram.sh
   ```

The backend will:
- Send the link to the configured bot
- Auto-click the best quality inline button
- Download the resulting file to `downloads/`
- Serve it to the website visitor

> ⚠️ Security: your session string grants full access to your Telegram account. Keep it secret. Consider using a secondary/throwaway Telegram account for this purpose.

## 📁 Project structure
```
video-downloader/
├── app.py                 # Flask server + both backends
├── templates/index.html   # Frontend UI
├── downloads/             # Downloaded files (gitignored)
├── generate_session.py    # Telegram session string helper
├── start_with_telegram.sh # One-liner to start in telegram mode
├── requirements.txt
└── .env.example           # Environment variable template
```

## 🛠 Deploying
For production use gunicorn/uvicorn:
```bash
pip install gunicorn
gunicorn -w 4 -b 0.0.0.0:5000 app:app
```
Put nginx in front for HTTPS + larger uploads.

## 📝 License
MIT. Respect copyright and platform TOS — only download content you have the right to.
