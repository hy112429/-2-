# -*- coding: utf-8 -*-
"""热点、榜单、选品广场统一筛选与详情补核验的隔离用例。"""

from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, patch

import browser_cdp


def promotion_row(
    product_id: str,
    category: tuple[str, str, str] | None,
    commission: float | None,
    experience: float | None,
) -> dict:
    info: dict = {"name": f"商品-{product_id}"}
    if category:
        info["category"] = {"first": category[0], "second": category[1], "third": category[2]}
    base = {
        "product_info": info,
        "promotion_info": {"cos_info": {"cos_ratio_text": commission} if commission is not None else {}},
        "shop_info": {"experience_score": experience} if experience is not None else {},
    }
    return {"product_id": product_id, "promotion_id": f"promo-{product_id}", "base_model": base}


class SelectionCandidateFilterTests(unittest.TestCase):
    def settings(self, **overrides):
        value = {
            "candidateLimit": 5,
            "selectionMode": "general",
            "categoryMode": "auto",
            "categorySelection": {"level1": [], "level2": [], "level3": []},
            "excludedCategorySelection": {"level1": [], "level2": [], "level3": ["blocked-leaf"]},
            "filters": {"delivery": {}, "product": {}, "features": []},
            "poolPriority": [],
            "poolFallback": True,
            "sources": {"hotSpotEnabled": True, "allianceRankEnabled": True, "priority": ["hotSpot", "allianceRank"]},
            "thresholds": {"maxCommissionRate": 10, "experienceScore": 4.5},
            "trendPreference": "不限走势",
            "categoryTree": [],
        }
        value.update(overrides)
        return value

    def test_category_verification_scope_is_narrowed_to_needed_modes(self):
        self.assertFalse(browser_cdp._category_verification_required({
            "selectionMode": "general",
            "excludedCategorySelection": {"level1": [], "level2": [], "level3": []},
        }))
        self.assertTrue(browser_cdp._category_verification_required({
            "selectionMode": "general",
            "excludedCategorySelection": {"level1": ["blocked"], "level2": [], "level3": []},
        }))
        self.assertTrue(browser_cdp._category_verification_required({
            "selectionMode": "category",
            "excludedCategorySelection": {"level1": [], "level2": [], "level3": []},
        }))

    def test_general_mode_without_exclusions_keeps_unknown_category_candidate(self):
        rows = {"hotSpot": [promotion_row("unknown-category", None, 25, 4.8)]}
        result = browser_cdp._visible_source_candidate_result(
            None,
            rows,
            {"hotSpot": {"unknown-category"}},
            [{"sourceKey": "hotSpot", "label": "热点", "status": "read", "productCount": 1}],
            [],
            {},
            [],
            {**self.settings(), "thresholds": {}, "excludedCategorySelection": {"level1": [], "level2": [], "level3": []}},
        )
        self.assertEqual([item["productId"] for item in result["candidates"]], ["unknown-category"])
        self.assertEqual(result["rejectedCandidates"], [])

    def test_three_sources_share_exclusion_and_thresholds(self):
        rows = {
            "hotSpot": [promotion_row("hot-good", ("root", "beauty", "ok-leaf"), 10, 4.8)],
            "allianceRank": [promotion_row("rank-bad", ("root", "beauty", "ok-leaf"), 25, 4.8)],
            "selectionSquare": [promotion_row("square-blocked", ("root", "other", "blocked-leaf"), 10, 4.9)],
        }
        membership = {key: {row["product_id"] for row in values} for key, values in rows.items()}
        result = browser_cdp._visible_source_candidate_result(
            None,
            rows,
            membership,
            [{"sourceKey": key, "label": key, "status": "read", "productCount": len(values)} for key, values in rows.items()],
            rows["selectionSquare"],
            {},
            [],
            self.settings(),
        )
        self.assertEqual([item["productId"] for item in result["candidates"]], ["hot-good"])
        rejected = {item["productId"]: item for item in result["rejectedCandidates"]}
        self.assertIn("rank-bad", rejected)
        self.assertIn("佣金率 25 超过设定上限 10", rejected["rank-bad"]["thresholdReasons"])
        self.assertIn("square-blocked", rejected)
        self.assertIn("命中当前设置中排除的类目", rejected["square-blocked"]["thresholdReasons"])

    def test_missing_threshold_is_not_silently_allowed_after_detail_failure(self):
        rows = {"hotSpot": [promotion_row("missing", ("root", "beauty", "ok-leaf"), None, None)]}
        membership = {"hotSpot": {"missing"}}
        with patch.object(browser_cdp, "_read_candidate_category_from_detail", return_value={
            "productId": "missing", "categoryIds": [], "reason": "DETAIL_UNAVAILABLE"
        }), patch.object(browser_cdp, "_cached_detail_payload", return_value=None):
            result = browser_cdp._visible_source_candidate_result(
                type("FakePage", (), {"context": object()})(),
                rows,
                membership,
                [{"sourceKey": "hotSpot", "label": "热点", "status": "read", "productCount": 1}],
                [],
                {},
                [],
                {**self.settings(), "excludedCategorySelection": {"level1": [], "level2": [], "level3": []}},
            )
        self.assertEqual(result["candidates"], [])
        reasons = result["rejectedCandidates"][0]["thresholdReasons"]
        self.assertIn("缺少佣金率实测数据，无法核验上限 10", reasons)
        self.assertIn("缺少体验分实测数据，无法核验阈值 4.5", reasons)

    def test_detail_metrics_are_written_back_to_common_candidate_shape(self):
        row = promotion_row("detail", ("root", "beauty", "ok-leaf"), None, None)
        payload = {"fixture": True}
        with patch.object(browser_cdp, "_detail_metrics", return_value={
            "productId": "detail",
            "error": "",
            "metrics": {
                "commissionRate": 22,
                "experienceScore": 4.7,
                "salesCount": 100,
            },
        }):
            browser_cdp._merge_detail_metrics_into_promotion_row(row, payload, "detail")
        metrics = browser_cdp._promotion_row_metric_values(row)
        self.assertEqual(metrics["commissionRate"], 22)
        self.assertEqual(metrics["experienceScore"], 4.7)
        self.assertEqual(metrics["salesCount"], 100)
        self.assertEqual(row["_detailMetricKeys"], ["commissionRate", "experienceScore", "salesCount"])

    def test_detail_category_parser_accepts_current_serialized_and_named_shapes(self):
        category_tree = [{
            "id": "1", "name": "一级", "children": [{
                "id": "2", "name": "二级", "children": [{"id": "3", "name": "三级", "children": []}]
            }]
        }]
        expected = ["1", "2", "2__RC_CASCADER_SPLIT__3"]
        payloads = [
            {"data": {"model": {"product": {"product_id": "p1", "product_base": {"category_path": "1,2,3"}}}}},
            {"data": json.dumps({"model": {"product": {"product_id": "p1", "product_base": {"categoryIds": ["1", "2", "3"]}}}})},
            {"data": {"model": {"product": {"product_id": "p1", "product_base": {
                "category": {"firstName": "一级", "secondName": "二级", "thirdName": "三级"}
            }}}}},
        ]
        for payload in payloads:
            category_ids, reason = browser_cdp._detail_category_path(payload, "p1", category_tree)
            self.assertEqual(category_ids, expected)
            self.assertEqual(reason, "")

    def test_detail_dom_explicit_path_is_accepted_after_identity_is_verified(self):
        page = MagicMock()
        page.evaluate.return_value = ["2__RC_CASCADER_SPLIT__3"]
        tree = [{
            "id": "1", "name": "一级", "children": [{
                "id": "2", "name": "二级", "children": [{"id": "3", "name": "三级", "children": []}]
            }]
        }]
        category_ids, reason = browser_cdp._read_detail_category_from_dom(page, "p1", tree)
        self.assertEqual(category_ids, ["1", "2", "2__RC_CASCADER_SPLIT__3"])
        self.assertEqual(reason, "")

    def test_commission_lower_and_upper_bounds_are_both_enforced(self):
        rows = [
            promotion_row("commission-good", ("root", "beauty", "ok-leaf"), 25, 4.8),
            promotion_row("commission-low", ("root", "beauty", "ok-leaf"), 5, 4.8),
            promotion_row("commission-high", ("root", "beauty", "ok-leaf"), 40, 4.8),
            promotion_row("commission-missing", ("root", "beauty", "ok-leaf"), None, 4.8),
        ]
        candidates, _, rejected = browser_cdp._candidate_rows(
            {"data": {"summary_promotions": rows}},
            {**self.settings(), "thresholds": {"minCommissionRate": 10, "maxCommissionRate": 30}},
            10,
        )
        self.assertEqual([item["productId"] for item in candidates], ["commission-good"])
        reasons = {item["productId"]: item["thresholdReasons"] for item in rejected}
        self.assertIn("佣金率 5 低于设定下限 10", reasons["commission-low"])
        self.assertIn("佣金率 40 超过设定上限 30", reasons["commission-high"])
        self.assertIn("缺少佣金率实测数据，无法核验下限 10", reasons["commission-missing"])

    def test_source_category_selection_can_exclude_explicit_source_tab(self):
        candidate = {"sourceCategorySelection": {"level1": ["1"], "level2": [], "level3": []}}
        self.assertTrue(browser_cdp._candidate_matches_source_categories(candidate, {"1"}))
        self.assertFalse(browser_cdp._candidate_matches_source_categories(candidate, {"2"}))

    def test_source_category_tab_maps_only_unique_local_category(self):
        tree = [{
            "id": "1", "name": "服饰内衣", "children": [{
                "id": "2", "name": "女装", "children": [{"id": "3", "name": "连衣裙", "children": []}]
            }]
        }]
        mapped = browser_cdp._map_source_category_options_to_selection(
            [{"label": "服饰内衣", "id": "1", "selected": True}], tree
        )
        self.assertEqual(mapped, {"level1": ["1"], "level2": [], "level3": []})
        unknown = browser_cdp._map_source_category_options_to_selection(
            [{"label": "不存在类目", "id": "", "selected": True}], tree
        )
        self.assertEqual(unknown, {"level1": [], "level2": [], "level3": []})

    def test_platform_commission_filter_is_applied_before_candidate_read(self):
        base = self.settings()
        base["thresholds"] = {"commissionRate": 10}
        self.assertEqual(browser_cdp._commission_rate_platform_filter_options(base), ["10%-20%", "20%-30%", "30%-40%", "40%-50%", "≥50%"])
        base["_platformCommissionRateOption"] = "10%-20%"
        self.assertEqual(browser_cdp._effective_selection_filters(base)["product"]["佣金区间"], "10%-20%")
        base["thresholds"] = {"minCommissionRate": 20, "maxCommissionRate": 30}
        base.pop("_platformCommissionRateOption", None)
        self.assertEqual(browser_cdp._commission_rate_platform_filter_options(base), ["20%-30%"])
        self.assertEqual(browser_cdp._effective_selection_filters(base)["product"]["佣金区间"], "20%-30%")
        base["thresholds"] = {"minCommissionRate": 50}
        self.assertEqual(browser_cdp._effective_selection_filters(base)["product"]["佣金区间"], "≥50%")

    def test_environment_risk_page_is_classified_as_account_detail_stop(self):
        self.assertEqual(browser_cdp._classify_baiying_prompt("环境危险，请使用常用设备打开"), "environment_risk")


if __name__ == "__main__":
    unittest.main()
