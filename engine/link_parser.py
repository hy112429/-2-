#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
link_parser.py —— 通用链接解析统一入口（二期 Step 10）

任意输入 → 平台识别 → 对应 extractor 解析 → 统一 JSON 输出。

支持输入（抖音四类已实现）：
  a) 分享短链    https://v.douyin.com/xxxx/
  b) 视频页链接  https://www.douyin.com/video/{id}
  c) 含链接的口令文本（如「7.68 复制打开抖音，看看… https://v.douyin.com/xxxx/」）
  d) 直接视频 ID

用法
    python link_parser.py --input "https://v.douyin.com/xxxx/"
    python link_parser.py --input "7.68 复制打开抖音… https://v.douyin.com/xxxx/"
    python link_parser.py --input 7300000000000000000
    echo {"cookie":"..."} | python link_parser.py --input "小红书链接"   # 登录态走 stdin，不落盘
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict

from extractors import build_registry, detect_platform, platform_name


def parse_single(raw_input: str, options: Dict[str, Any]) -> Dict[str, Any]:
    """识别平台并调用对应 extractor；未知平台给明确中文错误。"""
    platform = detect_platform(raw_input)
    registry = build_registry()
    extractor = registry.get(platform)
    if extractor is None:
        return {
            "ok": False,
            "platform": platform,
            "input": raw_input,
            "error": f"未识别的平台（{platform_name(platform)}）：请粘贴抖音/头条/小红书链接或分享文本",
        }
    return extractor(raw_input, options)


def main() -> int:
    parser = argparse.ArgumentParser(description="通用链接解析（多平台）")
    parser.add_argument("--input", required=True, help="任意输入：链接 / 口令文本 / 视频 ID")
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--max-attempts", type=int, default=8)
    parser.add_argument("--cookie", default="", help="小红书等需登录平台的 Cookie 请求头（可选）")
    args = parser.parse_args()

    # stdin 可选登录态：{"cookie": "..."} —— 与 link_batch.py 同约定，cookie 不经过进程命令行
    stdin_cookie = ""
    raw = sys.stdin.read()
    if raw.strip():
        try:
            stdin_cookie = str((json.loads(raw) or {}).get("cookie", ""))
        except Exception:
            stdin_cookie = ""

    cookie = stdin_cookie or args.cookie
    options: Dict[str, Any] = {"timeout": args.timeout, "max_attempts": args.max_attempts}
    if cookie:
        options["cookie"] = cookie
    result = parse_single(args.input, options)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
