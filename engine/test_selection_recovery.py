# -*- coding: utf-8 -*-
"""选品任务恢复与配额幂等的本地 SQLite 验证。"""

import tempfile
import unittest
import sqlite3
import json
import os
import subprocess
import sys
from pathlib import Path

import selection_recovery
import selection_settings


class SelectionRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "recovery-test.db"
        selection_connection = selection_settings.connect(self.db_path)
        selection_connection.close()
        self.connection = selection_recovery.connect(self.db_path)
        selection_recovery.create_batch(self.connection, "batch-a", {
            "settingsSnapshot": {"candidateLimit": 1},
            "accounts": [{"id": 11, "name": "测试账号"}],
        })

    def tearDown(self):
        self.connection.close()
        self.temp_dir.cleanup()

    def test_running_batch_and_candidate_checkpoint_survive_reopen(self):
        selection_recovery.update_account(self.connection, "batch-a", 11, {
            "state": "in_progress",
            "stage": "cart_pending",
            "candidates": [{"productId": "product-1", "cardIndex": 3}],
            "selectedIndexes": [3],
        })
        self.connection.close()

        self.connection = selection_recovery.connect(self.db_path)
        batch = selection_recovery.latest_incomplete_batch(self.connection)
        self.assertEqual(batch["batchId"], "batch-a")
        self.assertEqual(batch["accounts"][0]["stage"], "cart_pending")
        self.assertEqual(batch["accounts"][0]["candidates"][0]["productId"], "product-1")
        self.assertEqual(batch["accounts"][0]["selectedIndexes"], [3])

    def test_quota_events_are_idempotent_on_recovery(self):
        first_cart = selection_recovery.record_quota_once(
            self.connection, "batch-a", 11, "product-1", "cart", 0
        )
        repeated_cart = selection_recovery.record_quota_once(
            self.connection, "batch-a", 11, "product-1", "cart", 0
        )
        first_video = selection_recovery.record_quota_once(
            self.connection, "batch-a", 11, "product-1", "video", 1
        )
        repeated_video = selection_recovery.record_quota_once(
            self.connection, "batch-a", 11, "product-1", "video", 1
        )

        self.assertTrue(first_cart["applied"])
        self.assertFalse(repeated_cart["applied"])
        self.assertTrue(first_video["applied"])
        self.assertFalse(repeated_video["applied"])
        self.assertEqual(repeated_video["usage"], {"products": 1, "videos": 1})

    def test_platform_failure_remains_pending_after_application_restart(self):
        selection_recovery.update_account(self.connection, "batch-a", 11, {
            "state": "failed", "stage": "candidate_fetch", "error": "模拟百应请求失败"
        })
        self.connection.close()
        self.connection = selection_recovery.connect(self.db_path)

        pending = selection_recovery.latest_incomplete_batch(self.connection)
        self.assertEqual(pending["accounts"][0]["state"], "failed")
        self.assertEqual(pending["accounts"][0]["error"], "模拟百应请求失败")

        selection_recovery.update_account(self.connection, "batch-a", 11, {
            "state": "in_progress", "error": ""
        })
        self.assertEqual(selection_recovery.latest_incomplete_batch(self.connection)["accounts"][0]["state"], "in_progress")

    def test_candidate_fetch_failure_can_safely_refresh_to_current_settings(self):
        selection_recovery.update_account(self.connection, "batch-a", 11, {
            "state": "failed", "stage": "candidate_fetch", "error": "旧类目未应用"
        })
        refreshed = selection_recovery.refresh_candidate_fetch_batch(self.connection, "batch-a", {
            "settingsSnapshot": {"candidateLimit": 3, "categorySelection": {"level1": ["current"]}},
            "accountContexts": {"11": {"settings": {"categorySelection": {"level1": ["current"]}}}},
            "reviewedAccountId": None,
            "reviewedProductIds": None,
            "accounts": [{"id": 11, "name": "测试账号"}],
        })

        self.assertEqual(refreshed["state"], "in_progress")
        self.assertEqual(refreshed["payload"]["settingsSnapshot"]["categorySelection"]["level1"], ["current"])
        self.assertEqual(refreshed["accounts"][0]["state"], "queued")
        self.assertEqual(refreshed["accounts"][0]["stage"], "queued")
        self.assertEqual(refreshed["accounts"][0]["error"], "")

    def test_candidate_settings_cannot_refresh_after_cart_checkpoint(self):
        selection_recovery.update_account(self.connection, "batch-a", 11, {
            "state": "needs_retry", "stage": "cart_pending",
            "candidates": [{"productId": "product-1"}], "selectedIndexes": [0]
        })
        with self.assertRaisesRegex(ValueError, "沿用原批次快照"):
            selection_recovery.refresh_candidate_fetch_batch(self.connection, "batch-a", {
                "settingsSnapshot": {"candidateLimit": 2},
                "accounts": [{"id": 11, "name": "测试账号"}],
            })

    def test_database_rejects_a_second_unfinished_batch(self):
        with self.assertRaises(sqlite3.IntegrityError):
            selection_recovery.create_batch(self.connection, "batch-b", {
                "settingsSnapshot": {},
                "accounts": [{"id": 12, "name": "第二账号"}],
            })

    def test_cli_persists_a_batch_from_stdin_and_returns_json(self):
        cli_db = Path(self.temp_dir.name) / "cli-recovery.db"
        payload = json.dumps({
            "settingsSnapshot": {"candidateLimit": 1},
            "accounts": [{"id": 22, "name": "命令行测试账号"}],
        })
        result = subprocess.run(
            [sys.executable, str(Path(selection_recovery.__file__)), "--db", str(cli_db),
             "batch", "create", "--batch-id", "cli-batch"],
            input=payload,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        parsed = json.loads(result.stdout)
        self.assertEqual(parsed["batch"]["batchId"], "cli-batch")
        self.assertEqual(parsed["batch"]["accounts"][0]["state"], "queued")

    def test_completed_batch_is_not_returned_as_pending(self):
        selection_recovery.update_account(self.connection, "batch-a", 11, {
            "state": "completed", "stage": "completed"
        })
        selection_recovery.update_batch(self.connection, "batch-a", "completed")
        self.assertIsNone(selection_recovery.latest_incomplete_batch(self.connection))

    def test_terminal_failed_batch_is_readable_and_not_returned_as_pending(self):
        selection_recovery.update_account(self.connection, "batch-a", 11, {
            "state": "failed",
            "stage": "candidate_fetch",
            "error": "CATEGORY_FILTER_APPLY_FAILED：自动重试耗尽",
        })

        failed = selection_recovery.update_batch(self.connection, "batch-a", "failed")

        self.assertEqual(failed["state"], "failed")
        self.assertIsNone(selection_recovery.latest_incomplete_batch(self.connection))
        history = selection_recovery.list_batches(self.connection, state="failed")
        self.assertEqual(history["total"], 1)
        self.assertEqual(history["items"][0]["batchId"], "batch-a")
        with self.assertRaisesRegex(ValueError, "批次已失败结束"):
            selection_recovery.update_batch(self.connection, "batch-a", "needs_retry")

    def test_abandon_pending_batch_preserves_checkpoint_and_frees_batch_slot(self):
        candidate_snapshot = [{"productId": "product-1", "cardIndex": 3}]
        selection_recovery.update_account(self.connection, "batch-a", 11, {
            "state": "failed",
            "stage": "cart_pending",
            "error": "平台响应失败",
            "candidates": candidate_snapshot,
            "selectedIndexes": [3],
        })
        selection_recovery.update_batch(self.connection, "batch-a", "needs_retry")

        ended = selection_recovery.abandon_batch(self.connection, "batch-a", "人工确认结束并保留记录")

        self.assertEqual(ended["state"], "abandoned")
        self.assertEqual(ended["accounts"][0]["state"], "failed")
        self.assertEqual(ended["accounts"][0]["stage"], "cart_pending")
        self.assertEqual(ended["accounts"][0]["candidates"], candidate_snapshot)
        self.assertEqual(ended["accounts"][0]["selectedIndexes"], [3])
        self.assertEqual(ended["payload"]["abandonment"]["reason"], "人工确认结束并保留记录")
        self.assertTrue(ended["payload"]["abandonment"]["checkpointPreserved"])
        self.assertIsNone(selection_recovery.latest_incomplete_batch(self.connection))
        with self.assertRaisesRegex(ValueError, "检查点只读保留"):
            selection_recovery.update_account(self.connection, "batch-a", 11, {"error": "不应更新"})
        with self.assertRaisesRegex(ValueError, "重新启用"):
            selection_recovery.update_batch(self.connection, "batch-a", "needs_retry")
        with self.assertRaisesRegex(ValueError, "不能新增配额事件"):
            selection_recovery.record_quota_once(
                self.connection, "batch-a", 11, "product-1", "cart", 0
            )

        created = selection_recovery.create_batch(self.connection, "batch-b", {
            "settingsSnapshot": {"candidateLimit": 1},
            "accounts": [{"id": 12, "name": "新批次账号"}],
        })
        self.assertEqual(created["batchId"], "batch-b")

    def test_abandon_rejects_active_or_running_batch(self):
        with self.assertRaisesRegex(ValueError, "仅允许结束 needs_retry"):
            selection_recovery.abandon_batch(self.connection, "batch-a", "过时批次")


if __name__ == "__main__":
    unittest.main()
