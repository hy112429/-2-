"""在 Step 17 入队前按实际画面筛掉高度重复的同款带货视频。

stdin: {"anchors": [{"id": "job:1", "path": "..."}],
        "candidates": [{"id": 2, "path": "..."}], "limit": 10}
stdout: {"ok": true, "acceptedIds": [2], "duplicates": [...], "unreadableIds": [...]}

仅当较短视频至少 60% 的抽样画面按时间顺序与另一视频高度一致时判重。
无法解码的文件不靠猜测拦截，仍交给后续素材质量检查。
"""

from __future__ import annotations

import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np


MAX_ITEMS = 500
MAX_SAMPLES = 90
PIXEL_DIFFERENCE_LIMIT = 12.0
HASH_DISTANCE_LIMIT = 8
OVERLAP_LIMIT = 0.60


@dataclass(frozen=True)
class FrameSignature:
    visual_hash: int
    pixels: np.ndarray


def _frame_signature(frame: np.ndarray) -> FrameSignature:
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    small = cv2.resize(gray, (32, 32), interpolation=cv2.INTER_AREA).astype(np.float32)
    coefficients = cv2.dct(small)[:8, :8].flatten()
    median = np.median(coefficients[1:])
    visual_hash = sum(1 << index for index, value in enumerate(coefficients) if value > median)
    pixels = cv2.resize(frame, (60, 106), interpolation=cv2.INTER_AREA).astype(np.int16)
    return FrameSignature(visual_hash, pixels)


def video_signature(path: Path) -> list[FrameSignature]:
    if not path.is_file():
        return []
    capture = cv2.VideoCapture(str(path))
    try:
        fps = float(capture.get(cv2.CAP_PROP_FPS))
        count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
        if not capture.isOpened() or not math.isfinite(fps) or fps <= 0 or count < 1:
            return []
        duration = count / fps
        sample_count = min(MAX_SAMPLES, max(1, int(duration)))
        times = np.linspace(min(0.5, duration / 2), max(0.5, duration - 0.5), sample_count)
        frames: list[FrameSignature] = []
        for seconds in times:
            capture.set(cv2.CAP_PROP_POS_MSEC, float(seconds) * 1000)
            ok, frame = capture.read()
            if ok and frame is not None:
                frames.append(_frame_signature(frame))
        return frames if len(frames) >= min(3, sample_count) else []
    finally:
        capture.release()


def _matching_frames(left: FrameSignature, right: FrameSignature) -> bool:
    if (left.visual_hash ^ right.visual_hash).bit_count() > HASH_DISTANCE_LIMIT:
        return False
    return float(np.mean(np.abs(left.pixels - right.pixels))) < PIXEL_DIFFERENCE_LIMIT


def ordered_match_count(left: list[FrameSignature], right: list[FrameSignature]) -> int:
    """LCS 避免将相同商品的零散镜头误当作整条重复视频。"""
    previous = [0] * (len(right) + 1)
    for first in left:
        current = [0]
        for index, second in enumerate(right):
            current.append(previous[index] + 1 if _matching_frames(first, second)
                           else max(previous[index + 1], current[-1]))
        previous = current
    return previous[-1]


def visual_overlap(left: list[FrameSignature], right: list[FrameSignature]) -> float:
    shortest = min(len(left), len(right))
    if shortest == 0:
        return 0.0
    matched = ordered_match_count(left, right)
    if matched < min(8, shortest):
        return 0.0
    return matched / shortest


def select_unique(anchors: list[dict[str, Any]], candidates: list[dict[str, Any]],
                  limit: int) -> dict[str, Any]:
    if len(anchors) + len(candidates) > MAX_ITEMS or not 1 <= limit <= MAX_ITEMS:
        raise ValueError("画面去重候选数量超出限制")
    accepted: list[int] = []
    duplicates: list[dict[str, Any]] = []
    unreadable: list[int] = []
    known: list[tuple[str, list[FrameSignature]]] = []
    cache: dict[str, list[FrameSignature]] = {}

    def signature(path: str) -> list[FrameSignature]:
        if path not in cache:
            cache[path] = video_signature(Path(path))
        return cache[path]

    for item in anchors:
        frames = signature(str(item["path"]))
        if frames:
            known.append((str(item["id"]), frames))
    for item in candidates:
        asset_id = int(item["id"])
        frames = signature(str(item["path"]))
        if not frames:
            unreadable.append(asset_id)
        duplicate = next(((other_id, overlap) for other_id, other in known
                          if (overlap := visual_overlap(frames, other)) >= OVERLAP_LIMIT), None)
        if duplicate:
            duplicates.append({"assetId": asset_id, "matches": duplicate[0],
                               "overlap": round(duplicate[1], 3)})
            continue
        accepted.append(asset_id)
        if frames:
            known.append((str(asset_id), frames))
        if len(accepted) >= limit:
            break
    return {"ok": True, "acceptedIds": accepted, "duplicates": duplicates,
            "unreadableIds": unreadable}


def main() -> None:
    request = json.loads(sys.stdin.readline())
    result = select_unique(request.get("anchors", []), request.get("candidates", []),
                           int(request.get("limit", MAX_ITEMS)))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
        raise SystemExit(1)
