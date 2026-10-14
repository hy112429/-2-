#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
link_batch.py —— 通用链接批量解析（二期 Step 10 批量版）

stdin 读取 JSON：{"links": ["https://v.douyin.com/xx/", "https://www.xiaohongshu.com/explore/xx", ...],
                 "cookie": "小红书登录 Cookie 头（可选，仅小红书需要）", "timeout": 15}

逐条走 extractors（自动识别平台），每条之间随机间隔限速（防风控），
NDJSON 逐条上报 + 汇总。抖音支持四类输入；小红书需登录态 cookie。
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import tempfile
import time
from typing import Any, Dict, List

from extractors import build_registry, detect_platform, platform_name

# 调试日志：每次解析结果落盘，便于 UI 侧看不到错误时排查
_DEBUG_LOG = os.path.join(tempfile.gettempdir(), "link_batch.log")


def _debug(*parts: Any) -> None:
    try:
        with open(_DEBUG_LOG, "a", encoding="utf-8") as f:
            f.write(" ".join(str(p) for p in parts) + "\n")
    except Exception:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description="通用链接批量解析（多平台）")
    parser.parse_args()

    raw = sys.stdin.read()
    try:
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        data = {}
    links = data.get("links") or []
    if not isinstance(links, list):
        links = []
    links = [str(x).strip() for x in links if str(x).strip()]
    cookie = data.get("cookie") or ""
    timeout = float(data.get("timeout", 15.0))

    if not links:
        print(json.dumps({"type": "error", "message": "没有收到任何链接（stdin 格式：{\"links\": [...]}）"}, ensure_ascii=False), flush=True)
        return 1

    registry = build_registry()
    items: List[Dict[str, Any]] = []
    total = len(links)
    for i, link in enumerate(links):
        tid = f"lk-{i + 1}"
        if i > 0:
            time.sleep(random.uniform(3.0, 8.0))

        platform = detect_platform(link)
        extractor = registry.get(platform)
        if extractor is None:
            result = {
                "ok": False,
                "platform": platform,
                "input": link,
                "error": f"未识别的平台（{platform_name(platform)}）",
            }
        else:
            opts: Dict[str, Any] = {"timeout": timeout}
            if platform == "xiaohongshu" and cookie:
                opts["cookie"] = cookie
            try:
                result = extractor(link, opts)
            except Exception as exc:
                result = {"ok": False, "platform": platform, "input": link, "error": f"解析失败：{exc}"}

        ok = bool(result.get("ok"))
        item = {
            "tid": tid,
            "input": link,
            "ok": ok,
            "platform": result.get("platform") or platform,
            "video_id": result.get("note_id") or result.get("aweme_id") or result.get("video_id"),
            "title": result.get("title") or "",
            "no_watermark_url": result.get("no_watermark_url"),
            "error": result.get("error"),
        }
        _debug(tid, "platform=", platform, "ok=", ok, "cookie_len=", len(cookie), "err=", repr(item["error"]), "url=", (item["no_watermark_url"] or "")[:80])
        items.append(item)
        message = item["title"] if ok else (item["error"] or "解析失败")
        print(
            json.dumps({"type": "task", "tid": tid, "status": "success" if ok else "failed", "message": message},
                       ensure_ascii=False),
            flush=True,
        )

    ok = sum(1 for it in items if it["ok"])
    print(
        json.dumps({"type": "result", "total": total, "ok": ok, "failed": total - ok, "items": items},
                   ensure_ascii=False),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
