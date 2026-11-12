#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发布成功后的千川素材编排状态；只存本机状态，不访问平台。"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from db import DEFAULT_DB_PATH, connect, emit, now_text

STATUSES = {
    "running", "disabled", "blocked_auth", "blocked_binding", "skipped_no_video",
    "skipped_quota", "failed_query", "failed_download", "needs_review", "enqueued",
    "awaiting_upload", "needs_fill", "candidate_exhausted", "completed",
}
RETRYABLE = {
    "disabled", "blocked_auth", "blocked_binding", "skipped_no_video", "skipped_quota",
    "failed_query", "failed_download", "needs_review", "needs_fill", "candidate_exhausted",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS post_publish_material_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    enabled INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS post_publish_material_runs (
    publish_queue_id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL,
    product_id TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    attempt_count INTEGER NOT NULL DEFAULT 1,
    video_count INTEGER NOT NULL DEFAULT 0,
    asset_ids_json TEXT NOT NULL DEFAULT '[]',
    upload_job_ids_json TEXT NOT NULL DEFAULT '[]',
    trigger_source TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_post_publish_material_runs_updated
    ON post_publish_material_runs(updated_at DESC);
CREATE TABLE IF NOT EXISTS post_publish_material_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    publish_queue_id INTEGER NOT NULL,
    attempt_count INTEGER NOT NULL,
    status TEXT NOT NULL,
    stage TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_post_publish_material_events_queue
    ON post_publish_material_events(publish_queue_id, id);
"""


def ensure_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)
    connection.execute(
        "INSERT OR IGNORE INTO post_publish_material_settings(id, enabled, updated_at) VALUES (1, 1, ?)",
        (now_text(),),
    )
    connection.commit()


def _settings(connection: sqlite3.Connection) -> Dict[str, Any]:
    row = connection.execute(
        "SELECT enabled, updated_at FROM post_publish_material_settings WHERE id = 1"
    ).fetchone()
    return {"ok": True, "enabled": bool(row["enabled"]), "updatedAt": row["updated_at"]}


def settings_set(connection: sqlite3.Connection, enabled: bool) -> Dict[str, Any]:
    connection.execute(
        "UPDATE post_publish_material_settings SET enabled = ?, updated_at = ? WHERE id = 1",
        (int(enabled), now_text()),
    )
    connection.commit()
    return _settings(connection)


def _published_task(connection: sqlite3.Connection, queue_id: int) -> sqlite3.Row:
    row = connection.execute(
        "SELECT id, account_id, product_id, product_name, platform_status, published_at "
        "FROM publish_queue WHERE id = ?", (queue_id,),
    ).fetchone()
    if not row:
        raise ValueError(f"发布队列任务不存在：{queue_id}")
    if str(row["platform_status"]) != "published":
        raise ValueError("只有 platform_status=published 的任务可以启动发布后千川编排")
    if not str(row["product_id"] or "").strip():
        raise ValueError("发布队列任务缺少商品 ID")
    return row


def _record_event(
    connection: sqlite3.Connection, queue_id: int, attempt: int, status: str,
    stage: str, reason: str = "", details: Optional[Dict[str, Any]] = None,
) -> None:
    connection.execute(
        "INSERT INTO post_publish_material_events "
        "(publish_queue_id, attempt_count, status, stage, reason, details_json, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (queue_id, attempt, status, stage, reason[:2000],
         json.dumps(details or {}, ensure_ascii=False), now_text()),
    )


def _payload(row: sqlite3.Row) -> Dict[str, Any]:
    try:
        asset_ids = json.loads(row["asset_ids_json"] or "[]")
    except (TypeError, json.JSONDecodeError):
        asset_ids = []
    try:
        upload_ids = json.loads(row["upload_job_ids_json"] or "[]")
    except (TypeError, json.JSONDecodeError):
        upload_ids = []
    return {
        "publishQueueId": int(row["publish_queue_id"]),
        "accountId": int(row["account_id"]),
        "productId": str(row["product_id"]),
        "productName": str(row["product_name"] or ""),
        "status": str(row["status"]),
        "reason": str(row["reason"] or ""),
        "attemptCount": int(row["attempt_count"]),
        "videoCount": int(row["video_count"]),
        "assetIds": asset_ids if isinstance(asset_ids, list) else [],
        "uploadJobIds": upload_ids if isinstance(upload_ids, list) else [],
        "triggerSource": str(row["trigger_source"] or ""),
        "createdAt": str(row["created_at"]),
        "updatedAt": str(row["updated_at"]),
        "canRetry": str(row["status"]) in RETRYABLE,
    }


def begin(connection: sqlite3.Connection, queue_id: int, trigger_source: str) -> Dict[str, Any]:
    task = _published_task(connection, queue_id)
    connection.commit()
    connection.execute("BEGIN IMMEDIATE")
    try:
        existing = connection.execute(
            "SELECT r.*, p.product_name FROM post_publish_material_runs r "
            "JOIN publish_queue p ON p.id = r.publish_queue_id WHERE r.publish_queue_id = ?", (queue_id,)
        ).fetchone()
        if existing:
            status = str(existing["status"])
            # 等待上传/补位/候选耗尽只能由 reconcile 在检测到外部进展后恢复；
            # 重复的发布成功通知必须保持幂等，不能再次增加 attempt_count。
            resumable = status == "enqueued" or (
                trigger_source == "reconcile" and
                status in {"awaiting_upload", "needs_fill", "candidate_exhausted"}
            )
            if status == "running":
                resumable = datetime.now() - datetime.fromisoformat(existing["updated_at"]) >= timedelta(minutes=30)
            if resumable:
                enabled = bool(_settings(connection)["enabled"])
                new_status = "running" if enabled else "disabled"
                timestamp = now_text()
                connection.execute(
                    "UPDATE post_publish_material_runs SET status=?, attempt_count=attempt_count+1, "
                    "trigger_source=?, updated_at=? WHERE publish_queue_id=?",
                    (new_status, trigger_source[:80], timestamp, queue_id),
                )
                _record_event(connection, queue_id, int(existing["attempt_count"]) + 1,
                              new_status, "reconcile", "", {"trigger": trigger_source})
                refreshed = connection.execute(
                    "SELECT r.*, p.product_name FROM post_publish_material_runs r "
                    "JOIN publish_queue p ON p.id=r.publish_queue_id WHERE r.publish_queue_id=?",
                    (queue_id,),
                ).fetchone()
                connection.commit()
                return {"ok": True, "shouldRun": enabled, "alreadyStarted": False,
                        "run": _payload(refreshed)}
            connection.commit()
            return {"ok": True, "shouldRun": False, "alreadyStarted": True, "run": _payload(existing)}
        enabled = bool(_settings(connection)["enabled"])
        status = "running" if enabled else "disabled"
        timestamp = now_text()
        connection.execute(
            "INSERT INTO post_publish_material_runs "
            "(publish_queue_id, account_id, product_id, status, reason, attempt_count, trigger_source, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)",
            (queue_id, int(task["account_id"]), str(task["product_id"]), status,
             "" if enabled else "全局自动下视频开关已关闭", trigger_source[:80], timestamp, timestamp),
        )
        _record_event(connection, queue_id, 1, status, "trigger", "", {"trigger": trigger_source})
        run = connection.execute(
            "SELECT r.*, p.product_name FROM post_publish_material_runs r "
            "JOIN publish_queue p ON p.id = r.publish_queue_id WHERE r.publish_queue_id = ?", (queue_id,)
        ).fetchone()
        connection.commit()
        return {"ok": True, "shouldRun": enabled, "alreadyStarted": False, "run": _payload(run)}
    except Exception:
        connection.rollback()
        raise


def retry(connection: sqlite3.Connection, queue_id: int, trigger_source: str = "manual_retry") -> Dict[str, Any]:
    task = _published_task(connection, queue_id)
    connection.commit()
    connection.execute("BEGIN IMMEDIATE")
    try:
        existing = connection.execute(
            "SELECT * FROM post_publish_material_runs WHERE publish_queue_id = ?", (queue_id,)
        ).fetchone()
        if not existing:
            connection.rollback()
            return begin(connection, queue_id, trigger_source)
        current_status = str(existing["status"])
        if current_status == "enqueued":
            raise ValueError("素材已进入 Step17 上传队列，不可重复启动")
        if current_status == "running":
            raise ValueError("该任务仍在处理中；请等状态更新后再重试")
        if current_status not in RETRYABLE:
            raise ValueError(f"当前状态 {current_status} 不支持重试")
        attempt = int(existing["attempt_count"]) + 1
        enabled = bool(_settings(connection)["enabled"])
        status = "running" if enabled else "disabled"
        reason = "" if enabled else "全局自动下视频开关已关闭"
        timestamp = now_text()
        connection.execute(
            "UPDATE post_publish_material_runs SET account_id = ?, product_id = ?, status = ?, reason = ?, "
            "attempt_count = ?, video_count = 0, asset_ids_json = '[]', upload_job_ids_json = '[]', "
            "trigger_source = ?, updated_at = ? WHERE publish_queue_id = ?",
            (int(task["account_id"]), str(task["product_id"]), status, reason, attempt,
             trigger_source[:80], timestamp, queue_id),
        )
        _record_event(connection, queue_id, attempt, status, "retry", reason, {"trigger": trigger_source})
        run = connection.execute(
            "SELECT r.*, p.product_name FROM post_publish_material_runs r "
            "JOIN publish_queue p ON p.id = r.publish_queue_id WHERE r.publish_queue_id = ?", (queue_id,)
        ).fetchone()
        connection.commit()
        return {"ok": True, "shouldRun": enabled, "alreadyStarted": False, "run": _payload(run)}
    except Exception:
        connection.rollback()
        raise


def update_run(
    connection: sqlite3.Connection, queue_id: int, status: str, reason: str = "",
    video_count: int = 0, asset_ids: Optional[List[int]] = None,
    upload_job_ids: Optional[List[int]] = None, stage: str = "",
) -> Dict[str, Any]:
    if status not in STATUSES:
        raise ValueError("未知的发布后素材状态")
    if video_count < 0 or video_count > 5000:
        raise ValueError("视频条数超出范围")
    task = connection.execute(
        "SELECT publish_queue_id, attempt_count FROM post_publish_material_runs WHERE publish_queue_id = ?",
        (queue_id,),
    ).fetchone()
    if not task:
        raise ValueError(f"发布后素材任务不存在：{queue_id}")
    assets = [int(item) for item in (asset_ids or []) if int(item) > 0]
    jobs = [int(item) for item in (upload_job_ids or []) if int(item) > 0]
    timestamp = now_text()
    connection.execute(
        "UPDATE post_publish_material_runs SET status = ?, reason = ?, video_count = ?, "
        "asset_ids_json = ?, upload_job_ids_json = ?, updated_at = ? WHERE publish_queue_id = ?",
        (status, reason[:2000], video_count, json.dumps(assets), json.dumps(jobs), timestamp, queue_id),
    )
    _record_event(connection, queue_id, int(task["attempt_count"]), status,
                  stage or status, reason, {"videoCount": video_count, "assetIds": assets, "uploadJobIds": jobs})
    connection.commit()
    row = connection.execute(
        "SELECT r.*, p.product_name FROM post_publish_material_runs r "
        "JOIN publish_queue p ON p.id = r.publish_queue_id WHERE r.publish_queue_id = ?", (queue_id,)
    ).fetchone()
    return {"ok": True, "run": _payload(row)}


def get_run(connection: sqlite3.Connection, queue_id: int) -> Dict[str, Any]:
    row = connection.execute(
        "SELECT r.*, p.product_name FROM post_publish_material_runs r "
        "JOIN publish_queue p ON p.id = r.publish_queue_id WHERE r.publish_queue_id = ?", (queue_id,)
    ).fetchone()
    return {"ok": True, "run": _payload(row) if row else None}


def _with_cart_state(connection: sqlite3.Connection, row: sqlite3.Row) -> Dict[str, Any]:
    item = _payload(row)
    table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='baiying_cart_video_downloads'"
    ).fetchone()
    if table:
        cart = connection.execute(
            "SELECT status, target_count, unique_count, available_count, error, next_retry_at "
            "FROM baiying_cart_video_downloads WHERE account_id=? AND product_id=?",
            (item["accountId"], item["productId"]),
        ).fetchone()
        if cart:
            item["cartDownload"] = {
                "status": cart["status"], "targetCount": cart["target_count"],
                "uniqueCount": cart["unique_count"], "availableCount": cart["available_count"],
                "error": cart["error"], "nextRetryAt": cart["next_retry_at"],
            }
    return item


def list_runs(connection: sqlite3.Connection, limit: int = 30) -> Dict[str, Any]:
    bounded = min(max(int(limit), 1), 200)
    rows = connection.execute(
        "SELECT r.*, p.product_name FROM post_publish_material_runs r "
        "JOIN publish_queue p ON p.id = r.publish_queue_id "
        "ORDER BY r.updated_at DESC, r.publish_queue_id DESC LIMIT ?", (bounded,)
    ).fetchall()
    return {"ok": True, "rows": [_with_cart_state(connection, row) for row in rows]}


def due_runs(connection: sqlite3.Connection, limit: int = 30) -> Dict[str, Any]:
    """只恢复确实有外部进展的发布后素材编排。

    ``awaiting_upload`` 和 ``needs_fill`` 是等待态，不是失败态。旧逻辑只要
    状态超过一分钟就再次调用 ``begin``，导致每分钟把 ``attempt_count`` 加一，
    即使上传队列和百应下载都没有任何变化也会形成重试风暴。这里要求对应的
    上传任务或百应下载状态在本次编排之后有更新，才把任务交给 reconcile。
    """
    cutoff = (datetime.now() - timedelta(seconds=60)).isoformat(timespec="seconds")
    settings = _settings(connection)
    # 绑定修复后自动恢复一次之前被挡住的任务。必须是启用账号、唯一关联、
    # 广告主 ID 为 8-20 位纯数字；自动下载总开关关闭时绝不触发。
    valid_binding = (
        "EXISTS (SELECT 1 FROM accounts a "
        "JOIN account_qianchuan aq ON aq.account_row_id=a.id "
        "JOIN qianchuan_accounts q ON q.id=aq.qianchuan_row_id "
        "WHERE a.id=r.account_id AND a.status='active' "
        "AND length(trim(coalesce(q.qc_id,''))) BETWEEN 8 AND 20 "
        "AND trim(q.qc_id) NOT GLOB '*[^0-9]*' "
        "AND NOT EXISTS (SELECT 1 FROM account_qianchuan aq2 "
        "WHERE aq2.account_row_id=a.id AND aq2.id<>aq.id))"
    )
    has_upload_jobs = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='qianchuan_upload_jobs'"
    ).fetchone() is not None
    upload_changed = (
        "EXISTS (SELECT 1 FROM qianchuan_upload_jobs j "
        "WHERE j.account_id=r.account_id AND j.product_id=r.product_id "
        "AND j.updated_at>r.updated_at)"
        if has_upload_jobs else "0"
    )
    has_cart_downloads = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='baiying_cart_video_downloads'"
    ).fetchone() is not None
    cart_changed = (
        "EXISTS (SELECT 1 FROM baiying_cart_video_downloads d "
        "WHERE d.account_id=r.account_id AND d.product_id=r.product_id "
        "AND d.updated_at>r.updated_at)"
        if has_cart_downloads else "0"
    )
    rows = connection.execute(
        "SELECT r.*, p.product_name FROM post_publish_material_runs r "
        "JOIN publish_queue p ON p.id=r.publish_queue_id "
        "WHERE p.platform_status='published' AND ((r.status IN ('enqueued','awaiting_upload') "
        f"AND {upload_changed}) "
        f"OR (r.status='needs_fill' AND ({upload_changed} OR {cart_changed})) "
        f"OR (r.status='candidate_exhausted' AND {upload_changed}) "
        f"OR (r.status='blocked_binding' AND r.attempt_count < 2 AND {valid_binding} AND ?=1)) "
        "AND r.updated_at<=? ORDER BY r.updated_at LIMIT ?",
        (int(settings["enabled"]), cutoff, min(max(limit, 1), 100)),
    ).fetchall()
    return {"ok": True, "rows": [_payload(row) for row in rows]}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="发布成功后的千川素材本机状态")
    parser.add_argument("--db", type=Path, default=Path(DEFAULT_DB_PATH))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("settings-get")
    setting = commands.add_parser("settings-set")
    setting.add_argument("--enabled", type=int, choices=(0, 1), required=True)
    start = commands.add_parser("begin")
    start.add_argument("--publish-queue-id", type=int, required=True)
    start.add_argument("--trigger", default="published")
    again = commands.add_parser("retry")
    again.add_argument("--publish-queue-id", type=int, required=True)
    again.add_argument("--trigger", default="manual_retry")
    update = commands.add_parser("update")
    update.add_argument("--publish-queue-id", type=int, required=True)
    update.add_argument("--status", choices=sorted(STATUSES), required=True)
    update.add_argument("--reason", default="")
    update.add_argument("--video-count", type=int, default=0)
    update.add_argument("--asset-ids-json", default="[]")
    update.add_argument("--upload-job-ids-json", default="[]")
    update.add_argument("--stage", default="")
    get = commands.add_parser("get")
    get.add_argument("--publish-queue-id", type=int, required=True)
    listing = commands.add_parser("list")
    listing.add_argument("--limit", type=int, default=30)
    due = commands.add_parser("due")
    due.add_argument("--limit", type=int, default=30)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        with connect(args.db) as connection:
            ensure_schema(connection)
            if args.command == "settings-get":
                result = _settings(connection)
            elif args.command == "settings-set":
                result = settings_set(connection, bool(args.enabled))
            elif args.command == "begin":
                result = begin(connection, args.publish_queue_id, args.trigger)
            elif args.command == "retry":
                result = retry(connection, args.publish_queue_id, args.trigger)
            elif args.command == "update":
                assets = json.loads(args.asset_ids_json)
                jobs = json.loads(args.upload_job_ids_json)
                if not isinstance(assets, list) or not isinstance(jobs, list):
                    raise ValueError("素材 ID 与上传任务 ID 必须是 JSON 数组")
                result = update_run(connection, args.publish_queue_id, args.status, args.reason,
                                    args.video_count, assets, jobs, args.stage)
            elif args.command == "get":
                result = get_run(connection, args.publish_queue_id)
            elif args.command == "list":
                result = list_runs(connection, args.limit)
            elif args.command == "due":
                result = due_runs(connection, args.limit)
            else:
                raise ValueError("未知命令")
        return emit(result)
    except (ValueError, OSError, sqlite3.Error, json.JSONDecodeError) as error:
        return emit({"ok": False, "error": str(error)}, 1)


if __name__ == "__main__":
    sys.exit(main())
