#!/usr/bin/env python3
"""notify_daemon — 协作平台事件通知（L3 事件驱动）。

监控 collab/inbox/ 与 collab/chat/，发现新任务/新消息时：
1. 追加结构化事件到 collab/notifications/feed.jsonl（有界：最多保留 MAX_FEED_LINES 行）
2. 新任务同时以"系统通知"身份发一条消息到 chat/（get_chat_history 可见，重复幂等）
3. 状态记录在 collab/notifications/state.json

崩溃安全设计（修复 PC-B 审查 #1/#2/#4）：
- 先发射事件、最后才保存 state → 崩溃不会"吞掉"事件
- 新任务系统消息带去重：若该任务已有系统消息，不再重复发送（崩溃恢复时不刷屏）
- 系统消息用原子写入（临时文件 + fsync + os.replace），与 utils.safe_write_json 语义一致
- 并发 --once 用锁文件互斥（陈旧锁自动清除）

注：本脚本独立于 collab_mcp 包运行（不引入 MCP 依赖），
其中的 now_iso/原子写与 collab_mcp/utils.py 功能对应，改动时注意同步。

用法：
    python3 notify_daemon.py --once           # 单次扫描（配合 cron 每分钟执行）
    python3 notify_daemon.py --watch          # 前台循环（默认每 5 秒）
    python3 notify_daemon.py --dir <collab目录> --once

Python >= 3.10，无第三方依赖。
"""

import argparse
import hashlib
import json
import logging
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from schedule_core import materialize_due_schedules

SYSTEM_SENDER = "系统通知"
MAX_FEED_LINES = 2000  # feed.jsonl 保留最近 2000 条，防止无界增长
STALE_TASK_MINUTES_DEFAULT = 30  # 任务超时告警阈值（可用 NOTIFY_STALE_MINUTES 覆盖）
CLAIM_TIMEOUT_MINUTES_DEFAULT = 60  # 认领超时自动释放阈值（可用 NOTIFY_CLAIM_TIMEOUT_MINUTES 覆盖）

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stderr),
    ],
)
logger = logging.getLogger("notify-daemon")

VM_DEFAULT_DIR = "/mnt/hgfs/myshare/collab"


def _default_collab_dir() -> Path:
    """解析默认协作目录（与 collab_mcp/config.py 保持一致）：
    1. COLLAB_DIR 环境变量（可覆盖）
    2. VM 部署默认 /mnt/hgfs/myshare/collab（存在时）
    3. 回退到本脚本所在目录（Windows 开发/测试）
    """
    env = os.environ.get("COLLAB_DIR")
    if env:
        return Path(env)
    vm_default = Path(VM_DEFAULT_DIR)
    if vm_default.exists():
        return vm_default
    return Path(__file__).resolve().parent


def now_local_iso() -> str:
    """本地时间带时区偏移（与系统消息/chat 文件名同源，避免 UTC 差 8 小时）。"""
    return datetime.now().astimezone().isoformat()


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _atomic_write_json(path: Path, data) -> None:
    """原子写入（与 collab_mcp/utils.py::safe_write_json 语义一致）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:6]}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        with open(tmp, "rb+") as f:
            os.fsync(f.fileno())
    except OSError:
        pass
    os.replace(tmp, path)


def _scan_dir(dir_path: Path) -> dict[str, float]:
    """返回 {文件名: mtime}，mtime 用于检测原地修改。"""
    result = {}
    if not dir_path.is_dir():
        return result
    for p in sorted(dir_path.glob("*.json")):
        try:
            # 统一截断到整秒：VM(hgfs) 只报告整秒，主机带小数，
            # 不截断会导致两边 state 互相判定"文件已变"、每分钟重复刷事件。
            result[p.name] = float(int(p.stat().st_mtime))
        except OSError:
            continue
    return result


def _save_state(state_file: Path, current: dict) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(state_file, current)


def _stable_event_id(kind: str, filename: str) -> str:
    return hashlib.sha1(f"{kind}:{filename}".encode("utf-8")).hexdigest()[:8]


def _trim_feed(feed_file: Path) -> None:
    try:
        lines = feed_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    if len(lines) > MAX_FEED_LINES:
        feed_file.write_text(
            "\n".join(lines[-MAX_FEED_LINES:]) + "\n", encoding="utf-8"
        )


def _append_feed(feed_file: Path, event: dict) -> None:
    feed_file.parent.mkdir(parents=True, exist_ok=True)
    with open(feed_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")
    _trim_feed(feed_file)


def _system_message_exists(chat_dir: Path, task_id: str) -> bool:
    """该任务是否已有系统通知消息（崩溃恢复时避免重复发送）。"""
    for p in chat_dir.glob("*.json"):
        data = _read_json(p)
        if data.get("sender") == SYSTEM_SENDER and task_id in data.get("content", ""):
            return True
    return False


def _system_chat_message(chat_dir: Path, task: dict, stale: bool = False) -> None:
    """以系统身份发一条聊天消息（原子写入）。stale=True 时发 URGENT 超时告警。"""
    msg_id = uuid.uuid4().hex[:12]
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    if stale:
        content = (
            f"⚠️ URGENT 任务 [{task.get('id')}]「{task.get('title')}」已超时未完成"
            f"（指派给 {task.get('assignee', 'any')}）— 请中枢协调处理或 force_assign"
        )
    else:
        content = (
            f"🔔 新任务 [{task.get('id')}]「{task.get('title')}」已到达 → "
            f"{task.get('assignee', 'any')}"
        )
    message = {
        "id": msg_id,
        "sender": SYSTEM_SENDER,
        "content": content,
        "timestamp": now_local_iso(),
    }
    _atomic_write_json(chat_dir / f"{timestamp}_{msg_id}.json", message)


def _parse_iso_ts(value: str) -> datetime | None:
    """解析 ISO 时间戳；无时区按本地时区处理，解析失败返回 None。"""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.astimezone()
        return dt
    except (ValueError, TypeError):
        return None


def _stale_threshold_minutes() -> int:
    """超时阈值（分钟），环境变量 NOTIFY_STALE_MINUTES 可覆盖，最小 1。"""
    try:
        return max(1, int(os.environ.get("NOTIFY_STALE_MINUTES", str(STALE_TASK_MINUTES_DEFAULT))))
    except ValueError:
        return STALE_TASK_MINUTES_DEFAULT


def _claim_timeout_minutes() -> int:
    """认领超时阈值（分钟），环境变量 NOTIFY_CLAIM_TIMEOUT_MINUTES 可覆盖，最小 1。"""
    try:
        return max(
            1,
            int(os.environ.get("NOTIFY_CLAIM_TIMEOUT_MINUTES", str(CLAIM_TIMEOUT_MINUTES_DEFAULT))),
        )
    except ValueError:
        return CLAIM_TIMEOUT_MINUTES_DEFAULT


def _system_stale_message_exists(chat_dir: Path, task_id: str) -> bool:
    """该任务是否已有 URGENT 超时告警消息（崩溃补发时不刷屏）。"""
    for p in chat_dir.glob("*.json"):
        data = _read_json(p)
        content = data.get("content", "")
        if data.get("sender") == SYSTEM_SENDER and "URGENT" in content and task_id in content:
            return True
    return False


def _system_claim_stale_message(chat_dir: Path, data: dict, event: dict) -> None:
    """以系统身份发一条认领超时告警（原子写入）。"""
    msg_id = uuid.uuid4().hex[:12]
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    content = (
        f"⚠️ URGENT 任务 [{data.get('id')}]「{data.get('title')}」认领超时："
        f"{event.get('claimed_by', '?')} 已认领 {event.get('age_minutes', '?')} 分钟无进展，"
        f"已自动释放回 pending，可重新认领或由中枢 force_assign"
    )
    message = {
        "id": msg_id,
        "sender": SYSTEM_SENDER,
        "content": content,
        "timestamp": now_local_iso(),
    }
    _atomic_write_json(chat_dir / f"{timestamp}_{msg_id}.json", message)


def _system_claim_stale_message_exists(chat_dir: Path, task_id: str) -> bool:
    """该任务是否已有认领超时告警消息（崩溃补发时不刷屏）。"""
    for p in chat_dir.glob("*.json"):
        data = _read_json(p)
        content = data.get("content", "")
        if data.get("sender") == SYSTEM_SENDER and "认领超时" in content and task_id in content:
            return True
    return False


def _scan_stale_tasks(collab_dir: Path, inbox_dir: Path, chat_dir: Path,
                      feed_file: Path, stale_alerted: dict,
                      emit: bool, skip_ids: set[str] | None = None) -> list[dict]:
    """检测超时未完成的任务，产生 stale_task 告警（每任务只告警一次）。"""
    threshold = _stale_threshold_minutes()
    now = datetime.now().astimezone()
    events = []
    current_ids = set()

    for p in sorted(inbox_dir.glob("*.json")):
        data = _read_json(p)
        task_id = data.get("id", p.stem)
        current_ids.add(task_id)
        if skip_ids and task_id in skip_ids:
            # v1.8.1：本轮刚被认领超时释放的任务，不再叠加 stale 告警
            continue
        if data.get("status") == "needs_review":
            # v1.8.0：待复核任务等 hub 审批，不算执行超时
            continue
        if data.get("status") == "blocked":
            # v1.9.0：流水线未解锁步骤不可执行，不算超时
            continue
        if data.get("status") in ("failed", "skipped"):
            # v1.9.1：已失败/跳过的步骤不可执行，不算超时
            continue
        created = _parse_iso_ts(data.get("created_at"))
        if created is None:
            continue
        age_minutes = (now - created).total_seconds() / 60.0
        if age_minutes < threshold or task_id in stale_alerted:
            continue

        event = {
            "id": _stable_event_id("stale", task_id),
            "ts": now_local_iso(),
            "type": "stale_task",
            "task_id": task_id,
            "title": data.get("title", ""),
            "assignee": data.get("assignee", "any"),
            "age_minutes": round(age_minutes, 1),
            "threshold_minutes": threshold,
        }
        if emit:
            _append_feed(feed_file, event)
            if not _system_stale_message_exists(chat_dir, task_id):
                _system_chat_message(chat_dir, data, stale=True)
        events.append(event)
        stale_alerted[task_id] = now_local_iso()
        logger.warning(f"⚠️ stale_task: {task_id} 已等待 {age_minutes:.0f} 分钟")

    # 清理已不在 inbox 的任务（已完成/已删除）的告警记录
    for tid in list(stale_alerted):
        if tid not in current_ids:
            del stale_alerted[tid]
    return events


def _acquire_journal_lock(lock_path: Path) -> bool:
    """journal 追加互斥锁：30 秒陈旧清理；Windows 共享违规按占用重试。"""
    deadline = time.monotonic() + 10.0
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return True
        except (FileExistsError, PermissionError):
            try:
                if time.time() - lock_path.stat().st_mtime > 30:
                    lock_path.unlink()
                    continue
            except OSError:
                pass
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.02)
        except OSError:
            return False


def _append_journal_event(collab_dir: Path, task_id: str, event_type: str,
                          detail: str = "") -> None:
    """与 collab_mcp/journal.py 同格式的追加写入（daemon 独立实现，注意同步）。"""
    journal_dir = collab_dir / "journal"
    journal_dir.mkdir(parents=True, exist_ok=True)
    lock_path = journal_dir / f".{task_id}.lock"
    if not _acquire_journal_lock(lock_path):
        logger.error(f"journal 锁获取失败，跳过事件 [{task_id}] {event_type}")
        return
    try:
        event = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "type": event_type,
            "task_id": task_id,
            "identity": "system",
        }
        if detail:
            event["detail"] = detail
        with open(journal_dir / f"{task_id}.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
            f.flush()
    except OSError as e:
        logger.error(f"journal 写入失败 [{task_id}] {event_type}: {e}")
    finally:
        try:
            lock_path.unlink()
        except OSError:
            pass


def _scan_claim_timeouts(collab_dir: Path, inbox_dir: Path, chat_dir: Path,
                         feed_file: Path, claim_alerted: dict,
                         emit: bool) -> tuple[list[dict], set[str]]:
    """检测认领超时的 in_progress 任务：自动释放回 pending + 告警（每任务一次）。

    返回 (事件列表, 被释放的文件名集合)——调用方据此跳过重复的 new_task 通知。
    """
    now = datetime.now().astimezone()
    events = []
    released_names = set()
    current_ids = set()

    for p in sorted(inbox_dir.glob("*.json")):
        data = _read_json(p)
        task_id = data.get("id", p.stem)
        current_ids.add(task_id)
        if data.get("status") != "in_progress":
            continue
        claimed_at = _parse_iso_ts(data.get("claimed_at"))
        if claimed_at is None:
            continue
        # v1.9.0：每任务可覆盖认领超时阈值（任务级 > 全局）
        threshold = _claim_timeout_minutes()
        task_timeout = int(data.get("claim_timeout_minutes") or 0)
        if task_timeout > 0:
            threshold = task_timeout
        age_minutes = (now - claimed_at).total_seconds() / 60.0
        if age_minutes < threshold or task_id in claim_alerted:
            continue

        event = {
            "id": _stable_event_id("claim_stale", task_id),
            "ts": now_local_iso(),
            "type": "claim_stale",
            "task_id": task_id,
            "title": data.get("title", ""),
            "assignee": data.get("assignee", "any"),
            "claimed_by": data.get("claimed_by", "?"),
            "age_minutes": round(age_minutes, 1),
            "threshold_minutes": threshold,
        }
        if emit:
            _append_feed(feed_file, event)
            # 自动释放认领：回到 pending，可被重新认领（防"僵尸任务"）
            data["status"] = "pending"
            data.pop("claimed_by", None)
            data.pop("claimed_at", None)
            _atomic_write_json(p, data)
            released_names.add(p.name)
            _append_journal_event(
                collab_dir, task_id, "timeout_released",
                detail=f"claimed_by={event['claimed_by']} age={round(age_minutes, 1)}m",
            )
            if not _system_claim_stale_message_exists(chat_dir, task_id):
                _system_claim_stale_message(chat_dir, data, event)
        events.append(event)
        claim_alerted[task_id] = now_local_iso()
        logger.warning(f"⚠️ claim_stale: {task_id} 认领超时，已自动释放")

    # 清理已不在 inbox 的任务（已完成/已删除）的告警记录
    for tid in list(claim_alerted):
        if tid not in current_ids:
            del claim_alerted[tid]
    return events, released_names


def scan_once(collab_dir: Path, state_file: Path, emit: bool = True) -> list[dict]:
    """扫描一次，返回本轮产生的事件列表。

    顺序约束（重要，勿改）：先发射事件，最后保存 state——
    若在保存 state 前崩溃，下一轮会重新发现文件并补发，事件不会丢失；
    新任务系统消息有去重，补发不会刷屏。
    """
    inbox_dir = collab_dir / "inbox"
    chat_dir = collab_dir / "chat"

    first_run = not state_file.exists()
    state = {} if first_run else _read_json(state_file)

    current = {}
    for key, dir_path in (("inbox", inbox_dir), ("chat", chat_dir)):
        current[key] = _scan_dir(dir_path)

    if first_run:
        # 首次运行只建立基线，不把存量文件当作新事件通知
        if emit:
            _save_state(state_file, {**current, "stale_alerted": {}, "claim_alerted": {}})
        logger.info("首次扫描：建立基线，不产生通知")
        return []

    events = []
    feed_file = collab_dir / "notifications" / "feed.jsonl"

    # 0) 认领超时回收（会改写任务文件，须先于新事件扫描执行）
    claim_alerted = state.get("claim_alerted", {})
    claim_events, released_names = _scan_claim_timeouts(
        collab_dir, inbox_dir, chat_dir, feed_file, claim_alerted, emit
    )
    events.extend(claim_events)
    # 回收可能改了 mtime，刷新快照避免误判为"新任务"
    current["inbox"] = _scan_dir(inbox_dir)

    # 0.5) 周期任务到期展开（先生成 inbox 任务，下一循环再按新任务通知）
    fired_schedules = materialize_due_schedules(collab_dir)
    for fired in fired_schedules:
        event = {
            "id": _stable_event_id("schedule", fired["task_id"]),
            "ts": now_local_iso(),
            "type": "schedule_fired",
            "schedule_id": fired["schedule_id"],
            "task_id": fired["task_id"],
            "occurrence": fired["occurrence"],
        }
        if emit:
            _append_feed(feed_file, event)
        events.append(event)
        logger.info(
            f"⏱️ schedule_fired: {fired['schedule_id']} -> {fired['task_id']}"
        )
    if fired_schedules:
        current["inbox"] = _scan_dir(inbox_dir)

    # 1) 先发射事件
    for key, dir_path in (("inbox", inbox_dir), ("chat", chat_dir)):
        seen = state.get(key, {})
        for name, mtime in current[key].items():
            # mtime 未变 → 已处理过；变了 → 视为新/修改事件
            if name in seen and seen[name] == mtime:
                continue
            if key == "inbox" and name in released_names:
                # 刚被认领超时释放：事件已由 claim_stale 表达，不再重复 new_task
                continue
            data = _read_json(dir_path / name)
            if not data:
                continue
            if key == "inbox" and data.get("status") in ("blocked", "failed", "skipped"):
                # v1.9.0/v1.9.1：流水线未解锁/失败/跳过步骤不通知；
                # 解锁后状态转 pending，mtime 变化再触发；失败/中止由 fail_task 主动告警
                continue

            if key == "inbox":
                task_id = data.get("id", name.removesuffix(".json"))
                if not _system_message_exists(chat_dir, task_id):
                    if emit:
                        _system_chat_message(chat_dir, data)
                event = {
                    "id": _stable_event_id("task", name),
                    "ts": now_local_iso(),
                    "type": "new_task",
                    "task_id": task_id,
                    "title": data.get("title", ""),
                    "assignee": data.get("assignee", "any"),
                }
            else:
                if data.get("sender") == SYSTEM_SENDER:
                    continue  # 系统消息不再通知
                event = {
                    "id": _stable_event_id("msg", name),
                    "ts": now_local_iso(),
                    "type": "new_message",
                    "message_id": data.get("id", name.removesuffix(".json")),
                    "sender": data.get("sender", "?"),
                }

            if emit:
                _append_feed(feed_file, event)
            events.append(event)
            logger.info(
                f"🔔 {event['type']}: {event.get('title') or event.get('sender')} ({name})"
            )

    # 2) 超时未完成任务 → stale_task 告警
    stale_alerted = state.get("stale_alerted", {})
    events.extend(
        _scan_stale_tasks(collab_dir, inbox_dir, chat_dir, feed_file,
                          stale_alerted, emit,
                          skip_ids={n.removesuffix(".json") for n in released_names})
    )

    # 3) 最后保存状态（此刻才标记"已见"）
    if emit:
        _save_state(
            state_file,
            {**current, "stale_alerted": stale_alerted, "claim_alerted": claim_alerted},
        )

    return events


def _acquire_lock(lock_path: Path) -> bool:
    """并发 --once 互斥；超过 10 分钟的陈旧锁自动清除。"""
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        try:
            if time.time() - lock_path.stat().st_mtime > 600:
                lock_path.unlink()
                return _acquire_lock(lock_path)
        except OSError:
            pass
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="协作平台事件通知")
    parser.add_argument("--dir", default="", help="collab 目录（默认取 COLLAB_DIR 环境变量；VM 上为 /mnt/hgfs/myshare/collab，其他平台回退到脚本所在目录）")
    parser.add_argument("--once", action="store_true", help="单次扫描后退出")
    parser.add_argument("--watch", action="store_true", help="前台循环")
    parser.add_argument("--interval", type=int, default=5, help="循环间隔秒数")
    args = parser.parse_args()

    collab_dir = Path(args.dir) if args.dir else _default_collab_dir()
    if not collab_dir.is_absolute():
        collab_dir = (Path(__file__).resolve().parent / collab_dir).resolve()

    # 确保锁/状态/feed 目录存在（Windows 首次运行可能缺失）
    (collab_dir / "notifications").mkdir(parents=True, exist_ok=True)
    lock_path = collab_dir / "notifications" / "daemon.lock"
    if not _acquire_lock(lock_path):
        logger.info("另一个 notify_daemon 正在运行，跳过本次")
        return 0

    state_file = collab_dir / "notifications" / "state.json"
    logger.info(f"notify_daemon 就绪，监控 {collab_dir}")
    try:
        if args.watch:
            while True:
                scan_once(collab_dir, state_file)
                time.sleep(max(1, args.interval))
        else:
            events = scan_once(collab_dir, state_file)
            print(f"本轮事件: {len(events)} 条")
    finally:
        try:
            lock_path.unlink()
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
