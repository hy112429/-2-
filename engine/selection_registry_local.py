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
        "CREATE INDEX IF NOT EXISTS idx_selection_local_registry_category_state "
        "ON selection_local_registry(state, leaf_category_id, account_key);"
        "CREATE TABLE IF NOT EXISTS selection_local_registry_meta ("
        " key TEXT PRIMARY KEY, value TEXT NOT NULL);"
    )
    # 老表索引只在首次需要时建立，后续历史检查可直接按候选 ID 查找。
    existing_tables = {str(row[0]) for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    if "products" in existing_tables and "product_id" in table_columns(connection, "products"):
        connection.execute("CREATE INDEX IF NOT EXISTS idx_products_product_id_dedupe ON products(product_id)")
    if "selection_quota_events" in existing_tables and {"product_id", "event_type"}.issubset(
            table_columns(connection, "selection_quota_events")):
        connection.execute("CREATE INDEX IF NOT EXISTS idx_selection_quota_product_dedupe "
                           "ON selection_quota_events(event_type, product_id)")
    publish_columns = table_columns(connection, "publish_queue")
    if {"product_id", "status", "platform_status", "account_id"}.issubset(publish_columns):
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_publish_queue_product_lock "
            "ON publish_queue(product_id, status, platform_status, account_id)"
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


def legacy_publish_queue_ids(connection: sqlite3.Connection) -> set[str]:
    columns = table_columns(connection, "publish_queue")
    if not {"product_id", "status"}.issubset(columns):
        return set()
    condition = "(status <> 'cancelled'"
    parameters: list[Any] = []
    if "platform_status" in columns:
        condition += " OR platform_status NOT IN ('not_uploaded', 'isolated')"
    condition += ")"
    query = (
        'SELECT DISTINCT CAST(product_id AS TEXT) FROM "publish_queue" '
        f"WHERE product_id IS NOT NULL AND {condition}"
    )
    return {
        str(row[0]).strip()
        for row in connection.execute(query, parameters)
        if str(row[0] or "").strip()
    }


def active_publish_queue_ids(connection: sqlite3.Connection, candidate_ids: Sequence[str]) -> set[str]:
    columns = table_columns(connection, "publish_queue")
    if not columns or not candidate_ids:
        return set()
    if not {"product_id", "status"}.issubset(columns):
        raise ValueError("本机发布队列结构不完整；已停止选品查重。")
    condition = "(status <> 'cancelled'"
    if "platform_status" in columns:
        condition += " OR platform_status NOT IN ('not_uploaded', 'isolated')"
    condition += ")"
    found: set[str] = set()
    ordered_ids = sorted(set(candidate_ids))
    for offset in range(0, len(ordered_ids), 500):
        chunk = ordered_ids[offset:offset + 500]
        placeholders = ",".join("?" for _ in chunk)
        rows = connection.execute(
            'SELECT DISTINCT CAST(product_id AS TEXT) FROM "publish_queue" '
            f"WHERE product_id IN ({placeholders}) AND {condition}",
            chunk,
        )
        found.update(str(row[0]).strip() for row in rows if str(row[0] or "").strip())
    return found


def migrate_legacy_product_ids(connection: sqlite3.Connection) -> int:
    """把旧商品表/加车事件导入主键索引，仅执行一次，不在每轮选品时重扫历史。"""
    if not any("product_id" in table_columns(connection, table)
               for table in ("products", "selection_quota_events")):
        return 0
    marker = connection.execute(
        "SELECT 1 FROM selection_local_registry_meta WHERE key='legacy_product_ids_v1'"
    ).fetchone()
    if marker:
        return 0
    owned_transaction = not connection.in_transaction
    if owned_transaction:
        connection.execute("BEGIN IMMEDIATE")
    try:
        if connection.execute(
            "SELECT 1 FROM selection_local_registry_meta WHERE key='legacy_product_ids_v1'"
        ).fetchone():
            if owned_transaction:
                connection.commit()
            return 0
        product_ids = sorted(legacy_product_ids(connection))
        timestamp = now_text()
        connection.executemany(
            "INSERT OR IGNORE INTO selection_local_registry "
            "(product_id, account_key, leaf_category_id, reservation_token, request_id, state, updated_at) "
            "VALUES (?, 'legacy:unknown', ?, ?, 'legacy-import-v1', 'COMMITTED', ?)",
            [(product_id, f"__unknown__:{product_id}", f"legacy:{product_id}", timestamp)
             for product_id in product_ids],
        )
        # 已登记后被标为 RELEASED 的 ID 若出现在旧加车记录中，历史成功记录优先。
        for offset in range(0, len(product_ids), 500):
            chunk = product_ids[offset:offset + 500]
            placeholders = ",".join("?" for _ in chunk)
            connection.execute(
                f"UPDATE selection_local_registry SET state='COMMITTED', updated_at=? "
                f"WHERE state='RELEASED' AND product_id IN ({placeholders})",
                [timestamp, *chunk],
            )
        connection.execute(
            "INSERT INTO selection_local_registry_meta(key, value) VALUES ('legacy_product_ids_v1', ?)",
            (timestamp,),
        )
        if owned_transaction:
            connection.commit()
        return len(product_ids)
    except Exception:
        if owned_transaction:
            connection.rollback()
        raise


def migrate_legacy_publish_queue_ids(connection: sqlite3.Connection) -> int:
    """一次性把升级前仍有效的发布队列商品纳入全账号防重索引。"""
    if not {"product_id", "status"}.issubset(table_columns(connection, "publish_queue")):
        return 0
    marker = connection.execute(
        "SELECT 1 FROM selection_local_registry_meta WHERE key='legacy_publish_queue_v1'"
    ).fetchone()
    if marker:
        return 0
    owned_transaction = not connection.in_transaction
    if owned_transaction:
        connection.execute("BEGIN IMMEDIATE")
    try:
        if connection.execute(
            "SELECT 1 FROM selection_local_registry_meta WHERE key='legacy_publish_queue_v1'"
        ).fetchone():
            if owned_transaction:
                connection.commit()
            return 0
        product_ids = sorted(legacy_publish_queue_ids(connection))
        timestamp = now_text()
        connection.executemany(
            "INSERT OR IGNORE INTO selection_local_registry "
            "(product_id, account_key, leaf_category_id, reservation_token, request_id, state, updated_at) "
            "VALUES (?, 'legacy:unknown', ?, ?, 'legacy-publish-queue-v1', 'COMMITTED', ?)",
            [(product_id, f"__unknown__:{product_id}", f"legacy-queue:{product_id}", timestamp)
             for product_id in product_ids],
        )
        for offset in range(0, len(product_ids), 500):
            chunk = product_ids[offset:offset + 500]
            placeholders = ",".join("?" for _ in chunk)
            connection.execute(
                f"UPDATE selection_local_registry SET state='COMMITTED', updated_at=? "
                f"WHERE state='RELEASED' AND product_id IN ({placeholders})",
                [timestamp, *chunk],
            )
        connection.execute(
            "INSERT INTO selection_local_registry_meta(key, value) VALUES ('legacy_publish_queue_v1', ?)",
            (timestamp,),
        )
        if owned_transaction:
            connection.commit()
        return len(product_ids)
    except Exception:
        if owned_transaction:
            connection.rollback()
        raise


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
    return snapshot_batch(connection, [{"accountKey": account_key, "candidates": candidates}])[account_key]


def snapshot_batch(
    connection: sqlite3.Connection,
    entries: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """一次读取多账号候选的历史命中与类目用量，只返回当前候选 ID 命中项。"""
    normalized: list[tuple[str, set[str], set[str]]] = []
    for entry in entries:
        account_key = str(entry.get("accountKey") or "").strip()
        candidates = entry.get("candidates") if isinstance(entry.get("candidates"), list) else []
        if not account_key:
            raise ValueError("本机账号标识为空。")
        ids = {
            str(item.get("productId") or "").strip()
            for item in candidates
            if isinstance(item, dict) and str(item.get("productId") or "").strip()
        }
        categories = {
            str(item.get("leafCategoryId") or "").strip()
            for item in candidates
            if isinstance(item, dict) and str(item.get("leafCategoryId") or "").strip()
            and not str(item.get("leafCategoryId") or "").strip().startswith("__unknown__:")
        }
        normalized.append((account_key, ids, categories))
    if len({account_key for account_key, _, _ in normalized}) != len(normalized):
        raise ValueError("批量本机选品快照包含重复账号。")

    needs_category_source = {"account_id", "completed_json"}.issubset(
        table_columns(connection, "selection_run_accounts")
    )
    needs_product_source = any(
        "product_id" in table_columns(connection, table)
        for table in ("products", "selection_quota_events")
    )
    needs_queue_source = {"product_id", "status"}.issubset(table_columns(connection, "publish_queue"))
    needs_category_migration = needs_category_source and not connection.execute(
        "SELECT 1 FROM selection_local_registry_meta WHERE key='legacy_categories_v1'"
    ).fetchone()
    needs_product_migration = needs_product_source and not connection.execute(
        "SELECT 1 FROM selection_local_registry_meta WHERE key='legacy_product_ids_v1'"
    ).fetchone()
    needs_queue_migration = needs_queue_source and not connection.execute(
        "SELECT 1 FROM selection_local_registry_meta WHERE key='legacy_publish_queue_v1'"
    ).fetchone()
    owned_transaction = not connection.in_transaction and (
        needs_category_migration or needs_product_migration or needs_queue_migration
    )
    if owned_transaction:
        connection.execute("BEGIN IMMEDIATE")
    if needs_category_migration or needs_product_migration or needs_queue_migration:
        try:
            if needs_category_migration:
                migrate_legacy_categories(connection)
            if needs_product_migration:
                migrate_legacy_product_ids(connection)
            if needs_queue_migration:
                migrate_legacy_publish_queue_ids(connection)
            if owned_transaction:
                connection.commit()
        except Exception:
            if owned_transaction:
                connection.rollback()
            raise

    candidate_ids = set().union(*(ids for _, ids, _ in normalized)) if normalized else set()
    used_ids: set[str] = set()
    ordered_ids = sorted(candidate_ids)
    for offset in range(0, len(ordered_ids), 500):
        chunk = ordered_ids[offset:offset + 500]
        placeholders = ",".join("?" for _ in chunk)
        rows = connection.execute(
            f"SELECT product_id FROM selection_local_registry "
            f"WHERE state IN ({','.join('?' for _ in ACTIVE_STATES)}) "
            f"AND product_id IN ({placeholders})",
            [*ACTIVE_STATES, *chunk],
        )
        used_ids.update(str(row["product_id"]) for row in rows)
    used_ids.update(active_publish_queue_ids(connection, ordered_ids))

    category_usage: dict[str, int] = {}
    account_categories: dict[str, set[str]] = {account_key: set() for account_key, _, _ in normalized}
    candidate_categories = sorted(set().union(*(categories for _, _, categories in normalized)) if normalized else set())
    for offset in range(0, len(candidate_categories), 500):
        chunk = candidate_categories[offset:offset + 500]
        placeholders = ",".join("?" for _ in chunk)
        rows = connection.execute(
            "SELECT account_key, leaf_category_id, COUNT(*) AS used_count "
            "FROM selection_local_registry "
            f"WHERE state IN ({','.join('?' for _ in ACTIVE_STATES)}) "
            f"AND leaf_category_id IN ({placeholders}) "
            "GROUP BY account_key, leaf_category_id",
            [*ACTIVE_STATES, *chunk],
        )
        for row in rows:
            category_id = str(row["leaf_category_id"])
            category_usage[category_id] = category_usage.get(category_id, 0) + int(row["used_count"])
            account_key = str(row["account_key"])
            if account_key in account_categories:
                account_categories[account_key].add(category_id)

    return {
        account_key: {
            "usedProductIds": sorted(ids.intersection(used_ids)),
            "accountUsedCategoryIds": sorted(account_categories[account_key]),
            "categoryUsage": category_usage,
            "mode": "local",
        }
        for account_key, ids, _ in normalized
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
        migrate_legacy_product_ids(connection)
        migrate_legacy_publish_queue_ids(connection)
        reserved: list[dict[str, Any]] = []
        rejected: list[dict[str, str]] = []
        requested_categories: set[str] = set()
        candidate_ids = sorted({str(item.get("productId") or "").strip()
                                for item in items if str(item.get("productId") or "").strip()})
        existing_by_product: dict[str, dict[str, Any]] = {}
        for offset in range(0, len(candidate_ids), 500):
            chunk = candidate_ids[offset:offset + 500]
            placeholders = ",".join("?" for _ in chunk)
            for row in connection.execute(
                f"SELECT product_id,state,account_key,leaf_category_id,reservation_token,request_id "
                f"FROM selection_local_registry WHERE product_id IN ({placeholders})", chunk
            ):
                existing_by_product[str(row["product_id"])] = dict(row)
        candidate_categories = sorted({
            str(item.get("leafCategoryId") or "").strip()
            for item in items
            if str(item.get("leafCategoryId") or "").strip() and
            not str(item.get("leafCategoryId") or "").strip().startswith("__unknown__:")
        })
        used_categories: set[str] = set()
        for offset in range(0, len(candidate_categories), 500):
            chunk = candidate_categories[offset:offset + 500]
            placeholders = ",".join("?" for _ in chunk)
            used_categories.update(str(row[0]) for row in connection.execute(
                f"SELECT DISTINCT leaf_category_id FROM selection_local_registry "
                f"WHERE account_key=? AND state IN ({','.join('?' for _ in ACTIVE_STATES)}) "
                f"AND leaf_category_id IN ({placeholders})",
                [account_key, *ACTIVE_STATES, *chunk],
            ))
        active_queue_ids = active_publish_queue_ids(connection, candidate_ids)
        for item in items:
            if max_reserved is not None and len(reserved) >= max_reserved:
                break
            product_id = str(item.get("productId") or "").strip()
            leaf_category_id = str(item.get("leafCategoryId") or "").strip()
            title = str(item.get("title") or "")[:512]
            if not product_id:
                rejected.append({"productId": product_id, "reason": "缺少商品 ID"})
                continue
            if product_id in active_queue_ids:
                rejected.append({"productId": product_id, "reason": "商品 ID 已进入本机发布队列"})
                continue
            if not leaf_category_id:
                # 通用数据模式下类目可能未知；商品 ID 仍必须纳入全局互斥登记。
                leaf_category_id = f"__unknown__:{product_id}"
            existing = existing_by_product.get(product_id)
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
            used_category = leaf_category_id in used_categories if not leaf_category_id.startswith("__unknown__:") else False
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
            existing_by_product[product_id] = {
                "state": "RESERVED", "account_key": account_key,
                "leaf_category_id": leaf_category_id, "reservation_token": token,
                "request_id": request_id,
            }
            if not leaf_category_id.startswith("__unknown__:"):
                used_categories.add(leaf_category_id)
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
    snapshot_parser.add_argument("--candidates-json")
    batch_snapshot_parser = commands.add_parser("snapshot-batch")
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
            candidates = parse_json_argument(args.candidates_json, "候选商品") if args.candidates_json is not None else json.load(sys.stdin)
            if not isinstance(candidates, list) or any(not isinstance(row, dict) for row in candidates):
                raise ValueError("候选商品必须是对象列表。")
            result = snapshot(connection, args.account_key, candidates)
        elif args.command == "snapshot-batch":
            payload = json.load(sys.stdin)
            entries = payload.get("entries") if isinstance(payload, dict) else None
            if not isinstance(entries, list) or any(not isinstance(row, dict) for row in entries):
                raise ValueError("批量本机选品快照必须包含账号对象列表。")
            result = {"snapshots": snapshot_batch(connection, entries)}
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
