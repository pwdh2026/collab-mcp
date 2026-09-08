"""项目级中央锁（压力测试清单第 6 项）— 共享代码修改前先取锁，防 Git 冲突。

锁文件存放在 collab/locks/ 下（跨机器可见），带 TTL 自动过期，
避免队友离机后锁永久卡死。仅锁属主本人或中枢可释放。
"""

import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import COLLAB_DIR
from .identity import assert_identity_allowed, current_identity, identity_note, is_hub
from .logging_setup import logger
from .utils import fail, now_iso, ok, safe_read_json, safe_write_json

LOCK_TTL_HOURS = 2  # 锁默认 2 小时自动过期
LOCKS_DIR = COLLAB_DIR / "locks"


def _parse_iso(value: str):
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _lock_path(project_path: str) -> Path:
    resolved = str(Path(project_path).resolve())
    digest = hashlib.sha1(resolved.encode("utf-8")).hexdigest()[:12]
    return LOCKS_DIR / f"{digest}.json"


def _read_valid_lock(lock_file: Path) -> dict:
    """读取锁；过期视为无锁。"""
    data = safe_read_json(lock_file) or {}
    expires = _parse_iso(data.get("expires_at", ""))
    if expires is not None and expires <= datetime.now(timezone.utc):
        return {}
    return data


async def acquire_project_lock(project_path: str, reason: str = "") -> str:
    """获取项目级中央锁（修改共享代码前调用）。

    同一项目同一时刻只能被一个队友持有；锁 2 小时自动过期。
    重复获取（自己持有中）幂等成功。

    Args:
        project_path: 项目根目录路径（如 /mnt/hgfs/myshare/my-app）
        reason: 可选，加锁原因（如"重构 main.py"）
    """
    denied = assert_identity_allowed("acquire_project_lock")
    if denied:
        return fail(denied)
    ident = current_identity() or "local"

    lock_file = _lock_path(project_path)
    existing = _read_valid_lock(lock_file)
    if existing and existing.get("owner") != ident:
        return fail(
            f"项目已被 {existing.get('owner')} 锁定（原因: {existing.get('reason', '-')}，"
            f"至 {existing.get('expires_at')}）。请等待释放或请中枢协调。"
        )

    LOCKS_DIR.mkdir(parents=True, exist_ok=True)
    lock = {
        "project_path": str(Path(project_path).resolve()),
        "owner": ident,
        "acquired_at": now_iso(),
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=LOCK_TTL_HOURS)).isoformat(),
        "reason": reason,
    }
    if not safe_write_json(lock_file, lock):
        return fail(f"无法写入锁文件: {lock_file}")

    logger.info(f"🔒 项目锁获取 [{lock['project_path']}] owner={ident} reason={reason}")
    return ok({
        "message": "锁已获取",
        "project_path": lock["project_path"],
        "owner": ident,
        "expires_at": lock["expires_at"],
        **identity_note(),
    })


async def release_project_lock(project_path: str) -> str:
    """释放项目级中央锁（仅锁属主本人或中枢可释放）。"""
    denied = assert_identity_allowed("release_project_lock")
    if denied:
        return fail(denied)
    ident = current_identity() or "local"

    lock_file = _lock_path(project_path)
    existing = _read_valid_lock(lock_file)
    if not existing:
        return ok({"message": "该路径当前没有有效锁（幂等）"})
    if existing.get("owner") != ident and not is_hub():
        return fail(f"锁属于 {existing.get('owner')}，只有其本人或中枢可释放")

    try:
        lock_file.unlink(missing_ok=True)
    except OSError as e:
        return fail(f"释放锁失败: {e}")
    logger.info(f"🔓 项目锁释放 [{existing.get('project_path')}] by {ident}")
    return ok({
        "message": "锁已释放",
        "project_path": existing.get("project_path"),
        "released_by": ident,
        **identity_note(),
    })


async def list_project_locks() -> str:
    """列出当前所有有效的项目锁（含属主与过期时间）。"""
    locks = []
    if LOCKS_DIR.is_dir():
        for f in sorted(LOCKS_DIR.glob("*.json")):
            lock = _read_valid_lock(f)
            if lock:
                locks.append({
                    "project_path": lock.get("project_path"),
                    "owner": lock.get("owner"),
                    "acquired_at": lock.get("acquired_at"),
                    "expires_at": lock.get("expires_at"),
                    "reason": lock.get("reason", ""),
                })
    return ok({"count": len(locks), "locks": locks})
