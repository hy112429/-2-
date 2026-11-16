#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地发布队列、创作者中心上传与自动发布。"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence
from urllib.parse import parse_qs, unquote, urlparse

from db import DEFAULT_DB_PATH, connect, emit, now_text

ACTIVE_STATUSES = ("awaiting_confirmation", "scheduled")
DEFAULT_QUOTA = {
    "perProductVideos": 10,
    "perAccountProducts": 5,
    "perAccountVideos": 50,
}

PUBLISH_QUEUE_SCHEMA = """
CREATE TABLE IF NOT EXISTS publish_queue (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id  INTEGER NOT NULL REFERENCES accounts(id),
    product_id  TEXT NOT NULL,
    product_name TEXT NOT NULL DEFAULT '',
    product_url TEXT NOT NULL DEFAULT '',
    video_path  TEXT NOT NULL,
    video_key   TEXT NOT NULL,
    title       TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    schedule_at TEXT NOT NULL,
    mode        TEXT NOT NULL DEFAULT 'manual',
    status      TEXT NOT NULL DEFAULT 'awaiting_confirmation',
    auto_publish INTEGER NOT NULL DEFAULT 0,
    platform_status TEXT NOT NULL DEFAULT 'not_uploaded',
    platform_error TEXT NOT NULL DEFAULT '',
    screenshot_path TEXT NOT NULL DEFAULT '',
    execution_started_at TEXT,
    isolated_from_status TEXT NOT NULL DEFAULT '',
    platform_updated_at TEXT,
    published_at TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    cancelled_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_publish_queue_schedule
    ON publish_queue(schedule_at, status, account_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_publish_queue_active_file
    ON publish_queue(account_id, video_key) WHERE status <> 'cancelled';
"""

PUBLISH_SETTINGS_SCHEMA = """
CREATE TABLE IF NOT EXISTS publish_settings (
    setting_id INTEGER PRIMARY KEY CHECK(setting_id = 1),
    auto_publish_enabled INTEGER NOT NULL DEFAULT 1,
    auto_publish_default_applied INTEGER NOT NULL DEFAULT 0,
    auto_publish_ui_default_applied INTEGER NOT NULL DEFAULT 0,
    auto_publish_activated_at TEXT NOT NULL DEFAULT '',
    auto_publish_queue_start_id INTEGER NOT NULL DEFAULT 0,
    publish_jitter_min_seconds INTEGER NOT NULL DEFAULT 5,
    publish_jitter_max_seconds INTEGER NOT NULL DEFAULT 15,
    publish_catchup_max_late_hours INTEGER NOT NULL DEFAULT 0,
    manual_review_enabled INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
"""

PUBLISH_EVENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS publish_queue_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES publish_queue(id),
    stage TEXT NOT NULL,
    outcome TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    screenshot_path TEXT NOT NULL DEFAULT '',
    cleared_at TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_publish_events_task ON publish_queue_events(task_id, id);
"""


def ensure_schema(connection: sqlite3.Connection, *, initialize_auto_publish_default: bool = False) -> None:
    """只增量新增/扩展发布队列表，不改写既有业务表或队列记录。"""
    connection.executescript(PUBLISH_QUEUE_SCHEMA)
    connection.executescript(PUBLISH_SETTINGS_SCHEMA)
    connection.executescript(PUBLISH_EVENTS_SCHEMA)
    event_columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(publish_queue_events)")}
    if "cleared_at" not in event_columns:
        connection.execute("ALTER TABLE publish_queue_events ADD COLUMN cleared_at TEXT")
    settings_columns = {
        str(row["name"])
        for row in connection.execute("PRAGMA table_info(publish_settings)").fetchall()
    }
    settings_migrations = {
        "auto_publish_default_applied": "INTEGER NOT NULL DEFAULT 0",
        "auto_publish_activated_at": "TEXT NOT NULL DEFAULT ''",
        "auto_publish_queue_start_id": "INTEGER NOT NULL DEFAULT 0",
        "publish_jitter_min_seconds": "INTEGER NOT NULL DEFAULT 5",
        "publish_jitter_max_seconds": "INTEGER NOT NULL DEFAULT 15",
        "publish_catchup_max_late_hours": "INTEGER NOT NULL DEFAULT 0",
        "manual_review_enabled": "INTEGER NOT NULL DEFAULT 0",
    }
    if initialize_auto_publish_default:
        settings_migrations["auto_publish_ui_default_applied"] = "INTEGER NOT NULL DEFAULT 0"
    for name, ddl in settings_migrations.items():
        if name not in settings_columns:
            try:
                connection.execute(f"ALTER TABLE publish_settings ADD COLUMN {name} {ddl}")
            except sqlite3.OperationalError as error:
                if "duplicate column name" not in str(error).lower():
                    raise

    connection.execute(
        "INSERT OR IGNORE INTO publish_settings(setting_id, auto_publish_enabled, auto_publish_default_applied, auto_publish_activated_at, updated_at) "
        "VALUES (1, 1, 0, '', ?)",
        (now_text(),),
    )

    # A-66：首次部署此版本时启用本机默认值一次；以后用户手动关闭必须保留。
    # 记录激活时的最大队列 ID，避免首次开启时追发此前已保存的任务。
    connection.execute(
        "UPDATE publish_settings SET auto_publish_enabled = 1, auto_publish_default_applied = 1, "
        "auto_publish_activated_at = ?, auto_publish_queue_start_id = "
        "(SELECT COALESCE(MAX(id), 0) FROM publish_queue), updated_at = ? "
        "WHERE setting_id = 1 AND auto_publish_default_applied = 0",
        (datetime.now().isoformat(timespec="microseconds"), now_text()),
    )
    if initialize_auto_publish_default:
        # A-68：移除页面开关后，一次性把既有本机配置切换到全自动默认。
        # 只由新客户端启动初始化命令触发；保留 A-66 队列 ID 边界，旧队列不会追发。
        connection.execute(
            "UPDATE publish_settings SET auto_publish_enabled = 1, auto_publish_ui_default_applied = 1, updated_at = ? "
            "WHERE setting_id = 1 AND auto_publish_ui_default_applied = 0",
            (now_text(),),
        )
    columns = {
        str(row["name"])
        for row in connection.execute("PRAGMA table_info(publish_queue)").fetchall()
    }
    migrations = {
        "product_name": "TEXT NOT NULL DEFAULT ''",
        "product_url": "TEXT NOT NULL DEFAULT ''",
        "description": "TEXT NOT NULL DEFAULT ''",
        "platform_status": "TEXT NOT NULL DEFAULT 'not_uploaded'",
        "platform_error": "TEXT NOT NULL DEFAULT ''",
        "screenshot_path": "TEXT NOT NULL DEFAULT ''",
        "execution_started_at": "TEXT",
        "isolated_from_status": "TEXT NOT NULL DEFAULT ''",
        "platform_updated_at": "TEXT",
        "published_at": "TEXT",
        "auto_publish": "INTEGER NOT NULL DEFAULT 0",
    }
    for name, ddl in migrations.items():
        if name not in columns:
            try:
                connection.execute(f"ALTER TABLE publish_queue ADD COLUMN {name} {ddl}")
            except sqlite3.OperationalError as error:
                # 两个客户端命令可能并发完成相同的增量迁移；只吞掉已被另一进程添加的列。
                if "duplicate column name" not in str(error).lower():
                    raise
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_publish_queue_account_product_lock "
        "ON publish_queue(account_id, product_id, status, platform_status)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_publish_queue_product_lock "
        "ON publish_queue(product_id, status, platform_status, account_id)"
    )


def selected_product_options(connection: sqlite3.Connection) -> List[Dict[str, Any]]:
    """从所有账号的选品检查点读取已确认加车的商品名称。"""
    options: Dict[tuple[int, str], Dict[str, Any]] = {}
    try:
        rows = connection.execute(
            "SELECT account_id, completed_json, updated_at FROM selection_run_accounts "
            "ORDER BY updated_at DESC"
        ).fetchall()
    except sqlite3.Error:
        rows = []
    for row in rows:
        try:
            completed = json.loads(row["completed_json"] or "[]")
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(completed, list):
            continue
        account_id = int(row["account_id"])
        for item in completed:
            if not isinstance(item, dict) or item.get("cartAdded") is not True:
                continue
            product_id = str(item.get("productId") or "").strip()
            product_name = str(item.get("title") or "").strip()
            if not product_id or not product_name:
                continue
            product_url = normalize_product_url(
                product_id,
                str(item.get("productUrl") or item.get("product_url") or "").strip(),
            )
            options.setdefault(
                (account_id, product_id),
                {
                    "account_id": account_id,
                    "product_id": product_id,
                    "product_name": product_name,
                    "product_url": product_url,
                    "description": product_name,
                    "selected_at": str(row["updated_at"] or ""),
                },
            )
    return sorted(
        options.values(),
        key=lambda item: (item["account_id"], item["product_name"].casefold(), item["product_id"]),
    )


def product_publish_locks(
    connection: sqlite3.Connection,
    account_id: int,
    product_ids: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """返回该账号已有的未取消商品发布任务，供选品阶段提前跳过。"""
    require_active_account(connection, account_id)
    if product_ids is None:
        rows = connection.execute(
            "SELECT product_id, product_name, id, status, platform_status, updated_at "
            "FROM publish_queue WHERE account_id = ? "
            "AND (status <> 'cancelled' OR platform_status NOT IN ('not_uploaded', 'isolated')) "
            "ORDER BY updated_at DESC, id DESC",
            (account_id,),
        ).fetchall()
    else:
        candidate_ids = sorted({str(value).strip() for value in product_ids if str(value).strip()})
        if not candidate_ids:
            return {"ok": True, "accountId": account_id, "locks": []}
        matched_rows = []
        for offset in range(0, len(candidate_ids), 500):
            chunk = candidate_ids[offset:offset + 500]
            placeholders = ",".join("?" for _ in chunk)
            matched_rows.extend(connection.execute(
                "SELECT product_id, product_name, id, status, platform_status, updated_at "
                "FROM publish_queue WHERE account_id = ? "
                f"AND product_id IN ({placeholders}) "
                "AND (status <> 'cancelled' OR platform_status NOT IN ('not_uploaded', 'isolated')) "
                "ORDER BY updated_at DESC, id DESC",
                [account_id, *chunk],
            ).fetchall())
        rows = sorted(matched_rows, key=lambda row: (str(row["updated_at"] or ""), int(row["id"])), reverse=True)
    locks: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        product_id = str(row["product_id"] or "").strip()
        if not product_id:
            continue
        lock = locks.setdefault(product_id, {
            "productId": product_id,
            "productName": str(row["product_name"] or ""),
            "taskIds": [],
            "queueStatuses": [],
            "platformStatuses": [],
            "updatedAt": str(row["updated_at"] or ""),
        })
        lock["taskIds"].append(int(row["id"]))
        lock["queueStatuses"].append(str(row["status"] or ""))
        lock["platformStatuses"].append(str(row["platform_status"] or ""))
    return {"ok": True, "accountId": account_id, "locks": list(locks.values())}


def ensure_product_not_locked(
    connection: sqlite3.Connection,
    account_id: int,
    product_id: str,
    *,
    exclude_task_id: Optional[int] = None,
) -> None:
    sql = (
        "SELECT id, status, platform_status FROM publish_queue "
        "WHERE account_id = ? AND product_id = ? "
        "AND (status <> 'cancelled' OR platform_status NOT IN ('not_uploaded', 'isolated'))"
    )
    parameters: List[Any] = [account_id, product_id]
    if exclude_task_id is not None:
        sql += " AND id <> ?"
        parameters.append(exclude_task_id)
    sql += " ORDER BY id LIMIT 1"
    row = connection.execute(sql, parameters).fetchone()
    if row:
        raise ValueError(
            f"该商品在此账号已有发布任务 #{row['id']} "
            f"（本地状态：{row['status']}，抖音状态：{row['platform_status']}）；"
            "为防止重复发布，本次已拦截。仅未开始上传的任务取消后才可重新选品"
        )


def ensure_task_product_is_unique(connection: sqlite3.Connection, task_id: int) -> None:
    """历史上若已有同账号同商品的多个活动任务，阻止再启动上传/发布。"""
    row = connection.execute(
        "SELECT id, account_id, product_id FROM publish_queue WHERE id = ?",
        (task_id,),
    ).fetchone()
    if not row:
        raise ValueError("队列任务不存在")
    ensure_product_not_locked(
        connection,
        int(row["account_id"]),
        str(row["product_id"]),
        exclude_task_id=task_id,
    )


def normalize_product_url(product_id: str, value: str = "") -> str:
    """优先使用百应详情返回的链接；旧检查点按官方商品详情 URL 兼容补全。"""
    product_id = str(product_id).strip()
    if not product_id:
        return ""
    if value:
        try:
            parsed = urlparse(value)
            query = parse_qs(parsed.query)
            query_ids = [
                str(candidate)
                for key in ("id", "product_id", "productId")
                for candidate in query.get(key, [])
            ]
            matching_id = (
                bool(query_ids) and all(candidate == product_id for candidate in query_ids)
            ) or (
                not query_ids and any(unquote(segment) == product_id for segment in parsed.path.split("/") if segment)
            )
            if parsed.scheme == "https" and parsed.hostname == "haohuo.jinritemai.com" and matching_id:
                return value
        except ValueError:
            pass
    return f"https://haohuo.jinritemai.com/views/product/item2?id={product_id}"


def normalize_video_path(value: str) -> tuple[str, str]:
    path = Path(value).expanduser().resolve(strict=True)
    if not path.is_file():
        raise ValueError("所选路径不是文件")
    if path.suffix.lower() != ".mp4":
        raise ValueError("发布队列仅接受 MP4 视频文件")
    normalized = os.path.normcase(str(path))
    return str(path), normalized


def list_accounts(connection: sqlite3.Connection) -> List[Dict[str, Any]]:
    """返回全部账号，供队列按账号查看历史记录；新增账号无需改代码。"""
    rows = connection.execute(
        "SELECT id, name, status FROM accounts ORDER BY id"
    ).fetchall()
    return [dict(row) for row in rows]


def require_active_account(connection: sqlite3.Connection, account_id: int) -> sqlite3.Row:
    """写入或开始账号任务前确认账号存在且启用。"""
    if account_id < 1:
        raise ValueError("账号 ID 无效")
    account = connection.execute(
        "SELECT id, name, status FROM accounts WHERE id = ? AND status = 'active'", (account_id,)
    ).fetchone()
    if not account:
        raise ValueError("所选账号不存在或已停用")
    return account


def load_quota(connection: sqlite3.Connection, account_id: int) -> Dict[str, int]:
    """加载通用默认配额，并应用当前百应账号的每日商品/视频上限覆盖。"""
    defaults = dict(DEFAULT_QUOTA)
    try:
        row = connection.execute(
            "SELECT settings_json FROM selection_settings WHERE scope_key = 'global'"
        ).fetchone()
        if row:
            settings = json.loads(row["settings_json"])
            quota = settings.get("quota", {}) if isinstance(settings, dict) else {}
            if isinstance(quota, dict):
                defaults.update(quota)
    except (sqlite3.Error, TypeError, json.JSONDecodeError):
        pass
    try:
        row = connection.execute(
            "SELECT settings_json FROM selection_settings WHERE scope_key = ?",
            (f"account:{account_id}",),
        ).fetchone()
        if row:
            account_settings = json.loads(row["settings_json"])
            override = account_settings.get("dailyQuotaOverride", {}) if isinstance(account_settings, dict) else {}
            if isinstance(override, dict) and override.get("enabled") is True:
                for key in ("perAccountProducts", "perAccountVideos"):
                    defaults[key] = override.get(key, defaults[key])
    except (sqlite3.Error, TypeError, json.JSONDecodeError):
        pass
    result: Dict[str, int] = {}
    for key, value in defaults.items():
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = int(DEFAULT_QUOTA[key])
        result[key] = max(1, parsed)
    return result


def active_rows_for_day(connection: sqlite3.Connection, day: date) -> List[sqlite3.Row]:
    placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
    return connection.execute(
        f"SELECT id, account_id, product_id FROM publish_queue "
        f"WHERE substr(schedule_at, 1, 10) = ? AND status IN ({placeholders})",
        (day.isoformat(), *ACTIVE_STATUSES),
    ).fetchall()


def slot_usage(
    rows: Iterable[sqlite3.Row], account_id: int, product_id: str,
    exclude_task_id: Optional[int] = None,
) -> Dict[str, int]:
    items = [row for row in rows if exclude_task_id is None or int(row["id"]) != exclude_task_id]
    account_items = [row for row in items if int(row["account_id"]) == account_id]
    account_products = {str(row["product_id"]) for row in account_items}
    return {
        "productVideos": sum(1 for row in account_items if str(row["product_id"]) == product_id),
        "accountProducts": len(account_products),
        "accountHasProduct": int(product_id in account_products),
        "accountVideos": len(account_items),
    }


def fits_daily_quota(usage: Dict[str, int], quota: Dict[str, int]) -> bool:
    return (
        usage["productVideos"] + 1 <= quota["perProductVideos"]
        and usage["accountProducts"] + (1 if not usage["accountHasProduct"] else 0) <= quota["perAccountProducts"]
        and usage["accountVideos"] + 1 <= quota["perAccountVideos"]
    )


def record_publish_event(
    connection: sqlite3.Connection, task_id: int, stage: str, outcome: str,
    reason: str = "", screenshot_path: str = "",
) -> None:
    """发布/上传阶段的追加式过程日志；与队列检查点在同一事务提交。"""
    connection.execute(
        "INSERT INTO publish_queue_events(task_id, stage, outcome, reason, screenshot_path, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (task_id, stage, outcome, reason[:2000], screenshot_path, now_text()),
    )


def publish_events(connection: sqlite3.Connection, task_id: int) -> Dict[str, Any]:
    if not connection.execute("SELECT 1 FROM publish_queue WHERE id = ?", (task_id,)).fetchone():
        raise ValueError("队列任务不存在")
    rows = connection.execute(
        "SELECT id, stage, outcome, reason, screenshot_path, cleared_at, created_at "
        "FROM publish_queue_events WHERE task_id = ? ORDER BY id", (task_id,),
    ).fetchall()
    return {"ok": True, "id": task_id, "events": [dict(row) for row in rows]}


def clear_failure_log(connection: sqlite3.Connection, task_id: int) -> Dict[str, Any]:
    """人工确认重试前清除当前失败提示，保留追加式审计轨迹。"""
    row = _manual_retry_row(connection, task_id)
    if not str(row["platform_error"] or "").strip():
        raise ValueError("当前任务没有可清除的失败提示")
    timestamp = now_text()
    connection.execute(
        "UPDATE publish_queue_events SET cleared_at = ? WHERE id = ("
        "SELECT id FROM publish_queue_events WHERE task_id = ? AND reason <> '' AND cleared_at IS NULL "
        "ORDER BY id DESC LIMIT 1)", (timestamp, task_id),
    )
    connection.execute(
        "UPDATE publish_queue SET platform_error = '', updated_at = ? WHERE id = ?",
        (timestamp, task_id),
    )
    record_publish_event(connection, task_id, "失败日志", "cleared_for_retry")
    return {"ok": True, "id": task_id, "clearedAt": timestamp}


def queue_list(connection: sqlite3.Connection, limit: int = 300) -> Dict[str, Any]:
    rows = connection.execute(
        "SELECT q.id, q.account_id, a.name AS account_name, q.product_id, q.product_name, q.product_url, "
        "q.video_path, q.title, q.description, q.schedule_at, q.mode, q.status, "
        "q.auto_publish, "
        "q.platform_status, q.platform_error, q.screenshot_path, q.execution_started_at, "
        "q.isolated_from_status, q.platform_updated_at, q.published_at, "
        "q.created_at, q.updated_at "
        "FROM publish_queue q JOIN accounts a ON a.id = q.account_id "
        "ORDER BY q.schedule_at, q.id LIMIT ?",
        (max(1, min(1000, limit)),),
    ).fetchall()
    return {
        "ok": True,
        "rows": [dict(row) for row in rows],
        "count": len(rows),
        "accounts": list_accounts(connection),
        "products": selected_product_options(connection),
    }


def export_receipts(connection: sqlite3.Connection, output: str) -> Dict[str, Any]:
    """导出全部本机发布回执；CSV 单元格避免被表格软件解释成公式。"""
    columns = (
        "id", "account_name", "product_id", "product_name", "video_path",
        "schedule_at", "execution_started_at", "platform_status", "platform_error",
        "screenshot_path", "platform_updated_at", "published_at",
    )
    target = Path(output).expanduser().resolve()
    if target.suffix.lower() != ".csv":
        raise ValueError("回执文件必须为 CSV")
    rows = connection.execute(
        "SELECT q.id, a.name AS account_name, q.product_id, q.product_name, q.video_path, "
        "q.schedule_at, q.execution_started_at, q.platform_status, q.platform_error, "
        "q.screenshot_path, q.platform_updated_at, q.published_at "
        "FROM publish_queue q JOIN accounts a ON a.id=q.account_id ORDER BY q.id",
    ).fetchall()
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(columns)
        for row in rows:
            writer.writerow(("'" + str(row[name]) if str(row[name] or "").startswith(("=", "+", "-", "@"))
                             else str(row[name] or "")) for name in columns)
    return {"ok": True, "path": str(target), "count": len(rows)}


def add_task(
    connection: sqlite3.Connection,
    account_id: int,
    product_id: str,
    video_path: str,
    title: str,
    description: str,
    planned_at: str,
    mode: str,
    auto_publish: bool = False,
) -> Dict[str, Any]:
    account = require_active_account(connection, account_id)
    normalized_product = product_id.strip()
    if not normalized_product:
        raise ValueError("商品 ID 不能为空")
    selected_product = next(
        (
            item for item in selected_product_options(connection)
            if item["account_id"] == account_id and item["product_id"] == normalized_product
        ),
        None,
    )
    if not selected_product:
        raise ValueError("该账号的本地选品记录中找不到此商品；请先完成选品并确认商品已加车")
    if mode not in ("manual", "scheduled"):
        raise ValueError("发布计划模式无效")
    path, file_key = normalize_video_path(video_path)
    planned = datetime.fromisoformat(planned_at)
    if planned.tzinfo is not None:
        raise ValueError("发布时间请使用本机本地时间")
    planned = planned.replace(second=0, microsecond=0)
    if planned < datetime.now().replace(second=0, microsecond=0):
        raise ValueError("发布时间不能早于当前本地时间")
    clean_title = title.strip() or Path(path).stem
    clean_description = description.strip() or str(selected_product["product_name"])
    if len(clean_description) > 500:
        raise ValueError("视频简介不能超过 500 个字符")

    # 用 BEGIN IMMEDIATE 避免两个客户端并发占到同一配额；若调用方已有事务则嵌套保存点。
    outer_transaction = connection.in_transaction
    if outer_transaction:
        connection.execute("SAVEPOINT publish_queue_add")
    else:
        connection.execute("BEGIN IMMEDIATE")
    try:
        duplicate = connection.execute(
            "SELECT id, status FROM publish_queue WHERE account_id = ? AND video_key = ? AND status <> 'cancelled'",
            (account_id, file_key),
        ).fetchone()
        if duplicate:
            raise ValueError(f"该视频已在此账号队列中（任务 #{duplicate['id']}，状态：{duplicate['status']}）")
        ensure_product_not_locked(connection, account_id, normalized_product)

        quota = load_quota(connection, account_id)
        rolled_days = 0
        chosen = planned
        usage: Dict[str, int] = {}
        for offset in range(366):
            usage = slot_usage(
                active_rows_for_day(connection, chosen.date()), account_id, normalized_product,
            )
            if fits_daily_quota(usage, quota):
                rolled_days = offset
                break
            chosen = datetime.combine(chosen.date() + timedelta(days=1), time(chosen.hour, chosen.minute))
        else:
            raise ValueError("未来 365 天内都没有可用发布配额，请减少队列任务或调整配额")

        status = "awaiting_confirmation" if mode == "manual" else "scheduled"
        timestamp = now_text()
        cursor = connection.execute(
            "INSERT INTO publish_queue(account_id, product_id, product_name, product_url, video_path, video_key, title, description, schedule_at, mode, status, auto_publish, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                account_id, normalized_product, selected_product["product_name"], selected_product["product_url"], path,
                file_key, clean_title, clean_description, chosen.isoformat(timespec="minutes"),
                mode, status, int(auto_publish), timestamp, timestamp,
            ),
        )
        result = {
            "ok": True,
            "id": int(cursor.lastrowid),
            "accountName": str(account["name"]),
            "scheduleAt": chosen.isoformat(timespec="minutes"),
            "rolledDays": rolled_days,
            "status": status,
            "quota": quota,
            "usageBeforeAdd": usage,
        }
        if outer_transaction:
            connection.execute("RELEASE SAVEPOINT publish_queue_add")
        else:
            connection.commit()
        return result
    except Exception:
        if outer_transaction:
            connection.execute("ROLLBACK TO SAVEPOINT publish_queue_add")
            connection.execute("RELEASE SAVEPOINT publish_queue_add")
        else:
            connection.rollback()
        raise


def add_generated_task(
    connection: sqlite3.Connection,
    account_id: int,
    product_id: str,
    video_path: str,
) -> Dict[str, Any]:
    """按当前发布策略，将已生成的视频加入待确认或全自动队列。"""
    require_active_account(connection, account_id)
    normalized_product = product_id.strip()
    if not normalized_product:
        raise ValueError("商品 ID 不能为空")
    path, file_key = normalize_video_path(video_path)
    existing = connection.execute(
        "SELECT id, product_id, status, auto_publish FROM publish_queue "
        "WHERE account_id = ? AND video_key = ? AND status <> 'cancelled'",
        (account_id, file_key),
    ).fetchone()
    if existing:
        if str(existing["product_id"]) != normalized_product:
            raise ValueError("该视频已关联此账号的其他商品，不能自动加入当前商品队列")
        return {
            "ok": True,
            "id": int(existing["id"]),
            "status": str(existing["status"]),
            "autoPublish": bool(existing["auto_publish"]),
            "alreadyExists": True,
        }

    product = next(
        (
            item for item in selected_product_options(connection)
            if item["account_id"] == account_id and item["product_id"] == normalized_product
        ),
        None,
    )
    if not product:
        raise ValueError("该账号的本地选品记录中找不到此商品；请先确认商品已加车")

    # 新选品视频始终登记为自动任务；全局暂停只阻止执行，不把任务降级为人工任务。
    # A-63 阻塞解除后可继续执行，配额顺延仍由 add_task 保留为未来计划时间。
    planned_at = datetime.now().replace(second=0, microsecond=0)
    auto_publish = True
    try:
        result = add_task(
            connection,
            account_id,
            normalized_product,
            path,
            str(product["product_name"]),
            str(product["product_name"]),
            planned_at.isoformat(timespec="minutes"),
            "scheduled",
            auto_publish=auto_publish,
        )
    except ValueError:
        # 并发触发时，另一调用可能已先完成插入；只将同商品同视频视为幂等成功。
        existing = connection.execute(
            "SELECT id, product_id, status, auto_publish FROM publish_queue "
            "WHERE account_id = ? AND video_key = ? AND status <> 'cancelled'",
            (account_id, file_key),
        ).fetchone()
        if not existing:
            raise
        if str(existing["product_id"]) != normalized_product:
            raise ValueError("该视频已关联此账号的其他商品，不能自动加入当前商品队列")
        return {
            "ok": True,
            "id": int(existing["id"]),
            "status": str(existing["status"]),
            "autoPublish": bool(existing["auto_publish"]),
            "alreadyExists": True,
        }
    result["alreadyExists"] = False
    result["autoPublish"] = auto_publish
    return result


def get_auto_publish_enabled(connection: sqlite3.Connection) -> bool:
    row = connection.execute(
        "SELECT auto_publish_enabled FROM publish_settings WHERE setting_id = 1"
    ).fetchone()
    return bool(row and int(row[0]))


def auto_publish_settings(connection: sqlite3.Connection) -> Dict[str, Any]:
    row = connection.execute(
        "SELECT auto_publish_enabled, auto_publish_activated_at, updated_at, "
        "publish_jitter_min_seconds, publish_jitter_max_seconds, publish_catchup_max_late_hours, manual_review_enabled "
        "FROM publish_settings WHERE setting_id = 1"
    ).fetchone()
    return {
        "ok": True,
        "autoPublishEnabled": bool(row and int(row["auto_publish_enabled"])),
        "activatedAt": str(row["auto_publish_activated_at"] or "") if row else "",
        "publishJitterMinSeconds": int(row["publish_jitter_min_seconds"] or 0) if row else 5,
        "publishJitterMaxSeconds": int(row["publish_jitter_max_seconds"] or 0) if row else 15,
        "publishCatchupMaxLateHours": int(row["publish_catchup_max_late_hours"] or 0) if row else 0,
        "manualReviewEnabled": bool(row and int(row["manual_review_enabled"])),
        "updatedAt": str(row["updated_at"] or "") if row else "",
    }


def set_auto_publish_enabled(connection: sqlite3.Connection, enabled: bool) -> Dict[str, Any]:
    return set_publish_scheduler_settings(connection, enabled=enabled)


def set_publish_scheduler_settings(
    connection: sqlite3.Connection,
    *,
    enabled: Optional[bool] = None,
    jitter_min_seconds: Optional[int] = None,
    jitter_max_seconds: Optional[int] = None,
    catchup_max_late_hours: Optional[int] = None,
    manual_review_enabled: Optional[bool] = None,
) -> Dict[str, Any]:
    """保存本机发布调度策略；0 小时表示逾期计划不自动过期。"""
    if all(value is None for value in (enabled, jitter_min_seconds, jitter_max_seconds, catchup_max_late_hours, manual_review_enabled)):
        raise ValueError("未提供要保存的调度设置")
    current = auto_publish_settings(connection)
    minimum = current["publishJitterMinSeconds"] if jitter_min_seconds is None else jitter_min_seconds
    maximum = current["publishJitterMaxSeconds"] if jitter_max_seconds is None else jitter_max_seconds
    catchup_hours = current["publishCatchupMaxLateHours"] if catchup_max_late_hours is None else catchup_max_late_hours
    for name, value, upper in (
        ("最短错峰间隔", minimum, 3600),
        ("最长错峰间隔", maximum, 3600),
        ("逾期补发期限", catchup_hours, 24 * 365),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > upper:
            raise ValueError(f"{name}必须是 0–{upper} 的整数")
    if minimum > maximum:
        raise ValueError("最长错峰间隔不能小于最短错峰间隔")
    values: Dict[str, Any] = {
        "publish_jitter_min_seconds": minimum,
        "publish_jitter_max_seconds": maximum,
        "publish_catchup_max_late_hours": catchup_hours,
        "updated_at": now_text(),
    }
    if enabled is not None:
        values["auto_publish_enabled"] = int(enabled)
    if manual_review_enabled is not None:
        values["manual_review_enabled"] = int(manual_review_enabled)
    assignments = ", ".join(f"{column} = ?" for column in values)
    connection.execute(
        f"UPDATE publish_settings SET {assignments} WHERE setting_id = 1",
        tuple(values.values()),
    )
    return auto_publish_settings(connection)


def next_auto_publish_task(connection: sqlite3.Connection, task_id: Optional[int] = None) -> Dict[str, Any]:
    """只取尚未上传的到期任务；上传中或待核查任务留在队列中隔离，不阻塞后续任务。"""
    if not get_auto_publish_enabled(connection):
        return {"ok": True, "task": None, "autoPublishEnabled": False}
    due_at = datetime.now().isoformat(timespec="minutes")
    activated_at_row = connection.execute(
        "SELECT auto_publish_activated_at, auto_publish_queue_start_id, publish_catchup_max_late_hours "
        "FROM publish_settings WHERE setting_id = 1"
    ).fetchone()
    activated_at = str(activated_at_row[0] or "") if activated_at_row else ""
    if not activated_at:
        return {"ok": True, "task": None, "autoPublishEnabled": True}
    queue_start_id = int(activated_at_row[1] or 0)
    catchup_max_late_hours = int(activated_at_row[2] or 0)
    skipped_stale: List[Dict[str, Any]] = []
    if catchup_max_late_hours > 0:
        cutoff = (datetime.now() - timedelta(hours=catchup_max_late_hours)).isoformat(timespec="minutes")
        stale_rows = connection.execute(
            "SELECT q.id, q.account_id, q.product_name, q.schedule_at "
            "FROM publish_queue q JOIN accounts a ON a.id = q.account_id "
            "WHERE q.auto_publish = 1 AND q.status = 'scheduled' AND a.status = 'active' "
            "AND q.platform_status = 'not_uploaded' AND q.schedule_at < ? AND q.id > ? "
            "ORDER BY q.schedule_at, q.id",
            (cutoff, queue_start_id),
        ).fetchall()
        if stale_rows:
            timestamp = now_text()
            for stale in stale_rows:
                task_id_value = int(stale["id"])
                reason = (
                    f"错过计划时间超过 {catchup_max_late_hours} 小时，按本机调度设置跳过自动补发；"
                    "未执行抖音平台操作，可检查后重新建立计划。"
                )
                connection.execute(
                    "UPDATE publish_queue SET status = 'cancelled', platform_error = ?, "
                    "cancelled_at = ?, updated_at = ? WHERE id = ? AND status = 'scheduled' "
                    "AND auto_publish = 1 AND platform_status = 'not_uploaded'",
                    (reason, timestamp, timestamp, task_id_value),
                )
                skipped_stale.append({
                    "id": task_id_value,
                    "accountId": int(stale["account_id"]),
                    "productName": str(stale["product_name"] or ""),
                    "scheduledAt": str(stale["schedule_at"] or ""),
                    "reason": reason,
                })
    task_filter = " AND q.id = ?" if task_id is not None else ""
    parameters: List[Any] = [due_at, queue_start_id]
    if task_id is not None:
        parameters.append(task_id)
    row = connection.execute(
        "SELECT q.id, q.account_id, q.product_id, q.product_name, q.product_url, "
        "q.video_path, q.description, q.schedule_at "
        "FROM publish_queue q JOIN accounts a ON a.id = q.account_id "
        "WHERE q.auto_publish = 1 AND q.status = 'scheduled' "
        "AND a.status = 'active' "
        "AND q.platform_status = 'not_uploaded' AND q.schedule_at <= ? "
        "AND q.id > ?" + task_filter + " "
        "ORDER BY q.schedule_at, q.id LIMIT 1",
        parameters,
    ).fetchone()
    return {
        "ok": True,
        "task": dict(row) if row else None,
        "autoPublishEnabled": True,
        "staleTasksSkipped": skipped_stale,
    }


def start_immediate_auto_publish_task(connection: sqlite3.Connection, task_id: int) -> Dict[str, Any]:
    """校验并立即安排单条自动发布任务，不受全局定时开关和原计划时间限制。"""
    outer_transaction = connection.in_transaction
    if outer_transaction:
        connection.execute("SAVEPOINT publish_queue_immediate")
    else:
        connection.execute("BEGIN IMMEDIATE")
    try:
        row = connection.execute(
            "SELECT q.id, q.account_id, q.product_id, q.product_name, q.product_url, "
            "q.video_path, q.description, q.status, q.auto_publish, q.platform_status, "
            "q.schedule_at, a.status AS account_status "
            "FROM publish_queue q JOIN accounts a ON a.id = q.account_id WHERE q.id = ?",
            (task_id,),
        ).fetchone()
        if not row:
            raise ValueError("队列任务不存在")
        if row["status"] != "scheduled" or row["platform_status"] != "not_uploaded":
            raise ValueError("只有已排期且尚未上传的平台任务可以马上发布；请刷新队列查看最新状态")
        if not bool(row["auto_publish"]):
            raise ValueError("此任务不是全自动发布计划，不能使用“马上发布”")
        if row["account_status"] != "active":
            raise ValueError("请先启用此任务所属账号，再马上发布")

        blockers = connection.execute(
            "SELECT id, product_name, platform_status FROM publish_queue "
            "WHERE auto_publish = 1 AND id <> ? "
            "AND account_id = ? AND product_id = ? "
            "AND platform_status IN ('uploading', 'uploaded_waiting_publish', 'needs_review', 'submitting', 'submission_uncertain') "
            "ORDER BY schedule_at, id LIMIT 5",
            (task_id, int(row["account_id"]), str(row["product_id"])),
        ).fetchall()
        if blockers:
            summary = "、".join(
                f"#{int(item['id'])} {str(item['product_name'] or '任务')}（{str(item['platform_status'])}）"
                for item in blockers
            )
            raise ValueError(f"同账号同商品已有发布任务尚待核查：{summary}。请先核对该商品后再马上发布")

        settings = connection.execute(
            "SELECT auto_publish_activated_at, auto_publish_queue_start_id "
            "FROM publish_settings WHERE setting_id = 1"
        ).fetchone()
        if not settings or not str(settings["auto_publish_activated_at"] or ""):
            raise ValueError("全自动发布尚未完成启用；请刷新队列后重试")
        queue_start_id = int(settings["auto_publish_queue_start_id"] or 0)
        if task_id <= queue_start_id:
            raise ValueError("此任务早于全自动发布启用时间，不会追发；请为新任务创建发布计划")

        ensure_task_product_is_unique(connection, task_id)
        current_time = datetime.now().replace(second=0, microsecond=0)
        quota = load_quota(connection, int(row["account_id"]))
        usage = slot_usage(
            active_rows_for_day(connection, current_time.date()),
            int(row["account_id"]), str(row["product_id"]), exclude_task_id=task_id,
        )
        if not fits_daily_quota(usage, quota):
            raise ValueError(
                "当前账号今日发布配额已满，不能马上发布；请调整计划时间或账号配额后重试"
            )

        timestamp = now_text()
        cursor = connection.execute(
            "UPDATE publish_queue SET schedule_at = ?, updated_at = ? "
            "WHERE id = ? AND status = 'scheduled' AND auto_publish = 1 AND platform_status = 'not_uploaded'",
            (current_time.isoformat(timespec="minutes"), timestamp, task_id),
        )
        if cursor.rowcount != 1:
            raise ValueError("任务状态已变化，请刷新队列后再试")
        task = {
            "id": int(row["id"]),
            "account_id": int(row["account_id"]),
            "product_id": str(row["product_id"]),
            "product_name": str(row["product_name"] or ""),
            "product_url": str(row["product_url"] or ""),
            "video_path": str(row["video_path"] or ""),
            "description": str(row["description"] or ""),
            "schedule_at": current_time.isoformat(timespec="minutes"),
        }
        if outer_transaction:
            connection.execute("RELEASE SAVEPOINT publish_queue_immediate")
        else:
            connection.commit()
        return {"ok": True, "task": task, "immediate": True}
    except Exception:
        if outer_transaction:
            connection.execute("ROLLBACK TO SAVEPOINT publish_queue_immediate")
            connection.execute("RELEASE SAVEPOINT publish_queue_immediate")
        else:
            connection.rollback()
        raise


def hold_auto_publish_task(connection: sqlite3.Connection, task_id: int, error: str) -> Dict[str, Any]:
    """将未启动的平台任务停在待核查，防止预检错误被定时器反复重试。"""
    timestamp = now_text()
    cursor = connection.execute(
        "UPDATE publish_queue SET platform_status = 'needs_review', platform_error = ?, "
        "platform_updated_at = ?, updated_at = ? WHERE id = ? AND auto_publish = 1 "
        "AND status = 'scheduled' AND platform_status = 'not_uploaded'",
        (error[:1000], timestamp, timestamp, task_id),
    )
    return {"ok": True, "id": task_id, "held": cursor.rowcount == 1}


def isolate_upload_task(connection: sqlite3.Connection, task_id: int, reason: str, screenshot_path: str = "") -> Dict[str, Any]:
    """隔离仍处于上传准备锁定态的单项任务，避免继续重试并阻塞后续队列。"""
    row = connection.execute(
        "SELECT id, status, platform_status FROM publish_queue WHERE id = ?",
        (task_id,),
    ).fetchone()
    if not row:
        raise ValueError("队列任务不存在")
    if row["status"] != "scheduled" or row["platform_status"] != "uploading":
        raise ValueError("只有已锁定且尚未完成发布准备的任务可以隔离")
    timestamp = now_text()
    cursor = connection.execute(
        "UPDATE publish_queue SET platform_status = 'isolated', platform_error = ?, "
        "screenshot_path = COALESCE(NULLIF(?, ''), screenshot_path), "
        "isolated_from_status = 'uploading', "
        "platform_updated_at = ?, updated_at = ? "
        "WHERE id = ? AND status = 'scheduled' AND platform_status = 'uploading'",
        (str(reason or "当前发布任务已隔离")[:1000], screenshot_path, timestamp, timestamp, task_id),
    )
    if cursor.rowcount != 1:
        raise ValueError("任务状态已变化，未能保存隔离检查点")
    record_publish_event(connection, task_id, "抖音上传", "isolated", reason, screenshot_path)
    return {"ok": True, "id": task_id, "isolated": True, "platformStatus": "isolated"}


def add_downloaded_product_video(
    connection: sqlite3.Connection, product_id: str, video_path: str,
) -> Dict[str, Any]:
    """按已加车商品与启用账号匹配下载素材，逐账号幂等加入待确认队列。"""
    normalized_product = product_id.strip()
    if not normalized_product:
        return {"ok": True, "matched": [], "queued": [], "unmatched": True}

    active_ids = {
        int(row["id"])
        for row in connection.execute(
            "SELECT id FROM accounts WHERE status = 'active' ORDER BY id"
        ).fetchall()
    }
    matches = sorted({
        int(item["account_id"])
        for item in selected_product_options(connection)
        if item["product_id"] == normalized_product and int(item["account_id"]) in active_ids
    })
    results: List[Dict[str, Any]] = []
    for account_id in matches:
        try:
            results.append({
                "account_id": account_id,
                **add_generated_task(connection, account_id, normalized_product, video_path),
            })
        except ValueError as error:
            results.append({"account_id": account_id, "ok": False, "error": str(error)})
    return {
        "ok": True,
        "product_id": normalized_product,
        "matched": matches,
        "queued": results,
        "unmatched": not matches,
    }


def start_upload(connection: sqlite3.Connection, task_id: int) -> Dict[str, Any]:
    """原子占用一次上传尝试，防止重复点击或重放导致重复创建草稿。"""
    outer_transaction = connection.in_transaction
    if outer_transaction:
        connection.execute("SAVEPOINT publish_queue_upload")
    else:
        connection.execute("BEGIN IMMEDIATE")
    try:
        row = connection.execute(
            "SELECT q.*, a.name AS account_name FROM publish_queue q "
            "JOIN accounts a ON a.id = q.account_id "
            "WHERE q.id = ?",
            (task_id,),
        ).fetchone()
        if not row:
            raise ValueError("队列任务不存在")
        require_active_account(connection, int(row["account_id"]))
        if row["status"] != "scheduled":
            raise ValueError("请先确认本地排期，再准备上传到创作者中心")
        if row["platform_status"] != "not_uploaded":
            raise ValueError("该任务已尝试上传或已进入后续状态；请先检查创作者中心，避免重复上传")
        ensure_task_product_is_unique(connection, task_id)
        selected_product = next(
            (
                item for item in selected_product_options(connection)
                if item["account_id"] == int(row["account_id"])
                and item["product_id"] == str(row["product_id"])
            ),
            None,
        )
        product_name = str(row["product_name"] or (selected_product or {}).get("product_name", "")).strip()
        product_url = normalize_product_url(
            str(row["product_id"]),
            str(row["product_url"] or (selected_product or {}).get("product_url", "")).strip(),
        )
        description = str(row["description"] or product_name).strip()
        if not product_name:
            raise ValueError("选品记录中没有该商品名称，无法生成视频简介")
        if not product_url:
            raise ValueError("选品记录中没有可用商品链接，未开始上传")
        path, _ = normalize_video_path(str(row["video_path"]))
        timestamp = now_text()
        connection.execute(
            "UPDATE publish_queue SET product_name = ?, product_url = ?, description = ?, platform_status = 'uploading', "
            "platform_error = '', execution_started_at = COALESCE(execution_started_at, ?), "
            "platform_updated_at = ?, updated_at = ? WHERE id = ?",
            (product_name, product_url, description, timestamp, timestamp, timestamp, task_id),
        )
        record_publish_event(connection, task_id, "抖音上传", "started")
        if outer_transaction:
            connection.execute("RELEASE SAVEPOINT publish_queue_upload")
        else:
            connection.commit()
        return {
            "ok": True,
            "id": task_id,
            "accountId": int(row["account_id"]),
            "accountName": str(row["account_name"]),
            "productId": str(row["product_id"]),
            "productName": product_name,
            "productUrl": product_url,
            "videoPath": path,
            "description": description,
        }
    except Exception:
        if outer_transaction:
            connection.execute("ROLLBACK TO SAVEPOINT publish_queue_upload")
            connection.execute("RELEASE SAVEPOINT publish_queue_upload")
        else:
            connection.rollback()
        raise


def finish_upload(
    connection: sqlite3.Connection,
    task_id: int,
    status: str,
    error: str = "",
    product_url: str = "",
    screenshot_path: str = "",
) -> Dict[str, Any]:
    if status not in {"uploaded_waiting_publish", "submitted_review", "published", "needs_review", "submission_uncertain"}:
        raise ValueError("创作者中心上传状态无效")
    row = connection.execute(
        "SELECT id, product_id, platform_status FROM publish_queue WHERE id = ?",
        (task_id,),
    ).fetchone()
    if not row:
        raise ValueError("队列任务不存在")
    if row["platform_status"] != "uploading":
        raise ValueError("任务当前没有进行中的上传尝试")
    product_url = str(product_url or "").strip()
    if product_url and normalize_product_url(str(row["product_id"]), product_url) != product_url:
        raise ValueError("平台返回的商品链接域名或商品 ID 与发布任务不匹配")
    timestamp = now_text()
    connection.execute(
        "UPDATE publish_queue SET platform_status = ?, product_url = COALESCE(?, product_url), "
        "platform_error = ?, screenshot_path = COALESCE(NULLIF(?, ''), screenshot_path), platform_updated_at = ?, "
        "published_at = COALESCE(?, published_at), updated_at = ? WHERE id = ?",
        (status, product_url or None, error[:1000], screenshot_path, timestamp, timestamp if status == "published" else None, timestamp, task_id),
    )
    record_publish_event(connection, task_id, "抖音上传", status, error, screenshot_path)
    result = {"ok": True, "id": task_id, "platformStatus": status}
    if product_url:
        result["productUrl"] = product_url
    return result


def start_publish(connection: sqlite3.Connection, task_id: int) -> Dict[str, Any]:
    """原子锁定已上传的投稿，避免重复点击造成重复提交。"""
    connection.execute("BEGIN IMMEDIATE")
    try:
        row = connection.execute(
            "SELECT q.*, a.name AS account_name FROM publish_queue q "
            "JOIN accounts a ON a.id = q.account_id WHERE q.id = ?",
            (task_id,),
        ).fetchone()
        if not row:
            raise ValueError("队列任务不存在")
        require_active_account(connection, int(row["account_id"]))
        if row["status"] != "scheduled":
            raise ValueError("请先确认本地排期，再提交抖音发布")
        if row["platform_status"] != "uploaded_waiting_publish":
            raise ValueError("只有已上传并等待发布的任务可以提交")
        ensure_task_product_is_unique(connection, task_id)
        if not str(row["product_name"] or "").strip() or not str(row["description"] or "").strip():
            raise ValueError("发布任务缺少商品名称或视频简介")
        saved_product_url = str(row["product_url"] or "").strip()
        if not saved_product_url or normalize_product_url(str(row["product_id"]), saved_product_url) != saved_product_url:
            raise ValueError("发布任务中的商品链接与商品 ID 不匹配")
        timestamp = now_text()
        cursor = connection.execute(
            "UPDATE publish_queue SET platform_status = 'submitting', platform_error = '', "
            "execution_started_at = COALESCE(execution_started_at, ?), "
            "platform_updated_at = ?, updated_at = ? "
            "WHERE id = ? AND status = 'scheduled' AND platform_status = 'uploaded_waiting_publish'",
            (timestamp, timestamp, timestamp, task_id),
        )
        if cursor.rowcount != 1:
            raise ValueError("任务状态已变化，请刷新队列后重试")
        record_publish_event(connection, task_id, "抖音发布", "started")
        connection.commit()
        return {
            "ok": True,
            "id": task_id,
            "accountId": int(row["account_id"]),
            "accountName": str(row["account_name"]),
            "productId": str(row["product_id"]),
            "productName": str(row["product_name"]),
            "productUrl": str(row["product_url"]),
            "videoPath": str(row["video_path"]),
            "description": str(row["description"]),
        }
    except Exception:
        connection.rollback()
        raise


def finish_publish(
    connection: sqlite3.Connection,
    task_id: int,
    status: str,
    error: str = "",
    screenshot_path: str = "",
) -> Dict[str, Any]:
    if status not in {"uploaded_waiting_publish", "submitted_review", "submission_uncertain", "published"}:
        raise ValueError("抖音发布结果状态无效")
    row = connection.execute(
        "SELECT id, platform_status FROM publish_queue WHERE id = ?",
        (task_id,),
    ).fetchone()
    if not row:
        raise ValueError("队列任务不存在")
    if row["platform_status"] != "submitting":
        raise ValueError("任务当前没有进行中的发布提交")
    timestamp = now_text()
    connection.execute(
        "UPDATE publish_queue SET platform_status = ?, platform_error = ?, "
        "screenshot_path = COALESCE(NULLIF(?, ''), screenshot_path), "
        "platform_updated_at = ?, published_at = COALESCE(?, published_at), updated_at = ? WHERE id = ?",
        (status, error[:1000], screenshot_path, timestamp, timestamp if status == "published" else None, timestamp, task_id),
    )
    record_publish_event(connection, task_id, "抖音发布", status, error, screenshot_path)
    return {"ok": True, "id": task_id, "platformStatus": status}


def isolate_publish_task(connection: sqlite3.Connection, task_id: int, reason: str, screenshot_path: str = "") -> Dict[str, Any]:
    """隔离平台已明确因缺封面拒绝提交的任务，禁止伪装成功或自动重试。"""
    row = connection.execute(
        "SELECT id, status, platform_status FROM publish_queue WHERE id = ?",
        (task_id,),
    ).fetchone()
    if not row:
        raise ValueError("队列任务不存在")
    if row["status"] != "scheduled" or row["platform_status"] != "submitting":
        raise ValueError("只有正在提交的任务可以记录平台缺封面隔离状态")
    timestamp = now_text()
    cursor = connection.execute(
        "UPDATE publish_queue SET platform_status = 'isolated', platform_error = ?, "
        "screenshot_path = COALESCE(NULLIF(?, ''), screenshot_path), "
        "isolated_from_status = 'submitting', "
        "platform_updated_at = ?, updated_at = ? "
        "WHERE id = ? AND status = 'scheduled' AND platform_status = 'submitting'",
        (str(reason or "平台要求封面，当前任务已隔离")[:1000], screenshot_path, timestamp, timestamp, task_id),
    )
    if cursor.rowcount != 1:
        raise ValueError("任务状态已变化，未能保存隔离检查点")
    record_publish_event(connection, task_id, "抖音发布", "isolated", reason, screenshot_path)
    return {"ok": True, "id": task_id, "isolated": True, "platformStatus": "isolated"}


def reset_submission(connection: sqlite3.Connection, task_id: int) -> Dict[str, Any]:
    """仅在用户检查创作者中心并确认未发布后恢复可提交状态。"""
    row = connection.execute(
        "SELECT id, platform_status FROM publish_queue WHERE id = ?",
        (task_id,),
    ).fetchone()
    if not row:
        raise ValueError("队列任务不存在")
    if row["platform_status"] not in {"submission_uncertain", "submitting"}:
        raise ValueError("只有发布结果待核对或中断的任务可以恢复")
    timestamp = now_text()
    connection.execute(
        "UPDATE publish_queue SET platform_status = 'uploaded_waiting_publish', platform_error = '', "
        "platform_updated_at = ?, updated_at = ? WHERE id = ?",
        (timestamp, timestamp, task_id),
    )
    return {"ok": True, "id": task_id, "platformStatus": "uploaded_waiting_publish"}


def mark_published(connection: sqlite3.Connection, task_id: int) -> Dict[str, Any]:
    row = connection.execute(
        "SELECT id, platform_status FROM publish_queue WHERE id = ?",
        (task_id,),
    ).fetchone()
    if not row:
        raise ValueError("队列任务不存在")
    if row["platform_status"] == "published":
        return {"ok": True, "id": task_id, "platformStatus": "published", "alreadyMarked": True}
    if row["platform_status"] not in {"uploaded_waiting_publish", "submission_uncertain", "submitting"}:
        raise ValueError("只有已上传待发布或结果待核对的任务可以记录为已发布")
    timestamp = now_text()
    connection.execute(
        "UPDATE publish_queue SET platform_status = 'published', platform_error = '', "
        "platform_updated_at = ?, published_at = ?, updated_at = ? WHERE id = ?",
        (timestamp, timestamp, timestamp, task_id),
    )
    return {"ok": True, "id": task_id, "platformStatus": "published"}


def reset_upload(connection: sqlite3.Connection, task_id: int) -> Dict[str, Any]:
    """仅供用户在浏览器确认没有成功上传后，将未知状态恢复为可重试。"""
    row = connection.execute(
        "SELECT id, platform_status FROM publish_queue WHERE id = ?",
        (task_id,),
    ).fetchone()
    if not row:
        raise ValueError("队列任务不存在")
    if row["platform_status"] not in {"needs_review", "uploading"}:
        raise ValueError("只有上传结果不确定的任务可以重置")
    timestamp = now_text()
    connection.execute(
        "UPDATE publish_queue SET platform_status = 'not_uploaded', platform_error = '', "
        "platform_updated_at = ?, updated_at = ? WHERE id = ?",
        (timestamp, timestamp, task_id),
    )
    return {"ok": True, "id": task_id, "platformStatus": "not_uploaded"}


def resume_upload(connection: sqlite3.Connection, task_id: int) -> Dict[str, Any]:
    """恢复已有投稿页流程；只允许 needs_review -> uploading，不重新传视频。"""
    connection.execute("BEGIN IMMEDIATE")
    try:
        row = connection.execute(
            "SELECT id, account_id, status, platform_status FROM publish_queue "
            "WHERE id = ?",
            (task_id,),
        ).fetchone()
        if not row:
            raise ValueError("队列任务不存在")
        require_active_account(connection, int(row["account_id"]))
        if row["status"] != "scheduled" or row["platform_status"] != "needs_review":
            raise ValueError("只有待核对的已排期任务可以继续现有投稿")
        timestamp = now_text()
        connection.execute(
            "UPDATE publish_queue SET platform_status = 'uploading', platform_error = '', "
            "platform_updated_at = ?, updated_at = ? WHERE id = ?",
            (timestamp, timestamp, task_id),
        )
        connection.commit()
        return {
            "ok": True,
            "id": task_id,
            "accountId": int(row["account_id"]),
            "platformStatus": "uploading",
            "resumeExistingUploadOnly": True,
        }
    except Exception:
        connection.rollback()
        raise


def _manual_retry_row(connection: sqlite3.Connection, task_id: int) -> sqlite3.Row:
    row = connection.execute(
        "SELECT q.*, a.name AS account_name FROM publish_queue q "
        "JOIN accounts a ON a.id = q.account_id WHERE q.id = ?",
        (task_id,),
    ).fetchone()
    if not row:
        raise ValueError("队列任务不存在")
    require_active_account(connection, int(row["account_id"]))
    if row["status"] != "scheduled":
        raise ValueError("只有已确认排期且未取消的任务可以手动重发")
    if row["platform_status"] in {"published", "submitted_review"}:
        raise ValueError("该作品已发布或已提交审核，禁止重复发布")
    if row["platform_status"] not in {
        "needs_review", "uploading", "isolated", "submission_uncertain",
        "submitting", "uploaded_waiting_publish",
    }:
        raise ValueError("该任务没有可恢复的发布检查点，请使用原发布入口")
    return row


def _manual_retry_task(connection: sqlite3.Connection, row: sqlite3.Row, *, validate_video: bool) -> Dict[str, Any]:
    selected_product = next(
        (
            item for item in selected_product_options(connection)
            if item["account_id"] == int(row["account_id"])
            and item["product_id"] == str(row["product_id"])
        ),
        None,
    )
    product_name = str(row["product_name"] or (selected_product or {}).get("product_name", "")).strip()
    product_url = normalize_product_url(
        str(row["product_id"]),
        str(row["product_url"] or (selected_product or {}).get("product_url", "")).strip(),
    )
    description = str(row["description"] or product_name).strip()
    if not product_name or not product_url or not description:
        raise ValueError("发布任务缺少商品名称、链接或视频简介，未启动手动重发")
    video_path = str(row["video_path"] or "")
    if validate_video:
        video_path, _ = normalize_video_path(video_path)
    return {
        "id": int(row["id"]),
        "accountId": int(row["account_id"]),
        "accountName": str(row["account_name"]),
        "productId": str(row["product_id"]),
        "productName": product_name,
        "productUrl": product_url,
        "videoPath": video_path,
        "description": description,
        "autoPublish": bool(row["auto_publish"]),
    }


def manual_retry_plan(connection: sqlite3.Connection, task_id: int) -> Dict[str, Any]:
    """只读说明单条重发检查点；历史隔离来源不明时要求先确认未提交。"""
    row = _manual_retry_row(connection, task_id)
    previous = str(row["platform_status"])
    isolation_from = str(row["isolated_from_status"] or "") if previous == "isolated" else ""
    needs_confirm_not_submitted = previous in {"submission_uncertain", "submitting"} or (
        previous == "isolated" and isolation_from != "uploading"
    )
    return {
        "ok": True,
        "task": _manual_retry_task(connection, row, validate_video=False),
        "previousStatus": previous,
        "isolationFromStatus": isolation_from,
        "needsConfirmNotSubmitted": needs_confirm_not_submitted,
        "needsConfirmNoDraftForNewUpload": previous in {"needs_review", "uploading", "isolated"},
    }


def start_manual_retry(
    connection: sqlite3.Connection,
    task_id: int,
    expected_status: str,
    *,
    confirmed_not_submitted: bool = False,
    confirmed_no_draft: bool = False,
) -> Dict[str, Any]:
    """原子复位单条检查点；先续原投稿，双重人工确认后才允许整段重传。"""
    outer_transaction = connection.in_transaction
    if outer_transaction:
        connection.execute("SAVEPOINT publish_queue_manual_retry")
    else:
        connection.execute("BEGIN IMMEDIATE")
    try:
        row = _manual_retry_row(connection, task_id)
        previous = str(row["platform_status"])
        if previous != expected_status:
            raise ValueError("任务状态已变化，请刷新队列后再重试")
        isolation_from = str(row["isolated_from_status"] or "") if previous == "isolated" else ""
        if previous == "isolated" and isolation_from not in {"", "uploading", "submitting"}:
            raise ValueError("隔离前状态无法识别，请先核对创作者中心")
        submission_stage = previous in {"submission_uncertain", "submitting"} or (
            previous == "isolated" and isolation_from == "submitting"
        )
        unknown_isolation = previous == "isolated" and not isolation_from
        if (submission_stage or unknown_isolation) and not confirmed_not_submitted:
            raise ValueError("请先确认创作者中心没有提交或审核中的同一作品，再继续手动重发")
        if confirmed_no_draft and previous in {"submission_uncertain", "submitting", "uploaded_waiting_publish"}:
            if not confirmed_not_submitted:
                raise ValueError("提交阶段整段重跑前，须同时确认作品未发布或进入审核，且没有现有草稿")

        ensure_task_product_is_unique(connection, task_id)
        task = _manual_retry_task(connection, row, validate_video=True)
        if confirmed_no_draft:
            route = "upload_new"
            new_status = "uploading"
        elif previous == "uploaded_waiting_publish" or submission_stage:
            review_mode = auto_publish_settings(connection)["manualReviewEnabled"]
            route = "submit" if task["autoPublish"] and not review_mode else "ready_manual"
            new_status = "submitting" if route == "submit" else "uploaded_waiting_publish"
        else:
            route = "upload_new" if confirmed_no_draft else "resume_upload"
            new_status = "uploading"
        timestamp = now_text()
        cursor = connection.execute(
            "UPDATE publish_queue SET product_name = ?, product_url = ?, description = ?, "
            "platform_status = ?, platform_error = '', isolated_from_status = '', "
            "execution_started_at = COALESCE(execution_started_at, ?), "
            "platform_updated_at = ?, updated_at = ? "
            "WHERE id = ? AND status = 'scheduled' AND platform_status = ?",
            (
                task["productName"], task["productUrl"], task["description"],
                new_status, timestamp, timestamp, timestamp, task_id, previous,
            ),
        )
        if cursor.rowcount != 1:
            raise ValueError("任务状态已变化，请刷新队列后再重试")
        record_publish_event(connection, task_id, "失败重试", route, f"前状态：{previous}")
        if outer_transaction:
            connection.execute("RELEASE SAVEPOINT publish_queue_manual_retry")
        else:
            connection.commit()
        return {
            "ok": True,
            "task": task,
            "route": route,
            "previousStatus": previous,
            "platformStatus": new_status,
            "isolationFromStatus": isolation_from,
        }
    except Exception:
        if outer_transaction:
            connection.execute("ROLLBACK TO SAVEPOINT publish_queue_manual_retry")
            connection.execute("RELEASE SAVEPOINT publish_queue_manual_retry")
        else:
            connection.rollback()
        raise


def cancel_task(connection: sqlite3.Connection, task_id: int) -> Dict[str, Any]:
    outer_transaction = connection.in_transaction
    if outer_transaction:
        connection.execute("SAVEPOINT publish_queue_cancel")
    else:
        connection.execute("BEGIN IMMEDIATE")
    try:
        row = connection.execute(
            "SELECT id, status, platform_status FROM publish_queue WHERE id = ?",
            (task_id,),
        ).fetchone()
        if not row:
            result = {"ok": False, "error": "队列任务不存在"}
        elif row["status"] == "cancelled":
            result = {"ok": True, "alreadyCancelled": True, "id": task_id}
        else:
            if row["platform_status"] not in {"not_uploaded", "isolated"}:
                raise ValueError("创作者中心上传已开始或结果待核对；请先处理平台内容，不能直接取消并释放本地配额")
            timestamp = now_text()
            connection.execute(
                "UPDATE publish_queue SET status = 'cancelled', cancelled_at = ?, updated_at = ? WHERE id = ?",
                (timestamp, timestamp, task_id),
            )
            result = {"ok": True, "id": task_id, "status": "cancelled"}
        if outer_transaction:
            connection.execute("RELEASE SAVEPOINT publish_queue_cancel")
        else:
            connection.commit()
        return result
    except Exception:
        if outer_transaction:
            connection.execute("ROLLBACK TO SAVEPOINT publish_queue_cancel")
            connection.execute("RELEASE SAVEPOINT publish_queue_cancel")
        else:
            connection.rollback()
        raise


def delete_task(connection: sqlite3.Connection, task_id: int) -> Dict[str, Any]:
    """只删除从未上传的平台任务；本地视频文件始终保留。"""
    outer_transaction = connection.in_transaction
    if outer_transaction:
        connection.execute("SAVEPOINT publish_queue_delete")
    else:
        connection.execute("BEGIN IMMEDIATE")
    try:
        row = connection.execute(
            "SELECT id, status, platform_status FROM publish_queue WHERE id = ?",
            (task_id,),
        ).fetchone()
        if not row:
            result = {"ok": False, "error": "队列任务不存在"}
        else:
            if str(row["platform_status"]) != "not_uploaded":
                raise ValueError("创作者中心上传已开始或结果待核对；不能删除该计划")
            if str(row["status"]) not in ("awaiting_confirmation", "scheduled", "cancelled"):
                raise ValueError("只有待确认、已排期或已取消的本地计划可以删除")
            cursor = connection.execute(
                "DELETE FROM publish_queue WHERE id = ? AND platform_status = 'not_uploaded' "
                "AND status IN ('awaiting_confirmation', 'scheduled', 'cancelled')",
                (task_id,),
            )
            if cursor.rowcount != 1:
                raise ValueError("计划状态已变化，刷新队列后再试")
            result = {"ok": True, "id": task_id, "deleted": True, "videoDeleted": False}
        if outer_transaction:
            connection.execute("RELEASE SAVEPOINT publish_queue_delete")
        else:
            connection.commit()
        return result
    except Exception:
        if outer_transaction:
            connection.execute("ROLLBACK TO SAVEPOINT publish_queue_delete")
            connection.execute("RELEASE SAVEPOINT publish_queue_delete")
        else:
            connection.rollback()
        raise


def confirm_task(connection: sqlite3.Connection, task_id: int) -> Dict[str, Any]:
    row = connection.execute(
        "SELECT id, status FROM publish_queue WHERE id = ?",
        (task_id,),
    ).fetchone()
    if not row:
        return {"ok": False, "error": "队列任务不存在"}
    if row["status"] == "scheduled":
        return {"ok": True, "alreadyConfirmed": True, "id": task_id, "status": "scheduled"}
    if row["status"] != "awaiting_confirmation":
        return {"ok": False, "error": "只有待确认任务可以确认排期"}
    connection.execute(
        "UPDATE publish_queue SET status = 'scheduled', updated_at = ? WHERE id = ?",
        (now_text(), task_id),
    )
    return {"ok": True, "id": task_id, "status": "scheduled"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="本地发布队列及创作者中心上传检查点")
    parser.add_argument("--db", type=Path, default=Path(DEFAULT_DB_PATH))
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="读取队列和可选账号")
    listing.add_argument("--limit", type=int, default=300, help="最多返回的队列行数（上限 1000）")
    product_locks = commands.add_parser("product-locks", help="读取该账号已排队或已发布商品的防重锁定状态")
    product_locks.add_argument("--account-id", type=int, required=True)
    product_locks.add_argument("--product-ids-json", help="仅读取指定商品 ID 的锁定状态")
    add = commands.add_parser("add", help="登记一个本地视频发布计划")
    add.add_argument("--account-id", type=int, required=True)
    add.add_argument("--product-id", required=True)
    add.add_argument("--video-path", required=True)
    add.add_argument("--title", default="")
    add.add_argument("--description", default="")
    add.add_argument("--planned-at", required=True, help="本地时间 ISO 格式 YYYY-MM-DDTHH:MM")
    add.add_argument("--mode", choices=("manual", "scheduled"), default="manual")
    generated = commands.add_parser("add-generated", help="把新生成的 9:16 视频幂等加入发布队列")
    generated.add_argument("--account-id", type=int, required=True)
    generated.add_argument("--product-id", required=True)
    generated.add_argument("--video-path", required=True)
    downloaded = commands.add_parser("add-downloaded", help="按已加车商品匹配启用账号，将下载视频幂等加入待确认队列")
    downloaded.add_argument("--product-id", required=True)
    downloaded.add_argument("--video-path", required=True)
    cancel = commands.add_parser("cancel", help="取消本地计划并释放该日期的队列配额")
    cancel.add_argument("--id", type=int, required=True)
    delete = commands.add_parser("delete", help="删除尚未上传的平台计划记录（保留视频文件）")
    delete.add_argument("--id", type=int, required=True)
    confirm = commands.add_parser("confirm", help="确认本地排期，不会提交到平台")
    confirm.add_argument("--id", type=int, required=True)
    upload_start = commands.add_parser("upload-start", help="开始单次创作者中心上传准备")
    upload_start.add_argument("--id", type=int, required=True)
    upload_finish = commands.add_parser("upload-finish", help="保存上传结果检查点")
    upload_finish.add_argument("--id", type=int, required=True)
    upload_finish.add_argument("--status", choices=("uploaded_waiting_publish", "submitted_review", "published", "needs_review", "submission_uncertain"), required=True)
    upload_finish.add_argument("--error", default="")
    upload_finish.add_argument("--product-url", default="")
    upload_finish.add_argument("--screenshot-path", default="")
    publish_start = commands.add_parser("submit-start", help="锁定已上传投稿并开始最终发布提交")
    publish_start.add_argument("--id", type=int, required=True)
    publish_finish = commands.add_parser("submit-finish", help="保存抖音最终发布提交结果")
    publish_finish.add_argument("--id", type=int, required=True)
    publish_finish.add_argument("--status", choices=("uploaded_waiting_publish", "submitted_review", "submission_uncertain", "published"), required=True)
    publish_finish.add_argument("--error", default="")
    publish_finish.add_argument("--screenshot-path", default="")
    publish_isolate = commands.add_parser("submit-isolate", help="隔离被平台明确拦截的单项提交任务")
    publish_isolate.add_argument("--id", type=int, required=True)
    publish_isolate.add_argument("--error", required=True)
    publish_isolate.add_argument("--screenshot-path", default="")
    reset_submission_parser = commands.add_parser("reset-submission", help="用户核实未发布后恢复最终提交")
    reset_submission_parser.add_argument("--id", type=int, required=True)
    published = commands.add_parser("mark-published", help="用户确认已在创作者中心完成发布")
    published.add_argument("--id", type=int, required=True)
    reset = commands.add_parser("reset-upload", help="在浏览器确认未上传后重置未知状态")
    reset.add_argument("--id", type=int, required=True)
    resume = commands.add_parser("resume-upload", help="继续待核对的现有投稿，不重新上传视频")
    resume.add_argument("--id", type=int, required=True)
    manual_plan = commands.add_parser("manual-retry-plan", help="读取单条手动重发所需的原状态与确认项")
    manual_plan.add_argument("--id", type=int, required=True)
    manual_start = commands.add_parser("manual-retry-start", help="按原检查点锁定并启动单条手动重发")
    manual_start.add_argument("--id", type=int, required=True)
    manual_start.add_argument("--expected-status", required=True)
    manual_start.add_argument("--confirmed-not-submitted", action="store_true")
    manual_start.add_argument("--confirmed-no-draft", action="store_true")
    receipt_export = commands.add_parser("receipt-export", help="将本机发布回执导出为 CSV")
    receipt_export.add_argument("--output", required=True)
    event_list = commands.add_parser("events", help="查看发布任务上传/发布/重试的过程日志")
    event_list.add_argument("--id", type=int, required=True)
    clear_failure = commands.add_parser("clear-failure-log", help="清除单条失败提示以触发人工确认重试；审计事件保留")
    clear_failure.add_argument("--id", type=int, required=True)
    auto_settings = commands.add_parser("auto-settings", help="读取全自动发布状态或初始化默认模式")
    auto_settings.add_argument("action", choices=("get", "set", "initialize-default"))
    auto_settings.add_argument("--enabled", choices=("true", "false"))
    auto_settings.add_argument("--jitter-min-seconds", type=int)
    auto_settings.add_argument("--jitter-max-seconds", type=int)
    auto_settings.add_argument("--catchup-max-late-hours", type=int)
    auto_settings.add_argument("--manual-review-enabled", choices=("true", "false"))
    auto_next = commands.add_parser("auto-next", help="读取一条到期且可自动发布的任务")
    auto_next.add_argument("--id", type=int, help="立即执行指定的新队列任务")
    publish_now = commands.add_parser("publish-now", help="校验并立即启动一条全自动发布计划")
    publish_now.add_argument("--id", type=int, required=True)
    auto_hold = commands.add_parser("auto-hold", help="将自动发布预检失败的任务停在待核查")
    auto_hold.add_argument("--id", type=int, required=True)
    auto_hold.add_argument("--error", required=True)
    upload_isolate = commands.add_parser("upload-isolate", help="隔离仍处于上传准备锁定态的单项发布任务")
    upload_isolate.add_argument("--id", type=int, required=True)
    upload_isolate.add_argument("--error", required=True)
    upload_isolate.add_argument("--screenshot-path", default="")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        with connect(args.db) as connection:
            initialize_auto_default = args.command == "auto-settings" and args.action == "initialize-default"
            ensure_schema(connection, initialize_auto_publish_default=initialize_auto_default)
            if args.command == "list":
                result = queue_list(connection, args.limit)
            elif args.command == "product-locks":
                product_ids = None
                if args.product_ids_json is not None:
                    try:
                        product_ids = json.loads(args.product_ids_json)
                    except json.JSONDecodeError as error:
                        raise ValueError("商品 ID 列表不是有效 JSON") from error
                    if not isinstance(product_ids, list) or any(not isinstance(item, (str, int)) for item in product_ids):
                        raise ValueError("商品 ID 列表必须是字符串或数字数组")
                result = product_publish_locks(connection, args.account_id, product_ids)
            elif args.command == "add":
                result = add_task(
                    connection, args.account_id, args.product_id, args.video_path,
                    args.title, args.description, args.planned_at, args.mode,
                )
            elif args.command == "add-generated":
                result = add_generated_task(
                    connection, args.account_id, args.product_id, args.video_path,
                )
            elif args.command == "add-downloaded":
                result = add_downloaded_product_video(
                    connection, args.product_id, args.video_path,
                )
            elif args.command == "cancel":
                result = cancel_task(connection, args.id)
            elif args.command == "delete":
                result = delete_task(connection, args.id)
            elif args.command == "confirm":
                result = confirm_task(connection, args.id)
            elif args.command == "upload-start":
                result = start_upload(connection, args.id)
            elif args.command == "upload-isolate":
                result = isolate_upload_task(connection, args.id, args.error, args.screenshot_path)
            elif args.command == "upload-finish":
                result = finish_upload(connection, args.id, args.status, args.error, args.product_url, args.screenshot_path)
            elif args.command == "submit-start":
                result = start_publish(connection, args.id)
            elif args.command == "submit-finish":
                result = finish_publish(connection, args.id, args.status, args.error, args.screenshot_path)
            elif args.command == "submit-isolate":
                result = isolate_publish_task(connection, args.id, args.error, args.screenshot_path)
            elif args.command == "reset-submission":
                result = reset_submission(connection, args.id)
            elif args.command == "mark-published":
                result = mark_published(connection, args.id)
            elif args.command == "reset-upload":
                result = reset_upload(connection, args.id)
            elif args.command == "resume-upload":
                result = resume_upload(connection, args.id)
            elif args.command == "manual-retry-plan":
                result = manual_retry_plan(connection, args.id)
            elif args.command == "manual-retry-start":
                result = start_manual_retry(
                    connection, args.id, args.expected_status,
                    confirmed_not_submitted=args.confirmed_not_submitted,
                    confirmed_no_draft=args.confirmed_no_draft,
                )
            elif args.command == "receipt-export":
                result = export_receipts(connection, args.output)
            elif args.command == "events":
                result = publish_events(connection, args.id)
            elif args.command == "clear-failure-log":
                result = clear_failure_log(connection, args.id)
            elif args.command == "auto-settings":
                if args.action == "get":
                    result = auto_publish_settings(connection)
                elif args.action == "initialize-default":
                    result = auto_publish_settings(connection)
                else:
                    enabled = None
                    if args.enabled is not None:
                        enabled = args.enabled == "true"
                    result = set_publish_scheduler_settings(
                        connection,
                        enabled=enabled,
                        jitter_min_seconds=args.jitter_min_seconds,
                        jitter_max_seconds=args.jitter_max_seconds,
                        catchup_max_late_hours=args.catchup_max_late_hours,
                        manual_review_enabled=None if args.manual_review_enabled is None else args.manual_review_enabled == "true",
                    )
            elif args.command == "auto-next":
                result = next_auto_publish_task(connection, args.id)
            elif args.command == "publish-now":
                result = start_immediate_auto_publish_task(connection, args.id)
            elif args.command == "auto-hold":
                result = hold_auto_publish_task(connection, args.id, args.error)
            else:
                return emit({"ok": False, "error": "未知命令"}, 2)
        return emit(result, 0 if result.get("ok") else 1)
    except (ValueError, OSError, sqlite3.Error) as error:
        return emit({"ok": False, "error": str(error)}, 1)


if __name__ == "__main__":
    sys.exit(main())
