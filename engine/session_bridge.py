#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
session_bridge.py —— 接收 Electron 传来的登录态，转成可直接使用的请求头

================================================================================
传递方式（这是本文件存在的意义）
================================================================================
Electron 主进程把登录态 JSON 写到 Python 子进程的 **stdin**，结构如下：

    {
      "cookieHeader": "sessionid=xxx; sessionid_ss=yyy; ...",
      "userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ...",
      "cookieCount": 42,
      "cookieNames": ["sessionid", "sessionid_ss", ...],
      "targetId": "douyin",
      "exportedAt": "2026-10-08T16:00:00.000Z"
    }

**为什么不写文件**：
  1. 写文件就多了一份副本，还要管清理，泄漏面变大
  2. 加密文件虽然可行，但解密密钥还得让 Python 拿到，绕一圈又回到"怎么给密钥"
  3. stdin 是进程私有的，**进程列表看不到、磁盘上不留痕**，是最干净的方式

**为什么必须连 User-Agent 一起传**：
  抖音的风控会把登录态和 UA 关联。如果登录用的是桌面 Chrome 的 UA，
  而 Python 请求时用 iPhone UA，服务端可能判定会话异常并直接失效。
  所以引擎发请求时必须同时带上 cookieHeader 和 userAgent，两者不能拆开用。

**⚠️ 安全约定**：
  本脚本不会把 cookie 值打印出来（除非显式加 --show-values）。
  默认输出只包含条数、长度、名字，便于在日志里安全记录。

================================================================================
用法
================================================================================
    # 通常由 Electron 调用；也可以自己造一份 JSON 手动测
    '{"cookieHeader":"a=b","userAgent":"UA","cookieCount":1,"cookieNames":["a"]}' |
        python session_bridge.py

    # 带联网探测：会真的请求抖音一次（默认关闭，避免触发风控）
    Get-Content session.json -Raw | python session_bridge.py --probe

    # 真的把 cookie 值打出来（仅本地调试用，别把输出贴到任何地方）
    Get-Content session.json -Raw | python session_bridge.py --show-values

退出码
    0 = 成功
    1 = 失败（stdin 为空或 JSON 非法）
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict, Optional

# 探测用的目标。只做一次 GET，不做重试 —— 登录态探测没必要重试，
# 而且重试会无意义地多打一次风控。
PROBE_URL = "https://www.douyin.com/"

# 判断「被踢回登录页」的 URL 特征
LOGIN_URL_MARKERS = ("passport", "/login", "sso", "verify")


def read_session() -> Dict[str, Any]:
    """
    从 stdin 读 JSON。

    为什么 stdin 会是空的：直接双击运行、或忘了管道就会这样。
    这里要给出明确的提示，而不是让 json.loads 抛一个看不懂的错。
    """
    if sys.stdin is None or sys.stdin.isatty():
        raise ValueError(
            "stdin 没有数据。本脚本需要从标准输入读 JSON。\n"
            "手动测试示例（PowerShell）：\n"
            '  \'{"cookieHeader":"a=b","userAgent":"UA","cookieCount":1}\' | python session_bridge.py'
        )

    raw = sys.stdin.read().strip()
    if not raw:
        raise ValueError("stdin 是空的，没收到登录态 JSON")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"stdin 不是合法 JSON：{exc}") from exc

    if not isinstance(data, dict):
        raise ValueError("stdin 的 JSON 顶层必须是对象")

    return data


def build_headers(session: Dict[str, Any]) -> Dict[str, str]:
    """
    把会话转成 requests 可直接用的请求头。

    这个函数就是给引擎其他脚本（比如 douyin_parse.py）复用的入口：
        headers = build_headers(session)
        requests.get(url, headers=headers)
    """
    cookie_header = session.get("cookieHeader") or ""
    user_agent = session.get("userAgent") or ""

    headers: Dict[str, str] = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if cookie_header:
        headers["Cookie"] = cookie_header
    if user_agent:
        headers["User-Agent"] = user_agent
    return headers


def probe(headers: Dict[str, str], timeout: float) -> Dict[str, Any]:
    """
    联网探测登录态是否还在生效。

    只做一次请求，如实报告客观事实，**不要过度解读**：
      · 状态码正常 + 最终 URL 没跳到登录域 → 大概率仍然有效
      · 最终 URL 跳到 passport/login 域 → 会话已失效，需要重新扫码
    """
    try:
        import requests
    except ImportError:
        return {"ok": False, "error": "缺少 requests，请先 pip install -r requirements.txt"}

    try:
        response = requests.get(
            PROBE_URL, headers=headers, timeout=timeout, allow_redirects=True
        )
    except Exception as exc:  # noqa: BLE001 网络异常统一兜底
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    final_url = response.url or ""
    redirected_to_login = any(marker in final_url.lower() for marker in LOGIN_URL_MARKERS)

    return {
        "ok": True,
        "status": response.status_code,
        "final_url": final_url,
        "redirected_to_login": redirected_to_login,
        "content_length": len(response.content),
        "conclusion": (
            "会话可能已失效（被重定向到登录相关页面）"
            if redirected_to_login
            else "未观察到失效迹象（注意：这不等同于『已登录』，只说明没被踢）"
        ),
    }


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(
        description="接收 Electron 传来的登录态（stdin JSON），转成可直接使用的请求头",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--probe",
        action="store_true",
        help=f"联网探测会话是否仍有效（会对 {PROBE_URL} 发一次 GET）",
    )
    parser.add_argument("--timeout", type=float, default=15.0, help="探测超时秒数，默认 15")
    parser.add_argument(
        "--show-values",
        action="store_true",
        help="输出里包含 cookie 明文（仅本地调试，切勿把输出外传）",
    )
    args = parser.parse_args()

    try:
        session = read_session()
    except ValueError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1

    headers = build_headers(session)
    cookie_header: str = headers.get("Cookie", "")
    user_agent: str = headers.get("User-Agent", "")

    result: Dict[str, Any] = {
        "ok": True,
        "received": {
            "targetId": session.get("targetId"),
            "exportedAt": session.get("exportedAt"),
            "cookieCount": session.get("cookieCount"),
            "cookieNames": session.get("cookieNames"),
            "cookieHeaderLength": len(cookie_header),
            "hasUserAgent": bool(user_agent),
        },
        # 引擎实际会用的请求头（值默认打码）
        "headers_preview": {
            "Cookie": cookie_header if args.show_values else _mask_header(cookie_header),
            "User-Agent": user_agent,
        },
        "warnings": [],
    }

    if not cookie_header:
        result["warnings"].append("cookieHeader 为空 —— 还没登录，或导出时选错了目标")
    if not user_agent:
        result["warnings"].append(
            "缺少 userAgent —— 抖音风控会把登录态和 UA 关联，缺了容易导致会话失效"
        )

    if args.probe:
        if not cookie_header:
            result["probe"] = {"ok": False, "error": "没有 cookie，跳过探测"}
        else:
            result["probe"] = probe(headers, args.timeout)

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _mask_header(header: str) -> str:
    """
    只保留 cookie 的名字，值一律打码。

    这样日志里能看出「带了哪些 cookie」，但泄漏不了凭据。
    """
    if not header:
        return ""
    parts: list[str] = []
    for segment in header.split(";"):
        segment = segment.strip()
        if not segment:
            continue
        name, _, value = segment.partition("=")
        parts.append(f"{name}=***({len(value)}字符)")
    return "; ".join(parts)


if __name__ == "__main__":
    sys.exit(main())