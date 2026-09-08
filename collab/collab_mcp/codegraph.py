"""CodeGraph SQLite 查询引擎 — 原生查询 .codegraph/codegraph.db。

无需 codegraph CLI 二进制，纯 Python sqlite3 实现。
优先使用 pysqlite3（CentOS 7 上 sqlite3 3.5x 支持 FTS5），
未安装时回退到标准库 sqlite3。
"""

import hashlib
import os
import shutil
import tempfile
import time
import uuid
from pathlib import Path

try:
    from pysqlite3 import dbapi2 as sqlite3
except ModuleNotFoundError:
    import sqlite3 as sqlite3

from .config import COLLAB_DIR
from .logging_setup import logger
from .utils import fail, ok


def _copy_db_atomic(src: Path, dest: Path, cache_dir: Path, project_hash: str) -> Path:
    """先把数据库复制到唯一临时文件，再 os.replace 到目标。

    源库位于 hgfs（只读复制，避免在共享盘上并发写）；替换发生在本地临时目录，
    os.replace 在同一文件系统内是原子的。若目标文件正被本进程其他连接占用
    （Windows 上 os.replace 会失败），则直接改用临时文件继续。
    """
    tmp = cache_dir / f".cg_{project_hash}_{os.getpid()}_{uuid.uuid4().hex[:6]}.tmp"
    try:
        shutil.copy2(str(src), str(tmp))
    except OSError:
        # 复制失败时清理临时文件，避免残留
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    try:
        os.replace(str(tmp), str(dest))
        return dest
    except OSError:
        return tmp


def _connect_with_retry(cached: Path, src: Path, cache_dir: Path,
                        project_hash: str) -> sqlite3.Connection:
    """打开缓存库：busy_timeout + 失败重建重试。

    - 'database is locked'：并发写锁，自动退避重试
    - 'file is not a database'：缓存副本损坏（复制残留），删除后重新复制再试
    """
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            conn = sqlite3.connect(str(cached), timeout=5.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=5000")
            # 快速校验可读性，坏副本在查询前暴露
            conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
            return conn
        except sqlite3.DatabaseError as e:
            last_error = e
            logger.warning(f"CodeGraph 缓存打开失败（第 {attempt} 次）: {e}")
            try:
                conn.close()
            except Exception:
                pass
            if attempt < 3:
                time.sleep(0.2 * attempt)
                try:
                    cached.unlink(missing_ok=True)
                except OSError:
                    pass
                cached = _copy_db_atomic(src, cached, cache_dir, project_hash)
                continue
    if last_error is None or not isinstance(last_error, BaseException):
        raise RuntimeError("CodeGraph 缓存打开失败")
    raise last_error


def open_codegraph_db(project_path: Path) -> sqlite3.Connection:
    """打开项目的 CodeGraph SQLite 数据库。

    为规避 VMware 共享文件夹 (hgfs) 的文件锁限制，将数据库复制到
    系统临时目录后打开。仅在数据库文件更新时才重新复制（通过 mtime 判断）。
    每个 server 进程使用独立的缓存文件（文件名含 pid），多个 SSH 会话
    （=多个 server 进程）并发复制/打开互不干扰。
    """
    src = project_path / ".codegraph" / "codegraph.db"
    if not src.exists():
        raise FileNotFoundError(
            f"项目 {project_path} 尚未初始化 CodeGraph。请执行: cd {project_path} && codegraph init"
        )

    # 缓存到系统临时目录（Windows/VM 通用），因为 hgfs 不支持 SQLite 文件锁
    cache_dir = Path(tempfile.gettempdir()) / "codegraph_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    # 项目路径 sha1 + 本进程 pid：多项目不冲突，跨进程各持副本不竞争
    project_hash = hashlib.sha1(
        str(project_path.resolve()).encode("utf-8")
    ).hexdigest()[:12]
    cached = cache_dir / f"cg_{project_hash}_{os.getpid()}.db"

    src_mtime = src.stat().st_mtime
    cache_mtime = cached.stat().st_mtime if cached.exists() else 0

    # 仅在源文件更新或缓存缺失时重新复制
    if not cached.exists() or src_mtime > cache_mtime:
        cached = _copy_db_atomic(src, cached, cache_dir, project_hash)
        logger.info(f"📋 CodeGraph 数据库已缓存到 {cached}")

    return _connect_with_retry(cached, src, cache_dir, project_hash)


def search_nodes(db: sqlite3.Connection, query: str, limit: int = 20) -> list[dict]:
    """通过 FTS5 全文搜索查找节点。支持多 token（空格分隔），逐个搜索后去重合并。"""
    tokens = [t.strip() for t in query.split() if t.strip()]
    all_nodes = []
    seen_ids = set()

    for token in tokens:
        # 转义 FTS5 特殊字符
        safe_token = token.replace('"', '""')

        # 尝试 FTS 短语搜索
        try:
            rows = db.execute(
                """SELECT n.id, n.kind, n.name, n.qualified_name, n.file_path,
                          n.start_line, n.end_line, n.signature, n.docstring,
                          n.visibility, rank
                   FROM nodes_fts fts
                   JOIN nodes n ON n.id = fts.id
                   WHERE nodes_fts MATCH ?
                   ORDER BY rank
                   LIMIT ?""",
                (f'"{safe_token}"', limit),
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []

        # FTS 无结果时回退到 LIKE
        if not rows:
            like_pattern = f"%{token}%"
            rows = db.execute(
                """SELECT id, kind, name, qualified_name, file_path,
                          start_line, end_line, signature, docstring, visibility
                   FROM nodes
                   WHERE name LIKE ? OR qualified_name LIKE ?
                   ORDER BY name
                   LIMIT ?""",
                (like_pattern, like_pattern, limit),
            ).fetchall()

        for row in rows:
            d = dict(row)
            if d["id"] not in seen_ids:
                seen_ids.add(d["id"])
                all_nodes.append(d)

    return all_nodes[:limit]


def get_node_callers(db: sqlite3.Connection, node_id: str) -> list[dict]:
    """获取调用此节点的所有节点（入边）。"""
    rows = db.execute(
        """SELECT n.id, n.kind, n.name, n.file_path, n.start_line,
                  e.kind as edge_kind, e.line as call_line
           FROM edges e
           JOIN nodes n ON n.id = e.source
           WHERE e.target = ?
           ORDER BY n.name""",
        (node_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_node_callees(db: sqlite3.Connection, node_id: str) -> list[dict]:
    """获取此节点调用的所有其他节点（出边）。"""
    rows = db.execute(
        """SELECT n.id, n.kind, n.name, n.file_path, n.start_line,
                  e.kind as edge_kind, e.line as call_line
           FROM edges e
           JOIN nodes n ON n.id = e.target
           WHERE e.source = ?
           ORDER BY n.name""",
        (node_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_file_symbols(db: sqlite3.Connection, file_path: str) -> list[dict]:
    """获取指定文件中的所有符号。"""
    rows = db.execute(
        """SELECT id, kind, name, qualified_name, start_line, end_line,
                  signature, visibility
           FROM nodes
           WHERE file_path LIKE ?
           ORDER BY start_line""",
        (f"%{file_path}%",),
    ).fetchall()
    return [dict(r) for r in rows]


def read_source_snippet(file_path_relative: str, start: int, end: int,
                        project_path: Path) -> str:
    """读取项目中某个文件的指定行范围（绝对路径由 project_path + 相对路径拼接）。"""
    full_path = project_path / file_path_relative
    if not full_path.exists():
        return f"# [文件不存在: {full_path}]"
    try:
        lines = full_path.read_text(encoding="utf-8", errors="replace").splitlines()
        # start/end 是 1-indexed 行号
        s = max(0, start - 1)
        e = min(len(lines), end)
        snippet = lines[s:e]
        return "\n".join(f"{s + 1 + i:4}|{line}" for i, line in enumerate(snippet))
    except Exception as e:
        return f"# [读取失败: {e}]"


def format_codegraph_result(db: sqlite3.Connection, nodes: list[dict],
                            project_path: Path) -> str:
    """将查询到的节点格式化为人可读的代码图谱报告。"""
    if not nodes:
        return "（未找到匹配的符号）"

    lines = []
    shown_files = set()

    for node in nodes:
        node_id = node["id"]
        name = node["name"]
        kind = node["kind"]
        file_path = node.get("file_path", "?")
        start_line = node.get("start_line", 0)
        end_line = node.get("end_line", start_line)
        signature = node.get("signature") or ""
        docstring = node.get("docstring") or ""

        # 头部
        lines.append(f"\n{'─' * 60}")
        lines.append(f"🔧 {name}  ({kind})")
        lines.append(f"   📁 {file_path}:{start_line}-{end_line}")
        if signature:
            lines.append(f"   ✏️  {signature}")
        if docstring:
            # 只取第一行
            first_line = docstring.strip().split("\n")[0][:120]
            lines.append(f"   📝 {first_line}")

        # 谁调用了它（入边）— 只显示前 5 个
        callers = get_node_callers(db, node_id)
        if callers:
            caller_names = [f"{c['name']}({c.get('file_path','?')}:{c.get('start_line','?')})"
                           for c in callers[:5]]
            more = f" +{len(callers) - 5} 个" if len(callers) > 5 else ""
            lines.append(f"   ⬆ 被调用: {', '.join(caller_names)}{more}")

        # 它调用了谁（出边）— 只显示前 5 个
        callees = get_node_callees(db, node_id)
        if callees:
            callee_names = [f"{c['name']}({c.get('file_path','?')}:{c.get('start_line','?')})"
                           for c in callees[:5]]
            more = f" +{len(callees) - 5} 个" if len(callees) > 5 else ""
            lines.append(f"   ⬇ 调用了: {', '.join(callee_names)}{more}")

        # 源代码片段（只对函数/类/方法展开）
        if kind in ("function", "method", "class") and start_line > 0:
            if file_path not in shown_files:
                shown_files.add(file_path)
            snippet = read_source_snippet(file_path, start_line, end_line, project_path)
            lines.append(f"   📄 源码:")
            for snippet_line in snippet.splitlines():
                lines.append(f"   {snippet_line}")

    # blast radius 摘要
    all_caller_names = set()
    for node in nodes:
        for c in get_node_callers(db, node["id"]):
            all_caller_names.add(c["name"])
    if all_caller_names:
        lines.insert(0, f"⚠️  Blast Radius — 依赖这些符号的有: {', '.join(sorted(all_caller_names))}")

    lines.insert(0, f"📊 找到 {len(nodes)} 个符号")
    return "\n".join(lines)


async def query_codegraph(query: str, project_path: str = "") -> str:
    """查询 CodeGraph 代码知识图谱，获取代码结构、调用关系等信息。

    直接读取 .codegraph/codegraph.db SQLite 数据库，无需 codegraph CLI。
    支持 FTS5 全文搜索和模糊匹配，返回符号定义、调用链、源码片段。

    使用场景：
    - "complete_task 函数被谁调用？"
    - "server.py 里有哪些 MCP 工具？"
    - "safe_read_json 和 safe_write_json 的调用链是什么？"

    Args:
        query: 查询内容，可以是符号名、文件名、多个符号（空格分隔）
        project_path: 项目根目录路径（可选）。不填则默认查询协作目录。
    """
    target = Path(project_path) if project_path else COLLAB_DIR
    if not target.exists():
        return fail(f"项目路径不存在: {target}")

    logger.info(f"🔍 CodeGraph 查询: {query[:60]}... (项目: {target})")

    try:
        db = open_codegraph_db(target)
    except FileNotFoundError as e:
        return fail(str(e))

    try:
        all_nodes = search_nodes(db, query, limit=10)
        report = format_codegraph_result(db, all_nodes, target)

        logger.info(f"✅ CodeGraph 查询完成: {len(all_nodes)} 个符号")
        return ok({
            "query": query,
            "project": str(target),
            "symbol_count": len(all_nodes),
            "result": report,
        })
    finally:
        db.close()
