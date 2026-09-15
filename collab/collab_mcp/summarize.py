"""文本摘要工具：summarize_text（v3.7.0）。

设计来源：progress/collab-v3.7-summarize-design.md
- 提取式（默认兜底，零依赖）：分句 → 词频（中文=CJK 连续序列 / 英文=单词）→ 位置加权
  → Top N 句保持原文顺序拼接 → 长度钳制。
- LLM 后端（可选）：OpenAI-compatible POST {base}/v1/chat/completions（urllib，零新依赖）；
  支持 OLLAMA_BASE_URL+OLLAMA_MODEL（无 key）与 OPENAI_API_KEY。
- backend=auto：有 LLM 配置 → llm；否则 extractive（零 API 成本）；LLM 失败 auto 降级 extractive（诚实标注）。

安全约定：身份闸门复用；日志脱敏（text 只记长度不记内容）；backend=llm 无配置 → 明确失败。
"""

import asyncio
import json
import os
import re
import urllib.error
import urllib.request

from .identity import assert_identity_allowed
from .logging_setup import logger
from .utils import fail, ok

_PREVIEW_CHARS = 5000
_MAX_TEXT_CHARS = 12000  # 喂 LLM 的文本上限（超长截断并标注）
_STOP_ZH = frozenset("的了是在和与及等之有就不也为此但或一个我们你们他们这那而于")
_STOP_EN = frozenset((
    "the", "a", "an", "is", "are", "was", "were", "to", "of", "and", "or",
    "in", "on", "at", "for", "with", "as", "by", "that", "this", "it",
    "be", "been", "have", "has", "had", "i", "you", "we", "they", "he",
    "she", "from", "not", "but", "do", "does", "did", "will", "would", "can",
))


def _clamp_int(value, lo: int, hi: int, default: int) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def _split_sentences(text: str, lang: str) -> list[str]:
    """按语言分句（保留结束标点）。"""
    if lang == "zh":
        parts = re.split(r"(?<=[。！？；])", text)
    elif lang == "en":
        parts = re.split(r"(?<=[.!?])\s+", text)
    else:
        parts = re.split(r"(?<=[。！？；.!?])\s*", text)
    return [p.strip() for p in parts if p and p.strip()]


def _tokenize(text: str, lang: str) -> list[str]:
    """词元：中文=连续 CJK 序列；英文=单词（小写，去单字符）。"""
    if lang == "zh":
        return re.findall(r"[\u4e00-\u9fff]{2,}", text)
    return [w.lower() for w in re.findall(r"[A-Za-z0-9]+", text) if len(w) > 1]


def _score_sentences(sentences: list[str], lang: str) -> list[tuple[str, float]]:
    """词频 + 位置加权 → 每句得分。"""
    stop = _STOP_ZH if lang == "zh" else _STOP_EN
    freq: dict[str, int] = {}
    for s in sentences:
        for w in _tokenize(s, lang):
            if w in stop:
                continue
            freq[w] = freq.get(w, 0) + 1
    n = len(sentences)
    scored = []
    for i, s in enumerate(sentences):
        score = sum(freq.get(w, 0) for w in _tokenize(s, lang))
        if i < 2:
            score += 2.0  # 标题/开头句加权
        if i == n - 1:
            score += 1.0  # 末句（结论）加权
        scored.append((s, score))
    return scored


def _extractive_summary(text: str, lang: str, ratio: float, max_chars: int) -> str:
    """提取式摘要：Top N 句保持原文顺序拼接，按 max_chars 截断。"""
    sentences = [s for s in _split_sentences(text, lang) if len(s) >= 8]
    if not sentences:
        return ""
    scored = _score_sentences(sentences, lang)
    n = max(1, min(len(scored), round(len(scored) * ratio)))
    # 低1（PC-C）：按索引追踪 top 选择（勿用句文本 set——重复句子会整体纳入、超 ratio 预期）
    top_indices = sorted(
        range(len(scored)),
        key=lambda i: scored[i][1],
        reverse=True,
    )[:n]
    out = "".join(sentences[i] for i in sorted(top_indices))
    return out[:max_chars]


def _llm_base_url(env_key: str, default: str) -> str:
    """归一化 OpenAI-compatible 服务根地址：去掉尾部 /v1，URL 拼装时统一补 /v1/chat/completions。

    避免默认值/用户值同时含 /v1 时拼出 /v1/v1/ 404（PC-C 闸门低-1，2026-08-07 吸收）。
    """
    base = (os.environ.get(env_key) or default).rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    return base


def _llm_configured() -> str:
    """返回可用 LLM 后端标识（ollama/openai/''）；auto 探测用。"""
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    if os.environ.get("OLLAMA_BASE_URL") or os.environ.get("OLLAMA_MODEL"):
        return "ollama"
    return ""


def _llm_summarize(text: str, lang: str, max_chars: int, timeout: int) -> tuple[str, str]:
    """LLM 摘要（OpenAI-compatible chat completions），返回 (summary, backend)。"""
    if os.environ.get("OPENAI_API_KEY"):
        base = _llm_base_url("OPENAI_BASE_URL", "https://api.openai.com/v1")
        model = os.environ.get("OPENAI_MODEL") or "gpt-4o-mini"
        headers = {"Content-Type": "application/json",
                   "Authorization": "Bearer " + os.environ["OPENAI_API_KEY"]}
        backend = "openai"
    else:
        base = _llm_base_url("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
        # v3.36.4：默认与 rag/ai_teacher/vision 对齐用 qwen2.5:3b（本机已装）；
        # 原默认 qwen2.5:7b 本机未拉取，设了 OLLAMA_BASE_URL 却漏设 OLLAMA_MODEL 时会调空。
        model = os.environ.get("OLLAMA_MODEL") or "qwen2.5:3b"
        headers = {"Content-Type": "application/json"}
        backend = "ollama"
    lang_hint = {"zh": "用中文输出。", "en": "Output in English.", "": ""}.get(lang, "")
    system_prompt = (
        "你是一个严谨的摘要助手。请用不超过 %d 字符输出给定文本的核心要点摘要，"
        "保留关键事实、数据与结论，不要添加原文没有的内容。%s"
    ) % (max_chars, lang_hint)
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text[:_MAX_TEXT_CHARS]},
        ],
        "temperature": 0.3,
        "max_tokens": min(2000, max(256, max_chars // 2)),
    }
    req = urllib.request.Request(
        f"{base}/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"LLM 摘要失败（HTTP {e.code}）: {e.reason}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise RuntimeError(f"LLM 摘要失败（网络）: {e}") from e
    content = ((data.get("choices") or [{}])[0].get("message", {}) or {}).get("content", "")
    content = (content or "").strip()
    if not content:
        raise RuntimeError("LLM 摘要返回空内容")
    return content[:max_chars], backend


async def summarize_text(
    text: str = "",
    ratio: float = 0.3,
    max_chars: int = 1500,
    backend: str = "auto",
    lang: str = "auto",
    timeout: int = 60,
) -> str:
    """文本摘要（提取式兜底 + 可选 LLM）。

    Args:
        text: 待摘要文本（媒体转写/网页正文/文档内容等）
        ratio: 提取式摘要句占比（0.1~0.8，默认 0.3）
        max_chars: 摘要最大字符数（100~8000，默认 1500）
        backend: auto（默认；有 LLM 配置则 LLM 否则提取式）/ extractive / llm
        lang: auto（默认）/ zh / en（提取式分句与词元化按语言）
        timeout: LLM 后端调用超时秒数（默认 60）
    """
    denied = assert_identity_allowed("summarize_text")
    if denied:
        return fail(denied)
    t = (text or "").strip()
    if not t:
        return fail("summarize_text 需要非空 text")
    try:
        ratio = max(0.1, min(0.8, float(ratio or 0.3)))
    except (TypeError, ValueError):
        ratio = 0.3
    max_chars = _clamp_int(max_chars, 100, 8000, 1500)
    timeout = _clamp_int(timeout, 10, 300, 60)
    b = (backend or "auto").strip().lower()
    if b not in ("auto", "extractive", "llm"):
        return fail(f"未知 backend: {backend}（可选 auto/extractive/llm）")
    l = (lang or "auto").strip().lower()
    if l not in ("auto", "zh", "en"):
        return fail(f"未知 lang: {lang}（可选 auto/zh/en）")
    use_lang = l if l != "auto" else ("zh" if re.search(r"[\u4e00-\u9fff]", t) else "en")
    # 提取式必须保留原文句子（不 NFKC，避免全角/半角标点被改写导致句子不匹配）
    logger.info(f"\U0001f4dd summarize_text: chars={len(t)} lang={use_lang} backend={b}")

    method = "extractive"
    llm_backend = ""
    configured = _llm_configured()
    if b == "llm":
        if not configured:
            return fail("backend=llm 但未配置 LLM（设 OPENAI_API_KEY 或 OLLAMA_BASE_URL+OLLAMA_MODEL）")
        try:
            summary, llm_backend = await asyncio.to_thread(_llm_summarize, t, use_lang, max_chars, timeout)
            method = "llm"
        except RuntimeError as e:
            return fail(str(e))
    elif b == "auto" and configured:
        try:
            summary, llm_backend = await asyncio.to_thread(_llm_summarize, t, use_lang, max_chars, timeout)
            method = "llm"
        except RuntimeError as e:
            # auto：LLM 失败降级提取式（诚实标注 method=extractive）
            logger.warning(f"⚠️ LLM 摘要失败，降级提取式: {e}")
            summary = _extractive_summary(t, use_lang, ratio, max_chars)
            method = "extractive"
    else:
        summary = _extractive_summary(t, use_lang, ratio, max_chars)

    if not summary:
        return fail("未能生成摘要（文本过短或无可提取句子）")
    truncated = len(t) > _MAX_TEXT_CHARS
    return ok({
        "method": method,
        "llm_backend": llm_backend if method == "llm" else "",
        "summary": summary[:_PREVIEW_CHARS],
        "truncated": len(summary) > _PREVIEW_CHARS or truncated,
        "chars": len(summary),
        "estimated_tokens": max(1, round(len(summary) / 4)),
        "source_chars": len(t),
    })
