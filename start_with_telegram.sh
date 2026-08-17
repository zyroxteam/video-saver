#!/bin/bash
# Start the website using Telegram bot backend.
# Pehle .env file bana lein (see .env.example) ya env vars export karein.
set -e
cd "$(dirname "$0")"

if [ -f .env ]; then
  echo "📄 Loading .env ..."
  set -a; source .env; set +a
fi

if [ -z "$TELEGRAM_API_ID" ] || [ -z "$TELEGRAM_API_HASH" ] || [ -z "$TELEGRAM_SESSION_STRING" ]; then
  echo ""
  echo "❌ TELEGRAM credentials missing!"
  echo "   1) https://my.telegram.org se api_id/api_hash lein"
  echo "   2) python generate_session.py  run karke session string lein"
  echo "   3) .env file me sabhi values fill karein (.env.example dekhiye)"
  echo ""
  exit 1
fi

export DOWNLOAD_BACKEND=telegram
echo "🚀 Starting VideoSaver in TELEGRAM mode (bot: @${BOT_USERNAME:-allsaverbot}) ..."
exec python app.py
