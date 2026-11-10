# -*- coding: utf-8 -*-
"""千川商品全域投放计划：账户配置、资格检查和幂等任务状态。"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from db import DEFAULT_DB_PATH, connect, emit, now_text
from qianchuan_upload import ensure_schema as ensure_upload_schema

SCHEMA = """
CREATE TABLE IF NOT EXISTS qianchuan_plan_settings (
  qianchuan_account_id INTEGER PRIMARY KEY REFERENCES qianchuan_accounts(id),
  enabled INTEGER NOT NULL DEFAULT 0,
  daily_budget_yuan INTEGER NOT NULL DEFAULT 0,
  video_source TEXT NOT NULL DEFAULT 'both',
  delivery_mode TEXT NOT NULL DEFAULT '控成本投放',
  long_running INTEGER NOT NULL DEFAULT 1,
  smart_coupon INTEGER NOT NULL DEFAULT 1,
  aigc_enabled INTEGER NOT NULL DEFAULT 0,
  activated_at TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS qianchuan_plan_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  publish_queue_id INTEGER NOT NULL REFERENCES publish_queue(id),
  account_id INTEGER NOT NULL REFERENCES accounts(id),
  qianchuan_account_id INTEGER NOT NULL REFERENCES qianchuan_accounts(id),
  advertiser_id TEXT NOT NULL,
  product_id TEXT NOT NULL,
  product_name TEXT NOT NULL DEFAULT '',
  published_video_title TEXT NOT NULL DEFAULT '',
  plan_name TEXT NOT NULL,
  commission_rate REAL NOT NULL DEFAULT 0,
  roi_target REAL NOT NULL DEFAULT 0,
  material_ids_json TEXT NOT NULL DEFAULT '[]',
  selected_material_ids_json TEXT NOT NULL DEFAULT '[]',
  applied_material_ids_json TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'ready',
  submit_attempted INTEGER NOT NULL DEFAULT 0,
  remote_plan_id TEXT NOT NULL DEFAULT '',
  reason TEXT NOT NULL DEFAULT '',
  screenshot_path TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(account_id, qianchuan_account_id, product_id)
);
CREATE INDEX IF NOT EXISTS idx_qc_plan_jobs_status ON qianchuan_plan_jobs(status, id);
CREATE TABLE IF NOT EXISTS qianchuan_plan_supplements (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  plan_job_id INTEGER NOT NULL REFERENCES qianchuan_plan_jobs(id),
  remote_material_id TEXT NOT NULL,
  scheduled_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'scheduled',
  submit_attempted INTEGER NOT NULL DEFAULT 0,
  reason TEXT NOT NULL DEFAULT '',
  screenshot_path TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(plan_job_id, remote_material_id)
);
CREATE INDEX IF NOT EXISTS idx_qc_plan_supplements_due
  ON qianchuan_plan_supplements(status, scheduled_at);
"""


def ensure_schema(db: sqlite3.Connection) -> None:
    ensure_upload_schema(db)
    db.executescript(SCHEMA)
    columns = {row["name"] for row in db.execute("PRAGMA table_info(qianchuan_plan_settings)")}
    if "activated_at" not in columns:
        db.execute("ALTER TABLE qianchuan_plan_settings ADD COLUMN activated_at TEXT NOT NULL DEFAULT ''")
        # 旧版本若曾启用，迁移时从当前时刻开始；禁止将历史作品批量建成付费计划。
        db.execute("UPDATE qianchuan_plan_settings SET activated_at=? WHERE enabled=1", (now_text(),))
    plan_columns = {row["name"] for row in db.execute("PRAGMA table_info(qianchuan_plan_jobs)")}
    for name in ("selected_material_ids_json", "applied_material_ids_json"):
        if name not in plan_columns:
            db.execute(f"ALTER TABLE qianchuan_plan_jobs ADD COLUMN {name} TEXT NOT NULL DEFAULT '[]'")
    if "commission_rate" not in plan_columns:
        db.execute("ALTER TABLE qianchuan_plan_jobs ADD COLUMN commission_rate REAL NOT NULL DEFAULT 0")
    if "roi_target" not in plan_columns:
        db.execute("ALTER TABLE qianchuan_plan_jobs ADD COLUMN roi_target REAL NOT NULL DEFAULT 0")
    # 新流程统一使用控成本投放；保留已创建的远端计划状态，不回写历史任务。
    db.execute("UPDATE qianchuan_plan_settings SET delivery_mode='控成本投放' WHERE delivery_mode<>'控成本投放'")


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row else None


def _settings(db: sqlite3.Connection, qc_id: int) -> dict[str, Any]:
    if not db.execute("SELECT 1 FROM qianchuan_accounts WHERE id=?", (qc_id,)).fetchone():
        raise ValueError("千川账户不存在")
    row = _row(db.execute("SELECT * FROM qianchuan_plan_settings WHERE qianchuan_account_id=?", (qc_id,)).fetchone())
    return row or {"qianchuan_account_id": qc_id, "enabled": 0, "daily_budget_yuan": 0,
                   "video_source": "both", "delivery_mode": "控成本投放", "long_running": 1,
                   "smart_coupon": 1, "aigc_enabled": 0, "activated_at": ""}


def save_settings(db: sqlite3.Connection, qc_id: int, budget: int, source: str,
                  enabled: bool, long_running: bool, coupon: bool, aigc: bool) -> dict[str, Any]:
    previous = _settings(db, qc_id)
    if not 0 <= budget <= 10000000 or (enabled and budget < 50):
        raise ValueError("启用自动发布时须设置每账户日预算，至少 50 元")
    if source not in {"uploaded", "my_video", "both"}:
        raise ValueError("视频来源无效")
    if not long_running:
        raise ValueError("当前只支持从今天起长期投放；自定义结束日期尚未配置")
    activated_at = (str(previous.get("activated_at") or "") if previous["enabled"] and enabled
                    else now_text() if enabled else "")
    with db:
        db.execute("""INSERT INTO qianchuan_plan_settings
            (qianchuan_account_id,enabled,daily_budget_yuan,video_source,delivery_mode,
             long_running,smart_coupon,aigc_enabled,activated_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(qianchuan_account_id) DO UPDATE SET
            enabled=excluded.enabled,daily_budget_yuan=excluded.daily_budget_yuan,
            video_source=excluded.video_source,delivery_mode=excluded.delivery_mode,
            long_running=excluded.long_running,smart_coupon=excluded.smart_coupon,
            aigc_enabled=excluded.aigc_enabled,activated_at=excluded.activated_at,
            updated_at=excluded.updated_at""",
            (qc_id, int(enabled), budget, source, "控成本投放", int(long_running),
             int(coupon), int(aigc), activated_at, now_text()))
    return _settings(db, qc_id)


def usable_material_ids(db: sqlite3.Connection, account_id: int, qc_id: int, product_id: str) -> list[str]:
    rows = db.execute("""SELECT remote_material_id, material_evaluation, review_status
        FROM qianchuan_upload_jobs WHERE account_id=? AND qianchuan_account_id=?
        AND product_id=? AND status='succeeded' AND remote_material_id<>'' ORDER BY id""",
        (account_id, qc_id, product_id)).fetchall()
    seen: set[str] = set()
    result: list[str] = []
    for row in rows:
        material_id = str(row["remote_material_id"])
        if not re.fullmatch(r"\d{8,20}", material_id) or material_id in seen:
            continue
        if "搬运" in str(row["material_evaluation"] or "") or re.search("不通过|拒绝|违规", str(row["review_status"] or "")):
            continue
        seen.add(material_id)
        result.append(material_id)
    return result


def uploads_settled(db: sqlite3.Connection, account_id: int, qc_id: int,
                    product_id: str, queue_id: int) -> bool:
    pending = db.execute("""SELECT 1 FROM qianchuan_upload_jobs
        WHERE account_id=? AND qianchuan_account_id=? AND product_id=?
        AND status IN ('queued','uploading','needs_review') LIMIT 1""",
        (account_id, qc_id, product_id)).fetchone()
    if pending:
        return False
    exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='post_publish_material_runs'").fetchone()
    if exists:
        run = db.execute("SELECT status FROM post_publish_material_runs WHERE publish_queue_id=?", (queue_id,)).fetchone()
        if run and run["status"] not in ("completed", "candidate_exhausted"):
            return False
    return True


def supplement_due_at(plan_updated_at: str, upload_completed_at: str) -> str:
    """原计划满 24 小时后追加；之后才上传的素材无需再等一天。"""
    plan_ready = datetime.fromisoformat(plan_updated_at) + timedelta(hours=24)
    material_ready = datetime.fromisoformat(upload_completed_at or plan_updated_at)
    return max(plan_ready, material_ready).isoformat(timespec="seconds")


def sync_supplements(db: sqlite3.Connection) -> dict[str, Any]:
    """已发布计划有新上传且可用的素材时，满 24 小时后追加到原计划。"""
    plans = db.execute("""SELECT p.* FROM qianchuan_plan_jobs p
        JOIN accounts a ON a.id=p.account_id
        JOIN qianchuan_plan_settings cfg ON cfg.qianchuan_account_id=p.qianchuan_account_id
        WHERE p.status='published' AND cfg.enabled=1
        AND p.remote_plan_id<>'' AND a.status='active' ORDER BY p.id""").fetchall()
    created = 0
    for plan in plans:
        # 提交结果待核查时，禁止继续修改同一条远端付费计划。
        uncertain = db.execute("""SELECT 1 FROM qianchuan_plan_supplements
            WHERE plan_job_id=? AND status IN ('appending','needs_review') LIMIT 1""", (plan["id"],)).fetchone()
        if uncertain:
            continue
        applied = set(json.loads(plan["applied_material_ids_json"] or "[]"))
        if not applied:
            # 旧计划缺少已挂载素材快照，须先从千川回读，不能把原有素材当新增素材。
            continue
        candidates = usable_material_ids(db, int(plan["account_id"]),
                                         int(plan["qianchuan_account_id"]), str(plan["product_id"]))
        for material_id in candidates:
            if material_id in applied:
                continue
            upload = db.execute("""SELECT completed_at FROM qianchuan_upload_jobs
                WHERE account_id=? AND qianchuan_account_id=? AND remote_material_id=?
                AND status='succeeded' ORDER BY id DESC LIMIT 1""",
                (plan["account_id"], plan["qianchuan_account_id"], material_id)).fetchone()
            due = supplement_due_at(str(plan["updated_at"]),
                                    str(upload["completed_at"] or ""))
            with db:
                cursor = db.execute("""INSERT OR IGNORE INTO qianchuan_plan_supplements
                    (plan_job_id,remote_material_id,scheduled_at,created_at,updated_at)
                    VALUES (?,?,?,?,?)""",
                    (plan["id"], material_id, due, now_text(), now_text()))
                created += int(cursor.rowcount > 0)
    return {"ok": True, "created": created}


def claim_supplement(db: sqlite3.Connection, supplement_id: int) -> dict[str, Any]:
    db.commit()
    db.execute("BEGIN IMMEDIATE")
    try:
        job = db.execute("""SELECT s.*,p.account_id,p.qianchuan_account_id,p.advertiser_id,
            p.product_id,p.plan_name,p.remote_plan_id FROM qianchuan_plan_supplements s
            JOIN qianchuan_plan_jobs p ON p.id=s.plan_job_id WHERE s.id=?""",
            (supplement_id,)).fetchone()
        if not job or job["status"] != "scheduled" or job["scheduled_at"] > now_text():
            raise ValueError("追加任务未到期或状态已改变")
        parent = db.execute("SELECT status FROM qianchuan_plan_jobs WHERE id=?", (job["plan_job_id"],)).fetchone()
        if not parent or parent["status"] != "published":
            raise ValueError("原千川计划尚未确认发布")
        active_settings = db.execute("""SELECT 1 FROM accounts a
            JOIN account_qianchuan aq ON aq.account_row_id=a.id
            JOIN qianchuan_plan_settings cfg ON cfg.qianchuan_account_id=aq.qianchuan_row_id
            WHERE a.id=? AND a.status='active' AND aq.qianchuan_row_id=? AND cfg.enabled=1""",
            (job["account_id"], job["qianchuan_account_id"])).fetchone()
        if not active_settings:
            raise ValueError("抖音账号、千川绑定或自动计划设置已停用")
        active = db.execute("""SELECT 1 FROM qianchuan_plan_supplements WHERE plan_job_id=?
            AND status IN ('appending','needs_review') AND id<>? LIMIT 1""",
            (job["plan_job_id"], supplement_id)).fetchone()
        if active:
            raise ValueError("同一原计划有正在追加或待核查的素材")
        if job["remote_material_id"] not in usable_material_ids(
                db, int(job["account_id"]), int(job["qianchuan_account_id"]), str(job["product_id"])):
            raise ValueError("素材已不符合千川投放条件，请先核查平台判定")
        db.execute("UPDATE qianchuan_plan_supplements SET status='appending',updated_at=? WHERE id=?",
                   (now_text(), supplement_id))
        db.commit()
        return {"ok": True, "job": dict(job)}
    except Exception:
        db.rollback()
        raise


def finish_supplement(db: sqlite3.Connection, supplement_id: int, status: str,
                      submitted: bool, reason: str = "", screenshot: str = "") -> dict[str, Any]:
    if status not in {"applied", "already_present", "waiting", "needs_review", "blocked"}:
        raise ValueError("追加任务终态无效")
    if submitted and status in {"waiting", "blocked", "already_present"}:
        raise ValueError("已提交的追加任务须核查远端结果")
    row = db.execute("""SELECT s.*,p.applied_material_ids_json FROM qianchuan_plan_supplements s
        JOIN qianchuan_plan_jobs p ON p.id=s.plan_job_id WHERE s.id=?""", (supplement_id,)).fetchone()
    if not row or row["status"] != "appending":
        raise ValueError("追加任务状态已改变，禁止覆盖")
    due = ((datetime.fromisoformat(now_text()) + timedelta(hours=24)).isoformat(timespec="seconds")
           if status == "waiting" else row["scheduled_at"])
    with db:
        db.execute("""UPDATE qianchuan_plan_supplements SET status=?,submit_attempted=?,reason=?,
            screenshot_path=?,scheduled_at=?,updated_at=? WHERE id=? AND status='appending'""",
            ("scheduled" if status == "waiting" else status, int(submitted), reason[:2000],
             screenshot, due, now_text(), supplement_id))
        if status in {"applied", "already_present"}:
            applied = list(dict.fromkeys([*json.loads(row["applied_material_ids_json"] or "[]"),
                                          row["remote_material_id"]]))
            db.execute("UPDATE qianchuan_plan_jobs SET applied_material_ids_json=?,updated_at=? WHERE id=?",
                       (json.dumps(applied), now_text(), row["plan_job_id"]))
    return {"ok": True, "job": _row(db.execute(
        "SELECT * FROM qianchuan_plan_supplements WHERE id=?", (supplement_id,)).fetchone())}


def sync(db: sqlite3.Connection) -> dict[str, Any]:
    """仅为已发布作品、已配置预算且有可识别素材的商品生成一个任务。"""
    has_baiying_products = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='baiying_products'"
    ).fetchone() is not None
    commission_column = "b.cos_ratio" if has_baiying_products else "NULL"
    commission_join = "LEFT JOIN baiying_products b ON b.product_id=p.product_id" if has_baiying_products else ""
    candidates = db.execute(f"""SELECT p.id publish_queue_id,p.account_id,p.product_id,
        p.product_name,p.title,q.id qianchuan_account_id,q.qc_id advertiser_id,
        s.video_source,{commission_column} commission_rate FROM publish_queue p
        JOIN accounts a ON a.id=p.account_id
        JOIN account_qianchuan aq ON aq.account_row_id=p.account_id
        JOIN qianchuan_accounts q ON q.id=aq.qianchuan_row_id
        JOIN qianchuan_plan_settings s ON s.qianchuan_account_id=q.id
        {commission_join}
        WHERE p.platform_status='published' AND a.status='active' AND s.enabled=1 AND s.daily_budget_yuan>=50
        AND s.activated_at<>'' AND p.published_at>s.activated_at
        AND p.product_id<>'' ORDER BY p.id""").fetchall()
    created = 0
    for row in candidates:
        account_id, qc_id, product_id = int(row["account_id"]), int(row["qianchuan_account_id"]), str(row["product_id"])
        advertiser_id = str(row["advertiser_id"] or "")
        if not re.fullmatch(r"\d{8,20}", advertiser_id):
            continue
        ids = usable_material_ids(db, account_id, qc_id, product_id)
        if row["video_source"] == "uploaded" and not ids:
            continue
        if row["video_source"] == "both" and not ids:
            continue
        if row["video_source"] in ("uploaded", "both") and not uploads_settled(
                db, account_id, qc_id, product_id, int(row["publish_queue_id"])):
            continue
        if row["video_source"] == "my_video" and not str(row["title"] or "").strip():
            continue
        try:
            commission_rate = float(row["commission_rate"])
        except (TypeError, ValueError):
            commission_rate = 0
        roi_target = round(100 / commission_rate, 2) if 0 < commission_rate <= 100 else 0
        name = f"{date.today().isoformat()}_商品全域投放_{product_id}"[:50]
        status = "ready" if roi_target > 0 else "blocked"
        reason = "" if roi_target > 0 else "无法计算保本 ROI：百应商品佣金率缺失或无效，请先同步商品数据。"
        with db:
            cursor = db.execute("""INSERT OR IGNORE INTO qianchuan_plan_jobs
                (publish_queue_id,account_id,qianchuan_account_id,advertiser_id,product_id,
                 product_name,published_video_title,plan_name,commission_rate,roi_target,
                 material_ids_json,status,reason,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (row["publish_queue_id"], account_id, qc_id, advertiser_id, product_id,
                 row["product_name"], row["title"], name, commission_rate, roi_target,
                 json.dumps(ids), status, reason, now_text(), now_text()))
            created += int(cursor.rowcount > 0)
            if roi_target > 0:
                db.execute("""UPDATE qianchuan_plan_jobs SET commission_rate=?,roi_target=?,
                    status=CASE WHEN status='blocked' AND reason LIKE '无法计算保本 ROI：%' THEN 'ready' ELSE status END,
                    reason=CASE WHEN status='blocked' AND reason LIKE '无法计算保本 ROI：%' THEN '' ELSE reason END,
                    updated_at=? WHERE account_id=? AND qianchuan_account_id=? AND product_id=?
                    AND status IN ('ready','blocked') AND submit_attempted=0""",
                    (commission_rate, roi_target, now_text(), account_id, qc_id, product_id))
            else:
                db.execute("""UPDATE qianchuan_plan_jobs SET status='blocked',reason=?,updated_at=?
                    WHERE account_id=? AND qianchuan_account_id=? AND product_id=?
                    AND status='ready' AND roi_target<=0 AND submit_attempted=0""",
                    (reason, now_text(), account_id, qc_id, product_id))
    return {"ok": True, "created": created}


def claim(db: sqlite3.Connection, job_id: int) -> dict[str, Any]:
    db.commit()
    db.execute("BEGIN IMMEDIATE")
    try:
        row = db.execute("SELECT * FROM qianchuan_plan_jobs WHERE id=?", (job_id,)).fetchone()
        if not row or row["status"] != "ready":
            raise ValueError("计划任务不存在或已执行；不会重复发布")
        settings = _settings(db, int(row["qianchuan_account_id"]))
        if not settings["enabled"] or int(settings["daily_budget_yuan"]) < 50:
            raise ValueError("该千川账户未启用自动发布或未配置预算")
        if float(row["roi_target"] or 0) <= 0 or float(row["commission_rate"] or 0) <= 0:
            raise ValueError("商品佣金率未就绪，无法计算控成本投放保本 ROI")
        published = db.execute("SELECT platform_status FROM publish_queue WHERE id=?", (row["publish_queue_id"],)).fetchone()
        if not published or published["platform_status"] != "published":
            raise ValueError("抖音作品未确认发布")
        account = db.execute("SELECT status FROM accounts WHERE id=?", (row["account_id"],)).fetchone()
        if not account or account["status"] != "active":
            raise ValueError("抖音账号已停用")
        ids = usable_material_ids(db, int(row["account_id"]), int(row["qianchuan_account_id"]), str(row["product_id"]))
        if settings["video_source"] in ("uploaded", "both") and not ids:
            raise ValueError("已上传素材尚未就绪")
        if settings["video_source"] in ("uploaded", "both") and not uploads_settled(
                db, int(row["account_id"]), int(row["qianchuan_account_id"]),
                str(row["product_id"]), int(row["publish_queue_id"])):
            raise ValueError("该商品上传或自动补位仍在进行")
        db.execute("UPDATE qianchuan_plan_jobs SET status='submitting',material_ids_json=?,updated_at=? WHERE id=?",
                   (json.dumps(ids), now_text(), job_id))
        db.commit()
        job = _row(db.execute("SELECT * FROM qianchuan_plan_jobs WHERE id=?", (job_id,)).fetchone())
        return {"ok": True, "job": job, "settings": settings}
    except Exception:
        db.rollback()
        raise


def finish(db: sqlite3.Connection, job_id: int, status: str, submitted: bool,
           remote_id: str = "", reason: str = "", screenshot: str = "",
           selected_material_ids: list[str] | None = None) -> dict[str, Any]:
    if status not in {"published", "needs_review", "blocked"}:
        raise ValueError("计划终态无效")
    if status == "published" and not re.fullmatch(r"\d{8,20}", remote_id):
        raise ValueError("没有可核对的千川计划 ID，不能标记已发布")
    if submitted and status == "blocked":
        raise ValueError("已点击发布的任务必须待核查")
    selected = list(dict.fromkeys(selected_material_ids or []))
    if any(not re.fullmatch(r"\d{8,20}", material_id) for material_id in selected):
        raise ValueError("计划所选素材 ID 无效")
    row = db.execute("SELECT material_ids_json FROM qianchuan_plan_jobs WHERE id=?", (job_id,)).fetchone()
    if not row or not set(selected).issubset(set(json.loads(row["material_ids_json"] or "[]"))):
        raise ValueError("所选素材不属于该计划候选集")
    with db:
        updated = db.execute("""UPDATE qianchuan_plan_jobs SET status=?,submit_attempted=?,
            remote_plan_id=?,reason=?,screenshot_path=?,selected_material_ids_json=?,
            applied_material_ids_json=CASE WHEN ?='published' THEN ? ELSE applied_material_ids_json END,
            updated_at=? WHERE id=? AND status='submitting'""",
            (status, int(submitted), remote_id, reason[:2000], screenshot, json.dumps(selected),
             status, json.dumps(selected), now_text(), job_id))
        if updated.rowcount != 1:
            raise ValueError("计划任务状态已改变，禁止覆盖")
    return {"ok": True, "job": _row(db.execute("SELECT * FROM qianchuan_plan_jobs WHERE id=?", (job_id,)).fetchone())}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=Path(DEFAULT_DB_PATH))
    parser.add_argument("command", choices=["settings", "set-settings", "sync", "jobs", "claim", "finish", "recover", "retry-blocked", "resolve-present", "resolve-absent", "sync-supplements", "supplement-jobs", "claim-supplement", "finish-supplement"])
    parser.add_argument("--qianchuan-account-id", type=int, default=0)
    parser.add_argument("--daily-budget-yuan", type=int, default=0)
    parser.add_argument("--video-source", default="both")
    parser.add_argument("--enabled", type=int, choices=[0, 1], default=0)
    parser.add_argument("--long-running", type=int, choices=[0, 1], default=1)
    parser.add_argument("--smart-coupon", type=int, choices=[0, 1], default=1)
    parser.add_argument("--aigc-enabled", type=int, choices=[0, 1], default=0)
    parser.add_argument("--id", type=int, default=0)
    parser.add_argument("--status", default="")
    parser.add_argument("--submit-attempted", type=int, choices=[0, 1], default=0)
    parser.add_argument("--remote-plan-id", default="")
    parser.add_argument("--reason", default="")
    parser.add_argument("--screenshot-path", default="")
    parser.add_argument("--selected-material-ids-json", default="[]")
    args = parser.parse_args()
    try:
        with connect(args.db) as db:
            ensure_schema(db)
            if args.command == "settings":
                result = {"ok": True, "settings": _settings(db, args.qianchuan_account_id)}
            elif args.command == "set-settings":
                result = {"ok": True, "settings": save_settings(db, args.qianchuan_account_id,
                    args.daily_budget_yuan, args.video_source, bool(args.enabled), bool(args.long_running),
                    bool(args.smart_coupon), bool(args.aigc_enabled))}
            elif args.command == "sync":
                result = sync(db)
            elif args.command == "sync-supplements":
                result = sync_supplements(db)
            elif args.command == "jobs":
                query = "SELECT * FROM qianchuan_plan_jobs"
                filters: list[Any] = []
                if args.qianchuan_account_id:
                    query += " WHERE qianchuan_account_id=?"
                    filters.append(args.qianchuan_account_id)
                result = {"ok": True, "rows": [dict(r) for r in db.execute(query + " ORDER BY id DESC LIMIT 200", filters)]}
            elif args.command == "supplement-jobs":
                result = {"ok": True, "rows": [dict(r) for r in db.execute("""
                    SELECT s.*,p.account_id,p.qianchuan_account_id,p.advertiser_id,
                    p.product_id,p.plan_name,p.remote_plan_id FROM qianchuan_plan_supplements s
                    JOIN qianchuan_plan_jobs p ON p.id=s.plan_job_id
                    ORDER BY s.id DESC LIMIT 200""")]}
            elif args.command == "claim":
                result = claim(db, args.id)
            elif args.command == "claim-supplement":
                result = claim_supplement(db, args.id)
            elif args.command == "recover":
                with db:
                    changed = db.execute("""UPDATE qianchuan_plan_jobs SET status='needs_review',
                        reason='客户端曾在计划提交阶段中断；请先到千川核对是否已发布',updated_at=?
                        WHERE status='submitting'""", (now_text(),)).rowcount
                    supplements = db.execute("""UPDATE qianchuan_plan_supplements
                        SET status='needs_review',reason='客户端曾在追加原计划素材时中断；请先核查远端',
                        updated_at=? WHERE status='appending'""", (now_text(),)).rowcount
                result = {"ok": True, "recovered": changed, "supplementsRecovered": supplements}
            elif args.command in ("retry-blocked", "resolve-present", "resolve-absent"):
                with db:
                    row = db.execute("SELECT * FROM qianchuan_plan_jobs WHERE id=?", (args.id,)).fetchone()
                    if not row:
                        raise ValueError("计划任务不存在")
                    if args.command == "retry-blocked":
                        if row["status"] != "blocked" or row["submit_attempted"]:
                            raise ValueError("只有发布前停止的计划可直接重试")
                        if float(row["roi_target"] or 0) <= 0:
                            raise ValueError("商品佣金率未同步，暂时无法计算 ROI；同步百应商品数据后再试")
                        status, remote_id = "ready", ""
                    else:
                        if row["status"] != "needs_review":
                            raise ValueError("只有待核查计划可录入平台结论")
                        if len(args.reason.strip()) < 6:
                            raise ValueError("请填写至少 6 字的千川核查依据")
                        if args.command == "resolve-present":
                            if not re.fullmatch(r"\d{8,20}", args.remote_plan_id):
                                raise ValueError("请填写千川页面核实的计划 ID")
                            status, remote_id = "published", args.remote_plan_id
                        else:
                            status, remote_id = "ready", ""
                    db.execute("""UPDATE qianchuan_plan_jobs SET status=?,remote_plan_id=?,
                        reason=?,applied_material_ids_json=CASE WHEN ?='published'
                            AND selected_material_ids_json<>'[]' THEN selected_material_ids_json
                            ELSE applied_material_ids_json END,updated_at=? WHERE id=?""",
                        (status, remote_id, args.reason.strip()[:2000], status, now_text(), args.id))
                result = {"ok": True, "job": _row(db.execute("SELECT * FROM qianchuan_plan_jobs WHERE id=?", (args.id,)).fetchone())}
            elif args.command == "finish-supplement":
                result = finish_supplement(db, args.id, args.status, bool(args.submit_attempted),
                                           args.reason, args.screenshot_path)
            else:
                result = finish(db, args.id, args.status, bool(args.submit_attempted),
                                args.remote_plan_id, args.reason, args.screenshot_path,
                                json.loads(args.selected_material_ids_json))
        return emit(result)
    except Exception as error:
        return emit({"ok": False, "error": str(error)}, 1)


if __name__ == "__main__":
    raise SystemExit(main())
