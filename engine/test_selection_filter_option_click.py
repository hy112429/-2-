# -*- coding: utf-8 -*-
"""百应筛选菜单和选项点击回退及状态确认回归。"""

import sys
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import browser_cdp


class FakeOption:
    def __init__(
        self,
        label="自动投千川",
        *,
        dom_click_works=True,
        click_changes_state=False,
        selected_then_timeout=False,
    ):
        self.label = label
        self.dom_click_works = dom_click_works
        self.click_changes_state = click_changes_state
        self.selected_then_timeout = selected_then_timeout
        self.selected = False
        self.visible = True
        self.dom_click_count = 0
        self.click_args = []

    def get_attribute(self, name, **_kwargs):
        if name == "class":
            suffix = " option-selected" if self.selected else ""
            return "aurora-select-item-option" + suffix
        return None

    def inner_text(self, **_kwargs):
        return self.label

    def click(self, **kwargs):
        self.click_args.append(kwargs)
        if self.selected_then_timeout:
            self.selected = True
            raise TimeoutError(
                "Locator.click: Timeout 1500ms exceeded waiting for "
                "locator('.aurora-select-item-option')"
            )
        if self.click_changes_state:
            self.selected = True
            return
        raise TimeoutError(
            "Locator.click: Timeout 1500ms exceeded waiting for "
            "locator('.aurora-select-item-option')"
        )

    def evaluate(self, expression, arg=None, **_kwargs):
        if "MutationObserver" in expression:
            return self.selected
        self.dom_click_count += 1
        if not self.visible:
            return False
        if not self.dom_click_works:
            raise RuntimeError("mock DOM click did not dispatch")
        self.selected = True
        return True


class FakeFilterControl:
    def __init__(self, page, *, opened_then_timeout=False):
        self.page = page
        self.opened_then_timeout = opened_then_timeout
        self.click_args = []
        self.dom_click_count = 0

    def click(self, **kwargs):
        self.click_args.append(kwargs)
        if self.opened_then_timeout:
            self.page.dropdown_open = True
        raise TimeoutError("Locator.click: Timeout 3000ms exceeded for .combine-select-content")

    def evaluate(self, _expression, **_kwargs):
        self.dom_click_count += 1
        self.page.dropdown_open = True
        return True


class FakePage:
    def __init__(self):
        self.dropdown_open = False

    def evaluate(self, _expression):
        return self.dropdown_open

    def wait_for_timeout(self, milliseconds):
        self.waits = getattr(self, "waits", []) + [milliseconds]

    def wait_for_function(self, expression, arg=None, **_kwargs):
        self.last_wait_function = expression
        self.last_wait_arg = arg
        return True


class SelectionFilterOptionClickTests(TestCase):
    def test_click_timeout_uses_dom_fallback_and_confirms_selected_class(self):
        option = FakeOption()

        browser_cdp._click_filter_option(option)

        self.assertEqual(option.click_args[0]["force"], True)
        self.assertEqual(option.click_args[0]["no_wait_after"], True)
        self.assertEqual(option.click_args[0]["timeout"], 1_500)
        self.assertEqual(option.dom_click_count, 1)
        self.assertTrue(option.selected)

    def test_dom_fallback_failure_raises_with_original_timeout_and_no_success(self):
        option = FakeOption(dom_click_works=False)

        with self.assertRaisesRegex(RuntimeError, "未假报成功") as raised:
            browser_cdp._click_filter_option(option)

        self.assertIn("Timeout 1500ms", str(raised.exception))
        self.assertFalse(option.selected)

    def test_timeout_after_selection_does_not_dispatch_a_second_toggle(self):
        option = FakeOption(selected_then_timeout=True)

        browser_cdp._click_filter_option(option)

        self.assertTrue(option.selected)
        self.assertEqual(option.dom_click_count, 0)

    def test_click_return_without_selected_state_is_not_treated_as_success(self):
        option = FakeOption(click_changes_state=False)
        option.click = lambda **_kwargs: None

        with self.assertRaisesRegex(RuntimeError, "未确认筛选生效"):
            browser_cdp._click_filter_option(option)

        self.assertEqual(option.dom_click_count, 0)
        self.assertFalse(option.selected)

    def test_single_select_can_confirm_from_active_filter_chip_after_menu_closes(self):
        option = FakeOption(click_changes_state=False)
        option.click = lambda **_kwargs: None

        browser_cdp._click_filter_option(
            option,
            confirmation_check=lambda: True,
        )

        self.assertEqual(option.dom_click_count, 0)
        self.assertFalse(option.selected)

    def test_filter_applied_chip_reader_scans_visible_summary(self):
        page = FakePage()

        self.assertTrue(browser_cdp._wait_filter_applied_chip(page, "带货达人数量", "≥500"))
        self.assertIn("已筛选", page.last_wait_function)
        self.assertEqual(page.last_wait_arg, {"label": "带货达人数量", "value": "≥500"})

    def test_filter_option_timeout_retries_with_fresh_state_then_continues(self):
        page = FakePage()
        warnings = []
        with patch.object(
            browser_cdp,
            "_apply_configured_filter_field_once",
            side_effect=[RuntimeError("option-selected timeout"), None],
        ) as apply_once:
            browser_cdp._apply_configured_filter_field(
                page, "带货达人数量", ["≥500"], None, warnings, max_attempts=3
            )

        self.assertEqual(apply_once.call_count, 2)
        self.assertEqual(page.waits, [250])

    def test_filter_option_timeout_exhaustion_is_readable_and_bounded(self):
        page = FakePage()
        with patch.object(
            browser_cdp,
            "_apply_configured_filter_field_once",
            side_effect=RuntimeError("option-selected timeout"),
        ) as apply_once:
            with self.assertRaisesRegex(RuntimeError, "FILTER_APPLY_RETRIES_EXHAUSTED.*自动重试 3 次"):
                browser_cdp._apply_configured_filter_field(
                    page, "带货达人数量", ["≥500"], None, [], max_attempts=3
                )

        self.assertEqual(apply_once.call_count, 3)
        self.assertEqual(page.waits, [250, 500])

    def test_filter_retry_does_not_repeat_rate_limit_or_manual_verification(self):
        for message in ("material_list 11001 rate limit", "百应要求手动完成验证码"):
            page = FakePage()
            with patch.object(
                browser_cdp,
                "_apply_configured_filter_field_once",
                side_effect=RuntimeError(message),
            ) as apply_once:
                with self.assertRaisesRegex(RuntimeError, message):
                    browser_cdp._apply_configured_filter_field(
                        page, "流量助推", ["自动投千川"], None, [], max_attempts=3
                    )
            self.assertEqual(apply_once.call_count, 1)
            self.assertEqual(getattr(page, "waits", []), [])

    def test_pending_material_list_waits_in_bounded_windows_without_restarting_request(self):
        page = FakePage()
        collector = object()
        with patch.object(
            browser_cdp,
            "_wait_for_material_list_settled",
            side_effect=["pending", "pending", "settled"],
        ) as wait_once:
            status = browser_cdp._wait_for_material_list_settled_bounded(
                page, collector, 7, window_seconds=8, max_windows=3
            )

        self.assertEqual(status, "settled")
        self.assertEqual(wait_once.call_count, 3)
        self.assertTrue(all(call.args[2] == 7 for call in wait_once.call_args_list))

    def test_pending_material_list_exhaustion_returns_explicit_pending_state(self):
        with patch.object(
            browser_cdp,
            "_wait_for_material_list_settled",
            return_value="pending",
        ) as wait_once:
            status = browser_cdp._wait_for_material_list_settled_bounded(
                FakePage(), object(), 0, window_seconds=8, max_windows=3
            )

        self.assertEqual(status, "pending")
        self.assertEqual(wait_once.call_count, 3)

    def test_filter_retry_waits_for_inflight_material_request_and_stops_if_still_pending(self):
        page = FakePage()

        class PendingCollector:
            def rate_limit_error_since(self, _mark):
                return None

            def pending_since(self, _mark):
                return True

        page._touguang_filter_retry_collector = (PendingCollector(), 4)
        with patch.object(
            browser_cdp,
            "_apply_configured_filter_field_once",
            side_effect=RuntimeError("option-selected timeout"),
        ) as apply_once, patch.object(
            browser_cdp,
            "_wait_for_material_list_settled_bounded",
            return_value="pending",
        ) as wait_pending:
            with self.assertRaisesRegex(RuntimeError, "MATERIAL_LIST_REQUEST_PENDING.*避免重复请求"):
                browser_cdp._apply_configured_filter_field(
                    page, "带货达人数量", ["≥500"], None, [], max_attempts=3
                )

        self.assertEqual(apply_once.call_count, 1)
        wait_pending.assert_called_once()
        self.assertEqual(getattr(page, "waits", []), [])

    def test_filter_control_timeout_uses_visible_dom_fallback_with_short_force_click(self):
        page = FakePage()
        control = FakeFilterControl(page)

        browser_cdp._click_filter_control(page, control, "自动投千川", expected_open=True)

        self.assertEqual(control.click_args[0]["force"], True)
        self.assertEqual(control.click_args[0]["no_wait_after"], True)
        self.assertEqual(control.click_args[0]["timeout"], 1_500)
        self.assertEqual(control.dom_click_count, 1)
        self.assertTrue(page.dropdown_open)

    def test_control_timeout_after_open_does_not_toggle_menu_closed_again(self):
        page = FakePage()
        control = FakeFilterControl(page, opened_then_timeout=True)

        browser_cdp._click_filter_control(page, control, "自动投千川", expected_open=True)

        self.assertTrue(page.dropdown_open)
        self.assertEqual(control.dom_click_count, 0)

    def test_auto_qianchuan_label_and_option_selector_timeouts_are_classified(self):
        url = "https://buyin.jinritemai.com/dashboard/merch-picking-library"
        login_state = {"state": "unknown"}
        dom_snapshot = {"title": "选品库", "controls": []}
        errors = (
            TimeoutError("Locator.click: Timeout 1500ms exceeded；筛选选项「自动投千川」"),
            TimeoutError("Locator.click: Timeout 3000ms exceeded on .aurora-select-item-option"),
            RuntimeError("百应筛选选项「商品状态」Locator.click 等待 option-selected 状态 timeout"),
        )

        for error in errors:
            with self.subTest(error=str(error)):
                classification = browser_cdp._classify_selection_failure(
                    url, login_state, dom_snapshot, [], set(), set(), error
                )
                self.assertEqual(classification, "CATEGORY_FILTER_APPLY_FAILED")


if __name__ == "__main__":
    import unittest
    unittest.main()
