import sys
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import browser_cdp


def candidate(product_id, category_id, score, source, pool_rank):
    return {
        "productId": product_id,
        "categoryIds": ["root", "middle", category_id],
        "analysisScore": score,
        "poolRank": pool_rank,
        "sources": [source],
    }


class VisibleSourceRecommendationTests(TestCase):
    def build_result(self, candidates, target_count=2):
        source_candidates = {
            source: [{"product_id": row["productId"]} for row in candidates if row["sources"][0] == source]
            for source in ("hotSpot", "allianceRank")
        }
        source_membership = {
            source: {row["product_id"] for row in rows}
            for source, rows in source_candidates.items()
        }
        source_results = [
            {"sourceKey": source, "status": "read", "productCount": len(rows), "parsedCandidateCount": len(rows)}
            for source, rows in source_candidates.items()
        ]
        parser_rows = [dict(row) for row in candidates]
        with patch.object(browser_cdp, "_candidate_rows", return_value=(parser_rows, [], [])), \
                patch.object(browser_cdp, "_candidate_quality_scores"), \
                patch.object(browser_cdp, "_save_category_enrichment_report", return_value="isolated-report"):
            return browser_cdp._visible_source_candidate_result(
                None,
                source_candidates,
                source_membership,
                source_results,
                [],
                {},
                [],
                {"candidateLimit": target_count, "_candidatePoolLimit": 30},
            )

    def test_high_score_category_count_precedes_average_score(self):
        result = self.build_result([
            candidate("x-1", "cat-x", 88, "hotSpot", 1),
            candidate("x-2", "cat-x", 72, "allianceRank", 2),
            candidate("y-1", "cat-y", 95, "allianceRank", 3),
            candidate("z-1", "cat-z", 59, "hotSpot", 4),
        ])

        self.assertEqual([
            (row["categoryId"], row["highScoreCount"], row["averageScore"])
            for row in result["recommendedCategories"]
        ], [("cat-x", 2, 85.0), ("cat-y", 1, 100.0), ("cat-z", 1, 64.0)])
        self.assertEqual([row["productId"] for row in result["candidates"]], ["y-1", "x-1", "x-2", "z-1"])

    def test_fallback_uses_top_a67_score_not_priority_source_order(self):
        result = self.build_result([
            candidate("lower-hotspot", "cat-low", 35, "hotSpot", 1),
            candidate("higher-alliance", "cat-high", 55, "allianceRank", 2),
        ], target_count=1)

        self.assertEqual([
            (row["categoryId"], row["highScoreCount"], row["averageScore"])
            for row in result["recommendedCategories"]
        ], [("cat-high", 1, 60.0)])

    def test_a67_candidate_score_uses_relative_metrics_and_penalizes_missing_coverage(self):
        candidates = [
            {"dataMetrics": {"salesAmount": 100}},
            {"dataMetrics": {"salesAmount": 50}},
            {"dataMetrics": {"salesAmount": 0}},
        ]

        browser_cdp._candidate_quality_scores(candidates, {})

        self.assertEqual([row["analysisScore"] for row in candidates], [16.7, 8.3, 0.0])
        self.assertEqual([row["analysisRawScore"] for row in candidates], [100.0, 50.0, 0.0])
        self.assertEqual([row["analysisCoverage"] for row in candidates], [1, 1, 1])


if __name__ == "__main__":
    import unittest
    unittest.main()
