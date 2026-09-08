"""通用辅助函数：时间戳、JSON 安全读写（原子写入）、响应包装、URL 归一化。"""

import json
import os
import urllib.parse
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_TRACKING_PARAMS = {
    "fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid",
    "igshid", "yclid", "_hsenc", "_hsmi", "vero_id", "oly_enc_id",
    "oly_anon_id",
}


from .logging_setup import logger


def now_iso() -> str:
    """返回当前 UTC 时间的 ISO 格式字符串。"""
    return datetime.now(timezone.utc).isoformat()


def safe_read_json(file_path: Path) -> Optional[dict]:
    """安全读取 JSON 文件，损坏时返回 None 并记录警告。"""
    try:
        return json.loads(file_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, IOError) as e:
        logger.warning(f"跳过损坏文件 {file_path.name}: {e}")
        return None


def safe_write_json(file_path: Path, data: dict) -> bool:
    """原子写入 JSON：先写同目录临时文件再替换。

    多台机器/多个 Claude 并发写同一目录时，避免目标文件被写一半，
    其他实例读到的永远是完整内容。写入前做一次 fsync，降低
    VMware hgfs 上非原子替换导致内容不完整的风险。
    """
    tmp_path = file_path.with_name(
        f".{file_path.name}.{os.getpid()}.{uuid.uuid4().hex[:6]}.tmp"
    )
    try:
        tmp_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        # 尽力 fsync 保证落盘（不支持 fsync 的文件系统忽略）
        try:
            with open(tmp_path, "rb+") as f:
                os.fsync(f.fileno())
        except OSError:
            pass
        os.replace(tmp_path, file_path)
        return True
    except (IOError, OSError) as e:
        logger.error(f"写入文件失败 {file_path}: {e}")
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def ok(data: dict) -> str:
    """包装成功响应为 JSON 字符串。"""
    return json.dumps({"success": True, **data}, ensure_ascii=False, indent=2)


def fail(reason: str) -> str:
    """包装失败响应为 JSON 字符串。"""
    logger.error(reason)
    return json.dumps({"success": False, "error": reason}, ensure_ascii=False, indent=2)


def normalize_url(url: str) -> str:
    """URL 归一化（v2.9.0，去重比对用）。

    小写 scheme/host、去默认端口、去 fragment、去常见跟踪参数
    （utm_* / fbclid / gclid 等）、剩余 query 参数按键排序。
    解析失败时原样返回（调用方按原 URL 比较兜底）。
    """
    if not url:
        return url
    try:
        p = urllib.parse.urlsplit(url)
    except ValueError:
        return url
    scheme = (p.scheme or "").lower()
    host = p.hostname or ""
    host = host.rstrip(".").lower()
    try:
        port = p.port
    except ValueError:
        port = None
    if port and not (
        (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    ):
        netloc = f"{host}:{port}"
    else:
        netloc = host
    kept = []
    for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True):
        kl = k.lower()
        if kl.startswith("utm_") or kl in _TRACKING_PARAMS:
            continue
        kept.append((k, v))
    kept.sort()
    query = urllib.parse.urlencode(kept)
    return urllib.parse.urlunsplit((scheme, netloc, p.path or "/", query, ""))
