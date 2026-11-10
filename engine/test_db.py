# -*- coding: utf-8 -*-
"""数据库启动初始化回归：已有半初始化文件也必须补齐核心表。"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

import db


class DatabaseInitializationTests(unittest.TestCase):
    def test_init_repairs_existing_partial_database_without_deleting_data(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "ad_client.db"
            connection = sqlite3.connect(path)
            connection.execute("CREATE TABLE existing_marker (value TEXT NOT NULL)")
            connection.execute("INSERT INTO existing_marker(value) VALUES ('keep')")
            connection.commit()
            connection.close()

            result = db.init_db(path)

            self.assertTrue(result["ok"])
            self.assertTrue({"accounts", "products", "videos", "tasks"}.issubset(result["tables"]))
            check = sqlite3.connect(path)
            self.assertEqual(check.execute("SELECT value FROM existing_marker").fetchone()[0], "keep")
            self.assertIsNotNone(check.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='accounts'"
            ).fetchone())
            check.close()


if __name__ == "__main__":
    unittest.main()
