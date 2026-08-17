#!/usr/bin/env python3
"""Deploy VideoSaver (Docker) to Render."""
import json, time, sys
import urllib.request, urllib.error

KEY = "rnd_dotakAOSka3mc1yH6yeQm8TtRQ3O"
REPO = "https://github.com/zyroxteam/video-saver"
OWNER_ID = "tea-d9shach42hec73cak4i0"
SVC_NAME = "videosaver"

H = {"Authorization": f"Bearer {KEY}", "Accept": "application/json", "Content-Type": "application/json"}

def api(m, path, body=None):
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(f"https://api.render.com/v1{path}", data=data, headers=H, method=m)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read()) if r.headers.get("content-type","").startswith("application/json") else r.read().decode()
    except urllib.error.HTTPError as e:
        print(f"  ❌ HTTP {e.code}: {e.read().decode()[:800]}")
        raise

# Check if service already exists
existing = api("GET", f"/services?name={SVC_NAME}&limit=5")
svc_id = None
for it in existing:
    s = it.get("service", it)
    if s.get("name") == SVC_NAME:
        svc_id = s.get("id")
        print(f"🗑️  Deleting old service {svc_id}...")
        api("DELETE", f"/services/{svc_id}")
        time.sleep(5)
        break

print("🚀 Creating Docker-based web service...")
payload = {
    "type": "web_service",
    "name": SVC_NAME,
    "ownerId": OWNER_ID,
    "repo": REPO,
    "branch": "main",
    "autoDeploy": "yes",
    "serviceDetails": {
        "env": "docker",
        "plan": "free",
        "region": "singapore",
        "pullRequestPreviewsEnabled": "no",
        "numInstances": 1,
        "envSpecificDetails": {
            "dockerfilePath": "./Dockerfile",
            "dockerContext": ".",
        },
    },
}
result = api("POST", "/services", payload)
svc = result.get("service", result)
svc_id = svc.get("id") or svc.get("serviceId")
svc_url = svc.get("url")
dashboard = svc.get("dashboardUrl")
print(f"✅ Service: id={svc_id}, url={svc_url}")
print(f"   Dashboard: {dashboard}")

# Set environment variables
print("\n🔧 Setting env vars...")
from pathlib import Path
env_lines = Path("/home/user/video-downloader/.env").read_text().splitlines()
session_val = ""
for ln in env_lines:
    if ln.startswith("TELEGRAM_SESSION_STRING="):
        session_val = ln.split("=",1)[1].strip()
        break

env_vars = [
    {"key": "BOT_USERNAME", "value": "allsaverbot"},
    {"key": "TELEGRAM_API_ID", "value": "32497436"},
    {"key": "TELEGRAM_API_HASH", "value": "4ddaed4e65609c53b60f185172e08d18"},
    {"key": "TELEGRAM_SESSION_STRING", "value": session_val},
    {"key": "PORT", "value": "10000"},
]
api("PUT", f"/services/{svc_id}/env-vars", [{"key":e["key"],"value":e["value"]} for e in env_vars])
print(f"✅ {len(env_vars)} env vars set (session={len(session_val)} chars)")

# Wait for deploy
print("\n⏳ Waiting for deploy (Docker build ~3-6 min)...")
last_status = None
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
        if status != last_status:
            print(f"   [{(i+1)*15}s] {status}")
            last_status = status
        if status == "live":
            # Re-fetch service for final URL
            info = api("GET", f"/services/{svc_id}")
            live_url = info.get("url") or svc_url
            print(f"\n🎉 DEPLOYED LIVE!")
            print(f"   URL: {live_url}")
            print(f"   Dashboard: {dashboard}")
            sys.exit(0)
        if status in ("build-failed", "deploy-failed", "canceled"):
            print(f"   ❌ Deploy failed. {dashboard}")
            sys.exit(1)

print(f"\n⏰ Timed out. Check: {dashboard}")
