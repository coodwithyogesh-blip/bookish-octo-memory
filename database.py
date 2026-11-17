"""SQLite database stored on Render's temporary filesystem (no external DB)."""
import os
import sqlite3
from contextlib import contextmanager

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS videos (
        id INTEGER PRIMARY KEY AUTOINCREMENT, telegram_file_id TEXT UNIQUE, file_path TEXT NOT NULL,
        original_name TEXT, title TEXT, description TEXT, tags TEXT, hashtags TEXT,
        thumbnail_path TEXT, status TEXT NOT NULL DEFAULT 'queued', youtube_video_id TEXT,
        scheduled_at TEXT, created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS processed_telegram_updates (update_id INTEGER PRIMARY KEY, created_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS processed_comments (comment_id TEXT PRIMARY KEY, replied_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS comment_queue (
        comment_id TEXT PRIMARY KEY, video_id TEXT NOT NULL, video_title TEXT, author TEXT,
        comment_text TEXT NOT NULL, suggested_reply TEXT, reply_text TEXT,
        status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL, replied_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS pending_thumbnails (
        prompt_message_id INTEGER PRIMARY KEY, chat_id TEXT NOT NULL,
        video_id INTEGER NOT NULL, created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS scheduled_lives (
        id INTEGER PRIMARY KEY AUTOINCREMENT, video_id INTEGER NOT NULL, title TEXT NOT NULL,
        privacy TEXT NOT NULL, loop INTEGER NOT NULL, scheduled_at TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'scheduled', broadcast_id TEXT, created_at TEXT NOT NULL
    )""",
)

class Connection:
    def __init__(self, raw):
        self.raw = raw
    def execute(self, sql, params=()):
        return self.raw.execute(sql, params or ())
    def __enter__(self):
        return self
    def __exit__(self, exc_type, exc, tb):
        try:
            self.raw.rollback() if exc_type else self.raw.commit()
        finally:
            self.raw.close()
        return False

def db():
    path = os.getenv("DATABASE_PATH", "/tmp/manager.db")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    raw = sqlite3.connect(path, timeout=30)
    raw.row_factory = sqlite3.Row
    raw.execute("PRAGMA journal_mode=WAL")
    raw.execute("PRAGMA busy_timeout=30000")
    return Connection(raw)

def init_db():
    with db() as con:
        for statement in SCHEMA:
            con.execute(statement)
        con.execute("CREATE INDEX IF NOT EXISTS idx_videos_status ON videos(status)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_comment_queue_status ON comment_queue(status)")
        con.execute("UPDATE scheduled_lives SET status='scheduled' WHERE status='starting'")
