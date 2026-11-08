#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""加车下载状态的隔离数据库回归；不访问百应。"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

import baiying_cart_download_state as state


class CartVideoDownloadStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = sqlite3.connect(Path(self.temp.name) / "state.sqlite")
        self.db.row_factory = sqlite3.Row
        self.addCleanup(self.db.close)
        state.ensure_schema(self.db)

    def test_partial_resumes_and_completes_without_restarting_completed_job(self) -> None:
        state.schedule(self.db, 4, "3823892972466995628", "3823897989030740078", 10)
        self.assertTrue(state.start(self.db, 4, "3823892972466995628")["started"])
        partial = state.finish(self.db, 4, "3823892972466995628", 3, 20, "")
        self.assertEqual(partial["job"]["status"], "partial")
        self.assertEqual(partial["job"]["uniqueCount"], 3)
        self.assertTrue(partial["job"]["nextRetryAt"])
        self.assertEqual(state.due(self.db)["rows"], [])
        self.db.execute(
            "UPDATE baiying_cart_video_downloads SET next_retry_at='2000-01-01T00:00:00'"
        )
        self.assertTrue(state.start(self.db, 4, "3823892972466995628")["started"])
        complete = state.finish(self.db, 4, "3823892972466995628", 10, 20, "")
        self.assertEqual(complete["job"]["status"], "completed")
        self.assertFalse(state.start(self.db, 4, "3823892972466995628")["started"])

    def test_remote_merge_can_raise_local_target_for_replacement(self) -> None:
        state.schedule(self.db, 4, "3823892972466995628", "", 10)
        state.start(self.db, 4, "3823892972466995628")
        state.finish(self.db, 4, "3823892972466995628", 10, 20, "")
        state.schedule(self.db, 4, "3823892972466995628", "", 11)
        due = state.due(self.db)["rows"]
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0]["targetCount"], 11)
        self.assertTrue(state.start(self.db, 4, "3823892972466995628")["started"])
        self.assertEqual(state.finish(self.db, 4, "3823892972466995628", 11, 20, "")["job"]["status"], "completed")

    def test_failure_retries_and_true_shortage_is_visible(self) -> None:
        state.schedule(self.db, 4, "3823892972466995628", "", 10)
        state.start(self.db, 4, "3823892972466995628")
        failed = state.finish(self.db, 4, "3823892972466995628", 2, 20, "浏览器未登录")
        self.assertEqual(failed["job"]["status"], "failed")
        self.assertIn("浏览器未登录", failed["job"]["error"])
        self.db.execute("UPDATE baiying_cart_video_downloads SET next_retry_at='2000-01-01T00:00:00'")
        self.assertEqual(len(state.due(self.db)["rows"]), 1)
        state.start(self.db, 4, "3823892972466995628")
        exhausted = state.finish(self.db, 4, "3823892972466995628", 4, 4, "")
        self.assertEqual(exhausted["job"]["status"], "exhausted")
        self.assertEqual(state.due(self.db)["rows"], [])
        state.schedule(self.db, 4, "3823892972466995628", "", 15)
        self.assertEqual(state.due(self.db)["rows"][0]["targetCount"], 15)


if __name__ == "__main__":
    unittest.main()
