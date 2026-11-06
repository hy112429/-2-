#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Step 20 删除清单与误删保护的隔离数据库回归；不连接千川。"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import qianchuan_violations as violations
from db import now_text


class ViolationSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.row_factory = sqlite3.Row
        self.db.execute("CREATE TABLE qianchuan_accounts(id INTEGER PRIMARY KEY)")
        self.db.execute("INSERT INTO qianchuan_accounts(id) VALUES (7)")
        violations.ensure_schema(self.db)
        self.material_id = "1234567890123456"
        self.db.execute("INSERT INTO qianchuan_violation_materials "
                        "(qianchuan_account_id,remote_material_id,material_name,verdict,reason,detected_at,deletable,last_seen_at) "
                        "VALUES (7,?,'测试素材','违规','页面判定原因','2026-11-03',1,?)",
                        (self.material_id, now_text()))
        self.db.commit()

    def test_prepare_requires_complete_recent_scan(self) -> None:
        with self.assertRaisesRegex(ValueError, "完整刷新"):
            violations.prepare(self.db, 7, [self.material_id], self.root)
        self.db.execute("INSERT INTO qianchuan_violation_scans VALUES (7,?,0)", (now_text(),))
        self.db.commit()
        with self.assertRaisesRegex(ValueError, "完整刷新"):
            violations.prepare(self.db, 7, [self.material_id], self.root)

    def test_manifest_and_uncertain_deletion_cannot_repeat(self) -> None:
        self.db.execute("INSERT INTO qianchuan_violation_scans VALUES (7,?,1)", (now_text(),))
        self.db.commit()
        result = violations.prepare(self.db, 7, [self.material_id], self.root)
        self.assertEqual(result["count"], 1)
        manifest = Path(result["batch"]["manifest_path"])
        self.assertTrue(manifest.is_file())
        self.assertIn(self.material_id, manifest.read_text(encoding="utf-8-sig"))
        item_id = result["items"][0]["id"]
        self.db.execute("UPDATE qianchuan_violation_items SET status='needs_review' WHERE id=?", (item_id,))
        self.db.commit()
        with self.assertRaisesRegex(ValueError, "禁止重复提交"):
            violations.prepare(self.db, 7, [self.material_id], self.root)

    def test_unidentified_row_cannot_enter_batch(self) -> None:
        self.db.execute("INSERT INTO qianchuan_violation_scans VALUES (7,?,1)", (now_text(),))
        self.db.execute("UPDATE qianchuan_violation_materials SET deletable=0")
        self.db.commit()
        with self.assertRaisesRegex(ValueError, "违规列表中确认"):
            violations.prepare(self.db, 7, [self.material_id], self.root)

    def test_interrupted_deletion_isolated_without_retry(self) -> None:
        self.db.execute("INSERT INTO qianchuan_violation_scans VALUES (7,?,1)", (now_text(),))
        self.db.commit()
        result = violations.prepare(self.db, 7, [self.material_id], self.root)
        self.db.execute("UPDATE qianchuan_violation_items SET status='deleting',updated_at='2020-01-01T00:00:00' "
                        "WHERE batch_id=?", (result["batch"]["id"],))
        self.db.commit()
        recovered = violations.batch_detail(self.db, result["batch"]["id"])
        self.assertEqual(recovered["items"][0]["status"], "needs_review")
        with self.assertRaisesRegex(ValueError, "禁止重复提交"):
            violations.prepare(self.db, 7, [self.material_id], self.root)

    def test_missing_remote_row_never_clicks(self) -> None:
        with patch.object(violations, "_check_page_gate"), patch.object(violations, "_find_target_row", return_value=None):
            status, reason = violations._delete_remote(object(), self.material_id)
        self.assertEqual(status, "failed")
        self.assertIn("未点击删除", reason)


if __name__ == "__main__":
    unittest.main()
