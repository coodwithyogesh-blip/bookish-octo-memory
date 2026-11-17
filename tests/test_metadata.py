import unittest
from metadata import clean_metadata, parse_json_response


class MetadataTests(unittest.TestCase):
    def test_parse_json_from_markdown_fence(self):
        raw = '''```json\n{"title":"Test video","description":"Hello","tags":["one","two"],"hashtags":["#one"]}\n```'''
        result = parse_json_response(raw)
        self.assertEqual(result["title"], "Test video")
        self.assertEqual(result["tags"], ["one", "two"])

    def test_clean_metadata_limits_youtube_fields(self):
        result = clean_metadata({"title": "  A title  ", "description": "Description", "tags": ["one", "one", "two"], "hashtags": ["one", "#two"]})
        self.assertEqual(result["title"], "A title")
        self.assertEqual(result["tags"], ["one", "two"])
        self.assertEqual(result["hashtags"], ["#one", "#two"])

    def test_empty_title_is_rejected(self):
        with self.assertRaises(ValueError):
            clean_metadata({"title": " ", "description": "x"})

    def test_youtube_snippet_includes_hashtags_and_category(self):
        from metadata import build_youtube_snippet
        result = build_youtube_snippet({
            "title": "AI Tutorial", "description": "Learn AI", "tags": ["AI", "Python"],
            "hashtags": ["#AI", "#Python"], "category_id": "28"
        })
        self.assertEqual(result["title"], "AI Tutorial")
        self.assertIn("#AI #Python", result["description"])
        self.assertEqual(result["tags"], ["AI", "Python"])
        self.assertEqual(result["categoryId"], "28")

    def test_youtube_snippet_avoids_duplicate_hashtags(self):
        from metadata import build_youtube_snippet
        result = build_youtube_snippet({
            "title": "AI Tutorial", "description": "Learn AI\\n\\n#AI", "tags": [],
            "hashtags": ["#AI", "#Python"]
        })
        self.assertEqual(result["description"].count("#AI"), 1)


if __name__ == "__main__":
    unittest.main()
