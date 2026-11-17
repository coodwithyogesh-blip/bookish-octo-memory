import unittest
from telegram_groups import (
    is_authorized_user,
    is_allowed_group,
    parse_telegram_id,
)


class TelegramGroupAccessTests(unittest.TestCase):
    def test_owner_is_always_authorized(self):
        self.assertTrue(is_authorized_user("101", "101", []))

    def test_team_member_is_authorized(self):
        self.assertTrue(is_authorized_user("202", "101", ["202", "303"]))

    def test_unknown_member_is_rejected(self):
        self.assertFalse(is_authorized_user("404", "101", ["202"]))

    def test_group_must_match_bound_group(self):
        self.assertTrue(is_allowed_group("-100123", "-100123"))
        self.assertFalse(is_allowed_group("-100999", "-100123"))
        self.assertFalse(is_allowed_group("-100123", ""))

    def test_telegram_ids_are_numeric_and_normalized(self):
        self.assertEqual(parse_telegram_id(" 202 "), "202")
        with self.assertRaises(ValueError):
            parse_telegram_id("not-an-id")


if __name__ == "__main__":
    unittest.main()
