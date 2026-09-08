"""collab 周期任务核心：不依赖 MCP 包，供 collab_mcp.schedules 与 notify_daemon 共用。

这是一段零第三方依赖的纯文件驱动调度逻辑。这里只负责「到期时把调度项展开成 inbox
普通任务」，不负责 MCP 工具暴露、通知 feed 或任务 journal；调用方决定如何记录事件。
为了保证主机与 VM 都在同一批文件上得到一致结果，时间一律按 UTC 比较与推进。
"""

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path


def parse_iso(value: str) -> datetime | None:
    """把 ISO 时间解析为 aware datetime；失败或无时区按 UTC 处理。"""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def valid_interval_minutes(value: int) -> tuple[int, str | None]:
    """校验周期；只接受 >=60 且为 60 整数倍的小时段。"""
    try:
        interval = int(value)
    except (TypeError, ValueError):
        return 0, "interval_minutes 必须是整数"
    if interval < 60 or interval % 60 != 0:
        return 0, "interval_minutes 必须是 >=60 且为 60 的整数倍"
    return interval, None


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _atomic_write_json(path: Path, data: dict) -> bool:
    """与 collab_mcp/utils.safe_write_json 语义一致的独立原子写。"""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:6]}.tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            with open(tmp, "rb+") as f:
                os.fsync(f.fileno())
        except OSError:
            pass
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def _schedule_task_id(schedule_id: str, occurrence: int) -> str:
    """由调度 ID 与期数生成确定性 task_id，使并发重扫只会写同一路径。"""
    return f"{schedule_id[:8]}{max(0, occurrence):04d}"


def materialize_due_schedules(
    collab_dir: Path,
    now: datetime | None = None,
) -> list[dict]:
    """扫描 schedules/，到期项各生成一期 inbox 任务，并推进 next_run_at。

    Returns:
        每一项形如 {"schedule_id", "task_id", "occurrence", "path"}，供调用方
        补记 journal / feed。失效或 skip 的调度不会出现。
    """
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    schedules_dir = collab_dir / "schedules"
    inbox_dir = collab_dir / "inbox"
    if not schedules_dir.is_dir():
        return []
    inbox_dir.mkdir(parents=True, exist_ok=True)

    fired = []
    for path in sorted(schedules_dir.glob("*.json")):
        schedule = _read_json(path)
        if not schedule:
            continue
        schedule_id = str(schedule.get("id") or path.stem)
        if schedule.get("enabled") is False:
            continue
        next_run = parse_iso(schedule.get("next_run_at", ""))
        if next_run is None or next_run > now:
            continue
        interval, err = valid_interval_minutes(schedule.get("interval_minutes", 0))
        if err:
            continue
        max_runs = schedule.get("max_runs")
        try:
            run_count = int(schedule.get("run_count") or 0)
        except (TypeError, ValueError):
            run_count = 0
        if max_runs is not None:
            try:
                if run_count >= int(max_runs):
                    continue
            except (TypeError, ValueError):
                continue

        occurrence = run_count + 1
        task_id = _schedule_task_id(schedule_id, occurrence)
        task_file = inbox_dir / f"{task_id}.json"
        task = {
            "id": task_id,
            "title": schedule.get("title", "周期任务"),
            "content": schedule.get("content", ""),
            "assignee": schedule.get("assignee", "any"),
            "status": "pending",
            "priority": schedule.get("priority", "medium"),
            "created_at": now.isoformat(),
            "completed_at": None,
            "recurring_schedule_id": schedule_id,
            "occurrence": occurrence,
        }
        for key in (
            "execution_env",
            "review_required",
            "claim_timeout_minutes",
            "max_retries",
        ):
            if schedule.get(key) not in (None, "", False, 0):
                task[key] = schedule.get(key)

        if not _atomic_write_json(task_file, task):
            continue

        schedule["run_count"] = occurrence
        schedule["last_run_at"] = next_run.isoformat()
        schedule["last_task_id"] = task_id
        schedule["next_run_at"] = (next_run + timedelta(minutes=interval)).isoformat()
        if not _atomic_write_json(path, schedule):
            continue

        fired.append(
            {
                "schedule_id": schedule_id,
                "task_id": task_id,
                "occurrence": occurrence,
                "path": str(task_file),
            }
        )
    return fired
