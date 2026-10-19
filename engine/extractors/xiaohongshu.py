# -*- coding: utf-8 -*-
"""
xiaohongshu.py —— 小红书 extractor（Step 10 补充实现）

解析链路：
  分享链接/xhslink 短链 → 跳转笔记页 → 抠 window.__INITIAL_STATE__ →
  提取视频流 h264 masterUrl（小红书源视频本身无水印）。

关键点：
  · 分享链接里带 xsec_token（防爬签名），必须原样带上；无 token 直连会被验证码拦截。
  · 纯 requests 可能被风控（验证码/登录墙），失败时如实报错，不做对抗性绕过。
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional

import requests

_XHS_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# 分享文本/链接里抠出第一个 http 链接
_URL_RE = re.compile(r"https?://[^\s，,、）)】\]]+")
# SSR 数据起始标记：window.__INITIAL_STATE__ = 之后是平衡 JSON
_STATE_START_RE = re.compile(r"window\.__INITIAL_STATE__\s*=\s*")


def _extract_state(html: str) -> Optional[str]:
    """从 window.__INITIAL_STATE__ = 之后提取平衡的 JSON 文本（跳过字符串里的括号）。"""
    m = _STATE_START_RE.search(html)
    if not m:
        return None
    start = m.end()
    depth = 0
    i = start
    while i < len(html):
        ch = html[i]
        if ch == '"':
            # 跳过字符串字面量（处理反斜杠转义），避免括号被误计
            i += 1
            while i < len(html):
                if html[i] == "\\":
                    i += 2
                    continue
                if html[i] == '"':
                    break
                i += 1
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return html[start : i + 1]
        i += 1
    return None


def _pick_link(raw_input: str) -> str:
    text = (raw_input or "").strip()
    if not text:
        raise ValueError("输入为空")
    m = _URL_RE.search(text)
    if not m:
        raise ValueError("没找到可用的链接，请粘贴小红书笔记链接或分享文本")
    return m.group(0)


def _extract_master_url(state: Dict[str, Any]) -> Optional[str]:
    """从 __INITIAL_STATE__ 递归找视频流 h264/h265 的 masterUrl/backupUrls。"""
    targets: list = []

    def walk(o: Any) -> None:
        if isinstance(o, dict):
            for k, v in o.items():
                if k in ("masterUrl", "backupUrls") and isinstance(v, str):
                    targets.append(v)
                elif k == "backupUrls" and isinstance(v, list):
                    targets.extend(x for x in v if isinstance(x, str))
                else:
                    walk(v)
        elif isinstance(o, list):
            for it in o:
                walk(it)

    walk(state)
    for url in targets:
        if "xhscdn.com" in url or "xhs" in url.lower():
            return url
    return targets[0] if targets else None


def extract(raw_input: str, options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    opts = options or {}
    timeout = float(opts.get("timeout", 15.0))
    cookie_header = opts.get("cookie") or ""
    try:
        link = _pick_link(raw_input)
        headers = {
            "User-Agent": _XHS_UA,
            "Referer": "https://www.xiaohongshu.com/",
        }
        if cookie_header:
            headers["Cookie"] = cookie_header
        resp = requests.get(
            link,
            headers=headers,
            timeout=timeout,
            allow_redirects=True,
        )
        resp.raise_for_status()
        html = resp.text

        # 未登录会被重定向到登录页：此时拿不到笔记数据
        if "xiaohongshu.com/login" in resp.url:
            return {
                "ok": False,
                "platform": "xiaohongshu",
                "input": raw_input,
                "final_url": resp.url,
                "error": "需要登录小红书才能解析（请在客户端「登录 → 小红书」扫码登录后重试）",
            }

        m = _extract_state(html)
        if not m:
            return {
                "ok": False,
                "platform": "xiaohongshu",
                "input": raw_input,
                "final_url": resp.url,
                "error": "页面里没有找到 __INITIAL_STATE__（可能被验证码拦截，或该链接不是笔记页）",
            }
        # SSR 数据含 JS 字面量/表达式，转成合法 JSON
        cleaned = re.sub(r"(?<=[\{\[,:])undefined(?=[\}\]\:,])", "null", m)
        cleaned = re.sub(r"(?<=[\{\[,:])NaN(?=[\}\]\:,])", "null", cleaned)
        cleaned = re.sub(r"(?<=[\{\[,:])!1(?=[\}\]\:,])", "false", cleaned)
        cleaned = re.sub(r"(?<=[\{\[,:])!0(?=[\}\]\:,])", "true", cleaned)
        cleaned = re.sub(r"new Map\(\[\]\)", "[]", cleaned)
        cleaned = re.sub(r"new Set\(\[\]\)", "[]", cleaned)
        cleaned = re.sub(r"new Date\([^)]*\)", "null", cleaned)
        cleaned = re.sub(r"new RegExp\([^)]*\)", "null", cleaned)
        try:
            state = json.loads(cleaned)
        except json.JSONDecodeError as jerr:
            # 解析失败：把错误位置附近文本落盘，便于定位剩余非 JSON 片段
            dump = cleaned[max(0, jerr.pos - 120) : jerr.pos + 120]
            try:
                import os
                import tempfile

                with open(os.path.join(tempfile.gettempdir(), "xhs_state_dump.txt"), "w", encoding="utf-8") as f:
                    f.write(f"pos={jerr.pos}\nmsg={jerr.msg}\n---\n{dump}")
            except Exception:
                pass
            return {
                "ok": False,
                "platform": "xiaohongshu",
                "input": raw_input,
                "final_url": resp.url,
                "error": f"SSR 数据解析失败（{jerr.msg} at {jerr.pos}），现场已存 %TEMP%\\xhs_state_dump.txt",
            }
        master_url = _extract_master_url(state)
        if not master_url:
            return {
                "ok": False,
                "platform": "xiaohongshu",
                "input": raw_input,
                "final_url": resp.url,
                "error": "该笔记没有可下载的视频流（可能是图文笔记，或视频已被删除/需登录）",
            }

        # 笔记标题：/explore/ 路径用 note.noteDataMap、/discovery/item/ 路径用 note.noteDetailMap，
        # 两种页面结构都要兼容；取不到就给空
        title = ""
        note_node = state.get("note")
        if isinstance(note_node, dict):
            for map_key in ("noteDataMap", "noteDetailMap"):
                note_data = note_node.get(map_key)
                if not isinstance(note_data, dict):
                    continue
                first = next(iter(note_data.values()), None)
                if isinstance(first, dict) and isinstance(first.get("note"), dict):
                    title = (first["note"] or {}).get("title") or ""
                    if title:
                        break

        return {
            "ok": True,
            "platform": "xiaohongshu",
            "input": raw_input,
            "final_url": resp.url,
            "note_id": _extract_note_id(resp.url),
            "title": title,
            "no_watermark_url": master_url,
            "error": None,
        }
    except requests.RequestException as exc:
        return {
            "ok": False,
            "platform": "xiaohongshu",
            "input": raw_input,
            "error": f"请求失败：{exc}",
        }
    except Exception as exc:  # 解析异常给可读中文，不抛堆栈
        return {
            "ok": False,
            "platform": "xiaohongshu",
            "input": raw_input,
            "error": f"解析失败：{exc}",
        }


def _extract_note_id(final_url: str) -> Optional[str]:
    """从落地 URL 提取笔记 ID：兼容 /explore/{id} 与 /discovery/item/{id} 两种路径。"""
    m = re.search(r"/(?:explore|discovery/item)/([0-9a-zA-Z]+)", final_url)
    return m.group(1) if m else None
