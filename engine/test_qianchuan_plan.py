#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""付费计划自动触发的隔离回归：历史商品、幂等与提交不确定态。"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
import json
from datetime import datetime, timedelta
from pathlib import Path

import db
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
        self.connection.execute("CREATE TABLE baiying_products(product_id TEXT PRIMARY KEY, cos_ratio REAL)")
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
            "INSERT OR REPLACE INTO baiying_products(product_id,cos_ratio) VALUES (?,?)",
            (product_id, 20.0),
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
