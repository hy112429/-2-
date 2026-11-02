#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read-only export of local product history for first-time shared registry import."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if not exists:
        return set()
    return {str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')}


def _choose(columns: set[str], *names: str) -> str | None:
    return next((name for name in names if name in columns), None)


def export_items(database: Path) -> list[dict[str, Any]]:
    if not database.is_file():
        raise FileNotFoundError(f"本地数据库不存在：{database}")
    uri = database.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        account_columns = _columns(connection, "accounts")
        account_id_column = _choose(account_columns, "id")
        douyin_column = _choose(account_columns, "douyin_id")
        account_keys: dict[int, str] = {}
        if account_id_column and douyin_column:
            account_keys = {
                int(row["id"]): str(row["douyin_id"] or "").strip()
                for row in connection.execute("SELECT id, douyin_id FROM accounts")
            }

        products: dict[str, dict[str, Any]] = {}

        def add_rows(table: str, account_columns_to_try: tuple[str, ...]) -> None:
            columns = _columns(connection, table)
            product_column = _choose(columns, "product_id", "productId", "goods_id")
            owner_column = _choose(columns, *account_columns_to_try)
            title_column = _choose(columns, "title", "product_name", "name")
            if not product_column:
                return
            selected = [f'"{product_column}" AS product_id']
            selected.append(f'"{owner_column}" AS owner_id' if owner_column else "NULL AS owner_id")
            selected.append(f'"{title_column}" AS title' if title_column else "NULL AS title")
            where = f'WHERE "{product_column}" IS NOT NULL AND TRIM(CAST("{product_column}" AS TEXT)) <> \'\''
            if table == "selection_quota_events" and "event_type" in columns:
                where += " AND event_type='cart'"
            for row in connection.execute(f'SELECT {", ".join(selected)} FROM "{table}" {where}'):
                product_id = str(row["product_id"] or "").strip()
                if not product_id:
                    continue
                owner_id = row["owner_id"]
                account_key = account_keys.get(int(owner_id), "") if owner_id is not None else ""
                item = products.setdefault(product_id, {"productId": product_id})
                if account_key:
                    item["accountKey"] = account_key
                if row["title"] and not item.get("title"):
                    item["title"] = str(row["title"]).strip()[:512]

        add_rows("products", ("account_row_id", "account_id"))
        add_rows("selection_quota_events", ("account_id", "account_row_id"))
        return [products[key] for key in sorted(products)]
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="只读导出本机已记录商品 ID，供共享登记服务首次导入")
    parser.add_argument("--db", required=True, help="本机客户端 SQLite 数据库路径")
    parser.add_argument("--output", required=True, help="输出 JSON 路径")
    args = parser.parse_args()
    items = export_items(Path(args.db))
    output = Path(args.output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"items": items}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": True, "count": len(items), "output": str(output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
