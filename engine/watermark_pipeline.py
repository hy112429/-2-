#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""下载完成后的自动水印处理：先尝试移动水印传统跟踪，再回退 AI。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


def emit(event: Dict[str, Any], exit_code: int = 0) -> int:
    print(json.dumps(event, ensure_ascii=False), flush=True)
    return exit_code


def child(script: str, args: list[str], stdin_json: Optional[Dict[str, Any]] = None) -> Tuple[int, Optional[Dict[str, Any]]]:
    script_path = Path(__file__).resolve().parent / script
    payload = json.dumps(stdin_json, ensure_ascii=False) if stdin_json is not None else None
    proc = subprocess.Popen(
        [sys.executable, str(script_path), *args],
        stdin=subprocess.PIPE if payload is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.stdin is not None:
        try:
            proc.stdin.write(payload or "")
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass
    assert proc.stdout is not None
    result: Optional[Dict[str, Any]] = None
    output_tail: list[str] = []
    for line in proc.stdout:
        value = line.strip()
        if not value:
            continue
        try:
            item = json.loads(value)
        except json.JSONDecodeError:
            output_tail.append(value[-300:])
            output_tail = output_tail[-12:]
            continue
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "stage":
            emit({"type": "stage", "stage": str(item.get("stage") or ""), "percent": item.get("percent", 0)})
        elif kind == "log":
            emit({"type": "log", "level": item.get("level", "info"), "message": str(item.get("message") or "")})
        elif kind == "error":
            result = {"ok": False, "error": str(item.get("message") or "子任务失败")}
        elif kind == "result":
            data = item.get("data")
            result = data if isinstance(data, dict) else {key: value for key, value in item.items() if key != "type"}
        else:
            result = item
    code = proc.wait()
    if result is None and output_tail:
        result = {"ok": False, "error": "\n".join(output_tail)[-1000:]}
    return code, result


def valid_video(path: Path, reference: Path) -> bool:
    if not path.is_file() or path.stat().st_size < 10_000:
        return False
    try:
        import cv2

        cap = cv2.VideoCapture(str(path))
        frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        ok, _ = cap.read()
        cap.release()
        if not ok or frames <= 0 or width <= 0 or height <= 0:
            return False
        source = cv2.VideoCapture(str(reference))
        sw = int(source.get(cv2.CAP_PROP_FRAME_WIDTH))
        sh = int(source.get(cv2.CAP_PROP_FRAME_HEIGHT))
        source.release()
        return (width, height) == (sw, sh)
    except Exception:
        return False


def output_candidate(source: Path) -> Path:
    candidate = source.with_name(f"{source.stem}_auto_nowm.mp4")
    if not candidate.exists():
        return candidate
    index = 2
    while True:
        numbered = source.with_name(f"{source.stem}_auto_nowm_{index}.mp4")
        if not numbered.exists():
            return numbered
        index += 1


def cached_auto_output(source: Path) -> Optional[Path]:
    """重试/重启时复用同源且已完成的视频，避免重复处理和重复排队。"""
    candidates = sorted(
        source.parent.glob(f"{source.stem}_auto_nowm*.mp4"),
        key=lambda item: item.stat().st_mtime_ns if item.exists() else 0,
        reverse=True,
    )
    source_mtime = source.stat().st_mtime_ns
    for candidate in candidates:
        try:
            if candidate.stat().st_mtime_ns >= source_mtime and valid_video(candidate, source):
                return candidate
        except OSError:
            continue
    return None


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return emit({"type": "error", "message": "自动去水印参数不是有效 JSON"}, 2)
    source = Path(str(data.get("video_path") or "")).expanduser()
    workers = max(1, min(int(data.get("workers") or 2), 8))
    if not source.is_file():
        return emit({"type": "error", "message": "待处理视频不存在"}, 2)
    bundled_bin = Path(__file__).resolve().parent / "bin"
    os.environ["PATH"] = str(bundled_bin) + os.pathsep + os.environ.get("PATH", "")
    cached = cached_auto_output(source)
    if cached:
        return emit({
            "type": "result",
            "data": {
                "ok": True,
                "status": "processed",
                "method": "复用已完成的自动处理文件",
                "file_path": str(cached),
            },
        })

    try:
        import cv2

        cap = cv2.VideoCapture(str(source))
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        cap.release()
    except Exception as exc:  # noqa: BLE001
        return emit({"type": "error", "message": f"读取视频失败：{exc}"}, 2)
    if total <= 0:
        return emit({"type": "error", "message": "视频没有可读取的画面"}, 2)

    frames = sorted({round(i * (total - 1) / 4) for i in range(5)})
    detection_code, detection = child(
        "remove_watermark.py",
        ["--input", str(source), "--detect", "--frames", ",".join(map(str, frames))],
    )
    points = detection.get("points", []) if detection else []
    valid_points = [point for point in points if isinstance(point, list) and len(point) == 3]
    destination = output_candidate(source)

    if detection_code == 0 and len(valid_points) >= 2:
        emit({"type": "stage", "stage": "检测到移动水印，自动跟踪修复", "percent": 5})
        points_arg = ";".join(",".join(str(int(part)) for part in point) for point in valid_points)
        code, result = child(
            "remove_watermark.py",
            ["--input", str(source), "--output", str(destination), "--points", points_arg],
        )
        covered = int((result or {}).get("covered_frames") or 0)
        if code == 0 and covered > 0 and valid_video(destination, source):
            return emit({
                "type": "result",
                "data": {
                    "ok": True,
                    "status": "processed",
                    "method": "传统跟踪修复",
                    "file_path": str(destination),
                    "covered_frames": covered,
                    "frames": total,
                },
            })
        if destination.exists():
            destination.unlink()
        emit({"type": "stage", "stage": "传统跟踪效果不足，转入 AI 自动检测", "percent": 10})
    else:
        emit({"type": "stage", "stage": "移动水印未能可靠定位，转入 AI 自动检测", "percent": 5})

    code, ai_result = child(
        "remove_watermark_ai.py",
        [],
        {
            "video_path": str(source),
            "out_path": str(destination),
            "workers": workers,
            "auto_detect": True,
        },
    )
    if ai_result and ai_result.get("status") == "no_watermark":
        return emit({
            "type": "result",
            "data": {
                "ok": True,
                "status": "no_watermark",
                "method": "AI 抽帧检测",
                "file_path": str(source),
                "processed": False,
            },
        })
    if ai_result and ai_result.get("status") == "needs_review":
        return emit({
            "type": "result",
            "data": {
                "ok": False,
                "status": "needs_review",
                "file_path": str(source),
                "message": str(ai_result.get("message") or "水印检测不确定，保留原视频"),
            },
        })
    ai_path = Path(str((ai_result or {}).get("file_path") or destination))
    if code != 0 or not (ai_result or {}).get("ok") or not valid_video(ai_path, source):
        if ai_path.exists() and ai_path != source:
            ai_path.unlink()
        return emit({
            "type": "error",
            "message": str((ai_result or {}).get("error") or "AI 去水印输出未通过视频完整性检查；原文件已保留"),
        }, 1)
    return emit({
        "type": "result",
        "data": {
            "ok": True,
            "status": "processed",
            "method": "AI 检测与修复",
            "file_path": str(ai_path),
            "frames": total,
        },
    })


if __name__ == "__main__":
    sys.exit(main())
