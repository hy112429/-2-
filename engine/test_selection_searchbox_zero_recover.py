# -*- coding: utf-8 -*-
"""P-24/P-25：搜索框有限恢复、当前账号软失败和预留结算边界。"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import browser_cdp
from browser_cdp import (
    BAIYING_SELECTION_LIBRARY_URL,
    SEARCH_KEYWORD_INPUT_SELECTOR,
    SearchboxUnavailableError,
    _cart_control_failure_result,
    _search_keyword_input,
)
from selection_registry_local import begin, connect, finish, reserve


class FakeInput:
    def __init__(self, page):
        self.page = page

    def is_visible(self, timeout=None):
        return self.page.input_visible


class FakeInputLocator:
    def __init__(self, page):
        self.page = page
        self.first = FakeInput(page)

    def count(self):
        return self.page.input_count

    def nth(self, index):
        return FakeInput(self.page)


class FakePage:
    def __init__(self, show_after_goto=False):
        self.input_count = 0
        self.input_visible = False
        self.show_after_goto = show_after_goto
        self.url = "https://buyin.jinritemai.com/dashboard/merch-picking-library?session=redacted"
        self.goto_calls = []
        self.ready_state = "complete"

    def locator(self, selector):
        assert selector == SEARCH_KEYWORD_INPUT_SELECTOR
        return FakeInputLocator(self)

    def goto(self, url, **kwargs):
        self.goto_calls.append((url, kwargs))
        self.url = url
        if self.show_after_goto:
            self.input_count = 1
            self.input_visible = True

    def evaluate(self, script, *args, **kwargs):
        if "document.readyState" in script:
            return self.ready_state
        return None

    def title(self):
        return "选品库"

    def screenshot(self, path, **kwargs):
        Path(path).write_bytes(b"fake screenshot")

    def wait_for_timeout(self, timeout):
        return None


class SelectionSearchboxRecoveryTests(unittest.TestCase):
    def test_zero_match_recovers_after_returning_to_selection_page(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict("os.environ", {"TOUGUANG_DATA_DIR": temp_dir}):
            page = FakePage(show_after_goto=True)
            field = _search_keyword_input(page, ready_timeout_ms=0)

            self.assertIsInstance(field, FakeInput)
            self.assertEqual(len(page.goto_calls), 1)
            self.assertEqual(page.goto_calls[0][0], BAIYING_SELECTION_LIBRARY_URL)

    def test_zero_match_exhaustion_has_diagnostics_and_is_soft_failure_class(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict("os.environ", {"TOUGUANG_DATA_DIR": temp_dir}):
            page = FakePage(show_after_goto=False)
            with self.assertRaises(SearchboxUnavailableError) as caught:
                _search_keyword_input(page, recovery_retries=2, ready_timeout_ms=0)

            message = str(caught.exception)
            self.assertEqual(caught.exception.classification, "SEARCHBOX_ZERO_EXHAUSTED")
            for field in ("URL=", "title=", "readyState=", "selectorHitCount=0", "screenshotPath="):
                self.assertIn(field, message)
            self.assertEqual(len(page.goto_calls), 2)
            diagnostics = list((Path(temp_dir) / "diagnostics" / "selection").glob("searchbox-*.json"))
            screenshots = list((Path(temp_dir) / "diagnostics" / "selection").glob("searchbox-*.png"))
            self.assertEqual(len(diagnostics), 3)
            self.assertEqual(len(screenshots), 3)
            payload = json.loads(diagnostics[-1].read_text(encoding="utf-8"))
            self.assertEqual(payload["selectorHitCount"], 0)
            self.assertEqual(payload["title"], "选品库")
            self.assertEqual(payload["readyState"], "complete")

    def test_rate_limit_during_recovery_is_not_searchbox_soft_failure(self):
        class RateLimitCollector:
            def rate_limit_error_since(self, mark):
                return (1, "11001", "业务限流")

        with tempfile.TemporaryDirectory() as temp_dir, patch.dict("os.environ", {"TOUGUANG_DATA_DIR": temp_dir}):
            with patch.object(browser_cdp, "_mark_page_rate_limited"):
                with self.assertRaisesRegex(RuntimeError, "BAIYING_RATE_LIMIT") as caught:
                    _search_keyword_input(FakePage(), collector=RateLimitCollector(), collector_mark=0)
            self.assertNotIsInstance(caught.exception, SearchboxUnavailableError)


class SelectionRegistrySettlementTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.connection = connect(Path(self.temp_dir.name) / "registry.sqlite")

    def tearDown(self):
        self.connection.close()
        self.temp_dir.cleanup()

    def _reserve(self, product_id, request_id):
        result = reserve(self.connection, "local:1", request_id, [{
            "productId": product_id,
            "leafCategoryId": f"category-{product_id}",
            "title": "test product",
        }])
        self.assertEqual(len(result["reserved"]), 1)
        return result["reserved"][0]["token"]

    def test_pre_add_control_failure_not_added_releases_and_allows_retry(self):
        token = self._reserve("control-fail-product", "request-1")
        begin(self.connection, token)

        result = finish(self.connection, token, "NOT_ADDED")

        self.assertEqual(result["state"], "RELEASED")
        retry = reserve(self.connection, "local:1", "request-2", [{
            "productId": "control-fail-product",
            "leafCategoryId": "category-control-fail-product",
            "title": "test product",
        }])
        self.assertEqual(len(retry["reserved"]), 1)
        self.assertEqual(retry["reserved"][0]["state"], "RESERVED")

    def test_true_business_uncertainty_stays_locked(self):
        token = self._reserve("uncertain-product", "request-3")
        begin(self.connection, token)

        result = finish(self.connection, token, "UNKNOWN")

        self.assertEqual(result["state"], "UNCERTAIN")
        retry = reserve(self.connection, "local:1", "request-4", [{
            "productId": "uncertain-product",
            "leafCategoryId": "category-uncertain-product",
            "title": "test product",
        }])
        self.assertEqual(retry["reserved"], [])
        self.assertEqual(retry["rejected"][0]["productId"], "uncertain-product")

    def test_control_failure_result_keeps_observable_diagnostic_fields(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict("os.environ", {"TOUGUANG_DATA_DIR": temp_dir}):
            result = _cart_control_failure_result(
                FakePage(),
                {"productId": "control-fail-product", "title": "test product"},
                "加车按钮命中 0 个",
                {"button.add-cart": 0},
            )

            self.assertEqual(result["cartFailureClass"], "CONTROL_FAILURE")
            self.assertIs(result["cartAttempted"], False)
            self.assertIn("selectorHitCounts=", result["status"])
            self.assertIn("screenshotPath=", result["status"])
            diagnostic = json.loads(Path(result["controlDiagnosticPath"]).read_text(encoding="utf-8"))
            self.assertEqual(diagnostic["readyState"], "complete")
            self.assertEqual(diagnostic["selectorHitCounts"]["button.add-cart"], 0)
            self.assertTrue(Path(diagnostic["screenshotPath"]).exists())


class SoftFailureSourceContractTests(unittest.TestCase):
    def test_searchbox_exhaustion_is_classified_and_skips_only_the_account(self):
        root = Path(browser_cdp.__file__).resolve().parent.parent
        ux_source = (root / "desktop/src/renderer/src/utils/selection_error_ux.ts").read_text(encoding="utf-8")
        vue_source = (root / "desktop/src/renderer/src/components/SelectionSettingsView.vue").read_text(encoding="utf-8")
        self.assertIn("SEARCHBOX_ZERO_EXHAUSTED", ux_source)
        self.assertIn("SEARCHBOX_ZERO_EXHAUSTED", vue_source)
        self.assertIn("accountSwitchFailures.set(account.id, createAccountSwitchFailure(account, cause, classification))", vue_source)
        self.assertIn("continue\n      }", vue_source)

    def test_control_failure_is_released_and_logged_separately_from_unknown(self):
        root = Path(browser_cdp.__file__).resolve().parent.parent
        main_source = (root / "desktop/src/main/login.ts").read_text(encoding="utf-8")
        vue_source = (root / "desktop/src/renderer/src/components/SelectionSettingsView.vue").read_text(encoding="utf-8")
        self.assertIn("item?.cartFailureClass === 'CONTROL_FAILURE' && item.cartAttempted !== true", main_source)
        self.assertIn("reason: 'control failure'", main_source)
        self.assertIn("REGISTRY_RELEASE_CONTROL_FAILURE", vue_source)
        self.assertIn("finishRegistryReservation(token, 'UNKNOWN')", main_source)


if __name__ == "__main__":
    unittest.main()
