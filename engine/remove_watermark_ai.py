#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
remove_watermark_ai.py —— AI 视频去水印（Moonshine：SLBR 检测 + LaMa 生成式填充）

纯 CPU、多进程并行：低分辨率检测定位水印 → 原分辨率局部生成式填充 → ffmpeg 合并音轨。
输入：stdin JSON {"video_path": "...", "workers": 4, "out_path": "..."}
输出：NDJSON 事件流（type: stage/log/result/error）+ {原名}_nowm.mp4

依赖环境：moonshine 独立 venv（Python 3.13 + torch CPU + moonshine_server）。
引擎脚本仅白名单调用，本脚本负责定位 moonshine 环境并自举。
"""

from __future__ import annotations

import json
import multiprocessing
import os
import shutil
import subprocess
import sys
import tempfile
import types
from pathlib import Path

# ---------------------------------------------------------------- Python 3.13 兼容：imghdr 被移除

if "imghdr" not in sys.modules:
    _shim = types.ModuleType("imghdr")

    def _what(name, h=None):
        data = h if h is not None else open(name, "rb").read(16)
        if data[:3] == b"\xff\xd8\xff":
            return "jpeg"
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return "png"
        if data[:4] == b"GIF8":
            return "gif"
        if data[:2] == b"BM":
            return "bmp"
        return None

    _shim.what = _what
    sys.modules["imghdr"] = _shim

# ---------------------------------------------------------------- 环境定位

MODEL_DIR = os.environ.get("MOONSHINE_MODEL_DIR", r"D:\software\moonshine\models")
SERVER_DIR = os.environ.get("MOONSHINE_SERVER_DIR", r"D:\software\moonshine\Moonshine-Image-main\server")


def _setup_env() -> None:
    os.environ.setdefault("XDG_CACHE_HOME", MODEL_DIR)
    if SERVER_DIR not in sys.path:
        sys.path.insert(0, SERVER_DIR)


# ---------------------------------------------------------------- 单帧处理

def main_mask_boxes(m, W, H, thr=100, pad=15, min_area=300, max_ratio=0.45):
    """从 SLBR mask 挑主要水印区域（连通域分析 + 过滤噪声/误检）。"""
    binm = (m > thr).astype("uint8")
    if not binm.any():
        return []
    import cv2
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    binm = cv2.morphologyEx(binm, cv2.MORPH_CLOSE, k)
    n, _, stats, _ = cv2.connectedComponentsWithStats(binm, connectivity=8)
    out = []
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        if area < min_area:
            continue
        if w * h > max_ratio * W * H:
            continue
        out.append((max(0, x - pad), max(0, y - pad),
                    min(W, x + w + pad), min(H, y + h + pad)))
    return out


def process_frame(frame, slbr, lama, cfg, low_w=360):
    """低分辨率 SLBR 检测 → mask 放大 → 原分辨率 LaMa 局部填充。"""
    import cv2
    import numpy as np
    H, W = frame.shape[:2]
    low_h = max(64, int(round(H * low_w / W)))
    low = cv2.resize(frame, (low_w, low_h))
    _, mask = slbr.infer_bgr(low, tile_size=256, tile_batch=4)
    m = mask[:, :, 0] if mask.ndim == 3 else mask
    m_big = cv2.resize(m, (W, H), interpolation=cv2.INTER_NEAREST)
    boxes = main_mask_boxes(m_big, W, H)
    if not boxes:
        return frame
    full = np.zeros((H, W), np.uint8)
    for (x0, y0, x1, y1) in boxes:
        full[y0:y1, x0:x1] = 255
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    res = lama(rgb, full, cfg)
    return np.clip(res, 0, 255).astype(np.uint8)


# ---------------------------------------------------------------- 帧块 worker

def _worker(video_path, start, end, out_part, model_dir, server_dir, progress_path, worker_id):
    """处理 [start, end) 帧区间，写 part mp4，每 30 帧更新进度文件。"""
    import cv2
    _setup_env_for_child(server_dir, model_dir)
    from moonshine_server.moonshine.slbr_runner import SlbrRunner
    from moonshine_server.model.lama import LaMa
    from moonshine_server.schema import InpaintRequest

    slbr = SlbrRunner(model_dir, device="cpu")
    slbr._load_model()
    lama = LaMa("cpu")
    cfg = InpaintRequest(hd_strategy="Crop")

    cap = cv2.VideoCapture(video_path)
    cap.set(cv2.CAP_PROP_POS_FRAMES, start)
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    vw = cv2.VideoWriter(out_part, cv2.VideoWriter_fourcc(*"mp4v"), fps, (width, height))

    idx = start
    processed = 0
    while idx < end:
        ok, frame = cap.read()
        if not ok:
            break
        try:
            out = process_frame(frame, slbr, lama, cfg)
        except Exception:
            out = frame  # 单帧失败保底原帧，不中断任务
        vw.write(out)
        idx += 1
        processed += 1
        if processed % 10 == 0:
            _write_progress(progress_path, worker_id, processed)
    cap.release()
    vw.release()
    _write_progress(progress_path, worker_id, processed, done=True)
    return processed


def _setup_env_for_child(server_dir, model_dir):
    global SERVER_DIR, MODEL_DIR
    os.environ.setdefault("XDG_CACHE_HOME", model_dir)
    if server_dir not in sys.path:
        sys.path.insert(0, server_dir)


def _write_progress(path, worker_id, count, done=False):
    try:
        tmp = f"{path}.{worker_id}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"worker": worker_id, "count": count, "done": done}, f)
        os.replace(tmp, f"{path}.{worker_id}")
    except Exception:
        pass


# ---------------------------------------------------------------- ffmpeg 定位

def resolve_ffmpeg() -> str:
    for cand in (
        shutil.which("ffmpeg"),
        os.environ.get("FFMPEG_PATH"),
        str(Path(__file__).resolve().parent / "bin" / "ffmpeg.exe"),
    ):
        if cand and Path(cand).is_file():
            return cand
    return "ffmpeg"


# ---------------------------------------------------------------- 主流程

def emit(event: dict) -> None:
    print(json.dumps(event, ensure_ascii=False), flush=True)


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    raw = sys.stdin.read()
    try:
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        data = {}
    video_path = str(data.get("video_path") or "")
    workers = int(data.get("workers") or 2)
    out_path = str(data.get("out_path") or "")

    if not video_path or not Path(video_path).is_file():
        emit({"type": "error", "message": "视频文件不存在，请重新选择"})
        return 1

    import cv2
    src = Path(video_path)
    if not out_path:
        out_path = str(src.with_name(f"{src.stem}_nowm.mp4"))
    dest = Path(out_path)
    dest.parent.mkdir(parents=True, exist_ok=True)

    cap = cv2.VideoCapture(video_path)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.release()
    if total_frames <= 0:
        emit({"type": "error", "message": "无法读取视频帧数"})
        return 1

    _setup_env()
    workers = max(1, min(workers, total_frames, 8))
    emit({"type": "stage", "stage": f"AI 去水印：共 {total_frames} 帧，{workers} 进程并行（纯 CPU，耗时较长）", "percent": 0})

    tmp_dir = Path(tempfile.mkdtemp(prefix="wm_ai_"))
    progress_base = str(tmp_dir / "progress")

    chunks = []
    step = max(1, total_frames // workers)
    start = 0
    for i in range(workers):
        end = total_frames if i == workers - 1 else min(start + step, total_frames)
        if start >= end:
            break
        chunks.append((start, end))
        start = end

    # Windows spawn：子进程会在 __main__ 重新执行本文件，故 worker 函数在模块级
    ctx = multiprocessing.get_context("spawn")
    procs = []
    for i, (s, e) in enumerate(chunks):
        part = str(tmp_dir / f"part_{i}.mp4")
        p = ctx.Process(
            target=_worker,
            args=(video_path, s, e, part, MODEL_DIR, SERVER_DIR, progress_base, i),
        )
        p.start()
        procs.append(p)

    # 进度轮询
    import time
    while any(p.is_alive() for p in procs):
        time.sleep(2)
        done_frames = 0
        for i in range(len(chunks)):
            try:
                with open(f"{progress_base}.{i}", encoding="utf-8") as f:
                    done_frames += json.load(f).get("count", 0)
            except Exception:
                pass
        percent = int(done_frames * 100 / max(1, total_frames))
        emit({"type": "stage", "stage": f"AI 去水印处理中：{done_frames}/{total_frames} 帧", "percent": percent})

    for p in procs:
        p.join()

    # 合并片段
    emit({"type": "stage", "stage": "合并片段并保留音轨…", "percent": 99})
    list_file = tmp_dir / "list.txt"
    parts = sorted(tmp_dir.glob("part_*.mp4"))
    with open(list_file, "w", encoding="utf-8") as f:
        for part in parts:
            f.write(f"file '{part.as_posix()}'\n")

    ffmpeg = resolve_ffmpeg()
    final_tmp = str(tmp_dir / "merged.mp4")
    cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", str(list_file),
        "-i", video_path,
        "-map", "0:v:0", "-map", "1:a:0?",
        "-c:v", "libx264", "-crf", "20", "-preset", "medium",
        "-c:a", "aac", "-b:a", "128k", "-shortest",
        final_tmp,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0 or not Path(final_tmp).is_file():
        emit({"type": "error", "message": f"ffmpeg 合并失败：{(proc.stderr or '').strip()[:300]}"})
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return 1

    # 跨盘移动（tmp 在 C 盘、目标可能在 D 盘，os.replace 不支持跨盘）
    shutil.move(final_tmp, dest)
    shutil.rmtree(tmp_dir, ignore_errors=True)
    emit({"type": "result", "data": {"ok": True, "file_path": str(dest), "frames": total_frames}})
    return 0


if __name__ == "__main__":
    sys.exit(main())
