# -*- coding: utf-8 -*-
"""投稿页购物车标签识别的隔离回归用例，不访问抖音平台。"""

import unittest
from pathlib import Path
from unittest.mock import patch

import browser_cdp
from playwright.sync_api import sync_playwright


class _Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def advance(self, milliseconds):
        self.now += milliseconds / 1000 + 0.01


class _Matches:
    def __init__(self, items):
        self.items = items

    def count(self):
        return len(self.items)

    def nth(self, index):
        return self.items[index]

    @property
    def first(self):
        return self.items[0]


class _Element:
    def __init__(self, page, kind, metadata=None):
        self.page = page
        self.kind = kind
        self.metadata = metadata or {}

    def is_visible(self):
        return True

    def evaluate(self, _script):
        return self.metadata

    def click(self, **_kwargs):
        self.page.clicks.append(self.kind)
        if self.kind == "location":
            self.page.menu_open = True
        elif self.kind == "cart_option":
            self.page.selected_tag = "购物车"
            self.page.menu_open = False


class _CreatorPage:
    def __init__(self, selected_tag="位置", cart_available=True, inputs=()):
        self.selected_tag = selected_tag
        self.cart_available = cart_available
        self.menu_open = False
        self.clicks = []
        self.clock = _Clock()
        self.inputs = [_Element(self, "link_input", metadata) for metadata in inputs]

    def locator(self, selector):
        assert selector.startswith("input:not([type='file'])")
        return _Matches(self.inputs if self.selected_tag == "购物车" else [])

    def get_by_text(self, text, exact=False):
        assert exact is True
        if text == "位置" and self.selected_tag == "位置":
            return _Matches([_Element(self, "location")])
        if text == "购物车" and self.menu_open and self.cart_available:
            return _Matches([_Element(self, "cart_option")])
        if text == "购物车" and self.selected_tag == "购物车":
            return _Matches([_Element(self, "cart_selected")])
        return _Matches([])

    def get_by_role(self, role, name=None, exact=False):
        assert role == "option" and name == "购物车" and exact is True
        return _Matches([_Element(self, "cart_option")] if self.menu_open and self.cart_available else [])

    def wait_for_timeout(self, milliseconds):
        self.clock.advance(milliseconds)


def _input_metadata(value="", *, context="购物车 添加链接", attrs="", same_row_add_link=True):
    return {
        "value": value,
        "context": context,
        "attrs": attrs,
        "sameRowAddLink": same_row_add_link,
    }


class CreatorCartTagTests(unittest.TestCase):
    def _select(self, page):
        with patch.object(browser_cdp.time, "monotonic", page.clock.monotonic):
            return browser_cdp._select_creator_cart_tag(page)

    def test_already_selected_cart_accepts_empty_link_field(self):
        page = _CreatorPage(selected_tag="购物车", inputs=[_input_metadata()])

        field = self._select(page)

        self.assertIs(field, page.inputs[0])
        self.assertEqual(page.clicks, [])

    def test_already_selected_cart_accepts_existing_link_field(self):
        page = _CreatorPage(
            selected_tag="购物车",
            inputs=[_input_metadata("https://haohuo.jinritemai.com/views/product/item2?id=123")],
        )

        field = self._select(page)

        self.assertIs(field, page.inputs[0])
        self.assertEqual(page.clicks, [])

    def test_location_dropdown_can_select_cart(self):
        page = _CreatorPage(
            selected_tag="位置",
            inputs=[_input_metadata(attrs="商品链接")],
        )

        field = self._select(page)

        self.assertIs(field, page.inputs[0])
        self.assertEqual(page.clicks, ["location", "cart_option"])

    def test_no_cart_option_stops_without_link_or_publish_action(self):
        page = _CreatorPage(selected_tag="位置", cart_available=False)

        with self.assertRaisesRegex(RuntimeError, "购物车"):
            self._select(page)

        self.assertEqual(page.clicks, ["location"])

    def test_two_equally_plausible_empty_link_fields_stay_ambiguous(self):
        page = _CreatorPage(
            selected_tag="购物车",
            inputs=[_input_metadata(), _input_metadata()],
        )

        with self.assertRaises(RuntimeError):
            self._select(page)

        self.assertEqual(page.clicks, [])

    def test_outer_ancestor_text_without_same_row_action_is_not_a_link_field(self):
        unrelated = _input_metadata(context="购物车 添加链接", same_row_add_link=False)
        actual = _input_metadata(context="购物车 添加链接", same_row_add_link=True)
        page = _CreatorPage(selected_tag="购物车", inputs=[unrelated, actual])

        field = self._select(page)

        self.assertIs(field, page.inputs[1])
        self.assertEqual(page.clicks, [])


class CreatorAddLinkControlDomTests(unittest.TestCase):
    """复现平台当前 DOM：添加链接是与输入框同父的 span，不是 button。"""

    @classmethod
    def setUpClass(cls):
        chrome = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
        if not chrome.is_file():
            raise unittest.SkipTest("本机没有可用于隔离 DOM 测试的 Chrome")
        cls.playwright = sync_playwright().start()
        try:
            cls.browser = cls.playwright.chromium.launch(executable_path=str(chrome), headless=True)
        except Exception:
            cls.playwright.stop()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.page = self.browser.new_page()

    def tearDown(self):
        self.page.close()

    def _load(self, extra="", same_row_actions=1):
        actions = "".join(
            '<span class="cart-mybtn-jPFx5X" '
            f'onclick="window.clicked.push(\'same-{index}\')">添加链接</span>'
            for index in range(same_row_actions)
        )
        self.page.set_content(
            "<script>window.clicked=[]</script>"
            '<div class="cart-part-_ykMSd"><div class="input-gOtpFB">'
            '<input placeholder="粘贴商品链接" class="input-inner-JYpXOO" '
            'value="https://haohuo.jinritemai.com/views/product/item2?id=123">'
            + actions + "</div></div>" + extra
        )
        return self.page.locator("input[placeholder='粘贴商品链接']")

    def test_real_span_dom_has_no_button_role_but_control_is_found(self):
        field = self._load()
        self.assertEqual(self.page.get_by_role("button", name="添加链接", exact=True).count(), 0)
        self.assertEqual(self.page.get_by_text("添加链接", exact=True).count(), 1)

        control = browser_cdp._creator_add_link_control(self.page, field)
        control.click()

        self.assertEqual(self.page.evaluate("window.clicked"), ["same-0"])

    def test_unrelated_same_text_elsewhere_does_not_redirect_click(self):
        field = self._load('<span onclick="window.clicked.push(\'outside\')">添加链接</span>')
        self.assertEqual(self.page.get_by_text("添加链接", exact=True).count(), 2)

        control = browser_cdp._creator_add_link_control(self.page, field)
        control.click()

        self.assertEqual(self.page.evaluate("window.clicked"), ["same-0"])

    def test_ambiguous_actions_in_same_link_row_stop_safely(self):
        field = self._load(same_row_actions=2)

        with self.assertRaises(RuntimeError):
            browser_cdp._creator_add_link_control(self.page, field)

        self.assertEqual(self.page.evaluate("window.clicked"), [])


if __name__ == "__main__":
    unittest.main()
