# -*- coding: utf-8 -*-
"""购物车添品后商品短标题弹窗的隔离回归；不访问平台。"""

import unittest
from html import escape
from pathlib import Path

import browser_cdp
from playwright.sync_api import sync_playwright


PRODUCT_NAME = "婴儿车睡袋推车脚套新生儿豆豆绒加厚保暖"
PRODUCT_ID = "3771360143091040606"
PRODUCT_URL = f"https://haohuo.jinritemai.com/views/product/item2?id={PRODUCT_ID}"


class CreatorProductEditDomTests(unittest.TestCase):
    """平台会在点击“添加链接”后先要求填写短标题，再显示关联商品。"""

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

    def _load(self, *, card_present, add_link_delay_ms=0, done_stays_disabled=False):
        initial_card = (
            '<div id="product-card"><span class="product-name">__NAME__</span>'
            '<span class="product-short">短标题：旧标题 每单赚17</span>'
            '<a href="__URL__">商品链接</a><button id="card-edit">编辑</button>'
            '<button>移除</button></div>'
            if card_present else ""
        )
        html = """
        <div id="cart-area">
          <span>购物车</span>
          <div class="input-gOtpFB">
            <input id="product-link" placeholder="粘贴商品链接" value="__URL__">
            <span id="add-link" class="cart-mybtn-jPFx5X" role="presentation">添加链接</span>
          </div>
          <div id="product-slot">__CARD__</div>
        </div>
        <div class="semi-modal" id="edit-modal" hidden>
          <h2>编辑商品</h2><p>商品原标题 __NAME__</p>
          <label>商品短标题* <input id="short-title" placeholder="请输入商品短标题" maxlength="10"></label>
          <button id="done" class="disabled-ixFjuz">完成编辑</button>
        </div>
        <script>
          window.events = [];
          const modal = document.querySelector('#edit-modal');
          const shortField = document.querySelector('#short-title');
          const done = document.querySelector('#done');
          function openEditor(source) {
            window.events.push(source);
            shortField.value = '';
            done.classList.add('disabled-ixFjuz');
            modal.hidden = false;
          }
          function wireCardEdit() {
            const button = document.querySelector('#card-edit');
            if (button) button.onclick = () => openEditor('edit-card');
          }
          document.querySelector('#add-link').onclick = () => {
            if (__DELAY_MS__ > 0) {
              window.events.push('add-link-request');
              setTimeout(() => {
                if (modal.hidden) openEditor('add-link');
              }, __DELAY_MS__);
            } else {
              openEditor('add-link');
            }
          };
          shortField.oninput = () => {
            window.events.push('fill-short-title');
            if (!__STAYS_DISABLED__) {
              done.classList.toggle('disabled-ixFjuz', !shortField.value.trim());
            }
          };
          done.onclick = () => {
            if (done.classList.contains('disabled-ixFjuz')) return;
            window.events.push('complete-edit');
            const slot = document.querySelector('#product-slot');
            if (!document.querySelector('#product-card')) {
              slot.innerHTML = '<div id="product-card"><span class="product-name">__NAME__</span>'
                + '<span class="product-short"></span><a href="__URL__">商品链接</a>'
                + '<button id="card-edit">编辑</button><button>移除</button></div>';
              wireCardEdit();
            }
            document.querySelector('.product-short').textContent =
              '短标题：' + shortField.value + ' 每单赚17';
            modal.hidden = true;
          };
          wireCardEdit();
        </script>
        """.replace("__CARD__", initial_card).replace("__NAME__", escape(PRODUCT_NAME)).replace("__URL__", escape(PRODUCT_URL)).replace("__DELAY_MS__", str(add_link_delay_ms)).replace("__STAYS_DISABLED__", "true" if done_stays_disabled else "false")
        self.page.set_content(html)
        return self.page.locator("#product-link")

    def test_add_link_opens_required_short_title_modal_before_product_is_confirmed(self):
        link_field = self._load(card_present=False)
        self.assertFalse(browser_cdp._creator_cart_context_matches_product(
            browser_cdp._creator_cart_link_context(link_field), PRODUCT_ID, PRODUCT_NAME
        ))

        browser_cdp._creator_add_link_control(self.page, link_field).click()
        self.assertTrue(self.page.locator("#edit-modal").is_visible())
        self.assertTrue(self.page.locator("#done").evaluate("el => el.classList.contains('disabled-ixFjuz')"))
        self.assertEqual(self.page.locator("#product-card").count(), 0)

        actual_short_title = browser_cdp._fill_creator_product_short_title(self.page, PRODUCT_NAME)

        self.assertEqual(actual_short_title, PRODUCT_NAME[:10])
        self.assertFalse(self.page.locator("#edit-modal").is_visible())
        self.assertEqual(self.page.evaluate("window.events"), [
            "add-link", "fill-short-title", "complete-edit"
        ])
        self.assertTrue(browser_cdp._creator_cart_context_matches_product(
            browser_cdp._creator_cart_link_context(link_field), PRODUCT_ID, PRODUCT_NAME
        ))

    def test_existing_product_card_opens_edit_modal_then_confirms_short_title(self):
        self._load(card_present=True)
        self.assertFalse(self.page.locator("#edit-modal").is_visible())

        actual_short_title = browser_cdp._fill_creator_product_short_title(self.page, PRODUCT_NAME)

        self.assertEqual(actual_short_title, PRODUCT_NAME[:10])
        self.assertFalse(self.page.locator("#edit-modal").is_visible())
        self.assertEqual(self.page.evaluate("window.events"), [
            "edit-card", "fill-short-title", "complete-edit"
        ])

    def test_resume_reuses_verified_product_without_adding_link_again(self):
        field = self._load(card_present=True)
        self.page.locator(".product-short").evaluate(
            "(el, title) => el.textContent = '短标题：' + title + ' 每单赚17'",
            PRODUCT_NAME[:10],
        )
        card, card_text = browser_cdp._creator_product_card(self.page, PRODUCT_NAME)
        short_title = browser_cdp._creator_resume_linked_short_title(
            browser_cdp._creator_product_card_hrefs(card, PRODUCT_NAME),
            card_text,
            PRODUCT_ID,
            PRODUCT_NAME,
        )
        self.assertEqual(short_title, PRODUCT_NAME[:10])
        self.assertEqual(self.page.evaluate("window.events"), [])

    def test_resume_refuses_same_title_with_another_product_id(self):
        field = self._load(card_present=True)
        self.page.locator(".product-short").evaluate(
            "(el, title) => el.textContent = '短标题：' + title + ' 每单赚17'",
            PRODUCT_NAME[:10],
        )
        other_url = PRODUCT_URL.replace(PRODUCT_ID, "9999999999999999999")
        self.page.locator("#product-card a").evaluate("(el, url) => el.href = url", other_url)
        field.fill(other_url)
        card, card_text = browser_cdp._creator_product_card(self.page, PRODUCT_NAME)
        field.fill(PRODUCT_URL)
        with self.assertRaisesRegex(RuntimeError, "商品 ID"):
            browser_cdp._creator_resume_linked_short_title(
                browser_cdp._creator_product_card_hrefs(card, PRODUCT_NAME),
                card_text,
                PRODUCT_ID,
                PRODUCT_NAME,
            )
        self.assertEqual(self.page.evaluate("window.events"), [])

    def test_modal_for_another_product_is_never_filled(self):
        link_field = self._load(card_present=False)
        browser_cdp._creator_add_link_control(self.page, link_field).click()
        self.page.locator("#edit-modal p").evaluate(
            "node => node.textContent = '商品原标题 另一件商品'"
        )

        with self.assertRaises(RuntimeError):
            browser_cdp._fill_creator_product_short_title(self.page, PRODUCT_NAME)

        self.assertEqual(self.page.locator("#short-title").input_value(), "")
        self.assertEqual(self.page.locator("#product-card").count(), 0)
        self.assertEqual(self.page.evaluate("window.events"), ["add-link"])

    def test_delayed_add_link_modal_does_not_open_existing_card_editor_first(self):
        link_field = self._load(card_present=True, add_link_delay_ms=1000)
        browser_cdp._creator_add_link_control(self.page, link_field).click()
        self.assertFalse(self.page.locator("#edit-modal").is_visible())

        actual_short_title = browser_cdp._fill_creator_product_short_title(self.page, PRODUCT_NAME)

        self.assertEqual(actual_short_title, PRODUCT_NAME[:10])
        self.assertEqual(self.page.evaluate("window.events"), [
            "add-link-request", "add-link", "fill-short-title", "complete-edit"
        ])

    def test_short_title_still_disabled_after_fill_stops_without_product_card(self):
        link_field = self._load(card_present=False, done_stays_disabled=True)
        browser_cdp._creator_add_link_control(self.page, link_field).click()

        with self.assertRaises(RuntimeError):
            browser_cdp._fill_creator_product_short_title(self.page, PRODUCT_NAME)

        self.assertTrue(self.page.locator("#edit-modal").is_visible())
        self.assertTrue(self.page.locator("#done").evaluate(
            "el => el.classList.contains('disabled-ixFjuz')"
        ))
        self.assertEqual(self.page.locator("#product-card").count(), 0)
        self.assertEqual(self.page.evaluate("window.events"), [
            "add-link", "fill-short-title"
        ])


if __name__ == "__main__":
    unittest.main()
