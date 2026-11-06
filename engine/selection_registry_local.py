#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""单机选品登记：在客户端 SQLite 中原子维护本机商品 ID 和账号末级类目。"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Sequence

try:
    from .client_paths import DEFAULT_DB_PATH
except ImportError:
    from client_paths import DEFAULT_DB_PATH


ACTIVE_STATES = ("RESERVED", "ATTEMPTING", "COMMITTED", "UNCERTAIN")


def now_text() -> str:
    return datetime.now().isoformat(timespec="seconds")


def emit(payload: dict[str, Any], code: int = 0) -> int:
    print(json.dumps(payload, ensure_ascii=False))
    return code


def connect(database: Path) -> sqlite3.Connection:
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(database), timeout=20, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 20000")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.executescript(
        "CREATE TABLE IF NOT EXISTS selection_local_registry ("
        " product_id TEXT PRIMARY KEY, account_key TEXT NOT NULL, leaf_category_id TEXT NOT NULL,"
        " reservation_token TEXT NOT NULL UNIQUE, request_id TEXT NOT NULL, state TEXT NOT NULL,"
        " updated_at TEXT NOT NULL);"
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_selection_local_account_category_active "
        "ON selection_local_registry(account_key, leaf_category_id) "
        "WHERE state IN ('RESERVED','ATTEMPTING','COMMITTED','UNCERTAIN');"
        "CREATE INDEX IF NOT EXISTS idx_selection_local_registry_state "
        "ON selection_local_registry(state, updated_at);"
        "CREATE TABLE IF NOT EXISTS selection_local_registry_meta ("
        " key TEXT PRIMARY KEY, value TEXT NOT NULL);"
    )
    return connection


def table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if not exists:
        return set()
    return {str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')}


def legacy_product_ids(connection: sqlite3.Connection) -> set[str]:
    result: set[str] = set()
    for table in ("products", "selection_quota_events"):
        columns = table_columns(connection, table)
        if "product_id" not in columns:
            continue
        query = f'SELECT DISTINCT CAST(product_id AS TEXT) FROM "{table}" WHERE product_id IS NOT NULL'
        if table == "selection_quota_events" and "event_type" in columns:
            query += " AND event_type='cart'"
        result.update(str(row[0]).strip() for row in connection.execute(query) if str(row[0] or "").strip())
    return result


def migrate_legacy_categories(connection: sqlite3.Connection) -> None:
    """从仍保留的旧选品检查点补回能识别到的本机账号类目历史。"""
    migrated = connection.execute(
        "SELECT 1 FROM selection_local_registry_meta WHERE key='legacy_categories_v1'"
    ).fetchone()
    if migrated:
        return
    columns = table_columns(connection, "selection_run_accounts")
    if not {"account_id", "completed_json"}.issubset(columns):
        return
    for row in connection.execute(
        "SELECT account_id, completed_json FROM selection_run_accounts WHERE completed_json IS NOT NULL"
    ):
        try:
            completed = json.loads(row["completed_json"])
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(completed, list):
            continue
        account_key = f"local:{int(row['account_id'])}"
        for item in completed:
            if not isinstance(item, dict) or item.get("cartAdded") is not True:
                continue
            product_id = str(item.get("productId") or "").strip()
            category_ids = item.get("categoryIds") if isinstance(item.get("categoryIds"), list) else []
            leaf_category_id = str(item.get("leafCategoryId") or (category_ids[-1] if category_ids else "")).strip()
            if not product_id or not leaf_category_id:
                continue
            connection.execute(
                "INSERT OR IGNORE INTO selection_local_registry "
                "(product_id, account_key, leaf_category_id, reservation_token, request_id, state, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 'COMMITTED', ?)",
                (product_id, account_key, leaf_category_id, f"legacy:{product_id}", "legacy", now_text()),
            )
    connection.execute(
        "INSERT OR IGNORE INTO selection_local_registry_meta(key, value) VALUES ('legacy_categories_v1', ?)",
        (now_text(),),
    )


def snapshot(
    connection: sqlite3.Connection,
    account_key: str,
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    if not account_key.strip():
        raise ValueError("本机账号标识为空。")
    migrate_legacy_categories(connection)
    used_products = legacy_product_ids(connection)
    account_categories: set[str] = set()
    category_usage: dict[str, int] = {}
    active_rows = connection.execute(
        "SELECT product_id, account_key, leaf_category_id FROM selection_local_registry "
        "WHERE state IN ('RESERVED','ATTEMPTING','COMMITTED','UNCERTAIN')"
    ).fetchall()
    for row in active_rows:
        used_products.add(str(row["product_id"]))
        category_id = str(row["leaf_category_id"])
        if category_id.startswith("__unknown__:"):
            continue
        category_usage[category_id] = category_usage.get(category_id, 0) + 1
        if str(row["account_key"]) == account_key:
            account_categories.add(category_id)
    return {
        "usedProductIds": sorted(used_products),
        "accountUsedCategoryIds": sorted(account_categories),
        "categoryUsage": category_usage,
        "mode": "local",
    }


def reserve(
    connection: sqlite3.Connection,
    account_key: str,
    request_id: str,
    items: list[dict[str, Any]],
    max_reserved: Optional[int] = None,
) -> dict[str, Any]:
    if not account_key.strip() or not request_id.strip():
        raise ValueError("本机账号标识或请求标识为空。")
    if max_reserved is not None and max_reserved < 0:
        raise ValueError("本机选品预留数量上限不能小于 0。")
    connection.execute("BEGIN IMMEDIATE")
    try:
        migrate_legacy_categories(connection)
        old_product_ids = legacy_product_ids(connection)
        reserved: list[dict[str, Any]] = []
        rejected: list[dict[str, str]] = []
        requested_categories: set[str] = set()
        for item in items:
            if max_reserved is not None and len(reserved) >= max_reserved:
                break
            product_id = str(item.get("productId") or "").strip()
            leaf_category_id = str(item.get("leafCategoryId") or "").strip()
            title = str(item.get("title") or "")[:512]
            if not product_id:
                rejected.append({"productId": product_id, "reason": "缺少商品 ID"})
                continue
            if not leaf_category_id:
                # 通用数据模式下类目可能未知；商品 ID 仍必须纳入全局互斥登记。
                leaf_category_id = f"__unknown__:{product_id}"
            existing = connection.execute(
                "SELECT state, account_key, leaf_category_id, reservation_token, request_id "
                "FROM selection_local_registry WHERE product_id=?", (product_id,)
            ).fetchone()
            if product_id in old_product_ids:
                rejected.append({"productId": product_id, "reason": "商品 ID 已在本机账号池记录"})
                continue
            if existing and existing["state"] in ACTIVE_STATES:
                if (
                    existing["state"] == "RESERVED"
                    and str(existing["account_key"]) == account_key
                    and str(existing["leaf_category_id"]) == leaf_category_id
                    and str(existing["request_id"]) == request_id
                ):
                    if not leaf_category_id.startswith("__unknown__:"):
                        requested_categories.add(leaf_category_id)
                    reserved.append({
                        "productId": product_id,
                        "token": str(existing["reservation_token"]),
                        "state": "RESERVED",
                        "title": title,
                    })
                else:
                    rejected.append({"productId": product_id, "reason": "商品 ID 已在本机账号池预留或待核查"})
                continue
            used_category = None if leaf_category_id.startswith("__unknown__:") else connection.execute(
                "SELECT 1 FROM selection_local_registry WHERE account_key=? AND leaf_category_id=? "
                "AND state IN ('RESERVED','ATTEMPTING','COMMITTED','UNCERTAIN') LIMIT 1",
                (account_key, leaf_category_id),
            ).fetchone()
            if used_category or (not leaf_category_id.startswith("__unknown__:") and leaf_category_id in requested_categories):
                rejected.append({"productId": product_id, "reason": "该账号历史或本轮已使用此末级类目"})
                continue
            token = str(uuid.uuid4())
            if existing:
                connection.execute(
                    "UPDATE selection_local_registry SET account_key=?, leaf_category_id=?, reservation_token=?, "
                    "request_id=?, state='RESERVED', updated_at=? WHERE product_id=? AND state='RELEASED'",
                    (account_key, leaf_category_id, token, request_id, now_text(), product_id),
                )
            else:
                connection.execute(
                    "INSERT INTO selection_local_registry "
                    "(product_id, account_key, leaf_category_id, reservation_token, request_id, state, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, 'RESERVED', ?)",
                    (product_id, account_key, leaf_category_id, token, request_id, now_text()),
                )
            if not leaf_category_id.startswith("__unknown__:"):
                requested_categories.add(leaf_category_id)
            reserved.append({"productId": product_id, "token": token, "state": "RESERVED", "title": title})
        connection.commit()
        return {"reserved": reserved, "rejected": rejected, "mode": "local"}
    except Exception:
        connection.rollback()
        raise


def begin(connection: sqlite3.Connection, token: str) -> dict[str, Any]:
    cursor = connection.execute(
        "UPDATE selection_local_registry SET state='ATTEMPTING', updated_at=? "
        "WHERE reservation_token=? AND state='RESERVED'",
        (now_text(), token),
    )
    if cursor.rowcount != 1:
        row = connection.execute(
            "SELECT state FROM selection_local_registry WHERE reservation_token=?", (token,)
        ).fetchone()
        if not row or row["state"] != "ATTEMPTING":
            raise ValueError("找不到本机预留记录，未执行加车。")
    return {"ok": True}


def finish(connection: sqlite3.Connection, token: str, outcome: str) -> dict[str, Any]:
    states = {"ADDED": "COMMITTED", "NOT_ADDED": "RELEASED", "UNKNOWN": "UNCERTAIN"}
    if outcome not in states:
        raise ValueError("本机登记结算状态无效。")
    target = states[outcome]
    cursor = connection.execute(
        "UPDATE selection_local_registry SET state=?, updated_at=? WHERE reservation_token=? "
        "AND state IN ('RESERVED','ATTEMPTING')",
        (target, now_text(), token),
    )
    if cursor.rowcount != 1:
        row = connection.execute(
            "SELECT state FROM selection_local_registry WHERE reservation_token=?", (token,)
        ).fetchone()
        if not row or row["state"] != target:
            raise ValueError("找不到本机预留记录，登记未结算。")
    return {"ok": True, "state": target}


def parse_json_argument(value: str, label: str) -> list[dict[str, Any]]:
    try:
        rows = json.loads(value)
    except json.JSONDecodeError as error:
        raise ValueError(f"{label}不是有效 JSON。") from error
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError(f"{label}必须是对象列表。")
    return rows


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="本机选品商品与类目登记")
    parser.add_argument("--db", default=DEFAULT_DB_PATH)
    commands = parser.add_subparsers(dest="command", required=True)
    snapshot_parser = commands.add_parser("snapshot")
    snapshot_parser.add_argument("--account-key", required=True)
    snapshot_parser.add_argument("--candidates-json", default="[]")
    reserve_parser = commands.add_parser("reserve")
    reserve_parser.add_argument("--account-key", required=True)
    reserve_parser.add_argument("--request-id", required=True)
    reserve_parser.add_argument("--items-json", required=True)
    reserve_parser.add_argument("--max-reserved", type=int)
    begin_parser = commands.add_parser("begin")
    begin_parser.add_argument("--token", required=True)
    finish_parser = commands.add_parser("finish")
    finish_parser.add_argument("--token", required=True)
    finish_parser.add_argument("--outcome", choices=("ADDED", "NOT_ADDED", "UNKNOWN"), required=True)
    args = parser.parse_args(argv)
    connection = connect(Path(args.db))
    try:
        if args.command == "snapshot":
            result = snapshot(connection, args.account_key, parse_json_argument(args.candidates_json, "候选商品"))
        elif args.command == "reserve":
            result = reserve(
                connection,
                args.account_key,
                args.request_id,
                parse_json_argument(args.items_json, "预留商品"),
                max_reserved=args.max_reserved,
            )
        elif args.command == "begin":
            result = begin(connection, args.token)
        else:
            result = finish(connection, args.token, args.outcome)
        return emit({"ok": True, **result})
    except (ValueError, sqlite3.Error, OSError) as error:
        return emit({"ok": False, "error": str(error)}, 1)
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
