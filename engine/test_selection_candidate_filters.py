# -*- coding: utf-8 -*-
"""热点、榜单、选品广场分来源筛选与详情补核验的隔离用例。"""

from __future__ import annotations

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
            "commissionRateRule": "minimum",
            "selectionMode": "general",
            "categoryMode": "auto",
            "categorySelection": {"level1": [], "level2": [], "level3": []},
            "excludedCategorySelection": {"level1": [], "level2": [], "level3": []},
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

    def rank_page(self, unlimited_labels: set[str]):
        page = MagicMock()
        locators = {}
        for label in {"爆款榜", "昨日", *unlimited_labels}:
            option = MagicMock()
            option.is_visible.return_value = True
            option.evaluate.return_value = True
            locator = MagicMock()
            locator.count.return_value = 1
            locator.nth.return_value = option
            locator.first = option
            locators[label] = locator
        missing = MagicMock()
        missing.count.return_value = 0
        page.get_by_text.side_effect = lambda label, exact=True: locators.get(label, missing)
        return page

    def test_rank_accepts_both_unlimited_carrier_page_labels(self):
        for page_label in ("不限载体", "不限体裁"):
            with self.subTest(page_label=page_label):
                page = self.rank_page({page_label})
                selected = browser_cdp._select_rank_source_options(page, {
                    "sources": {"rankBoard": "爆款榜", "rankPeriod": "昨日", "rankCarrier": "不限载体"}
                })
                self.assertEqual(selected, {
                    "rankBoard": "爆款榜", "rankPeriod": "昨日", "rankCarrier": "不限载体"
                })

    def test_rank_rejects_ambiguous_unlimited_carrier_controls(self):
        page = self.rank_page({"不限载体", "不限体裁"})
        with self.assertRaisesRegex(browser_cdp.RecoverablePrioritySourceReadError, "控件未唯一定位"):
            browser_cdp._select_rank_source_options(page, {
                "sources": {"rankBoard": "爆款榜", "rankPeriod": "昨日", "rankCarrier": "不限载体"}
            })

    def test_rank_price_uses_verified_cents_and_rejects_missing_price(self):
        row = promotion_row("rank-price", ("4", "100", "200"), 15, 4.8)
        self.assertFalse(browser_cdp._rank_row_matches_price(row, "10-50"))
        row["base_model"]["marketing_info"] = {"price_desc": {"price": {"origin": 990}}}
        self.assertEqual(browser_cdp._rank_row_price_yuan(row), 9.9)
        self.assertFalse(browser_cdp._rank_row_matches_price(row, "10-50"))
        row["base_model"]["marketing_info"]["price_desc"]["price"]["origin"] = 1050
        self.assertTrue(browser_cdp._rank_row_matches_price(row, "10-50"))
        self.assertFalse(browser_cdp._rank_row_matches_price(row, "1-10"))
        self.assertTrue(browser_cdp._rank_row_matches_price(row, "不限"))

    def test_rank_platform_thresholds_never_narrow_beyond_local_minimum(self):
        self.assertEqual(browser_cdp._rank_commission_platform_floor(
            self.settings(thresholds={"commissionRate": 15, "commissionRateMax": 40})
        ), "≥10%")
        self.assertEqual(browser_cdp._rank_commission_platform_floor(
            self.settings(thresholds={"commissionRate": None, "commissionRateMax": 40})
        ), "不限")
        self.assertEqual(browser_cdp._rank_experience_platform_floor(
            self.settings(thresholds={"experienceScore": 4.5})
        ), "不限")
        self.assertEqual(browser_cdp._rank_experience_platform_floor(
            self.settings(thresholds={"experienceScore": 85})
        ), "≥85")

    def test_rank_tabs_require_unique_official_category_id(self):
        page = MagicMock()
        page.evaluate.return_value = [
            {"index": 0, "name": "全部", "categoryId": "", "selected": True},
            {"index": 1, "name": "服饰内衣", "categoryId": "", "selected": False},
        ]
        tree = [{"id": "4", "children": [{"id": "100", "children": [{"id": "201"}]}]}]
        mapped = browser_cdp._source_category_tabs(page, "allianceRank", tree, {"服饰内衣": {"4"}})
        self.assertEqual(mapped[1]["categoryIds"], ["4"])
        self.assertEqual(mapped[1]["status"], "mapped")
        ambiguous = browser_cdp._source_category_tabs(page, "allianceRank", tree, {"服饰内衣": {"4", "5"}})
        self.assertEqual(ambiguous[1]["status"], "unresolved")

    def test_commission_confirm_click_waits_for_stable_button(self):
        page = MagicMock()
        confirm = MagicMock()
        confirm.is_visible.return_value = True
        confirm.is_enabled.return_value = True
        confirm.bounding_box.return_value = {"x": 10, "y": 20, "width": 60, "height": 24}
        browser_cdp._click_filter_confirm_stably(page, confirm)
        confirm.scroll_into_view_if_needed.assert_called_once()
        confirm.click.assert_called_once_with(timeout=2500, no_wait_after=True)

    def test_commission_confirm_accepts_applied_chip_after_click_timeout(self):
        page = MagicMock()
        confirm = MagicMock()
        confirm.is_visible.return_value = True
        confirm.is_enabled.return_value = True
        confirm.bounding_box.return_value = {"x": 10, "y": 20, "width": 60, "height": 24}
        confirm.click.side_effect = TimeoutError("element is not stable after click")
        browser_cdp._click_filter_confirm_stably(page, confirm, confirmation_check=lambda: True)
        confirm.click.assert_called_once_with(timeout=2500, no_wait_after=True)

    def test_commission_confirm_does_not_reclick_when_popup_state_is_unknown(self):
        page = MagicMock()
        confirm = MagicMock()
        confirm.click.side_effect = TimeoutError("element is not stable after click")
        with patch.object(browser_cdp, "_wait_filter_confirm_stable"), \
             patch.object(browser_cdp, "_wait_filter_dropdown_closed", side_effect=browser_cdp.PlaywrightTimeoutError("popup remains open")):
            with self.assertRaises(browser_cdp.CommissionFilterUnconfirmed):
                browser_cdp._click_filter_confirm_stably(page, confirm)
        confirm.click.assert_called_once_with(timeout=2500, no_wait_after=True)

    def test_filter_chip_readback_uses_keyword_arg_and_only_swallows_timeout(self):
        for readback in (browser_cdp._wait_filter_applied_chip, browser_cdp._wait_filter_summary_text):
            with self.subTest(readback=readback.__name__):
                class KeywordOnlyPage:
                    def __init__(self, error=None):
                        self.error = error
                        self.arg = None

                    def wait_for_function(self, expression, *, arg=None, timeout=None, polling=None):
                        self.arg = arg
                        if self.error is not None:
                            raise self.error

                page = KeywordOnlyPage()
                self.assertTrue(readback(page, "佣金区间", "10%-50%", timeout_ms=350))
                self.assertEqual(page.arg, {"label": "佣金区间", "value": "10%-50%"})
                self.assertFalse(readback(
                    KeywordOnlyPage(browser_cdp.PlaywrightTimeoutError("result chip missing")),
                    "佣金区间", "10%-50%", timeout_ms=350,
                ))
                with self.assertRaises(TypeError):
                    readback(KeywordOnlyPage(TypeError("invalid Playwright call")), "佣金区间", "10%-50%")

    def test_commission_range_reuses_verified_result_chip_without_reopening_menu(self):
        page = MagicMock()
        with patch.object(browser_cdp, "_wait_filter_dropdown_closed"), \
             patch.object(browser_cdp, "_wait_filter_summary_text", return_value=True) as summary, \
             patch.object(browser_cdp, "_open_filter_dropdown") as open_menu:
            browser_cdp._apply_square_commission_range(
                page, self.settings(thresholds={"commissionRate": 10, "commissionRateMax": 50})
            )
        summary.assert_called_once_with(page, "佣金区间", "10%-50%", timeout_ms=350)
        open_menu.assert_not_called()

    def test_commission_range_rejects_confirm_without_result_chip(self):
        page = MagicMock()
        page.is_closed.return_value = False
        menu = MagicMock()
        menu.locator.return_value.count.return_value = 1
        menu.get_by_text.return_value.count.return_value = 1
        with patch.object(browser_cdp, "_wait_filter_dropdown_closed"), \
             patch.object(browser_cdp, "_wait_filter_summary_text", return_value=False), \
             patch.object(browser_cdp, "_open_filter_dropdown", return_value=(MagicMock(), menu)), \
             patch.object(browser_cdp, "_click_filter_confirm_stably") as click_confirm, \
             patch.object(browser_cdp, "_close_filter_dropdown"):
            with self.assertRaises(browser_cdp.CommissionFilterUnconfirmed):
                browser_cdp._apply_square_commission_range(
                    page, self.settings(thresholds={"commissionRate": 10, "commissionRateMax": 50})
                )
        click_confirm.assert_called_once()

    def test_commission_range_accepts_result_chip_after_click_timeout(self):
        page = MagicMock()
        page.is_closed.return_value = False
        menu = MagicMock()
        menu.locator.return_value.count.return_value = 1
        menu.get_by_text.return_value.count.return_value = 1
        with patch.object(browser_cdp, "_wait_filter_dropdown_closed"), \
             patch.object(browser_cdp, "_wait_filter_summary_text", side_effect=[False, True]), \
             patch.object(browser_cdp, "_open_filter_dropdown", return_value=(MagicMock(), menu)), \
             patch.object(browser_cdp, "_click_filter_confirm_stably", side_effect=browser_cdp.CommissionFilterUnconfirmed("click timeout")) as click_confirm, \
             patch.object(browser_cdp, "_close_filter_dropdown"):
            browser_cdp._apply_square_commission_range(
                page, self.settings(thresholds={"commissionRate": 10, "commissionRateMax": 50})
            )
        click_confirm.assert_called_once()

    def test_unconfirmed_square_source_is_skipped_without_marking_collection_failed(self):
        settings = self.settings(
            _visibleSourcesOnly=True, _candidatePoolCollectOnly=True,
            sources={"hotSpotEnabled": False, "allianceRankEnabled": False, "selectionSquareEnabled": True, "priority": []},
        )
        with patch.object(browser_cdp, "_collect_priority_source_ids", return_value=({}, [], [], {})), \
             patch.object(browser_cdp, "_collect_selection_square_rows", side_effect=browser_cdp.CommissionFilterUnconfirmed("COMMISSION_FILTER_UNCONFIRMED")):
            result = browser_cdp.apply_settings_and_collect_candidates(MagicMock(), settings)
        self.assertTrue(result["ok"])
        self.assertEqual(result["candidateCount"], 0)
        self.assertEqual(result["prioritySourceResults"][-1]["status"], "skipped")
        self.assertIn("COMMISSION_FILTER_UNCONFIRMED", result["prioritySourceResults"][-1]["detail"])

    def test_source_policies_apply_shared_thresholds_to_hotspot_and_rank(self):
        rows = {
            "hotSpot": [promotion_row("hot-good", ("1", "201", "301"), 12, 4.8)],
            "allianceRank": [promotion_row("rank-bad", ("1", "201", "302"), 5, 4.8)],
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
            self.settings(
                excludedCategorySelection={"level1": [], "level2": [], "level3": ["202__RC_CASCADER_SPLIT__309"]},
                categoryTree=[{"id": "1", "children": [
                    {"id": "201", "children": [{"id": "301"}, {"id": "302"}]},
                    {"id": "202", "children": [{"id": "309"}]},
                ]}],
            ),
        )
        self.assertEqual([item["productId"] for item in result["candidates"]], ["hot-good"])
        rejected = {item["productId"]: item for item in result["rejectedCandidates"]}
        self.assertIn("rank-bad", rejected)
        self.assertIn("佣金率 5 低于设定下限 10", rejected["rank-bad"]["thresholdReasons"])
        self.assertIn("square-blocked", rejected)
        self.assertIn("命中当前设置中排除的类目", rejected["square-blocked"]["thresholdReasons"])
        self.assertEqual(result["candidates"][0]["sourceKey"], "hotSpot")
        self.assertEqual(result["candidates"][0]["sourcePolicy"], "hotspot_category_and_shared_threshold_recheck")

    def test_missing_threshold_is_not_silently_allowed_after_detail_failure(self):
        rows = {"allianceRank": [promotion_row("missing", ("1", "201", "301"), None, None)]}
        membership = {"allianceRank": {"missing"}}
        with patch.object(browser_cdp, "_read_candidate_category_from_detail", return_value={
            "productId": "missing", "categoryIds": [], "reason": "DETAIL_UNAVAILABLE"
        }), patch.object(browser_cdp, "_cached_detail_payload", return_value=None):
            result = browser_cdp._visible_source_candidate_result(
                type("FakePage", (), {"context": object()})(),
                rows,
                membership,
                [{"sourceKey": "allianceRank", "label": "榜单", "status": "read", "productCount": 1}],
                [],
                {},
                [],
                {**self.settings(), "excludedCategorySelection": {"level1": [], "level2": [], "level3": []}},
            )
        self.assertEqual(result["candidates"], [])
        reasons = result["rejectedCandidates"][0]["thresholdReasons"]
        self.assertIn("缺少佣金率实测数据，无法核验设定区间", reasons)
        self.assertIn("缺少体验分实测数据，无法核验阈值 4.5", reasons)

    def test_hotspot_rejects_candidates_below_shared_commission_and_experience_thresholds(self):
        row = promotion_row("hot-low", ("1", "201", "301"), 1, 2.0)
        candidates, _messages, rejected = browser_cdp._candidate_rows_by_source_policy(
            {"hotSpot": [row]}, {"hotSpot": {"hot-low"}}, self.settings(), 20,
        )
        self.assertEqual(candidates, [])
        self.assertEqual([item["productId"] for item in rejected], ["hot-low"])
        self.assertIn("佣金率 1 低于设定下限 10", rejected[0]["thresholdReasons"])
        self.assertIn("体验分 2 低于阈值 4.5", rejected[0]["thresholdReasons"])

    def test_rank_keeps_configured_thresholds_while_square_rechecks_them_locally(self):
        rank = promotion_row("rank-low", ("1", "201", "301"), 5, 4.8)
        square = promotion_row("square-low", ("1", "201", "302"), 5, 4.8)
        candidates, _messages, rejected = browser_cdp._candidate_rows_by_source_policy(
            {"allianceRank": [rank], "selectionSquare": [square]},
            {"allianceRank": {"rank-low"}, "selectionSquare": {"square-low"}},
            self.settings(), 20,
        )
        self.assertEqual(candidates, [])
        self.assertEqual({item["productId"] for item in rejected}, {"rank-low", "square-low"})

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
        named_tree = [{"id": "1", "name": "分类", "children": []}]
        name_only_page = type("NameOnlyPage", (), {"evaluate": lambda self, script, args: [
            {"index": 0, "name": "分类", "categoryId": "", "selected": False}
        ]})()
        name_only_tabs = browser_cdp._source_category_tabs(name_only_page, "hotSpot", named_tree)
        self.assertEqual(name_only_tabs[0]["status"], "request_pending")
        self.assertEqual(name_only_tabs[0]["categoryIds"], ["1"])

    def test_general_square_category_paths_skip_excluded_levels_before_query(self):
        settings = self.settings(
            excludedCategorySelection={"level1": ["2"], "level2": [], "level3": ["101__RC_CASCADER_SPLIT__302"]},
            categoryTree=[
                {"id": "1", "children": [
                    {"id": "101", "children": [{"id": "301"}, {"id": "302"}]},
                    {"id": "102", "children": [{"id": "303"}]},
                ]},
                {"id": "2", "children": [{"id": "201", "children": [{"id": "401"}]}]},
            ],
        )
        self.assertEqual(
            browser_cdp._selection_square_allowed_category_paths(settings),
            [("1", "102")],
        )

    def test_filter_conditions_are_applied_before_category_selection(self):
        page = MagicMock()
        page.get_by_text.return_value.count.return_value = 0
        events: list[str] = []
        settings = self.settings(
            filters={"delivery": {}, "product": {"商家体验分": ["80分以上"]}, "features": []},
            filterCatalog={"filters": {"商家体验分": []}, "features": []},
            categorySelection={"level1": ["1"], "level2": ["101"], "level3": ["101__RC_CASCADER_SPLIT__301"]},
            thresholds={"commissionRate": 10},
        )
        with patch.object(browser_cdp, "_page_applied_filter_signature", return_value=""), \
             patch.object(browser_cdp, "_apply_configured_filter_field", side_effect=lambda *args: events.append("field")), \
             patch.object(browser_cdp, "_apply_square_commission_range", side_effect=lambda *args: events.append("commission")), \
             patch.object(browser_cdp, "apply_category_settings", side_effect=lambda *args, **kwargs: events.append("category") or []):
            browser_cdp.apply_filter_settings(page, settings)
        self.assertEqual(events, ["field", "commission", "category"])

    def test_general_square_collects_allowed_categories_one_by_one(self):
        page = MagicMock()
        page.url = browser_cdp.BAIYING_SELECTION_SQUARE_SOURCE[1]
        settings = self.settings(
            _candidatePoolScrollSteps=1,
            categoryTree=[
                {"id": "1", "children": [
                    {"id": "101", "children": [{"id": "301"}, {"id": "302"}]},
                    {"id": "102", "children": [{"id": "303"}]},
                ]},
                {"id": "2", "children": [{"id": "201", "children": [{"id": "401"}]}]},
            ],
            excludedCategorySelection={"level1": ["2"], "level2": [], "level3": ["101__RC_CASCADER_SPLIT__302"]},
        )
        seen: list[dict[str, list[str]]] = []

        def collect(_page, query_settings, **_kwargs):
            selection = query_settings["categorySelection"]
            seen.append(selection)
            return {"candidates": [{"productId": str(len(seen)), "title": "商品"}]}

        with patch.object(browser_cdp, "_visible_baiying_prompt", return_value=("", "")), \
             patch.object(browser_cdp, "_apply_one_category_and_collect_candidates", side_effect=collect), \
             patch.object(browser_cdp, "_page_applied_filter_signature", return_value=""), \
             patch.object(browser_cdp, "_can_reuse_applied_non_category_filters", return_value=True), \
             patch.object(browser_cdp, "_get_cached_material_list", return_value=None), \
             patch.object(browser_cdp, "_rendered_material_payload", return_value={}), \
             patch.object(browser_cdp, "_promotion_items", return_value=[]), \
             patch.object(browser_cdp, "_scroll_selection_square_rows", return_value=([], [])), \
             patch.object(browser_cdp, "_row_category_evidence", return_value={"status": "known", "categoryIds": ["1", "102", "102__RC_CASCADER_SPLIT__303"]}):
            rows, _result = browser_cdp._collect_selection_square_rows(page, settings)

        self.assertEqual(seen, [{
            "level1": ["1"], "level2": ["102"], "level3": []
        }])
        self.assertEqual([row["product_id"] for row in rows], ["1"])
        self.assertEqual(rows[0]["_categoryEvidence"]["categoryIds"], ["1", "102", "102__RC_CASCADER_SPLIT__303"])
        self.assertEqual(rows[0]["_sourceCategory"]["selectionConfirmed"], True)

    def test_material_list_login_business_error_is_recorded_separately(self):
        collector = browser_cdp._MaterialListCollector(self.settings())
        request = MagicMock()
        request.url = "https://buyin.jinritemai.com/pc/selection/common/material_list"
        response = MagicMock()
        response.url = request.url
        response.request = request
        response.status = 200
        response.json.return_value = {"code": -2, "msg": "用户未登录"}
        collector.on_request(request)
        collector.on_response(response)
        self.assertIsNotNone(collector.login_error_since(0))
        self.assertEqual(collector.login_error_since(0)[1], -2)
        self.assertEqual(collector.successes, [])

    def test_rank_source_scans_allowed_category_paths_without_total_board(self):
        settings = self.settings(
            candidateLimit=1,
            _candidatePoolCollectOnly=True,
            _quotaCategoryPath=[],
            _collectorSourceKey="allianceRank",
            categoryTree=[{"id": "1", "children": [{"id": "101", "children": [{"id": "301"}]}]}],
            excludedCategorySelection={"level1": [], "level2": [], "level3": []},
            sources={"hotSpotEnabled": True, "allianceRankEnabled": True, "priority": ["hotSpot", "allianceRank"]},
        )
        seen: list[list[str]] = []

        def read_source(_page, source_key, _tree, category_tab=None, settings=None):
            self.assertEqual(source_key, "allianceRank")
            self.assertIsNone(category_tab)
            path = list(settings.get("_sourceCategoryPath") or [])
            seen.append(path)
            row = promotion_row("123456789", None, 12, 4.8)
            row["_sourceCategory"] = {"categoryIds": path, "id": path[-1], "level": len(path), "selected": True}
            return {"123456789"}, [row], {"readAt": "2026-11-17T00:00:00", "pagePath": "/rank"}

        with patch.object(browser_cdp, "_read_priority_source_product_ids", side_effect=read_source):
            membership, _warnings, results, candidates = browser_cdp._collect_priority_source_ids(object(), settings)
        self.assertEqual(seen, [["1"]])
        self.assertEqual(membership["allianceRank"], {"123456789"})
        self.assertEqual(candidates["allianceRank"][0]["_sourceCategories"][0]["categoryIds"], seen[0])
        self.assertEqual(results[0]["categoryTabTotal"], 1)

    def test_hotspot_third_exclusion_skips_the_entire_first_level_branch(self):
        settings = self.settings(
            candidateLimit=1,
            _candidatePoolCollectOnly=True,
            _quotaCategoryPath=[],
            _collectorSourceKey="hotSpot",
            categoryTree=[
                {"id": "1", "children": [
                    {"id": "101", "children": [{"id": "301"}]},
                    {"id": "102", "children": [{"id": "302"}]},
                ]},
                {"id": "2", "children": [{"id": "201", "children": [{"id": "401"}]}]},
            ],
            excludedCategorySelection={"level1": [], "level2": [], "level3": ["101__RC_CASCADER_SPLIT__301"]},
            sources={"hotSpotEnabled": True, "allianceRankEnabled": False, "priority": ["hotSpot"]},
        )
        calls: list[dict] = []

        def read_source(_page, source_key, _tree, category_tab=None, settings=None):
            calls.append({"source": source_key, "tab": category_tab, "path": settings.get("_sourceCategoryPath") if settings else None})
            if category_tab is None:
                return set(), [], {"sourceCategoryTabs": [{
                    "index": 0, "name": "类目1", "categoryId": "1", "categoryIds": ["1"],
                    "level": 1, "status": "mapped", "selected": False,
                }, {
                    "index": 1, "name": "类目2", "categoryId": "2", "categoryIds": ["2"],
                    "level": 1, "status": "mapped", "selected": False,
                }]}
            row = promotion_row("123456789", None, 12, 4.8)
            row["_sourceCategory"] = {"categoryIds": category_tab["categoryIds"], "id": category_tab["categoryIds"][-1], "level": len(category_tab["categoryIds"]), "selected": True}
            return {"123456789"}, [row], {"readAt": "2026-11-17T00:00:00", "pagePath": "/hot"}

        with patch.object(browser_cdp, "_read_priority_source_product_ids", side_effect=read_source):
            membership, _warnings, results, candidates = browser_cdp._collect_priority_source_ids(object(), settings)
        self.assertEqual(calls[1]["path"], ["2"])
        self.assertEqual(membership["hotSpot"], {"123456789"})
        self.assertEqual(candidates["hotSpot"][0]["_sourceCategories"][0]["categoryIds"], calls[1]["path"])
        self.assertEqual(results[0]["categoryTabTotal"], 1)

    def test_source_level_exclusion_blocks_even_when_row_has_exact_category(self):
        row = promotion_row("excluded", ("1", "201", "301"), 12, 4.8)
        row["_sourceCategory"] = {"name": "分类", "id": "1", "categoryIds": ["1"], "level": 1}
        settings = self.settings(excludedCategorySelection={"level1": ["1"], "level2": [], "level3": []})
        result = browser_cdp._visible_source_candidate_result(
            None, {"hotSpot": [row]}, {"hotSpot": {"excluded"}},
            [{"sourceKey": "hotSpot", "label": "热点", "status": "read", "productCount": 1}],
            [], {}, [], settings,
        )
        self.assertEqual(result["candidates"], [])
        self.assertIn("命中已核验的来源一级排除类目", result["rejectedCandidates"][0]["thresholdReasons"])

    def test_general_mode_accepts_source_category_for_root_only_exclusions(self):
        row = promotion_row("source-level", None, 3, 3.1)
        row["_sourceCategory"] = {"name": "允许类目", "id": "1", "categoryIds": ["1"], "level": 1}
        page = type("FakePage", (), {"context": object()})()
        settings = self.settings(
            thresholds={},
            excludedCategorySelection={"level1": ["2"], "level2": [], "level3": []},
        )
        with patch.object(browser_cdp, "_probe_source_card_category", return_value={
            "status": "entry_unavailable", "categoryIds": [], "reason": "来源卡片未定位"
        }), patch.object(browser_cdp, "_read_candidate_category_from_detail", side_effect=AssertionError("不应因来源一级路径重复打开详情")):
            result = browser_cdp._visible_source_candidate_result(
                page, {"hotSpot": [row]}, {"hotSpot": {"source-level"}},
                [{"sourceKey": "hotSpot", "label": "热点", "status": "read", "productCount": 1}],
                [], {}, [], settings,
            )
        self.assertEqual([item["productId"] for item in result["candidates"]], ["source-level"])
        self.assertEqual(result["candidates"][0]["categoryStatus"], "source_level")

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

    def test_samples_are_spread_across_source_categories(self):
        rows = [promotion_row(str(index), None, 8, 4.8) for index in range(1, 5)]
        for index, row in enumerate(rows, start=1):
            row["_sourceCategory"] = {"id": str(index), "categoryIds": [str(index)], "level": 1}
        samples, groups = browser_cdp._category_probe_sample_rows(rows)
        self.assertEqual(groups, 4)
        self.assertEqual([row["product_id"] for row in samples], ["1", "3", "4"])
        same_page = [promotion_row(str(index), None, 8, 4.8) for index in range(1, 40)]
        for row in same_page:
            row["_sourceReadPage"] = "query:1:initial"
        spread, groups = browser_cdp._category_probe_sample_rows(same_page)
        self.assertEqual(groups, 1)
        self.assertEqual([row["product_id"] for row in spread], ["1", "20", "39"])

    def test_detail_reads_stop_after_target_and_leave_checkpoint_cursor(self):
        rows = [promotion_row(str(index), None, 8, 4.8) for index in range(1, 41)]
        page = type("FakePage", (), {"context": object()})()
        with patch.object(browser_cdp, "_probe_source_card_category", return_value={
            "status": "category_from_source_card", "categoryIds": ["1", "201", "301"],
        }), patch.object(browser_cdp, "_read_candidate_category_from_detail", side_effect=lambda context, row, product_id, tree: {
            "productId": product_id, "categoryIds": ["1", "201", "301"], "reason": "",
        }) as detail, patch.object(browser_cdp, "_cached_detail_payload", return_value=None):
            result = browser_cdp._visible_source_candidate_result(
                page, {"hotSpot": rows}, {"hotSpot": {str(index) for index in range(1, 41)}},
                [{"sourceKey": "hotSpot", "label": "热点", "status": "read", "productCount": 40}],
                [], {}, [], self.settings(candidateLimit=1, thresholds={}),
            )
        self.assertGreaterEqual(len(result["candidates"]), 1)
        self.assertLessEqual(detail.call_count, 10)
        self.assertGreater(result["categoryEnrichment"]["deferredCount"], 0)
        self.assertLessEqual(len(result["categoryEnrichment"]["detailAttemptedProductIds"]), 12)
        self.assertEqual(result["categoryEnrichment"]["detailUnresolvedProductIds"], [])
        self.assertIn("待续核验", result["categoryEnrichment"]["summary"])

    def test_scrolled_react_cards_supply_new_id_without_extra_api_request(self):
        page = type("FakePage", (), {
            "evaluate": lambda self, script: {"moved": True, "kind": "card_ancestor", "top": 800, "height": 2000},
            "wait_for_timeout": lambda self, ms: None,
        })()
        row = promotion_row("123456789", ("1", "201", "301"), 8, 4.8)
        with patch.object(browser_cdp, "_listen_to_material_list"), patch.object(
            browser_cdp, "_remove_material_list_listeners"
        ), patch.object(browser_cdp, "_rendered_material_payload", return_value={
            "data": {"summary_promotions": [row]}
        }):
            rows, reads = browser_cdp._scroll_selection_square_rows(page, self.settings(), set(), max_steps=1)
        self.assertEqual([item["product_id"] for item in rows], ["123456789"])
        self.assertEqual(rows[0]["_categorySourceHint"], "react_card")
        self.assertEqual(reads[0]["reactNewProductIds"], 1)

    def test_source_tabs_resume_from_saved_offset_in_bounded_group(self):
        tabs = [
            {"index": index, "name": str(index), "categoryId": str(index + 1),
             "categoryIds": [str(index + 1)], "status": "mapped", "selected": False}
            for index in range(6)
        ]
        seen: list[int] = []

        def read_source(page, source_key, tree, category_tab=None, settings=None):
            if category_tab is None:
                return set(), [], {"sourceCategoryTabs": tabs, "productCount": 0}
            seen.append(category_tab["index"])
            product_id = str(100000000 + category_tab["index"])
            return {product_id}, [promotion_row(product_id, None, 8, 4.8)], {"readAt": "2026-11-15T12:00:00"}

        with patch.object(browser_cdp, "_read_priority_source_product_ids", side_effect=read_source):
            _, _, results, _ = browser_cdp._collect_priority_source_ids(
                object(), self.settings(candidateLimit=1,
                    categoryTree=[{"id": str(index + 1), "children": []} for index in range(6)],
                    sources={"hotSpotEnabled": True, "allianceRankEnabled": False, "priority": ["hotSpot"]},
                    excludedCategorySelection={"level1": [], "level2": [], "level3": []},
                    _sourceCategoryTabOffsets={"hotSpot": 2}),
            )
        self.assertEqual(seen, [2, 3, 4])
        self.assertEqual(results[0]["categoryTabNextOffset"], 5)
        self.assertEqual(results[0]["categoryTabTotal"], 6)

    def test_fatal_detail_state_stops_later_detail_reads(self):
        rows = [promotion_row(str(index), ("1", "201", "301"), None, 4.8) for index in range(1, 3)]
        page = type("FakePage", (), {"context": object()})()
        with patch.object(browser_cdp, "_read_candidate_category_from_detail", return_value={
            "productId": "1", "categoryIds": [], "reason": "账号登录状态异常", "fatal": True,
        }) as detail, patch.object(browser_cdp, "_cached_detail_payload", return_value=None):
            browser_cdp._visible_source_candidate_result(
                page, {"hotSpot": rows}, {"hotSpot": {"1", "2"}},
                [{"sourceKey": "hotSpot", "label": "热点", "status": "read", "productCount": 2}],
                [], {}, [], self.settings(),
            )
        self.assertEqual(detail.call_count, 1)


if __name__ == "__main__":
    unittest.main()
