#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P-23 发布核验错行重查成功与耗尽隔离路径。"""

from __future__ import annotations

import sqlite3
import unittest
from unittest.mock import MagicMock, patch

import browser_cdp
import publish_queue
from browser_cdp import PublishVerificationIsolated, WrongBaiyingProductRow


class PublishVerifyOtherRowRetryTests(unittest.TestCase):
    def _page(self):
        page = MagicMock()
        old_confirmation = MagicMock()
        old_confirmation.count.return_value = 0
        copied_confirmation = MagicMock()
        copied_confirmation.count.return_value = 1
        copied_confirmation.nth.return_value = MagicMock()
        page.get_by_text.side_effect = [old_confirmation, copied_confirmation]
        field = MagicMock()
        field.input_value.return_value = "target-1"
        query_button = MagicMock()
        return page, field, query_button

    def test_wrong_row_retry_then_correct_row_continues_to_copy(self):
        page, field, query_button = self._page()
        progress = []
        with (
            patch.object(browser_cdp, "_baiying_cart_page", return_value=page),
            patch.object(browser_cdp, "_baiying_product_search_field", return_value=field),
            patch.object(browser_cdp, "_baiying_row_snapshot", return_value="可见行=1"),
            patch.object(browser_cdp, "_visible_exact_text", return_value=[query_button]),
            patch.object(
                browser_cdp,
                "_locate_baiying_product_row",
                side_effect=[
                    WrongBaiyingProductRow("target-1", "按商品 ID 查询后只看到其他商品行（可见商品 ID：other-2）"),
                    (MagicMock(), "按商品 ID 命中 1 行"),
                ],
            ) as locate,
            patch.object(browser_cdp, "_open_baiying_product_content_menu") as open_menu,
            patch.object(browser_cdp, "_is_visible", return_value=True),
            patch.object(browser_cdp, "_extract_copied_product_url", return_value="https://haohuo.jinritemai.com/views/product/item2?id=target-1"),
            patch.object(browser_cdp, "_read_windows_clipboard_text", return_value="copied"),
        ):
            result_page, product_url = browser_cdp._copy_baiying_product_link(
                MagicMock(), "target-1", "目标商品", progress.append
            )

        self.assertIs(result_page, page)
        self.assertIn("id=target-1", product_url)
        self.assertEqual(locate.call_count, 2)
        self.assertEqual(query_button.click.call_count, 2)
        open_menu.assert_called_once()
        self.assertTrue(any(item["stage"] == "百应商品核验重查成功" for item in progress))

    def test_wrong_row_retry_exhaustion_returns_isolation_not_hard_skip(self):
        page, field, query_button = self._page()
        page.get_by_text.side_effect = [page.get_by_text.return_value]
        page.get_by_text.return_value.count.return_value = 0
        wrong_row = WrongBaiyingProductRow(
            "target-1", "按商品 ID 查询后只看到其他商品行（可见商品 ID：other-2）"
        )
        progress = []
        with (
            patch.object(browser_cdp, "_baiying_cart_page", return_value=page),
            patch.object(browser_cdp, "_baiying_product_search_field", return_value=field),
            patch.object(browser_cdp, "_baiying_row_snapshot", return_value="可见行=other-2"),
            patch.object(browser_cdp, "_visible_exact_text", return_value=[query_button]),
            patch.object(browser_cdp, "_locate_baiying_product_row", side_effect=wrong_row) as locate,
        ):
            with self.assertRaises(PublishVerificationIsolated) as raised:
                browser_cdp._copy_baiying_product_link(
                    MagicMock(), "target-1", "目标商品", progress.append
                )

        self.assertEqual(locate.call_count, browser_cdp.BAIYING_PUBLISH_VERIFY_RECHECKS + 1)
        self.assertEqual(query_button.click.call_count, browser_cdp.BAIYING_PUBLISH_VERIFY_RECHECKS + 1)
        self.assertEqual(raised.exception.classification, "PUBLISH_VERIFY_OTHER_ROW_ISOLATED")
        self.assertEqual(raised.exception.retry_count, 2)
        self.assertIn("已隔离", raised.exception.warning)
        self.assertIn("仍未能确认目标商品行", raised.exception.warning)
        self.assertIn("未上传视频", raised.exception.warning)
        self.assertTrue(any(item["stage"] == "商品核验已隔离" for item in progress))

    def test_login_prompt_after_wrong_row_is_not_classified_as_isolation(self):
        page, field, query_button = self._page()
        wrong_row = WrongBaiyingProductRow(
            "target-1", "按商品 ID 查询后只看到其他商品行（可见商品 ID：other-2）"
        )
        with (
            patch.object(browser_cdp, "_baiying_cart_page", return_value=page),
            patch.object(browser_cdp, "_baiying_product_search_field", return_value=field),
            patch.object(browser_cdp, "_baiying_row_snapshot", return_value="可见行=other-2"),
            patch.object(browser_cdp, "_visible_exact_text", return_value=[query_button]),
            patch.object(
                browser_cdp,
                "_locate_baiying_product_row",
                side_effect=[wrong_row, RuntimeError("未识别到商品行")],
            ) as locate,
            patch.object(browser_cdp, "_visible_cart_error_message", return_value="当前账号需要重新登录"),
        ):
            with self.assertRaisesRegex(RuntimeError, "平台提示") as raised:
                browser_cdp._copy_baiying_product_link(MagicMock(), "target-1", "目标商品")

        self.assertNotIsInstance(raised.exception, PublishVerificationIsolated)
        self.assertEqual(locate.call_count, 2)
        self.assertEqual(query_button.click.call_count, 2)

    def test_isolated_queue_checkpoint_excludes_item_and_cancel_releases_product_lock(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.row_factory = sqlite3.Row
        connection.execute("CREATE TABLE accounts (id INTEGER PRIMARY KEY, name TEXT, status TEXT)")
        publish_queue.ensure_schema(connection)
        connection.execute("INSERT INTO accounts VALUES (1, '主号A', 'active')")
        connection.execute(
            "INSERT INTO publish_queue (id, account_id, product_id, product_name, video_path, video_key, title, "
            "schedule_at, mode, status, platform_status, created_at, updated_at) "
            "VALUES (1, 1, 'target-1', '目标商品', 'test.mp4', 'test.mp4', '目标商品', "
            "'2026-11-06 10:00:00', 'scheduled', 'scheduled', 'uploading', '2026-11-06 10:00:00', '2026-11-06 10:00:00')"
        )
        result = publish_queue.isolate_upload_task(connection, 1, "错行重查耗尽")
        self.assertTrue(result["isolated"])
        self.assertEqual(result["platformStatus"], "isolated")
        row = connection.execute("SELECT platform_status, platform_error, isolated_from_status FROM publish_queue WHERE id = 1").fetchone()
        self.assertEqual(row["platform_status"], "isolated")
        self.assertEqual(row["isolated_from_status"], "uploading")
        self.assertIn("错行重查耗尽", row["platform_error"])
        self.assertEqual(len(publish_queue.product_publish_locks(connection, 1)["locks"]), 1)
        cancelled = publish_queue.cancel_task(connection, 1)
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(publish_queue.product_publish_locks(connection, 1)["locks"], [])
        publish_queue.ensure_product_not_locked(connection, 1, "target-1")


if __name__ == "__main__":
    unittest.main()
