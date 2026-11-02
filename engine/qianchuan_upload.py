#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Step 17 千川素材上传本地队列：资格、独立配额和可核查的结果检查点。

本模块不访问千川。浏览器执行器只在 claim 后处理单条任务，再以 complete/fail
写回。SQLite 的 BEGIN IMMEDIATE 保证同一数据库文件内的预占与状态迁移串行；
多台电脑各自持有数据库时不能据此承诺全局配额，部署时需要共享登记服务。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from db import DEFAULT_DB_PATH, connect, emit, now_text


DEFAULT_LIMITS = {
    "perProductVideos": 10,
    "perAccountProducts": 5,
    "perAccountVideos": 50,
    "perGroupProducts": 25,
    "perGroupVideos": 250,
    "maxConcurrentUploads": 1,
    "minStepDelayMs": 800,
    "maxStepDelayMs": 2000,
}
OCCUPIED_STATUSES = ("queued", "uploading", "needs_review", "succeeded")
MAX_DEFER_DAYS = 366

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS qianchuan_quota_groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS qianchuan_quota_group_members (
    account_id INTEGER PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
    group_id INTEGER NOT NULL REFERENCES qianchuan_quota_groups(id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS qianchuan_quota_settings (
    account_id INTEGER PRIMARY KEY,
    per_product_videos INTEGER NOT NULL,
    per_account_products INTEGER NOT NULL,
    per_account_videos INTEGER NOT NULL,
    per_group_products INTEGER NOT NULL,
    per_group_videos INTEGER NOT NULL,
    max_concurrent_uploads INTEGER NOT NULL DEFAULT 1,
    min_step_delay_ms INTEGER NOT NULL DEFAULT 800,
    max_step_delay_ms INTEGER NOT NULL DEFAULT 2000,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS qianchuan_upload_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id INTEGER NOT NULL,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    qianchuan_account_id INTEGER NOT NULL REFERENCES qianchuan_accounts(id),
    quota_group_id INTEGER NOT NULL REFERENCES qianchuan_quota_groups(id),
    product_id TEXT NOT NULL,
    file_path TEXT NOT NULL,
    material_name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    scheduled_date TEXT NOT NULL,
    remote_material_id TEXT NOT NULL DEFAULT '',
    error_code TEXT NOT NULL DEFAULT '',
    error_message TEXT NOT NULL DEFAULT '',
    failure_stage TEXT NOT NULL DEFAULT '',
    screenshot_path TEXT NOT NULL DEFAULT '',
    submit_attempted INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE(asset_id, account_id)
);
CREATE INDEX IF NOT EXISTS idx_qc_jobs_schedule
    ON qianchuan_upload_jobs(status, scheduled_date, account_id);
CREATE INDEX IF NOT EXISTS idx_qc_jobs_quota
    ON qianchuan_upload_jobs(quota_group_id, scheduled_date, product_id);
CREATE TABLE IF NOT EXISTS qianchuan_upload_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES qianchuan_upload_jobs(id),
    stage TEXT NOT NULL,
    outcome TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    screenshot_path TEXT NOT NULL DEFAULT '',
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_qc_events_job ON qianchuan_upload_events(job_id, id);
CREATE TABLE IF NOT EXISTS qianchuan_product_upload_status (
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    product_id TEXT NOT NULL,
    status TEXT NOT NULL,
    uploaded_count INTEGER NOT NULL DEFAULT 0,
    last_remote_material_id TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    PRIMARY KEY(account_id, product_id)
);
"""


def ensure_schema(connection: sqlite3.Connection) -> None:
    """只新增千川专用表，不改动抖音发布及选品旧表。"""
    connection.executescript(SCHEMA_SQL)
    settings_columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(qianchuan_quota_settings)")}
    for column, ddl in (
        ("max_concurrent_uploads", "INTEGER NOT NULL DEFAULT 1"),
        ("min_step_delay_ms", "INTEGER NOT NULL DEFAULT 800"),
        ("max_step_delay_ms", "INTEGER NOT NULL DEFAULT 2000"),
    ):
        if column not in settings_columns:
            connection.execute(f"ALTER TABLE qianchuan_quota_settings ADD COLUMN {column} {ddl}")
    connection.execute(
        "INSERT OR IGNORE INTO qianchuan_quota_settings "
        "(account_id, per_product_videos, per_account_products, per_account_videos, "
        "per_group_products, per_group_videos, max_concurrent_uploads, "
        "min_step_delay_ms, max_step_delay_ms, updated_at) "
        "VALUES (0, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (*DEFAULT_LIMITS.values(), now_text()),
    )
    connection.commit()


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone() is not None


def _asset_columns(connection: sqlite3.Connection) -> set[str]:
    if not _table_exists(connection, "assets"):
        return set()
    return {str(row["name"]) for row in connection.execute("PRAGMA table_info(assets)")}


def _required_asset_schema(connection: sqlite3.Connection) -> None:
    needed = {"id", "file_path", "file_name", "status", "product_id", "account_id", "source_type"}
    missing = needed - _asset_columns(connection)
    if missing:
        raise ValueError("素材库尚未完成 Step 17 字段迁移：" + ", ".join(sorted(missing)))


def _event(
    connection: sqlite3.Connection, job_id: int, stage: str, outcome: str,
    reason: str = "", screenshot_path: str = "", details: Optional[Dict[str, Any]] = None,
) -> None:
    connection.execute(
        "INSERT INTO qianchuan_upload_events "
        "(job_id, stage, outcome, reason, screenshot_path, details_json, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (job_id, stage, outcome, reason[:2000], screenshot_path,
         json.dumps(details or {}, ensure_ascii=False), now_text()),
    )


def record_event(
    connection: sqlite3.Connection, job_id: int, stage: str, outcome: str,
    reason: str = "", screenshot_path: str = "", details: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if not connection.execute("SELECT 1 FROM qianchuan_upload_jobs WHERE id = ?", (job_id,)).fetchone():
        raise ValueError(f"千川上传任务不存在：{job_id}")
    if not stage.strip() or not outcome.strip():
        raise ValueError("阶段和结果不能为空")
    _event(connection, job_id, stage.strip(), outcome.strip(), reason, screenshot_path, details)
    connection.commit()
    return {"ok": True, "jobId": job_id, "stage": stage, "outcome": outcome}


def _limits(connection: sqlite3.Connection, account_id: int) -> Dict[str, int]:
    row = connection.execute(
        "SELECT * FROM qianchuan_quota_settings WHERE account_id IN (0, ?) "
        "ORDER BY account_id DESC LIMIT 1", (account_id,),
    ).fetchone()
    if row is None:
        return DEFAULT_LIMITS.copy()
    return {
        "perProductVideos": int(row["per_product_videos"]),
        "perAccountProducts": int(row["per_account_products"]),
        "perAccountVideos": int(row["per_account_videos"]),
        "perGroupProducts": int(row["per_group_products"]),
        "perGroupVideos": int(row["per_group_videos"]),
        "maxConcurrentUploads": int(row["max_concurrent_uploads"]),
        "minStepDelayMs": int(row["min_step_delay_ms"]),
        "maxStepDelayMs": int(row["max_step_delay_ms"]),
    }


def quota_config(
    connection: sqlite3.Connection, account_id: int = 0,
    updates: Optional[Dict[str, int]] = None,
) -> Dict[str, Any]:
    if account_id < 0:
        raise ValueError("账号 ID 不能为负")
    if account_id and not connection.execute("SELECT 1 FROM accounts WHERE id = ?", (account_id,)).fetchone():
        raise ValueError(f"账号不存在：{account_id}")
    if updates:
        if not set(updates).issubset(DEFAULT_LIMITS):
            raise ValueError("未知配额字段")
        if any(not isinstance(value, int) for value in updates.values()):
            raise ValueError("配置必须为整数")
        values = _limits(connection, account_id)
        values.update(updates)
        if any(values[key] < 1 for key in (
            "perProductVideos", "perAccountProducts", "perAccountVideos",
            "perGroupProducts", "perGroupVideos", "maxConcurrentUploads",
        )) or values["minStepDelayMs"] < 1 or values["maxStepDelayMs"] < values["minStepDelayMs"]:
            raise ValueError("配额/并发须为正整数，步骤延时须满足 1 ≤ 最小值 ≤ 最大值")
        connection.execute(
            "INSERT INTO qianchuan_quota_settings "
            "(account_id, per_product_videos, per_account_products, per_account_videos, "
            "per_group_products, per_group_videos, max_concurrent_uploads, "
            "min_step_delay_ms, max_step_delay_ms, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(account_id) DO UPDATE SET "
            "per_product_videos=excluded.per_product_videos, "
            "per_account_products=excluded.per_account_products, "
            "per_account_videos=excluded.per_account_videos, "
            "per_group_products=excluded.per_group_products, "
            "per_group_videos=excluded.per_group_videos, "
            "max_concurrent_uploads=excluded.max_concurrent_uploads, "
            "min_step_delay_ms=excluded.min_step_delay_ms, "
            "max_step_delay_ms=excluded.max_step_delay_ms, updated_at=excluded.updated_at",
            (account_id, *values.values(), now_text()),
        )
        connection.commit()
    return {"ok": True, "accountId": account_id, "limits": _limits(connection, account_id),
            "quotaScope": "local_sqlite"}


def _group_for_account(connection: sqlite3.Connection, account_id: int) -> int:
    row = connection.execute(
        "SELECT group_id FROM qianchuan_quota_group_members WHERE account_id = ?", (account_id,)
    ).fetchone()
    if row:
        return int(row["group_id"])
    # 固定登记分组，避免按数据库 ID 临时重算引起配额漂移。一个组最多五个账号。
    group = connection.execute(
        "SELECT g.id FROM qianchuan_quota_groups g "
        "LEFT JOIN qianchuan_quota_group_members m ON m.group_id = g.id "
        "GROUP BY g.id HAVING COUNT(m.account_id) < 5 ORDER BY g.id LIMIT 1"
    ).fetchone()
    if group:
        group_id = int(group["id"])
    else:
        cursor = connection.execute(
            "INSERT INTO qianchuan_quota_groups(name, created_at) VALUES (?, ?)",
            ("千川配额组-" + datetime.now().strftime("%Y%m%d%H%M%S%f"), now_text()),
        )
        group_id = int(cursor.lastrowid)
    connection.execute(
        "INSERT INTO qianchuan_quota_group_members(account_id, group_id, created_at) VALUES (?, ?, ?)",
        (account_id, group_id, now_text()),
    )
    return group_id


def list_targets(connection: sqlite3.Connection) -> Dict[str, Any]:
    rows = connection.execute(
        "SELECT a.id AS account_id, a.name AS account_name, a.status AS account_status, "
        "q.id AS qianchuan_account_id, q.qc_name, q.qc_id, q.login_state, "
        "m.group_id AS quota_group_id "
        "FROM accounts a LEFT JOIN account_qianchuan aq ON aq.account_row_id = a.id "
        "LEFT JOIN qianchuan_accounts q ON q.id = aq.qianchuan_row_id "
        "LEFT JOIN qianchuan_quota_group_members m ON m.account_id = a.id "
        "ORDER BY a.id, q.id"
    ).fetchall()
    return {"ok": True, "rows": [dict(row) for row in rows], "quotaScope": "local_sqlite"}


def eligible_assets(connection: sqlite3.Connection, account_id: int, limit: int = 200) -> Dict[str, Any]:
    _required_asset_schema(connection)
    account = connection.execute("SELECT name FROM accounts WHERE id = ?", (account_id,)).fetchone()
    if not account:
        raise ValueError(f"账号不存在：{account_id}")
    publish_join = (
        "LEFT JOIN publish_queue p ON p.id = (SELECT MAX(id) FROM publish_queue "
        "WHERE account_id = a.account_id AND product_id = a.product_id "
        "AND platform_status = 'published') "
        if _table_exists(connection, "publish_queue")
        else "LEFT JOIN (SELECT NULL AS id, NULL AS published_at) p ON 1 = 0 "
    )
    rows = connection.execute(
        "SELECT a.id AS assetId, a.file_path AS filePath, a.file_name AS fileName, "
        "a.product_id AS productId, a.account_id AS accountId, a.source_type AS sourceType, "
        "p.id AS publishedTaskId, p.published_at AS publishedAt, "
        "j.id AS jobId, j.status AS uploadStatus "
        "FROM assets a "
        + publish_join +
        "LEFT JOIN qianchuan_upload_jobs j ON j.asset_id = a.id AND j.account_id = a.account_id "
        "WHERE a.account_id = ? AND a.source_type = 'web_download' "
        "AND a.status = 'ready' AND TRIM(COALESCE(a.product_id, '')) <> '' "
        "ORDER BY a.id DESC LIMIT ?", (account_id, min(max(limit, 1), 1000)),
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        file_path = Path(item["filePath"])
        item["eligible"] = bool(item["publishedTaskId"] and item["jobId"] is None
                                and file_path.suffix.lower() == ".mp4" and file_path.is_file())
        if not item["publishedTaskId"]:
            item["reason"] = "该账号同商品尚未确认抖音发布"
        elif item["jobId"] is not None:
            item["reason"] = "已有千川上传任务"
        elif file_path.suffix.lower() != ".mp4" or not file_path.is_file():
            item["reason"] = "本地素材缺少可上传的 MP4 文件"
        result.append(item)
    return {"ok": True, "accountId": account_id, "rows": result}


def _validated_asset(connection: sqlite3.Connection, asset_id: int, account_id: int) -> sqlite3.Row:
    _required_asset_schema(connection)
    asset = connection.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
    if not asset:
        raise ValueError(f"素材不存在：{asset_id}")
    if asset["account_id"] is None or int(asset["account_id"]) != account_id:
        raise ValueError(f"素材 {asset_id} 账号归属不匹配或尚未核实")
    if str(asset["source_type"]) != "web_download":
        raise ValueError(f"素材 {asset_id} 不是已确认的网页下载素材")
    if str(asset["status"]) != "ready":
        raise ValueError(f"素材 {asset_id} 尚未就绪")
    product_id = str(asset["product_id"] or "").strip()
    if not product_id:
        raise ValueError(f"素材 {asset_id} 缺少商品 ID")
    file_path = Path(str(asset["file_path"]))
    if file_path.suffix.lower() != ".mp4" or not file_path.is_file():
        raise ValueError(f"素材 {asset_id} 缺少可上传的 MP4 文件")
    if not _table_exists(connection, "publish_queue") or not connection.execute(
        "SELECT 1 FROM publish_queue WHERE account_id = ? AND product_id = ? "
        "AND platform_status = 'published' LIMIT 1", (account_id, product_id),
    ).fetchone():
        raise ValueError(f"素材 {asset_id} 同账号商品尚未确认抖音发布")
    return asset


def _target(connection: sqlite3.Connection, account_id: int, qianchuan_account_id: Optional[int]) -> int:
    account = connection.execute(
        "SELECT id, status FROM accounts WHERE id = ?", (account_id,)
    ).fetchone()
    if not account or account["status"] != "active":
        raise ValueError(f"账号不存在或未启用：{account_id}")
    bindings = connection.execute(
        "SELECT q.id FROM account_qianchuan aq JOIN qianchuan_accounts q "
        "ON q.id = aq.qianchuan_row_id WHERE aq.account_row_id = ? ORDER BY q.id",
        (account_id,),
    ).fetchall()
    ids = [int(row["id"]) for row in bindings]
    if not ids:
        raise ValueError(f"账号 {account_id} 未绑定千川账户")
    if qianchuan_account_id is None:
        if len(ids) != 1:
            raise ValueError(f"账号 {account_id} 绑定多个千川账户，请明确选择目标")
        return ids[0]
    if qianchuan_account_id not in ids:
        raise ValueError(f"千川账户 {qianchuan_account_id} 未绑定账号 {account_id}")
    return qianchuan_account_id


def _quota_usage(
    connection: sqlite3.Connection, account_id: int, group_id: int,
    product_id: str, day: str,
) -> Dict[str, int]:
    placeholders = ",".join("?" for _ in OCCUPIED_STATUSES)
    base = f"status IN ({placeholders}) AND scheduled_date = ?"
    account = connection.execute(
        f"SELECT COUNT(*) AS videos, COUNT(DISTINCT product_id) AS products "
        f"FROM qianchuan_upload_jobs WHERE {base} AND account_id = ?",
        (*OCCUPIED_STATUSES, day, account_id),
    ).fetchone()
    group = connection.execute(
        f"SELECT COUNT(*) AS videos, COUNT(DISTINCT product_id) AS products "
        f"FROM qianchuan_upload_jobs WHERE {base} AND quota_group_id = ?",
        (*OCCUPIED_STATUSES, day, group_id),
    ).fetchone()
    product = connection.execute(
        f"SELECT COUNT(*) AS videos FROM qianchuan_upload_jobs "
        f"WHERE {base} AND account_id = ? AND product_id = ?",
        (*OCCUPIED_STATUSES, day, account_id, product_id),
    ).fetchone()
    has_account_product = connection.execute(
        f"SELECT 1 FROM qianchuan_upload_jobs WHERE {base} "
        "AND account_id = ? AND product_id = ? LIMIT 1",
        (*OCCUPIED_STATUSES, day, account_id, product_id),
    ).fetchone() is not None
    has_group_product = connection.execute(
        f"SELECT 1 FROM qianchuan_upload_jobs WHERE {base} "
        "AND quota_group_id = ? AND product_id = ? LIMIT 1",
        (*OCCUPIED_STATUSES, day, group_id, product_id),
    ).fetchone() is not None
    return {
        "productVideos": int(product["videos"]),
        "accountVideos": int(account["videos"]),
        "accountProducts": int(account["products"]),
        "groupVideos": int(group["videos"]),
        "groupProducts": int(group["products"]),
        "newAccountProduct": 0 if has_account_product else 1,
        "newGroupProduct": 0 if has_group_product else 1,
    }


def _reserve_date(
    connection: sqlite3.Connection, account_id: int, group_id: int,
    product_id: str, from_day: date,
) -> date:
    limits = _limits(connection, account_id)
    # 同组账号各自可能有个性化值。按最严格成员口径执行组配额，不能因换一个
    # 账号入队就绕过已经占用的五账号组上限。
    members = connection.execute(
        "SELECT account_id FROM qianchuan_quota_group_members WHERE group_id = ?",
        (group_id,),
    ).fetchall()
    group_limits = [_limits(connection, int(member["account_id"])) for member in members]
    group_product_cap = min(item["perGroupProducts"] for item in group_limits)
    group_video_cap = min(item["perGroupVideos"] for item in group_limits)
    for offset in range(MAX_DEFER_DAYS):
        candidate = from_day + timedelta(days=offset)
        usage = _quota_usage(connection, account_id, group_id, product_id, candidate.isoformat())
        if (
            usage["productVideos"] < limits["perProductVideos"]
            and usage["accountVideos"] < limits["perAccountVideos"]
            and usage["accountProducts"] + usage["newAccountProduct"] <= limits["perAccountProducts"]
            and usage["groupVideos"] < group_video_cap
            and usage["groupProducts"] + usage["newGroupProduct"] <= group_product_cap
        ):
            return candidate
    raise ValueError("未来一年千川配额均已占用，请调整额度或人工核查")


def enqueue(
    connection: sqlite3.Connection, account_id: int, asset_ids: Iterable[int],
    qianchuan_account_id: Optional[int] = None, today: Optional[date] = None,
) -> Dict[str, Any]:
    ids = list(dict.fromkeys(int(value) for value in asset_ids))
    if not ids:
        raise ValueError("请选择至少一条素材")
    today = today or date.today()
    connection.commit()
    connection.execute("BEGIN IMMEDIATE")
    try:
        target_id = _target(connection, account_id, qianchuan_account_id)
        group_id = _group_for_account(connection, account_id)
        jobs: List[Dict[str, Any]] = []
        rejected: List[Dict[str, Any]] = []
        for asset_id in ids:
            existing = connection.execute(
                "SELECT id, status, scheduled_date FROM qianchuan_upload_jobs "
                "WHERE asset_id = ? AND account_id = ?", (asset_id, account_id),
            ).fetchone()
            if existing:
                rejected.append({"assetId": asset_id, "reason": "该素材已有千川上传记录，禁止重复提交", "jobId": existing["id"]})
                continue
            try:
                asset = _validated_asset(connection, asset_id, account_id)
                product_id = str(asset["product_id"]).strip()
                scheduled = _reserve_date(connection, account_id, group_id, product_id, today)
            except ValueError as error:
                rejected.append({"assetId": asset_id, "reason": str(error)})
                continue
            timestamp = now_text()
            cursor = connection.execute(
                "INSERT INTO qianchuan_upload_jobs "
                "(asset_id, account_id, qianchuan_account_id, quota_group_id, product_id, "
                "file_path, material_name, scheduled_date, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (asset_id, account_id, target_id, group_id, product_id,
                 str(asset["file_path"]), str(asset["file_name"]), scheduled.isoformat(),
                 timestamp, timestamp),
            )
            job_id = int(cursor.lastrowid)
            deferred = scheduled > today
            _event(connection, job_id, "quota_reservation", "deferred" if deferred else "queued",
                   "当日配额已满，顺延至次日或后续日期" if deferred else "", details={
                       "scheduledDate": scheduled.isoformat(), "accountId": account_id,
                       "quotaGroupId": group_id, "limits": _limits(connection, account_id),
                   })
            jobs.append({"id": job_id, "assetId": asset_id, "status": "queued",
                         "scheduledDate": scheduled.isoformat(), "deferred": deferred})
        connection.commit()
        return {"ok": True, "jobs": jobs, "rejected": rejected,
                "acceptedCount": len(jobs), "rejectedCount": len(rejected),
                "accountId": account_id, "qianchuanAccountId": target_id,
                "quotaGroupId": group_id, "quotaScope": "local_sqlite"}
    except Exception:
        connection.rollback()
        raise


def _job(connection: sqlite3.Connection, job_id: int) -> sqlite3.Row:
    row = connection.execute("SELECT * FROM qianchuan_upload_jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        raise ValueError(f"千川上传任务不存在：{job_id}")
    return row


def _job_payload(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": int(row["id"]), "assetId": int(row["asset_id"]),
        "accountId": int(row["account_id"]),
        "qianchuanAccountId": int(row["qianchuan_account_id"]),
        "quotaGroupId": int(row["quota_group_id"]),
        "productId": row["product_id"], "filePath": row["file_path"],
        "materialName": row["material_name"], "status": row["status"],
        "scheduledDate": row["scheduled_date"],
        "remoteMaterialId": row["remote_material_id"],
        "reason": row["error_message"], "stage": row["failure_stage"],
        "screenshotPath": row["screenshot_path"],
        "submitAttempted": bool(row["submit_attempted"]),
        "createdAt": row["created_at"], "updatedAt": row["updated_at"],
    }


def claim(connection: sqlite3.Connection, job_id: int, today: Optional[date] = None) -> Dict[str, Any]:
    today = today or date.today()
    connection.commit()
    connection.execute("BEGIN IMMEDIATE")
    try:
        row = _job(connection, job_id)
        if row["status"] != "queued":
            raise ValueError(f"任务状态为 {row['status']}，不可再次领取")
        if row["scheduled_date"] > today.isoformat():
            raise ValueError(f"该素材配额已顺延至 {row['scheduled_date']}")
        running = int(connection.execute(
            "SELECT COUNT(*) FROM qianchuan_upload_jobs WHERE status = 'uploading'"
        ).fetchone()[0])
        concurrency = _limits(connection, int(row["account_id"]))["maxConcurrentUploads"]
        if running >= concurrency:
            raise ValueError(f"当前上传并发已达 {concurrency}，稍后重试领取")
        _target(connection, int(row["account_id"]), int(row["qianchuan_account_id"]))
        _validated_asset(connection, int(row["asset_id"]), int(row["account_id"]))
        # 跨日未执行任务在真正上传日重新预占，旧日期的预占不跨日冒充额度。
        if row["scheduled_date"] < today.isoformat():
            scheduled = _reserve_date(connection, int(row["account_id"]),
                                      int(row["quota_group_id"]), row["product_id"], today)
            connection.execute(
                "UPDATE qianchuan_upload_jobs SET scheduled_date = ?, updated_at = ? WHERE id = ?",
                (scheduled.isoformat(), now_text(), job_id),
            )
            if scheduled > today:
                _event(connection, job_id, "quota_reservation", "deferred", "跨日重排后当日额度不足",
                       details={"scheduledDate": scheduled.isoformat()})
                connection.commit()
                return {"ok": False, "deferred": True,
                        "error": f"当日千川配额已满，任务顺延至 {scheduled.isoformat()}",
                        "job": _job_payload(_job(connection, job_id))}
        if not Path(row["file_path"]).is_file():
            raise ValueError("任务素材文件已不存在，停止上传")
        connection.execute(
            "UPDATE qianchuan_upload_jobs SET status = 'uploading', started_at = ?, updated_at = ? "
            "WHERE id = ? AND status = 'queued'", (now_text(), now_text(), job_id),
        )
        _event(connection, job_id, "upload_claim", "started")
        connection.commit()
        return {"ok": True, "job": _job_payload(_job(connection, job_id)),
                "quotaScope": "local_sqlite"}
    except Exception:
        connection.rollback()
        raise


def complete(connection: sqlite3.Connection, job_id: int, remote_material_id: str) -> Dict[str, Any]:
    remote_material_id = remote_material_id.strip()
    if not remote_material_id:
        raise ValueError("未回读千川素材 ID，不能标记上传成功")
    connection.commit()
    connection.execute("BEGIN IMMEDIATE")
    try:
        row = _job(connection, job_id)
        if row["status"] not in ("uploading", "needs_review"):
            raise ValueError(f"任务状态为 {row['status']}，不能标记成功")
        timestamp = now_text()
        connection.execute(
            "UPDATE qianchuan_upload_jobs SET status = 'succeeded', remote_material_id = ?, "
            "error_code = '', error_message = '', failure_stage = '', completed_at = ?, "
            "updated_at = ? WHERE id = ?",
            (remote_material_id, timestamp, timestamp, job_id),
        )
        count = connection.execute(
            "SELECT COUNT(*) FROM qianchuan_upload_jobs WHERE account_id = ? AND product_id = ? "
            "AND status = 'succeeded'", (row["account_id"], row["product_id"]),
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO qianchuan_product_upload_status "
            "(account_id, product_id, status, uploaded_count, last_remote_material_id, updated_at) "
            "VALUES (?, ?, 'uploaded', ?, ?, ?) "
            "ON CONFLICT(account_id, product_id) DO UPDATE SET status = 'uploaded', "
            "uploaded_count = excluded.uploaded_count, "
            "last_remote_material_id = excluded.last_remote_material_id, updated_at = excluded.updated_at",
            (row["account_id"], row["product_id"], count, remote_material_id, timestamp),
        )
        _event(connection, job_id, "remote_verify", "succeeded", details={
            "remoteMaterialId": remote_material_id, "productUploadStatus": "uploaded",
        })
        connection.commit()
        return {"ok": True, "job": _job_payload(_job(connection, job_id))}
    except Exception:
        connection.rollback()
        raise


def fail(
    connection: sqlite3.Connection, job_id: int, reason: str, stage: str,
    screenshot_path: str = "", submit_attempted: bool = False,
    uncertain: bool = False,
) -> Dict[str, Any]:
    if not reason.strip() or not stage.strip():
        raise ValueError("失败原因和阶段不能为空")
    connection.commit()
    connection.execute("BEGIN IMMEDIATE")
    try:
        row = _job(connection, job_id)
        if row["status"] != "uploading":
            raise ValueError(f"任务状态为 {row['status']}，不能重复登记失败")
        needs_review = bool(submit_attempted or uncertain)
        status = "needs_review" if needs_review else "failed"
        timestamp = now_text()
        connection.execute(
            "UPDATE qianchuan_upload_jobs SET status = ?, error_code = ?, error_message = ?, "
            "failure_stage = ?, screenshot_path = ?, submit_attempted = ?, "
            "completed_at = ?, updated_at = ? WHERE id = ?",
            (status, "SUBMISSION_UNCERTAIN" if needs_review else "UPLOAD_FAILED",
             reason.strip()[:2000], stage.strip(), screenshot_path.strip(),
             1 if submit_attempted else 0, timestamp, timestamp, job_id),
        )
        _event(connection, job_id, stage.strip(), status, reason.strip(), screenshot_path.strip(), {
            "submitAttempted": bool(submit_attempted), "uncertain": bool(uncertain),
            "quotaHeld": needs_review,
        })
        connection.commit()
        return {"ok": True, "job": _job_payload(_job(connection, job_id)), "quotaHeld": needs_review}
    except Exception:
        connection.rollback()
        raise


def recover_interrupted(
    connection: sqlite3.Connection, older_than_minutes: int = 30,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """隔离失去执行器的旧 uploading；不把未知远端结果重新排队。"""
    if older_than_minutes < 0:
        raise ValueError("等待分钟数不能为负")
    cutoff = (now or datetime.now()) - timedelta(minutes=older_than_minutes)
    connection.commit()
    connection.execute("BEGIN IMMEDIATE")
    try:
        rows = connection.execute(
            "SELECT id FROM qianchuan_upload_jobs WHERE status = 'uploading' "
            "AND started_at IS NOT NULL AND started_at <= ? ORDER BY id",
            (cutoff.isoformat(timespec="seconds"),),
        ).fetchall()
        recovered = [int(row["id"]) for row in rows]
        for job_id in recovered:
            connection.execute(
                "UPDATE qianchuan_upload_jobs SET status = 'needs_review', "
                "error_code = 'PROCESS_INTERRUPTED', "
                "error_message = '上传执行器中断；远端结果未确认，须核查后处理', "
                "failure_stage = 'interrupted', completed_at = ?, updated_at = ? WHERE id = ?",
                (now_text(), now_text(), job_id),
            )
            _event(connection, job_id, "interrupted", "needs_review",
                   "上传执行器中断；远端结果未确认，禁止自动重传",
                   details={"quotaHeld": True, "olderThanMinutes": older_than_minutes})
        connection.commit()
        return {"ok": True, "recoveredJobIds": recovered, "count": len(recovered)}
    except Exception:
        connection.rollback()
        raise


def retry_safe(
    connection: sqlite3.Connection, job_id: int, today: Optional[date] = None,
) -> Dict[str, Any]:
    """仅明确未触及远端写入的失败允许人工重排，待核查不可走此路径。"""
    today = today or date.today()
    connection.commit()
    connection.execute("BEGIN IMMEDIATE")
    try:
        row = _job(connection, job_id)
        if row["status"] != "failed" or int(row["submit_attempted"]):
            raise ValueError("仅明确未开始平台写入的失败任务可重排；待核查须先人工核实远端")
        account_id = int(row["account_id"])
        _target(connection, account_id, int(row["qianchuan_account_id"]))
        _validated_asset(connection, int(row["asset_id"]), account_id)
        scheduled = _reserve_date(connection, account_id, int(row["quota_group_id"]),
                                  str(row["product_id"]), today)
        connection.execute(
            "UPDATE qianchuan_upload_jobs SET status = 'queued', scheduled_date = ?, "
            "error_code = '', error_message = '', failure_stage = '', screenshot_path = '', "
            "started_at = NULL, completed_at = NULL, updated_at = ? WHERE id = ?",
            (scheduled.isoformat(), now_text(), job_id),
        )
        _event(connection, job_id, "manual_retry", "queued", details={
            "scheduledDate": scheduled.isoformat(), "remoteWriteAttempted": False,
        })
        connection.commit()
        return {"ok": True, "job": _job_payload(_job(connection, job_id))}
    except Exception:
        connection.rollback()
        raise


def resolve_review_absent(
    connection: sqlite3.Connection, job_id: int, note: str,
    today: Optional[date] = None,
) -> Dict[str, Any]:
    """人工核对千川确无素材后，才解除未知提交隔离并重新按额度排队。"""
    if len(note.strip()) < 6:
        raise ValueError("请写明已如何核实千川没有该素材（至少 6 字）")
    today = today or date.today()
    connection.commit()
    connection.execute("BEGIN IMMEDIATE")
    try:
        row = _job(connection, job_id)
        if row["status"] != "needs_review":
            raise ValueError("仅待核查任务可按人工确认未上传重新排队")
        account_id = int(row["account_id"])
        _target(connection, account_id, int(row["qianchuan_account_id"]))
        _validated_asset(connection, int(row["asset_id"]), account_id)
        # 原任务仍占用当日额度；本事务中先释放，再对新日期重新预占。
        connection.execute("UPDATE qianchuan_upload_jobs SET status = 'failed' WHERE id = ?", (job_id,))
        scheduled = _reserve_date(connection, account_id, int(row["quota_group_id"]),
                                  str(row["product_id"]), today)
        timestamp = now_text()
        connection.execute(
            "UPDATE qianchuan_upload_jobs SET status = 'queued', scheduled_date = ?, "
            "error_code = '', error_message = '', failure_stage = '', screenshot_path = '', "
            "submit_attempted = 0, started_at = NULL, completed_at = NULL, updated_at = ? "
            "WHERE id = ?",
            (scheduled.isoformat(), timestamp, job_id),
        )
        _event(connection, job_id, "manual_review", "queued", note.strip(), details={
            "confirmedRemoteAbsent": True, "scheduledDate": scheduled.isoformat(),
        })
        connection.commit()
        return {"ok": True, "job": _job_payload(_job(connection, job_id))}
    except Exception:
        connection.rollback()
        raise


def list_jobs(
    connection: sqlite3.Connection, account_id: Optional[int] = None,
    status: Optional[str] = None, limit: int = 200,
) -> Dict[str, Any]:
    where: List[str] = []
    params: List[Any] = []
    if account_id is not None:
        where.append("account_id = ?")
        params.append(account_id)
    if status:
        where.append("status = ?")
        params.append(status)
    sql = "SELECT * FROM qianchuan_upload_jobs"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    rows = connection.execute(sql, (*params, min(max(limit, 1), 1000))).fetchall()
    return {"ok": True, "rows": [_job_payload(row) for row in rows]}


def list_events(connection: sqlite3.Connection, job_id: int) -> Dict[str, Any]:
    _job(connection, job_id)
    rows = connection.execute(
        "SELECT * FROM qianchuan_upload_events WHERE job_id = ? ORDER BY id", (job_id,)
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        try:
            item["details"] = json.loads(item.pop("details_json"))
        except json.JSONDecodeError:
            item["details"] = {}
        result.append(item)
    return {"ok": True, "jobId": job_id, "rows": result}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="千川素材上传本地队列与配额")
    parser.add_argument("--db", type=Path, default=Path(DEFAULT_DB_PATH))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("targets", help="列出抖音账号与 Step 11 千川绑定")
    assets = commands.add_parser("eligible-assets", help="列出本账号网页下载素材及资格原因")
    assets.add_argument("--account-id", type=int, required=True)
    assets.add_argument("--limit", type=int, default=200)
    add = commands.add_parser("enqueue", help="批量预占配额并登记上传任务")
    add.add_argument("--account-id", type=int, required=True)
    add.add_argument("--asset-ids", required=True, help="逗号分隔的素材 ID")
    add.add_argument("--qianchuan-account-id", type=int)
    lock = commands.add_parser("claim", help="原子锁定单条到期任务；禁止重复上传")
    lock.add_argument("--id", type=int, required=True)
    one = commands.add_parser("job", help="只读查询单条任务，供浏览器前置检查")
    one.add_argument("--id", type=int, required=True)
    done = commands.add_parser("complete", help="远端 ID 回读后标记成功")
    done.add_argument("--id", type=int, required=True)
    done.add_argument("--remote-material-id", required=True)
    failure = commands.add_parser("fail", help="登记失败；提交后不确定自动隔离待核查")
    failure.add_argument("--id", type=int, required=True)
    failure.add_argument("--reason", required=True)
    failure.add_argument("--stage", required=True)
    failure.add_argument("--screenshot-path", default="")
    failure.add_argument("--submit-attempted", action="store_true")
    failure.add_argument("--uncertain", action="store_true")
    interrupted = commands.add_parser("recover-interrupted", help="显式隔离超时的上传中任务，保留配额待人工核查")
    interrupted.add_argument("--older-than-minutes", type=int, default=30)
    retry = commands.add_parser("retry-safe", help="人工重排明确未触及远端写入的失败任务")
    retry.add_argument("--id", type=int, required=True)
    review = commands.add_parser("resolve-review-absent", help="人工核对远端无素材后解除隔离并重排")
    review.add_argument("--id", type=int, required=True)
    review.add_argument("--note", required=True)
    jobs = commands.add_parser("jobs", help="查看上传任务及状态")
    jobs.add_argument("--account-id", type=int)
    jobs.add_argument("--status")
    jobs.add_argument("--limit", type=int, default=200)
    events = commands.add_parser("events", help="查看单条任务的 Step 14 过程日志")
    events.add_argument("--id", type=int, required=True)
    event = commands.add_parser("event", help="记录浏览器步骤和截图证据")
    event.add_argument("--id", type=int, required=True)
    event.add_argument("--stage", required=True)
    event.add_argument("--outcome", required=True)
    event.add_argument("--reason", default="")
    event.add_argument("--screenshot-path", default="")
    quota = commands.add_parser("quota-config", help="读取/调整千川独立配额；账号 0 是默认")
    quota.add_argument("--account-id", type=int, default=0)
    quota.add_argument("--per-product-videos", type=int)
    quota.add_argument("--per-account-products", type=int)
    quota.add_argument("--per-account-videos", type=int)
    quota.add_argument("--per-group-products", type=int)
    quota.add_argument("--per-group-videos", type=int)
    quota.add_argument("--max-concurrent-uploads", type=int)
    quota.add_argument("--min-step-delay-ms", type=int)
    quota.add_argument("--max-step-delay-ms", type=int)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        with connect(args.db) as connection:
            ensure_schema(connection)
            if args.command == "targets":
                result = list_targets(connection)
            elif args.command == "eligible-assets":
                result = eligible_assets(connection, args.account_id, args.limit)
            elif args.command == "enqueue":
                ids = [int(value.strip()) for value in args.asset_ids.split(",") if value.strip()]
                result = enqueue(connection, args.account_id, ids, args.qianchuan_account_id)
            elif args.command == "claim":
                result = claim(connection, args.id)
            elif args.command == "job":
                result = {"ok": True, "job": _job_payload(_job(connection, args.id))}
            elif args.command == "complete":
                result = complete(connection, args.id, args.remote_material_id)
            elif args.command == "fail":
                result = fail(connection, args.id, args.reason, args.stage,
                              args.screenshot_path, args.submit_attempted, args.uncertain)
            elif args.command == "recover-interrupted":
                result = recover_interrupted(connection, args.older_than_minutes)
            elif args.command == "retry-safe":
                result = retry_safe(connection, args.id)
            elif args.command == "resolve-review-absent":
                result = resolve_review_absent(connection, args.id, args.note)
            elif args.command == "jobs":
                result = list_jobs(connection, args.account_id, args.status, args.limit)
            elif args.command == "events":
                result = list_events(connection, args.id)
            elif args.command == "event":
                result = record_event(connection, args.id, args.stage, args.outcome,
                                      args.reason, args.screenshot_path)
            elif args.command == "quota-config":
                keys = {
                    "per_product_videos": "perProductVideos",
                    "per_account_products": "perAccountProducts",
                    "per_account_videos": "perAccountVideos",
                    "per_group_products": "perGroupProducts",
                    "per_group_videos": "perGroupVideos",
                    "max_concurrent_uploads": "maxConcurrentUploads",
                    "min_step_delay_ms": "minStepDelayMs",
                    "max_step_delay_ms": "maxStepDelayMs",
                }
                updates = {key: getattr(args, attr) for attr, key in keys.items()
                           if getattr(args, attr) is not None}
                result = quota_config(connection, args.account_id, updates or None)
            else:
                return emit({"ok": False, "error": "未知命令"}, 2)
        # 次日额度重排是可观测的排队结论，供主进程继续显示 scheduledDate。
        return emit(result, 0 if result.get("ok") or result.get("deferred") else 1)
    except (ValueError, OSError, sqlite3.Error) as error:
        return emit({"ok": False, "error": str(error)}, 1)


if __name__ == "__main__":
    sys.exit(main())
