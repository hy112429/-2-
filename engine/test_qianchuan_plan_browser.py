"""付费计划浏览器执行器的账户隔离与回执识别。"""

from __future__ import annotations

import unittest

from qianchuan_plan_browser import Stopped, _account_gate, _plan_id


class _Body:
    def __init__(self, text: str):
        self.text = text

    def inner_text(self, timeout: int) -> str:
        return self.text


class _Page:
    def __init__(self, url: str, text: str):
        self.url = url
        self.text = text

    def locator(self, selector: str) -> _Body:
        assert selector == "body"
        return _Body(self.text)


class QianchuanPlanBrowserTest(unittest.TestCase):
    def test_url_and_visible_advertiser_must_both_match(self) -> None:
        target = "1234567890123456"
        _account_gate(_Page(f"https://qianchuan.jinritemai.com/uni-creation/product?aavid={target}",
                            f"账户名\nID：{target}"), target, "account")
        with self.assertRaises(Stopped):
            _account_gate(_Page("https://qianchuan.jinritemai.com/uni-creation/product?aavid=9999999999999999",
                                f"账户名\nID：{target}"), target, "account")
        with self.assertRaises(Stopped):
            _account_gate(_Page(f"https://qianchuan.jinritemai.com/uni-creation/product?aavid={target}",
                                "账户名\nID：9999999999999999"), target, "account")

    def test_captcha_stops_before_plan_submission(self) -> None:
        target = "1234567890123456"
        with self.assertRaisesRegex(Stopped, "人工"):
            _account_gate(_Page(f"https://qianchuan.jinritemai.com/uni-creation/product?aavid={target}",
                                f"ID：{target}\n请完成验证"), target, "before_submit")

    def test_plan_id_requires_known_response_field(self) -> None:
        self.assertEqual(_plan_id({"data": {"plan_id": "123456789"}}), "123456789")
        self.assertEqual(_plan_id({"data": {"material_id": "123456789"}}), "")


if __name__ == "__main__":
    unittest.main()
