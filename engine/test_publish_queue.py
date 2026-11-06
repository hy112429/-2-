#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发布队列和创作者中心检查点的隔离 SQLite 回归验证。"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path

import db
import publish_queue
import selection_recovery


class PublishQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db_path = self.root / "isolated.db"
        self.video_a = self.root / "a.mp4"
        self.video_b = self.root / "b.mp4"
        self.video_c = self.root / "c.mp4"
        self.video_d = self.root / "d.mp4"
        self.video_a.write_bytes(b"test mp4 placeholder")
        self.video_b.write_bytes(b"test mp4 placeholder")
        self.video_c.write_bytes(b"test mp4 placeholder")
        self.video_d.write_bytes(b"test mp4 placeholder")
        db.init_db(self.db_path)
        with db.connect(self.db_path) as connection:
            db.add_account(connection, "主号A", "douyin", None)
            db.add_account(connection, "矩阵号B", "douyin", None)
            db.add_account(connection, "矩阵号C", "douyin", None)
            db.add_account(connection, "新增账号D", "douyin", None)
        self.seed_selection_history()
        self.connection = self.open_connection()
        publish_queue.ensure_schema(self.connection)

    def tearDown(self) -> None:
        self.connection.close()
        self.temp.cleanup()

    def open_connection(self):
        import sqlite3

        connection = sqlite3.connect(str(self.db_path), timeout=15)
        connection.row_factory = sqlite3.Row
        return connection

    def seed_selection_history(self) -> None:
        connection = selection_recovery.connect(self.db_path)
        try:
            selection_recovery.create_batch(
                connection,
                "publish-test-batch",
                {
                    "accounts": [
                        {"id": 1, "name": "主号A"},
                        {"id": 2, "name": "矩阵号B"},
                        {"id": 3, "name": "矩阵号C"},
                        {"id": 4, "name": "新增账号D"},
                    ]
                },
            )
            for account_id, product_id, title in (
                (1, "product-a", "A 测试商品"),
                (2, "product-b", "B 测试商品"),
                (3, "product-c", "C 测试商品"),
                (4, "product-d", "D 测试商品"),
            ):
                selection_recovery.update_account(
                    connection,
                    "publish-test-batch",
                    account_id,
                    {
                        "state": "completed",
                    "completed": [{
                        "productId": product_id,
                        "productUrl": f"https://haohuo.jinritemai.com/views/product/item2?id={product_id}",
                        "title": title,
                        "cartAdded": True,
                    }],
                    },
                )
        finally:
            connection.close()

    def planned_at(self) -> str:
        return (datetime.now() + timedelta(minutes=20)).replace(second=0, microsecond=0).isoformat(timespec="minutes")

    def test_product_names_are_loaded_for_all_accounts_and_description_defaults_to_name(self) -> None:
        options = publish_queue.selected_product_options(self.connection)
        self.assertEqual({item["account_id"] for item in options}, {1, 2, 3, 4})
        self.assertIn("product-b", {item["product_id"] for item in options})
        self.assertEqual(
            {item["account_id"]: item["product_url"] for item in options},
            {
                1: "https://haohuo.jinritemai.com/views/product/item2?id=product-a",
                2: "https://haohuo.jinritemai.com/views/product/item2?id=product-b",
                3: "https://haohuo.jinritemai.com/views/product/item2?id=product-c",
                4: "https://haohuo.jinritemai.com/views/product/item2?id=product-d",
            },
        )
        added = publish_queue.add_task(
            self.connection, 1, "product-a", str(self.video_a), "A 视频",
            "", self.planned_at(), "scheduled",
        )
        row = self.connection.execute(
            "SELECT product_name, product_url, description, platform_status FROM publish_queue WHERE id = ?",
            (added["id"],),
        ).fetchone()
        self.assertEqual(row["product_name"], "A 测试商品")
        self.assertEqual(row["product_url"], "https://haohuo.jinritemai.com/views/product/item2?id=product-a")
        self.assertEqual(row["description"], "A 测试商品")
        self.assertEqual(row["platform_status"], "not_uploaded")

    def test_generated_video_is_queued_once_for_automatic_publish(self) -> None:
        first = publish_queue.add_generated_task(
            self.connection, 1, "product-a", str(self.video_a),
        )
        second = publish_queue.add_generated_task(
            self.connection, 1, "product-a", str(self.video_a),
        )
        row = self.connection.execute(
            "SELECT account_id, product_id, product_name, video_path, title, description, mode, status, schedule_at "
            "FROM publish_queue WHERE id = ?",
            (first["id"],),
        ).fetchone()
        self.assertEqual(first["status"], "scheduled")
        self.assertFalse(first["alreadyExists"])
        self.assertTrue(second["alreadyExists"])
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(row["account_id"], 1)
        self.assertEqual(row["product_id"], "product-a")
        self.assertEqual(row["product_name"], "A 测试商品")
        self.assertEqual(row["title"], "A 测试商品")
        self.assertEqual(row["description"], "A 测试商品")
        self.assertEqual(row["mode"], "scheduled")
        self.assertEqual(row["status"], "scheduled")
        self.assertLessEqual(abs((datetime.fromisoformat(row["schedule_at"]) - datetime.now()).total_seconds()), 60)
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM publish_queue").fetchone()[0],
            1,
        )

    def test_generated_video_accepts_b_and_new_accounts_but_rejects_wrong_products(self) -> None:
        b_result = publish_queue.add_generated_task(self.connection, 2, "product-b", str(self.video_b))
        d_result = publish_queue.add_generated_task(self.connection, 4, "product-d", str(self.video_d))
        self.assertFalse(b_result["alreadyExists"])
        self.assertFalse(d_result["alreadyExists"])
        with self.assertRaisesRegex(ValueError, "找不到此商品"):
            publish_queue.add_generated_task(self.connection, 1, "unknown", str(self.video_a))
        publish_queue.add_generated_task(self.connection, 1, "product-a", str(self.video_a))
        with self.assertRaisesRegex(ValueError, "其他商品"):
            publish_queue.add_generated_task(self.connection, 1, "product-c", str(self.video_a))

    def test_generated_video_cli_command_returns_queue_task(self) -> None:
        self.connection.commit()
        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = publish_queue.main([
                "--db", str(self.db_path), "add-generated",
                "--account-id", "3", "--product-id", "product-c",
                "--video-path", str(self.video_c),
            ])
        result = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "scheduled")
        self.assertEqual(result["alreadyExists"], False)

    def test_upload_transitions_are_single_attempt_and_user_confirmed(self) -> None:
        added = publish_queue.add_task(
            self.connection, 3, "product-c", str(self.video_c), "C 视频",
            "用户编辑后的简介", self.planned_at(), "scheduled",
        )
        started = publish_queue.start_upload(self.connection, added["id"])
        self.assertEqual(started["productName"], "C 测试商品")
        self.assertEqual(started["productUrl"], "https://haohuo.jinritemai.com/views/product/item2?id=product-c")
        self.assertEqual(started["description"], "用户编辑后的简介")
        with self.assertRaisesRegex(ValueError, "避免重复上传"):
            publish_queue.start_upload(self.connection, added["id"])
        publish_queue.finish_upload(self.connection, added["id"], "uploaded_waiting_publish")
        self.connection.commit()
        with self.assertRaisesRegex(ValueError, "不能直接取消"):
            publish_queue.cancel_task(self.connection, added["id"])
        marked = publish_queue.mark_published(self.connection, added["id"])
        self.assertEqual(marked["platformStatus"], "published")
        self.connection.commit()
        with self.assertRaisesRegex(ValueError, "不能直接取消"):
            publish_queue.cancel_task(self.connection, added["id"])
        self.assertTrue(publish_queue.mark_published(self.connection, added["id"])["alreadyMarked"])

    def test_automatic_publish_result_is_persisted_once(self) -> None:
        added = publish_queue.add_task(
            self.connection, 1, "product-a", str(self.video_a), "A 自动发布",
            "A 测试商品", self.planned_at(), "scheduled",
        )
        publish_queue.start_upload(self.connection, added["id"])
        copied_url = "https://haohuo.jinritemai.com/ecommerce/trade/detail?id=product-a&from=buyin"
        result = publish_queue.finish_upload(
            self.connection, added["id"], "published", product_url=copied_url
        )
        self.connection.commit()
        row = self.connection.execute(
            "SELECT platform_status, published_at, product_url FROM publish_queue WHERE id = ?", (added["id"],)
        ).fetchone()
        self.assertEqual(result["platformStatus"], "published")
        self.assertEqual(result["productUrl"], copied_url)
        self.assertEqual(row["platform_status"], "published")
        self.assertTrue(row["published_at"])
        self.assertEqual(row["product_url"], copied_url)
        with self.assertRaisesRegex(ValueError, "避免重复上传"):
            publish_queue.start_upload(self.connection, added["id"])

    def test_upload_finish_rejects_copied_link_for_another_product(self) -> None:
        added = publish_queue.add_task(
            self.connection, 1, "product-a", str(self.video_a), "A 自动发布",
            "A 测试商品", self.planned_at(), "scheduled",
        )
        publish_queue.start_upload(self.connection, added["id"])
        with self.assertRaisesRegex(ValueError, "商品 ID"):
            publish_queue.finish_upload(
                self.connection,
                added["id"],
                "published",
                product_url="https://haohuo.jinritemai.com/views/product/item2?id=product-b",
            )
        row = self.connection.execute(
            "SELECT platform_status, product_url FROM publish_queue WHERE id = ?", (added["id"],)
        ).fetchone()
        self.assertEqual(row["platform_status"], "uploading")
        self.assertNotIn("product-b", row["product_url"])

    def test_platform_review_result_is_not_misreported_as_published(self) -> None:
        added = publish_queue.add_task(
            self.connection, 3, "product-c", str(self.video_c), "C 审核中",
            "C 测试商品", self.planned_at(), "scheduled",
        )
        publish_queue.start_upload(self.connection, added["id"])
        publish_queue.finish_upload(self.connection, added["id"], "submitted_review")
        self.connection.commit()
        row = self.connection.execute(
            "SELECT platform_status, published_at FROM publish_queue WHERE id = ?", (added["id"],)
        ).fetchone()
        self.assertEqual(row["platform_status"], "submitted_review")
        self.assertIsNone(row["published_at"])

    def test_explicit_missing_cover_block_is_isolated_and_never_saved_as_published(self) -> None:
        added = publish_queue.add_task(
            self.connection, 1, "product-a", str(self.video_a), "A 封面拦截",
            "A 测试商品", self.planned_at(), "scheduled",
        )
        publish_queue.start_upload(self.connection, added["id"])
        publish_queue.finish_upload(self.connection, added["id"], "uploaded_waiting_publish")
        self.connection.commit()
        publish_queue.start_publish(self.connection, added["id"])

        warning = "平台明确要求封面，当前任务已隔离；未记录为发布成功。"
        result = publish_queue.isolate_publish_task(self.connection, added["id"], warning)
        with self.assertRaisesRegex(ValueError, "没有进行中的发布提交"):
            publish_queue.finish_publish(self.connection, added["id"], "published")
        self.connection.commit()

        row = self.connection.execute(
            "SELECT platform_status, platform_error, published_at FROM publish_queue WHERE id = ?",
            (added["id"],),
        ).fetchone()
        self.assertTrue(result["isolated"])
        self.assertEqual(row["platform_status"], "isolated")
        self.assertEqual(row["platform_error"], warning)
        self.assertIsNone(row["published_at"])

    def test_uncertain_upload_requires_explicit_reset_before_retry(self) -> None:
        added = publish_queue.add_task(
            self.connection, 1, "product-a", str(self.video_a), "A 视频",
            "A 测试商品", self.planned_at(), "scheduled",
        )
        publish_queue.start_upload(self.connection, added["id"])
        publish_queue.finish_upload(self.connection, added["id"], "needs_review", "CDP interrupted")
        self.connection.commit()
        with self.assertRaisesRegex(ValueError, "避免重复上传"):
            publish_queue.start_upload(self.connection, added["id"])
        publish_queue.reset_upload(self.connection, added["id"])
        self.connection.commit()
        self.assertEqual(publish_queue.start_upload(self.connection, added["id"])["productId"], "product-a")

    def test_resume_upload_continues_existing_attempt_without_reenabling_new_upload(self) -> None:
        added = publish_queue.add_task(
            self.connection, 1, "product-a", str(self.video_a), "A 继续投稿",
            "A 测试商品", self.planned_at(), "scheduled",
        )
        publish_queue.start_upload(self.connection, added["id"])
        publish_queue.finish_upload(self.connection, added["id"], "needs_review", "上传后待核对")
        self.connection.commit()

        resumed = publish_queue.resume_upload(self.connection, added["id"])
        self.assertTrue(resumed["resumeExistingUploadOnly"])
        self.assertEqual(resumed["platformStatus"], "uploading")
        with self.assertRaisesRegex(ValueError, "避免重复上传"):
            publish_queue.start_upload(self.connection, added["id"])
        with self.assertRaisesRegex(ValueError, "只有待核对"):
            publish_queue.resume_upload(self.connection, added["id"])

    def test_all_accounts_are_listed_and_active_accounts_can_queue(self) -> None:
        self.assertEqual([account["id"] for account in publish_queue.list_accounts(self.connection)], [1, 2, 3, 4])
        b_task = publish_queue.add_task(
            self.connection, 2, "product-b", str(self.video_b), "B 视频",
            "B 测试商品", self.planned_at(), "scheduled",
        )
        d_task = publish_queue.add_task(
            self.connection, 4, "product-d", str(self.video_d), "D 视频",
            "D 测试商品", self.planned_at(), "scheduled",
        )
        self.assertTrue(b_task["ok"])
        self.assertTrue(d_task["ok"])
        queued_account_ids = {
            int(row[0]) for row in self.connection.execute(
                "SELECT account_id FROM publish_queue WHERE id IN (?, ?)",
                (b_task["id"], d_task["id"]),
            ).fetchall()
        }
        self.assertEqual(queued_account_ids, {2, 4})
        with self.assertRaisesRegex(ValueError, "选品记录中找不到"):
            publish_queue.add_task(
                self.connection, 1, "unknown", str(self.video_a), "未选商品",
                "未选商品", self.planned_at(), "scheduled",
            )

    def test_disabled_account_stays_visible_but_cannot_create_or_start_platform_tasks(self) -> None:
        added = publish_queue.add_task(
            self.connection, 4, "product-d", str(self.video_d), "D 视频",
            "D 测试商品", self.planned_at(), "scheduled",
        )
        self.connection.execute("UPDATE accounts SET status = 'disabled' WHERE id = 4")
        self.connection.commit()
        accounts = publish_queue.list_accounts(self.connection)
        self.assertEqual(next(account["status"] for account in accounts if account["id"] == 4), "disabled")
        with self.assertRaisesRegex(ValueError, "不存在或已停用"):
            publish_queue.add_task(
                self.connection, 4, "product-d", str(self.root / "another.mp4"), "D 重试",
                "D 测试商品", self.planned_at(), "scheduled",
            )
        with self.assertRaisesRegex(ValueError, "不存在或已停用"):
            publish_queue.start_upload(self.connection, added["id"])
        platform_status = self.connection.execute(
            "SELECT platform_status FROM publish_queue WHERE id = ?", (added["id"],)
        ).fetchone()[0]
        self.assertEqual(platform_status, "not_uploaded")

    def test_legacy_queue_schema_is_extended_incrementally(self) -> None:
        self.connection.execute("DROP TABLE publish_queue")
        self.connection.executescript("""
            CREATE TABLE publish_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL,
                product_id TEXT NOT NULL,
                video_path TEXT NOT NULL,
                video_key TEXT NOT NULL,
                title TEXT NOT NULL,
                schedule_at TEXT NOT NULL,
                mode TEXT NOT NULL DEFAULT 'manual',
                status TEXT NOT NULL DEFAULT 'awaiting_confirmation',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                cancelled_at TEXT
            );
        """)
        publish_queue.ensure_schema(self.connection)
        columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(publish_queue)")}
        self.assertTrue({"product_name", "product_url", "description", "platform_status", "published_at"}.issubset(columns))


if __name__ == "__main__":
    unittest.main()
