#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PostFlow 准备/提交边界的隔离回归验证，不连接真实平台。"""

from __future__ import annotations

import tempfile
import unittest
import sqlite3
from pathlib import Path

import db
import publish_queue
import selection_recovery


class PostFlowAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = self.root / "postflow.db"
        self.video = self.root / "postflow.mp4"
        self.video.write_bytes(b"isolated placeholder")
        db.init_db(self.database)
        with db.connect(self.database) as connection:
            db.add_account(connection, "PostFlow 隔离账号", "douyin", None)
        selection = selection_recovery.connect(self.database)
        try:
            selection_recovery.create_batch(selection, "postflow-batch", {"accounts": [{"id": 1, "name": "PostFlow 隔离账号"}]})
            selection_recovery.update_account(
                selection,
                "postflow-batch",
                1,
                {"state": "completed", "completed": [{
                    "productId": "postflow-product",
                    "productUrl": "https://haohuo.jinritemai.com/views/product/item2?id=postflow-product",
                    "title": "PostFlow 隔离商品",
                    "cartAdded": True,
                }]},
            )
        finally:
            selection.close()
        self.connection = sqlite3.connect(str(self.database))
        self.connection.row_factory = sqlite3.Row
        publish_queue.ensure_schema(self.connection)

    def tearDown(self) -> None:
        self.connection.close()
        self.temp.cleanup()

    def test_prepare_contract_is_explicitly_non_submitting(self) -> None:
        source = (Path(__file__).with_name("browser_cdp.py")).read_text(encoding="utf-8")
        self.assertIn('"prepareOnly": True', source)
        self.assertIn('"submissionAttempted": False', source)
        self.assertIn('"publishMode": "prepare"', source)
        self.assertIn('publish_mode == "submit"', source)

    def test_submit_requires_prepared_checkpoint_and_records_one_attempt(self) -> None:
        task = publish_queue.add_task(
            self.connection,
            1,
            "postflow-product",
            str(self.video),
            "PostFlow 隔离商品",
            "PostFlow 隔离商品",
            "2099-01-01T00:00",
            "scheduled",
        )
        publish_queue.start_upload(self.connection, task["id"])
        publish_queue.finish_upload(self.connection, task["id"], "uploaded_waiting_publish")
        self.connection.commit()
        prepared = self.connection.execute(
            "SELECT platform_status FROM publish_queue WHERE id = ?", (task["id"],)
        ).fetchone()
        self.assertEqual(prepared["platform_status"], "uploaded_waiting_publish")

        started = publish_queue.start_publish(self.connection, task["id"])
        self.assertEqual(started["id"], task["id"])
        with self.assertRaisesRegex(ValueError, "只有已上传并等待发布"):
            publish_queue.start_publish(self.connection, task["id"])
        uncertain = publish_queue.finish_publish(
            self.connection,
            task["id"],
            "submission_uncertain",
            "隔离测试：提交结果不明",
        )
        self.connection.commit()
        self.assertEqual(uncertain["platformStatus"], "submission_uncertain")
        row = self.connection.execute(
            "SELECT platform_status FROM publish_queue WHERE id = ?", (task["id"],)
        ).fetchone()
        self.assertEqual(row["platform_status"], "submission_uncertain")


if __name__ == "__main__":
    unittest.main()
