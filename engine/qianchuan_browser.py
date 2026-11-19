"""千川素材上传的独立 CDP 执行器。

只连接已打开且校验归属的千川或抖音账号专属 profile。页面结构未获得实页证据时，
选择器必须唯一，提交结果必须回读远端素材 ID；不确定的写入一律隔离待核查。
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import urlopen


QIANCHUAN_HOST = "qianchuan.jinritemai.com"
PLAN_CREATE_PATH = "/uni-creation/product"
MATERIAL_PATH = re.compile(r"material|creative|asset", re.I)
WRITE_ENDPOINT = re.compile(r"(?:material|creative|asset|video).*(?:create|save|submit|add|publish)", re.I)
REMOTE_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
SUCCESS_TEXT = re.compile(r"上传成功|提交成功|保存成功|创建成功")
FAIL_TEXT = re.compile(r"上传失败|上传中断|网络异常|请求失败|操作被拦截")
CAPTCHA_TEXT = re.compile(r"请完成验证|滑动验证|安全验证|人机验证|验证码错误")
UPLOAD_LABELS = ("点击上传", "上传视频", "上传")


@dataclass(frozen=True)
class UploadOptions:
    profile_path: Path
    cdp_port: int
    qianchuan_account_id: int
    asset_path: Path
    asset_name: str
    diagnostics_dir: Path
    min_delay_ms: int = 800
    max_delay_ms: int = 2_000
    upload_timeout_s: int = 600
    result_timeout_s: int = 120
    advertiser_id: str = ""
    douyin_account_id: int = 0
    product_id: str = ""


class UploadStopped(RuntimeError):
    def __init__(self, reason: str, *, stage: str, manual: bool = False):
        super().__init__(reason)
        self.stage = stage
        self.manual = manual


def _result(status: str, reason: str, stage: str, submitted: bool,
            remote_id: str | None = None, screenshot: str | None = None,
            diagnostic: str | None = None) -> dict[str, Any]:
    return {
        "ok": True,  # 进程正常返回；业务结果以 status 区分。
        "status": status,
        "remoteMaterialId": remote_id,
        "reason": reason,
        "screenshotPath": screenshot,
        "diagnosticPath": diagnostic,
        "stage": stage,
        "submitAttempted": submitted,
    }


def _host(url: str) -> str:
    try:
        parsed = urlparse(url)
        return (parsed.hostname or "").lower() if parsed.scheme == "https" else ""
    except ValueError:
        return ""


def is_qianchuan_page(url: str) -> bool:
    return _host(url) == QIANCHUAN_HOST


def is_login_page(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").lower()
    return host in {QIANCHUAN_HOST, "business.oceanengine.com"} and "/login" in path


def read_verified_port(profile_path: Path, expected_port: int, account_id: int,
                       douyin_account_id: int = 0) -> int:
    """防止把别的账号的 CDP 端口传给本次上传。"""
    expected_name = (f"account-{douyin_account_id}" if douyin_account_id > 0
                     else f"qianchuan-{account_id}")
    if profile_path.name.lower() != expected_name:
        raise UploadStopped(f"千川浏览器目录应为 {expected_name}，实际为 {profile_path.name}", stage="profile")
    try:
        actual_port = int((profile_path / "DevToolsActivePort").read_text(encoding="utf-8").splitlines()[0])
    except (OSError, ValueError, IndexError) as error:
        raise UploadStopped("千川专属浏览器未打开或 CDP 端口不可读", stage="profile") from error
    if actual_port != expected_port or not 1 <= expected_port <= 65535:
        raise UploadStopped("千川浏览器端口与账号专属目录不一致", stage="profile")
    try:
        with urlopen(f"http://127.0.0.1:{expected_port}/json/version", timeout=2) as response:
            payload = json.load(response)
        if not isinstance(payload.get("webSocketDebuggerUrl"), str):
            raise ValueError("CDP 版本响应缺少调试连接")
    except Exception as error:
        raise UploadStopped("千川专属浏览器 CDP 未连接", stage="profile") from error
    return expected_port


def _validate(options: UploadOptions) -> None:
    if options.qianchuan_account_id <= 0:
        raise UploadStopped("千川账户 ID 无效", stage="input")
    if not re.fullmatch(r"\d{8,20}", options.advertiser_id):
        raise UploadStopped("计划页上传必须有有效的千川广告主 ID", stage="input")
    if not re.fullmatch(r"\d{1,30}", options.product_id):
        raise UploadStopped("计划页上传必须有精确商品 ID", stage="input")
    if options.douyin_account_id and (options.douyin_account_id <= 0 or not options.advertiser_id):
        raise UploadStopped("复用抖音账号浏览器时必须指定账号和千川广告主 ID", stage="input")
    if not options.asset_name.strip():
        raise UploadStopped("素材名称为空", stage="input")
    if options.asset_path.suffix.lower() != ".mp4" or not options.asset_path.is_file():
        raise UploadStopped("仅接受已存在的本地 MP4 素材", stage="input")
    if options.asset_path.stat().st_size <= 0:
        raise UploadStopped("本地 MP4 素材为空文件", stage="input")
    if options.min_delay_ms < 0 or options.max_delay_ms < options.min_delay_ms:
        raise UploadStopped("随机延时配置无效", stage="input")
    if options.upload_timeout_s <= 0 or options.result_timeout_s <= 0:
        raise UploadStopped("等待超时配置无效", stage="input")


def _delay(options: UploadOptions) -> None:
    time.sleep(random.uniform(options.min_delay_ms, options.max_delay_ms) / 1000)


def _visible_count(locator: Any) -> int:
    return sum(1 for index in range(locator.count()) if locator.nth(index).is_visible())


def _one_visible(locator: Any) -> Any | None:
    visible = [locator.nth(index) for index in range(locator.count()) if locator.nth(index).is_visible()]
    if len(visible) > 1:
        raise UploadStopped(f"页面控件定位不唯一：{len(visible)} 个可见匹配", stage="locate")
    return visible[0] if visible else None


def _named_control(page: Any, label: str, *, scope: Any | None = None) -> Any | None:
    root = scope or page
    for role in ("button", "link", "menuitem"):
        found = _one_visible(root.get_by_role(role, name=label, exact=True))
        if found is not None:
            return found
    return _one_visible(root.get_by_text(label, exact=True))


def _page_text(page: Any) -> str:
    return page.locator("body").inner_text(timeout=5_000)[:30_000]


def _check_page_gate(page: Any, stage: str) -> None:
    url = page.url
    if is_login_page(url):
        raise UploadStopped("千川登录态失效，请在该账户专属浏览器人工登录", stage=stage, manual=True)
    if not is_qianchuan_page(url):
        raise UploadStopped(f"当前页面不是千川工作台：{url}", stage=stage, manual=True)
    text = _page_text(page)
    dialogs = page.get_by_role("dialog")
    challenge_dialog = any(
        CAPTCHA_TEXT.search(dialogs.nth(index).inner_text())
        for index in range(dialogs.count()) if dialogs.nth(index).is_visible()
    )
    challenge_iframe = _visible_count(page.locator('iframe[src*="captcha"],iframe[src*="verify"]')) > 0
    if challenge_dialog or challenge_iframe or re.search(r"请完成验证|滑动验证|安全验证|人机验证", text):
        raise UploadStopped("千川页面要求人工完成验证", stage=stage, manual=True)
    if re.search(r"当前账号(?:无权|没有权限)|无访问权限|权限不足|无权限访问", text):
        raise UploadStopped("千川账户无上传页面权限，请账户管理员核实授权", stage=stage, manual=True)
    if _visible_count(page.locator('input[type="password"]')) > 0 and "登录" in text:
        raise UploadStopped("千川页面显示登录表单，请人工登录", stage=stage, manual=True)


def _advertiser_matches_page(page: Any, advertiser_id: str) -> bool:
    parsed = urlparse(page.url)
    declared = parse_qs(parsed.query).get("aavid", [])
    if declared:
        return declared == [advertiser_id]
    if "/openapi/" in parsed.path:
        return False
    try:
        return advertiser_id in _page_text(page)
    except Exception:
        return False


def _select_page(browser: Any, advertiser_id: str = "") -> Any:
    contexts = browser.contexts
    if len(contexts) != 1:
        raise UploadStopped(f"千川浏览器上下文数量异常：{len(contexts)}", stage="connect")
    pages = contexts[0].pages
    candidates = [page for page in pages if is_qianchuan_page(page.url)
                  and "/openapi/" not in urlparse(page.url).path]
    if advertiser_id:
        candidates = [page for page in candidates
                      if _advertiser_matches_page(page, advertiser_id)]
    if not candidates:
        login_pages = [page for page in pages if is_login_page(page.url)]
        if len(login_pages) == 1:
            return login_pages[0]  # 由登录闸门拦下，并留下登录页截图。
        reason = ("专属浏览器未显示目标千川广告主，请核对右上角账户 ID"
                  if advertiser_id else
                  "千川登录页不唯一，请人工核对" if login_pages else "千川工作台页面未打开")
        raise UploadStopped(reason, stage="connect", manual=bool(login_pages))
    material_pages = [page for page in candidates if MATERIAL_PATH.search(urlparse(page.url).path)]
    chosen = material_pages if material_pages else candidates
    if len(chosen) != 1:
        raise UploadStopped(f"千川页面有 {len(chosen)} 个候选标签，无法唯一定位", stage="connect")
    return chosen[0]


def _has_upload_action(page: Any) -> bool:
    if page.locator('input[type="file"]').count() > 0:
        return True
    return any(_named_control(page, label) is not None for label in UPLOAD_LABELS)


def _open_material_area(page: Any, options: UploadOptions) -> Any:
    """在商品全域投放创编页选定两处商品后，进入该商品的上传视频页签。"""
    if not _advertiser_matches_page(page, options.advertiser_id):
        raise UploadStopped("当前千川浏览器不是目标广告主", stage="navigate", manual=True)
    # 避免覆盖用户正在查看的千川标签；登录或控件异常时保留创编页供人工核查。
    plan_page = page.context.new_page()
    plan_page.goto(
        f"https://{QIANCHUAN_HOST}{PLAN_CREATE_PATH}?" +
        urlencode({"aavid": options.advertiser_id}),
        wait_until="domcontentloaded", timeout=30_000,
    )
    _check_page_gate(plan_page, "navigate")
    plan_page.get_by_text(options.advertiser_id, exact=False).first.wait_for(
        state="visible", timeout=15_000)
    if urlparse(plan_page.url).path != PLAN_CREATE_PATH:
        raise UploadStopped("没有进入商品全域投放创编页", stage="navigate", manual=True)
    # 执行器之间只共享已核对的页面定位；延迟导入避免初始化时循环引用。
    from qianchuan_plan_browser import (
        check_account, check_global_delivery, exact, one, select_material_product,
        select_product, select_radio,
    )
    check_account(plan_page, options.advertiser_id)
    check_global_delivery(plan_page)
    exact(plan_page, "添加商品").wait_for(state="visible", timeout=10_000)
    select_product(plan_page, options.product_id)
    select_radio(plan_page, "控成本投放")
    exact(plan_page, "自选投放素材").click(timeout=8_000)
    select_material_product(plan_page, options.product_id)
    add_video = exact(plan_page, "添加视频")
    if not add_video.is_enabled():
        raise UploadStopped("素材所属商品选中后仍无法添加视频", stage="navigate")
    add_video.click(timeout=8_000)
    upload_tab = one(plan_page.get_by_text("上传视频", exact=True), "上传视频页签")
    upload_tab.click(timeout=8_000)
    if not _has_upload_action(plan_page):
        raise UploadStopped("计划页上传视频页签没有唯一可用的文件入口", stage="file_input")
    return plan_page


def _file_input(page: Any) -> Any | None:
    all_inputs = page.locator('input[type="file"]')
    candidates = []
    for index in range(all_inputs.count()):
        item = all_inputs.nth(index)
        accept = (item.get_attribute("accept") or "").lower()
        if not accept or "video" in accept or "mp4" in accept:
            candidates.append(item)
    if len(candidates) > 1:
        raise UploadStopped(f"页面有 {len(candidates)} 个视频文件控件，无法唯一定位", stage="file_input")
    return candidates[0] if candidates else None


def _is_large_file_transfer_error(error: BaseException) -> bool:
    return bool(re.search(
        r"cannot transfer files larger than 50\s*mb|larger than 50\s*mb",
        str(error),
        flags=re.IGNORECASE,
    ))


def _set_files_via_local_cdp(page: Any, element_handle: Any, paths: list[Path]) -> None:
    """在本机浏览器上通过 CDP 设置文件，绕过 Playwright 远程传输 50 MB 限制。

    仅在浏览器和客户端位于同一台电脑时可用；远程浏览器会抛出原始异常，
    由上层把任务保留为待核查，避免把传输失败误记成上传成功。
    """
    marker = f"touguang-file-input-{uuid.uuid4().hex}"
    element_handle.evaluate(
        "(element, value) => element.setAttribute('data-touguang-file-input', value)",
        marker,
    )
    session = None
    try:
        session = page.context.new_cdp_session(page)
        document = session.send("DOM.getDocument", {"depth": -1, "pierce": True})
        root_node_id = int(document["root"]["nodeId"])
        selector = f'[data-touguang-file-input="{marker}"]'
        match = session.send("DOM.querySelector", {"nodeId": root_node_id, "selector": selector})
        node_id = int(match.get("nodeId") or 0)
        if node_id <= 0:
            raise RuntimeError("CDP 未找到千川视频文件控件")
        session.send("DOM.setFileInputFiles", {
            "nodeId": node_id,
            "files": [str(path) for path in paths],
        })
    finally:
        try:
            element_handle.evaluate(
                "element => element.removeAttribute('data-touguang-file-input')"
            )
        except Exception:
            pass
        if session is not None:
            try:
                session.detach()
            except Exception:
                pass


def _set_file_input_files(page: Any, file_input: Any, paths: list[Path], timeout: int) -> None:
    try:
        file_input.set_input_files(
            [str(path) for path in paths] if len(paths) > 1 else str(paths[0]),
            timeout=timeout,
        )
        return
    except Exception as error:
        if not _is_large_file_transfer_error(error):
            raise
        element_handle = file_input.element_handle()
        if element_handle is None:
            raise
        _set_files_via_local_cdp(page, element_handle, paths)


def _set_file_chooser_files(page: Any, chooser: Any, paths: list[Path], timeout: int) -> None:
    try:
        chooser.set_files(
            [str(path) for path in paths] if len(paths) > 1 else str(paths[0]),
            timeout=timeout,
        )
        return
    except Exception as error:
        if not _is_large_file_transfer_error(error):
            raise
        element_handle = getattr(chooser, "element", None)
        if element_handle is None:
            raise
        _set_files_via_local_cdp(page, element_handle, paths)


def _choose_file(page: Any, options: UploadOptions, mark_selection_started: Callable[[], None]) -> None:
    file_timeout_ms = options.upload_timeout_s * 1_000
    file_input = _file_input(page)
    if file_input is not None:
        mark_selection_started()
        _set_file_input_files(page, file_input, [options.asset_path], file_timeout_ms)
        return
    # 千川视频库先弹上传窗口，窗口内的“点击上传”才打开原生文件选择器。
    trigger = (_named_control(page, "点击上传") or
               next((control for label in UPLOAD_LABELS
                     if (control := _named_control(page, label)) is not None), None))
    if trigger is None:
        raise UploadStopped("素材页没有可唯一定位的上传入口", stage="file_input")
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    try:
        with page.expect_file_chooser(timeout=5_000) as chooser:
            trigger.click(timeout=5_000)
    except PlaywrightTimeoutError as error:
        # 有些版本点击后只打开弹窗，未立即弹原生文件选择器。
        file_input = _file_input(page)
        if file_input is not None:
            mark_selection_started()
            _set_file_input_files(page, file_input, [options.asset_path], file_timeout_ms)
            return
        inner_trigger = _named_control(page, "点击上传")
        if inner_trigger is None or trigger.inner_text().strip() == "点击上传":
            raise UploadStopped(f"点击上传入口后未出现文件控件：{error}", stage="file_input") from error
        try:
            with page.expect_file_chooser(timeout=5_000) as chooser:
                inner_trigger.click(timeout=5_000)
        except PlaywrightTimeoutError as nested_error:
            raise UploadStopped(f"上传弹窗未打开文件选择器：{nested_error}", stage="file_input") from nested_error
        mark_selection_started()
        _set_file_chooser_files(page, chooser.value, [options.asset_path], file_timeout_ms)
        return
    # 已收到文件选择事件后若设置文件失败，不再尝试第二次，避免重复上传。
    mark_selection_started()
    _set_file_chooser_files(page, chooser.value, [options.asset_path], file_timeout_ms)


def _upload_ready(page: Any, asset_path: Path) -> tuple[bool, str]:
    text = _page_text(page)
    if FAIL_TEXT.search(text):
        return False, "页面提示上传失败或网络中断"
    name_visible = asset_path.name in text or asset_path.stem in text
    success_visible = bool(SUCCESS_TEXT.search(text))
    hundred_percent = bool(re.search(r"(?:^|\s)100\s*%", text))
    if name_visible and (success_visible or hundred_percent):
        return True, "页面显示文件名和上传完成标记"
    # 实页上传完成时仅显示绿色完成图标；“确定”此前保持禁用。
    confirm = _one_visible(page.get_by_role("button", name="确定", exact=True))
    if name_visible and "已选择1/100" in text and confirm is not None and confirm.is_enabled():
        return True, "页面显示文件名且上传窗口允许确认"
    return False, "上传中或未出现文件名与完成标记"


def _wait_upload_ready(page: Any, options: UploadOptions) -> None:
    # 文件选择后页面可能短暂重绘，单次 body 读取超时不代表上传失败。
    # 只在原任务的等待窗口内重读页面，不重新选择文件或发起第二次上传。
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    deadline = time.monotonic() + options.upload_timeout_s
    while time.monotonic() < deadline:
        try:
            _check_page_gate(page, "upload_wait")
            ready, note = _upload_ready(page, options.asset_path)
        except PlaywrightTimeoutError:
            time.sleep(1)
            continue
        if ready:
            return
        if note.startswith("页面提示"):
            raise UploadStopped(note, stage="upload_wait", manual=True)
        time.sleep(1)
    raise UploadStopped(f"等待上传完成超时 {options.upload_timeout_s} 秒", stage="upload_wait", manual=True)


def _fill_name(page: Any, name: str) -> None:
    selectors = (
        'input[placeholder*="素材名称"]', 'input[placeholder="名称"]',
        'input[aria-label="素材名称"]', 'textarea[placeholder*="素材名称"]',
    )
    for selector in selectors:
        target = _one_visible(page.locator(selector))
        if target is None:
            continue
        target.fill(name, timeout=5_000)
        if target.input_value() != name:
            raise UploadStopped("素材名称填写后回读不一致", stage="name")
        return
    # 页面允许自动取文件名时不改名；若名称确为必填，提交按钮会保持禁用。


def _json_remote_id(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    for key in ("materialId", "material_id", "creativeId", "creative_id"):
        value = payload.get(key)
        if REMOTE_ID.fullmatch(str(value or "")):
            return str(value)
    for key in ("data", "result"):
        found = _json_remote_id(payload.get(key))
        if found:
            return found
    return None


def confirmed_response_id(responses: list[Any]) -> tuple[str | None, str | None]:
    """只认可提交/创建类响应，不把分片上传或预签名 ID 当成素材 ID。"""
    for response in responses:
        request = response.request
        if request.method.upper() not in {"POST", "PUT"} or not 200 <= response.status < 300:
            continue
        if not is_qianchuan_page(response.url) or not WRITE_ENDPOINT.search(urlparse(response.url).path):
            continue
        try:
            payload = response.json()
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        code = payload.get("code", payload.get("err_no", payload.get("status")))
        if code is not None and code not in (0, "0", 200, "200"):
            continue
        if code is None and payload.get("success") is not True:
            continue
        remote_id = _json_remote_id(payload)
        if remote_id:
            return remote_id, response.url
    return None, None


def _wait_confirmation(page: Any, responses: list[Any], options: UploadOptions) -> tuple[str | None, str | None]:
    """仅从本次上传后的成功响应取得素材 ID；同名旧素材不能充当回执。"""
    deadline = time.monotonic() + options.result_timeout_s
    while time.monotonic() < deadline:
        _check_page_gate(page, "verify")
        text = _page_text(page)
        if FAIL_TEXT.search(text):
            raise UploadStopped("千川返回上传/提交失败，需核对是否已创建素材", stage="verify", manual=True)
        remote_id, endpoint = confirmed_response_id(responses)
        same_material_visible = (
            options.asset_name in text or options.asset_path.name in text or options.asset_path.stem in text
        )
        if remote_id and same_material_visible:
            return remote_id, endpoint
        time.sleep(1)
    return None, None


def _save_evidence(page: Any | None, options: UploadOptions, result: dict[str, Any],
                   endpoint: str | None = None) -> dict[str, Any]:
    try:
        options.diagnostics_dir.mkdir(parents=True, exist_ok=True)
        stem = f"qianchuan-upload-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
        screenshot = options.diagnostics_dir / f"{stem}.png"
        diagnostic = options.diagnostics_dir / f"{stem}.json"
        if page is not None:
            try:
                page.screenshot(path=str(screenshot), full_page=True, timeout=8_000)
                result["screenshotPath"] = str(screenshot)
            except Exception:
                pass  # 浏览器断连时无法截图，仍保留诊断 JSON。
        details: dict[str, Any] = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "qianchuanAccountId": options.qianchuan_account_id,
            "productId": options.product_id,
            "assetFileName": options.asset_path.name,
            "assetName": options.asset_name,
            "responseEndpoint": endpoint,
            "result": result,
        }
        if page is not None:
            try:
                details.update({"url": page.url, "title": page.title(),
                                "readyState": page.evaluate("document.readyState")})
                details["uiControls"] = page.evaluate("""() => ({
                  headings: [...document.querySelectorAll('h1,h2,h3,[role="heading"]')]
                    .map(el => el.textContent?.trim().slice(0, 80)).filter(Boolean).slice(0, 20),
                  buttons: [...document.querySelectorAll('button,[role="button"],a,[role="menuitem"]')]
                    .filter(el => el.getClientRects().length)
                    .map(el => el.textContent?.trim().slice(0, 80)).filter(Boolean).slice(0, 60),
                  fileInputs: [...document.querySelectorAll('input[type="file"]')]
                    .map(el => ({ accept: el.getAttribute('accept'), multiple: el.multiple }))
                })""")
            except Exception:
                details["pageReadError"] = "浏览器断连或页面已关闭"
        result["diagnosticPath"] = str(diagnostic)
        diagnostic.write_text(json.dumps(details, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as error:
        result["reason"] += f"；诊断落盘失败：{error}"
    return result


def upload_material(options: UploadOptions) -> dict[str, Any]:
    stage = "input"
    page = None
    submitted = False
    try:
        _validate(options)
        port = read_verified_port(options.profile_path, options.cdp_port,
                                  options.qianchuan_account_id, options.douyin_account_id)
        # 延迟导入使输入校验/协议单测不依赖本机安装 Playwright 浏览器。
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            stage = "connect"
            browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}", timeout=15_000)
            page = _select_page(browser, options.advertiser_id)
            _check_page_gate(page, stage)
            responses: list[Any] = []
            stage = "navigate"
            page = _open_material_area(page, options)
            if options.advertiser_id and not _advertiser_matches_page(page, options.advertiser_id):
                raise UploadStopped("素材页未显示目标千川广告主，已停止文件选择", stage="navigate")
            page.on("response", lambda response: responses.append(response))
            _delay(options)
            # 计划页的同名行未必属于当前商品，不能凭文件名冒充本次上传回执。
            responses.clear()  # 不把打开计划页时的旧响应当成本次上传回执。
            stage = "file_input"
            # 找不到控件时明确未提交；真正设置文件前开始保守隔离窗口。
            def mark_selection_started() -> None:
                nonlocal submitted
                submitted = True
            _choose_file(page, options, mark_selection_started)
            stage = "upload_wait"
            _wait_upload_ready(page, options)
            _delay(options)
            stage = "name"
            _fill_name(page, options.asset_name)
            _delay(options)
            stage = "submit"
            # 这里只能确认“上传视频”弹窗；绝不触碰计划页的“发布计划”。
            control = _one_visible(page.get_by_role("button", name="确定", exact=True))
            if control is None or not control.is_enabled():
                raise UploadStopped("上传视频弹窗没有唯一可确认的确定按钮", stage="submit", manual=True)
            control.click(timeout=5_000)
            stage = "verify"
            remote_id, endpoint = _wait_confirmation(page, responses, options)
            if remote_id:
                result = _save_evidence(page, options,
                                        _result("succeeded", "千川上传结果已确认", stage, submitted, remote_id),
                                        endpoint)
                try:
                    page.close()
                except Exception:
                    pass
                return result
            reason = "素材文件已交给千川，但未回读到明确的远端素材 ID；请人工核查后处理"
            return _save_evidence(page, options, _result("needs_review", reason, stage, submitted))
    except UploadStopped as error:
        status = "needs_review" if submitted else "failed"
        result = _result(status, str(error), error.stage, submitted)
        result["manualActionRequired"] = error.manual
        return _save_evidence(page, options, result)
    except Exception as error:
        status = "needs_review" if submitted else "failed"
        return _save_evidence(page, options, _result(status, f"千川上传在 {stage} 阶段异常：{error}", stage, submitted))




def upload_material_batch(options_list: list[UploadOptions]) -> dict[str, Any]:
    """旧批量入口停用；计划页上传必须逐件核对两处商品 ID。"""
    return {"ok": False, "status": "failed", "items": [
        {"assetName": option.asset_name, **_result(
            "failed", "计划页上传须逐件选定投放商品与素材所属商品；旧批量入口已停用",
            "input", False)} for option in options_list]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="通过 Step 11 千川专属 Chrome 只执行一次素材上传")
    parser.add_argument("upload", choices=["upload", "batch-upload"])
    parser.add_argument("--profile-path", type=Path, required=True)
    parser.add_argument("--cdp-port", type=int, required=True)
    parser.add_argument("--qianchuan-account-id", type=int, required=True)
    parser.add_argument("--advertiser-id", default="")
    parser.add_argument("--douyin-account-id", type=int, default=0)
    parser.add_argument("--asset-path", type=Path)
    parser.add_argument("--asset-name", default="")
    parser.add_argument("--product-id", default="")
    parser.add_argument("--batch-assets-json", default="[]")
    parser.add_argument("--diagnostics-dir", type=Path, required=True)
    parser.add_argument("--min-delay-ms", type=int, default=800)
    parser.add_argument("--max-delay-ms", type=int, default=2_000)
    parser.add_argument("--upload-timeout-s", type=int, default=600)
    parser.add_argument("--result-timeout-s", type=int, default=120)
    args = parser.parse_args(argv)
    if args.upload == "upload" and args.asset_path is None:
        parser.error("单条上传必须提供 --asset-path")
    if args.upload == "batch-upload":
        try:
            assets = json.loads(args.batch_assets_json)
            if not isinstance(assets, list) or not 2 <= len(assets) <= 10:
                raise ValueError("批量素材须为 2–10 条")
            options_list = [UploadOptions(
                profile_path=args.profile_path, cdp_port=args.cdp_port,
                qianchuan_account_id=args.qianchuan_account_id,
                asset_path=Path(item["filePath"]), asset_name=item["materialName"],
                diagnostics_dir=args.diagnostics_dir, min_delay_ms=args.min_delay_ms,
                max_delay_ms=args.max_delay_ms, upload_timeout_s=args.upload_timeout_s,
                result_timeout_s=args.result_timeout_s, advertiser_id=args.advertiser_id,
                douyin_account_id=args.douyin_account_id,
                product_id=str(item.get("productId", args.product_id))) for item in assets]
        except (ValueError, TypeError, KeyError) as error:
            parser.error(f"批量素材参数无效：{error}")
        print(json.dumps(upload_material_batch(options_list), ensure_ascii=False), flush=True)
        return 0
    options = UploadOptions(
        profile_path=args.profile_path, cdp_port=args.cdp_port,
        qianchuan_account_id=args.qianchuan_account_id,
        asset_path=args.asset_path, asset_name=args.asset_name,
        diagnostics_dir=args.diagnostics_dir,
        min_delay_ms=args.min_delay_ms, max_delay_ms=args.max_delay_ms,
        upload_timeout_s=args.upload_timeout_s, result_timeout_s=args.result_timeout_s,
        advertiser_id=args.advertiser_id, douyin_account_id=args.douyin_account_id,
        product_id=args.product_id,
    )
    result = upload_material(options)
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
