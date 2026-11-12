#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发布后千川编排状态与幂等性的隔离 SQLite 回归。"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

import db
import post_publish_material as material
import publish_queue


class PostPublishMaterialTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "isolated.sqlite"
        db.init_db(self.path)
        self.connection = sqlite3.connect(self.path, timeout=15)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.account_id = int(db.add_account(self.connection, "隔离测试账号", "douyin", None)["account"]["id"])
        publish_queue.ensure_schema(self.connection)
        material.ensure_schema(self.connection)
        self.published_id = self.add_task("1234567890123456789", "published")
        self.review_id = self.add_task("1234567890123456790", "submitted_review")
        self.connection.commit()
        self.addCleanup(self.connection.close)

    def add_task(self, product_id: str, platform_status: str) -> int:
        cursor = self.connection.execute(
            "INSERT INTO publish_queue "
            "(account_id, product_id, product_name, video_path, video_key, title, schedule_at, "
            "platform_status, published_at, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (self.account_id, product_id, "隔离商品", "D:/isolated/test.mp4", f"{product_id}.mp4",
             "隔离标题", "2026-11-05T10:00:00", platform_status,
             "2026-11-05T10:00:00" if platform_status == "published" else None,
             "2026-11-05T10:00:00", "2026-11-05T10:00:00"),
        )
        return int(cursor.lastrowid)

    def test_published_trigger_is_once_and_duplicate_is_idempotent(self) -> None:
        first = material.begin(self.connection, self.published_id, "submit-finish")
        duplicate = material.begin(self.connection, self.published_id, "upload-finish")
        self.assertTrue(first["shouldRun"])
        self.assertFalse(first["alreadyStarted"])
        self.assertFalse(duplicate["shouldRun"])
        self.assertTrue(duplicate["alreadyStarted"])
        event_count = self.connection.execute(
            "SELECT COUNT(*) FROM post_publish_material_events WHERE publish_queue_id = ?",
            (self.published_id,),
        ).fetchone()[0]
        self.assertEqual(event_count, 1)

    def test_submitted_review_cannot_start_post_publish_flow(self) -> None:
        with self.assertRaisesRegex(ValueError, "platform_status=published"):
            material.begin(self.connection, self.review_id, "submit-finish")
        self.assertIsNone(material.get_run(self.connection, self.review_id)["run"])

    def test_missing_auth_and_no_video_are_saved_as_distinct_retriable_states(self) -> None:
        material.begin(self.connection, self.published_id, "mark-published")
        blocked = material.update_run(
            self.connection, self.published_id, "blocked_auth", "没有 QIANCHUAN 授权",
            stage="authorization_preflight",
        )
        self.assertEqual(blocked["run"]["status"], "blocked_auth")
        self.assertTrue(blocked["run"]["canRetry"])

        retry = material.retry(self.connection, self.published_id)
        self.assertTrue(retry["shouldRun"])
        self.assertEqual(retry["run"]["attemptCount"], 2)
        skipped = material.update_run(
            self.connection, self.published_id, "skipped_no_video", "接口没有返回视频",
            stage="video_query", video_count=0,
        )
        self.assertEqual(skipped["run"]["status"], "skipped_no_video")
        self.assertTrue(skipped["run"]["canRetry"])

    def test_global_switch_defaults_on_and_disabled_run_can_resume_explicitly(self) -> None:
        self.assertTrue(material._settings(self.connection)["enabled"])
        material.settings_set(self.connection, False)
        disabled = material.begin(self.connection, self.published_id, "upload-finish")
        self.assertFalse(disabled["shouldRun"])
        self.assertEqual(disabled["run"]["status"], "disabled")
        material.settings_set(self.connection, True)
        retried = material.retry(self.connection, self.published_id)
        self.assertTrue(retried["shouldRun"])
        self.assertEqual(retried["run"]["attemptCount"], 2)

    def test_partial_upload_run_is_reconciled_until_remote_target_complete(self) -> None:
        material.begin(self.connection, self.published_id, "mark-published")
        material.update_run(self.connection, self.published_id, "awaiting_upload", "已入队 3 条")
        duplicate_notification = material.begin(self.connection, self.published_id, "upload-finish")
        self.assertTrue(duplicate_notification["alreadyStarted"])
        self.assertEqual(duplicate_notification["run"]["attemptCount"], 1)
        self.connection.execute(
            "UPDATE post_publish_material_runs SET updated_at='2000-01-01T00:00:00' "
            "WHERE publish_queue_id=?", (self.published_id,),
        )
        self.connection.commit()
        # 等待态本身不构成重试原因；没有上传任务变化时不能反复增加 attempt_count。
        self.assertEqual(material.due_runs(self.connection)["rows"], [])
        self.connection.execute(
            "CREATE TABLE qianchuan_upload_jobs "
            "(id INTEGER PRIMARY KEY, account_id INTEGER, product_id TEXT, updated_at TEXT)"
        )
        self.connection.execute(
            "INSERT INTO qianchuan_upload_jobs (account_id, product_id, updated_at) "
            "VALUES (?, ?, '2026-11-07T20:00:00')",
            (self.account_id, "1234567890123456789"),
        )
        self.connection.commit()
        self.assertEqual(len(material.due_runs(self.connection)["rows"]), 1)
        resumed = material.begin(self.connection, self.published_id, "reconcile")
        self.assertTrue(resumed["shouldRun"])
        self.assertEqual(resumed["run"]["attemptCount"], 2)
        material.update_run(self.connection, self.published_id, "completed", "不同素材达标")
        self.assertEqual(material.due_runs(self.connection)["rows"], [])
        self.assertFalse(material.begin(self.connection, self.published_id, "reconcile")["shouldRun"])

    def test_needs_fill_waits_for_cart_or_upload_progress(self) -> None:
        material.begin(self.connection, self.published_id, "mark-published")
        material.update_run(self.connection, self.published_id, "needs_fill", "等待百应补位")
        self.connection.execute(
            "UPDATE post_publish_material_runs SET updated_at='2000-01-01T00:00:00' "
            "WHERE publish_queue_id=?", (self.published_id,),
        )
        self.connection.execute("""
            CREATE TABLE baiying_cart_video_downloads (
                account_id INTEGER, product_id TEXT, status TEXT,
                updated_at TEXT
            )
        """)
        self.connection.execute(
            "INSERT INTO baiying_cart_video_downloads(account_id,product_id,status,updated_at) "
            "VALUES (?,?,?,?)", (self.account_id, "1234567890123456789", "partial", "2026-11-07T20:00:00")
        )
        self.connection.commit()
        self.assertEqual(len(material.due_runs(self.connection)["rows"]), 1)
        resumed = material.begin(self.connection, self.published_id, "reconcile")
        self.assertTrue(resumed["shouldRun"])
        self.assertEqual(resumed["run"]["attemptCount"], 2)
        # reconcile 后没有新下载/上传进展时保持等待，不再被每分钟选中。
        self.assertEqual(material.due_runs(self.connection)["rows"], [])

    def test_exhausted_run_reconciles_only_after_upload_job_changes(self) -> None:
        material.begin(self.connection, self.published_id, "mark-published")
        material.update_run(self.connection, self.published_id, "candidate_exhausted", "候选不足", video_count=0)
        self.connection.execute(
            "UPDATE post_publish_material_runs SET updated_at='2000-01-01T00:00:00' "
            "WHERE publish_queue_id=?", (self.published_id,),
        )
        self.connection.commit()
        self.assertEqual(material.due_runs(self.connection)["rows"], [])
        self.connection.execute(
            "CREATE TABLE qianchuan_upload_jobs "
            "(id INTEGER PRIMARY KEY, account_id INTEGER, product_id TEXT, updated_at TEXT)"
        )
        self.connection.execute(
            "INSERT INTO qianchuan_upload_jobs (account_id, product_id, updated_at) "
            "VALUES (?, ?, '2026-11-07T20:00:00')",
            (self.account_id, "1234567890123456789"),
        )
        self.connection.commit()
        self.assertEqual(len(material.due_runs(self.connection)["rows"]), 1)
        resumed = material.begin(self.connection, self.published_id, "reconcile")
        self.assertTrue(resumed["shouldRun"])
        self.assertEqual(resumed["run"]["attemptCount"], 2)


if __name__ == "__main__":
    unittest.main()
