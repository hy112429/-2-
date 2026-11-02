#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
db.py —— 投广管理客户端 · SQLite 数据层（Step 3）

职责
    管理「账号 → 商品 → 视频 → 任务」四层数据的本地存储，并提供查重能力。
    Electron 前端不需要自己读 SQLite 文件，直接调用本脚本拿 JSON 即可。

表结构
    accounts  账号（运营账号 / 矩阵号；当店铺用也装得下）
      └─ products  商品（一个账号下多个商品）
           └─ videos   视频（一个商品下多条视频；video_id 全局唯一用于查重）
                └─ tasks    任务（下载 / 转码 / 裁剪的执行记录，带重试次数与进度）

设计要点
    · 只用 Python 内置 sqlite3，零第三方依赖：SQLite 免安装、免起服务，单文件即库。
    · 开启 WAL 日志模式：引擎在写数据时界面照样能读，桌面端必需。
    · 每个连接都开 PRAGMA foreign_keys：SQLite 默认关闭外键约束，不开等于没约束。
    · 所有命令输出 JSON，统一格式 {ok: true/false, ...}，方便前端消费。
    · init 是幂等的（CREATE TABLE IF NOT EXISTS），重复跑不会坏数据。

用法
    python db.py init
    python db.py account-add --name 主账号
    python db.py account-list
    python db.py product-add --account 主账号 --product-id P123456 --title "某商品"
    python db.py next-seq --account 主账号 --product-id P123456
    python db.py video-add --account 主账号 --product-id P123456 --video-id 7300000000000000000 \
        --title "视频标题" --author "作者" --duration-ms 15600 --width 1080 --height 1920 \
        --file-path "D:\\...\\xxx.mp4" --file-size 5242880 --status done
    python db.py video-check --video-id 7300000000000000000
    python db.py video-list --account 主账号 --limit 20
    python db.py stats
    python db.py sql --query "SELECT * FROM videos LIMIT 5"

退出码
    0 = 成功
    1 = 失败（看输出的 error 字段）
    2 = 参数/用法错误
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence

try:
    from .client_paths import DEFAULT_DB_PATH
except ImportError:
    from client_paths import DEFAULT_DB_PATH

try:
    from .selection_settings import banned_words_from_connection, find_banned_word
except ImportError:
    from selection_settings import banned_words_from_connection, find_banned_word

# ============================================================ 常量

# 表结构版本号。以后改表结构时 +1，便于写迁移逻辑。
SCHEMA_VERSION = 2

# 增量迁移（Step 11）：已有库补列，不破坏历史数据。
# SQLite 的 CREATE TABLE IF NOT EXISTS 不会给已有表加列，必须显式 ALTER。
ACCOUNT_MIGRATE_COLUMNS: Dict[str, str] = {
    "douyin_id": "TEXT",
    "qianchuan_id": "TEXT",
    "category": "TEXT",
    "phone": "TEXT",
    "status": "TEXT NOT NULL DEFAULT 'active'",
    "nickname": "TEXT",
    "follower_count": "INTEGER",
    "aweme_count": "INTEGER",
}
VIDEO_MIGRATE_COLUMNS: Dict[str, str] = {
    "published": "INTEGER NOT NULL DEFAULT 0",
}


def _ensure_column(connection: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    cols = [row["name"] for row in connection.execute(f"PRAGMA table_info({table})")]
    if column not in cols:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def migrate_schema(connection: sqlite3.Connection) -> None:
    """幂等迁移：为已有库补上缺失列。新建库无需改动（CREATE 已含新列）。"""
    for column, ddl in ACCOUNT_MIGRATE_COLUMNS.items():
        _ensure_column(connection, "accounts", column, ddl)
    for column, ddl in VIDEO_MIGRATE_COLUMNS.items():
        _ensure_column(connection, "videos", column, ddl)
    # 历史遗留：早期库的 tasks 表无 video_row_id 列（当时为规避不存在的外键重建过），
    # 补上后 idx_tasks_video 索引才能建。
    _ensure_column(
        connection,
        "tasks",
        "video_row_id",
        "INTEGER REFERENCES videos(id) ON DELETE CASCADE",
    )

# 视频状态取值（约定，不是数据库强制约束，避免改状态时还要改表）
VIDEO_STATUSES = ("pending", "downloading", "done", "failed")


SCHEMA_TABLES = """
-- 账号：运营账号 / 矩阵号。platform 留通用，将来接快手只改这个字段。
CREATE TABLE IF NOT EXISTS accounts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT    NOT NULL UNIQUE,       -- 账号名，也是目录第二层用的名字
    platform       TEXT    NOT NULL DEFAULT 'douyin',
    douyin_id      TEXT,                          -- 抖音号
    qianchuan_id   TEXT,                          -- 千川ID（快捷绑定，完整关联见 account_qianchuan）
    category       TEXT,                          -- 运营分类
    phone          TEXT,                          -- 绑定手机号
    status         TEXT    NOT NULL DEFAULT 'active', -- 账号状态：active=启用 / disabled=停用
    nickname       TEXT,                          -- 昵称（拉取信息回填）
    follower_count INTEGER,                       -- 粉丝数（拉取信息回填）
    aweme_count    INTEGER,                       -- 作品数（拉取信息回填）
    remark         TEXT,
    created_at     TEXT    NOT NULL
);

-- 商品：一个账号下多个商品。product_id 是业务 ID（商品页那个），不是本表主键。
CREATE TABLE IF NOT EXISTS products (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    account_row_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    product_id     TEXT    NOT NULL,
    title          TEXT,
    source_url     TEXT,
    created_at     TEXT    NOT NULL,
    UNIQUE (account_row_id, product_id)          -- 同一账号下商品 ID 不重复
);

-- 视频：video_id（抖音 aweme_id）全局唯一，这就是查重依据。
-- 命名规则里的「序号」存 seq，「时间戳」由 created_at 体现。
CREATE TABLE IF NOT EXISTS videos (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id       TEXT    NOT NULL UNIQUE,
    product_row_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    account_row_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    seq            INTEGER NOT NULL DEFAULT 1,
    title          TEXT,
    author         TEXT,
    duration_ms    INTEGER,
    source_url     TEXT,
    file_path      TEXT,
    file_size      INTEGER,
    width          INTEGER,
    height         INTEGER,
    crop_top       INTEGER NOT NULL DEFAULT 0,
    crop_right     INTEGER NOT NULL DEFAULT 0,
    crop_bottom    INTEGER NOT NULL DEFAULT 0,
    crop_left      INTEGER NOT NULL DEFAULT 0,
    status         TEXT    NOT NULL DEFAULT 'pending',
    error          TEXT,
    published      INTEGER NOT NULL DEFAULT 0,    -- 发布标记：0=待发布 / 1=已发布
    created_at     TEXT    NOT NULL,
    updated_at     TEXT    NOT NULL
);

-- 任务：把「某条视频做了什么、试了几次、到哪一步」单独记下来，便于断点与排查。
CREATE TABLE IF NOT EXISTS tasks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    video_row_id INTEGER REFERENCES videos(id) ON DELETE CASCADE,
    task_type    TEXT    NOT NULL,               -- download / transcode / crop
    status       TEXT    NOT NULL DEFAULT 'pending',
    attempts     INTEGER NOT NULL DEFAULT 0,
    progress     INTEGER NOT NULL DEFAULT 0,
    message      TEXT,
    created_at   TEXT    NOT NULL,
    updated_at   TEXT    NOT NULL
);

-- 分组与账号-分组关联（Step 11）
CREATE TABLE IF NOT EXISTS account_groups (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL UNIQUE,          -- 分组名（按运营分类自动归组时与分类同名）
    remark      TEXT,
    created_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS account_group_members (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    account_row_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    group_row_id   INTEGER NOT NULL REFERENCES account_groups(id) ON DELETE CASCADE,
    created_at     TEXT    NOT NULL,
    UNIQUE (account_row_id, group_row_id)
);

-- 千川账户（Step 11，SRS FR-C1-01 / A-12）
CREATE TABLE IF NOT EXISTS qianchuan_accounts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    qc_name     TEXT,                             -- 千川账户名
    qc_id       TEXT,                             -- 千川ID
    subject     TEXT,                             -- 所属主体
    login_state TEXT    NOT NULL DEFAULT 'offline', -- online=在线 / offline=离线
    last_check  TEXT,                             -- 最近校验时间
    remark      TEXT,
    created_at  TEXT    NOT NULL
);

-- 账号-千川关联：一个千川账户可关联多个抖音号（1:N / N:N，SRS A-12）
CREATE TABLE IF NOT EXISTS account_qianchuan (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    account_row_id   INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    qianchuan_row_id INTEGER NOT NULL REFERENCES qianchuan_accounts(id) ON DELETE CASCADE,
    created_at       TEXT    NOT NULL,
    UNIQUE (account_row_id, qianchuan_row_id)
);
"""

# 索引统一放这里，与表定义分离。原因：SQLite 的 CREATE TABLE IF NOT EXISTS 不会
# 给已有表加列，而旧库可能缺列（如历史 tasks 表无 video_row_id）——若索引与表在
# 同一段 executescript，旧库会因「索引引用了不存在的列」整体中断。先建表 →
# migrate_schema 补列 → 再建索引，即可幂等。
SCHEMA_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_products_account ON products(account_row_id);
CREATE INDEX IF NOT EXISTS idx_videos_product   ON videos(product_row_id);
CREATE INDEX IF NOT EXISTS idx_videos_account   ON videos(account_row_id);
CREATE INDEX IF NOT EXISTS idx_videos_status    ON videos(status);
CREATE INDEX IF NOT EXISTS idx_tasks_video      ON tasks(video_row_id);

CREATE INDEX IF NOT EXISTS idx_group_members_account ON account_group_members(account_row_id);
CREATE INDEX IF NOT EXISTS idx_group_members_group  ON account_group_members(group_row_id);
CREATE INDEX IF NOT EXISTS idx_account_qc_account   ON account_qianchuan(account_row_id);
CREATE INDEX IF NOT EXISTS idx_account_qc_qc        ON account_qianchuan(qianchuan_row_id);
"""


# ============================================================ 基础工具


def now_text() -> str:
    """统一的时间格式：本地时间 ISO8601，精确到秒。可读、可排序、可当字符串比大小。"""
    return datetime.now().isoformat(timespec="seconds")


def emit(payload: Dict[str, Any], exit_code: int = 0) -> int:
    """所有命令的统一出口：打印 JSON 并返回退出码。"""
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return exit_code


@contextmanager
def connect(db_path: Path) -> Iterator[sqlite3.Connection]:
    """
    打开数据库连接。

    三个必须做的事：
      1. row_factory = sqlite3.Row —— 查询结果可以按列名取值，转 dict 直接给前端用
      2. PRAGMA foreign_keys = ON —— SQLite 默认**关闭**外键约束，不显式打开，
         删账号时子表数据不会级联删除，约束也形同虚设
      3. timeout —— 界面和引擎同时访问时，遇到锁会等一会儿而不是立刻报 database is locked
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(db_path), timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def row_to_dict(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    return dict(row) if row is not None else None


def rows_to_list(rows: Sequence[sqlite3.Row]) -> List[Dict[str, Any]]:
    return [dict(row) for row in rows]


# ============================================================ 初始化


def init_db(db_path: Path) -> Dict[str, Any]:
    """建库建表。幂等，可以反复执行。"""
    with connect(db_path) as connection:
        # WAL 日志模式：写操作不阻塞读操作。桌面端「引擎在写、界面在读」场景必需。
        # 这个设置是持久化在库文件里的，设一次就行。
        connection.execute("PRAGMA journal_mode = WAL")
        connection.executescript(SCHEMA_TABLES)
        migrate_schema(connection)
        connection.executescript(SCHEMA_INDEXES)
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

        tables = [
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]

    return {
        "ok": True,
        "db_path": str(db_path),
        "schema_version": SCHEMA_VERSION,
        "journal_mode": journal_mode,
        "tables": tables,
    }


# ============================================================ 账号 / 商品


def add_account(
    connection: sqlite3.Connection, name: str, platform: str, remark: Optional[str]
) -> Dict[str, Any]:
    """新增账号。已存在则直接返回原有的，不报错（幂等，便于脚本反复执行）。"""
    existing = connection.execute(
        "SELECT * FROM accounts WHERE name = ?", (name,)
    ).fetchone()
    if existing:
        return {"ok": True, "created": False, "account": dict(existing)}

    cursor = connection.execute(
        "INSERT INTO accounts (name, platform, remark, created_at) VALUES (?, ?, ?, ?)",
        (name, platform, remark, now_text()),
    )
    created = connection.execute(
        "SELECT * FROM accounts WHERE id = ?", (cursor.lastrowid,)
    ).fetchone()
    return {"ok": True, "created": True, "account": dict(created)}


def require_account(connection: sqlite3.Connection, name: str) -> int:
    row = connection.execute("SELECT id FROM accounts WHERE name = ?", (name,)).fetchone()
    if row is None:
        raise ValueError(f"账号不存在：{name}（请先用 account-add 创建）")
    return int(row["id"])


def add_product(
    connection: sqlite3.Connection,
    account_row_id: int,
    product_id: str,
    title: Optional[str],
    source_url: Optional[str],
) -> Dict[str, Any]:
    """新增商品。同一账号下同 ID 已存在则返回原有的。"""
    existing = connection.execute(
        "SELECT * FROM products WHERE account_row_id = ? AND product_id = ?",
        (account_row_id, product_id),
    ).fetchone()
    if existing:
        return {"ok": True, "created": False, "product": dict(existing)}

    cursor = connection.execute(
        "INSERT INTO products (account_row_id, product_id, title, source_url, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (account_row_id, product_id, title, source_url, now_text()),
    )
    created = connection.execute(
        "SELECT * FROM products WHERE id = ?", (cursor.lastrowid,)
    ).fetchone()
    return {"ok": True, "created": True, "product": dict(created)}


def require_or_create_product(
    connection: sqlite3.Connection,
    account_row_id: int,
    product_id: str,
    title: Optional[str] = None,
    source_url: Optional[str] = None,
) -> Dict[str, Any]:
    """
    取商品；不存在就顺手创建。

    为什么这么设计：录视频时手上只有「账号 + 商品ID」，
    强制先建商品会让调用方多一步、还容易漏。
    """
    return add_product(connection, account_row_id, product_id, title, source_url)


# ============================================================ 视频


def next_seq(connection: sqlite3.Connection, product_row_id: int) -> int:
    """算出某商品下一条视频的序号（当前最大值 + 1）。"""
    row = connection.execute(
        "SELECT MAX(seq) AS max_seq FROM videos WHERE product_row_id = ?",
        (product_row_id,),
    ).fetchone()
    return int(row["max_seq"] or 0) + 1


def video_check(connection: sqlite3.Connection, video_id: str) -> Dict[str, Any]:
    """查重：这个视频是否已经处理过。下载前先调它，避免重复下载。"""
    row = connection.execute(
        "SELECT v.*, a.name AS account_name, p.product_id AS product_code "
        "FROM videos v "
        "JOIN accounts a ON a.id = v.account_row_id "
        "JOIN products p ON p.id = v.product_row_id "
        "WHERE v.video_id = ?",
        (video_id,),
    ).fetchone()

    if row is None:
        return {"ok": True, "exists": False, "video_id": video_id}

    video = dict(row)
    return {
        "ok": True,
        "exists": True,
        "video_id": video_id,
        "video": video,
        # 顺带告诉调用方文件还在不在，避免"库里有记录但文件被手动删了"这种坑
        "file_exists": bool(video.get("file_path")) and Path(video["file_path"]).exists(),
    }


def video_add(
    connection: sqlite3.Connection,
    account: str,
    product_id: str,
    video_id: str,
    seq: Optional[int] = None,
    title: Optional[str] = None,
    author: Optional[str] = None,
    duration_ms: Optional[int] = None,
    source_url: Optional[str] = None,
    file_path: Optional[str] = None,
    file_size: Optional[int] = None,
    width: Optional[int] = None,
    height: Optional[int] = None,
    crop_top: int = 0,
    crop_right: int = 0,
    crop_bottom: int = 0,
    crop_left: int = 0,
    status: str = "pending",
    error: Optional[str] = None,
    product_title: Optional[str] = None,
) -> Dict[str, Any]:
    """
    登记一条视频。

    查重规则：按 video_id 全局唯一（抖音 aweme_id 全局唯一，
    同一个视频无论出现在哪个商品下都只存一条）。已存在则返回 created=false。
    """
    existing = connection.execute(
        "SELECT id FROM videos WHERE video_id = ?", (video_id,)
    ).fetchone()
    if existing:
        return {
            "ok": True,
            "created": False,
            "reason": "该视频已登记过（按 video_id 全局去重）",
            "video_id": video_id,
            "row_id": existing["id"],
        }

    if status not in VIDEO_STATUSES:
        raise ValueError(f"status 取值非法：{status}（可选 {'/'.join(VIDEO_STATUSES)}）")

    account_row_id = require_account(connection, account)
    product_result = require_or_create_product(
        connection, account_row_id, product_id, title=product_title
    )
    product_row_id = int(product_result["product"]["id"])

    # 序号不传就自动取「该商品下最大值 + 1」
    assigned_seq = seq if seq is not None else next_seq(connection, product_row_id)

    timestamp = now_text()
    cursor = connection.execute(
        """
        INSERT INTO videos (
            video_id, product_row_id, account_row_id, seq, title, author, duration_ms,
            source_url, file_path, file_size, width, height,
            crop_top, crop_right, crop_bottom, crop_left,
            status, error, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            video_id, product_row_id, account_row_id, assigned_seq, title, author,
            duration_ms, source_url, file_path, file_size, width, height,
            crop_top, crop_right, crop_bottom, crop_left,
            status, error, timestamp, timestamp,
        ),
    )

    created = connection.execute(
        "SELECT * FROM videos WHERE id = ?", (cursor.lastrowid,)
    ).fetchone()

    return {
        "ok": True,
        "created": True,
        "video_id": video_id,
        "row_id": cursor.lastrowid,
        "assigned_seq": assigned_seq,
        "product_created": product_result["created"],
        "video": dict(created),
    }


def video_list(
    connection: sqlite3.Connection,
    account: Optional[str] = None,
    product_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
) -> Dict[str, Any]:
    """列出视频，带账号名和商品 ID，方便直接看。"""
    conditions: List[str] = []
    params: List[Any] = []

    if account:
        conditions.append("a.name = ?")
        params.append(account)
    if product_id:
        conditions.append("p.product_id = ?")
        params.append(product_id)
    if status:
        conditions.append("v.status = ?")
        params.append(status)

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    params.append(limit)

    rows = connection.execute(
        f"""
        SELECT v.id, v.video_id, v.seq, a.name AS account_name, p.product_id AS product_code,
               v.title, v.author, v.duration_ms, v.width, v.height,
               v.file_path, v.file_size, v.status, v.error, v.created_at
        FROM videos v
        JOIN accounts a ON a.id = v.account_row_id
        JOIN products p ON p.id = v.product_row_id
        {where_clause}
        ORDER BY v.id DESC
        LIMIT ?
        """,
        params,
    ).fetchall()

    return {"ok": True, "count": len(rows), "videos": rows_to_list(rows)}


# ============================================================ 查询 / 统计


def account_list(connection: sqlite3.Connection) -> Dict[str, Any]:
    rows = connection.execute(
        """
        SELECT a.*, (SELECT COUNT(*) FROM products p WHERE p.account_row_id = a.id) AS product_count,
               (SELECT COUNT(*) FROM videos v WHERE v.account_row_id = a.id) AS video_count
        FROM accounts a ORDER BY a.id
        """
    ).fetchall()
    return {"ok": True, "count": len(rows), "accounts": rows_to_list(rows)}


def product_list(
    connection: sqlite3.Connection, account: Optional[str] = None
) -> Dict[str, Any]:
    conditions = ""
    params: List[Any] = []
    if account:
        conditions = "WHERE a.name = ?"
        params.append(account)

    rows = connection.execute(
        f"""
        SELECT p.id, a.name AS account_name, p.product_id, p.title, p.source_url,
               p.created_at,
               (SELECT COUNT(*) FROM videos v WHERE v.product_row_id = p.id) AS video_count
        FROM products p
        JOIN accounts a ON a.id = p.account_row_id
        {conditions}
        ORDER BY p.id DESC
        """,
        params,
    ).fetchall()
    banned_words = banned_words_from_connection(connection)
    products = [
        product for product in rows_to_list(rows)
        if not find_banned_word(product.get("title"), banned_words)
    ]
    return {
        "ok": True,
        "count": len(products),
        "products": products,
        "filteredCount": len(rows) - len(products),
    }


def stats(connection: sqlite3.Connection) -> Dict[str, Any]:
    """总览：各表条数、视频状态分布、占用空间。"""
    def scalar(sql: str) -> int:
        return int(connection.execute(sql).fetchone()[0] or 0)

    status_rows = connection.execute(
        "SELECT status, COUNT(*) AS count FROM videos GROUP BY status ORDER BY status"
    ).fetchall()

    return {
        "ok": True,
        "schema_version": connection.execute("PRAGMA user_version").fetchone()[0],
        "journal_mode": connection.execute("PRAGMA journal_mode").fetchone()[0],
        "accounts": scalar("SELECT COUNT(*) FROM accounts"),
        "products": scalar("SELECT COUNT(*) FROM products"),
        "videos": scalar("SELECT COUNT(*) FROM videos"),
        "tasks": scalar("SELECT COUNT(*) FROM tasks"),
        "videos_by_status": {row["status"]: row["count"] for row in status_rows},
        "total_file_size": scalar("SELECT COALESCE(SUM(file_size), 0) FROM videos"),
    }


def run_raw_sql(
    connection: sqlite3.Connection, query: str, params: Sequence[Any]
) -> Dict[str, Any]:
    """
    逃生舱：直接跑一条 SELECT / PRAGMA，学习或排查时用。

    只放行只读语句 —— 数据修改必须走专用命令，避免一条手滑的 DELETE 清空数据。
    """
    stripped = query.strip().rstrip(";").strip()
    if ";" in stripped:
        raise ValueError("只允许执行单条语句（检测到分号）")
    if not stripped.lower().startswith(("select", "pragma")):
        raise ValueError("只允许 SELECT / PRAGMA 查询；写操作请用专用命令")

    rows = connection.execute(stripped, params).fetchall()
    return {"ok": True, "count": len(rows), "rows": rows_to_list(rows)}


# ============================================================ 命令行


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="投广管理客户端 SQLite 数据层（账号 / 商品 / 视频 / 任务）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--db", default=DEFAULT_DB_PATH, help=f"数据库路径，默认 {DEFAULT_DB_PATH}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="建库建表（幂等，可反复执行）")

    p_account_add = subparsers.add_parser("account-add", help="新增账号")
    p_account_add.add_argument("--name", required=True, help="账号名（也是目录名）")
    p_account_add.add_argument("--platform", default="douyin", help="平台，默认 douyin")
    p_account_add.add_argument("--remark", help="备注")

    subparsers.add_parser("account-list", help="列出账号")

    p_product_add = subparsers.add_parser("product-add", help="新增商品")
    p_product_add.add_argument("--account", required=True, help="账号名")
    p_product_add.add_argument("--product-id", required=True, help="商品 ID")
    p_product_add.add_argument("--title", help="商品标题")
    p_product_add.add_argument("--source-url", help="商品页链接")

    p_product_list = subparsers.add_parser("product-list", help="列出商品")
    p_product_list.add_argument("--account", help="只看某个账号")

    p_next_seq = subparsers.add_parser("next-seq", help="查某商品下一条视频的序号")
    p_next_seq.add_argument("--account", required=True, help="账号名")
    p_next_seq.add_argument("--product-id", required=True, help="商品 ID")

    p_video_add = subparsers.add_parser("video-add", help="登记一条视频（自动查重）")
    p_video_add.add_argument("--account", required=True, help="账号名")
    p_video_add.add_argument("--product-id", required=True, help="商品 ID（不存在会自动创建）")
    p_video_add.add_argument("--video-id", required=True, help="视频 ID（抖音 aweme_id）")
    p_video_add.add_argument("--seq", type=int, help="序号；不传则自动取该商品下最大值+1")
    p_video_add.add_argument("--title", help="视频标题")
    p_video_add.add_argument("--author", help="作者昵称")
    p_video_add.add_argument("--duration-ms", type=int, help="时长（毫秒）")
    p_video_add.add_argument("--source-url", help="来源链接")
    p_video_add.add_argument("--file-path", help="成品文件绝对路径")
    p_video_add.add_argument("--file-size", type=int, help="成品文件字节数")
    p_video_add.add_argument("--width", type=int, help="成品宽度")
    p_video_add.add_argument("--height", type=int, help="成品高度")
    p_video_add.add_argument("--crop-top", type=int, default=0, help="裁剪：上")
    p_video_add.add_argument("--crop-right", type=int, default=0, help="裁剪：右")
    p_video_add.add_argument("--crop-bottom", type=int, default=0, help="裁剪：下")
    p_video_add.add_argument("--crop-left", type=int, default=0, help="裁剪：左")
    p_video_add.add_argument("--status", default="pending", help=f"状态，默认 pending（可选 {'/'.join(VIDEO_STATUSES)}）")
    p_video_add.add_argument("--error", help="失败原因（status=failed 时）")
    p_video_add.add_argument("--product-title", help="顺带设置商品标题")

    p_video_check = subparsers.add_parser("video-check", help="查重：该视频是否已登记")
    p_video_check.add_argument("--video-id", required=True, help="视频 ID")

    p_video_list = subparsers.add_parser("video-list", help="列出视频")
    p_video_list.add_argument("--account", help="只看某个账号")
    p_video_list.add_argument("--product-id", help="只看某个商品")
    p_video_list.add_argument("--status", help="只看某个状态")
    p_video_list.add_argument("--limit", type=int, default=50, help="最多返回条数，默认 50")

    subparsers.add_parser("stats", help="总览统计")

    p_sql = subparsers.add_parser("sql", help="跑一条只读 SQL（学习/排查用）")
    p_sql.add_argument("--query", required=True, help="SELECT / PRAGMA 语句")

    return parser


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    args = build_parser().parse_args()
    db_path = Path(args.db)

    try:
        # init 自己会建目录，其他命令要求库已存在，给个清晰提示而不是报底层错
        if args.command != "init" and not db_path.exists():
            return emit(
                {"ok": False, "error": f"数据库不存在：{db_path}。请先执行 python db.py init"},
                exit_code=1,
            )

        if args.command == "init":
            return emit(init_db(db_path))

        with connect(db_path) as connection:
            if args.command == "account-add":
                return emit(add_account(connection, args.name, args.platform, args.remark))

            if args.command == "account-list":
                return emit(account_list(connection))

            if args.command == "product-add":
                account_row_id = require_account(connection, args.account)
                return emit(
                    add_product(connection, account_row_id, args.product_id, args.title, args.source_url)
                )

            if args.command == "product-list":
                return emit(product_list(connection, args.account))

            if args.command == "next-seq":
                account_row_id = require_account(connection, args.account)
                product_row = connection.execute(
                    "SELECT id FROM products WHERE account_row_id = ? AND product_id = ?",
                    (account_row_id, args.product_id),
                ).fetchone()
                if product_row is None:
                    return emit(
                        {
                            "ok": True,
                            "next_seq": 1,
                            "note": "该商品还没建立，首条视频序号为 1",
                        }
                    )
                return emit({"ok": True, "next_seq": next_seq(connection, int(product_row["id"]))})

            if args.command == "video-add":
                return emit(
                    video_add(
                        connection,
                        account=args.account,
                        product_id=args.product_id,
                        video_id=args.video_id,
                        seq=args.seq,
                        title=args.title,
                        author=args.author,
                        duration_ms=args.duration_ms,
                        source_url=args.source_url,
                        file_path=args.file_path,
                        file_size=args.file_size,
                        width=args.width,
                        height=args.height,
                        crop_top=args.crop_top,
                        crop_right=args.crop_right,
                        crop_bottom=args.crop_bottom,
                        crop_left=args.crop_left,
                        status=args.status,
                        error=args.error,
                        product_title=args.product_title,
                    )
                )

            if args.command == "video-check":
                return emit(video_check(connection, args.video_id))

            if args.command == "video-list":
                return emit(
                    video_list(
                        connection,
                        account=args.account,
                        product_id=args.product_id,
                        status=args.status,
                        limit=args.limit,
                    )
                )

            if args.command == "stats":
                return emit(stats(connection))

            if args.command == "sql":
                return emit(run_raw_sql(connection, args.query, []))

    except (ValueError, sqlite3.Error) as exc:
        return emit({"ok": False, "command": args.command, "error": f"{type(exc).__name__}: {exc}"}, exit_code=1)

    return emit({"ok": False, "error": f"未知命令：{args.command}"}, exit_code=2)


if __name__ == "__main__":
    sys.exit(main())
