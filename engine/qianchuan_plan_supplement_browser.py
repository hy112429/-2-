#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""向已有千川商品全域计划追加一条素材；保存结果必须回读原计划。"""
from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

from playwright.sync_api import sync_playwright

from db import connect, emit
from qianchuan_browser import UploadStopped, read_verified_port
from qianchuan_plan_browser import CREATE_PATH, check_account, exact, one, video_drawer


def job_data(db_path: Path, supplement_id: int, dry_run: bool) -> dict:
    with connect(db_path) as db:
        job = db.execute("""SELECT s.*,p.account_id,p.qianchuan_account_id,p.advertiser_id,
            p.product_id,p.plan_name,p.remote_plan_id,p.status plan_status,
            p.applied_material_ids_json,
            u.material_name FROM qianchuan_plan_supplements s
            JOIN qianchuan_plan_jobs p ON p.id=s.plan_job_id
            JOIN qianchuan_upload_jobs u ON u.remote_material_id=s.remote_material_id
                AND u.account_id=p.account_id AND u.qianchuan_account_id=p.qianchuan_account_id
            WHERE s.id=? AND u.status='succeeded' ORDER BY u.id DESC LIMIT 1""",
            (supplement_id,)).fetchone()
        if not job or job["plan_status"] != "published" or job["status"] not in (
                {"appending", "scheduled"} if dry_run else {"appending"}):
            raise UploadStopped("原计划或追加任务状态不允许执行", stage="input")
        return dict(job)


def edit_url(job: dict) -> str:
    return "https://qianchuan.jinritemai.com" + CREATE_PATH + "?" + urlencode({
        "aavid": job["advertiser_id"], "adId": job["remote_plan_id"], "type": "edit"})


def _open_verified_edit(context, job: dict):
    page = context.new_page()
    ids: list[str] = []

    def capture(response):
        if urlparse(response.url).path != "/ad/api/creation/v1/ad/adDetail":
            return
        try:
            details = response.json()["data"]["adDetailInfo"]["createMultiProductsCreative"]
            found = []
            for detail in details:
                videos = detail["createCreativeInfo"]["proceduralCreative"]["videoMaterial"]
                found.extend(str(video["legoMid"]) for video in videos)
            if found and all(re.fullmatch(r"\d{8,20}", value) for value in found):
                ids[:] = found
        except (KeyError, TypeError, ValueError):
            pass

    page.on("response", capture)
    page.goto(edit_url(job), wait_until="domcontentloaded", timeout=45_000)
    page.get_by_text(str(job["advertiser_id"]), exact=False).first.wait_for(state="visible", timeout=25_000)
    check_account(page, str(job["advertiser_id"]))
    parsed = urlparse(page.url)
    query = parse_qs(parsed.query)
    if (parsed.path != CREATE_PATH or query.get("adId") != [str(job["remote_plan_id"])]
            or query.get("type") != ["edit"]):
        raise UploadStopped("未进入指定原计划的编辑页", stage="plan")
    try:
        page.wait_for_function("id => document.body.innerText.includes(id)",
                               arg=str(job["product_id"]), timeout=20_000)
    except Exception as error:
        raise UploadStopped("原计划的商品 ID 不匹配或详情未加载", stage="plan") from error
    label = one(page.get_by_text("计划名称", exact=True), "计划名称标签")
    name = one(label.locator("xpath=ancestor::*[.//input][1]").locator("input"), "计划名称输入框")
    if name.input_value() != job["plan_name"]:
        raise UploadStopped("原计划名称不匹配", stage="plan")
    deadline = time.monotonic() + 12
    while not ids and time.monotonic() < deadline:
        page.wait_for_timeout(400)
    if not ids or len(ids) != len(set(ids)):
        raise UploadStopped("原计划未回读到唯一素材 ID 列表", stage="plan")
    body = page.locator("body").inner_text()
    count = re.search(r"视频\s*\(\s*(\d+)\s*\)", body)
    if not count or int(count.group(1)) != len(ids):
        raise UploadStopped("原计划素材 ID 与页面视频数量不一致", stage="plan")
    return page, ids


def open_verified_edit(context, job: dict):
    existing = set(context.pages)
    try:
        return _open_verified_edit(context, job)
    except Exception:
        # 读取远端计划失败时，也关闭本次新开的标签。
        for page in context.pages:
            if page not in existing:
                try:
                    page.close()
                except Exception:
                    pass
        raise


def append_material(db_path: Path, supplement_id: int, profile_path: Path, port: int,
                    diagnostics_dir: Path, douyin_account_id: int = 0,
                    dry_run: bool = False) -> dict:
    job = job_data(db_path, supplement_id, dry_run)
    if douyin_account_id and douyin_account_id != int(job["account_id"]):
        raise UploadStopped("浏览器资料与素材所属账号不一致", stage="profile")
    read_verified_port(profile_path, port, int(job["qianchuan_account_id"]), douyin_account_id)
    diagnostics_dir.mkdir(parents=True, exist_ok=True)
    screenshot = diagnostics_dir / f"supplement-{supplement_id}.png"
    submitted = False
    page = None
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}", timeout=60_000)
            if len(browser.contexts) != 1:
                raise UploadStopped("千川浏览器上下文不唯一", stage="connect")
            context = browser.contexts[0]
            try:
                page, before = open_verified_edit(context, job)
                applied = set(json.loads(job["applied_material_ids_json"] or "[]"))
                if not applied or not applied.issubset(set(before)):
                    raise UploadStopped("原计划素材与上次核查记录不一致，已停止自动追加", stage="plan")
                material_id = str(job["remote_material_id"])
                if material_id in before:
                    return {"ok": True, "status": "already_present", "submitAttempted": False,
                            "reason": "原计划编辑页已包含精确素材 ID，未重复保存"}
                if not video_drawer(page, "uploaded", str(job["material_name"]),
                                    material_id, len(before) + 1, allow_missing=True):
                    return {"ok": True, "status": "waiting", "submitAttempted": False,
                            "reason": "素材库已有该素材，但原计划编辑页尚不可选；24 小时后重查"}
                save = exact(page, "保存")
                if not save.is_enabled():
                    raise UploadStopped("千川原计划保存按钮不可用", stage="save")
                if dry_run:
                    return {"ok": True, "status": "ready_to_save", "submitAttempted": False,
                            "beforeCount": len(before), "afterCount": len(before) + 1}
                submitted = True
                save.click(timeout=8_000)
                page.wait_for_timeout(8_000)
                page.screenshot(path=str(screenshot), full_page=True)
                verify_page, after = open_verified_edit(context, job)
                verify_page.close()
                if (material_id in after and len(after) == len(before) + 1
                        and set(before).issubset(set(after))):
                    return {"ok": True, "status": "applied", "submitAttempted": True,
                            "screenshotPath": str(screenshot),
                            "reason": "原计划编辑页回读到新增素材 ID，未新建投放计划"}
                return {"ok": True, "status": "needs_review", "submitAttempted": True,
                        "screenshotPath": str(screenshot),
                        "reason": "已点击保存，但原计划尚未回读到新增素材 ID；禁止自动重复保存"}
            finally:
                if page is not None:
                    page.close()
    except Exception as error:
        if page is not None:
            try:
                page.screenshot(path=str(screenshot), full_page=True, timeout=5_000)
            except Exception:
                pass
        return {"ok": True, "status": "needs_review" if submitted else "blocked",
                "submitAttempted": submitted,
                "screenshotPath": str(screenshot) if screenshot.exists() else "",
                "reason": str(error)[:500]}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--supplement-id", type=int, required=True)
    parser.add_argument("--profile-path", type=Path, required=True)
    parser.add_argument("--cdp-port", type=int, required=True)
    parser.add_argument("--diagnostics-dir", type=Path, required=True)
    parser.add_argument("--douyin-account-id", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        return emit(append_material(args.db, args.supplement_id, args.profile_path,
                                    args.cdp_port, args.diagnostics_dir,
                                    args.douyin_account_id, args.dry_run))
    except Exception as error:
        return emit({"ok": False, "error": str(error)[:500]}, 1)


if __name__ == "__main__":
    raise SystemExit(main())
