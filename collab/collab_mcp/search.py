"""文档全文检索工具：search_documents（v2.7.0）。

借鉴 SQLite FTS5 全文索引思路，把共享目录里带元数据头（--- JSON ---）的 .md
文档（documents/ 摄入产物、collab/skills、collab/memory 等）建立全文索引，
提供关键词检索 + 命中摘要。与 add_document 闭环：摄入 → 可检索；与
find_skill / search_memory 联动（target_dir 可指向任意共享子目录）。

索引策略：
- SQLite FTS5，优先 trigram tokenizer（中文子串友好，SQLite >= 3.34）；
  不支持时回退 unicode61。
- 索引是派生缓存：存系统临时目录（每进程一份、文件名含 pid），首次检索时
  懒构建，之后按共享根内 *.md 的 mtime/size 签名判断是否需重建；源真相
  始终是文件本身，索引坏了删掉重建即可（与 codegraph 缓存同策略）。
- 检索语义：query 按空白拆词、全部词命中（AND）。trigram 可用且所有词
  >= 3 字符时用 FTS5 MATCH（子串语义与 LIKE 等价）+ bm25 相关度排序；
  含 < 3 字符词或 trigram 不可用时退回逐行 LIKE（平台文档规模下足够快）。

安全约定：
- target_dir 必须解析到共享根内，防目录穿越；
- 全文索引只读文件、不写共享目录（派生缓存放系统临时目录）；
- 身份闸门：REQUIRE_IDENTITY=1 时未绑定身份被拒。
"""

import hashlib
import json
import os
import re
import tempfile
import atexit
import unicodedata
from pathlib import Path

# v2.7.3：与 codegraph.py 同策略——VM 的 Python 3.11 无 sqlite3 标准库，
# 优先 pysqlite3（SQLite 3.5x，含 FTS5 + trigram），未安装时回退标准库。
# （v2.7.0 直接 import sqlite3 导致 VM 上 server 启动即崩，PC-B 连不上 -32000）
try:
    from pysqlite3 import dbapi2 as sqlite3
except ModuleNotFoundError:
    import sqlite3 as sqlite3

from .config import COLLAB_DIR
from .identity import assert_identity_allowed, current_identity
from .logging_setup import logger
from .utils import fail, ok

_META_SEP = "---"
_SNIPPET_RADIUS = 120
_SHARED_ROOT = COLLAB_DIR.parent
_DEFAULT_SCOPE = "documents"
_TRIGRAM_MIN_TERM = 3  # trigram 短语匹配要求词长 >= 3

# 进程内索引状态：连接 + 已索引文件的签名（派生缓存，不落共享目录）
_state: dict = {"conn": None, "sig": None, "scope": None}


def _close_index() -> None:
    """进程退出时关闭索引连接（派生缓存，清理临时文件由 OS 负责）。"""
    conn = _state.get("conn")
    if conn is not None:
        try:
            conn.close()
        except sqlite3.Error:
            pass
        _state["conn"] = None


def reset_state() -> None:
    """Reset in-process index cache (for tests)."""
    _close_index()
    _state.clear()


atexit.register(_close_index)


def _db_path() -> Path:
    """每进程一份索引文件（临时目录），命名含 COLLAB_DIR 哈希 + pid。"""
    key = hashlib.sha1(str(COLLAB_DIR).encode("utf-8")).hexdigest()[:12]
    return Path(tempfile.gettempdir()) / f"claude-collab-search-{key}-{os.getpid()}.db"


def _iter_md(base: Path):
    """遍历共享根内 *.md，容忍扫描期间外部进程删除目录。

    2026-08-07 uv311 全量复现：COLLAB_DIR 位于 %TEMP% 时 _SHARED_ROOT=%TEMP%，
    Qoder SDK 等外部进程会创建/删除 qoder-sdk-auth-* 瞬态目录，rglob 中途
    目录消失抛 FileNotFoundError。索引是派生缓存，容忍不完整扫描：
    下次签名变化会触发重建，比直接崩溃（检索失败）更合理。
    """
    try:
        yield from base.rglob("*.md")
    except OSError:
        # 扫描中断：返回已遍历部分即可（派生缓存可重建）
        return


def _docs_signature(base: Path) -> str:
    """共享根内 *.md 的 (相对路径:mtime_ns:size) 签名，判断索引是否过期。"""
    parts = []
    for p in sorted(_iter_md(base)):
        if not p.is_file():
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        parts.append(f"{p.relative_to(base)}:{st.st_mtime_ns}:{st.st_size}")
    return hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()


def _read_doc_file(file_path: Path) -> dict | None:
    """读取文档，返回 {元数据..., title, body, path}；无法解析/无正文返回 None。

    v3.20.2：documents/ 下无头普通 Markdown 按普通文档索引（format=md，
    method=plain），消除手写文档检索盲区；documents/ 以外无头 .md（vendor
    技能、交接副本等）与 YAML frontmatter/损坏元数据维持跳过，避免噪音入索引。
    """
    try:
        text = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        logger.warning(f"跳过不可读文档 {file_path.name}: {e}")
        return None
    lines = text.splitlines()
    rel = str(file_path.relative_to(_SHARED_ROOT)).replace("\\", "/")
    if lines and lines[0].strip() == _META_SEP:
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
            logger.warning(f"跳过元数据损坏的文档 {file_path.name}: {e}")
            return None
        if not isinstance(meta, dict):
            return None
        body = "\n".join(lines[end + 1 :]).strip()
    else:
        # 无头普通 Markdown：仅限 documents/ 知识库子树
        if not rel.startswith("documents/"):
            return None
        meta = {"format": "md", "method": "plain", "source": file_path.name}
        body = text.strip()
    if not body:
        return None
    # 标题：正文第一个 "# 标题"，否则源文件名
    title = ""
    for line in body.splitlines():
        m = re.match(r"^#\s+(.+)$", line.strip())
        if m:
            title = m.group(1).strip()
            break
    if not title:
        title = str(meta.get("source", "") or file_path.stem)
    return {
        # 统一正斜杠：跨平台（Windows/VM）路径前缀过滤一致
        "path": rel,
        "filename": file_path.name,
        "title": title,
        "source": str(meta.get("source", "")),
        "format": str(meta.get("format", "")),
        "method": str(meta.get("method", "")),
        "chars": meta.get("chars"),
        "estimated_tokens": meta.get("estimated_tokens"),
        "ingested_by": str(meta.get("ingested_by", "")),
        "ingested_at": str(meta.get("ingested_at", "")),
        "body": body,
    }


def _create_table(conn: sqlite3.Connection) -> str:
    """建 FTS5 表；trigram 不可用时回退 unicode61，返回实际 tokenizer。"""
    schema = (
        "CREATE VIRTUAL TABLE docidx USING fts5("
        "title, content, path UNINDEXED, filename UNINDEXED, "
        "source UNINDEXED, format UNINDEXED, ingested_at UNINDEXED, "
        "ingested_by UNINDEXED"
    )
    try:
        conn.execute(schema + ", tokenize = 'trigram')")
        return "trigram"
    except sqlite3.OperationalError:
        conn.execute(schema + ", tokenize = 'unicode61')")
        return "unicode61"


def _rebuild(conn: sqlite3.Connection, base: Path, tokenizer: str) -> None:
    """全量重建索引（扫描共享根内可索引的 .md，见 _read_doc_file）。"""
    conn.execute("DROP TABLE IF EXISTS docidx")
    _create_table(conn)
    count = 0
    for f in _iter_md(base):
        doc = _read_doc_file(f)
        if not doc:
            continue
        conn.execute(
            "INSERT INTO docidx(title, content, path, filename, source, format, "
            "ingested_at, ingested_by) VALUES (?,?,?,?,?,?,?,?)",
            (
                doc["title"],
                doc["body"],
                doc["path"],
                doc["filename"],
                doc["source"],
                doc["format"],
                doc["ingested_at"],
                doc["ingested_by"],
            ),
        )
        count += 1
    conn.commit()
    logger.info(f"🔎 全文索引重建: {count} 个文档 ({tokenizer})")


def _ensure_index(scope_path: Path):
    """返回 (连接, tokenizer)；当前 scope 的索引过期时仅重建该 scope。

    v3.30.1：索引范围从共享根全量改为当前 target_dir。search_documents
    与 rag_query 通常只查 documents（或某个子目录），全量扫描会让小范围
    查询承担全库重建成本，并放大量无关 .md 的“跳过元数据损坏”日志。
    """
    conn = _state.get("conn")
    if conn is None:
        conn = sqlite3.connect(str(_db_path()))
        conn.execute("PRAGMA busy_timeout=5000")
        _state["conn"] = conn
        _state["sig"] = None
        _state["scope"] = None
    scope_key = str(scope_path.resolve())
    sig = _docs_signature(scope_path)
    if scope_key != _state.get("scope") or sig != _state.get("sig"):
        _rebuild(conn, scope_path, "trigram")
        # 探测 tokenizer：重建用的 tokenizer 已在表上固化
        tokenizer = _table_tokenizer(conn)
        _state["sig"] = sig
        _state["scope"] = scope_key
        _state["tokenizer"] = tokenizer
    return conn, _state.get("tokenizer", "trigram")


def _table_tokenizer(conn: sqlite3.Connection) -> str:
    """从表定义里读出实际 tokenizer（trigram / unicode61 / 其它）。"""
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='docidx'"
        ).fetchone()
        sql = (row[0] if row else "") or ""
        return "trigram" if "trigram" in sql else "unicode61"
    except sqlite3.Error:
        return "unicode61"


def _like_escape(term: str) -> str:
    """转义 LIKE 通配符，配合 ESCAPE '\' 使用。"""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _is_pure_punct(term: str) -> bool:
    """词是否纯标点（无字母/数字/中文等有效字符）。"""
    return not any(ch.isalnum() or ch == "_" for ch in term)


def _fts_query(terms: list[str]) -> str:
    """构造 FTS5 MATCH 表达式：各词作为短语，AND 组合（引号内无注入面）。"""
    parts = []
    for t in terms:
        parts.append(f'"{t.replace(chr(34), chr(34) * 2)}"')
    return " AND ".join(parts)


def _snippet(body: str, terms: list[str]) -> str:
    """命中摘要：取最早命中词前后 ±120 字符（压缩换行）。"""
    lower = body.lower()
    first = None
    for t in terms:
        idx = lower.find(t.lower())
        if idx != -1:
            first = idx
            break
    if first is None:
        return body[:_SNIPPET_RADIUS * 2].replace("\n", " ").strip()
    start = max(0, first - _SNIPPET_RADIUS)
    end = min(len(body), first + len(terms[0]) + _SNIPPET_RADIUS)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(body) else ""
    return prefix + body[start:end].replace("\n", " ").strip() + suffix


def _run_query(
    conn: sqlite3.Connection,
    tokenizer: str,
    terms: list[str],
    scope_prefix: str,
) -> list[tuple]:
    """执行检索：FTS5（trigram 全词 >= 3 字符）或 LIKE 兜底。"""
    short = [t for t in terms if len(t) < _TRIGRAM_MIN_TERM]
    use_fts = tokenizer == "trigram" and not short
    like_conds = " AND ".join(
        [f"content LIKE ? ESCAPE '\\'" for _ in terms]
    )
    params: list[str] = [f"%{_like_escape(t)}%" for t in terms]
    # ⚠️ 坑（实测 SQLite 行为）：trigram tokenizer 下 FTS5 的 UNINDEXED 列
    # 上 LIKE/GLOB 一律不命中（`=`/instr 正常），unicode61 无此问题。
    # 因此 scope 前缀过滤统一用 instr(path, ?) = 1（路径以 scope/ 开头）。
    if use_fts:
        sql = (
            "SELECT path, filename, title, source, format, ingested_at, "
            "ingested_by, bm25(docidx) AS b FROM docidx "
            f"WHERE docidx MATCH ? AND instr(path, ?) = 1 AND {like_conds} "
            "ORDER BY b ASC"
        )
        params = [_fts_query(terms), scope_prefix] + params
    else:
        sql = (
            "SELECT path, filename, title, source, format, ingested_at, "
            "ingested_by, 0 AS b FROM docidx "
            f"WHERE instr(path, ?) = 1 AND {like_conds} "
            "ORDER BY ingested_at DESC"
        )
        params = [scope_prefix] + params
    rows = conn.execute(sql, params).fetchall()
    return rows


async def search_documents(query: str, target_dir: str = "", limit: int = 20) -> str:
    """全文检索共享目录中的文档（默认 documents/ 摄入产物，可指定任意共享子目录）。

    基于 SQLite FTS5（trigram，中文子串友好）全文索引；索引为派生缓存，
    随文件变化自动重建。检索语义：query 按空白拆词、全部词命中（AND），
    返回相关度排序（bm25）与命中摘要。与 add_document 闭环：摄入 → 可检索。

    Args:
        query: 检索词（必填；空格分隔多词，全部命中）
        target_dir: 可选，限定搜索范围（相对共享根的子目录），默认 "documents"
        limit: 可选，返回条数上限，默认 20，夹取 [1, 100]
    """
    denied = assert_identity_allowed("search_documents")
    if denied:
        return fail(denied)
    q = (query or "").strip()
    if not q:
        return fail("search_documents 需要非空 query")
    # v2.7.1：查询词同步 NFKC 规范化（与 add_document 摄入侧一致，
    # 用户输入连字字形时也能命中规范化后的文档）
    q = unicodedata.normalize("NFKC", q)
    try:
        limit = max(1, min(int(limit), 100))
    except (TypeError, ValueError):
        limit = 20

    scope = (target_dir or "").strip() or _DEFAULT_SCOPE
    scope_path = (_SHARED_ROOT / scope).resolve()
    if not scope_path.is_relative_to(_SHARED_ROOT.resolve()):
        return fail(f"target_dir 超出共享目录范围: {target_dir}")
    if not scope_path.is_dir():
        return ok({"count": 0, "results": []})
    scope_prefix = scope.rstrip("/\\") + "/"

    terms = [t for t in q.split() if t.strip()]
    # v2.7.4（PC-B L2）：过滤纯标点词——NFKC 后如 "," ":" 等词在 LIKE 兑底路径
    # 会命中大量低价值结果；过滤后若无有效检索词，给出明确错误而非静默空结果
    terms = [t for t in terms if not _is_pure_punct(t)]
    if not terms:
        return fail("query 不含有效检索词（仅标点）")
    conn, tokenizer = _ensure_index(scope_path)
    rows = _run_query(conn, tokenizer, terms, scope_prefix)

    results = []
    for row in rows:
        (
            path, filename, title, source, fmt,
            ingested_at, ingested_by, b,
        ) = row
        body = ""
        doc = _read_doc_file(_SHARED_ROOT / path)
        if doc:
            body = doc["body"]
        results.append({
            "path": path,
            "filename": filename,
            "title": title,
            "source": source,
            "format": fmt,
            "ingested_at": ingested_at,
            "ingested_by": ingested_by,
            "score": round(-b, 2) if b else 0,
            "snippet": _snippet(body, terms),
        })
    results = results[:limit]
    logger.info(f"🔎 文档检索: query={q!r} scope={scope} 命中 {len(results)} 条")
    return ok({
        "count": len(results),
        "query": q,
        "scope": scope,
        "results": results,
    })
