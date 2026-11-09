"""千川浏览器上传的保守判定与防重复回归。"""

from __future__ import annotations

import tempfile
import types
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import qianchuan_browser as qc


class FakeResponse:
    def __init__(self, url: str, payload: dict, *, method: str = "POST", status: int = 200):
        self.url = url
        self._payload = payload
        self.request = types.SimpleNamespace(method=method)
        self.status = status

    def json(self):
        return self._payload


class FakePlaywrightContext:
    def __enter__(self):
        browser = types.SimpleNamespace(contexts=[])
        return types.SimpleNamespace(chromium=types.SimpleNamespace(connect_over_cdp=lambda *a, **k: browser))

    def __exit__(self, *_args):
        return False


class QianchuanBrowserTests(unittest.TestCase):
    def test_login_and_workbench_hosts_are_distinct(self):
        self.assertTrue(qc.is_login_page("https://business.oceanengine.com/login?appKey=80"))
        self.assertFalse(qc.is_qianchuan_page("https://business.oceanengine.com/login?appKey=80"))
        self.assertTrue(qc.is_qianchuan_page("https://qianchuan.jinritemai.com/dashboard"))
        self.assertFalse(qc.is_qianchuan_page("http://qianchuan.jinritemai.com/dashboard"))

    def test_account_permission_gate_requires_manual_action(self):
        page = types.SimpleNamespace(
            url="https://qianchuan.jinritemai.com/dashboard",
            get_by_role=lambda *_args: types.SimpleNamespace(count=lambda: 0),
            locator=lambda *_args: object(),
        )
        with patch.object(qc, "_page_text", return_value="当前账号权限不足"), \
             patch.object(qc, "_visible_count", return_value=0), \
             self.assertRaises(qc.UploadStopped) as error:
            qc._check_page_gate(page, "navigate")
        self.assertTrue(error.exception.manual)

    def test_only_successful_create_response_with_material_id_counts(self):
        responses = [
            FakeResponse("https://qianchuan.jinritemai.com/api/material/upload", {"code": 0, "data": {"materialId": "123456789"}}),
            FakeResponse("https://qianchuan.jinritemai.com/api/material/create", {"code": 11001, "data": {"materialId": "234567890"}}),
        ]
        self.assertEqual(qc.confirmed_response_id(responses), (None, None))
        responses.append(FakeResponse(
            "https://qianchuan.jinritemai.com/api/material/create",
            {"code": 0, "data": {"materialId": "345678901"}},
        ))
        self.assertEqual(qc.confirmed_response_id(responses)[0], "345678901")

    def test_other_domain_or_http_error_cannot_confirm(self):
        responses = [
            FakeResponse("https://other.example/api/material/create", {"code": 0, "materialId": "123456789"}),
            FakeResponse("https://qianchuan.jinritemai.com/api/material/create",
                         {"code": 0, "materialId": "123456789"}, status=500),
            FakeResponse("https://qianchuan.jinritemai.com/api/material/create",
                         {"code": 11001, "success": True, "materialId": "123456789"}),
        ]
        self.assertEqual(qc.confirmed_response_id(responses), (None, None))

    def test_mismatched_account_profile_stops_before_connect(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            profile = Path(temp_dir) / "qianchuan-2"
            profile.mkdir()
            (profile / "DevToolsActivePort").write_text("51040\n", encoding="utf-8")
            with self.assertRaises(qc.UploadStopped):
                qc.read_verified_port(profile, 51040, 1)

    def test_reused_account_profile_requires_matching_advertiser(self):
        matching = types.SimpleNamespace(
            url="https://qianchuan.jinritemai.com/home?aavid=1878909235499335")
        other = types.SimpleNamespace(
            url="https://qianchuan.jinritemai.com/home?aavid=1875414412067016")
        oauth = types.SimpleNamespace(
            url="https://qianchuan.jinritemai.com/openapi/qc/audit/oauth.html")
        browser = types.SimpleNamespace(contexts=[types.SimpleNamespace(
            pages=[oauth, other, matching])])
        self.assertIs(qc._select_page(browser, "1878909235499335"), matching)
        with self.assertRaises(qc.UploadStopped):
            qc._select_page(browser, "1879582387470400")
        with tempfile.TemporaryDirectory() as temp_dir:
            profile = Path(temp_dir) / "account-5"
            profile.mkdir()
            with self.assertRaises(qc.UploadStopped):
                qc.read_verified_port(profile, 51040, 4, 4)

    def test_invalid_file_writes_failure_diagnostic_without_browser(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            options = qc.UploadOptions(root / "qianchuan-1", 51040, 1,
                                       root / "missing.mp4", "素材", root / "evidence")
            result = qc.upload_material(options)
            self.assertEqual(result["status"], "failed")
            self.assertFalse(result["submitAttempted"])
            self.assertTrue(Path(result["diagnosticPath"]).is_file())

    def test_file_selection_exception_is_isolated_for_manual_review(self):
        def select_then_disconnect(_page, _options, mark_selection_started):
            mark_selection_started()
            raise qc.UploadStopped("选择文件后断线", stage="file_input")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "video.mp4"
            video.write_bytes(b"test")
            options = qc.UploadOptions(root / "qianchuan-1", 51040, 1,
                                       video, "视频素材", root / "evidence")
            fake_page = types.SimpleNamespace(on=lambda *_args: None)
            fake_module = types.ModuleType("playwright.sync_api")
            fake_module.sync_playwright = FakePlaywrightContext
            with patch.dict("sys.modules", {"playwright": types.ModuleType("playwright"),
                                            "playwright.sync_api": fake_module}), \
                 patch.object(qc, "read_verified_port", return_value=51040), \
                 patch.object(qc, "_select_page", return_value=fake_page), \
                 patch.object(qc, "_check_page_gate"), \
                 patch.object(qc, "_open_material_area", return_value=fake_page), \
                 patch.object(qc, "_dom_remote_id", return_value=None), \
                 patch.object(qc, "_search_material_id", return_value=None), \
                 patch.object(qc, "_delay"), \
                 patch.object(qc, "_choose_file", side_effect=select_then_disconnect), \
                 patch.object(qc, "_save_evidence", side_effect=lambda page, options, result: result):
                result = qc.upload_material(options)
            self.assertEqual(result["status"], "needs_review")
            self.assertTrue(result["submitAttempted"])
            self.assertIsNone(result["remoteMaterialId"])

    def test_upload_wait_recovers_from_one_transient_page_timeout(self):
        class FakePlaywrightTimeout(Exception):
            pass

        fake_module = types.ModuleType("playwright.sync_api")
        fake_module.TimeoutError = FakePlaywrightTimeout
        options = qc.UploadOptions(Path("qianchuan-1"), 51040, 1,
                                   Path("video.mp4"), "video.mp4", Path("evidence"),
                                   upload_timeout_s=30)
        with patch.dict("sys.modules", {"playwright": types.ModuleType("playwright"),
                                        "playwright.sync_api": fake_module}), \
             patch.object(qc, "_check_page_gate", side_effect=[FakePlaywrightTimeout(), None]) as gate, \
             patch.object(qc, "_upload_ready", return_value=(True, "已完成")) as ready, \
             patch.object(qc.time, "sleep"):
            qc._wait_upload_ready(object(), options)
        self.assertEqual(gate.call_count, 2)
        self.assertEqual(ready.call_count, 1)

    def test_existing_remote_material_is_reused_without_upload(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "video.mp4"
            video.write_bytes(b"test")
            options = qc.UploadOptions(root / "qianchuan-1", 51040, 1,
                                       video, "video.mp4", root / "evidence")
            fake_page = types.SimpleNamespace(on=lambda *_args: None)
            fake_module = types.ModuleType("playwright.sync_api")
            fake_module.sync_playwright = FakePlaywrightContext
            with patch.dict("sys.modules", {"playwright": types.ModuleType("playwright"),
                                            "playwright.sync_api": fake_module}), \
                 patch.object(qc, "read_verified_port", return_value=51040), \
                 patch.object(qc, "_select_page", return_value=fake_page), \
                 patch.object(qc, "_check_page_gate"), \
                 patch.object(qc, "_open_material_area", return_value=fake_page), \
                 patch.object(qc, "_search_material_id", return_value="7702637375117262889"), \
                 patch.object(qc, "_delay"), \
                 patch.object(qc, "_choose_file") as choose_file, \
                 patch.object(qc, "_save_evidence", side_effect=lambda page, options, result, *a: result):
                result = qc.upload_material(options)
            self.assertEqual(result["status"], "succeeded")
            self.assertEqual(result["remoteMaterialId"], "7702637375117262889")
            self.assertFalse(result["submitAttempted"])
            choose_file.assert_not_called()

    def test_missing_file_control_before_selection_is_recoverable_failure(self):
        def no_control(_page, _options, _mark_selection_started):
            raise qc.UploadStopped("未识别上传控件", stage="file_input")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "video.mp4"
            video.write_bytes(b"test")
            options = qc.UploadOptions(root / "qianchuan-1", 51040, 1,
                                       video, "视频素材", root / "evidence")
            fake_page = types.SimpleNamespace(on=lambda *_args: None)
            fake_module = types.ModuleType("playwright.sync_api")
            fake_module.sync_playwright = FakePlaywrightContext
            with patch.dict("sys.modules", {"playwright": types.ModuleType("playwright"),
                                            "playwright.sync_api": fake_module}), \
                 patch.object(qc, "read_verified_port", return_value=51040), \
                 patch.object(qc, "_select_page", return_value=fake_page), \
                 patch.object(qc, "_check_page_gate"), \
                 patch.object(qc, "_open_material_area", return_value=fake_page), \
                 patch.object(qc, "_dom_remote_id", return_value=None), \
                 patch.object(qc, "_search_material_id", return_value=None), \
                 patch.object(qc, "_delay"), \
                 patch.object(qc, "_choose_file", side_effect=no_control), \
                 patch.object(qc, "_save_evidence", side_effect=lambda page, options, result: result):
                result = qc.upload_material(options)
            self.assertEqual(result["status"], "failed")
            self.assertFalse(result["submitAttempted"])

    def test_login_required_before_file_selection_releases_quota(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "video.mp4"
            video.write_bytes(b"test")
            options = qc.UploadOptions(root / "qianchuan-1", 51040, 1,
                                       video, "视频素材", root / "evidence")
            fake_module = types.ModuleType("playwright.sync_api")
            fake_module.sync_playwright = FakePlaywrightContext
            with patch.dict("sys.modules", {"playwright": types.ModuleType("playwright"),
                                            "playwright.sync_api": fake_module}), \
                 patch.object(qc, "read_verified_port", return_value=51040), \
                 patch.object(qc, "_select_page", return_value=object()), \
                 patch.object(qc, "_check_page_gate", side_effect=qc.UploadStopped(
                     "请人工登录", stage="connect", manual=True)), \
                 patch.object(qc, "_save_evidence", side_effect=lambda page, options, result: result):
                result = qc.upload_material(options)
            self.assertEqual(result["status"], "failed")
            self.assertFalse(result["submitAttempted"])
            self.assertTrue(result["manualActionRequired"])

    def test_classified_business_failure_exits_zero_for_queue(self):
        arguments = [
            "upload", "--profile-path", "D:/browser-profiles/qianchuan-1",
            "--cdp-port", "51040", "--qianchuan-account-id", "1",
            "--asset-path", "D:/missing.mp4", "--asset-name", "素材",
            "--diagnostics-dir", "D:/diagnostics",
        ]
        with patch.object(qc, "upload_material", return_value=qc._result("failed", "输入无效", "input", False)), \
             patch("sys.stdout", new_callable=StringIO) as output:
            self.assertEqual(qc.main(arguments), 0)
        self.assertIn('"ok": true', output.getvalue())
        self.assertIn('"status": "failed"', output.getvalue())

    def test_batch_refuses_single_file_input_before_selecting_any_file(self):
        input_control = types.SimpleNamespace(get_attribute=lambda _name: None)
        option = qc.UploadOptions(Path("qianchuan-1"), 51040, 1,
                                  Path("one.mp4"), "one.mp4", Path("evidence"))
        selected = []
        with patch.object(qc, "_file_input", return_value=input_control), \
             self.assertRaisesRegex(qc.UploadStopped, "不支持多选"):
            qc._choose_files(object(), option, [Path("one.mp4"), Path("two.mp4")],
                             lambda: selected.append(True))
        self.assertEqual(selected, [])

    def test_batch_partial_remote_confirmation_is_per_material(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            options = []
            for name in ("one.mp4", "two.mp4"):
                path = root / name
                path.write_bytes(b"isolated video")
                options.append(qc.UploadOptions(root / "qianchuan-1", 51040, 1,
                                                path, name, root / "evidence"))
            fake_page = types.SimpleNamespace()
            fake_module = types.ModuleType("playwright.sync_api")
            fake_module.sync_playwright = FakePlaywrightContext

            def select_files(_page, _options, paths, started):
                self.assertEqual([path.name for path in paths], ["one.mp4", "two.mp4"])
                started()

            responses = [None, None, "7700000000000000001", None]
            with patch.dict("sys.modules", {"playwright": types.ModuleType("playwright"),
                                            "playwright.sync_api": fake_module}), \
                 patch.object(qc, "read_verified_port", return_value=51040), \
                 patch.object(qc, "_select_page", return_value=fake_page), \
                 patch.object(qc, "_check_page_gate"), \
                 patch.object(qc, "_open_material_area", return_value=fake_page), \
                 patch.object(qc, "_search_material_id", side_effect=responses), \
                 patch.object(qc, "_delay"), \
                 patch.object(qc, "_choose_files", side_effect=select_files), \
                 patch.object(qc, "_wait_batch_ready"), \
                 patch.object(qc, "_submit_control", return_value=types.SimpleNamespace(click=lambda **_k: None)), \
                 patch.object(qc.time, "monotonic", side_effect=[0, 1, 121]), \
                 patch.object(qc.time, "sleep"), \
                 patch.object(qc, "_save_evidence", side_effect=lambda page, option, result: result):
                result = qc.upload_material_batch(options)
            self.assertEqual([item["status"] for item in result["items"]],
                             ["succeeded", "needs_review"])
            self.assertEqual(result["items"][0]["remoteMaterialId"], "7700000000000000001")
            self.assertTrue(result["items"][1]["submitAttempted"])

    def test_batch_duplicate_remote_ids_are_not_marked_as_unique_successes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            options = []
            for name in ("one.mp4", "two.mp4"):
                path = root / name
                path.write_bytes(b"same content")
                options.append(qc.UploadOptions(root / "qianchuan-1", 51040, 1,
                                                path, name, root / "evidence"))
            fake_page = types.SimpleNamespace()
            fake_module = types.ModuleType("playwright.sync_api")
            fake_module.sync_playwright = FakePlaywrightContext
            def choose_files(_page, _options, _paths, started):
                started()
            with patch.dict("sys.modules", {"playwright": types.ModuleType("playwright"),
                                            "playwright.sync_api": fake_module}), \
                 patch.object(qc, "read_verified_port", return_value=51040), \
                 patch.object(qc, "_select_page", return_value=fake_page), \
                 patch.object(qc, "_check_page_gate"), \
                 patch.object(qc, "_open_material_area", return_value=fake_page), \
                 patch.object(qc, "_search_material_id", side_effect=[None, None,
                                                                         "7700000000000000001",
                                                                         "7700000000000000001"]), \
                 patch.object(qc, "_delay"), \
                 patch.object(qc, "_choose_files", side_effect=choose_files), \
                 patch.object(qc, "_wait_batch_ready"), \
                 patch.object(qc, "_submit_control", return_value=types.SimpleNamespace(click=lambda **_k: None)), \
                 patch.object(qc.time, "monotonic", side_effect=[0, 1, 121]), \
                 patch.object(qc.time, "sleep"), \
                 patch.object(qc, "_save_evidence", side_effect=lambda page, option, result: result):
                result = qc.upload_material_batch(options)
            self.assertEqual([item["status"] for item in result["items"]],
                             ["needs_review", "needs_review"])
            self.assertEqual([item["remoteMaterialId"] for item in result["items"]], [None, None])
            self.assertEqual([item["submitAttempted"] for item in result["items"]], [True, True])

    def test_batch_failure_before_file_selection_is_safe_for_single_fallback(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            options = []
            for name in ("one.mp4", "two.mp4"):
                path = root / name
                path.write_bytes(b"isolated video")
                options.append(qc.UploadOptions(root / "qianchuan-1", 51040, 1,
                                                path, name, root / "evidence"))
            fake_page = types.SimpleNamespace()
            fake_module = types.ModuleType("playwright.sync_api")
            fake_module.sync_playwright = FakePlaywrightContext
            with patch.dict("sys.modules", {"playwright": types.ModuleType("playwright"),
                                            "playwright.sync_api": fake_module}), \
                 patch.object(qc, "read_verified_port", return_value=51040), \
                 patch.object(qc, "_select_page", return_value=fake_page), \
                 patch.object(qc, "_check_page_gate"), \
                 patch.object(qc, "_open_material_area", return_value=fake_page), \
                 patch.object(qc, "_search_material_id", return_value=None), \
                 patch.object(qc, "_delay"), \
                 patch.object(qc, "_choose_files", side_effect=qc.UploadStopped(
                     "上传窗口未打开", stage="file_input")), \
                 patch.object(qc, "_save_evidence", side_effect=lambda page, option, result: result):
                result = qc.upload_material_batch(options)
            self.assertEqual(result["status"], "failed")
            self.assertEqual([item["status"] for item in result["items"]], ["failed", "failed"])
            self.assertEqual([item["submitAttempted"] for item in result["items"]], [False, False])

    def test_batch_reuses_all_existing_materials_without_selecting_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            options = []
            for name in ("one.mp4", "two.mp4"):
                path = root / name
                path.write_bytes(b"isolated video")
                options.append(qc.UploadOptions(root / "qianchuan-1", 51040, 1,
                                                path, name, root / "evidence"))
            fake_page = types.SimpleNamespace()
            fake_module = types.ModuleType("playwright.sync_api")
            fake_module.sync_playwright = FakePlaywrightContext
            with patch.dict("sys.modules", {"playwright": types.ModuleType("playwright"),
                                            "playwright.sync_api": fake_module}), \
                 patch.object(qc, "read_verified_port", return_value=51040), \
                 patch.object(qc, "_select_page", return_value=fake_page), \
                 patch.object(qc, "_check_page_gate"), \
                 patch.object(qc, "_open_material_area", return_value=fake_page), \
                 patch.object(qc, "_search_material_id", side_effect=["7700000000000000001", "7700000000000000002"]), \
                 patch.object(qc, "_delay"), \
                 patch.object(qc, "_choose_files") as choose_files, \
                 patch.object(qc, "_save_evidence", side_effect=lambda page, option, result: result):
                result = qc.upload_material_batch(options)
            self.assertEqual([item["status"] for item in result["items"]],
                             ["succeeded", "succeeded"])
            choose_files.assert_not_called()

    def test_failure_with_page_saves_screenshot_and_diagnostic(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            options = qc.UploadOptions(root / "qianchuan-1", 51040, 1,
                                       root / "video.mp4", "视频素材", root / "evidence")

            class Page:
                url = "https://qianchuan.jinritemai.com/dashboard"

                def title(self):
                    return "巨量千川"

                def evaluate(self, expression):
                    return "complete" if expression == "document.readyState" else {"buttons": [], "fileInputs": []}

                def screenshot(self, *, path, **_kwargs):
                    Path(path).write_bytes(b"screenshot")

            result = qc._save_evidence(Page(), options, qc._result("failed", "未找到入口", "navigate", False))
            self.assertTrue(Path(result["screenshotPath"]).is_file())
            self.assertTrue(Path(result["diagnosticPath"]).is_file())


if __name__ == "__main__":
    unittest.main()
