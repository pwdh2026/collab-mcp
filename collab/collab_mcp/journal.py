"""任务级事件日志（journal）— 借鉴 MAF 的事件溯源 / 时间旅行设计。

每个任务一个 collab/journal/<task_id>.jsonl，追加式结构化事件，支持完整回放，
审计不再依赖全局 server.log。读取时损坏行自动跳过。

写入一致性：采用"同目录锁文件互斥 + 追加 + fsync"（与 notify_daemon 的
daemon.lock 同一套模式），跨 Windows NTFS 与 VM hgfs 均可用：
- flock（fcntl）在 hgfs 上不可靠，因此统一用 O_CREAT|O_EXCL 锁文件；
- 单条事件 JSON 序列化后一行写入，行级原子性有锁保证，不会交错；
- 崩溃安全：锁文件带 30 秒陈旧检测自动清理；事件先落盘、工具调用方后改任务状态。

用法：
    append_journal_event(task_id, "claimed", detail="PC-B")
    read_task_journal(task_id)  # 按时间顺序返回事件列表
"""

import json
import os
import time
from pathlib import Path

from .config import COLLAB_DIR
from .identity import current_identity
from .logging_setup import logger
from .utils import now_iso

JOURNAL_DIR = COLLAB_DIR / "journal"
_LOCK_STALE_SECONDS = 30  # 崩溃遗留锁文件的自动清理阈值
_LOCK_WAIT_SECONDS = 10.0  # 获取锁的最大等待时间


def _lock_path(task_id: str) -> Path:
    return JOURNAL_DIR / f".{task_id}.lock"


def _acquire_lock(lock_path: Path) -> bool:
    """O_CREAT|O_EXCL 互斥获取锁文件；陈旧锁自动清理。"""
    deadline = time.monotonic() + _LOCK_WAIT_SECONDS
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return True
        except (FileExistsError, PermissionError):
            # Windows 上：文件刚被其他线程创建并短暂持有（create→close 窗口）时，
            # os.open(O_CREAT|O_EXCL) 会抛 PermissionError(13) 而非 FileExistsError——
            # 这是因为 Windows 的共享违规（sharing violation）被 C open() 翻译成
            # EACCES(13)，而 CPython 没有把它归一到 FileExistsError(183)。
            # 参考：CPython 自己在 O_EXCL 竞态下也会遇到 FileExistsError 之外的错误
            #   （issue #13303 的字节码文件创建修复，见 cpython-checkins 2011-10）：
            #   https://mail.python.org/pipermail/python-checkins/2011-October/108665.html
            # 以及 Python-list 讨论 os.open 无法区分"权限错误/占用"：
            #   https://mail.python.org/archives/list/python-list@python.org/thread/XJTM5K5MCUEAWXUM5DS6INLQEYCDTOII/
            # 因此这里把 PermissionError 与 FileExistsError 同等对待：都按"被占用"
            # 重试，否则并发追加会偶发失败。此重试是必要的，勿删。
            try:
                if time.time() - lock_path.stat().st_mtime > _LOCK_STALE_SECONDS:
                    lock_path.unlink()
                    continue
            except OSError:
                pass
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.02)
        except OSError:
            return False


def _release_lock(lock_path: Path) -> None:
    try:
        lock_path.unlink()
    except OSError:
        pass


def append_journal_event(
    task_id: str,
    event_type: str,
    detail: str = "",
    identity: str = "",
) -> bool:
    """追加一条任务事件到 journal/<task_id>.jsonl。

    返回是否写入成功；失败仅记日志，不阻塞调用方（journal 是审计数据，
    任务状态文件才是事实来源）。
    """
    JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    event = {
        "ts": now_iso(),
        "type": event_type,
        "task_id": task_id,
        "identity": identity or current_identity() or "local",
    }
    if detail:
        event["detail"] = detail
    line = json.dumps(event, ensure_ascii=False) + "\n"

    lock_path = _lock_path(task_id)
    if not _acquire_lock(lock_path):
        logger.error(f"journal 锁获取失败，跳过事件 [{task_id}] {event_type}")
        return False
    try:
        journal_file = JOURNAL_DIR / f"{task_id}.jsonl"
        with open(journal_file, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        return True
    except OSError as e:
        logger.error(f"journal 写入失败 [{task_id}] {event_type}: {e}")
        return False
    finally:
        _release_lock(lock_path)


def read_task_journal(task_id: str, limit: int = 200) -> list[dict]:
    """按时间顺序读取任务事件日志（尾部截断），损坏行跳过。"""
    journal_file = JOURNAL_DIR / f"{task_id}.jsonl"
    if not journal_file.exists():
        return []
    events = []
    try:
        with open(journal_file, encoding="utf-8") as f:
            for line in f.readlines()[-limit:]:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    logger.warning(f"跳过损坏 journal 行 [{task_id}]")
                    continue
    except OSError as e:
        logger.warning(f"读取 journal 失败 [{task_id}]: {e}")
    return events
