"""商品全域计划只在明确准入后领取，提交结果不明不得重试。"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from db import init_db, now_text
from publish_queue import ensure_schema as ensure_publish
from qianchuan_upload import ensure_schema as ensure_upload
from qianchuan_plan import claim, ensure_schema, finish, recover, refresh, resolve_review, retry_safe, save_settings


class QianchuanPlanTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        path = Path(self.temp.name) / "test.db"
        init_db(path)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        ensure_publish(self.db)
        ensure_upload(self.db)
        ensure_schema(self.db)
        now = now_text()
        self.db.execute("INSERT INTO accounts (id,name,status,created_at) VALUES (1,'测试账号','active',?)", (now,))
        self.db.execute("INSERT INTO qianchuan_accounts (id,qc_name,qc_id,created_at) VALUES (2,'测试千川','1234567890123456',?)", (now,))
        self.db.execute("INSERT INTO account_qianchuan (account_row_id,qianchuan_row_id,created_at) VALUES (1,2,?)", (now,))
        self.db.execute("INSERT INTO qianchuan_quota_groups (id,name,created_at) VALUES (1,'测试组',?)", (now,))
        self.db.commit()
        save_settings(self.db, 2, enabled=True, daily_budget_fen=5000,
                      material_source="homepage", smart_coupon=True, aigc_enabled=False)
        self.pub_id = self._published("111222333444555")

    def tearDown(self) -> None:
        self.db.close()
        self.temp.cleanup()

    def _published(self, product: str) -> int:
        future = (datetime.now() + timedelta(seconds=2)).isoformat(timespec="seconds")
        cursor = self.db.execute(
            "INSERT INTO publish_queue (account_id,product_id,video_path,video_key,title,schedule_at,"
            "status,platform_status,published_at,created_at,updated_at) "
            "VALUES (1,?,?,?,?,?,'scheduled','published',?,?,?)",
            (product, "D:/test/video.mp4", product, "测试作品", future, future, future, future),
        )
        self.db.commit()
        return int(cursor.lastrowid)

    def test_idempotent_and_submit_uncertain_isolated(self) -> None:
        first = refresh(self.db, self.pub_id)
        second = refresh(self.db, self.pub_id)
        self.assertEqual(first["job"]["id"], second["job"]["id"])
        job_id = first["job"]["id"]
        self.assertTrue(claim(self.db, job_id)["claimed"])
        with self.assertRaisesRegex(ValueError, "待执行"):
            claim(self.db, job_id)
        failed = finish(self.db, job_id, status="failed", reason="页面超时", submit_attempted=True)
        self.assertEqual(failed["job"]["status"], "needs_review")
        self.assertEqual(refresh(self.db, self.pub_id)["job"]["status"], "needs_review")

    def test_uploaded_source_waits_for_same_account_product_remote_id(self) -> None:
        save_settings(self.db, 2, enabled=True, daily_budget_fen=5000,
                      material_source="uploaded", smart_coupon=True, aigc_enabled=False)
        waiting = refresh(self.db, self.pub_id)
        self.assertEqual(waiting["job"]["status"], "waiting_material")
        now = now_text()
        self.db.execute(
            "INSERT INTO qianchuan_upload_jobs (asset_id,account_id,qianchuan_account_id,"
            "quota_group_id,product_id,file_path,material_name,scheduled_date,status,"
            "remote_material_id,created_at,updated_at) "
            "VALUES (1,1,2,1,?,'D:/test/video.mp4','测试素材',date('now'),'succeeded','9876543210',?,?)",
            ("999999999999999", now, now),
        )
        self.db.commit()
        self.assertEqual(refresh(self.db, self.pub_id)["job"]["status"], "waiting_material")
        self.db.execute(
            "INSERT INTO qianchuan_upload_jobs (asset_id,account_id,qianchuan_account_id,"
            "quota_group_id,product_id,file_path,material_name,scheduled_date,status,"
            "remote_material_id,created_at,updated_at) "
            "VALUES (2,1,2,1,?,'D:/test/video.mp4','测试素材2',date('now'),'succeeded','1234567890',?,?)",
            ("111222333444555", now, now),
        )
        self.db.commit()
        ready = refresh(self.db, self.pub_id)
        self.assertEqual(ready["job"]["status"], "queued")
        self.assertEqual(ready["job"]["remote_material_id"], "1234567890")

    def test_no_budget_no_job(self) -> None:
        save_settings(self.db, 2, enabled=False, daily_budget_fen=5000,
                      material_source="homepage", smart_coupon=True, aigc_enabled=False)
        self.assertFalse(refresh(self.db, self.pub_id)["queued"])
        count = self.db.execute("SELECT COUNT(*) FROM qianchuan_plan_jobs").fetchone()[0]
        self.assertEqual(count, 0)

    def test_disable_after_enqueue_blocks_before_browser(self) -> None:
        job_id = refresh(self.db, self.pub_id)["job"]["id"]
        save_settings(self.db, 2, enabled=False, daily_budget_fen=5000,
                      material_source="homepage", smart_coupon=True, aigc_enabled=False)
        self.assertFalse(claim(self.db, job_id)["claimed"])
        status = self.db.execute("SELECT status FROM qianchuan_plan_jobs WHERE id=?", (job_id,)).fetchone()[0]
        self.assertEqual(status, "blocked_config")

    def test_crashed_creation_requires_remote_review(self) -> None:
        job_id = refresh(self.db, self.pub_id)["job"]["id"]
        claim(self.db, job_id)
        self.db.execute("UPDATE qianchuan_plan_jobs SET started_at='2000-01-01T00:00:00' WHERE id=?", (job_id,))
        self.assertEqual(recover(self.db)["recoveredCount"], 1)
        with self.assertRaisesRegex(ValueError, "未点击"):
            retry_safe(self.db, job_id)
        resolved = resolve_review(self.db, job_id, remote_plan_id="1234567890")
        self.assertEqual(resolved["job"]["status"], "succeeded")

    def test_pre_submit_failure_can_be_retried(self) -> None:
        job_id = refresh(self.db, self.pub_id)["job"]["id"]
        claim(self.db, job_id)
        finish(self.db, job_id, status="failed", reason="页面选择器变化", submit_attempted=False)
        self.assertEqual(retry_safe(self.db, job_id)["job"]["status"], "queued")


if __name__ == "__main__":
    unittest.main()
