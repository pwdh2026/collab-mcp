"""大文件交付登记工具：register_artifact / verify_artifact。

平台控制面与数据面分离：MCP 消息只传「路径 + sha256 + size」，原始字节留在
共享文件夹 C:\\myshare\\artifacts\\<task_id>\\ 里。register_artifact 把交付登记写进
manifest.json，并同步核对磁盘上的真实文件哈希与大小，禁止脱离实物的空登记；
verify_artifact 向接收方提供 PASS/FAIL 裁决，是「结果不可复现 = 一票否决」的可复现开关。

存储：artifacts/<task_id>/manifest.json，遵循 A2A Artifact 的最小结构化子集。
"""

import hashlib
from pathlib import Path

from .config import COLLAB_DIR, DONE_DIR, INBOX_DIR
from .identity import assert_identity_allowed, current_identity
from .logging_setup import logger
from .utils import fail, now_iso, ok, safe_read_json, safe_write_json

SHARED_ROOT = COLLAB_DIR.parent
ARTIFACTS_DIR = SHARED_ROOT / "artifacts"
_MANIFEST_NAME = "manifest.json"
_MANIFEST_SCHEMA = "collab-artifact-manifest-v1"
_SHA256_LEN = 64


def _normalize_rel_path(rel_path: str) -> str:
    """把用户传入的相对路径规范化为正斜杠、无 . 与空段的形式。

    只做规范化，不负责解析到磁盘；路径穿越在最终 resolve 阶段拦截。
    """
    raw = (rel_path or "").strip().replace("\\", "/")
    if not raw:
        return ""
    if raw.startswith("/") or ":" in raw:
        return ""
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        return ""
    return "/".join(parts)


def _task_dir(task_id: str) -> Path:
    return ARTIFACTS_DIR / task_id


def _resolve_within_artifacts(base: Path, rel_path: str) -> Path:
    """把规范化后的相对路径解析到 base 内；未规范化已被上层提前拒绝。"""
    parts = rel_path.split("/")
    target = base.joinpath(*parts).resolve()
    base_resolved = base.resolve()
    if not target.is_relative_to(base_resolved):
        raise ValueError(f"路径超出 artifacts 目录范围: {rel_path}")
    return target


def _manifest_path_for_task(task_id: str) -> Path:
    return _task_dir(task_id) / _MANIFEST_NAME


def _task_exists(task_id: str) -> bool:
    for d in (INBOX_DIR, DONE_DIR):
        if (d / f"{task_id}.json").exists():
            return True
    return False


def _valid_sha256(value: str) -> str:
    value = (value or "").strip().lower()
    if len(value) != _SHA256_LEN or any(c not in "0123456789abcdef" for c in value):
        return ""
    return value


def _read_manifest(manifest: Path) -> dict | None:
    if not manifest.is_file():
        return None
    data = safe_read_json(manifest)
    if not isinstance(data, dict):
        return None
    if data.get("schema") != _MANIFEST_SCHEMA:
        return None
    if not isinstance(data.get("artifacts"), list):
        return None
    return data


def _hash_file(target: Path) -> str:
    h = hashlib.sha256()
    with open(target, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


async def register_artifact(
    task_id: str,
    rel_path: str,
    sha256: str,
    size: int,
) -> str:
    """登记一个任务的大文件交付物，并把引用写入 artifacts/<task_id>/manifest.json。

    登记不是只写一张纸：本工具会先核对 artifacts/<task_id>/<rel_path> 的真实
    SHA256 与字节数是否和调用方声明一致，任一不符即返回失败，避免把损坏文件
    或不存在的文件登记成可交付结果。

    Args:
        task_id: 任务唯一 ID（12 位十六进制，如 "a1b2c3d4e5f6"）
        rel_path: 相对该任务 artifacts 目录的文件路径（如 "data.csv" 或 "sub/model.bin"）
        sha256: 文件的 64 位十六进制 SHA256（大小写不敏感）
        size: 文件的字节数
    """
    denied = assert_identity_allowed("register_artifact")
    if denied:
        return fail(denied)

    task_id = (task_id or "").strip().lower()
    if len(task_id) != 12 or any(c not in "0123456789abcdef" for c in task_id):
        return fail("task_id 必须是 12 位十六进制字符串")
    rel_path = _normalize_rel_path(rel_path)
    if not rel_path:
        return fail("rel_path 必须是 artifacts/<task_id>/ 下的相对路径，且不能包含 .. 或绝对路径")

    try:
        size = int(size)
    except (TypeError, ValueError):
        return fail("size 必须是整数字节数")
    if size < 0:
        return fail("size 不能为负数")
    expected_sha = _valid_sha256(sha256)
    if not expected_sha:
        return fail("sha256 必须是 64 位十六进制字符串")
    if not _task_exists(task_id):
        return fail(f"任务 {task_id} 不存在，请先 create_task 再登记 artifact")

    task_dir = _task_dir(task_id)
    task_dir.mkdir(parents=True, exist_ok=True)
    try:
        target = _resolve_within_artifacts(task_dir, rel_path)
    except ValueError as e:
        return fail(str(e))

    if not target.exists() or not target.is_file():
        return fail(f"artifact 文件不存在或不是文件: {target}")
    actual_size = target.stat().st_size
    actual_sha = _hash_file(target)
    if size != actual_size:
        return fail(
            f"size 与实际文件不符：声明 {size} 字节，磁盘上是 {actual_size} 字节"
        )
    if expected_sha != actual_sha:
        return fail(
            f"sha256 与实际文件不符：声明 {expected_sha}，磁盘计算为 {actual_sha}"
        )

    manifest_path = _manifest_path_for_task(task_id)
    manifest = _read_manifest(manifest_path) or {
        "schema": _MANIFEST_SCHEMA,
        "task_id": task_id,
        "artifacts": [],
    }
    entry = {
        "rel_path": rel_path,
        "sha256": actual_sha,
        "size": actual_size,
        "registered_by": current_identity() or "local",
        "registered_at": now_iso(),
    }
    artifacts = [
        a for a in manifest.get("artifacts", [])
        if isinstance(a, dict) and a.get("rel_path") != rel_path
    ]
    artifacts.append(entry)
    manifest["task_id"] = task_id
    manifest["artifacts"] = artifacts
    manifest["updated_at"] = now_iso()

    if not safe_write_json(manifest_path, manifest):
        return fail(f"无法写入 artifact manifest: {manifest_path}")

    logger.info(f"📦 artifact 登记 [{task_id}] {rel_path} ({actual_size} 字节)")
    return ok({
        "message": "artifact 已登记",
        "task_id": task_id,
        "rel_path": rel_path,
        "sha256": actual_sha,
        "size": actual_size,
        "manifest_path": str(manifest_path),
    })


async def verify_artifact(manifest_path: str, rel_path: str) -> str:
    """按 manifest 重算一个 artifact 的 SHA256/大小，返回 PASS 或 FAIL。

    接收方在取到字节后调用此工具做完整性裁决。真实文件缺失、大小不一致、哈希
    不一致都会返回 status=FAIL；manifest 本身缺失或损坏则作为工具错误返回。

    Args:
        manifest_path: artifacts 目录下的 manifest 路径，可相对共享根，也可绝对路径；
                       如 "artifacts/a1b2c3d4e5f6/manifest.json"
        rel_path: manifest 中登记的 artifact 相对路径
    """
    denied = assert_identity_allowed("verify_artifact")
    if denied:
        return fail(denied)

    rel_path = _normalize_rel_path(rel_path)
    if not rel_path:
        return fail("rel_path 不能为空，且不能包含 .. 或绝对路径")

    try:
        manifest_path_obj = Path(manifest_path or "").expanduser()
        if not manifest_path_obj.is_absolute():
            manifest_path_obj = SHARED_ROOT / manifest_path_obj
        manifest_path_obj = manifest_path_obj.resolve()
        root = ARTIFACTS_DIR.resolve()
        if not manifest_path_obj.is_relative_to(root):
            return fail(f"manifest 路径超出 artifacts 目录范围: {manifest_path}")
    except (OSError, ValueError) as e:
        return fail(f"manifest 路径解析失败: {e}")

    if not manifest_path_obj.is_file():
        return fail(f"manifest 不存在: {manifest_path_obj}")
    manifest = _read_manifest(manifest_path_obj)
    if manifest is None:
        return fail(f"manifest 损坏或缺少 {_MANIFEST_SCHEMA} 结构: {manifest_path_obj}")

    match = next(
        (a for a in manifest.get("artifacts", [])
         if isinstance(a, dict) and a.get("rel_path") == rel_path),
        None,
    )
    if match is None:
        return fail(f"manifest 中没有登记的 artifact: {rel_path}")

    expected_sha = str(match.get("sha256", ""))
    try:
        expected_size = int(match.get("size"))
    except (TypeError, ValueError):
        return fail("manifest 中的 size 字段非法")

    try:
        target = _resolve_within_artifacts(manifest_path_obj.parent, rel_path)
    except ValueError as e:
        return fail(str(e))

    if not target.exists() or not target.is_file():
        return ok({
            "status": "FAIL",
            "verified": False,
            "message": "artifact 文件缺失",
            "rel_path": rel_path,
            "expected_sha256": expected_sha,
            "expected_size": expected_size,
        })

    actual_size = target.stat().st_size
    actual_sha = _hash_file(target)
    passed = actual_sha == expected_sha and actual_size == expected_size
    reason = ""
    if actual_size != expected_size:
        reason = (
            f"size 不一致：expect {expected_size}，actual {actual_size}；"
        )
    if actual_sha != expected_sha:
        reason += (
            f"sha256 不一致：expect {expected_sha}，actual {actual_sha}"
        )
    logger.info(
        f"🔎 artifact 校验 [{rel_path}] -> {'PASS' if passed else 'FAIL'}"
    )
    return ok({
        "status": "PASS" if passed else "FAIL",
        "verified": passed,
        "message": "artifact 校验完成" if passed else (reason or "artifact 校验失败"),
        "rel_path": rel_path,
        "expected_sha256": expected_sha,
        "actual_sha256": actual_sha,
        "expected_size": expected_size,
        "actual_size": actual_size,
    })
