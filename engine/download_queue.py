#!/usr/bin/env python3
"""批量下载任务队列引擎（下载全部 / 任务队列 UI 的数据与调度核心）。

能力：
  * 并发下载（--workers 可配，默认 2）
  * 失败自动重试，指数退避（base 2s → 4s → 8s…），最多 --max-retries 次
  * 任务间随机间隔限速（--min-delay / --max-delay 秒），避免请求节奏固定被风控识别
  * 断点续传：目标文件已存在且非空 → 直接跳过（标 skipped）
  * 状态持久化到 SQLite（与 db.py 同一个库的 tasks 表），重启后可查可恢复
  * 取消：进程收到 SIGTERM/SIGINT 后停止调度新任务，正在下载的立即中断

输入：任务列表 JSON 走 stdin（与登录态同一通道，不落盘）：
  [ {"id":"1","url":"https://…/a.mp4","dest":"D:\\…\\1.mp4"}, … ]

事件（stdout，每行一条 JSON）：
  {"type":"task","tid":"1","status":"running","percent":37,"attempts":0,"message":"…"}
  {"type":"stage","stage":"任务 2/5 完成","percent":40}
  {"type":"result","data":{"total":5,"success":3,"failed":1,"skipped":1}}

注：`tid` 是队列任务 ID；引擎层的 `taskId` 字段保留给 engine.ts 做任务路由。

CLI：
  python download_queue.py --workers 2 --max-retries 3 --min-delay 1 --max-delay 3
"""

import argparse
import hashlib
import json
import random
import sqlite3
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager, nullcontext
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Optional

try:
    from .client_paths import DEFAULT_DB_PATH
    from .asset_library import ensure_schema as ensure_asset_schema, register_asset
except ImportError:
    from client_paths import DEFAULT_DB_PATH
    from asset_library import ensure_schema as ensure_asset_schema, register_asset

TASK_STATUSES = {"pending", "running", "success", "failed", "skipped"}

# 每个任务一个锁，保护进度事件不交叉
_event_lock = Lock()
_db_lock = Lock()
cancel_flag = {"value": False}


class NoRetryError(RuntimeError):
    """不可重试错误：重试同一 URL 无意义（如签名已过期的直链），直接失败。"""


@contextmanager
def visual_product_lock(db_path: Path, task: Dict[str, Any], dest: Path):
    """同一商品的画面检查、成品落盘和登记跨下载进程串行。"""
    product_id = str(task.get("product_id") or "")
    if not product_id.isdigit() or dest.parent.name != product_id:
        raise ValueError("画面去重任务缺少匹配的商品 ID 下载目录")
    account_scope = str(task.get("account_id") or dest.parents[2])
    key = hashlib.sha256(f"{account_scope}:{product_id}".encode("utf-8")).hexdigest()[:24]
    lock_dir = db_path.parent / "download-dedupe-locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    with open(lock_dir / f"{key}.lock", "a+b") as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if sys.platform == "win32":
            import msvcrt
            deadline = time.monotonic() + 600
            while True:
                if cancel_flag["value"]:
                    raise RuntimeError("任务已取消，停止等待同商品画面检查")
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("同商品画面检查等待超过 10 分钟")
                    time.sleep(0.25)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def emit(event: dict) -> None:
    with _event_lock:
        print(json.dumps(event, ensure_ascii=False), flush=True)


def now_text() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ============================================================ SQLite（持久化）

def connect_db(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False：连接会被多个 worker 线程共用（配合 _db_lock 串行写）
    con = sqlite3.connect(db_path, check_same_thread=False)
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")
    # 保证 tasks 表存在（真实库里由 db.py 建；这里是兜底，去掉外键避免依赖 videos 表）
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS tasks (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            task_type    TEXT    NOT NULL,
            status       TEXT    NOT NULL DEFAULT 'pending',
            attempts     INTEGER NOT NULL DEFAULT 0,
            progress     INTEGER NOT NULL DEFAULT 0,
            message      TEXT,
            created_at   TEXT    NOT NULL,
            updated_at   TEXT    NOT NULL
        );
        """
    )
    return con


def upsert_task(con: sqlite3.Connection, task_id: str, url: str, status: str,
                attempts: int, progress: int, message: Optional[str]) -> None:
    """按 url 作为任务标识 upsert（url 唯一，避免重复插入）。"""
    now = now_text()
    with _db_lock:
        row = con.execute(
            "SELECT id FROM tasks WHERE message = ? AND task_type = 'download' LIMIT 1",
            (url,),
        ).fetchone()
        if row is None:
            con.execute(
                "INSERT INTO tasks (task_type, status, attempts, progress, message, created_at, updated_at) "
                "VALUES ('download', ?, ?, ?, ?, ?, ?)",
                (status, attempts, progress, url, now, now),
            )
        else:
            con.execute(
                "UPDATE tasks SET status = ?, attempts = ?, progress = ?, message = ?, updated_at = ? WHERE id = ?",
                (status, attempts, progress, url, now, row[0]),
            )
        con.commit()


def recover_stale_running(con: sqlite3.Connection) -> None:
    """重启恢复：上次进程被杀残留的 running 重置为 failed（进程已不存在）。"""
    con.execute(
        "UPDATE tasks SET status = 'failed', message = '进程中断，任务未完成', updated_at = ? "
        "WHERE task_type = 'download' AND status = 'running'",
        (now_text(),),
    )
    con.commit()


# ============================================================ 下载逻辑

# 与 douyin_parse.py 的 IPHONE_UA 保持一致：抖音 CDN 对 requests 默认 UA（python-requests/x）
# 通常会降速/限流。最初「全速」的 download.py 链路正是带了这组移动端浏览器请求头
# （实测 5.66MB/1.84s）。Referer 是关键，缺了容易被判成爬虫。
DOWNLOAD_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/17.2 Mobile/15E148 Safari/604.1"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://www.douyin.com/",
    "Upgrade-Insecure-Requests": "1",
}


def _ffprobe() -> str:
    """定位 ffprobe：打包环境从 <engine>/bin/ffprobe.exe 取，否则走 PATH。"""
    bundled = Path(__file__).resolve().parent / "bin" / "ffprobe.exe"
    if bundled.exists():
        return str(bundled)
    return "ffprobe"


def probe_codec(dest: Path, stream_sel: str) -> str:
    """查指定流（v:0 / a:0）的编码名；查不到返回空串。"""
    try:
        result = subprocess.run(
            [_ffprobe(), "-v", "error", "-select_streams", stream_sel,
             "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(dest)],
            capture_output=True, text=True, timeout=60,
        )
        return (result.stdout or "").strip().splitlines()[0] if result.stdout.strip() else ""
    except Exception:
        return ""


def ensure_h264(dest: Path) -> str:
    """
    规格要求 MP4 + H.264 + AAC 128k。小红书等平台源是 HEVC(H.265)，
    下载后统一转码为 H.264/AAC（仅当编码不符时，避免无谓重编码损失画质）。
    返回处理说明（空串 = 无需转码）。
    """
    vcodec = probe_codec(dest, "v:0")
    acodec = probe_codec(dest, "a:0")
    if vcodec == "h264" and acodec == "aac":
        return ""
    tmp = dest.with_suffix(".h264.mp4")
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(dest),
             "-c:v", "libx264", "-crf", "20", "-preset", "medium",
             "-c:a", "aac", "-b:a", "128k",
             "-movflags", "+faststart", str(tmp)],
            capture_output=True, text=True, timeout=600,
        )
        tmp.replace(dest)
        return f"已转码 {vcodec or '?'}→H.264/AAC"
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        return f"转码失败：{exc}（已保留原文件）"


def visual_anchor_paths(con: sqlite3.Connection, task: Dict[str, Any], dest: Path) -> list[Path]:
    """只比较同账号同商品的已登记视频，以及当前商品目录内的成品。"""
    paths: list[Path] = []
    account_id = task.get("account_id")
    if account_id is not None:
        with _db_lock:
            if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='assets'").fetchone():
                rows = con.execute(
                    "SELECT file_path FROM assets WHERE account_id=? AND product_id=? "
                    "AND source_type='web_download' AND status='ready' ORDER BY id DESC",
                    (int(account_id), str(task["product_id"])),
                ).fetchall()
                paths.extend(Path(str(row[0])) for row in rows)
    if dest.parent.is_dir():
        paths.extend(dest.parent.glob("*.mp4"))
    unique: dict[str, Path] = {}
    for path in paths:
        if path != dest and path.is_file() and path.stat().st_size > 0:
            unique[str(path.resolve())] = path
    return list(unique.values())


def visual_duplicate(con: sqlite3.Connection, task: Dict[str, Any], staged: Path,
                     dest: Path) -> tuple[Optional[str], float, str]:
    """证据不足时保留候选，并在任务结果中说明未完成画面检查。"""
    try:
        try:
            from .video_visual_dedupe import (DOWNLOAD_MAX_SAMPLES, DOWNLOAD_OVERLAP_LIMIT,
                                               video_signature, visual_overlap)
        except ImportError:
            from video_visual_dedupe import (DOWNLOAD_MAX_SAMPLES, DOWNLOAD_OVERLAP_LIMIT,
                                             video_signature, visual_overlap)
        frames = video_signature(staged, DOWNLOAD_MAX_SAMPLES)
        if not frames:
            return None, 0.0, "画面无法解码，未据此判重"
        for anchor in visual_anchor_paths(con, task, dest):
            other = video_signature(anchor, DOWNLOAD_MAX_SAMPLES)
            if not other:
                continue
            overlap = visual_overlap(frames, other)
            if overlap >= DOWNLOAD_OVERLAP_LIMIT:
                return anchor.name, overlap, ""
        return None, 0.0, ""
    except Exception as error:  # OpenCV 解码错误也不能作为“相似”证据
        return None, 0.0, f"画面识别未完成，已保留视频供复核：{error}"


def download_one(session: Any, task: Dict[str, Any], dest: Path, timeout: float,
                 db_con: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """执行一次下载。成功返回统计，失败抛异常。"""
    url = task["url"]

    # 断点续传：已存在且非空 → 跳过
    if dest.exists() and dest.stat().st_size > 0:
        return {
            "status": "skipped",
            "bytes": dest.stat().st_size,
            "message": f"文件已存在（{dest.stat().st_size} 字节），跳过下载",
            "file_path": str(dest),
        }

    with session.get(
        url, headers=DOWNLOAD_HEADERS, timeout=timeout, stream=True, allow_redirects=True
    ) as response:
        if response.status_code != 200:
            # 小红书带时效签名的直链（sns-*.xhscdn.com + sign&t）抓取后需尽快下载，
            # 过时会返回 403。给出针对性中文提示；且这是「不可重试」错误——
            # 重试同一个已过期的 URL 毫无意义（实测 3 次徒劳重试白等退避延迟）。
            if response.status_code == 403 and "xhscdn.com" in url:
                raise NoRetryError("直链签名已过期（403）：请重新解析该链接获取新直链后下载")
            raise RuntimeError(f"HTTP {response.status_code}")

        total = int(response.headers.get("Content-Length") or 0)
        written = 0
        visual_check = task.get("visual_dedupe") is True
        # 画面检查先放在账号目录的临时区，候选不提前进入商品成品目录。
        tmp = (dest.parents[2] / ".download-staging" / f"{uuid.uuid4().hex}.mp4"
               if visual_check else dest.with_suffix(dest.suffix + ".part"))
        tmp.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(tmp, "wb") as handle:
                # 注：分块保持 8KB + 每块上报（与最初基线一致），真实提速靠请求头（DOWNLOAD_HEADERS），
                # 分块大小对 CDN 吞吐影响有限，不再改动以免引入新变量。
                for chunk in response.iter_content(8192):
                    if cancel_flag["value"]:
                        raise RuntimeError("任务已取消（进程收到中断信号）")
                    if not chunk:
                        continue
                    handle.write(chunk)
                    written += len(chunk)
                    if total > 0:
                        percent = int(written * 100 / total)
                        emit({
                            "type": "task",
                            "status": "running",
                            "tid": task["id"],
                            "percent": percent,
                            "attempts": task["attempts"],
                            "message": f"下载中 {percent}%",
                        })
            # 千川开放平台地址须确实返回可识别的视频，不能把 200 HTML 错误页当 MP4 登记。
            if task.get("verify_video") and not probe_codec(tmp, "v:0"):
                raise NoRetryError("接口地址未返回可识别的视频文件（可能是过期直链或 HTML 错误页）")
            if total > 0 and written != total:
                raise RuntimeError(f"下载不完整：期望 {total} 字节，实际 {written} 字节")
            visual_note = ""
            if visual_check:
                if db_con is None:
                    raise ValueError("画面去重任务缺少素材库连接")
                matched_name, overlap, visual_note = visual_duplicate(db_con, task, tmp, dest)
                if matched_name:
                    return {"status": "skipped", "bytes": written,
                            "message": f"画面高度相似（重合 {overlap:.0%}，对应 {matched_name}），未保存到正式目录"}
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists() and dest.stat().st_size > 0:
                return {"status": "skipped", "bytes": dest.stat().st_size,
                        "message": "文件已存在，跳过下载", "file_path": str(dest)}
            tmp.replace(dest)
        finally:
            tmp.unlink(missing_ok=True)

    # 规格转码：非 H.264/AAC 时转码（小红书 HEVC 等），抖音 h264 源不重编码
    transcode_note = ensure_h264(dest)

    notes = [note for note in (transcode_note, visual_note if task.get("visual_dedupe") else "") if note]
    return {"status": "success", "bytes": written,
            "message": "下载完成" + (f"，{'；'.join(notes)}" if notes else ""), "file_path": str(dest)}


def run_task(session: Any, task: Dict[str, Any], max_retries: int, timeout: float,
             db_con: sqlite3.Connection, db_path: Optional[Path] = None) -> Dict[str, Any]:
    """带重试的单个任务执行。"""
    task["attempts"] = 0
    last_error = ""
    dest = Path(task["dest"])

    # 断点续传检查（不进 attempts，不算失败）
    if dest.exists() and dest.stat().st_size > 0:
        upsert_task(db_con, task["id"], task["url"], "skipped", 0, 100, "文件已存在，跳过")
        emit({"type": "task", "status": "skipped", "tid": task["id"], "percent": 100, "attempts": 0, "message": "文件已存在，跳过", "file_path": str(dest)})
        return {"status": "skipped", "id": task["id"]}

    for attempt in range(max_retries + 1):
        task["attempts"] = attempt
        emit({
            "type": "task",
            "status": "running",
            "tid": task["id"],
            "percent": 0,
            "attempts": attempt,
            "message": "开始下载" if attempt == 0 else f"第 {attempt} 次重试",
        })
        upsert_task(db_con, task["id"], task["url"], "running", attempt, 0, "下载中")
        try:
            visual_check = task.get("visual_dedupe") is True
            lock = (visual_product_lock(db_path or Path(DEFAULT_DB_PATH), task, dest) if visual_check
                    else nullcontext())
            with lock:
                result = (download_one(session, task, dest, timeout, db_con) if visual_check
                          else download_one(session, task, dest, timeout))
                # 只有本次真实下载成功，才足以证明 web_download 来源；已有文件跳过不作来源推断。
                if result["status"] == "success":
                    try:
                        with _db_lock:
                            ensure_asset_schema(db_con)
                            explicit_account = task.get("account_id")
                            register_asset(
                                db_con, str(dest), "web_download",
                                int(explicit_account) if explicit_account is not None else None,
                                str(task["product_id"]) if task.get("product_id") else None,
                            )
                    except (OSError, sqlite3.Error, ValueError) as error:
                        result["message"] = result.get("message", "下载完成") + f"；素材库登记待核查：{error}"
                        emit({
                            "type": "asset_registration_warning", "tid": task["id"],
                            "message": f"文件下载成功，但素材来源登记失败：{error}",
                        })
            upsert_task(db_con, task["id"], task["url"], result["status"], attempt, 100, result.get("message", ""))
            emit({
                "type": "task",
                "status": result["status"],
                "tid": task["id"],
                "percent": 100,
                "attempts": attempt,
                "message": result.get("message", ""),
                "file_path": result.get("file_path", ""),
            })
            return {**result, "id": task["id"]}
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
            if cancel_flag["value"]:
                upsert_task(db_con, task["id"], task["url"], "failed", attempt, 0, "任务已取消")
                emit({"type": "task", "status": "failed", "tid": task["id"], "percent": 0, "attempts": attempt, "message": "任务已取消"})
                return {"status": "failed", "id": task["id"], "message": "任务已取消"}
            # 不可重试错误（如签名过期直链）：不白等退避重试，直接判失败
            if isinstance(exc, NoRetryError):
                upsert_task(db_con, task["id"], task["url"], "failed", attempt, 0, last_error)
                emit({
                    "type": "task",
                    "status": "failed",
                    "tid": task["id"],
                    "percent": 0,
                    "attempts": attempt,
                    "message": f"失败：{last_error}",
                })
                return {"status": "failed", "id": task["id"], "message": last_error}
            if attempt < max_retries:
                delay = 2 ** attempt  # 指数退避：1s → 2s → 4s → 8s
                emit({
                    "type": "task",
                    "status": "retrying",
                    "tid": task["id"],
                    "percent": 0,
                    "attempts": attempt,
                    "message": f"失败（{last_error}），{delay}s 后第 {attempt + 1} 次重试",
                })
                upsert_task(db_con, task["id"], task["url"], "running", attempt, 0, f"等待重试：{last_error}")
                time.sleep(delay)
            else:
                upsert_task(db_con, task["id"], task["url"], "failed", attempt, 0, last_error)
                emit({
                    "type": "task",
                    "status": "failed",
                    "tid": task["id"],
                    "percent": 0,
                    "attempts": attempt,
                    "message": f"失败：{last_error}",
                })
                return {"status": "failed", "id": task["id"], "message": last_error}

    return {"status": "failed", "id": task["id"], "message": last_error}


# ============================================================ 主流程

def read_tasks() -> List[Dict[str, Any]]:
    # utf-8-sig：Windows PowerShell 管道常带 UTF-8 BOM，直接 json.loads 会报错
    raw = sys.stdin.buffer.read().decode("utf-8-sig").strip() if hasattr(sys.stdin, "buffer") else sys.stdin.read().strip()
    if not raw:
        raise ValueError("stdin 是空的，没收到任务列表 JSON")
    data = json.loads(raw)
    if not isinstance(data, list):
        raise ValueError("任务列表必须是 JSON 数组")
    for t in data:
        if not t.get("url") or not t.get("dest"):
            raise ValueError("每个任务必须包含 url 和 dest 字段")
        t.setdefault("id", t["url"])
    return data


def main() -> int:
    parser = argparse.ArgumentParser(description="批量下载任务队列引擎")
    parser.add_argument("--workers", type=int, default=2, help="并发数，默认 2")
    parser.add_argument("--max-retries", type=int, default=3, help="每个任务最大重试次数，默认 3")
    parser.add_argument("--min-delay", type=float, default=1.0, help="任务间随机间隔下限（秒）")
    parser.add_argument("--max-delay", type=float, default=3.0, help="任务间随机间隔上限（秒）")
    parser.add_argument("--timeout", type=float, default=30.0, help="单次下载超时秒数")
    parser.add_argument("--db", default=DEFAULT_DB_PATH, help="SQLite 路径（复用 db.py 的库）")
    args = parser.parse_args()

    import signal
    import requests

    signal.signal(signal.SIGTERM, lambda *_: cancel_flag.update(value=True))
    try:
        signal.signal(signal.SIGINT, lambda *_: cancel_flag.update(value=True))
    except ValueError:
        pass

    tasks = read_tasks()
    total = len(tasks)
    emit({"type": "stage", "stage": f"队列就绪：{total} 个任务，并发 {args.workers}", "percent": 0})

    db_con = connect_db(args.db)
    recover_stale_running(db_con)

    session = requests.Session()
    stats = {"success": 0, "failed": 0, "skipped": 0, "done": 0}
    done_lock = Lock()

    def worker_one(task: Dict[str, Any]) -> Dict[str, Any]:
        # 任务间随机间隔（限速）：先睡一段再取任务，打散固定节拍
        if not cancel_flag["value"]:
            time.sleep(random.uniform(args.min_delay, args.max_delay))
        result = run_task(session, task, args.max_retries, args.timeout, db_con, Path(args.db))
        with done_lock:
            stats["done"] += 1
            if result.get("status") == "success":
                stats["success"] += 1
            elif result.get("status") == "skipped":
                stats["skipped"] += 1
            else:
                stats["failed"] += 1
            percent = int(stats["done"] * 100 / total)
            emit({
                "type": "stage",
                "stage": f"任务 {stats['done']}/{total} 完成（成功 {stats['success']} / 失败 {stats['failed']} / 跳过 {stats['skipped']}）",
                "percent": percent,
            })
        return result

    try:
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
            list(pool.map(worker_one, tasks))
    except KeyboardInterrupt:
        cancel_flag.update(value=True)

    db_con.close()

    canceled = cancel_flag["value"]
    result_payload = {
        "total": total,
        "success": stats["success"],
        "failed": stats["failed"],
        "skipped": stats["skipped"],
        "canceled": canceled,
    }
    emit({"type": "stage", "stage": "全部结束" if not canceled else "已被取消，队列停止", "percent": 100})
    emit({"type": "result", "data": result_payload})
    return 0 if not canceled and stats["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
