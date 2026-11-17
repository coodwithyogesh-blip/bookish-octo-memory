"""Small SQLite-compatible DB facade backed by Supabase PostgreSQL."""
import os
import re
from contextlib import contextmanager

import psycopg2
from psycopg2.extras import RealDictCursor


SCHEMA = (
    """CREATE TABLE IF NOT EXISTS videos (
        id BIGSERIAL PRIMARY KEY, telegram_file_id TEXT UNIQUE, file_path TEXT NOT NULL,
        original_name TEXT, title TEXT, description TEXT, tags TEXT, hashtags TEXT,
        thumbnail_path TEXT, status TEXT NOT NULL DEFAULT 'queued', youtube_video_id TEXT,
        scheduled_at TEXT, created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS processed_telegram_updates (update_id BIGINT PRIMARY KEY, created_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS processed_comments (comment_id TEXT PRIMARY KEY, replied_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS comment_queue (
        comment_id TEXT PRIMARY KEY, video_id TEXT NOT NULL, video_title TEXT, author TEXT,
        comment_text TEXT NOT NULL, suggested_reply TEXT, reply_text TEXT,
        status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL, replied_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS pending_thumbnails (
        prompt_message_id BIGINT PRIMARY KEY, chat_id TEXT NOT NULL,
        video_id BIGINT NOT NULL, created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS scheduled_lives (
        id BIGSERIAL PRIMARY KEY, video_id BIGINT NOT NULL, title TEXT NOT NULL,
        privacy TEXT NOT NULL, loop INTEGER NOT NULL, scheduled_at TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'scheduled', broadcast_id TEXT, created_at TEXT NOT NULL
    )""",
)


class Cursor:
    def __init__(self, cursor):
        self._cursor = cursor
        self.lastrowid = None
        self._returned = None

    @staticmethod
    def _translate(sql):
        statement = sql.strip()
        ignore = bool(re.match(r"(?is)^INSERT\s+OR\s+IGNORE\s+INTO", statement))
        replace = bool(re.match(r"(?is)^INSERT\s+OR\s+REPLACE\s+INTO", statement))
        statement = re.sub(r"(?is)^INSERT\s+OR\s+IGNORE\s+INTO", "INSERT INTO", statement)
        statement = re.sub(r"(?is)^INSERT\s+OR\s+REPLACE\s+INTO", "INSERT INTO", statement)
        if ignore:
            statement = statement.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
        elif replace:
            table = re.search(r"(?is)^INSERT\s+INTO\s+([a-z_]+)", statement).group(1).lower()
            if table == "settings":
                statement = statement.rstrip().rstrip(";") + " ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value"
            elif table == "pending_thumbnails":
                statement = statement.rstrip().rstrip(";") + (
                    " ON CONFLICT (prompt_message_id) DO UPDATE SET "
                    "chat_id=EXCLUDED.chat_id, video_id=EXCLUDED.video_id, created_at=EXCLUDED.created_at"
                )
            else:
                raise ValueError(f"Unsupported INSERT OR REPLACE table: {table}")
        statement = statement.replace("?", "%s")
        return statement

    def execute(self, sql, params=None):
        statement = self._translate(sql)
        insert = re.match(r"(?is)^INSERT\s+INTO\s+(videos|scheduled_lives)\b", statement)
        if insert and " RETURNING " not in statement.upper():
            statement = statement.rstrip().rstrip(";") + " RETURNING id"
        self._cursor.execute(statement, params or ())
        if insert:
            row = self._cursor.fetchone()
            self.lastrowid = row["id"] if row else None
            self._returned = row
        return self

    def fetchone(self):
        if self._returned is not None:
            row, self._returned = self._returned, None
            return row
        return self._cursor.fetchone()

    def fetchall(self):
        rows = self._cursor.fetchall()
        if self._returned is not None:
            rows.insert(0, self._returned)
            self._returned = None
        return rows

    @property
    def rowcount(self):
        return self._cursor.rowcount


class Connection:
    def __init__(self, raw):
        self.raw = raw

    def execute(self, sql, params=None):
        cursor = self.raw.cursor(cursor_factory=RealDictCursor)
        return Cursor(cursor).execute(sql, params)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type:
                self.raw.rollback()
            else:
                self.raw.commit()
        finally:
            self.raw.close()
        return False


def db():
    url = os.getenv("SUPABASE_DB_URL", "").strip() or os.getenv("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError("SUPABASE_DB_URL missing. Add the Supabase Session Pooler connection string in Render.")
    raw = psycopg2.connect(url, sslmode="require", connect_timeout=15)
    return Connection(raw)


def init_db():
    with db() as con:
        for statement in SCHEMA:
            con.execute(statement)
        con.execute("CREATE INDEX IF NOT EXISTS idx_videos_status ON videos(status)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_comment_queue_status ON comment_queue(status)")
        con.execute("UPDATE scheduled_lives SET status='scheduled' WHERE status='starting'")
