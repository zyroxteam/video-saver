# 📝 Telegram Session Setup — Step by Step (Hindi)

@allsaverbot ke through download karne ke liye ye 3 steps follow karein:

---

## Step 1: Telegram API credentials banayein
1. Browser me jayein: **https://my.telegram.org**
2. Apne Telegram account se login karein (OTP aayega phone pe)
3. "API development tools" par click karein
4. Ek naya app banayein:
   - App name: `VideoSaver`
   - Short name: `vsaver`
   - URL/Platform: kuchh bhi daal sakte ho
5. Submit karne par aapko milega:
   - **App api_id** (number, jaise `12345678`)
   - **App api_hash** (text, jaise `abc123def456...`)

⚠️ Ye credentials save kar lein.

---

## Step 2: Session string generate karein
Terminal me ye command run karein:
```bash
cd video-downloader
python generate_session.py
```
Script puchega:
- API ID daalein
- API Hash daalein
- Phir aapke Telegram account pe login karega (phone number + OTI + 2FA agar hai)

Login successful hone par ek **bada session string** dikhega (like `BQD...xxx...wQA=`) — woh poora copy kar lein.

---

## Step 3: .env file banayein
`video-downloader/` folder me `.env` naam ki file banayein:
```bash
cp .env.example .env
nano .env   # ya VS Code/kisi editor se kholkar edit karein
```
Isme ye values fill karein:
```
DOWNLOAD_BACKEND=telegram
TELEGRAM_API_ID=12345678
TELEGRAM_API_HASH=abc123def456...
TELEGRAM_SESSION_STRING=BQD...xxx...wQA=
BOT_USERNAME=allsaverbot
```

---

## Step 4: Server start karein
```bash
./start_with_telegram.sh
```
Ya manually:
```bash
export DOWNLOAD_BACKEND=telegram
export TELEGRAM_API_ID=...
export TELEGRAM_API_HASH=...
export TELEGRAM_SESSION_STRING=...
python app.py
```

Ab browser me `http://localhost:5000` khol ke test karein! 🎉

---

## 🛠️ Kaise kaam karta hai?
1. User website pe YouTube/Instagram/Facebook link paste karta hai
2. Website backend (Pyrogram se aapke Telegram account ke through) @allsaverbot ko URL bhejta hai
3. @allsaverbot process karke video/audio file reply me deta hai
4. Backend us file ko server pe download karta hai (`downloads/` folder me)
5. User ko direct download link milta hai website par

## ⚠️ Important Notes
1. **Session string confidential hai** — kisi se share mat karein (aapke Telegram account ka poora access deta hai)
2. Agar Telegram par 2FA (two-step verification) on hai, to script login ke time password maangega
3. @allsaverbot agar kabhi reply na de ya slow ho, to 2 min me timeout error dikhega — page refresh karke dobara try karein
4. Agar @allsaverbot band ho jaye to `.env` me `BOT_USERNAME` change karke kisi aur downloader bot (jaise `@YouTube_Download_Bot`, `@InstaSaveYtDlBot` etc.) par switch kar sakte ho
5. **Rate limit ka dhyan rakhein** — baar-baar mat bhejein, nahi to bot aapko temporarily block kar sakta hai
