import json
import logging
import os
import re
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, request
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from PIL import Image, ImageDraw, ImageFont
import sqlite3

from metadata import clean_metadata, parse_json_response

load_dotenv()
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("youtube-ai-manager")
app = Flask(__name__)
app.secret_key = os.getenv("APP_SECRET", "change-me-before-deploy")
DB = os.getenv("DATABASE_PATH", "manager.db")
MEDIA = Path(os.getenv("MEDIA_DIR", "media"))
MEDIA.mkdir(parents=True, exist_ok=True)
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.force-ssl",
]
stop_event = threading.Event()


def db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS videos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_file_id TEXT UNIQUE,
            file_path TEXT NOT NULL,
            original_name TEXT,
            title TEXT,
            description TEXT,
            tags TEXT,
            hashtags TEXT,
            thumbnail_path TEXT,
            status TEXT NOT NULL DEFAULT 'queued',
            youtube_video_id TEXT,
            created_at TEXT NOT NULL
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY, value TEXT NOT NULL
        )""")


def owner_allowed(user_id):
    owner = os.getenv("OWNER_TELEGRAM_ID", "").strip()
    return bool(owner) and str(user_id) == owner


def telegram_call(method, payload=None):
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN missing")
    response = requests.post(f"https://api.telegram.org/bot{token}/{method}", json=payload or {}, timeout=60)
    response.raise_for_status()
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError(str(data))
    return data["result"]


def send_message(chat_id, text):
    telegram_call("sendMessage", {"chat_id": chat_id, "text": text})


def gemini_metadata(file_path, filename):
    key = os.getenv("GEMINI_API_KEY", "").strip()
    if not key:
        stem = Path(filename).stem.replace("_", " ").replace("-", " ")
        return clean_metadata({"title": stem[:100] or "New video", "description": "New video.\n\n" + "#YouTube #CoodWithYogesh", "tags": ["CoodWithYogesh"], "hashtags": ["CoodWithYogesh"]})
    model = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
    prompt = ("You are a YouTube SEO editor. Return ONLY JSON with title (<=100 chars), "
              "description, tags (max 20 strings), hashtags (max 10 strings), category_id, language. "
              "Use Hindi/Hinglish where appropriate. Never invent facts about the video. "
              "If the filename is the only context, create a conservative title and say little. "
              "Filename: " + filename)
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
    body = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"responseMimeType": "application/json"}}
    response = requests.post(url, json=body, timeout=90)
    response.raise_for_status()
    raw = response.json()["candidates"][0]["content"]["parts"][0]["text"]
    return clean_metadata(parse_json_response(raw))


def make_thumbnail(video_path, title, output_path):
    # Create a usable custom thumbnail from a real frame of the uploaded video.
    frame = MEDIA / (Path(output_path).stem + "_frame.jpg")
    try:
        subprocess.run(["ffmpeg", "-y", "-ss", "00:00:02", "-i", str(video_path), "-frames:v", "1", str(frame)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=45)
        image = Image.open(frame).convert("RGB").resize((1280, 720))
        draw = ImageDraw.Draw(image, "RGBA")
        draw.rectangle((0, 470, 1280, 720), fill=(0, 0, 0, 185))
        font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        font = ImageFont.truetype(font_path, 58) if Path(font_path).exists() else ImageFont.load_default()
        words, lines, line = title.split(), [], ""
        for word in words:
            candidate = (line + " " + word).strip()
            if draw.textbbox((0, 0), candidate, font=font)[2] > 1180 and line:
                lines.append(line)
                line = word
            else:
                line = candidate
        if line:
            lines.append(line)
        y = 500
        for item in lines[:3]:
            draw.text((55, y), item, font=font, fill="white", stroke_width=2, stroke_fill="black")
            y += 68
        image.save(output_path, quality=92)
        frame.unlink(missing_ok=True)
        return str(output_path)
    except Exception as exc:
        log.warning("Thumbnail frame generation skipped: %s", exc)
        return None


def youtube_service():
    token_file = os.getenv("YOUTUBE_TOKEN_FILE", "token.json")
    creds = None
    if Path(token_file).exists():
        creds = Credentials.from_authorized_user_file(token_file, SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(GoogleRequest())
        Path(token_file).write_text(creds.to_json(), encoding="utf-8")
    if not creds or not creds.valid:
        raise RuntimeError("YouTube not connected. Open /oauth/start first.")
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def upload_video(video_id, privacy="private"):
    with db() as con:
        row = con.execute("SELECT * FROM videos WHERE id=?", (video_id,)).fetchone()
    if not row:
        raise ValueError("Video queue ID not found")
    service = youtube_service()
    body = {
        "snippet": {
            "title": row["title"][:100],
            "description": row["description"] or "",
            "tags": json.loads(row["tags"] or "[]"),
            "categoryId": "22",
        },
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
    }
    media = MediaFileUpload(row["file_path"], chunksize=-1, resumable=True)
    result = service.videos().insert(part="snippet,status", body=body, media_body=media).execute()
    yt_id = result["id"]
    thumb = row["thumbnail_path"]
    if thumb and Path(thumb).exists():
        service.thumbnails().set(videoId=yt_id, media_body=MediaFileUpload(thumb)).execute()
    with db() as con:
        con.execute("UPDATE videos SET status='uploaded', youtube_video_id=? WHERE id=?", (yt_id, video_id))
    return yt_id


def handle_video(message):
    user = message.get("from", {})
    chat_id = message.get("chat", {}).get("id")
    if not owner_allowed(user.get("id")):
        send_message(chat_id, "Sorry, ye bot sirf owner ke liye hai.")
        return
    video = message.get("video") or message.get("document")
    if not video:
        send_message(chat_id, "Video file bhejo. MP4 recommended hai.")
        return
    file_id = video["file_id"]
    with db() as con:
        old = con.execute("SELECT id FROM videos WHERE telegram_file_id=?", (file_id,)).fetchone()
    if old:
        send_message(chat_id, f"Ye video pehle se queue mein hai. ID: {old['id']}")
        return
    info = telegram_call("getFile", {"file_id": file_id})
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    url = f"https://api.telegram.org/file/bot{token}/{info['file_path']}"
    suffix = Path(info["file_path"]).suffix or ".mp4"
    target = MEDIA / f"telegram_{file_id}{suffix}"
    with requests.get(url, stream=True, timeout=180) as response:
        response.raise_for_status()
        with target.open("wb") as out:
            for chunk in response.iter_content(1024 * 1024):
                if chunk:
                    out.write(chunk)
    name = message.get("caption") or Path(info["file_path"]).name
    metadata = gemini_metadata(target, name)
    thumb_path = make_thumbnail(target, metadata["title"], MEDIA / f"thumb_{file_id}.jpg")
    with db() as con:
        cur = con.execute("""INSERT INTO videos
            (telegram_file_id,file_path,original_name,title,description,tags,hashtags,thumbnail_path,status,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (file_id,str(target),name,metadata["title"],metadata["description"],json.dumps(metadata["tags"]),
             json.dumps(metadata["hashtags"]),thumb_path,"queued",datetime.now(timezone.utc).isoformat()))
        item_id = cur.lastrowid
    send_message(chat_id, f"✅ Video queue mein add ho gaya!\nID: {item_id}\n\nTitle: {metadata['title']}\nTags: {', '.join(metadata['tags'][:8])}\n\nPublish ke liye /publish {item_id} bhejo. Abhi upload private rahega.")


def telegram_loop():
    offset = None
    while not stop_event.is_set():
        try:
            params = {"timeout": 25}
            if offset is not None:
                params["offset"] = offset
            token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
            if not token:
                time.sleep(5)
                continue
            res = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", params=params, timeout=35)
            res.raise_for_status()
            for update in res.json().get("result", []):
                offset = update["update_id"] + 1
                message = update.get("message", {})
                text = (message.get("text") or "").strip()
                chat_id = message.get("chat", {}).get("id")
                if text.startswith("/start"):
                    send_message(chat_id, "YouTube AI Manager ready. Video bhejo. Commands: /queue, /publish ID")
                elif text.startswith("/queue") and owner_allowed(message.get("from", {}).get("id")):
                    with db() as con:
                        rows = con.execute("SELECT id,title,status FROM videos ORDER BY id DESC LIMIT 10").fetchall()
                    send_message(chat_id, "\n".join([f"#{r['id']} [{r['status']}] {r['title']}" for r in rows]) or "Queue empty.")
                elif text.startswith("/publish") and owner_allowed(message.get("from", {}).get("id")):
                    match = re.search(r"/publish\s+(\d+)", text)
                    if not match:
                        send_message(chat_id, "Use: /publish 1")
                    else:
                        try:
                            yt_id = upload_video(int(match.group(1)), "private")
                            send_message(chat_id, f"✅ YouTube par private upload complete: https://youtu.be/{yt_id}\nPublic karne ke liye YouTube Studio mein review karo.")
                        except Exception as exc:
                            log.exception("Upload failed")
                            send_message(chat_id, f"Upload failed: {exc}")
                elif message.get("video") or (message.get("document", {}).get("mime_type", "").startswith("video/")):
                    try:
                        handle_video(message)
                    except Exception as exc:
                        log.exception("Telegram video handling failed")
                        send_message(chat_id, f"Video process nahi hua: {exc}")
        except Exception:
            log.exception("Telegram polling error")
            time.sleep(5)


@app.get("/")
def dashboard():
    with db() as con:
        rows = con.execute("SELECT id,title,status,youtube_video_id,created_at FROM videos ORDER BY id DESC LIMIT 50").fetchall()
    items = "".join(
        f"<tr><td>{r['id']}</td><td>{r['title'] or ''}</td><td>{r['status']}</td><td>{r['youtube_video_id'] or ''}</td></tr>"
        for r in rows
    )
    return f"""<!doctype html><html><head><meta name='viewport' content='width=device-width,initial-scale=1'>
    <title>YouTube AI Manager</title><style>body{{font:16px Arial;background:#101827;color:#eef2ff;padding:20px}}
    main{{max-width:900px;margin:auto}}section{{background:#1d293b;padding:16px;border-radius:14px}}
    table{{width:100%;border-collapse:collapse}}td,th{{padding:10px;border-bottom:1px solid #39465b;text-align:left}}
    a{{color:#60a5fa}}</style></head><body><main><h1>🎬 YouTube AI Manager</h1>
    <p>Telegram video intake · AI metadata · YouTube upload queue</p><p><a href='/oauth/start'>Connect YouTube channel</a> · <a href='/api/queue'>Queue JSON</a></p>
    <section><h2>Recent videos</h2><table><tr><th>ID</th><th>Title</th><th>Status</th><th>YouTube ID</th></tr>{items}</table></section></main></body></html>"""


@app.get("/api/queue")
def queue_api():
    with db() as con:
        rows = con.execute("SELECT id,title,status,youtube_video_id,created_at FROM videos ORDER BY id DESC LIMIT 100").fetchall()
    return jsonify([dict(row) for row in rows])


@app.get("/oauth/start")
def oauth_start():
    secrets = os.getenv("YOUTUBE_CLIENT_SECRETS_FILE", "client_secret.json")
    if not Path(secrets).exists():
        return "Upload Google OAuth client_secret.json and set YOUTUBE_CLIENT_SECRETS_FILE.", 400
    flow = Flow.from_client_secrets_file(secrets, scopes=SCOPES, redirect_uri=request.url_root.rstrip("/") + "/oauth/callback")
    auth_url, state = flow.authorization_url(access_type="offline", include_granted_scopes="true", prompt="consent")
    app.config["oauth_state"] = state
    return redirect(auth_url)


@app.get("/oauth/callback")
def oauth_callback():
    secrets = os.getenv("YOUTUBE_CLIENT_SECRETS_FILE", "client_secret.json")
    flow = Flow.from_client_secrets_file(secrets, scopes=SCOPES, state=app.config.get("oauth_state"),
                                         redirect_uri=request.url_root.rstrip("/") + "/oauth/callback")
    flow.fetch_token(authorization_response=request.url)
    Path(os.getenv("YOUTUBE_TOKEN_FILE", "token.json")).write_text(flow.credentials.to_json(), encoding="utf-8")
    return "YouTube channel connected. You can close this page and send a video to your Telegram bot."


def main():
    init_db()
    if os.getenv("TELEGRAM_BOT_TOKEN", "").strip():
        threading.Thread(target=telegram_loop, daemon=True, name="telegram-poller").start()
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
