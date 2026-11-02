#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""客户端数据路径默认值；Electron 运行时通过环境变量传入安装版目录。"""

from __future__ import annotations

import os
from pathlib import Path

LEGACY_CLIENT_ROOT = Path(r"D:\software\投广客户端")
DATA_DIR = Path(os.environ.get("TOUGUANG_DATA_DIR", str(LEGACY_CLIENT_ROOT / "data")))
DOWNLOADS_DIR = Path(
    os.environ.get("TOUGUANG_DOWNLOADS_DIR", str(LEGACY_CLIENT_ROOT / "downloads"))
)
DEFAULT_DB_PATH = str(DATA_DIR / "ad_client.db")
DEFAULT_DOWNLOAD_ROOT = str(DOWNLOADS_DIR)
