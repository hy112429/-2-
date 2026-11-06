#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Step 20 千川违规素材治理。浏览器只连接 Step 17 的账户专属 CDP。"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from db import DEFAULT_DB_PATH, connect, emit, now_text
from qianchuan_browser import (
    NAV_LABELS, UploadStopped, _check_page_gate, _material_context,
    _named_control, _one_visible, _select_page, read_verified_port,
)

REMOTE_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
VIOLATION = re.compile(r"^(?:违规|劣质|低质|不合规|审核不通过|审核拒绝)$")
DATE_TEXT = re.compile(r"20\d{2}[-/.]\d{1,2}[-/.]\d{1,2}(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?")
SCHEMA = """
CREATE TABLE IF NOT EXISTS qianchuan_violation_materials (
  qianchuan_account_id INTEGER NOT NULL, remote_material_id TEXT NOT NULL,
  material_name TEXT NOT NULL, verdict TEXT NOT NULL, reason TEXT NOT NULL,
  detected_at TEXT NOT NULL, deletable INTEGER NOT NULL DEFAULT 0, last_seen_at TEXT NOT NULL,
  PRIMARY KEY(qianchuan_account_id, remote_material_id)
);
CREATE TABLE IF NOT EXISTS qianchuan_violation_scans (
  qianchuan_account_id INTEGER PRIMARY KEY, completed_at TEXT NOT NULL, complete INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS qianchuan_violation_batches (
  id INTEGER PRIMARY KEY AUTOINCREMENT, qianchuan_account_id INTEGER NOT NULL,
  status TEXT NOT NULL, manifest_path TEXT NOT NULL, created_at TEXT NOT NULL,
  completed_at TEXT
);
CREATE TABLE IF NOT EXISTS qianchuan_violation_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id INTEGER NOT NULL REFERENCES qianchuan_violation_batches(id),
  remote_material_id TEXT NOT NULL, material_name TEXT NOT NULL, verdict TEXT NOT NULL,
  reason TEXT NOT NULL, detected_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
  outcome_reason TEXT NOT NULL DEFAULT '', screenshot_path TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL, UNIQUE(batch_id, remote_material_id)
);
CREATE TABLE IF NOT EXISTS qianchuan_violation_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id INTEGER, item_id INTEGER,
  qianchuan_account_id INTEGER NOT NULL, stage TEXT NOT NULL, outcome TEXT NOT NULL,
  details TEXT NOT NULL, created_at TEXT NOT NULL
);
"""


def ensure_schema(db: sqlite3.Connection) -> None:
    db.executescript(SCHEMA)
    columns = {row["name"] for row in db.execute("PRAGMA table_info(qianchuan_violation_materials)")}
    if "deletable" not in columns:
        db.execute("ALTER TABLE qianchuan_violation_materials ADD COLUMN deletable INTEGER NOT NULL DEFAULT 0")
    db.commit()


def event(db: sqlite3.Connection, qc_id: int, stage: str, outcome: str,
          details: str = "", batch_id: int | None = None, item_id: int | None = None) -> None:
    db.execute("INSERT INTO qianchuan_violation_events "
               "(batch_id,item_id,qianchuan_account_id,stage,outcome,details,created_at) "
               "VALUES (?,?,?,?,?,?,?)",
               (batch_id, item_id, qc_id, stage, outcome, details[:2000], now_text()))


def require_account(db: sqlite3.Connection, qc_id: int) -> None:
    if qc_id < 1 or db.execute("SELECT 1 FROM qianchuan_accounts WHERE id=?", (qc_id,)).fetchone() is None:
        raise ValueError("千川账户不存在")


def _material_page(browser: Any) -> Any:
    page = _select_page(browser)
    for _ in range(4):
        _check_page_gate(page, "violation_list")
        if _material_context(page):
            return page
        control = next((found for label in NAV_LABELS
                        if (found := _named_control(page, label)) is not None), None)
        if control is None:
            break
        previous = set(page.context.pages)
        control.click(timeout=5_000)
        created = [item for item in page.context.pages if item not in previous]
        if len(created) > 1:
            raise UploadStopped("千川素材入口同时打开多个页面", stage="navigate")
        if created:
            page = created[0]
        try:
            page.wait_for_load_state("domcontentloaded", timeout=10_000)
        except Exception:
            pass
    raise UploadStopped("未找到千川素材列表，需要实页校准", stage="navigate")


def _read_page_rows(page: Any) -> list[dict[str, Any]]:
    return page.evaluate("""() => [...document.querySelectorAll('tr,[role="row"],[data-material-id]')]
      .filter(el => el.getClientRects().length)
      .map(el => {
        const cells = [...el.querySelectorAll('td,[role="cell"]')].map(x => (x.innerText || '').trim());
        const verdict = cells.slice(1).find(x => /^(违规|劣质|低质|不合规|审核不通过|审核拒绝)$/.test(x)) || '';
        const ownId = el.getAttribute('data-material-id') || el.getAttribute('data-creative-id') || '';
        const link = el.querySelector('a[href*="material"]')?.getAttribute('href') || '';
        const linkedId = /(?:material[_-]?id|creative[_-]?id)=([A-Za-z0-9_-]{8,64})/i.exec(link)?.[1] || '';
        return {remoteMaterialId: ownId || linkedId, deletable: !!ownId, verdict,
          materialName: el.querySelector('[title]')?.getAttribute('title') || cells[0] || '',
          raw: (el.innerText || '').trim().slice(0, 1500), cells};
      }).filter(row => !!row.verdict)""")


def _parse_row(raw: dict[str, Any]) -> dict[str, Any]:
    verdict = str(raw.get("verdict", ""))
    material_id = str(raw.get("remoteMaterialId", "")).strip()
    cells = [str(cell) for cell in raw.get("cells", [])]
    reason = next((cell for cell in cells if "原因" in cell), "")
    date_cell = next((cell for cell in cells if "检测" in cell or "审核" in cell), "")
    date = DATE_TEXT.search(date_cell)
    return {
        "remoteMaterialId": material_id if REMOTE_ID.fullmatch(material_id) else "",
        "materialName": str(raw.get("materialName", "")).strip()[:300],
        "verdict": verdict if VIOLATION.fullmatch(verdict) else "",
        "reason": reason[:500], "detectedAt": date.group(0) if date else "",
        "deletable": bool(raw.get("deletable") and REMOTE_ID.fullmatch(material_id)),
    }


def _next_page(page: Any) -> bool:
    for label in ("下一页", "下页"):
        control = _one_visible(page.get_by_role("button", name=label, exact=True))
        if control is not None:
            if control.is_enabled():
                control.click(timeout=5_000)
                return True
            return False
    return False


def _connect(args: argparse.Namespace) -> Any:
    port = read_verified_port(args.profile_path, args.cdp_port, args.qianchuan_account_id)
    from playwright.sync_api import sync_playwright
    playwright = sync_playwright().start()
    try:
        browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}", timeout=15_000)
        return playwright, browser, _material_page(browser)
    except Exception:
        playwright.stop()
        raise


def scan(db: sqlite3.Connection, args: argparse.Namespace) -> dict[str, Any]:
    require_account(db, args.qianchuan_account_id)
    playwright, _browser, page = _connect(args)
    try:
        rows: dict[str, dict[str, Any]] = {}
        unidentifiable: list[dict[str, Any]] = []
        seen_pages: set[str] = set()
        complete = False
        for _ in range(50):
            _check_page_gate(page, "violation_list")
            raw = _read_page_rows(page)
            fingerprint = json.dumps(raw, ensure_ascii=False)
            if fingerprint in seen_pages:
                raise UploadStopped("千川列表翻页后内容未变，无法确认列表完整", stage="pagination")
            seen_pages.add(fingerprint)
            for item in map(_parse_row, raw):
                if item["remoteMaterialId"]:
                    rows[item["remoteMaterialId"]] = item
                else:
                    unidentifiable.append(item)
            if not _next_page(page):
                complete = True
                break
            page.wait_for_timeout(800)
        now = now_text()
        for item in rows.values():
            db.execute("INSERT INTO qianchuan_violation_materials "
                       "(qianchuan_account_id,remote_material_id,material_name,verdict,reason,detected_at,deletable,last_seen_at) "
                       "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(qianchuan_account_id,remote_material_id) "
                       "DO UPDATE SET material_name=excluded.material_name,verdict=excluded.verdict,"
                       "reason=excluded.reason,detected_at=excluded.detected_at,"
                       "deletable=excluded.deletable,last_seen_at=excluded.last_seen_at",
                       (args.qianchuan_account_id, item["remoteMaterialId"], item["materialName"],
                        item["verdict"], item["reason"], item["detectedAt"], int(item["deletable"]), now))
        db.execute("INSERT INTO qianchuan_violation_scans (qianchuan_account_id,completed_at,complete) "
                   "VALUES (?,?,?) ON CONFLICT(qianchuan_account_id) DO UPDATE SET "
                   "completed_at=excluded.completed_at,complete=excluded.complete",
                   (args.qianchuan_account_id, now, int(complete)))
        event(db, args.qianchuan_account_id, "scan", "complete" if complete else "partial",
              f"识别 {len(rows)} 条，缺少素材 ID {len(unidentifiable)} 条")
        db.commit()
        return {"ok": True, "rows": list(rows.values()) + unidentifiable, "complete": complete,
                "scannedAt": now}
    finally:
        playwright.stop()


def _batch(db: sqlite3.Connection, batch_id: int) -> sqlite3.Row:
    row = db.execute("SELECT * FROM qianchuan_violation_batches WHERE id=?", (batch_id,)).fetchone()
    if row is None:
        raise ValueError("删除批次不存在")
    return row


def batch_detail(db: sqlite3.Connection, batch_id: int) -> dict[str, Any]:
    recover_interrupted(db)
    batch = dict(_batch(db, batch_id))
    items = [dict(row) for row in db.execute(
        "SELECT * FROM qianchuan_violation_items WHERE batch_id=? ORDER BY id", (batch_id,))]
    return {"ok": True, "batch": batch, "items": items, "count": len(items)}


def recover_interrupted(db: sqlite3.Connection) -> None:
    cutoff = (datetime.now() - timedelta(minutes=30)).isoformat(timespec="seconds")
    rows = db.execute("SELECT i.id,i.batch_id,b.qianchuan_account_id FROM qianchuan_violation_items i "
                      "JOIN qianchuan_violation_batches b ON b.id=i.batch_id "
                      "WHERE i.status='deleting' AND i.updated_at<?", (cutoff,)).fetchall()
    for row in rows:
        db.execute("UPDATE qianchuan_violation_items SET status='needs_review',"
                   "outcome_reason='客户端中断后远端结果不确定，请人工核查',updated_at=? WHERE id=?",
                   (now_text(), row["id"]))
        db.execute("UPDATE qianchuan_violation_batches SET status='needs_review',completed_at=? WHERE id=?",
                   (now_text(), row["batch_id"]))
        event(db, row["qianchuan_account_id"], "recover", "needs_review",
              "删除中断，禁止自动重试", row["batch_id"], row["id"])
    if rows:
        db.commit()


def history(db: sqlite3.Connection, qc_id: int) -> dict[str, Any]:
    require_account(db, qc_id)
    recover_interrupted(db)
    rows = db.execute("SELECT b.*,COUNT(i.id) AS item_count,"
                      "SUM(CASE WHEN i.status='deleted' THEN 1 ELSE 0 END) AS deleted_count "
                      "FROM qianchuan_violation_batches b LEFT JOIN qianchuan_violation_items i ON i.batch_id=b.id "
                      "WHERE b.qianchuan_account_id=? GROUP BY b.id ORDER BY b.id DESC LIMIT 30", (qc_id,)).fetchall()
    return {"ok": True, "rows": [dict(row) for row in rows]}


def prepare(db: sqlite3.Connection, qc_id: int, ids: list[str], export_dir: Path) -> dict[str, Any]:
    require_account(db, qc_id)
    if not ids or len(ids) > 50 or len(set(ids)) != len(ids) or any(not REMOTE_ID.fullmatch(x) for x in ids):
        raise ValueError("请选择 1–50 条有唯一千川素材 ID 的违规素材")
    cutoff = (datetime.now() - timedelta(minutes=30)).isoformat(timespec="seconds")
    scan_row = db.execute("SELECT complete,completed_at FROM qianchuan_violation_scans "
                          "WHERE qianchuan_account_id=?", (qc_id,)).fetchone()
    if scan_row is None or not scan_row["complete"] or scan_row["completed_at"] < cutoff:
        raise ValueError("请先完整刷新该千川账户的违规素材列表")
    records = []
    for material_id in ids:
        row = db.execute("SELECT * FROM qianchuan_violation_materials WHERE qianchuan_account_id=? "
                         "AND remote_material_id=? AND last_seen_at>=?", (qc_id, material_id, cutoff)).fetchone()
        if row is None or not row["deletable"] or not VIOLATION.fullmatch(row["verdict"]):
            raise ValueError(f"素材 {material_id} 未在最近 30 分钟的违规列表中确认，请刷新")
        uncertain = db.execute("SELECT 1 FROM qianchuan_violation_items i "
                               "JOIN qianchuan_violation_batches b ON b.id=i.batch_id "
                               "WHERE b.qianchuan_account_id=? AND i.remote_material_id=? "
                               "AND i.status IN ('deleting','needs_review','deleted') LIMIT 1",
                               (qc_id, material_id)).fetchone()
        if uncertain:
            raise ValueError(f"素材 {material_id} 已删除或结果待核查，禁止重复提交")
        records.append(row)
    export_dir.mkdir(parents=True, exist_ok=True)
    filename = f"千川违规素材删除清单-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}.csv"
    manifest = export_dir / filename
    def safe_cell(value: Any) -> str:
        text = str(value or "")
        return "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) else text
    with manifest.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(("千川账户ID", "千川素材ID", "素材名称", "判定结果", "原因", "检测时间"))
        for row in records:
            writer.writerow(tuple(map(safe_cell, (qc_id, row["remote_material_id"], row["material_name"],
                                               row["verdict"], row["reason"], row["detected_at"]))))
    now = now_text()
    try:
        db.execute("BEGIN IMMEDIATE")
        cursor = db.execute("INSERT INTO qianchuan_violation_batches "
                            "(qianchuan_account_id,status,manifest_path,created_at) VALUES (?,?,?,?)",
                            (qc_id, "prepared", str(manifest), now))
        batch_id = cursor.lastrowid
        for row in records:
            db.execute("INSERT INTO qianchuan_violation_items "
                       "(batch_id,remote_material_id,material_name,verdict,reason,detected_at,status,updated_at) "
                       "VALUES (?,?,?,?,?,?,?,?)",
                       (batch_id, row["remote_material_id"], row["material_name"], row["verdict"],
                        row["reason"], row["detected_at"], "pending", now))
        event(db, qc_id, "prepare", "ready", f"{len(records)} 条；清单 {manifest}", batch_id)
        db.commit()
    except Exception:
        db.rollback()
        manifest.unlink(missing_ok=True)
        raise
    return batch_detail(db, batch_id)


def _find_target_row(page: Any, material_id: str) -> Any | None:
    selector = f'[data-material-id="{material_id}"],[data-creative-id="{material_id}"]'
    for _ in range(50):
        row = _one_visible(page.locator(selector))
        if row is not None:
            return row
        if not _next_page(page):
            return None
        page.wait_for_timeout(800)
    raise UploadStopped("千川素材翻页超过 50 页，无法确认目标是否存在", stage="pagination")


def _delete_remote(page: Any, material_id: str, mark_attempted: Any = None) -> tuple[str, str]:
    _check_page_gate(page, "delete")
    row = _find_target_row(page, material_id)
    if row is None:
        return "failed", "未唯一定位带千川素材 ID 的可见行，未点击删除"
    status_cells = [text.strip() for text in row.locator('td,[role="cell"]').all_inner_texts()]
    if not any(VIOLATION.fullmatch(text) for text in status_cells[1:]):
        return "failed", "目标行已不显示违规或劣质判定，未点击删除"
    control = _one_visible(row.get_by_role("button", name="删除", exact=True))
    if control is None:
        return "failed", "目标行没有唯一的删除按钮，未点击删除"
    if mark_attempted is not None:
        mark_attempted()
    control.click(timeout=5_000)
    # 平台确认框必须唯一；最终点击后若无法核实结果，一律待人工核查。
    dialog = _one_visible(page.get_by_role("dialog"))
    if dialog is None:
        return "needs_review", "已点删除，但未唯一识别平台确认框"
    if not re.search(r"删除", dialog.inner_text(timeout=5_000)):
        return "needs_review", "平台确认框未明确显示删除动作"
    confirm = _one_visible(dialog.get_by_role("button", name=re.compile(r"^(确认删除|确定删除|删除|确定)$")))
    if confirm is None:
        return "needs_review", "平台确认框没有唯一删除确认按钮"
    confirm.click(timeout=5_000)
    page.wait_for_timeout(1200)
    try:
        page.reload(wait_until="domcontentloaded", timeout=15_000)
        _check_page_gate(page, "verify")
        if not _material_context(page):
            return "needs_review", "删除后离开千川素材列表，无法核验结果"
        if _find_target_row(page, material_id) is None:
            return "deleted", "刷新后目标素材行已消失"
    except Exception as error:
        return "needs_review", f"点击平台确认后核验中断：{error}"
    return "needs_review", "点击平台确认后目标行仍存在，结果待核查"


def delete_one(db: sqlite3.Connection, args: argparse.Namespace) -> dict[str, Any]:
    batch = _batch(db, args.batch_id)
    qc_id = batch["qianchuan_account_id"]
    if qc_id != args.qianchuan_account_id:
        raise ValueError("批次与千川账户不一致")
    item = db.execute("SELECT * FROM qianchuan_violation_items WHERE id=? AND batch_id=?",
                      (args.item_id, args.batch_id)).fetchone()
    if item is None or item["status"] != "pending" or batch["status"] not in ("prepared", "running"):
        raise ValueError("该素材不在可执行的删除清单内")
    if batch["status"] == "prepared" and batch["created_at"] < (datetime.now() - timedelta(minutes=30)).isoformat(timespec="seconds"):
        raise ValueError("删除清单已超过 30 分钟，请刷新违规列表后重新生成")
    db.execute("UPDATE qianchuan_violation_batches SET status='running' WHERE id=?", (args.batch_id,))
    db.execute("UPDATE qianchuan_violation_items SET status='deleting',updated_at=? WHERE id=?",
               (now_text(), args.item_id))
    event(db, qc_id, "delete", "started", item["remote_material_id"], args.batch_id, args.item_id)
    db.commit()
    status, reason, screenshot = "failed", "删除前连接中断，可刷新列表后重试", ""
    attempted = False
    def mark_attempted() -> None:
        nonlocal attempted
        attempted = True
    playwright = None
    page = None
    try:
        playwright, _browser, page = _connect(args)
        status, reason = _delete_remote(page, item["remote_material_id"], mark_attempted)
    except Exception as error:
        reason = str(error)
        status = "needs_review" if attempted else "failed"
    finally:
        if status != "deleted" and page is not None:
            try:
                args.diagnostics_dir.mkdir(parents=True, exist_ok=True)
                path = args.diagnostics_dir / f"violation-delete-{args.batch_id}-{args.item_id}.png"
                page.screenshot(path=str(path), full_page=True, timeout=8_000)
                screenshot = str(path)
            except Exception:
                pass
        if playwright is not None:
            playwright.stop()
    db.execute("UPDATE qianchuan_violation_items SET status=?,outcome_reason=?,screenshot_path=?,updated_at=? WHERE id=?",
               (status, reason[:2000], screenshot, now_text(), args.item_id))
    event(db, qc_id, "delete", status, reason, args.batch_id, args.item_id)
    remaining = db.execute("SELECT COUNT(*) FROM qianchuan_violation_items WHERE batch_id=? AND status='pending'",
                           (args.batch_id,)).fetchone()[0]
    if status != "deleted" or remaining == 0:
        batch_status = "completed" if status == "deleted" else status
        db.execute("UPDATE qianchuan_violation_batches SET status=?,completed_at=? WHERE id=?",
                   (batch_status, now_text(), args.batch_id))
    db.commit()
    return {"ok": status == "deleted", "status": status, "reason": reason,
            "screenshotPath": screenshot, "batch": batch_detail(db, args.batch_id)}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="千川违规素材列表与删除审计")
    parser.add_argument("--db", type=Path, default=Path(DEFAULT_DB_PATH))
    commands = parser.add_subparsers(dest="command", required=True)
    scan_cmd = commands.add_parser("scan")
    prep = commands.add_parser("prepare")
    prep.add_argument("--ids", required=True)
    prep.add_argument("--export-dir", type=Path, required=True)
    detail = commands.add_parser("batch")
    detail.add_argument("--batch-id", type=int, required=True)
    old = commands.add_parser("history")
    old.add_argument("--qianchuan-account-id", type=int, required=True)
    delete = commands.add_parser("delete-one")
    delete.add_argument("--batch-id", type=int, required=True)
    delete.add_argument("--item-id", type=int, required=True)
    for cmd in (scan_cmd, prep, delete):
        cmd.add_argument("--qianchuan-account-id", type=int, required=True)
    for cmd in (scan_cmd, delete):
        cmd.add_argument("--profile-path", type=Path, required=True)
        cmd.add_argument("--cdp-port", type=int, required=True)
    delete.add_argument("--diagnostics-dir", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        with connect(args.db) as db:
            ensure_schema(db)
            if args.command == "scan":
                result = scan(db, args)
            elif args.command == "prepare":
                result = prepare(db, args.qianchuan_account_id,
                                 [item.strip() for item in args.ids.split(",")], args.export_dir)
            elif args.command == "batch":
                result = batch_detail(db, args.batch_id)
            elif args.command == "history":
                result = history(db, args.qianchuan_account_id)
            else:
                result = delete_one(db, args)
        return emit(result)
    except (ValueError, OSError, sqlite3.Error, UploadStopped, Exception) as error:
        return emit({"ok": False, "error": str(error)}, 1)


if __name__ == "__main__":
    sys.exit(main())
