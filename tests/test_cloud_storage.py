import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from database import db, init_db
from cloud_storage import configured, persist, materialize


class RenderLocalStorageTests(unittest.TestCase):
    def test_database_initializes_and_inserts_video(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict("os.environ", {"DATABASE_PATH": str(Path(tmp) / "db.sqlite")}):
                init_db()
                with db() as con:
                    cur = con.execute(
                        "INSERT INTO videos(file_path,title,created_at) VALUES(?,?,?)",
                        ("/tmp/video.mp4", "Test", "now"),
                    )
                    self.assertGreater(cur.lastrowid, 0)
                with db() as con:
                    row = con.execute("SELECT title FROM videos WHERE id=?", (cur.lastrowid,)).fetchone()
                    self.assertEqual(row["title"], "Test")

    def test_no_external_storage_is_required(self):
        self.assertFalse(configured())

    def test_media_helpers_use_local_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "a.mp4"
            source.write_bytes(b"video")
            saved = persist(source, "videos/a.mp4")
            self.assertEqual(saved, str(source))
            self.assertEqual(materialize(saved, Path(tmp) / "copy.mp4"), str(source))


if __name__ == "__main__":
    unittest.main()
