"""队友注册工具：register_teammate / list_teammates。"""

import asyncio
import uuid

from .config import COLLAB_DIR, INBOX_DIR
from .identity import assert_identity_allowed, current_identity, is_hub
from .journal import append_journal_event
from .logging_setup import logger
from .utils import fail, now_iso, ok, safe_read_json, safe_write_json

# 保护 teammates.json 的读-改-写（多实例并发注册时防止互相覆盖）。
# 用 asyncio.Lock：threading.Lock 在事件循环中一旦持锁期间出现 await 会死锁。
_registry_lock = asyncio.Lock()


async def touch_teammate_last_seen(identity: str) -> bool:
    """更新与身份匹配的队友 last_seen；未注册队友时不新建条目。"""
    if not identity:
        return False
    registry_file = COLLAB_DIR / "teammates.json"
    if not registry_file.exists():
        return False
    async with _registry_lock:
        registry = safe_read_json(registry_file) or {}
        target = registry.get(identity)
        if target is None:
            for entry in registry.values():
                if isinstance(entry, dict) and entry.get("identity") == identity:
                    target = entry
                    break
        if target is None:
            return False
        target["last_seen"] = now_iso()
        if not safe_write_json(registry_file, registry):
            return False
    return True


async def register_teammate(
    name: str,
    capabilities: str = "",
    skills: str = "",
    max_concurrency: int = 0,
) -> str:
    """新队友注册到协作网络。首次接入时调用，系统会自动创建欢迎任务。

    注册信息会保存到 collab/teammates.json，方便中枢了解每位队友的能力。
    v1.7.3 起支持结构化能力标签：skills 用逗号分隔的 "domain:action" 标签
    （如 "code-review:python,testing:unit"），供 should_delegate 精确匹配；
    capabilities 描述文本保留作为标签缺失时的语义兜底。

    Args:
        name: 队友标识（如 "PC-B", "PC-C"，或自定义名称）
        capabilities: 可选，队友的能力描述（如 "Docker,GPU,前端"）
        skills: 可选，逗号分隔的能力标签（如 "code-review:python,testing:unit"）
        max_concurrency: 可选，最大并发任务数（0 表示不限制）
    """
    denied = assert_identity_allowed("register_teammate")
    if denied:
        return fail(denied)

    ident = current_identity()
    if ident and not is_hub() and name != ident:
        logger.warning(f"⛔ 权限拒绝 [register_teammate] identity={ident} 试图注册为 {name}")
        return fail(
            f"身份不匹配：当前 SSH 身份为 {ident}，无法注册为 {name}。"
            f"请使用与公钥绑定的名字 {ident} 注册。"
        )

    registry_file = COLLAB_DIR / "teammates.json"

    async with _registry_lock:
        # 读取现有注册表
        registry = {}
        if registry_file.exists():
            registry = safe_read_json(registry_file) or {}

        is_new = name not in registry

        skills_list = [s.strip() for s in skills.split(",") if s.strip()]
        entry = {
            "name": name,
            "capabilities": capabilities,
            "first_seen": registry[name]["first_seen"] if name in registry else now_iso(),
            "last_seen": now_iso(),
        }
        if skills_list:
            entry["skills"] = skills_list
        if max_concurrency and max_concurrency > 0:
            entry["max_concurrency"] = max_concurrency
        if ident:
            entry["identity"] = ident
        registry[name] = entry

        if not safe_write_json(registry_file, registry):
            return fail("无法写入注册表文件")

        # 新队友：自动创建欢迎任务
        welcome_task = None
        if is_new:
            task_id = uuid.uuid4().hex[:12]
            welcome_task = {
                "id": task_id,
                "title": f"[欢迎] {name} 首次接入协作网络",
                "content": (
                    f"欢迎 {name} 加入 Claude 协作网络！🎉\n\n"
                    f"请执行以下自检步骤：\n"
                    f"1. 调用 health_check 确认环境正常\n"
                    f"2. 调用 get_collab_status 查看当前状态\n"
                    f"3. 调用 send_message 发送一条自我介绍到 chat\n"
                    f"4. 完成以上后，调用 complete_task('{task_id}') 标记完成\n\n"
                    f"能力描述: {capabilities if capabilities else '待补充'}\n"
                    f"能力标签: {', '.join(skills_list) if skills_list else '待补充'}\n"
                    f"最大并发: {max_concurrency if max_concurrency > 0 else '不限'}\n"
                    f"接入时间: {now_iso()}"
                ),
                "assignee": name,
                "status": "pending",
                "created_at": now_iso(),
                "completed_at": None,
                "is_welcome": True,
            }
            welcome_file = INBOX_DIR / f"{task_id}.json"
            if not safe_write_json(welcome_file, welcome_task):
                return fail("无法创建欢迎任务")
            append_journal_event(task_id, "created", detail=f"[欢迎] {name}")
            logger.info(f"🎉 新队友 {name} 注册！欢迎任务: {task_id}")

        # 统计当前成员
        members = list(registry.keys())
        logger.info(f"📋 队友注册: {name} (共 {len(members)} 人: {', '.join(members)})")

    return ok({
        "message": (
            f"欢迎 {name} 首次加入！" if is_new else f"{name} 已重新连接"
        ),
        "is_new": is_new,
        "teammates": members,
        "total_teammates": len(members),
        "welcome_task_id": welcome_task["id"] if welcome_task else None,
    })


async def list_teammates() -> str:
    """列出所有注册的队友及其能力、最近活动时间。

    读取 collab/teammates.json，帮助中枢了解每位队友的状态。
    """
    registry_file = COLLAB_DIR / "teammates.json"
    if not registry_file.exists():
        return ok({"teammates": [], "message": "暂无注册队友"})

    registry = safe_read_json(registry_file)
    if not registry:
        return ok({"teammates": [], "message": "注册表为空"})

    teammates = list(registry.values())
    # 按首次出现时间排序
    teammates.sort(key=lambda t: t.get("first_seen", ""))

    logger.info(f"👥 队友列表: {len(teammates)} 人")
    return ok({
        "teammates": teammates,
        "total": len(teammates),
    })
