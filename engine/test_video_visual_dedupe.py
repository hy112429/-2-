"""不同 ID 的重复视频与不同带货作品的画面去重测试。"""

import unittest
from unittest.mock import patch

import numpy as np

from video_visual_dedupe import FrameSignature, select_unique, visual_overlap


def frame(seed: int) -> FrameSignature:
    pixels = np.random.default_rng(seed).integers(0, 256, (106, 60, 3), dtype=np.uint8)
    return FrameSignature(0, pixels.astype(np.int16))


class VideoVisualDedupeTests(unittest.TestCase):
    def test_different_video_ids_with_same_or_mostly_same_frames_are_skipped(self) -> None:
        original = [frame(index) for index in range(12)]
        different = [frame(index) for index in range(40, 52)]
        signatures = {
            "first": original,
            "same-with-another-id": original,
            "mostly-same": original[:8] + different[:4],
            "separate-work": different,
        }
        with patch("video_visual_dedupe.video_signature", side_effect=lambda path: signatures[str(path)]):
            result = select_unique([], [
                {"id": 1, "path": "first"},
                {"id": 2, "path": "same-with-another-id"},
                {"id": 3, "path": "mostly-same"},
                {"id": 4, "path": "separate-work"},
            ], 10)
        self.assertEqual(result["acceptedIds"], [1, 4])
        self.assertEqual([item["assetId"] for item in result["duplicates"]], [2, 3])

    def test_short_shared_segment_does_not_reject_a_different_work(self) -> None:
        first = [frame(index) for index in range(12)]
        other = first[:5] + [frame(index) for index in range(70, 77)]
        self.assertLess(visual_overlap(first, other), 0.60)

    def test_existing_upload_is_an_anchor_for_the_next_candidate(self) -> None:
        original = [frame(index) for index in range(12)]
        signatures = {"uploaded": original, "new-id": original, "unreadable": []}
        with patch("video_visual_dedupe.video_signature", side_effect=lambda path: signatures[str(path)]):
            result = select_unique([{"id": "job:20", "path": "uploaded"}], [
                {"id": 6, "path": "new-id"}, {"id": 7, "path": "unreadable"}
            ], 1)
        self.assertEqual(result["acceptedIds"], [7])
        self.assertEqual(result["duplicates"][0]["matches"], "job:20")
        self.assertEqual(result["unreadableIds"], [7])


if __name__ == "__main__":
    unittest.main()
