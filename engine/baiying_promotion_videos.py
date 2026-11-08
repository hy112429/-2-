#!/usr/bin/env python3
"""从已登录的百应商品决策页读取同款带货作品，不保存浏览器凭据。

百应 ``pack_detail`` 的 ``sale_content_data.content_list`` 包含作品 ID 和完整
``media_info.video_url``。页面请求由用户登录的 Chrome 正常发起；脚本只读取响应。
临时打开的商品标签在结束时关闭；已有同商品标签会复用并停留在带货内容页。
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib.request import urlopen

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

try:
    from .client_paths import DEFAULT_DB_PATH
except ImportError:
    from client_paths import DEFAULT_DB_PATH


DETAIL_ROUTE = "https://buyin.jinritemai.com/dashboard/merch-picking-library/merch-promoting"
FIXED_PARAMS = (
    "commodity_location=1&decision_enter_from=pc.selection_square.recommend_main"
    "&only_query_params=1&universal_append=1"
    "&pre_universal_page_params_id=87e0475e-4976-40dd-83c8-658b76d382de"
)
MIN_QUOTA = 10


def valid_id(value: object) -> str:
    text = str(value or "").strip()
    return text if re.fullmatch(r"\d{8,20}", text) else ""


def resolve_account(db_path: str, product_id: str) -> dict:
    """按选品记录确定账号；浏览缓存只在没有选品归属时兜底。"""
    database = Path(db_path)
    if not database.is_file():
        return {"ok": True, "found": False}
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    try:
        tables = {
            str(row[0]) for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        sources: list[tuple[str, set[int]]] = []
        if "selection_local_registry" in tables:
            rows = connection.execute(
                "SELECT account_key FROM selection_local_registry "
                "WHERE product_id=? AND state='COMMITTED'", (product_id,)
            )
            sources.append(("选品登记", {
                int(match.group(1)) for row in rows
                if (match := re.fullmatch(r"local:(\d+)", str(row[0])))
            }))
        if "product_list_rows" in tables:
            rows = connection.execute(
                "SELECT account_id FROM product_list_rows WHERE product_id=? "
                "AND deleted_at IS NULL AND status='cart_added'", (product_id,)
            )
            sources.append(("已加选品车", {int(row[0]) for row in rows}))
        if "publish_queue" in tables:
            rows = connection.execute(
                "SELECT DISTINCT account_id FROM publish_queue WHERE product_id=? "
                "AND status!='cancelled'", (product_id,)
            )
            sources.append(("发布队列", {int(row[0]) for row in rows}))
        if "selection_local_registry" in tables:
            rows = connection.execute(
                "SELECT account_key FROM selection_local_registry WHERE product_id=? "
                "AND state IN ('RESERVED','ATTEMPTING','UNCERTAIN')", (product_id,)
            )
            sources.append(("选品处理中", {
                int(match.group(1)) for row in rows
                if (match := re.fullmatch(r"local:(\d+)", str(row[0])))
            }))
        if "product_list_rows" in tables:
            rows = connection.execute(
                "SELECT account_id FROM product_list_rows WHERE product_id=? "
                "AND deleted_at IS NULL AND status='needs_review'", (product_id,)
            )
            sources.append(("选品待核查", {int(row[0]) for row in rows}))
        if "baiying_products" in tables:
            rows = connection.execute(
                "SELECT account_id FROM baiying_products WHERE product_id=? AND account_id IS NOT NULL",
                (product_id,),
            )
            sources.append(("百应浏览缓存", {int(row[0]) for row in rows}))

        for source, account_ids in sources:
            if not account_ids:
                continue
            if len(account_ids) != 1:
                raise RuntimeError(f"{source}中同一商品关联多个账号，已停止自动下载以免归错类")
            account_id = next(iter(account_ids))
            commodity_id = ""
            if "product_list_rows" in tables:
                row = connection.execute(
                    "SELECT commodity_id FROM product_list_rows WHERE product_id=? AND account_id=? "
                    "AND deleted_at IS NULL ORDER BY selected_at DESC LIMIT 1",
                    (product_id, account_id),
                ).fetchone()
                if row:
                    commodity_id = valid_id(row[0])
            if not commodity_id and "baiying_products" in tables:
                row = connection.execute(
                    "SELECT commodity_id FROM baiying_products WHERE product_id=? AND account_id=?",
                    (product_id, account_id),
                ).fetchone()
                if row:
                    commodity_id = valid_id(row[0])
            return {
                "ok": True, "found": True, "accountId": account_id,
                "commodityId": commodity_id, "accountSource": source,
            }
        return {"ok": True, "found": False}
    finally:
        connection.close()


def find_open_product(profile_path: str, product_id: str) -> dict:
    """只查现有 Chrome 标签，不启动浏览器、不访问商品接口。"""
    port_file = Path(profile_path) / "DevToolsActivePort"
    if not port_file.is_file():
        return {"ok": True, "found": False}
    try:
        port = int(port_file.read_text(encoding="utf-8").splitlines()[0])
        if not 1 <= port <= 65535:
            return {"ok": True, "found": False}
        with urlopen(f"http://127.0.0.1:{port}/json/list", timeout=3) as response:
            targets = json.load(response)
    except (OSError, ValueError, IndexError):
        return {"ok": True, "found": False}
    for target in targets if isinstance(targets, list) else []:
        if not isinstance(target, dict) or target.get("type") != "page":
            continue
        parsed = urlparse(str(target.get("url") or ""))
        if parsed.hostname != "buyin.jinritemai.com" or parsed.path != urlparse(DETAIL_ROUTE).path:
            continue
        params = parse_qs(parsed.query)
        if params.get("product_id") != [product_id]:
            continue
        commodity_id = valid_id((params.get("commodity_id") or params.get("id") or [""])[0])
        if commodity_id:
            return {"ok": True, "found": True, "commodityId": commodity_id}
    return {"ok": True, "found": False}


def output(payload: dict) -> int:
    print(json.dumps(payload, ensure_ascii=False), flush=True)
    return 0 if payload.get("ok") else 1


def quota_connection(db_path: str) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path)
    connection.execute(
        "CREATE TABLE IF NOT EXISTS baiying_promotion_video_quota ("
        "account_id INTEGER PRIMARY KEY, video_limit INTEGER NOT NULL CHECK(video_limit >= 10))"
    )
    connection.commit()
    return connection


def get_quota(db_path: str, account_id: int) -> int:
    with quota_connection(db_path) as connection:
        row = connection.execute(
            "SELECT video_limit FROM baiying_promotion_video_quota WHERE account_id = ?",
            (account_id,),
        ).fetchone()
    return int(row[0]) if row else MIN_QUOTA


def existing_video_ids(db_path: str, account_id: int, product_id: str) -> dict:
    """只认同账号、同商品、已入素材库且文件仍存在的网页下载视频。"""
    database = Path(db_path)
    if not database.is_file():
        return {"ok": True, "videoIds": [], "files": []}
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    try:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='assets'"
        ).fetchone()
        if not table:
            return {"ok": True, "videoIds": [], "files": []}
        rows = connection.execute(
            "SELECT video_id, file_path FROM assets WHERE account_id=? AND product_id=? "
            "AND source_type='web_download' AND status='ready'",
            (account_id, product_id),
        )
        files = [(str(video_id), str(file_path)) for video_id, file_path in rows
                 if video_id and Path(str(file_path)).is_file()]
        return {"ok": True, "videoIds": sorted({item[0] for item in files}),
                "files": [{"videoId": video_id, "filePath": path} for video_id, path in files]}
    finally:
        connection.close()


def set_quota(db_path: str, account_id: int, limit: int) -> int:
    if not MIN_QUOTA <= limit <= 500:
        raise ValueError(f"每账号下载额度须为 {MIN_QUOTA}–500 条")
    with quota_connection(db_path) as connection:
        connection.execute(
            "INSERT INTO baiying_promotion_video_quota(account_id, video_limit) VALUES (?, ?) "
            "ON CONFLICT(account_id) DO UPDATE SET video_limit=excluded.video_limit",
            (account_id, limit),
        )
        connection.commit()
    return limit


def detail_url(product_id: str, commodity_id: str) -> str:
    return (
        f"{DETAIL_ROUTE}?commodity_id={commodity_id}&id={commodity_id}"
        f"&product_id={product_id}&{FIXED_PARAMS}"
    )


def is_detail_response(response, product_id: str) -> bool:
    parsed = urlparse(response.url)
    return (
        parsed.hostname == "buyin.jinritemai.com"
        and "/pc/selection/decision/pack_detail" in parsed.path
        and response.status == 200
    )


def is_sale_response(response, product_id: str) -> bool:
    if not is_detail_response(response, product_id):
        return False
    try:
        data = response.json().get("data") or {}
        sale = (data.get("model") or {}).get("sale_content_data") or {}
        return str(data.get("product_id")) == product_id and isinstance(sale.get("content_list"), list)
    except (ValueError, RuntimeError):
        return False


def parse_page(response, product_id: str) -> tuple[list[dict], int]:
    try:
        payload = response.json()
    except ValueError as error:
        raise RuntimeError(
            f"百应详情响应不是 JSON（HTTP {response.status}，"
            f"类型 {response.headers.get('content-type', '未知')}，"
            f"长度 {len(response.body())} 字节）"
        ) from error
    data = payload.get("data") or {}
    if str(data.get("product_id")) != product_id:
        raise RuntimeError("百应返回了其他商品的数据，已停止下载")
    sale = (data.get("model") or {}).get("sale_content_data") or {}
    rows = sale.get("content_list") or []
    if not isinstance(rows, list):
        raise RuntimeError("百应带货作品列表格式变化，请检查页面")
    videos: list[dict] = []
    for item in rows:
        if not isinstance(item, dict) or item.get("content_type") != "video":
            continue
        video_id = str(item.get("content_id") or "")
        url = (item.get("media_info") or {}).get("video_url")
        if not re.fullmatch(r"\d{8,25}", video_id) or not isinstance(url, str):
            continue
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname:
            continue
        videos.append({
            "videoId": video_id,
            "url": url,
            "title": str(item.get("content_title") or ""),
            "author": str(item.get("author_name") or ""),
            "duration": (item.get("media_info") or {}).get("duration"),
            "publishTime": item.get("publish_time"),
            "shareUrl": str(item.get("share_url") or ""),
        })
    return videos, int(sale.get("count") or len(videos))


def discover(profile_path: str, product_id: str, commodity_id: str | None, limit: int) -> dict:
    profile = Path(profile_path)
    port_file = profile / "DevToolsActivePort"
    if not port_file.is_file():
        raise RuntimeError("该账号专属 Chrome 尚未启动，请先打开账号浏览器并登录百应")
    port = int(port_file.read_text(encoding="utf-8").splitlines()[0])
    if not 1 <= port <= 65535:
        raise RuntimeError("账号浏览器调试端口无效")

    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}", timeout=15_000)
        temporary_page = None
        try:
            if not browser.contexts:
                raise RuntimeError("账号浏览器没有可用的登录会话")
            context = browser.contexts[0]
            page = next(
                (
                    candidate for candidate in context.pages
                    if urlparse(candidate.url).path == urlparse(DETAIL_ROUTE).path
                    and parse_qs(urlparse(candidate.url).query).get("product_id") == [product_id]
                ),
                None,
            )
            if page is not None:
                page_params = parse_qs(urlparse(page.url).query)
                page_commodity = (page_params.get("commodity_id") or page_params.get("id") or [""])[0]
                if re.fullmatch(r"\d{8,20}", page_commodity):
                    commodity_id = page_commodity
            if not commodity_id:
                raise RuntimeError(
                    "本机未记录该商品的百应推广 ID；请在所选账号的专属百应浏览器打开该商品页后重试"
                )
            if page is None:
                page = context.new_page()
                temporary_page = page
                target_url = detail_url(product_id, commodity_id)
            else:
                # 保留用户从选品广场打开的原生 URL（其中可能有不同的页面展示参数）。
                target_url = page.url
            page.goto(target_url, wait_until="commit", timeout=20_000)
            if urlparse(page.url).hostname != "buyin.jinritemai.com":
                raise RuntimeError("该账号百应登录态已失效；请在该账号专属浏览器登录百应后重试")
            tab = page.get_by_text("带货内容", exact=True)
            try:
                tab.wait_for(state="visible", timeout=20_000)
            except PlaywrightTimeoutError as error:
                if urlparse(page.url).hostname != "buyin.jinritemai.com":
                    raise RuntimeError("该账号百应登录态已失效；请在该账号专属浏览器登录百应后重试") from error
                raise
            with page.expect_response(lambda response: is_sale_response(response, product_id), timeout=20_000) as pending:
                tab.click(timeout=10_000)
            first_page, available = parse_page(pending.value, product_id)
            videos: list[dict] = []
            seen_ids: set[str] = set()
            seen_sources: set[str] = set()

            def add_unique(page_videos: list[dict]) -> None:
                for video in page_videos:
                    # 百应可能给同一视频源分配多个 content_id；仅按 ID 去重会把
                    # 字节相同的文件下载和上传多次，千川最终仍归并成一个素材 ID。
                    source = urlparse(video["url"]).path
                    if video["videoId"] in seen_ids or source in seen_sources:
                        continue
                    videos.append(video)
                    seen_ids.add(video["videoId"])
                    seen_sources.add(source)

            add_unique(first_page)
            page_number = 1
            while len(videos) < limit and page_number * 12 < available:
                page_number += 1
                button = page.locator(f"li.aurora-pagination-item-{page_number}")
                if button.count() == 0:
                    break
                with page.expect_response(lambda response: is_sale_response(response, product_id), timeout=20_000) as pending:
                    button.click(timeout=10_000)
                page_videos, _ = parse_page(pending.value, product_id)
                if not page_videos:
                    break
                add_unique(page_videos)
            return {
                "ok": True,
                "productId": product_id,
                "commodityId": commodity_id,
                "available": available,
                "limit": limit,
                "videos": videos[:limit],
            }
        finally:
            if temporary_page is not None and not temporary_page.is_closed():
                temporary_page.close()
            browser.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=DEFAULT_DB_PATH)
    parser.add_argument("--account-id", type=int)
    parser.add_argument("--resolve-account", action="store_true")
    parser.add_argument("--find-open-product", action="store_true")
    parser.add_argument("--set-quota", type=int)
    parser.add_argument("--get-quota", action="store_true")
    parser.add_argument("--existing-videos", action="store_true")
    parser.add_argument("--product-id")
    parser.add_argument("--commodity-id")
    parser.add_argument("--profile-path")
    parser.add_argument("--candidate-limit", type=int)
    args = parser.parse_args()
    try:
        if args.resolve_account or args.find_open_product:
            if not valid_id(args.product_id):
                raise ValueError("商品 ID 必须是 8–20 位数字")
            if args.resolve_account:
                return output(resolve_account(args.db, args.product_id))
            if not args.profile_path:
                raise ValueError("缺少账号专属浏览器路径")
            return output(find_open_product(args.profile_path, args.product_id))
        if args.account_id is None or args.account_id < 1:
            raise ValueError("账号 ID 无效")
        if args.existing_videos:
            if not valid_id(args.product_id):
                raise ValueError("商品 ID 必须是 8–20 位数字")
            return output(existing_video_ids(args.db, args.account_id, args.product_id))
        if args.set_quota is not None:
            return output({"ok": True, "quota": set_quota(args.db, args.account_id, args.set_quota)})
        quota = get_quota(args.db, args.account_id)
        if args.get_quota:
            return output({"ok": True, "quota": quota})
        if not re.fullmatch(r"\d{8,20}", args.product_id or ""):
            raise ValueError("商品 ID 必须是 8–20 位数字")
        if args.commodity_id and not re.fullmatch(r"\d{8,20}", args.commodity_id):
            raise ValueError("百应推广 ID 格式无效")
        if not args.profile_path:
            raise ValueError("缺少账号专属浏览器路径")
        candidate_limit = args.candidate_limit if args.candidate_limit is not None else quota
        if not quota <= candidate_limit <= 500:
            raise ValueError("候选读取上限须在账号额度到 500 条之间")
        return output(discover(args.profile_path, args.product_id, args.commodity_id, candidate_limit))
    except (OSError, ValueError, RuntimeError, sqlite3.Error, PlaywrightTimeoutError) as error:
        return output({"ok": False, "error": str(error)})


if __name__ == "__main__":
    sys.exit(main())
