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
            "excludedCategorySelection": {"level1": [], "level2": [], "level3": ["blocked-leaf"]},
            "filters": {"delivery": {}, "product": {}, "features": []},
            "poolPriority": [],
            "poolFallback": True,
            "sources": {"hotSpotEnabled": True, "allianceRankEnabled": True, "priority": ["hotSpot", "allianceRank"]},
            "thresholds": {"commissionRate": 20, "experienceScore": 4.5},
            "trendPreference": "不限走势",
            "categoryTree": [],
        }
        value.update(overrides)
        return value

    def test_three_sources_share_exclusion_and_thresholds(self):
        rows = {
            "hotSpot": [promotion_row("hot-good", ("root", "beauty", "ok-leaf"), 25, 4.8)],
            "allianceRank": [promotion_row("rank-bad", ("root", "beauty", "ok-leaf"), 10, 4.8)],
            "selectionSquare": [promotion_row("square-blocked", ("root", "other", "blocked-leaf"), 30, 4.9)],
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
        self.assertIn("佣金率 10 低于设定阈值 20", rejected["rank-bad"]["thresholdReasons"])
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
        self.assertIn("缺少佣金率实测数据，无法核验阈值 20", reasons)
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


if __name__ == "__main__":
    unittest.main()
