#!/usr/bin/env python3
"""Deploy VideoSaver to Render via API."""
import json, time, sys, os
import urllib.request, urllib.error

KEY = "rnd_dotakAOSka3mc1yH6yeQm8TtRQ3O"
REPO = "https://github.com/zyroxteam/video-saver"
OWNER_ID = "tea-d9shach42hec73cak4i0"  # from the services list above

HEADERS = {
    "Authorization": f"Bearer {KEY}",
    "Accept": "application/json",
    "Content-Type": "application/json",
}

def api(method, path, body=None):
    url = "https://api.render.com/v1" + path
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, headers=HEADERS, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"  ❌ HTTP {e.code}: {body[:500]}")
        raise

# 1. Create web service
print("🚀 Creating Render web service...")
payload = {
    "type": "web_service",
    "name": "videosaver",
    "ownerId": OWNER_ID,
    "repo": REPO,
    "branch": "main",
    "autoDeploy": "yes",
    "serviceDetails": {
        "env": "python",
        "plan": "free",
        "region": "singapore",
        "pullRequestPreviewsEnabled": "no",
        "numInstances": 1,
        "envSpecificDetails": {
            "buildCommand": "pip install -r requirements.txt && apt-get update -qq && apt-get install -y -qq ffmpeg nodejs",
            "startCommand": "gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --threads 4 --timeout 300",
        },
    },
}
result = api("POST", "/services", payload)
svc = result.get("service", result)
svc_id = svc.get("id") or svc.get("serviceId")
svc_url = svc.get("service", svc).get("url")
dashboard = svc.get("service", svc).get("dashboardUrl")
print(f"✅ Created service: id={svc_id}")
print(f"   Dashboard: {dashboard}")
print(f"   URL (will be available after deploy): {svc_url}")

# 2. Set environment variables
print("\n🔧 Setting environment variables...")
env_vars = [
    {"key": "PYTHON_VERSION", "value": "3.11.10"},
    {"key": "PIP_NO_CACHE_DIR", "value": "1"},
    {"key": "BOT_USERNAME", "value": "allsaverbot"},
    {"key": "TELEGRAM_API_ID", "value": "32497436"},
    {"key": "TELEGRAM_API_HASH", "value": "4ddaed4e65609c53b60f185172e08d18"},
    {"key": "TELEGRAM_SESSION_STRING", "value": open("/home/user/video-downloader/.env").read().split("TELEGRAM_SESSION_STRING=",1)[1].split("\n",1)[0]},
]
api("PUT", f"/services/{svc_id}/env-vars", [{"key": e["key"], "value": e["value"]} for e in env_vars])
print(f"✅ Set {len(env_vars)} env vars")

# 3. Wait for deploy
print("\n⏳ Waiting for first deploy (this takes 3-5 minutes)...")
for i in range(60):
    time.sleep(15)
    try:
        deploys = api("GET", f"/services/{svc_id}/deploys?limit=1")
    except Exception as e:
        print(f"   poll {i}: {e}")
        continue
    if deploys:
        d = deploys[0].get("deploy", deploys[0])
        status = d.get("status", "?")
        print(f"   [{i*15}s] status = {status}")
        if status == "live":
            live_url = svc.get("url") or f"https://videosaver.onrender.com"
            print(f"\n🎉 DEPLOYED LIVE!")
            print(f"   URL: {live_url}")
            print(f"   Dashboard: {dashboard}")
            break
        if status in ("build-failed", "deploy-failed", "canceled"):
            print(f"   ❌ Deploy failed! Check logs at {dashboard}")
            break
else:
    print("⏰ Timed out waiting for deploy. Check dashboard.")

print(f"\n📌 Final dashboard URL: {dashboard}")
