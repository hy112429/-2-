#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
download.py —— 投广管理客户端 · 视频下载与规范化处理引擎（Step 2）

职责
    把 Step 1（douyin_parse.py）解析出的「无水印播放地址」，下载并处理成符合规格的成品：
      1. 按规格命名落盘：{商品ID}_{视频ID}_{序号}_{时间戳}.mp4
      2. 目录结构：{根目录}/{账号}/{日期}/{商品ID}/
      3. FFmpeg 转码：MP4 + H.264 + AAC 128kbps，分辨率保留原始最高（≤1080p）
      4. 可选「裁剪去水印」：裁掉画面指定边缘的像素
      5. 下载显示进度；失败自动重试（指数退避 1s → 2s → 5s）
      6. 可选画质校验：SSIM / PSNR 对比源文件与成品

用法
    # 最简：只转码，不裁剪
    python download.py "https://v.douyin.com/xxxxx/" --product-id P123456 --account 主账号

    # 裁剪去水印（实测：抖音水印在左上角，见 TODO.md T-04）
    python download.py "https://v.douyin.com/xxxxx/" --product-id P123456 --account 主账号 \
        --crop-top 140 --crop-left 140

    # 裁剪 + 画质校验 + 保留原始文件
    python download.py "https://v.douyin.com/xxxxx/" --product-id P123456 --account 主账号 \
        --crop-top 140 --crop-left 140 --check-quality --keep-source

    # 批量：序号自己递增
    python download.py "链接1" --product-id P123456 --account 主账号 --index 1
    python download.py "链接2" --product-id P123456 --account 主账号 --index 2

产物
    {根目录}/{账号}/{日期}/{商品ID}/{商品ID}_{视频ID}_{序号}_{时间戳}.mp4      成品
    {根目录}/{账号}/{日期}/{商品ID}/{商品ID}_{视频ID}_{序号}_{时间戳}.src.mp4  原始文件（默认处理后删除）

退出码
    0 = 成功
    1 = 失败（看输出的 error 字段）
    2 = 参数/用法错误
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from .client_paths import DEFAULT_DOWNLOAD_ROOT
except ImportError:
    from client_paths import DEFAULT_DOWNLOAD_ROOT

import requests

# 复用 Step 1 的解析逻辑。download.py 必须和 douyin_parse.py 放在同一个目录。
try:
    import douyin_parse
except ImportError:
    print(
        "错误：找不到 douyin_parse.py。请确认 download.py 和 douyin_parse.py 在同一个目录下。",
        file=sys.stderr,
    )
    sys.exit(2)


# ============================================================ 常量与默认值

# 重试退避间隔，按你指定的 1s → 2s → 5s。
# 注意：严格数学意义上的指数退避是 1/2/4/8，这里按你的规格用 1/2/5。
RETRY_DELAYS: Tuple[float, ...] = (1.0, 2.0, 5.0)

# 默认根目录（对应规格里的「根目录」）。由 Electron 运行时注入安装版或开发态目录。
DEFAULT_ROOT = DEFAULT_DOWNLOAD_ROOT

# 下载分块大小：256KB。太小会频繁写盘，太大进度条不流畅。
CHUNK_SIZE = 256 * 1024

# 分辨率上限：短视频多为竖屏，所以「1080p」要按方向理解——
# 横屏上限 1920x1080，竖屏上限 1080x1920。这样竖屏 1080x1920 不会被误压成 607x1080。
MAX_LONG_SIDE = 1920
MAX_SHORT_SIDE = 1080

# Windows 文件名非法字符
ILLEGAL_PATH_CHARS = re.compile(r'[\\/:*?"<>|\r\n\t]')

# ffmpeg -progress 输出的时间行。
# 只认 out_time_us：它旁边那个 out_time_ms 在 ffmpeg 一个长期存在的怪癖里
# 实际填的也是微秒值，两个都读会让进度来回抽搐，所以直接忽略 ms。
FFMPEG_TIME_RE = re.compile(r"^out_time_us=(\d+)$")

# ffmpeg -progress 输出的速度行，形如 speed=1.23x 或 speed=N/A
FFMPEG_SPEED_RE = re.compile(r"^speed=\s*(\S+)$")

# SSIM 结果行形如 "SSIM Y:0.99 U:0.99 V:0.99 All:0.991234 (19.5)"
SSIM_RE = re.compile(r"All:\s*([0-9.]+)")

# PSNR 结果行形如 "PSNR y:40.1 u:42.3 v:42.1 average:40.62 min:..."
PSNR_RE = re.compile(r"average:\s*([0-9.]+)")


# ============================================================ 小工具


def format_size(num_bytes: float) -> str:
    """把字节数变成人看得懂的大小。"""
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{int(value)}B" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}GB"


def build_bar(percent: float, width: int = 24) -> str:
    """生成 [####....] 形式的文本进度条。"""
    percent = max(0.0, min(100.0, percent))
    filled = int(width * percent / 100)
    return "[" + "#" * filled + "." * (width - filled) + "]"


def sanitize_component(value: str, fallback: str = "unknown") -> str:
    """
    清理路径片段。

    为什么必须做：商品ID/账号是人工输入的，一旦含 \\ / : * ? " < > | 这些字符，
    Windows 建目录会直接失败；含 .. 还可能跑到目录外面去。
    """
    cleaned = ILLEGAL_PATH_CHARS.sub("_", str(value or ""))
    cleaned = cleaned.strip().strip(".")          # 去掉首尾空格和点，Windows 不允许结尾是点
    return cleaned[:80] or fallback


def render_command(cmd: List[str]) -> str:
    """把命令列表渲染成可直接粘进 PowerShell 的一行（含空格的参数加引号）。"""
    return " ".join(f'"{token}"' if " " in token else token for token in cmd)


def print_progress(label: str, done: int, total: int, started: float) -> None:
    """原地刷新一行进度（\\r 回到行首重写）。"""
    elapsed = max(time.monotonic() - started, 1e-6)
    speed = done / elapsed
    if total > 0:
        percent = done * 100.0 / total
        text = (
            f"\r{label} {build_bar(percent)} {percent:5.1f}%  "
            f"{format_size(done)}/{format_size(total)}  {format_size(speed)}/s"
        )
    else:
        text = f"\r{label} {format_size(done)}  {format_size(speed)}/s"
    sys.stdout.write(text)
    sys.stdout.flush()


def print_time_progress(
    label: str, seconds: float, duration_sec: Optional[float], speed_text: str = ""
) -> None:
    """
    按「时间」刷新的进度（转码专用）。

    为什么不能复用 print_progress：那个函数把 done 当成字节数换算成 KB/MB，
    而 ffmpeg 给的是「已处理了多少秒的视频」。硬套会把秒数当字节打印出
    「4.9KB/4.9KB」这种胡说八道的结果（已实测踩坑）。
    """
    if duration_sec and duration_sec > 0:
        percent = min(100.0, seconds * 100.0 / duration_sec)
        text = (
            f"\r{label} {build_bar(percent)} {percent:5.1f}%  "
            f"{seconds:6.1f}s/{duration_sec:.1f}s"
        )
    else:
        text = f"\r{label} 已处理 {seconds:6.1f}s"

    if speed_text:
        text += f"  {speed_text}"

    sys.stdout.write(text)
    sys.stdout.flush()


def finish_progress_line() -> None:
    """进度条结束后换行，避免后续日志黏在同一行。"""
    sys.stdout.write("\n")
    sys.stdout.flush()


# ============================================================ 外部工具探测


def resolve_tool(name: str, override: Optional[str] = None) -> str:
    """定位 ffmpeg / ffprobe。优先级：--参数 > 随包 <引擎目录>/bin/<name>.exe > PATH。"""
    if override:
        path = Path(override)
        if not path.exists():
            raise FileNotFoundError(f"指定的 {name} 不存在：{override}")
        return str(path)

    # 打包环境：ffmpeg/ffprobe 随 engine 一起内置在 <engine>/bin/ 下（甲方机器无 WinGet 路径）
    bundled = Path(__file__).resolve().parent / "bin" / (name + ".exe")
    if bundled.exists():
        return str(bundled)

    found = shutil.which(name)
    if not found:
        raise FileNotFoundError(
            f"找不到 {name}。请确认它已安装并在 PATH 里，或用 --{name} 参数指定完整路径。"
        )
    return found


# ============================================================ 媒体探测（ffprobe）


def parse_frame_rate(rate: Optional[str]) -> Optional[float]:
    """把 ffprobe 的 '30000/1001' 转成 29.97。"""
    if not rate or "/" not in rate:
        return None
    numerator, _, denominator = rate.partition("/")
    try:
        denom = float(denominator)
        return round(float(numerator) / denom, 3) if denom else None
    except ValueError:
        return None


def ffprobe_media(ffprobe_path: str, media: Path) -> Dict[str, Any]:
    """
    用 ffprobe 读媒体信息。

    为什么需要它：
      1. 裁剪必须在原分辨率上按像素做，不先知道宽高就没法算裁剪框；
      2. 判断「是否需要缩放」必须先知道原始分辨率；
      3. 回答「画质是否损失」也要靠它对比前后分辨率/码率。
    """
    command = [
        ffprobe_path,
        "-v", "error",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(media),
    ]
    completed = subprocess.run(
        command, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if completed.returncode != 0:
        raise RuntimeError(f"ffprobe 读取失败：{completed.stderr.strip()[:300]}")

    data = json.loads(completed.stdout or "{}")
    streams = data.get("streams") or []
    fmt = data.get("format") or {}

    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    duration = fmt.get("duration")
    if duration is None and video:
        duration = video.get("duration")

    return {
        "width": int(video["width"]) if video and video.get("width") else None,
        "height": int(video["height"]) if video and video.get("height") else None,
        "duration_sec": float(duration) if duration else None,
        "video_codec": (video or {}).get("codec_name"),
        "pix_fmt": (video or {}).get("pix_fmt"),
        "fps": parse_frame_rate((video or {}).get("r_frame_rate")),
        "has_audio": audio is not None,
        "audio_codec": (audio or {}).get("codec_name"),
        "audio_sample_rate": int(audio["sample_rate"]) if audio and audio.get("sample_rate") else None,
        "audio_channels": (audio or {}).get("channels"),
        "audio_bitrate": int(audio["bit_rate"]) if audio and audio.get("bit_rate") else None,
        "format_bitrate": int(fmt["bit_rate"]) if fmt.get("bit_rate") else None,
        "size_bytes": int(fmt["size"]) if fmt.get("size") else None,
    }


# ============================================================ 几何计算：裁剪与缩放


def compute_crop_box(
    width: int, height: int, top: int, right: int, bottom: int, left: int
) -> Tuple[int, int, int, int]:
    """
    按「裁掉边缘多少像素」算出 crop 滤镜的 宽:高:x:y。

    H.264 要求宽高是偶数，所以这里向下取偶——奇数尺寸会让编码器直接报错。
    """
    crop_w = width - left - right
    crop_h = height - top - bottom
    if crop_w <= 0 or crop_h <= 0:
        raise ValueError(
            f"裁剪参数过大：原图 {width}x{height}，裁掉 左{left} 右{right} 上{top} 下{bottom} "
            f"后剩 {crop_w}x{crop_h}，无效"
        )

    crop_w -= crop_w % 2      # 向下取偶
    crop_h -= crop_h % 2

    if crop_w < 16 or crop_h < 16:
        raise ValueError(f"裁剪后尺寸过小：{crop_w}x{crop_h}，至少保留 16x16")

    return crop_w, crop_h, left, top


def compute_scale_target(width: int, height: int) -> Tuple[int, int, bool]:
    """
    判断是否需要缩小到 ≤1080p，返回 (目标宽, 目标高, 是否真的需要缩放)。

    关键设计：
      · 竖屏视频（抖音绝大多数）按 1080x1920 的盒子算，横屏按 1920x1080。
        如果无脑用「长边≤1920」，竖屏 1080x1920 会被压成 607x1080，白白丢一半像素。
      · 比例上限取 min(..., 1.0) —— 只缩不放，小视频绝不上采样（上采样只会变糊）。
      · 不需要缩放时直接返回 (原尺寸, False)，从而跳过 scale 滤镜。
        跳过缩放 = 不做重采样 = 不引入额外画质损失。
    """
    if width >= height:
        box_w, box_h = MAX_LONG_SIDE, MAX_SHORT_SIDE
    else:
        box_w, box_h = MAX_SHORT_SIDE, MAX_LONG_SIDE

    ratio = min(box_w / width, box_h / height, 1.0)

    if ratio >= 1.0:
        return width, height, False

    target_w = max(2, int(width * ratio) // 2 * 2)
    target_h = max(2, int(height * ratio) // 2 * 2)
    return target_w, target_h, True


def build_video_filter(
    crop_width: int, crop_height: int, crop_x: int, crop_y: int,
    need_crop: bool, target_w: int, target_h: int, need_scale: bool,
) -> Optional[str]:
    """
    拼装 -vf 滤镜链。顺序是「先裁剪、后缩放」，这个顺序不能反：

      · 先裁剪：你在播放器里量的像素值就是原图像素，裁完剩下的像素原封不动。
      · 后缩放：只有裁完还超过 1080p 才缩，且用 lanczos（缩小场景下最锐利的重采样）。
    """
    filters: List[str] = []
    if need_crop:
        filters.append(f"crop={crop_width}:{crop_height}:{crop_x}:{crop_y}")
    if need_scale:
        filters.append(f"scale={target_w}:{target_h}:flags=lanczos")
    return ",".join(filters) if filters else None


# ============================================================ 下载（进度 + 重试）


def assert_is_mp4(path: Path) -> None:
    """
    确认下载到的是真视频。

    为什么必须查：抖音的播放地址失效时，可能返回 HTTP 200 但内容是空的或 HTML 错误页。
    只看状态码会把它当成功，最后 ffmpeg 报一堆看不懂的错。
    判断依据：MP4 文件第 4~8 字节固定是 'ftyp'（文件类型盒）。
    """
    size = path.stat().st_size
    if size == 0:
        raise RuntimeError("下载到的是空文件（0 字节）。播放地址多半已失效或被拒绝。")

    with open(path, "rb") as handle:
        header = handle.read(16)

    if len(header) < 8:
        raise RuntimeError(f"下载到的文件过小（{size} 字节），不是有效视频。")

    if header[4:8] != b"ftyp":
        raise RuntimeError(
            f"下载到的不是 MP4（文件头 {header[:8].hex()}，正常应为 ...66747970）。"
            f"多半是播放地址已失效、返回了错误页。"
        )


def download_once(
    session: requests.Session, url: str, dest: Path, timeout: float, quiet: bool
) -> Dict[str, Any]:
    """执行一次下载，成功返回统计信息，失败抛异常。"""
    headers = douyin_parse.build_headers("https://www.douyin.com/")

    with session.get(
        url, headers=headers, timeout=timeout, stream=True, allow_redirects=True
    ) as response:
        if response.status_code != 200:
            raise RuntimeError(f"HTTP {response.status_code}")

        total = int(response.headers.get("Content-Length") or 0)
        written = 0
        started = time.monotonic()

        with open(dest, "wb") as handle:
            for chunk in response.iter_content(CHUNK_SIZE):
                if not chunk:
                    continue
                handle.write(chunk)
                written += len(chunk)
                if not quiet:
                    print_progress("下载", written, total, started)

        if not quiet:
            finish_progress_line()

        if total > 0 and written != total:
            raise RuntimeError(
                f"下载不完整：期望 {format_size(total)}，实际 {format_size(written)}"
            )

    assert_is_mp4(dest)

    return {
        "bytes": written,
        "seconds": round(time.monotonic() - started, 2),
        "final_url": response.url,
        "content_type": response.headers.get("Content-Type"),
    }


def download_with_retry(
    session: requests.Session,
    url: str,
    dest: Path,
    max_retries: int,
    timeout: float,
    quiet: bool,
) -> Dict[str, Any]:
    """
    下载并自动重试。

    重试节奏按你的规格：第 1 次失败等 1s，第 2 次等 2s，第 3 次等 5s。
    max_retries=3 表示最多重试 3 次（总尝试 4 次）。
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    last_error: Optional[Exception] = None

    for attempt in range(max_retries + 1):
        if attempt > 0:
            delay = RETRY_DELAYS[min(attempt - 1, len(RETRY_DELAYS) - 1)]
            print(f"[重试 {attempt}/{max_retries}] {delay:.0f}s 后重试…（原因：{last_error}）")
            time.sleep(delay)

        try:
            info = download_once(session, url, dest, timeout, quiet)
            info["attempts"] = attempt + 1
            return info
        except Exception as exc:                      # noqa: BLE001 需要兜住所有网络异常
            last_error = exc
            print(f"[失败] 第 {attempt + 1} 次尝试：{type(exc).__name__}: {exc}")
            if dest.exists():
                dest.unlink()                         # 清掉半截文件，避免下次误判

    raise RuntimeError(f"下载失败，已重试 {max_retries} 次。最后原因：{last_error}")


# ============================================================ FFmpeg 转码


def run_ffmpeg_with_progress(
    command: List[str], duration_sec: Optional[float], quiet: bool
) -> None:
    """
    运行 ffmpeg 并显示进度。

    为什么要单开线程读 stderr：
      ffmpeg 会往 stderr 写大量日志，管道缓冲区（通常 64KB）写满后 ffmpeg 会阻塞，
      而主线程又在等 stdout —— 双方互等就死锁，表现为「卡住不动」。
      所以必须让 stderr 有独立的消费线程。
    """
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )

    stderr_lines: List[str] = []

    def drain_stderr() -> None:
        for line in process.stderr:                   # type: ignore[union-attr]
            stderr_lines.append(line)

    drain_thread = threading.Thread(target=drain_stderr, daemon=True)
    drain_thread.start()

    speed_text = ""

    for line in process.stdout:                       # type: ignore[union-attr]
        stripped = line.strip()

        # ffmpeg 的 speed= 行会先于 out_time_us 出现，先记下来供进度行复用。
        # 刚启动时它还没测出速度，会输出 speed=N/A，这种就显示成空，别把 N/A 亮给用户。
        speed_match = FFMPEG_SPEED_RE.match(stripped)
        if speed_match:
            value = speed_match.group(1)
            speed_text = "" if value.upper().startswith("N/A") else f"速度 {value}"
            continue

        time_match = FFMPEG_TIME_RE.match(stripped)
        if not time_match:
            continue

        seconds = int(time_match.group(1)) / 1_000_000
        if not quiet:
            print_time_progress("转码", seconds, duration_sec, speed_text)

    return_code = process.wait()
    drain_thread.join(timeout=5)
    if not quiet:
        finish_progress_line()

    if return_code != 0:
        tail = "".join(stderr_lines[-12:]).strip()
        raise RuntimeError(f"FFmpeg 退出码 {return_code}。日志尾部：\n{tail}")


def build_transcode_command(
    ffmpeg_path: str,
    source: Path,
    target: Path,
    video_filter: Optional[str],
    crf: int,
    preset: str,
) -> List[str]:
    """组装转码命令。参数含义见 README/对话里的逐参数解释。"""
    command = [
        ffmpeg_path,
        "-hide_banner",          # 不打印版权横幅，日志更干净
        "-y",                    # 覆盖已存在的输出文件
        "-i", str(source),       # 输入：下载到的原始视频
        "-map", "0:v:0",         # 只取第 1 路视频流
        "-map", "0:a:0?",        # 取第 1 路音频流；末尾 ? 表示「没有音轨也不报错」
    ]

    if video_filter:
        command += ["-vf", video_filter]   # 视频滤镜链：crop / scale

    command += [
        "-c:v", "libx264",       # 视频编码器：H.264
        "-preset", preset,       # 编码速度档：越快压缩率越低
        "-crf", str(crf),        # 画质档：数值越小越清晰、文件越大
        "-pix_fmt", "yuv420p",   # 像素格式：浏览器/剪辑软件兼容性最好的格式
        "-c:a", "aac",           # 音频编码器：AAC
        "-b:a", "128k",          # 音频码率：规格要求的 128kbps
        "-movflags", "+faststart",  # 把元数据挪到文件头，网页可边下边播
        "-progress", "pipe:1",   # 把进度输出到 stdout（机器可读 key=value）
        "-nostats",              # 关掉默认的刷屏进度统计
        str(target),
    ]
    return command


# ============================================================ 画质校验（SSIM / PSNR）


def check_quality(
    ffmpeg_path: str,
    reference: Path,
    target: Path,
    reference_filter: Optional[str],
    sample_seconds: float = 10.0,
) -> Dict[str, Any]:
    """
    用 SSIM / PSNR 量化「转码到底损失了多少」。

    关键点：成品可能被裁剪过，尺寸和源不一样，直接比对会失败。
    所以先给「源」套上同一套几何滤镜（裁剪 + 缩放），把两边拉到同尺寸再比。
    这样测出来的就是「纯编码损失」，把裁剪的影响排除掉了。

    只抽样前 sample_seconds 秒，够用且快。
    """
    ref_prefix = f"{reference_filter}," if reference_filter else ""

    def run(metric: str) -> Tuple[Optional[float], str]:
        graph = (
            f"[0:v]{ref_prefix}setpts=PTS-STARTPTS[ref];"
            f"[1:v]setpts=PTS-STARTPTS[out];"
            f"[ref][out]{metric}"
        )
        command = [
            ffmpeg_path,
            "-hide_banner",
            "-nostats",
            "-t", str(sample_seconds),
            "-i", str(reference),
            "-t", str(sample_seconds),
            "-i", str(target),
            "-lavfi", graph,
            "-an",
            "-f", "null", "-",
        ]
        completed = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8", errors="replace"
        )
        output = (completed.stderr or "") + (completed.stdout or "")
        pattern = SSIM_RE if metric == "ssim" else PSNR_RE
        found = pattern.search(output)
        if not found:
            return None, output.strip()[-400:]
        return float(found.group(1)), ""

    ssim_value, ssim_log = run("ssim")
    psnr_value, psnr_log = run("psnr")

    if ssim_value is None:
        return {
            "ok": False,
            "error": "SSIM 计算失败（看 ssim_log）",
            "ssim_log": ssim_log,
        }

    if ssim_value >= 0.99:
        verdict = "几乎无损，肉眼不可分辨"
    elif ssim_value >= 0.98:
        verdict = "极轻微损失，正常观看看不出来"
    elif ssim_value >= 0.95:
        verdict = "有轻微损失，放大或盯细节能看出来"
    else:
        verdict = "损失明显，建议调低 --crf（如 16）或换更慢的 preset"

    return {
        "ok": True,
        "sample_seconds": sample_seconds,
        "note": "已对源视频套用相同裁剪/缩放，因此该分数只反映编码损失，不含裁剪影响",
        "ssim": round(ssim_value, 5),
        "psnr_db": round(psnr_value, 2) if psnr_value is not None else None,
        "verdict": verdict,
    }


# ============================================================ 命名与目录规则


def build_output_paths(
    root: Path,
    account: str,
    date_text: str,
    product_id: str,
    video_id: str,
    index: int,
    timestamp: str,
) -> Dict[str, Path]:
    """
    按规格拼路径：

      {根目录}/{账号}/{日期}/{商品ID}/
      {商品ID}_{视频ID}_{序号}_{时间戳}.mp4
    """
    target_dir = (
        root
        / sanitize_component(account, "默认账号")
        / sanitize_component(date_text, "未知日期")
        / sanitize_component(product_id, "未知商品")
    )

    # 序号补零到 2 位：这样 01..09 在文件管理器里不会排到 10 后面
    index_text = f"{index:02d}"
    base_name = "_".join(
        [
            sanitize_component(product_id, "未知商品"),
            sanitize_component(video_id, "未知视频"),
            index_text,
            sanitize_component(timestamp, "0"),
        ]
    )

    return {
        "dir": target_dir,
        "final": target_dir / f"{base_name}.mp4",
        "source": target_dir / f"{base_name}.src.mp4",
    }


# ============================================================ 主流程


def process(args: argparse.Namespace) -> Dict[str, Any]:
    """完整流程：解析 → 下载 → 探测 → 裁剪/缩放 → 转码 → 校验。"""
    ffmpeg_path = resolve_tool("ffmpeg", args.ffmpeg)
    ffprobe_path = resolve_tool("ffprobe", args.ffprobe)

    # ---- 1. 复用 Step 1 的解析，拿无水印地址
    print("[1/5] 解析分享链接…")
    parse_args = argparse.Namespace(
        input=args.input,
        timeout=args.timeout,
        verify=False,
        save_html=None,
        proxy=args.proxy,
    )
    parsed = douyin_parse.parse(parse_args)

    if not parsed.get("ok"):
        return {
            "ok": False,
            "stage": "parse",
            "error": "解析失败，请先按 Step 1 的排查流程处理",
            "parse_result": parsed,
        }

    play_url = parsed.get("no_watermark_url") or parsed.get("diagnostics", {}).get("uri_built_url")
    if not play_url:
        return {
            "ok": False,
            "stage": "parse",
            "error": "解析结果里没有可用的无水印地址",
            "parse_result": parsed,
        }

    video_id = parsed.get("aweme_id") or "未知视频"
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    paths = build_output_paths(
        root=Path(args.root),
        account=args.account,
        date_text=args.date,
        product_id=args.product_id,
        video_id=video_id,
        index=args.index,
        timestamp=timestamp,
    )
    paths["dir"].mkdir(parents=True, exist_ok=True)

    print(f"      标题：{parsed.get('title')}")
    print(f"      作者：{(parsed.get('author') or {}).get('nickname')}")
    print(f"      无水印地址：{play_url[:100]}…")
    print(f"      目标目录：{paths['dir']}")

    session = douyin_parse.new_session(args.proxy)

    try:
        # ---- 2. 下载
        print("[2/5] 下载原始文件…")
        download_info = download_with_retry(
            session=session,
            url=play_url,
            dest=paths["source"],
            max_retries=args.max_retries,
            timeout=args.timeout,
            quiet=args.quiet,
        )
        print(
            f"      完成：{format_size(download_info['bytes'])}，"
            f"耗时 {download_info['seconds']}s，尝试 {download_info['attempts']} 次"
        )

        # ---- 3. 探测源文件
        print("[3/5] 探测源文件规格…")
        source_info = ffprobe_media(ffprobe_path, paths["source"])
        source_w = source_info["width"]
        source_h = source_info["height"]
        if not source_w or not source_h:
            raise RuntimeError("读不到源视频的分辨率，ffprobe 返回异常")
        print(
            f"      源：{source_w}x{source_h}  {source_info['video_codec']}  "
            f"{source_info['fps']}fps  {source_info['duration_sec']:.1f}s  "
            f"音轨={'有' if source_info['has_audio'] else '无'}"
        )

        # ---- 4. 计算裁剪 / 缩放
        need_crop = any([args.crop_top, args.crop_right, args.crop_bottom, args.crop_left])
        if need_crop:
            crop_w, crop_h, crop_x, crop_y = compute_crop_box(
                source_w, source_h,
                args.crop_top, args.crop_right, args.crop_bottom, args.crop_left,
            )
            print(
                f"      裁剪：裁掉 左{args.crop_left} 右{args.crop_right} "
                f"上{args.crop_top} 下{args.crop_bottom} → {crop_w}x{crop_h}"
            )
        else:
            crop_w, crop_h, crop_x, crop_y = source_w, source_h, 0, 0

        target_w, target_h, need_scale = compute_scale_target(crop_w, crop_h)
        if need_scale:
            print(f"      缩放：{crop_w}x{crop_h} → {target_w}x{target_h}（超过 1080p 上限）")
        else:
            print(f"      缩放：不需要，保持 {crop_w}x{crop_h}（不重采样，无额外损失）")

        video_filter = build_video_filter(
            crop_w, crop_h, crop_x, crop_y, need_crop,
            target_w, target_h, need_scale,
        )

        # ---- 5. 转码
        print("[4/5] FFmpeg 转码…")
        command = build_transcode_command(
            ffmpeg_path=ffmpeg_path,
            source=paths["source"],
            target=paths["final"],
            video_filter=video_filter,
            crf=args.crf,
            preset=args.preset,
        )
        print(f"      命令：{render_command(command)}")
        run_ffmpeg_with_progress(command, source_info["duration_sec"], args.quiet)

        output_info = ffprobe_media(ffprobe_path, paths["final"])
        print(
            f"      成品：{output_info['width']}x{output_info['height']}  "
            f"{output_info['video_codec']}  {output_info['pix_fmt']}  "
            f"音轨={output_info['audio_codec'] or '无'}"
        )

        # ---- 画质校验（可选）
        quality = None
        if args.check_quality:
            print("[5/5] 画质校验（SSIM / PSNR）…")
            quality = check_quality(
                ffmpeg_path=ffmpeg_path,
                reference=paths["source"],
                target=paths["final"],
                reference_filter=video_filter,
            )
            if quality.get("ok"):
                print(
                    f"      SSIM {quality['ssim']}  PSNR {quality['psnr_db']}dB"
                    f"  → {quality['verdict']}"
                )
            else:
                print(f"      校验失败：{quality.get('error')}")
        else:
            print("[5/5] 跳过画质校验（加 --check-quality 可开启）")

        result = {
            "ok": True,
            "stage": "done",
            "product_id": args.product_id,
            "account": args.account,
            "video_id": video_id,
            "index": args.index,
            "title": parsed.get("title"),
            "author": (parsed.get("author") or {}).get("nickname"),
            "output_path": str(paths["final"]),
            "output_size_bytes": paths["final"].stat().st_size,
            "source_path": str(paths["source"]),
            "crop": {
                "applied": need_crop,
                "top": args.crop_top,
                "right": args.crop_right,
                "bottom": args.crop_bottom,
                "left": args.crop_left,
            },
            "source_media": source_info,
            "output_media": output_info,
            "download": download_info,
            "quality": quality,
            "ffmpeg_command": render_command(command),
        }

        # 清理原始文件（--keep-source 时保留，便于反复裁剪和画质对比）
        if not args.keep_source:
            paths["source"].unlink(missing_ok=True)
            result["source_path"] = None
            result["source_kept"] = False
        else:
            result["source_kept"] = True

        return result

    except Exception as exc:
        # 失败时保留已下载的原始文件，方便排查，不要删
        return {
            "ok": False,
            "stage": "process",
            "error": f"{type(exc).__name__}: {exc}",
            "source_path": str(paths["source"]) if paths["source"].exists() else None,
        }


# ============================================================ 命令行入口


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="下载抖音无水印视频，按规格命名落盘，并转码为 MP4/H.264/AAC 128k",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument("input", help="抖音分享链接 / 整段分享文本 / 纯数字视频 ID")

    # --- 命名与目录（对应规格） ---
    group_name = parser.add_argument_group("命名与目录")
    group_name.add_argument("--product-id", required=True, help="商品 ID，用于命名和目录")
    group_name.add_argument("--account", required=True, help="账号名，用于目录第二层")
    group_name.add_argument("--index", type=int, default=1, help="同一商品下的第几条视频，默认 1（会补零成 01）")
    group_name.add_argument("--root", default=DEFAULT_ROOT, help=f"根目录，默认 {DEFAULT_ROOT}")
    group_name.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"), help="日期目录，默认今天")

    # --- 裁剪去水印 ---
    group_crop = parser.add_argument_group("裁剪去水印（单位：原图像素）")
    group_crop.add_argument("--crop-top", type=int, default=0, help="裁掉顶部像素数（实测抖音水印在左上角，见 T-04）")
    group_crop.add_argument("--crop-right", type=int, default=0, help="裁掉右侧像素数")
    group_crop.add_argument("--crop-bottom", type=int, default=0, help="裁掉底部像素数")
    group_crop.add_argument("--crop-left", type=int, default=0, help="裁掉左侧像素数（实测抖音水印在左上角，见 T-04）")

    # --- 编码参数 ---
    group_codec = parser.add_argument_group("编码参数")
    group_codec.add_argument("--crf", type=int, default=20, help="H.264 画质档，越小越清晰，默认 20（推荐 18-23）")
    group_codec.add_argument("--preset", default="medium", help="编码速度档，默认 medium（可选 veryfast/fast/slow）")

    # --- 运行控制 ---
    group_run = parser.add_argument_group("运行控制")
    group_run.add_argument("--max-retries", type=int, default=3, help="下载最大重试次数，默认 3（退避 1s/2s/5s）")
    group_run.add_argument("--timeout", type=float, default=30.0, help="下载超时秒数，默认 30")
    group_run.add_argument("--keep-source", action="store_true", help="保留下载的原始文件（.src.mp4）")
    group_run.add_argument("--check-quality", action="store_true", help="用 SSIM/PSNR 量化转码损失")
    group_run.add_argument("--quiet", action="store_true", help="不显示进度条")
    group_run.add_argument("--ffmpeg", help="ffmpeg 完整路径（PATH 里找不到时用）")
    group_run.add_argument("--ffprobe", help="ffprobe 完整路径（PATH 里找不到时用）")
    group_run.add_argument("--proxy", help="代理地址，如 http://127.0.0.1:7890")

    return parser


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    args = build_parser().parse_args()

    try:
        result = process(args)
    except Exception as exc:
        result = {"ok": False, "stage": "unhandled", "error": f"{type(exc).__name__}: {exc}"}

    print()
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
