"""通知工具：get_notifications — 读取 notify_daemon 产生的事件流。"""

import json

from .config import COLLAB_DIR
from .logging_setup import logger
from .utils import fail, ok


async def get_notifications(limit: int = 50) -> str:
    """读取协作平台的通知事件流（notify_daemon 写入）。

    事件类型：
    - new_task: 新任务到达（含 task_id / title / assignee）
    - new_message: 新聊天消息（含 sender）
    - stale_task: 任务超时未完成告警（v1.6.0）
    - claim_stale: 认领超时已自动释放回 pending（v1.8.1）

    新任务到达时还会自动在 chat/ 中产生一条"系统通知"消息，
    因此 get_chat_history 也能看到。

    Args:
        limit: 最多返回最近多少条事件，默认 50
    """
    # 下限 1、上限 500，防止负值/超大值导致全量读取
    limit = max(1, min(int(limit), 500))
    feed = COLLAB_DIR / "notifications" / "feed.jsonl"
    if not feed.exists():
        return ok({"count": 0, "events": []})

    events = []
    try:
        # 只从尾部读最后 limit 行（feed 已按 2000 行裁剪，这里再限制单次读取量）
        with open(feed, encoding="utf-8") as f:
            for line in f.readlines()[-limit:]:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError as e:
        return fail(f"读取通知流失败: {e}")

    logger.info(f"🔔 通知流: {len(events)} 条")
    return ok({"count": len(events), "events": events})
