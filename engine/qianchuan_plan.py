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
  creation_source TEXT NOT NULL DEFAULT 'auto',
  daily_budget_yuan INTEGER NOT NULL DEFAULT 0,
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
CREATE TABLE IF NOT EXISTS qianchuan_plan_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  plan_job_id INTEGER NOT NULL REFERENCES qianchuan_plan_jobs(id),
  supplement_id INTEGER NOT NULL DEFAULT 0,
  event_type TEXT NOT NULL,
  stage TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT '',
  message TEXT NOT NULL DEFAULT '',
  detail TEXT NOT NULL DEFAULT '',
  remote_plan_id TEXT NOT NULL DEFAULT '',
  screenshot_path TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_qc_plan_events_job_time
  ON qianchuan_plan_events(plan_job_id, created_at, id);
CREATE INDEX IF NOT EXISTS idx_qc_plan_events_type_time
  ON qianchuan_plan_events(event_type, created_at);
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
    if "creation_source" not in plan_columns:
        db.execute("ALTER TABLE qianchuan_plan_jobs ADD COLUMN creation_source TEXT NOT NULL DEFAULT 'auto'")
    if "daily_budget_yuan" not in plan_columns:
        db.execute("ALTER TABLE qianchuan_plan_jobs ADD COLUMN daily_budget_yuan INTEGER NOT NULL DEFAULT 0")
    # 新流程统一使用控成本投放；保留已创建的远端计划状态，不回写历史任务。
    db.execute("UPDATE qianchuan_plan_settings SET delivery_mode='控成本投放' WHERE delivery_mode<>'控成本投放'")
    # 首次升级时为旧计划补一条“历史快照”，避免日志页对已有任务显示为空。
    legacy_rows = db.execute("""SELECT p.id,p.status,p.remote_plan_id,p.reason,p.created_at
        FROM qianchuan_plan_jobs p
        WHERE NOT EXISTS (SELECT 1 FROM qianchuan_plan_events e WHERE e.plan_job_id=p.id)""").fetchall()
    for row in legacy_rows:
        status = str(row["status"] or "")
        event_type = {
            "published": "PLAN_PUBLISHED",
            "needs_review": "PLAN_SUBMISSION_UNCERTAIN",
            "blocked": "PLAN_BLOCKED",
            "submitting": "PLAN_RECOVERED_TO_REVIEW",
        }.get(status, "PLAN_HISTORY_SNAPSHOT")
        message = {
            "published": "历史计划已记录为已发布",
            "needs_review": "历史计划当前待核查，请先核对千川页面",
            "blocked": "历史计划当时在提交前停止",
            "submitting": "历史计划处于提交中，当前日志只保留快照",
        }.get(status, "历史计划状态快照")
        record_event(db, int(row["id"]), event_type, "migration", status, message,
                     str(row["reason"] or ""), str(row["remote_plan_id"] or ""))


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row else None


def record_event(db: sqlite3.Connection, plan_job_id: int, event_type: str,
                 stage: str = "", status: str = "", message: str = "",
                 detail: str = "", remote_plan_id: str = "",
                 screenshot_path: str = "", supplement_id: int = 0) -> None:
    """写入千川计划时间线；只保存运行元数据，不保存 Cookie、Token 或请求正文。"""
    db.execute("""INSERT INTO qianchuan_plan_events
        (plan_job_id,supplement_id,event_type,stage,status,message,detail,
         remote_plan_id,screenshot_path,created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (plan_job_id, supplement_id, event_type[:80], stage[:80], status[:40],
         message[:2000], detail[:4000], remote_plan_id[:80], screenshot_path[:500], now_text()))


def _upload_log_summary(db: sqlite3.Connection, account_id: int, qianchuan_account_id: int,
                        product_id: str) -> dict[str, Any]:
    """为计划日志补充素材上传结果；只读本机状态，不读取平台凭据或请求正文。"""
    table = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='qianchuan_upload_jobs'"
    ).fetchone()
    if not table:
        return {"total": 0, "succeeded": 0, "needsReview": 0, "failed": 0,
                "pending": 0, "remoteMaterialIds": [], "lastFailure": ""}
    rows = db.execute("""SELECT status,remote_material_id,error_message,failure_stage,updated_at
        FROM qianchuan_upload_jobs
        WHERE account_id=? AND qianchuan_account_id=? AND product_id=?
        ORDER BY id""", (account_id, qianchuan_account_id, product_id)).fetchall()
    ids = list(dict.fromkeys(str(row["remote_material_id"] or "") for row in rows
                            if str(row["remote_material_id"] or "")))
    failures = [
        f"{str(row['failure_stage'] or 'upload')}: {str(row['error_message'] or '上传失败')}"
        for row in rows if str(row["status"] or "") in {"failed", "needs_review", "rejected_quality"}
    ]
    return {
        "total": len(rows),
        "succeeded": sum(str(row["status"] or "") == "succeeded" for row in rows),
        "needsReview": sum(str(row["status"] or "") == "needs_review" for row in rows),
        "failed": sum(str(row["status"] or "") in {"failed", "rejected_quality"} for row in rows),
        "pending": sum(str(row["status"] or "") in {"queued", "uploading"} for row in rows),
        "remoteMaterialIds": ids,
        "lastFailure": failures[-1][:2000] if failures else "",
    }


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


def plan_candidates(db: sqlite3.Connection, account_id: int = 0,
                    qianchuan_account_id: int = 0) -> dict[str, Any]:
    """列出已发布且绑定目标千川账户的商品，并说明是否可在计划工作区手动建计划。"""
    has_products = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='baiying_products'"
    ).fetchone() is not None
    product_columns = ({str(row["name"]) for row in db.execute("PRAGMA table_info(baiying_products)")}
                       if has_products else set())
    has_products = {"product_id", "account_id", "cos_ratio"}.issubset(product_columns)
    commission = "b.cos_ratio" if has_products else "NULL"
    commission_join = ("LEFT JOIN baiying_products b ON b.product_id=p.product_id AND b.account_id=p.account_id"
                       if has_products else "")
    filters = ["p.platform_status='published'", "a.status='active'", "p.product_id<>''"]
    params: list[Any] = []
    if account_id:
        filters.append("p.account_id=?")
        params.append(account_id)
    if qianchuan_account_id:
        filters.append("q.id=?")
        params.append(qianchuan_account_id)
    query = f"""SELECT p.id publish_queue_id,p.account_id,p.product_id,p.product_name,p.title,
        q.id qianchuan_account_id,q.qc_id advertiser_id,
        COALESCE(s.daily_budget_yuan,0) daily_budget_yuan,{commission} commission_rate
        FROM publish_queue p JOIN accounts a ON a.id=p.account_id
        JOIN account_qianchuan aq ON aq.account_row_id=p.account_id
        JOIN qianchuan_accounts q ON q.id=aq.qianchuan_row_id
        LEFT JOIN qianchuan_plan_settings s ON s.qianchuan_account_id=q.id
        {commission_join}
        WHERE {' AND '.join(filters)}
        AND p.id=(SELECT MAX(p2.id) FROM publish_queue p2
          WHERE p2.account_id=p.account_id AND p2.product_id=p.product_id
          AND p2.platform_status='published')
        ORDER BY p.id DESC LIMIT 500"""
    rows = db.execute(query, params).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        account = int(row["account_id"])
        qc_id = int(row["qianchuan_account_id"])
        product_id = str(row["product_id"])
        queue_id = int(row["publish_queue_id"])
        advertiser = str(row["advertiser_id"] or "")
        ids = usable_material_ids(db, account, qc_id, product_id)
        duplicate_ids = duplicate_material_ids(db, account, qc_id, product_id)
        settled = uploads_settled(db, account, qc_id, product_id, queue_id)
        try:
            rate = float(row["commission_rate"])
        except (TypeError, ValueError):
            rate = 0
        roi = round(100 / rate, 2) if 0 < rate <= 100 else 0
        existing = db.execute("""SELECT id,status,submit_attempted FROM qianchuan_plan_jobs
            WHERE account_id=? AND qianchuan_account_id=? AND product_id=?""",
            (account, qc_id, product_id)).fetchone()
        reason = ""
        if not re.fullmatch(r"\d{8,20}", advertiser):
            reason = "所选千川档案的广告主 ID 未填写或格式无效（需 8–20 位数字）；请核对账号管理中的千川账户档案并刷新。"
        elif not settled:
            reason = "素材上传或发布后素材处理尚未完成，暂不可建计划"
        elif duplicate_ids:
            reason = "远端素材 ID 重复，暂不可建计划：" + ",".join(duplicate_ids[:5])
        elif not ids:
            reason = "该商品在目标千川账户没有可用的已上传素材"
        elif not roi:
            reason = "该账号该商品佣金率缺失或无效，暂不能计算 ROI"
        elif int(row["daily_budget_yuan"] or 0) < 50:
            reason = "请先在本页设置至少 50 元的计划日预算"
        elif existing:
            reason = f"已有计划任务 #{int(existing['id'])}（{existing['status']}），请在计划列表继续处理"
        result.append({
            "publishQueueId": queue_id, "accountId": account,
            "qianchuanAccountId": qc_id, "advertiserId": advertiser,
            "productId": product_id, "productName": str(row["product_name"] or ""),
            "publishedVideoTitle": str(row["title"] or ""),
            "dailyBudgetYuan": int(row["daily_budget_yuan"] or 0),
            "commissionRate": rate, "roiTarget": roi,
            "materialCount": len(ids), "remoteMaterialIds": ids,
            "existingPlanId": int(existing["id"]) if existing else 0,
            "existingPlanStatus": str(existing["status"]) if existing else "",
            "canCreate": not reason, "reason": reason,
        })
    return {"ok": True, "rows": result}


def create_manual_plan(db: sqlite3.Connection, publish_queue_id: int,
                       qianchuan_account_id: int) -> dict[str, Any]:
    """由千川计划工作区显式创建本地待发布任务；不触发平台提交。"""
    has_products = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='baiying_products'"
    ).fetchone() is not None
    product_columns = ({str(item["name"]) for item in db.execute("PRAGMA table_info(baiying_products)")}
                       if has_products else set())
    commission_column = "b.cos_ratio" if {"product_id", "account_id", "cos_ratio"}.issubset(product_columns) else "NULL"
    commission_join = ("LEFT JOIN baiying_products b ON b.product_id=p.product_id AND b.account_id=p.account_id"
                       if commission_column != "NULL" else "")
    row = db.execute(f"""SELECT p.id publish_queue_id,p.account_id,p.product_id,p.product_name,p.title,
        q.id qianchuan_account_id,q.qc_id advertiser_id,{commission_column} commission_rate
        FROM publish_queue p JOIN accounts a ON a.id=p.account_id
        JOIN account_qianchuan aq ON aq.account_row_id=p.account_id
        JOIN qianchuan_accounts q ON q.id=aq.qianchuan_row_id
        {commission_join}
        WHERE p.id=? AND q.id=? AND p.platform_status='published' AND a.status='active'
        AND p.product_id<>''""", (publish_queue_id, qianchuan_account_id)).fetchone()
    if not row:
        raise ValueError("所选作品未确认发布、账号已停用，或该抖音账号没有绑定目标千川账户")
    account_id = int(row["account_id"])
    qc_id = int(row["qianchuan_account_id"])
    product_id = str(row["product_id"])
    advertiser = str(row["advertiser_id"] or "")
    settings = _settings(db, qc_id)
    if int(settings["daily_budget_yuan"] or 0) < 50:
        raise ValueError("请先在千川计划工作区设置日预算，至少 50 元")
    if not re.fullmatch(r"\d{8,20}", advertiser):
        raise ValueError("目标千川档案的广告主 ID 未填写或格式无效（需 8–20 位数字）；请核对账号管理中的千川账户档案")
    if not uploads_settled(db, account_id, qc_id, product_id, publish_queue_id):
        raise ValueError("该商品的素材上传或发布后处理尚未完成")
    duplicate_ids = duplicate_material_ids(db, account_id, qc_id, product_id)
    if duplicate_ids:
        raise ValueError("目标千川账户内发现重复远端素材 ID，暂不可建计划：" + ",".join(duplicate_ids[:5]))
    material_ids = usable_material_ids(db, account_id, qc_id, product_id)
    if not material_ids:
        raise ValueError("该商品在目标千川账户没有可用的已上传素材")
    try:
        commission_rate = float(row["commission_rate"])
    except (TypeError, ValueError):
        commission_rate = 0
    if not 0 < commission_rate <= 100:
        raise ValueError("该账号该商品佣金率缺失或无效，请先同步百应商品数据")
    roi_target = round(100 / commission_rate, 2)
    existing = db.execute("""SELECT id,status,submit_attempted FROM qianchuan_plan_jobs
        WHERE account_id=? AND qianchuan_account_id=? AND product_id=?""",
        (account_id, qc_id, product_id)).fetchone()
    if existing:
        raise ValueError(f"该商品已有计划任务 #{int(existing['id'])}（{existing['status']}），请在计划列表继续处理")
    name = f"{date.today().isoformat()}_商品全域投放_{product_id}"[:50]
    with db:
        cursor = db.execute("""INSERT INTO qianchuan_plan_jobs
            (publish_queue_id,account_id,qianchuan_account_id,advertiser_id,product_id,
             product_name,published_video_title,plan_name,creation_source,daily_budget_yuan,commission_rate,
             roi_target,material_ids_json,status,reason,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'ready','',?,?)""",
            (publish_queue_id, account_id, qc_id, advertiser, product_id,
             row["product_name"], row["title"], name, "manual", int(settings["daily_budget_yuan"]), commission_rate,
             roi_target, json.dumps(material_ids), now_text(), now_text()))
        if cursor.rowcount != 1:
            raise ValueError("计划任务未能创建，请刷新列表后核对")
        record_event(db, int(cursor.lastrowid), "PLAN_DRAFT_CREATED", "draft", "ready",
                     "已生成本地待发布计划，尚未触发千川提交",
                      f"来源=manual; 商品 ID={product_id}; 素材上传结果=已确认 {len(material_ids)} 条; "
                      f"远端素材 ID={','.join(material_ids)}; 日预算={int(settings['daily_budget_yuan'])}; "
                      f"保本 ROI={roi_target:.2f}")
    return {"ok": True, "job": _row(db.execute(
        "SELECT * FROM qianchuan_plan_jobs WHERE id=?", (cursor.lastrowid,)).fetchone())}


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


def duplicate_material_ids(db: sqlite3.Connection, account_id: int, qc_id: int,
                           product_id: str) -> list[str]:
    """在目标千川广告主范围内，发现同一远端素材 ID 被多条本地记录认领。"""
    rows = db.execute("""SELECT DISTINCT remote_material_id FROM qianchuan_upload_jobs
        WHERE account_id=? AND qianchuan_account_id=? AND product_id=?
        AND status='succeeded' AND remote_material_id<>''""",
        (account_id, qc_id, product_id)).fetchall()
    material_ids = sorted({str(row["remote_material_id"]) for row in rows
                           if re.fullmatch(r"\d{8,20}", str(row["remote_material_id"]))})
    if not material_ids:
        return []
    placeholders = ",".join("?" for _ in material_ids)
    duplicates = db.execute(f"""SELECT remote_material_id FROM qianchuan_upload_jobs
        WHERE qianchuan_account_id=? AND status='succeeded'
        AND remote_material_id IN ({placeholders})
        GROUP BY remote_material_id HAVING COUNT(*)>1""",
        [qc_id, *material_ids]).fetchall()
    return sorted(str(row["remote_material_id"]) for row in duplicates)


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
        if duplicate_material_ids(db, int(plan["account_id"]), int(plan["qianchuan_account_id"]),
                                 str(plan["product_id"])):
            continue
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
        record_event(db, int(job["plan_job_id"]), "SUPPLEMENT_SUBMIT_STARTED", "supplement", "appending",
                     f"开始向原计划追加素材 {job['remote_material_id']}",
                     f"追加任务 #{supplement_id}; 素材 ID={job['remote_material_id']}",
                     str(job["remote_plan_id"] or ""), supplement_id=supplement_id)
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
        event_type = {
            "applied": "SUPPLEMENT_APPLIED",
            "already_present": "SUPPLEMENT_ALREADY_PRESENT",
            "waiting": "SUPPLEMENT_WAITING",
            "needs_review": "SUPPLEMENT_SUBMISSION_UNCERTAIN",
            "blocked": "SUPPLEMENT_BLOCKED",
        }[status]
        message = {
            "applied": "追加素材已确认写入原千川计划",
            "already_present": "平台已存在该素材，未重复追加",
            "waiting": "追加暂未执行，已重新排队等待下一次处理",
            "needs_review": "追加结果不明确，等待核查原计划",
            "blocked": "追加在提交前停止，未继续操作",
        }[status]
        record_event(db, int(row["plan_job_id"]), event_type, "supplement", status,
                     message, reason or "追加任务已记录", screenshot_path=screenshot,
                     supplement_id=supplement_id)
    return {"ok": True, "job": _row(db.execute(
        "SELECT * FROM qianchuan_plan_supplements WHERE id=?", (supplement_id,)).fetchone())}


def sync(db: sqlite3.Connection) -> dict[str, Any]:
    """仅为已发布作品、已配置预算且有可识别素材的商品生成一个任务。"""
    has_baiying_products = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='baiying_products'"
    ).fetchone() is not None
    if has_baiying_products:
        columns = {str(column["name"]) for column in db.execute("PRAGMA table_info(baiying_products)")}
        has_baiying_products = {"product_id", "account_id", "cos_ratio"}.issubset(columns)
    commission_column = "b.cos_ratio" if has_baiying_products else "NULL"
    commission_join = ("LEFT JOIN baiying_products b ON b.product_id=p.product_id "
                       "AND b.account_id=p.account_id") if has_baiying_products else ""
    candidates = db.execute(f"""SELECT p.id publish_queue_id,p.account_id,p.product_id,
        p.product_name,p.title,q.id qianchuan_account_id,q.qc_id advertiser_id,
        s.video_source,s.daily_budget_yuan,{commission_column} commission_rate FROM publish_queue p
        JOIN accounts a ON a.id=p.account_id
        JOIN account_qianchuan aq ON aq.account_row_id=p.account_id
        JOIN qianchuan_accounts q ON q.id=aq.qianchuan_row_id
        JOIN qianchuan_plan_settings s ON s.qianchuan_account_id=q.id
        {commission_join}
        LEFT JOIN qianchuan_plan_jobs existing ON existing.account_id=p.account_id
        AND existing.qianchuan_account_id=q.id AND existing.product_id=p.product_id
        WHERE p.platform_status='published' AND a.status='active' AND s.enabled=1 AND s.daily_budget_yuan>=50
        AND s.activated_at<>'' AND p.published_at>s.activated_at
        AND p.product_id<>'' AND (existing.id IS NULL OR
            (existing.status IN ('ready','blocked') AND existing.submit_attempted=0))
        ORDER BY p.id""").fetchall()
    created = 0
    for row in candidates:
        account_id, qc_id, product_id = int(row["account_id"]), int(row["qianchuan_account_id"]), str(row["product_id"])
        advertiser_id = str(row["advertiser_id"] or "")
        if not re.fullmatch(r"\d{8,20}", advertiser_id):
            continue
        if duplicate_material_ids(db, account_id, qc_id, product_id):
            with db:
                db.execute("""UPDATE qianchuan_plan_jobs SET status='blocked',
                    reason='目标千川账户内发现重复远端素材 ID，请先核对素材归属',updated_at=?
                    WHERE account_id=? AND qianchuan_account_id=? AND product_id=?
                    AND status IN ('ready','blocked') AND submit_attempted=0""",
                    (now_text(), account_id, qc_id, product_id))
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
                 product_name,published_video_title,plan_name,daily_budget_yuan,commission_rate,roi_target,
                 material_ids_json,status,reason,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (row["publish_queue_id"], account_id, qc_id, advertiser_id, product_id,
                 row["product_name"], row["title"], name, int(row["daily_budget_yuan"]), commission_rate, roi_target,
                 json.dumps(ids), status, reason, now_text(), now_text()))
            created += int(cursor.rowcount > 0)
            if cursor.rowcount > 0:
                record_event(db, int(cursor.lastrowid), "PLAN_DRAFT_CREATED", "draft", status,
                             "自动调度已生成千川计划待发布任务",
                              f"来源=auto; 商品 ID={product_id}; 素材上传结果=已确认 {len(ids)} 条; "
                              f"远端素材 ID={','.join(ids)}; 日预算={int(row['daily_budget_yuan'])}; "
                              f"保本 ROI={roi_target:.2f}")
            if roi_target > 0:
                db.execute("""UPDATE qianchuan_plan_jobs SET daily_budget_yuan=?,commission_rate=?,roi_target=?,
                    status=CASE WHEN status='blocked' AND reason LIKE '无法计算保本 ROI：%' THEN 'ready' ELSE status END,
                    reason=CASE WHEN status='blocked' AND reason LIKE '无法计算保本 ROI：%' THEN '' ELSE reason END,
                    updated_at=? WHERE account_id=? AND qianchuan_account_id=? AND product_id=?
                    AND status IN ('ready','blocked') AND submit_attempted=0""",
                    (int(row["daily_budget_yuan"]), commission_rate, roi_target, now_text(), account_id, qc_id, product_id))
            else:
                db.execute("""UPDATE qianchuan_plan_jobs SET commission_rate=0,roi_target=0,
                    status='blocked',reason=?,updated_at=?
                    WHERE account_id=? AND qianchuan_account_id=? AND product_id=?
                    AND status='ready' AND submit_attempted=0""",
                    (reason, now_text(), account_id, qc_id, product_id))
    return {"ok": True, "created": created}


def claim(db: sqlite3.Connection, job_id: int, expected_daily_budget_yuan: int = 0) -> dict[str, Any]:
    db.commit()
    db.execute("BEGIN IMMEDIATE")
    try:
        row = db.execute("SELECT * FROM qianchuan_plan_jobs WHERE id=?", (job_id,)).fetchone()
        if not row or row["status"] != "ready":
            raise ValueError("计划任务不存在或已执行；不会重复发布")
        settings = _settings(db, int(row["qianchuan_account_id"]))
        if (not settings["enabled"] and str(row["creation_source"] or "auto") != "manual") or int(settings["daily_budget_yuan"]) < 50:
            raise ValueError("该千川账户未启用自动发布或未配置至少 50 元日预算")
        current_budget = int(settings["daily_budget_yuan"])
        if expected_daily_budget_yuan and current_budget != expected_daily_budget_yuan:
            raise ValueError("计划预算已变化，请刷新设置并重新确认后再发布")
        if float(row["roi_target"] or 0) <= 0 or float(row["commission_rate"] or 0) <= 0:
            raise ValueError("商品佣金率未就绪，无法计算控成本投放保本 ROI")
        cache_columns = ({str(column["name"]) for column in db.execute("PRAGMA table_info(baiying_products)")}
                         if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='baiying_products'").fetchone()
                         else set())
        if not {"product_id", "account_id", "cos_ratio"}.issubset(cache_columns):
            raise ValueError("百应佣金率缓存不可用，请先同步商品数据")
        cached = db.execute("SELECT cos_ratio FROM baiying_products WHERE product_id=? AND account_id=?",
                            (row["product_id"], row["account_id"])).fetchone()
        try:
            current_rate = float(cached["cos_ratio"]) if cached else 0
        except (TypeError, ValueError):
            current_rate = 0
        if not 0 < current_rate <= 100:
            raise ValueError("该账号商品佣金率已失效，请先同步计划任务")
        if current_rate != float(row["commission_rate"]) or round(100 / current_rate, 2) != float(row["roi_target"]):
            raise ValueError("商品佣金率已变化，请先同步计划任务和保本 ROI")
        published = db.execute("SELECT platform_status FROM publish_queue WHERE id=?", (row["publish_queue_id"],)).fetchone()
        if not published or published["platform_status"] != "published":
            raise ValueError("抖音作品未确认发布")
        account = db.execute("SELECT status FROM accounts WHERE id=?", (row["account_id"],)).fetchone()
        if not account or account["status"] != "active":
            raise ValueError("抖音账号已停用")
        if duplicate_material_ids(db, int(row["account_id"]), int(row["qianchuan_account_id"]), str(row["product_id"])):
            raise ValueError("目标千川账户内发现重复远端素材 ID，请先核对素材归属")
        ids = usable_material_ids(db, int(row["account_id"]), int(row["qianchuan_account_id"]), str(row["product_id"]))
        if settings["video_source"] in ("uploaded", "both") and not ids:
            raise ValueError("已上传素材尚未就绪")
        if settings["video_source"] in ("uploaded", "both") and not uploads_settled(
                db, int(row["account_id"]), int(row["qianchuan_account_id"]),
                str(row["product_id"]), int(row["publish_queue_id"])):
            raise ValueError("该商品上传或自动补位仍在进行")
        db.execute("UPDATE qianchuan_plan_jobs SET status='submitting',daily_budget_yuan=?,material_ids_json=?,reason='',updated_at=? WHERE id=?",
                   (current_budget, json.dumps(ids), now_text(), job_id))
        record_event(db, job_id, "PLAN_SUBMIT_STARTED", "submit", "submitting",
                     "已通过发布前校验，开始进入千川计划提交页面",
                     f"广告主 ID={row['advertiser_id']}; 日预算={current_budget}; "
                      f"保本 ROI={float(row['roi_target']):.2f}; 素材上传结果=已确认 {len(ids)} 条; "
                      f"远端素材 ID={','.join(ids)}")
        db.commit()
        job = _row(db.execute("SELECT * FROM qianchuan_plan_jobs WHERE id=?", (job_id,)).fetchone())
        return {"ok": True, "job": job, "settings": settings}
    except Exception:
        db.rollback()
        raise


def defer(db: sqlite3.Connection, job_id: int, reason: str) -> dict[str, Any]:
    """记录可自动恢复的本地前置等待，不把未提交任务误标成发布失败。"""
    message = reason.strip()[:2000] or "等待千川账号浏览器就绪"
    row = db.execute("SELECT status,submit_attempted,reason FROM qianchuan_plan_jobs WHERE id=?",
                     (job_id,)).fetchone()
    if not row or row["status"] != "ready" or row["submit_attempted"]:
        raise ValueError("只有尚未提交的待发布计划可以记录等待原因")
    if str(row["reason"] or "") != message:
        with db:
            updated = db.execute("UPDATE qianchuan_plan_jobs SET reason=?,updated_at=? WHERE id=? AND status='ready' AND submit_attempted=0",
                                 (message, now_text(), job_id))
            if updated.rowcount != 1:
                raise ValueError("计划任务已变化，不能覆盖当前状态")
            record_event(db, job_id, "PLAN_DEFERRED", "automatic_preflight", "ready",
                         "计划暂缓执行并保留在队列中", message)
    return {"ok": True, "job": _row(db.execute(
        "SELECT * FROM qianchuan_plan_jobs WHERE id=?", (job_id,)).fetchone())}


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
        event_type = {
            "published": "PLAN_PUBLISHED",
            "needs_review": "PLAN_SUBMISSION_UNCERTAIN",
            "blocked": "PLAN_BLOCKED",
        }[status]
        if status == "published":
            message = f"千川计划已发布，计划 ID：{remote_id}"
        elif status == "needs_review":
            message = "千川提交结果不明确，已暂停后续自动操作，等待人工核查"
        else:
            message = "计划在提交前停止，未触发千川付费投放"
        record_event(db, job_id, event_type, "result", status, message,
                     reason or "浏览器执行器已返回终态", remote_id, screenshot)
    return {"ok": True, "job": _row(db.execute("SELECT * FROM qianchuan_plan_jobs WHERE id=?", (job_id,)).fetchone())}


def list_plan_logs(db: sqlite3.Connection, account_id: int = 0,
                   qianchuan_account_id: int = 0, status: str = "",
                   start_date: str = "", end_date: str = "", offset: int = 0,
                   limit: int = 25) -> dict[str, Any]:
    """按计划查看发布历史摘要；详情由 plan_log_details 返回。"""
    conditions: list[str] = []
    params: list[Any] = []
    if account_id:
        conditions.append("p.account_id=?")
        params.append(account_id)
    if qianchuan_account_id:
        conditions.append("p.qianchuan_account_id=?")
        params.append(qianchuan_account_id)
    if status:
        conditions.append("p.status=?")
        params.append(status)
    if start_date:
        conditions.append("substr(p.created_at,1,10)>=?")
        params.append(start_date)
    if end_date:
        conditions.append("substr(p.created_at,1,10)<=?")
        params.append(end_date)
    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    total = int(db.execute("SELECT COUNT(*) FROM qianchuan_plan_jobs p" + where, params).fetchone()[0])
    query = """SELECT p.*,a.name account_name,q.qc_name,
        (SELECT COUNT(*) FROM qianchuan_plan_events e WHERE e.plan_job_id=p.id) event_count,
        (SELECT MAX(e.created_at) FROM qianchuan_plan_events e WHERE e.plan_job_id=p.id) last_event_at
        FROM qianchuan_plan_jobs p
        LEFT JOIN accounts a ON a.id=p.account_id
        LEFT JOIN qianchuan_accounts q ON q.id=p.qianchuan_account_id""" + where + \
        " ORDER BY COALESCE(last_event_at,p.updated_at) DESC,p.id DESC LIMIT ? OFFSET ?"
    rows = []
    for row in db.execute(query, [*params, min(max(limit, 1), 200), max(offset, 0)]):
        item = dict(row)
        item["upload_summary"] = _upload_log_summary(
            db, int(item["account_id"]), int(item["qianchuan_account_id"]), str(item["product_id"])
        )
        rows.append(item)
    account_rows = db.execute("""SELECT DISTINCT p.account_id,COALESCE(a.name,'') account_name
        FROM qianchuan_plan_jobs p LEFT JOIN accounts a ON a.id=p.account_id
        ORDER BY account_name,p.account_id""").fetchall()
    return {"ok": True, "items": rows, "total": total,
            "offset": max(offset, 0), "limit": min(max(limit, 1), 200),
            "accountOptions": [{"accountId": int(row["account_id"]),
                                 "accountName": str(row["account_name"] or "")}
                                for row in account_rows]}


def plan_log_details(db: sqlite3.Connection, job_id: int) -> dict[str, Any]:
    job = db.execute("""SELECT p.*,a.name account_name,q.qc_name
        FROM qianchuan_plan_jobs p
        LEFT JOIN accounts a ON a.id=p.account_id
        LEFT JOIN qianchuan_accounts q ON q.id=p.qianchuan_account_id
        WHERE p.id=?""", (job_id,)).fetchone()
    if not job:
        raise ValueError("千川计划任务不存在")
    events = [dict(row) for row in db.execute(
        "SELECT * FROM qianchuan_plan_events WHERE plan_job_id=? ORDER BY created_at,id",
        (job_id,)).fetchall()]
    item = dict(job)
    item["upload_summary"] = _upload_log_summary(
        db, int(item["account_id"]), int(item["qianchuan_account_id"]), str(item["product_id"])
    )
    return {"ok": True, "job": item, "events": events}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=Path(DEFAULT_DB_PATH))
    parser.add_argument("command", choices=["settings", "set-settings", "sync", "candidates", "create-manual", "jobs", "logs", "claim", "finish", "defer", "recover", "retry-blocked", "resolve-present", "resolve-absent", "sync-supplements", "supplement-jobs", "claim-supplement", "finish-supplement"])
    parser.add_argument("--account-id", type=int, default=0)
    parser.add_argument("--publish-queue-id", type=int, default=0)
    parser.add_argument("--qianchuan-account-id", type=int, default=0)
    parser.add_argument("--daily-budget-yuan", type=int, default=0)
    parser.add_argument("--expected-daily-budget-yuan", type=int, default=0)
    parser.add_argument("--video-source", default="both")
    parser.add_argument("--enabled", type=int, choices=[0, 1], default=0)
    parser.add_argument("--long-running", type=int, choices=[0, 1], default=1)
    parser.add_argument("--smart-coupon", type=int, choices=[0, 1], default=1)
    parser.add_argument("--aigc-enabled", type=int, choices=[0, 1], default=0)
    parser.add_argument("--id", type=int, default=0)
    parser.add_argument("--status", default="")
    parser.add_argument("--due-only", action="store_true")
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--submit-attempted", type=int, choices=[0, 1], default=0)
    parser.add_argument("--remote-plan-id", default="")
    parser.add_argument("--reason", default="")
    parser.add_argument("--screenshot-path", default="")
    parser.add_argument("--selected-material-ids-json", default="[]")
    parser.add_argument("--start-date", default="")
    parser.add_argument("--end-date", default="")
    args = parser.parse_args()
    try:
        if args.offset < 0 or not 1 <= args.limit <= 200:
            raise ValueError("任务列表分页参数无效")
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
            elif args.command == "candidates":
                result = plan_candidates(db, args.account_id, args.qianchuan_account_id)
            elif args.command == "create-manual":
                result = create_manual_plan(db, args.publish_queue_id, args.qianchuan_account_id)
            elif args.command == "sync-supplements":
                result = sync_supplements(db)
            elif args.command == "jobs":
                query = "SELECT * FROM qianchuan_plan_jobs"
                filters: list[Any] = []
                conditions: list[str] = []
                if args.qianchuan_account_id:
                    conditions.append("qianchuan_account_id=?")
                    filters.append(args.qianchuan_account_id)
                if args.account_id:
                    conditions.append("account_id=?")
                    filters.append(args.account_id)
                if args.id:
                    conditions.append("id=?")
                    filters.append(args.id)
                if args.status:
                    conditions.append("status=?")
                    filters.append(args.status)
                if conditions:
                    query += " WHERE " + " AND ".join(conditions)
                order = "ASC" if args.status == "ready" else "DESC"
                count_query = "SELECT COUNT(*) FROM qianchuan_plan_jobs"
                if conditions:
                    count_query += " WHERE " + " AND ".join(conditions)
                total = int(db.execute(count_query, filters).fetchone()[0])
                result = {"ok": True, "rows": [dict(r) for r in db.execute(
                    query + f" ORDER BY id {order} LIMIT ? OFFSET ?", [*filters, args.limit, args.offset])],
                    "total": total, "offset": args.offset, "limit": args.limit}
            elif args.command == "logs":
                if args.id:
                    result = plan_log_details(db, args.id)
                else:
                    result = list_plan_logs(db, args.account_id, args.qianchuan_account_id,
                                            args.status, args.start_date, args.end_date,
                                            args.offset, args.limit)
            elif args.command == "supplement-jobs":
                query = """
                    SELECT s.*,p.account_id,p.qianchuan_account_id,p.advertiser_id,
                    p.product_id,p.plan_name,p.remote_plan_id FROM qianchuan_plan_supplements s
                    JOIN qianchuan_plan_jobs p ON p.id=s.plan_job_id
                """
                conditions = []
                filters = []
                if args.id:
                    conditions.append("s.id=?")
                    filters.append(args.id)
                if args.status:
                    conditions.append("s.status=?")
                    filters.append(args.status)
                if args.due_only:
                    conditions.append("s.scheduled_at<=?")
                    filters.append(now_text())
                if conditions:
                    query += " WHERE " + " AND ".join(conditions)
                query += (" ORDER BY s.scheduled_at ASC,s.id ASC LIMIT 200 OFFSET ?" if args.due_only
                          else " ORDER BY s.id DESC LIMIT 200 OFFSET ?")
                result = {"ok": True, "rows": [dict(r) for r in db.execute(query, [*filters, args.offset])]}
            elif args.command == "claim":
                result = claim(db, args.id, args.expected_daily_budget_yuan)
            elif args.command == "defer":
                result = defer(db, args.id, args.reason)
            elif args.command == "claim-supplement":
                result = claim_supplement(db, args.id)
            elif args.command == "recover":
                with db:
                    recovering = db.execute("""SELECT id FROM qianchuan_plan_jobs WHERE status='submitting'""").fetchall()
                    changed = db.execute("""UPDATE qianchuan_plan_jobs SET status='needs_review',
                        reason='客户端曾在计划提交阶段中断；请先到千川核对是否已发布',updated_at=?
                        WHERE status='submitting'""", (now_text(),)).rowcount
                    for row in recovering:
                        record_event(db, int(row["id"]), "PLAN_RECOVERED_TO_REVIEW", "recovery", "needs_review",
                                     "客户端重启发现计划提交阶段中断，已转为待核查",
                                     "必须先在千川核对是否已创建计划，禁止盲目重传")
                    supplement_rows = db.execute("""SELECT id,plan_job_id,remote_material_id
                        FROM qianchuan_plan_supplements WHERE status='appending'""").fetchall()
                    supplements = db.execute("""UPDATE qianchuan_plan_supplements
                        SET status='needs_review',reason='客户端曾在追加原计划素材时中断；请先核查远端',
                        updated_at=? WHERE status='appending'""", (now_text(),)).rowcount
                    for row in supplement_rows:
                        record_event(db, int(row["plan_job_id"]), "SUPPLEMENT_RECOVERED_TO_REVIEW", "recovery", "needs_review",
                                     "客户端重启发现追加素材阶段中断，已转为待核查",
                                     f"追加任务 #{row['id']}; 素材 ID={row['remote_material_id']}",
                                     supplement_id=int(row["id"]))
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
                    if args.command == "retry-blocked":
                        record_event(db, args.id, "PLAN_RETRY_QUEUED", "recovery", "ready",
                                     "发布前停止的计划已重新排队，等待再次确认预算后提交",
                                     args.reason.strip()[:400])
                    elif status == "published":
                        record_event(db, args.id, "PLAN_MANUALLY_RESOLVED", "review", "published",
                                     f"人工核查确认千川计划已存在，计划 ID：{remote_id}",
                                     args.reason.strip()[:400], remote_id)
                    else:
                        record_event(db, args.id, "PLAN_MANUALLY_RESOLVED", "review", "ready",
                                     "人工核查确认远端不存在，计划重新回到待发布",
                                     args.reason.strip()[:400])
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
