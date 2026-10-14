# -*- coding: utf-8 -*-
"""
douyin.py —— 抖音 extractor（Step 10）

复用 douyin_parse.py 的完整解析链路（短链跳转 / SSR 抠数据 / playwm→play 去水印）。
平台改版时只改本文件或 douyin_parse.py。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, Optional

from douyin_parse import parse as douyin_parse


def extract(raw_input: str, options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    解析一条抖音输入，返回统一结果字典。
    options: {timeout, max_attempts, verify}
    """
    opts = options or {}
    ns = SimpleNamespace(
        input=raw_input,
        timeout=float(opts.get("timeout", 15.0)),
        save_html=None,
        max_attempts=int(opts.get("max_attempts", 8)),
        quiet=True,
        proxy=None,
        verify=bool(opts.get("verify", False)),
    )
    try:
        result = douyin_parse(ns)
    except Exception as exc:
        return {"ok": False, "platform": "douyin", "input": raw_input, "error": str(exc)}

    return {
        "ok": bool(result.get("ok")),
        "platform": "douyin",
        "input": raw_input,
        "aweme_id": result.get("aweme_id"),
        "title": result.get("title") or "",
        "author": (result.get("author") or {}).get("nickname"),
        "cover_url": result.get("cover_url"),
        "duration_ms": result.get("duration_ms"),
        "no_watermark_url": result.get("no_watermark_url"),
        "watermarked_url": result.get("watermarked_url"),
        "error": _friendly_error(result),
    }


def _friendly_error(result: Dict[str, Any]) -> Optional[str]:
    if result.get("ok"):
        return None
    diagnostics = result.get("diagnostics") or {}
    warnings = diagnostics.get("warnings") or []
    if warnings:
        return warnings[-1]
    return result.get("error") or "解析失败"
