#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
douyin_batch.py —— 抖音分享链接批量解析（T-15 补充路径）

背景
    百应「带货内容」视频为 canvas 加密拿不到（平台边界），商品主图视频每商品仅 1 条。
    用户需要「一个商品多个带货视频」的素材时，补充路径是：用户从抖音复制带货视频的
    分享链接，本脚本批量解析出无水印地址（复用 douyin_parse 的 playwm→play 去水印）。

输入（stdin）
    一行一条 JSON，或直接每行一个链接；内容为链接数组。

输出（stdout，NDJSON）
    每条解析完成时：{"type":"task","tid":"dy-1","status":"success|failed","message":"…"}
    全部结束时：    {"type":"result","total":N,"ok":K,"failed":M,
                    "items":[{"tid","input","ok","aweme_id","title","no_watermark_url","error"}]}

限速
    每条之间随机间隔（--interval-min/max），避免触发抖音 WAF 风控。
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from types import SimpleNamespace
from typing import Any, Dict, List

from douyin_parse import parse


def parse_links(lines: List[str], timeout: float, max_attempts: int) -> List[Dict[str, Any]]:
    """逐条解析链接，返回汇总条目列表。"""
    items: List[Dict[str, Any]] = []
    total = len(lines)
    for i, link in enumerate(lines):
        tid = f"dy-{i + 1}"
        # 限速：第一条直接跑，之后每条随机间隔
        if i > 0:
            interval = random.uniform(3.0, 8.0)
            time.sleep(interval)

        ns = SimpleNamespace(
            input=link,
            timeout=timeout,
            save_html=None,
            max_attempts=max_attempts,
            quiet=True,
            proxy=None,
            verify=False,
        )
        try:
            result = parse(ns)
        except Exception as exc:  # 脚本自身异常不能中断整批
            result = {"ok": False, "input": link, "error": str(exc)}

        ok = bool(result.get("ok"))
        item: Dict[str, Any] = {
            "tid": tid,
            "input": link,
            "ok": ok,
            "aweme_id": result.get("aweme_id"),
            "title": result.get("title") or "",
            "no_watermark_url": result.get("no_watermark_url"),
        }
        if ok:
            item["error"] = None
            message = (result.get("title") or "").strip() or "解析成功"
        else:
            diagnostics = result.get("diagnostics") or {}
            warnings = diagnostics.get("warnings") or []
            item["error"] = warnings[-1] if warnings else (result.get("error") or "解析失败")
            message = item["error"]

        items.append(item)
        print(
            json.dumps(
                {"type": "task", "tid": tid, "status": "success" if ok else "failed", "message": message},
                ensure_ascii=False,
            ),
            flush=True,
        )

    return items


def main() -> int:
    parser = argparse.ArgumentParser(description="抖音分享链接批量解析（去水印）")
    parser.add_argument("--timeout", type=float, default=15.0, help="单链接请求超时秒数")
    parser.add_argument("--max-attempts", type=int, default=8, help="单链接最大重试次数")
    args = parser.parse_args()

    raw = sys.stdin.read()
    links: List[str] = []
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            links = [str(x).strip() for x in parsed if str(x).strip()]
        elif isinstance(parsed, str) and parsed.strip():
            links = [parsed.strip()]
    except Exception:
        # 非 JSON：按每行一条链接处理
        links = [l.strip() for l in raw.splitlines() if l.strip()]

    if not links:
        print(
            json.dumps(
                {"type": "error", "message": "没有收到任何链接（stdin 为空或格式不正确）"},
                ensure_ascii=False,
            ),
            flush=True,
        )
        return 1

    items = parse_links(links, args.timeout, args.max_attempts)
    ok = sum(1 for it in items if it["ok"])
    failed = len(items) - ok
    print(
        json.dumps(
            {"type": "result", "total": len(items), "ok": ok, "failed": failed, "items": items},
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
