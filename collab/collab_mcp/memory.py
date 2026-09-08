"""团队记忆工具：remember_fact / search_memory（v2.2.0）。

存储：collab/memory/ 下按 <时间戳>_<uuid12>.md 命名的事实文件，
文件首块为 JSON 元数据（--- 包裹），其后为 Markdown 正文。
设计对齐借鉴分析（TencentDB-Agent-Memory）：团队级事实本地优先、
可全文检索；journal 负责事件时间线，memory 补语义记忆。

安全约定：
- 文件名仅由时间戳 + 随机 id 生成，不接受用户输入路径 → 无路径穿越面
- 写入（remember_fact）与检索（search_memory）都做身份校验：
  REQUIRE_IDENTITY=1 时未绑定身份的会话被拒；本地开发保持开放
"""

import json
import os
import uuid
from datetime import datetime
from pathlib import Path

from .config import MEMORY_DIR
from .identity import assert_identity_allowed, current_identity
from .logging_setup import logger
from .utils import fail, now_iso, ok

_META_SEP = "---"
_MAX_CONTENT_CHARS = 20000


def _parse_tags(tags: str) -> list[str]:
    """把逗号/顿号分隔的标签串解析为去重后的干净列表。"""
    out: list[str] = []
    for raw in (tags or "").replace("，", ",").replace("、", ",").split(","):
        tag = raw.strip().lstrip("#")
        if tag and tag not in out:
            out.append(tag)
    return out


def _normalize_tags(raw) -> list[str]:
    """把元数据里的 tags 归一为 list[str]（v2.2.1，吸收 PC-B M1）。

    容忍手工/损坏文件里的异常类型：字符串按标签串解析；list/tuple/set 逐项
    转 str 去重；其余类型（数字等）视为无标签，保证检索不因坏元数据崩溃。
    """
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
    """原子写文本：临时文件 + fsync + os.replace（与 safe_write_json 同策略）。"""
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
        logger.error(f"写入记忆文件失败 {file_path}: {e}")
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def _read_memory_file(file_path: Path) -> dict | None:
    """读取记忆文件：解析首块 JSON 元数据，返回 {元数据..., content, path}。"""
    try:
        text = file_path.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning(f"跳过损坏记忆文件 {file_path.name}: {e}")
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
        logger.warning(f"跳过元数据损坏的记忆文件 {file_path.name}: {e}")
        return None
    if not isinstance(meta, dict):
        return None
    body = "\n".join(lines[end + 1 :]).strip()
    return {
        "id": str(meta.get("id", "")),
        "title": str(meta.get("title", "")),
        "tags": _normalize_tags(meta.get("tags")),
        "created_by": str(meta.get("created_by", "")),
        "created_at": str(meta.get("created_at", "")),
        "source": str(meta.get("source", "")),
        "content": body,
        "path": str(file_path.relative_to(MEMORY_DIR)),
    }


async def remember_fact(
    title: str,
    content: str,
    tags: str = "",
    source: str = "",
) -> str:
    """记录一条团队记忆事实到 collab/memory/*.md。

    Args:
        title: 事实标题（简短概括，便于检索）
        content: 事实正文（Markdown 文本）
        tags: 可选，逗号/顿号分隔的标签（如 "架构,决策,坑"）
        source: 可选，事实来源（如任务 id / 文档路径 / 对话）
    """
    denied = assert_identity_allowed("remember_fact")
    if denied:
        return fail(denied)
    if not title or not title.strip():
        return fail("remember_fact 需要非空 title")
    if not content or not content.strip():
        return fail("remember_fact 需要非空 content")
    if len(content) > _MAX_CONTENT_CHARS:
        return fail(f"content 超过上限 {_MAX_CONTENT_CHARS} 字符")

    ident = current_identity() or "local"
    fact_id = uuid.uuid4().hex[:12]
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    tag_list = _parse_tags(tags)
    meta = {
        "id": fact_id,
        "title": title.strip(),
        "tags": tag_list,
        "created_by": ident,
        "created_at": now_iso(),
        "source": source.strip() or "",
    }
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    file_path = MEMORY_DIR / f"{ts}_{fact_id}.md"
    body = (
        f"{_META_SEP}\n"
        f"{json.dumps(meta, ensure_ascii=False, indent=2)}\n"
        f"{_META_SEP}\n\n"
        f"{content.strip()}\n"
    )
    if not _atomic_write_text(file_path, body):
        return fail(f"无法写入记忆文件: {file_path.name}")

    logger.info(f"🧠 记忆写入 [{fact_id}] {meta['title'][:40]} by {ident}")
    return ok({
        "message": f"记忆已记录 (ID: {fact_id})",
        "fact_id": fact_id,
        "path": file_path.name,
        "title": meta["title"],
        "tags": tag_list,
        "created_by": ident,
    })


async def search_memory(query: str = "", tags: str = "", limit: int = 20) -> str:
    """全文/标签检索团队记忆，按时间倒序返回（新在前）。

    检索语义：query 按空白拆词、全部命中才算匹配（AND）；tags 为子集过滤；
    query 与 tags 都为空时返回最近 limit 条。limit 夹取 [1, 100]。

    Args:
        query: 可选，关键词（空格分隔多个词，全部命中）
        tags: 可选，只返回包含全部指定标签的事实
        limit: 可选，返回条数上限，默认 20
    """
    denied = assert_identity_allowed("search_memory")
    if denied:
        return fail(denied)
    try:
        limit = max(1, min(int(limit), 100))
    except (TypeError, ValueError):
        limit = 20

    terms = [t.lower() for t in (query or "").split() if t.strip()]
    want_tags = set(_parse_tags(tags))
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)

    results = []
    for f in MEMORY_DIR.glob("*.md"):
        fact = _read_memory_file(f)
        if not fact:
            continue
        fact_tags = set(fact.get("tags", []) or [])
        if want_tags and not want_tags.issubset(fact_tags):
            continue
        if terms:
            hay = (
                f"{fact.get('title', '')}\n{fact.get('content', '')}\n"
                f"{' '.join(fact_tags)}"
            ).lower()
            if not all(t in hay for t in terms):
                continue
        snippet = fact.get("content", "")[:200]
        results.append({
            "id": fact.get("id"),
            "title": fact.get("title", ""),
            "tags": sorted(fact_tags),
            "snippet": snippet,
            "created_by": fact.get("created_by", ""),
            "created_at": fact.get("created_at", ""),
            "source": fact.get("source", ""),
            "path": fact.get("path", f.name),
        })

    results.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    results = results[:limit]
    logger.info(f"🧠 记忆检索: {len(results)} 条")
    return ok({"count": len(results), "results": results})
