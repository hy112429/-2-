# -*- coding: utf-8 -*-
"""在账号专属千川 Chrome 中创建商品全域投放计划。页面不确定时停止。"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlencode, urlparse, parse_qs

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

from db import connect
from qianchuan_browser import read_verified_port, UploadStopped

CREATE_PATH = "/uni-creation/product"


def plan_id_in_list_text(body: str, plan_name: str, budget: int,
                         delivery_mode: str = "控成本投放") -> str:
    """只接受列表里唯一的同名计划，并核对投放方式和日预算。"""
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    matches = [index for index, line in enumerate(lines) if line == plan_name]
    if len(matches) != 1:
        return ""
    index = matches[0]
    segment = lines[index:index + 18]
    if len(segment) < 12:
        return ""
    found = re.fullmatch(r"ID[：:]\s*(\d{8,20})", segment[1])
    if not found or delivery_mode not in segment:
        return ""
    if not any(re.fullmatch(rf"{budget}\s*元", line) for line in segment):
        return ""
    return found.group(1)


def verify_created_plan(context, job: dict, budget: int) -> str:
    """创建回执不带 ID 时，只读回查千川计划列表，不重新提交。"""
    list_page = next((item for item in reversed(context.pages)
                      if not item.is_closed() and urlparse(item.url).path == "/uni-prom"), None)
    created_page = list_page is None
    if list_page is None:
        list_page = context.new_page()
    try:
        advertiser = str(job["advertiser_id"])
        list_page.goto("https://qianchuan.jinritemai.com/uni-prom?" +
                       urlencode({"aavid": advertiser}), wait_until="domcontentloaded", timeout=30_000)
        list_page.get_by_text(str(job["plan_name"]), exact=True).first.wait_for(
            state="visible", timeout=20_000)
        check_account(list_page, advertiser)
        if urlparse(list_page.url).path != "/uni-prom":
            return ""
        return plan_id_in_list_text(list_page.locator("body").inner_text(),
                                    str(job["plan_name"]), budget)
    finally:
        if created_page:
            list_page.close()


def one(locator, label: str):
    visible = [locator.nth(i) for i in range(locator.count()) if locator.nth(i).is_visible()]
    if len(visible) != 1:
        raise UploadStopped(f"{label}匹配到 {len(visible)} 个可见控件", stage="locate")
    return visible[0]


def exact(page, label: str):
    controls = page.get_by_role("button", name=label, exact=True)
    try:
        controls.first.wait_for(state="visible", timeout=5_000)
        return one(controls, label)
    except PlaywrightTimeoutError:
        content = page.get_by_text(label, exact=True)
        content.first.wait_for(state="visible", timeout=8_000)
        return one(content, label)


def form_job(db_path: Path, job_id: int):
    with connect(db_path) as db:
        job = db.execute("SELECT * FROM qianchuan_plan_jobs WHERE id=?", (job_id,)).fetchone()
        if not job or job["status"] != "submitting":
            raise UploadStopped("本机计划任务未取得执行锁", stage="input")
        settings = db.execute("SELECT * FROM qianchuan_plan_settings WHERE qianchuan_account_id=?",
                              (job["qianchuan_account_id"],)).fetchone()
        if (not settings or int(job["daily_budget_yuan"] or 0) < 50 or
                (not settings["enabled"] and str(job["creation_source"] or "auto") != "manual")):
            raise UploadStopped("千川账户未启用自动计划，或未配置至少 50 元日预算", stage="input")
        rows = db.execute("""SELECT DISTINCT remote_material_id,material_name
            FROM qianchuan_upload_jobs WHERE account_id=? AND qianchuan_account_id=?
            AND product_id=? AND status='succeeded' AND remote_material_id<>''""",
            (job["account_id"], job["qianchuan_account_id"], job["product_id"])).fetchall()
        requested = set(json.loads(job["material_ids_json"]))
        materials = [(str(row["remote_material_id"]), str(row["material_name"]))
                     for row in rows if str(row["remote_material_id"]) in requested]
        settings = dict(settings)
        # 预算在原子 claim 时写入任务快照，表单与结果回查都使用同一数值。
        settings["daily_budget_yuan"] = int(job["daily_budget_yuan"])
        return dict(job), settings, materials


def check_account(page, advertiser_id: str):
    parsed = urlparse(page.url)
    if parsed.hostname != "qianchuan.jinritemai.com":
        raise UploadStopped("当前浏览器不在千川工作台", stage="account", manual=True)
    declared = parse_qs(parsed.query).get("aavid", [])
    if declared and declared != [advertiser_id]:
        raise UploadStopped("当前页面广告主与计划任务不一致", stage="account", manual=True)
    body = page.locator("body").inner_text(timeout=10_000)
    if advertiser_id not in body:
        raise UploadStopped("页面没有显示目标千川广告主 ID；仅修改网址不能证明已切换账号", stage="account", manual=True)
    if re.search("请登录|立即登录|人机验证|滑动验证|安全验证|无访问权限", body):
        raise UploadStopped("千川页面需要登录、验证或账户权限", stage="account", manual=True)


def check_global_delivery(page):
    """确认当前创建页属于视频中展示的“商品全域投放”入口。"""
    try:
        page.get_by_text(re.compile(r"商品全域投放|全域投放")).first.wait_for(
            state="visible", timeout=10_000)
    except PlaywrightTimeoutError as error:
        raise UploadStopped("当前页面不是商品全域投放创建页", stage="navigate") from error
    body = page.locator("body").inner_text(timeout=10_000)
    if not re.search(r"商品全域投放|全域投放", body):
        raise UploadStopped("当前页面不是商品全域投放创建页", stage="navigate")


def select_product(page, product_id: str):
    exact(page, "添加商品").click(timeout=8_000)
    search = one(page.locator('input[placeholder*="商品名称/ID"]'), "商品搜索框")
    search.fill(product_id)
    search.press("Enter")
    marker = page.get_by_text(re.compile(rf"ID[:：]\s*{re.escape(product_id)}(?!\d)"))
    marker.first.wait_for(state="visible", timeout=15_000)
    item = one(marker, "商品 ID")
    checkbox = nearest_checkbox(item, "商品选择框")
    select_checkbox(checkbox, "商品选择框")
    if not page.get_by_text(re.compile(r"已选\s*1/30")).count():
        raise UploadStopped("平台未确认已选择一个商品", stage="product")
    exact(page, "确定").click(timeout=8_000)
    if not page.get_by_text(product_id).count():
        raise UploadStopped("商品选择后未回显目标 ID", stage="product")


def fill_budget(page, budget: int):
    label = one(page.get_by_text("投放日预算", exact=True), "日预算标签")
    section = label.locator("xpath=ancestor::*[.//input][1]")
    field = one(section.locator("input"), "日预算输入框")
    field.fill(str(budget))
    field.press("Tab")
    if field.input_value() != str(budget):
        raise UploadStopped("平台日预算回显与账户配置不一致", stage="budget")


def fill_roi_target(page, roi_target: float):
    label = one(page.get_by_text("净成交ROI目标", exact=True), "净成交 ROI 目标标签")
    section = label.locator("xpath=ancestor::*[.//input][1]")
    field = one(section.locator("input"), "净成交 ROI 目标输入框")
    expected = f"{roi_target:.2f}".rstrip("0").rstrip(".")
    field.fill(expected)
    field.press("Tab")
    try:
        matched = abs(float(field.input_value()) - roi_target) < 0.005
    except (TypeError, ValueError):
        matched = False
    if not matched:
        raise UploadStopped("平台净成交 ROI 目标回显与本机计算值不一致", stage="roi")


def select_radio(page, label: str):
    option = one(page.locator(".ovui-radio-item").filter(has_text=label), label)
    if "ovui-radio-item--checked" not in (option.get_attribute("class") or "").split():
        option.click(timeout=8_000)
    if "ovui-radio-item--checked" not in (option.get_attribute("class") or "").split():
        raise UploadStopped(label + "没有被平台选中", stage="settings")


def set_switch(page, label: str, desired: bool):
    heading = one(page.get_by_text(label, exact=True), label)
    area = heading.locator("xpath=ancestor::*[.//*[contains(@class,'ovui-switch')]][1]")
    switch = one(area.locator(".ovui-switch"), label + "开关")
    current = "ovui-switch--checked" in (switch.get_attribute("class") or "").split()
    if current != desired:
        if "ovui-switch--disabled" in (switch.get_attribute("class") or "").split():
            raise UploadStopped(label + "在千川页面不可修改", stage="settings")
        switch.click()
    if ("ovui-switch--checked" in (switch.get_attribute("class") or "").split()) != desired:
        raise UploadStopped(label + "状态无法确认", stage="settings")


def nearest_checkbox(item, label: str):
    for depth in range(1, 7):
        row = item.locator(f"xpath=ancestor::*[{depth}]")
        choices = row.locator('input[type="checkbox"]')
        if choices.count() == 1:
            return one(choices, label)
    raise UploadStopped(f"{label}无法唯一定位", stage="locate")


def select_checkbox(checkbox, label: str):
    if checkbox.is_checked():
        return
    visual = checkbox.locator("xpath=ancestor::label[1]")
    if visual.count() == 1:
        visual.click(timeout=8_000)
    else:
        checkbox.check(timeout=8_000)
    if not checkbox.is_checked():
        raise UploadStopped(label + "选中状态未回显", stage="locate")


def select_material_product(page, product_id: str):
    picker = one(page.locator(".ovui-select").filter(has_text="选择商品："), "素材所属商品选择框")
    picker.click(timeout=8_000)
    one(page.get_by_text("ID:" + product_id, exact=True), "素材所属商品 ID").click(timeout=8_000)
    if page.get_by_text("请先选择要添加创意的商品", exact=True).count():
        raise UploadStopped("素材所属商品尚未选中", stage="video")


def video_drawer(page, source: str, search_term: str, remote_id: str,
                 required_count: int, allow_missing: bool = False) -> bool:
    exact(page, "添加视频").click(timeout=8_000)
    tab = page.get_by_text("我的视频", exact=True)
    tab.first.wait_for(state="visible", timeout=10_000)
    one(tab, "我的视频页签")
    page.get_by_text("来源", exact=False).first.wait_for(state="visible", timeout=10_000)
    one(page.locator(".ovui-select").filter(has_text="来源："), "素材来源筛选").click(timeout=8_000)
    category = "本地上传" if source == "uploaded" else "抖音号主页素材"
    choice = page.get_by_text(category, exact=True)
    choice.first.wait_for(state="visible", timeout=8_000)
    one(choice, "素材来源").click(timeout=8_000)
    term = search_term.strip()
    if not term:
        raise UploadStopped("视频缺少可核对的名称", stage="video")
    if source == "uploaded":
        page.locator('input[placeholder*="视频名称/ID搜索"]').first.wait_for(state="visible", timeout=10_000)
        search = one(page.locator('input[placeholder*="视频名称/ID搜索"]'), "已上传素材搜索框")
        search.fill(remote_id)
        search.press("Enter")
        candidates = page.get_by_text(term, exact=True)
    else:
        # 主页视频搜索框仅接受分享链接；本地只有作品标题，必须在可见列表中唯一匹配。
        candidates = page.get_by_text(term[:16], exact=False)
    try:
        candidates.first.wait_for(state="visible", timeout=6_000)
    except PlaywrightTimeoutError as error:
        if source == "uploaded":
            # 平台搜索有时按文件名建索引，有时按素材 ID 建索引；两者都要核对。
            search.fill(term)
            search.press("Enter")
            try:
                candidates.first.wait_for(state="visible", timeout=6_000)
            except PlaywrightTimeoutError:
                if allow_missing:
                    buttons = page.get_by_role("button", name="取消", exact=True)
                    visible = [buttons.nth(i) for i in range(buttons.count()) if buttons.nth(i).is_visible()]
                    if not visible:
                        raise UploadStopped("素材选择弹窗无法安全关闭", stage="video")
                    cancel = visible[-1]
                    cancel.click(timeout=8_000)
                    search.wait_for(state="hidden", timeout=8_000)
                    return False
                raise UploadStopped("千川计划页未找到匹配的已上传视频，请核对平台选材资格", stage="video") from error
        body = page.locator("body").inner_text(timeout=5_000)
        if source != "uploaded" and ("未获得视频授权" in body or "暂无可选视频" in body):
            raise UploadStopped("千川计划页无可选素材：请核查视频库授权、素材投放资格和所属广告主", stage="video", manual=True) from error
        if source != "uploaded":
            raise UploadStopped("千川计划页未找到匹配的视频，请核对作品标题", stage="video") from error
    item = one(candidates, "视频名称")
    card = one(item.locator("xpath=ancestor::*[contains(@class,'oc-create-material-card-wrapper')][1]"), "视频卡片")
    if source == "uploaded" and card.get_attribute("title") != term:
        raise UploadStopped("平台素材名称与本机文件名不一致", stage="video")
    card.click(timeout=8_000)
    # 每次重新打开选择框只勾选当前视频；分母会随计划中已添加的视频减少。
    page.get_by_text(re.compile(r"已选择\s*1/\d+")).first.wait_for(state="visible", timeout=5_000)
    confirm = exact(page, "确定")
    if not confirm.is_enabled():
        raise UploadStopped("平台未接受视频卡片选择", stage="video")
    confirm.click(timeout=8_000)
    # 发布前还会核对页面选择数量；本次必须严格增加，避免重复点击同一视频。
    count_text = page.get_by_text(re.compile(r"视频\s*\(\s*\d+\s*\)"))
    numbers = [int(match.group(1)) for match in
               (re.search(r"视频\s*\(\s*(\d+)\s*\)", count_text.nth(i).inner_text())
                for i in range(count_text.count())) if match]
    if required_count not in numbers:
        raise UploadStopped(f"平台视频数量未达到 {required_count} 条", stage="video")
    return True


def video_drawer_batch(page, materials: list[tuple[str, str]],
                       selected_before: int, allow_missing: bool = False) -> tuple[list[str], list[str]]:
    """在同一个选材窗口内选择多条已上传素材。

    千川计划页的“我的视频”窗口支持多选。逐条打开窗口会重复等待列表、
    搜索框和确认按钮，上传后素材较多时耗时明显；这里保持每条素材的
    ID、文件名和卡片标题核对，只把窗口打开/确认各做一次。
    """
    if not materials:
        return [], []
    exact(page, "添加视频").click(timeout=8_000)
    tab = page.get_by_text("我的视频", exact=True)
    tab.first.wait_for(state="visible", timeout=10_000)
    one(tab, "我的视频页签")
    page.get_by_text("来源", exact=False).first.wait_for(state="visible", timeout=10_000)
    one(page.locator(".ovui-select").filter(has_text="来源："), "素材来源筛选").click(timeout=8_000)
    choice = page.get_by_text("本地上传", exact=True)
    choice.first.wait_for(state="visible", timeout=8_000)
    one(choice, "素材来源").click(timeout=8_000)

    search = page.locator('input[placeholder*="视频名称/ID搜索"]').first
    search.wait_for(state="visible", timeout=10_000)
    selected: list[str] = []
    skipped: list[str] = []
    for remote_id, material_name in materials:
        term = material_name.strip()
        if not term:
            raise UploadStopped("视频缺少可核对的名称", stage="video")
        search.fill(remote_id)
        search.press("Enter")
        candidates = page.get_by_text(term, exact=True)
        try:
            candidates.first.wait_for(state="visible", timeout=6_000)
        except PlaywrightTimeoutError as error:
            # 平台有时按文件名索引，有时按素材 ID 索引；同一窗口内切换一次查询。
            search.fill(term)
            search.press("Enter")
            try:
                candidates.first.wait_for(state="visible", timeout=6_000)
            except PlaywrightTimeoutError:
                if allow_missing:
                    skipped.append(remote_id)
                    continue
                raise UploadStopped("千川计划页未找到匹配的已上传视频，请核对平台选材资格",
                                     stage="video") from error
        item = one(candidates, "视频名称")
        card = one(item.locator("xpath=ancestor::*[contains(@class,'oc-create-material-card-wrapper')][1]"),
                   "视频卡片")
        if card.get_attribute("title") != term:
            raise UploadStopped("平台素材名称与本机文件名不一致", stage="video")
        card.click(timeout=8_000)
        expected = selected_before + len(selected) + 1
        page.get_by_text(re.compile(rf"已选择\s*{expected}/\d+")).first.wait_for(
            state="visible", timeout=5_000)
        selected.append(remote_id)

    if not selected:
        buttons = page.get_by_role("button", name="取消", exact=True)
        visible = [buttons.nth(i) for i in range(buttons.count()) if buttons.nth(i).is_visible()]
        if visible:
            visible[-1].click(timeout=8_000)
        return selected, skipped
    confirm = exact(page, "确定")
    if not confirm.is_enabled():
        raise UploadStopped("平台未接受批量视频选择", stage="video")
    confirm.click(timeout=8_000)
    final_count = selected_before + len(selected)
    count_text = page.get_by_text(re.compile(r"视频\s*\(\s*\d+\s*\)"))
    numbers = [int(match.group(1)) for match in
               (re.search(r"视频\s*\(\s*(\d+)\s*\)", count_text.nth(i).inner_text())
                for i in range(count_text.count())) if match]
    if final_count not in numbers:
        raise UploadStopped(f"平台视频数量未达到 {final_count} 条", stage="video")
    return selected, skipped


def add_title(page, title: str):
    value = title.strip()[:55]
    if not value:
        raise UploadStopped("缺少可用于投放的标题", stage="title")
    exact(page, "添加标题").click(timeout=8_000)
    field = one(page.locator('input[placeholder*="请输入标题或使用系统推荐标题"]'), "投放标题输入框")
    field.fill(value)
    field.press("Tab")
    if field.input_value() != value:
        raise UploadStopped("投放标题回显不一致", stage="title")


def create_plan(db_path: Path, job_id: int, profile_path: Path, port: int,
                diagnostics_dir: Path, douyin_account_id: int = 0,
                dry_run: bool = False) -> dict:
    job, settings, materials = form_job(db_path, job_id)
    advertiser = str(job["advertiser_id"])
    if douyin_account_id and douyin_account_id != int(job["account_id"]):
        raise UploadStopped("抖音浏览器与计划任务账号不一致", stage="profile")
    read_verified_port(profile_path, port, int(job["qianchuan_account_id"]), douyin_account_id)
    diagnostics_dir.mkdir(parents=True, exist_ok=True)
    screenshot = diagnostics_dir / f"plan-{job_id}.png"
    stage, submitted, page, created_page = "connect", False, None, False
    responses: list[str] = []
    submission_errors: list[str] = []
    api_events: list[dict[str, str | int]] = []
    playwright = None
    try:
        playwright = sync_playwright().start()
        browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        if len(browser.contexts) != 1:
            raise UploadStopped("千川浏览器上下文不唯一", stage=stage)
        # 在已连接的账号浏览器中使用一个临时标签，执行完立即关闭；不覆盖用户正在看的页面。
        existing_page = next((item for item in reversed(browser.contexts[0].pages)
                              if not item.is_closed() and urlparse(item.url).path == CREATE_PATH), None)
        created_page = existing_page is None
        page = existing_page or browser.contexts[0].new_page()
        stage = "navigate"
        page.goto("https://qianchuan.jinritemai.com" + CREATE_PATH + "?" +
                  urlencode({"aavid": advertiser}), wait_until="domcontentloaded", timeout=30_000)
        page.get_by_text(advertiser, exact=False).first.wait_for(state="visible", timeout=15_000)
        check_account(page, advertiser)
        check_global_delivery(page)
        if urlparse(page.url).path != CREATE_PATH:
            raise UploadStopped("没有进入商品全域投放创建页", stage=stage)
        exact(page, "添加商品").wait_for(state="visible", timeout=10_000)
        page.get_by_text("投放日预算", exact=True).first.wait_for(state="visible", timeout=10_000)
        stage = "product"
        select_product(page, str(job["product_id"]))
        stage = "budget"
        select_radio(page, "控成本投放")
        fill_budget(page, int(settings["daily_budget_yuan"]))
        stage = "roi"
        fill_roi_target(page, float(job["roi_target"]))
        if settings["long_running"]:
            select_radio(page, "从今天起长期投放")
        stage = "settings"
        set_switch(page, "智能优惠券", bool(settings["smart_coupon"]))
        set_switch(page, "AIGC 动态创意", bool(settings["aigc_enabled"]))
        stage = "video"
        exact(page, "自选投放素材").click(timeout=8_000)
        select_material_product(page, str(job["product_id"]))
        source = str(settings["video_source"])
        selected_count = 0
        uploaded_count = 0
        selected_material_ids: list[str] = []
        skipped_material_ids: list[str] = []
        if source in ("my_video", "both"):
            try:
                video_drawer(page, "my_video", str(job["published_video_title"]), "", selected_count + 1)
                selected_count += 1
            except Exception:
                if source == "my_video":
                    raise
                # “两种来源”允许主页作品尚未进入千川列表；此时仍使用已上传素材。
                cancel = page.get_by_role("button", name="取消", exact=True)
                if cancel.count() and cancel.last.is_visible():
                    cancel.last.click(timeout=8_000)
        if source in ("uploaded", "both"):
            if not materials:
                raise UploadStopped("可用千川上传视频为空", stage=stage)
            # 千川投放选材可能尚未收录全部上传成功的素材；在一个多选窗口内
            # 逐条核对同名卡片，减少重复打开窗口、切换来源和等待列表的耗时。
            remaining = materials[:max(0, 10 - selected_count)]
            selected, skipped = video_drawer_batch(
                page, remaining, selected_count, allow_missing=True)
            selected_count += len(selected)
            uploaded_count += len(selected)
            selected_material_ids.extend(selected)
            skipped_material_ids.extend(skipped)
        if uploaded_count == 0:
            raise UploadStopped("千川计划页尚无可选的已上传视频；已停止发布，稍后重试", stage=stage)
        if source == "my_video":
            raise UploadStopped("千川自选素材要求至少一条非主页视频；当前仅配置主页作品", stage=stage)
        stage = "title"
        add_title(page, str(job["published_video_title"] or job["product_name"]))
        stage = "plan_name"
        title = one(page.get_by_text("计划名称", exact=True), "计划名称标签")
        name_input = one(title.locator("xpath=ancestor::*[.//input][1]").locator("input"), "计划名称输入框")
        name_input.fill(str(job["plan_name"]))
        # 千川异步检查计划名称是否重复。必须失焦并等待校验结束，
        # 否则第一次点击发布只触发名称校验，不发创建请求。
        name_input.press("Tab")
        page.wait_for_timeout(2_000)
        if name_input.input_value() != job["plan_name"]:
            raise UploadStopped("计划名称回显不一致", stage=stage)
        if str(job["product_id"]) not in page.locator("body").inner_text():
            raise UploadStopped("提交前商品 ID 不在计划页", stage=stage)
        stage = "submit"
        # 平台介绍提示可能浮在表单上；先关闭一次性提示，
        # 否则第一次点击“发布计划”可能仅触发介绍而没有提交计划。
        acknowledgement = page.get_by_text("我知道了", exact=True)
        if acknowledgement.count() and acknowledgement.first.is_visible():
            acknowledgement.first.click(timeout=8_000)
        publish = exact(page, "发布计划")
        if not publish.is_enabled():
            raise UploadStopped("千川发布计划按钮不可用，请检查素材、标题和预算", stage="submit")
        if dry_run:
            return {"ok": True, "status": "ready_to_publish", "submitAttempted": False,
                    "selectedUploadedVideos": uploaded_count,
                    "selectedMaterialIds": selected_material_ids,
                    "skippedMaterialIds": skipped_material_ids}
        def capture(response):
            request = response.request
            parsed = urlparse(response.url)
            path = parsed.path
            if (request.method == "POST" and
                    (parsed.hostname or "").endswith(("jinritemai.com", "bytedance.net"))):
                try:
                    payload = response.json()
                    if len(api_events) < 80:
                        api_events.append({"path": path[:180], "httpStatus": response.status,
                                           "code": str(payload.get("code", ""))[:40] if isinstance(payload, dict) else "",
                                           "message": str(payload.get("msg") or payload.get("message") or "")[:160]
                                           if isinstance(payload, dict) else ""})
                    if isinstance(payload, dict) and payload.get("code") in (0, "0"):
                        data = payload.get("data") or {}
                        found = (data.get("ad_id") or data.get("adId") or data.get("plan_id")
                                 or data.get("promotion_id") or data.get("promotionId")) if isinstance(data, dict) else None
                        if ((re.search(r"create|publish|promotion|plan", path, re.I)
                             or str(job["product_id"]) in (request.post_data or ""))
                                and re.fullmatch(r"\d{8,20}", str(found or ""))):
                            responses.append(str(found))
                    elif isinstance(payload, dict):
                        message = str(payload.get("msg") or payload.get("message") or "")[:160]
                        if message and message.lower() not in {"ok", "success"}:
                            submission_errors.append(f"{path}: {message}")
                except Exception:
                    pass
        page.on("response", capture)
        # 点击可能已经送达平台却因导航超时抛错；从这一刻起一律先按待核查处理。
        submitted = True
        publish.click(timeout=8_000)
        confirmation = page.get_by_text("为保证全域投放的唯一性", exact=False)
        try:
            confirmation.first.wait_for(state="visible", timeout=3_000)
        except PlaywrightTimeoutError:
            pass
        if confirmation.count() and confirmation.first.is_visible():
            # 千川先打开二次确认框，此时创建请求尚未发送。确认框文案必须
            # 对应全域投放，再点最终按钮；仍按一次不确定提交处理。
            exact(page, "确认发布").click(timeout=8_000)
        page.wait_for_timeout(12_000)
        # 点击后若平台才展示介绍，关闭它以便截图能看清表单和校验结果；
        # 这里绝不再次点击发布，首次请求是否送达仍须核查。
        if acknowledgement.count() and acknowledgement.first.is_visible():
            acknowledgement.first.click(timeout=8_000)
            page.wait_for_timeout(2_000)
        # 只保存接口路径和状态，不保存请求体、Token 或完整网址。
        (diagnostics_dir / f"plan-{job_id}-events.json").write_text(
            json.dumps(api_events, ensure_ascii=False, indent=2), encoding="utf-8")
        page.screenshot(path=str(screenshot), full_page=True)
        unique = set(responses)
        if not unique:
            try:
                verified = verify_created_plan(browser.contexts[0], job,
                                               int(settings["daily_budget_yuan"]))
                if verified:
                    unique.add(verified)
            except Exception:
                # 列表暂未同步时保留待核查，绝不再次点击发布。
                pass
        material_note = (f"；本次选中 {uploaded_count} 条已上传视频，另有 "
                         f"{len(skipped_material_ids)} 条平台暂不可选：{','.join(skipped_material_ids)}"
                         if skipped_material_ids else f"；本次选中 {uploaded_count} 条已上传视频")
        if len(unique) == 1:
            return {"ok": True, "status": "published", "remotePlanId": unique.pop(),
                    "submitAttempted": True, "screenshotPath": str(screenshot),
                    "selectedMaterialIds": selected_material_ids,
                    "reason": "平台创建回执含明确计划 ID" + material_note}
        feedback = ("；平台提示：" + "；".join(dict.fromkeys(submission_errors))[:450]
                    if submission_errors else "")
        if not any(re.search(r"create|publish|submit", str(event.get("path")), re.I)
                   for event in api_events):
            feedback += "；发布点击后未观察到创建请求，可能仍有表单校验未完成"
        return {"ok": True, "status": "needs_review", "remotePlanId": "", "submitAttempted": True,
                "selectedMaterialIds": selected_material_ids,
                "screenshotPath": str(screenshot), "reason": "已点击发布计划，但未取得唯一计划 ID；请在千川核查，禁止自动重发" + material_note + feedback}
    except Exception as error:
        if created_page and page is not None:
            try:
                page.screenshot(path=str(screenshot), full_page=True, timeout=8_000)
            except Exception:
                pass
        return {"ok": True, "status": "needs_review" if submitted else "blocked",
                "remotePlanId": "", "submitAttempted": submitted,
                "screenshotPath": str(screenshot) if screenshot.exists() else "",
                "reason": f"{stage} 阶段：{error}"}
    finally:
        if page is not None:
            try:
                page.close()
            except Exception:
                pass
        if playwright is not None:
            playwright.stop()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--job-id", type=int, required=True)
    parser.add_argument("--profile-path", type=Path, required=True)
    parser.add_argument("--cdp-port", type=int, required=True)
    parser.add_argument("--douyin-account-id", type=int, default=0)
    parser.add_argument("--diagnostics-dir", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true", help="检查草稿但不点击发布计划")
    args = parser.parse_args()
    print(json.dumps(create_plan(args.db, args.job_id, args.profile_path, args.cdp_port,
                                 args.diagnostics_dir, args.douyin_account_id, args.dry_run),
                     ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
