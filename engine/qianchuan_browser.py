"""千川素材上传的独立 CDP 执行器。

只连接 Step 11 已打开的 qianchuan-{id} profile。页面结构未获得实页证据时，
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
from urllib.parse import urlparse
from urllib.request import urlopen


QIANCHUAN_HOST = "qianchuan.jinritemai.com"
MATERIAL_PATH = re.compile(r"material|creative|asset", re.I)
WRITE_ENDPOINT = re.compile(r"(?:material|creative|asset|video).*(?:create|save|submit|add|publish)", re.I)
REMOTE_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
SUCCESS_TEXT = re.compile(r"上传成功|提交成功|保存成功|创建成功")
FAIL_TEXT = re.compile(r"上传失败|上传中断|网络异常|请求失败|操作被拦截")
CAPTCHA_TEXT = re.compile(r"请完成验证|滑动验证|安全验证|人机验证|验证码错误")
NAV_LABELS = ("素材中心", "素材管理", "素材库", "视频素材", "素材")
UPLOAD_LABELS = ("上传素材", "上传视频", "新建素材", "新增素材", "上传")
SUBMIT_LABELS = ("确认上传", "提交素材", "开始上传", "提交", "保存", "确定", "完成")


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
    upload_timeout_s: int = 120
    result_timeout_s: int = 60


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


def read_verified_port(profile_path: Path, expected_port: int, account_id: int) -> int:
    """防止把别的账号的 CDP 端口传给本次上传。"""
    expected_name = f"qianchuan-{account_id}"
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
    if _visible_count(page.locator('input[type="password"]')) > 0 and "登录" in text:
        raise UploadStopped("千川页面显示登录表单，请人工登录", stage=stage, manual=True)


def _select_page(browser: Any) -> Any:
    contexts = browser.contexts
    if len(contexts) != 1:
        raise UploadStopped(f"千川浏览器上下文数量异常：{len(contexts)}", stage="connect")
    pages = contexts[0].pages
    candidates = [page for page in pages if is_qianchuan_page(page.url)]
    if not candidates:
        login_pages = [page for page in pages if is_login_page(page.url)]
        if len(login_pages) == 1:
            return login_pages[0]  # 由登录闸门拦下，并留下登录页截图。
        reason = "千川登录页不唯一，请人工核对" if login_pages else "千川工作台页面未打开"
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


def _material_context(page: Any) -> bool:
    if MATERIAL_PATH.search(urlparse(page.url).path):
        return True
    try:
        title = page.title()
        headings = page.get_by_role("heading", name=re.compile("素材"))
        return "素材" in title or _visible_count(headings) > 0
    except Exception:
        return False


def _open_material_area(page: Any, options: UploadOptions) -> Any:
    if _material_context(page) and _has_upload_action(page):
        return page
    for _ in range(3):
        _check_page_gate(page, "navigate")
        if _material_context(page) and _has_upload_action(page):
            return page
        next_control = next((control for label in NAV_LABELS
                             if (control := _named_control(page, label)) is not None), None)
        if next_control is None:
            break
        before_url = page.url
        before_pages = set(page.context.pages)
        next_control.click(timeout=5_000)
        _delay(options)
        new_pages = [opened for opened in page.context.pages if opened not in before_pages]
        if len(new_pages) > 1:
            raise UploadStopped("素材入口同时打开多个千川页面，无法唯一定位", stage="navigate")
        if new_pages:
            page = new_pages[0]
        try:
            page.wait_for_load_state("domcontentloaded", timeout=10_000)
        except Exception:
            pass  # SPA 页面可能不会触发新导航；下面继续检查实际页面。
        if page.url == before_url and not _has_upload_action(page):
            continue
    _check_page_gate(page, "navigate")
    if not _material_context(page) or not _has_upload_action(page):
        raise UploadStopped("未唯一识别千川素材上传入口；需要实页 DOM 校准", stage="navigate")
    return page


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


def _choose_file(page: Any, options: UploadOptions, mark_selection_started: Callable[[], None]) -> None:
    file_input = _file_input(page)
    if file_input is not None:
        mark_selection_started()
        file_input.set_input_files(str(options.asset_path), timeout=10_000)
        return
    trigger = next((control for label in UPLOAD_LABELS
                    if (control := _named_control(page, label)) is not None), None)
    if trigger is None:
        raise UploadStopped("素材页没有可唯一定位的上传入口", stage="file_input")
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    try:
        with page.expect_file_chooser(timeout=5_000) as chooser:
            trigger.click(timeout=5_000)
    except PlaywrightTimeoutError as error:
        # 有些版本点击后只打开弹窗，未立即弹原生文件选择器。
        file_input = _file_input(page)
        if file_input is None:
            raise UploadStopped(f"点击上传入口后未出现文件控件：{error}", stage="file_input") from error
        mark_selection_started()
        file_input.set_input_files(str(options.asset_path), timeout=10_000)
        return
    # 已收到文件选择事件后若设置文件失败，不再尝试第二次，避免重复上传。
    mark_selection_started()
    chooser.value.set_files(str(options.asset_path))


def _upload_ready(page: Any, asset_path: Path) -> tuple[bool, str]:
    text = _page_text(page)
    if FAIL_TEXT.search(text):
        return False, "页面提示上传失败或网络中断"
    name_visible = asset_path.name in text or asset_path.stem in text
    success_visible = bool(SUCCESS_TEXT.search(text))
    hundred_percent = bool(re.search(r"(?:^|\s)100\s*%", text))
    if name_visible and (success_visible or hundred_percent):
        return True, "页面显示文件名和上传完成标记"
    return False, "上传中或未出现文件名与完成标记"


def _wait_upload_ready(page: Any, options: UploadOptions) -> None:
    deadline = time.monotonic() + options.upload_timeout_s
    while time.monotonic() < deadline:
        _check_page_gate(page, "upload_wait")
        ready, note = _upload_ready(page, options.asset_path)
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


def _submit_control(page: Any) -> Any | None:
    dialogs = page.get_by_role("dialog")
    root = _one_visible(dialogs) if _visible_count(dialogs) else page
    for label in SUBMIT_LABELS:
        control = _named_control(page, label, scope=root)
        if control is not None and control.is_enabled():
            return control
    return None


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


def _dom_remote_id(page: Any, name: str) -> str | None:
    """仅从包含本素材名称的行中取显式 materialId，避免拿到账号或别的素材 ID。"""
    return page.evaluate("""(name) => {
      const matches = [];
      for (const row of document.querySelectorAll('tr,[role="row"],[data-material-id]')) {
        if (!row.textContent?.includes(name)) continue;
        const value = row.getAttribute('data-material-id') ||
          row.querySelector('[data-material-id]')?.getAttribute('data-material-id');
        if (value && /^[A-Za-z0-9_-]{8,64}$/.test(value)) matches.push(value);
      }
      const unique = [...new Set(matches)];
      return unique.length === 1 ? unique[0] : null;
    }""", name)


def _wait_confirmation(page: Any, responses: list[Any], options: UploadOptions,
                       prior_remote_id: str | None) -> tuple[str | None, str | None]:
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
        if SUCCESS_TEXT.search(text):
            remote_id = _dom_remote_id(page, options.asset_name)
            if remote_id and remote_id != prior_remote_id:
                return remote_id, "material_row"
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
        port = read_verified_port(options.profile_path, options.cdp_port, options.qianchuan_account_id)
        # 延迟导入使输入校验/协议单测不依赖本机安装 Playwright 浏览器。
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            stage = "connect"
            browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}", timeout=15_000)
            page = _select_page(browser)
            _check_page_gate(page, stage)
            responses: list[Any] = []
            stage = "navigate"
            page = _open_material_area(page, options)
            page.on("response", responses.append)
            _delay(options)
            prior_remote_id = _dom_remote_id(page, options.asset_name)
            responses.clear()  # 不把打开素材页时的旧响应当成本次提交回执。
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
            control = _submit_control(page)
            if control is not None:
                control.click(timeout=5_000)
            stage = "verify"
            remote_id, endpoint = _wait_confirmation(page, responses, options, prior_remote_id)
            if remote_id:
                return _save_evidence(page, options,
                                      _result("succeeded", "千川提交结果已确认", stage, submitted, remote_id),
                                      endpoint)
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="通过 Step 11 千川专属 Chrome 只执行一次素材上传")
    parser.add_argument("upload", choices=["upload"])
    parser.add_argument("--profile-path", type=Path, required=True)
    parser.add_argument("--cdp-port", type=int, required=True)
    parser.add_argument("--qianchuan-account-id", type=int, required=True)
    parser.add_argument("--asset-path", type=Path, required=True)
    parser.add_argument("--asset-name", required=True)
    parser.add_argument("--diagnostics-dir", type=Path, required=True)
    parser.add_argument("--min-delay-ms", type=int, default=800)
    parser.add_argument("--max-delay-ms", type=int, default=2_000)
    parser.add_argument("--upload-timeout-s", type=int, default=120)
    parser.add_argument("--result-timeout-s", type=int, default=60)
    args = parser.parse_args(argv)
    options = UploadOptions(
        profile_path=args.profile_path, cdp_port=args.cdp_port,
        qianchuan_account_id=args.qianchuan_account_id,
        asset_path=args.asset_path, asset_name=args.asset_name,
        diagnostics_dir=args.diagnostics_dir,
        min_delay_ms=args.min_delay_ms, max_delay_ms=args.max_delay_ms,
        upload_timeout_s=args.upload_timeout_s, result_timeout_s=args.result_timeout_s,
    )
    result = upload_material(options)
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
