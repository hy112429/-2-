#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""选品批次恢复、过程日志与幂等配额记账。"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

try:
    from .client_paths import DEFAULT_DB_PATH
except ImportError:
    from client_paths import DEFAULT_DB_PATH



def now_text() -> str:
    return datetime.now().isoformat(timespec="seconds")


def emit(payload: Dict[str, Any], code: int = 0) -> int:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
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
        "completed_json TEXT, updated_at TEXT NOT NULL, "
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
    )
    connection.commit()
    return connection


def connect_readonly(db_path: Path) -> sqlite3.Connection:
    """历史日志查询专用连接；不建表、不迁移、不写入数据库。"""
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


def list_run_logs(connection: sqlite3.Connection, batch_id: str) -> list[Dict[str, Any]]:
    rows = connection.execute(
        "SELECT log_id, batch_id, account_id, account_name, product_id, product_title, "
        "stage, code, message, detail, created_at FROM selection_run_logs "
        "WHERE batch_id = ? ORDER BY log_id",
        (batch_id,),
    ).fetchall()
    return [
        {
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
        for row in rows
    ]


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
    return next(
        (item for item in list_run_logs(connection, batch_id) if item["id"] == int(row["log_id"])),
        {},
    ) if row else {}


# 保留旧函数名，兼容已有脚本和测试调用。
append_failure_log = append_run_log


def batch_dict(connection: sqlite3.Connection, row: sqlite3.Row) -> Dict[str, Any]:
    accounts = connection.execute(
        "SELECT * FROM selection_run_accounts WHERE batch_id = ? ORDER BY sequence_no, account_id",
        (row["batch_id"],),
    ).fetchall()
    run_logs = list_run_logs(connection, row["batch_id"])
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
    }
    merged = {key: current.get(key) for key in allowed}
    merged.update({key: value[key] for key in allowed if key in value})
    timestamp = now_text()
    connection.execute(
        "UPDATE selection_run_accounts SET state = ?, stage = ?, error = ?, candidates_json = ?, "
        "selected_indexes_json = ?, completed_json = ?, updated_at = ? "
        "WHERE batch_id = ? AND account_id = ?",
        (
            str(merged["state"]), str(merged["stage"] or ""), str(merged["error"] or ""),
            json.dumps(merged["candidates"], ensure_ascii=False) if merged["candidates"] is not None else None,
            json.dumps(merged["selectedIndexes"], ensure_ascii=False) if merged["selectedIndexes"] is not None else None,
            json.dumps(merged["completed"], ensure_ascii=False) if merged["completed"] is not None else None,
            timestamp, batch_id, account_id,
        ),
    )
    connection.execute(
        "UPDATE selection_run_batches SET updated_at = ? WHERE batch_id = ?", (timestamp, batch_id)
    )
    connection.commit()
    return get_account(connection, batch_id, account_id)


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
            "candidates_json = NULL, selected_indexes_json = NULL, completed_json = NULL, updated_at = ? "
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
    abandon = batch_sub.add_parser("abandon")
    abandon.add_argument("--batch-id", required=True)
    abandon.add_argument("--reason", required=True)
    update = batch_sub.add_parser("update")
    update.add_argument("--batch-id", required=True)
    update.add_argument("--state", required=True)

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
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        # 历史日志查询必须先走 mode=ro；不能调用会建表、切换 WAL 的写连接。
        if args.command == "batch" and args.action in {"list", "logs"}:
            connection = connect_readonly(args.db)
            try:
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
                return emit(batch_log_details(connection, args.batch_id))
            finally:
                connection.close()

        connection = connect(args.db)
        try:
            if args.command == "batch" and args.action == "latest":
                return emit({"batch": latest_incomplete_batch(connection)})
            if args.command == "batch" and args.action == "recent":
                return emit({"batch": recent_batch(connection)})
            if args.command == "batch" and args.action == "create":
                payload = json.load(sys.stdin)
                return emit({"batch": create_batch(connection, args.batch_id, payload)})
            if args.command == "batch" and args.action == "refresh-candidates":
                payload = json.load(sys.stdin)
                return emit({"batch": refresh_candidate_fetch_batch(connection, args.batch_id, payload)})
            if args.command == "batch" and args.action == "abandon":
                return emit({"batch": abandon_batch(connection, args.batch_id, args.reason)})
            if args.command == "batch" and args.action == "update":
                return emit({"batch": update_batch(connection, args.batch_id, args.state)})
            if args.command == "account" and args.action == "get":
                return emit({"account": get_account(connection, args.batch_id, args.account_id)})
            if args.command == "account" and args.action == "update":
                value = json.load(sys.stdin)
                return emit({"account": update_account(connection, args.batch_id, args.account_id, value)})
            if args.command == "log" and args.action == "append":
                value = json.load(sys.stdin)
                return emit({"log": append_run_log(connection, args.batch_id, args.account_id, value)})
            if args.command == "quota-record-once":
                return emit(record_quota_once(connection, args.batch_id, args.account_id,
                                              args.product_id, args.event, args.videos))
            raise ValueError("未知恢复命令")
        finally:
            connection.close()
    except Exception as error:  # CLI 错误转成可读 JSON，由 desktop 引擎统一显示。
        return emit({"error": str(error)}, 1)


if __name__ == "__main__":
    raise SystemExit(main())
