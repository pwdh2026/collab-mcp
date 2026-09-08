"""技能注册表工具：list_skills / find_skill / register_skill（v2.5.0）。

借鉴 mattpocock/skills 的设计哲学：技能小而可组合、可发现、可修改——
平台把"会干什么"集中登记为可检索的注册表，派单前按 capabilities 标签路由到
正确的模板/流水线/工具/文档（只做发现与路由，不把流程写死）。

存储：collab/skills/<slug>.md（文件首块 JSON 元数据，--- 包裹，其后为说明正文）。
元数据字段：
- name: 技能 slug（^[a-z0-9][a-z0-9-]{0,63}$）
- description: 一句话描述（进入检索）
- capability_tags: 能力标签（domain:action 格式，与 teammates.skills 对齐）
- entry: 执行入口（template:<模板> / pipeline:<流水线> / tools:<工具1,工具2> / doc:<相对路径>）
- scope: 适用范围（task / tool / platform）
- status: ready / draft
- created_by / created_at / updated_by / updated_at

安全约定：
- 文件名仅由 name slug 生成（严格正则），不接受用户输入路径 → 无路径穿越面
- 写入（register_skill）与检索（list/find）都做身份校验：
  REQUIRE_IDENTITY=1 时未绑定身份被拒；本地开发保持开放
"""

import json
import os
import re
import uuid
from pathlib import Path

from .config import SKILLS_DIR
from .identity import assert_identity_allowed, current_identity
from .logging_setup import logger
from .utils import fail, now_iso, ok

_META_SEP = "---"
_MAX_BODY_CHARS = 20000
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_ENTRY_PREFIXES = ("template:", "pipeline:", "tools:", "doc:")
_SCOPES = ("task", "tool", "platform")
_STATUSES = ("ready", "draft")


def _parse_tags(tags: str) -> list[str]:
    """把逗号/顿号分隔的标签串解析为去重后的干净列表。"""
    out: list[str] = []
    for raw in (tags or "").replace("，", ",").replace("、", ",").split(","):
        tag = raw.strip().lstrip("#")
        if tag and tag not in out:
            out.append(tag)
    return out


def _normalize_tags(raw) -> list[str]:
    """把元数据里的 capability_tags 归一为 list[str]（容忍手工/损坏文件）。"""
    if isinstance(raw, str):
        return _parse_tags(raw)
    if isinstance(raw, (list, tuple, set)):
        out: list[str] = []
        for item in raw:
            tag = str(item).strip().lstrip("#") if item is not None else ""
            if tag and tag not in out:
                out.append(tag)
        return out
    return []


def _atomic_write_text(file_path: Path, text: str) -> bool:
    """原子写文本：临时文件 + fsync + os.replace（与 memory 同策略）。"""
    tmp_path = file_path.with_name(
        f".{file_path.name}.{os.getpid()}.{uuid.uuid4().hex[:6]}.tmp"
    )
    try:
        tmp_path.write_text(text, encoding="utf-8")
        try:
            with open(tmp_path, "rb+") as f:
                os.fsync(f.fileno())
        except OSError:
            pass
        os.replace(tmp_path, file_path)
        return True
    except (IOError, OSError) as e:
        logger.error(f"写入技能文件失败 {file_path}: {e}")
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def _read_skill_file(file_path: Path) -> dict | None:
    """读取技能文件：解析首块 JSON 元数据，返回 {元数据..., body, path}。"""
    try:
        text = file_path.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning(f"跳过损坏技能文件 {file_path.name}: {e}")
        return None
    lines = text.splitlines()
    if not lines or lines[0].strip() != _META_SEP:
        return None
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == _META_SEP:
            end = i
            break
    if end is None:
        return None
    try:
        meta = json.loads("\n".join(lines[1:end]))
    except json.JSONDecodeError as e:
        logger.warning(f"跳过元数据损坏的技能文件 {file_path.name}: {e}")
        return None
    if not isinstance(meta, dict):
        return None
    body = "\n".join(lines[end + 1 :]).strip()
    return {
        "name": str(meta.get("name", "")),
        "description": str(meta.get("description", "")),
        "capability_tags": _normalize_tags(meta.get("capability_tags")),
        "entry": str(meta.get("entry", "")),
        "scope": str(meta.get("scope", "task")),
        "status": str(meta.get("status", "draft")),
        "created_by": str(meta.get("created_by", "")),
        "created_at": str(meta.get("created_at", "")),
        "updated_by": str(meta.get("updated_by", "")),
        "updated_at": str(meta.get("updated_at", "")),
        "body": body,
        "path": str(file_path.relative_to(SKILLS_DIR)),
    }


def _valid_entry(entry: str) -> bool:
    """校验 entry 格式：带前缀且前缀后有内容。"""
    for prefix in _ENTRY_PREFIXES:
        if entry.startswith(prefix):
            return len(entry) > len(prefix)
    return False


async def register_skill(
    name: str,
    description: str,
    capability_tags: str = "",
    entry: str = "",
    scope: str = "task",
    status: str = "ready",
    body: str = "",
) -> str:
    """登记或更新一个技能到 collab/skills/<slug>.md。

    技能注册表用于派单前发现与路由：description/capability_tags 进入检索，
    entry 给出执行入口（template/pipeline/tools/doc 前缀）。同名已存在则更新
    （保留 created_at，记录 updated_by/updated_at）。

    Args:
        name: 技能 slug（大小写不敏感，统一归一为小写；小写字母/数字开头，仅小写字母、数字、连字符，≤64 字符）
        description: 一句话描述（必填，进入检索）
        capability_tags: 可选，逗号分隔能力标签（"domain:action" 格式）
        entry: 可选，执行入口（template:<名> / pipeline:<名> / tools:<a,b> / doc:<路径>）
        scope: 适用范围（task / tool / platform），默认 task
        status: 状态（ready / draft），默认 ready
        body: 可选，使用说明正文（Markdown）
    """
    denied = assert_identity_allowed("register_skill")
    if denied:
        return fail(denied)
    name = (name or "").strip().lower()
    if not _SLUG_RE.match(name):
        return fail("name 统一归一为小写 slug（大小写不敏感）：小写字母/数字开头，仅含小写字母、数字、连字符（≤64 字符）")
    if not description or not description.strip():
        return fail("register_skill 需要非空 description")
    entry = (entry or "").strip()
    if entry and not _valid_entry(entry):
        return fail("entry 必须是 template:<名> / pipeline:<名> / tools:<a,b> / doc:<路径>")
    if scope not in _SCOPES:
        return fail(f"scope 必须是 {', '.join(_SCOPES)} 之一")
    if status not in _STATUSES:
        return fail(f"status 必须是 {', '.join(_STATUSES)} 之一")
    if len(body) > _MAX_BODY_CHARS:
        return fail(f"body 超过上限 {_MAX_BODY_CHARS} 字符")

    ident = current_identity() or "local"
    tag_list = _parse_tags(capability_tags)
    file_path = SKILLS_DIR / f"{name}.md"
    existing = _read_skill_file(file_path) if file_path.exists() else None
    now = now_iso()
    if existing:
        meta = {
            "name": name,
            "description": description.strip(),
            "capability_tags": tag_list,
            "entry": entry or existing.get("entry", ""),
            "scope": scope or existing.get("scope", "task"),
            "status": status or existing.get("status", "ready"),
            "created_by": existing.get("created_by", ""),
            "created_at": existing.get("created_at", ""),
            "updated_by": ident,
            "updated_at": now,
        }
        action = "更新"
    else:
        meta = {
            "name": name,
            "description": description.strip(),
            "capability_tags": tag_list,
            "entry": entry,
            "scope": scope,
            "status": status,
            "created_by": ident,
            "created_at": now,
            "updated_by": "",
            "updated_at": "",
        }
        action = "登记"
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    text = (
        f"{_META_SEP}\n"
        f"{json.dumps(meta, ensure_ascii=False, indent=2)}\n"
        f"{_META_SEP}\n\n"
        f"{(body or '').strip()}\n"
    )
    if not _atomic_write_text(file_path, text):
        return fail(f"无法写入技能文件: {file_path.name}")

    logger.info(f"🗂 技能{action} [{name}] by {ident}")
    return ok({
        "message": f"技能已{action} (name: {name})",
        "name": name,
        "action": action,
        "entry": meta["entry"],
        "capability_tags": tag_list,
        "path": file_path.name,
    })


async def list_skills(tag: str = "", status: str = "") -> str:
    """列出技能注册表中的全部技能（可按标签/状态过滤）。

    Args:
        tag: 可选，只返回包含该标签的技能
        status: 可选，只返回指定状态（ready / draft）
    """
    denied = assert_identity_allowed("list_skills")
    if denied:
        return fail(denied)
    want_tag = (tag or "").strip()
    want_status = (status or "").strip()
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    skills = []
    for f in sorted(SKILLS_DIR.glob("*.md")):
        s = _read_skill_file(f)
        if not s:
            continue
        if want_tag and want_tag not in s.get("capability_tags", []):
            continue
        if want_status and s.get("status", "") != want_status:
            continue
        skills.append({
            "name": s["name"],
            "description": s["description"],
            "capability_tags": s.get("capability_tags", []),
            "entry": s.get("entry", ""),
            "scope": s.get("scope", "task"),
            "status": s.get("status", "draft"),
            "updated_at": s.get("updated_at", "") or s.get("created_at", ""),
        })
    logger.info(f"🗂 技能列表: {len(skills)} 个")
    return ok({"count": len(skills), "skills": skills})


async def find_skill(query: str = "", tags: str = "", limit: int = 20) -> str:
    """按关键词/标签检索技能注册表，返回匹配技能与建议入口。

    检索语义：query 按空白拆词、全部命中（AND）name/description/标签/正文；
    tags 为子集过滤；都为空时返回最近登记的前 limit 条。limit 夹取 [1, 100]。
    结果里的 entry 可直接用于 create_task(template=...) 派单。

    Args:
        query: 可选，关键词（空格分隔多个词，全部命中）
        tags: 可选，只返回包含全部指定标签的技能
        limit: 可选，返回条数上限，默认 20
    """
    denied = assert_identity_allowed("find_skill")
    if denied:
        return fail(denied)
    try:
        limit = max(1, min(int(limit), 100))
    except (TypeError, ValueError):
        limit = 20
    terms = [t.lower() for t in (query or "").split() if t.strip()]
    want_tags = set(_parse_tags(tags))
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    results = []
    for f in SKILLS_DIR.glob("*.md"):
        s = _read_skill_file(f)
        if not s:
            continue
        skill_tags = set(s.get("capability_tags", []) or [])
        if want_tags and not want_tags.issubset(skill_tags):
            continue
        if terms:
            hay = (
                f"{s.get('name', '')}\n{s.get('description', '')}\n"
                f"{' '.join(skill_tags)}\n{s.get('body', '')}"
            ).lower()
            if not all(t in hay for t in terms):
                continue
        results.append({
            "name": s["name"],
            "description": s["description"],
            "capability_tags": sorted(skill_tags),
            "entry": s.get("entry", ""),
            "scope": s.get("scope", "task"),
            "status": s.get("status", "draft"),
            # 排序键（docstring：都为空时返回最近登记的前 limit 条）。
            # v3.20.1 修复：此前结果字典漏带 updated_at/created_at，
            # 排序键恒为空串 → 实际退化为文件名字母序，新增技能可能顶掉“最近登记”。
            "updated_at": s.get("updated_at", "") or s.get("created_at", ""),
            "snippet": (s.get("body", "") or "")[:200],
        })
    results.sort(
        key=lambda r: (r.get("updated_at", "") or r.get("created_at", "")),
        reverse=True,
    )
    results = results[:limit]
    logger.info(f"🗂 技能检索: {len(results)} 条")
    return ok({"count": len(results), "results": results})
