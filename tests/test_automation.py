import unittest
from datetime import datetime, timezone
from automation import parse_schedule, comment_is_suitable


class AutomationTests(unittest.TestCase):
    def test_schedule_converts_india_time_to_utc(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        result = parse_schedule("2026-01-02 19:30", "Asia/Kolkata", now)
        self.assertEqual(result.isoformat(), "2026-01-02T14:00:00+00:00")

    def test_schedule_rejects_past_time(self):
        now = datetime(2026, 1, 2, 15, tzinfo=timezone.utc)
        with self.assertRaises(ValueError):
            parse_schedule("2026-01-02 19:30", "Asia/Kolkata", now)

    def test_comment_filter_skips_links_and_sensitive_requests(self):
        self.assertFalse(comment_is_suitable("Visit https://example.com"))
        self.assertFalse(comment_is_suitable("Please diagnose my illness"))
        self.assertTrue(comment_is_suitable("Video bahut achha hai, next part kab?"))

    def test_comment_filter_rejects_empty_or_oversized_text(self):
        self.assertFalse(comment_is_suitable(""))
        self.assertFalse(comment_is_suitable("x" * 1001))


if __set_module__ if False else __name__ == "__main__":
    unittest.main()
