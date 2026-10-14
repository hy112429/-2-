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
    python asset_library.py list [--product-id X] [--tag T] [--date YYYY-MM-DD]
                               [--status ready] [--limit N]
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

DEFAULT_DB_PATH = r"D:\software\投广客户端\data\ad_client.db"
DEFAULT_ROOT = r"D:\software\投广客户端\downloads"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS assets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path   TEXT    NOT NULL UNIQUE,
    file_name   TEXT    NOT NULL,
    file_size   INTEGER NOT NULL,
    product_id  TEXT,
    video_id    TEXT,
    account     TEXT,
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
    con.executescript(SCHEMA_SQL)
    return con


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
    for candidate in [
        r"C:\Users\9\AppData\Local\Microsoft\WinGet\Links\ffmpeg.EXE",
        "ffmpeg",
    ]:
        if candidate == "ffmpeg" and not shutil_which(candidate):
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

        row = con.execute("SELECT id FROM assets WHERE file_path = ?", (str(fp),)).fetchone()
        values = (
            str(fp), fp.name, stat.st_size,
            info.get("product_id"), parse_video_id(fp.name), info.get("account"),
            info["category"], "ready", thumb, mtime, now, now,
        )
        if row is None:
            con.execute(
                "INSERT INTO assets (file_path, file_name, file_size, product_id, video_id, account, "
                "category, status, thumb_path, file_mtime, scanned_at, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                values,
            )
            added += 1
        else:
            con.execute(
                "UPDATE assets SET file_size=?, product_id=?, video_id=?, account=?, category=?, "
                "thumb_path=?, file_mtime=?, scanned_at=? WHERE id=?",
                (stat.st_size, info.get("product_id"), parse_video_id(fp.name), info.get("account"),
                 info["category"], thumb, mtime, now, row[0]),
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
    sql = "SELECT * FROM assets"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(args.limit)
    rows = [row_to_dict(con, r) for r in con.execute(sql, params).fetchall()]

    # 标签过滤（与主条件 AND）：有 tag 参数时二次过滤
    if args.tag:
        rows = [r for r in rows if args.tag in r["tags"]]
    return {"ok": True, "count": len(rows), "rows": rows}


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
    p_list = sub.add_parser("list", help="检索素材")
    p_list.add_argument("--product-id", help="按商品 ID 过滤")
    p_list.add_argument("--tag", help="按标签过滤")
    p_list.add_argument("--date", help="按目录日期过滤（YYYY-MM-DD）")
    p_list.add_argument("--status", default="ready", help="状态过滤，默认 ready")
    p_list.add_argument("--limit", type=int, default=200)
    p_del = sub.add_parser("delete", help="删除素材（记录+文件）")
    p_del.add_argument("--id", type=int, required=True)
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

    if args.command == "scan":
        payload = cmd_scan(con, root, ffmpeg)
    elif args.command == "list":
        payload = cmd_list(con, args)
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

    con.close()
    emit(payload)
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())