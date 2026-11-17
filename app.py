import base64
import json
import logging
import os
import re
import subprocess
import threading
import time
from datetime import datetime, timezone
from apscheduler.schedulers.background import BackgroundScheduler
from automation import parse_schedule, comment_is_suitable
from pathlib import Path

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, request, Response
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from PIL import Image, ImageDraw, ImageFont
import sqlite3

from metadata import build_youtube_snippet, clean_metadata, parse_json_response
from livestream import RecordedLiveManager
import hmac

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
live_manager = RecordedLiveManager()


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
            scheduled_at TEXT,
            created_at TEXT NOT NULL
        )""")
        columns = {row["name"] for row in con.execute("PRAGMA table_info(videos)").fetchall()}
        if "scheduled_at" not in columns:
            con.execute("ALTER TABLE videos ADD COLUMN scheduled_at TEXT")
        con.execute("""CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY, value TEXT NOT NULL
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS processed_comments (
            comment_id TEXT PRIMARY KEY, replied_at TEXT NOT NULL
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS scheduled_lives (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            privacy TEXT NOT NULL,
            loop INTEGER NOT NULL,
            scheduled_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'scheduled',
            broadcast_id TEXT,
            created_at TEXT NOT NULL
        )""")
        con.execute("UPDATE scheduled_lives SET status='scheduled' WHERE status='starting'")


@app.before_request
def protect_dashboard():
    if request.path not in ("/", "/api/queue", "/api/settings"):
        return None
    password = os.getenv("DASHBOARD_PASSWORD", "").strip()
    if not password:
        return "Set DASHBOARD_PASSWORD in environment before opening the dashboard.", 503
    auth = request.authorization
    import hmac
    if not auth or not hmac.compare_digest(auth.password or "", password):
        return Response("Dashboard login required", 401,
                        {"WWW-Authenticate": 'Basic realm="YouTube AI Manager"'})
    return None


def get_setting(key, default):
    with db() as con:
        row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


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
              "Use Hindi/Hinglish where appropriate. Base suggestions on the supplied representative "
              "video frame and filename. Do not claim events or details that are not visible or stated. "
              "Filename: " + filename)
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
    parts = [{"text": prompt}]
    frame = MEDIA / ("metadata_" + Path(file_path).stem + ".jpg")
    try:
        subprocess.run(["ffmpeg", "-y", "-ss", "00:00:03", "-i", str(file_path),
                        "-frames:v", "1", str(frame)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=45)
        parts.append({"inline_data": {"mime_type": "image/jpeg",
                                      "data": base64.b64encode(frame.read_bytes()).decode("ascii")}})
    except Exception as exc:
        log.warning("Video frame analysis unavailable; using filename only: %s", exc)
    finally:
        frame.unlink(missing_ok=True)
    body = {"contents": [{"parts": parts}],
            "generationConfig": {"responseMimeType": "application/json"}}
    response = requests.post(url, json=body, timeout=90)
    response.raise_for_status()
    raw = response.json()["candidates"][0]["content"]["parts"][0]["text"]
    return clean_metadata(parse_json_response(raw))


def make_thumbnail(video_path, title, output_path):
    """Generate a video-aware AI thumbnail; fall back to a real frame with title."""
    frame = MEDIA / (Path(output_path).stem + "_frame.jpg")
    try:
        subprocess.run(["ffmpeg", "-y", "-ss", "00:00:02", "-i", str(video_path),
                        "-frames:v", "1", str(frame)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=45)
        key = os.getenv("GEMINI_API_KEY", "").strip()
        if key:
            image_bytes = frame.read_bytes()
            prompt = (
                "Create a striking YouTube thumbnail using this video frame as visual context. "
                "Make it a polished, high-contrast, professional 16:9 creator thumbnail. "
                "Keep the main subject recognizable, use bold cinematic lighting and a clean "
                "composition. Add short, correctly spelled readable text: " + title[:55] +
                ". Do not add logos, watermarks, fake people, or unrelated objects."
            )
            model = os.getenv("GEMINI_IMAGE_MODEL", "gemini-3.1-flash-image")
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
            body = {
                "contents": [{"parts": [
                    {"text": prompt},
                    {"inline_data": {"mime_type": "image/jpeg",
                                     "data": base64.b64encode(image_bytes).decode("ascii")}}
                ]}],
                "generationConfig": {"responseModalities": ["IMAGE"],
                                     "imageConfig": {"aspectRatio": "16:9"}}
            }
            response = requests.post(url, json=body, timeout=180)
            response.raise_for_status()
            parts = response.json()["candidates"][0]["content"]["parts"]
            for part in parts:
                data = part.get("inlineData") or part.get("inline_data")
                if data and data.get("data"):
                    Path(output_path).write_bytes(base64.b64decode(data["data"]))
                    Image.open(output_path).convert("RGB").resize((1280, 720)).save(output_path, quality=94)
                    frame.unlink(missing_ok=True)
                    return str(output_path)
            raise RuntimeError("Gemini image model returned no image")
    except Exception as exc:
        log.warning("AI thumbnail unavailable; using frame fallback: %s", exc)
    try:
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
        return str(output_path)
    except Exception as exc:
        log.warning("Thumbnail fallback failed: %s", exc)
        return None
    finally:
        frame.unlink(missing_ok=True)


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


def upload_video(video_id, privacy="private", publish_at=None):
    with db() as con:
        row = con.execute("SELECT * FROM videos WHERE id=?", (video_id,)).fetchone()
    if not row:
        raise ValueError("Video queue ID not found")
    service = youtube_service()
    status = {"privacyStatus": privacy, "selfDeclaredMadeForKids": False}
    if publish_at:
        status["publishAt"] = publish_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    snippet = build_youtube_snippet({
        "title": row["title"],
        "description": row["description"] or "",
        "tags": json.loads(row["tags"] or "[]"),
        "hashtags": json.loads(row["hashtags"] or "[]"),
    })
    body = {"snippet": snippet, "status": status}
    media = MediaFileUpload(row["file_path"], chunksize=-1, resumable=True)
    result = service.videos().insert(part="snippet,status", body=body, media_body=media).execute()
    yt_id = result["id"]
    thumb = row["thumbnail_path"]
    if thumb and Path(thumb).exists():
        service.thumbnails().set(videoId=yt_id, media_body=MediaFileUpload(thumb)).execute()
    with db() as con:
        final_status = "scheduled" if publish_at else "uploaded"
        con.execute("UPDATE videos SET status=?, youtube_video_id=?, scheduled_at=? WHERE id=?",
                    (final_status, yt_id, publish_at.isoformat() if publish_at else None, video_id))
    return yt_id



def generate_comment_reply(comment_text):
    if not comment_is_suitable(comment_text):
        return None
    key = os.getenv("GEMINI_API_KEY", "").strip()
    if not key:
        return "Thank you for watching! 😊"
    model = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
    prompt = ("Write one short, friendly creator reply in the same language as this YouTube comment. "
              "Do not give medical, legal, financial or safety advice. Do not ask for personal data. "
              "If the comment needs a sensitive answer, return exactly SKIP. Comment: " + comment_text[:1000])
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
    response = requests.post(url, json={"contents":[{"parts":[{"text":prompt}]}]}, timeout=45)
    response.raise_for_status()
    answer = response.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
    if answer.upper() == "SKIP" or not answer or len(answer) > 500:
        return None
    return answer


def poll_comment_replies():
    if get_setting("auto_reply_enabled", os.getenv("AUTO_REPLY_ENABLED", "true")).lower() != "true":
        return
    service = youtube_service()
    with db() as con:
        videos = con.execute("SELECT youtube_video_id FROM videos WHERE youtube_video_id IS NOT NULL AND status IN ('uploaded','scheduled')").fetchall()
    mine = service.channels().list(part="id", mine=True).execute()
    my_channel_id = (mine.get("items") or [{}])[0].get("id", "")
    for video in videos:
        try:
            result = service.commentThreads().list(part="snippet", videoId=video["youtube_video_id"],
                                                   maxResults=20, order="time").execute()
            for thread in result.get("items", []):
                top = thread["snippet"]["topLevelComment"]
                comment_id = top["id"]
                comment = top["snippet"].get("textOriginal", top["snippet"].get("textDisplay", ""))
                author_id = top["snippet"].get("authorChannelId", {}).get("value", "")
                if author_id and author_id == my_channel_id:
                    with db() as con:
                        con.execute("INSERT OR IGNORE INTO processed_comments VALUES (?,?)",
                                    (comment_id, datetime.now(timezone.utc).isoformat()))
                    continue
                with db() as con:
                    done = con.execute("SELECT 1 FROM processed_comments WHERE comment_id=?", (comment_id,)).fetchone()
                if done:
                    continue
                if not comment_is_suitable(comment):
                    with db() as con:
                        con.execute("INSERT OR IGNORE INTO processed_comments VALUES (?,?)",
                                    (comment_id, datetime.now(timezone.utc).isoformat()))
                    continue
                reply = generate_comment_reply(comment)
                if not reply:
                    continue
                service.comments().insert(part="snippet", body={"snippet":{"parentId":comment_id,
                                                                           "textOriginal":reply}}).execute()
                with db() as con:
                    con.execute("INSERT OR IGNORE INTO processed_comments VALUES (?,?)",
                                (comment_id, datetime.now(timezone.utc).isoformat()))
        except Exception:
            log.exception("Comment reply check failed for video %s", video["youtube_video_id"])


def schedule_video_upload(video_id, local_time):
    scheduled = parse_schedule(local_time, os.getenv("TIMEZONE", "Asia/Kolkata"))
    yt_id = upload_video(video_id, "private", scheduled)
    return yt_id, scheduled

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
    send_message(chat_id, f"✅ Video ready!\nID: {item_id}\\nTitle: {metadata['title']}\\nAI thumbnail: {'ready' if thumb_path else 'fallback unavailable'}")
    auto_publish = get_setting("auto_publish_enabled", os.getenv("AUTO_UPLOAD_DEFAULT", "true")).lower() == "true"
    if auto_publish:
        privacy = get_setting("upload_privacy", os.getenv("AUTO_UPLOAD_PRIVACY", "public")).lower()
        if privacy not in ("public", "unlisted", "private"):
            privacy = "public"
        try:
            yt_id = upload_video(item_id, privacy)
            send_message(chat_id, f"🚀 Auto upload complete ({privacy})!\nhttps://youtu.be/{yt_id}")
        except Exception as exc:
            log.exception("Automatic YouTube upload failed")
            send_message(chat_id, f"⚠️ AI processing complete, lekin upload fail hua. Queue ID {item_id}. Error: {exc}\\nYouTube connection/API settings check karo.")
    else:
        send_message(chat_id, f"Auto upload OFF hai. Publish ke liye /publish {item_id} bhejo.")


def telegram_loop():
    offset = None
    retry_delay = 5

    while not stop_event.is_set():
        try:
            params = {"timeout": 25}
            if offset is not None:
                params["offset"] = offset

            token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
            if not token:
                stop_event.wait(5)
                continue

            res = requests.get(
                f"https://api.telegram.org/bot{token}/getUpdates",
                params=params,
                timeout=(10, 35),
            )
            res.raise_for_status()
            payload = res.json()
            if not payload.get("ok"):
                raise RuntimeError("Telegram getUpdates returned ok=false")

            # A successful poll resets the backoff.
            retry_delay = 5

            for update in payload.get("result", []):
                offset = update["update_id"] + 1
                message = update.get("message", {})
                text = (message.get("text") or "").strip()
                chat_id = message.get("chat", {}).get("id")
                if not chat_id:
                    continue

                if text.startswith("/start"):
                    send_message(chat_id, "YouTube AI Manager ready. Video bhejo. Commands: /queue, /autopublish on|off, /privacy public|unlisted|private, /publish ID, /schedule ID YYYY-MM-DD HH:MM")
                elif text.startswith("/autopublish") and owner_allowed(message.get("from", {}).get("id")):
                    value = text.split(maxsplit=1)[1].strip().lower() if len(text.split(maxsplit=1)) > 1 else ""
                    if value not in ("on", "off"):
                        current = get_setting("auto_publish_enabled", os.getenv("AUTO_UPLOAD_DEFAULT", "true"))
                        send_message(chat_id, f"Auto publish: {current}. Use /autopublish on or /autopublish off")
                    else:
                        with db() as con:
                            con.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('auto_publish_enabled',?)",
                                        ("true" if value == "on" else "false",))
                        send_message(chat_id, f"Auto publish {value.upper()}.")
                elif text.startswith("/privacy") and owner_allowed(message.get("from", {}).get("id")):
                    value = text.split(maxsplit=1)[1].strip().lower() if len(text.split(maxsplit=1)) > 1 else ""
                    if value not in ("public", "unlisted", "private"):
                        current = get_setting("upload_privacy", os.getenv("AUTO_UPLOAD_PRIVACY", "public"))
                        send_message(chat_id, f"Upload privacy: {current}. Use /privacy public, /privacy unlisted, or /privacy private")
                    else:
                        with db() as con:
                            con.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('upload_privacy',?)", (value,))
                        send_message(chat_id, f"Default upload privacy set to {value}.")
                elif text.startswith("/queue") and owner_allowed(message.get("from", {}).get("id")):
                    with db() as con:
                        rows = con.execute("SELECT id,title,status FROM videos ORDER BY id DESC LIMIT 10").fetchall()
                    send_message(chat_id, "\n".join([f"#{r['id']} [{r['status']}] {r['title']}" for r in rows]) or "Queue empty.")
                elif text.startswith("/schedule") and owner_allowed(message.get("from", {}).get("id")):
                    match = re.search(r"/schedule\s+(\d+)\s+(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})", text)
                    if not match:
                        send_message(chat_id, "Use: /schedule 1 2026-11-20 19:30 (India time)")
                    else:
                        try:
                            yt_id, scheduled = schedule_video_upload(int(match.group(1)), match.group(2))
                            send_message(chat_id, f"Scheduled: {scheduled.strftime('%d-%m-%Y %I:%M %p')} UTC. Video: https://youtu.be/{yt_id}")
                        except Exception as exc:
                            log.exception("Schedule upload failed")
                            send_message(chat_id, f"Schedule failed: {exc}")
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
                elif message.get("video") or message.get("document", {}).get("mime_type", "").startswith("video/"):
                    try:
                        handle_video(message)
                    except Exception as exc:
                        log.exception("Telegram video handling failed")
                        send_message(chat_id, f"Video process nahi hua: {exc}")

        except requests.exceptions.RequestException:
            log.exception("Telegram network error; retrying in %s seconds", retry_delay)
            stop_event.wait(retry_delay)
            retry_delay = min(retry_delay * 2, 60)
        except Exception:
            log.exception("Telegram polling/handler error")
            stop_event.wait(5)


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
    <p>Telegram video intake · AI metadata · Scheduled publishing · Automatic comment replies</p><p><a href='/oauth/start'>Connect YouTube channel</a> · <a href='/api/queue'>Queue JSON</a></p>
    <section><h2>Automation settings</h2><label><input id='autoReply' type='checkbox' {'checked' if get_setting("auto_reply_enabled", os.getenv("AUTO_REPLY_ENABLED","true")).lower()=="true" else ''}> Automatic comment replies</label><button onclick='saveSettings()'>Save settings</button><p id='saveStatus'></p></section>
    <section><h2>Recent videos</h2><table><tr><th>ID</th><th>Title</th><th>Status</th><th>YouTube ID</th></tr>{items}</table></section>
    <section><h2>Owner controls</h2>
    <label>Manager API token <input id='managerToken' type='password' placeholder='Set MANAGER_API_TOKEN in .env'></label>
    <p><label>Video queue ID <input id='videoId' type='number' min='1'></label></p>
    <p><label>Publish date/time (India) <input id='publishAt' type='datetime-local'></label>
    <button onclick='scheduleVideo()'>Schedule upload</button></p>
    <p><label>Live title <input id='liveTitle' placeholder='Live stream title'></label>
    <select id='livePrivacy'><option>private</option><option>unlisted</option><option>public</option></select>
    <label><input id='liveLoop' type='checkbox' checked> Loop recorded video continuously</label>
    <label>Start live at (India time) <input id='liveAt' type='datetime-local'></label>
    <button onclick='scheduleLive()'>Schedule recorded live</button>
    <button onclick='startLive()'>Start recorded live now</button>
    <button onclick='stopLive()'>Stop live</button>
    <button onclick='liveStatus()'>Live status</button></p><pre id='controlStatus'>Ready</pre></section>
    <script>
    async function managerApi(path,body){{const token=document.getElementById('managerToken').value;
      const options={{method:body?'POST':'GET',headers:{{'X-Manager-Token':token,'Content-Type':'application/json'}}}};
      if(body)options.body=JSON.stringify(body);const r=await fetch(path,options);const d=await r.json();
      document.getElementById('controlStatus').textContent=JSON.stringify(d,null,2);if(!r.ok)throw Error(d.error||'Request failed');return d;}}
    function videoId(){{return Number(document.getElementById('videoId').value);}}
    async function scheduleVideo(){{try{{const t=document.getElementById('publishAt').value;if(!videoId()||!t)throw Error('Video ID and time required');
      await managerApi('/api/schedule',{{video_id:videoId(),publish_at:t.replace('T',' ')}});}}catch(e){{document.getElementById('controlStatus').textContent=e.message;}}}}
    async function scheduleLive(){{try{{const t=document.getElementById('liveAt').value;if(!videoId()||!t)throw Error('Video ID and live time required');
      await managerApi('/api/live/schedule',{{video_id:videoId(),scheduled_at:t.replace('T',' '),title:document.getElementById('liveTitle').value,
      privacy:document.getElementById('livePrivacy').value,loop:document.getElementById('liveLoop').checked}});}}
      catch(e){{document.getElementById('controlStatus').textContent=e.message;}}}}
    async function startLive(){{try{{if(!videoId())throw Error('Video ID required');await managerApi('/api/live/start',{{video_id:videoId(),
      title:document.getElementById('liveTitle').value,privacy:document.getElementById('livePrivacy').value,loop:document.getElementById('liveLoop').checked}});}}
      catch(e){{document.getElementById('controlStatus').textContent=e.message;}}}}
    async function stopLive(){{try{{await managerApi('/api/live/stop',{{}});}}catch(e){{document.getElementById('controlStatus').textContent=e.message;}}}}
    async function liveStatus(){{try{{await managerApi('/api/live/status');}}catch(e){{document.getElementById('controlStatus').textContent=e.message;}}}}
    async function saveSettings(){{const enabled=document.getElementById('autoReply').checked;const r=await fetch('/api/settings',{{method:'POST',headers:{{'Content-Type':'application/json','X-Manager-Token':document.getElementById('managerToken').value}},body:JSON.stringify({{auto_reply_enabled:enabled}})}});document.getElementById('saveStatus').textContent=r.ok?'Settings saved':'Save failed';}}</script></main></body></html>"""


@app.get("/api/queue")
def queue_api():
    with db() as con:
        rows = con.execute("SELECT id,title,status,youtube_video_id,created_at FROM videos ORDER BY id DESC LIMIT 100").fetchall()
    return jsonify([dict(row) for row in rows])



@app.route("/api/settings", methods=["GET", "POST"])
def settings_api():
    if request.method == "POST":
        if not manager_api_authorized():
            return jsonify({"error": "Unauthorized"}), 401
        data = request.get_json(silent=True) or {}
        if "auto_reply_enabled" not in data or not isinstance(data["auto_reply_enabled"], bool):
            return jsonify({"error": "auto_reply_enabled boolean required"}), 400
        with db() as con:
            con.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('auto_reply_enabled',?)",
                        ("true" if data["auto_reply_enabled"] else "false",))
    return jsonify({"auto_reply_enabled": get_setting(
        "auto_reply_enabled", os.getenv("AUTO_REPLY_ENABLED", "true")) == "true"})


@app.get("/oauth/start")
def oauth_start():
    secrets = os.getenv("YOUTUBE_CLIENT_SECRETS_FILE", "client_secret.json")
    if not Path(secrets).exists():
        return "Upload Google OAuth client_secret.json and set YOUTUBE_CLIENT_SECRETS_FILE.", 400
    flow = Flow.from_client_secrets_file(secrets, scopes=SCOPES, redirect_uri=os.getenv("RENDER_EXTERNAL_URL", request.url_root).rstrip("/") + "/oauth/callback")
    auth_url, state = flow.authorization_url(access_type="offline", include_granted_scopes="true", prompt="consent")
    app.config["oauth_state"] = state
    return redirect(auth_url)


@app.get("/oauth/callback")
def oauth_callback():
    secrets = os.getenv("YOUTUBE_CLIENT_SECRETS_FILE", "client_secret.json")
    flow = Flow.from_client_secrets_file(secrets, scopes=SCOPES, state=app.config.get("oauth_state"),
                                         redirect_uri=os.getenv("RENDER_EXTERNAL_URL", request.url_root).rstrip("/") + "/oauth/callback")
    # OAuth permits plain HTTP only for local development callbacks.
    # Never enable insecure OAuth transport for public/hosted domains.
    local_hosts = {"127.0.0.1", "localhost", "::1"}
    if request.scheme == "http" and request.host.split(":")[0].strip("[]") in local_hosts:
        os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
    external_base = os.getenv("RENDER_EXTERNAL_URL", request.url_root).rstrip("/")
    callback_url = external_base + request.full_path
    flow.fetch_token(authorization_response=callback_url)
    Path(os.getenv("YOUTUBE_TOKEN_FILE", "token.json")).write_text(flow.credentials.to_json(), encoding="utf-8")
    return "YouTube channel connected. You can close this page and send a video to your Telegram bot."



def manager_api_authorized():
    expected = os.getenv("MANAGER_API_TOKEN", "").strip()
    supplied = request.headers.get("X-Manager-Token", "")
    return bool(expected) and hmac.compare_digest(supplied, expected)


@app.post("/api/schedule")
def schedule_api():
    if not manager_api_authorized():
        return jsonify({"error": "Unauthorized. Configure MANAGER_API_TOKEN and send X-Manager-Token."}), 401
    payload = request.get_json(silent=True) or {}
    try:
        video_id = int(payload.get("video_id", 0))
        local_time = str(payload.get("publish_at", "")).strip()
        scheduled = parse_schedule(local_time, os.getenv("TIMEZONE", "Asia/Kolkata"))
        yt_id, scheduled = schedule_video_upload(video_id, local_time)
        return jsonify({"ok": True, "video_id": video_id, "youtube_video_id": yt_id,
                        "publish_at_utc": scheduled.isoformat(),
                        "timezone": os.getenv("TIMEZONE", "Asia/Kolkata")}), 201
    except (ValueError, RuntimeError) as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        log.exception("Dashboard scheduling failed")
        return jsonify({"error": str(exc)}), 500



@app.post("/api/live/schedule")
def live_schedule_api():
    if not manager_api_authorized():
        return jsonify({"error": "Unauthorized"}), 401
    payload = request.get_json(silent=True) or {}
    try:
        video_id = int(payload.get("video_id", 0))
        title = str(payload.get("title") or "").strip()[:100] or "Recorded live stream"
        privacy = str(payload.get("privacy", "private"))
        loop = bool(payload.get("loop", True))
        if privacy not in ("private", "unlisted", "public"):
            raise ValueError("Invalid live privacy setting")
        with db() as con:
            row = con.execute("SELECT file_path FROM videos WHERE id=?", (video_id,)).fetchone()
            if not row or not Path(row["file_path"]).is_file():
                return jsonify({"error": "Video queue item or its file was not found"}), 404
        scheduled = parse_schedule(str(payload.get("scheduled_at", "")), os.getenv("TIMEZONE", "Asia/Kolkata"))
        with db() as con:
            cur = con.execute(
                """INSERT INTO scheduled_lives(video_id,title,privacy,loop,scheduled_at,status,created_at)
                   VALUES(?,?,?,?,?,'scheduled',?)""",
                (video_id, title, privacy, int(loop), scheduled.isoformat(), datetime.now(timezone.utc).isoformat())
            )
        return jsonify({"ok": True, "schedule_id": cur.lastrowid, "scheduled_at_utc": scheduled.isoformat(),
                        "timezone": os.getenv("TIMEZONE", "Asia/Kolkata")}), 201
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


def process_scheduled_lives():
    if live_manager.status()["running"]:
        return
    now = datetime.now(timezone.utc).isoformat()
    with db() as con:
        job = con.execute(
            "SELECT * FROM scheduled_lives WHERE status='scheduled' AND scheduled_at<=? ORDER BY scheduled_at LIMIT 1",
            (now,)
        ).fetchone()
        if not job:
            return
        con.execute("UPDATE scheduled_lives SET status='starting' WHERE id=?", (job["id"],))
        video = con.execute("SELECT file_path FROM videos WHERE id=?", (job["video_id"],)).fetchone()
    try:
        if not video or not Path(video["file_path"]).is_file():
            raise FileNotFoundError("Scheduled live video file is missing")
        result = live_manager.start(youtube_service(), video["file_path"], job["title"],
                                    privacy=job["privacy"], loop=bool(job["loop"]))
        with db() as con:
            con.execute("UPDATE scheduled_lives SET status='running',broadcast_id=? WHERE id=?",
                        (result["broadcast_id"], job["id"]))
    except Exception as exc:
        log.exception("Scheduled live start failed")
        with db() as con:
            con.execute("UPDATE scheduled_lives SET status='failed' WHERE id=?", (job["id"],))


@app.post("/api/live/start")
def live_start_api():
    if not manager_api_authorized():
        return jsonify({"error": "Unauthorized"}), 401
    payload = request.get_json(silent=True) or {}
    try:
        video_id = int(payload.get("video_id", 0))
        with db() as con:
            row = con.execute("SELECT * FROM videos WHERE id=?", (video_id,)).fetchone()
        if not row:
            return jsonify({"error": "Video queue ID not found"}), 404
        result = live_manager.start(
            youtube_service(), row["file_path"], payload.get("title") or row["title"] or "Live stream",
            privacy=payload.get("privacy", "private"),
            loop=bool(payload.get("loop", True)),
        )
        return jsonify({"ok": True, **result, **live_manager.status()}), 201
    except (ValueError, RuntimeError, FileNotFoundError) as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        log.exception("Recorded live start failed")
        return jsonify({"error": str(exc)}), 500


@app.get("/api/live/status")
def live_status_api():
    if not manager_api_authorized():
        return jsonify({"error": "Unauthorized"}), 401
    return jsonify(live_manager.status())


@app.post("/api/live/stop")
def live_stop_api():
    if not manager_api_authorized():
        return jsonify({"error": "Unauthorized"}), 401
    try:
        result = live_manager.stop(youtube_service())
        return jsonify({"ok": True, **result})
    except Exception as exc:
        log.exception("Recorded live stop failed")
        return jsonify({"error": str(exc)}), 500


def main():
    init_db()
    if os.getenv("TELEGRAM_BOT_TOKEN", "").strip():
        threading.Thread(target=telegram_loop, daemon=True, name="telegram-poller").start()
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(poll_comment_replies, "interval", minutes=2, id="comment-replies",
                      max_instances=1, coalesce=True)
    scheduler.add_job(process_scheduled_lives, "interval", seconds=30, id="scheduled-lives",
                      max_instances=1, coalesce=True)
    scheduler.start()
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
