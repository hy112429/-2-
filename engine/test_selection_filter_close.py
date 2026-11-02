# -*- coding: utf-8 -*-
"""选品搜索词清理与页面失败分类回归。"""

import sys
from pathlib import Path
from unittest import TestCase

sys.path.insert(0, str(Path(__file__).resolve().parent))
import browser_cdp


class FakeInput:
    def __init__(self, value):
        self.value = value
        self.presses = []

    def input_value(self, **_kwargs):
        return self.value

    def press(self, key, **_kwargs):
        self.presses.append(key)
        if key == "Backspace" and self.presses[-2:] == ["Control+A", "Backspace"]:
            self.value = ""


class FakeInputs:
    def __init__(self, first):
        self.first = first

    def count(self):
        return 1


class FakeClear:
    def __init__(self, field, js_clears=True):
        self.field = field
        self.js_clears = js_clears
        self.click_args = []

    def click(self, **kwargs):
        self.click_args.append(kwargs)
        raise TimeoutError(
            "Locator.click: Timeout 3000ms exceeded while clicking "
            ".searchInput__input [aria-label='close-circle']"
        )

    def evaluate(self, _expression, **_kwargs):
        if not self.js_clears:
            raise RuntimeError("mocked DOM click did not clear")
        self.field.value = ""
        return True


class FakeClears:
    def __init__(self, first):
        self.first = first

    def count(self):
        return 1


class FakePage:
    def __init__(self, value, js_clears=True):
        self.field = FakeInput(value)
        self.clear = FakeClear(self.field, js_clears=js_clears)

    def locator(self, selector):
        if selector == browser_cdp.SEARCH_KEYWORD_INPUT_SELECTOR:
            return FakeInputs(self.field)
        if selector == browser_cdp.SEARCH_KEYWORD_CLEAR_SELECTOR:
            return FakeClears(self.clear)
        raise AssertionError(f"unexpected selector: {selector}")

    def wait_for_timeout(self, _milliseconds):
        return None


class SelectionFilterCloseTests(TestCase):
    def test_clear_control_timeout_falls_back_to_visible_control_and_verifies_value(self):
        page = FakePage("旧关键词")

        changed = browser_cdp._apply_search_keyword(page, {})

        self.assertTrue(changed)
        self.assertEqual(page.field.value, "")
        self.assertEqual(page.clear.click_args[0]["force"], True)
        self.assertEqual(page.clear.click_args[0]["no_wait_after"], True)
        self.assertLess(page.clear.click_args[0]["timeout"], 3_000)
        self.assertEqual(page.field.presses, [])

    def test_keyboard_fallback_is_used_when_dom_click_does_not_clear(self):
        page = FakePage("旧关键词", js_clears=False)

        changed = browser_cdp._apply_search_keyword(page, {})

        self.assertTrue(changed)
        self.assertEqual(page.field.value, "")
        self.assertEqual(page.field.presses, ["Control+A", "Backspace"])

    def test_buyin_selection_click_timeout_is_not_misclassified_as_login_unknown(self):
        classification = browser_cdp._classify_selection_failure(
            "https://buyin.jinritemai.com/dashboard/merch-picking-library",
            {"state": "unknown", "message": "页面当前位于空白页"},
            {"title": "选品库", "controls": [{"text": "个护家清"}]},
            [{"completion": "pending"}],
            set(),
            set(),
            TimeoutError(
                "Locator.click: Timeout 3000ms exceeded for "
                ".searchInput__input [aria-label='close-circle']"
            ),
        )

        self.assertEqual(classification, "CATEGORY_FILTER_APPLY_FAILED")

    def test_login_unknown_remains_for_unconfirmed_non_selection_page(self):
        classification = browser_cdp._classify_selection_failure(
            "https://buyin.jinritemai.com/dashboard/other-page",
            {"state": "unknown"},
            {"title": ""},
            [],
            set(),
            set(),
            RuntimeError("其他页面不可用"),
        )

        self.assertEqual(classification, "LOGIN_OR_PAGE_STATE_UNCONFIRMED")


if __name__ == "__main__":
    import unittest
    unittest.main()
