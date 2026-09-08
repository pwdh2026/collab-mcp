"""向量语义检索工具：semantic_search（v3.9.0）。

设计来源：progress/collab-v3.9-semantic-search-design.md
与 FTS5（search_documents 关键词精确检索）互补：语义相关检索。
embedding 用 ollama（OLLAMA_BASE_URL + OLLAMA_EMBED_MODEL，默认 nomic-embed-text，本机已装 768 维），
零新依赖（urllib + sqlite + 纯 python 余弦，无 numpy）。

安全约定：
- 身份闸门复用；日志脱敏（query 截断 120，不记正文/向量）；
- target_dir 必须解析到共享根内，防目录穿越（同 search_documents 语义）；
- ollama 为 operator 配置的信任端点，非用户输入 → 无 SSRF 面。
"""

import asyncio
import atexit
import hashlib
import json
import os
import re
import struct
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from .config import COLLAB_DIR
from .identity import assert_identity_allowed
from .logging_setup import logger
from .search import _DEFAULT_SCOPE, _SHARED_ROOT, _docs_signature, _read_doc_file
from .utils import fail, ok

_OLLAMA_DEFAULT = "http://127.0.0.1:11434"
# v3.21：中文语义检索优先 qwen3-embedding:0.6b（Apache-2.0，1024 维，~600MB，
# 中文 MTEB 第一档；本机已拉取时自动选用）；未拉取回退 nomic-embed-text。
# OLLAMA_EMBED_MODEL 环境变量仍可强制覆盖（权威）。
_EMBED_MODEL_PREFERRED = "qwen3-embedding:0.6b"
_EMBED_MODEL_DEFAULT = "nomic-embed-text"
# v3.21：默认相似度下限，过滤低置信度噪音；显式 min_score=0 恢复全量
_DEFAULT_MIN_SCORE = 0.3
# v3.21：默认 documents scope 排除 media/（转写/杂项噪音），需要时显式 target_dir="documents/media"
_SCOPE_EXCLUDED_PREFIXES = ("documents/media/",)
_CHUNK_CHARS = 800
_BATCH = 16
# v3.21：qwen3-embedding 冷启动（首次加载）比 nomic 慢，60s 兜底防首次查询重建超时
_TIMEOUT = 60
_STATE: dict = {}
# L2：scope 注册表（记录 scope_key → 表名 → 路径），用于清理已删除目录的旧索引表
_META_REGISTRY = "meta_scope_registry"


_sqlite_driver = None  # 惰性：pysqlite3（VM 无标准库 _sqlite3）→ 标准库 → None


def _sqlite_module():
    """惰性加载 sqlite 驱动；均不可用返回 None（server 启动不受影响，调用时明确报错）。

    v3.9 修复（PC-C notice-pc-a-v39-sqlite3-breakage）：顶层 import sqlite3 会让 VM
    （Python 3.11 定制构建缺 _sqlite3 C 扩展）server 启动即崩——与 search.py v2.7.3
    同源教训；保持零硬依赖、探测到才用。
    """
    global _sqlite_driver
    if _sqlite_driver is not None:
        return _sqlite_driver if _sqlite_driver is not False else None
    try:
        from pysqlite3 import dbapi2 as m
    except ModuleNotFoundError:
        try:
            import sqlite3 as m
        except ModuleNotFoundError:
            _sqlite_driver = False
            return None
    _sqlite_driver = m
    return m


def _db_path() -> Path:
    """每进程一份索引文件（临时目录），命名含 COLLAB_DIR 哈希 + pid。"""
    key = hashlib.sha1(str(COLLAB_DIR).encode("utf-8")).hexdigest()[:12]
    return Path(tempfile.gettempdir()) / f"claude-collab-semantic-{key}-{os.getpid()}.db"


def _close_index() -> None:
    conn = _STATE.get("conn")
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
        _STATE["conn"] = None


atexit.register(_close_index)


def _ollama_base() -> str:
    return (os.environ.get("OLLAMA_BASE_URL") or _OLLAMA_DEFAULT).rstrip("/")


def _model_basename(name: str) -> str:
    """模型名去 tag（qwen3-embedding:0.6b → qwen3-embedding）。"""
    return str(name or "").split(":")[0]


def _select_embed_model(models: list[str]) -> str:
    """按优先级选择 embedding 模型：env 强制 > qwen3 首选 > nomic 回退。"""
    override = os.environ.get("OLLAMA_EMBED_MODEL")
    if override:
        return override.strip()
    if any(_model_basename(m) == _model_basename(_EMBED_MODEL_PREFERRED) for m in models):
        return _EMBED_MODEL_PREFERRED
    return _EMBED_MODEL_DEFAULT


def _embed_model() -> str:
    override = os.environ.get("OLLAMA_EMBED_MODEL")
    if override:
        return override.strip()
    return _STATE.get("embed_model") or _EMBED_MODEL_DEFAULT


def _ollama_embed_ready() -> bool:
    """探测 ollama /api/tags 含 embedding 模型（进程内缓存），选定实际模型。"""
    if _STATE.get("embed_ready") is True:
        return True
    try:
        with urllib.request.urlopen(f"{_ollama_base()}/api/tags", timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        models = [str(m.get("name", "")) for m in data.get("models", [])]
        chosen = _select_embed_model(models)
        ready = any(_model_basename(m) == _model_basename(chosen) for m in models)
    except Exception:
        ready = False
    # 低3（PC-C）：仅成功态缓存；失败态不缓存——ollama 晚于 server 启动时下次调用可恢复
    if ready:
        _STATE["embed_ready"] = True
        _STATE["embed_model"] = chosen
    return ready


def _embed(texts: list[str], timeout: int = _TIMEOUT) -> list[list[float]]:
    """批量 embedding（ollama /api/embed）。失败抛 RuntimeError（明确，不静默）。"""
    payload = {"model": _embed_model(), "input": texts}
    req = urllib.request.Request(
        f"{_ollama_base()}/api/embed",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"ollama embedding 失败（HTTP {e.code}）: {e.reason}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise RuntimeError(f"ollama embedding 失败（网络）: {e}") from e
    except (ValueError, TypeError) as e:
        # 低2（PC-C）：200 但非 JSON（如代理异常页）→ 明确错误，不逃出框架内部
        raise RuntimeError(f"ollama embedding 返回非 JSON 响应: {e}") from e
    embs = data.get("embeddings")
    if not isinstance(embs, list) or not embs or not isinstance(embs[0], list):
        raise RuntimeError("ollama embedding 返回空/异常结构")
    return embs


def _chunk_text(text: str, size: int = _CHUNK_CHARS) -> list[str]:
    """按段落/句切块，尽量凑到 size 字符（保持完整，不回写）。"""
    text = text.strip()
    if not text:
        return []
    chunks: list[str] = []
    cur = ""
    for para in re.split(r"\n{1,}", text):
        para = para.strip()
        if not para:
            continue
        if not cur or len(cur) + len(para) + 1 <= size:
            cur = (cur + "\n" + para).strip() if cur else para
            continue
        if cur:
            chunks.append(cur)
        cur = ""
        # 单段落超长：按句切
        for sent in re.split(r"(?<=[。！？；.!?])\s*", para):
            sent = sent.strip()
            if not sent:
                continue
            if cur and len(cur) + len(sent) + 1 > size:
                chunks.append(cur)
                cur = sent
            else:
                cur = (cur + " " + sent).strip() if cur else sent
    if cur:
        chunks.append(cur)
    return chunks


def _pack_emb(emb: list[float]) -> bytes:
    return struct.pack(f"{len(emb)}f", *emb)


def _unpack_emb(blob: bytes) -> list[float]:
    return list(struct.unpack(f"{len(blob) // 4}f", blob))


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _snippet(text: str, limit: int = 160) -> str:
    flat = re.sub(r"\s+", " ", text).strip()
    return flat[:limit] + ("…" if len(flat) > limit else "")


def _table_name(scope_key: str) -> str:
    """每 scope 独立表名：chunks_<sha1(scope_key)[:8]>（内部 hash，无注入面）。"""
    return "chunks_" + hashlib.sha1(scope_key.encode("utf-8")).hexdigest()[:8]


def _scope_signature(scope_path: Path) -> str:
    """scope 索引签名 = 文件签名 + 模型名。

    v3.21：模型名必须入签名——qwen3-embedding(1024 维) 与 nomic(768 维) 维度不同，
    仅按文件 mtime/size 判签名会导致换模型后不重建索引，_cosine 对维度不一致
    静默返回 0（检索全空），比报错更难排查。
    """
    return _docs_signature(scope_path) + "|" + _embed_model()


def _retry_locked(fn, tries: int = 3, delay: float = 0.2):
    """sqlite 锁冲突重试（L3）：OperationalError 且消息含 locked/busy 时退避重试。

    多进程（中枢 + 队友）并发重建索引时 SELECT/写入可能短暂互斥；busy_timeout 之外
    再兜一层重试，避免偶发 "database is locked" 直接失败。
    """
    last = None
    for i in range(max(1, tries)):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            msg = str(e).lower()
            if "locked" not in msg and "busy" not in msg:
                raise
            last = e
            time.sleep(delay * (i + 1))
    raise last


def _ensure_registry(conn) -> None:
    """确保 scope 注册表存在。"""
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {_META_REGISTRY} "
        "(scope_key TEXT PRIMARY KEY, table_name TEXT, path TEXT)"
    )


def _cleanup_orphan_scopes(conn) -> int:
    """L2：清理已不存在目录的旧 scope 索引表（防 DB 膨胀）；返回清理数。"""
    try:
        rows = conn.execute(
            f"SELECT scope_key, table_name, path FROM {_META_REGISTRY}"
        ).fetchall()
    except Exception:  # noqa: BLE001（注册表缺失/损坏不影响主流程）
        return 0
    removed = 0
    for scope_key, table_name, path in rows:
        try:
            alive = Path(path).is_dir()
        except Exception:  # noqa: BLE001
            alive = False
        if alive:
            continue
        try:
            conn.execute(f"DROP TABLE IF EXISTS {table_name}")
            conn.execute(f"DELETE FROM {_META_REGISTRY} WHERE scope_key = ?", (scope_key,))
            removed += 1
        except Exception:  # noqa: BLE001
            pass
    if removed:
        try:
            conn.commit()
        except Exception:  # noqa: BLE001
            pass
    if removed:
        logger.info(f"🧹 semantic: 清理 {removed} 个失效 scope 索引表")
    return removed


def _rebuild_scope(conn, table: str, scope_path: Path) -> int:
    """重建单个 scope 的语义索引表（扫描 scope 内 .md → 分块 → embedding → 入库）。"""
    conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.execute(
        f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, path TEXT, title TEXT, "
        "chunk_idx INTEGER, text TEXT, emb BLOB)"
    )
    conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_path ON {table}(path)")
    docs = []
    for f in sorted(scope_path.rglob("*.md")):
        doc = _read_doc_file(f)
        if doc:
            docs.append(doc)
    chunks: list[tuple[str, str, int, str]] = []
    for doc in docs:
        for i, c in enumerate(_chunk_text(doc["body"])):
            chunks.append((doc["path"], doc["title"], i, c))
    if not chunks:
        conn.commit()
        return 0
    embs: list[list[float]] = []
    for i in range(0, len(chunks), _BATCH):
        embs.extend(_embed([c[3] for c in chunks[i:i + _BATCH]]))
    if len(embs) != len(chunks):
        raise RuntimeError("embedding 数量与块数不一致")
    for (path, title, idx, text), emb in zip(chunks, embs):
        conn.execute(
            f"INSERT INTO {table}(path, title, chunk_idx, text, emb) VALUES (?,?,?,?,?)",
            (path, title, idx, text, _pack_emb(emb)),
        )
    conn.commit()
    logger.info(f"🧠 语义索引重建[{table}]: {len(chunks)} 个块 / {len(docs)} 个文档")
    return len(chunks)


def _ensure_index(scope_path: Path, force: bool = False) -> tuple[object, str, int]:
    """返回 (连接, 表名, 块数)；该 scope 签名过期或 force 时**仅重建该 scope 表**。

    v3.10.0（PC-C 建议 b）：per-scope 索引——_STATE["scopes"] 内存缓存每 scope 的
    签名+块数；scope A→B→A 切换免全量重建（仅 query embedding 开销）。
    """
    m = _sqlite_module()
    if m is None:
        raise RuntimeError("sqlite3 不可用（VM 需安装 pysqlite3，或使用带标准库 sqlite3 的 Python）")
    conn = _STATE.get("conn")
    if conn is None:
        # 索引构建走 to_thread（低1），连接须允许跨线程（sqlite3 模块线程安全）
        conn = m.connect(str(_db_path()), check_same_thread=False)
        conn.execute("PRAGMA busy_timeout=5000")
        _STATE["conn"] = conn
        _STATE["scopes"] = {}
        _ensure_registry(conn)
    scope_key = str(scope_path.resolve())
    table = _table_name(scope_key)
    scopes = _STATE.setdefault("scopes", {})
    info = scopes.get(scope_key) or {}
    sig = _scope_signature(scope_path)
    if force or sig != info.get("sig"):
        count = _rebuild_scope(conn, table, scope_path)
        scopes[scope_key] = {"sig": sig, "chunks": count}
        _retry_locked(lambda: conn.execute(
            f"INSERT OR REPLACE INTO {_META_REGISTRY} (scope_key, table_name, path) VALUES (?, ?, ?)",
            (scope_key, table, str(scope_path)),
        ))
        _retry_locked(lambda: conn.commit())
    else:
        count = info.get("chunks", 0)
    # L2：顺带清理已删除 scope 目录的旧表（每次 ensure 一次，开销可忽略）
    _retry_locked(lambda: _cleanup_orphan_scopes(conn))
    return conn, table, count


async def semantic_search(
    query: str = "",
    target_dir: str = "",
    limit: int = 10,
    min_score: float = _DEFAULT_MIN_SCORE,
    rebuild: bool = False,
) -> str:
    """向量语义检索共享目录中的文档（与 FTS5 search_documents 互补）。

    基于 ollama embedding（OLLAMA_BASE_URL + OLLAMA_EMBED_MODEL，默认
    http://127.0.0.1:11434；模型自动优选 qwen3-embedding:0.6b，未拉取回退
    nomic-embed-text，env 可强制覆盖）构建语义索引（sqlite 派生缓存，
    随文件变化/模型切换自动重建），查询向量与全部块做余弦相似度，返回 top-k
    （≥ min_score）。

    Args:
        query: 检索语义（必填）
        target_dir: 可选，限定范围（相对共享根的子目录），默认 "documents"
        limit: 可选，返回条数上限，默认 10，夹取 [1, 50]
        min_score: 可选，相似度下限（0~1，默认 0.3，过滤低置信度噪音；
            显式传 0 恢复全量）
        rebuild: 可选，为 True 时强制重建语义索引
    """
    denied = assert_identity_allowed("semantic_search")
    if denied:
        return fail(denied)
    q = (query or "").strip()
    if not q:
        return fail("semantic_search 需要非空 query")
    if _sqlite_module() is None:
        return fail("semantic_search 需要 sqlite3（VM 请安装 pysqlite3，或使用带标准库 sqlite3 的 Python）")
    if not _ollama_embed_ready():
        return fail(
            "semantic_search 需要 ollama embedding（OLLAMA_BASE_URL + 模型 "
            f"{_embed_model()}，先执行 ollama pull nomic-embed-text）"
        )
    try:
        limit = max(1, min(int(limit), 50))
    except (TypeError, ValueError):
        limit = 10
    try:
        min_score = max(0.0, min(1.0, float(min_score)))
    except (TypeError, ValueError):
        min_score = 0.0
    scope = (target_dir or "").strip() or _DEFAULT_SCOPE
    scope_path = (_SHARED_ROOT / scope).resolve()
    if not scope_path.is_relative_to(_SHARED_ROOT.resolve()):
        return fail(f"target_dir 超出共享目录范围: {target_dir}")
    if not scope_path.is_dir():
        return ok({
            "method": "semantic", "count": 0, "query": q, "scope": scope,
            "results": [], "indexed_chunks": 0,
        })
    # v3.21：默认 documents scope 排除 media/（转写/杂项噪音入列会污染排序）
    exclude_prefixes = _SCOPE_EXCLUDED_PREFIXES if scope == "documents" else ()
    try:
        # 低1（PC-C）：embedding/索引构建为同步阻塞（urllib+余弦），to_thread 避免卡事件循环
        qv = (await asyncio.to_thread(_embed, [q]))[0]
        conn, table, total_chunks = await asyncio.to_thread(_ensure_index, scope_path, bool(rebuild))
    except RuntimeError as e:
        return fail(str(e))
    rows = _retry_locked(lambda: conn.execute(f"SELECT path, title, text, emb FROM {table}").fetchall())
    scored = []
    for path, title, text, blob in rows:
        if exclude_prefixes and path.startswith(exclude_prefixes):
            continue
        score = _cosine(qv, _unpack_emb(blob))
        if score >= min_score:
            scored.append((score, path, title, text))
    scored.sort(key=lambda x: x[0], reverse=True)
    results = [
        {"path": path, "title": title, "score": round(score, 4), "snippet": _snippet(text)}
        for score, path, title, text in scored[:limit]
    ]
    logger.info(f"🧠 语义检索: query={q[:120]} scope={scope} 命中 {len(results)}/{len(scored)}")
    return ok({
        "method": "semantic",
        "count": len(results),
        "query": q,
        "scope": scope,
        "model": _embed_model(),
        "results": results,
        "indexed_chunks": total_chunks,
    })
