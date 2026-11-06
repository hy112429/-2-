"""Step 17 素材来源与账号归属：迁移和保守判定。"""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

if __package__:
    from engine import asset_library, download_queue
else:
    import asset_library
    import download_queue


class AssetLibraryStep17Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = str(self.root / "assets.db")

    def _account_table(self, connection: sqlite3.Connection) -> None:
        connection.execute("CREATE TABLE accounts (id INTEGER PRIMARY KEY, name TEXT UNIQUE)")
        connection.executemany(
            "INSERT INTO accounts (id, name) VALUES (?, ?)",
            [(1, "甲号"), (2, "乙号")],
        )
        connection.commit()

    def test_legacy_backfill_only_with_unique_evidence(self) -> None:
        old = sqlite3.connect(self.db)
        old.executescript("""
            CREATE TABLE assets (
                id INTEGER PRIMARY KEY, file_path TEXT UNIQUE NOT NULL,
                file_name TEXT NOT NULL, file_size INTEGER NOT NULL,
                product_id TEXT, video_id TEXT, account TEXT,
                category TEXT NOT NULL DEFAULT '未分类', status TEXT NOT NULL DEFAULT 'ready',
                thumb_path TEXT, file_mtime TEXT, scanned_at TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE accounts (id INTEGER PRIMARY KEY, name TEXT UNIQUE);
            CREATE TABLE videos (
                file_path TEXT, account_row_id INTEGER, source_url TEXT, status TEXT
            );
            INSERT INTO accounts VALUES (1, '甲号'), (2, '乙号');
            INSERT INTO assets (id, file_path, file_name, file_size, account, scanned_at, created_at)
                VALUES (1, 'D:/material/a.mp4', 'a.mp4', 1, '百应素材', 'now', 'now'),
                       (2, 'D:/material/b.mp4', 'b.mp4', 1, '甲号', 'now', 'now'),
                       (3, 'D:/material/c.mp4', 'c.mp4', 1, '百应素材', 'now', 'now');
            INSERT INTO videos VALUES
                ('D:/material/a.mp4', 2, 'https://example.test/video', 'done'),
                ('D:/material/c.mp4', 1, 'https://example.test/video', 'done'),
                ('D:/material/c.mp4', 2, 'https://example.test/video', 'done');
        """)
        old.commit()
        old.close()

        con = asset_library.connect(self.db)
        rows = con.execute(
            "SELECT id, account_id, source_type FROM assets ORDER BY id"
        ).fetchall()
        self.assertEqual([(row[0], row[1], row[2]) for row in rows], [
            (1, 2, "web_download"), (2, 1, "unknown"), (3, None, "web_download"),
        ])
        con.close()

    def test_explicit_registration_and_assignment_preserve_source(self) -> None:
        con = asset_library.connect(self.db)
        self._account_table(con)
        video = self.root / "download.mp4"
        video.write_bytes(b"mp4 content")
        registered = asset_library.register_asset(con, str(video), "web_download")
        self.assertIsNone(registered["account_id"])
        self.assertEqual(registered["source_type"], "web_download")
        assigned = asset_library.assign_asset_account(con, registered["id"], 1)
        self.assertEqual((assigned["account_id"], assigned["source_type"]), (1, "web_download"))
        with self.assertRaisesRegex(ValueError, "其他明确来源"):
            asset_library.register_asset(con, str(video), "detail_generated", account_id=1)
        with self.assertRaisesRegex(ValueError, "不能隐式改绑"):
            asset_library.assign_asset_account(con, registered["id"], 2)
        con.close()

    def test_scan_does_not_erase_registered_metadata(self) -> None:
        con = asset_library.connect(self.db)
        self._account_table(con)
        video = self.root / "2026-11-01" / "商品标题" / "商品详情图视频_9比16.mp4"
        video.parent.mkdir(parents=True)
        video.write_bytes(b"generated mp4")
        registered = asset_library.register_asset(
            con, str(video), "detail_generated", account_id=1, product_id="P1"
        )
        with patch.object(asset_library, "ensure_thumb", return_value=None):
            result = asset_library.cmd_scan(con, self.root, "ffmpeg")
        row = con.execute(
            "SELECT account_id, source_type, product_id FROM assets WHERE id = ?",
            (registered["id"],),
        ).fetchone()
        self.assertTrue(result["ok"])
        self.assertEqual(tuple(row), (1, "detail_generated", "P1"))
        con.close()

    def test_unknown_source_stays_unknown_after_account_assignment(self) -> None:
        con = asset_library.connect(self.db)
        self._account_table(con)
        video = self.root / "ambiguous.mp4"
        video.write_bytes(b"mp4")
        with patch.object(asset_library, "ensure_thumb", return_value=None):
            asset_library.cmd_scan(con, self.root, "ffmpeg")
        asset_id = con.execute("SELECT id FROM assets WHERE file_path = ?", (str(video),)).fetchone()[0]
        assigned = asset_library.assign_asset_account(con, asset_id, 1)
        self.assertEqual(assigned["source_type"], "unknown")
        con.close()

    def test_download_queue_registers_only_newly_downloaded_file(self) -> None:
        con = download_queue.connect_db(self.db)
        downloaded = self.root / "downloaded.mp4"
        task = {"id": "video-1", "url": "https://example.test/video.mp4", "dest": str(downloaded)}

        def fake_download(_session, _task, dest, _timeout):
            dest.write_bytes(b"new web video")
            return {"status": "success", "file_path": str(dest), "message": "下载完成"}

        with patch.object(download_queue, "download_one", side_effect=fake_download), patch.object(download_queue, "emit"):
            result = download_queue.run_task(None, task, 0, 1, con)
        self.assertEqual(result["status"], "success")
        source = con.execute("SELECT account_id, source_type FROM assets").fetchone()
        self.assertEqual(source, (None, "web_download"))

        skipped = self.root / "preexisting.mp4"
        skipped.write_bytes(b"existing unknown file")
        with patch.object(download_queue, "download_one", side_effect=AssertionError("不应下载")), patch.object(download_queue, "emit"):
            result = download_queue.run_task(
                None, {"id": "video-2", "url": "https://example.test/other.mp4", "dest": str(skipped)}, 0, 1, con
            )
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(con.execute("SELECT COUNT(*) FROM assets").fetchone()[0], 1)
        con.close()


if __name__ == "__main__":
    unittest.main()
