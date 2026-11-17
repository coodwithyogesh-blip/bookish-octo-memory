import unittest

from bot import parse_channel_id, parse_feed, make_caption


class YouTubeBotTests(unittest.TestCase):
    def test_parse_channel_id_from_channel_page(self):
        html = '<meta itemprop="channelId" content="UC1234567890123456789012">'
        self.assertEqual(parse_channel_id(html), "UC1234567890123456789012")

    def test_parse_feed_extracts_video_metadata(self):
        xml = """<?xml version="1.0"?>
        <feed xmlns="http://www.w3.org/2005/Atom"
              xmlns:yt="http://www.youtube.com/xml/schemas/2015">
          <entry>
            <id>yt:video:abc123</id>
            <yt:videoId>abc123</yt:videoId>
            <title>New video</title>
            <link rel="alternate" href="https://www.youtube.com/watch?v=abc123"/>
            <published>2026-11-17T03:00:00+00:00</published>
            <author><name>Code With Yogesh</name></author>
          </entry>
        </feed>"""
        videos = parse_feed(xml)
        self.assertEqual(len(videos), 1)
        self.assertEqual(videos[0]["video_id"], "abc123")
        self.assertEqual(videos[0]["title"], "New video")
        self.assertEqual(videos[0]["channel"], "Code With Yogesh")

    def test_caption_contains_title_description_and_link(self):
        caption = make_caption({
            "title": "My <new> video",
            "description": "A test description",
            "link": "https://youtube.com/watch?v=abc",
            "published": "2026-11-17T03:00:00+00:00",
            "channel": "My Channel",
        })
        self.assertIn("My &lt;new&gt; video", caption)
        self.assertIn("A test description", caption)
        self.assertIn("https://youtube.com/watch?v=abc", caption)


if __name__ == "__main__":
    unittest.main()
