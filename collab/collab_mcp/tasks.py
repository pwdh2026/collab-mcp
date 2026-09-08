"""任务管理工具：create_task / 流水线 / get_pending_tasks / claim_task / complete_task / get_task_context / list_templates。"""

import asyncio
import json
import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .codegraph import format_codegraph_result, open_codegraph_db, search_nodes
from .config import CHAT_DIR, COLLAB_DIR, DONE_DIR, INBOX_DIR, TEMPLATES_DIR
from .identity import assert_identity_allowed, current_identity, identity_note, is_hub
from .journal import append_journal_event, read_task_journal
from .logging_setup import logger
from .teammates import touch_teammate_last_seen
from .utils import fail, now_iso, ok, safe_read_json, safe_write_json

# v3.27.0: 默认认领超时（分钟）— 超时后自动释放回 pending
DEFAULT_CLAIM_TIMEOUT_MIN = 30
QUEUE_LOCK_TTL_SECONDS = 60
QUEUE_LOCK_WAIT_SECONDS = 2.0
QUEUE_LOCK_RETRY_SECONDS = 0.05
QUEUE_LOCK_STALE_SECONDS = 5.0
PRIORITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}

# v1.7.0：evidence 上限，防止结果过长撑爆中枢上下文
EVIDENCE_MAX_CHARS = 8000
CODE_CONTEXT_MAX_CHARS = 4000
MAX_PIPELINE_DEPTH = 3  # v2.1.0：子流水线嵌套最大深度，防循环引用死递归


def _attach_evidence(task: dict, evidence: str, result_path: str) -> None:
    """把 evidence / result_path 附加到任务字典（超长自动截断）。"""
    if evidence:
        if len(evidence) > EVIDENCE_MAX_CHARS:
            evidence = (
                evidence[:EVIDENCE_MAX_CHARS]
                + f"\n…[evidence 已截断至 {EVIDENCE_MAX_CHARS} 字符，完整内容见 result_path: {result_path or '未提供'}]"
            )
        task["evidence"] = evidence
    if result_path:
        task["result_path"] = result_path


def _attach_token_usage(
    task: dict,
    total_tokens: int,
    input_tokens: int,
    output_tokens: int,
    cost: float,
    model: str,
) -> None:
    """把可选的 token/成本用量写入任务；全为空时不落字段。"""
    total_tokens = int(total_tokens or 0)
    input_tokens = int(input_tokens or 0)
    output_tokens = int(output_tokens or 0)
    try:
        cost = float(cost or 0.0)
    except (TypeError, ValueError):
        cost = 0.0
    if total_tokens <= 0 and input_tokens <= 0 and output_tokens <= 0 and cost == 0.0:
        return
    usage = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens or (input_tokens + output_tokens),
        "cost": cost,
    }
    if model:
        usage["model"] = model
    task["token_usage"] = usage


def _parse_claim_iso(value: str):
    """把 ISO 时间字符串解析成 aware datetime；无效返回 None。"""
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _queue_lock_file() -> Path:
    return COLLAB_DIR / "locks" / "claim_next.json"


def _queue_lock_sentinel() -> Path:
    return _queue_lock_file().with_name(".claim_next.lock")


def _read_valid_queue_lock(lock_file: Path) -> dict:
    """读取 claim_next 队列锁；缺失、损坏或过期一律视为无锁。"""
    data = safe_read_json(lock_file) or {}
    expires = _parse_claim_iso(data.get("expires_at", ""))
    if expires is None:
        return {}
    if expires <= datetime.now(timezone.utc):
        return {}
    return data


def _sentinel_stale(sentinel: Path) -> bool:
    try:
        return time.time() - sentinel.stat().st_mtime > QUEUE_LOCK_STALE_SECONDS
    except OSError:
        return True


def _release_queue_lock() -> None:
    """释放 claim_next 队列锁：先删 JSON 载荷，再删互斥 sentinel。"""
    for path in (_queue_lock_file(), _queue_lock_sentinel()):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


async def _acquire_queue_lock(owner: str) -> bool:
    """短暂等待并获取 claim_next 队列锁；拿不到时返回 False 而不无限轮询。

    锁采用双层设计：`.claim_next.lock` 是跨进程互斥 sentinel，`claim_next.json`
    只保存锁属主与过期时间，避免 JSON 覆盖竞态。TTL 为 60 秒，崩溃后可由
    5 秒陈旧 sentinel 检测自动恢复。
    """
    locks_dir = COLLAB_DIR / "locks"
    locks_dir.mkdir(parents=True, exist_ok=True)
    lock_file = _queue_lock_file()
    sentinel = _queue_lock_sentinel()
    deadline = time.monotonic() + QUEUE_LOCK_WAIT_SECONDS

    while True:
        acquired = False
        try:
            fd = os.open(sentinel, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            acquired = True
        except (FileExistsError, PermissionError):
            # Windows 会把文件占用翻译成 PermissionError(13)，与 FileExistsError
            # 一样按“别人正在持有”处理。只有陈旧 sentinel 才清理，避免抢正在
            # 创建 payload 的新持锁者。
            if _sentinel_stale(sentinel):
                try:
                    sentinel.unlink(missing_ok=True)
                except OSError:
                    pass
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(QUEUE_LOCK_RETRY_SECONDS)
            continue
        except OSError:
            return False

        if not acquired:
            continue

        # 已拿到互斥 sentinel：清理任何过期/陈旧 payload，然后写入本次锁。
        try:
            lock_file.unlink(missing_ok=True)
        except OSError:
            pass
        lock = {
            "owner": owner,
            "acquired_at": now_iso(),
            "expires_at": (
                datetime.now(timezone.utc)
                + timedelta(seconds=QUEUE_LOCK_TTL_SECONDS)
            ).isoformat(),
            "reason": "claim_next_task",
        }
        if not safe_write_json(lock_file, lock):
            try:
                sentinel.unlink(missing_ok=True)
            except OSError:
                pass
            return False
        return True


def _normalize_priority(priority: str) -> str:
    """规范化优先级；无法识别时回退 medium。"""
    normalized = (priority or "").strip().lower()
    return normalized if normalized in PRIORITY_RANK else "medium"


def _claim_deadline_key(task: dict):
    deadline = _parse_claim_iso(task.get("deadline", ""))
    if deadline is None:
        return (1, "")
    return (0, deadline.isoformat())


def _claim_sort_key(task: dict):
    priority = _normalize_priority(str(task.get("priority", "medium")))
    return (
        PRIORITY_RANK[priority],
        _claim_deadline_key(task),
        str(task.get("created_at", "")),
    )


def _teammate_capacity_entry(ident: str) -> dict:
    """按身份读取 teammates.json 中的注册项；无身份或无匹配返回空 dict。"""
    if not ident:
        return {}
    registry_file = COLLAB_DIR / "teammates.json"
    if not registry_file.exists():
        return {}
    registry = safe_read_json(registry_file) or {}
    if ident in registry:
        return registry.get(ident) or {}
    for entry in registry.values():
        if isinstance(entry, dict) and entry.get("identity") == ident:
            return entry
    return {}


def _count_own_in_progress(ident: str) -> int:
    count = 0
    try:
        files = list(INBOX_DIR.glob("*.json"))
    except OSError:
        return 0
    for f in files:
        task = safe_read_json(f)
        if not task:
            continue
        if task.get("status") == "in_progress" and task.get("claimed_by") == ident:
            count += 1
    return count


def _load_template(template_name: str) -> dict | None:
    """读取 collab/templates/<name>.json；不存在或损坏返回 None。"""
    tpl_file = TEMPLATES_DIR / f"{template_name}.json"
    if not tpl_file.exists():
        return None
    tpl = safe_read_json(tpl_file)
    if not tpl or tpl.get("name") != template_name:
        return None
    return tpl


def _fill_template(text: str, params: dict) -> str:
    """用 {param} 占位符替换模板文本（未提供的占位符保留原样）。"""
    for key, value in params.items():
        text = text.replace("{" + key + "}", str(value))
    # v2.4.1（PC-B L3）：仍有占位符形状残留时记 warning，便于排查模板缺参
    # v2.4.2（PC-B 低危意见）：改用 %s 惰性格式化，避免 f-string 无条件求值
    if re.search(r"\{[a-zA-Z_][a-zA-Z0-9_]*\}", text):
        logger.warning("模板展开后仍含未填充占位符: %s…", text[:80])
    return text


def _step_condition_met(step_def: dict, params: dict) -> bool:
    """评估步骤的 if 条件（v2.0.1，条件分支 Conditional Edge）。

    支持两种简单安全的表达式（不做 eval）：
      {"param": "lang", "equals": "python"}
      {"param": "lang", "not_equals": "python"}
    传 list 表示多个条件，全部满足才通过。条件不满足的步骤创建即 skipped。
    """
    cond = step_def.get("if")
    if not cond:
        return True
    conds = cond if isinstance(cond, list) else [cond]
    for c in conds:
        if not isinstance(c, dict):
            return False
        param = c.get("param", "")
        value = str(params.get(param, ""))
        if "equals" in c and value != str(c["equals"]):
            return False
        if "not_equals" in c and value == str(c["not_equals"]):
            return False
    return True


def _expand_pipeline_steps(
    template_name: str,
    tpl: dict,
    params: dict,
    pipeline_id: str,
    parent_step_id: str = "",
    depth: int = 1,
    chain: tuple[str, ...] = (),
    created_ids: list[str] | None = None,
) -> list[dict]:
    """递归展开流水线步骤（v2.1.0，含子流水线嵌套 Sub-workflow）。

    子流水线步骤额外携带：
      - sub_pipeline_id / is_pipeline_parent（父步骤）
      - parent_step_id / parent_pipeline_id（子步骤 → 审计链路）
    父步骤由子流水线终态驱动：blocked → in_progress → done/failed。
    校验：嵌套深度 ≤ MAX_PIPELINE_DEPTH；模板不得在链路中重复（防循环引用）。
    返回步骤列表 [{step_name, task_id, status, template, parent_step?}]。
    """
    if depth > MAX_PIPELINE_DEPTH:
        raise ValueError(
            f"流水线嵌套超过最大深度 {MAX_PIPELINE_DEPTH}（{template_name}）"
        )
    if template_name in chain:
        raise ValueError(f"流水线循环引用：{template_name} 已在链路 {chain}")
    steps_def = tpl.get("pipeline") or []
    if not steps_def:
        raise ValueError(f"模板 {template_name} 的 pipeline 为空")

    created_steps: list[dict] = []
    name_to_id: dict[str, str] = {}
    chain = chain + (template_name,)
    created_ids = created_ids if created_ids is not None else []

    for idx, step_def in enumerate(steps_def):
        step_name = step_def.get("name") or f"step{idx + 1}"
        step_tpl_name = step_def.get("template", "")
        step_tpl = _load_template(step_tpl_name) if step_tpl_name else None
        if step_tpl is None:
            raise ValueError(
                f"流水线 {template_name} 第 {idx + 1} 步模板不存在: {step_tpl_name}"
            )
        if step_tpl.get("pipeline") and step_tpl_name in chain:
            # v2.1.0：循环引用在参数校验前拦截（避免先报缺参再报循环）
            raise ValueError(f"流水线循环引用：{step_tpl_name} 已在链路 {chain}")

        step_params_raw = dict(step_def.get("params", {}) or {})
        step_params = {
            k: _fill_template(str(v), params)
            for k, v in step_params_raw.items()
        }
        # v2.4.3（PC-B L3）：支持步骤级 assignee 覆盖（可引用顶层参数，如 {spec_assignee}），
        # 避免流水线步骤单点绑定某队友
        step_assignee = str(step_def.get("assignee", "") or "").strip()
        if step_assignee:
            step_assignee = _fill_template(step_assignee, params)
        merged_step = dict(step_tpl.get("default_params", {}) or {})
        merged_step.update(step_params)
        for req in step_tpl.get("required_params", []) or []:
            if req not in merged_step or not str(merged_step.get(req, "")).strip():
                raise ValueError(
                    f"流水线 {template_name} 步骤 {step_name} 缺少必填参数: {req}"
                )

        deps = step_def.get("depends_on", []) or []
        dep_ids = []
        for d in deps:
            if d not in name_to_id:
                raise ValueError(
                    f"流水线 {template_name} 步骤 {step_name} 依赖未定义的前序步骤: {d}"
                )
            dep_ids.append(name_to_id[d])

        condition_met = _step_condition_met(step_def, params)
        is_sub = bool(step_tpl.get("pipeline"))
        sub_pipeline_id = uuid.uuid4().hex[:12] if is_sub else ""
        task_id = uuid.uuid4().hex[:12]

        if not condition_met:
            task_status = "skipped"
        elif is_sub:
            # 父步骤：无依赖 → in_progress（子流水线即刻可跑）；有依赖 → blocked
            task_status = "in_progress" if not dep_ids else "blocked"
        else:
            task_status = "blocked" if dep_ids else "pending"

        task = {
            "id": task_id,
            "title": _fill_template(step_tpl.get("title", step_name), merged_step),
            "content": _fill_template(step_tpl.get("content", ""), merged_step),
            "assignee": step_assignee or (step_tpl.get("default_assignee", "any") or "any"),
            "status": task_status,
            "created_at": now_iso(),
            "completed_at": None,
            "pipeline_id": pipeline_id,
            "pipeline_name": template_name,
            "step_name": step_name,
            "step_index": idx,
            "depends_on": dep_ids,
            "template": step_tpl_name,
        }
        if parent_step_id:
            task["parent_step_id"] = parent_step_id
            task["parent_pipeline_id"] = chain[-2] if len(chain) > 1 else ""
        if is_sub:
            task["is_pipeline_parent"] = True
            task["sub_pipeline_id"] = sub_pipeline_id
        if not condition_met:
            task["skipped_reason"] = "condition_not_met"
        if step_tpl.get("default_execution_env"):
            task["execution_env"] = step_tpl["default_execution_env"]
        if step_tpl.get("review_required"):
            task["review_required"] = True
        if step_tpl.get("default_deadline_minutes"):
            minutes = max(1, int(step_tpl["default_deadline_minutes"]))
            task["deadline"] = (
                datetime.now(timezone.utc) + timedelta(minutes=minutes)
            ).isoformat()
        step_timeout = int(step_def.get("claim_timeout_minutes") or 0)
        if step_timeout > 0:
            task["claim_timeout_minutes"] = step_timeout
        step_retries = int(step_def.get("max_retries") or 0)
        if step_retries > 0:
            task["max_retries"] = step_retries
            task["retry_count"] = 0

        file_path = INBOX_DIR / f"{task_id}.json"
        if not safe_write_json(file_path, task):
            raise ValueError(f"无法创建流水线步骤文件: {file_path}")
        created_ids.append(task_id)
        created_detail = task["title"]
        if parent_step_id:
            created_detail += f"（parent_step={parent_step_id}）"
        append_journal_event(task_id, "created", detail=created_detail)
        if not condition_met:
            append_journal_event(task_id, "step_skipped", detail="condition_not_met")
        name_to_id[step_name] = task_id
        entry = {
            "step_name": step_name,
            "task_id": task_id,
            "status": task["status"],
            "template": step_tpl_name,
        }
        if parent_step_id:
            entry["parent_step"] = parent_step_id
        created_steps.append(entry)
        logger.info(
            f"🔗 流水线步骤 [{task_id}] {template_name}/{step_name} → {task['status']}"
        )

        if is_sub and condition_met:
            # v2.1.0：递归展开子流水线
            sub_steps = _expand_pipeline_steps(
                step_tpl_name,
                step_tpl,
                merged_step,
                pipeline_id=sub_pipeline_id,
                parent_step_id=task_id,
                depth=depth + 1,
                chain=chain,
                created_ids=created_ids,
            )
            created_steps.extend(sub_steps)
            # 父步骤有依赖（暂 blocked）时，子首波步骤先 gate 住，父解锁后再放行
            if dep_ids:
                for s in sub_steps:
                    t = safe_read_json(INBOX_DIR / f"{s['task_id']}.json")
                    if t and not t.get("depends_on") and t.get("status") == "pending":
                        t["status"] = "blocked"
                        t["parent_gated"] = True
                        safe_write_json(INBOX_DIR / f"{s['task_id']}.json", t)
                        s["status"] = "blocked"

    # v2.0.1：条件不满足导致步骤创建即 skipped → 依赖它的 blocked 步骤同步级联跳过
    _cascade_skip_dependents({"pipeline_id": pipeline_id})
    # 级联可能改了后续步骤状态：刷新响应里的步骤快照
    for entry in created_steps:
        t = safe_read_json(INBOX_DIR / f"{entry['task_id']}.json")
        if t:
            entry["status"] = t.get("status", entry["status"])
    return created_steps


def _ungate_sub_pipeline(parent: dict) -> None:
    """父步骤解锁后，把子流水线首波步骤（parent_gated）置为 pending。"""
    sub_id = parent.get("sub_pipeline_id", "")
    if not sub_id:
        return
    for f in INBOX_DIR.glob("*.json"):
        t = safe_read_json(f)
        if not t or t.get("pipeline_id") != sub_id:
            continue
        if t.get("parent_gated") and t.get("status") == "blocked":
            t["status"] = "pending"
            t.pop("parent_gated", None)
            t["unblocked_at"] = now_iso()
            if safe_write_json(f, t):
                append_journal_event(
                    t.get("id", f.stem),
                    "unblocked",
                    detail=f"parent={parent.get('id')}",
                )
                logger.info(f"🔓 子流水线步骤放行 [{t.get('id')}] {t.get('step_name', '')}")


async def _create_pipeline(template_name: str, tpl: dict, params: dict) -> str:
    """把一个带 pipeline 字段的模板展开为多个步骤任务（支持子流水线嵌套）。"""
    pipeline_id = uuid.uuid4().hex[:12]
    created_ids: list[str] = []
    try:
        created_steps = _expand_pipeline_steps(
            template_name,
            tpl,
            params,
            pipeline_id=pipeline_id,
            depth=1,
            chain=(),
            created_ids=created_ids,
        )
    except ValueError as e:
        # v2.1.1：创建失败（深度超限/循环引用/缺参）时回滚已建步骤，避免孤儿任务
        for tid in created_ids:
            try:
                (INBOX_DIR / f"{tid}.json").unlink()
            except OSError:
                pass
            # v2.4.1（PC-B L1）：回滚同步清理 journal 孤儿 created 事件
            # v2.4.2（PC-B 低危意见）：静默路径加 logger.debug 提升可观测性
            try:
                (COLLAB_DIR / "journal" / f"{tid}.jsonl").unlink()
            except OSError:
                logger.debug("回滚清理 journal 孤儿跳过: %s.jsonl", tid)
        return fail(f"{e}（已回滚 {len(created_ids)} 个已建步骤）")

    logger.info(f"🔗 流水线创建 [{pipeline_id}] {template_name}（{len(created_steps)} 步）")
    return ok({
        "message": f"流水线「{template_name}」已创建（{len(created_steps)} 步）",
        "pipeline_id": pipeline_id,
        "template": template_name,
        "count": len(created_steps),
        "steps": created_steps,
    })


def _unblock_pipeline_dependents(completed_task: dict) -> None:
    """流水线依赖解锁：一个步骤完成/审批后，把依赖满足的 blocked 步骤转为 pending。"""
    pipeline_id = completed_task.get("pipeline_id")
    if not pipeline_id:
        return
    for f in sorted(INBOX_DIR.glob("*.json")):
        t = safe_read_json(f)
        if not t or t.get("pipeline_id") != pipeline_id:
            continue
        if t.get("status") != "blocked":
            continue
        deps = t.get("depends_on", []) or []
        if not deps:
            continue
        dep_statuses = [_task_status(d) for d in deps]
        if any(s in ("failed", "skipped") for s in dep_statuses):
            # v1.9.1：依赖中有失败/跳过 → 本步骤也跳过（级联安全网）
            t["status"] = "skipped"
            t["skipped_reason"] = "upstream_failed"
            t["skipped_at"] = now_iso()
            if safe_write_json(f, t):
                append_journal_event(
                    t.get("id", f.stem),
                    "step_skipped",
                    detail="upstream_failed",
                )
                logger.info(
                    f"⏭️ 流水线步骤跳过 [{t.get('id')}] {t.get('step_name', '')}（上游失败）"
                )
        elif all(s == "done" for s in dep_statuses):
            if t.get("is_pipeline_parent"):
                # v2.1.0：父步骤解锁 → in_progress（子流水线开始运行）+ 放行子首波
                t["status"] = "in_progress"
                t["unblocked_at"] = now_iso()
                if safe_write_json(f, t):
                    append_journal_event(
                        t.get("id", f.stem),
                        "unblocked",
                        detail=f"pipeline={pipeline_id}（子流水线）",
                    )
                    _ungate_sub_pipeline(t)
                    logger.info(
                        f"🔓 子流水线父步骤启动 [{t.get('id')}] {t.get('step_name', '')}"
                    )
            else:
                t["status"] = "pending"
                t["unblocked_at"] = now_iso()
                if safe_write_json(f, t):
                    append_journal_event(
                        t.get("id", f.stem),
                        "unblocked",
                        detail=f"pipeline={pipeline_id}",
                    )
                    logger.info(
                        f"🔓 流水线步骤解锁 [{t.get('id')}] {t.get('step_name', '')}"
                    )


def _task_status(task_id: str) -> str:
    """查询任务当前状态：done/ 优先，其次 inbox 内状态。"""
    if (DONE_DIR / f"{task_id}.json").exists():
        return "done"
    t = safe_read_json(INBOX_DIR / f"{task_id}.json")
    return t.get("status", "pending") if t else ""


def _append_feed_event(event: dict) -> None:
    """向 notifications/feed.jsonl 追加一条事件（与 notify_daemon 同格式）。"""
    feed = COLLAB_DIR / "notifications" / "feed.jsonl"
    try:
        feed.parent.mkdir(parents=True, exist_ok=True)
        with open(feed, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _system_chat_message(content: str) -> None:
    """以系统身份发一条聊天消息（与 notify_daemon 语义一致）。"""
    msg_id = uuid.uuid4().hex[:12]
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    message = {
        "id": msg_id,
        "sender": "系统通知",
        "content": content,
        "timestamp": datetime.now().astimezone().isoformat(),
    }
    safe_write_json(CHAT_DIR / f"{timestamp}_{msg_id}.json", message)


def aggregate_pipeline(pipeline_id: str) -> dict:
    """扫描某流水线的全部步骤（inbox + done），返回聚合状态（v1.9.1）。

    聚合语义：任一步骤 failed → "failed"；全部步骤 done → "completed"；否则 "running"。
    供 fail_task / 看板分组 / 指标共同使用，单一事实来源。
    """
    steps = []
    for f in list(INBOX_DIR.glob("*.json")) + list(DONE_DIR.glob("*.json")):
        t = safe_read_json(f)
        if t and t.get("pipeline_id") == pipeline_id:
            steps.append(t)
    steps.sort(key=lambda s: s.get("step_index", 0))

    statuses = [s.get("status", "pending") for s in steps]
    total = len(steps)
    done = sum(1 for s in statuses if s == "done")
    failed = sum(1 for s in statuses if s == "failed")
    skipped = sum(1 for s in statuses if s == "skipped")
    in_review = sum(1 for s in statuses if s == "needs_review")
    active = sum(1 for s in statuses if s in ("pending", "in_progress", "blocked"))

    if failed:
        agg = "failed"
    elif total and done == total:
        agg = "completed"
    else:
        agg = "running"

    return {
        "pipeline_id": pipeline_id,
        "status": agg,
        "total_steps": total,
        "done_steps": done,
        "failed_steps": failed,
        "skipped_steps": skipped,
        "in_review_steps": in_review,
        "active_steps": active,
        "steps": [
            {
                "step_name": s.get("step_name", "?"),
                "pipeline_name": s.get("pipeline_name", ""),
                "task_id": s.get("id"),
                "status": s.get("status"),
                "template": s.get("template"),
                "failure_reason": s.get("failure_reason"),
            }
            for s in steps
        ],
    }


def _note_pipeline_status(
    pipeline_id: str,
    trigger_task_id: str,
    _depth: int = 0,
    _seen: set[str] | None = None,
) -> None:
    """状态迁移后刷新流水线聚合；终态时桥接父步骤并向上传播（v2.1.0）。

    父步骤桥接：子流水线 completed → 父步骤 done（移入 done/）并解锁外层后续；
    子流水线 failed → 父步骤 failed 并级联跳过外层后续。深度/去重防循环。
    pipeline_status 字段语义（v2.1.2 澄清）：任务文件上的该字段是每次状态
    迁移时写入的聚合快照；终态（completed/failed）时同步回写 done/ 文件，
    保证与 journal 的 pipeline_status 事件及 aggregate_pipeline 一致。
    权威状态以 journal + aggregate_pipeline 为准。
    """
    if _depth > MAX_PIPELINE_DEPTH:
        return
    seen = _seen if _seen is not None else set()
    if pipeline_id in seen:
        return
    seen.add(pipeline_id)

    agg = aggregate_pipeline(pipeline_id)
    for f in INBOX_DIR.glob("*.json"):
        t = safe_read_json(f)
        if (
            t
            and t.get("pipeline_id") == pipeline_id
            and t.get("status") not in ("failed", "skipped")
        ):
            t["pipeline_status"] = agg["status"]
            safe_write_json(f, t)
    if agg["status"] in ("failed", "completed"):
        append_journal_event(
            trigger_task_id,
            "pipeline_status",
            detail=agg["status"],
        )
        # v2.1.2：终态聚合时回写 done/ 步骤文件，避免快照停留在 running/缺失
        for f in DONE_DIR.glob("*.json"):
            t = safe_read_json(f)
            if t and t.get("pipeline_id") == pipeline_id:
                t["pipeline_status"] = agg["status"]
                safe_write_json(f, t)
        # 桥接：本流水线是某个父步骤的子流水线 → 同步父步骤状态
        parent = None
        for f in INBOX_DIR.glob("*.json"):
            t = safe_read_json(f)
            if t and t.get("sub_pipeline_id") == pipeline_id:
                parent = t
                break
        if parent:
            parent_id = parent.get("id", "")
            if agg["status"] == "completed":
                parent["status"] = "done"
                parent["completed_at"] = now_iso()
                parent["pipeline_status"] = agg["status"]
                if safe_write_json(DONE_DIR / f"{parent_id}.json", parent):
                    try:
                        (INBOX_DIR / f"{parent_id}.json").unlink()
                    except OSError:
                        pass
                    append_journal_event(
                        parent_id, "sub_pipeline_completed", detail=pipeline_id
                    )
                    _unblock_pipeline_dependents(parent)
            else:  # failed
                parent["status"] = "failed"
                parent["failed_at"] = now_iso()
                parent["failure_reason"] = f"子流水线失败: {pipeline_id}"
                if safe_write_json(INBOX_DIR / f"{parent_id}.json", parent):
                    append_journal_event(
                        parent_id, "sub_pipeline_failed", detail=pipeline_id
                    )
                    _cascade_skip_dependents(parent)
            outer = parent.get("pipeline_id", "")
            if outer:
                _note_pipeline_status(outer, parent_id, _depth + 1, seen)


def _cascade_skip_dependents(failed_task: dict) -> list[str]:
    """把依赖（含传递依赖）已失败步骤的 blocked 步骤级联标记为 skipped。"""
    pipeline_id = failed_task.get("pipeline_id")
    if not pipeline_id:
        return []
    skipped_ids = []
    changed = True
    while changed:
        changed = False
        for f in sorted(INBOX_DIR.glob("*.json")):
            t = safe_read_json(f)
            if not t or t.get("pipeline_id") != pipeline_id:
                continue
            if t.get("status") != "blocked":
                continue
            deps = t.get("depends_on", []) or []
            for d in deps:
                if _task_status(d) in ("failed", "skipped"):
                    t["status"] = "skipped"
                    t["skipped_reason"] = "upstream_failed"
                    t["skipped_at"] = now_iso()
                    if safe_write_json(f, t):
                        append_journal_event(
                            t.get("id", f.stem),
                            "step_skipped",
                            detail="upstream_failed",
                        )
                        skipped_ids.append(t.get("id", f.stem))
                        changed = True
                    break
    return skipped_ids


async def create_task(
    task_title: str,
    task_content: str,
    assignee: str = "any",
    project_path: str = "",
    related_symbols: str = "",
    deadline: str = "",
    execution_env: str = "",
    review_required: bool = False,
    template: str = "",
    template_params: str = "",
    claim_timeout_minutes: int = 0,
    max_retries: int = 0,
    research_required: bool = False,
    priority: str = "medium",
) -> str:
    """创建一个新的协作任务，保存到 collab/inbox/ 目录。

    任务以 JSON 文件形式存储，文件名使用唯一 ID。
    创建后任何 Claude 实例都可以通过 get_pending_tasks 看到此任务。

    支持附加代码上下文，帮助接任务的 Claude 快速定位相关代码：
    - project_path: 相关项目的根目录路径
    - related_symbols: 逗号分隔的符号名（函数、类、文件等），
      接任务后可用 get_task_context 自动获取这些符号的代码图谱信息

    Args:
        task_title: 任务标题，简短描述（如 "修复登录 Bug"）
        task_content: 任务的详细说明，可以包含步骤、要求等
        assignee: 指派给哪个 Claude 实例（PC-A / PC-B / PC-C / any），默认 "any"
        project_path: 可选，相关项目的绝对路径（如 /mnt/hgfs/myshare/my-app）
        related_symbols: 可选，逗号分隔的相关符号（如 "login_handler,AuthService,config.py"）
        deadline: 可选，截止时间（ISO 8601 字符串）。超时未完成会被 notify_daemon
            标记为 stale_task 并告警。
        execution_env: 可选，执行环境约定。推荐值：hub VM / PC-B 本机 / 共享文件夹 / 任意。
            派单时写明任务应在哪一侧执行，避免路径歧义——例如 C:\\myshare 只存在于 hub VM，
            队友机需经 read_shared_file 读取或 SSH 到 VM 执行。
        template: 可选，任务模板（Playbook）名，如 "code-review" / "run-tests" / "write-doc"。
            传了模板后 title/content 由模板展开，可覆盖 assignee/deadline/execution_env/review_required。
        template_params: 可选，模板参数 JSON 对象字符串（如 '{"target": "login.py"}'）。
        claim_timeout_minutes: 可选，认领超时阈值（分钟），覆盖全局默认；0 表示用全局值。
        max_retries: 可选，失败自动重试次数（0 表示不重试）。重试把任务重置回 pending
            并清空认领，retry_count 递增；耗尽后仍失败则走终态失败 + 级联跳过。
        priority: 可选，任务优先级 critical / high / medium / low；非法值回退 medium。
    """
    params: dict = {}
    if template_params:
        try:
            loaded = json.loads(template_params)
        except json.JSONDecodeError:
            return fail("template_params 不是合法 JSON")
        if not isinstance(loaded, dict):
            return fail("template_params 必须是 JSON 对象")
        params = loaded

    template_used = ""
    if template:
        tpl = _load_template(template)
        if tpl is None:
            return fail(f"模板不存在: {template}（可用 list_templates 查看）")
        # 合并默认参数（显式传入优先）
        merged = dict(tpl.get("default_params", {}) or {})
        merged.update(params)
        params = merged
        # 校验必填参数
        for req in tpl.get("required_params", []) or []:
            if req not in params or not str(params.get(req, "")).strip():
                return fail(f"模板 {template} 缺少必填参数: {req}")
        if tpl.get("pipeline"):
            return await _create_pipeline(template, tpl, params)
        task_title = _fill_template(tpl.get("title", template), params)
        task_content = _fill_template(tpl.get("content", ""), params)
        if assignee == "any" and tpl.get("default_assignee"):
            assignee = tpl["default_assignee"]
        if not execution_env and tpl.get("default_execution_env"):
            execution_env = tpl["default_execution_env"]
        if not deadline and tpl.get("default_deadline_minutes"):
            minutes = max(1, int(tpl["default_deadline_minutes"]))
            deadline = (
                datetime.now(timezone.utc) + timedelta(minutes=minutes)
            ).isoformat()
        if tpl.get("review_required"):
            review_required = True
        template_used = template

    task_id = uuid.uuid4().hex[:12]
    task = {
        "id": task_id,
        "title": task_title,
        "content": task_content,
        "assignee": assignee,
        "status": "pending",
        "priority": _normalize_priority(priority),
        "created_at": now_iso(),
        "completed_at": None,
    }

    # 附加代码上下文
    if project_path:
        task["project_path"] = project_path
    if related_symbols:
        task["related_symbols"] = [
            s.strip() for s in related_symbols.split(",") if s.strip()
        ]
    if deadline:
        task["deadline"] = deadline
    if execution_env:
        task["execution_env"] = execution_env
    if review_required:
        task["review_required"] = True
    if template_used:
        task["template"] = template_used
    if claim_timeout_minutes and claim_timeout_minutes > 0:
        task["claim_timeout_minutes"] = claim_timeout_minutes
    if research_required:
        task["research_required"] = True
        task["research_done"] = False
        task["research_findings"] = ""
    if max_retries and max_retries > 0:
        task["max_retries"] = max_retries
        task["retry_count"] = 0

    file_path = INBOX_DIR / f"{task_id}.json"
    if not safe_write_json(file_path, task):
        return fail(f"无法创建任务文件: {file_path}")
    append_journal_event(task_id, "created", detail=task_title)

    code_info = ""
    if project_path:
        code_info += f" 📁 {project_path}"
    if related_symbols:
        code_info += f" 🔗 {related_symbols}"
    if execution_env:
        code_info += f" 🌐 {execution_env}"
    if template_used:
        code_info += f" 📋 {template_used}"

    logger.info(f"✅ 任务已创建 [{task_id}] 「{task_title}」→ {assignee}{code_info}")
    return ok({
        "message": f"任务「{task_title}」已创建",
        "task_id": task_id,
        "file": str(file_path),
        "has_code_context": bool(project_path or related_symbols),
        **({"template": template_used} if template_used else {}),
    })


def _release_stale_claims() -> int:
    """扫描 inbox/ 中已过期认领的 in_progress 任务，自动释放回 pending。

    v3.27.0: 防止 agent 崩溃后任务永久卡在 in_progress。
    超时阈值取任务级 claim_timeout_minutes，未设置则用 DEFAULT_CLAIM_TIMEOUT_MIN。
    返回释放数量。
    """
    released = 0
    now = datetime.now(timezone.utc)
    try:
        files = list(INBOX_DIR.glob("*.json"))
    except OSError:
        return 0
    for f in files:
        t = safe_read_json(f)
        if not t or t.get("status") != "in_progress":
            continue
        timeout_min = int(t.get("claim_timeout_minutes") or DEFAULT_CLAIM_TIMEOUT_MIN)
        claimed_at_str = t.get("claimed_at", "")
        if not claimed_at_str:
            continue
        try:
            claimed_dt = datetime.fromisoformat(claimed_at_str)
            if claimed_dt.tzinfo is None:
                claimed_dt = claimed_dt.astimezone()
        except (ValueError, TypeError):
            continue
        elapsed_min = (now - claimed_dt).total_seconds() / 60.0
        if elapsed_min > timeout_min:
            t["status"] = "pending"
            t["released_at"] = now_iso()
            prev = t.pop("claimed_by", None)
            if prev:
                t["previous_claimer"] = prev
            safe_write_json(f, t)
            logger.info(
                f"⏰ 释放过期认领: {t.get('id', f.stem)} "
                f"(原认领者={prev}, 已过 {elapsed_min:.0f} 分钟)"
            )
            released += 1
    return released




async def mark_research_done(task_id: str, findings: str = "") -> str:
    """标记一个任务的开工前调研已完成，记录调研结论。

    v3.27.0: 配合 create_task(research_required=true) 使用。
    调研完成后才能调用 claim_task，确保"先摸清再动手"纪律的机制化执行。

    Args:
        task_id: 任务的唯一标识
        findings: 可选，调研发现摘要（领域现状、已有方案、关键约束等）
    """
    denied = assert_identity_allowed("mark_research_done")
    if denied:
        return fail(denied)
    source = INBOX_DIR / f"{task_id}.json"
    if not source.exists():
        return fail(f"任务 {task_id} 未找到（可能已完成或不存在）")
    task = safe_read_json(source)
    if task is None:
        return fail(f"任务文件 {task_id} 损坏，无法读取")
    if not findings.strip():
        return fail("findings 不能为空：请提供调研发现摘要（哪怕一句话也比空好）")
    task["research_done"] = True
    task["research_findings"] = findings[:2000]
    task["research_completed_at"] = now_iso()
    task["research_completed_by"] = current_identity() or "local"
    safe_write_json(source, task)
    logger.info(f"📝 research_done: 任务={task_id} by={current_identity()}")
    return ok({
        "message": f"任务 {task_id} 调研已标记完成，可以认领",
        "findings_chars": len(findings),
    })


async def heartbeat() -> str:
    """发送心跳：刷新当前身份所有 in_progress 任务的 claimed_at，并更新队友 last_seen。

    v3.27.0: 防止长时间执行的任务被 _release_stale_claims 误释放。
    Agent 在执行长任务期间应定期调用（建议每 10 分钟）。
    v3.31.0: 同时更新 teammates.json 中匹配身份的 last_seen，供在线状态判定。
    """
    ident = current_identity()
    if not ident:
        return fail("heartbeat 需要调用者身份（COLLAB_IDENTITY）")
    refreshed = 0
    try:
        files = list(INBOX_DIR.glob("*.json"))
    except OSError as e:
        return fail(f"读取 inbox 目录失败: {e}")
    for f in files:
        t = safe_read_json(f)
        if not t or t.get("status") != "in_progress":
            continue
        if t.get("claimed_by") != ident:
            continue
        t["claimed_at"] = now_iso()
        safe_write_json(f, t)
        refreshed += 1
    last_seen_updated = await touch_teammate_last_seen(ident)
    logger.info(
        f"💓 heartbeat: identity={ident} refreshed={refreshed} "
        f"last_seen_updated={last_seen_updated}"
    )
    return ok({
        "identity": ident,
        "refreshed_tasks": refreshed,
        "last_seen_updated": last_seen_updated,
    })


async def get_pending_tasks(assignee: str = "") -> str:
    """获取所有待办任务列表，读取 collab/inbox/ 目录下的所有任务 JSON 文件。

    可按 assignee 筛选，只返回指派给特定 Claude 的任务。

    Args:
        assignee: 可选筛选条件。留空返回全部待办；填入 "PC-A" 则只返回指派给 PC-A 的任务
    """
    denied = assert_identity_allowed("get_pending_tasks")
    if denied:
        return fail(denied)

    # v3.27.0: 自动释放过期认领
    _release_stale_claims()

    ident = current_identity()
    if ident and not is_hub():
        if assignee and assignee not in (ident, "any"):
            logger.warning(f"⛔ 权限拒绝 [get_pending_tasks] identity={ident} 查询他人任务 assignee={assignee}")
            return fail(f"无权限查看指派给 {assignee} 的任务（当前身份: {ident}）")
        effective_assignee = assignee or ident
    else:
        effective_assignee = assignee

    tasks = []
    try:
        for file_path in sorted(INBOX_DIR.glob("*.json")):
            task = safe_read_json(file_path)
            if task is None:
                continue
            # done/ 中已存在同 ID → 视为已完成（旧版本非原子移动的残留），清理并跳过，
            # 避免同一任务被重复执行
            task_id = task.get("id", "")
            if task_id and (DONE_DIR / f"{task_id}.json").exists():
                try:
                    file_path.unlink()
                except OSError:
                    pass
                continue
            # 筛选逻辑
            if effective_assignee and task.get("assignee", "any") not in (
                effective_assignee, "any",
            ):
                continue
            tasks.append(task)
    except OSError as e:
        return fail(f"读取 inbox 目录失败: {e}")

    filter_info = f"（筛选: {effective_assignee}）" if effective_assignee else ""

    # v2.1.0：流水线父步骤由子流水线驱动，不参与待办
    tasks = [t for t in tasks if not t.get("is_pipeline_parent")]
    # v1.9.1：失败/跳过是终态，对所有身份都不参与待办
    tasks = [t for t in tasks if t.get("status") not in ("failed", "skipped")]
    # v1.7.1/v1.9.0：队友视角隐藏"已被他人认领/待审核/未解锁流水线步骤"，避免撞车
    if ident and not is_hub():
        tasks = [
            t for t in tasks
            if t.get("status") not in ("needs_review", "blocked")
            and (not t.get("claimed_by") or t.get("claimed_by") == ident)
        ]

    logger.info(f"📋 查询待办任务: {len(tasks)} 个 {filter_info}")
    return ok({"count": len(tasks), "tasks": tasks, **identity_note()})


async def claim_task(task_id: str) -> str:
    """认领一个任务，将其状态置为 in_progress 并记录认领者。

    v1.7.1 起（借鉴 MAF checkpointing / 所有权模型）：显式认领防止多个队友
    同时执行同一个任务。认领是幂等的——同一认领者重复认领返回成功
    （already_claimed=true），不会报错。

    Args:
        task_id: 任务的唯一标识（如 "a1b2c3d4"）
    """
    denied = assert_identity_allowed("claim_task")
    if denied:
        return fail(denied)
    ident = current_identity() or "local"

    source = INBOX_DIR / f"{task_id}.json"
    if not source.exists():
        return fail(f"任务 {task_id} 未找到（可能已完成或不存在）")
    task = safe_read_json(source)
    if task is None:
        return fail(f"任务文件 {task_id} 损坏，无法读取")

    if ident != "local" and not is_hub():
        task_assignee = task.get("assignee", "any")
        if task_assignee not in (ident, "any"):
            logger.warning(
                f"⛔ 权限拒绝 [claim_task] identity={ident} 任务={task_id} assignee={task_assignee}"
            )
            return fail(
                f"无权认领任务 {task_id}：指派给 {task_assignee}（当前身份: {ident}）"
            )

    # v3.27.0: research-first gate
    if task.get("research_required") and not task.get("research_done"):
        return fail(
            f"任务 {task_id} 需要先完成调研（research_required=true）。"
            "请调用 mark_research_done(task_id=..., findings=...) 后再认领。"
        )

    status = task.get("status", "pending")
    if status == "done":
        return fail(f"任务 {task_id} 已完成，无需认领")
    if status == "needs_review":
        return fail(f"任务 {task_id} 已提交审核，等待 hub 审批")
    if status == "blocked":
        return fail(f"任务 {task_id} 是流水线步骤，尚未解锁（依赖前序步骤完成）")
    if status in ("failed", "skipped"):
        return fail(f"任务 {task_id} 已失败/跳过，无法认领")
    if task.get("is_pipeline_parent"):
        return fail(f"任务 {task_id} 是流水线父步骤，由子流水线驱动，不可直接认领")

    claimed_by = task.get("claimed_by", "")
    if claimed_by:
        if claimed_by == ident:
            logger.info(f"🔁 认领幂等 [{task_id}] by {ident}")
            return ok({
                "message": f"任务 {task_id} 已由你认领",
                "task_id": task_id,
                "claimed_by": ident,
                "already_claimed": True,
                **identity_note(),
            })
        logger.info(f"⛔ 认领冲突 [{task_id}] 已被 {claimed_by} 认领")
        return fail(f"任务 {task_id} 已被 {claimed_by} 认领")

    task["status"] = "in_progress"
    task["claimed_by"] = ident
    task["claimed_at"] = now_iso()
    if not safe_write_json(source, task):
        return fail(f"无法写入任务文件: {source}")
    append_journal_event(task_id, "claimed", detail=ident)

    logger.info(f"🔒 任务认领 [{task_id}] by {ident} → in_progress")
    return ok({
        "message": f"任务 {task_id} 认领成功",
        "task_id": task_id,
        "claimed_by": ident,
        "claimed_at": task["claimed_at"],
        "already_claimed": False,
        **identity_note(),
    })


async def claim_next_task(assignee: str = "") -> str:
    """按优先级自动认领 inbox 中的下一个可执行任务。

    v3.32.0：供 Agent 自主取任务，避免中枢逐个派单。任务选择顺序为
    priority（critical > high > medium > low）、deadline（更早优先、无期限最后）、
    created_at（先创建优先）。认领通过 60 秒 TTL 的队列锁保证不重复认领。
    注册队友可用 max_concurrency 限制自己的并行任务数。

    Args:
        assignee: 可选，只认领指定给该队友的任务；队友留空时只认领给自己或 any。
    """
    denied = assert_identity_allowed("claim_next_task")
    if denied:
        return fail(denied)

    ident = current_identity() or "local"
    if ident != "local" and not is_hub():
        if assignee and assignee not in (ident, "any"):
            logger.warning(
                f"⛔ 权限拒绝 [claim_next_task] identity={ident} assignee={assignee}"
            )
            return fail(
                f"无权限认领指派给 {assignee} 的任务（当前身份: {ident}）"
            )
        effective_assignee = assignee or ident
    else:
        effective_assignee = assignee

    if not await _acquire_queue_lock(ident):
        logger.warning(f"⏳ claim_next_task 锁等待超时 by={ident}")
        return ok({
            "message": "队列锁正被持有，请稍后重试",
            "busy": True,
            "task_id": None,
            **identity_note(),
        })

    try:
        # 与 get_pending_tasks 一致：先释放过期认领。
        _release_stale_claims()

        capacity = _teammate_capacity_entry(ident)
        max_concurrency = int(capacity.get("max_concurrency") or 0)
        if max_concurrency > 0:
            in_progress = _count_own_in_progress(ident)
            if in_progress >= max_concurrency:
                return ok({
                    "message": f"已达最大并发 {max_concurrency}，暂不认领新任务",
                    "at_capacity": True,
                    "concurrency": in_progress,
                    "max_concurrency": max_concurrency,
                    "task_id": None,
                    **identity_note(),
                })

        candidates: list[dict] = []
        for file_path in sorted(INBOX_DIR.glob("*.json")):
            task = safe_read_json(file_path)
            if not task:
                continue
            task_id = task.get("id", "")
            if task_id and (DONE_DIR / f"{task_id}.json").exists():
                try:
                    file_path.unlink()
                except OSError:
                    pass
                continue
            if task.get("status") != "pending":
                continue
            if task.get("is_pipeline_parent"):
                continue
            if task.get("research_required") and not task.get("research_done"):
                continue
            task_assignee = task.get("assignee", "any")
            if effective_assignee and task_assignee not in (effective_assignee, "any"):
                continue
            candidates.append(task)

        if not candidates:
            return ok({
                "message": "no_tasks_available",
                "task_id": None,
                **identity_note(),
            })

        candidates.sort(key=_claim_sort_key)
        task = candidates[0]
        task_id = task.get("id", "")
        task["status"] = "in_progress"
        task["claimed_by"] = ident
        task["claimed_at"] = now_iso()
        target = INBOX_DIR / f"{task_id}.json"
        if not safe_write_json(target, task):
            return fail(f"无法写入任务文件: {target}")
        append_journal_event(task_id, "claimed", detail=f"claim_next:{ident}")

        logger.info(
            f"🔒 claim_next_task [{task_id}] 「{task.get('title', '')}」→ {ident}"
        )
        return ok({
            "message": f"任务 {task_id} 已自动认领",
            "task_id": task_id,
            "task_title": task.get("title", ""),
            "priority": task.get("priority", "medium"),
            "claimed_by": ident,
            "claimed_at": task["claimed_at"],
            "already_claimed": False,
            **identity_note(),
        })
    finally:
        _release_queue_lock()


async def complete_task(
    task_id: str,
    evidence: str = "",
    result_path: str = "",
    total_tokens: int = 0,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cost: float = 0.0,
    model: str = "",
) -> str:
    """标记一个任务为已完成：将任务文件从 collab/inbox/ 移动到 collab/done/。

    任务 JSON 中的 status 会更新为 "done"，并记录完成时间。
    v1.6.0 起：建议附 evidence（验证证据，如测试输出/关键 diff），
    中枢整合结果前可读取 done/ 中的任务核实，降低"口头完成"风险。
    v1.7.0 起：evidence 超过 8000 字符自动截断（防止上下文溢出），
    完整输出请写到共享文件并用 result_path 引用。

    Args:
        task_id: 任务的唯一标识（如 "a1b2c3d4"）
        evidence: 必填（或 result_path 二选一），完成证据（测试结果摘要、关键代码 diff、验证步骤等）
        result_path: 可选，完整输出的共享文件相对路径（如 "results/task123.md"）
        total_tokens: 可选，本次完成任务消耗的 token 总数
        input_tokens: 可选，输入 token 数
        output_tokens: 可选，输出 token 数
        cost: 可选，本次任务成本（由 agent 自行上报，不自动折算）
        model: 可选，消耗 token 的模型名
    """
    denied = assert_identity_allowed("complete_task")
    if denied:
        return fail(denied)

    ident = current_identity()
    source = INBOX_DIR / f"{task_id}.json"

    if not source.exists():
        # 幂等：dest 已存在说明之前已成功完成
        dest = DONE_DIR / f"{task_id}.json"
        if dest.exists():
            done_task = safe_read_json(dest)
            if ident and not is_hub():
                done_assignee = done_task.get("assignee", "any") if done_task else "any"
                if done_assignee not in (ident, "any"):
                    return fail(
                        f"无权限完成任务 {task_id}：指派给 {done_assignee}（当前身份: {ident}）"
                    )
            approved = bool(done_task and done_task.get("approved_by"))
            logger.info(f"🎉 任务 {task_id} 已完成（幂等返回{'，已审批锁定' if approved else ''}）")
            return ok({
                "message": f"任务 {task_id} 已完成" + ("（已审批锁定）" if approved else ""),
                "task_title": done_task.get("title") if done_task else None,
                "locked": approved,
            })
        return fail(f"任务 {task_id} 未找到（可能已完成或不存在）")

    task = safe_read_json(source)
    if task is None:
        return fail(f"任务文件 {task_id} 损坏，无法读取")

    # v3.26.0: evidence 必填化 — 零造假红线的机制保障
    if not evidence.strip() and not result_path.strip():
        return fail(
            f"完成任务 {task_id} 必须附 evidence（验证证据）或 result_path（完整输出文件路径）。"
            "零造假红线：不虚报已完成。请提供测试输出摘要、关键 diff、或共享文件路径。"
        )

    if ident and not is_hub():
        task_assignee = task.get("assignee", "any")
        if task_assignee not in (ident, "any"):
            logger.warning(f"⛔ 权限拒绝 [complete_task] identity={ident} 任务={task_id} assignee={task_assignee}")
            return fail(
                f"无权限完成任务 {task_id}：指派给 {task_assignee}（当前身份: {ident}）"
            )

    # v1.7.1：显式认领 + 隐式 self-claim（旧版 Agent 直接 complete 也能工作）
    status = task.get("status", "pending")
    claimed_by = task.get("claimed_by", "")
    if status == "needs_review":
        return fail(f"任务 {task_id} 已提交审核，等待 hub 审批（needs_review）")
    if status == "blocked":
        return fail(f"任务 {task_id} 是流水线步骤，尚未解锁（依赖前序步骤完成）")
    if status in ("failed", "skipped"):
        return fail(f"任务 {task_id} 已失败/跳过，无法完成")
    if task.get("is_pipeline_parent"):
        return fail(f"任务 {task_id} 是流水线父步骤，由子流水线驱动，不可直接完成")
    if ident and not is_hub() and claimed_by and claimed_by != ident:
        logger.warning(
            f"⛔ 完成冲突 [{task_id}] 已由 {claimed_by} 认领，当前身份 {ident}"
        )
        return fail(f"任务 {task_id} 已被 {claimed_by} 认领，只有认领者可完成")
    if status == "pending" and not claimed_by:
        # 隐式 self-claim：兼容未显式调用 claim_task 的旧 Agent
        task["status"] = "in_progress"
        task["claimed_by"] = ident or "local"
        task["claimed_at"] = now_iso()

    _attach_token_usage(task, total_tokens, input_tokens, output_tokens, cost, model)

    # v1.8.0：复核闸门（human-in-the-loop）— 需复核的任务提交后进入 needs_review，
    # 文件留在 inbox，等 hub approve_task / request_changes
    if task.get("review_required"):
        task["status"] = "needs_review"
        task["completed_at"] = now_iso()
        task["completed_by"] = ident or "unknown"
        _attach_evidence(task, evidence, result_path)
        if not safe_write_json(source, task):
            return fail(f"无法写入任务文件: {source}")
        append_journal_event(task_id, "submitted", detail=ident or "local")
        logger.info(f"🔎 任务提交审核 [{task_id}] 「{task.get('title', 'N/A')}」")
        return ok({
            "message": f"任务 {task_id} 已提交审核，等待 hub 审批",
            "task_title": task.get("title"),
            "completed_by": task.get("completed_by"),
            "status": "needs_review",
            "needs_review": True,
            "has_evidence": bool(task.get("evidence")),
            **identity_note(),
        })

    # 更新任务状态
    task["status"] = "done"
    task["completed_at"] = now_iso()
    task["completed_by"] = ident or "unknown"
    _attach_evidence(task, evidence, result_path)

    dest = DONE_DIR / f"{task_id}.json"
    if dest.exists():
        prev = safe_read_json(dest)
        if prev and prev.get("approved_by"):
            return fail(f"任务 {task_id} 在 done/ 中已审批锁定，拒绝覆盖")
    if not safe_write_json(dest, task):
        return fail(f"无法写入完成目录: {dest}")
    append_journal_event(task_id, "completed", detail=ident or "local")

    try:
        source.unlink()  # 从 inbox 移除
    except OSError as e:
        logger.warning(f"无法删除 inbox 中的任务文件 {source}: {e}")
        # 即使残留，done/ 为权威状态，get_pending_tasks 会跳过该任务

    # v1.9.0：流水线依赖解锁（前序完成 → 后续步骤转 pending）
    _unblock_pipeline_dependents(task)
    # v2.1.0：流水线聚合刷新（completed 时桥接父步骤）
    if task.get("pipeline_id"):
        _note_pipeline_status(task["pipeline_id"], task_id)

    logger.info(f"🎉 任务完成 [{task_id}] 「{task.get('title', 'N/A')}」")
    return ok({
        "message": f"任务 {task_id} 已完成",
        "task_title": task.get("title"),
        "completed_by": task.get("completed_by"),
        "has_evidence": bool(task.get("evidence")),
        **identity_note(),
    })


async def get_task_context(task_id: str) -> str:
    """获取任务的完整上下文，包括任务详情和相关代码的 CodeGraph 图谱信息。

    如果任务在创建时指定了 project_path 和 related_symbols，
    此工具会自动查询 CodeGraph 获取这些符号的定义、调用链等信息，
    帮助接任务的 Claude 快速理解相关代码结构。

    Args:
        task_id: 任务的唯一标识（如 "a1b2c3d4"）
    """
    denied = assert_identity_allowed("get_task_context")
    if denied:
        return fail(denied)

    source = INBOX_DIR / f"{task_id}.json"
    if not source.exists():
        # v1.6.0：已完成任务也可读取（done/），中枢可核实 evidence
        done_source = DONE_DIR / f"{task_id}.json"
        if done_source.exists():
            source = done_source
        else:
            return fail(f"任务 {task_id} 未找到（可能已完成或不存在）")

    task = safe_read_json(source)
    if task is None:
        return fail(f"任务文件 {task_id} 损坏，无法读取")

    ident = current_identity()
    if ident and not is_hub():
        task_assignee = task.get("assignee", "any")
        if task_assignee not in (ident, "any"):
            logger.warning(f"⛔ 权限拒绝 [get_task_context] identity={ident} 任务={task_id} assignee={task_assignee}")
            return fail(
                f"无权限查看任务 {task_id}：指派给 {task_assignee}（当前身份: {ident}）"
            )

    result = {"task": task, "code_context": []}
    # v1.7.2：任务事件日志（时间旅行/回放视图）
    result["history"] = read_task_journal(task_id)

    # 如果任务带有代码上下文，查询 CodeGraph
    project_path = task.get("project_path", "")
    related_symbols = task.get("related_symbols", [])

    if project_path and related_symbols:
        target = Path(project_path)

        if not target.exists():
            result["code_context"].append({
                "type": "warning",
                "message": f"项目路径不存在: {project_path}",
            })
        else:
            try:
                db = open_codegraph_db(target)
            except FileNotFoundError:
                result["code_context"].append({
                    "type": "warning",
                    "message": (
                        f"项目 {project_path} 尚未初始化 CodeGraph。"
                        f"请执行: cd {project_path} && codegraph init"
                    ),
                })
            else:
                try:
                    for symbol in related_symbols:
                        logger.info(f"🔍 查询任务上下文 [{task_id}]: {symbol}")
                        nodes = search_nodes(db, symbol, limit=3)
                        if nodes:
                            report = format_codegraph_result(db, nodes, target)
                            if len(report) > CODE_CONTEXT_MAX_CHARS:
                                report = (
                                    report[:CODE_CONTEXT_MAX_CHARS]
                                    + f"\n…[代码上下文已截断至 {CODE_CONTEXT_MAX_CHARS} 字符]"
                                )
                            result["code_context"].append({
                                "symbol": symbol,
                                "status": "found",
                                "insight": report,
                            })
                        else:
                            result["code_context"].append({
                                "symbol": symbol,
                                "status": "not_found",
                                "message": f"未找到符号: {symbol}",
                            })
                finally:
                    db.close()

    has_context = len(result["code_context"]) > 0
    logger.info(
        f"📋 任务上下文 [{task_id}]: "
        + (f"{len(result['code_context'])} 个符号已查询" if has_context else "无代码上下文")
    )

    return ok({
        "task": result["task"],
        "code_context": result["code_context"],
        "history": result["history"],
        "has_code_context": has_context,
    })


async def force_assign(task_id: str, new_assignee: str, reason: str = "") -> str:
    """强制转移任务给其他队友（仅中枢可用，用于死信/超时任务回收）。

    v1.6.0 起：任务超时无人接（notify_daemon 发出 stale_task 告警）后，
    中枢可以把任务转给在线队友。转移动作写入 assign_history，可审计。

    Args:
        task_id: 任务的唯一标识
        new_assignee: 新的 assignee（如 "PC-B" / "any"）
        reason: 可选，转移原因（建议填超时/队友离线等）
    """
    denied = assert_identity_allowed("force_assign")
    if denied:
        return fail(denied)
    ident = current_identity()
    if ident and not is_hub():
        return fail("force_assign 仅中枢（PC-A / COLLAB_ROLE=hub）可用")

    source = INBOX_DIR / f"{task_id}.json"
    if not source.exists():
        return fail(f"任务 {task_id} 未找到（可能已完成或不存在）")
    task = safe_read_json(source)
    if task is None:
        return fail(f"任务文件 {task_id} 损坏，无法读取")

    history = task.get("assign_history", [])
    history.append({
        "from": task.get("assignee"),
        "to": new_assignee,
        "by": ident or "local",
        "at": now_iso(),
        "reason": reason,
    })
    task["assignee"] = new_assignee
    task["assign_history"] = history
    # v1.7.1：转移即释放认领，回到 pending 让新执行者可认领
    task["status"] = "pending"
    task.pop("claimed_by", None)
    task.pop("claimed_at", None)

    if not safe_write_json(source, task):
        return fail(f"无法写入任务文件: {source}")
    append_journal_event(
        task_id,
        "transferred",
        detail=f"{history[-1].get('from')} → {new_assignee}",
    )

    logger.info(f"🔄 任务转移 [{task_id}] → {new_assignee}（by {ident or 'local'}）")
    return ok({
        "message": f"任务 {task_id} 已转移给 {new_assignee}",
        "task_id": task_id,
        "new_assignee": new_assignee,
        "assign_history": history,
        **identity_note(),
    })


async def approve_task(task_id: str, comment: str = "") -> str:
    """审批通过一个待复核任务（仅中枢可用），并移动到 done/。

    v1.8.0：配合 create_task(review_required=true) 使用，构成人工复核闸门。
    审批通过后任务状态置为 done，记录 approved_by / approved_at。

    Args:
        task_id: 任务的唯一标识
        comment: 可选，审批意见
    """
    denied = assert_identity_allowed("approve_task")
    if denied:
        return fail(denied)
    ident = current_identity()
    if ident and not is_hub():
        return fail("approve_task 仅中枢（PC-A / COLLAB_ROLE=hub）可用")

    source = INBOX_DIR / f"{task_id}.json"
    if not source.exists():
        return fail(f"任务 {task_id} 未找到（可能已完成或不存在）")
    task = safe_read_json(source)
    if task is None:
        return fail(f"任务文件 {task_id} 损坏，无法读取")
    if task.get("status") != "needs_review":
        return fail(
            f"任务 {task_id} 不在待审核状态（当前: {task.get('status', 'pending')}）"
        )

    task["status"] = "done"
    task["approved_by"] = ident or "local"
    task["approved_at"] = now_iso()
    if comment:
        task["review_comment"] = comment

    dest = DONE_DIR / f"{task_id}.json"
    if dest.exists():
        prev = safe_read_json(dest)
        if prev and prev.get("approved_by"):
            return fail(f"任务 {task_id} 在 done/ 中已审批锁定，拒绝覆盖")
    if not safe_write_json(dest, task):
        return fail(f"无法写入完成目录: {dest}")
    try:
        source.unlink()  # 从 inbox 移除
    except OSError as e:
        logger.warning(f"无法删除 inbox 中的任务文件 {source}: {e}")
    append_journal_event(task_id, "approved", detail=ident or "local")
    # v1.9.0：审批通过的流水线步骤同样触发依赖解锁
    _unblock_pipeline_dependents(task)
    # v2.1.0：流水线聚合刷新（completed 时桥接父步骤）
    if task.get("pipeline_id"):
        _note_pipeline_status(task["pipeline_id"], task_id)

    logger.info(f"✅ 任务审批通过 [{task_id}] 「{task.get('title', 'N/A')}」")
    return ok({
        "message": f"任务 {task_id} 审核通过",
        "task_title": task.get("title"),
        "approved_by": task.get("approved_by"),
        **identity_note(),
    })


async def request_changes(task_id: str, reason: str = "") -> str:
    """打回一个待复核任务（仅中枢可用）：回到 pending 并释放认领。

    v1.8.0：执行者可在收到打回后重新认领并修改，review_notes 记录完整打回历史。

    Args:
        task_id: 任务的唯一标识
        reason: 打回原因（建议必填，供执行者理解）
    """
    denied = assert_identity_allowed("request_changes")
    if denied:
        return fail(denied)
    ident = current_identity()
    if ident and not is_hub():
        return fail("request_changes 仅中枢（PC-A / COLLAB_ROLE=hub）可用")

    source = INBOX_DIR / f"{task_id}.json"
    if not source.exists():
        return fail(f"任务 {task_id} 未找到（可能已完成或不存在）")
    task = safe_read_json(source)
    if task is None:
        return fail(f"任务文件 {task_id} 损坏，无法读取")
    if task.get("status") != "needs_review":
        return fail(
            f"任务 {task_id} 不在待审核状态（当前: {task.get('status', 'pending')}）"
        )

    notes = task.get("review_notes", [])
    notes.append({
        "by": ident or "local",
        "at": now_iso(),
        "reason": reason,
    })
    task["review_notes"] = notes
    task["status"] = "pending"
    task.pop("claimed_by", None)
    task.pop("claimed_at", None)

    if not safe_write_json(source, task):
        return fail(f"无法写入任务文件: {source}")
    append_journal_event(task_id, "changes_requested", detail=reason or "打回")

    logger.info(f"↩️ 任务打回 [{task_id}] 「{task.get('title', 'N/A')}」: {reason}")
    return ok({
        "message": f"任务 {task_id} 已打回，可重新认领",
        "task_title": task.get("title"),
        "review_notes": notes,
        **identity_note(),
    })


async def fail_task(task_id: str, reason: str = "") -> str:
    """标记一个任务/流水线步骤失败（v1.9.1）。

    失败语义：
    - 任务状态置为 failed，记录 failed_by / failed_at / failure_reason
    - 若属于流水线：依赖它的 blocked 步骤级联标记 skipped（upstream_failed），
      pipeline_status 聚合为 failed，feed 发 pipeline_aborted + 聊天 URGENT 告警
    - journal 记 step_failed / step_skipped / pipeline_status

    Args:
        task_id: 任务的唯一标识
        reason: 失败原因（建议必填）
    """
    denied = assert_identity_allowed("fail_task")
    if denied:
        return fail(denied)
    ident = current_identity() or "local"

    source = INBOX_DIR / f"{task_id}.json"
    if not source.exists():
        return fail(f"任务 {task_id} 未找到（可能已完成或不存在）")
    task = safe_read_json(source)
    if task is None:
        return fail(f"任务文件 {task_id} 损坏，无法读取")
    if task.get("is_pipeline_parent"):
        return fail(f"任务 {task_id} 是流水线父步骤，由子流水线驱动，不可直接标记失败")

    status = task.get("status", "pending")
    if status in ("done", "failed", "skipped", "needs_review"):
        return fail(f"任务 {task_id} 当前状态 {status} 不可标记失败")
    if ident != "local" and not is_hub():
        task_assignee = task.get("assignee", "any")
        if task_assignee not in (ident, "any"):
            logger.warning(
                f"⛔ 权限拒绝 [fail_task] identity={ident} 任务={task_id} assignee={task_assignee}"
            )
            return fail(
                f"无权操作任务 {task_id}：指派给 {task_assignee}（当前身份: {ident}）"
            )
        claimed_by = task.get("claimed_by", "")
        if claimed_by and claimed_by != ident:
            return fail(f"任务 {task_id} 已被 {claimed_by} 认领，只有认领者可操作")

    # v2.0.2：Retry Loop — 未耗尽重试次数时重置回 pending，而非终态失败
    max_retries = int(task.get("max_retries") or 0)
    retry_count = int(task.get("retry_count") or 0)
    if max_retries > 0 and retry_count < max_retries:
        task["retry_count"] = retry_count + 1
        task["status"] = "pending"
        task.pop("claimed_by", None)
        task.pop("claimed_at", None)
        task["last_failure"] = {
            "reason": reason,
            "at": now_iso(),
            "by": ident,
        }
        if not safe_write_json(source, task):
            return fail(f"无法写入任务文件: {source}")
        append_journal_event(
            task_id,
            "step_retried",
            detail=f"attempt {task['retry_count']}/{max_retries}",
        )
        _append_feed_event({
            "id": uuid.uuid4().hex[:8],
            "ts": now_iso(),
            "type": "step_retried",
            "task_id": task_id,
            "title": task.get("title", ""),
            "attempt": task["retry_count"],
            "max_retries": max_retries,
            "reason": reason,
        })
        logger.info(f"🔁 任务重试 [{task_id}] {task['retry_count']}/{max_retries}")
        return ok({
            "message": f"任务 {task_id} 已自动重试（{task['retry_count']}/{max_retries}）",
            "task_id": task_id,
            "status": "pending",
            "retry_count": task["retry_count"],
            "max_retries": max_retries,
            **identity_note(),
        })

    # 重试耗尽（或无重试策略）→ 终态失败 + 级联
    task["status"] = "failed"
    task["failed_by"] = ident
    task["failed_at"] = now_iso()
    if reason:
        task["failure_reason"] = reason
    if not safe_write_json(source, task):
        return fail(f"无法写入任务文件: {source}")
    append_journal_event(task_id, "step_failed", detail=reason or "无原因")

    pipeline_id = task.get("pipeline_id", "")
    agg = None
    skipped_ids = []
    if pipeline_id:
        skipped_ids = _cascade_skip_dependents(task)
        _note_pipeline_status(pipeline_id, task_id)
        agg = aggregate_pipeline(pipeline_id)
        _append_feed_event({
            "id": uuid.uuid4().hex[:8],
            "ts": now_iso(),
            "type": "pipeline_aborted",
            "pipeline_id": pipeline_id,
            "pipeline_name": task.get("pipeline_name", ""),
            "at_step": task.get("step_name", task_id),
            "failed_task_id": task_id,
            "reason": reason,
        })
        _system_chat_message(
            f"🚨 URGENT 流水线 [{pipeline_id}] 中止：步骤「{task.get('step_name', task_id)}」"
            f"失败（{reason or '未提供原因'}），{len(skipped_ids)} 个依赖步骤已跳过，请中枢处理"
        )

    logger.info(f"❌ 任务失败 [{task_id}]：{reason or '无原因'}")
    return ok({
        "message": f"任务 {task_id} 已标记失败",
        "task_id": task_id,
        "status": "failed",
        "failed_by": ident,
        "skipped_steps": skipped_ids,
        **({"pipeline_status": agg["status"]} if agg else {}),
        **identity_note(),
    })


async def list_templates() -> str:
    """列出可用的任务模板（Playbook）。

    v1.8.4：模板是 collab/templates/*.json，供 create_task(template=...) 一键展开，
    相当于 MAF workflow 的最轻形态。返回每个模板的名称、说明、必填/默认参数与默认策略。
    """
    templates = []
    if TEMPLATES_DIR.is_dir():
        for f in sorted(TEMPLATES_DIR.glob("*.json")):
            tpl = safe_read_json(f)
            if not tpl:
                continue
            templates.append({
                "name": tpl.get("name", f.stem),
                "description": tpl.get("description", ""),
                "pipeline": bool(tpl.get("pipeline")),
                "step_count": (
                    len(tpl.get("pipeline") or [])
                    if tpl.get("pipeline")
                    else None
                ),
                "required_params": tpl.get("required_params", []),
                "default_params": tpl.get("default_params", {}),
                "default_assignee": tpl.get("default_assignee", ""),
                "default_execution_env": tpl.get("default_execution_env", ""),
                "default_deadline_minutes": tpl.get("default_deadline_minutes"),
                "review_required": bool(tpl.get("review_required")),
            })
    logger.info(f"📋 模板列表: {len(templates)} 个")
    return ok({"count": len(templates), "templates": templates})
