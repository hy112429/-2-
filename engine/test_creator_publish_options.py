# -*- coding: utf-8 -*-
"""隔离复现投稿页的发布选项 DOM；不连接平台或点击发布。"""

import unittest
from pathlib import Path

from playwright.sync_api import sync_playwright

import browser_cdp


class CreatorPublishOptionsDomTests(unittest.TestCase):
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
        self.page.set_content("""
            <section id="publish-settings">
              <div class="setting-row">
                <span>保存权限</span>
                <div>
                  <label data-checked="true"><input type="checkbox" checked value="允许"><span>允许</span></label>
                  <label data-checked="false"><input type="checkbox" value="不允许"><span>不允许</span></label>
                </div>
              </div>
              <div class="setting-row">
                <span>发布时间</span>
                <div>
                  <label data-checked="true"><input type="checkbox" checked value="立即发布"><span>立即发布</span></label>
                  <label data-checked="false"><input type="checkbox" value="定时发布"><span>定时发布</span></label>
                </div>
              </div>
            </section>
            <script>
              window.optionChanges = [];
              document.querySelectorAll('label input').forEach(input => {
                input.addEventListener('change', () => {
                  input.closest('label').dataset.checked = String(input.checked);
                  window.optionChanges.push(input.value);
                });
              });
            </script>
        """)

    def tearDown(self):
        self.page.close()

    def test_checked_checkbox_labels_pass_without_radio_or_click(self):
        self.assertEqual(self.page.get_by_role("radio").count(), 0)
        for name in ("允许", "立即发布"):
            self.assertIs(browser_cdp._creator_radio_state(self.page.get_by_text(name, exact=True)), True)
            browser_cdp._ensure_creator_radio(self.page, name)
        self.assertEqual(self.page.evaluate("window.optionChanges"), [])

    def test_unchecked_checkbox_label_is_selected_and_verified(self):
        self.page.locator('input[value="允许"]').evaluate("el => { el.checked = false; el.closest('label').dataset.checked = 'false'; }")
        self.assertIs(browser_cdp._creator_radio_state(self.page.get_by_text("允许", exact=True)), False)

        browser_cdp._ensure_creator_radio(self.page, "允许")

        self.assertIs(browser_cdp._creator_radio_state(self.page.get_by_text("允许", exact=True)), True)
        self.assertEqual(self.page.evaluate("window.optionChanges"), ["允许"])


if __name__ == "__main__":
    unittest.main()
