# -*- coding: utf-8 -*-
"""
extractors —— 多平台链接识别与解析（Step 10）

职责
    把「任意输入」识别为具体平台，路由到对应 extractor 解析。
    平台改版时只改对应 extractor，不动主流程（link_parser.py）。

识别规则集中在这里，便于维护。
"""

from __future__ import annotations

import re
from typing import Dict, Optional

# ============================================================ 平台识别规则

# 抖音：域名 douyin.com / v.douyin.com / ics.douyin.com；以及口令中的「抖音」与视频 CDN
DOUYIN_RE = re.compile(
    r"(?:douyin\.com|v\.douyin\.com|iesdouyin\.com|aweme\.snssdk\.com|zjcdn\.com|抖音)",
    re.IGNORECASE,
)

# 今日头条：ixigua.com（西瓜视频）/ toutiao.com / 365yg.com / toutiaovod.com（视频直链 CDN）
TOUTIAO_RE = re.compile(
    r"(?:ixigua\.com|toutiao\.com|365yg\.com|toutiaovod\.com|今日头条|西瓜视频)", re.IGNORECASE
)

# 小红书：xiaohongshu.com / xhslink.com / xhscdn.com（视频直链 CDN）
XIAOHONGSHU_RE = re.compile(
    r"(?:xiaohongshu\.com|xhslink\.com|xhscdn\.com|小红书)", re.IGNORECASE
)

# 纯数字 ID：默认视为抖音视频 ID
PURE_ID_RE = re.compile(r"^\d{5,}$")


def detect_platform(raw_input: str) -> str:
    """
    识别输入所属平台。返回 'douyin' / 'toutiao' / 'xiaohongshu' / 'unknown'。
    纯数字 ID 默认按抖音处理（当前唯一已实现的 extractor）。
    """
    text = (raw_input or "").strip()
    if not text:
        return "unknown"
    if PURE_ID_RE.fullmatch(text):
        return "douyin"
    if XIAOHONGSHU_RE.search(text):
        return "xiaohongshu"
    if TOUTIAO_RE.search(text):
        return "toutiao"
    if DOUYIN_RE.search(text):
        return "douyin"
    return "unknown"


def platform_name(platform: str) -> str:
    return {
        "douyin": "抖音",
        "toutiao": "今日头条/西瓜视频",
        "xiaohongshu": "小红书",
    }.get(platform, "未知平台")


# ============================================================ extractor 注册表

from typing import Callable

Extractor = Callable[[str, Dict], Dict]


def build_registry() -> Dict[str, Extractor]:
    from . import douyin  # 延迟导入，避免循环
    from . import toutiao, xiaohongshu

    return {
        "douyin": douyin.extract,
        "toutiao": toutiao.extract,
        "xiaohongshu": xiaohongshu.extract,
    }
