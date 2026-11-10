#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""付费计划自动触发的隔离回归：历史商品、幂等与提交不确定态。"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
import json
import io
import sys
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import db
import baiying_cache
import publish_queue
import qianchuan_plan as plan
from qianchuan_plan_browser import plan_id_in_list_text


class QianchuanPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "isolated.sqlite"
        db.init_db(self.path)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("CREATE TABLE baiying_products(product_id TEXT PRIMARY KEY, account_id INTEGER, cos_ratio REAL)")
        self.addCleanup(self.connection.close)
        self.account_id = int(db.add_account(self.connection, "隔离账号", "douyin", None)["account"]["id"])
        publish_queue.ensure_schema(self.connection)
        qc = self.connection.execute(
            "INSERT INTO qianchuan_accounts(qc_name,qc_id,created_at) VALUES (?,?,?)",
            ("隔离千川", "1878909235499335", db.now_text()),
        )
        self.qc_id = int(qc.lastrowid)
        self.connection.execute(
            "INSERT INTO account_qianchuan(account_row_id,qianchuan_row_id,created_at) VALUES (?,?,?)",
            (self.account_id, self.qc_id, db.now_text()),
        )
        plan.ensure_schema(self.connection)
        self.connection.commit()

    def add_published(self, product_id: str, published_at: str) -> int:
        self.connection.execute(
            "INSERT OR REPLACE INTO baiying_products(product_id,account_id,cos_ratio) VALUES (?,?,?)",
            (product_id, self.account_id, 20.0),
        )
        cursor = self.connection.execute(
            "INSERT INTO publish_queue(account_id,product_id,product_name,video_path,video_key,"
            "title,schedule_at,platform_status,published_at,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (self.account_id, product_id, "隔离商品", "D:/isolated/video.mp4",
             product_id + ".mp4", "作品标题", published_at, "published", published_at,
             published_at, published_at),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def cli(self, *args: str) -> dict:
        output = io.StringIO()
        with patch.object(sys, "argv", ["qianchuan_plan.py", "--db", str(self.path), *args]):
            with redirect_stdout(output):
                self.assertEqual(plan.main(), 0)
        return json.loads(output.getvalue())

    def test_enabling_does_not_backfill_old_paid_plans(self) -> None:
        self.add_published("1111111111111111111", "2020-01-01T00:00:00")
        settings = plan.save_settings(self.connection, self.qc_id, 50, "my_video", True, True, True, False)
        self.assertTrue(settings["activated_at"])
        self.assertEqual(plan.sync(self.connection)["created"], 0)
        self.add_published("2222222222222222222", "2099-01-01T00:00:00")
        self.assertEqual(plan.sync(self.connection)["created"], 1)
        self.assertEqual(plan.sync(self.connection)["created"], 0)
        saved = plan.save_settings(self.connection, self.qc_id, 55, "my_video", True, True, True, False)
        self.assertEqual(saved["activated_at"], settings["activated_at"])

    def test_roi_waits_for_same_account_commission_and_then_recovers(self) -> None:
        plan.save_settings(self.connection, self.qc_id, 50, "my_video", True, True, True, False)
        product_id = "5555555555555555555"
        self.add_published(product_id, "2099-01-01T00:00:00")
        self.connection.execute("UPDATE baiying_products SET account_id=? WHERE product_id=?",
                                (self.account_id + 1, product_id))
        self.connection.commit()
        self.assertEqual(plan.sync(self.connection)["created"], 1)
        job = self.connection.execute("SELECT * FROM qianchuan_plan_jobs WHERE product_id=?",
                                      (product_id,)).fetchone()
        self.assertEqual(job["status"], "blocked")
        self.assertEqual(job["roi_target"], 0)
        self.connection.execute("UPDATE baiying_products SET account_id=?,cos_ratio=NULL WHERE product_id=?",
                                (self.account_id, product_id))
        self.connection.commit()
        self.assertEqual(plan.sync(self.connection)["created"], 0)
        self.assertEqual(self.connection.execute("SELECT status FROM qianchuan_plan_jobs WHERE id=?",
                                                 (job["id"],)).fetchone()[0], "blocked")
        self.connection.execute("UPDATE baiying_products SET cos_ratio=20 WHERE product_id=?", (product_id,))
        self.connection.commit()
        self.assertEqual(plan.sync(self.connection)["created"], 0)
        recovered = self.connection.execute("SELECT * FROM qianchuan_plan_jobs WHERE id=?",
                                            (job["id"],)).fetchone()
        self.assertEqual(recovered["status"], "ready")
        self.assertEqual(recovered["commission_rate"], 20)
        self.assertEqual(recovered["roi_target"], 5)
        self.connection.execute("UPDATE baiying_products SET account_id=? WHERE product_id=?",
                                (self.account_id + 1, product_id))
        self.connection.commit()
        with self.assertRaisesRegex(ValueError, "佣金率"):
            plan.claim(self.connection, int(job["id"]))
        plan.sync(self.connection)
        blocked_again = self.connection.execute("SELECT * FROM qianchuan_plan_jobs WHERE id=?",
                                                (job["id"],)).fetchone()
        self.assertEqual((blocked_again["status"], blocked_again["commission_rate"], blocked_again["roi_target"]),
                         ("blocked", 0, 0))
        self.connection.execute("UPDATE baiying_products SET account_id=?,cos_ratio=25 WHERE product_id=?",
                                (self.account_id, product_id))
        self.connection.commit()
        plan.sync(self.connection)
        self.assertEqual(self.connection.execute("SELECT roi_target FROM qianchuan_plan_jobs WHERE id=?",
                                                 (job["id"],)).fetchone()[0], 4)
        self.connection.execute("UPDATE baiying_products SET cos_ratio=20 WHERE product_id=?", (product_id,))
        self.connection.commit()
        with self.assertRaisesRegex(ValueError, "佣金率已变化"):
            plan.claim(self.connection, int(job["id"]))
        plan.sync(self.connection)
        self.assertEqual(self.connection.execute("SELECT roi_target FROM qianchuan_plan_jobs WHERE id=?",
                                                 (job["id"],)).fetchone()[0], 5)

    def test_scheduler_lists_old_ready_jobs_and_due_supplements(self) -> None:
        plan.save_settings(self.connection, self.qc_id, 50, "my_video", True, True, True, False)
        for number in range(205):
            self.add_published(str(7000000000000000000 + number), "2099-01-01T00:00:00")
        self.assertEqual(plan.sync(self.connection)["created"], 205)
        oldest = int(self.connection.execute("SELECT min(id) FROM qianchuan_plan_jobs").fetchone()[0])
        self.connection.execute("UPDATE qianchuan_plan_jobs SET status='published' WHERE id<>?", (oldest,))
        self.connection.commit()
        self.assertEqual([row["id"] for row in self.cli("jobs", "--status", "ready")["rows"]], [oldest])
        self.assertEqual([row["id"] for row in self.cli("jobs", "--id", str(oldest))["rows"]], [oldest])
        self.assertEqual(self.cli("jobs", "--status", "ready", "--offset", "1")["rows"], [])
        now = datetime.now()
        for number, scheduled in enumerate((now - timedelta(minutes=1), now + timedelta(days=1)), start=1):
            self.connection.execute("""INSERT INTO qianchuan_plan_supplements
                (plan_job_id,remote_material_id,scheduled_at,created_at,updated_at)
                VALUES (?,?,?,?,?)""", (oldest, str(7700000000000000000 + number),
                                  scheduled.isoformat(timespec="seconds"), db.now_text(), db.now_text()))
        self.connection.commit()
        due = self.cli("supplement-jobs", "--status", "scheduled", "--due-only")["rows"]
        self.assertEqual(len(due), 1)
        self.assertEqual([row["id"] for row in self.cli("supplement-jobs", "--id", str(due[0]["id"]))["rows"]],
                         [due[0]["id"]])

    def test_cache_preserves_known_account_commission_against_unknown_capture(self) -> None:
        cache_path = Path(self.temp.name) / "cache.sqlite"
        product_id = "6666666666666666666"
        baiying_cache.upsert_product(cache_path, product_id, "商品", "", 20.0, account_id=1)
        unknown = baiying_cache.upsert_product(cache_path, product_id, "商品", "", 5.0)
        self.assertEqual(unknown["product"]["account_id"], 1)
        self.assertEqual(unknown["product"]["cos_ratio"], 20.0)
        changed = baiying_cache.upsert_product(cache_path, product_id, "商品", "", None, account_id=2)
        self.assertEqual(changed["product"]["account_id"], 2)
        self.assertIsNone(changed["product"]["cos_ratio"])
        baiying_cache.upsert_product(cache_path, product_id, "商品", "", 30.0, account_id=2)
        moved = baiying_cache.set_commodity(cache_path, product_id, "9876543210987654321", account_id=1)
        self.assertEqual(moved["product"]["account_id"], 1)
        self.assertIsNone(moved["product"]["cos_ratio"])

    def test_submission_requires_verified_remote_id_and_never_reclaims(self) -> None:
        plan.save_settings(self.connection, self.qc_id, 50, "my_video", True, True, True, False)
        self.add_published("3333333333333333333", "2099-01-01T00:00:00")
        plan.sync(self.connection)
        job_id = int(self.connection.execute("SELECT id FROM qianchuan_plan_jobs").fetchone()[0])
        self.assertEqual(plan.claim(self.connection, job_id)["job"]["status"], "submitting")
        with self.assertRaisesRegex(ValueError, "计划 ID"):
            plan.finish(self.connection, job_id, "published", True)
        plan.finish(self.connection, job_id, "needs_review", True, reason="平台回执不明确")
        with self.assertRaisesRegex(ValueError, "不会重复发布"):
            plan.claim(self.connection, job_id)
        with patch.object(plan, "usable_material_ids", side_effect=AssertionError("终态计划不应反复扫描素材")):
            self.assertEqual(plan.sync(self.connection)["created"], 0)

    def test_next_day_material_is_added_to_original_plan_once(self) -> None:
        plan.save_settings(self.connection, self.qc_id, 50, "uploaded", True, True, True, False)
        product_id = "4444444444444444444"
        self.add_published(product_id, "2099-01-01T00:00:00")
        self.connection.execute(
            "INSERT INTO qianchuan_quota_groups(name,created_at) VALUES (?,?)",
            ("隔离额度组", db.now_text()),
        )
        group_id = int(self.connection.execute(
            "SELECT id FROM qianchuan_quota_groups WHERE name=?", ("隔离额度组",)
        ).fetchone()[0])
        material_ids = ["7700000000000000001", "7700000000000000002"]
        for asset_id, material_id in enumerate(material_ids, start=1):
            self.connection.execute("""INSERT INTO qianchuan_upload_jobs
                (asset_id,account_id,qianchuan_account_id,quota_group_id,product_id,
                 file_path,material_name,status,scheduled_date,remote_material_id,
                 created_at,completed_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (asset_id, self.account_id, self.qc_id, group_id, product_id,
                 f"D:/isolated/{asset_id}.mp4", f"{asset_id}.mp4", "succeeded",
                 datetime.now().date().isoformat(), material_id,
                 db.now_text(), db.now_text(), db.now_text()),
            )
        self.connection.commit()
        self.assertEqual(plan.sync(self.connection)["created"], 1)
        job_id = int(self.connection.execute("SELECT id FROM qianchuan_plan_jobs").fetchone()[0])
        plan.claim(self.connection, job_id)
        plan.finish(self.connection, job_id, "published", True,
                    remote_id="1880000000000001", selected_material_ids=[material_ids[0]])
        self.assertEqual(plan.sync_supplements(self.connection)["created"], 1)
        self.assertEqual(plan.sync_supplements(self.connection)["created"], 0)
        supplement = self.connection.execute(
            "SELECT * FROM qianchuan_plan_supplements"
        ).fetchone()
        self.assertEqual(supplement["remote_material_id"], material_ids[1])
        self.assertGreater(datetime.fromisoformat(supplement["scheduled_at"]), datetime.now())
        with self.assertRaisesRegex(ValueError, "未到期"):
            plan.claim_supplement(self.connection, int(supplement["id"]))
        self.connection.execute(
            "UPDATE qianchuan_plan_supplements SET scheduled_at=? WHERE id=?",
            ((datetime.now() - timedelta(minutes=1)).isoformat(timespec="seconds"), supplement["id"]),
        )
        self.connection.commit()
        plan.claim_supplement(self.connection, int(supplement["id"]))
        plan.finish_supplement(self.connection, int(supplement["id"]), "applied", True)
        applied = self.connection.execute(
            "SELECT applied_material_ids_json FROM qianchuan_plan_jobs WHERE id=?", (job_id,)
        ).fetchone()[0]
        self.assertEqual(json.loads(applied), material_ids)
        self.assertEqual(plan.sync_supplements(self.connection)["created"], 0)
        with self.assertRaisesRegex(ValueError, "状态已改变"):
            plan.finish_supplement(self.connection, int(supplement["id"]), "applied", True)

    def test_created_plan_list_requires_exact_name_and_budget(self) -> None:
        name = "2026-11-08_商品全域投放_3745547582882054358"
        body = "\n".join(["商品全域投放", name, "ID：1881291633037435", "1", "商品",
                          "素材", "投放中", "每日预算", "50元", "控成本投放", "编辑",
                          "诊断", "更多"])
        self.assertEqual(plan_id_in_list_text(body, name, 50), "1881291633037435")
        self.assertEqual(plan_id_in_list_text(body, name, 100), "")
        self.assertEqual(plan_id_in_list_text(body + "\n" + body, name, 50), "")

    def test_later_upload_can_join_on_the_same_next_day(self) -> None:
        self.assertEqual(plan.supplement_due_at("2026-11-08T20:25:00",
                                                 "2026-11-09T21:00:00"),
                         "2026-11-09T21:00:00")
        self.assertEqual(plan.supplement_due_at("2026-11-08T20:25:00",
                                                 "2026-11-08T20:10:00"),
                         "2026-11-09T20:25:00")


if __name__ == "__main__":
    unittest.main()
