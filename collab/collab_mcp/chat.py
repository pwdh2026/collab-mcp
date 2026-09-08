"""聊天工具：send_message / get_chat_history。"""

import uuid
from datetime import datetime

from .config import CHAT_DIR
from .logging_setup import logger
from .utils import fail, now_iso, ok, safe_read_json, safe_write_json


async def send_message(sender: str, content: str, task_id: str = "") -> str:
    """发送一条聊天消息到 collab/chat/ 目录。

    消息以 JSON 文件存储，文件名包含时间戳以确保按时间排序。
    v1.8.2 起支持关联任务：传 task_id 后消息带该字段，聊天可变成"任务线程"。

    Args:
        sender: 发送者名称（如 "PC-A", "PC-B", "PC-C" 或自定义名称）
        content: 消息内容，支持多行文本
        task_id: 可选，关联的任务 ID（用于按任务过滤聊天记录）
    """
    msg_id = uuid.uuid4().hex[:12]
    message = {
        "id": msg_id,
        "sender": sender,
        "content": content,
        # 与文件名同源（本地时间带时区偏移），避免文件名/时间戳相差 8 小时
        "timestamp": datetime.now().astimezone().isoformat(),
    }
    if task_id:
        message["task_id"] = task_id

    # 文件名: YYYYMMDD-HHMMSS_uuid.json → 按名称排序即按时间排序
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    file_path = CHAT_DIR / f"{timestamp}_{msg_id}.json"

    if not safe_write_json(file_path, message):
        return fail(f"无法写入消息文件: {file_path}")

    logger.info(f"💬 消息 [{msg_id}] {sender}: {content[:50]}...")
    return ok({"message": f"消息已发送 (ID: {msg_id})"})


async def get_chat_history(limit: int = 50, filter_task_id: str = "") -> str:
    """获取最近的聊天记录，按时间从早到晚排序。

    读取 collab/chat/ 目录下的消息 JSON 文件。

    Args:
        limit: 返回的最大消息数量，默认 50 条
        filter_task_id: 可选，只返回关联该任务的消息（v1.8.2）
    """
    messages = []
    try:
        # 文件名按时间排序 → 自然就是时间顺序
        files = sorted(CHAT_DIR.glob("*.json"))[-limit:]
        for file_path in files:
            msg = safe_read_json(file_path)
            if msg is None:
                continue
            if filter_task_id and msg.get("task_id") != filter_task_id:
                continue
            messages.append(msg)
    except OSError as e:
        return fail(f"读取 chat 目录失败: {e}")

    logger.info(f"💬 获取聊天记录: {len(messages)} 条")
    return ok({"count": len(messages), "messages": messages})
