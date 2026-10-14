# -*- coding: utf-8 -*-
"""
toutiao.py —— 今日头条 / 西瓜视频 extractor（Step 10 补充实现）

解析链路：
  分享链接/视频页链接 → 请求视频页 → 抠 SSR 数据（_ROUTER_DATA / __INITIAL_STATE__）→
  提取视频直链（playUrl / videoUrl / masterUrl / backupPlayerUrl，字节系 CDN 源，无水印）。

注意：
  · 头条系与抖音同源，风控规则相近：请求频率要克制，命中的是正常页面即可解析。
  · 纯 requests 被验证码拦截时如实报错，不做对抗性绕过。
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional

import requests

_TT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_URL_RE = re.compile(r"https?://[^\s，,、）)】\]]+")
# 各种 SSR 数据块
_SSR_RE = re.compile(
    r"window\.(_ROUTER_DATA|__INITIAL_STATE__|__SSR_HYDRATED_DATA__|_SSR_DATA)\s*=\s*(\{.*?\})</script>",
    re.DOTALL,
)
# 头条系视频直链域名
_TT_CDN_RE = re.compile(r"(ixigua\.com|toutiao\.com|bytecdn|byteimg|tosv\.bytedstatic|snssdk)", re.IGNORECASE)


def _pick_link(raw_input: str) -> str:
    """
    从分享文本/链接里抠出**头条系**链接（toutiao.com / ixigua.com / toutiaovod.com）。
    分享文本常夹带 google 统计等追踪 URL（g/collect?...），必须过滤掉，只认头条域名。
    """
    text = (raw_input or "").strip()
    if not text:
        raise ValueError("输入为空")
    for m in _URL_RE.finditer(text):
        link = m.group(0)
        if re.search(r"toutiao\.com|ixigua\.com|toutiaovod\.com|365yg\.com", link, re.IGNORECASE):
            return link
    raise ValueError("没找到头条系链接（请粘贴头条/西瓜视频链接，或 vod 直链 toutiaovod.com）")


def _extract_video_url(state: Any) -> Optional[str]:
    """递归提取视频直链候选，优先头条系 CDN 域名。"""
    candidates: list = []

    def walk(o: Any) -> None:
        if isinstance(o, dict):
            for k, v in o.items():
                if isinstance(v, str) and (
                    "playUrl" in k or "videoUrl" in k or "masterUrl" in k
                    or "backupPlayerUrl" in k or "video_id" in k or "main_url" in k
                ):
                    candidates.append(v)
                elif isinstance(v, list) and "url" == k:
                    candidates.extend(x for x in v if isinstance(x, str))
                else:
                    walk(v)
        elif isinstance(o, list):
            for it in o:
                walk(it)

    walk(state)
    for url in candidates:
        if _TT_CDN_RE.search(url) or url.startswith("http"):
            return url
    return candidates[0] if candidates else None


def extract(raw_input: str, options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    opts = options or {}
    timeout = float(opts.get("timeout", 15.0))
    try:
        link = _pick_link(raw_input)

        # 头条/西瓜视频直链（如 v26-web.toutiaovod.com/.../tos/...?lr=unwatermarked）可直接下载，无需解析页面
        if "toutiaovod.com" in link or "/tos/" in link or "ixigua.com" in link and "/video/" in link:
            return {
                "ok": True,
                "platform": "toutiao",
                "input": raw_input,
                "final_url": link,
                "title": "",
                "no_watermark_url": link,
                "error": None,
            }

        resp = requests.get(
            link,
            headers={"User-Agent": _TT_UA, "Referer": "https://www.ixigua.com/"},
            timeout=timeout,
            allow_redirects=True,
        )
        resp.raise_for_status()
        html = resp.text

        # 字节系 JS 防护页（_$jsvmprt 特征，同抖音 WAF）：纯 requests 拿不到真实内容
        if "_$jsvmprt" in html or "window._ROUTER_DATA" not in html and "window.__INITIAL_STATE__" not in html:
            return {
                "ok": False,
                "platform": "toutiao",
                "input": raw_input,
                "final_url": resp.url,
                "error": "头条视频页有 JS 防护（无法直接解析）。请获取视频直链：浏览器打开该视频页 → F12 → Network → 筛选 media/video → 复制类型为 video/mp4 的地址（含 toutiaovod.com），再粘贴到本面板",
            }

        m = _SSR_RE.search(html)
        if not m:
            return {
                "ok": False,
                "platform": "toutiao",
                "input": raw_input,
                "final_url": resp.url,
                "error": "页面里没有找到 SSR 数据（可能被验证码拦截，或该链接不是视频页）",
            }
        state = json.loads(m.group(2))
        video_url = _extract_video_url(state)
        if not video_url:
            return {
                "ok": False,
                "platform": "toutiao",
                "input": raw_input,
                "final_url": resp.url,
                "error": "页面里没有提取到视频直链（可能是图文内容，或视频需登录/已删除）",
            }

        return {
            "ok": True,
            "platform": "toutiao",
            "input": raw_input,
            "final_url": resp.url,
            "title": _pick_title(state),
            "no_watermark_url": video_url,
            "error": None,
        }
    except requests.RequestException as exc:
        return {
            "ok": False,
            "platform": "toutiao",
            "input": raw_input,
            "error": f"请求失败：{exc}",
        }
    except Exception as exc:
        return {
            "ok": False,
            "platform": "toutiao",
            "input": raw_input,
            "error": f"解析失败：{exc}",
        }


def _pick_title(state: Any) -> str:
    hits: list = []

    def walk(o: Any) -> None:
        if isinstance(o, dict):
            for k, v in o.items():
                if k in ("title", "desc") and isinstance(v, str):
                    hits.append(v)
                else:
                    walk(v)
        elif isinstance(o, list):
            for it in o:
                walk(it)

    walk(state)
    return hits[0] if hits else ""
