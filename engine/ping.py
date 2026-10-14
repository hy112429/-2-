#!/usr/bin/env python3
"""连通性自检脚本（T-11）：同时是 NDJSON 进度协议的最小示范。

协议约定（引擎脚本与客户端之间，走 stdout，每行一条 JSON）：
  * 带 "type" 字段的行 = 事件：
      {"type":"stage", "stage":"…", "percent":30}   进度
      {"type":"log",   "level":"info", "message":"…"} 日志
      {"type":"error", "message":"…"}               失败终态
      {"type":"result", …任意字段…}                  成功终态
  * type 为 result 的行是最终结果，发完脚本就该退出。
"""

import json
import platform
import sys
import time


def emit(event: dict) -> None:
    print(json.dumps(event, ensure_ascii=False), flush=True)


def main() -> int:
    try:
        import requests
    except ImportError:
        requests = None  # type: ignore[assignment]

    emit({"type": "stage", "stage": "定位 Python 解释器", "percent": 25})
    time.sleep(0.25)
    emit({"type": "log", "level": "info", "message": f"运行时：Python {platform.python_version()}，{platform.platform()}"})
    emit({"type": "stage", "stage": "检查依赖与工作目录", "percent": 60})
    time.sleep(0.25)
    emit({"type": "stage", "stage": "完成", "percent": 100})
    emit({
        "type": "result",
        "ok": True,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "requests": requests.__version__ if requests else "未安装",
        "engine_dir": __file__,
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())