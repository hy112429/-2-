"""千川接口返回 200 HTML 时，不登记成视频文件。"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from download_queue import NoRetryError, download_one


class FakeResponse:
    status_code = 200
    headers = {"Content-Length": "16"}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def iter_content(self, _size):
        yield b"<html>bad</html>"


class FakeSession:
    def get(self, *_args, **_kwargs):
        return FakeResponse()


class VideoValidationTest(unittest.TestCase):
    def test_rejects_html_in_video_task(self):
        with tempfile.TemporaryDirectory(dir=r"D:\software\codex") as directory:
            dest = Path(directory) / "video.mp4"
            with patch("download_queue.probe_codec", return_value=""):
                with self.assertRaises(NoRetryError):
                    download_one(FakeSession(), {"url": "https://example.com/video.mp4", "attempts": 0, "id": "qc-test", "verify_video": True}, dest, 10)
            self.assertFalse(dest.exists())
            self.assertFalse(dest.with_suffix(".mp4.part").exists())


if __name__ == "__main__":
    unittest.main()
