#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""千川商品全域投放计划：账户配置、发布准入与一次性提交队列。"""

from __future__ import annotations

import argparse
import re
import sqlite3
from pathlib import Path
from datetime import datetime, timedelta
from typing import Any

from db import DEFAULT_DB_PATH, connect, emit, now_text


PRODUCT_ID = re.compile(r"^\d{1,20}$")
ADVERTISER_ID = re.compile(r"^\d{1,20}$")
REMOTE_PLAN_ID = re.compile(r"^[A-Za-z0-9_-]{6,64}$")
SOURCES = ("homepage", "uploaded", "both")
SCHEMA = """
CREATE TABLE IF NOT EXISTS qianchuan_plan_settings (
    qianchuan_account_id INTEGER PRIMARY KEY REFERENCES qianchuan_accounts(id),
    enabled INTEGER NOT NULL DEFAULT 0,
    daily_budget_fen INTEGER,
    material_source TEXT NOT NULL DEFAULT 'homepage',
    delivery_mode TEXT NOT NULL DEFAULT 'volume',
    schedule_mode TEXT NOT NULL DEFAULT 'today_long',
    smart_coupon INTEGER NOT NULL DEFAULT 1,
    aigc_enabled INTEGER NOT NULL DEFAULT 0,
    enabled_at TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS qianchuan_plan_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    publish_queue_id INTEGER NOT NULL REFERENCES publish_queue(id),
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    qianchuan_account_id INTEGER NOT NULL REFERENCES qianchuan_accounts(id),
    product_id TEXT NOT NULL,
    advertiser_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'waiting_material',
    material_source TEXT NOT NULL,
    remote_material_id TEXT NOT NULL DEFAULT '',
    remote_plan_id TEXT NOT NULL DEFAULT '',
    plan_name TEXT NOT NULL,
    budget_fen INTEGER NOT NULL,
    smart_coupon INTEGER NOT NULL,
    aigc_enabled INTEGER NOT NULL,
    submit_attempted INTEGER NOT NULL DEFAULT 0,
    reason TEXT NOT NULL DEFAULT '',
    diagnostic_path TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE(qianchuan_account_id, account_id, product_id)
);
CREATE INDEX IF NOT EXISTS idx_qc_plan_status ON qianchuan_plan_jobs(status, id);
"""


def ensure_schema(db: sqlite3.Connection) -> None:
    db.executescript(SCHEMA)
    db.commit()


def settings(db: sqlite3.Connection, qianchuan_account_id: int) -> dict[str, Any]:
    account = db.execute(
        "SELECT id, qc_name, qc_id FROM qianchuan_accounts WHERE id = ?",
        (qianchuan_account_id,),
    ).fetchone()
    if account is None:
        raise ValueError("千川账户档案不存在")
    row = db.execute(
        "SELECT * FROM qianchuan_plan_settings WHERE qianchuan_account_id = ?",
        (qianchuan_account_id,),
    ).fetchone()
    result = dict(row) if row else {
        "qianchuan_account_id": qianchuan_account_id,
        "enabled": 0,
        "daily_budget_fen": None,
        "material_source": "homepage",
        "delivery_mode": "volume",
        "schedule_mode": "today_long",
        "smart_coupon": 1,
        "aigc_enabled": 0,
        "enabled_at": None,
    }
    result.update({"qc_name": account["qc_name"], "qc_id": account["qc_id"]})
    return result


def save_settings(db: sqlite3.Connection, account_id: int, *, enabled: bool,
                  daily_budget_fen: int, material_source: str,
                  smart_coupon: bool, aigc_enabled: bool) -> dict[str, Any]:
    if material_source not in SOURCES:
        raise ValueError("素材来源只支持主页作品、已上传 MP4 或两者")
    if not 1 <= daily_budget_fen <= 100_000_000:
        raise ValueError("日预算必须大于 0 且不超过 100 万元")
    current = settings(db, account_id)
    if not ADVERTISER_ID.fullmatch(str(current["qc_id"] or "")):
        raise ValueError("千川档案缺少有效广告主 ID")
    timestamp = now_text()
    enabled_at = current.get("enabled_at") if current["enabled"] and enabled else (timestamp if enabled else None)
    db.execute(
        "INSERT INTO qianchuan_plan_settings "
        "(qianchuan_account_id, enabled, daily_budget_fen, material_source, delivery_mode, "
        "schedule_mode, smart_coupon, aigc_enabled, enabled_at, updated_at) "
        "VALUES (?, ?, ?, ?, 'volume', 'today_long', ?, ?, ?, ?) "
        "ON CONFLICT(qianchuan_account_id) DO UPDATE SET "
        "enabled=excluded.enabled, daily_budget_fen=excluded.daily_budget_fen, "
        "material_source=excluded.material_source, smart_coupon=excluded.smart_coupon, "
        "aigc_enabled=excluded.aigc_enabled, enabled_at=excluded.enabled_at, updated_at=excluded.updated_at",
        (account_id, int(enabled), daily_budget_fen, material_source, int(smart_coupon),
         int(aigc_enabled), enabled_at, timestamp),
    )
    return {"ok": True, "settings": settings(db, account_id)}


def _candidate(db: sqlite3.Connection, queue_id: int) -> sqlite3.Row:
    row = db.execute(
        "SELECT p.id, p.account_id, p.product_id, p.product_name, p.title, p.video_path, "
        "p.platform_status, p.published_at, a.status AS account_status "
        "FROM publish_queue p JOIN accounts a ON a.id = p.account_id WHERE p.id = ?",
        (queue_id,),
    ).fetchone()
    if row is None:
        raise ValueError("抖音发布队列记录不存在")
    if row["platform_status"] != "published" or not row["published_at"]:
        raise ValueError("同账号同商品抖音作品尚未确认发布")
    if row["account_status"] != "active" or not PRODUCT_ID.fullmatch(str(row["product_id"])):
        raise ValueError("抖音账号未启用或商品 ID 无效")
    return row


def _binding(db: sqlite3.Connection, account_id: int) -> sqlite3.Row:
    rows = db.execute(
        "SELECT q.id, q.qc_id FROM account_qianchuan aq JOIN qianchuan_accounts q "
        "ON q.id = aq.qianchuan_row_id WHERE aq.account_row_id = ?",
        (account_id,),
    ).fetchall()
    if len(rows) != 1:
        raise ValueError(f"抖音账号必须唯一绑定一个千川账户，当前为 {len(rows)} 个")
    if not ADVERTISER_ID.fullmatch(str(rows[0]["qc_id"] or "")):
        raise ValueError("绑定的千川账户缺少有效广告主 ID")
    return rows[0]


def _material(db: sqlite3.Connection, account_id: int, qc_id: int,
              product_id: str, source: str) -> tuple[bool, str]:
    if source == "homepage":
        return True, ""
    if not db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='qianchuan_upload_jobs'").fetchone():
        return False, "Step17 上传队列尚未建立"
    row = db.execute(
        "SELECT remote_material_id FROM qianchuan_upload_jobs WHERE account_id = ? "
        "AND qianchuan_account_id = ? AND product_id = ? AND status = 'succeeded' "
        "AND remote_material_id <> '' ORDER BY id DESC LIMIT 1",
        (account_id, qc_id, product_id),
    ).fetchone()
    return (row is not None, str(row["remote_material_id"]) if row else "等待同账号同商品的 Step17 MP4 上传成功")


def _payload(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


def refresh(db: sqlite3.Connection, queue_id: int) -> dict[str, Any]:
    pub = _candidate(db, queue_id)
    binding = _binding(db, pub["account_id"])
    config = settings(db, binding["id"])
    if not config["enabled"] or config["daily_budget_fen"] is None:
        return {"ok": True, "queued": False, "reason": "该千川账户尚未启用自动计划或未配置日预算"}
    if pub["published_at"] < str(config["enabled_at"] or ""):
        return {"ok": True, "queued": False, "reason": "这是启用自动投放前的旧发布记录，不追溯创建付费计划"}
    ready, material = _material(db, pub["account_id"], binding["id"], pub["product_id"], config["material_source"])
    status = "queued" if ready else "waiting_material"
    timestamp = now_text()
    name = f"商品全域_{pub['product_id']}_{pub['id']}"[:50]
    db.execute(
        "INSERT OR IGNORE INTO qianchuan_plan_jobs "
        "(publish_queue_id, account_id, qianchuan_account_id, product_id, advertiser_id, status, "
        "material_source, remote_material_id, plan_name, budget_fen, smart_coupon, aigc_enabled, "
        "reason, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (queue_id, pub["account_id"], binding["id"], pub["product_id"], binding["qc_id"],
         status, config["material_source"], material if ready and config["material_source"] != "homepage" else "",
         name, config["daily_budget_fen"], config["smart_coupon"], config["aigc_enabled"],
         "" if ready else material, timestamp, timestamp),
    )
    row = db.execute(
        "SELECT * FROM qianchuan_plan_jobs WHERE qianchuan_account_id = ? "
        "AND account_id = ? AND product_id = ?",
        (binding["id"], pub["account_id"], pub["product_id"]),
    ).fetchone()
    if row["status"] in ("waiting_material", "blocked_config") and ready:
        db.execute(
            "UPDATE qianchuan_plan_jobs SET status='queued', remote_material_id=?, "
            "material_source=?, budget_fen=?, smart_coupon=?, aigc_enabled=?, "
            "reason='', updated_at=? WHERE id=?",
            (material if config["material_source"] != "homepage" else "", config["material_source"],
             config["daily_budget_fen"], config["smart_coupon"], config["aigc_enabled"],
             timestamp, row["id"]),
        )
        row = db.execute("SELECT * FROM qianchuan_plan_jobs WHERE id=?", (row["id"],)).fetchone()
    return {"ok": True, "queued": row["status"] == "queued", "job": _payload(row)}


def refresh_waiting(db: sqlite3.Connection, qc_id: int | None = None) -> dict[str, Any]:
    rows = db.execute(
        "SELECT publish_queue_id FROM qianchuan_plan_jobs WHERE status IN ('waiting_material','blocked_config') "
        + ("AND qianchuan_account_id=? " if qc_id is not None else "") + "ORDER BY id LIMIT 200",
        (qc_id,) if qc_id is not None else (),
    ).fetchall()
    updated = 0
    for row in rows:
        try:
            if refresh(db, row["publish_queue_id"])["queued"]:
                updated += 1
        except ValueError:
            pass
    return {"ok": True, "readyCount": updated}


def claim(db: sqlite3.Connection, job_id: int) -> dict[str, Any]:
    db.commit()
    db.execute("BEGIN IMMEDIATE")
    row = db.execute("SELECT * FROM qianchuan_plan_jobs WHERE id=?", (job_id,)).fetchone()
    if row is None or row["status"] != "queued":
        raise ValueError("计划任务不存在或不处于待执行状态")
    pub = _candidate(db, row["publish_queue_id"])
    binding = _binding(db, pub["account_id"])
    if binding["id"] != row["qianchuan_account_id"] or binding["qc_id"] != row["advertiser_id"]:
        raise ValueError("千川绑定在入队后发生变化")
    config = settings(db, row["qianchuan_account_id"])
    expected = (row["material_source"], row["budget_fen"], row["smart_coupon"], row["aigc_enabled"])
    current = (config["material_source"], config["daily_budget_fen"], config["smart_coupon"], config["aigc_enabled"])
    if not config["enabled"] or expected != current:
        db.execute("UPDATE qianchuan_plan_jobs SET status='blocked_config', "
                   "reason='账户投放配置已关闭或变更，执行前停止旧快照', updated_at=? WHERE id=?",
                   (now_text(), job_id))
        return {"ok": True, "claimed": False, "reason": "账户投放配置已关闭或变更"}
    ready, material = _material(db, row["account_id"], row["qianchuan_account_id"],
                                row["product_id"], row["material_source"])
    if not ready:
        db.execute("UPDATE qianchuan_plan_jobs SET status='waiting_material', reason=?, updated_at=? WHERE id=?",
                   (material, now_text(), job_id))
        return {"ok": True, "claimed": False, "reason": material}
    if row["material_source"] != "homepage" and not db.execute(
        "SELECT 1 FROM qianchuan_upload_jobs WHERE account_id=? AND qianchuan_account_id=? "
        "AND product_id=? AND status='succeeded' AND remote_material_id=? LIMIT 1",
        (row["account_id"], row["qianchuan_account_id"], row["product_id"], row["remote_material_id"]),
    ).fetchone():
        raise ValueError("计划入队后远端素材已失效，须人工核查")
    now = now_text()
    db.execute("UPDATE qianchuan_plan_jobs SET status='creating', started_at=?, updated_at=? WHERE id=?",
               (now, now, job_id))
    material_name = ""
    if row["material_source"] != "homepage":
        material_row = db.execute(
            "SELECT material_name FROM qianchuan_upload_jobs WHERE account_id=? AND qianchuan_account_id=? "
            "AND product_id=? AND remote_material_id=? AND status='succeeded' ORDER BY id DESC LIMIT 1",
            (row["account_id"], row["qianchuan_account_id"], row["product_id"], row["remote_material_id"]),
        ).fetchone()
        material_name = str(material_row["material_name"] or "") if material_row else ""
    return {"ok": True, "claimed": True,
            "job": _payload(db.execute("SELECT * FROM qianchuan_plan_jobs WHERE id=?", (job_id,)).fetchone()),
            "publishedTitle": pub["title"], "videoPath": pub["video_path"],
            "remoteMaterialName": material_name}


def finish(db: sqlite3.Connection, job_id: int, *, status: str, reason: str,
           submit_attempted: bool = False, remote_plan_id: str = "",
           diagnostic_path: str = "") -> dict[str, Any]:
    if status not in ("succeeded", "failed", "needs_review"):
        raise ValueError("计划结果状态无效")
    row = db.execute("SELECT * FROM qianchuan_plan_jobs WHERE id=?", (job_id,)).fetchone()
    if row is None or row["status"] != "creating":
        raise ValueError("计划任务不处于创建中状态")
    if status == "succeeded" and (not submit_attempted or not REMOTE_PLAN_ID.fullmatch(remote_plan_id)):
        raise ValueError("没有可验证的远端计划 ID，不能标记成功")
    if submit_attempted and status == "failed":
        status = "needs_review"
    now = now_text()
    db.execute(
        "UPDATE qianchuan_plan_jobs SET status=?, reason=?, submit_attempted=?, remote_plan_id=?, "
        "diagnostic_path=?, completed_at=?, updated_at=? WHERE id=?",
        (status, reason[:1800], int(submit_attempted), remote_plan_id, diagnostic_path, now, now, job_id),
    )
    return {"ok": True, "job": _payload(db.execute("SELECT * FROM qianchuan_plan_jobs WHERE id=?", (job_id,)).fetchone())}


def recover(db: sqlite3.Connection, older_than_minutes: int = 30) -> dict[str, Any]:
    if not 1 <= older_than_minutes <= 1440:
        raise ValueError("异常恢复阈值必须为 1–1440 分钟")
    cutoff = (datetime.now() - timedelta(minutes=older_than_minutes)).isoformat(timespec="seconds")
    now = now_text()
    rows = db.execute(
        "SELECT id FROM qianchuan_plan_jobs WHERE status='creating' AND started_at <= ?",
        (cutoff,),
    ).fetchall()
    for row in rows:
        db.execute(
            "UPDATE qianchuan_plan_jobs SET status='needs_review', submit_attempted=1, "
            "reason='客户端中断，无法证明是否已在千川发布；请在平台核对后处理', "
            "updated_at=? WHERE id=?", (now, row["id"]),
        )
    return {"ok": True, "recoveredCount": len(rows)}


def retry_safe(db: sqlite3.Connection, job_id: int) -> dict[str, Any]:
    row = db.execute("SELECT * FROM qianchuan_plan_jobs WHERE id=?", (job_id,)).fetchone()
    if row is None or row["status"] != "failed" or row["submit_attempted"]:
        raise ValueError("仅可重排明确未点击发布的失败任务")
    db.execute("UPDATE qianchuan_plan_jobs SET status='queued', reason='', updated_at=? WHERE id=?",
               (now_text(), job_id))
    return {"ok": True, "job": _payload(db.execute("SELECT * FROM qianchuan_plan_jobs WHERE id=?", (job_id,)).fetchone())}


def resolve_review(db: sqlite3.Connection, job_id: int, *, remote_plan_id: str = "", note: str = "") -> dict[str, Any]:
    row = db.execute("SELECT * FROM qianchuan_plan_jobs WHERE id=?", (job_id,)).fetchone()
    if row is None or row["status"] != "needs_review":
        raise ValueError("仅可处理待核查任务")
    if remote_plan_id:
        if not REMOTE_PLAN_ID.fullmatch(remote_plan_id):
            raise ValueError("远端计划 ID 格式无效")
        db.execute("UPDATE qianchuan_plan_jobs SET status='succeeded', remote_plan_id=?, "
                   "reason='人工在千川核实计划已发布', completed_at=?, updated_at=? WHERE id=?",
                   (remote_plan_id, now_text(), now_text(), job_id))
    else:
        if len(note.strip()) < 6:
            raise ValueError("确认千川没有此计划时须填写至少 6 字核查依据")
        db.execute("UPDATE qianchuan_plan_jobs SET status='queued', submit_attempted=0, "
                   "reason=?, updated_at=? WHERE id=?", (f"人工核实未发布：{note.strip()[:300]}", now_text(), job_id))
    return {"ok": True, "job": _payload(db.execute("SELECT * FROM qianchuan_plan_jobs WHERE id=?", (job_id,)).fetchone())}


def list_jobs(db: sqlite3.Connection, qc_id: int | None = None) -> dict[str, Any]:
    rows = db.execute(
        "SELECT * FROM qianchuan_plan_jobs " + ("WHERE qianchuan_account_id=? " if qc_id else "")
        + "ORDER BY id DESC LIMIT 200", (qc_id,) if qc_id else (),
    ).fetchall()
    return {"ok": True, "rows": [_payload(row) for row in rows]}


def main() -> int:
    parser = argparse.ArgumentParser(description="千川商品全域投放计划队列")
    parser.add_argument("--db", type=Path, default=Path(DEFAULT_DB_PATH))
    sub = parser.add_subparsers(dest="command", required=True)
    config = sub.add_parser("config")
    config.add_argument("--qianchuan-account-id", type=int, required=True)
    config.add_argument("--enabled", type=int, choices=(0, 1))
    config.add_argument("--daily-budget-fen", type=int)
    config.add_argument("--material-source", choices=SOURCES)
    config.add_argument("--smart-coupon", type=int, choices=(0, 1))
    config.add_argument("--aigc-enabled", type=int, choices=(0, 1))
    add = sub.add_parser("refresh")
    add.add_argument("--publish-queue-id", type=int, required=True)
    waiting = sub.add_parser("refresh-waiting")
    waiting.add_argument("--qianchuan-account-id", type=int)
    jobs = sub.add_parser("jobs")
    jobs.add_argument("--qianchuan-account-id", type=int)
    one = sub.add_parser("job")
    one.add_argument("--id", type=int, required=True)
    lock = sub.add_parser("claim")
    lock.add_argument("--id", type=int, required=True)
    done = sub.add_parser("finish")
    done.add_argument("--id", type=int, required=True)
    done.add_argument("--status", choices=("succeeded", "failed", "needs_review"), required=True)
    done.add_argument("--reason", default="")
    done.add_argument("--remote-plan-id", default="")
    done.add_argument("--diagnostic-path", default="")
    done.add_argument("--submit-attempted", action="store_true")
    recovery = sub.add_parser("recover")
    recovery.add_argument("--older-than-minutes", type=int, default=30)
    retry = sub.add_parser("retry-safe")
    retry.add_argument("--id", type=int, required=True)
    review = sub.add_parser("resolve-review")
    review.add_argument("--id", type=int, required=True)
    review.add_argument("--remote-plan-id", default="")
    review.add_argument("--note", default="")
    args = parser.parse_args()
    try:
        with connect(args.db) as db:
            ensure_schema(db)
            if args.command == "config":
                if args.enabled is None:
                    result = {"ok": True, "settings": settings(db, args.qianchuan_account_id)}
                else:
                    if args.daily_budget_fen is None or args.material_source is None or args.smart_coupon is None or args.aigc_enabled is None:
                        raise ValueError("保存配置必须提供预算、素材来源、优惠券和 AIGC 设置")
                    result = save_settings(db, args.qianchuan_account_id, enabled=bool(args.enabled),
                                           daily_budget_fen=args.daily_budget_fen,
                                           material_source=args.material_source,
                                           smart_coupon=bool(args.smart_coupon), aigc_enabled=bool(args.aigc_enabled))
            elif args.command == "refresh":
                result = refresh(db, args.publish_queue_id)
            elif args.command == "refresh-waiting":
                result = refresh_waiting(db, args.qianchuan_account_id)
            elif args.command == "jobs":
                result = list_jobs(db, args.qianchuan_account_id)
            elif args.command == "job":
                row = db.execute("SELECT * FROM qianchuan_plan_jobs WHERE id=?", (args.id,)).fetchone()
                if row is None:
                    raise ValueError("计划任务不存在")
                result = {"ok": True, "job": _payload(row)}
            elif args.command == "claim":
                result = claim(db, args.id)
            elif args.command == "recover":
                result = recover(db, args.older_than_minutes)
            elif args.command == "retry-safe":
                result = retry_safe(db, args.id)
            elif args.command == "resolve-review":
                result = resolve_review(db, args.id, remote_plan_id=args.remote_plan_id, note=args.note)
            else:
                result = finish(db, args.id, status=args.status, reason=args.reason,
                                submit_attempted=args.submit_attempted,
                                remote_plan_id=args.remote_plan_id, diagnostic_path=args.diagnostic_path)
        return emit(result)
    except (ValueError, sqlite3.Error) as error:
        return emit({"ok": False, "error": str(error)}, 1)


if __name__ == "__main__":
    raise SystemExit(main())
