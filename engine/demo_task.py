#!/usr/bin/env python3
"""任务进度演示脚本（T-11）：模拟 8 步、约 4 秒的任务，用于验证
「实时进度回传」与「取消」两条链路。将来真实抓取任务照这个格式输出。

协议：每行一条 JSON（NDJSON）
  {"type":"stage", "stage":"…", "percent":30}
  {"type":"log",   "level":"info", "message":"…"}
  {"type":"error", "message":"…"}             # 中途失败/取消
  {"type":"result", …任意字段…}                # 成功终态
"""

import json
import sys
import time

running = True


def emit(event: dict) -> None:
    print(json.dumps(event, ensure_ascii=False), flush=True)


def on_signal(_signum, _frame) -> None:
    global running
    running = False


def main() -> int:
    import signal

    # Windows 上仅 SIGTERM 可注册即可；taskkill /F 是强杀，不走这里
    signal.signal(signal.SIGTERM, on_signal)
    try:
        signal.signal(signal.SIGINT, on_signal)
    except ValueError:
        pass

    emit({"type": "stage", "stage": "任务开始", "percent": 0})
    for step in range(1, 9):
        if not running:
            emit({"type": "error", "message": "任务已取消（脚本收到中断信号）"})
            return 2
        percent = int(step * 100 / 8)
        emit({"type": "stage", "stage": f"步骤 {step}/8（模拟抓取）", "percent": percent})
        emit({"type": "log", "level": "info", "message": f"第 {step} 步完成，累计进度 {percent}%"})
        time.sleep(0.5)

    emit({"type": "result", "ok": True, "steps": 8, "duration_ms": 4000})
    return 0


if __name__ == "__main__":
    sys.exit(main())