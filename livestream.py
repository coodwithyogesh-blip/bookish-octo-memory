"""Recorded-video YouTube Live streaming helpers.

The caller supplies an authenticated YouTube Data API service. FFmpeg runs locally
(or on the persistent worker hosting this app); keep the media directory persistent.
"""
import os
import subprocess
from pathlib import Path


def build_ffmpeg_command(video_path, ingest_url, loop=True):
    path = Path(video_path)
    if not path.is_file():
        raise FileNotFoundError(f"Recorded video not found: {path}")
    if not ingest_url.startswith("rtmp"):
        raise ValueError("YouTube ingest URL must use RTMP/RTMPS")
    command = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-re"]
    if loop:
        command += ["-stream_loop", "-1"]
    command += [
        "-i", str(path), "-map", "0:v:0", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
        "-pix_fmt", "yuv420p", "-r", "30", "-g", "60",
        "-b:v", "2500k", "-maxrate", "2500k", "-bufsize", "5000k",
        "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
        "-f", "flv", ingest_url,
    ]
    return command


class RecordedLiveManager:
    """Owns one FFmpeg process per app process; never persist stream keys."""

    def __init__(self):
        self.process = None
        self.broadcast_id = None

    def start(self, youtube, video_path, title, privacy="private", loop=True):
        if self.process and self.process.poll() is None:
            raise RuntimeError("A recorded live stream is already running")
        path = Path(video_path)
        if not path.is_file():
            raise FileNotFoundError(f"Recorded video not found: {path}")
        if privacy not in ("private", "unlisted", "public"):
            raise ValueError("Invalid live privacy setting")

        stream = youtube.liveStreams().insert(
            part="snippet,cdn,contentDetails,status",
            body={
                "snippet": {"title": title[:100], "description": "Recorded video live stream"},
                "cdn": {"ingestionType": "rtmp", "resolution": "720p", "frameRate": "30fps"},
                "contentDetails": {"isReusable": True},
            },
        ).execute()
        broadcast = youtube.liveBroadcasts().insert(
            part="snippet,status,contentDetails",
            body={
                "snippet": {"title": title[:100]},
                "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
                "contentDetails": {
                    "enableAutoStart": False,
                    "enableAutoStop": False,
                    "recordFromStart": True,
                },
            },
        ).execute()
        broadcast_id = broadcast["id"]
        youtube.liveBroadcasts().bind(
            part="id,contentDetails", id=broadcast_id, streamId=stream["id"]
        ).execute()
        ingest = stream["cdn"]["ingestionInfo"]
        ingest_url = ingest["ingestionAddress"].rstrip("/") + "/" + ingest["streamName"]
        command = build_ffmpeg_command(path, ingest_url, loop=loop)
        try:
            self.process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        except Exception:
            youtube.liveBroadcasts().delete(id=broadcast_id).execute()
            raise
        self.broadcast_id = broadcast_id
        return {"broadcast_id": broadcast_id, "stream_id": stream["id"]}

    def status(self):
        running = bool(self.process and self.process.poll() is None)
        return {"running": running, "broadcast_id": self.broadcast_id}

    def stop(self, youtube):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        if self.broadcast_id:
            youtube.liveBroadcasts().transition(
                broadcastStatus="complete", id=self.broadcast_id, part="status"
            ).execute()
        stopped_id = self.broadcast_id
        self.process = None
        self.broadcast_id = None
        return {"stopped": True, "broadcast_id": stopped_id}
