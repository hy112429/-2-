#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""选品批次恢复、过程日志与幂等配额记账。"""

from __future__ import annotations

import argparse
import hashlib
import json
import ntpath
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

try:
    from .client_paths import DEFAULT_DB_PATH
except ImportError:
    from client_paths import DEFAULT_DB_PATH



def now_text() -> str:
    return datetime.now().isoformat(timespec="seconds")


def emit(payload: Dict[str, Any], code: int = 0) -> int:
    # Electron 按行读取；大批次的多行 JSON 会触发逐行反复解析。
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return code


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(db_path), timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA busy_timeout = 30000")
    connection.executescript(
        "CREATE TABLE IF NOT EXISTS selection_run_batches ("
        "batch_id TEXT PRIMARY KEY, state TEXT NOT NULL, payload_json TEXT NOT NULL, "
        "created_at TEXT NOT NULL, updated_at TEXT NOT NULL);"
        "CREATE TABLE IF NOT EXISTS selection_run_accounts ("
        "batch_id TEXT NOT NULL, account_id INTEGER NOT NULL, account_name TEXT NOT NULL, "
        "sequence_no INTEGER NOT NULL, state TEXT NOT NULL, stage TEXT NOT NULL DEFAULT '', "
        "error TEXT NOT NULL DEFAULT '', candidates_json TEXT, selected_indexes_json TEXT, "
        "completed_json TEXT, source_pool_state_json TEXT, updated_at TEXT NOT NULL, "
        "PRIMARY KEY(batch_id, account_id), "
        "FOREIGN KEY(batch_id) REFERENCES selection_run_batches(batch_id));"
        "CREATE TABLE IF NOT EXISTS selection_run_logs ("
        "log_id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id TEXT NOT NULL, "
        "account_id INTEGER NOT NULL, account_name TEXT NOT NULL, product_id TEXT NOT NULL DEFAULT '', "
        "product_title TEXT NOT NULL DEFAULT '', stage TEXT NOT NULL, code TEXT NOT NULL, "
        "message TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, "
        "FOREIGN KEY(batch_id) REFERENCES selection_run_batches(batch_id));"
        "CREATE INDEX IF NOT EXISTS idx_selection_run_batches_state "
        "ON selection_run_batches(state, updated_at);"
        "CREATE INDEX IF NOT EXISTS idx_selection_run_logs_batch_time "
        "ON selection_run_logs(batch_id, log_id);"
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_one_unfinished_selection_batch "
        "ON selection_run_batches((1)) WHERE state IN ('in_progress', 'needs_retry');"
        "CREATE TABLE IF NOT EXISTS selection_quota_events ("
        "event_key TEXT PRIMARY KEY, batch_id TEXT NOT NULL, usage_date TEXT NOT NULL, "
        "account_id INTEGER NOT NULL, product_id TEXT NOT NULL, event_type TEXT NOT NULL, "
        "video_count INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);"
        "CREATE TABLE IF NOT EXISTS product_list_rows ("
        "account_id INTEGER NOT NULL, account_name TEXT NOT NULL, product_id TEXT NOT NULL, "
        "title TEXT NOT NULL DEFAULT '', commodity_id TEXT NOT NULL DEFAULT '', source_url TEXT NOT NULL DEFAULT '', "
        "cover_url TEXT NOT NULL DEFAULT '', sku TEXT NOT NULL DEFAULT '', category_labels_json TEXT NOT NULL DEFAULT '[]', "
        "candidate_json TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'pending', failure_reason TEXT NOT NULL DEFAULT '', batch_id TEXT NOT NULL, "
        "selected_at TEXT NOT NULL, updated_at TEXT NOT NULL, deleted_at TEXT, "
        "PRIMARY KEY(account_id, product_id));"
        "CREATE TABLE IF NOT EXISTS product_list_meta (meta_key TEXT PRIMARY KEY, meta_value TEXT NOT NULL);"
        "CREATE INDEX IF NOT EXISTS idx_product_list_rows_status ON product_list_rows(status, updated_at);"
    )
    product_columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(product_list_rows)")}
    if "candidate_json" not in product_columns:
        connection.execute("ALTER TABLE product_list_rows ADD COLUMN candidate_json TEXT NOT NULL DEFAULT '{}'")
    account_columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(selection_run_accounts)")}
    if "source_pool_state_json" not in account_columns:
        connection.execute("ALTER TABLE selection_run_accounts ADD COLUMN source_pool_state_json TEXT")
    connection.commit()
    return connection


def connect_readonly(db_path: Path) -> sqlite3.Connection:
    """选品批次读取专用连接；不建表、不迁移、不写入数据库。"""
    resolved_path = db_path.expanduser().resolve()
    if not resolved_path.is_file():
        raise FileNotFoundError(f"找不到本机数据库：{resolved_path}")
    connection = sqlite3.connect(f"{resolved_path.as_uri()}?mode=ro", uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def decode_json(value: Optional[str], fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def account_dict(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "batchId": row["batch_id"],
        "accountId": int(row["account_id"]),
        "accountName": row["account_name"],
        "sequence": int(row["sequence_no"]),
        "state": row["state"],
        "stage": row["stage"],
        "error": row["error"],
        "candidates": decode_json(row["candidates_json"], None),
        "selectedIndexes": decode_json(row["selected_indexes_json"], None),
        "completed": decode_json(row["completed_json"], None),
        "sourcePoolState": decode_json(row["source_pool_state_json"], None)
        if "source_pool_state_json" in row.keys() else None,
        "updatedAt": row["updated_at"],
    }


def sanitize_log_text(value: Any, limit: int) -> str:
    """只保留诊断文本；遮蔽常见凭据字段并限制单条日志长度。"""
    text = str(value or "")
    text = re.sub(r"(?im)^\s*(cookie|set-cookie|authorization)\s*:\s*.*$", r"\1: [已脱敏]", text)
    text = re.sub(
        r"(?i)([?&][^=&]*(?:token|secret|signature|cookie|session|authorization)[^=&]*=)[^&\s]+",
        r"\1[已脱敏]",
        text,
    )
    secret_key = r"cookie|set-cookie|authorization|[\w-]*token|[\w-]*secret|[\w-]*signature"
    text = re.sub(
        rf"(?i)([\"']?(?:{secret_key})[\"']\s*:\s*[\"'])[^\"']*([\"'])",
        r"\1[已脱敏]\2",
        text,
    )
    text = re.sub(rf"(?i)\b({secret_key})\s*=\s*([^\s,;&]+)", r"\1=[已脱敏]", text)
    text = re.sub(rf"(?i)\b({secret_key})\s*:\s*([^\s,;&]+)", r"\1: [已脱敏]", text)
    return text[:limit]


def run_log_dict(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": int(row["log_id"]),
        "batchId": row["batch_id"],
        "accountId": int(row["account_id"]),
        "accountName": row["account_name"],
        "productId": row["product_id"],
        "productTitle": row["product_title"],
        "stage": row["stage"],
        "code": row["code"],
        "message": row["message"],
        "detail": row["detail"],
        "createdAt": row["created_at"],
    }


def list_run_logs(connection: sqlite3.Connection, batch_id: str, limit: Optional[int] = None) -> list[Dict[str, Any]]:
    query = (
        "SELECT log_id, batch_id, account_id, account_name, product_id, product_title, "
        "stage, code, message, detail, created_at FROM selection_run_logs "
        "WHERE batch_id = ? ORDER BY log_id"
    )
    if limit is None:
        rows = connection.execute(query, (batch_id,)).fetchall()
    else:
        rows = connection.execute(query + " DESC LIMIT ?", (batch_id, max(1, limit))).fetchall()
        rows.reverse()
    return [run_log_dict(row) for row in rows]


def append_run_log(
    connection: sqlite3.Connection, batch_id: str, account_id: int, value: Any
) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("过程日志内容无效")
    batch = connection.execute(
        "SELECT batch_id FROM selection_run_batches WHERE batch_id = ?", (batch_id,)
    ).fetchone()
    if not batch:
        raise ValueError("找不到过程日志所属的选品批次")
    if account_id == 0:
        account_name = "选品批次"
    else:
        account = connection.execute(
            "SELECT account_name FROM selection_run_accounts WHERE batch_id = ? AND account_id = ?",
            (batch_id, account_id),
        ).fetchone()
        if not account:
            raise ValueError("找不到过程日志所属的批次账号")
        account_name = str(account["account_name"])
    stage = sanitize_log_text(value.get("stage"), 80).strip()
    code = sanitize_log_text(value.get("code") or "SELECTION_EVENT", 80).strip()
    message = sanitize_log_text(value.get("message"), 1000).strip()
    if not stage or not message:
        raise ValueError("过程日志必须包含阶段和说明")
    cursor = connection.execute(
        "INSERT INTO selection_run_logs(batch_id, account_id, account_name, product_id, product_title, "
        "stage, code, message, detail, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            batch_id,
            account_id,
            account_name,
            sanitize_log_text(value.get("productId"), 120),
            sanitize_log_text(value.get("productTitle"), 300),
            stage,
            code,
            message,
            sanitize_log_text(value.get("detail"), 8000),
            now_text(),
        ),
    )
    connection.commit()
    row = connection.execute(
        "SELECT * FROM selection_run_logs WHERE log_id = ?", (cursor.lastrowid,)
    ).fetchone()
    return run_log_dict(row) if row else {}


def append_run_logs(
    connection: sqlite3.Connection, batch_id: str, account_id: int, values: Any
) -> int:
    """在一个事务中批量追加同一批次、同一账号的过程日志。"""
    if not isinstance(values, list) or any(not isinstance(value, dict) for value in values):
        raise ValueError("过程日志批量内容必须是对象列表")
    if len(values) > 2000:
        raise ValueError("单次过程日志最多写入 2000 条")
    if not values:
        return 0
    owned_transaction = not connection.in_transaction
    if owned_transaction:
        connection.execute("BEGIN IMMEDIATE")
    try:
        batch = connection.execute(
            "SELECT batch_id FROM selection_run_batches WHERE batch_id = ?", (batch_id,)
        ).fetchone()
        if not batch:
            raise ValueError("找不到过程日志所属的选品批次")
        if account_id == 0:
            account_name = "选品批次"
        else:
            account = connection.execute(
                "SELECT account_name FROM selection_run_accounts WHERE batch_id = ? AND account_id = ?",
                (batch_id, account_id),
            ).fetchone()
            if not account:
                raise ValueError("找不到过程日志所属的批次账号")
            account_name = str(account["account_name"])
        rows = []
        for value in values:
            stage = sanitize_log_text(value.get("stage"), 80).strip()
            code = sanitize_log_text(value.get("code") or "SELECTION_EVENT", 80).strip()
            message = sanitize_log_text(value.get("message"), 1000).strip()
            if not stage or not message:
                raise ValueError("过程日志必须包含阶段和说明")
            rows.append((
                batch_id,
                account_id,
                account_name,
                sanitize_log_text(value.get("productId"), 120),
                sanitize_log_text(value.get("productTitle"), 300),
                stage,
                code,
                message,
                sanitize_log_text(value.get("detail"), 8000),
                now_text(),
            ))
        connection.executemany(
            "INSERT INTO selection_run_logs(batch_id, account_id, account_name, product_id, product_title, "
            "stage, code, message, detail, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        if owned_transaction:
            connection.commit()
        return len(rows)
    except Exception:
        if owned_transaction:
            connection.rollback()
        raise


# 保留旧函数名，兼容已有脚本和测试调用。
append_failure_log = append_run_log


def batch_dict(connection: sqlite3.Connection, row: sqlite3.Row) -> Dict[str, Any]:
    accounts = connection.execute(
        "SELECT * FROM selection_run_accounts WHERE batch_id = ? ORDER BY sequence_no, account_id",
        (row["batch_id"],),
    ).fetchall()
    # 启动恢复只需近期日志；完整过程记录由 batch logs 单独查询。
    run_logs = list_run_logs(connection, row["batch_id"], limit=200)
    return {
        "batchId": row["batch_id"],
        "state": row["state"],
        "payload": decode_json(row["payload_json"], {}),
        "accounts": [account_dict(item) for item in accounts],
        "runLogs": run_logs,
        "failureLogs": run_logs,
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def create_batch(connection: sqlite3.Connection, batch_id: str, payload: Any) -> Dict[str, Any]:
    if not batch_id.strip() or not isinstance(payload, dict):
        raise ValueError("批次标识或批次内容无效")
    accounts = payload.get("accounts")
    if not isinstance(accounts, list) or not accounts:
        raise ValueError("批次至少需要一个启用账号")
    timestamp = now_text()
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO selection_run_batches(batch_id, state, payload_json, created_at, updated_at) "
            "VALUES (?, 'in_progress', ?, ?, ?)",
            (batch_id, json.dumps(payload, ensure_ascii=False), timestamp, timestamp),
        )
        for sequence, account in enumerate(accounts):
            if not isinstance(account, dict) or not isinstance(account.get("id"), int):
                raise ValueError("批次账号信息无效")
            connection.execute(
                "INSERT INTO selection_run_accounts(batch_id, account_id, account_name, sequence_no, state, updated_at) "
                "VALUES (?, ?, ?, ?, 'queued', ?)",
                (batch_id, account["id"], str(account.get("name", account["id"])), sequence, timestamp),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    row = connection.execute("SELECT * FROM selection_run_batches WHERE batch_id = ?", (batch_id,)).fetchone()
    return batch_dict(connection, row)


POOL_CANDIDATE_FIELDS = frozenset({
    "cardIndex", "productId", "commodityId", "title", "price", "monthSales", "saleAmount",
    "estimatedSalesAmount", "conversionRate", "views", "commissionRate", "shopScore",
    "trendRising", "trendKnown", "trendStatus", "trendSlope", "trendPoints",
    "dataMetrics", "metricSources", "poolRank", "sources", "sourceKey",
    "categoryIds", "leafCategoryId", "categoryStatus", "categorySource", "sourceCategoryIds",
    "sourceCategoryId", "sourceCategoryName", "sources", "sourceRecords",
    "quotaRuleId", "sourceCategorySelection",
    "analysisScore", "unifiedScore", "reusedFromBatchId",
})


def _bounded_checkpoint_int(value: Any, maximum: int) -> int:
    try:
        number = int(float(value))
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, min(maximum, number))


def _pool_category_id(value: Any) -> str:
    return str(value or "").strip().rsplit("__RC_CASCADER_SPLIT__", 1)[-1]


def _pool_excluded_branches(settings: Dict[str, Any]) -> tuple[set[str], set[tuple[str, str]]]:
    """把二、三级排除项映射到唯一的二级分支；映射不明时拒绝新商品入池。"""
    selection = settings.get("excludedCategorySelection")
    selection = selection if isinstance(selection, dict) else {}
    tree = settings.get("categoryTree") if isinstance(settings.get("categoryTree"), list) else []
    excluded_roots = selection.get("level1")
    blocked_roots = set()
    if isinstance(excluded_roots, list):
        blocked_roots = {_pool_category_id(item) for item in excluded_roots if _pool_category_id(item)}
    blocked_seconds: set[tuple[str, str]] = set()
    for level in (2, 3):
        values = selection.get(f"level{level}")
        if not isinstance(values, list):
            continue
        for blocked in values:
            blocked_id = _pool_category_id(blocked)
            if not blocked_id:
                continue
            matches: set[tuple[str, str]] = set()
            for root in tree:
                if not isinstance(root, dict):
                    continue
                root_id = _pool_category_id(root.get("id"))
                seconds = root.get("children")
                if not isinstance(seconds, list):
                    continue
                for second in seconds:
                    if not isinstance(second, dict):
                        continue
                    second_id = _pool_category_id(second.get("id"))
                    if level == 2:
                        found = second_id == blocked_id
                    else:
                        thirds = second.get("children")
                        found = isinstance(thirds, list) and any(
                            isinstance(third, dict) and _pool_category_id(third.get("id")) == blocked_id
                            for third in thirds
                        )
                    if root_id and second_id and found:
                        matches.add((root_id, second_id))
            if len(matches) != 1:
                raise ValueError(f"排除的{level}级类目无法在批次百应目录中唯一定位二级分支")
            blocked_seconds.update(matches)
    return blocked_roots, blocked_seconds


def save_candidate_pool(connection: sqlite3.Connection, batch_id: str, value: Any) -> Dict[str, Any]:
    """按商品 ID 增量保存本批只读候选；不保存账号凭证、原始响应或加车状态。"""
    if not isinstance(value, dict):
        raise ValueError("候选池检查点格式无效")
    row = connection.execute(
        "SELECT state,payload_json FROM selection_run_batches WHERE batch_id=?", (batch_id,)
    ).fetchone()
    if not row or row["state"] not in {"in_progress", "needs_retry"}:
        raise ValueError("候选池所属批次不存在或已经结束")
    payload = decode_json(row["payload_json"], {})
    if not isinstance(payload, dict):
        raise ValueError("选品批次快照无效")
    settings = payload.get("settingsSnapshot") if isinstance(payload.get("settingsSnapshot"), dict) else {}
    collector_ids = settings.get("candidateCollectorAccountIds")
    target = settings.get("candidatePoolTarget")
    if not isinstance(collector_ids, list) or not 1 <= len(collector_ids) <= 2 or not isinstance(target, int) or not 1 <= target <= 2000:
        raise ValueError("批次缺少有效的候选采集账号或池目标数")
    if value.get("target") != target or value.get("collectorAccountIds") != collector_ids:
        raise ValueError("候选池配置与批次启动快照不一致")
    mode = settings.get("candidatePoolMode", "total")
    if mode not in {"total", "quota"} or value.get("mode", "total") != mode:
        raise ValueError("候选池模式与批次快照不一致")
    quota_rules = settings.get("candidatePoolQuotas") if mode == "quota" else []
    if mode == "quota":
        if not isinstance(quota_rules, list) or not quota_rules or value.get("quotaRules") != quota_rules:
            raise ValueError("品类配额与批次快照不一致")
        if any(not isinstance(rule, dict) or not isinstance(rule.get("target"), int) or
               isinstance(rule.get("target"), bool) or rule["target"] < 1 for rule in quota_rules):
            raise ValueError("品类配额数量无效")
        if sum(rule["target"] for rule in quota_rules) != target:
            raise ValueError("品类配额合计与批次目标不一致")
    rule_targets = {str(rule["id"]): rule["target"] for rule in quota_rules} if mode == "quota" else {}
    rules_by_id = {str(rule["id"]): rule for rule in quota_rules} if mode == "quota" else {}
    fingerprint = value.get("filterFingerprint")
    if not isinstance(fingerprint, str) or not fingerprint or len(fingerprint) > 100_000:
        raise ValueError("候选池缺少有效的筛选指纹")
    previous = payload.get("candidatePool") if isinstance(payload.get("candidatePool"), dict) else {}
    if previous.get("filterFingerprint") != fingerprint:
        previous = {}
    existing_rows = previous.get("rows") if isinstance(previous.get("rows"), list) else []
    incoming_rows = value.get("rows")
    if not isinstance(incoming_rows, list):
        raise ValueError("候选池商品列表格式无效")
    revalidate_ids = value.get("revalidateProductIds", [])
    if not isinstance(revalidate_ids, list) or any(not isinstance(item, str) or not item.isdigit() for item in revalidate_ids):
        raise ValueError("待重核商品 ID 格式无效")
    revalidate = set(revalidate_ids)
    pending_rows = previous.get("revalidationRows") if isinstance(previous.get("revalidationRows"), list) else []
    pending_by_id = {
        str(item.get("productId")): item for item in pending_rows
        if isinstance(item, dict) and str(item.get("productId") or "").isdigit()
    }
    for item in existing_rows:
        if isinstance(item, dict) and str(item.get("productId") or "") in revalidate:
            pending_by_id[str(item["productId"])] = item
    existing_rows = [item for item in existing_rows if isinstance(item, dict) and
                     str(item.get("productId") or "") not in revalidate]
    excluded_ids = value.get("excludedProductIds", [])
    if not isinstance(excluded_ids, list) or any(not isinstance(item, str) or not item.isdigit() for item in excluded_ids):
        raise ValueError("候选池防重剔除 ID 格式无效")
    prior_excluded = previous.get("excludedProductIds", [])
    if not isinstance(prior_excluded, list) or any(not isinstance(item, str) or not item.isdigit() for item in prior_excluded):
        prior_excluded = []
    excluded = set(prior_excluded) | set(excluded_ids)
    for product_id in excluded:
        pending_by_id.pop(product_id, None)
    by_product: dict[str, dict[str, Any]] = {}
    origin_rows_by_batch: dict[str, dict[str, dict[str, Any]]] = {}
    excluded_branches: Optional[tuple[set[str], set[tuple[str, str]]]] = None
    for candidate, is_existing in ([(row, True) for row in existing_rows] +
                                   [(row, False) for row in incoming_rows]):
        if not isinstance(candidate, dict):
            continue
        product_id = str(candidate.get("productId") or "").strip()
        if product_id in excluded:
            continue
        title = str(candidate.get("title") or "").strip()
        if not product_id.isdigit() or not title:
            continue
        safe = {key: candidate[key] for key in POOL_CANDIDATE_FIELDS if key in candidate}
        safe["productId"] = product_id
        safe["title"] = title[:512]
        if mode == "quota" and safe.get("quotaRuleId") not in rule_targets:
            raise ValueError("候选商品没有有效的品类配额归属")
        if mode == "quota":
            rule = rules_by_id[safe["quotaRuleId"]]
            path = rule["categoryPath"]
            category_ids = safe.get("categoryIds") if isinstance(safe.get("categoryIds"), list) else []
            source_ids = safe.get("sourceCategoryIds") if isinstance(safe.get("sourceCategoryIds"), list) else []
            matches_exact = len(category_ids) >= len(path) and all(
                str(category_ids[index]).rsplit("__RC_CASCADER_SPLIT__", 1)[-1] ==
                str(expected).rsplit("__RC_CASCADER_SPLIT__", 1)[-1]
                for index, expected in enumerate(path)
            )
            matches_source_path = (safe.get("categoryStatus") in {"source_level", "known"} and
                                   len(source_ids) >= max(1, len(path)) and all(
                str(source_ids[index]).rsplit("__RC_CASCADER_SPLIT__", 1)[-1] ==
                str(expected).rsplit("__RC_CASCADER_SPLIT__", 1)[-1]
                for index, expected in enumerate(path)
            ))
            quota_path_confirmed = (matches_exact or matches_source_path) if is_existing else matches_source_path
            if safe.get("sourceKey") != rule["sourceKey"] or not quota_path_confirmed:
                raise ValueError("候选商品来源或类目与所归属配额不一致")
            if not is_existing:
                if not 1 <= len(source_ids) <= 3 or any(not _pool_category_id(item) for item in source_ids):
                    raise ValueError("候选商品缺少已确认的页面类目路径")
                if len(category_ids) == 3 and any(
                    _pool_category_id(category_ids[index]) != _pool_category_id(source_ids[index])
                    for index in range(min(len(source_ids), 3))
                ):
                    raise ValueError("候选商品详情类目与来源页面类目路径冲突")
                if excluded_branches is None:
                    excluded_branches = _pool_excluded_branches(settings)
                blocked_roots, blocked_seconds = excluded_branches
                root_id = _pool_category_id(source_ids[0])
                blocked_under_root = {second for root, second in blocked_seconds if root == root_id}
                if root_id in blocked_roots or (blocked_under_root and (
                    len(source_ids) < 2 or _pool_category_id(source_ids[1]) in blocked_under_root
                )):
                    raise ValueError("候选商品来源路径命中排除分支或不足以避开排除分支")
        safe["collectedByAccountId"] = int(candidate.get("collectedByAccountId") or value.get("collectorAccountId") or 0)
        safe["capturedAt"] = str(candidate.get("capturedAt") or value.get("capturedAt") or now_text())[:40]
        origin_batch_id = str(safe.get("reusedFromBatchId") or "").strip()
        if origin_batch_id:
            if origin_batch_id not in origin_rows_by_batch:
                origin_row = connection.execute(
                    "SELECT payload_json FROM selection_run_batches WHERE batch_id=? AND batch_id<>?",
                    (origin_batch_id, batch_id),
                ).fetchone()
                origin_payload = decode_json(origin_row["payload_json"], {}) if origin_row else {}
                origin_pool = origin_payload.get("candidatePool") if isinstance(origin_payload, dict) else None
                origin_rows = origin_pool.get("rows") if isinstance(origin_pool, dict) and origin_pool.get("status") in {"ready", "collecting"} else None
                origin_rows_by_batch[origin_batch_id] = {
                    str(item.get("productId")): item for item in origin_rows if isinstance(item, dict)
                } if isinstance(origin_rows, list) else {}
            origin_candidate = origin_rows_by_batch[origin_batch_id].get(product_id)
            if not origin_candidate or origin_candidate.get("collectedByAccountId") != safe["collectedByAccountId"] or (
                str(origin_candidate.get("capturedAt") or "") != safe["capturedAt"]
            ):
                raise ValueError("复用候选商品的原批次来源无法核对")
        elif safe["collectedByAccountId"] not in collector_ids:
            raise ValueError("候选商品来源账号不属于本批采集账号")
        previous_candidate = by_product.get(product_id)
        if not is_existing:
            pending_by_id.pop(product_id, None)
        if previous_candidate is None:
            by_product[product_id] = safe
        else:
            if mode == "quota" and safe.get("quotaRuleId") != previous_candidate.get("quotaRuleId"):
                for field in (
                    "quotaRuleId", "sourceKey", "categoryIds", "sourceCategoryIds", "categoryStatus",
                    "categorySource", "sourceCategoryId", "sourceCategoryName", "sourceCategorySelection",
                ):
                    if field in safe:
                        previous_candidate[field] = safe[field]
            merged_sources = list(dict.fromkeys([
                *(previous_candidate.get("sources") if isinstance(previous_candidate.get("sources"), list) else []),
                *(safe.get("sources") if isinstance(safe.get("sources"), list) else []),
            ]))
            if merged_sources:
                previous_candidate["sources"] = merged_sources
            records = [
                item for item in [
                    *(previous_candidate.get("sourceRecords") if isinstance(previous_candidate.get("sourceRecords"), list) else []),
                    *(safe.get("sourceRecords") if isinstance(safe.get("sourceRecords"), list) else []),
                ] if isinstance(item, dict)
            ]
            unique_records: list[dict[str, Any]] = []
            seen_records: set[tuple[str, str, str]] = set()
            for item in records:
                key = (str(item.get("sourceKey") or ""),
                       "/".join(str(part) for part in item.get("categoryPath", []) if str(part))
                       if isinstance(item.get("categoryPath"), list) else "",
                       str(item.get("rankBoard") or ""))
                if key in seen_records:
                    continue
                seen_records.add(key)
                unique_records.append({
                    key: value for key, value in item.items()
                    if key in {"sourceKey", "categoryPath", "rankBoard", "sourceCategoryId", "sourceCategoryName"}
                })
            if unique_records:
                previous_candidate["sourceRecords"] = unique_records[:20]
    if len(by_product) > 2000:
        raise ValueError("候选池超过 2000 件上限")
    quota_transfers = value.get("quotaTransfers", previous.get("quotaTransfers", [])) if mode == "quota" else []
    if mode == "quota":
        if not isinstance(quota_transfers, list) or len(quota_transfers) > 2000:
            raise ValueError("候选池缺额转移记录格式无效")
        effective_targets = dict(rule_targets)
        for transfer in quota_transfers:
            if not isinstance(transfer, dict):
                raise ValueError("候选池缺额转移记录格式无效")
            from_id, to_id, count = transfer.get("fromRuleId"), transfer.get("toRuleId"), transfer.get("count")
            if (from_id not in effective_targets or to_id not in effective_targets or from_id == to_id or
                    not isinstance(count, int) or isinstance(count, bool) or count < 1):
                raise ValueError("候选池缺额转移引用或数量无效")
            effective_targets[from_id] -= count
            effective_targets[to_id] += count
        if any(number < 0 for number in effective_targets.values()) or sum(effective_targets.values()) != target:
            raise ValueError("候选池缺额转移不能改变总池目标")
    else:
        effective_targets = {}
    quota_counts = {rule_id: 0 for rule_id in rule_targets}
    for candidate in by_product.values():
        if mode == "quota":
            quota_counts[candidate["quotaRuleId"]] += 1
    if any(quota_counts[rule_id] > effective_targets[rule_id] for rule_id in rule_targets):
        raise ValueError("候选商品超过单条有效品类配额")
    state = value.get("sourceState")
    if not isinstance(state, dict):
        raise ValueError("候选池采集游标无效")
    collectors = state.get("collectors") if isinstance(state.get("collectors"), dict) else {}
    clean_collectors: dict[str, dict[str, Any]] = {}
    for account_id in collector_ids:
        collector = collectors.get(str(account_id))
        if not isinstance(collector, dict):
            continue
        offsets = collector.get("tabOffsets") if isinstance(collector.get("tabOffsets"), dict) else {}
        clean_offsets = {str(key): max(0, int(number)) for key, number in offsets.items()
                         if str(key) in {"hotSpot", "allianceRank", "selectionSquare"} and str(number).isdigit()}
        raw_scroll_positions = collector.get("scrollPositions") if isinstance(collector.get("scrollPositions"), dict) else {}
        clean_scroll_positions = {
            str(key): _bounded_checkpoint_int(number, 100_000_000)
            for key, number in raw_scroll_positions.items()
            if len(str(key)) <= 512 and any(
                str(key) == source or str(key).startswith(f"{source}:")
                for source in ("hotSpot", "allianceRank", "selectionSquare")
            )
        }
        clean_collectors[str(account_id)] = {
            "tabOffsets": clean_offsets,
            "samePoolCount": max(0, int(collector.get("samePoolCount") or 0)),
            "rateLimitCount": _bounded_checkpoint_int(collector.get("rateLimitCount"), 5),
            "nextRetryAt": str(collector.get("nextRetryAt") or "")[:40],
            "scrollSteps": max(0, min(120, int(collector.get("scrollSteps") or 0))),
            "rawLimit": max(0, min(2000, int(collector.get("rawLimit") or 0))),
            "scrollPositions": clean_scroll_positions,
            "detailAttemptedIds": sorted({str(item) for item in collector.get("detailAttemptedIds", [])
                                           if str(item).isdigit()})[-2000:]
            if isinstance(collector.get("detailAttemptedIds", []), list) else [],
            "ruleStates": {
                str(rule_id): {
                    "samePoolCount": max(0, min(100, int(rule_state.get("samePoolCount") or 0))),
                    "nextRetryAt": str(rule_state.get("nextRetryAt") or "")[:40],
                    "exhausted": rule_state.get("exhausted") is True,
                    "scrollSteps": max(0, min(120, int(rule_state.get("scrollSteps") or 0))),
                    "rawLimit": max(0, min(2000, int(rule_state.get("rawLimit") or 0))),
                    "scrollPosition": _bounded_checkpoint_int(rule_state.get("scrollPosition"), 100_000_000),
                    "categoryTabOffset": _bounded_checkpoint_int(rule_state.get("categoryTabOffset"), 100_000),
                    "detailAttemptedIds": sorted({str(item) for item in rule_state.get("detailAttemptedIds", [])
                                                   if str(item).isdigit()})[-2000:]
                    if isinstance(rule_state.get("detailAttemptedIds", []), list) else [],
                }
                for rule_id, rule_state in (collector.get("ruleStates") or {}).items()
                if rule_id in rule_targets and isinstance(rule_state, dict)
            } if isinstance(collector.get("ruleStates"), dict) else {},
        }
    transfer_in = {rule_id: 0 for rule_id in rule_targets}
    transfer_out = {rule_id: 0 for rule_id in rule_targets}
    for transfer in quota_transfers:
        transfer_out[transfer["fromRuleId"]] += transfer["count"]
        transfer_in[transfer["toRuleId"]] += transfer["count"]
    quota_progress = [
        {"ruleId": rule["id"], "collected": quota_counts[rule["id"]], "target": rule["target"],
         "effectiveTarget": effective_targets[rule["id"]], "transferredIn": transfer_in[rule["id"]],
         "transferredOut": transfer_out[rule["id"]],
         "deficit": effective_targets[rule["id"]] - quota_counts[rule["id"]]}
        for rule in quota_rules
    ]
    pool = {
        "target": target,
        "mode": mode,
        "quotaRules": quota_rules,
        "quotaProgress": quota_progress,
        "quotaTransfers": quota_transfers,
        "excludedProductIds": sorted(excluded),
        "collectorAccountIds": collector_ids,
        "filterFingerprint": fingerprint,
        "rows": list(by_product.values()),
        "revalidationRows": list(pending_by_id.values()),
        "sourceState": {
            "autoResumeAllowed": state.get("autoResumeAllowed") is not False,
            "collectors": clean_collectors,
        },
        "status": "ready" if (all(item["deficit"] == 0 for item in quota_progress) if mode == "quota" else len(by_product) >= target) else "collecting",
        "capturedAt": str(value.get("capturedAt") or now_text())[:40],
    }
    payload["candidatePool"] = pool
    with connection:
        connection.execute(
            "UPDATE selection_run_batches SET payload_json=?,updated_at=? WHERE batch_id=? AND state IN ('in_progress','needs_retry')",
            (json.dumps(payload, ensure_ascii=False, separators=(",", ":")), now_text(), batch_id),
        )
    return {"status": pool["status"], "count": len(by_product), "target": target}


def _candidate_captured_at(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        captured = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if captured.tzinfo is None:
        captured = captured.astimezone()
    return captured.astimezone(timezone.utc)


def _same_collector_profiles(current: Dict[str, Any], previous: Dict[str, Any], collector_ids: list[int]) -> bool:
    current_contexts = current.get("accountContexts")
    previous_contexts = previous.get("accountContexts")
    if not isinstance(current_contexts, dict) or not isinstance(previous_contexts, dict):
        return False
    for account_id in collector_ids:
        current_context = current_contexts.get(str(account_id))
        previous_context = previous_contexts.get(str(account_id))
        if not isinstance(current_context, dict) or not isinstance(previous_context, dict):
            return False
        for field in ("profileKey", "resolvedProfilePath"):
            identity = str(current_context.get(field) or "").strip()
            if not identity or identity != str(previous_context.get(field) or "").strip():
                return False
    return True


def saved_candidate_pool_summary(batch_id: str, payload: Any, updated_at: str) -> Optional[Dict[str, Any]]:
    if not isinstance(payload, dict):
        return None
    pool = payload.get("candidatePool")
    if not isinstance(pool, dict) or pool.get("status") not in {"ready", "collecting"}:
        return None
    rows = pool.get("rows")
    pending = pool.get("revalidationRows") if isinstance(pool.get("revalidationRows"), list) else []
    if not isinstance(rows, list):
        return None
    saved_rows = [item for item in [*rows, *pending] if isinstance(item, dict)]
    if not saved_rows:
        return None
    product_ids = sorted({str(item.get("productId") or "").strip()
                          for item in saved_rows if str(item.get("productId") or "").strip()})
    pool_fingerprint = hashlib.sha256("\n".join(product_ids).encode("utf-8")).hexdigest()
    now = datetime.now(timezone.utc)
    earliest = now - timedelta(hours=24)
    eligible = [item for item in rows if isinstance(item, dict) and
                (captured := _candidate_captured_at(item.get("capturedAt"))) is not None and
                earliest <= captured <= now]
    source_counts: dict[str, int] = {}
    for item in saved_rows:
        if isinstance(item, dict):
            source = str(item.get("sourceKey") or "unknown")
            source_counts[source] = source_counts.get(source, 0) + 1
    settings = payload.get("settingsSnapshot") if isinstance(payload.get("settingsSnapshot"), dict) else {}
    thresholds = settings.get("thresholds") if isinstance(settings.get("thresholds"), dict) else {}
    excluded = settings.get("excludedCategorySelection") if isinstance(settings.get("excludedCategorySelection"), dict) else {}
    return {
        "batchId": batch_id,
        "poolFingerprint": pool_fingerprint,
        "duplicateBatches": [],
        "status": pool["status"],
        "count": len(rows), "recentCount": len(eligible),
        "pendingRevalidationCount": len(pending),
        "target": pool.get("target"), "mode": pool.get("mode", "total"),
        "capturedAt": pool.get("capturedAt"), "updatedAt": updated_at,
        "collectorAccountIds": pool.get("collectorAccountIds", []),
        "sourceCounts": source_counts,
        "minimumCommissionRate": thresholds.get("commissionRate"),
        "maximumCommissionRate": thresholds.get("commissionRateMax"),
        "excludedCategoryCount": sum(len(excluded.get(level, [])) for level in ("level1", "level2", "level3")
                                     if isinstance(excluded.get(level), list)),
        "preview": [
            {"productId": str(item.get("productId") or ""), "title": str(item.get("title") or "")[:120],
             "sourceKey": str(item.get("sourceKey") or "")}
            for item in saved_rows[:20]
        ],
    }


def list_saved_candidate_pools(connection: sqlite3.Connection, limit: int = 10) -> list[Dict[str, Any]]:
    output: list[Dict[str, Any]] = []
    by_fingerprint: dict[str, Dict[str, Any]] = {}
    for row in connection.execute(
        "SELECT batch_id,payload_json,updated_at FROM selection_run_batches "
        "WHERE payload_json LIKE '%\"candidatePool\"%' ORDER BY updated_at DESC"
    ):
        summary = saved_candidate_pool_summary(
            row["batch_id"], decode_json(row["payload_json"], {}), row["updated_at"]
        )
        if summary:
            fingerprint = str(summary.pop("poolFingerprint"))
            existing = by_fingerprint.get(fingerprint)
            if existing:
                existing["duplicateBatches"].append({
                    "batchId": summary["batchId"],
                    "capturedAt": summary["capturedAt"],
                    "updatedAt": summary["updatedAt"],
                })
            else:
                by_fingerprint[fingerprint] = summary
                output.append(summary)
    return output[:max(1, limit)]


def list_saved_candidate_pool_items(connection: sqlite3.Connection, batch_id: str,
                                    query: str = "", limit: int = 20, offset: int = 0) -> Dict[str, Any]:
    row = connection.execute(
        "SELECT payload_json,updated_at FROM selection_run_batches WHERE batch_id=?", (batch_id,)
    ).fetchone()
    payload = decode_json(row["payload_json"], {}) if row else {}
    if not row or not saved_candidate_pool_summary(batch_id, payload, row["updated_at"]):
        raise ValueError("所选本机候选池不存在或没有已存商品")
    pool = payload["candidatePool"]
    pending_rows = pool.get("revalidationRows") if isinstance(pool.get("revalidationRows"), list) else []
    rows = [(item, False) for item in pool["rows"]] + [(item, True) for item in pending_rows]
    keyword = query.strip().casefold()[:120]
    matching = [(item, pending) for item, pending in rows if isinstance(item, dict) and (
        not keyword or keyword in str(item.get("productId") or "").casefold() or
        keyword in str(item.get("title") or "").casefold()
    )]
    page_size = max(1, min(int(limit), 50))
    start = max(0, int(offset))
    visible_fields = POOL_CANDIDATE_FIELDS | {"capturedAt", "collectedByAccountId"}
    return {
        "batchId": batch_id, "total": len(matching), "limit": page_size, "offset": start,
        "rows": [{**{key: item[key] for key in visible_fields if key in item}, "revalidationPending": pending}
                 for item, pending in matching[start:start + page_size]],
    }


def saved_candidate_pool_settings(connection: sqlite3.Connection, batch_id: str) -> Dict[str, Any]:
    row = connection.execute(
        "SELECT payload_json,updated_at FROM selection_run_batches WHERE batch_id=?", (batch_id,)
    ).fetchone()
    payload = decode_json(row["payload_json"], {}) if row else {}
    summary = saved_candidate_pool_summary(batch_id, payload, row["updated_at"]) if row else None
    settings = payload.get("settingsSnapshot") if isinstance(payload, dict) else None
    if not summary or not isinstance(settings, dict):
        raise ValueError("所选本机候选池不存在或没有已存商品")
    summary.pop("poolFingerprint", None)
    return {
        "pool": summary, "settingsSnapshot": settings,
        "filterFingerprint": payload["candidatePool"].get("filterFingerprint"),
    }


def reuse_candidate_pool(
    connection: sqlite3.Connection, batch_id: str, fingerprint: Any, source_batch_id: str = ""
) -> Dict[str, Any]:
    """合并本机已存候选；旧值仅作重核线索，不复制采集游标或加车结果。"""
    if not isinstance(fingerprint, str) or not fingerprint or len(fingerprint) > 100_000:
        raise ValueError("候选池复用缺少有效的筛选指纹")
    current_row = connection.execute(
        "SELECT state,payload_json FROM selection_run_batches WHERE batch_id=?", (batch_id,)
    ).fetchone()
    if not current_row or current_row["state"] not in {"in_progress", "needs_retry"}:
        raise ValueError("候选池复用的目标批次不存在或已经结束")
    current = decode_json(current_row["payload_json"], {})
    if not isinstance(current, dict):
        raise ValueError("候选池复用的目标批次快照无效")
    existing = current.get("candidatePool")
    if isinstance(existing, dict):
        return {"count": 0, "reason": "current_pool_exists"}
    settings = current.get("settingsSnapshot")
    if not isinstance(settings, dict):
        raise ValueError("候选池复用的目标批次缺少设置快照")
    collector_ids = settings.get("candidateCollectorAccountIds")
    if not isinstance(collector_ids, list) or not 1 <= len(collector_ids) <= 2 or any(
        not isinstance(item, int) or isinstance(item, bool) for item in collector_ids
    ):
        raise ValueError("候选池复用的采集账号快照无效")
    mode = settings.get("candidatePoolMode", "total")
    if mode not in {"total", "quota"}:
        raise ValueError("候选池复用的模式无效")
    current_time = datetime.now(timezone.utc)
    earliest = current_time - timedelta(hours=24)
    comparable_fields = (
        ("sources", "thresholds", "filters", "bannedWords", "excludedCategorySelection")
        if mode == "quota" else
        ("sources", "thresholds", "filters", "bannedWords", "selectionMode", "categoryMode",
         "categorySelection", "excludedCategorySelection", "candidatePoolMode", "candidatePoolQuotas")
    )
    historical_rows = connection.execute(
        "SELECT batch_id,payload_json FROM selection_run_batches "
        "WHERE batch_id<>? AND payload_json LIKE '%\"candidatePool\"%' "
        "ORDER BY updated_at DESC", (batch_id,),
    ).fetchall()
    if source_batch_id:
        if not any(row["batch_id"] == source_batch_id for row in historical_rows):
            selected_row = connection.execute(
                "SELECT batch_id,payload_json FROM selection_run_batches WHERE batch_id=? AND batch_id<>?",
                (source_batch_id, batch_id),
            ).fetchone()
            if selected_row:
                historical_rows.insert(0, selected_row)
        historical_rows.sort(key=lambda row: row["batch_id"] != source_batch_id)
    rule_targets = {
        str(rule.get("id")): rule.get("target")
        for rule in settings.get("candidatePoolQuotas", []) if isinstance(rule, dict)
    } if mode == "quota" else {}
    rule_counts = {rule_id: 0 for rule_id in rule_targets}
    direct_rows: list[dict[str, Any]] = []
    recheck_ids: list[str] = []
    seen_ids: set[str] = set()
    source_batches: list[str] = []
    selected_found = not source_batch_id
    target = int(settings.get("candidatePoolTarget") or 0)
    history_candidates: list[tuple[datetime, dict[str, Any], bool, bool, str]] = []
    for historical in historical_rows:
        previous = decode_json(historical["payload_json"], {})
        if not isinstance(previous, dict):
            continue
        old_pool = previous.get("candidatePool")
        old_settings = previous.get("settingsSnapshot")
        if not isinstance(old_pool, dict) or not isinstance(old_settings, dict):
            continue
        if old_pool.get("status") not in {"ready", "collecting"}:
            continue
        old_rows = old_pool.get("rows")
        if not isinstance(old_rows, list):
            continue
        pending_rows = old_pool.get("revalidationRows") if isinstance(old_pool.get("revalidationRows"), list) else []
        if historical["batch_id"] == source_batch_id:
            selected_found = True
        same_rules = (
            old_pool.get("filterFingerprint") == fingerprint and old_pool.get("mode", "total") == mode and
            all(settings.get(field) == old_settings.get(field) for field in comparable_fields) and
            settings.get("categoryTree") == old_settings.get("categoryTree") and
            old_pool.get("collectorAccountIds") == collector_ids and
            _same_collector_profiles(current, previous, collector_ids)
        )
        for candidate, is_pending in ([(item, False) for item in old_rows] +
                                      [(item, True) for item in pending_rows]):
            if not isinstance(candidate, dict):
                continue
            captured = _candidate_captured_at(candidate.get("capturedAt"))
            history_candidates.append((captured or datetime.min.replace(tzinfo=timezone.utc),
                                       candidate, is_pending, same_rules, historical["batch_id"]))
    history_candidates.sort(key=lambda entry: (entry[0], entry[4] == source_batch_id), reverse=True)
    for captured, candidate, is_pending, same_rules, source_id in history_candidates:
        product_id = str(candidate.get("productId") or "").strip()
        if not product_id.isdigit() or product_id in seen_ids:
            continue
        seen_ids.add(product_id)
        if source_id not in source_batches:
            source_batches.append(source_id)
        source_path = candidate.get("sourceCategoryIds")
        path_confirmed = isinstance(source_path, list) and 1 <= len(source_path) <= 3 and not any(
            _pool_category_id(part) in {"", "0"} for part in source_path
        ) and candidate.get("categoryStatus") in {"source_level", "known"}
        direct = (not is_pending and same_rules and captured is not None and
                  earliest <= captured <= current_time and path_confirmed and len(direct_rows) < target)
        if direct and mode == "quota":
            rule_id = str(candidate.get("quotaRuleId") or "")
            rule_target = rule_targets.get(rule_id)
            direct = isinstance(rule_target, int) and rule_counts.get(rule_id, 0) < rule_target
        if direct:
            if mode == "quota":
                rule_id = str(candidate["quotaRuleId"])
                rule_counts[rule_id] += 1
            direct_rows.append({**candidate, "reusedFromBatchId": source_id})
        elif len(recheck_ids) < 2000:
            recheck_ids.append(product_id)
    if not selected_found:
        return {"count": 0, "reason": "selected_pool_unavailable"}
    if not direct_rows and not recheck_ids:
        return {"count": 0, "reason": "no_saved_pool_items"}
    # 重核线索必须先随批次落盘；即使随后采集进程中断，下次仍能优先核对旧 ID。
    current["candidatePoolReuseHints"] = recheck_ids
    with connection:
        connection.execute(
            "UPDATE selection_run_batches SET payload_json=?,updated_at=? WHERE batch_id=?",
            (json.dumps(current, ensure_ascii=False, separators=(",", ":")), now_text(), batch_id),
        )
    validation_error = ""
    if direct_rows:
        value = {
            "target": settings.get("candidatePoolTarget"),
            "mode": mode,
            "quotaRules": settings.get("candidatePoolQuotas") if mode == "quota" else [],
            "quotaTransfers": [],
            "collectorAccountIds": collector_ids,
            "collectorAccountId": collector_ids[0],
            "filterFingerprint": fingerprint,
            "capturedAt": current_time.isoformat(),
            "rows": direct_rows,
            "sourceState": {"collectors": {}},
        }
        try:
            saved = save_candidate_pool(connection, batch_id, value)
            return {"count": saved["count"], "status": saved["status"],
                    "recheckCount": len(recheck_ids), "sourceBatchIds": source_batches}
        except ValueError as cause:
            # 旧版来源路径不满足当前保存规则时，保留旧 ID 等待当前来源重核。
            validation_error = str(cause)[:240]
            recheck_ids = list(dict.fromkeys([*recheck_ids, *(str(item["productId"]) for item in direct_rows)]))[:2000]
            current["candidatePoolReuseHints"] = recheck_ids
            with connection:
                connection.execute(
                    "UPDATE selection_run_batches SET payload_json=?,updated_at=? WHERE batch_id=?",
                    (json.dumps(current, ensure_ascii=False, separators=(",", ":")), now_text(), batch_id),
                )
    return {"count": 0, "status": "collecting", "recheckCount": len(recheck_ids),
            "sourceBatchIds": source_batches, "detail": validation_error}


def latest_incomplete_batch(connection: sqlite3.Connection) -> Optional[Dict[str, Any]]:
    row = connection.execute(
        "SELECT * FROM selection_run_batches WHERE state IN ('in_progress', 'needs_retry') "
        "ORDER BY updated_at DESC, created_at DESC LIMIT 1"
    ).fetchone()
    return batch_dict(connection, row) if row else None


def recent_batch(connection: sqlite3.Connection) -> Optional[Dict[str, Any]]:
    row = connection.execute(
        "SELECT * FROM selection_run_batches ORDER BY created_at DESC, updated_at DESC LIMIT 1"
    ).fetchone()
    return batch_dict(connection, row) if row else None


def read_batch_checkpoint(connection: sqlite3.Connection, action: str) -> Optional[Dict[str, Any]]:
    """只读恢复检查点；空库直接返回，残缺表结构明确报错。"""
    existing_tables = {
        str(row[0]) for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    if "selection_run_batches" not in existing_tables:
        return None
    if not {"selection_run_accounts", "selection_run_logs"}.issubset(existing_tables):
        raise ValueError("选品批次表结构不完整，请检查本地数据库")
    return latest_incomplete_batch(connection) if action == "latest" else recent_batch(connection)


def list_batches(
    connection: sqlite3.Connection,
    limit: int = 25,
    offset: int = 0,
    account_id: Optional[int] = None,
    state: str = "",
    start_date: str = "",
    end_date: str = "",
) -> Dict[str, Any]:
    """只读分页查询选品批次摘要及历史账号筛选项。"""
    existing_tables = {
        str(row[0]) for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    if "selection_run_batches" not in existing_tables:
        return {
            "ok": True,
            "items": [],
            "accountOptions": [],
            "total": 0,
            "limit": min(100, max(1, int(limit))),
            "offset": max(0, int(offset)),
        }
    if not {"selection_run_accounts", "selection_run_logs"}.issubset(existing_tables):
        raise ValueError("选品日志表结构不完整，当前只读查询无法读取")

    allowed_states = {"in_progress", "needs_retry", "completed", "failed", "abandoned"}
    if state and state not in allowed_states:
        raise ValueError("批次状态筛选值无效")
    for value, label in ((start_date, "开始日期"), (end_date, "结束日期")):
        if value:
            try:
                datetime.strptime(value, "%Y-%m-%d")
            except ValueError as error:
                raise ValueError(f"{label}格式无效，应为 YYYY-MM-DD") from error
    if start_date and end_date and start_date > end_date:
        raise ValueError("开始日期不能晚于结束日期")

    safe_limit = min(100, max(1, int(limit)))
    safe_offset = max(0, int(offset))
    clauses: list[str] = []
    params: list[Any] = []
    if account_id is not None:
        clauses.append(
            "EXISTS (SELECT 1 FROM selection_run_accounts a "
            "WHERE a.batch_id = b.batch_id AND a.account_id = ?)"
        )
        params.append(int(account_id))
    if state:
        clauses.append("b.state = ?")
        params.append(state)
    if start_date:
        clauses.append("substr(b.created_at, 1, 10) >= ?")
        params.append(start_date)
    if end_date:
        clauses.append("substr(b.created_at, 1, 10) <= ?")
        params.append(end_date)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""

    total = int(connection.execute(
        f"SELECT COUNT(*) FROM selection_run_batches b{where}", params
    ).fetchone()[0])
    rows = connection.execute(
        "SELECT b.batch_id, b.state, b.created_at, b.updated_at, "
        "(SELECT COUNT(*) FROM selection_run_accounts a WHERE a.batch_id = b.batch_id) AS account_count, "
        "(SELECT COUNT(*) FROM selection_run_logs l WHERE l.batch_id = b.batch_id) AS log_count, "
        "(SELECT group_concat(account_name, '、') FROM selection_run_accounts a WHERE a.batch_id = b.batch_id) AS account_names "
        f"FROM selection_run_batches b{where} "
        "ORDER BY b.created_at DESC, b.updated_at DESC LIMIT ? OFFSET ?",
        [*params, safe_limit, safe_offset],
    ).fetchall()
    accounts = connection.execute(
        "SELECT account_id, MAX(account_name) AS account_name FROM selection_run_accounts "
        "GROUP BY account_id ORDER BY account_name COLLATE NOCASE, account_id"
    ).fetchall()
    return {
        "ok": True,
        "items": [
            {
                "batchId": row["batch_id"],
                "state": row["state"],
                "createdAt": row["created_at"],
                "updatedAt": row["updated_at"],
                "accountCount": int(row["account_count"] or 0),
                "logCount": int(row["log_count"] or 0),
                "accountNames": row["account_names"] or "",
            }
            for row in rows
        ],
        "accountOptions": [
            {"accountId": int(row["account_id"]), "accountName": row["account_name"]}
            for row in accounts
        ],
        "total": total,
        "limit": safe_limit,
        "offset": safe_offset,
    }


def batch_log_details(connection: sqlite3.Connection, batch_id: str) -> Dict[str, Any]:
    """只返回日志页需要的批次摘要、账号状态和日志，不暴露候选快照或路径载荷。"""
    batch = connection.execute(
        "SELECT batch_id, state, created_at, updated_at FROM selection_run_batches WHERE batch_id = ?",
        (batch_id,),
    ).fetchone()
    if not batch:
        raise ValueError("找不到选品批次")
    accounts = connection.execute(
        "SELECT account_id, account_name, sequence_no, state, stage, error, updated_at "
        "FROM selection_run_accounts WHERE batch_id = ? ORDER BY sequence_no, account_id",
        (batch_id,),
    ).fetchall()
    logs = list_run_logs(connection, batch_id)
    return {
        "batch": {
            "batchId": batch["batch_id"],
            "state": batch["state"],
            "createdAt": batch["created_at"],
            "updatedAt": batch["updated_at"],
        },
        "accounts": [
            {
                "accountId": int(row["account_id"]),
                "accountName": sanitize_log_text(row["account_name"], 120),
                "sequence": int(row["sequence_no"]),
                "state": row["state"],
                "stage": row["stage"],
                "error": sanitize_log_text(row["error"], 8000),
                "updatedAt": row["updated_at"],
            }
            for row in accounts
        ],
        "runLogs": [
            {
                **item,
                "accountName": sanitize_log_text(item["accountName"], 120),
                "productId": sanitize_log_text(item["productId"], 120),
                "productTitle": sanitize_log_text(item["productTitle"], 300),
                "stage": sanitize_log_text(item["stage"], 80),
                "code": sanitize_log_text(item["code"], 80),
                "message": sanitize_log_text(item["message"], 1000),
                "detail": sanitize_log_text(item["detail"], 8000),
            }
            for item in reversed(logs)
        ],
    }


def get_account(connection: sqlite3.Connection, batch_id: str, account_id: int) -> Dict[str, Any]:
    row = connection.execute(
        "SELECT * FROM selection_run_accounts WHERE batch_id = ? AND account_id = ?",
        (batch_id, account_id),
    ).fetchone()
    if not row:
        raise ValueError("找不到批次中的账号任务")
    return account_dict(row)


def update_account(connection: sqlite3.Connection, batch_id: str, account_id: int, value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("账号任务更新内容无效")
    batch = connection.execute(
        "SELECT state FROM selection_run_batches WHERE batch_id = ?", (batch_id,)
    ).fetchone()
    if not batch:
        raise ValueError("找不到待更新的批次")
    if batch["state"] == "abandoned":
        raise ValueError("批次已结束，检查点只读保留，不能继续更新")
    current = get_account(connection, batch_id, account_id)
    allowed = {
        "state": "state",
        "stage": "stage",
        "error": "error",
        "candidates": "candidates",
        "selectedIndexes": "selected_indexes",
        "completed": "completed",
        "sourcePoolState": "source_pool_state",
    }
    merged = {key: current.get(key) for key in allowed}
    merged.update({key: value[key] for key in allowed if key in value})
    timestamp = now_text()
    connection.execute(
        "UPDATE selection_run_accounts SET state = ?, stage = ?, error = ?, candidates_json = ?, "
        "selected_indexes_json = ?, completed_json = ?, source_pool_state_json = ?, updated_at = ? "
        "WHERE batch_id = ? AND account_id = ?",
        (
            str(merged["state"]), str(merged["stage"] or ""), str(merged["error"] or ""),
            json.dumps(merged["candidates"], ensure_ascii=False) if merged["candidates"] is not None else None,
            json.dumps(merged["selectedIndexes"], ensure_ascii=False) if merged["selectedIndexes"] is not None else None,
            json.dumps(merged["completed"], ensure_ascii=False) if merged["completed"] is not None else None,
            json.dumps(merged["sourcePoolState"], ensure_ascii=False) if merged["sourcePoolState"] is not None else None,
            timestamp, batch_id, account_id,
        ),
    )
    sync_product_list_rows(connection, batch_id, account_id, merged)
    connection.execute(
        "UPDATE selection_run_batches SET updated_at = ? WHERE batch_id = ?", (timestamp, batch_id)
    )
    connection.commit()
    return get_account(connection, batch_id, account_id)


PRODUCT_STATUS_ORDER = {
    "pending": 0,
    "selection_failed": 1,
    "needs_review": 2,
    "cart_added": 3,
    "publish_failed": 4,
    "published": 5,
    "uploaded": 6,
}


def _candidate_category_labels(candidate: Dict[str, Any]) -> list[str]:
    for key in ("categoryLabels", "categoryNames", "categoryPathNames"):
        value = candidate.get(key)
        if isinstance(value, list):
            labels = [str(item).strip() for item in value if str(item).strip()]
            if labels:
                return labels[:6]
    for key in ("leafCategoryName", "categoryName", "category"):
        value = candidate.get(key)
        if isinstance(value, str) and value.strip():
            return [value.strip()]
    return []


def _product_cart_state(item: Optional[Dict[str, Any]]) -> tuple[str, str]:
    if not item:
        return "pending", ""
    if item.get("skipped") is True:
        return "needs_review", str(item.get("status") or "本批被本机选品登记拦截，请核对后处理")[:500]
    if item.get("cartAdded") is True:
        return "cart_added", ""
    reason = str(item.get("error") or item.get("status") or "加车结果未确认")[:500]
    normalized = reason.lower()
    if item.get("cartAttempted") is True and any(word in normalized for word in (
        "不确定", "未确认", "结果未知", "待核查", "超时", "连接断开", "唯一识别"
    )):
        return "needs_review", reason
    if item.get("cartAttempted") is True:
        return "needs_review", reason or "已尝试操作但百应未确认加车结果，请核对选品车。"
    if item.get("cartAttempted") is True or item.get("analysisRejected") is True:
        return "selection_failed", reason
    return "pending", reason


def sync_product_list_rows(
    connection: sqlite3.Connection, batch_id: str, account_id: int, state: Dict[str, Any]
) -> None:
    """把选品检查点中的实际入选商品幂等同步到商品列表，不覆盖较新的后续状态。"""
    if str(state.get("stage") or "") not in {"cart_pending", "quota_cart", "video_pending"}:
        return
    candidates = state.get("candidates")
    indexes = state.get("selectedIndexes")
    completed = state.get("completed")
    if not isinstance(candidates, list) or not isinstance(indexes, list):
        return
    account_row = connection.execute(
        "SELECT account_name FROM selection_run_accounts WHERE batch_id = ? AND account_id = ?",
        (batch_id, account_id),
    ).fetchone()
    if not account_row:
        return
    by_index: Dict[int, Dict[str, Any]] = {}
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        try:
            by_index[int(candidate.get("cardIndex"))] = candidate
        except (TypeError, ValueError):
            continue
    completed_by_id = {
        str(item.get("productId") or "").strip(): item
        for item in completed if isinstance(item, dict) and str(item.get("productId") or "").strip()
    } if isinstance(completed, list) else {}
    timestamp = now_text()
    for raw_index in indexes:
        try:
            candidate = by_index.get(int(raw_index))
        except (TypeError, ValueError):
            continue
        if not candidate:
            continue
        product_id = str(candidate.get("productId") or "").strip()
        if not re.fullmatch(r"\d{8,20}", product_id):
            continue
        item = completed_by_id.get(product_id)
        status, reason = _product_cart_state(item)
        category_labels = _candidate_category_labels(candidate)
        cover_url = str(candidate.get("coverUrl") or candidate.get("cover_url") or candidate.get("imageUrl") or "")
        source_url = str(candidate.get("productUrl") or candidate.get("product_url") or candidate.get("sourceUrl") or "")
        connection.execute(
            "INSERT INTO product_list_rows(account_id, account_name, product_id, title, commodity_id, source_url, "
            "cover_url, category_labels_json, candidate_json, status, failure_reason, batch_id, selected_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(account_id, product_id) DO UPDATE SET "
            "account_name = excluded.account_name, title = CASE WHEN product_list_rows.title = '' THEN excluded.title ELSE product_list_rows.title END, "
            "commodity_id = CASE WHEN excluded.commodity_id <> '' THEN excluded.commodity_id ELSE product_list_rows.commodity_id END, "
            "source_url = CASE WHEN excluded.source_url <> '' THEN excluded.source_url ELSE product_list_rows.source_url END, "
            "cover_url = CASE WHEN excluded.cover_url <> '' THEN excluded.cover_url ELSE product_list_rows.cover_url END, "
            "category_labels_json = CASE WHEN product_list_rows.category_labels_json = '[]' THEN excluded.category_labels_json ELSE product_list_rows.category_labels_json END, "
            "candidate_json = CASE WHEN excluded.status = 'pending' AND product_list_rows.status IN ('pending', 'selection_failed') THEN excluded.candidate_json ELSE product_list_rows.candidate_json END, "
            "status = CASE WHEN product_list_rows.deleted_at IS NOT NULL THEN product_list_rows.status "
            "WHEN ? >= CASE product_list_rows.status "
            "WHEN 'uploaded' THEN 6 WHEN 'published' THEN 5 WHEN 'publish_failed' THEN 4 "
            "WHEN 'cart_added' THEN 3 WHEN 'needs_review' THEN 2 WHEN 'selection_failed' THEN 1 ELSE 0 END "
            "THEN excluded.status ELSE product_list_rows.status END, "
            "failure_reason = CASE WHEN excluded.status IN ('selection_failed', 'needs_review') THEN excluded.failure_reason ELSE product_list_rows.failure_reason END, "
            "batch_id = excluded.batch_id, updated_at = excluded.updated_at",
            (
                account_id, str(account_row["account_name"]), product_id,
                str(candidate.get("title") or "")[:500], str(candidate.get("commodityId") or ""), source_url[:2000],
                cover_url[:2000], json.dumps(category_labels, ensure_ascii=False), json.dumps(candidate, ensure_ascii=False), status, reason,
                batch_id, timestamp, timestamp, PRODUCT_STATUS_ORDER[status],
            ),
        )


def list_product_rows(
    connection: sqlite3.Connection, *, account_id: Optional[int] = None,
    status: str = "", query: str = "", limit: int = 20, offset: int = 0,
) -> Dict[str, Any]:
    backfill_product_list_rows(connection)
    allowed = set(PRODUCT_STATUS_ORDER)
    if status and status not in allowed:
        raise ValueError("商品状态筛选值无效")
    safe_limit = min(100, max(1, int(limit)))
    safe_offset = max(0, int(offset))
    clauses = ["deleted_at IS NULL"]
    params: list[Any] = []
    if account_id is not None:
        if account_id < 1:
            raise ValueError("账号 ID 无效")
        clauses.append("account_id = ?")
        params.append(account_id)
    normalized_query = str(query or "").strip()[:200]
    if normalized_query:
        clauses.append("(product_id LIKE ? OR title LIKE ? OR account_name LIKE ?)")
        params.extend([f"%{normalized_query}%"] * 3)
    rows = connection.execute(
        "SELECT * FROM product_list_rows WHERE " + " AND ".join(clauses) + " ORDER BY selected_at DESC, account_id, product_id",
        params,
    ).fetchall()
    table_names = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    upload_status: Dict[tuple[int, str], tuple[str, str]] = {}
    publish_status: Dict[tuple[int, str], tuple[str, str, str]] = {}
    product_covers: Dict[str, str] = {}
    product_details: Dict[str, tuple[str, str, str]] = {}
    material_jobs: Dict[tuple[int, str], Dict[tuple[int, str], sqlite3.Row]] = {}
    if "qianchuan_product_upload_status" in table_names:
        upload_status = {
            (int(row["account_id"]), str(row["product_id"])): (str(row["status"]), str(row["updated_at"] or ""))
            for row in connection.execute("SELECT account_id, product_id, status, updated_at FROM qianchuan_product_upload_status").fetchall()
        }
    if "publish_queue" in table_names:
        for row in connection.execute(
            "SELECT account_id, product_id, platform_status, platform_error, published_at, updated_at "
            "FROM publish_queue WHERE status <> 'cancelled' ORDER BY updated_at DESC, id DESC"
        ).fetchall():
            key = (int(row["account_id"]), str(row["product_id"]))
            publish_status.setdefault(key, (
                str(row["platform_status"]), str(row["platform_error"] or ""),
                str(row["published_at"] or row["updated_at"] or ""),
            ))
    if "baiying_products" in table_names:
        for detail in connection.execute("SELECT product_id, title, commodity_id, cover_url FROM baiying_products"):
            product_details[str(detail["product_id"])] = (
                str(detail["title"] or ""), str(detail["commodity_id"] or ""), str(detail["cover_url"] or ""))
        product_covers = {key: detail[2] for key, detail in product_details.items()}
    if "qianchuan_upload_jobs" in table_names:
        job_columns = {str(column["name"]) for column in connection.execute("PRAGMA table_info(qianchuan_upload_jobs)")}
        evaluation_column = "material_evaluation" if "material_evaluation" in job_columns else "'' AS material_evaluation"
        checked_column = "evaluation_checked_at" if "evaluation_checked_at" in job_columns else "'' AS evaluation_checked_at"
        for job in connection.execute(
            "SELECT account_id, qianchuan_account_id, product_id, remote_material_id, "
            f"{evaluation_column}, {checked_column}, completed_at, updated_at "
            "FROM qianchuan_upload_jobs WHERE status = 'succeeded' "
            "AND remote_material_id <> '' ORDER BY id DESC"
        ):
            key = (int(job["account_id"]), str(job["product_id"]))
            remote_key = (int(job["qianchuan_account_id"]), str(job["remote_material_id"]))
            material_jobs.setdefault(key, {}).setdefault(remote_key, job)

    def material_summary(key: tuple[int, str]) -> Dict[str, int]:
        jobs = list(material_jobs.get(key, {}).values())
        moved = sum(bool(job["evaluation_checked_at"]) and
                    "搬运" in str(job["material_evaluation"] or "") for job in jobs)
        first = sum(bool(job["evaluation_checked_at"]) and
                    "首发" in str(job["material_evaluation"] or "")
                    and "搬运" not in str(job["material_evaluation"] or "") for job in jobs)
        pending = sum(not job["evaluation_checked_at"] or
                      str(job["material_evaluation"] or "").strip() in {"", "-"} for job in jobs)
        return {"materialCount": len(jobs), "materialFirstCount": first,
                "materialMovedCount": moved, "materialPendingCount": pending,
                "materialOtherCount": len(jobs) - first - moved - pending}

    products = []
    existing_keys: set[tuple[int, str]] = set()
    for row in rows:
        key = (int(row["account_id"]), str(row["product_id"]))
        existing_keys.add(key)
        base_status = str(row["status"])
        uploaded, upload_updated_at = upload_status.get(key, ("", ""))
        effective = "uploaded" if uploaded == "uploaded" or key in material_jobs else base_status
        reason = str(row["failure_reason"] or "")
        uploaded_job_time = max(
            (str(job["updated_at"] or "") for job in material_jobs.get(key, {}).values()), default="")
        updated_at = (upload_updated_at or uploaded_job_time or str(row["updated_at"])) if effective == "uploaded" else str(row["updated_at"])
        platform_status, platform_error, publish_updated_at = publish_status.get(key, ("", "", ""))
        if effective != "uploaded" and platform_status == "published":
            effective = "published"
            reason = ""
            updated_at = publish_updated_at or updated_at
        elif effective not in {"uploaded", "published"} and platform_status == "needs_review":
            effective = "publish_failed"
            reason = platform_error or "发布任务需要核查，请到发布队列查看详情。"
            updated_at = publish_updated_at or updated_at
        elif effective not in {"uploaded", "published", "publish_failed"} and platform_status in {"isolated", "submission_uncertain"}:
            effective = "needs_review"
            reason = platform_error or "发布结果待核查，请到发布队列查看详情。"
            updated_at = publish_updated_at or updated_at
        if status and effective != status:
            continue
        try:
            category_labels = json.loads(str(row["category_labels_json"] or "[]"))
        except json.JSONDecodeError:
            category_labels = []
        products.append({
            "accountId": key[0], "accountName": str(row["account_name"]),
            "productId": key[1], "title": str(row["title"]), "commodityId": str(row["commodity_id"]),
            "sourceUrl": str(row["source_url"]), "coverUrl": str(row["cover_url"] or product_covers.get(key[1], "")),
            "sku": str(row["sku"]), "categoryLabels": category_labels if isinstance(category_labels, list) else [],
            "status": effective, "failureReason": reason, "batchId": str(row["batch_id"]),
            "candidateSnapshot": decode_json(row["candidate_json"], {}),
            "selectedAt": str(row["selected_at"]), "updatedAt": updated_at,
            "recordSource": "selection", **material_summary(key),
        })
    account_rows = connection.execute(
        "SELECT DISTINCT account_id AS id, account_name AS name FROM product_list_rows WHERE deleted_at IS NULL ORDER BY account_name"
    ).fetchall()
    listed_account_ids = {int(row["id"]) for row in account_rows} | {key[0] for key in material_jobs}
    account_names = {int(row["id"]): str(row["name"]) for row in account_rows}
    if "accounts" in table_names:
        for account in connection.execute("SELECT id, name FROM accounts"):
            if int(account["id"]) in listed_account_ids:
                account_names[int(account["id"])] = str(account["name"])
    for key, jobs_by_remote in material_jobs.items():
        if key in existing_keys:
            continue
        name = account_names.get(key[0], f"账号 {key[0]}")
        detail = product_details.get(key[1], ("", "", ""))
        if account_id is not None and key[0] != account_id:
            continue
        if normalized_query and not any(normalized_query in value for value in (key[1], detail[0], name)):
            continue
        if status and status != "uploaded":
            continue
        latest = max(jobs_by_remote.values(), key=lambda job: str(job["updated_at"] or ""))
        timestamp = str(latest["completed_at"] or latest["updated_at"] or "")
        products.append({
            "accountId": key[0], "accountName": name, "productId": key[1],
            "title": detail[0], "commodityId": detail[1], "sourceUrl": "", "coverUrl": detail[2],
            "sku": "", "categoryLabels": [], "status": "uploaded", "failureReason": "",
            "batchId": "", "candidateSnapshot": {}, "selectedAt": timestamp,
            "updatedAt": str(latest["updated_at"] or timestamp), "recordSource": "qianchuan_upload",
            **material_summary(key),
        })
    products.sort(key=lambda item: (item["selectedAt"], item["accountId"], item["productId"]), reverse=True)
    return {
        "ok": True, "rows": products[safe_offset:safe_offset + safe_limit], "total": len(products),
        "limit": safe_limit, "offset": safe_offset,
        "accounts": [{"id": key, "name": value} for key, value in sorted(account_names.items(), key=lambda pair: pair[1])],
    }


def backfill_product_list_rows(connection: sqlite3.Connection) -> None:
    """首次打开列表时，从已有 Step 12 检查点补建条目，不丢失升级前的选品结果。"""
    marker = connection.execute(
        "SELECT meta_value FROM product_list_meta WHERE meta_key = 'selection-checkpoints-v1'"
    ).fetchone()
    if marker:
        return
    tables = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "selection_run_accounts" in tables:
        checkpoints = connection.execute(
            "SELECT batch_id, account_id, stage, candidates_json, selected_indexes_json, completed_json "
            "FROM selection_run_accounts ORDER BY updated_at"
        ).fetchall()
        for row in checkpoints:
            state = {
                "stage": str(row["stage"] or ""),
                "candidates": decode_json(row["candidates_json"], None),
                "selectedIndexes": decode_json(row["selected_indexes_json"], None),
                "completed": decode_json(row["completed_json"], None),
            }
            sync_product_list_rows(connection, str(row["batch_id"]), int(row["account_id"]), state)
    connection.execute(
        "INSERT OR REPLACE INTO product_list_meta(meta_key, meta_value) VALUES ('selection-checkpoints-v1', ?)",
        (now_text(),),
    )
    connection.commit()


def update_product_row(connection: sqlite3.Connection, account_id: int, product_id: str, value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("商品编辑内容无效")
    title = str(value.get("title") or "").strip()
    sku = str(value.get("sku") or "").strip()
    category_labels = value.get("categoryLabels") if isinstance(value.get("categoryLabels"), list) else []
    categories = [str(item).strip()[:80] for item in category_labels if str(item).strip()][:6]
    if not title or len(title) > 500 or len(sku) > 200:
        raise ValueError("商品名称不能为空且不超过 500 字，规格不超过 200 字")
    cursor = connection.execute(
        "UPDATE product_list_rows SET title = ?, sku = ?, category_labels_json = ?, updated_at = ? "
        "WHERE account_id = ? AND product_id = ? AND deleted_at IS NULL",
        (title, sku, json.dumps(categories, ensure_ascii=False), now_text(), account_id, product_id),
    )
    connection.commit()
    if cursor.rowcount != 1:
        raise ValueError("商品不存在或已删除")
    return {"ok": True}


def update_product_cart_outcome(
    connection: sqlite3.Connection, account_id: int, product_id: str, status: str, reason: str = ""
) -> Dict[str, Any]:
    if status not in {"cart_added", "selection_failed", "needs_review"}:
        raise ValueError("不允许写入该选品车状态")
    row = connection.execute(
        "SELECT status FROM product_list_rows WHERE account_id = ? AND product_id = ? AND deleted_at IS NULL",
        (account_id, product_id),
    ).fetchone()
    if not row:
        raise ValueError("商品不存在或已删除")
    current = str(row["status"])
    if current in {"cart_added", "publish_failed", "published", "uploaded"}:
        raise ValueError("商品已有更新的确认状态，拒绝回退")
    timestamp = now_text()
    connection.execute(
        "UPDATE product_list_rows SET status = ?, failure_reason = ?, updated_at = ? "
        "WHERE account_id = ? AND product_id = ? AND deleted_at IS NULL",
        (status, str(reason or "")[:500], timestamp, account_id, product_id),
    )
    if "selection_run_accounts" in {
        str(item[0]) for item in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }:
        checkpoints = connection.execute(
            "SELECT a.batch_id, a.candidates_json, a.completed_json FROM selection_run_accounts a "
            "JOIN selection_run_batches b ON b.batch_id = a.batch_id "
            "WHERE a.account_id = ? AND b.state <> 'abandoned' ORDER BY a.updated_at DESC",
            (account_id,),
        ).fetchall()
        for checkpoint in checkpoints:
            candidates = decode_json(checkpoint["candidates_json"], [])
            completed = decode_json(checkpoint["completed_json"], [])
            candidates = candidates if isinstance(candidates, list) else []
            completed = completed if isinstance(completed, list) else []
            source_candidate = next((
                item for item in candidates
                if isinstance(item, dict) and str(item.get("productId") or "").strip() == product_id
            ), None)
            completed_index = next((index for index, item in enumerate(completed)
                                    if isinstance(item, dict) and str(item.get("productId") or "").strip() == product_id), None)
            if source_candidate is None and completed_index is None:
                continue
            result_item = dict(completed[completed_index]) if completed_index is not None else dict(source_candidate or {})
            result_item.update({
                "productId": product_id,
                "cartAdded": status == "cart_added",
                "cartAttempted": status != "selection_failed",
                "status": "商品列表已确认加入选品车" if status == "cart_added" else (str(reason or "")[:500] or "加车结果待处理"),
                "error": "" if status == "cart_added" else str(reason or "")[:500],
                "skipped": False,
            })
            if completed_index is None:
                completed.append(result_item)
            else:
                completed[completed_index] = result_item
            connection.execute(
                "UPDATE selection_run_accounts SET completed_json = ?, updated_at = ? WHERE batch_id = ? AND account_id = ?",
                (json.dumps(completed, ensure_ascii=False), timestamp, str(checkpoint["batch_id"]), account_id),
            )
            break
    connection.commit()
    return {"ok": True, "status": status}


def delete_product_row(connection: sqlite3.Connection, account_id: int, product_id: str) -> Dict[str, Any]:
    row = connection.execute(
        "SELECT status FROM product_list_rows WHERE account_id = ? AND product_id = ? AND deleted_at IS NULL",
        (account_id, product_id),
    ).fetchone()
    if not row:
        raise ValueError("商品不存在或已删除")
    if str(row["status"]) == "needs_review":
        raise ValueError("商品状态待核查，完成核查前不能删除")
    tables = {str(item[0]) for item in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "publish_queue" in tables and connection.execute(
        "SELECT 1 FROM publish_queue WHERE account_id = ? AND product_id = ? LIMIT 1", (account_id, product_id)
    ).fetchone():
        raise ValueError("商品关联过发布任务，不能从商品列表删除")
    if "qianchuan_product_upload_status" in tables and connection.execute(
        "SELECT 1 FROM qianchuan_product_upload_status WHERE account_id = ? AND product_id = ? LIMIT 1", (account_id, product_id)
    ).fetchone():
        raise ValueError("商品已有千川素材上传记录，不能从商品列表删除")
    if "qianchuan_upload_jobs" in tables and connection.execute(
        "SELECT 1 FROM qianchuan_upload_jobs WHERE account_id = ? AND product_id = ? LIMIT 1", (account_id, product_id)
    ).fetchone():
        raise ValueError("商品关联千川上传任务，不能从商品列表删除")
    connection.execute(
        "UPDATE product_list_rows SET deleted_at = ?, updated_at = ? WHERE account_id = ? AND product_id = ?",
        (now_text(), now_text(), account_id, product_id),
    )
    connection.commit()
    return {"ok": True}


def update_batch(connection: sqlite3.Connection, batch_id: str, state: str) -> Dict[str, Any]:
    if state not in {"in_progress", "needs_retry", "completed", "failed"}:
        raise ValueError("未知批次状态")
    current = connection.execute(
        "SELECT state FROM selection_run_batches WHERE batch_id = ?", (batch_id,)
    ).fetchone()
    if not current:
        raise ValueError("找不到待更新的批次")
    if current["state"] == "abandoned":
        raise ValueError("批次已结束，不能通过状态更新重新启用")
    if current["state"] == "failed" and state != "failed":
        raise ValueError("批次已失败结束，不能通过状态更新重新启用")
    timestamp = now_text()
    cursor = connection.execute(
        "UPDATE selection_run_batches SET state = ?, updated_at = ? WHERE batch_id = ?",
        (state, timestamp, batch_id),
    )
    connection.commit()
    if cursor.rowcount == 0:
        raise ValueError("找不到待更新的批次")
    row = connection.execute("SELECT * FROM selection_run_batches WHERE batch_id = ?", (batch_id,)).fetchone()
    return batch_dict(connection, row)


def set_batch_manual_pause(connection: sqlite3.Connection, batch_id: str, paused: bool) -> Dict[str, Any]:
    """持久化用户手动暂停闸门；运行中的平台命令由 renderer 在安全边界收尾。"""
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT * FROM selection_run_batches WHERE batch_id = ?", (batch_id,)
        ).fetchone()
        if not row or row["state"] not in {"in_progress", "needs_retry"}:
            raise ValueError("只有未完成的选品批次可设置手动暂停")
        payload = decode_json(row["payload_json"], {})
        if not isinstance(payload, dict):
            raise ValueError("选品批次检查点格式无效")
        previous = payload.get("manualPause")
        previous = previous if isinstance(previous, dict) else {}
        timestamp = now_text()
        payload["manualPause"] = {
            "active": paused,
            "requestedAt": (str(previous.get("requestedAt") or timestamp)
                            if paused and previous.get("active") is True else
                            timestamp if paused else str(previous.get("requestedAt") or "")),
            "resumedAt": "" if paused else timestamp,
        }
        connection.execute(
            "UPDATE selection_run_batches SET payload_json=?,updated_at=? WHERE batch_id=?",
            (json.dumps(payload, ensure_ascii=False, separators=(",", ":")), timestamp, batch_id),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    updated = connection.execute("SELECT * FROM selection_run_batches WHERE batch_id=?", (batch_id,)).fetchone()
    return batch_dict(connection, updated)


def abandon_batch(connection: sqlite3.Connection, batch_id: str, reason: str) -> Dict[str, Any]:
    """显式结束待恢复批次；保留检查点和配额事件，不把任务伪装成成功。"""
    normalized_reason = str(reason or "").strip()
    if not normalized_reason or len(normalized_reason) > 500:
        raise ValueError("结束批次必须提供 1–500 个字符的审计原因")

    timestamp = now_text()
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT * FROM selection_run_batches WHERE batch_id = ?", (batch_id,)
        ).fetchone()
        if not row:
            raise ValueError("找不到待结束的批次")
        if row["state"] != "needs_retry":
            raise ValueError("仅允许结束 needs_retry 状态的待恢复批次")
        running = connection.execute(
            "SELECT COUNT(*) FROM selection_run_accounts WHERE batch_id = ? AND state = 'in_progress'",
            (batch_id,),
        ).fetchone()[0]
        if int(running or 0):
            raise ValueError("批次中仍有执行中的账号任务，不能结束")

        payload = decode_json(row["payload_json"], {})
        if not isinstance(payload, dict):
            payload = {"preservedPayload": payload}
        payload["abandonment"] = {
            "at": timestamp,
            "reason": normalized_reason,
            "previousState": row["state"],
            "checkpointPreserved": True,
        }
        connection.execute(
            "UPDATE selection_run_batches SET state = 'abandoned', payload_json = ?, updated_at = ? "
            "WHERE batch_id = ?",
            (json.dumps(payload, ensure_ascii=False), timestamp, batch_id),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise

    updated = connection.execute(
        "SELECT * FROM selection_run_batches WHERE batch_id = ?", (batch_id,)
    ).fetchone()
    return batch_dict(connection, updated)


def refresh_candidate_fetch_batch(
    connection: sqlite3.Connection, batch_id: str, payload: Any
) -> Dict[str, Any]:
    """仅在尚未产生候选/加车副作用时，用当前设置刷新失败候选批次。"""
    if not isinstance(payload, dict):
        raise ValueError("新的选品批次内容无效")
    row = connection.execute(
        "SELECT * FROM selection_run_batches WHERE batch_id = ?", (batch_id,)
    ).fetchone()
    if not row or row["state"] not in {"in_progress", "needs_retry"}:
        raise ValueError("找不到可安全刷新设置的未完成批次")
    previous_payload = decode_json(row["payload_json"], {})
    previous_accounts = previous_payload.get("accounts", []) if isinstance(previous_payload, dict) else []
    previous_pool = previous_payload.get("candidatePool") if isinstance(previous_payload, dict) else None
    old_settings = previous_payload.get("settingsSnapshot") if isinstance(previous_payload, dict) else None
    new_settings = payload.get("settingsSnapshot")
    pool_fields = (
        "sources", "thresholds", "filters", "bannedWords", "selectionMode", "categoryMode",
        "categorySelection", "excludedCategorySelection", "candidatePoolMode", "candidatePoolQuotas",
        "candidatePoolTarget", "candidateCollectorAccountIds", "categoryTree",
    )
    collector_ids = old_settings.get("candidateCollectorAccountIds") if isinstance(old_settings, dict) else None
    same_pool_inputs = (
        isinstance(old_settings, dict) and isinstance(new_settings, dict) and
        all(old_settings.get(field) == new_settings.get(field) for field in pool_fields) and
        isinstance(collector_ids, list) and _same_collector_profiles(payload, previous_payload, collector_ids)
    )
    if isinstance(previous_pool, dict) and (previous_pool.get("rows") or previous_pool.get("revalidationRows")) and not same_pool_inputs:
        raise ValueError("原批次已有候选商品；筛选条件或采集账号资料变化时不能覆盖原池，请结束旧批次后新建批次重核已存 ID")
    requested_accounts = payload.get("accounts", [])
    if not isinstance(previous_accounts, list) or not isinstance(requested_accounts, list):
        raise ValueError("批次账号快照无效，不能刷新")
    if [item.get("id") for item in previous_accounts if isinstance(item, dict)] != [
        item.get("id") for item in requested_accounts if isinstance(item, dict)
    ]:
        raise ValueError("不能在恢复批次时变更账号列表")

    accounts = connection.execute(
        "SELECT * FROM selection_run_accounts WHERE batch_id = ? ORDER BY sequence_no, account_id",
        (batch_id,),
    ).fetchall()
    pending = [account for account in accounts if account["state"] != "completed"]
    if not pending:
        raise ValueError("批次没有待恢复的账号")
    for account in pending:
        if account["stage"] not in {"", "queued", "candidate_fetch", "quota_check"}:
            raise ValueError("任务已越过候选查询阶段，必须沿用原批次快照以保证恢复安全")
        for column in ("candidates_json", "selected_indexes_json", "completed_json"):
            value = decode_json(account[column], None)
            if value:
                raise ValueError("任务已保存候选或执行结果，不能刷新原批次设置")

    refreshed_payload = dict(payload)
    if isinstance(previous_pool, dict) and same_pool_inputs:
        refreshed_payload["candidatePool"] = previous_pool
        if isinstance(previous_payload.get("candidatePoolReuseHints"), list):
            refreshed_payload["candidatePoolReuseHints"] = previous_payload["candidatePoolReuseHints"]
    else:
        refreshed_payload.pop("candidatePool", None)
    # 同一批次保留原账号集合和幂等事件键；新设置只替换尚未执行账号的输入快照。
    refreshed_payload["accounts"] = previous_accounts
    timestamp = now_text()
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE selection_run_batches SET state = 'in_progress', payload_json = ?, updated_at = ? "
            "WHERE batch_id = ?",
            (json.dumps(refreshed_payload, ensure_ascii=False), timestamp, batch_id),
        )
        connection.execute(
            "UPDATE selection_run_accounts SET state = 'queued', stage = 'queued', error = '', "
            "candidates_json = NULL, selected_indexes_json = NULL, completed_json = NULL, "
            "source_pool_state_json = NULL, updated_at = ? "
            "WHERE batch_id = ? AND state != 'completed'",
            (timestamp, batch_id),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    updated = connection.execute(
        "SELECT * FROM selection_run_batches WHERE batch_id = ?", (batch_id,)
    ).fetchone()
    return batch_dict(connection, updated)


def update_batch_download_paths(
    connection: sqlite3.Connection, batch_id: str, download_paths: Any
) -> Dict[str, Any]:
    """更新已有候选批次中未完成账号的实际素材目录，不改选品设置或候选快照。"""
    if not isinstance(download_paths, dict) or not download_paths:
        raise ValueError("新的账号下载目录无效")
    timestamp = now_text()
    try:
        connection.execute("BEGIN IMMEDIATE")
        batch_row = connection.execute(
            "SELECT * FROM selection_run_batches WHERE batch_id = ?", (batch_id,)
        ).fetchone()
        if not batch_row or batch_row["state"] not in {"in_progress", "needs_retry"}:
            raise ValueError("找不到可更新下载目录的未完成批次")
        payload = decode_json(batch_row["payload_json"], {})
        if not isinstance(payload, dict):
            raise ValueError("批次资源快照无效，不能更新下载目录")
        contexts = payload.get("accountContexts")
        if not isinstance(contexts, dict):
            raise ValueError("批次缺少账号资源快照，不能更新下载目录")

        accounts = connection.execute(
            "SELECT * FROM selection_run_accounts WHERE batch_id = ? ORDER BY sequence_no, account_id",
            (batch_id,),
        ).fetchall()
        by_id = {str(int(account["account_id"])): account for account in accounts}
        allowed_stages = {"", "queued", "quota_check", "candidate_fetch", "cart_pending", "quota_cart", "video_pending"}
        updated_ids: set[str] = set()
        for raw_account_id, raw_path in download_paths.items():
            account_id = str(raw_account_id)
            account = by_id.get(account_id)
            if not account:
                raise ValueError(f"账号 {account_id} 不属于此批次")
            if account["state"] == "completed":
                raise ValueError(f"账号 {account_id} 已完成，不能改写其检查点")
            if account["stage"] not in allowed_stages:
                raise ValueError(f"账号 {account_id} 当前阶段不允许更新下载目录")
            new_path = str(raw_path or "").strip()
            if not new_path or "\x00" in new_path or not ntpath.isabs(new_path):
                raise ValueError(f"账号 {account_id} 的下载目录必须是有效绝对路径")
            context = contexts.get(account_id)
            if not isinstance(context, dict):
                raise ValueError(f"账号 {account_id} 缺少资源快照，不能更新下载目录")
            old_path = str(context.get("downloadPath") or "").strip()
            if not old_path:
                raise ValueError(f"账号 {account_id} 缺少原下载目录，不能安全迁移检查点")
            new_path = ntpath.normpath(new_path)
            if ntpath.normcase(ntpath.normpath(old_path)) == ntpath.normcase(new_path):
                continue

            context["downloadPath"] = new_path
            completed = decode_json(account["completed_json"], None)
            if isinstance(completed, list):
                for index, item in enumerate(completed):
                    if not isinstance(item, dict) or item.get("video9x16") or item.get("video1x1"):
                        continue
                    output_dir = str(item.get("outputDir") or "").strip()
                    if not output_dir:
                        continue
                    try:
                        relative = ntpath.relpath(output_dir, old_path)
                    except ValueError:
                        relative = ".."
                    if relative == ".":
                        relative = ntpath.join("图转视频恢复", str(item.get("productId") or index + 1))
                    if relative == ".." or relative.startswith("..\\") or ntpath.isabs(relative):
                        safe_id = re.sub(r"[^0-9A-Za-z_-]+", "_", str(item.get("productId") or index + 1))
                        relative = ntpath.join("图转视频恢复", safe_id)
                    item["outputDir"] = ntpath.normpath(ntpath.join(new_path, relative))
                connection.execute(
                    "UPDATE selection_run_accounts SET completed_json = ?, updated_at = ? "
                    "WHERE batch_id = ? AND account_id = ?",
                    (json.dumps(completed, ensure_ascii=False), timestamp, batch_id, int(account_id)),
                )
            updated_ids.add(account_id)

        if not updated_ids:
            raise ValueError("所选账号的有效下载目录没有变化")
        connection.execute(
            "UPDATE selection_run_batches SET payload_json = ?, updated_at = ? WHERE batch_id = ?",
            (json.dumps(payload, ensure_ascii=False), timestamp, batch_id),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise

    updated = connection.execute(
        "SELECT * FROM selection_run_batches WHERE batch_id = ?", (batch_id,)
    ).fetchone()
    return batch_dict(connection, updated)


def record_quota_once(
    connection: sqlite3.Connection,
    batch_id: str,
    account_id: int,
    product_id: str,
    event_type: str,
    videos: int,
) -> Dict[str, Any]:
    normalized_product = product_id.strip()
    if not normalized_product:
        raise ValueError("商品 ID 不能为空")
    if event_type not in {"cart", "video"}:
        raise ValueError("未知配额事件")
    if videos < 0:
        raise ValueError("视频数量不能小于 0")
    if event_type == "cart" and videos != 0:
        raise ValueError("加车事件不能记视频数量")
    if event_type == "video" and videos < 1:
        raise ValueError("视频生成事件必须至少记 1 条")

    day = datetime.now().date().isoformat()
    event_key = f"{batch_id}:{account_id}:{normalized_product}:{event_type}"
    timestamp = now_text()
    try:
        connection.execute("BEGIN IMMEDIATE")
        batch = connection.execute(
            "SELECT state FROM selection_run_batches WHERE batch_id = ?", (batch_id,)
        ).fetchone()
        if not batch:
            raise ValueError("找不到配额事件所属批次")
        if batch["state"] == "abandoned":
            raise ValueError("批次已结束，不能新增配额事件")
        cursor = connection.execute(
            "INSERT OR IGNORE INTO selection_quota_events "
            "(event_key, batch_id, usage_date, account_id, product_id, event_type, video_count, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (event_key, batch_id, day, account_id, normalized_product, event_type, videos, timestamp),
        )
        applied = cursor.rowcount == 1
        if applied:
            connection.execute(
                "INSERT INTO selection_daily_usage(usage_date, account_id, product_id, video_count, updated_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(usage_date, account_id, product_id) DO UPDATE SET "
                "video_count = selection_daily_usage.video_count + excluded.video_count, updated_at = excluded.updated_at",
                (day, account_id, normalized_product, videos, timestamp),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise

    usage = connection.execute(
        "SELECT COUNT(*), COALESCE(SUM(video_count), 0) FROM selection_daily_usage WHERE usage_date = ? AND account_id = ?",
        (day, account_id),
    ).fetchone()
    return {"ok": True, "applied": applied, "eventKey": event_key,
            "usage": {"products": int(usage[0] or 0), "videos": int(usage[1] or 0)}}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="选品批次恢复和幂等配额事件")
    parser.add_argument("--db", type=Path, default=Path(DEFAULT_DB_PATH))
    commands = parser.add_subparsers(dest="command", required=True)

    batch = commands.add_parser("batch")
    batch_sub = batch.add_subparsers(dest="action", required=True)
    batch_sub.add_parser("latest")
    batch_sub.add_parser("recent")
    saved_pools = batch_sub.add_parser("pool-list")
    saved_pools.add_argument("--limit", type=int, default=10)
    saved_pool_items = batch_sub.add_parser("pool-items")
    saved_pool_items.add_argument("--batch-id", required=True)
    saved_pool_items.add_argument("--query", default="")
    saved_pool_items.add_argument("--limit", type=int, default=20)
    saved_pool_items.add_argument("--offset", type=int, default=0)
    saved_pool_settings = batch_sub.add_parser("pool-settings")
    saved_pool_settings.add_argument("--batch-id", required=True)
    batch_list = batch_sub.add_parser("list", help="只读分页查询历史选品批次")
    batch_list.add_argument("--limit", type=int, default=25)
    batch_list.add_argument("--offset", type=int, default=0)
    batch_list.add_argument("--account-id", type=int)
    batch_list.add_argument(
        "--state", choices=["in_progress", "needs_retry", "completed", "failed", "abandoned"], default=""
    )
    batch_list.add_argument("--start-date", default="")
    batch_list.add_argument("--end-date", default="")
    batch_logs = batch_sub.add_parser("logs", help="只读查看指定批次日志详情")
    batch_logs.add_argument("--batch-id", required=True)
    create = batch_sub.add_parser("create")
    create.add_argument("--batch-id", required=True)
    refresh = batch_sub.add_parser("refresh-candidates")
    refresh.add_argument("--batch-id", required=True)
    save_pool = batch_sub.add_parser("pool-save")
    save_pool.add_argument("--batch-id", required=True)
    reuse_pool = batch_sub.add_parser("pool-reuse")
    reuse_pool.add_argument("--batch-id", required=True)
    update_paths = batch_sub.add_parser("update-download-paths")
    update_paths.add_argument("--batch-id", required=True)
    abandon = batch_sub.add_parser("abandon")
    abandon.add_argument("--batch-id", required=True)
    abandon.add_argument("--reason", required=True)
    update = batch_sub.add_parser("update")
    update.add_argument("--batch-id", required=True)
    update.add_argument("--state", required=True)
    manual_pause = batch_sub.add_parser("manual-pause")
    manual_pause.add_argument("--batch-id", required=True)
    manual_pause.add_argument("--state", choices=["request", "clear"], required=True)

    account = commands.add_parser("account")
    account_sub = account.add_subparsers(dest="action", required=True)
    get = account_sub.add_parser("get")
    get.add_argument("--batch-id", required=True)
    get.add_argument("--account-id", type=int, required=True)
    save = account_sub.add_parser("update")
    save.add_argument("--batch-id", required=True)
    save.add_argument("--account-id", type=int, required=True)

    quota = commands.add_parser("quota-record-once")
    quota.add_argument("--batch-id", required=True)
    quota.add_argument("--account-id", type=int, required=True)
    quota.add_argument("--product-id", required=True)
    quota.add_argument("--event", choices=["cart", "video"], required=True)
    quota.add_argument("--videos", type=int, default=0)

    log = commands.add_parser("log")
    log_sub = log.add_subparsers(dest="action", required=True)
    append_log = log_sub.add_parser("append")
    append_log.add_argument("--batch-id", required=True)
    append_log.add_argument("--account-id", type=int, required=True)
    append_logs = log_sub.add_parser("append-batch")
    append_logs.add_argument("--batch-id", required=True)
    append_logs.add_argument("--account-id", type=int, required=True)

    product = commands.add_parser("product")
    product_sub = product.add_subparsers(dest="action", required=True)
    product_list = product_sub.add_parser("list")
    product_list.add_argument("--account-id", type=int)
    product_list.add_argument("--status", default="")
    product_list.add_argument("--query", default="")
    product_list.add_argument("--limit", type=int, default=20)
    product_list.add_argument("--offset", type=int, default=0)
    product_update = product_sub.add_parser("update")
    product_update.add_argument("--account-id", type=int, required=True)
    product_update.add_argument("--product-id", required=True)
    product_delete = product_sub.add_parser("delete")
    product_delete.add_argument("--account-id", type=int, required=True)
    product_delete.add_argument("--product-id", required=True)
    product_cart = product_sub.add_parser("cart-outcome")
    product_cart.add_argument("--account-id", type=int, required=True)
    product_cart.add_argument("--product-id", required=True)
    product_cart.add_argument("--status", choices=["cart_added", "selection_failed", "needs_review"], required=True)
    product_cart.add_argument("--reason", default="")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        # 所有批次读取先走 mode=ro；不能调用会建表、切换 WAL 的写连接。
        if args.command == "batch" and args.action in {"latest", "recent", "list", "logs", "pool-list", "pool-items", "pool-settings"}:
            if args.action == "pool-list" and not args.db.expanduser().is_file():
                return emit({"pools": []})
            if args.action in {"latest", "recent"} and not args.db.expanduser().is_file():
                return emit({"batch": None})
            connection = connect_readonly(args.db)
            try:
                if args.action in {"latest", "recent"}:
                    return emit({"batch": read_batch_checkpoint(connection, args.action)})
                if args.action == "list":
                    return emit(list_batches(
                        connection,
                        limit=args.limit,
                        offset=args.offset,
                        account_id=args.account_id,
                        state=args.state,
                        start_date=args.start_date,
                        end_date=args.end_date,
                    ))
                if args.action == "pool-list":
                    return emit({"pools": list_saved_candidate_pools(connection, max(1, min(args.limit, 20)))})
                if args.action == "pool-items":
                    return emit(list_saved_candidate_pool_items(
                        connection, args.batch_id, args.query, args.limit, args.offset))
                if args.action == "pool-settings":
                    return emit(saved_candidate_pool_settings(connection, args.batch_id))
                return emit(batch_log_details(connection, args.batch_id))
            finally:
                connection.close()

        connection = connect(args.db)
        try:
            if args.command == "batch" and args.action == "create":
                payload = json.load(sys.stdin)
                return emit({"batch": create_batch(connection, args.batch_id, payload)})
            if args.command == "batch" and args.action == "refresh-candidates":
                payload = json.load(sys.stdin)
                return emit({"batch": refresh_candidate_fetch_batch(connection, args.batch_id, payload)})
            if args.command == "batch" and args.action == "pool-save":
                payload = json.load(sys.stdin)
                return emit({"pool": save_candidate_pool(connection, args.batch_id, payload)})
            if args.command == "batch" and args.action == "pool-reuse":
                payload = json.load(sys.stdin)
                return emit({"pool": reuse_candidate_pool(
                    connection, args.batch_id,
                    payload.get("filterFingerprint") if isinstance(payload, dict) else None,
                    str(payload.get("sourceBatchId") or "") if isinstance(payload, dict) else "",
                )})
            if args.command == "batch" and args.action == "update-download-paths":
                payload = json.load(sys.stdin)
                paths = payload.get("downloadPaths") if isinstance(payload, dict) else None
                return emit({"batch": update_batch_download_paths(connection, args.batch_id, paths)})
            if args.command == "batch" and args.action == "abandon":
                return emit({"batch": abandon_batch(connection, args.batch_id, args.reason)})
            if args.command == "batch" and args.action == "update":
                return emit({"batch": update_batch(connection, args.batch_id, args.state)})
            if args.command == "batch" and args.action == "manual-pause":
                return emit({"batch": set_batch_manual_pause(
                    connection, args.batch_id, args.state == "request")})
            if args.command == "account" and args.action == "get":
                return emit({"account": get_account(connection, args.batch_id, args.account_id)})
            if args.command == "account" and args.action == "update":
                value = json.load(sys.stdin)
                return emit({"account": update_account(connection, args.batch_id, args.account_id, value)})
            if args.command == "log" and args.action == "append":
                value = json.load(sys.stdin)
                return emit({"log": append_run_log(connection, args.batch_id, args.account_id, value)})
            if args.command == "log" and args.action == "append-batch":
                values = json.load(sys.stdin)
                return emit({"count": append_run_logs(connection, args.batch_id, args.account_id, values)})
            if args.command == "quota-record-once":
                return emit(record_quota_once(connection, args.batch_id, args.account_id,
                                              args.product_id, args.event, args.videos))
            if args.command == "product" and args.action == "list":
                return emit(list_product_rows(
                    connection, account_id=args.account_id, status=args.status, query=args.query,
                    limit=args.limit, offset=args.offset,
                ))
            if args.command == "product" and args.action == "update":
                value = json.load(sys.stdin)
                return emit(update_product_row(connection, args.account_id, args.product_id, value))
            if args.command == "product" and args.action == "delete":
                return emit(delete_product_row(connection, args.account_id, args.product_id))
            if args.command == "product" and args.action == "cart-outcome":
                return emit(update_product_cart_outcome(connection, args.account_id, args.product_id, args.status, args.reason))
            raise ValueError("未知恢复命令")
        finally:
            connection.close()
    except Exception as error:  # CLI 错误转成可读 JSON，由 desktop 引擎统一显示。
        return emit({"error": str(error)}, 1)


if __name__ == "__main__":
    raise SystemExit(main())
