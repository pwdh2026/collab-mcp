"""配置 — 协作目录、日志路径。"""

import os
from pathlib import Path


def _default_collab_dir() -> Path:
    """确定协作目录：

    1. 优先使用环境变量 COLLAB_DIR（可覆盖）
    2. VM 部署默认 /mnt/hgfs/myshare/collab（存在时）
    3. 否则回退到本仓库内的 collab/ 目录（Windows 开发/测试）
    """
    env = os.environ.get("COLLAB_DIR")
    if env:
        return Path(env)
    vm_default = Path("/mnt/hgfs/myshare/collab")
    if vm_default.exists():
        return vm_default
    # collab/collab_mcp/config.py → 上一级即 collab/
    return Path(__file__).resolve().parent.parent


COLLAB_DIR = _default_collab_dir()
INBOX_DIR = COLLAB_DIR / "inbox"
DONE_DIR = COLLAB_DIR / "done"
CHAT_DIR = COLLAB_DIR / "chat"
MEMORY_DIR = COLLAB_DIR / "memory"
TEMPLATES_DIR = COLLAB_DIR / "templates"
SKILLS_DIR = COLLAB_DIR / "skills"
DOCS_DIR = COLLAB_DIR.parent / "documents"
SCHEDULES_DIR = COLLAB_DIR / "schedules"
LOG_FILE = COLLAB_DIR.parent / "server.log"

# 共享根：collab/ 的父目录（所有共享文件的基准路径）
SHARED_ROOT = COLLAB_DIR.parent


def assert_path_within_shared(target: str | Path) -> str | None:
    """校验目标路径是否在共享目录范围内。

    防止路径穿越攻击：返回 None 表示安全，返回字符串表示拒绝原因。
    """
    try:
        resolved = (SHARED_ROOT / target).resolve()
        if not resolved.is_relative_to(SHARED_ROOT.resolve()):
            return f"路径穿越拒绝: {target} 超出共享目录范围"
    except (OSError, ValueError) as e:
        return f"路径解析失败: {e}"
    return None


def ensure_directories() -> None:
    """确保协作目录结构存在。在 server 启动时调用。"""
    for d in (
        INBOX_DIR,
        DONE_DIR,
        CHAT_DIR,
        MEMORY_DIR,
        TEMPLATES_DIR,
        SKILLS_DIR,
        DOCS_DIR,
        SCHEDULES_DIR,
    ):
        d.mkdir(parents=True, exist_ok=True)
