#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Step 18 本机定时调度与逾期补发策略的隔离数据库测试。"""

from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timedelta

import publish_queue


class PublishSchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(
            "CREATE TABLE accounts (id INTEGER PRIMARY KEY, status TEXT NOT NULL)"
        )
        self.connection.execute("INSERT INTO accounts(id, status) VALUES (1, 'active')")
        publish_queue.ensure_schema(self.connection)

    def tearDown(self) -> None:
        self.connection.close()

    def add_scheduled_task(self, schedule_at: datetime, *, task_id: int = 0) -> int:
        values = (
            1, f"product-{task_id}", f"C:/test/video-{task_id}.mp4", f"video-{task_id}",
            f"测试任务 {task_id}", schedule_at.isoformat(timespec="minutes"),
            "scheduled", "scheduled", 1, "not_uploaded", datetime.now().isoformat(),
            datetime.now().isoformat(),
        )
        cursor = self.connection.execute(
            "INSERT INTO publish_queue(account_id, product_id, video_path, video_key, title, "
            "schedule_at, mode, status, auto_publish, platform_status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", values,
        )
        return int(cursor.lastrowid)

    def test_startup_catchup_returns_overdue_task_by_default(self) -> None:
        task_id = self.add_scheduled_task(datetime.now() - timedelta(days=3), task_id=1)

        result = publish_queue.next_auto_publish_task(self.connection)

        self.assertEqual(result["task"]["id"], task_id)
        self.assertEqual(result["staleTasksSkipped"], [])
        self.assertEqual(
            self.connection.execute("SELECT status FROM publish_queue WHERE id = ?", (task_id,)).fetchone()[0],
            "scheduled",
        )

    def test_expired_catchup_is_soft_skipped_and_recent_overdue_task_remains_runnable(self) -> None:
        expired_id = self.add_scheduled_task(datetime.now() - timedelta(hours=3), task_id=1)
        recent_id = self.add_scheduled_task(datetime.now() - timedelta(minutes=30), task_id=2)
        publish_queue.set_publish_scheduler_settings(
            self.connection,
            catchup_max_late_hours=1,
        )

        result = publish_queue.next_auto_publish_task(self.connection)

        self.assertEqual(result["task"]["id"], recent_id)
        self.assertEqual([row["id"] for row in result["staleTasksSkipped"]], [expired_id])
        row = self.connection.execute(
            "SELECT status, platform_status, platform_error FROM publish_queue WHERE id = ?",
            (expired_id,),
        ).fetchone()
        self.assertEqual(row["status"], "cancelled")
        self.assertEqual(row["platform_status"], "not_uploaded")
        self.assertIn("未执行抖音平台操作", row["platform_error"])

    def test_scheduler_settings_validate_ranges_and_preserve_publish_enabled(self) -> None:
        changed = publish_queue.set_publish_scheduler_settings(
            self.connection,
            jitter_min_seconds=8,
            jitter_max_seconds=22,
            catchup_max_late_hours=48,
        )
        self.assertTrue(changed["autoPublishEnabled"])
        self.assertEqual(changed["publishJitterMinSeconds"], 8)
        self.assertEqual(changed["publishJitterMaxSeconds"], 22)
        self.assertEqual(changed["publishCatchupMaxLateHours"], 48)
        with self.assertRaisesRegex(ValueError, "最长错峰间隔"):
            publish_queue.set_publish_scheduler_settings(
                self.connection, jitter_min_seconds=30, jitter_max_seconds=10,
            )
        with self.assertRaisesRegex(ValueError, "0–8760"):
            publish_queue.set_publish_scheduler_settings(
                self.connection, catchup_max_late_hours=8761,
            )


if __name__ == "__main__":
    unittest.main()
