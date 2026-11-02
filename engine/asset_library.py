#!/usr/bin/env python3
"""本地素材库引擎（素材与工具导航）。

登记下载目录里的成品素材到 SQLite（与 db.py 同一个库），支持检索、
缩略图、删除与标签。

表设计（在 db.py 的库中新增两张表，由本脚本幂等建表）：

    assets                   素材主表
      id            INTEGER  PRIMARY KEY AUTOINCREMENT
      file_path     TEXT     NOT NULL UNIQUE      -- 成品文件绝对路径（唯一键）
      file_name     TEXT     NOT NULL             -- 文件名
      file_size     INTEGER  NOT NULL             -- 字节
      product_id    TEXT                          -- 目录结构解析出的商品 ID
      video_id      TEXT                          -- 文件名解析出的视频 ID
      account       TEXT                          -- 账号目录名
      account_id    INTEGER                       -- 明确归属的抖音账号 ID；未知时为空
      source_type   TEXT     NOT NULL DEFAULT 'unknown' -- web_download / detail_generated / unknown
      category      TEXT     NOT NULL DEFAULT '未分类'
      status        TEXT     NOT NULL DEFAULT 'ready'   -- ready / archived
      thumb_path    TEXT                          -- 缩略图缓存绝对路径
      file_mtime    TEXT                          -- 文件修改时间（增量扫描用）
      scanned_at    TEXT     NOT NULL
      created_at    TEXT     NOT NULL

    asset_tags               标签（多对一）
      asset_id      INTEGER  REFERENCES assets(id) ON DELETE CASCADE
      tag           TEXT     NOT NULL
      PRIMARY KEY (asset_id, tag)

目录约定（下载成品）：
    {root}/{账号}/{YYYY-MM-DD}/{商品ID}/{商品ID}_{视频ID}_{序号}_{时间戳}.mp4
    以及开发期产物 {root}/_queue_test/xxx.mp4（无账号结构，分类记为「未分类」）

缩略图：
    生成：ffmpeg 抽第 1 帧，scale 宽 240（等比），JPEG q5
    缓存：{root}/.thumbs/{path_md5[:16]}_{mtime_epoch}.jpg
          文件名含 mtime → 源文件变化后自动重新生成；存在即复用
    读取：由主进程 assets:getThumb 转 base64 给渲染进程

命令：
    python asset_library.py scan [--root DIR] [--db PATH]
    python asset_library.py list [--account NAME] [--product-id X] [--tag T]
                               [--date YYYY-MM-DD] [--status ready] [--limit N]
    python asset_library.py accounts
    python asset_library.py register --file-path FILE --source-type web_download
                                    [--account-id N] [--product-id ID]
    python asset_library.py assign-account --id N --account-id N
    python asset_library.py delete --id N
    python asset_library.py tags
    python asset_library.py categories
    python asset_library.py tag-set --id N --tags a,b,c
    python asset_library.py make-thumb --id N

事件（NDJSON）：
    {"type":"stage","stage":"扫描中 3/10","percent":30}
    {"type":"result","data":{ ...命令结果... }}
"""

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from .client_paths import DEFAULT_DB_PATH, DEFAULT_DOWNLOAD_ROOT
except ImportError:
    from client_paths import DEFAULT_DB_PATH, DEFAULT_DOWNLOAD_ROOT

DEFAULT_ROOT = DEFAULT_DOWNLOAD_ROOT
UNASSIGNED_ACCOUNT_FILTER = "__unassigned__"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS assets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path   TEXT    NOT NULL UNIQUE,
    file_name   TEXT    NOT NULL,
    file_size   INTEGER NOT NULL,
    product_id  TEXT,
    video_id    TEXT,
    account     TEXT,
    account_id  INTEGER,
    source_type TEXT    NOT NULL DEFAULT 'unknown',
    category    TEXT    NOT NULL DEFAULT '未分类',
    status      TEXT    NOT NULL DEFAULT 'ready',
    thumb_path  TEXT,
    file_mtime  TEXT,
    scanned_at  TEXT    NOT NULL,
    created_at  TEXT    NOT NULL
);
CREATE TABLE IF NOT EXISTS asset_tags (
    asset_id    INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    tag         TEXT    NOT NULL,
    PRIMARY KEY (asset_id, tag)
);
CREATE INDEX IF NOT EXISTS idx_assets_product ON assets(product_id);
CREATE INDEX IF NOT EXISTS idx_assets_status   ON assets(status);
CREATE INDEX IF NOT EXISTS idx_assets_category ON assets(category);
CREATE INDEX IF NOT EXISTS idx_assets_tags_tag ON asset_tags(tag);
"""

SOURCE_TYPES = ("web_download", "detail_generated", "unknown")


def emit(payload: Dict[str, Any], exit_code: int = 0) -> int:
    print(json.dumps(payload, ensure_ascii=False, default=str))
    return exit_code


def now_text() -> str:
    return datetime.now().isoformat(timespec="seconds")


def connect(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")
    ensure_schema(con)
    return con


def table_exists(con: sqlite3.Connection, name: str) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone() is not None


def ensure_schema(con: sqlite3.Connection) -> None:
    """兼容旧素材表；只补列，不覆盖已登记的来源或归属。"""
    con.executescript(SCHEMA_SQL)
    columns = {row[1] for row in con.execute("PRAGMA table_info(assets)")}
    migrated = False
    if "account_id" not in columns:
        con.execute("ALTER TABLE assets ADD COLUMN account_id INTEGER")
        migrated = True
    if "source_type" not in columns:
        con.execute("ALTER TABLE assets ADD COLUMN source_type TEXT NOT NULL DEFAULT 'unknown'")
        migrated = True
    con.execute("CREATE INDEX IF NOT EXISTS idx_assets_account_id ON assets(account_id)")
    if migrated:
        backfill_legacy_metadata(con)
    con.commit()


def account_name(con: sqlite3.Connection, account_id: int) -> str:
    if not table_exists(con, "accounts"):
        raise ValueError("账号表尚未建立，无法确认素材归属")
    row = con.execute("SELECT name FROM accounts WHERE id = ?", (account_id,)).fetchone()
    if row is None:
        raise ValueError(f"账号不存在：{account_id}")
    return str(row[0])


def infer_legacy_metadata(
    con: sqlite3.Connection, file_path: str, account_label: Optional[str]
) -> tuple[Optional[int], str]:
    """仅从精确文件路径和唯一账号名回填；商品 ID/目录名本身不证明来源。"""
    account_ids: set[int] = set()
    source_type = "unknown"
    has_accounts = table_exists(con, "accounts")
    if has_accounts and account_label and account_label.strip():
        matches = con.execute(
            "SELECT id FROM accounts WHERE TRIM(name) = ?", (account_label.strip(),)
        ).fetchall()
        if len(matches) == 1:
            account_ids.add(int(matches[0][0]))
        elif len(matches) > 1:
            return None, "unknown"
    if table_exists(con, "videos"):
        video_columns = {row[1] for row in con.execute("PRAGMA table_info(videos)")}
        if {"file_path", "account_row_id", "source_url", "status"} <= video_columns:
            videos = con.execute(
                "SELECT account_row_id, source_url, status FROM videos WHERE file_path = ?",
                (file_path,),
            ).fetchall()
            if has_accounts:
                account_ids.update(
                    int(row[0]) for row in videos
                    if row[0] is not None and con.execute(
                        "SELECT 1 FROM accounts WHERE id = ?", (row[0],)
                    ).fetchone() is not None
                )
            if videos and all(
                str(row[2]) == "done" and str(row[1] or "").startswith(("https://", "http://"))
                for row in videos
            ):
                source_type = "web_download"
    return (next(iter(account_ids)) if len(account_ids) == 1 else None), source_type


def backfill_legacy_metadata(con: sqlite3.Connection) -> None:
    for row in con.execute(
        "SELECT id, file_path, account, account_id, source_type FROM assets"
    ).fetchall():
        if row[3] is not None and str(row[4] or "unknown") != "unknown":
            continue
        inferred_account_id, inferred_source = infer_legacy_metadata(con, str(row[1]), row[2])
        inferred_name = account_name(con, inferred_account_id) if inferred_account_id is not None else None
        con.execute(
            "UPDATE assets SET account_id = COALESCE(account_id, ?), "
            "account = CASE WHEN account_id IS NULL AND ? IS NOT NULL THEN ? ELSE account END, "
            "source_type = CASE WHEN source_type = 'unknown' THEN ? ELSE source_type END WHERE id = ?",
            (inferred_account_id, inferred_name, inferred_name, inferred_source, row[0]),
        )


def register_asset(
    con: sqlite3.Connection, file_path: str, source_type: str,
    account_id: Optional[int] = None, product_id: Optional[str] = None,
) -> Dict[str, Any]:
    """由确定的生产者登记来源；网页下载在账号未明确时允许暂不归属。"""
    if source_type not in SOURCE_TYPES or source_type == "unknown":
        raise ValueError("登记来源必须是 web_download 或 detail_generated")
    if source_type == "detail_generated" and account_id is None:
        raise ValueError("详情图生成素材必须指定所属账号 ID")
    fp = Path(file_path).expanduser().resolve(strict=True)
    if not fp.is_file() or fp.suffix.lower() != ".mp4" or fp.stat().st_size <= 0:
        raise ValueError("素材必须是非空 MP4 文件")
    path = str(fp)
    name = account_name(con, account_id) if account_id is not None else None
    row = con.execute(
        "SELECT id, account_id, account, product_id, source_type FROM assets WHERE file_path = ?",
        (path,),
    ).fetchone()
    if row is not None:
        current_source = str(row[4] or "unknown")
        if current_source not in ("unknown", source_type):
            raise ValueError(f"素材已有其他明确来源：{current_source}")
        if account_id is not None and row[1] is not None and int(row[1]) != account_id:
            raise ValueError(f"素材已归属账号 {row[1]}，不能隐式改绑")
        if product_id and row[3] and str(row[3]) != product_id:
            raise ValueError("素材已关联其他商品 ID，不能隐式改绑")
        if account_id is None and row[1] is None:
            inferred_id, _ = infer_legacy_metadata(con, path, row[2])
            if inferred_id is not None:
                account_id = inferred_id
                name = account_name(con, inferred_id)
    stat = fp.stat()
    now = now_text()
    mtime = datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds")
    if row is None:
        con.execute(
            "INSERT INTO assets (file_path, file_name, file_size, product_id, video_id, account, "
            "account_id, source_type, category, status, thumb_path, file_mtime, scanned_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'ready', NULL, ?, ?, ?)",
            (path, fp.name, stat.st_size, product_id, parse_video_id(fp.name), name,
             account_id, source_type, name or "未分类", mtime, now, now),
        )
        asset_id = int(con.execute("SELECT last_insert_rowid()").fetchone()[0])
    else:
        asset_id = int(row[0])
        con.execute(
            "UPDATE assets SET file_name = ?, file_size = ?, product_id = COALESCE(?, product_id), "
            "video_id = ?, account_id = COALESCE(?, account_id), "
            "account = CASE WHEN ? IS NULL THEN account ELSE ? END, "
            "category = CASE WHEN ? IS NULL THEN category ELSE ? END, "
            "source_type = ?, status = 'ready', file_mtime = ?, scanned_at = ? WHERE id = ?",
            (fp.name, stat.st_size, product_id, parse_video_id(fp.name), account_id,
             name, name, name, name, source_type, mtime, now, asset_id),
        )
    con.commit()
    saved = con.execute(
        "SELECT account_id, account, source_type FROM assets WHERE id = ?", (asset_id,)
    ).fetchone()
    return {
        "ok": True, "id": asset_id, "file_path": path,
        "account_id": saved[0], "account": saved[1], "source_type": saved[2],
        "already_registered": row is not None,
    }


def assign_asset_account(con: sqlite3.Connection, asset_id: int, account_id: int) -> Dict[str, Any]:
    """人工明确指定归属；来源仍由生产者证据决定。"""
    name = account_name(con, account_id)
    row = con.execute(
        "SELECT account_id, source_type FROM assets WHERE id = ?", (asset_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"素材不存在：id={asset_id}")
    if row[0] is not None and int(row[0]) != account_id:
        raise ValueError(f"素材已归属账号 {row[0]}，不能隐式改绑")
    con.execute(
        "UPDATE assets SET account_id = ?, account = ?, category = ? WHERE id = ?",
        (account_id, name, name, asset_id),
    )
    con.commit()
    return {
        "ok": True, "id": asset_id, "account_id": account_id,
        "account": name, "source_type": str(row[1] or "unknown"),
    }


# ============================================================ 目录/文件名解析

_DATE_RE = None  # 惰性初始化


def looks_like_date(s: str) -> bool:
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def parse_video_id(file_name: str) -> Optional[str]:
    """{商品ID}_{视频ID}_{序号}_{时间戳}.mp4 → 第 2 段。解析失败返回 None。"""
    stem = Path(file_name).stem
    parts = stem.split("_")
    if len(parts) >= 4:
        return parts[1]
    return None


def classify_path(rel_parts: List[str], file_name: str) -> Dict[str, Any]:
    """
    按目录约定解析元数据。
      _queue_test/xxx.mp4                      → 未分类（开发期产物）
      {账号}/{日期}/{商品ID}/{file}.mp4         → 账号/日期/商品ID
    返回 account / product_id / category。
    """
    info: Dict[str, Any] = {"account": None, "product_id": None, "category": "未分类"}
    if len(rel_parts) >= 3 and looks_like_date(rel_parts[1]):
        info["account"] = rel_parts[0]
        info["product_id"] = rel_parts[2]
        info["category"] = rel_parts[0]
    elif rel_parts and rel_parts[0] == "_queue_test":
        info["category"] = "测试"
    return info


def thumb_cache_path(root: Path, file_path: Path) -> Path:
    """缩略图缓存路径：{root}/.thumbs/{path_md5[:16]}_{mtime}.jpg"""
    md5 = hashlib.md5(str(file_path).encode("utf-8")).hexdigest()[:16]
    mtime = int(file_path.stat().st_mtime)
    return root / ".thumbs" / f"{md5}_{mtime}.jpg"


def ensure_thumb(root: Path, file_path: Path, ffmpeg: str) -> Optional[str]:
    """生成缩略图（存在且 mtime 匹配则复用），返回路径或 None。"""
    try:
        cache = thumb_cache_path(root, file_path)
        if cache.exists():
            return str(cache)
        cache.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                ffmpeg, "-y", "-ss", "0.5", "-i", str(file_path),
                "-frames:v", "1", "-vf", "scale=240:-2", "-q:v", "5", str(cache),
            ],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20,
        )
        return str(cache) if cache.exists() else None
    except Exception:
        return None


def resolve_ffmpeg() -> str:
    bundled = str(Path(__file__).resolve().parent / "bin" / "ffmpeg.exe")
    for candidate in [
        bundled,
        r"C:\Users\9\AppData\Local\Microsoft\WinGet\Links\ffmpeg.EXE",
        "ffmpeg",
    ]:
        if candidate == "ffmpeg" and not shutil_which(candidate):
            continue
        if candidate != "ffmpeg" and not os.path.exists(candidate):
            continue
        return candidate
    return "ffmpeg"


def shutil_which(name: str) -> Optional[str]:
    import shutil

    return shutil.which(name)


# ============================================================ 命令：scan

def cmd_scan(con: sqlite3.Connection, root: Path, ffmpeg: str) -> Dict[str, Any]:
    if not root.exists():
        return {"ok": False, "error": f"下载目录不存在：{root}"}

    files = sorted(p for p in root.rglob("*") if p.suffix.lower() == ".mp4" and ".thumbs" not in p.parts)
    added, updated, thumbs_ok, missing = 0, 0, 0, 0
    total = len(files)
    now = now_text()

    for i, fp in enumerate(files):
        try:
            stat = fp.stat()
        except OSError:
            continue
        rel = fp.relative_to(root).parts
        info = classify_path(list(rel), fp.name)
        mtime = datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds")
        thumb = ensure_thumb(root, fp, ffmpeg)
        if thumb:
            thumbs_ok += 1

        row = con.execute(
            "SELECT id, account_id, source_type, account FROM assets WHERE file_path = ?", (str(fp),)
        ).fetchone()
        inferred_id, inferred_source = infer_legacy_metadata(con, str(fp), info.get("account"))
        resolved_id = int(row[1]) if row is not None and row[1] is not None else inferred_id
        resolved_source = (
            str(row[2]) if row is not None and row[2] and row[2] != "unknown" else inferred_source
        )
        resolved_account = (
            account_name(con, resolved_id) if resolved_id is not None
            else row[3] if row is not None and resolved_source != "unknown"
            else info.get("account")
        )
        resolved_category = resolved_account or info["category"]
        values = (
            str(fp), fp.name, stat.st_size,
            info.get("product_id"), parse_video_id(fp.name), resolved_account,
            resolved_id, resolved_source, resolved_category, "ready", thumb, mtime, now, now,
        )
        if row is None:
            con.execute(
                "INSERT INTO assets (file_path, file_name, file_size, product_id, video_id, account, "
                "account_id, source_type, category, status, thumb_path, file_mtime, scanned_at, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                values,
            )
            added += 1
        else:
            con.execute(
                "UPDATE assets SET file_size=?, product_id=COALESCE(product_id, ?), video_id=?, "
                "account=?, account_id=?, source_type=?, category=?, "
                "thumb_path=?, file_mtime=?, scanned_at=? WHERE id=?",
                (stat.st_size, info.get("product_id"), parse_video_id(fp.name), resolved_account,
                 resolved_id, resolved_source, resolved_category, thumb, mtime, now, row[0]),
            )
            updated += 1
        con.commit()
        emit({"type": "stage", "stage": f"扫描中 {i + 1}/{total}", "percent": int((i + 1) * 100 / total)})

    # 清理：库里记录但文件已不存在（被外部删除）→ 移除记录
    for (row_id,) in con.execute("SELECT id FROM assets").fetchall():
        p = con.execute("SELECT file_path FROM assets WHERE id = ?", (row_id,)).fetchone()[0]
        if not Path(p).exists():
            con.execute("DELETE FROM assets WHERE id = ?", (row_id,))
            con.commit()
            missing += 1

    return {
        "ok": True, "root": str(root),
        "scanned": total, "added": added, "updated": updated,
        "thumbs_ok": thumbs_ok, "removed_missing": missing,
    }


# ============================================================ 命令：list / 检索

def row_to_dict(con: sqlite3.Connection, r: sqlite3.Row) -> Dict[str, Any]:
    d = dict(r)
    d["tags"] = [t[0] for t in con.execute(
        "SELECT tag FROM asset_tags WHERE asset_id = ? ORDER BY tag", (d["id"],)
    ).fetchall()]
    return d


def cmd_list(con: sqlite3.Connection, args: argparse.Namespace) -> Dict[str, Any]:
    where: List[str] = []
    params: List[Any] = []
    if args.account == UNASSIGNED_ACCOUNT_FILTER:
        where.append("(account IS NULL OR TRIM(account) = '')")
    elif args.account:
        where.append("TRIM(account) = ?")
        params.append(args.account)
    if args.product_id:
        where.append("product_id = ?")
        params.append(args.product_id)
    if args.status:
        where.append("status = ?")
        params.append(args.status)
    if args.date:
        # 目录日期语义：{账号}/{YYYY-MM-DD}/{商品ID}/... → 路径含 /日期/ 或 \日期\（Windows）
        where.append("(file_path LIKE ? OR file_path LIKE ?)")
        params.extend([f"%/{args.date}/%", f"%\\{args.date}\\%"])
    if args.tag:
        where.append("EXISTS (SELECT 1 FROM asset_tags AS asset_tag WHERE asset_tag.asset_id = assets.id AND asset_tag.tag = ?)")
        params.append(args.tag)
    where_sql = " WHERE " + " AND ".join(where) if where else ""
    sql = "SELECT * FROM assets" + where_sql
    sql += " ORDER BY id DESC LIMIT ?"
    rows = [row_to_dict(con, r) for r in con.execute(sql, [*params, args.limit]).fetchall()]
    account_counts = [dict(row) for row in con.execute(
        "SELECT COALESCE(NULLIF(TRIM(account), ''), '__unassigned__') AS account_key, COUNT(*) AS count "
        "FROM assets" + where_sql + " GROUP BY account_key ORDER BY account_key COLLATE NOCASE",
        params,
    ).fetchall()]
    return {"ok": True, "count": len(rows), "rows": rows, "account_counts": account_counts}


def cmd_accounts(con: sqlite3.Connection) -> Dict[str, Any]:
    accounts = [row[0] for row in con.execute(
        "SELECT DISTINCT TRIM(account) FROM assets "
        "WHERE account IS NOT NULL AND TRIM(account) <> '' ORDER BY TRIM(account) COLLATE NOCASE"
    ).fetchall()]
    return {"ok": True, "accounts": accounts}


def cmd_tags(con: sqlite3.Connection) -> Dict[str, Any]:
    tags = [r[0] for r in con.execute("SELECT DISTINCT tag FROM asset_tags ORDER BY tag")]
    return {"ok": True, "tags": tags}


def cmd_categories(con: sqlite3.Connection) -> Dict[str, Any]:
    cats = [r[0] for r in con.execute("SELECT DISTINCT category FROM assets ORDER BY category")]
    return {"ok": True, "categories": cats}


def cmd_tag_set(con: sqlite3.Connection, args: argparse.Namespace) -> Dict[str, Any]:
    row = con.execute("SELECT id FROM assets WHERE id = ?", (args.id,)).fetchone()
    if row is None:
        return {"ok": False, "error": f"素材不存在：id={args.id}"}
    tags = [t.strip() for t in (args.tags or "").split(",") if t.strip()]
    con.execute("DELETE FROM asset_tags WHERE asset_id = ?", (args.id,))
    for t in tags:
        con.execute("INSERT OR IGNORE INTO asset_tags (asset_id, tag) VALUES (?, ?)", (args.id, t))
    con.commit()
    return {"ok": True, "id": args.id, "tags": tags}


def cmd_delete(con: sqlite3.Connection, root: Path, args: argparse.Namespace) -> Dict[str, Any]:
    """删除记录 + 缩略图 + 文件（校验文件必须在下载目录内，防误删）。"""
    row = con.execute("SELECT file_path, thumb_path FROM assets WHERE id = ?", (args.id,)).fetchone()
    if row is None:
        return {"ok": False, "error": f"素材不存在：id={args.id}"}
    file_path, thumb_path = row
    fp = Path(file_path)
    root_resolved = root.resolve()
    try:
        fp_resolved = fp.resolve()
        if not fp_resolved.is_relative_to(root_resolved):
            return {"ok": False, "error": f"文件不在下载目录内，拒绝删除：{fp}"}
    except (ValueError, OSError) as exc:
        return {"ok": False, "error": f"路径校验失败：{exc}"}

    try:
        if fp.exists():
            fp.unlink()
    except OSError as exc:
        return {"ok": False, "error": f"文件删除失败：{exc}"}
    if thumb_path and Path(thumb_path).exists():
        try:
            Path(thumb_path).unlink()
        except OSError:
            pass
    con.execute("DELETE FROM assets WHERE id = ?", (args.id,))
    con.commit()
    return {"ok": True, "deleted": args.id, "file": file_path}


def cmd_make_thumb(con: sqlite3.Connection, root: Path, ffmpeg: str, args: argparse.Namespace) -> Dict[str, Any]:
    row = con.execute("SELECT file_path FROM assets WHERE id = ?", (args.id,)).fetchone()
    if row is None:
        return {"ok": False, "error": f"素材不存在：id={args.id}"}
    thumb = ensure_thumb(root, Path(row[0]), ffmpeg)
    if thumb:
        con.execute("UPDATE assets SET thumb_path = ? WHERE id = ?", (thumb, args.id))
        con.commit()
    return {"ok": bool(thumb), "id": args.id, "thumb_path": thumb}


# ============================================================ 主流程

def main() -> int:
    parser = argparse.ArgumentParser(description="本地素材库引擎")
    parser.add_argument("--db", default=DEFAULT_DB_PATH, help=f"SQLite 路径，默认 {DEFAULT_DB_PATH}")
    parser.add_argument("--root", default=DEFAULT_ROOT, help=f"下载目录，默认 {DEFAULT_ROOT}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_scan = sub.add_parser("scan", help="扫描下载目录并登记素材")
    p_register = sub.add_parser("register", help="由下载或生成流程显式登记素材来源")
    p_register.add_argument("--file-path", required=True)
    p_register.add_argument("--source-type", choices=SOURCE_TYPES[:2], required=True)
    p_register.add_argument("--account-id", type=int)
    p_register.add_argument("--product-id")
    p_assign = sub.add_parser("assign-account", help="人工确认素材所属抖音账号，不修改来源")
    p_assign.add_argument("--id", type=int, required=True)
    p_assign.add_argument("--account-id", type=int, required=True)
    p_list = sub.add_parser("list", help="检索素材")
    p_list.add_argument("--account", help=f"按账号过滤；使用 {UNASSIGNED_ACCOUNT_FILTER} 过滤未归属账号素材")
    p_list.add_argument("--product-id", help="按商品 ID 过滤")
    p_list.add_argument("--tag", help="按标签过滤")
    p_list.add_argument("--date", help="按目录日期过滤（YYYY-MM-DD）")
    p_list.add_argument("--status", default="ready", help="状态过滤，默认 ready")
    p_list.add_argument("--limit", type=int, default=200)
    p_del = sub.add_parser("delete", help="删除素材（记录+文件）")
    p_del.add_argument("--id", type=int, required=True)
    sub.add_parser("accounts", help="列出素材记录中的账号")
    sub.add_parser("tags", help="全部标签")
    sub.add_parser("categories", help="全部分类")
    p_tag = sub.add_parser("tag-set", help="设置素材标签")
    p_tag.add_argument("--id", type=int, required=True)
    p_tag.add_argument("--tags", required=True, help="逗号分隔，如 爆款,新品")
    p_thumb = sub.add_parser("make-thumb", help="生成/刷新单个缩略图")
    p_thumb.add_argument("--id", type=int, required=True)
    args = parser.parse_args()

    con = connect(args.db)
    root = Path(args.root)
    ffmpeg = resolve_ffmpeg()

    try:
        if args.command == "scan":
            payload = cmd_scan(con, root, ffmpeg)
        elif args.command == "register":
            payload = register_asset(con, args.file_path, args.source_type, args.account_id, args.product_id)
        elif args.command == "assign-account":
            payload = assign_asset_account(con, args.id, args.account_id)
        elif args.command == "list":
            payload = cmd_list(con, args)
        elif args.command == "accounts":
            payload = cmd_accounts(con)
        elif args.command == "delete":
            payload = cmd_delete(con, root, args)
        elif args.command == "tags":
            payload = cmd_tags(con)
        elif args.command == "categories":
            payload = cmd_categories(con)
        elif args.command == "tag-set":
            payload = cmd_tag_set(con, args)
        elif args.command == "make-thumb":
            payload = cmd_make_thumb(con, root, ffmpeg, args)
        else:
            payload = {"ok": False, "error": f"未知命令：{args.command}"}
    except (OSError, sqlite3.Error, ValueError) as error:
        payload = {"ok": False, "error": str(error)}

    con.close()
    emit(payload)
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
