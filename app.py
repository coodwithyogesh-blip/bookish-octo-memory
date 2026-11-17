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
from PIL import Image, ImageDraw, ImageFont, ImageOps
from database import db, init_db
from cloud_storage import configured as storage_configured, persist as persist_file, materialize as materialize_file

from metadata import build_youtube_snippet, clean_metadata, parse_json_response
from comment_manager import parse_reply_command, parse_comment_action, comment_notification
from telegram_groups import is_authorized_user, is_allowed_group, parse_telegram_id
from livestream import RecordedLiveManager
import hmac

load_dotenv()
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("youtube-ai-manager")
app = Flask(__name__)
app.secret_key = os.getenv("APP_SECRET", "change-me-before-deploy")
MEDIA = Path(os.getenv("MEDIA_DIR", "media"))
MEDIA.mkdir(parents=True, exist_ok=True)
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.force-ssl",
]
stop_event = threading.Event()
live_manager = RecordedLiveManager()


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

def configured_group_id():
    return get_setting("telegram_group_id", os.getenv("TELEGRAM_GROUP_ID", os.getenv("TELEGRAM_CHAT_ID", "")))


def team_user_ids():
    with db() as con:
        rows = con.execute("SELECT key FROM settings WHERE key LIKE 'team_user:%' AND value='true'").fetchall()
    return [row["key"].split(":", 1)[1] for row in rows]


def team_allowed(user_id):
    return is_authorized_user(user_id, os.getenv("OWNER_TELEGRAM_ID", ""), team_user_ids())


def message_authorized(message):
    chat = message.get("chat", {})
    chat_type = chat.get("type", "")
    chat_id = str(chat.get("id", ""))
    user_id = message.get("from", {}).get("id")
    if chat_type == "private":
        return owner_allowed(user_id)
    return (chat_type in ("group", "supergroup")
            and is_allowed_group(chat_id, configured_group_id())
            and team_allowed(user_id))


def owner_group_command(message):
    return owner_allowed(message.get("from", {}).get("id"))


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


def send_message(chat_id, text, **extra):
    payload = {"chat_id": chat_id, "text": text}
    payload.update(extra)
    return telegram_call("sendMessage", payload)


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


def token_path():
    return Path(os.getenv("YOUTUBE_TOKEN_FILE", str(MEDIA / "token.json")))


def restore_youtube_token():
    path = token_path()
    if not path.exists() and storage_configured():
        materialize_file("supabase://" + os.getenv("SUPABASE_STORAGE_BUCKET", "youtube-media") + "/oauth/token.json", path)
    return path


def save_youtube_token(credentials):
    path = token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(credentials.to_json(), encoding="utf-8")
    if storage_configured():
        persist_file(path, "oauth/token.json")


def youtube_service():
    path = restore_youtube_token()
    creds = Credentials.from_authorized_user_file(str(path), SCOPES) if path.exists() else None
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(GoogleRequest())
        save_youtube_token(creds)
    if not creds or not creds.valid:
        raise RuntimeError("YouTube not connected. Open /oauth/start first.")
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def upload_video(video_id, privacy="private", publish_at=None):
    with db() as con:
        row = con.execute("SELECT * FROM videos WHERE id=?", (video_id,)).fetchone()
    if not row:
        raise ValueError("Video queue ID not found")
    if row["status"] == "awaiting_thumbnail" or not row["thumbnail_path"]:
        raise ValueError("Custom thumbnail pending. Reply with a thumbnail photo before publishing.")
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
    video_local = materialize_file(row["file_path"], MEDIA / f"publish_{video_id}.mp4")
    media = MediaFileUpload(video_local, chunksize=-1, resumable=True)
    result = service.videos().insert(part="snippet,status", body=body, media_body=media).execute()
    yt_id = result["id"]
    with db() as con:
        final_status = "scheduled" if publish_at else "uploaded"
        con.execute("UPDATE videos SET status=?, youtube_video_id=?, scheduled_at=? WHERE id=?",
                    (final_status, yt_id, publish_at.isoformat() if publish_at else None, video_id))
    thumb = row["thumbnail_path"]
    if thumb:
        try:
            thumb_local = materialize_file(thumb, MEDIA / f"publish_thumb_{video_id}.jpg")
            service.thumbnails().set(videoId=yt_id, media_body=MediaFileUpload(thumb_local)).execute()
        except Exception:
            # Do not retry the whole video upload if YouTube temporarily rejects the thumbnail.
            log.exception("Video uploaded, but custom thumbnail could not be applied yet")
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


def publish_comment_reply(comment_id, reply_text):
    if not reply_text or len(reply_text.strip()) > 10000:
        raise ValueError("Reply 1-10000 characters ka hona chahiye.")
    with db() as con:
        row = con.execute("SELECT status FROM comment_queue WHERE comment_id=?", (comment_id,)).fetchone()
    if not row:
        raise ValueError("Comment ID nahi mila.")
    if row["status"] != "pending":
        raise ValueError("Ye comment ab pending nahi hai.")
    youtube_service().comments().insert(
        part="snippet",
        body={"snippet": {"parentId": comment_id, "textOriginal": reply_text.strip()}}
    ).execute()
    now = datetime.now(timezone.utc).isoformat()
    with db() as con:
        con.execute("UPDATE comment_queue SET status='replied',reply_text=?,replied_at=? WHERE comment_id=? AND status='pending'",
                    (reply_text.strip(), now, comment_id))
        con.execute("INSERT OR IGNORE INTO processed_comments VALUES (?,?)", (comment_id, now))
    return True


def skip_comment(comment_id):
    with db() as con:
        cur = con.execute("UPDATE comment_queue SET status='skipped' WHERE comment_id=? AND status='pending'",
                          (comment_id,))
    return cur.rowcount > 0


def delete_youtube_comment(comment_id):
    with db() as con:
        row = con.execute("SELECT status FROM comment_queue WHERE comment_id=?", (comment_id,)).fetchone()
    if not row:
        raise ValueError("Comment ID nahi mila.")
    youtube_service().comments().delete(id=comment_id).execute()
    with db() as con:
        con.execute("UPDATE comment_queue SET status='deleted' WHERE comment_id=?", (comment_id,))
    return True


def notify_new_comment(comment_id, video_title, author, comment, suggestion):
    group_id = configured_group_id()
    if not group_id:
        log.warning("Comment %s pending but Telegram group is not bound", comment_id)
        return
    message, keyboard = comment_notification(comment_id, video_title, author, comment, suggestion)
    send_message(group_id, message, reply_markup=keyboard)


def handle_comment_callback(callback):
    callback_id = callback.get("id")
    actor_message = callback.get("message") or {}
    actor = {**actor_message, "from": callback.get("from") or {}}
    data = parse_comment_action(callback.get("data"))
    if not message_authorized(actor) or not data:
        telegram_call("answerCallbackQuery", {"callback_query_id": callback_id,
                                               "text": "Not authorized or invalid action", "show_alert": True})
        return
    action, comment_id = data
    try:
        if action == "approve":
            with db() as con:
                row = con.execute("SELECT suggested_reply FROM comment_queue WHERE comment_id=? AND status='pending'",
                                  (comment_id,)).fetchone()
            if not row or not row["suggested_reply"]:
                raise ValueError("AI reply available nahi hai. /reply COMMENT_ID your text use karo.")
            publish_comment_reply(comment_id, row["suggested_reply"])
            send_message(actor_message.get("chat", {}).get("id"), f"✅ YouTube comment ka reply publish ho gaya. ID: {comment_id}")
        else:
            if not skip_comment(comment_id):
                raise ValueError("Comment pending nahi hai ya pehle process ho chuka hai.")
            send_message(actor_message.get("chat", {}).get("id"), f"⏭ Comment skip kar diya. ID: {comment_id}")
        telegram_call("answerCallbackQuery", {"callback_query_id": callback_id, "text": "Done"})
    except Exception as exc:
        telegram_call("answerCallbackQuery", {"callback_query_id": callback_id, "text": str(exc)[:180], "show_alert": True})


def poll_comment_replies():
    service = youtube_service()
    with db() as con:
        videos = con.execute("SELECT youtube_video_id,title FROM videos WHERE youtube_video_id IS NOT NULL AND status IN ('uploaded','scheduled')").fetchall()
    mine = service.channels().list(part="id", mine=True).execute()
    my_channel_id = (mine.get("items") or [{}])[0].get("id", "")
    auto_reply = get_setting("auto_reply_enabled", os.getenv("AUTO_REPLY_ENABLED", "false")).lower() == "true"
    for video in videos:
        try:
            result = service.commentThreads().list(part="snippet", videoId=video["youtube_video_id"],
                                                   maxResults=100, order="time").execute()
            for thread in result.get("items", []):
                top = thread["snippet"]["topLevelComment"]
                comment_id = top["id"]
                comment = top["snippet"].get("textOriginal", top["snippet"].get("textDisplay", ""))
                author = top["snippet"].get("authorDisplayName", "Viewer")
                author_id = top["snippet"].get("authorChannelId", {}).get("value", "")
                if author_id and author_id == my_channel_id:
                    continue
                with db() as con:
                    exists = con.execute("SELECT 1 FROM comment_queue WHERE comment_id=?", (comment_id,)).fetchone()
                    already_done = con.execute("SELECT 1 FROM processed_comments WHERE comment_id=?", (comment_id,)).fetchone()
                if exists or already_done:
                    continue
                suggestion = generate_comment_reply(comment) if comment_is_suitable(comment) else None
                if auto_reply and suggestion:
                    try:
                        service.comments().insert(part="snippet", body={"snippet":{"parentId":comment_id,
                                                                                   "textOriginal":suggestion}}).execute()
                        status, replied_at, reply_text = "replied", datetime.now(timezone.utc).isoformat(), suggestion
                        with db() as con:
                            con.execute("""INSERT OR IGNORE INTO comment_queue
                                (comment_id,video_id,video_title,author,comment_text,suggested_reply,reply_text,status,created_at,replied_at)
                                VALUES(?,?,?,?,?,?,?,?,?,?)""",
                                (comment_id, video["youtube_video_id"], video["title"], author, comment,
                                 suggestion, reply_text, status, datetime.now(timezone.utc).isoformat(), replied_at))
                            con.execute("INSERT OR IGNORE INTO processed_comments VALUES (?,?)", (comment_id, replied_at))
                        continue
                    except Exception:
                        log.exception("Automatic reply failed for comment %s; sending for manual review", comment_id)
                with db() as con:
                    cur = con.execute("""INSERT OR IGNORE INTO comment_queue
                        (comment_id,video_id,video_title,author,comment_text,suggested_reply,status,created_at)
                        VALUES(?,?,?,?,?,?, 'pending', ?)""",
                        (comment_id, video["youtube_video_id"], video["title"], author, comment,
                         suggestion, datetime.now(timezone.utc).isoformat()))
                if cur.rowcount:
                    try:
                        notify_new_comment(comment_id, video["title"], author, comment, suggestion)
                    except Exception:
                        log.exception("Could not send Telegram notification for comment %s", comment_id)
        except Exception:
            log.exception("Comment check failed for video %s", video["youtube_video_id"])


def schedule_video_upload(video_id, local_time):
    scheduled = parse_schedule(local_time, os.getenv("TIMEZONE", "Asia/Kolkata"))
    yt_id = upload_video(video_id, "private", scheduled)
    return yt_id, scheduled

def handle_video(message):
    user = message.get("from", {})
    chat_id = message.get("chat", {}).get("id")
    if not message_authorized(message):
        send_message(chat_id, "⛔ Is group ke liye tum authorized nahi ho. Owner se access lo.")
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
    safe_file_id = re.sub(r"[^A-Za-z0-9_-]", "_", file_id)
    remote_video = persist_file(target, f"videos/{safe_file_id}{suffix}") if storage_configured() else str(target)
    with db() as con:
        cur = con.execute("""INSERT INTO videos
            (telegram_file_id,file_path,original_name,title,description,tags,hashtags,thumbnail_path,status,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (file_id,remote_video,name,metadata["title"],metadata["description"],json.dumps(metadata["tags"]),
             json.dumps(metadata["hashtags"]),None,"awaiting_thumbnail",datetime.now(timezone.utc).isoformat()))
        item_id = cur.lastrowid
    prompt = send_message(
        chat_id,
        f"🎬 Video #{item_id} AI analysis ready!\nTitle: {metadata['title']}\n\nAb apna custom thumbnail PHOTO ke roop mein is message ka reply karke bhejo. Thumbnail milte hi bot upload karega.",
        reply_markup={"force_reply": True, "selective": True},
    )
    with db() as con:
        con.execute("INSERT OR REPLACE INTO pending_thumbnails(prompt_message_id,chat_id,video_id,created_at) VALUES(?,?,?,?)",
                    (prompt["message_id"], str(chat_id), item_id, datetime.now(timezone.utc).isoformat()))



def handle_thumbnail(message):
    if not message_authorized(message):
        return
    reply = message.get("reply_to_message") or {}
    prompt_id = reply.get("message_id")
    chat_id = str(message.get("chat", {}).get("id", ""))
    with db() as con:
        pending = con.execute(
            "SELECT video_id FROM pending_thumbnails WHERE prompt_message_id=? AND chat_id=?",
            (prompt_id, chat_id)
        ).fetchone()
    if not pending:
        send_message(chat_id, "Thumbnail attach karne ke liye bot ke thumbnail prompt ka reply karo, ya /thumbnail VIDEO_ID use karo.")
        return
    photo = message.get("photo") or []
    document = message.get("document") or {}
    file_id = photo[-1]["file_id"] if photo else (
        document.get("file_id") if document.get("mime_type", "").startswith("image/") else None
    )
    if not file_id:
        send_message(chat_id, "Please JPG/PNG image photo ke roop mein bhejo.")
        return
    info = telegram_call("getFile", {"file_id": file_id})
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    url = f"https://api.telegram.org/file/bot{token}/{info['file_path']}"
    response = requests.get(url, timeout=90)
    response.raise_for_status()
    raw = MEDIA / f"thumb_upload_{pending['video_id']}_source"
    raw.write_bytes(response.content)
    thumb_path = MEDIA / f"thumb_custom_{pending['video_id']}.jpg"
    try:
        with Image.open(raw) as source:
            ImageOps.fit(source.convert("RGB"), (1280, 720), method=Image.Resampling.LANCZOS).save(thumb_path, "JPEG", quality=94)
    finally:
        raw.unlink(missing_ok=True)
    remote_thumb = persist_file(thumb_path, f"thumbnails/video_{pending['video_id']}.jpg") if storage_configured() else str(thumb_path)
    with db() as con:
        con.execute("UPDATE videos SET thumbnail_path=?, status=CASE WHEN youtube_video_id IS NULL THEN 'queued' ELSE status END WHERE id=?",
                    (remote_thumb, pending["video_id"]))
        con.execute("DELETE FROM pending_thumbnails WHERE prompt_message_id=?", (prompt_id,))
        saved_video = con.execute("SELECT youtube_video_id FROM videos WHERE id=?", (pending["video_id"],)).fetchone()
    if saved_video and saved_video["youtube_video_id"]:
        thumb_local = materialize_file(remote_thumb, MEDIA / f"existing_thumb_{pending['video_id']}.jpg")
        youtube_service().thumbnails().set(
            videoId=saved_video["youtube_video_id"],
            media_body=MediaFileUpload(thumb_local)
        ).execute()
        send_message(chat_id, f"✅ Existing YouTube video #{pending['video_id']} ka thumbnail update ho gaya.")
        return
    send_message(chat_id, f"🖼 Custom thumbnail attached to video #{pending['video_id']}.")
    auto_publish = get_setting("auto_publish_enabled", os.getenv("AUTO_UPLOAD_DEFAULT", "false")).lower() == "true"
    if auto_publish:
        privacy = get_setting("upload_privacy", os.getenv("AUTO_UPLOAD_PRIVACY", "private")).lower()
        if privacy not in ("public", "unlisted", "private"):
            privacy = "public"
        try:
            yt_id = upload_video(pending["video_id"], privacy)
            send_message(chat_id, f"🚀 Published ({privacy}) with your thumbnail!\nhttps://youtu.be/{yt_id}")
        except Exception as exc:
            log.exception("Auto publish after thumbnail failed")
            send_message(chat_id, f"⚠️ Thumbnail saved, upload failed. Use /publish {pending['video_id']} after checking YouTube connection.\\n{exc}")
    else:
        send_message(chat_id, f"Thumbnail saved. Use /publish {pending['video_id']} when ready.")


def process_telegram_update(update):
    callback = update.get("callback_query")
    if callback:
        try:
            handle_comment_callback(callback)
        except Exception:
            log.exception("Comment callback failed")
        continue
    message = update.get("message", {})
    text = (message.get("text") or "").strip()
    chat_id = message.get("chat", {}).get("id")
    if not chat_id:
        continue

    if text.startswith("/whoami"):
        send_message(chat_id, f"Your Telegram user ID: {message.get('from', {}).get('id')}")
    elif text.startswith("/bindgroup") and owner_group_command(message):
        if message.get("chat", {}).get("type") not in ("group", "supergroup"):
            send_message(chat_id, "Ye command apne Telegram group mein bhejo.")
        else:
            with db() as con:
                con.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('telegram_group_id',?)", (str(chat_id),))
            send_message(chat_id, f"✅ Ye group YouTube Studio group se connect ho gaya.\nGroup ID: {chat_id}\nAb /allow USER_ID se team add karo.")
    elif text.startswith("/allow") and owner_group_command(message):
        match = re.search(r"/allow(?:@\w+)?\s+(-?\d+)", text)
        if not match:
            send_message(chat_id, "Use: /allow TELEGRAM_USER_ID (user ko pehle /whoami bhejne bolo)")
        else:
            user_id = parse_telegram_id(match.group(1))
            with db() as con:
                con.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?, 'true')", (f"team_user:{user_id}",))
            send_message(chat_id, f"✅ Team member {user_id} ko access mil gaya.")
    elif text.startswith("/revoke") and owner_group_command(message):
        match = re.search(r"/revoke(?:@\w+)?\s+(-?\d+)", text)
        if not match:
            send_message(chat_id, "Use: /revoke TELEGRAM_USER_ID")
        else:
            user_id = parse_telegram_id(match.group(1))
            with db() as con:
                con.execute("DELETE FROM settings WHERE key=?", (f"team_user:{user_id}",))
            send_message(chat_id, f"Access removed for {user_id}.")
    elif text.startswith("/team") and message_authorized(message):
        send_message(chat_id, "Authorized team IDs: " + (", ".join(team_user_ids()) or "No team members yet."))
    elif text.startswith("/comments") and message_authorized(message):
        with db() as con:
            rows = con.execute("SELECT comment_id,video_title,author,comment_text FROM comment_queue WHERE status='pending' ORDER BY created_at DESC LIMIT 10").fetchall()
        if not rows:
            send_message(chat_id, "🎉 Koi pending comment nahi hai.")
        else:
            lines = [f"💬 {r['comment_id']} · {r['video_title'] or 'Video'}\n{r['author'] or 'Viewer'}: {r['comment_text'][:300]}" for r in rows]
            send_message(chat_id, "Pending comments:\n\n" + "\n\n".join(lines))
    elif text.startswith("/reply") and message_authorized(message):
        try:
            comment_id, reply_text = parse_reply_command(text)
            publish_comment_reply(comment_id, reply_text)
            send_message(chat_id, f"✅ Reply YouTube par publish ho gaya. ID: {comment_id}")
        except Exception as exc:
            send_message(chat_id, f"Reply nahi bheja gaya: {exc}")
    elif text.startswith("/approve") and message_authorized(message):
        match = re.search(r"/approve(?:@\w+)?\s+(\S+)", text)
        if not match:
            send_message(chat_id, "Use: /approve COMMENT_ID")
        else:
            try:
                with db() as con:
                    row = con.execute("SELECT suggested_reply FROM comment_queue WHERE comment_id=? AND status='pending'", (match.group(1),)).fetchone()
                if not row or not row["suggested_reply"]:
                    raise ValueError("AI suggestion nahi hai. /reply COMMENT_ID your text use karo.")
                publish_comment_reply(match.group(1), row["suggested_reply"])
                send_message(chat_id, "✅ AI reply publish ho gaya.")
            except Exception as exc:
                send_message(chat_id, f"Approve failed: {exc}")
    elif text.startswith("/skip") and message_authorized(message):
        match = re.search(r"/skip(?:@\w+)?\s+(\S+)", text)
        if not match or not skip_comment(match.group(1)):
            send_message(chat_id, "Use: /skip COMMENT_ID (sirf pending comment)")
        else:
            send_message(chat_id, "⏭ Comment skipped.")
    elif text.startswith("/deletecomment") and message_authorized(message):
        match = re.search(r"/deletecomment(?:@\w+)?\s+(\S+)", text)
        if not match:
            send_message(chat_id, "Use: /deletecomment COMMENT_ID")
        else:
            try:
                delete_youtube_comment(match.group(1))
                send_message(chat_id, "🗑 YouTube comment delete ho gaya.")
            except Exception as exc:
                send_message(chat_id, f"Delete failed: {exc}")
    elif text.startswith("/autoreply") and message_authorized(message):
        value = text.split(maxsplit=1)[1].strip().lower() if len(text.split(maxsplit=1)) > 1 else ""
        if value not in ("on", "off"):
            current = get_setting("auto_reply_enabled", os.getenv("AUTO_REPLY_ENABLED", "false"))
            send_message(chat_id, f"Auto reply: {current}. Use /autoreply on or /autoreply off")
        else:
            with db() as con:
                con.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('auto_reply_enabled',?)",
                            ("true" if value == "on" else "false",))
            send_message(chat_id, f"Automatic comment replies {value.upper()}.")
    elif text.startswith("/start") and (message_authorized(message) or owner_group_command(message)):
        keyboard = {
            "keyboard": [
                [{"text": "/upload"}, {"text": "/queue"}],
                [{"text": "/autopublish on"}, {"text": "/autopublish off"}],
                [{"text": "/privacy public"}, {"text": "/privacy unlisted"}, {"text": "/privacy private"}],
                [{"text": "/comments"}, {"text": "/team"}, {"text": "/help"}],
            ],
            "resize_keyboard": True,
            "is_persistent": True,
        }
        send_message(chat_id, "🎬 YouTube AI Studio\n\nVideo bhejo → apna thumbnail reply karo → AI metadata ke saath auto upload.\n\nOwner: /bindgroup, /allow ID, /revoke ID.\nTeam: /upload, /queue, /thumbnail ID, /publish ID, /schedule ID YYYY-MM-DD HH:MM.", reply_markup=keyboard)
    elif text.startswith("/help") and message_authorized(message):
        send_message(chat_id, "Commands: /upload, /queue, /thumbnail ID, /comments, /reply COMMENT_ID text, /approve COMMENT_ID, /skip COMMENT_ID, /deletecomment COMMENT_ID, /autoreply on|off, /autopublish on|off, /privacy public|unlisted|private, /publish ID, /schedule ID YYYY-MM-DD HH:MM, /team. Owner-only: /bindgroup, /allow ID, /revoke ID.")
    elif text.startswith("/upload") and message_authorized(message):
        send_message(chat_id, "📤 Ab isi group mein video file bhejo. AI title, description, tags aur hashtags banayega; phir apni thumbnail image bot ke prompt ka reply karke bhejo.")
    elif text.startswith("/autopublish") and message_authorized(message):
        value = text.split(maxsplit=1)[1].strip().lower() if len(text.split(maxsplit=1)) > 1 else ""
        if value not in ("on", "off"):
            current = get_setting("auto_publish_enabled", os.getenv("AUTO_UPLOAD_DEFAULT", "false"))
            send_message(chat_id, f"Auto publish: {current}. Use /autopublish on or /autopublish off")
        else:
            with db() as con:
                con.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('auto_publish_enabled',?)",
                            ("true" if value == "on" else "false",))
            send_message(chat_id, f"Auto publish {value.upper()}.")
    elif text.startswith("/privacy") and message_authorized(message):
        value = text.split(maxsplit=1)[1].strip().lower() if len(text.split(maxsplit=1)) > 1 else ""
        if value not in ("public", "unlisted", "private"):
            current = get_setting("upload_privacy", os.getenv("AUTO_UPLOAD_PRIVACY", "private"))
            send_message(chat_id, f"Upload privacy: {current}. Use /privacy public, /privacy unlisted, or /privacy private")
        else:
            with db() as con:
                con.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('upload_privacy',?)", (value,))
            send_message(chat_id, f"Default upload privacy set to {value}.")
    elif text.startswith("/thumbnail") and message_authorized(message):
        match = re.search(r"/thumbnail(?:@\w+)?\s+(\d+)", text)
        if not match:
            send_message(chat_id, "Use: /thumbnail VIDEO_ID")
        else:
            video_id = int(match.group(1))
            with db() as con:
                row = con.execute("SELECT id FROM videos WHERE id=?", (video_id,)).fetchone()
            if not row:
                send_message(chat_id, "Video queue ID nahi mila.")
            else:
                prompt = send_message(chat_id, f"🖼 Video #{video_id} ke liye thumbnail PHOTO is message ka reply karke bhejo.", reply_markup={"force_reply": True, "selective": True})
                with db() as con:
                    con.execute("INSERT OR REPLACE INTO pending_thumbnails(prompt_message_id,chat_id,video_id,created_at) VALUES(?,?,?,?)",
                                (prompt["message_id"], str(chat_id), video_id, datetime.now(timezone.utc).isoformat()))
    elif text.startswith("/queue") and message_authorized(message):
        with db() as con:
            rows = con.execute("SELECT id,title,status FROM videos ORDER BY id DESC LIMIT 10").fetchall()
        send_message(chat_id, "\n".join([f"#{r['id']} [{r['status']}] {r['title']}" for r in rows]) or "Queue empty.")
    elif text.startswith("/schedule") and message_authorized(message):
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
    elif text.startswith("/publish") and message_authorized(message):
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
    elif message.get("photo") or message.get("document", {}).get("mime_type", "").startswith("image/"):
        try:
            handle_thumbnail(message)
        except Exception as exc:
            log.exception("Telegram thumbnail handling failed")
            send_message(chat_id, f"Thumbnail process nahi hua: {exc}")
    elif message.get("video") or message.get("document", {}).get("mime_type", "").startswith("video/"):
        try:
            handle_video(message)
        except Exception as exc:
            log.exception("Telegram video handling failed")
            send_message(chat_id, f"Video process nahi hua: {exc}")



@app.post("/telegram/webhook")
def telegram_webhook():
    secret = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()
    if not secret or not hmac.compare_digest(
        request.headers.get("X-Telegram-Bot-Api-Secret-Token", ""), secret
    ):
        return jsonify({"error": "Unauthorized"}), 401
    update = request.get_json(silent=True) or {}
    threading.Thread(target=process_telegram_update, args=(update,), daemon=True,
                     name=f"telegram-update-{update.get('update_id', 'unknown')}").start()
    return jsonify({"ok": True})


def configure_telegram_webhook():
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    base = os.getenv("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
    secret = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()
    if not token or not base or not secret:
        log.warning("Telegram webhook not configured: set TELEGRAM_BOT_TOKEN, RENDER_EXTERNAL_URL and TELEGRAM_WEBHOOK_SECRET")
        return
    response = requests.post(
        f"https://api.telegram.org/bot{token}/setWebhook",
        json={"url": base + "/telegram/webhook", "secret_token": secret,
              "allowed_updates": ["message", "callback_query"], "drop_pending_updates": False},
        timeout=30,
    )
    response.raise_for_status()
    if not response.json().get("ok"):
        raise RuntimeError("Telegram setWebhook failed: " + response.text[:300])
    log.info("Telegram webhook configured for Render")



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
    <title>YouTube AI Studio</title><style>
    :root{{color-scheme:dark;font-family:Inter,system-ui,Arial,sans-serif;background:#080d18;color:#edf2ff}}
    *{{box-sizing:border-box}}body{{margin:0;padding:22px;background:radial-gradient(circle at 15% 0%,#172554 0,#080d18 42%);min-height:100vh}}
    main{{max-width:1120px;margin:0 auto}}h1{{font-size:clamp(28px,5vw,42px);letter-spacing:-1.4px;margin:10px 0}}
    main>p{{color:#9eacc5;line-height:1.6}}section{{background:linear-gradient(145deg,#151f33,#101827);border:1px solid #26344c;padding:20px;border-radius:20px;margin:18px 0;box-shadow:0 14px 36px #0005}}
    h2{{font-size:19px;margin:0 0 16px}}a{{color:#8db8ff;text-decoration:none}}a:hover{{color:#c5d9ff}}
    button{{border:0;border-radius:11px;padding:11px 16px;background:linear-gradient(110deg,#2563eb,#7c3aed);color:white;font-weight:700;margin:6px 4px 6px 0;cursor:pointer}}
    button:hover{{filter:brightness(1.12)}}input,select{{background:#0b1220;color:#eef2ff;border:1px solid #34445f;border-radius:10px;padding:10px;max-width:100%;margin:5px}}
    label{{display:inline-flex;align-items:center;gap:8px;color:#cbd5e1;margin:8px 10px 8px 0}}
    table{{width:100%;border-collapse:collapse;overflow:hidden}}td,th{{padding:13px 10px;border-bottom:1px solid #26344c;text-align:left}}
    th{{color:#9fb2d0;font-size:12px;text-transform:uppercase;letter-spacing:.08em}}td{{color:#e5edff;font-size:14px}}
    tr:last-child td{{border-bottom:0}}pre{{white-space:pre-wrap;background:#080e1a;border:1px solid #26344c;border-radius:12px;padding:14px;color:#a9c7ff;overflow:auto}}
    @media(max-width:650px){{body{{padding:12px}}section{{padding:15px;border-radius:15px}}table{{display:block;overflow-x:auto;white-space:nowrap}}input{{width:100%}}}}
    </style></head><body><main><h1>🎬 YouTube AI Studio</h1>
    <p>Telegram video intake · AI metadata · Scheduled publishing · Automatic comment replies</p><p><a href='/oauth/start'>Connect YouTube channel</a> · <a href='/api/queue'>Queue JSON</a></p>
    <section><h2>Automation settings</h2><label><input id='autoReply' type='checkbox' {'checked' if get_setting("auto_reply_enabled", os.getenv("AUTO_REPLY_ENABLED","false")).lower()=="true" else ''}> Automatic comment replies</label><button onclick='saveSettings()'>Save settings</button><p id='saveStatus'></p></section>
    <section><h2>Comment inbox</h2><p>New YouTube comments · AI suggestions · Approve, reply, skip or delete</p>
    <button onclick='loadComments()'>Refresh comments</button><pre id='commentInbox'>Press Refresh comments to load the inbox.</pre>
    <p><label>Comment ID <input id='replyCommentId' placeholder='YouTube comment ID'></label></p>
    <p><label>Your reply <input id='replyCommentText' placeholder='Type your reply'></label><button onclick='sendCommentReply()'>Send reply</button></p>
    </section>
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
    async function loadComments(){{try{{const rows=await managerApi('/api/comments?status=pending');document.getElementById('commentInbox').textContent=rows.length?rows.map(c=>'ID: '+c.comment_id+' | '+(c.video_title||'Video')+' | '+(c.author||'Viewer')+'\\n'+c.comment_text+'\\nAI: '+(c.suggested_reply||'No suggestion')+'\\nStatus: '+c.status).join('\\n\\n'):'No pending comments.';}}catch(e){{document.getElementById('commentInbox').textContent=e.message;}}}}
    async function sendCommentReply(){{try{{const id=document.getElementById('replyCommentId').value.trim();const reply=document.getElementById('replyCommentText').value.trim();if(!id||!reply)throw Error('Comment ID and reply required');await managerApi('/api/comments/reply',{{comment_id:id,reply}});document.getElementById('replyCommentText').value='';await loadComments();}}catch(e){{document.getElementById('commentInbox').textContent=e.message;}}}}
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


@app.get("/healthz")
def health_check():
    return jsonify({"ok": True, "service": "youtube-ai-studio"})


@app.get("/api/queue")
def queue_api():
    with db() as con:
        rows = con.execute("SELECT id,title,status,youtube_video_id,created_at FROM videos ORDER BY id DESC LIMIT 100").fetchall()
    return jsonify([dict(row) for row in rows])



@app.get("/api/comments")
def comments_api():
    if not manager_api_authorized():
        return jsonify({"error": "Unauthorized"}), 401
    status = request.args.get("status", "pending")
    if status not in ("pending", "replied", "skipped", "deleted", "all"):
        return jsonify({"error": "Invalid status"}), 400
    with db() as con:
        if status == "all":
            rows = con.execute("SELECT * FROM comment_queue ORDER BY created_at DESC LIMIT 100").fetchall()
        else:
            rows = con.execute("SELECT * FROM comment_queue WHERE status=? ORDER BY created_at DESC LIMIT 100",
                               (status,)).fetchall()
    return jsonify([dict(row) for row in rows])


@app.post("/api/comments/reply")
def comment_reply_api():
    if not manager_api_authorized():
        return jsonify({"error": "Unauthorized"}), 401
    payload = request.get_json(silent=True) or {}
    try:
        comment_id = str(payload.get("comment_id", "")).strip()
        reply_text = str(payload.get("reply", "")).strip()
        publish_comment_reply(comment_id, reply_text)
        return jsonify({"ok": True, "comment_id": comment_id})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/comments/action")
def comment_action_api():
    if not manager_api_authorized():
        return jsonify({"error": "Unauthorized"}), 401
    payload = request.get_json(silent=True) or {}
    comment_id = str(payload.get("comment_id", "")).strip()
    action = str(payload.get("action", "")).strip().lower()
    try:
        if action == "approve":
            with db() as con:
                row = con.execute("SELECT suggested_reply FROM comment_queue WHERE comment_id=? AND status='pending'",
                                  (comment_id,)).fetchone()
            if not row or not row["suggested_reply"]:
                raise ValueError("AI suggestion unavailable")
            publish_comment_reply(comment_id, row["suggested_reply"])
        elif action == "skip":
            if not skip_comment(comment_id):
                raise ValueError("Comment is not pending")
        elif action == "delete":
            delete_youtube_comment(comment_id)
        else:
            return jsonify({"error": "Action must be approve, skip, or delete"}), 400
        return jsonify({"ok": True, "comment_id": comment_id, "action": action})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


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
        "auto_reply_enabled", os.getenv("AUTO_REPLY_ENABLED", "false")) == "true"})


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
    save_youtube_token(flow.credentials)
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
        if not row:
            return jsonify({"error": "Video queue item was not found"}), 404
        live_video_path = materialize_file(row["file_path"], MEDIA / f"live_{video_id}.mp4")
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
        if not video:
            raise FileNotFoundError("Scheduled live video file is missing")
        live_video_path = materialize_file(video["file_path"], MEDIA / f"scheduled_live_{job['video_id']}.mp4")
        result = live_manager.start(youtube_service(), live_video_path, job["title"],
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
        live_video_path = materialize_file(row["file_path"], MEDIA / f"live_{video_id}.mp4")
        result = live_manager.start(
            youtube_service(), live_video_path, payload.get("title") or row["title"] or "Live stream",
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


_services_started = False
_services_lock = threading.Lock()
_background_scheduler = None


def start_background_services():
    global _services_started, _background_scheduler
    with _services_lock:
        if _services_started:
            return
        init_db()
        if os.getenv("TELEGRAM_BOT_TOKEN", "").strip():
            configure_telegram_webhook()
        _background_scheduler = BackgroundScheduler(timezone="UTC")
        _background_scheduler.add_job(poll_comment_replies, "interval", minutes=2, id="comment-replies",
                                      max_instances=1, coalesce=True)
        _background_scheduler.add_job(process_scheduled_lives, "interval", seconds=30, id="scheduled-lives",
                                      max_instances=1, coalesce=True)
        _background_scheduler.start()
        _services_started = True
        log.info("Cloud background services started")


def main():
    start_background_services()
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
