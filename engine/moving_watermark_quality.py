"""上传前移动文字水印质检：只拦截多帧证据明确的素材。"""

from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import unicodedata
from pathlib import Path
from typing import Any


ALGORITHM_VERSION = 1
SAMPLE_FRACTIONS = (0.05, 0.20, 0.35, 0.50, 0.65, 0.80, 0.95)
MIN_OCR_SCORE = 0.85


def _text_key(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).lower()
    return "".join(char for char in normalized if char.isalnum())


def _meaningful_text(text: str) -> bool:
    cjk = len(re.findall(r"[\u3400-\u9fff]", text))
    return cjk >= 3 or len(text) >= 6


def _edge_bands(x: float, y: float) -> tuple[str, ...]:
    bands = []
    if y >= 0.77:
        bands.append("bottom")
    if y <= 0.23:
        bands.append("top")
    if x <= 0.16:
        bands.append("left")
    if x >= 0.84:
        bands.append("right")
    return tuple(bands)


def classify_observations(observations: list[dict[str, Any]], duration: float) -> dict[str, Any]:
    """文本、边缘位置、尺寸、倾角和位移同时稳定，才判定移动水印。"""
    grouped: dict[tuple[str, str], dict[int, dict[str, Any]]] = {}
    for item in observations:
        text = _text_key(str(item.get("text", "")))
        score = float(item.get("score", 0))
        if not _meaningful_text(text) or score < MIN_OCR_SCORE:
            continue
        if float(item["longSide"]) > 0.38 or float(item["shortSide"]) > 0.15:
            continue
        for band in _edge_bands(float(item["x"]), float(item["y"])):
            key = (text, band)
            frame = int(item["frameIndex"])
            previous = grouped.setdefault(key, {}).get(frame)
            if previous is None or score > float(previous["score"]):
                grouped[key][frame] = item

    suspected = []
    for (text, band), by_frame in grouped.items():
        points = sorted(by_frame.values(), key=lambda item: int(item["frameIndex"]))
        if len(points) < 3:
            continue
        xs = [float(item["x"]) for item in points]
        ys = [float(item["y"]) for item in points]
        longs = [float(item["longSide"]) for item in points]
        shorts = [float(item["shortSide"]) for item in points]
        angles = [float(item["angle"]) for item in points]
        movement = max(max(xs) - min(xs), max(ys) - min(ys))
        time_span = float(points[-1]["seconds"]) - float(points[0]["seconds"])
        long_median = statistics.median(longs)
        short_median = statistics.median(shorts)
        angle_median = statistics.median(angles)
        size_stable = (
            long_median > 0 and short_median > 0
            and max(abs(value - long_median) / long_median for value in longs) <= 0.25
            and max(abs(value - short_median) / short_median for value in shorts) <= 0.30
        )
        angle_stable = max(abs(value - angle_median) for value in angles) <= 15
        if movement < 0.18 or time_span < duration * 0.25 or not size_stable or not angle_stable:
            continue
        evidence = {
            "text": text, "band": band, "frames": len(points),
            "movement": round(movement, 3), "angle": round(angle_median, 1),
            "positions": [
                {"seconds": round(float(item["seconds"]), 1),
                 "x": round(float(item["x"]), 3), "y": round(float(item["y"]), 3)}
                for item in points
            ],
        }
        # 倾斜/竖排小字跨场景移动且尺寸、方向保持一致，是当前可高置信区分的样式。
        # 水平移动文字仍记疑似，避免把移动商品包装文字误判为劣质。
        if abs(angle_median) >= 35:
            return {
                "verdict": "poor_quality", "reason":
                f"确认移动文字水印「{text}」：{len(points)} 个采样帧，位置变化 {movement:.0%}",
                "evidence": evidence, "algorithmVersion": ALGORITHM_VERSION,
            }
        suspected.append(evidence)
    if suspected:
        return {"verdict": "suspected", "reason": "发现疑似移动文字，证据不足以自动判为劣质",
                "evidence": suspected[0], "algorithmVersion": ALGORITHM_VERSION}
    return {"verdict": "unconfirmed", "reason": "未发现符合高置信规则的移动文字水印；不代表视频绝对无水印",
            "evidence": {}, "algorithmVersion": ALGORITHM_VERSION}


def inspect_video(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.suffix.lower() != ".mp4":
        raise ValueError("移动水印质检只接受已存在的 MP4 文件")
    try:
        import cv2
        import numpy as np
        from rapidocr import RapidOCR
    except ImportError as error:
        return {"verdict": "unconfirmed", "reason": f"本地质检依赖不可用：{error.name}",
                "evidence": {}, "algorithmVersion": ALGORITHM_VERSION}

    capture = cv2.VideoCapture(str(path))
    if not capture.isOpened():
        return {"verdict": "unconfirmed", "reason": "视频解码失败，移动水印未判定",
                "evidence": {}, "algorithmVersion": ALGORITHM_VERSION}
    try:
        fps = float(capture.get(cv2.CAP_PROP_FPS))
        frame_count = float(capture.get(cv2.CAP_PROP_FRAME_COUNT))
        duration = frame_count / fps if fps > 0 and frame_count > 0 else 0
        if not math.isfinite(duration) or duration < 3:
            return {"verdict": "unconfirmed", "reason": "视频时长不足或不可读，移动水印未判定",
                    "evidence": {}, "algorithmVersion": ALGORITHM_VERSION}
        ocr = RapidOCR()
        observations: list[dict[str, Any]] = []
        sampled = 0
        for frame_index, fraction in enumerate(SAMPLE_FRACTIONS):
            seconds = duration * fraction
            capture.set(cv2.CAP_PROP_POS_MSEC, seconds * 1000)
            ok, frame = capture.read()
            if not ok:
                continue
            sampled += 1
            height, width = frame.shape[:2]
            base = float(min(width, height))
            result = ocr(frame)
            boxes = result.boxes if result.boxes is not None else []
            for box, text, score in zip(boxes, result.txts or [], result.scores or []):
                points = np.asarray(box, dtype=np.float64)
                if points.shape != (4, 2):
                    continue
                direction = points[1] - points[0]
                angle = (math.degrees(math.atan2(direction[1], direction[0])) + 90) % 180 - 90
                center = points.mean(axis=0)
                observations.append({
                    "frameIndex": frame_index, "seconds": seconds,
                    "text": str(text), "score": float(score),
                    "x": float(center[0]) / width, "y": float(center[1]) / height,
                    "longSide": float(np.linalg.norm(direction)) / base,
                    "shortSide": float(np.linalg.norm(points[3] - points[0])) / base,
                    "angle": angle,
                })
        if sampled < 3:
            return {"verdict": "unconfirmed", "reason": "可解码采样帧不足，移动水印未判定",
                    "evidence": {}, "algorithmVersion": ALGORITHM_VERSION}
        result = classify_observations(observations, duration)
        result["sampledFrames"] = sampled
        return result
    except Exception as error:
        return {"verdict": "unconfirmed", "reason": f"移动水印质检未完成：{type(error).__name__}",
                "evidence": {}, "algorithmVersion": ALGORITHM_VERSION}
    finally:
        capture.release()


def main() -> int:
    parser = argparse.ArgumentParser(description="对 MP4 执行上传前移动文字水印质检")
    parser.add_argument("--file", type=Path, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(inspect_video(args.file), ensure_ascii=False))
        return 0
    except ValueError as error:
        print(json.dumps({"verdict": "unconfirmed", "reason": str(error),
                          "evidence": {}, "algorithmVersion": ALGORITHM_VERSION}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
