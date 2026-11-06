#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""保留原片并可裁剪首尾，生成中文语音字幕和商品信息卡。"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Sequence


def emit(payload: dict[str, Any], code: int = 0) -> int:
    print(json.dumps(payload, ensure_ascii=False), flush=True)
    return code


def emit_stage(stage: str, percent: int) -> None:
    print(json.dumps({"type": "stage", "stage": stage, "percent": percent}, ensure_ascii=False), flush=True)


def resolve_media_tool(name: str) -> str:
    bundled = Path(__file__).resolve().parent / "bin" / f"{name}.exe"
    if bundled.is_file():
        return str(bundled)
    found = shutil.which(name)
    if found:
        return found
    raise FileNotFoundError(f"找不到 {name}。请将 {name}.exe 放入 engine/bin 或配置到 PATH。")


def probe_video(ffprobe: str, source: Path) -> tuple[float, int, int, bool]:
    result = subprocess.run(
        [ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(source)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or "无法读取视频信息").strip()[-1200:])
    payload = json.loads(result.stdout or "{}")
    streams = payload.get("streams") or []
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    if not video:
        raise ValueError("所选文件没有视频轨道")
    duration = float(video.get("duration") or (payload.get("format") or {}).get("duration") or 0)
    width, height = int(video.get("width") or 0), int(video.get("height") or 0)
    if duration <= 0 or width <= 0 or height <= 0:
        raise ValueError("视频时长或分辨率无效")
    has_audio = any(stream.get("codec_type") == "audio" for stream in streams)
    return duration, width, height, has_audio


def srt_time(seconds: float) -> str:
    millis = max(0, int(round(seconds * 1000)))
    hours, remainder = divmod(millis, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def ass_time(seconds: float) -> str:
    centis = max(0, int(round(seconds * 100)))
    hours, remainder = divmod(centis, 360_000)
    minutes, remainder = divmod(remainder, 6_000)
    secs, centis = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{centis:02d}"


def wrap_text(text: str, limit: int) -> str:
    normalized = re.sub(r"\s+", " ", text).strip()
    if not normalized:
        return ""
    lines: list[str] = []
    current = ""
    for char in normalized:
        if char == " " and not current:
            continue
        current += char
        if len(current) >= limit:
            lines.append(current.rstrip())
            current = ""
    if current:
        lines.append(current.rstrip())
    return r"\N".join(line for line in lines if line)


def ass_text(text: str) -> str:
    # 保留自己插入的 ASS 换行控制符，只转义用户/识别文本中的控制字符。
    parts = text.split(r"\N")
    escaped = [part.replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}").replace("\r", "") for part in parts]
    return r"\N".join(escaped)


def filtered_segments(raw_segments: list[tuple[float, float, str]], clip_start: float, clip_end: float) -> list[tuple[float, float, str]]:
    kept: list[tuple[float, float, str]] = []
    for start, end, text in raw_segments:
        start, end = max(start, clip_start), min(end, clip_end)
        text = re.sub(r"\s+", " ", text).strip()
        if end <= start or not text:
            continue
        kept.append((start - clip_start, end - clip_start, text))
    return kept


def write_srt(path: Path, segments: list[tuple[float, float, str]]) -> None:
    blocks = [f"{index}\n{srt_time(start)} --> {srt_time(end)}\n{text}" for index, (start, end, text) in enumerate(segments, 1)]
    path.write_text("\n\n".join(blocks) + ("\n" if blocks else ""), encoding="utf-8-sig")


def write_ass(
    path: Path,
    segments: list[tuple[float, float, str]],
    clip_duration: float,
    width: int,
    height: int,
    product_id: str,
    title: str,
    price: str,
    selling_point: str,
) -> None:
    short_side = min(width, height)
    caption_size = max(24, min(52, round(short_side * 0.052)))
    product_size = max(22, min(42, round(short_side * 0.043)))
    caption_limit = max(12, round(width / caption_size * 1.55))
    product_limit = max(12, round(width / product_size * 1.65))
    product_lines = [f"商品：{title}"]
    if product_id:
        product_lines.append(f"商品ID：{product_id}")
    if price:
        product_lines.append(f"价格：{price}")
    if selling_point:
        product_lines.append(f"卖点：{selling_point}")
    product_text = r"\N".join(wrap_text(line, product_limit) for line in product_lines if line)

    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        "WrapStyle: 2",
        "ScaledBorderAndShadow: yes",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "",
        "[V4+ Styles]",
        "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding",
        f"Style: Default,Microsoft YaHei,{caption_size},&H00FFFFFF,&H000000FF,&HCC000000,&H77000000,0,0,1,2,1,2,55,55,90,1",
        f"Style: Product,Microsoft YaHei,{product_size},&H00FFFFFF,&H000000FF,&H66000000,&H88000000,1,0,3,8,0,8,45,45,40,1",
        "",
        "[Events]",
        "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text",
    ]
    if product_text and clip_duration > 0:
        lines.append(f"Dialogue: 1,{ass_time(0)},{ass_time(clip_duration)},Product,,0,0,0,,{ass_text(product_text)}")
    for start, end, text in segments:
        caption = ass_text(wrap_text(text, caption_limit))
        if caption:
            lines.append(f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Default,,0,0,0,,{caption}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")


def filter_path(path: Path) -> str:
    escaped = path.resolve().as_posix().replace(":", r"\:").replace("'", r"\'")
    return f"'{escaped}'"


def safe_stem(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*]+', "_", value).strip(" ._")
    return cleaned[:100] or "video"


def output_dimensions(width: int, height: int) -> tuple[int, int]:
    scale = min(1.0, 1080.0 / max(width, height))
    out_width = max(2, int(width * scale) // 2 * 2)
    out_height = max(2, int(height * scale) // 2 * 2)
    return out_width, out_height


def unique_output_path(directory: Path, stem: str) -> Path:
    candidate = directory / f"{stem}.mp4"
    suffix = 2
    while candidate.exists():
        candidate = directory / f"{stem}_{suffix}.mp4"
        suffix += 1
    return candidate


def transcribe(source: Path, model_cache: Path) -> tuple[list[tuple[float, float, str]], str]:
    try:
        from faster_whisper import WhisperModel
    except ImportError as error:
        raise RuntimeError("缺少 faster-whisper 语音识别组件。请在项目 Python 环境安装 engine/requirements.txt 后重启客户端。") from error

    model_cache.mkdir(parents=True, exist_ok=True)
    model = WhisperModel(
        "small",
        device="cpu",
        compute_type="int8",
        cpu_threads=max(2, min(8, os.cpu_count() or 4)),
        download_root=str(model_cache),
    )
    segments, info = model.transcribe(str(source), beam_size=5, vad_filter=True)
    result: list[tuple[float, float, str]] = []
    for segment in segments:
        text = str(segment.text or "").strip()
        if text:
            result.append((float(segment.start), float(segment.end), text))
    language = str(getattr(info, "language", "unknown") or "unknown")
    return result, language


def render(
    ffmpeg: str,
    source: Path,
    output: Path,
    ass_path: Path,
    start: float,
    duration: float,
    width: int,
    height: int,
    has_audio: bool,
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_name(f"{output.stem}.partial.mp4")
    fonts = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    subtitle_filter = f"subtitles=filename={filter_path(ass_path)}"
    if fonts.is_dir():
        subtitle_filter += f":fontsdir={filter_path(fonts)}"
    filters: list[str] = []
    scaled_width, scaled_height = output_dimensions(width, height)
    if (scaled_width, scaled_height) != (width, height):
        filters.append(f"scale={scaled_width}:{scaled_height}:flags=lanczos")
    filters.append(subtitle_filter)

    command = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{start:.3f}", "-i", str(source)]
    if not has_audio:
        command.extend(["-f", "lavfi", "-t", f"{duration:.3f}", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"])
    command.extend([
        "-t", f"{duration:.3f}",
        "-vf", ",".join(filters),
        "-map", "0:v:0",
        "-map", "0:a:0?" if has_audio else "1:a:0",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
        "-movflags", "+faststart", str(partial),
    ])
    try:
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=max(600, int(duration * 12)))
        if result.returncode != 0 or not partial.is_file() or partial.stat().st_size < 1024:
            detail = (result.stderr or "FFmpeg 没有生成有效视频").strip()[-1600:]
            raise RuntimeError(f"视频合成失败：{detail}")
        partial.replace(output)
    finally:
        partial.unlink(missing_ok=True)


def run(source: Path, output_root: Path, payload: dict[str, Any]) -> dict[str, Any]:
    if not source.is_file():
        raise FileNotFoundError(f"所选视频不存在：{source}")
    if source.suffix.lower() != ".mp4":
        raise ValueError("目前只支持 MP4 视频")
    title = str(payload.get("title") or "").strip()
    if not title:
        raise ValueError("请填写商品名称")

    emit_stage("检查视频文件", 2)
    ffmpeg, ffprobe = resolve_media_tool("ffmpeg"), resolve_media_tool("ffprobe")
    total_duration, width, height, has_audio = probe_video(ffprobe, source)
    try:
        clip_start = max(0.0, float(payload.get("trimStart") or 0))
        raw_end = payload.get("trimEnd")
        clip_end = total_duration if raw_end in (None, "") else min(total_duration, float(raw_end))
    except (TypeError, ValueError) as error:
        raise ValueError("首尾裁剪时间必须填写秒数") from error
    if clip_start >= total_duration or clip_end <= clip_start:
        raise ValueError(f"裁剪范围无效。视频总时长 {total_duration:.1f} 秒，请确认开始/结束时间。")
    clip_duration = clip_end - clip_start
    output_width, output_height = output_dimensions(width, height)

    output_root.mkdir(parents=True, exist_ok=True)
    output_dir = output_root / "视频自动剪辑"
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = f"{safe_stem(source.stem)}_字幕商品信息_{timestamp}"
    output = unique_output_path(output_dir, stem)
    subtitle_path = output.with_suffix(".srt")
    ass_path = output.with_suffix(".ass")

    if has_audio:
        emit_stage("加载中文语音识别模型（首次运行会下载模型）", 8)
        model_cache = output_root / ".touguang-models" / "faster-whisper-small"
        emit_stage("识别视频语音并生成同步字幕", 20)
        raw_segments, language = transcribe(source, model_cache)
    else:
        raw_segments, language = [], "unknown"
        emit_stage("视频没有音轨，将只添加商品信息卡", 65)
    segments = filtered_segments(raw_segments, clip_start, clip_end)
    write_srt(subtitle_path, segments)

    emit_stage("合成字幕、商品信息与 MP4", 72)
    write_ass(
        ass_path,
        segments,
        clip_duration,
        output_width,
        output_height,
        str(payload.get("productId") or "").strip(),
        title,
        str(payload.get("price") or "").strip(),
        str(payload.get("sellingPoint") or "").strip(),
    )
    try:
        render(ffmpeg, source, output, ass_path, clip_start, clip_duration, width, height, has_audio)
    finally:
        ass_path.unlink(missing_ok=True)

    warning = "未识别到清晰语音；成片仅添加商品信息卡。" if not segments else ""
    return {
        "ok": True,
        "output": str(output),
        "subtitle": str(subtitle_path),
        "subtitleCount": len(segments),
        "language": language,
        "durationSeconds": round(clip_duration, 3),
        "width": output_width,
        "height": output_height,
        "videoCodec": "H.264",
        "audioCodec": "AAC 128kbps",
        "warning": warning,
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="自动裁剪视频首尾、识别语音字幕并叠加商品信息")
    parser.add_argument("--input", required=True, help="本地 MP4 文件路径")
    parser.add_argument("--output-dir", required=True, help="输出目录")
    args = parser.parse_args(argv)
    try:
        raw = sys.stdin.readline()
        payload = json.loads(raw) if raw else {}
        if not isinstance(payload, dict):
            raise ValueError("剪辑参数格式无效")
        source = Path(args.input).expanduser().resolve()
        output_root = Path(args.output_dir).expanduser().resolve()
        result = run(source, output_root, payload)
        emit_stage("成片已完成", 100)
        return emit({"type": "result", **result})
    except Exception as error:
        return emit({"type": "error", "message": str(error)}, 1)


if __name__ == "__main__":
    raise SystemExit(main())
