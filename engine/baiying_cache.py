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
        title       商品标题
        cover_url   封面图
        cos_ratio   佣金率（%）
        video_url   商品素材视频直链（无签名时效，抓到即用，过期需重新捕获）
        captured_at 抓取时间

设计要点
    · 与 ad_client.db 同库（D:/software/投广客户端/data/ad_client.db），WAL + 每连接外键。
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

# ============================================================ 常量

# 与 db.py 同一个数据库文件
DEFAULT_DB_PATH = r"D:\software\投广客户端\data\ad_client.db"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS baiying_products (
    product_id  TEXT    PRIMARY KEY,             -- 业务商品 ID（19 位数字）
    commodity_id TEXT,                           -- 推广 ID（pack_detail 触发所必需，探针可从选品列表接口补全）
    title       TEXT,
    cover_url   TEXT,
    cos_ratio   REAL,                            -- 佣金率（%），如 5.0 = 5%
    video_url   TEXT,                            -- 商品素材视频直链
    captured_at TEXT    NOT NULL                 -- 抓取时间（本地 ISO8601）
);
"""

# 老库迁移：补充 commodity_id 列（列已存在时忽略）
MIGRATION_ALTERS = [
    "ALTER TABLE baiying_products ADD COLUMN commodity_id TEXT",
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
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def row_to_dict(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    return dict(row) if row is not None else None


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
    video_url: str,
    commodity_id: Optional[str] = None,
) -> Dict[str, Any]:
    with connect(db_path) as connection:
        existing = connection.execute(
            "SELECT * FROM baiying_products WHERE product_id = ?", (product_id,)
        ).fetchone()
        connection.execute(
            "INSERT INTO baiying_products (product_id, commodity_id, title, cover_url, cos_ratio, video_url, captured_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(product_id) DO UPDATE SET "
            "commodity_id = COALESCE(excluded.commodity_id, baiying_products.commodity_id), "
            "title = excluded.title, cover_url = excluded.cover_url, "
            "cos_ratio = excluded.cos_ratio, video_url = excluded.video_url, "
            "captured_at = excluded.captured_at",
            (product_id, commodity_id, title, cover_url, cos_ratio, video_url, now_text()),
        )
        row = connection.execute(
            "SELECT * FROM baiying_products WHERE product_id = ?", (product_id,)
        ).fetchone()
    return {"ok": True, "created": existing is None, "product": dict(row)}


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
    product = row_to_dict(row)
    return {"ok": True, "found": product is not None, "product": product}


def list_products(db_path: Path, limit: int) -> Dict[str, Any]:
    with connect(db_path) as connection:
        rows = connection.execute(
            "SELECT * FROM baiying_products ORDER BY captured_at DESC, product_id LIMIT ?",
            (limit,),
        ).fetchall()
    return {"ok": True, "count": len(rows), "products": [dict(r) for r in rows]}


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
    p_upsert.add_argument("--video-url", required=True)

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
            payload = upsert_product(
                db_path,
                args.product_id,
                args.title,
                args.cover,
                args.cos_ratio,
                args.video_url,
                args.commodity_id,
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
