#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Step 19 回执、人工复核与过程日志的隔离数据库验证。"""

from __future__ import annotations

import csv
import io
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import db
import browser_cdp
import publish_queue


class PublishStep19Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = self.root / "isolated.db"
        self.video = self.root / "product.mp4"
        self.video.write_bytes(b"isolated test video")
        db.init_db(self.database)
        self.connection = sqlite3.connect(str(self.database))
        self.connection.row_factory = sqlite3.Row
        db.add_account(self.connection, "测试账号", "douyin", None)
        publish_queue.ensure_schema(self.connection)
        self.connection.commit()

    def tearDown(self) -> None:
        self.connection.close()
        self.temp.cleanup()

    def task(self, *, status: str = "not_uploaded", name: str = "测试商品") -> int:
        stamp = db.now_text()
        cursor = self.connection.execute(
            "INSERT INTO publish_queue (account_id, product_id, product_name, product_url, video_path, "
            "video_key, title, description, schedule_at, mode, status, auto_publish, platform_status, "
            "created_at, updated_at) VALUES (1, 'product-19', ?, "
            "'https://haohuo.jinritemai.com/views/product/item2?id=product-19', ?, ?, "
            "'测试视频', '测试简介', ?, 'scheduled', 'scheduled', 1, ?, ?, ?)",
            (name, str(self.video), str(self.video).lower(), stamp, status, stamp, stamp),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def test_manual_review_defaults_off_and_persists_without_touching_old_task(self) -> None:
        task_id = self.task(status="needs_review")
        self.assertFalse(publish_queue.auto_publish_settings(self.connection)["manualReviewEnabled"])
        saved = publish_queue.set_publish_scheduler_settings(self.connection, manual_review_enabled=True)
        self.connection.commit()
        self.assertTrue(saved["manualReviewEnabled"])
        publish_queue.ensure_schema(self.connection)
        self.assertTrue(publish_queue.auto_publish_settings(self.connection)["manualReviewEnabled"])
        row = self.connection.execute("SELECT platform_status FROM publish_queue WHERE id = ?", (task_id,)).fetchone()
        self.assertEqual(row[0], "needs_review")

    def test_review_mode_manual_retry_stops_before_submit(self) -> None:
        task_id = self.task(status="uploaded_waiting_publish")
        publish_queue.set_publish_scheduler_settings(self.connection, manual_review_enabled=True)
        self.connection.commit()
        result = publish_queue.start_manual_retry(self.connection, task_id, "uploaded_waiting_publish")
        self.assertEqual(result["route"], "ready_manual")
        self.assertEqual(result["platformStatus"], "uploaded_waiting_publish")
        events = publish_queue.publish_events(self.connection, task_id)["events"]
        self.assertEqual(events[-1]["stage"], "失败重试")
        self.assertEqual(events[-1]["outcome"], "ready_manual")

    def test_browser_final_click_rechecks_review_setting(self) -> None:
        command = {"selectionSettingsDbPath": str(self.database)}
        self.assertFalse(browser_cdp._creator_manual_review_active(command))
        publish_queue.set_publish_scheduler_settings(self.connection, manual_review_enabled=True)
        self.connection.commit()
        self.assertTrue(browser_cdp._creator_manual_review_active(command))

    def test_failure_capture_saves_only_visible_publish_page(self) -> None:
        class FakePage:
            url = "https://creator.douyin.com/creator-micro/content/post/video"

            def is_closed(self) -> bool:
                return False

            def screenshot(self, *, path: str, full_page: bool, timeout: int) -> None:
                Path(path).write_bytes(b"fake png")

        context = type("FakeContext", (), {"pages": [FakePage()]})()
        with patch.dict(os.environ, {"TOUGUANG_DATA_DIR": str(self.root)}):
            screenshot = browser_cdp._capture_publish_failure(context, 19)
        self.assertTrue(Path(screenshot).is_file())
        self.assertIn("queue-19-", Path(screenshot).name)
        self.assertEqual(Path(screenshot).read_bytes(), b"fake png")

    def test_publish_submit_events_and_receipt(self) -> None:
        task_id = self.task(status="uploaded_waiting_publish")
        publish_queue.start_publish(self.connection, task_id)
        publish_queue.finish_publish(self.connection, task_id, "submitted_review")
        self.connection.commit()
        events = publish_queue.publish_events(self.connection, task_id)["events"]
        self.assertEqual([(item["stage"], item["outcome"]) for item in events], [
            ("抖音发布", "started"), ("抖音发布", "submitted_review"),
        ])
        receipt = publish_queue.queue_list(self.connection)["rows"][0]
        self.assertEqual(receipt["platform_status"], "submitted_review")
        self.assertIsNone(receipt["published_at"])

    def test_receipt_and_events_cli_wiring(self) -> None:
        task_id = self.task()
        output = self.root / "cli-receipts.csv"
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            exit_code = publish_queue.main([
                "--db", str(self.database), "receipt-export", "--output", str(output),
            ])
        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(stdout.getvalue())["count"], 1)
        self.assertTrue(output.exists())
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            exit_code = publish_queue.main(["--db", str(self.database), "events", "--id", str(task_id)])
        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(stdout.getvalue())["events"], [])

    def test_failure_screenshot_log_and_csv_receipt(self) -> None:
        task_id = self.task(name="=HYPERLINK(\"x\")")
        publish_queue.start_upload(self.connection, task_id)
        screenshot = str(self.root / "diagnostics" / "publish" / "failed.png")
        publish_queue.finish_upload(
            self.connection, task_id, "needs_review", "测试上传失败", screenshot_path=screenshot,
        )
        self.connection.commit()
        row = publish_queue.queue_list(self.connection)["rows"][0]
        self.assertEqual(row["screenshot_path"], screenshot)
        self.assertTrue(row["execution_started_at"])
        events = publish_queue.publish_events(self.connection, task_id)["events"]
        self.assertEqual([item["outcome"] for item in events], ["started", "needs_review"])
        self.assertEqual(events[-1]["reason"], "测试上传失败")
        self.assertEqual(events[-1]["screenshot_path"], screenshot)

        output = self.root / "receipts.csv"
        receipt = publish_queue.export_receipts(self.connection, str(output))
        self.assertEqual(receipt["count"], 1)
        with output.open(encoding="utf-8-sig", newline="") as stream:
            exported = list(csv.DictReader(stream))
        self.assertEqual(exported[0]["platform_error"], "测试上传失败")
        self.assertEqual(exported[0]["screenshot_path"], screenshot)
        self.assertTrue(exported[0]["product_name"].startswith("'=HYPERLINK"))

        cleared = publish_queue.clear_failure_log(self.connection, task_id)
        self.assertTrue(cleared["ok"])
        self.assertEqual(
            self.connection.execute("SELECT platform_error FROM publish_queue WHERE id = ?", (task_id,)).fetchone()[0],
            "",
        )
        self.assertTrue(publish_queue.publish_events(self.connection, task_id)["events"][1]["cleared_at"])
        retried = publish_queue.start_manual_retry(self.connection, task_id, "needs_review")
        self.assertEqual(retried["route"], "resume_upload")
        publish_queue.finish_upload(self.connection, task_id, "uploaded_waiting_publish")
        self.connection.commit()
        history = publish_queue.publish_events(self.connection, task_id)["events"]
        self.assertEqual(history[-2]["stage"], "失败重试")
        self.assertEqual(history[-1]["outcome"], "uploaded_waiting_publish")


if __name__ == "__main__":
    unittest.main()
