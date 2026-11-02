#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把百应商品详情图合成为竖版和方版无配乐视频。"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional, Sequence


def emit(payload: dict[str, Any], code: int = 0) -> int:
    print(json.dumps(payload, ensure_ascii=False))
    return code


def emit_stage(stage: str, percent: int) -> None:
    print(json.dumps({"type": "stage", "stage": stage, "percent": percent}, ensure_ascii=False), flush=True)


def resolve_ffmpeg(override: Optional[str] = None) -> str:
    if override:
        path = Path(override)
        if not path.exists():
            raise FileNotFoundError(f"指定的 FFmpeg 不存在：{override}")
        return str(path)
    bundled = Path(__file__).resolve().parent / "bin" / "ffmpeg.exe"
    if bundled.exists():
        return str(bundled)
    found = shutil.which("ffmpeg")
    if not found:
        raise FileNotFoundError("找不到 FFmpeg。请将 ffmpeg.exe 放入 engine/bin 或配置到 PATH。")
    return found


def build_video(ffmpeg: str, images: list[Path], output: Path, width: int, height: int) -> None:
    duration = 2
    command = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]
    for image in images:
        command.extend(["-loop", "1", "-framerate", "30", "-t", str(duration), "-i", str(image)])

    filters: list[str] = []
    inputs: list[str] = []
    for index in range(len(images)):
        filters.append(
            f"[{index}:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps=30,format=yuv420p[v{index}]"
        )
        inputs.append(f"[v{index}]")
    filters.append(f"{''.join(inputs)}concat=n={len(images)}:v=1:a=0[vout]")
    total_duration = duration * len(images)
    command.extend([
        # 微弱白噪声不含音乐，电平约为 -100 dBFS；避免纯数字静音被 AAC
        # 编码器压缩成远低于目标值的实际码率。
        "-f", "lavfi", "-t", str(total_duration), "-i",
        "anoisesrc=color=white:amplitude=0.00001:sample_rate=44100",
        "-filter_complex", ";".join(filters), "-map", "[vout]", "-map", f"{len(images)}:a:0",
        "-c:v", "libx264", "-preset", "medium", "-crf", "22", "-pix_fmt", "yuv420p",
        "-ac", "2", "-c:a", "aac", "-b:a", "128k", "-shortest", "-movflags", "+faststart", str(output),
    ])
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or "FFmpeg 合成失败").strip()[-1200:])


def build_product_cover(ffmpeg: str, image: Path, output: Path, width: int, height: int) -> None:
    """用一张商品详情图做无 AI 的比例封面；保留原图主体并用模糊背景补齐画布。"""
    if not image.is_file():
        raise FileNotFoundError(f"商品详情图不存在：{image}")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f"{output.stem}.partial{output.suffix or '.jpg'}")
    # 背景先按“覆盖”缩放再精确裁到画布尺寸，然后铺满全幅；不使用 pad=black，避免黑边/黑框。
    filter_graph = (
        f"[0:v]split=2[background][foreground];"
        f"[background]scale={width}:{height}:force_original_aspect_ratio=increase:flags=lanczos,"
        f"crop={width}:{height},gblur=sigma=28:steps=2,eq=brightness=-0.14:saturation=0.82[background];"
        f"[foreground]scale={width}:{height}:force_original_aspect_ratio=decrease:flags=lanczos[foreground];"
        "[background][foreground]overlay=(W-w)/2:(H-h)/2:shortest=1,setsar=1,format=yuvj420p[cover]"
    )
    command = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(image),
        "-filter_complex", filter_graph, "-map", "[cover]", "-frames:v", "1", "-q:v", "2", str(temporary),
    ]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90
        )
        if result.returncode != 0 or not temporary.is_file() or temporary.stat().st_size < 1024:
            detail = (result.stderr or "输出封面文件为空").strip()[-1200:]
            raise RuntimeError(f"商品详情图封面生成失败：{detail}")
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="百应商品详情图转视频")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--ffmpeg")
    args = parser.parse_args(argv)
    try:
        raw = sys.stdin.readline()
        payload = json.loads(raw) if raw else {}
        source_paths = payload.get("images", []) if isinstance(payload, dict) else []
        if not isinstance(source_paths, list):
            raise ValueError("商品图片列表格式无效")
        images = [Path(str(path)).expanduser().resolve() for path in source_paths]
        images = [path for path in images if path.is_file()][:5]
        if not images:
            raise ValueError("没有可用的商品详情图")
        source_count = len(images)
        # 百应部分商品只提供主图和白底图；按原顺序循环到 5 个画面，
        # 保持单画面 2 秒、成片约 10 秒的业务规格。
        if len(images) < 5:
            images = [images[index % len(images)] for index in range(5)]
        output_dir = Path(args.output_dir).expanduser().resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        ffmpeg = resolve_ffmpeg(args.ffmpeg)
        vertical = output_dir / "商品详情图视频_9比16.mp4"
        square = output_dir / "商品详情图视频_1比1.mp4"
        emit_stage("生成 9:16 视频", 0)
        build_video(ffmpeg, images, vertical, 1080, 1920)
        emit_stage("9:16 视频完成，生成 1:1 视频", 50)
        build_video(ffmpeg, images, square, 1080, 1080)
        emit_stage("9:16 与 1:1 视频均已生成", 100)
        return emit({"ok": True, "sourceImages": source_count, "timelineImages": len(images), "durationSeconds": len(images) * 2,
                     "video9x16": str(vertical), "video1x1": str(square), "audio": "无音乐微弱底噪 AAC 128kbps", "videoCodec": "H.264"})
    except Exception as error:
        return emit({"ok": False, "error": str(error)}, 1)


if __name__ == "__main__":
    raise SystemExit(main())
