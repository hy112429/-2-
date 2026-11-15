# -*- coding: utf-8 -*-
"""热点、榜单、选品广场统一筛选与详情补核验的隔离用例。"""

from __future__ import annotations

import unittest
from unittest.mock import patch

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
            "excludedCategorySelection": {"level1": [], "level2": [], "level3": ["309"]},
            "filters": {"delivery": {}, "product": {}, "features": []},
            "poolPriority": [],
            "poolFallback": True,
            "sources": {"hotSpotEnabled": True, "allianceRankEnabled": True, "priority": ["hotSpot", "allianceRank"]},
            "thresholds": {"commissionRate": 10, "experienceScore": 4.5},
            "trendPreference": "不限走势",
            "categoryTree": [],
        }
        value.update(overrides)
        return value

    def test_three_sources_share_exclusion_and_thresholds(self):
        rows = {
            "hotSpot": [promotion_row("hot-good", ("1", "201", "301"), 10, 4.8)],
            "allianceRank": [promotion_row("rank-bad", ("1", "201", "302"), 25, 4.8)],
            "selectionSquare": [promotion_row("square-blocked", ("1", "202", "309"), 10, 4.9)],
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
        rows = {"hotSpot": [promotion_row("missing", ("1", "201", "301"), None, None)]}
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
        row = promotion_row("detail", ("1", "201", "301"), None, None)
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

    def test_zero_and_partial_category_cannot_become_exact(self):
        tree = [{"id": "1", "children": [{"id": "201", "children": [{"id": "301"}]}]}]
        invalid = promotion_row("invalid", ("1", "201", "0"), 9, 4.8)
        self.assertEqual(browser_cdp._row_category_evidence(invalid, "invalid", tree, "material_list")["status"], "unknown")
        partial = promotion_row("partial", None, 9, 4.8)
        partial["category_id"] = "301"
        self.assertEqual(browser_cdp._row_category_evidence(partial, "partial", tree, "material_list")["categoryIds"], [
            "1", "201", "201__RC_CASCADER_SPLIT__301"
        ])
        partial["category_id"] = "0"
        self.assertEqual(browser_cdp._row_category_evidence(partial, "partial", tree, "material_list")["status"], "unknown")

    def test_material_list_path_and_source_tab_id_use_category_tree(self):
        tree = [{"id": "1", "children": [{"id": "201", "children": [{"id": "301"}]}]}]
        row = promotion_row("exact", None, 8, 4.8)
        row["categoryPath"] = "1,201,301"
        evidence = browser_cdp._row_category_evidence(row, "exact", tree, "material_list")
        self.assertEqual(evidence["status"], "known")
        self.assertEqual(evidence["categorySource"], "material_list")
        self.assertEqual(evidence["categoryIds"], ["1", "201", "201__RC_CASCADER_SPLIT__301"])
        page = type("FakePage", (), {"evaluate": lambda self, script, args: [
            {"index": 0, "name": "分类", "categoryId": "1", "selected": True}
        ]})()
        tabs = browser_cdp._source_category_tabs(page, "hotSpot", tree)
        self.assertEqual(tabs[0]["categoryIds"], ["1"])
        self.assertEqual(tabs[0]["level"], 1)

    def test_source_level_exclusion_blocks_even_when_row_has_exact_category(self):
        row = promotion_row("excluded", ("1", "201", "301"), 8, 4.8)
        row["_sourceCategory"] = {"name": "分类", "id": "1", "categoryIds": ["1"], "level": 1}
        settings = self.settings(excludedCategorySelection={"level1": ["1"], "level2": [], "level3": []})
        result = browser_cdp._visible_source_candidate_result(
            None, {"hotSpot": [row]}, {"hotSpot": {"excluded"}},
            [{"sourceKey": "hotSpot", "label": "热点", "status": "read", "productCount": 1}],
            [], {}, [], settings,
        )
        self.assertEqual(result["candidates"], [])
        self.assertIn("命中已核验的来源一级排除类目", result["rejectedCandidates"][0]["thresholdReasons"])

    def test_three_missing_detail_samples_stop_repeating_same_source(self):
        rows = [promotion_row(str(index), None, 8, 4.8) for index in range(1, 5)]
        page = type("FakePage", (), {"context": object()})()
        with patch.object(browser_cdp, "_probe_source_card_category", side_effect=lambda *args: {
            "status": "matched_response_without_category", "exactResponseSeen": True,
            "categoryIds": [], "reason": "详情响应无类目",
        }) as card, patch.object(browser_cdp, "_read_candidate_category_from_detail", return_value={
            "productId": "", "categoryIds": [], "reason": "详情响应无类目",
            "diagnostics": {"verifiedResponseCount": 1},
        }) as detail:
            result = browser_cdp._visible_source_candidate_result(
                page, {"hotSpot": rows}, {"hotSpot": {str(index) for index in range(1, 5)}},
                [{"sourceKey": "hotSpot", "label": "热点", "status": "read", "productCount": 4}],
                [], {}, [], self.settings(thresholds={}),
            )
        self.assertEqual(card.call_count, 3)
        self.assertEqual(detail.call_count, 3)
        self.assertEqual(result["prioritySourceResults"][0]["categoryProbeStatus"], "category_data_unavailable")
        self.assertEqual(result["candidates"], [])


if __name__ == "__main__":
    unittest.main()
