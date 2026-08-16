"""
Step 1: Telegram Session String Generator
=========================================
Is script ko run karke aap apna Telegram account (personal, NOT bot) Pyrogram se
login karoge, aur ek SESSION STRING milega — woh .env file me daalna hai.
Phir aapki website @allsaverbot ko message bhej sakegi.

Run:  python generate_session.py
"""
from pyrogram import Client

print("=" * 60)
print("  Telegram Session String Generator")
print("=" * 60)
print()
print("Pehle https://my.telegram.org se login karke 'API development tools' me")
print("jaake ek naya app banaiye. Wahan se api_id (number) aur api_hash (text)")
print("milega. Niche daalein:")
print()

API_ID = input("API ID: ").strip()
API_HASH = input("API Hash: ").strip()

with Client(":memory:", api_id=int(API_ID), api_hash=API_HASH) as app:
    session_str = app.export_session_string()
    me = app.get_me()
    print()
    print("=" * 60)
    print(f"✅ Login successful! Account: @{me.username or me.first_name}")
    print()
    print("Apna SESSION STRING (yeh save kar lein, .env me daalna hai):")
    print("-" * 60)
    print(session_str)
    print("-" * 60)
    print()
    print("Is string ko kisi se share mat kariye — yeh aapke account ka")
    print("access deta hai.")
    print("=" * 60)
