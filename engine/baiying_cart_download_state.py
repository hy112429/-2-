#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""百应确认加车后的同款视频下载状态；不访问平台。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from db import DEFAULT_DB_PATH, connect, now_text


SCHEMA = """
CREATE TABLE IF NOT EXISTS baiying_cart_video_downloads (
    account_id INTEGER NOT NULL,
    product_id TEXT NOT NULL,
    commodity_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'queued',
    target_count INTEGER NOT NULL DEFAULT 10,
    unique_count INTEGER NOT NULL DEFAULT 0,
    available_count INTEGER NOT NULL DEFAULT 0,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    next_retry_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (account_id, product_id)
);
CREATE INDEX IF NOT EXISTS idx_baiying_cart_video_downloads_due
    ON baiying_cart_video_downloads(status, next_retry_at);
"""


def ensure_schema(db: sqlite3.Connection) -> None:
    db.executescript(SCHEMA)
    db.commit()


def payload(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "accountId": row["account_id"], "productId": row["product_id"],
        "commodityId": row["commodity_id"], "status": row["status"],
        "targetCount": row["target_count"], "uniqueCount": row["unique_count"],
        "availableCount": row["available_count"], "attemptCount": row["attempt_count"],
        "error": row["error"], "nextRetryAt": row["next_retry_at"],
        "updatedAt": row["updated_at"],
    }


def get(db: sqlite3.Connection, account_id: int, product_id: str) -> dict[str, Any]:
    row = db.execute(
        "SELECT * FROM baiying_cart_video_downloads WHERE account_id=? AND product_id=?",
        (account_id, product_id),
    ).fetchone()
    return {"ok": True, "job": payload(row) if row else None}


def schedule(db: sqlite3.Connection, account_id: int, product_id: str,
             commodity_id: str, target_count: int) -> dict[str, Any]:
    if account_id < 1 or not product_id.isdigit() or not 8 <= len(product_id) <= 20:
        raise ValueError("加车下载的账号或商品 ID 无效")
    if target_count < 10 or target_count > 500:
        raise ValueError("每商品同款视频目标应为 10–500 条")
    timestamp = now_text()
    db.execute(
        "INSERT INTO baiying_cart_video_downloads "
        "(account_id, product_id, commodity_id, target_count, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(account_id, product_id) DO UPDATE SET "
        "commodity_id=CASE WHEN excluded.commodity_id<>'' THEN excluded.commodity_id "
        "ELSE commodity_id END, "
        "status=CASE WHEN excluded.target_count>target_count "
        "AND status IN ('completed','exhausted') THEN 'queued' ELSE status END, "
        "next_retry_at=CASE WHEN excluded.target_count>target_count THEN '' ELSE next_retry_at END, "
        "target_count=MAX(target_count, excluded.target_count), updated_at=excluded.updated_at",
        (account_id, product_id, commodity_id, target_count, timestamp, timestamp),
    )
    db.commit()
    return get(db, account_id, product_id)


def start(db: sqlite3.Connection, account_id: int, product_id: str) -> dict[str, Any]:
    db.commit()
    db.execute("BEGIN IMMEDIATE")
    try:
        row = db.execute(
            "SELECT * FROM baiying_cart_video_downloads WHERE account_id=? AND product_id=?",
            (account_id, product_id),
        ).fetchone()
        if row is None:
            raise ValueError("加车下载任务尚未登记")
        if row["status"] == "completed" and row["unique_count"] >= row["target_count"]:
            db.commit()
            return {"ok": True, "started": False, "job": payload(row)}
        if row["status"] == "running":
            updated = datetime.fromisoformat(row["updated_at"])
            if datetime.now() - updated < timedelta(minutes=30):
                db.commit()
                return {"ok": True, "started": False, "job": payload(row)}
        timestamp = now_text()
        db.execute(
            "UPDATE baiying_cart_video_downloads SET status='running', "
            "attempt_count=attempt_count+1, error='', next_retry_at='', updated_at=? "
            "WHERE account_id=? AND product_id=?",
            (timestamp, account_id, product_id),
        )
        db.commit()
        return {"ok": True, "started": True, **get(db, account_id, product_id)}
    except Exception:
        db.rollback()
        raise


def finish(db: sqlite3.Connection, account_id: int, product_id: str,
           unique_count: int, available_count: int, error: str,
           source_exhausted: bool = False) -> dict[str, Any]:
    row = db.execute(
        "SELECT * FROM baiying_cart_video_downloads WHERE account_id=? AND product_id=?",
        (account_id, product_id),
    ).fetchone()
    if row is None or row["status"] != "running":
        raise ValueError("加车下载任务不在运行状态")
    target = int(row["target_count"])
    if unique_count < 0 or available_count < 0:
        raise ValueError("视频数量无效")
    if unique_count >= target:
        status, next_retry, error = "completed", "", ""
    elif error:
        status = "failed"
        minutes = (5, 15, 60)[min(int(row["attempt_count"]) - 1, 2)]
        next_retry = (datetime.now() + timedelta(minutes=minutes)).isoformat(timespec="seconds")
    elif source_exhausted or available_count < target:
        status, next_retry = "exhausted", ""
    else:
        status = "partial"
        next_retry = (datetime.now() + timedelta(minutes=30)).isoformat(timespec="seconds")
    db.execute(
        "UPDATE baiying_cart_video_downloads SET status=?, unique_count=?, available_count=?, "
        "error=?, next_retry_at=?, updated_at=? WHERE account_id=? AND product_id=?",
        (status, unique_count, available_count, error[:1800], next_retry, now_text(),
         account_id, product_id),
    )
    db.commit()
    return get(db, account_id, product_id)


def due(db: sqlite3.Connection, limit: int = 20) -> dict[str, Any]:
    stale_cutoff = (datetime.now() - timedelta(minutes=30)).isoformat(timespec="seconds")
    rows = db.execute(
        "SELECT * FROM baiying_cart_video_downloads "
        "WHERE status='queued' OR (status IN ('failed','partial') AND next_retry_at<=?) "
        "OR (status='running' AND updated_at<=?) "
        "ORDER BY updated_at LIMIT ?", (now_text(), stale_cutoff, min(max(limit, 1), 100)),
    ).fetchall()
    return {"ok": True, "rows": [payload(row) for row in rows]}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=DEFAULT_DB_PATH)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("get", "schedule", "start", "finish"):
        command = commands.add_parser(name)
        command.add_argument("--account-id", type=int, required=True)
        command.add_argument("--product-id", required=True)
        if name == "schedule":
            command.add_argument("--commodity-id", default="")
            command.add_argument("--target-count", type=int, required=True)
        if name == "finish":
            command.add_argument("--unique-count", type=int, required=True)
            command.add_argument("--available-count", type=int, required=True)
            command.add_argument("--error", default="")
            command.add_argument("--source-exhausted", action="store_true")
    due_command = commands.add_parser("due")
    due_command.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()
    try:
        with connect(Path(args.db)) as db:
            ensure_schema(db)
            if args.command == "due":
                result = due(db, args.limit)
            elif args.command == "get":
                result = get(db, args.account_id, args.product_id)
            elif args.command == "schedule":
                result = schedule(db, args.account_id, args.product_id,
                                  args.commodity_id, args.target_count)
            elif args.command == "start":
                result = start(db, args.account_id, args.product_id)
            else:
                result = finish(db, args.account_id, args.product_id,
                                args.unique_count, args.available_count, args.error,
                                args.source_exhausted)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return 0
    except Exception as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
