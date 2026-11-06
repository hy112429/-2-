#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Step 17 队列的隔离数据库回归；不连接千川。"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

import db
import qianchuan_upload as qc


TODAY = date(2026, 11, 1)


class QianchuanUploadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db_path = self.root / "isolated.sqlite"
        db.init_db(self.db_path)
        self.connection = sqlite3.connect(self.db_path, timeout=15)
        self.addCleanup(self.connection.close)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.executescript("""
            CREATE TABLE assets (
                id INTEGER PRIMARY KEY, file_path TEXT NOT NULL, file_name TEXT NOT NULL,
                status TEXT NOT NULL, product_id TEXT, account_id INTEGER,
                source_type TEXT NOT NULL
            );
            CREATE TABLE publish_queue (
                id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL,
                product_id TEXT NOT NULL, platform_status TEXT NOT NULL,
                published_at TEXT
            );
        """)
        qc.ensure_schema(self.connection)
        self.account_id = self.add_account("账号A")
        self.qc_id = self.add_qc(self.account_id)
        self.connection.commit()

    def add_account(self, name: str) -> int:
        cursor = self.connection.execute(
            "INSERT INTO accounts(name, platform, status, created_at) VALUES (?, 'douyin', 'active', ?)",
            (name, "2026-11-01T00:00:00"),
        )
        return int(cursor.lastrowid)

    def add_qc(self, account_id: int) -> int:
        cursor = self.connection.execute(
            "INSERT INTO qianchuan_accounts(qc_name, login_state, created_at) "
            "VALUES (?, 'online', ?)", (f"千川-{account_id}", "2026-11-01T00:00:00"),
        )
        qc_id = int(cursor.lastrowid)
        self.connection.execute(
            "INSERT INTO account_qianchuan(account_row_id, qianchuan_row_id, created_at) "
            "VALUES (?, ?, ?)", (account_id, qc_id, "2026-11-01T00:00:00"),
        )
        return qc_id

    def add_asset(
        self, product_id: str, account_id: int | None = None,
        source_type: str = "web_download", published: bool = True,
    ) -> int:
        aid = account_id if account_id is not None else self.account_id
        file_path = self.root / f"{product_id}-{len(list(self.root.glob('*.mp4')))}.mp4"
        file_path.write_bytes(b"isolated test video")
        cursor = self.connection.execute(
            "INSERT INTO assets(file_path, file_name, status, product_id, account_id, source_type) "
            "VALUES (?, ?, 'ready', ?, ?, ?)",
            (str(file_path), file_path.name, product_id, aid, source_type),
        )
        asset_id = int(cursor.lastrowid)
        if published:
            self.connection.execute(
                "INSERT INTO publish_queue(account_id, product_id, platform_status, published_at) "
                "VALUES (?, ?, 'published', ?)", (aid, product_id, "2026-11-01T00:00:00"),
            )
        self.connection.commit()
        return asset_id

    def test_requires_confirmed_web_asset_same_account_and_published_product(self) -> None:
        good = self.add_asset("p-good")
        unknown = self.add_asset("p-unknown", source_type="unknown")
        generated = self.add_asset("p-generated", source_type="detail_generated")
        unpublished = self.add_asset("p-unpublished", published=False)
        other = self.add_account("账号B")
        self.add_qc(other)
        self.connection.commit()
        result = qc.enqueue(self.connection, self.account_id,
                            [good, unknown, generated, unpublished], today=TODAY)
        self.assertEqual([item["assetId"] for item in result["jobs"]], [good])
        self.assertEqual(len(result["rejected"]), 3)
        with self.assertRaisesRegex(ValueError, "账号归属不匹配"):
            qc._validated_asset(self.connection, good, other)
        eligible = qc.eligible_assets(self.connection, self.account_id)
        self.assertEqual({item["assetId"] for item in eligible["rows"]}, {good, unpublished})

    def test_assets_page_does_not_crash_before_publish_queue_schema_exists(self) -> None:
        asset = self.add_asset("p-no-publish-schema")
        self.connection.execute("DROP TABLE publish_queue")
        self.connection.commit()
        rows = qc.eligible_assets(self.connection, self.account_id)["rows"]
        self.assertEqual(rows[0]["assetId"], asset)
        self.assertFalse(rows[0]["eligible"])
        self.assertIn("尚未确认抖音发布", rows[0]["reason"])

    def test_ambiguous_or_missing_qianchuan_binding_refused(self) -> None:
        asset = self.add_asset("p-bind")
        self.add_qc(self.account_id)
        self.connection.commit()
        with self.assertRaisesRegex(ValueError, "绑定多个千川账户"):
            qc.enqueue(self.connection, self.account_id, [asset], today=TODAY)
        result = qc.enqueue(self.connection, self.account_id, [asset], self.qc_id, today=TODAY)
        self.assertTrue(result["ok"])
        self.assertEqual(result["qianchuanAccountId"], self.qc_id)

    def test_daily_quotas_defer_and_failed_pre_submit_releases_slot(self) -> None:
        qc.retry_config(self.connection, False)
        qc.quota_config(self.connection, self.account_id, {
            "perAccountProducts": 1, "perAccountVideos": 1, "perProductVideos": 1,
        })
        first = self.add_asset("p-one")
        second = self.add_asset("p-two")
        result = qc.enqueue(self.connection, self.account_id, [first, second], today=TODAY)
        self.assertEqual([item["scheduledDate"] for item in result["jobs"]],
                         [TODAY.isoformat(), (TODAY + timedelta(days=1)).isoformat()])
        job = result["jobs"][0]["id"]
        qc.claim(self.connection, job, today=TODAY)
        failed = qc.fail(self.connection, job, "选择文件前网络断开", "open_page")
        self.assertEqual(failed["job"]["status"], "failed")
        self.assertFalse(failed["quotaHeld"])
        third = self.add_asset("p-three")
        new_result = qc.enqueue(self.connection, self.account_id, [third], today=TODAY)
        self.assertEqual(new_result["jobs"][0]["scheduledDate"], TODAY.isoformat())
        with self.assertRaisesRegex(ValueError, "不可再次领取"):
            qc.claim(self.connection, job, today=TODAY)
        retried = qc.retry_safe(self.connection, job, today=TODAY)
        self.assertEqual(retried["job"]["status"], "queued")
        self.assertEqual(retried["job"]["scheduledDate"], (TODAY + timedelta(days=2)).isoformat())

    def test_product_quota_remaining_matches_step17_daily_reservation(self) -> None:
        qc.quota_config(self.connection, self.account_id, {
            "perProductVideos": 2, "perAccountVideos": 2, "perAccountProducts": 1,
        })
        first = self.add_asset("p-quota")
        initial = qc.product_quota_remaining(self.connection, self.account_id, "p-quota", TODAY)
        self.assertEqual(initial["remaining"], 2)
        self.assertIsNone(initial["quotaGroupId"])

        first_job = qc.enqueue(self.connection, self.account_id, [first], today=TODAY)["jobs"][0]
        self.assertEqual(first_job["scheduledDate"], TODAY.isoformat())
        after_one = qc.product_quota_remaining(self.connection, self.account_id, "p-quota", TODAY)
        self.assertEqual(after_one["remaining"], 1)

        second = self.add_asset("p-quota")
        qc.enqueue(self.connection, self.account_id, [second], today=TODAY)
        after_two = qc.product_quota_remaining(self.connection, self.account_id, "p-quota", TODAY)
        self.assertEqual(after_two["remaining"], 0)

    def test_submit_uncertain_holds_quota_and_prevents_blind_retry(self) -> None:
        qc.quota_config(self.connection, self.account_id, {"perAccountVideos": 1})
        first = self.add_asset("p-a")
        job = qc.enqueue(self.connection, self.account_id, [first], today=TODAY)["jobs"][0]["id"]
        qc.claim(self.connection, job, today=TODAY)
        result = qc.fail(self.connection, job, "回执丢失", "submit", "D:/evidence.png",
                         submit_attempted=True)
        self.assertEqual(result["job"]["status"], "needs_review")
        self.assertTrue(result["quotaHeld"])
        self.assertEqual(result["job"]["screenshotPath"], "D:/evidence.png")
        with self.assertRaisesRegex(ValueError, "不可再次领取"):
            qc.claim(self.connection, job, today=TODAY)
        with self.assertRaisesRegex(ValueError, "待核查"):
            qc.retry_safe(self.connection, job, today=TODAY)
        second = self.add_asset("p-b")
        deferred = qc.enqueue(self.connection, self.account_id, [second], today=TODAY)
        self.assertEqual(deferred["jobs"][0]["scheduledDate"],
                         (TODAY + timedelta(days=1)).isoformat())
        self.assertEqual(qc.list_events(self.connection, job)["rows"][-1]["outcome"], "needs_review")

    def test_manual_review_requires_explicit_remote_absence_before_requeue(self) -> None:
        asset = self.add_asset("p-review")
        job = qc.enqueue(self.connection, self.account_id, [asset], today=TODAY)["jobs"][0]["id"]
        qc.claim(self.connection, job, today=TODAY)
        qc.fail(self.connection, job, "提交后断连", "submit", submit_attempted=True)
        with self.assertRaisesRegex(ValueError, "至少 6 字"):
            qc.resolve_review_absent(self.connection, job, "无", today=TODAY)
        self.assertEqual(qc._job_payload(qc._job(self.connection, job))["status"], "needs_review")
        result = qc.resolve_review_absent(self.connection, job, "已在千川素材库按名称核实没有该素材", today=TODAY)
        self.assertEqual(result["job"]["status"], "queued")
        self.assertFalse(result["job"]["submitAttempted"])
        self.assertEqual(qc.list_events(self.connection, job)["rows"][-1]["stage"], "manual_review")

    def test_success_requires_remote_id_and_updates_product_status(self) -> None:
        asset = self.add_asset("p-success")
        job = qc.enqueue(self.connection, self.account_id, [asset], today=TODAY)["jobs"][0]["id"]
        qc.claim(self.connection, job, today=TODAY)
        with self.assertRaisesRegex(ValueError, "素材 ID"):
            qc.complete(self.connection, job, "")
        completed = qc.complete(self.connection, job, "remote-123")
        self.assertEqual(completed["job"]["status"], "succeeded")
        self.assertEqual(completed["job"]["remoteMaterialId"], "remote-123")
        row = self.connection.execute(
            "SELECT status, uploaded_count FROM qianchuan_product_upload_status "
            "WHERE account_id = ? AND product_id = 'p-success'", (self.account_id,),
        ).fetchone()
        self.assertEqual(tuple(row), ("uploaded", 1))
        duplicate = qc.enqueue(self.connection, self.account_id, [asset], today=TODAY)
        self.assertTrue(duplicate["ok"])
        self.assertEqual(duplicate["acceptedCount"], 0)
        self.assertEqual(duplicate["rejected"][0]["jobId"], job)

    def test_five_account_groups_are_stable_and_group_quota_applies(self) -> None:
        qc.quota_config(self.connection, 0, {"perGroupProducts": 1, "perGroupVideos": 1})
        accounts = [self.account_id]
        for index in range(2, 7):
            account = self.add_account(f"账号{index}")
            self.add_qc(account)
            accounts.append(account)
        self.connection.commit()
        jobs = []
        for account in accounts:
            asset = self.add_asset(f"p-{account}", account)
            jobs.append(qc.enqueue(self.connection, account, [asset], today=TODAY))
        group_ids = [result["quotaGroupId"] for result in jobs]
        self.assertEqual(len(set(group_ids[:5])), 1)
        self.assertNotEqual(group_ids[4], group_ids[5])
        self.assertEqual(jobs[0]["jobs"][0]["scheduledDate"], TODAY.isoformat())
        self.assertEqual(jobs[1]["jobs"][0]["scheduledDate"],
                         (TODAY + timedelta(days=1)).isoformat())
        self.assertEqual(jobs[5]["jobs"][0]["scheduledDate"], TODAY.isoformat())
        self.assertEqual(qc.list_targets(self.connection)["quotaScope"], "local_sqlite")

    def test_deferred_job_can_be_claimed_next_day(self) -> None:
        qc.quota_config(self.connection, self.account_id, {"perAccountVideos": 1})
        first = self.add_asset("p-today")
        second = self.add_asset("p-next")
        jobs = qc.enqueue(self.connection, self.account_id, [first, second], today=TODAY)["jobs"]
        with self.assertRaisesRegex(ValueError, "顺延"):
            qc.claim(self.connection, jobs[1]["id"], today=TODAY)
        claimed = qc.claim(self.connection, jobs[1]["id"], today=TODAY + timedelta(days=1))
        self.assertEqual(claimed["job"]["status"], "uploading")

    def test_claim_concurrency_limit_and_next_day_claim(self) -> None:
        qc.retry_config(self.connection, False)
        one = self.add_asset("p-a")
        two = self.add_asset("p-b")
        jobs = qc.enqueue(self.connection, self.account_id, [one, two], today=TODAY)["jobs"]
        qc.claim(self.connection, jobs[0]["id"], today=TODAY)
        with self.assertRaisesRegex(ValueError, "并发"):
            qc.claim(self.connection, jobs[1]["id"], today=TODAY)
        qc.fail(self.connection, jobs[0]["id"], "明确未选文件", "preflight")
        claimed = qc.claim(self.connection, jobs[1]["id"], today=TODAY)
        self.assertEqual(claimed["job"]["status"], "uploading")

    def test_pre_submit_auto_retry_waits_and_stops_after_three_attempts(self) -> None:
        self.assertTrue(qc.retry_config(self.connection)["autoRetryEnabled"])
        asset = self.add_asset("p-auto-retry")
        job = qc.enqueue(self.connection, self.account_id, [asset], today=TODAY)["jobs"][0]["id"]
        start = datetime(2026, 11, 1, 10, 0)
        qc.claim(self.connection, job, today=TODAY, now=start)
        for attempt, delay in enumerate((5, 15, 60), 1):
            failed = qc.fail(self.connection, job, "提交前网络断开", "navigate", now=start)
            self.assertEqual(failed["job"]["id"], job)
            self.assertEqual(failed["job"]["status"], "queued")
            self.assertEqual(failed["job"]["autoRetryCount"], attempt)
            retry_at = start + timedelta(minutes=delay)
            self.assertEqual(failed["job"]["nextRetryAt"], retry_at.isoformat(timespec="seconds"))
            with self.assertRaisesRegex(ValueError, "等待自动重试"):
                qc.claim(self.connection, job, today=TODAY, now=retry_at - timedelta(seconds=1))
            qc.claim(self.connection, job, today=TODAY, now=retry_at)
            start = retry_at
        exhausted = qc.fail(self.connection, job, "提交前网络仍异常", "navigate", now=start)
        self.assertEqual(exhausted["job"]["status"], "failed")
        self.assertEqual(exhausted["job"]["autoRetryCount"], 3)
        self.assertIsNone(exhausted["job"]["nextRetryAt"])
        self.assertEqual([row["stage"] for row in qc.list_events(self.connection, job)["rows"]].count("auto_retry"), 3)

    def test_manual_gate_and_uncertain_submit_never_auto_retry(self) -> None:
        first = self.add_asset("p-login")
        second = self.add_asset("p-uncertain")
        jobs = qc.enqueue(self.connection, self.account_id, [first, second], today=TODAY)["jobs"]
        qc.claim(self.connection, jobs[0]["id"], today=TODAY)
        manual = qc.fail(self.connection, jobs[0]["id"], "请登录", "connect", manual_action_required=True)
        self.assertEqual(manual["job"]["status"], "failed")
        self.assertEqual(manual["job"]["autoRetryCount"], 0)
        qc.claim(self.connection, jobs[1]["id"], today=TODAY)
        uncertain = qc.fail(self.connection, jobs[1]["id"], "回执未知", "verify", submit_attempted=True)
        self.assertEqual(uncertain["job"]["status"], "needs_review")
        self.assertEqual(uncertain["job"]["autoRetryCount"], 0)

    def test_disabling_retry_cancels_waiting_without_touching_regular_queue(self) -> None:
        first = self.add_asset("p-retry-off")
        second = self.add_asset("p-regular")
        jobs = qc.enqueue(self.connection, self.account_id, [first, second], today=TODAY)["jobs"]
        retry_id, regular_id = [row["id"] for row in jobs]
        qc.claim(self.connection, retry_id, today=TODAY)
        self.assertEqual(qc.fail(self.connection, retry_id, "提交前断网", "navigate")["job"]["status"], "queued")
        self.assertFalse(qc.retry_config(self.connection, False)["autoRetryEnabled"])
        self.assertEqual(qc._job_payload(qc._job(self.connection, retry_id))["status"], "failed")
        self.assertEqual(qc._job_payload(qc._job(self.connection, regular_id))["status"], "queued")
        self.assertEqual(qc.list_events(self.connection, retry_id)["rows"][-1]["outcome"], "disabled")

    def test_existing_upload_table_adds_retry_columns_without_replacing_jobs(self) -> None:
        asset = self.add_asset("p-migrate")
        job = qc.enqueue(self.connection, self.account_id, [asset], today=TODAY)["jobs"][0]["id"]
        # 旧版数据库没有新增字段时，迁移只补字段；这里用独立连接重建旧表形态。
        self.connection.execute("ALTER TABLE qianchuan_upload_jobs DROP COLUMN auto_retry_count")
        self.connection.execute("ALTER TABLE qianchuan_upload_jobs DROP COLUMN next_retry_at")
        self.connection.commit()
        qc.ensure_schema(self.connection)
        self.assertEqual(qc._job_payload(qc._job(self.connection, job))["id"], job)
        self.assertEqual(qc._job_payload(qc._job(self.connection, job))["autoRetryCount"], 0)

    def test_interrupted_job_is_isolated_and_quota_held(self) -> None:
        first = self.add_asset("p-interrupted")
        job = qc.enqueue(self.connection, self.account_id, [first], today=TODAY)["jobs"][0]["id"]
        qc.claim(self.connection, job, today=TODAY)
        self.connection.execute(
            "UPDATE qianchuan_upload_jobs SET started_at = '2026-11-01T10:00:00' WHERE id = ?",
            (job,),
        )
        self.connection.commit()
        result = qc.recover_interrupted(self.connection, 30, datetime(2026, 11, 1, 10, 31))
        self.assertEqual(result["recoveredJobIds"], [job])
        self.assertEqual(qc.list_jobs(self.connection)["rows"][0]["status"], "needs_review")
        with self.assertRaisesRegex(ValueError, "不可再次领取"):
            qc.claim(self.connection, job, today=TODAY)


if __name__ == "__main__":
    unittest.main()
