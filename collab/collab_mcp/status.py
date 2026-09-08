"""系统状态工具：get_collab_status / get_task_metrics。"""

from datetime import datetime

from .config import CHAT_DIR, COLLAB_DIR, DONE_DIR, INBOX_DIR
from .logging_setup import logger
from .utils import fail, ok, safe_read_json


def _parse_iso(value: str):
    """解析 ISO 时间戳；无时区按本地时区处理，失败返回 None。"""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.astimezone()
        return dt
    except (ValueError, TypeError):
        return None


async def get_collab_status() -> str:
    """获取协作系统状态概览。

    返回各目录中的文件数量：待办任务数、已完成任务数、聊天消息数。
    不依赖任何外部服务，纯粹统计文件数量。
    """
    try:
        pending = len(list(INBOX_DIR.glob("*.json")))
        done = len(list(DONE_DIR.glob("*.json")))
        chat = len(list(CHAT_DIR.glob("*.json")))
    except OSError as e:
        return fail(f"读取协作目录失败: {e}")

    logger.info(f"📊 状态: {pending} 待办 / {done} 已完成 / {chat} 消息")
    return ok({
        "status": {
            "pending_tasks": pending,
            "completed_tasks": done,
            "chat_messages": chat,
            "collab_dir": str(COLLAB_DIR),
        },
    })


async def get_task_metrics() -> str:
    """解析 done/ 计算任务生命周期指标（纯读，不落盘）。

    v1.8.3：基于状态机字段（created_at / claimed_at / completed_at）统计：
    - 总完成数、平均全周期耗时（created→completed）、平均活跃耗时（claimed→completed）
    - 按完成者聚合：完成数、平均全周期耗时
    - token/成本聚合：仅统计附带了 token_usage 的任务，缺失项不虚报
    - by_model 维度：按 token_usage.model 聚合 token/成本，未提供模型的任务不计入
    - 最近完成的 10 个任务（含各阶段耗时）
    """
    try:
        files = sorted(DONE_DIR.glob("*.json"))
    except OSError as e:
        return fail(f"读取 done 目录失败: {e}")

    completed = []
    for f in files:
        task = safe_read_json(f)
        if not task:
            continue
        created = _parse_iso(task.get("created_at"))
        completed_at = _parse_iso(task.get("completed_at"))
        claimed_at = _parse_iso(task.get("claimed_at"))
        row = {
            "task_id": task.get("id", f.stem),
            "title": task.get("title", ""),
            "completed_by": task.get("completed_by", "?"),
            "approved_by": task.get("approved_by"),
            "completed_at": task.get("completed_at"),
        }
        if created and completed_at:
            row["cycle_minutes"] = round(
                (completed_at - created).total_seconds() / 60.0, 1
            )
        if claimed_at and completed_at:
            row["active_minutes"] = round(
                (completed_at - claimed_at).total_seconds() / 60.0, 1
            )
        usage = task.get("token_usage")
        if isinstance(usage, dict):
            row["total_tokens"] = int(usage.get("total_tokens") or 0)
            row["cost"] = float(usage.get("cost") or 0.0)
            if usage.get("model"):
                row["model"] = str(usage["model"])
        completed.append(row)

    cycles = [r["cycle_minutes"] for r in completed if "cycle_minutes" in r]
    actives = [r["active_minutes"] for r in completed if "active_minutes" in r]

    by_teammate: dict[str, dict] = {}
    for r in completed:
        who = r["completed_by"]
        agg = by_teammate.setdefault(
            who, {
                "completed": 0,
                "cycle_sum": 0.0,
                "cycle_count": 0,
                "token_sum": 0,
                "cost_sum": 0.0,
                "token_count": 0,
            }
        )
        agg["completed"] += 1
        if "cycle_minutes" in r:
            agg["cycle_sum"] += r["cycle_minutes"]
            agg["cycle_count"] += 1
        if "total_tokens" in r:
            agg["token_sum"] += r["total_tokens"]
            agg["cost_sum"] += r["cost"]
            agg["token_count"] += 1
    teammate_stats = [
        {
            "name": name,
            "completed": agg["completed"],
            "avg_cycle_minutes": (
                round(agg["cycle_sum"] / agg["cycle_count"], 1)
                if agg["cycle_count"]
                else None
            ),
            "total_tokens": agg["token_sum"],
            "total_cost": round(agg["cost_sum"], 4),
            "requests_with_usage": agg["token_count"],
            "avg_tokens_per_request": (
                round(agg["token_sum"] / agg["token_count"], 1)
                if agg["token_count"]
                else None
            ),
            "avg_cost_per_request": (
                round(agg["cost_sum"] / agg["token_count"], 4)
                if agg["token_count"]
                else None
            ),
        }
        for name, agg in sorted(by_teammate.items(), key=lambda kv: -kv[1]["completed"])
    ]

    by_model: dict[str, dict] = {}
    for r in completed:
        if "model" not in r or "total_tokens" not in r:
            continue
        model_name = r["model"]
        agg = by_model.setdefault(
            model_name, {
                "token_sum": 0,
                "cost_sum": 0.0,
                "request_count": 0,
            }
        )
        agg["token_sum"] += r["total_tokens"]
        agg["cost_sum"] += r["cost"]
        agg["request_count"] += 1
    model_stats = [
        {
            "model": name,
            "total_tokens": agg["token_sum"],
            "total_cost": round(agg["cost_sum"], 4),
            "requests_with_usage": agg["request_count"],
            "avg_tokens_per_request": (
                round(agg["token_sum"] / agg["request_count"], 1)
                if agg["request_count"]
                else None
            ),
            "avg_cost_per_request": (
                round(agg["cost_sum"] / agg["request_count"], 4)
                if agg["request_count"]
                else None
            ),
        }
        for name, agg in sorted(by_model.items(), key=lambda kv: -kv[1]["token_sum"])
    ]

    recent = sorted(
        (r for r in completed if r.get("completed_at")),
        key=lambda r: r["completed_at"],
        reverse=True,
    )[:10]

    logger.info(f"📊 任务指标: {len(completed)} 个完成")
    usage_rows = [r for r in completed if "total_tokens" in r]
    return ok({
        "total_completed": len(completed),
        "avg_cycle_minutes": (
            round(sum(cycles) / len(cycles), 1) if cycles else None
        ),
        "avg_active_minutes": (
            round(sum(actives) / len(actives), 1) if actives else None
        ),
        "total_tokens": sum(r["total_tokens"] for r in usage_rows),
        "total_cost": round(sum(r["cost"] for r in usage_rows), 4),
        "requests_with_usage": len(usage_rows),
        "by_teammate": teammate_stats,
        "by_model": model_stats,
        "recent": recent,
    })
