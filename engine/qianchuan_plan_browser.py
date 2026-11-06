#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""千川商品全域计划的专属浏览器执行器；任何不唯一或未证实结果都停止。"""

from __future__ import annotations

import argparse
import json
import re
import time
import uuid
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from db import emit
from qianchuan_browser import read_verified_port


HOST = "qianchuan.jinritemai.com"
PLAN_ID = re.compile(r"^[A-Za-z0-9_-]{6,64}$")
CAPTCHA = re.compile(r"滑动验证|安全验证|人机验证|验证码错误|请完成验证")
ERROR = re.compile(r"创建失败|发布失败|无权限|余额不足|素材不可用|商品不可投放")


class Stopped(RuntimeError):
    def __init__(self, message: str, stage: str):
        super().__init__(message)
        self.stage = stage


def _unique(locator: Any, label: str) -> Any:
    visible = [locator.nth(i) for i in range(locator.count()) if locator.nth(i).is_visible()]
    if len(visible) != 1:
        raise Stopped(f"{label}匹配 {len(visible)} 个，无法安全操作", "locate")
    return visible[0]


def _body(page: Any) -> str:
    return page.locator("body").inner_text(timeout=5000)[:30000]


def _account_gate(page: Any, advertiser_id: str, stage: str) -> None:
    parsed = urlparse(page.url)
    if parsed.scheme != "https" or parsed.hostname != HOST:
        raise Stopped("页面已离开千川工作台或跳到登录页", stage)
    page_account = parse_qs(parsed.query).get("aavid", [""])[0]
    if page_account != advertiser_id:
        raise Stopped("地址栏广告主 ID 与任务绑定不一致", stage)
    body = _body(page)
    if not re.search(r"ID\s*[：:]\s*" + re.escape(advertiser_id) + r"\b", body[:1500]):
        raise Stopped("页面显示的广告主 ID 与任务绑定不一致", stage)
    if CAPTCHA.search(body):
        raise Stopped("千川要求人工完成安全验证", stage)
    if ERROR.search(body):
        raise Stopped("千川页面出现权限、余额或素材错误", stage)


def _choose_product(page: Any, product_id: str) -> None:
    _unique(page.get_by_role("button", name="添加商品", exact=True), "添加商品按钮").click()
    search = _unique(page.locator('input[placeholder="请输入商品名称/ID搜索"]'), "商品搜索框")
    search.fill(product_id)
    search.press("Enter")
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        if page.get_by_text(product_id, exact=True).count():
            break
        time.sleep(0.5)
    target = _unique(page.get_by_text(product_id, exact=True), "目标商品 ID")
    holder = target
    checkbox = None
    for _ in range(8):
        holder = holder.locator("xpath=..")
        if len(holder.inner_text()) > 1800:
            break
        found = holder.locator('input[type="checkbox"]')
        if found.count() == 1:
            checkbox = found
            break
    if checkbox is None:
        raise Stopped("搜索结果未唯一关联目标商品勾选框", "product")
    checkbox.check(force=True)
    confirm = _unique(page.get_by_role("button", name="确定", exact=True), "商品确认按钮")
    if not confirm.is_enabled():
        raise Stopped("商品确认按钮不可用", "product")
    confirm.click()
    if product_id not in _body(page) or not re.search(r"已添加\s*[：:]\s*1\s*/\s*30", _body(page)):
        raise Stopped("商品确认后未回读到唯一目标商品", "product")


def _checked_radio(page: Any, text: str, label: str) -> None:
    radio = _unique(page.locator(".ovui-radio-item").filter(has_text=text), label)
    if "ovui-radio-item--checked" not in (radio.get_attribute("class") or ""):
        radio.click()
    if "ovui-radio-item--checked" not in (radio.get_attribute("class") or ""):
        raise Stopped(f"{label}未选中", "settings")


def _switch_near(page: Any, text: str, desired: bool) -> None:
    target = _unique(page.get_by_text(text, exact=True), text)
    ancestor = target
    switch = None
    for _ in range(8):
        ancestor = ancestor.locator("xpath=..")
        found = ancestor.locator(".ovui-switch")
        if found.count() == 1:
            switch = found
            break
        if found.count() > 1:
            break
    if switch is None:
        raise Stopped(f"{text}开关无法唯一定位", "settings")
    active = "ovui-switch--checked" in (switch.get_attribute("class") or "")
    if active != desired:
        switch.click()
    active = "ovui-switch--checked" in (switch.get_attribute("class") or "")
    if active != desired:
        raise Stopped(f"{text}开关状态回读不一致", "settings")


def _set_basics(page: Any, args: argparse.Namespace) -> None:
    _checked_radio(page, "放量投放", "放量投放")
    _checked_radio(page, "从今天起长期投放", "长期投放")
    budget = _unique(page.locator('input[placeholder="请输入"]'), "投放日预算")
    if not budget.is_editable():
        raise Stopped("投放日预算尚不可编辑，商品可能未成功选中", "budget")
    yuan = f"{args.budget_fen / 100:.2f}".rstrip("0").rstrip(".")
    budget.fill(yuan)
    budget.press("Tab")
    if budget.input_value().replace(",", "") != yuan:
        raise Stopped("投放日预算回读与账户配置不一致", "budget")
    _switch_near(page, "智能优惠券", bool(args.smart_coupon))
    _switch_near(page, "AIGC 动态创意", bool(args.aigc_enabled))
    name = _unique(page.locator('input[placeholder*="请输入计划名称"]'), "计划名称")
    name.fill(args.plan_name)
    if name.input_value() != args.plan_name:
        raise Stopped("计划名称回读不一致", "settings")


def _select_video_card(page: Any, marker: str) -> None:
    if not marker:
        raise Stopped("缺少可唯一标识视频的标题或素材 ID", "material")
    text = _unique(page.get_by_text(marker, exact=False), "目标视频")
    holder = text
    for _ in range(8):
        holder = holder.locator("xpath=..")
        checkbox = holder.locator('input[type="checkbox"]')
        if checkbox.count() == 1 and len(holder.inner_text()) < 1200:
            checkbox.check(force=True)
            return
    raise Stopped("目标视频未唯一关联勾选框", "material")


def _add_material(page: Any, args: argparse.Namespace) -> None:
    card = _unique(page.locator(".oc-switch-card-item").filter(has_text="自选投放素材"), "自选投放素材")
    checkbox = _unique(card.locator('input[type="checkbox"]'), "自选投放素材勾选框")
    checkbox.check(force=True)
    add = _unique(page.get_by_role("button", name="添加视频", exact=True), "添加视频按钮")
    if not add.is_enabled():
        raise Stopped("添加视频按钮不可用，需核对所选商品的素材设置", "material")
    add.click()
    time.sleep(0.8)
    tab = _unique(page.get_by_text("我的视频", exact=True), "我的视频标签")
    tab.click()
    if args.material_source in ("homepage", "both"):
        if page.get_by_text("抖音号主页素材", exact=True).count() == 0:
            raise Stopped("我的视频中未确认抖音号主页素材来源", "material")
        _select_video_card(page, args.published_title)
    if args.material_source in ("uploaded", "both"):
        # 仅选 Step17 已确认的远端素材；不在计划页重复上传 MP4。
        if not args.remote_material_id:
            raise Stopped("没有 Step17 已确认的远端 MP4 素材 ID", "material")
        marker = Path(args.remote_material_name).stem if args.remote_material_name else args.remote_material_id
        _select_video_card(page, marker)
    confirm = _unique(page.get_by_role("button", name="确定", exact=True), "素材确认按钮")
    if not confirm.is_enabled():
        raise Stopped("素材确认按钮不可用", "material")
    confirm.click()
    body = _body(page)
    count = re.search(r"视频\s*\((\d+)\)", body)
    required = 2 if args.material_source == "both" else 1
    if count is None or int(count.group(1)) < required:
        raise Stopped("素材确认后没有视频数量回读", "material")


def _plan_id(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("plan_id", "planId", "project_id", "projectId"):
        value = str(payload.get(key, ""))
        if PLAN_ID.fullmatch(value):
            return value
    for key in ("data", "result"):
        value = _plan_id(payload.get(key))
        if value:
            return value
    return ""


def _publish(page: Any, args: argparse.Namespace, mark_attempted: Callable[[], None]) -> str:
    _account_gate(page, args.advertiser_id, "before_submit")
    body = _body(page)
    if args.product_id not in body or args.plan_name not in body:
        raise Stopped("发布前商品或计划名称回读不一致", "before_submit")
    if not re.search(r"视频\s*\([1-9]", body):
        raise Stopped("发布前未看到已选视频", "before_submit")
    budget = _unique(page.locator('input[placeholder="请输入"]'), "发布前日预算")
    actual_fen = int(round(float(budget.input_value().replace(",", "")) * 100))
    if actual_fen != args.budget_fen:
        raise Stopped("发布前预算与账户配置不一致", "before_submit")
    button = _unique(page.get_by_role("button", name="发布计划", exact=True), "发布计划按钮")
    if not button.is_enabled():
        raise Stopped("发布计划按钮不可用", "before_submit")
    responses: list[str] = []

    def collect(response: Any) -> None:
        parsed = urlparse(response.url)
        if parsed.hostname != HOST or not re.search(r"create|publish|submit", parsed.path, re.I):
            return
        try:
            payload = response.json()
            candidate = _plan_id(payload)
            if candidate and isinstance(payload, dict) and payload.get("code", 0) in (0, "0", None):
                responses.append(candidate)
        except Exception:
            pass

    page.on("response", collect)
    mark_attempted()
    button.click()
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        if len(set(responses)) == 1:
            return responses[0]
        if len(set(responses)) > 1:
            raise Stopped("发布响应出现多个不同计划 ID", "verification")
        if CAPTCHA.search(_body(page)) or ERROR.search(_body(page)):
            raise Stopped("发布后页面要求人工验证或显示失败", "verification")
        time.sleep(1)
    raise Stopped("发布后未回读到唯一远端计划 ID", "verification")


def _diagnostic(args: argparse.Namespace, page: Any | None, stage: str, reason: str,
                submit_attempted: bool) -> str:
    args.diagnostics_dir.mkdir(parents=True, exist_ok=True)
    stem = f"plan-{args.qianchuan_account_id}-{uuid.uuid4().hex[:12]}"
    screenshot_path = ""
    if page is not None:
        try:
            screenshot = args.diagnostics_dir / f"{stem}.png"
            page.screenshot(path=str(screenshot), full_page=False, timeout=5000)
            screenshot_path = str(screenshot)
        except Exception:
            pass
    info = args.diagnostics_dir / f"{stem}.json"
    info.write_text(json.dumps({"stage": stage, "reason": reason,
                                "submitAttempted": submit_attempted,
                                "advertiserId": args.advertiser_id,
                                "productId": args.product_id,
                                "screenshotPath": screenshot_path},
                               ensure_ascii=False, indent=2), encoding="utf-8")
    return str(info)


def execute(args: argparse.Namespace) -> dict[str, Any]:
    if args.budget_fen < 1 or not re.fullmatch(r"\d{1,20}", args.product_id) or not re.fullmatch(r"\d{1,20}", args.advertiser_id):
        return {"ok": True, "status": "failed", "reason": "计划输入参数无效", "submitAttempted": False}
    browser = None
    page = None
    submitted = False
    stage = "connect"

    def mark_attempted() -> None:
        nonlocal submitted
        submitted = True

    try:
        from playwright.sync_api import sync_playwright

        read_verified_port(args.profile_path, args.cdp_port, args.qianchuan_account_id)
        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{args.cdp_port}")
            if len(browser.contexts) != 1:
                raise Stopped("千川浏览器上下文数量异常", "connect")
            page = browser.contexts[0].new_page()
            try:
                page.goto(f"https://{HOST}/uni-creation/product?aavid={args.advertiser_id}&enter_from_inside=promotion_uni_header_create",
                          wait_until="domcontentloaded", timeout=30000)
                _account_gate(page, args.advertiser_id, "account")
                stage = "product"
                _choose_product(page, args.product_id)
                stage = "settings"
                _set_basics(page, args)
                stage = "material"
                _add_material(page, args)
                stage = "before_submit"
                _account_gate(page, args.advertiser_id, stage)
                remote_id = _publish(page, args, mark_attempted)
                return {"ok": True, "status": "succeeded", "remotePlanId": remote_id,
                        "reason": "已回读唯一千川计划 ID", "submitAttempted": True}
            except Exception as error:
                if isinstance(error, Stopped):
                    stage = error.stage
                diagnostic = _diagnostic(args, page, stage, str(error), submitted)
                return {"ok": True, "status": "needs_review" if submitted else "failed", "reason": str(error),
                        "submitAttempted": submitted, "diagnosticPath": diagnostic}
            finally:
                # 本次只用一个临时标签；执行结束关闭，不留下重复千川页面。
                if page is not None:
                    page.close()
                browser.close()
    except Exception as error:
        if isinstance(error, Stopped):
            stage = error.stage
        reason = str(error)
        diagnostic = _diagnostic(args, page if page and not page.is_closed() else None, stage, reason, submitted)
        return {"ok": True, "status": "needs_review" if submitted else "failed", "reason": reason,
                "submitAttempted": submitted, "diagnosticPath": diagnostic}


def main() -> int:
    parser = argparse.ArgumentParser(description="千川商品全域计划浏览器执行器")
    sub = parser.add_subparsers(dest="command", required=True)
    command = sub.add_parser("publish")
    command.add_argument("--cdp-port", type=int, required=True)
    command.add_argument("--profile-path", type=Path, required=True)
    command.add_argument("--qianchuan-account-id", type=int, required=True)
    command.add_argument("--advertiser-id", required=True)
    command.add_argument("--product-id", required=True)
    command.add_argument("--plan-name", required=True)
    command.add_argument("--budget-fen", type=int, required=True)
    command.add_argument("--material-source", choices=("homepage", "uploaded", "both"), required=True)
    command.add_argument("--remote-material-id", default="")
    command.add_argument("--remote-material-name", default="")
    command.add_argument("--published-title", default="")
    command.add_argument("--video-path", default="")
    command.add_argument("--smart-coupon", type=int, choices=(0, 1), required=True)
    command.add_argument("--aigc-enabled", type=int, choices=(0, 1), required=True)
    command.add_argument("--diagnostics-dir", type=Path, required=True)
    args = parser.parse_args()
    return emit(execute(args))


if __name__ == "__main__":
    raise SystemExit(main())
