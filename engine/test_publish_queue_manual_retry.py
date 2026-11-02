#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""单条手动重发检查点、隔离复位及禁止重复提交的隔离 SQLite 测试。"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
import io
import json
from contextlib import redirect_stdout
from pathlib import Path

import db
import publish_queue


class ManualRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "publish.db"
        self.video_path = Path(self.temp.name) / "item.mp4"
        self.video_path.write_bytes(b"isolated placeholder")
        db.init_db(self.db_path)
        self.connection = sqlite3.connect(str(self.db_path))
        self.connection.row_factory = sqlite3.Row
        db.add_account(self.connection, "主号A", "douyin", None)
        publish_queue.ensure_schema(self.connection)
        self.connection.commit()
        self.sequence = 0

    def tearDown(self) -> None:
        self.connection.close()
        self.temp.cleanup()

    def make_task(
        self, platform_status: str, *, auto_publish: bool = True,
        isolated_from_status: str = "", status: str = "scheduled",
    ) -> int:
        self.sequence += 1
        product_id = f"product-{self.sequence}"
        timestamp = db.now_text()
        cursor = self.connection.execute(
            "INSERT INTO publish_queue (account_id, product_id, product_name, product_url, "
            "video_path, video_key, title, description, schedule_at, mode, status, auto_publish, "
            "platform_status, isolated_from_status, created_at, updated_at) "
            "VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, 'scheduled', ?, ?, ?, ?, ?, ?)",
            (
                product_id, f"商品{self.sequence}",
                f"https://haohuo.jinritemai.com/views/product/item2?id={product_id}",
                str(self.video_path), f"video-{self.sequence}", f"视频{self.sequence}",
                f"商品{self.sequence}", timestamp, status, int(auto_publish),
                platform_status, isolated_from_status, timestamp, timestamp,
            ),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def current_status(self, task_id: int) -> str:
        row = self.connection.execute(
            "SELECT platform_status FROM publish_queue WHERE id = ?", (task_id,),
        ).fetchone()
        return str(row[0])

    def test_cli_plan_and_start_return_same_task_and_route(self) -> None:
        task_id = self.make_task("needs_review")
        output = io.StringIO()
        with redirect_stdout(output):
            code = publish_queue.main([
                "--db", str(self.db_path), "manual-retry-plan", "--id", str(task_id),
            ])
        self.assertEqual(code, 0)
        plan = json.loads(output.getvalue())
        self.assertEqual(plan["task"]["id"], task_id)
        self.assertEqual(plan["previousStatus"], "needs_review")

        output = io.StringIO()
        with redirect_stdout(output):
            code = publish_queue.main([
                "--db", str(self.db_path), "manual-retry-start", "--id", str(task_id),
                "--expected-status", plan["previousStatus"],
            ])
        self.assertEqual(code, 0)
        started = json.loads(output.getvalue())
        self.assertEqual(started["task"]["id"], task_id)
        self.assertEqual(started["route"], "resume_upload")
        self.assertEqual(self.current_status(task_id), "uploading")

    def test_upload_checkpoint_prefers_existing_post_and_ignores_other_review_tasks(self) -> None:
        blocker = self.make_task("submission_uncertain")
        task_id = self.make_task("needs_review", auto_publish=True)
        plan = publish_queue.manual_retry_plan(self.connection, task_id)
        self.assertEqual(plan["previousStatus"], "needs_review")
        self.assertFalse(plan["needsConfirmNotSubmitted"])
        self.assertTrue(plan["task"]["autoPublish"])

        started = publish_queue.start_manual_retry(self.connection, task_id, "needs_review")
        self.assertEqual(started["route"], "resume_upload")
        self.assertEqual(started["task"]["productId"], "product-2")
        self.assertEqual(self.current_status(task_id), "uploading")
        self.assertEqual(self.current_status(blocker), "submission_uncertain")

    def test_explicit_no_draft_falls_back_to_new_upload_only_after_required_confirmations(self) -> None:
        task_id = self.make_task("uploading")
        started = publish_queue.start_manual_retry(
            self.connection, task_id, "uploading", confirmed_no_draft=True,
        )
        self.assertEqual(started["route"], "upload_new")
        self.assertEqual(self.current_status(task_id), "uploading")

        submitted = self.make_task("submission_uncertain")
        with self.assertRaisesRegex(ValueError, "请先确认创作者中心"):
            publish_queue.start_manual_retry(
                self.connection, submitted, "submission_uncertain",
                confirmed_no_draft=True,
            )
        self.assertEqual(self.current_status(submitted), "submission_uncertain")
        restarted = publish_queue.start_manual_retry(
            self.connection, submitted, "submission_uncertain",
            confirmed_not_submitted=True, confirmed_no_draft=True,
        )
        self.assertEqual(restarted["route"], "upload_new")
        self.assertEqual(self.current_status(submitted), "uploading")
        waiting = self.make_task("uploaded_waiting_publish")
        with self.assertRaisesRegex(ValueError, "须同时确认"):
            publish_queue.start_manual_retry(
                self.connection, waiting, "uploaded_waiting_publish", confirmed_no_draft=True,
            )
        self.assertEqual(self.current_status(waiting), "uploaded_waiting_publish")

    def test_uploaded_checkpoint_follows_original_auto_publish(self) -> None:
        auto_id = self.make_task("uploaded_waiting_publish", auto_publish=True)
        manual_id = self.make_task("uploaded_waiting_publish", auto_publish=False)
        automatic = publish_queue.start_manual_retry(self.connection, auto_id, "uploaded_waiting_publish")
        manual = publish_queue.start_manual_retry(self.connection, manual_id, "uploaded_waiting_publish")
        self.assertEqual((automatic["route"], automatic["platformStatus"]), ("submit", "submitting"))
        self.assertEqual((manual["route"], manual["platformStatus"]), ("ready_manual", "uploaded_waiting_publish"))
        self.assertTrue(automatic["task"]["autoPublish"])
        self.assertFalse(manual["task"]["autoPublish"])

    def test_submission_uncertain_requires_confirmation_and_only_resumes_submit(self) -> None:
        task_id = self.make_task("submission_uncertain")
        self.assertTrue(publish_queue.manual_retry_plan(self.connection, task_id)["needsConfirmNotSubmitted"])
        with self.assertRaisesRegex(ValueError, "确认创作者中心"):
            publish_queue.start_manual_retry(self.connection, task_id, "submission_uncertain")
        self.assertEqual(self.current_status(task_id), "submission_uncertain")
        started = publish_queue.start_manual_retry(
            self.connection, task_id, "submission_uncertain", confirmed_not_submitted=True,
        )
        self.assertEqual((started["route"], self.current_status(task_id)), ("submit", "submitting"))

    def test_isolation_origin_preserved_and_same_task_resumes(self) -> None:
        upload_id = self.make_task("uploading")
        submit_id = self.make_task("submitting", auto_publish=False)
        publish_queue.isolate_upload_task(self.connection, upload_id, "核验失败")
        publish_queue.isolate_publish_task(self.connection, submit_id, "封面阻断")
        self.assertEqual(publish_queue.manual_retry_plan(self.connection, upload_id)["isolationFromStatus"], "uploading")
        self.assertEqual(publish_queue.manual_retry_plan(self.connection, submit_id)["isolationFromStatus"], "submitting")
        self.assertFalse(publish_queue.manual_retry_plan(self.connection, upload_id)["needsConfirmNotSubmitted"])
        self.assertTrue(publish_queue.manual_retry_plan(self.connection, submit_id)["needsConfirmNotSubmitted"])
        upload_retry = publish_queue.start_manual_retry(self.connection, upload_id, "isolated")
        submit_retry = publish_queue.start_manual_retry(
            self.connection, submit_id, "isolated", confirmed_not_submitted=True,
        )
        self.assertEqual(upload_retry["route"], "resume_upload")
        self.assertEqual(submit_retry["route"], "ready_manual")
        self.assertEqual(self.current_status(submit_id), "uploaded_waiting_publish")
        provenance = self.connection.execute(
            "SELECT isolated_from_status FROM publish_queue WHERE id = ?", (upload_id,),
        ).fetchone()[0]
        self.assertEqual(provenance, "")

    def test_legacy_isolation_requires_submission_confirmation(self) -> None:
        task_id = self.make_task("isolated")
        plan = publish_queue.manual_retry_plan(self.connection, task_id)
        self.assertEqual(plan["isolationFromStatus"], "")
        self.assertTrue(plan["needsConfirmNotSubmitted"])
        with self.assertRaisesRegex(ValueError, "确认创作者中心"):
            publish_queue.start_manual_retry(self.connection, task_id, "isolated")
        started = publish_queue.start_manual_retry(
            self.connection, task_id, "isolated", confirmed_not_submitted=True,
        )
        self.assertEqual(started["route"], "resume_upload")

    def test_published_review_cancelled_and_old_snapshot_rejected(self) -> None:
        for platform_status in ("published", "submitted_review", "not_uploaded"):
            task_id = self.make_task(platform_status)
            with self.subTest(platform_status=platform_status), self.assertRaises(ValueError):
                publish_queue.manual_retry_plan(self.connection, task_id)
        cancelled = self.make_task("isolated", status="cancelled")
        with self.assertRaisesRegex(ValueError, "已确认排期"):
            publish_queue.manual_retry_plan(self.connection, cancelled)
        stale = self.make_task("needs_review")
        with self.assertRaisesRegex(ValueError, "状态已变化"):
            publish_queue.start_manual_retry(self.connection, stale, "uploading")
        self.assertEqual(self.current_status(stale), "needs_review")

    def test_disabled_account_and_savepoint_rollback(self) -> None:
        task_id = self.make_task("needs_review")
        self.connection.execute("BEGIN IMMEDIATE")
        with self.assertRaisesRegex(ValueError, "状态已变化"):
            publish_queue.start_manual_retry(self.connection, task_id, "isolated")
        self.assertTrue(self.connection.in_transaction)
        self.connection.rollback()
        self.connection.execute("UPDATE accounts SET status = 'disabled' WHERE id = 1")
        self.connection.commit()
        with self.assertRaises(ValueError):
            publish_queue.manual_retry_plan(self.connection, task_id)


if __name__ == "__main__":
    unittest.main()
