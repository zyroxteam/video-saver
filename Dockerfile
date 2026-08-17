FROM python:3.11-slim

# Install ffmpeg + node (for yt-dlp YouTube JS) + dependencies
RUN apt-get update -qq && \
    apt-get install -y -qq --no-install-recommends \
        ffmpeg nodejs npm curl ca-certificates && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app code
COPY . .

# Persist downloads across restarts (Render ephemeral FS, but nice to have)
RUN mkdir -p downloads

ENV PORT=10000 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    BOT_USERNAME=YTfinderbot,allsaverbot

EXPOSE 10000

CMD gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 8 --timeout 300
