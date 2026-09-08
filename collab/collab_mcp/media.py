"""媒体转写入库工具：transcribe_media（v3.0）。

把本地音频/视频文件转写为文本并摄入 documents/media/（复用 add_document，
带元数据头 + 内容指纹去重 + NFKC），让平台可检索「听过的内容」，与 web_fetch
（抓回来的网页）形成「代替人类上网」第三步闭环（设计：
progress/collab-v3.0-media-ingest-design.md）。

后端矩阵（零硬依赖原则：全部可选、自动探测、无后端时明确报错）：
1. faster-whisper（推荐本地）：faster_whisper.WhisperModel，CPU int8 小模型
2. openai-whisper：whisper.load_model
3. OpenAI audio.transcriptions API：需 OPENAI_API_KEY + openai 已装
探测顺序：faster-whisper → whisper → openai；backend 参数可显式指定。

安全约定：
- 输入 source_path 允许执行端本机绝对路径或共享根相对路径（与 add_document 一致），
  相对路径必须解析到共享根内，防目录穿越；
- 单文件大小上限 1GB；
- 身份闸门：REQUIRE_IDENTITY=1 时未绑定身份被拒；
- 日志脱敏：记文件名/后端，不记转录全文。
"""

import asyncio
import hashlib
import json
import os
import re
import tempfile
import unicodedata
from pathlib import Path

from .config import COLLAB_DIR
from .documents import add_document
from .identity import assert_identity_allowed
from .logging_setup import logger
from .utils import fail, ok

_MEDIA_MAX_BYTES = 1024 * 1024 * 1024  # 1GB
_PREVIEW_CHARS = 5000
_SHARED_ROOT = COLLAB_DIR.parent
_BACKEND_ORDER = ("faster-whisper", "whisper", "openai")


def _backend_available(name: str) -> bool:
    """后端是否可用：包已安装（openai 还需 OPENAI_API_KEY）。"""
    try:
        if name == "faster-whisper":
            import faster_whisper  # noqa: F401
        elif name == "whisper":
            import whisper  # noqa: F401
        elif name == "openai":
            import openai  # noqa: F401
            if not os.environ.get("OPENAI_API_KEY", "").strip():
                return False
        else:
            return False
        return True
    except Exception:
        return False


def _detect_backend(requested: str) -> str:
    """确定实际转写后端：显式指定优先（不可用则返回空），否则按推荐序自动探测。"""
    req = (requested or "").strip().lower()
    if req:
        return req if req in _BACKEND_ORDER and _backend_available(req) else ""
    for name in _BACKEND_ORDER:
        if _backend_available(name):
            return name
    return ""


def _run_faster_whisper(path: Path, language: str, model: str = "small") -> tuple[str, str]:
    """faster-whisper（CPU int8）：返回 (文本, 模型名)。"""
    from faster_whisper import WhisperModel
    m = WhisperModel(model, device="cpu", compute_type="int8")
    segments, _info = m.transcribe(str(path), language=language or None)
    text = " ".join((seg.text or "").strip() for seg in segments).strip()
    return text, model


def _run_whisper(path: Path, language: str, model: str = "small") -> tuple[str, str]:
    """openai-whisper：返回 (文本, 模型名)。"""
    import whisper
    m = whisper.load_model(model)
    result = m.transcribe(str(path), language=language or None)
    return (result.get("text") or "").strip(), model


def _run_openai(path: Path, language: str) -> tuple[str, str]:
    """OpenAI audio.transcriptions API：返回 (文本, 模型名)。"""
    from openai import OpenAI
    client = OpenAI()
    with open(path, "rb") as f:
        result = client.audio.transcriptions.create(
            model="whisper-1",
            file=f,
            language=language or None,
        )
    return (getattr(result, "text", "") or "").strip(), "whisper-1"


def _transcribe(path: Path, backend: str, language: str) -> tuple[str, str, str]:
    """执行转写，返回 (文本, 实际后端, 模型名)。"""
    if backend == "faster-whisper":
        text, model = _run_faster_whisper(path, language)
    elif backend == "whisper":
        text, model = _run_whisper(path, language)
    elif backend == "openai":
        text, model = _run_openai(path, language)
    else:
        raise ValueError(f"未知转写后端: {backend}")
    return text, backend, model


async def transcribe_media(
    source_path: str = "",
    language: str = "",
    save: bool = True,
    backend: str = "",
) -> str:
    """转写本地音频/视频为文本，默认入库 documents/media/（可检索）。

    Args:
        source_path: 媒体文件路径——执行端本机绝对路径，或相对共享根（如 "results/meeting.mp3"）
        language: 可选，语音语言代码（如 "zh"/"en"）；缺省自动检测
        save: 可选，为 True 时把转写文本摄入 documents/media/（内容指纹去重 + 元数据）
        backend: 可选，显式指定转写后端（faster-whisper/whisper/openai）；缺省自动探测
    """
    denied = assert_identity_allowed("transcribe_media")
    if denied:
        return fail(denied)
    src = (source_path or "").strip()
    if not src:
        return fail("transcribe_media 需要非空 source_path")
    p = Path(src)
    if not p.is_absolute():
        p = (_SHARED_ROOT / p).resolve()
        if not p.is_relative_to(_SHARED_ROOT.resolve()):
            return fail(f"source_path 超出共享目录范围: {source_path}")
    if not p.exists() or not p.is_file():
        return fail(f"源文件不存在: {source_path}")
    size = p.stat().st_size
    if size > _MEDIA_MAX_BYTES:
        return fail(f"媒体文件超过上限 {_MEDIA_MAX_BYTES // (1024 * 1024)}MB")

    active = _detect_backend(backend)
    if not active:
        return fail(
            "转写后端不可用：请安装 faster-whisper 或 openai-whisper，"
            "或设置 OPENAI_API_KEY 启用 OpenAI 转写"
        )
    try:
        # 转写为 CPU 密集操作，丢到线程池避免阻塞事件循环
        text, used_backend, model = await asyncio.to_thread(_transcribe, p, active, language)
    except Exception as e:
        logger.warning(f"媒体转写失败 {p.name} ({active}): {e}")
        return fail(f"媒体转写失败（{active}）: {e}")
    text = unicodedata.normalize("NFKC", text or "").strip()
    if not text:
        return fail("未能从媒体提取到转写文本（可能是静音/纯音乐/不支持格式）")
    chars = len(text)
    preview = text[:_PREVIEW_CHARS]
    media_type = p.suffix.lower().lstrip(".") or "unknown"

    saved = None
    if save:
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", p.stem) or "media"
        tmp_dir = Path(tempfile.mkdtemp(prefix="collab_media_"))
        tmp_file = tmp_dir / f"{safe_name}.md"
        try:
            tmp_file.write_text(text, encoding="utf-8")
            dedup_key = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
            res = await add_document(
                str(tmp_file),
                target_dir="documents/media",
                skip_duplicates=True,
                dedup_key=dedup_key,
                extra_meta={
                    "media_file": p.name,
                    "media_type": media_type,
                    "backend": used_backend,
                    "model": model,
                    "language": language,
                    "transcript_length": chars,
                },
            )
            parsed = json.loads(res) if isinstance(res, str) else res
            if not parsed.get("success"):
                return fail(f"转写文本入库失败: {parsed.get('error', '未知错误')}")
            saved = {
                "path": str(parsed.get("path") or "").replace("\\", "/"),
                "filename": parsed.get("filename"),
                "duplicate": bool(parsed.get("duplicate", False)),
                "duplicate_count": int(parsed.get("duplicate_count") or 0),
                "session_duplicates": int(parsed.get("session_duplicates") or 0),
                "dedup_key": dedup_key,
            }
        except Exception as e:
            # PC-C L1：入库块（写临时文件/add_document/解析）异常转干净 fail()，
            # 与其余错误处理风格一致；临时文件由 finally 清理
            logger.warning(f"转写文本入库失败 {p.name}: {type(e).__name__}")
            return fail(f"转写文本入库失败: {type(e).__name__}")
        finally:
            try:
                tmp_file.unlink(missing_ok=True)
                tmp_dir.rmdir()
            except OSError:
                pass

    logger.info(f"🎙️ 媒体转写: {p.name} backend={used_backend} chars={chars} saved={bool(saved)}")
    return ok({
        "source": p.name,
        "backend": used_backend,
        "model": model,
        "media_type": media_type,
        "chars": chars,
        "estimated_tokens": max(1, round(chars / 4)),
        "truncated": chars > _PREVIEW_CHARS,
        "transcript_preview": preview,
        "saved": saved,
    })
