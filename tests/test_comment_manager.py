import unittest
from comment_manager import parse_reply_command, parse_comment_action, comment_notification


class CommentManagerTests(unittest.TestCase):
    def test_parse_manual_reply_with_multiword_text(self):
        self.assertEqual(
            parse_reply_command("/reply UGC123 Thanks for watching!"),
            ("UGC123", "Thanks for watching!"),
        )

    def test_reply_requires_comment_id_and_text(self):
        with self.assertRaises(ValueError):
            parse_reply_command("/reply UGC123")
        with self.assertRaises(ValueError):
            parse_reply_command("/reply")

    def test_parse_only_supported_comment_actions(self):
        self.assertEqual(parse_comment_action("comment:approve:UGC123"), ("approve", "UGC123"))
        self.assertEqual(parse_comment_action("comment:skip:UGC123"), ("skip", "UGC123"))
        self.assertIsNone(parse_comment_action("comment:delete:UGC123"))
        self.assertIsNone(parse_comment_action("other:approve:UGC123"))

    def test_notification_contains_comment_and_ai_suggestion(self):
        message, keyboard = comment_notification(
            "UGC123", "My video", "Viewer", "Nice video!", "Thanks for watching!"
        )
        self.assertIn("Nice video!", message)
        self.assertIn("Thanks for watching!", message)
        self.assertEqual(keyboard["inline_keyboard"][0][0]["callback_data"], "comment:approve:UGC123")


if __name__ == "__main__":
    unittest.main()
