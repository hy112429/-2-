#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""相同远端素材 ID 的判定写回隔离测试。"""

import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from qianchuan_material_status import _read_status, save_status_for_remote


class MaterialStatusTests(unittest.TestCase):
    def test_new_material_is_searched_by_full_filename_before_remote_id(self) -> None:
        page = SimpleNamespace(url="https://qianchuan.jinritemai.com/tools/creative-management/video-library",
                               locator=MagicMock())
        search = MagicMock()
        expected = {"remoteMaterialId": "7700418842329595914", "reviewStatus": "-",
                    "materialEvaluation": "搬运"}
        with patch("qianchuan_material_status._one_visible", return_value=search), \
             patch("qianchuan_material_status._visible_status_row", return_value=expected), \
             patch("qianchuan_material_status._check_page_gate"):
            self.assertEqual(_read_status(page, expected["remoteMaterialId"], "商品_视频.mp4"), expected)
        search.fill.assert_called_once_with("商品_视频.mp4", timeout=5_000)
        search.press.assert_called_once_with("Enter", timeout=5_000)

    def test_same_remote_id_updates_only_same_advertiser_successes(self) -> None:
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.execute(
            "CREATE TABLE qianchuan_upload_jobs (id INTEGER, qianchuan_account_id INTEGER, "
            "status TEXT, remote_material_id TEXT, review_status TEXT, "
            "material_evaluation TEXT, evaluation_checked_at TEXT)"
        )
        db.executemany(
            "INSERT INTO qianchuan_upload_jobs VALUES (?, ?, ?, ?, '', '', '')",
            [(1, 4, "succeeded", "123456789"),
             (2, 4, "succeeded", "123456789"),
             (3, 5, "succeeded", "123456789"),
             (4, 4, "needs_review", "123456789")],
        )
        save_status_for_remote(db, 4, {
            "remoteMaterialId": "123456789", "reviewStatus": "审核通过",
            "materialEvaluation": "优质",
        }, "2026-11-07T20:00:00")
        values = db.execute(
            "SELECT id, review_status, material_evaluation FROM qianchuan_upload_jobs ORDER BY id"
        ).fetchall()
        self.assertEqual(values, [
            (1, "审核通过", "优质"), (2, "审核通过", "优质"),
            (3, "", ""), (4, "", ""),
        ])


if __name__ == "__main__":
    unittest.main()
