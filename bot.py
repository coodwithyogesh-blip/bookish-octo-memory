import html
import json
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("youtube-telegram")

ATOM = "{http://www.w3.org/2005/Atom}"
YT = "{http://www.youtube.com/xml/schemas/2015}"
STATE_FILE = Path(os.getenv("STATE_FILE", "state.json"))
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Mozilla/5.0 (compatible; YouTubeTelegramNotifier/1.0)"})


def parse_channel_id(page_html):
    """Extract the stable UC... channel ID from a YouTube channel page."""
    for tag in re.findall(r'<meta\\b[^>]*>', page_html):
        if re.search(r'\\bitemprop=["\\']channelId["\\']', tag):
            match = re.search(r'\\bcontent=["\\'](UC[\\w-]+)', tag)
            if match:
                return match.group(1)
    for pattern in (
        r'"externalId"\\s*:\\s*"(UC[\\w-]+)"',
        r'"channelId"\\s*:\\s*"(UC[\\w-]+)"',
    ):
        match = re.search(pattern, page_html)
        if match:
            return match.group(1)
    raise ValueError("Channel ID nahi mila. YOUTUBE_CHANNEL handle/URL check karo.")


def channel_page_url(channel):
    channel = channel.strip()
    if channel.startswith("http://") or channel.startswith("https://"):
        return channel
    if not channel.startswith("@"):
        channel = "@" + channel
    return "https://www.youtube.com/" + channel


def resolve_channel_id(channel):
    response = SESSION.get(channel_page_url(channel), timeout=25)
    response.raise_for_status()
    return parse_channel_id(response.text)


def parse_feed(xml_text):
    root = ET.fromstring(xml_text)
    videos = []
    for entry in root.findall(ATOM + "entry"):
        video_id = entry.findtext(YT + "videoId")
        title = entry.findtext(ATOM + "title", default="Untitled video")
        published = entry.findtext(ATOM + "published", default="")
        author = entry.find(ATOM + "author")
        channel = author.findtext(ATOM + "name", default="YouTube") if author is not None else "YouTube"
        link = ""
        for item in entry.findall(ATOM + "link"):
            if item.attrib.get("rel") == "alternate":
                link = item.attrib.get("href", "")
                break
        if not link and video_id:
            link = "https://www.youtube.com/watch?v=" + video_id
        if video_id:
            videos.append({
                "video_id": video_id,
                "title": title,
                "published": published,
                "channel": channel,
                "link": link,
                "thumbnail": f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
                "description": "",
            })
    return videos


def fetch_description(video_url):
    try:
        response = SESSION.get(video_url, timeout=25)
        response.raise_for_status()
        match = re.search(r'"shortDescription"\s*:\s*"((?:\\.|[^"\\])*)"', response.text)
        if match:
            return json.loads('"' + match.group(1) + '"')
    except (requests.RequestException, ValueError, json.JSONDecodeError) as exc:
        log.warning("Description fetch nahi hui: %s", exc)
    return ""


def make_caption(video):
    title = html.escape(video.get("title", "New YouTube video"))
    channel = html.escape(video.get("channel", "YouTube"))
    published = html.escape(video.get("published", ""))
    description = video.get("description", "").strip()
    description = html.escape(description[:420])
    link = html.escape(video.get("link", ""), quote=True)
    caption = (
        f"🎬 <b>{title}</b>\n\n"
        f"📺 <b>Channel:</b> {channel}\n"
        f"🕒 <b>Uploaded:</b> {published or 'Recently'}\n\n"
        f"📝 <b>Description:</b>\n{description or 'No description available'}\n\n"
        f'🔗 <a href="{link}">Watch on YouTube</a>'
    )
    return caption[:1024]


def send_to_telegram(token, chat_id, video):
    api = f"https://api.telegram.org/bot{token}/sendPhoto"
    payload = {
        "chat_id": chat_id,
        "photo": video["thumbnail"],
        "caption": make_caption(video),
        "parse_mode": "HTML",
    }
    response = SESSION.post(api, data=payload, timeout=30)
    response.raise_for_status()
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError("Telegram API error: " + str(result))
    log.info("Telegram group mein bhej diya: %s", video["title"])


def load_seen():
    if not STATE_FILE.exists():
        return None
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return set(data.get("seen_video_ids", []))
    except (OSError, json.JSONDecodeError):
        log.warning("State file read nahi hui; fresh state banegi.")
        return None


def save_seen(seen):
    STATE_FILE.write_text(
        json.dumps({"seen_video_ids": list(seen)[-100:]}, indent=2),
        encoding="utf-8",
    )


def poll_once(channel_id, token, chat_id, send_latest_on_start=False):
    feed_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    response = SESSION.get(feed_url, timeout=25)
    response.raise_for_status()
    videos = parse_feed(response.text)
    if not videos:
        log.info("Feed mein abhi koi video nahi mila.")
        return

    seen = load_seen()
    if seen is None:
        if send_latest_on_start:
            newest = videos[0]
            newest["description"] = fetch_description(newest["link"])
            send_to_telegram(token, chat_id, newest)
        save_seen({video["video_id"] for video in videos})
        log.info("Initial feed saved. Ab naye uploads monitor honge.")
        return

    for video in reversed(videos):
        if video["video_id"] in seen:
            continue
        video["description"] = fetch_description(video["link"])
        send_to_telegram(token, chat_id, video)
        seen.add(video["video_id"])
        save_seen(seen)


def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    channel = os.getenv("YOUTUBE_CHANNEL", "").strip()
    interval = max(60, int(os.getenv("POLL_INTERVAL_SECONDS", "300")))
    send_latest = os.getenv("SEND_LATEST_ON_START", "false").lower() == "true"

    if not token or not chat_id or not channel:
        raise SystemExit(
            ".env mein TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID aur YOUTUBE_CHANNEL set karo."
        )

    channel_id = resolve_channel_id(channel)
    log.info("YouTube channel connected: %s (%s)", channel, channel_id)
    log.info("Telegram group notifications start. Check interval: %s sec", interval)

    while True:
        try:
            poll_once(channel_id, token, chat_id, send_latest)
        except requests.RequestException as exc:
            log.error("Network/API error: %s", exc)
        except Exception:
            log.exception("Polling error")
        time.sleep(interval)


if __name__ == "__main__":
    main()
