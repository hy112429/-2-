#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
douyin_parse.py —— 抖音分享链接解析（可行性验证版）

用途
    输入一条抖音分享链接（v.douyin.com 短链，或整段分享文本），或直接给视频 ID，
    输出该视频的 标题 / 作者 / 封面 / 无水印播放地址，JSON 格式打印到标准输出。

解析链路
    1. 短链跳转     ：v.douyin.com/xxx   ->   /video/{aweme_id}
    2. 取分享页     ：https://www.iesdouyin.com/share/video/{aweme_id}/   （iPhone Safari UA）
    3. 抠 SSR 数据  ：页面里的 window._ROUTER_DATA = {...}  ->  JSON
    4. 定位视频节点 ：loaderData["video_(id)/page"].videoInfoRes.item_list[0]
    5. 无水印地址   ：play_addr.url_list 里的 playwm 替换成 play

关于重试（重要）
    抖音的 SSR 会「随机降级」：同一条链接、同一分钟内反复请求，会随机返回
    完整页（html_length≈38249，含 item_list）或空壳页（html_length≈32333，
    item_list 为空）。实测 8 次只成功 3 次（37.5%），且空壳页里没有任何风控
    关键词 —— 所以不是验证码，是服务端随机降级。脚本默认重试 8 次，
    可用 --max-attempts 调整。重试过程会打到 stderr，不污染 stdout 的 JSON。

用法
    python douyin_parse.py "https://v.douyin.com/xxxxx/"
    python douyin_parse.py "7.68 复制打开抖音... https://v.douyin.com/xxxxx/ ..."
    python douyin_parse.py 7300000000000000000
    python douyin_parse.py "https://v.douyin.com/xxxxx/" --verify
    python douyin_parse.py "https://v.douyin.com/xxxxx/" --max-attempts 15
    python douyin_parse.py "https://v.douyin.com/xxxxx/" --save-html debug.html

退出码
    0 = 解析成功（ok=true）
    1 = 解析失败（ok=false，看 diagnostics 定位原因）
    2 = 参数/用法错误
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

# ============================================================ 常量

# 移动端 User-Agent：iPhone Safari。抖音给移动端分享页返回的是带
# window._ROUTER_DATA 的 SSR 页面；桌面端 UA 常常直接跳登录/风控页。
IPHONE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.2 Mobile/15E148 Safari/604.1"
)

# 分享页模板：这是本次要验证的核心入口
SHARE_PAGE_TMPL = "https://www.iesdouyin.com/share/video/{aweme_id}/"

# 无水印播放地址模板：当页面只给了 uri（视频内部标识）时，用它自己拼一条
PLAY_URL_TMPL = (
    "https://www.douyin.com/aweme/v1/play/?video_id={uri}&ratio=1080p&line=0"
)

# 风控/验证页特征词。命中说明拿到的不是正常分享页。
# 注意：这里只放「正常页面不会出现」的词。像 secsdk、captcha 这类是抖音自己
# 的 JS 包名/类名，正常页面也有，放进来会误报（已实测踩坑，勿加）。
BLOCK_HINT_RE = re.compile(
    r"(访问过于频繁|操作频繁|请完成安全验证|拖动滑块|请输入验证码|verify-center)",
)

# 抖音 WAF（Web 应用防火墙）的 JavaScript 挑战页特征。
#
# 实测抓到的页面（html_length ≈ 2492）长这样：
#   <script src="https://lf-waf-js.byted-static.com/obj/waf-jschallenge/out-sha256.js">
#   window.WAFJS('context.merge', { ..., webdriver: navigator.webdriver, ... })
#   expect === s256(prefix, ""+i) && [找 i 通过] && document.cookie = "_wafchallengeid=..."
#   Please wait...
#
# 它要求客户端执行 JS 暴力搜索 SHA-256 前像（工作量证明）再写 cookie 刷新。
# 结论：**纯 requests 客户端无法通过**，重试也没用，必须换 Playwright 真浏览器
# 或等封锁衰减。本项目不实现这个挑战的绕过 —— 那属于对抗性反爬绕过。
WAF_CHALLENGE_RE = re.compile(
    r"(waf-jschallenge|lf-waf-js\.byted-static\.com|_wafchallengeid|WAFJS)",
)

# 从链接/文本里提取 aweme_id（视频唯一 ID）的候选正则，按可靠性排序
AWEME_ID_PATTERNS = (
    r"/video/(\d{5,})",
    r"/note/(\d{5,})",
    r"modal_id=(\d{5,})",
    r"aweme_id=(\d{5,})",
    r'"aweme_id"\s*:\s*"(\d{5,})"',
    r'"itemId"\s*:\s*"(\d{5,})"',
)

# ---- 分享页重试配置（实测必需，不是保险起见） ----
#
# 实测现象：抖音的 SSR 会「随机降级」。同一条链接、同一分钟内反复请求，
# 会在两种页面之间随机切换：
#     html_length ≈ 38249  完整页，_ROUTER_DATA 里含 item_list  → 能解析
#     html_length ≈ 32333  空壳页，有 video_(id)/page 但 item_list 为空 → 解析失败
# 8 次实测只成功 3 次（成功率 37.5%），且失败页里「验证/频繁/登录」等
# 风控关键词一处都没有 —— 所以这不是验证码，是服务端随机降级。
#
# 既然是随机的，重试就能命中可用的那一次。8 次尝试可把成功率提到约 98%。
DEFAULT_MAX_ATTEMPTS = 8
RETRY_DELAY_SEC = 1.2


# ============================================================ 基础工具


def build_headers(referer: str = "https://www.douyin.com/") -> Dict[str, str]:
    """构造移动端浏览器请求头。Referer 很关键，缺了容易被判成爬虫。"""
    return {
        "User-Agent": IPHONE_UA,
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": referer,
        "Upgrade-Insecure-Requests": "1",
    }


def new_session(proxy: Optional[str] = None) -> requests.Session:
    """
    建一个 requests 会话。

    trust_env=False 的作用：忽略系统环境变量里的 HTTP_PROXY / HTTPS_PROXY，
    避免机器上一个失效的系统代理把请求悄悄搞挂。需要代理时用 --proxy 显式指定。
    """
    session = requests.Session()
    session.trust_env = False
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
    return session


def extract_aweme_id(text: Optional[str]) -> Optional[str]:
    """从任意文本（URL、HTML 片段、分享文案）里抠出 aweme_id。"""
    if not text:
        return None
    for pattern in AWEME_ID_PATTERNS:
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    stripped = text.strip()
    if re.fullmatch(r"\d{5,}", stripped):
        return stripped
    return None


def truncate(text: Optional[str], limit: int = 400) -> str:
    """日志用的截断，避免把整页 HTML 打出来。"""
    if not text:
        return ""
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + "...(截断)"


# ============================================================ 第 1 步：解析输入


def resolve_input(
    session: requests.Session, raw_input: str, timeout: float
) -> Dict[str, Any]:
    """
    把用户的任意输入统一成 (最终 URL, aweme_id)。

    支持三种输入：
      a) 纯数字视频 ID
      b) 纯链接
      c) 从抖音复制的一整段分享文本（里面夹着链接）
    """
    text = (raw_input or "").strip()
    if not text:
        raise ValueError("输入为空")

    # a) 纯数字 ID
    if re.fullmatch(r"\d{5,}", text):
        return {"final_url": None, "aweme_id": text, "step": "input_is_id"}

    # c) 分享文本里抠出第一个 http 链接。中文标点也要当分隔符，否则会把
    #    后面跟着的中文一起吞进 URL。
    match = re.search(r"https?://[^\s，,、）)】\]]+", text)
    url = match.group(0) if match else text
    if not url.lower().startswith("http"):
        raise ValueError("没找到可用的链接，请粘贴完整分享文本或链接")

    # 跟随跳转：v.douyin.com 短链会 302 到 /video/{id} 或 www.douyin.com
    response = session.get(
        url, headers=build_headers(), timeout=timeout, allow_redirects=True
    )

    aweme_id = extract_aweme_id(response.url)
    source = "final_url"
    if not aweme_id:
        # 有些跳转落地的 URL 里不带数字 ID，但落地页 HTML 里有
        aweme_id = extract_aweme_id(response.text)
        source = "landing_html"

    return {
        "final_url": response.url,
        "aweme_id": aweme_id,
        "aweme_id_source": source if aweme_id else None,
        "step": "followed_redirect",
        "http_status": response.status_code,
    }


# ============================================================ 第 2 步：取分享页


def fetch_share_page(
    session: requests.Session,
    aweme_id: str,
    timeout: float,
    save_html: Optional[str] = None,
) -> Tuple[int, str, str]:
    """请求分享页，返回 (HTTP 状态码, HTML, 请求的 URL)。"""
    url = SHARE_PAGE_TMPL.format(aweme_id=aweme_id)
    response = session.get(url, headers=build_headers(), timeout=timeout)

    # requests 在没有 charset 时会猜 iso-8859-1，中文会变乱码，这里强制回 utf-8
    if not response.encoding or response.encoding.lower() == "iso-8859-1":
        response.encoding = "utf-8"

    html = response.text

    # --save-html：把原始 HTML 落盘，方便和浏览器 F12 里的 Response 逐字对照
    if save_html:
        target = Path(save_html)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(html, encoding="utf-8")

    return response.status_code, html, url


# ============================================================ 第 3 步：抠 _ROUTER_DATA


def scan_balanced_json(text: str, start: int) -> Optional[str]:
    """
    从 text[start]（必须是 '{'）开始做「字符串感知」的括号配对，返回完整 JSON 字面量。

    为什么不能直接用正则 `\\{.*?\\}`：_ROUTER_DATA 是多层嵌套对象，
    非贪婪正则在第一个 '}' 就停了；而且视频标题里本身也可能带花括号。
    这里手工扫一遍，遇到引号内的内容跳过（含转义处理）。
    """
    depth = 0
    in_string = False
    escaped = False

    for index in range(start, len(text)):
        char = text[index]

        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]

    return None


def extract_router_data(html: str) -> Tuple[Optional[dict], Optional[str]]:
    """从 HTML 里抠出并解析 window._ROUTER_DATA，返回 (数据, 错误原因)。"""
    marker = "window._ROUTER_DATA"
    marker_index = html.find(marker)
    if marker_index < 0:
        return None, "页面里没有 window._ROUTER_DATA（多半是风控页或页面已改版）"

    equals_index = html.find("=", marker_index)
    if equals_index < 0:
        return None, "找到 _ROUTER_DATA 但没有等号"

    brace_index = html.find("{", equals_index)
    if brace_index < 0:
        return None, "_ROUTER_DATA 后面没有 JSON 对象"

    literal = scan_balanced_json(html, brace_index)
    if not literal:
        return None, "_ROUTER_DATA 的 JSON 括号不闭合（可能被截断）"

    # 抖音偶尔会塞裸 undefined，标准 json 不认；先原样试，再兜底替换
    candidates = [
        literal,
        re.sub(r'(?<![\w"\'])undefined(?![\w"\'])', "null", literal),
    ]
    for candidate in candidates:
        try:
            return json.loads(candidate), None
        except json.JSONDecodeError:
            continue

    return None, "_ROUTER_DATA 不是合法 JSON（已尝试把裸 undefined 换成 null）"


# ============================================================ 第 4 步：定位视频节点


def locate_video_item(
    router_data: dict,
) -> Tuple[Optional[dict], Optional[str], Optional[str]]:
    """
    在 loaderData 里找到视频数据节点。

    返回 (视频节点, 命中的 page_key, 失败原因)。

    实测：抖音分享页的 key 就是字面量 "video_(id)/page"（它并没有把真实 ID
    填进去），图文页是 "note_(id)/page"。为避免写死 key，这里遍历所有分支找
    item_list。
    """
    loader_data = router_data.get("loaderData")
    if not isinstance(loader_data, dict):
        return None, None, "loaderData 不存在或不是对象"

    saw_video_key: Optional[str] = None

    for key, value in loader_data.items():
        if not isinstance(value, dict):
            continue
        if "video" in key or "note" in key:
            saw_video_key = key

        # 最常见：value.videoInfoRes.item_list[0]
        # 兼容：value.aweme_detail（部分页面结构）
        for holder in (value.get("videoInfoRes"), value):
            if not isinstance(holder, dict):
                continue
            items = holder.get("item_list")
            if isinstance(items, list) and items and isinstance(items[0], dict):
                return items[0], key, None
            detail = holder.get("aweme_detail")
            if isinstance(detail, dict):
                return detail, key, None

    # 区分两种「空」：节点在但没内容 vs 压根没节点。前者基本是视频本身的问题，
    # 后者才是页面改版/风控。这个区分直接决定你往哪个方向排查。
    if saw_video_key:
        return None, saw_video_key, (
            "视频节点存在但内容为空：该视频可能已删除、已设为私密，或需要登录态"
        )
    return None, None, "loaderData 里没有视频节点（页面可能已改版或命中风控）"


# ============================================================ 带重试的整段取数


def fetch_share_page_with_retry(
    session: requests.Session,
    aweme_id: str,
    timeout: float,
    save_html: Optional[str],
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    quiet: bool = False,
) -> Dict[str, Any]:
    """
    取分享页 → 抠 _ROUTER_DATA → 定位视频节点，整段自带重试。

    为什么整段一起重试而不是只重试某一步：
    抖音随机降级时，失败点可能是「没抠到 _ROUTER_DATA」，
    也可能是「抠到了但 item_list 为空」。对调用方来说这两种都是
    「再试一次可能就好」，所以合并重试最省事。

    注意：--save-html 每次尝试都会覆盖写，所以落盘的是**最后一次**的页面。
    """
    attempt_records: List[Dict[str, Any]] = []
    latest: Dict[str, Any] = {
        "item": None,
        "page_key": None,
        "router_data": None,
        "router_error": None,
        "locate_reason": None,
        "status": 0,
        "html": "",
        "html_length": 0,
        "page_url": "",
        "waf_challenge": False,
    }

    for attempt in range(1, max_attempts + 1):
        status, html, page_url = fetch_share_page(session, aweme_id, timeout, save_html)
        waf_hit = WAF_CHALLENGE_RE.search(html) is not None
        router_data, router_error = extract_router_data(html)

        item: Optional[dict] = None
        page_key: Optional[str] = None
        locate_reason: Optional[str] = None
        if router_data is not None:
            item, page_key, locate_reason = locate_video_item(router_data)

        reason = locate_reason or router_error
        if waf_hit and not reason:
            reason = "命中 WAF JavaScript 挑战页（纯 HTTP 客户端无法通过）"

        attempt_records.append({
            "attempt": attempt,
            "http_status": status,
            "html_length": len(html),
            "router_data_found": router_data is not None,
            "page_key": page_key,
            "item_found": item is not None,
            "waf_challenge": waf_hit,
            "reason": reason,
        })

        latest = {
            "item": item,
            "page_key": page_key,
            "router_data": router_data,
            "router_error": router_error,
            "locate_reason": locate_reason,
            "status": status,
            "html": html,
            "html_length": len(html),
            "page_url": page_url,
            "waf_challenge": waf_hit,
        }

        if item is not None:
            latest["attempts"] = attempt_records
            latest["succeeded_on_attempt"] = attempt
            return latest

        # 命中 WAF 挑战立刻停手：这种情况重试多少次都是同一个页面，
        # 而且继续高频请求只会让封锁持续更久。正确做法是等封锁衰减或换 Playwright。
        if waf_hit:
            if not quiet:
                print(
                    f"[解析] 第 {attempt} 次请求命中抖音 WAF JavaScript 挑战，"
                    f"已立即停止重试（继续请求只会加重封锁）。",
                    file=sys.stderr,
                )
            break

        if attempt < max_attempts:
            if not quiet:
                # 输出到 stderr：stdout 要留给最终 JSON，混在一起不好解析
                print(
                    f"[解析] 第 {attempt}/{max_attempts} 次被抖音降级"
                    f"（html_length={len(html)}，{reason or '原因未知'}），"
                    f"{RETRY_DELAY_SEC}s 后重试…",
                    file=sys.stderr,
                )
            time.sleep(RETRY_DELAY_SEC)

    latest["attempts"] = attempt_records
    latest["succeeded_on_attempt"] = None
    return latest


# ============================================================ 第 5 步：组装播放地址


def dedupe_by_url(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按 url 去重，保留首次出现的（列表已按码率降序排过）。"""
    seen = set()
    result = []
    for item in items:
        if item["url"] in seen:
            continue
        seen.add(item["url"])
        result.append(item)
    return result


def collect_play_candidates(video: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    收集视频所有候选播放地址，并按码率从高到低排序。

    来源有三个，都要收：
      - video.play_addr.url_list        默认地址（往往是 720p 带水印）
      - video.bit_rate[].play_addr      多档清晰度（找得到 1080p）
      - video.download_addr.url_list    下载地址
    """
    collected: List[Dict[str, Any]] = []

    def add(urls: Any, source: str, bit_rate: Any = None,
            gear_name: Any = None, quality_type: Any = None) -> None:
        if not isinstance(urls, list):
            return
        for url in urls:
            if isinstance(url, str) and url.startswith("http"):
                collected.append({
                    "url": url,
                    "source": source,
                    "bit_rate": bit_rate if isinstance(bit_rate, int) else None,
                    "gear_name": gear_name,
                    "quality_type": quality_type,
                })

    play_addr = video.get("play_addr") or {}
    add(play_addr.get("url_list"), "play_addr")

    download_addr = video.get("download_addr") or {}
    add(download_addr.get("url_list"), "download_addr")

    for entry in video.get("bit_rate") or []:
        if not isinstance(entry, dict):
            continue
        add(
            (entry.get("play_addr") or {}).get("url_list"),
            "bit_rate",
            entry.get("bit_rate"),
            entry.get("gear_name"),
            entry.get("quality_type"),
        )

    collected.sort(key=lambda item: item.get("bit_rate") or 0, reverse=True)
    return dedupe_by_url(collected)


def to_no_watermark(url: str) -> str:
    """
    带水印地址 -> 无水印地址。

    抖音的播放接口历史上用 /aweme/v1/playwm/ 表示「带水印」，
    /aweme/v1/play/ 表示「无水印」，路径仅差一个 wm。
    注意：这是遗留技巧，抖音已多次收紧，能否成功由 --verify 实测。
    """
    return url.replace("playwm", "play")


def build_uri_url(video: Dict[str, Any]) -> Optional[str]:
    """用 play_addr.uri 自己拼一条无水印地址（有些页面只给 uri）。"""
    uri = (video.get("play_addr") or {}).get("uri")
    if isinstance(uri, str) and uri:
        return PLAY_URL_TMPL.format(uri=uri)
    return None


# ============================================================ 可选：实测无水印地址


def verify_play_url(
    session: requests.Session, url: str, timeout: float
) -> Dict[str, Any]:
    """
    真的去请求一次无水印地址，确认它可用。

    判断「是不是真视频」的办法：MP4 文件第 4~8 字节是 'ftyp'（文件类型盒），
    只读前 1KB 就能确认，不用下载整个视频。
    """
    try:
        response = session.get(
            url,
            headers=build_headers("https://www.douyin.com/"),
            timeout=timeout,
            allow_redirects=True,
            stream=True,
        )
    except Exception as exc:  # 网络层异常
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    try:
        content_type = response.headers.get("Content-Type", "")
        content_length = response.headers.get("Content-Length")
        first_chunk = b""
        for chunk in response.iter_content(1024):
            first_chunk = chunk
            break

        return {
            "ok": response.status_code == 200,
            "status": response.status_code,
            "final_url": response.url,
            "content_type": content_type,
            "content_length": int(content_length) if (content_length or "").isdigit() else None,
            # ftyp 出现在第 4~8 字节，说明这是标准 MP4
            "looks_like_mp4": len(first_chunk) >= 8 and first_chunk[4:8] == b"ftyp",
            "first_bytes_hex": first_chunk[:16].hex(),
        }
    finally:
        response.close()


# ============================================================ 主流程


def parse(args: argparse.Namespace) -> Dict[str, Any]:
    """执行完整解析，返回可直接 json.dumps 的结果字典。"""
    diagnostics: Dict[str, Any] = {
        "strategy": "share_page_router_data",
        "warnings": [],
    }

    session = new_session(args.proxy)

    # ---- 1. 解析输入，拿 aweme_id
    resolved = resolve_input(session, args.input, args.timeout)
    diagnostics["resolve_step"] = resolved["step"]
    if resolved.get("final_url"):
        diagnostics["final_url"] = resolved["final_url"]
    if resolved.get("http_status"):
        diagnostics["resolve_http_status"] = resolved["http_status"]
    if resolved.get("aweme_id_source"):
        diagnostics["aweme_id_source"] = resolved["aweme_id_source"]

    aweme_id = resolved.get("aweme_id")
    if not aweme_id:
        diagnostics["warnings"].append(
            "没能提取出 aweme_id：短链可能已失效，或跳转到了非视频页（用户主页/直播页）"
        )
        return {
            "ok": False,
            "stage": "resolve_input",
            "input": args.input,
            "diagnostics": diagnostics,
        }

    # ---- 2~4. 取分享页 → 抠 _ROUTER_DATA → 定位视频节点（整段带重试）
    #            必须带重试：抖音 SSR 会随机降级，实测单次成功率仅 37.5%
    max_attempts = int(getattr(args, "max_attempts", None) or DEFAULT_MAX_ATTEMPTS)
    fetched = fetch_share_page_with_retry(
        session,
        aweme_id,
        args.timeout,
        args.save_html,
        max_attempts=max_attempts,
        quiet=bool(getattr(args, "quiet", False)),
    )

    diagnostics["share_page_url"] = fetched["page_url"]
    diagnostics["share_page_status"] = fetched["status"]
    diagnostics["html_length"] = fetched["html_length"]
    diagnostics["router_data_found"] = fetched["router_data"] is not None
    diagnostics["attempt_count"] = len(fetched["attempts"])
    diagnostics["succeeded_on_attempt"] = fetched["succeeded_on_attempt"]
    diagnostics["attempts"] = fetched["attempts"]
    if args.save_html:
        diagnostics["html_saved_to"] = str(Path(args.save_html).resolve())

    html = fetched["html"]
    block_hit = BLOCK_HINT_RE.search(html)
    if block_hit:
        diagnostics["block_hint"] = block_hit.group(0)
        diagnostics["warnings"].append(
            f"页面命中风控/验证特征「{block_hit.group(0)}」，拿到的不是正常分享页"
        )
    if fetched["status"] != 200:
        diagnostics["warnings"].append(f"分享页 HTTP 状态码异常：{fetched['status']}")

    if fetched["item"] is None:
        # 三种失败要分清楚，因为处置方式完全不同
        if fetched.get("waf_challenge"):
            stage = "waf_challenge"
            diagnostics["waf_challenge"] = True
            diagnostics["warnings"].append(
                "命中抖音 WAF JavaScript 反爬挑战：页面要求执行 JS 算 SHA-256 工作量证明"
                "再写 cookie 刷新，纯 requests 客户端无法通过，重试也无效"
                "（已提前停止重试，避免继续请求加重封锁）。"
                "处置：① 停止请求，等若干分钟到数小时让封锁衰减；"
                "② 改用 Playwright 真浏览器（本项目不实现 WAF 绕过）。"
            )
        elif fetched["router_data"] is None:
            stage = "extract_router_data"
        else:
            stage = "locate_video_item"

        if fetched["router_error"]:
            diagnostics["router_data_error"] = fetched["router_error"]
        reason = fetched["locate_reason"] or fetched["router_error"] or "未知原因"
        diagnostics["locate_error"] = reason
        # 已经报过 WAF 就不再加这条泛泛的提示，避免自相矛盾
        if not fetched.get("waf_challenge"):
            diagnostics["warnings"].append(
                f"尝试 {len(fetched['attempts'])} 次仍未拿到视频数据：{reason}"
            )
        return {
            "ok": False,
            "stage": stage,
            "input": args.input,
            "aweme_id": aweme_id,
            "diagnostics": diagnostics,
        }

    item = fetched["item"]
    router_data = fetched["router_data"]
    diagnostics["page_key"] = fetched["page_key"]
    diagnostics["loader_data_keys"] = list(
        (router_data.get("loaderData") or {}).keys()
    )

    video = item.get("video") or {}
    author = item.get("author") or {}
    cover = video.get("cover") or {}
    cover_urls = cover.get("url_list") or []

    # ---- 5. 组装播放地址
    candidates = collect_play_candidates(video)
    for entry in candidates:
        entry["no_watermark_url"] = to_no_watermark(entry["url"])

    watermarked_url = candidates[0]["url"] if candidates else None
    no_watermark_url = candidates[0]["no_watermark_url"] if candidates else None
    uri_fallback = build_uri_url(video)

    if not candidates and not uri_fallback:
        diagnostics["warnings"].append(
            "视频节点里没有任何播放地址（play_addr / bit_rate / download_addr 全空），"
            "多半是页面改版或需要登录态"
        )
    if uri_fallback:
        diagnostics["uri_built_url"] = uri_fallback

    result: Dict[str, Any] = {
        "ok": bool(no_watermark_url or uri_fallback),
        "stage": "done",
        "input": args.input,
        "aweme_id": aweme_id,
        "title": item.get("desc") or "",
        "author": {
            "nickname": author.get("nickname"),
            "unique_id": author.get("unique_id"),
            "sec_uid": author.get("sec_uid"),
        },
        "cover_url": cover_urls[0] if cover_urls else None,
        "duration_ms": video.get("duration"),
        "video_uri": (video.get("play_addr") or {}).get("uri"),
        "watermarked_url": watermarked_url,
        "no_watermark_url": no_watermark_url,
        "no_watermark_candidates": [
            {
                "url": entry["no_watermark_url"],
                "source": entry["source"],
                "bit_rate": entry["bit_rate"],
                "gear_name": entry["gear_name"],
            }
            for entry in candidates
        ],
        "diagnostics": diagnostics,
    }

    # ---- 6. 可选：实测无水印地址
    if args.verify:
        target = no_watermark_url or uri_fallback
        if target:
            result["verify"] = verify_play_url(session, target, args.timeout)
            if not result["verify"].get("looks_like_mp4"):
                diagnostics["warnings"].append(
                    "实测无水印地址未返回 MP4（看 verify 字段），说明 playwm->play 已失效，"
                    "需要改用 yt-dlp 或 Playwright 方案"
                )
        else:
            result["verify"] = {"ok": False, "error": "没有可实测的目标地址"}

    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="解析抖音分享链接，输出视频信息与无水印播放地址（JSON）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "input",
        help="抖音分享链接 / 整段分享文本 / 纯数字视频 ID",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        help="单次 HTTP 超时秒数，默认 15",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="对解析出的无水印地址发起真实请求，校验是否为 MP4",
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=DEFAULT_MAX_ATTEMPTS,
        help=f"分享页最大请求次数，默认 {DEFAULT_MAX_ATTEMPTS}（抖音 SSR 随机降级，需重试）",
    )
    parser.add_argument(
        "--save-html",
        metavar="PATH",
        help="把分享页原始 HTML 落盘，便于和浏览器 F12 的 Response 对照排查",
    )
    parser.add_argument(
        "--proxy",
        help="显式指定代理，如 http://127.0.0.1:7890（默认不使用系统代理）",
    )
    return parser


def main() -> int:
    # Windows 下把标准输出钉死成 UTF-8，避免中文/emoji 触发 UnicodeEncodeError
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = build_parser()
    args = parser.parse_args()

    try:
        result = parse(args)
    except Exception as exc:
        result = {
            "ok": False,
            "stage": "unhandled_exception",
            "input": args.input,
            "error": f"{type(exc).__name__}: {exc}",
        }

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())