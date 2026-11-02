# -*- coding: utf-8 -*-
"""P-15: 百应明确无库存/错品时只跳过当前商品并继续。"""

import unittest
from unittest.mock import MagicMock, patch

import browser_cdp
from browser_cdp import (
    CandidateCartSkip,
    _baiying_row_skip_reason,
    _classify_baiying_prompt,
    _copy_baiying_product_link,
    _is_product_specific_cart_rejection,
    _locate_baiying_product_row,
    _selection_error_requires_pause,
    execute_selected_candidates,
)


class CandidateOutOfStockSkipTests(unittest.TestCase):
    def test_explicit_target_row_out_of_stock_is_skippable(self):
        reason = _baiying_row_skip_reason("target-1", [{
            "matchesProductId": True,
            "matchesProductTitle": True,
            "rowText": "目标商品 暂无库存",
            "identityHints": ["goodsId=target-1"],
        }])
        self.assertIn("无库存", reason)

    def test_visible_wrong_product_id_is_skippable_with_identity_evidence(self):
        reason = _baiying_row_skip_reason("target-1", [{
            "matchesProductId": False,
            "matchesProductTitle": False,
            "rowText": "另一个商品",
            "identityHints": ["goodsId=other-2"],
        }])
        self.assertIn("其他商品行", reason)
        self.assertIn("other-2", reason)

    def test_wrong_row_identity_falls_back_when_dom_hints_are_empty(self):
        reason = _baiying_row_skip_reason("target-1", [{
            "matchesProductId": False,
            "matchesProductTitle": False,
            "rowText": "另一个商品",
            "domIdentityHints": [],
            "identityHints": ["goodsId=other-2"],
        }])
        self.assertIn("其他商品行", reason)
        self.assertIn("other-2", reason)

    def test_row_locator_turns_wrong_item_diagnostic_sample_into_candidate_skip(self):
        row = MagicMock()
        row.evaluate.return_value = {
            "matchesProductId": False,
            "matchesProductTitle": False,
            "rowKey": "wrong-row",
            "rowText": "另一商品 无库存",
            "identityHints": ["goodsId=other-2"],
        }
        rows = MagicMock()
        rows.count.return_value = 1
        rows.nth.return_value = row
        page = MagicMock()
        page.locator.return_value = rows
        with (
            patch.object(browser_cdp, "BAIYING_CART_ROW_SELECTORS", ("tbody tr",)),
            patch.object(browser_cdp, "_is_visible", return_value=True),
        ):
            with self.assertRaises(CandidateCartSkip) as raised:
                _locate_baiying_product_row(page, "target-1", "目标商品")
        self.assertIn("target-1", str(raised.exception))
        self.assertIn("other-2", str(raised.exception))

    def test_identity_ambiguity_without_oos_or_wrong_item_evidence_still_pauses(self):
        reason = _baiying_row_skip_reason("target-1", [{
            "matchesProductId": False,
            "matchesProductTitle": False,
            "rowText": "商品卡片",
            "identityHints": [],
        }])
        self.assertEqual(reason, "")
        error = RuntimeError("百应列表存在同名商品，无法按精确商品 ID target-1 唯一定位")
        self.assertTrue(_selection_error_requires_pause(error))

    def test_oos_prompt_is_product_rejection_but_account_limit_is_not(self):
        self.assertEqual(_classify_baiying_prompt("该商品无库存，请选择其他商品"), "cart_error")
        self.assertTrue(_is_product_specific_cart_rejection("该商品无库存"))
        self.assertFalse(_is_product_specific_cart_rejection("当前账号无权限加车"))

    def test_copy_link_propagates_evidence_based_skip_without_retrying_as_timeout(self):
        page = MagicMock()
        page.get_by_text.return_value.count.return_value = 0
        field = MagicMock()
        field.input_value.return_value = "target-1"
        query_button = MagicMock()
        skip = CandidateCartSkip("target-1", "目标商品行明确显示无库存")
        with (
            patch.object(browser_cdp, "_baiying_cart_page", return_value=page),
            patch.object(browser_cdp, "_baiying_product_search_field", return_value=field),
            patch.object(browser_cdp, "_baiying_row_snapshot", return_value=""),
            patch.object(browser_cdp, "_visible_exact_text", return_value=[query_button]),
            patch.object(browser_cdp, "_locate_baiying_product_row", side_effect=skip) as locate,
        ):
            with self.assertRaises(CandidateCartSkip) as raised:
                _copy_baiying_product_link(MagicMock(), "target-1", "目标商品")
        self.assertIs(raised.exception, skip)
        locate.assert_called_once()

    def test_copy_link_oos_alert_is_candidate_skip(self):
        page = MagicMock()
        page.get_by_text.return_value.count.return_value = 0
        field = MagicMock()
        field.input_value.return_value = "target-1"
        query_button = MagicMock()
        with (
            patch.object(browser_cdp, "_baiying_cart_page", return_value=page),
            patch.object(browser_cdp, "_baiying_product_search_field", return_value=field),
            patch.object(browser_cdp, "_baiying_row_snapshot", return_value=""),
            patch.object(browser_cdp, "_visible_exact_text", return_value=[query_button]),
            patch.object(
                browser_cdp, "_locate_baiying_product_row",
                side_effect=RuntimeError("尚未识别到结果行"),
            ),
            patch.object(browser_cdp, "_visible_cart_error_message", return_value="该商品无库存"),
        ):
            with self.assertRaises(CandidateCartSkip) as raised:
                _copy_baiying_product_link(MagicMock(), "target-1", "目标商品")
        self.assertIn("target-1", str(raised.exception))
        self.assertIn("无库存", str(raised.exception))

    def test_batch_skips_confirmed_bad_candidate_and_processes_next(self):
        first = {"cardIndex": 0, "productId": "oos-1", "title": "无库存商品"}
        second = {"cardIndex": 1, "productId": "ready-2", "title": "已在车商品"}
        second_card = MagicMock()
        second_card.inner_text.return_value = "已加入选品车"
        skip = CandidateCartSkip("oos-1", "目标商品行明确显示无库存")
        with (
            patch.object(browser_cdp, "_visible_baiying_prompt", return_value=("", "")),
            patch.object(
                browser_cdp,
                "_locate_candidate_card",
                side_effect=[skip, (MagicMock(), second_card)],
            ) as locate,
        ):
            results = execute_selected_candidates(
                MagicMock(), MagicMock(), [first, second], [0, 1], ".", stop_after_cart=True
            )

        self.assertEqual(locate.call_count, 2)
        self.assertEqual([item["productId"] for item in results], ["oos-1", "ready-2"])
        self.assertTrue(results[0]["skipped"])
        self.assertFalse(results[0]["cartAdded"])
        self.assertIn("oos-1", results[0]["status"])
        self.assertIn("无库存", results[0]["status"])
        self.assertTrue(results[1]["cartAdded"])

    def test_stale_oos_toast_does_not_block_next_candidate(self):
        candidate = {"cardIndex": 0, "productId": "next-2", "title": "下一商品"}
        card = MagicMock()
        card.inner_text.return_value = "已加入选品车"
        with (
            patch.object(
                browser_cdp, "_visible_baiying_prompt",
                side_effect=[("cart_error", "该商品无库存"), ("", "")],
            ),
            patch.object(browser_cdp, "_dismiss_cart_error_dialog") as dismiss,
            patch.object(browser_cdp, "_locate_candidate_card", return_value=(MagicMock(), card)),
        ):
            results = execute_selected_candidates(
                MagicMock(), MagicMock(), [candidate], [0], ".", stop_after_cart=True
            )
        dismiss.assert_called_once()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["productId"], "next-2")
        self.assertTrue(results[0]["cartAdded"])
        self.assertIn("残留的无库存提示", results[0]["platformNotices"][0])


if __name__ == "__main__":
    unittest.main()
