"""系统健康检查工具：health_check。"""

import platform
import shutil
import subprocess

from .config import CHAT_DIR, COLLAB_DIR, DONE_DIR, INBOX_DIR, LOG_FILE
from .identity import current_identity, is_hub
from .logging_setup import logger
from .utils import ok


def collect_health_checks() -> tuple[dict, bool]:
    """收集全部健康检查项（同步，供 MCP 工具与 CLI --health 共用）。"""
    checks = {}
    all_ok = True

    # 1. 目录可读写
    dir_checks = {}
    for name, d in [("inbox", INBOX_DIR), ("done", DONE_DIR), ("chat", CHAT_DIR)]:
        test_file = d / ".health_check_test"
        try:
            d.mkdir(parents=True, exist_ok=True)
            test_file.write_text("health", encoding="utf-8")
            test_file.unlink()
            dir_checks[name] = "✅ 可读写"
        except Exception as e:
            dir_checks[name] = f"❌ {e}"
            all_ok = False
    checks["directories"] = dir_checks

    # 2. 磁盘空间
    try:
        usage = shutil.disk_usage(COLLAB_DIR)
        free_gb = usage.free / (1024 ** 3)
        checks["disk_free"] = f"{free_gb:.1f} GB"
        if free_gb < 1:
            checks["disk_free"] += " ⚠️ 空间不足"
            all_ok = False
        else:
            checks["disk_free"] += " ✅"
    except Exception as e:
        checks["disk_free"] = f"❌ {e}"
        all_ok = False

    # 3. 系统信息（Windows 上 uptime/free 不存在 → N/A）
    try:
        result = subprocess.run(
            ["uptime"], capture_output=True, text=True, timeout=5
        )
        checks["uptime"] = result.stdout.strip()
    except Exception:
        checks["uptime"] = "N/A"

    try:
        result = subprocess.run(
            ["free", "-h"], capture_output=True, text=True, timeout=5
        )
        mem_line = result.stdout.splitlines()[1] if len(result.stdout.splitlines()) > 1 else ""
        checks["memory"] = mem_line.strip()
    except Exception:
        checks["memory"] = "N/A"

    # 4. Python 版本
    checks["python"] = f"{platform.python_version()} ✅"

    # 5. MCP SDK 版本
    try:
        import mcp
        checks["mcp_sdk"] = f"{getattr(mcp, '__version__', 'installed')} ✅"
    except ImportError:
        checks["mcp_sdk"] = "❌ 未安装"

    # 6. sqlite3 可用性
    try:
        try:
            from pysqlite3 import dbapi2 as sqlite3
            driver = "pysqlite3"
        except ModuleNotFoundError:
            import sqlite3
            driver = "sqlite3"
        checks["sqlite3"] = f"{driver} {sqlite3.sqlite_version} ✅"
    except ModuleNotFoundError:
        checks["sqlite3"] = "❌ 不可用"
        all_ok = False

    # 7. 任务/消息统计
    try:
        checks["inbox_count"] = f"{len(list(INBOX_DIR.glob('*.json')))} 个待办"
        checks["done_count"] = f"{len(list(DONE_DIR.glob('*.json')))} 个完成"
        checks["chat_count"] = f"{len(list(CHAT_DIR.glob('*.json')))} 条消息"
    except Exception as e:
        checks["counts"] = f"❌ {e}"

    # 8. 日志文件大小
    if LOG_FILE.exists():
        size_kb = LOG_FILE.stat().st_size / 1024
        checks["log_size"] = f"{size_kb:.0f} KB"

    # 9. 协议版本
    try:
        vf = COLLAB_DIR / "version"
        if vf.exists():
            checks["protocol_version"] = vf.read_text(encoding="utf-8").strip()
        else:
            checks["protocol_version"] = "N/A (legacy)"
    except Exception as e:
        checks["protocol_version"] = f"read error: {e}"

    # 10. 活跃锁数量
    try:
        locks_dir = COLLAB_DIR / "locks"
        if locks_dir.is_dir():
            lock_count = len(list(locks_dir.glob("*.json")))
            checks["active_locks"] = f"{lock_count} 个"
        else:
            checks["active_locks"] = "0 个"
    except Exception as e:
        checks["active_locks"] = f"error: {e}"

    # 11. 通知流
    try:
        feed = COLLAB_DIR / "notifications" / "feed.jsonl"
        if feed.exists():
            size_kb = feed.stat().st_size / 1024
            checks["notification_feed"] = f"{size_kb:.0f} KB"
        else:
            checks["notification_feed"] = "N/A (no feed)"
    except Exception as e:
        checks["notification_feed"] = f"error: {e}"

    # 12. 身份信息
    try:
        ident = current_identity() or "local"
        role = "hub" if is_hub() else "teammate"
        checks["identity"] = f"{ident} ({role})"
    except Exception as e:
        checks["identity"] = f"error: {e}"

    return checks, all_ok


async def health_check() -> str:
    """对协作平台进行全面的健康检查。

    检查项：
    - 协作目录可读写性
    - 磁盘空间
    - 内存使用
    - 系统 uptime
    - Python/依赖版本
    - 日志文件大小

    新队友接入后可用此工具自检环境是否正常。
    """
    checks, all_ok = collect_health_checks()

    logger.info(
        f"🏥 健康检查: {'全部通过 ✅' if all_ok else '有问题 ⚠️'} "
        f"(pending:{checks.get('inbox_count','?')}, "
        f"done:{checks.get('done_count','?')}, "
        f"chat:{checks.get('chat_count','?')})"
    )

    return ok({
        "healthy": all_ok,
        "checks": checks,
        "collab_dir": str(COLLAB_DIR),
        "log_file": str(LOG_FILE),
    })
