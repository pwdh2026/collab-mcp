"""周期/重复任务 MCP 工具：schedule_recurring_task / list_schedules / set_schedule_enabled。"""

import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import COLLAB_DIR, SCHEDULES_DIR
from .identity import assert_identity_allowed, current_identity
from .journal import append_journal_event
from .logging_setup import logger
from .utils import fail, now_iso, ok, safe_read_json, safe_write_json

from schedule_core import materialize_due_schedules, parse_iso, valid_interval_minutes

PRIORITY_RANK = {"critical", "high", "medium", "low"}


def _normalize_priority(priority: str) -> str:
    normalized = (priority or "").strip().lower()
    return normalized if normalized in PRIORITY_RANK else "medium"


def _max_runs_or_none(max_runs: int) -> int | None:
    try:
        value = int(max_runs)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


async def schedule_recurring_task(
    title: str,
    content: str,
    interval_minutes: int,
    assignee: str = "any",
    priority: str = "medium",
    execution_env: str = "",
    start_at: str = "",
    max_runs: int = 0,
    claim_timeout_minutes: int = 0,
    max_retries: int = 0,
    review_required: bool = False,
) -> str:
    """创建一个周期任务调度项，到时由 notify_daemon 在 inbox 生成普通任务。

    interval_minutes 当前只接受 >=60 且为 60 的整数倍；不传 start_at 时，
    首期从 now + interval_minutes 开始。返回的 schedule_id 可用于 list / 启停。
    """
    denied = assert_identity_allowed("schedule_recurring_task")
    if denied:
        return fail(denied)
    if not title.strip():
        return fail("title 不能为空")

    interval, err = valid_interval_minutes(interval_minutes)
    if err:
        return fail(err)

    now = datetime.now(timezone.utc)
    if start_at:
        parsed_start = parse_iso(start_at)
        if parsed_start is None:
            return fail("start_at 不是合法 ISO 时间")
        start_at = parsed_start.isoformat()
    else:
        parsed_start = now
        start_at = parsed_start.isoformat()

    schedule_id = uuid.uuid4().hex[:12]
    schedule = {
        "id": schedule_id,
        "title": title.strip(),
        "content": content,
        "assignee": assignee or "any",
        "execution_env": execution_env,
        "priority": _normalize_priority(priority),
        "interval_minutes": interval,
        "start_at": start_at,
        "next_run_at": parsed_start.isoformat(),
        "last_run_at": None,
        "last_task_id": None,
        "run_count": 0,
        "max_runs": _max_runs_or_none(max_runs),
        "enabled": True,
        "claim_timeout_minutes": int(claim_timeout_minutes or 0),
        "max_retries": int(max_retries or 0),
        "review_required": bool(review_required),
        "created_by": current_identity() or "local",
        "created_at": now_iso(),
    }

    SCHEDULES_DIR.mkdir(parents=True, exist_ok=True)
    file_path = SCHEDULES_DIR / f"{schedule_id}.json"
    if not safe_write_json(file_path, schedule):
        return fail(f"无法创建调度项: {file_path}")
    append_journal_event(
        schedule_id,
        "schedule_created",
        detail=title.strip(),
        identity=current_identity() or "local",
    )
    return ok(
        {
            "message": "周期任务调度项已创建",
            "schedule_id": schedule_id,
            "file": str(file_path),
            "next_run_at": schedule["next_run_at"],
            "interval_minutes": interval,
        }
    )


async def list_schedules(enabled_only: bool = False) -> str:
    """列出 schedules/ 下的调度项。"""
    schedules = []
    if SCHEDULES_DIR.is_dir():
        for f in sorted(SCHEDULES_DIR.glob("*.json")):
            data = safe_read_json(f)
            if not data:
                continue
            if enabled_only and data.get("enabled") is False:
                continue
            schedules.append(
                {
                    "schedule_id": data.get("id", f.stem),
                    "title": data.get("title", ""),
                    "interval_minutes": data.get("interval_minutes"),
                    "next_run_at": data.get("next_run_at"),
                    "run_count": data.get("run_count", 0),
                    "max_runs": data.get("max_runs"),
                    "enabled": data.get("enabled", True),
                    "last_task_id": data.get("last_task_id"),
                }
            )
    return ok({"count": len(schedules), "schedules": schedules})


async def set_schedule_enabled(schedule_id: str, enabled: bool) -> str:
    """启用或停用一个调度项；只改 enabled，保留历史运行记录。"""
    denied = assert_identity_allowed("set_schedule_enabled")
    if denied:
        return fail(denied)
    if not schedule_id:
        return fail("schedule_id 不能为空")

    file_path = SCHEDULES_DIR / f"{schedule_id}.json"
    schedule = safe_read_json(file_path) if file_path.exists() else None
    if not schedule:
        return fail(f"调度项不存在: {schedule_id}")
    schedule["enabled"] = bool(enabled)
    if not safe_write_json(file_path, schedule):
        return fail(f"无法更新调度项: {file_path}")
    append_journal_event(
        schedule_id,
        "schedule_enabled" if enabled else "schedule_disabled",
        identity=current_identity() or "local",
    )
    logger.info(f"⏱️ 调度项 [{schedule_id}] enabled={enabled}")
    return ok(
        {
            "message": "调度项已启用" if enabled else "调度项已停用",
            "schedule_id": schedule_id,
            "enabled": bool(enabled),
        }
    )


def fire_due_schedules() -> list[dict]:
    """执行一次到期展开，供测试固定时间使用；生产通知由 notify_daemon 复用。"""
    return materialize_due_schedules(COLLAB_DIR)


__all__ = [
    "schedule_recurring_task",
    "list_schedules",
    "set_schedule_enabled",
    "fire_due_schedules",
]
