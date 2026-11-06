#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
baiying_cache.py —— 百应商品数据缓存（T-15 探针方案的落库端）

背景
    百应选品的商品详情接口 pack_detail 带 a_bogus / msToken 签名，引擎无法直接构造请求
    （合规边界：不逆向签名）。所以由客户端登录视图里的 CDP 探针（probe.ts）在用户打开
    商品详情时捕获接口响应，把「product_id → 商品信息 + 可下载视频直链」落到这里。

表结构
    baiying_products
        product_id  业务商品 ID（19 位数字），主键
        account_id  捕获来源的本地抖音账号 ID；无法确认时为 NULL
        title       商品标题
        cover_url   封面图
        cos_ratio   佣金率（%）
        video_url   商品素材视频直链（无签名时效，抓到即用，过期需重新捕获）
        captured_at 抓取时间

设计要点
    · 与当前客户端 ad_client.db 同库，WAL + 每连接外键；安装版路径由 Electron 注入。
    · 输出统一 JSON（indent=2），与 db.py 一致，供 Electron 消费。
    · upsert 幂等：重复抓取同一个商品只更新字段，不产生重复行。

用法
    python baiying_cache.py init
    python baiying_cache.py set-commodity --product-id 3829105980558409845 --commodity-id 3829642692380459374
    python baiying_cache.py upsert --product-id 3829105980558409845 --commodity-id 3829642692380459374 --title "瑞幸咖啡…" \
        --cover "https://…" --cos-ratio 5 --video-url "https://…"
    python baiying_cache.py get --product-id 3829105980558409845
    python baiying_cache.py list --limit 50
    python baiying_cache.py count
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, Optional, Sequence

try:
    from .client_paths import DEFAULT_DB_PATH
except ImportError:
    from client_paths import DEFAULT_DB_PATH

try:
    from .selection_settings import banned_words_from_connection, find_banned_word
except ImportError:
    from selection_settings import banned_words_from_connection, find_banned_word

# ============================================================ 常量

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS baiying_products (
    product_id  TEXT    PRIMARY KEY,             -- 业务商品 ID（19 位数字）
    account_id  INTEGER,                          -- 捕获来源的本地抖音账号 ID；无法确定时为 NULL
    commodity_id TEXT,                           -- 推广 ID（pack_detail 触发所必需，探针可从选品列表接口补全）
    title       TEXT,
    cover_url   TEXT,
    cos_ratio   REAL,                            -- 佣金率（%），如 5.0 = 5%
    video_url   TEXT,                            -- 商品素材视频直链
    video_urls_json TEXT,                        -- 商品全部视频直链 JSON 数组
    gallery_json TEXT,                           -- 百应商品详情图 URL 列表
    captured_at TEXT    NOT NULL                 -- 抓取时间（本地 ISO8601）
);
"""

# 老库迁移：补充 commodity_id 列（列已存在时忽略）
MIGRATION_ALTERS = [
    "ALTER TABLE baiying_products ADD COLUMN account_id INTEGER",
    "ALTER TABLE baiying_products ADD COLUMN commodity_id TEXT",
    "ALTER TABLE baiying_products ADD COLUMN video_urls_json TEXT",
    "ALTER TABLE baiying_products ADD COLUMN gallery_json TEXT",
]


# ============================================================ 基础工具


def now_text() -> str:
    return datetime.now().isoformat(timespec="seconds")


def emit(payload: Dict[str, Any], exit_code: int = 0) -> int:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return exit_code


@contextmanager
def connect(db_path: Path) -> Iterator[sqlite3.Connection]:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(db_path), timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        connection.executescript(SCHEMA_SQL)
        for alter in MIGRATION_ALTERS:
            try:
                connection.execute(alter)
            except sqlite3.OperationalError:
                pass
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def row_to_dict(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    result = dict(row)
    try:
        videos = json.loads(result.get("video_urls_json") or "[]")
    except (TypeError, json.JSONDecodeError):
        videos = []
    if not isinstance(videos, list):
        videos = []
    videos = [str(url) for url in videos if isinstance(url, str) and url.startswith(("http://", "https://"))]
    if not videos and result.get("video_url"):
        videos = [str(result["video_url"])]
    result["video_urls"] = videos
    return result


def global_banned_words(connection: sqlite3.Connection) -> list[str]:
    """读取共享的全局屏蔽词；旧库/未初始化时使用统一内置词表。"""
    return banned_words_from_connection(connection)


def ban_match(title: Optional[str], words: Sequence[str]) -> Optional[str]:
    return find_banned_word(title, words)


# ============================================================ 命令


def init_db(db_path: Path) -> Dict[str, Any]:
    with connect(db_path) as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.executescript(SCHEMA_SQL)
        # 老库补列（已存在则跳过，不抛错）
        for alter in MIGRATION_ALTERS:
            try:
                connection.execute(alter)
            except sqlite3.OperationalError:
                pass
        tables = [
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
    return {"ok": True, "tables": tables}


def upsert_product(
    db_path: Path,
    product_id: str,
    title: Optional[str],
    cover_url: Optional[str],
    cos_ratio: Optional[float],
    video_url: Optional[str] = None,
    commodity_id: Optional[str] = None,
    gallery: Optional[Sequence[str]] = None,
    video_urls: Optional[Sequence[str]] = None,
    account_id: Optional[int] = None,
) -> Dict[str, Any]:
    if type(account_id) is not int or account_id < 1:
        account_id = None
    normalized_video_urls = list(dict.fromkeys(
        str(url).strip() for url in (video_urls or [])
        if isinstance(url, str) and str(url).strip().startswith(("http://", "https://"))
    ))
    if not normalized_video_urls and video_url and video_url.startswith(("http://", "https://")):
        normalized_video_urls = [video_url]
    with connect(db_path) as connection:
        existing = connection.execute(
            "SELECT * FROM baiying_products WHERE product_id = ?", (product_id,)
        ).fetchone()
        old_video_urls: list[str] = []
        if existing is not None:
            try:
                old_video_urls = json.loads(existing["video_urls_json"] or "[]")
            except (TypeError, json.JSONDecodeError):
                old_video_urls = []
            if not isinstance(old_video_urls, list):
                old_video_urls = []
            old_video_urls = [
                str(url) for url in old_video_urls
                if isinstance(url, str) and url.startswith(("http://", "https://"))
            ]
            if not old_video_urls and existing["video_url"]:
                old_video_urls = [str(existing["video_url"])]
        normalized_video_urls = list(dict.fromkeys(normalized_video_urls + old_video_urls))
        primary_video_url = normalized_video_urls[0] if normalized_video_urls else video_url
        connection.execute(
            "INSERT INTO baiying_products (product_id, account_id, commodity_id, title, cover_url, cos_ratio, video_url, video_urls_json, gallery_json, captured_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(product_id) DO UPDATE SET "
            "account_id = COALESCE(excluded.account_id, baiying_products.account_id), "
            "commodity_id = COALESCE(excluded.commodity_id, baiying_products.commodity_id), "
            "title = excluded.title, cover_url = excluded.cover_url, "
            "cos_ratio = excluded.cos_ratio, video_url = COALESCE(excluded.video_url, baiying_products.video_url), "
            "video_urls_json = COALESCE(excluded.video_urls_json, baiying_products.video_urls_json), "
            "gallery_json = COALESCE(excluded.gallery_json, baiying_products.gallery_json), "
            "captured_at = excluded.captured_at",
            (product_id, account_id, commodity_id, title, cover_url, cos_ratio, primary_video_url,
             json.dumps(normalized_video_urls, ensure_ascii=False) if normalized_video_urls else None,
             json.dumps(list(gallery), ensure_ascii=False) if gallery else None, now_text()),
        )
        row = connection.execute(
            "SELECT * FROM baiying_products WHERE product_id = ?", (product_id,)
        ).fetchone()
    return {"ok": True, "created": existing is None, "product": row_to_dict(row)}


def set_commodity(db_path: Path, product_id: str, commodity_id: str) -> Dict[str, Any]:
    """补全/更新 product_id 对应的 commodity_id（推广ID），行不存在则建占位行。"""
    with connect(db_path) as connection:
        existing = connection.execute(
            "SELECT * FROM baiying_products WHERE product_id = ?", (product_id,)
        ).fetchone()
        if existing is None:
            connection.execute(
                "INSERT INTO baiying_products (product_id, commodity_id, captured_at) VALUES (?, ?, ?)",
                (product_id, commodity_id, now_text()),
            )
        else:
            connection.execute(
                "UPDATE baiying_products SET commodity_id = ?, captured_at = ? WHERE product_id = ?",
                (commodity_id, now_text(), product_id),
            )
        row = connection.execute(
            "SELECT * FROM baiying_products WHERE product_id = ?", (product_id,)
        ).fetchone()
    return {"ok": True, "created": existing is None, "product": dict(row)}


def get_product(db_path: Path, product_id: str) -> Dict[str, Any]:
    with connect(db_path) as connection:
        row = connection.execute(
            "SELECT * FROM baiying_products WHERE product_id = ?", (product_id,)
        ).fetchone()
        words = global_banned_words(connection)
    product = row_to_dict(row)
    if product and product.get("gallery_json"):
        try:
            product["gallery"] = json.loads(product["gallery_json"])
        except json.JSONDecodeError:
            product["gallery"] = []
    matched_word = ban_match(product.get("title") if product else None, words)
    if matched_word:
        return {"ok": True, "found": False, "filtered": True, "reason": f"商品名称命中全局屏蔽词：{matched_word}"}
    return {"ok": True, "found": product is not None, "product": product}


def list_products(db_path: Path, limit: int) -> Dict[str, Any]:
    with connect(db_path) as connection:
        rows = connection.execute(
            "SELECT * FROM baiying_products ORDER BY captured_at DESC, product_id LIMIT ?",
            (max(limit * 5, limit),),
        ).fetchall()
        words = global_banned_words(connection)
    products = [row_to_dict(row) for row in rows if not ban_match(row["title"], words)][:limit]
    return {"ok": True, "count": len(products), "products": products}


def count_products(db_path: Path) -> Dict[str, Any]:
    with connect(db_path) as connection:
        count = connection.execute("SELECT COUNT(*) FROM baiying_products").fetchone()[0]
    return {"ok": True, "count": count}


# ============================================================ 主入口


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="百应商品数据缓存")
    parser.add_argument("--db", default=DEFAULT_DB_PATH, help="数据库文件路径")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="建表（幂等）")

    p_upsert = sub.add_parser("upsert", help="新增/更新商品缓存")
    p_upsert.add_argument("--product-id", required=True)
    p_upsert.add_argument("--commodity-id", default=None)
    p_upsert.add_argument("--title", default=None)
    p_upsert.add_argument("--cover", default=None)
    p_upsert.add_argument("--cos-ratio", type=float, default=None)
    p_upsert.add_argument("--video-url", default=None)
    p_upsert.add_argument("--account-id", type=int, default=None, help="捕获来源的本地抖音账号 ID")
    p_upsert.add_argument("--gallery-stdin", action="store_true", help="从 stdin 读取商品详情图 URL JSON 列表")
    p_upsert.add_argument("--detail-stdin", action="store_true", help="从 stdin 读取 {gallery, videoUrls} 商品详情数据")

    p_commodity = sub.add_parser("set-commodity", help="补全 product_id 的 commodity_id（推广ID）")
    p_commodity.add_argument("--product-id", required=True)
    p_commodity.add_argument("--commodity-id", required=True)

    p_get = sub.add_parser("get", help="按商品 ID 查询")
    p_get.add_argument("--product-id", required=True)

    p_list = sub.add_parser("list", help="列出缓存商品")
    p_list.add_argument("--limit", type=int, default=50)

    sub.add_parser("count", help="缓存商品总数")

    args = parser.parse_args(argv)
    db_path = Path(args.db)

    try:
        if args.command == "init":
            payload = init_db(db_path)
        elif args.command == "upsert":
            gallery = None
            video_urls = None
            account_id = args.account_id
            if args.gallery_stdin or args.detail_stdin:
                raw_gallery = sys.stdin.readline()
                gallery_value = json.loads(raw_gallery) if raw_gallery else []
                if isinstance(gallery_value, dict):
                    gallery = gallery_value.get("gallery") if isinstance(gallery_value.get("gallery"), list) else []
                    video_urls = gallery_value.get("videoUrls") if isinstance(gallery_value.get("videoUrls"), list) else []
                    if account_id is None:
                        raw_account_id = gallery_value.get("accountId")
                        account_id = raw_account_id if type(raw_account_id) is int and raw_account_id > 0 else None
                else:
                    gallery = gallery_value if isinstance(gallery_value, list) else []
            payload = upsert_product(
                db_path,
                args.product_id,
                args.title,
                args.cover,
                args.cos_ratio,
                args.video_url,
                args.commodity_id,
                gallery,
                video_urls,
                account_id,
            )
        elif args.command == "set-commodity":
            payload = set_commodity(db_path, args.product_id, args.commodity_id)
        elif args.command == "get":
            payload = get_product(db_path, args.product_id)
        elif args.command == "list":
            payload = list_products(db_path, args.limit)
        elif args.command == "count":
            payload = count_products(db_path)
        else:
            parser.error(f"未知命令：{args.command}")
    except sqlite3.Error as error:
        return emit({"ok": False, "error": f"数据库错误：{error}"}, 1)

    return emit(payload)


if __name__ == "__main__":
    sys.exit(main())
