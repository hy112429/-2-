#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""选品设置持久化：独立增量建表，不改动既有业务表。"""

from __future__ import annotations

import argparse
import copy
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

try:
    from .client_paths import DEFAULT_DB_PATH
except ImportError:
    from client_paths import DEFAULT_DB_PATH

COMPLIANCE_DEFAULT_BANNED_WORDS = [
    "高仿", "仿牌", "假货", "假冒", "伪劣", "三无产品", "无中文标签", "无生产日期",
    "无保质期", "无生产厂家", "盗版", "侵权", "山寨", "套牌", "假证", "假检测报告",
    "虚假宣传", "夸大宣传", "包治", "根治", "特效药", "处方药", "禁售", "违禁品",
    "毒品", "枪支", "弹药", "管制刀具", "赌博", "博彩", "六合彩", "色情", "成人用品",
    "成人视频", "裸聊", "刷单", "刷粉", "刷量", "代考", "代写", "作弊器", "窃听器",
    "针孔摄像", "破解软件", "盗号", "外挂", "个人信息", "银行卡信息", "假币", "人民币",
]

PROTOTYPE_BANNED_WORDS = [
    "罗永浩", "郝劭文", "正能量小明", "小明", "贾乃亮", "木森", "森弟", "大漠叔叔", "别叫我马大哈", "查文娟",
    "娟砸的废话bot", "笑笑姐", "小蝴蝶", "心动方程式", "与辉", "董与辉", "K总", "达乐", "老罗", "大舞台",
    "朱朱", "乡妹朱朱", "朱婷", "农村涛涛励志百货", "小熊励志百货", "励志娃聪聪百货", "乐观励志阿文百货", "励志阿文百货", "农村励志小聪百货", "周周百货园",
    "毛毛和爷爷的小店", "小勇在努力", "黄家敏", "匡赐有好物", "与辉同行", "汉森", "罗总", "郝邵文", "交个朋友", "博学罗交个朋友",
    "亮哥", "罗罗", "亮亮", "安安", "唐梦琪", "三公主", "唐三妹", "一路向前", "一颗小小草",
]
BANNED_WORDS_SEED_VERSION = 1
DEFAULT_BANNED_WORDS = list(dict.fromkeys([*COMPLIANCE_DEFAULT_BANNED_WORDS, *PROTOTYPE_BANNED_WORDS]))

DEFAULT_SETTINGS: Dict[str, Any] = {
    "candidateLimit": 5,
    "selectionMode": "general",
    "categoryMode": "auto",
    "categorySelection": {"level1": [], "level2": [], "level3": []},
    "filters": {"delivery": {}, "product": {}, "features": {}},
    "poolPriority": ["流量扶持", "小二严选", "低价好货", "福袋奖品"],
    "poolFallback": True,
    "sources": {
        "hotSpotEnabled": True,
        "allianceRankEnabled": True,
        "priority": ["hotSpot", "allianceRank"],
    },
    "thresholds": {"salesAmount": None, "salesCount": None, "conversionRate": None, "views": None, "commissionRate": None, "experienceScore": None},
    "trendPreference": "上升优先",
    "bannedWords": DEFAULT_BANNED_WORDS,
    "bannedWordsSeedVersion": BANNED_WORDS_SEED_VERSION,
    "execution": {"publishMode": "自动", "pinTime": "", "randomIntervalMin": 1, "randomIntervalMax": 5},
    "quota": {"perProductVideos": 10, "perAccountProducts": 5, "perAccountVideos": 50},
}


def now_text() -> str:
    return datetime.now().isoformat(timespec="seconds")


def emit(payload: Dict[str, Any], code: int = 0) -> int:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return code


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(db_path), timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute(
        "CREATE TABLE IF NOT EXISTS selection_settings ("
        "scope_key TEXT PRIMARY KEY, settings_json TEXT NOT NULL, updated_at TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS selection_category_catalog ("
        "catalog_key TEXT PRIMARY KEY, catalog_json TEXT NOT NULL, updated_at TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS selection_daily_usage ("
        "usage_date TEXT NOT NULL, account_id INTEGER NOT NULL, product_id TEXT NOT NULL, "
        "video_count INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL, "
        "PRIMARY KEY (usage_date, account_id, product_id))"
    )
    connection.commit()
    return connection


def scope_key(account_id: Optional[int]) -> str:
    return "global" if account_id is None else f"account:{account_id}"


def migrate_prototype_banned_words(connection: sqlite3.Connection) -> None:
    """只向已有通用设置追加一次原型名单，不覆盖用户保存的屏蔽词。"""
    try:
        row = connection.execute(
            "SELECT settings_json FROM selection_settings WHERE scope_key = 'global'"
        ).fetchone()
    except sqlite3.Error:
        return
    if not row:
        return
    try:
        settings = json.loads(row["settings_json"])
    except (TypeError, json.JSONDecodeError):
        return
    if not isinstance(settings, dict):
        return
    try:
        seed_version = int(settings.get("bannedWordsSeedVersion", 0))
    except (TypeError, ValueError):
        seed_version = 0
    if seed_version >= BANNED_WORDS_SEED_VERSION:
        return

    current_words = settings.get("bannedWords", DEFAULT_BANNED_WORDS)
    if not isinstance(current_words, list):
        return
    merged_words: list[str] = []
    seen_words: set[str] = set()
    for value in [*current_words, *PROTOTYPE_BANNED_WORDS]:
        word = str(value).strip()
        normalized = word.casefold()
        if word and normalized not in seen_words:
            seen_words.add(normalized)
            merged_words.append(word)
    settings["bannedWords"] = merged_words
    settings["bannedWordsSeedVersion"] = BANNED_WORDS_SEED_VERSION

    savepoint = "selection_banned_words_seed"
    connection.execute(f"SAVEPOINT {savepoint}")
    try:
        connection.execute(
            "UPDATE selection_settings SET settings_json = ?, updated_at = ? WHERE scope_key = 'global'",
            (json.dumps(settings, ensure_ascii=False), now_text()),
        )
        connection.execute(f"RELEASE SAVEPOINT {savepoint}")
    except sqlite3.Error:
        connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        connection.execute(f"RELEASE SAVEPOINT {savepoint}")


def load_settings(connection: sqlite3.Connection, account_id: Optional[int]) -> Dict[str, Any]:
    migrate_prototype_banned_words(connection)
    global_row = connection.execute(
        "SELECT settings_json FROM selection_settings WHERE scope_key = 'global'"
    ).fetchone()
    if global_row:
        global_settings = {**DEFAULT_SETTINGS, **json.loads(global_row["settings_json"])}
    else:
        global_settings = DEFAULT_SETTINGS
    if account_id is None:
        return global_settings
    row = connection.execute(
        "SELECT settings_json FROM selection_settings WHERE scope_key = ?", (scope_key(account_id),)
    ).fetchone()
    account_settings = json.loads(row["settings_json"]) if row else {}
    account_settings.setdefault("selectionMode", "inherit")
    account_settings.setdefault("categoryMode", "inherit")
    return {"global": global_settings, "account": account_settings, "inherited": not bool(row)}


def save_settings(connection: sqlite3.Connection, account_id: Optional[int], value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("设置内容必须是 JSON 对象")
    key = scope_key(account_id)
    if account_id is None:
        merged = dict(DEFAULT_SETTINGS)
        merged.update(value)
        value = merged
        value["selectionMode"] = "category" if value.get("selectionMode") == "category" else "general"
        value["categoryMode"] = "manual" if value.get("categoryMode") == "manual" else "auto"
        if not isinstance(value.get("candidateLimit"), int) or not 1 <= value["candidateLimit"] <= 10:
            raise ValueError("候选上限必须是 1–10 的整数")
        if not isinstance(value.get("bannedWords"), list):
            raise ValueError("屏蔽词必须是列表")
        value["bannedWords"] = list(dict.fromkeys(str(w).strip() for w in value["bannedWords"] if str(w).strip()))
    else:
        value.setdefault("selectionMode", "inherit")
        if value.get("selectionMode") not in {"inherit", "general", "category"}:
            raise ValueError("账号选品模式必须是继承、通用数据或品类模式")
        value.setdefault("categoryMode", "inherit")
        if value.get("categoryMode") not in {"inherit", "auto", "manual"}:
            raise ValueError("账号类目模式必须是继承、自动或手动")
    connection.execute(
        "INSERT INTO selection_settings(scope_key, settings_json, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(scope_key) DO UPDATE SET settings_json=excluded.settings_json, updated_at=excluded.updated_at",
        (key, json.dumps(value, ensure_ascii=False), now_text()),
    )
    connection.commit()
    return {"ok": True, "scope": key, "settings": value}


def banned_words_from_connection(connection: sqlite3.Connection) -> list[str]:
    """从共享数据库读取全局屏蔽词；尚未迁移的旧库使用内置词表。"""
    migrate_prototype_banned_words(connection)
    try:
        row = connection.execute(
            "SELECT settings_json FROM selection_settings WHERE scope_key = 'global'"
        ).fetchone()
    except sqlite3.Error:
        return list(DEFAULT_BANNED_WORDS)
    if not row:
        return list(DEFAULT_BANNED_WORDS)
    try:
        settings = json.loads(row["settings_json"])
        values = settings.get("bannedWords", DEFAULT_BANNED_WORDS) if isinstance(settings, dict) else DEFAULT_BANNED_WORDS
    except (TypeError, json.JSONDecodeError):
        return list(DEFAULT_BANNED_WORDS)
    if not isinstance(values, list):
        return list(DEFAULT_BANNED_WORDS)
    return list(dict.fromkeys(str(word).strip() for word in values if str(word).strip()))


def find_banned_word(title: Any, words: Sequence[str]) -> Optional[str]:
    """返回商品名称中命中的第一个屏蔽词。"""
    normalized_title = str(title or "").casefold()
    if not normalized_title:
        return None
    return next((str(word) for word in words if str(word) and str(word).casefold() in normalized_title), None)


def get_banned_words(db_path: Path) -> list[str]:
    try:
        connection = connect(db_path)
        try:
            return banned_words_from_connection(connection)
        finally:
            connection.close()
    except sqlite3.Error:
        return list(DEFAULT_BANNED_WORDS)


def save_catalog(connection: sqlite3.Connection, value: Any, mode: str = "merge", parent_id: Optional[str] = None) -> Dict[str, Any]:
    if isinstance(value, dict):
        mode = str(value.get("mode", mode))
        parent_id = str(value.get("parentId", parent_id or "")) or None
        value = value.get("categories", value.get("children", []))
    if not isinstance(value, list):
        raise ValueError("百应类目目录必须是列表")
    existing = read_catalog(connection)
    if mode == "root-snapshot":
        incoming = merge_category_nodes([], value)
        # 页面刷新得到的完整树为权威快照；不能把旧的简单三级 ID 合并回动态路径 ID。
        merged = incoming
    elif mode == "children":
        if not parent_id:
            raise ValueError("局部类目响应缺少父类目 ID")
        merged = attach_category_children(existing, parent_id, value)
        if merged is None:
            return {"ok": False, "error": f"局部类目的父节点 {parent_id} 尚未捕获；未将子类目提升为一级目录"}
    elif mode == "merge":
        merged = merge_category_nodes(existing, value)
    else:
        raise ValueError("未知百应类目保存模式")
    payload = json.dumps(merged, ensure_ascii=False)
    connection.execute(
        "INSERT INTO selection_category_catalog(catalog_key, catalog_json, updated_at) VALUES ('baiying', ?, ?) "
        "ON CONFLICT(catalog_key) DO UPDATE SET catalog_json=excluded.catalog_json, updated_at=excluded.updated_at",
        (payload, now_text()),
    )
    connection.commit()
    return {"ok": True, "count": len(merged), "categories": merged, "mode": mode}


def attach_category_children(existing: Any, parent_id: str, children: Any) -> Optional[list[Dict[str, Any]]]:
    found = False

    def update(nodes: Any) -> list[Dict[str, Any]]:
        nonlocal found
        result: list[Dict[str, Any]] = []
        for raw in nodes if isinstance(nodes, list) else []:
            if not isinstance(raw, dict):
                continue
            node = {"id": str(raw.get("id", "")), "name": str(raw.get("name", "")), "children": []}
            node["children"] = update(raw.get("children", []))
            if node["id"] == parent_id:
                node["children"] = merge_category_nodes(node["children"], children)
                found = True
            if node["id"] and node["name"]:
                result.append(node)
        return result

    result = update(existing)
    return result if found else None


def quota_usage(connection: sqlite3.Connection, account_ids: list[int], usage_date: Optional[str] = None) -> Dict[str, int]:
    day = usage_date or datetime.now().date().isoformat()
    ids = list(dict.fromkeys(int(value) for value in account_ids))
    if not ids:
        return {"products": 0, "videos": 0}
    placeholders = ",".join("?" for _ in ids)
    row = connection.execute(
        f"SELECT COUNT(*), COALESCE(SUM(video_count), 0) FROM selection_daily_usage WHERE usage_date = ? AND account_id IN ({placeholders})",
        [day, *ids],
    ).fetchone()
    return {"products": int(row[0] or 0), "videos": int(row[1] or 0)}


def record_quota_usage(connection: sqlite3.Connection, account_id: int, product_id: str, videos: int) -> Dict[str, Any]:
    if not product_id.strip():
        raise ValueError("商品 ID 不能为空")
    if videos < 0:
        raise ValueError("视频数量不能小于 0")
    day = datetime.now().date().isoformat()
    connection.execute(
        "INSERT INTO selection_daily_usage(usage_date, account_id, product_id, video_count, updated_at) "
        "VALUES (?, ?, ?, ?, ?) ON CONFLICT(usage_date, account_id, product_id) DO UPDATE SET "
        "video_count = selection_daily_usage.video_count + excluded.video_count, updated_at = excluded.updated_at",
        (day, account_id, product_id.strip(), videos, now_text()),
    )
    connection.commit()
    return {"ok": True, "date": day, "usage": quota_usage(connection, [account_id])}


def merge_category_nodes(existing: Any, incoming: Any) -> list[Dict[str, Any]]:
    """合并百应分层返回的目录快照，避免某个父类目的局部响应覆盖完整目录。"""
    result: list[Dict[str, Any]] = []
    by_id: Dict[str, Dict[str, Any]] = {}
    existing_nodes = existing if isinstance(existing, list) else []
    incoming_nodes = incoming if isinstance(incoming, list) else []

    for node in existing_nodes + incoming_nodes:
        if not isinstance(node, dict):
            continue
        category_id = str(node.get("id", "")).strip()
        name = str(node.get("name", "")).strip()
        if not category_id or not name:
            continue
        children = merge_category_nodes([], node.get("children", []))
        # 百应旧缓存的三级 ID 只有末段，新目录会返回“父 ID + 分隔符 + 子 ID”。
        # 同一父节点下按末段合并，并始终保留较完整的路径 ID，避免同一类目重复出现。
        compatible_id = category_id.rsplit("__RC_CASCADER_SPLIT__", 1)[-1]
        current = by_id.get(compatible_id)
        if current is None:
            current = {"id": category_id, "name": name, "children": children}
            by_id[compatible_id] = current
            result.append(current)
        else:
            if len(category_id) > len(str(current.get("id", ""))):
                current["id"] = category_id
            current["name"] = name
            current["children"] = merge_category_nodes(current.get("children", []), children)
    return result


def merge_missing_category_nodes(existing: Any, incoming: Any) -> list[Dict[str, Any]]:
    """只将缺失类目追加到已有目录，不改写已有节点的名称、ID 或其他字段。"""
    result = copy.deepcopy(existing) if isinstance(existing, list) else []
    by_id: Dict[str, Dict[str, Any]] = {}

    def compatible_id(node: Dict[str, Any]) -> str:
        raw_id = node.get("id", "")
        if raw_id is None:
            return ""
        return str(raw_id).strip().rsplit("__RC_CASCADER_SPLIT__", 1)[-1]

    for node in result:
        if not isinstance(node, dict):
            continue
        node_id = compatible_id(node)
        if node_id:
            by_id.setdefault(node_id, node)

    for incoming_node in incoming if isinstance(incoming, list) else []:
        if not isinstance(incoming_node, dict):
            continue
        node_id = compatible_id(incoming_node)
        node_name = str(incoming_node.get("name", "") or "").strip()
        if not node_id or not node_name:
            continue
        current = by_id.get(node_id)
        if current is None:
            added = copy.deepcopy(incoming_node)
            if "children" in added:
                added["children"] = merge_missing_category_nodes([], added.get("children"))
            result.append(added)
            by_id[node_id] = added
            continue

        incoming_children = incoming_node.get("children", [])
        if not isinstance(incoming_children, list) or not incoming_children:
            continue
        current_children = current.get("children", [])
        merged_children = merge_missing_category_nodes(current_children, incoming_children)
        if merged_children != current_children:
            current["children"] = merged_children

    return result


def read_catalog(connection: sqlite3.Connection) -> list[Any]:
    row = connection.execute(
        "SELECT catalog_json FROM selection_category_catalog WHERE catalog_key = 'baiying'"
    ).fetchone()
    return json.loads(row["catalog_json"]) if row else []


def read_filter_catalog(connection: sqlite3.Connection) -> Dict[str, Any]:
    row = connection.execute(
        "SELECT catalog_json, updated_at FROM selection_category_catalog WHERE catalog_key = 'baiying-filter-catalog'"
    ).fetchone()
    if not row:
        return {"filters": {}, "filterGroups": {}, "features": [], "categoryRoots": [], "updatedAt": ""}
    value = json.loads(row["catalog_json"])
    if not isinstance(value, dict):
        value = {}
    value["updatedAt"] = str(row["updated_at"])
    return value


def seed_or_supplement_catalog(connection: sqlite3.Connection) -> bool:
    """首次导入目录快照；已有缓存时只补缺失节点，不覆盖现有内容。"""
    seed_path = Path(__file__).resolve().parent / "resources" / "baiying_catalog_seed.json"
    try:
        seed = json.loads(seed_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False

    categories = seed.get("categories") if isinstance(seed, dict) else None
    filter_catalog = seed.get("filterCatalog") if isinstance(seed, dict) else None
    captured_at = seed.get("capturedAt") if isinstance(seed, dict) else None
    if (
        not isinstance(categories, list)
        or not categories
        or not isinstance(filter_catalog, dict)
        or not isinstance(filter_catalog.get("filters"), dict)
        or not isinstance(filter_catalog.get("filterGroups"), dict)
        or not isinstance(filter_catalog.get("features"), list)
        or not isinstance(filter_catalog.get("categoryRoots"), list)
        or not isinstance(captured_at, dict)
    ):
        return False

    category_updated_at = str(captured_at.get("categories", "")).strip()
    filter_updated_at = str(captured_at.get("filters", "")).strip()
    if not category_updated_at or not filter_updated_at:
        return False

    seed_category_tree = merge_missing_category_nodes(
        categories,
        filter_catalog.get("categoryRoots", []),
    )

    try:
        connection.execute("BEGIN IMMEDIATE")
        rows = connection.execute(
            "SELECT catalog_key, catalog_json, updated_at FROM selection_category_catalog "
            "WHERE catalog_key IN ('baiying', 'baiying-filter-catalog')"
        ).fetchall()
        existing = {str(row["catalog_key"]): row for row in rows}
        changed = False

        category_row = existing.get("baiying")
        if category_row is None:
            connection.execute(
                "INSERT INTO selection_category_catalog(catalog_key, catalog_json, updated_at) VALUES (?, ?, ?)",
                ("baiying", json.dumps(seed_category_tree, ensure_ascii=False), category_updated_at),
            )
            changed = True
        else:
            try:
                current_categories = json.loads(category_row["catalog_json"])
            except (TypeError, json.JSONDecodeError):
                current_categories = None
            if isinstance(current_categories, list):
                merged_categories = merge_missing_category_nodes(current_categories, seed_category_tree)
                if merged_categories != current_categories:
                    connection.execute(
                        "UPDATE selection_category_catalog SET catalog_json = ? WHERE catalog_key = 'baiying'",
                        (json.dumps(merged_categories, ensure_ascii=False),),
                    )
                    changed = True

        filter_row = existing.get("baiying-filter-catalog")
        if filter_row is None:
            seeded_filter_catalog = dict(filter_catalog)
            seeded_filter_catalog["categoryRoots"] = seed_category_tree
            connection.execute(
                "INSERT INTO selection_category_catalog(catalog_key, catalog_json, updated_at) VALUES (?, ?, ?)",
                (
                    "baiying-filter-catalog",
                    json.dumps(seeded_filter_catalog, ensure_ascii=False),
                    filter_updated_at,
                ),
            )
            changed = True
        else:
            try:
                current_filter_catalog = json.loads(filter_row["catalog_json"])
            except (TypeError, json.JSONDecodeError):
                current_filter_catalog = None
            if isinstance(current_filter_catalog, dict):
                current_roots = current_filter_catalog.get("categoryRoots")
                if "categoryRoots" not in current_filter_catalog:
                    updated_filter_catalog = dict(current_filter_catalog)
                    updated_filter_catalog["categoryRoots"] = seed_category_tree
                    connection.execute(
                        "UPDATE selection_category_catalog SET catalog_json = ? "
                        "WHERE catalog_key = 'baiying-filter-catalog'",
                        (json.dumps(updated_filter_catalog, ensure_ascii=False),),
                    )
                    changed = True
                elif isinstance(current_roots, list):
                    merged_roots = merge_missing_category_nodes(current_roots, seed_category_tree)
                    if merged_roots != current_roots:
                        updated_filter_catalog = dict(current_filter_catalog)
                        updated_filter_catalog["categoryRoots"] = merged_roots
                        connection.execute(
                            "UPDATE selection_category_catalog SET catalog_json = ? "
                            "WHERE catalog_key = 'baiying-filter-catalog'",
                            (json.dumps(updated_filter_catalog, ensure_ascii=False),),
                        )
                        changed = True

        if not changed:
            connection.rollback()
            return False
        connection.commit()
        return True
    except sqlite3.Error:
        connection.rollback()
        raise


def read_effective_category_catalog(connection: sqlite3.Connection) -> list[Dict[str, Any]]:
    """返回本机最完整的类目树，同时兼容旧的两个缓存键。"""
    primary = read_catalog(connection)
    filter_catalog = read_filter_catalog(connection)
    filter_roots = filter_catalog.get("categoryRoots", [])
    return merge_missing_category_nodes(primary, filter_roots if isinstance(filter_roots, list) else [])


def save_filter_catalog(connection: sqlite3.Connection, value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("百应筛选目录必须是 JSON 对象")
    filters = value.get("filters")
    groups = value.get("filterGroups")
    features = value.get("features")
    category_roots = value.get("categoryRoots")
    if not isinstance(filters, dict) or not isinstance(groups, dict) or not isinstance(features, list):
        raise ValueError("百应筛选目录缺少有效的筛选字段、分组或特色货品")
    payload = {
        "filters": filters,
        "filterGroups": groups,
        "features": features,
        "categoryRoots": category_roots if isinstance(category_roots, list) else [],
    }
    updated_at = now_text()
    connection.execute(
        "INSERT INTO selection_category_catalog(catalog_key, catalog_json, updated_at) VALUES ('baiying-filter-catalog', ?, ?) "
        "ON CONFLICT(catalog_key) DO UPDATE SET catalog_json=excluded.catalog_json, updated_at=excluded.updated_at",
        (json.dumps(payload, ensure_ascii=False), updated_at),
    )
    connection.commit()
    payload["updatedAt"] = updated_at
    return {"ok": True, "filterCatalog": payload}


def prune_stale_category_selections(connection: sqlite3.Connection) -> Dict[str, Any]:
    """按已刷新百应目录清理所有设置范围内失效的一级/二级/三级勾选。"""
    roots = read_catalog(connection)
    if not roots:
        raise ValueError("百应类目目录为空，未清理任何已保存勾选")

    separator = "__RC_CASCADER_SPLIT__"

    def canonical_ids(values: Any, options: list[Dict[str, Any]]) -> list[str]:
        if not isinstance(values, list):
            return []
        lookup: Dict[str, str] = {}
        for node in options:
            if not isinstance(node, dict):
                continue
            node_id = str(node.get("id", "")).strip()
            compatible_id = node_id.rsplit(separator, 1)[-1]
            if node_id:
                lookup[node_id] = node_id
                lookup.setdefault(compatible_id, node_id)
        result: list[str] = []
        for value in values:
            raw_id = str(value or "").strip()
            match = lookup.get(raw_id) or lookup.get(raw_id.rsplit(separator, 1)[-1])
            if match and match not in result:
                result.append(match)
        return result

    rows = connection.execute(
        "SELECT scope_key, settings_json FROM selection_settings WHERE settings_json IS NOT NULL"
    ).fetchall()
    updated_scopes = 0
    removed_selections = 0
    for row in rows:
        try:
            settings = json.loads(row["settings_json"])
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(settings, dict):
            continue
        selection = settings.get("categorySelection")
        if not isinstance(selection, dict):
            continue

        old_values = [selection.get(level, []) for level in ("level1", "level2", "level3")]
        selected_roots = canonical_ids(old_values[0], roots)
        selected_root_set = set(selected_roots)
        second_options = [
            child for root in roots if isinstance(root, dict) and str(root.get("id", "")) in selected_root_set
            for child in root.get("children", []) if isinstance(child, dict)
        ]
        selected_seconds = canonical_ids(old_values[1], second_options)
        selected_second_set = set(selected_seconds)
        third_options = [
            child for second in second_options if str(second.get("id", "")) in selected_second_set
            for child in second.get("children", []) if isinstance(child, dict)
        ]
        selected_thirds = canonical_ids(old_values[2], third_options)
        new_selection = {**selection, "level1": selected_roots, "level2": selected_seconds, "level3": selected_thirds}
        removed_selections += sum(
            max(0, (len(values) if isinstance(values, list) else 0) - len(cleaned))
            for values, cleaned in zip(old_values, (selected_roots, selected_seconds, selected_thirds))
        )
        if new_selection != selection:
            settings["categorySelection"] = new_selection
            connection.execute(
                "UPDATE selection_settings SET settings_json = ?, updated_at = ? WHERE scope_key = ?",
                (json.dumps(settings, ensure_ascii=False), now_text(), str(row["scope_key"])),
            )
            updated_scopes += 1
    if updated_scopes:
        connection.commit()
    return {"ok": True, "updatedScopes": updated_scopes, "removedSelections": removed_selections}


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="选品设置与百应类目缓存")
    parser.add_argument("--db", default=DEFAULT_DB_PATH)
    commands = parser.add_subparsers(dest="command", required=True)
    get_parser = commands.add_parser("get")
    get_parser.add_argument("--account-id", type=int)
    save_parser = commands.add_parser("save")
    save_parser.add_argument("--account-id", type=int)
    catalog_parser = commands.add_parser("catalog")
    catalog_parser.add_argument("action", choices=("get", "save", "filters-get", "filters-save", "selections-prune"))
    catalog_parser.add_argument("--mode", choices=("merge", "root-snapshot", "children"), default="merge")
    catalog_parser.add_argument("--parent-id")
    quota_parser = commands.add_parser("quota")
    quota_sub = quota_parser.add_subparsers(dest="quota_action", required=True)
    quota_get = quota_sub.add_parser("get")
    quota_get.add_argument("--account-id", type=int, required=True)
    quota_record = quota_sub.add_parser("record")
    quota_record.add_argument("--account-id", type=int, required=True)
    quota_record.add_argument("--product-id", required=True)
    quota_record.add_argument("--videos", type=int, default=0)
    args = parser.parse_args(argv)
    connection = connect(Path(args.db))
    try:
        if args.command == "get":
            seed_or_supplement_catalog(connection)
            settings = load_settings(connection, args.account_id)
            return emit({"ok": True, "settings": settings, "categories": read_effective_category_catalog(connection)})
        if args.command == "save":
            raw = sys.stdin.readline()
            if not raw:
                raise ValueError("未收到设置 JSON")
            return emit(save_settings(connection, args.account_id, json.loads(raw)))
        if args.command == "catalog" and args.action == "get":
            seed_or_supplement_catalog(connection)
            return emit({"ok": True, "categories": read_effective_category_catalog(connection)})
        if args.command == "catalog" and args.action == "filters-get":
            seed_or_supplement_catalog(connection)
            return emit({"ok": True, "filterCatalog": read_filter_catalog(connection)})
        if args.command == "catalog" and args.action == "filters-save":
            raw = sys.stdin.readline()
            if not raw:
                raise ValueError("未收到百应筛选目录 JSON")
            return emit(save_filter_catalog(connection, json.loads(raw)))
        if args.command == "catalog" and args.action == "selections-prune":
            return emit(prune_stale_category_selections(connection))
        if args.command == "quota" and args.quota_action == "get":
            today = datetime.now().date().isoformat()
            usage_rows = connection.execute(
                "SELECT product_id, video_count FROM selection_daily_usage WHERE usage_date = ? AND account_id = ?",
                (today, args.account_id),
            ).fetchall()
            return emit({
                "ok": True,
                "account": quota_usage(connection, [args.account_id]),
                "productUsage": {str(row["product_id"]): int(row["video_count"]) for row in usage_rows},
            })
        if args.command == "quota" and args.quota_action == "record":
            return emit(record_quota_usage(connection, args.account_id, args.product_id, args.videos))
        raw = sys.stdin.readline()
        if not raw:
            raise ValueError("未收到百应类目 JSON")
        return emit(save_catalog(connection, json.loads(raw), args.mode, args.parent_id))
    except (ValueError, json.JSONDecodeError, sqlite3.Error) as error:
        return emit({"ok": False, "error": str(error)}, 1)
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
