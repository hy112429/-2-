# -*- coding: utf-8 -*-
"""P-15: 百应明确无库存/错品时只跳过当前商品并继续。"""

import unittest
from unittest.mock import MagicMock, patch

import browser_cdp
from browser_cdp import (
    CandidateCartSkip,
    WrongBaiyingProductRow,
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

    def test_single_unmatched_row_with_empty_columns_and_oos_badge_skips(self):
        row = MagicMock()
        row.evaluate.return_value = {
            "matchesProductId": False,
            "matchesProductTitle": False,
            "rowKey": "oos-row",
            "rowText": "",
            "statusHints": ["无库存"],
            "identityHints": [],
            "domIdentityHints": [],
        }
        visible_rows = MagicMock()
        visible_rows.count.return_value = 1
        visible_rows.nth.return_value = row
        empty_rows = MagicMock()
        empty_rows.count.return_value = 0
        page = MagicMock()
        page.locator.side_effect = lambda selector: visible_rows if selector == "tbody tr" else empty_rows

        with patch.object(browser_cdp, "BAIYING_CART_ROW_SELECTORS", ("tbody tr",)), \
                patch.object(browser_cdp, "_is_visible", return_value=True):
            with self.assertRaises(CandidateCartSkip) as raised:
                _locate_baiying_product_row(page, "3832406783184470433", "实机无库存商品标题")

        self.assertIn("3832406783184470433", str(raised.exception))
        self.assertIn("核验行不可识别", str(raised.exception))
        self.assertIn("无库存", str(raised.exception))
        self.assertIn("aria-label", browser_cdp.BAIYING_ROW_METADATA_SCRIPT)
        self.assertIn("statusHints", browser_cdp.BAIYING_ROW_METADATA_SCRIPT)
        self.assertIn("node.innerText || node.textContent", browser_cdp.BAIYING_ROW_METADATA_SCRIPT)
        self.assertIn("getComputedStyle(node, pseudo)", browser_cdp.BAIYING_ROW_METADATA_SCRIPT)

    def test_multiple_unmatched_rows_with_one_oos_badge_remain_ambiguous(self):
        reason = _baiying_row_skip_reason("target-1", [
            {"matchesProductId": False, "matchesProductTitle": False, "statusHints": ["无库存"]},
            {"matchesProductId": False, "matchesProductTitle": False, "statusHints": []},
        ])
        self.assertEqual(reason, "")

    def test_single_visible_oos_row_after_hidden_dom_rows_skips(self):
        hidden_rows = [MagicMock(), MagicMock()]
        visible_row = MagicMock()
        visible_row.evaluate.return_value = {
            "matchesProductId": False,
            "matchesProductTitle": False,
            "rowText": "",
            "statusHints": ["无库存"],
            "identityHints": [],
            "domIdentityHints": [],
        }
        rows = MagicMock()
        rows.count.return_value = 3
        rows.nth.side_effect = [*hidden_rows, visible_row, *hidden_rows, visible_row]
        page = MagicMock()
        page.locator.return_value = rows
        with (
            patch.object(browser_cdp, "BAIYING_CART_ROW_SELECTORS", ("tbody tr",)),
            patch.object(browser_cdp, "_is_visible", side_effect=lambda row: row is visible_row),
        ):
            with self.assertRaises(CandidateCartSkip) as raised:
                _locate_baiying_product_row(page, "target-1", "目标商品")
        self.assertIn("无库存", str(raised.exception))

    def test_matched_target_row_with_oos_only_in_badge_skips(self):
        row = MagicMock()
        row.evaluate.return_value = {
            "matchesProductId": True,
            "matchesProductTitle": False,
            "rowText": "目标商品",
            "statusHints": ["无库存"],
            "identityHints": ["goodsId=target-1"],
        }
        rows = MagicMock()
        rows.count.return_value = 1
        rows.nth.return_value = row
        page = MagicMock()
        page.locator.return_value = rows
        with patch.object(browser_cdp, "_is_visible", return_value=True):
            with self.assertRaises(CandidateCartSkip) as raised:
                _locate_baiying_product_row(page, "target-1", "目标商品")
        self.assertIn("无库存", str(raised.exception))

    def test_same_title_wrong_id_with_oos_badge_remains_wrong_row(self):
        row = MagicMock()
        row.evaluate.return_value = {
            "matchesProductId": False,
            "matchesProductTitle": True,
            "rowText": "同标题商品",
            "statusHints": ["无库存"],
            "domIdentityHints": ["goodsId=other-2"],
        }
        rows = MagicMock()
        rows.count.return_value = 1
        rows.nth.return_value = row
        page = MagicMock()
        page.locator.return_value = rows
        with patch.object(browser_cdp, "_is_visible", return_value=True):
            with self.assertRaises(WrongBaiyingProductRow) as raised:
                _locate_baiying_product_row(page, "target-1", "同标题商品")
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
        first = {
            "cardIndex": 0,
            "productId": "3832406783184470433",
            "title": "【玉米天下】内蒙古沙土地黄糯玉米8支*240g（试吃2支）26年头茬玉米",
        }
        second = {"cardIndex": 1, "productId": "ready-2", "title": "已在车商品"}
        second_card = MagicMock()
        second_card.inner_text.return_value = "已加入选品车"
        skip = CandidateCartSkip(
            "3832406783184470433",
            "核验行不可识别·唯一可见行明确显示无库存/不可加车：无库存",
        )
        progress_events = []
        with (
            patch.object(browser_cdp, "_visible_baiying_prompt", return_value=("", "")),
            patch.object(
                browser_cdp,
                "_locate_candidate_card",
                side_effect=[skip, (MagicMock(), second_card)],
            ) as locate,
        ):
            results = execute_selected_candidates(
                MagicMock(), MagicMock(), [first, second], [0, 1], ".",
                stop_after_cart=True, progress=progress_events.append,
            )

        self.assertEqual(locate.call_count, 2)
        self.assertEqual([item["productId"] for item in results], ["3832406783184470433", "ready-2"])
        self.assertTrue(results[0]["skipped"])
        self.assertFalse(results[0]["cartAdded"])
        self.assertEqual(results[0]["title"], first["title"])
        self.assertIn("3832406783184470433", results[0]["status"])
        self.assertIn("无库存", results[0]["status"])
        self.assertIn("继续下一候选", results[0]["status"])
        self.assertTrue(any(
            event.get("productId") == first["productId"]
            and event.get("productTitle") == first["title"]
            and "无库存" in event.get("message", "")
            and "继续下一候选" in event.get("message", "")
            for event in progress_events
        ))
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
