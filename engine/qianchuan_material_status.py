"""只读查询千川视频库的审核状态与素材评估，并按远端 ID 写回本机上传任务。"""

from __future__ import annotations

import argparse
import re
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlparse

from db import connect, emit, now_text
from qianchuan_browser import (
    UploadStopped, _advertiser_matches_page, _check_page_gate,
    _one_visible, _open_material_area, _select_page, read_verified_port,
)
from qianchuan_upload import ensure_schema


REMOTE_ID = re.compile(r"\d{8,20}")


def _visible_status_row(page: Any, remote_id: str) -> dict[str, str] | None:
    """核对视频库列名，只读取视频列中精确匹配素材 ID 的唯一行。"""
    result = page.evaluate(r"""(remoteId) => {
      const tables = [...document.querySelectorAll('table')].filter(t => t.getClientRects().length);
      const matches = [];
      let schemaFound = false;
      for (const table of tables) {
        const headers = [...table.querySelectorAll('thead tr:first-child th')]
          .map(th => (th.innerText || '').trim());
        if (headers[1] !== '视频' || headers[3] !== '审核状态' || headers[4] !== '素材评估') continue;
        schemaFound = true;
        for (const row of table.querySelectorAll('tbody tr')) {
          if (!row.getClientRects().length) continue;
          const cells = [...row.querySelectorAll('td')];
          if (cells.length < 5) continue;
          const displayedId = (cells[1].innerText || '').match(/ID[：:]\s*(\d{8,20})/)?.[1];
          if (displayedId !== remoteId) continue;
          matches.push({
            remoteMaterialId: displayedId,
            materialName: (cells[1].querySelector('.title')?.textContent || '').trim(),
            reviewStatus: (cells[3].innerText || '').trim(),
            materialEvaluation: (cells[4].innerText || '').trim()
          });
        }
      }
      return {schemaFound, matches};
    }""", remote_id)
    if not result.get("schemaFound"):
        raise UploadStopped("千川视频库列名已变化，无法确认审核状态和素材评估", stage="status_read")
    matches = result.get("matches") or []
    if len(matches) > 1:
        raise UploadStopped("千川视频库出现重复素材 ID 行，停止写回状态", stage="status_read")
    return matches[0] if matches else None


def _read_status(page: Any, remote_id: str, material_name: str = "") -> dict[str, str]:
    if "/video-library" not in urlparse(page.url).path:
        raise UploadStopped("未进入千川视频库，不能读取素材判定", stage="status_read")
    search = _one_visible(page.locator('input[placeholder*="视频名称/ID搜索"]'))
    if search is None:
        raise UploadStopped("千川视频库搜索框未找到", stage="status_read")
    # 千川视频库对新上传素材的完整文件名搜索更稳定；无论用哪个条件查询，
    # 只接受同一行显示的精确远端 ID，避免把相似文件名的判定写错账号。
    queries = list(dict.fromkeys(query for query in (material_name, remote_id) if query))
    for query in queries:
        search.fill(query, timeout=5_000)
        search.press("Enter", timeout=5_000)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            _check_page_gate(page, "status_read")
            row = _visible_status_row(page, remote_id)
            if row is not None:
                return row
            time.sleep(0.5)
    raise UploadStopped("千川视频库没有查到此素材 ID，原判定保持不变", stage="status_read")


def save_status_for_remote(db: sqlite3.Connection, qc_id: int,
                           row: dict[str, str], checked_at: str) -> None:
    """同一个远端 ID 被多个本地任务引用时，判定必须一致。"""
    db.commit()
    db.execute("BEGIN IMMEDIATE")
    try:
        updated = db.execute(
            "UPDATE qianchuan_upload_jobs SET review_status=?, material_evaluation=?, "
            "evaluation_checked_at=? WHERE status='succeeded' AND remote_material_id=? "
            "AND qianchuan_account_id=?",
            (row["reviewStatus"], row["materialEvaluation"], checked_at,
             row["remoteMaterialId"], qc_id),
        )
        if updated.rowcount < 1:
            raise ValueError("上传任务已变化，平台判定未写回")
        db.commit()
    except Exception:
        db.rollback()
        raise


def refresh_status(db: sqlite3.Connection, args: argparse.Namespace) -> dict[str, Any]:
    ensure_schema(db)
    job = db.execute("SELECT * FROM qianchuan_upload_jobs WHERE id=?", (args.job_id,)).fetchone()
    if job is None or job["status"] != "succeeded" or not REMOTE_ID.fullmatch(job["remote_material_id"]):
        raise ValueError("仅能查询已有远端素材 ID 的成功上传任务")
    qc_id = int(job["qianchuan_account_id"])
    account_id = int(job["account_id"])
    binding = db.execute(
        "SELECT q.qc_id FROM account_qianchuan aq "
        "JOIN qianchuan_accounts q ON q.id=aq.qianchuan_row_id "
        "WHERE aq.account_row_id=? AND q.id=?", (account_id, qc_id),
    ).fetchone()
    if binding is None or binding["qc_id"] != args.advertiser_id:
        raise ValueError("上传任务的抖音账号与目标千川广告主绑定不一致")
    if args.douyin_account_id and args.douyin_account_id != account_id:
        raise ValueError("浏览器资料目录与上传任务账号不一致")
    port = read_verified_port(args.profile_path, args.cdp_port, qc_id, args.douyin_account_id)
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}", timeout=15_000)
        page = _select_page(browser, args.advertiser_id)
        _check_page_gate(page, "status_connect")
        page = _open_material_area(page, SimpleNamespace(min_delay_ms=0, max_delay_ms=0))
        if not _advertiser_matches_page(page, args.advertiser_id):
            raise UploadStopped("素材页广告主 ID 不匹配，未读取或写回判定", stage="status_read")
        row = _read_status(page, job["remote_material_id"], job["material_name"])

    checked_at = now_text()
    save_status_for_remote(db, qc_id, row, checked_at)
    return {"ok": True, "jobId": args.job_id, "remoteMaterialId": row["remoteMaterialId"],
            "reviewStatus": row["reviewStatus"], "materialEvaluation": row["materialEvaluation"],
            "checkedAt": checked_at}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="只读刷新千川视频库单条素材判定")
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--job-id", type=int, required=True)
    parser.add_argument("--profile-path", type=Path, required=True)
    parser.add_argument("--cdp-port", type=int, required=True)
    parser.add_argument("--advertiser-id", required=True)
    parser.add_argument("--douyin-account-id", type=int, default=0)
    args = parser.parse_args(argv)
    if not args.db.is_file() or args.job_id < 1 or not REMOTE_ID.fullmatch(args.advertiser_id):
        return emit({"ok": False, "error": "数据库、任务 ID 或千川广告主 ID 无效"}, 1)
    try:
        with connect(args.db) as db:
            return emit(refresh_status(db, args))
    except (ValueError, UploadStopped) as error:
        return emit({"ok": False, "error": str(error)}, 1)
    except Exception as error:
        return emit({"ok": False, "error": f"读取千川素材判定失败：{error}"}, 1)


if __name__ == "__main__":
    raise SystemExit(main())
