import unittest
from database import Cursor
from cloud_storage import is_remote


class CursorTranslationTests(unittest.TestCase):
    def test_qmark_placeholders_translate_to_psycopg(self):
        self.assertEqual(Cursor._translate("SELECT * FROM videos WHERE id=?"), "SELECT * FROM videos WHERE id=%s")

    def test_insert_ignore_becomes_on_conflict(self):
        sql = Cursor._translate("INSERT OR IGNORE INTO processed_comments VALUES (?,?)")
        self.assertIn("INSERT INTO processed_comments", sql)
        self.assertTrue(sql.endswith("ON CONFLICT DO NOTHING"))

    def test_replace_settings_updates_value(self):
        sql = Cursor._translate("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)")
        self.assertIn("ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value", sql)

    def test_replace_pending_thumbnail_updates_fields(self):
        sql = Cursor._translate("INSERT OR REPLACE INTO pending_thumbnails(prompt_message_id,chat_id,video_id,created_at) VALUES(?,?,?,?)")
        self.assertIn("ON CONFLICT (prompt_message_id) DO UPDATE SET", sql)

    def test_generated_ids_returned_for_video_inserts(self):
        class FakeCursor:
            def execute(self, statement, params):
                self.statement = statement
                self.params = params
            def fetchone(self):
                return {"id": 17}
        fake = FakeCursor()
        cur = Cursor(fake).execute("INSERT INTO videos(title) VALUES(?)", ("Test",))
        self.assertEqual(cur.lastrowid, 17)
        self.assertTrue(fake.statement.endswith("RETURNING id"))

    def test_remote_storage_uri_detection(self):
        self.assertTrue(is_remote("supabase://youtube-media/videos/a.mp4"))
        self.assertFalse(is_remote("/tmp/a.mp4"))


if __name__ == "__main__":
    unittest.main()
