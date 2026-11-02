#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
account_manager.py —— 投广管理客户端 · 账号管理数据层（Step 11）

职责
    管理「抖音账号 / 分组 / 千川账户」三类数据的 CRUD、分配、统计与自动分组。
    数据表由 db.py 的 init 统一建立（含增量迁移），本脚本只读/写同一个 SQLite 库。

表结构（由 db.py SCHEMA_SQL 建立）
    accounts              抖音账号（含 抖音号/千川ID/运营分类/手机号/状态/拉取信息回填列）
    account_groups        分组
    account_group_members 账号-分组关联
    qianchuan_accounts    千川账户（千川账户名/千川ID/所属主体/登录态/在线状态/最近校验）
    account_qianchuan     账号-千川关联（一个千川账户可关联多个抖音号，1:N / N:N）

统计口径（SRS FR-G3-01）
    - 每账号统计列：素材数量(videos)、计划发布率/卡优率/广告条数（依赖发布与千川模块，暂为 0）
    - 待发布 = videos.published=0 且 status='done'（已下载完成未标记发布）

用法
    python account_manager.py init 等价于 db.py init
    python account_manager.py account-add --name 主号 --douyin-id 123 --category 美妆 --phone 138xxxx
    python account_manager.py account-list
    python account_manager.py group-auto
    python account_manager.py qianchuan-add --name 千川A --qc-id 16761573861161
    python account_manager.py qianchuan-bind --qianchuan-id 1 --account-id 2
    python account_manager.py stats
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from db import DEFAULT_DB_PATH, connect, emit, now_text

try:
    from .selection_settings import read_effective_category_catalog
except ImportError:
    from selection_settings import read_effective_category_catalog

# 账号状态与千川在线状态的合法取值（约定，非库约束）
ACCOUNT_STATUSES = ("active", "disabled")
QC_LOGIN_STATES = ("online", "offline")

# 账号可更新字段（account-update 用）
ACCOUNT_EDIT_FIELDS = (
    "name",
    "douyin_id",
    "qianchuan_id",
    "category",
    "phone",
    "remark",
    "status",
)


# ============================================================ 账号


def account_add(
    connection: sqlite3.Connection,
    name: str,
    douyin_id: Optional[str],
    qianchuan_id: Optional[str],
    category: Optional[str],
    phone: Optional[str],
    remark: Optional[str],
    status: str,
) -> Dict[str, Any]:
    """新增抖音账号。同名已存在则返回原有（幂等）。"""
    existing = connection.execute(
        "SELECT * FROM accounts WHERE name = ?", (name,)
    ).fetchone()
    if existing:
        return {"ok": True, "created": False, "account": dict(existing)}

    cursor = connection.execute(
        "INSERT INTO accounts "
        "(name, douyin_id, qianchuan_id, category, phone, status, remark, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (name, douyin_id, qianchuan_id, category, phone, status, remark, now_text()),
    )
    row = connection.execute(
        "SELECT * FROM accounts WHERE id = ?", (cursor.lastrowid,)
    ).fetchone()
    return {"ok": True, "created": True, "account": dict(row)}


def account_update(
    connection: sqlite3.Connection, aid: int, **kw: Optional[str]
) -> Dict[str, Any]:
    """部分更新账号（只更新传了值的字段）。"""
    row = connection.execute("SELECT * FROM accounts WHERE id = ?", (aid,)).fetchone()
    if row is None:
        return {"ok": False, "error": f"账号不存在：id={aid}"}

    if "status" in kw and kw["status"] not in ACCOUNT_STATUSES:
        return {"ok": False, "error": f"状态必须是 {'/'.join(ACCOUNT_STATUSES)}"}

    sets, vals = [], []
    for field in ACCOUNT_EDIT_FIELDS:
        value = kw.get(field)
        if value is not None:
            sets.append(f"{field} = ?")
            vals.append(value)
    if sets:
        vals.append(aid)
        connection.execute(f"UPDATE accounts SET {', '.join(sets)} WHERE id = ?", vals)

    updated = connection.execute(
        "SELECT * FROM accounts WHERE id = ?", (aid,)
    ).fetchone()
    return {"ok": True, "account": dict(updated)}


def account_delete(connection: sqlite3.Connection, aid: int) -> Dict[str, Any]:
    """删除账号。子表数据（商品/视频/分组关联/千川关联）靠外键级联删除。"""
    row = connection.execute("SELECT * FROM accounts WHERE id = ?", (aid,)).fetchone()
    if row is None:
        return {"ok": False, "error": f"账号不存在：id={aid}"}
    connection.execute("DELETE FROM accounts WHERE id = ?", (aid,))
    return {"ok": True, "deleted_account": dict(row)}


def _group_map(connection: sqlite3.Connection) -> Dict[int, List[str]]:
    """账号 id -> 所在分组名列表"""
    mapping: Dict[int, List[str]] = {}
    for row in connection.execute(
        """
        SELECT m.account_row_id, g.name AS group_name
        FROM account_group_members m
        JOIN account_groups g ON g.id = m.group_row_id
        ORDER BY g.name
        """
    ):
        mapping.setdefault(row["account_row_id"], []).append(row["group_name"])
    return mapping


def _table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def _decode_settings(value: Optional[str]) -> Dict[str, Any]:
    try:
        decoded = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _category_path_map(nodes: Any) -> Dict[str, str]:
    paths: Dict[str, str] = {}

    def visit(items: Any, parent: str = "") -> None:
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            node_id = str(item.get("id", ""))
            name = str(item.get("name", "")).strip()
            if not node_id or not name:
                continue
            path = f"{parent} / {name}" if parent else name
            paths[node_id] = path
            visit(item.get("children", []), path)

    visit(nodes)
    return paths


def _selection_category_label(
    account_id: int,
    settings_by_scope: Dict[str, Dict[str, Any]],
    category_paths: Dict[str, str],
) -> str:
    account_settings = settings_by_scope.get(f"account:{account_id}", {})
    global_settings = settings_by_scope.get("global", {})
    account_selection = account_settings.get("categorySelection", {})
    global_selection = global_settings.get("categorySelection", {})

    def selected_ids(selection: Any) -> Dict[str, List[str]]:
        if not isinstance(selection, dict):
            return {"level1": [], "level2": [], "level3": []}
        return {
            level: [str(value) for value in selection.get(level, []) if str(value)]
            if isinstance(selection.get(level, []), list) else []
            for level in ("level1", "level2", "level3")
        }

    account_ids = selected_ids(account_selection)
    has_account_categories = any(account_ids.values())
    effective_ids = account_ids if has_account_categories else selected_ids(global_selection)
    deepest_ids = effective_ids["level3"] or effective_ids["level2"] or effective_ids["level1"]
    labels: List[str] = []
    for selected_id in deepest_ids:
        label = category_paths.get(selected_id)
        if label is None:
            for catalog_id, catalog_path in category_paths.items():
                if catalog_id.endswith(f"__RC_CASCADER_SPLIT__{selected_id}") or selected_id.endswith(
                    f"__RC_CASCADER_SPLIT__{catalog_id}"
                ):
                    label = catalog_path
                    break
        if label and label not in labels:
            labels.append(label)

    category_text = "；".join(labels) if labels else ("已设置（类目目录待同步）" if deepest_ids else "不限")
    source_text = "账号" if has_account_categories else "继承通用"
    return f"{source_text}：{category_text}"


def account_list(
    connection: sqlite3.Connection, group_id: Optional[int] = None
) -> Dict[str, Any]:
    """账号列表，含分组名与每账号统计列（SRS FR-G3-01）。"""
    where, params = "", []
    if group_id is not None:
        where = "WHERE a.id IN (SELECT account_row_id FROM account_group_members WHERE group_row_id = ?)"
        params.append(group_id)

    rows = connection.execute(
        f"""
        SELECT a.*,
               (SELECT COUNT(*) FROM videos v WHERE v.account_row_id = a.id) AS material_count,
               (SELECT COUNT(*) FROM videos v
                 WHERE v.account_row_id = a.id AND v.published = 0 AND v.status = 'done') AS pending_publish,
               (SELECT COUNT(*) FROM account_qianchuan q WHERE q.account_row_id = a.id) AS qc_bind_count
        FROM accounts a
        {where}
        ORDER BY a.id
        """,
        params,
    ).fetchall()

    groups = _group_map(connection)
    settings_by_scope: Dict[str, Dict[str, Any]] = {}
    category_paths: Dict[str, str] = {}
    if _table_exists(connection, "selection_settings"):
        settings_by_scope = {
            str(row["scope_key"]): _decode_settings(row["settings_json"])
            for row in connection.execute("SELECT scope_key, settings_json FROM selection_settings")
        }
    if _table_exists(connection, "selection_category_catalog"):
        try:
            category_paths = _category_path_map(read_effective_category_catalog(connection))
        except (sqlite3.Error, TypeError, json.JSONDecodeError):
            category_paths = {}

    success_counts: Dict[int, int] = {}
    success_count_available = _table_exists(connection, "selection_quota_events")
    if success_count_available:
        try:
            success_counts = {
                int(row["account_id"]): int(row["success_count"] or 0)
                for row in connection.execute(
                    "SELECT account_id, COUNT(DISTINCT product_id) AS success_count "
                    "FROM selection_quota_events WHERE event_type = 'cart' GROUP BY account_id"
                )
            }
        except sqlite3.Error:
            success_count_available = False

    accounts = []
    for row in rows:
        item = dict(row)
        item["groups"] = groups.get(row["id"], [])
        item["selection_category"] = _selection_category_label(
            int(row["id"]), settings_by_scope, category_paths
        )
        # 成功数按成功加车事件按账号+商品去重；旧数据没有事件记录时显示不可用。
        item["success_count"] = success_counts.get(int(row["id"]), 0) if success_count_available else None
        # 发布率 / 卡优率 / 广告数依赖真实发布及千川统计，未接入前返回空值，避免伪装为 0。
        item["publish_rate"] = None
        item["karl_rate"] = None
        item["ad_count"] = None
        accounts.append(item)

    return {"ok": True, "count": len(accounts), "accounts": accounts}


# ============================================================ 分组


def group_add(connection: sqlite3.Connection, name: str, remark: Optional[str]) -> Dict[str, Any]:
    existing = connection.execute(
        "SELECT * FROM account_groups WHERE name = ?", (name,)
    ).fetchone()
    if existing:
        return {"ok": True, "created": False, "group": dict(existing)}
    cursor = connection.execute(
        "INSERT INTO account_groups (name, remark, created_at) VALUES (?, ?, ?)",
        (name, remark, now_text()),
    )
    row = connection.execute(
        "SELECT * FROM account_groups WHERE id = ?", (cursor.lastrowid,)
    ).fetchone()
    return {"ok": True, "created": True, "group": dict(row)}


def group_rename(connection: sqlite3.Connection, gid: int, name: str) -> Dict[str, Any]:
    row = connection.execute(
        "SELECT * FROM account_groups WHERE id = ?", (gid,)
    ).fetchone()
    if row is None:
        return {"ok": False, "error": f"分组不存在：id={gid}"}
    dup = connection.execute(
        "SELECT * FROM account_groups WHERE name = ? AND id != ?", (name, gid)
    ).fetchone()
    if dup:
        return {"ok": False, "error": f"分组名已存在：{name}"}
    connection.execute("UPDATE account_groups SET name = ? WHERE id = ?", (name, gid))
    return {"ok": True, "group": {"id": gid, "name": name}}


def group_delete(connection: sqlite3.Connection, gid: int) -> Dict[str, Any]:
    row = connection.execute(
        "SELECT * FROM account_groups WHERE id = ?", (gid,)
    ).fetchone()
    if row is None:
        return {"ok": False, "error": f"分组不存在：id={gid}"}
    connection.execute("DELETE FROM account_groups WHERE id = ?", (gid,))
    return {"ok": True, "deleted_group": dict(row)}


def group_list(connection: sqlite3.Connection) -> Dict[str, Any]:
    rows = connection.execute(
        """
        SELECT g.*, COUNT(m.id) AS member_count
        FROM account_groups g
        LEFT JOIN account_group_members m ON m.group_row_id = g.id
        GROUP BY g.id ORDER BY g.id
        """
    ).fetchall()
    return {"ok": True, "count": len(rows), "groups": rows_to_list(rows)}


def group_assign(
    connection: sqlite3.Connection,
    aid: int,
    gid: int,
    remove: bool = False,
) -> Dict[str, Any]:
    """把账号分配/移出分组。"""
    account = connection.execute(
        "SELECT * FROM accounts WHERE id = ?", (aid,)
    ).fetchone()
    if account is None:
        return {"ok": False, "error": f"账号不存在：id={aid}"}
    group = connection.execute(
        "SELECT * FROM account_groups WHERE id = ?", (gid,)
    ).fetchone()
    if group is None:
        return {"ok": False, "error": f"分组不存在：id={gid}"}

    if remove:
        connection.execute(
            "DELETE FROM account_group_members WHERE account_row_id = ? AND group_row_id = ?",
            (aid, gid),
        )
        action = "removed"
    else:
        connection.execute(
            "INSERT OR IGNORE INTO account_group_members (account_row_id, group_row_id, created_at) VALUES (?, ?, ?)",
            (aid, gid, now_text()),
        )
        action = "assigned"
    return {"ok": True, "action": action, "account_id": aid, "group_id": gid}


def group_auto(connection: sqlite3.Connection) -> Dict[str, Any]:
    """自动分组：按运营分类归组。同名分组不存在则自动创建。"""
    categories = [
        row["category"]
        for row in connection.execute(
            "SELECT DISTINCT category FROM accounts "
            "WHERE category IS NOT NULL AND category <> '' ORDER BY category"
        )
    ]
    created: List[str] = []
    assigned = 0
    for category in categories:
        group = connection.execute(
            "SELECT id FROM account_groups WHERE name = ?", (category,)
        ).fetchone()
        if group is None:
            cursor = connection.execute(
                "INSERT INTO account_groups (name, created_at) VALUES (?, ?)",
                (category, now_text()),
            )
            gid = cursor.lastrowid
            created.append(category)
        else:
            gid = group["id"]
        for account in connection.execute(
            "SELECT id FROM accounts WHERE category = ?", (category,)
        ):
            connection.execute(
                "INSERT OR IGNORE INTO account_group_members (account_row_id, group_row_id, created_at) VALUES (?, ?, ?)",
                (account["id"], gid, now_text()),
            )
            assigned += 1
    return {"ok": True, "groups_created": created, "accounts_assigned": assigned}


# ============================================================ 千川账户


def qianchuan_add(
    connection: sqlite3.Connection,
    name: Optional[str],
    qc_id: Optional[str],
    subject: Optional[str],
    remark: Optional[str],
) -> Dict[str, Any]:
    cursor = connection.execute(
        "INSERT INTO qianchuan_accounts (qc_name, qc_id, subject, remark, created_at) VALUES (?, ?, ?, ?, ?)",
        (name, qc_id, subject, remark, now_text()),
    )
    row = connection.execute(
        "SELECT * FROM qianchuan_accounts WHERE id = ?", (cursor.lastrowid,)
    ).fetchone()
    return {"ok": True, "qianchuan": dict(row)}


def qianchuan_update(
    connection: sqlite3.Connection,
    qid: int,
    **kw: Optional[str],
) -> Dict[str, Any]:
    row = connection.execute(
        "SELECT * FROM qianchuan_accounts WHERE id = ?", (qid,)
    ).fetchone()
    if row is None:
        return {"ok": False, "error": f"千川账户不存在：id={qid}"}
    if "login_state" in kw and kw["login_state"] not in QC_LOGIN_STATES:
        return {"ok": False, "error": f"在线状态必须是 {'/'.join(QC_LOGIN_STATES)}"}

    fields = ("qc_name", "qc_id", "subject", "login_state", "last_check", "remark")
    sets, vals = [], []
    for field in fields:
        value = kw.get(field)
        if value is not None:
            sets.append(f"{field} = ?")
            vals.append(value)
    if sets:
        vals.append(qid)
        connection.execute(f"UPDATE qianchuan_accounts SET {', '.join(sets)} WHERE id = ?", vals)
    updated = connection.execute(
        "SELECT * FROM qianchuan_accounts WHERE id = ?", (qid,)
    ).fetchone()
    return {"ok": True, "qianchuan": dict(updated)}


def qianchuan_delete(connection: sqlite3.Connection, qid: int) -> Dict[str, Any]:
    row = connection.execute(
        "SELECT * FROM qianchuan_accounts WHERE id = ?", (qid,)
    ).fetchone()
    if row is None:
        return {"ok": False, "error": f"千川账户不存在：id={qid}"}
    connection.execute("DELETE FROM qianchuan_accounts WHERE id = ?", (qid,))
    return {"ok": True, "deleted_qianchuan": dict(row)}


def qianchuan_list(connection: sqlite3.Connection) -> Dict[str, Any]:
    rows = connection.execute(
        """
        SELECT q.*,
               (SELECT COUNT(*) FROM account_qianchuan aq WHERE aq.qianchuan_row_id = q.id) AS bind_count,
               (SELECT GROUP_CONCAT(account_row_id) FROM account_qianchuan aq
                 WHERE aq.qianchuan_row_id = q.id) AS bound_account_ids
        FROM qianchuan_accounts q ORDER BY q.id
        """
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["bound_account_ids"] = [
            int(x) for x in (item.get("bound_account_ids") or "").split(",") if x
        ]
        result.append(item)
    return {"ok": True, "count": len(result), "qianchuan_accounts": result}


def qianchuan_bind(
    connection: sqlite3.Connection,
    qid: int,
    aid: int,
    unbind: bool = False,
) -> Dict[str, Any]:
    """账号-千川绑定/解绑（一个千川账户可关联多个抖音号）。"""
    qc = connection.execute(
        "SELECT * FROM qianchuan_accounts WHERE id = ?", (qid,)
    ).fetchone()
    if qc is None:
        return {"ok": False, "error": f"千川账户不存在：id={qid}"}
    account = connection.execute(
        "SELECT * FROM accounts WHERE id = ?", (aid,)
    ).fetchone()
    if account is None:
        return {"ok": False, "error": f"账号不存在：id={aid}"}

    if unbind:
        connection.execute(
            "DELETE FROM account_qianchuan WHERE account_row_id = ? AND qianchuan_row_id = ?",
            (aid, qid),
        )
        action = "unbound"
    else:
        connection.execute(
            "INSERT OR IGNORE INTO account_qianchuan (account_row_id, qianchuan_row_id, created_at) VALUES (?, ?, ?)",
            (aid, qid, now_text()),
        )
        action = "bound"
    return {"ok": True, "action": action, "account_id": aid, "qianchuan_id": qid}


# ============================================================ 拉取信息回填 / 全局统计


def info_update(
    connection: sqlite3.Connection,
    aid: int,
    nickname: Optional[str],
    follower_count: Optional[int],
    aweme_count: Optional[int],
) -> Dict[str, Any]:
    """「拉取信息」结果回填：昵称 / 粉丝数 / 作品数。"""
    row = connection.execute(
        "SELECT * FROM accounts WHERE id = ?", (aid,)
    ).fetchone()
    if row is None:
        return {"ok": False, "error": f"账号不存在：id={aid}"}
    connection.execute(
        "UPDATE accounts SET nickname = ?, follower_count = ?, aweme_count = ? WHERE id = ?",
        (nickname, follower_count, aweme_count, aid),
    )
    updated = connection.execute(
        "SELECT * FROM accounts WHERE id = ?", (aid,)
    ).fetchone()
    return {"ok": True, "account": dict(updated)}


def stats(connection: sqlite3.Connection) -> Dict[str, Any]:
    """全局统计卡：账号数 / 已分组 / 素材数 / 待发布 / 千川账户数。"""
    def scalar(sql: str) -> int:
        return int(connection.execute(sql).fetchone()[0] or 0)

    return {
        "ok": True,
        "account_count": scalar("SELECT COUNT(*) FROM accounts"),
        "grouped_count": scalar(
            "SELECT COUNT(DISTINCT account_row_id) FROM account_group_members"
        ),
        "material_count": scalar("SELECT COUNT(*) FROM videos"),
        "pending_publish": scalar(
            "SELECT COUNT(*) FROM videos WHERE published = 0 AND status = 'done'"
        ),
        "qianchuan_count": scalar("SELECT COUNT(*) FROM qianchuan_accounts"),
    }


# ============================================================ 工具

def rows_to_list(rows) -> List[Dict[str, Any]]:
    return [dict(row) for row in rows]


# ============================================================ 命令行


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="投广管理客户端 账号管理数据层（账号 / 分组 / 千川账户）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--db", default=DEFAULT_DB_PATH, help=f"数据库路径，默认 {DEFAULT_DB_PATH}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    p = subparsers.add_parser("account-add", help="新增抖音账号")
    p.add_argument("--name", required=True, help="账号名（也是目录名）")
    p.add_argument("--douyin-id", help="抖音号")
    p.add_argument("--qianchuan-id", help="千川ID（快捷绑定）")
    p.add_argument("--category", help="运营分类")
    p.add_argument("--phone", help="绑定手机号")
    p.add_argument("--remark", help="备注")
    p.add_argument("--status", default="active", help=f"状态，默认 active（可选 {'/'.join(ACCOUNT_STATUSES)}）")

    p = subparsers.add_parser("account-update", help="更新账号（只更新传了值的字段）")
    p.add_argument("--id", type=int, required=True)
    p.add_argument("--name", help="账号名")
    p.add_argument("--douyin-id", help="抖音号")
    p.add_argument("--qianchuan-id", help="千川ID")
    p.add_argument("--category", help="运营分类")
    p.add_argument("--phone", help="绑定手机号")
    p.add_argument("--remark", help="备注")
    p.add_argument("--status", help=f"状态（可选 {'/'.join(ACCOUNT_STATUSES)}）")

    p = subparsers.add_parser("account-delete", help="删除账号（级联删除关联数据）")
    p.add_argument("--id", type=int, required=True)

    p = subparsers.add_parser("account-list", help="列出账号（含分组与每账号统计列）")
    p.add_argument("--group-id", type=int, help="只看某个分组")

    p = subparsers.add_parser("account-set-status", help="启用 / 停用账号")
    p.add_argument("--id", type=int, required=True)
    p.add_argument("--status", required=True, choices=ACCOUNT_STATUSES)

    p = subparsers.add_parser("info-update", help="「拉取信息」结果回填")
    p.add_argument("--account-id", type=int, required=True)
    p.add_argument("--nickname", help="昵称")
    p.add_argument("--follower-count", type=int, help="粉丝数")
    p.add_argument("--aweme-count", type=int, help="作品数")

    p = subparsers.add_parser("group-add", help="新建分组")
    p.add_argument("--name", required=True)
    p.add_argument("--remark", help="备注")

    p = subparsers.add_parser("group-rename", help="重命名分组")
    p.add_argument("--id", type=int, required=True)
    p.add_argument("--name", required=True)

    p = subparsers.add_parser("group-delete", help="删除分组")
    p.add_argument("--id", type=int, required=True)

    subparsers.add_parser("group-list", help="列出分组（含成员数）")

    p = subparsers.add_parser("group-assign", help="账号分配到分组（或移出）")
    p.add_argument("--account-id", type=int, required=True)
    p.add_argument("--group-id", type=int, required=True)
    p.add_argument("--remove", action="store_true", help="从分组移出")

    subparsers.add_parser("group-auto", help="自动分组（按运营分类归组）")

    p = subparsers.add_parser("qianchuan-add", help="新增千川账户")
    p.add_argument("--name", help="千川账户名")
    p.add_argument("--qc-id", help="千川ID")
    p.add_argument("--subject", help="所属主体")
    p.add_argument("--remark", help="备注")

    p = subparsers.add_parser("qianchuan-update", help="更新千川账户")
    p.add_argument("--id", type=int, required=True)
    p.add_argument("--name", help="千川账户名")
    p.add_argument("--qc-id", help="千川ID")
    p.add_argument("--subject", help="所属主体")
    p.add_argument("--remark", help="备注")
    p.add_argument("--login-state", help=f"在线状态（可选 {'/'.join(QC_LOGIN_STATES)}）")
    p.add_argument("--last-check", help="最近校验时间（ISO8601）")

    p = subparsers.add_parser("qianchuan-delete", help="删除千川账户")
    p.add_argument("--id", type=int, required=True)

    subparsers.add_parser("qianchuan-list", help="列出千川账户（含绑定账号数）")

    p = subparsers.add_parser("qianchuan-bind", help="绑定 / 解绑账号-千川")
    p.add_argument("--qianchuan-id", type=int, required=True)
    p.add_argument("--account-id", type=int, required=True)
    p.add_argument("--unbind", action="store_true", help="解绑")

    subparsers.add_parser("stats", help="全局统计卡")

    return parser


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    args = build_parser().parse_args()
    db_path = Path(args.db)

    if not db_path.exists():
        return emit(
            {"ok": False, "error": f"数据库不存在：{db_path}。请先执行 python db.py init"},
            exit_code=1,
        )

    with connect(db_path) as connection:
        command = args.command

        if command == "account-add":
            return emit(
                account_add(
                    connection, args.name, args.douyin_id, args.qianchuan_id,
                    args.category, args.phone, args.remark, args.status,
                )
            )

        if command == "account-update":
            return emit(account_update(connection, args.id, **vars(args)))

        if command == "account-delete":
            return emit(account_delete(connection, args.id))

        if command == "account-list":
            return emit(account_list(connection, args.group_id))

        if command == "account-set-status":
            return emit(
                account_update(connection, args.id, status=args.status)
            )

        if command == "info-update":
            return emit(
                info_update(
                    connection, args.account_id,
                    args.nickname, args.follower_count, args.aweme_count,
                )
            )

        if command == "group-add":
            return emit(group_add(connection, args.name, args.remark))

        if command == "group-rename":
            return emit(group_rename(connection, args.id, args.name))

        if command == "group-delete":
            return emit(group_delete(connection, args.id))

        if command == "group-list":
            return emit(group_list(connection))

        if command == "group-assign":
            return emit(group_assign(connection, args.account_id, args.group_id, args.remove))

        if command == "group-auto":
            return emit(group_auto(connection))

        if command == "qianchuan-add":
            return emit(
                qianchuan_add(connection, args.name, args.qc_id, args.subject, args.remark)
            )

        if command == "qianchuan-update":
            return emit(qianchuan_update(connection, args.id, **vars(args)))

        if command == "qianchuan-delete":
            return emit(qianchuan_delete(connection, args.id))

        if command == "qianchuan-list":
            return emit(qianchuan_list(connection))

        if command == "qianchuan-bind":
            return emit(qianchuan_bind(connection, args.qianchuan_id, args.account_id, args.unbind))

        if command == "stats":
            return emit(stats(connection))

    return 0


if __name__ == "__main__":
    sys.exit(main())
