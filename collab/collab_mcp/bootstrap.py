"""Bootstrap 工具：一键会话初始化。

新会话接入时调用一次 bootstrap() 即可获取：
- 当前身份与角色
- 协作目录状态概览
- 指派给我的待办任务
- 最近的聊天消息
- 协议版本号

替代原来需要分别调用 get_collab_status + get_pending_tasks +
get_chat_history 的多步流程。
"""

from .config import CHAT_DIR, COLLAB_DIR, DONE_DIR, INBOX_DIR
from .identity import current_identity, identity_note, is_hub
from .logging_setup import logger
from .utils import fail, ok, safe_read_json


def _read_protocol_version() -> str:
    """读取 collab/version 文件；不存在返回 "0"（旧版兼容）。"""
    vf = COLLAB_DIR / "version"
    try:
        return vf.read_text(encoding="utf-8").strip()
    except OSError:
        return "0"


def _my_pending_tasks(ident: str, hub: bool, limit: int = 20) -> list[dict]:
    """读取 inbox/ 中指派给当前身份（或 any）的待办任务摘要。"""
    tasks = []
    try:
        files = sorted(INBOX_DIR.glob("*.json"))
    except OSError:
        return tasks
    for f in files:
        t = safe_read_json(f)
        if not t:
            continue
        assignee = t.get("assignee", "any")
        status = t.get("status", "pending")
        # hub 看到全部；队友只看自己的或 any 的活跃任务
        if not hub and assignee not in (ident, "any"):
            continue
        if status in ("failed", "skipped"):
            continue
        tasks.append({
            "id": t.get("id", f.stem),
            "title": t.get("title", ""),
            "assignee": assignee,
            "status": status,
            "claimed_by": t.get("claimed_by"),
            "deadline": t.get("deadline"),
            "execution_env": t.get("execution_env"),
        })
        if len(tasks) >= limit:
            break
    # 按 deadline 升序排列（有 deadline 的排前面，无 deadline 的排后面）
    tasks.sort(key=lambda t: t.get("deadline") or "9999")
    return tasks


def _recent_messages(limit: int = 10) -> list[dict]:
    """读取最近 N 条聊天消息摘要。"""
    msgs = []
    try:
        files = sorted(CHAT_DIR.glob("*.json"))[-limit:]
    except OSError:
        return msgs
    for f in files:
        m = safe_read_json(f)
        if not m:
            continue
        msgs.append({
            "sender": m.get("sender", "?"),
            "content": m.get("content", "")[:200],
            "timestamp": m.get("timestamp") or f.stem,
            "task_id": m.get("task_id"),
        })
    return msgs


async def bootstrap() -> str:
    """一键会话初始化：注册信息 + 当前状态 + 待领任务 + 最近消息。

    新会话接入协作系统时，只需调用此工具一次即可了解全局上下文，
    不再需要分别调用 get_collab_status / get_pending_tasks /
    get_chat_history。
    """
    ident = current_identity()
    hub = is_hub()

    try:
        inbox_files = list(INBOX_DIR.glob("*.json"))
        done_count = len(list(DONE_DIR.glob("*.json")))
        chat_count = len(list(CHAT_DIR.glob("*.json")))
    except OSError as e:
        return fail(f"读取协作目录失败: {e}")

    # 细分 inbox 状态
    pending_count = 0
    claimed_count = 0
    for f in inbox_files:
        t = safe_read_json(f)
        if not t:
            continue
        s = t.get("status", "pending")
        if s == "claimed":
            claimed_count += 1
        elif s == "pending":
            pending_count += 1

    my_tasks = _my_pending_tasks(ident, hub)
    messages = _recent_messages()
    protocol_version = _read_protocol_version()

    logger.info(
        f"bootstrap: identity={ident or 'local'} "
        f"pending={pending_count} claimed={claimed_count} my_tasks={len(my_tasks)}"
    )

    return ok({
        "identity": identity_note(),
        "protocol_version": protocol_version,
        "server_dir": str(COLLAB_DIR),
        "overview": {
            "pending_tasks": pending_count,
            "claimed_tasks": claimed_count,
            "completed_tasks": done_count,
            "chat_messages": chat_count,
        },
        "my_pending_tasks": my_tasks,
        "recent_messages": messages,
    })
