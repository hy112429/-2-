#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
remove_watermark.py —— 动态水印去除（Step 11 补充 · 任意链接下载视频去水印）

背景
    抖音分享源的视频常带「抖音号」动态水印（半透明白/灰文字，随画面移动、时隐时现），
    固定区域裁剪（delogo）无效。本脚本用 OpenCV 逐帧「模板匹配 + 局部修复」去除。

流程
    1. 模板获取：
       - 优先用 --template 指定的模板图（UI 人工框选导出）；
       - 否则自动捕获：取开头若干帧，在四角候选区检测稳定的文字块作为模板。
    2. 逐帧处理：matchTemplate 定位 → 匹配分达标就在该位置用 inpaint/模糊修复。
    3. 合成：OpenCV 只写视频帧（无音频），最后用 ffmpeg 合并原音轨并转 H.264/AAC。

输出
    NDJSON 事件流（与引擎协议一致）：
      {"type":"stage","percent":N}           进度
      {"type":"result","ok":true,"output":path}  成功终态
      {"type":"error","message":"…"}            失败终态
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import cv2
import numpy as np

# 模板自动捕获：开头扫描的帧数（间隔 5 帧取一帧）
CAPTURE_FRAMES = 12
CAPTURE_STEP = 5
# 每帧处理时的进度上报间隔
PROGRESS_STEP = 10


def emit(payload: Dict[str, Any], exit_code: int = 0) -> int:
    print(json.dumps(payload, ensure_ascii=False))
    return exit_code


def stage(percent: int) -> None:
    print(json.dumps({"type": "stage", "stage": "remove_watermark", "percent": percent}, ensure_ascii=False))


# ============================================================ 模板自动捕获

def _candidate_rois(w: int, h: int) -> Dict[str, Tuple[int, int, int, int]]:
    """四角候选区（水印常出现在角落）。"""
    return {
        "tl": (0, 0, int(w * 0.5), int(h * 0.3)),
        "tr": (int(w * 0.5), 0, w, int(h * 0.3)),
        "bl": (0, int(h * 0.7), int(w * 0.5), h),
        "br": (int(w * 0.5), int(h * 0.7), w, h),
    }


def _pick_text_block(gray: np.ndarray) -> Optional[Tuple[int, int, int, int]]:
    """在灰度 ROI 里找一个「像文字块」的区域：中小尺寸、宽高比合理、对比明显。"""
    h, w = gray.shape
    best: Optional[Tuple[float, Tuple[int, int, int, int]]] = None
    for mode in (cv2.THRESH_BINARY, cv2.THRESH_BINARY_INV):
        binary = cv2.adaptiveThreshold(
            gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, mode, 15, 5
        )
        contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for c in contours:
            x, y, cw, ch = cv2.boundingRect(c)
            if cw < 12 or ch < 10:
                continue
            if cw > w * 0.8 or ch > h * 0.7:
                continue
            if cw < ch * 0.3 or cw > ch * 8:
                continue
            score = float(cw * ch)
            if best is None or score > best[0]:
                best = (score, (max(0, x - 6), max(0, y - 6), min(w, cw + 12), min(h, ch + 12)))
    return best[1] if best else None


def auto_capture_template(cap: cv2.VideoCapture) -> Optional[np.ndarray]:
    """
    自动捕获水印模板（v3）。

    难点：水印是半透明小文字，梯度远弱于商品包装文字/字幕，按「最大块」会误检到内容。
    关键判别特征：**水印是移动的，内容文字静止**。
      - 步骤1：开头帧（t≈1s）全帧梯度检测，过滤出「小尺寸文字块」候选，按「贴近画面边缘」优先排序；
      - 步骤2：把候选块放到 0.8s 后的帧做模板匹配，若匹配位置发生明显移动（>25px）→ 是移动水印，采纳；
        匹配位置静止/匹配分低 → 内容文字，丢弃。
    """
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    base = 25  # t≈1s：水印通常此时已在角落出现

    cap.set(cv2.CAP_PROP_POS_FRAMES, base)
    ok, frame = cap.read()
    if not ok:
        return None
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    diff = cv2.absdiff(clahe.apply(gray), cv2.GaussianBlur(gray, (9, 9), 0))
    _, bin_ = cv2.threshold(diff, 15, 255, cv2.THRESH_BINARY)
    bin_ = cv2.dilate(bin_, np.ones((3, 3), np.uint8))
    contours, _ = cv2.findContours(bin_, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    candidates: List[Tuple[float, float, int, int, int, int, np.ndarray]] = []
    for c in contours:
        x, y, cw, ch = cv2.boundingRect(c)
        # 小号文字块（水印尺度），排除大块内容文字/字幕
        if cw < 20 or ch < 10 or cw > int(w * 0.3) or ch > int(h * 0.12):
            continue
        if cw < ch * 0.3 or cw > ch * 8:
            continue
        edge_dist = min(x, y, w - (x + cw), h - (y + ch))
        # 边缘近的排前面（水印常在画面边缘）；同边缘时面积小者优先
        candidates.append((edge_dist, float(cw * ch), x, y, cw, ch, frame[y : y + ch, x : x + cw]))
    if not candidates:
        return None
    candidates.sort(key=lambda t: (t[0], t[1]))

    # 步骤2：移动性验证（间隔 20 帧 ≈ 0.8s）
    cap.set(cv2.CAP_PROP_POS_FRAMES, base + 20)
    ok2, frame2 = cap.read()
    if not ok2:
        return candidates[0][6]
    g2 = cv2.cvtColor(frame2, cv2.COLOR_BGR2GRAY)
    clahe2 = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    g2 = clahe2.apply(g2)
    for edge_dist, _score, x, y, cw, ch, tpl in candidates:
        tpl_g = cv2.cvtColor(tpl, cv2.COLOR_BGR2GRAY)
        res = cv2.matchTemplate(g2, tpl_g, cv2.TM_CCOEFF_NORMED)
        _, maxv, _, maxloc = cv2.minMaxLoc(res)
        if maxv >= 0.3:
            dx = abs(int(maxloc[0]) - x)
            dy = abs(int(maxloc[1]) - y)
            if dx + dy > 25:
                # 移动水印确认。关键：返回的模板要含背景上下文——
                # 纯文字小块在复杂画面里匹配不稳定（半透明文字区分度低）。
                # 向四周扩大到 2~3 倍，给匹配提供纹理特征，也保证文字 mask 非空。
                padx = cw // 2
                pady = ch
                bx = max(0, x - padx)
                by = max(0, y - pady)
                bw = min(w - bx, cw + padx * 2)
                bh = min(h - by, ch + pady * 2)
                return frame[by : by + bh, bx : bx + bw]
    # 验证无果：退回最靠边缘的候选（尽力而为，同样扩大）
    _, _sc, x, y, cw, ch, tpl = candidates[0]
    padx = cw // 2
    pady = ch
    bx = max(0, x - padx)
    by = max(0, y - pady)
    bw = min(w - bx, cw + padx * 2)
    bh = min(h - by, ch + pady * 2)
    return frame[by : by + bh, bx : bx + bw]


def auto_crop_template(template: np.ndarray) -> np.ndarray:
    """
    把模板自动裁剪到「文字核心」区域。
    用户框选的水印区域往往包含背景，背景纹理会让模板匹配漂移。
    用 CLAHE+模糊差生成文字 mask，取包围盒裁剪掉背景，提高匹配鲁棒性。
    """
    gray = cv2.cvtColor(template, cv2.COLOR_BGR2GRAY)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    diff = cv2.absdiff(clahe.apply(gray), cv2.GaussianBlur(gray, (9, 9), 0))
    _, m = cv2.threshold(diff, 20, 255, cv2.THRESH_BINARY)
    m = cv2.dilate(m, np.ones((5, 5), np.uint8))
    ys, xs = np.where(m > 0)
    if len(xs) == 0:
        return template
    p = 4
    x1 = max(0, int(xs.min()) - p)
    y1 = max(0, int(ys.min()) - p)
    x2 = min(template.shape[1], int(xs.max()) + p)
    y2 = min(template.shape[0], int(ys.max()) + p)
    if x2 - x1 < 10 or y2 - y1 < 10:
        return template
    return template[y1:y2, x1:x2]


def load_template(path: Path) -> np.ndarray:
    img = cv2.imread(str(path))
    if img is None:
        raise ValueError(f"无法读取模板图片：{path}")
    return img


# ============================================================ 逐帧去水印

def _enhance_gray(img: np.ndarray) -> np.ndarray:
    """CLAHE 对比度增强：半透明水印文字局部对比低，先增强再匹配更易命中。"""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    return clahe.apply(gray)


def _repair_roi(
    frame: np.ndarray,
    x: int,
    y: int,
    tw: int,
    th: int,
    blur: int,
    text_mask: Optional[np.ndarray] = None,
) -> None:
    """
    对水印区域做局部修复。
    关键：inpaint 的 mask 只标注「文字像素」（模板文字 mask），不全白——
    OpenCV 的 inpaint 在 mask 覆盖整个 ROI 时会把整块当作受损区且无参考像素，实际不生效。
    只修文字，周边完好背景作为 inpaint 参考，效果自然。
    """
    roi = frame[y : y + th, x : x + tw]
    try:
        m = np.zeros((th, tw), dtype=np.uint8)
        if text_mask is not None:
            mh, mw = text_mask.shape[:2]
            m[: min(mh, th), : min(mw, tw)] = text_mask[: min(mh, th), : min(mw, tw)]
        if not m.any():
            # 文字 mask 为空（模板过小 / 半透明水印梯度未过阈值）：
            # 不可直接跳过修复（否则水印原样保留），退化为高斯模糊整块盖住水印。
            frame[y : y + th, x : x + tw] = cv2.GaussianBlur(roi, (blur, blur), 0)
            return
        # 膨胀 mask：半透明水印文字内部（与背景融合的低梯度像素）也要被覆盖，
        # 只修边缘会残留文字内部。膨胀到能盖住整个文字笔画。
        m = cv2.dilate(m, np.ones((5, 5), np.uint8))
        frame[y : y + th, x : x + tw] = cv2.inpaint(roi, m, 3, cv2.INPAINT_TELEA)
    except Exception:
        blurred = cv2.GaussianBlur(roi, (blur, blur), 0)
        frame[y : y + th, x : x + tw] = blurred


def detect_points(
    input_path: Path, frame_nums: List[int], confidence: float
) -> Dict[str, Any]:
    """
    自动检测水印轨迹（供 UI「自动找水印」按钮）。
    流程与去水印主流程一致：
      1. auto_capture 捕获水印模板（v3）；
      2. 预扫描采样 + 中值平滑得轨迹；
      3. 对每个请求帧号线性插值坐标。
    返回 {"ok":true, "points":[[frame,x,y], ...]}，检测不到的帧为 null。
    """
    cap = cv2.VideoCapture(str(input_path))
    if not cap.isOpened():
        raise ValueError(f"无法打开视频：{input_path}")
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    template = auto_capture_template(cap)
    if template is None:
        cap.release()
        raise ValueError("自动定位水印失败（水印过淡或未在开头出现）")
    template = auto_crop_template(template)
    tpl_gray = _enhance_gray(template)

    # 预扫描采样（STEP=5）+ 中值平滑 → 轨迹
    STEP = 5
    samples: List[Tuple[int, int, int]] = []
    for idx in range(0, total, STEP):
        cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
        ok, frame = cap.read()
        if not ok:
            break
        res = cv2.matchTemplate(_enhance_gray(frame), tpl_gray, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(res)
        if max_val >= confidence:
            samples.append((idx, int(max_loc[0]), int(max_loc[1])))

    def median_filter(seq: List[float], window: int = 5) -> List[float]:
        half = window // 2
        return [
            statistics.median(seq[max(0, i - half) : min(len(seq), i + half + 1)])
            for i in range(len(seq))
        ]

    if len(samples) >= 3:
        xs = median_filter([s[1] for s in samples])
        ys = median_filter([s[2] for s in samples])
        track: List[Tuple[int, int, int]] = [
            (samples[i][0], int(xs[i]), int(ys[i])) for i in range(len(samples))
        ]
    else:
        track = samples
    cap.release()

    def pos_at(idx: int) -> Optional[Tuple[int, int]]:
        if not track:
            return None
        if idx <= track[0][0]:
            return (track[0][1], track[0][2])
        if idx >= track[-1][0]:
            return (track[-1][1], track[-1][2])
        for k in range(len(track) - 1):
            if track[k][0] <= idx <= track[k + 1][0]:
                span = max(1, track[k + 1][0] - track[k][0])
                f = (idx - track[k][0]) / span
                x = int(track[k][1] + (track[k + 1][1] - track[k][1]) * f)
                y = int(track[k][2] + (track[k + 1][2] - track[k][2]) * f)
                return (x, y)
        return None

    points = []
    for fn in frame_nums:
        p = pos_at(fn)
        points.append([fn, p[0], p[1]] if p is not None else None)
    return {"ok": True, "points": points}


def remove_watermark(
    input_path: Path,
    output_path: Path,
    template: Optional[np.ndarray],
    confidence: float,
    blur: int,
    track_points: Optional[List[Tuple[int, int, int]]] = None,
) -> Dict[str, Any]:
    """
    去水印主流程（两种模式）：
      1. 模板匹配模式：template 提供，自动预扫描采样轨迹（自动定位）；
      2. 轨迹标注模式：track_points 提供（半自动，用户在关键帧标注水印位置），
         直接按标注点插值轨迹修复——位置准确，最可靠。
    """
    cap = cv2.VideoCapture(str(input_path))
    if not cap.isOpened():
        raise ValueError(f"无法打开视频：{input_path}")

    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    # mkstemp 会返回一个已打开的文件句柄，Windows 下不关闭会占用文件导致后续读写失败
    fd, tmpname = tempfile.mkstemp(suffix=".avi")
    os.close(fd)
    tmp = Path(tmpname)
    out = cv2.VideoWriter(str(tmp), cv2.VideoWriter_fourcc(*"MJPG"), fps, (w, h))

    if template is not None:
        tw = int(template.shape[1])
        th = int(template.shape[0])
        # 匹配用增强灰度：突出半透明文字边缘、抑制背景纹理干扰。
        # 定位必须用 TM_CCOEFF_NORMED（归一化相关）：实测 TM_CCORR_NORMED 在 CLAHE 增强的
        # 复杂画面上处处 0.96 高分，maxLoc 随机、轨迹完全失效；CCOEFF 有真实区分度。
        tpl_gray = _enhance_gray(template)
        # 文字 mask 仅供修复（inpaint 定位水印文字像素）使用，不参与匹配（CCOEFF 不支持 mask）
        tpl_blur = cv2.GaussianBlur(tpl_gray, (9, 9), 0)
        _, tpl_mask = cv2.threshold(cv2.absdiff(tpl_gray, tpl_blur), 24, 255, cv2.THRESH_BINARY)
        tpl_mask = cv2.dilate(tpl_mask, np.ones((3, 3), np.uint8))
    else:
        # 轨迹标注模式：无模板，用「水印文字典型尺寸」作为修复块大小（宽≈9%、高≈3% 画面）
        tw = max(20, int(w * 0.09))
        th = max(12, int(h * 0.03))
        tpl_gray = None
        tpl_mask = None
    # 修复区域在匹配位置基础上外扩（覆盖半透明文字边缘）
    pad = 12

    # ---- 1. 轨迹来源：优先用人工标注点；否则预扫描采样 + 中值平滑 ----
    if track_points is not None:
        track: List[Tuple[int, int, int]] = [(t, x, y) for (t, x, y) in track_points]
        samples = track
    else:
        # 单帧模板匹配对复杂画面容易误匹配到内容纹理，
        # 真实水印随画面移动、轨迹连续；误匹配位置随机。
        # 采样整段轨迹后做中值平滑滤除离群点，再按轨迹插值每帧位置。
        STEP = 5
        samples = []
        for idx in range(0, total, STEP):
            cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
            ok, frame = cap.read()
            if not ok:
                break
            res = cv2.matchTemplate(_enhance_gray(frame), tpl_gray, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, max_loc = cv2.minMaxLoc(res)
            if max_val >= confidence:
                samples.append((idx, int(max_loc[0]), int(max_loc[1])))

        def median_filter(seq: List[float], window: int = 5) -> List[float]:
            half = window // 2
            return [
                statistics.median(seq[max(0, i - half) : min(len(seq), i + half + 1)])
                for i in range(len(seq))
            ]

        if len(samples) >= 3:
            xs = median_filter([s[1] for s in samples])
            ys = median_filter([s[2] for s in samples])
            track = [(samples[i][0], int(xs[i]), int(ys[i])) for i in range(len(samples))]
        else:
            track = samples

    # ---- 2. 主循环：按轨迹线性插值每帧水印位置，逐帧修复 ----
    def pos_at(idx: int) -> Optional[Tuple[int, int]]:
        if not track:
            return None
        if idx <= track[0][0]:
            return (track[0][1], track[0][2])
        if idx >= track[-1][0]:
            return (track[-1][1], track[-1][2])
        for k in range(len(track) - 1):
            if track[k][0] <= idx <= track[k + 1][0]:
                span = max(1, track[k + 1][0] - track[k][0])
                f = (idx - track[k][0]) / span
                x = int(track[k][1] + (track[k + 1][1] - track[k][1]) * f)
                y = int(track[k][2] + (track[k + 1][2] - track[k][2]) * f)
                return (x, y)
        return None

    covered = 0
    frame_idx = 0
    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            if frame_idx % PROGRESS_STEP == 0 and total > 0:
                stage(int(frame_idx * 100 / total))
            p = pos_at(frame_idx)
            if p is not None:
                _repair_roi(
                    frame,
                    max(0, p[0] - pad),
                    max(0, p[1] - pad),
                    min(w, tw + pad * 2),
                    min(h, th + pad * 2),
                    blur,
                    tpl_mask,
                )
                covered += 1
            out.write(frame)
            frame_idx += 1
        out.release()
        cap.release()

        if frame_idx == 0:
            raise ValueError("视频无有效帧")

        # 合并原音轨并转 H.264/AAC（OpenCV 写的文件无音频）
        ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
        subprocess.run(
            [
                ffmpeg, "-y", "-loglevel", "error",
                "-i", str(tmp), "-i", str(input_path),
                "-map", "0:v:0", "-map", "1:a:0?",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-c:a", "aac", "-b:a", "128k", "-shortest",
                str(output_path),
            ],
            check=True,
        )
    finally:
        for v in (cap, out):
            try:
                v.release()
            except Exception:
                pass
        tmp.unlink(missing_ok=True)

    return {
        "ok": True,
        "output": str(output_path),
        "frames": frame_idx,
        "covered_frames": covered,
        "note": f"共 {frame_idx} 帧，{covered} 帧检测到水印并修复",
    }


# ============================================================ 命令行

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="动态水印去除（OpenCV 逐帧模板匹配）")
    parser.add_argument("--input", required=True, help="输入视频路径")
    parser.add_argument("--output", help="输出路径（默认 输入同目录加 _nowm 后缀）")
    parser.add_argument("--template", help="水印模板图路径（提供则跳过自动捕获）")
    parser.add_argument("--confidence", type=float, default=0.2, help="模板匹配阈值（0~1，默认 0.2）")
    parser.add_argument(
        "--points",
        help='轨迹标注模式：水印位置点 "帧号,x,y;帧号,x,y;..."（像素坐标，至少 2 点）。'
        "提供时跳过自动定位，直接按标注点插值轨迹修复（最可靠）。",
    )
    parser.add_argument(
        "--detect",
        action="store_true",
        help="自动检测水印轨迹模式：结合 --frames 使用，输出每帧水印坐标（供 UI「自动找水印」）",
    )
    parser.add_argument(
        "--frames",
        help='--detect 模式要检测的帧号列表，如 "85,170,255"',
    )
    parser.add_argument("--blur", type=int, default=9, help="遮盖模糊核（奇数，默认 9）")
    return parser


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = build_parser().parse_args()
    input_path = Path(args.input)
    if not input_path.exists():
        return emit({"ok": False, "error": f"输入视频不存在：{input_path}"}, 1)

    output_path = Path(args.output) if args.output else input_path.with_name(input_path.stem + "_nowm.mp4")

    try:
        # 自动检测模式：输出每帧水印坐标（UI「自动找水印」）
        if args.detect:
            frame_nums = [int(x) for x in (args.frames or "").split(",") if x.strip().isdigit()]
            if not frame_nums:
                return emit({"ok": False, "error": "--detect 模式需提供 --frames 帧号列表（如 85,170,255）"}, 1)
            try:
                result = detect_points(input_path, frame_nums, args.confidence)
            except Exception as exc:  # noqa: BLE001
                return emit({"ok": False, "error": f"自动检测水印失败：{exc}"}, 1)
            return emit(result)

        # 轨迹标注模式：跳过模板与自动定位，直接用人工标注点
        if args.points:
            points: List[Tuple[int, int, int]] = []
            for seg in args.points.split(";"):
                parts = seg.strip().split(",")
                if len(parts) != 3:
                    continue
                t, x, y = int(parts[0]), int(parts[1]), int(parts[2])
                points.append((t, x, y))
            if len(points) < 2:
                return emit({"ok": False, "error": "轨迹点不足（至少标注 2 个位置）"}, 1)
            result = remove_watermark(input_path, output_path, None, args.confidence, args.blur, track_points=points)
            return emit(result)

        if args.template:
            template = load_template(Path(args.template))
            # 用户框选的大模板（常含大片背景）裁剪到文字核心，提高匹配鲁棒性
            template = auto_crop_template(template)
        else:
            cap = cv2.VideoCapture(str(input_path))
            if not cap.isOpened():
                return emit({"ok": False, "error": f"无法打开视频：{input_path}"}, 1)
            stage(2)
            template = auto_capture_template(cap)
            cap.release()
            if template is None:
                return emit(
                    {
                        "ok": False,
                        "error": "自动定位水印失败：请在界面框选水印区域后重试",
                    },
                    1,
                )
            # 自动捕获已按「文字+上下文」尺度返回，不裁剪（半透明文字裁剪会破坏模板）

        result = remove_watermark(input_path, output_path, template, args.confidence, args.blur)
        return emit(result)
    except Exception as exc:  # noqa: BLE001
        return emit({"ok": False, "error": f"去水印失败：{exc}"}, 1)


if __name__ == "__main__":
    sys.exit(main())
