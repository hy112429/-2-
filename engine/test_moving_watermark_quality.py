"""移动文字水印高置信分类的隔离测试。"""

import unittest

from moving_watermark_quality import classify_observations


def point(frame: int, x: float, *, angle: float = -60.0) -> dict:
    return {
        "frameIndex": frame, "seconds": frame * 10.0, "text": "小文子吖",
        "score": 0.96, "x": x, "y": 0.88, "longSide": 0.25,
        "shortSide": 0.09, "angle": angle,
    }


class MovingWatermarkQualityTests(unittest.TestCase):
    def test_rotated_repeated_text_moving_along_edge_is_poor_quality(self) -> None:
        result = classify_observations([
            point(0, 0.18), point(1, 0.49), point(2, 0.72), point(3, 0.43),
        ], duration=40)
        self.assertEqual(result["verdict"], "poor_quality")
        self.assertEqual(result["evidence"]["frames"], 4)

    def test_static_or_single_frame_duplicates_are_not_blocked(self) -> None:
        static = [point(0, 0.5), point(1, 0.51), point(2, 0.49)]
        self.assertEqual(classify_observations(static, 40)["verdict"], "unconfirmed")
        duplicate = [point(0, 0.18), point(0, 0.52), point(1, 0.74)]
        self.assertEqual(classify_observations(duplicate, 40)["verdict"], "unconfirmed")

    def test_horizontal_moving_text_remains_suspected(self) -> None:
        points = [point(0, 0.18, angle=0), point(1, 0.49, angle=0),
                  point(2, 0.72, angle=0)]
        self.assertEqual(classify_observations(points, 40)["verdict"], "suspected")


if __name__ == "__main__":
    unittest.main()
