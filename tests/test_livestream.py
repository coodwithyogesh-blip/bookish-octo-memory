import tempfile
import unittest
from pathlib import Path
from livestream import build_ffmpeg_command


class LiveStreamTests(unittest.TestCase):
    def test_command_loops_recorded_video_and_uses_youtube_ingest(self):
        with tempfile.TemporaryDirectory() as folder:
            video = Path(folder) / "clip.mp4"
            video.write_bytes(b"video")
            command = build_ffmpeg_command(video, "rtmp://a.rtmp.youtube.com/live2/key")
            self.assertIn("-stream_loop", command)
            self.assertIn("-1", command)
            self.assertEqual(command[-1], "rtmp://a.rtmp.youtube.com/live2/key")
            self.assertIn("libx264", command)
            self.assertIn("aac", command)

    def test_command_can_stream_once_without_looping(self):
        with tempfile.TemporaryDirectory() as folder:
            video = Path(folder) / "clip.mp4"
            video.write_bytes(b"video")
            command = build_ffmpeg_command(video, "rtmps://example/live/key", loop=False)
            self.assertNotIn("-stream_loop", command)

    def test_rejects_missing_video_and_non_rtmp_destination(self):
        with self.assertRaises(FileNotFoundError):
            build_ffmpeg_command("missing.mp4", "rtmp://example/key")
        with tempfile.TemporaryDirectory() as folder:
            video = Path(folder) / "clip.mp4"
            video.write_bytes(b"video")
            with self.assertRaises(ValueError):
                build_ffmpeg_command(video, "https://example/key")


if __name__ == "__main__":
    unittest.main()
