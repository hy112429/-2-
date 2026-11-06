#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发布成功后的千川素材编排状态；只存本机状态，不访问平台。"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from db import DEFAULT_DB_PATH, connect, emit, now_text

STATUSES = {
    "running", "disabled", "blocked_auth", "blocked_binding", "skipped_no_video",
    "skipped_quota", "failed_query", "failed_download", "needs_review", "enqueued",
}
RETRYABLE = {
    "disabled", "blocked_auth", "blocked_binding", "skipped_no_video", "skipped_quota",
    "failed_query", "failed_download", "needs_review",
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


def list_runs(connection: sqlite3.Connection, limit: int = 30) -> Dict[str, Any]:
    bounded = min(max(int(limit), 1), 200)
    rows = connection.execute(
        "SELECT r.*, p.product_name FROM post_publish_material_runs r "
        "JOIN publish_queue p ON p.id = r.publish_queue_id "
        "ORDER BY r.updated_at DESC, r.publish_queue_id DESC LIMIT ?", (bounded,)
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
            else:
                raise ValueError("未知命令")
        return emit(result)
    except (ValueError, OSError, sqlite3.Error, json.JSONDecodeError) as error:
        return emit({"ok": False, "error": str(error)}, 1)


if __name__ == "__main__":
    sys.exit(main())
