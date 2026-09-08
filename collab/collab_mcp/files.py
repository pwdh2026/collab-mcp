"""共享文件夹访问工具：list_shared_dir / read_shared_file。

队友机的 Claude 运行在各自的 Windows 上，无法直接访问 /mnt/hgfs/myshare
（那是中枢 VM 上的挂载）。这两个工具让任何队友通过 MCP 协议读取共享文件夹，
例如查看目录结构、读取工作指南，无需手动拷贝。
"""

from pathlib import Path

from .config import COLLAB_DIR
from .logging_setup import logger
from .utils import fail, ok

# 共享文件夹根 = 协作目录的上一级（VM: /mnt/hgfs/myshare，Windows: C:\myshare）
SHARED_ROOT = COLLAB_DIR.parent

MAX_READ_BYTES = 256 * 1024  # 单文件最多读 256KB
MAX_LIST_ENTRIES = 200       # 单目录最多列 200 项


def _resolve_within_shared(relative_path: str) -> Path:
    """把相对路径解析到共享文件夹内，并拦截目录穿越。"""
    rel = Path(relative_path)
    if rel.is_absolute():
        raise ValueError(
            f"请使用相对路径（相对 {SHARED_ROOT}），不要传绝对路径: {relative_path}"
        )
    target = (SHARED_ROOT / rel).resolve()
    root = SHARED_ROOT.resolve()
    if not target.is_relative_to(root):
        raise ValueError(f"路径超出共享文件夹范围: {relative_path}")
    return target


async def list_shared_dir(relative_path: str = "") -> str:
    """列出共享文件夹（/mnt/hgfs/myshare）中某个目录的内容。

    队友机无法直接访问中枢 VM 的挂载目录，用此工具可通过 MCP 查看共享文件夹结构，
    例如查看工作指南文件（teammate-CLAUDE.md）是否存在。

    Args:
        relative_path: 相对共享文件夹根目录的路径，留空表示根目录（如 "" 或 "progress"）
    """
    try:
        target = _resolve_within_shared(relative_path)
    except ValueError as e:
        return fail(str(e))

    if not target.exists():
        return fail(f"目录不存在: {target}")
    if not target.is_dir():
        return fail(f"不是目录: {target}")

    try:
        children = sorted(
        (p for p in target.iterdir() if not p.name.startswith(".")),
        key=lambda p: p.name.lower(),
    )
    except OSError as e:
        return fail(f"读取目录失败 {target}: {e}")

    entries = []
    for child in children[:MAX_LIST_ENTRIES]:
        try:
            is_dir = child.is_dir()
            size = 0 if is_dir else child.stat().st_size
        except OSError:
            is_dir, size = False, 0
        entries.append({
            "name": child.name,
            "type": "dir" if is_dir else "file",
            "size": size,
        })

    truncated = len(children) > MAX_LIST_ENTRIES
    logger.info(f"📂 列出共享目录: {target} ({len(entries)} 项)")
    return ok({
        "path": str(target),
        "count": len(entries),
        "entries": entries,
        "truncated": truncated,
    })


async def read_shared_file(relative_path: str) -> str:
    """读取共享文件夹中的文本文件内容（限制 256KB）。

    用于让队友机通过 MCP 直接阅读共享文件夹里的指南/文档，
    例如 read_shared_file("teammate-CLAUDE.md")。

    Args:
        relative_path: 相对共享文件夹根目录的文件路径（如 "teammate-CLAUDE.md"）
    """
    try:
        target = _resolve_within_shared(relative_path)
    except ValueError as e:
        return fail(str(e))

    if not target.exists():
        return fail(f"文件不存在: {target}")
    if not target.is_file():
        return fail(f"不是文件: {target}")

    size = target.stat().st_size
    if size > MAX_READ_BYTES:
        return fail(f"文件过大（{size} 字节 > {MAX_READ_BYTES}），请让中枢处理")

    try:
        content = target.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return fail(f"读取文件失败 {target}: {e}")

    logger.info(f"📄 读取共享文件: {target} ({size} 字节)")
    return ok({
        "path": str(target),
        "size": size,
        "content": content,
    })
