#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
read_vod.py —— 读取探针捕获的头条/西瓜视频直链（toutiaovod.com）

探针（probe.ts）在用户用客户端内置浏览器打开头条视频页时，会把 Network 里
toutiaovod.com 的媒体地址写入 %TEMP%\\baiying_probe.jsonl（action=vod）。
本脚本读取并去重，输出 JSON 数组供界面一键导入下载。

输出（stdout，单行 JSON）：
  {"type":"result","links":["https://v26-web.toutiaovod.com/...", ...]}
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from typing import List


def read_vod_links(probe_log: str) -> List[str]:
    links: List[str] = []
    seen = set()
    if not os.path.exists(probe_log):
        return links
    with open(probe_log, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if obj.get("action") == "vod":
                url = obj.get("url") or ""
                if url and url not in seen:
                    seen.add(url)
                    links.append(url)
    return links


def main() -> int:
    probe_log = os.path.join(tempfile.gettempdir(), "baiying_probe.jsonl")
    links = read_vod_links(probe_log)
    print(json.dumps({"type": "result", "links": links}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
