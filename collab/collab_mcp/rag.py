"""本地 RAG 一体机薄层：rag_query（v3.30.2）。

这一层不新造检索/摄入引擎，而是把 collab 里已经落地的三块能力串成一个用户出口：
- add_document：摄入 PDF/Word/网页/纯文本到 documents/（已有）
- search_documents + semantic_search：关键词全文检索 + 向量语义检索（已有）
- 本工具：混合召回（RRF 去重排序）→ 取原文片段 → 本地 Ollama 引用式回答；
  Ollama 不可用时自动降级为 retrieval-only（只返回资料片段，不编造答案）。

安全约定：
- 身份闸门复用；
- target_dir 必须落在共享根内（与 search/semantic 同规则）；
- query / context 只用于本句回答，不写回共享目录、不入库。
"""

import asyncio
import json
import os
import re
import urllib.error
import urllib.request

from . import search as _search_mod
from . import semantic as _semantic_mod
from .identity import assert_identity_allowed
from .logging_setup import logger
from .search import _DEFAULT_SCOPE, _SHARED_ROOT, _read_doc_file
from .utils import fail, ok

_DEFAULT_MODEL = "qwen2.5:3b"
_DEFAULT_PASSAGE_CHARS = 1600
_MAX_CONTEXT_PIECES = 12
_MAX_ANSWER_CHARS = 2400
_LLM_TIMEOUT = 120


def _clamp_int(value, lo: int, hi: int, default: int) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def _parse_response(raw: str) -> dict:
    """解析工具返回的 JSON 字符串，失败按空 dict 处理。"""
    try:
        data = json.loads(raw or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _norm_path(path: str) -> str:
    return (path or "").replace("\\", "/").strip().strip("/")


def _rrf_merge(semantic_hits: list[dict], lexical_hits: list[dict]) -> list[dict]:
    """两份召回结果做 Reciprocal Rank Fusion，按 path 去重。"""
    merged: dict[str, dict] = {}
    for source, hits in (("semantic", semantic_hits), ("lexical", lexical_hits)):
        for rank, item in enumerate(hits or [], 1):
            path = _norm_path(item.get("path"))
            if not path:
                continue
            entry = merged.setdefault(
                path,
                {
                    "path": path,
                    "title": "",
                    "score": 0.0,
                    "snippet": "",
                    "_rrf": 0.0,
                    "sources": [],
                },
            )
            title = (item.get("title") or "").strip()
            snippet = (item.get("snippet") or "").strip()
            if title and not entry["title"]:
                entry["title"] = title
            if snippet and not entry["snippet"]:
                entry["snippet"] = snippet
            entry["_rrf"] += 1.0 / (60 + rank)
            entry["sources"].append(source)
            entry["score"] = max(entry["score"], float(item.get("score") or 0.0))
    items = list(merged.values())
    for entry in items:
        if not entry["title"]:
            entry["title"] = entry["path"].rsplit("/", 1)[-1]
        if not entry["snippet"]:
            entry["snippet"] = entry["title"]
    items.sort(key=lambda x: (x["_rrf"], x["score"]), reverse=True)
    return items


def _query_terms(query: str) -> list[str]:
    """从查询里取出可用于定位高亮区间的有效词元。"""
    words = re.findall(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]{2,}", (query or "").strip())
    return [w for w in words if len(w) >= 2]


def _passage(text: str, query: str, max_chars: int = _DEFAULT_PASSAGE_CHARS) -> str:
    """取正文里最贴近查询的一段；无命中时从头截取，保证回答有上下文。"""
    clean = re.sub(r"[ \t]+", " ", (text or "").strip())
    clean = re.sub(r"\n{2,}", "\n", clean)
    if not clean:
        return ""
    low = clean.lower()
    first = None
    for term in _query_terms(query):
        idx = low.find(term.lower())
        if idx != -1:
            first = idx
            break
    if first is None:
        start = 0
    else:
        start = max(0, first - max_chars // 4)
    end = min(len(clean), start + max_chars)
    if end < len(clean):
        clean = clean[:end].rstrip() + "…"
    if start > 0:
        clean = "…" + clean[start - 1 :]
    return clean


def _ollama_url() -> str:
    return (os.environ.get("OLLAMA_BASE_URL") or "http://127.0.0.1:11434").rstrip("/")


def _ollama_model() -> str:
    return (os.environ.get("OLLAMA_MODEL") or _DEFAULT_MODEL).strip() or _DEFAULT_MODEL


def _ollama_available() -> bool:
    """探测本地 Ollama chat 是否可用；一次启动失败不影响 retrieval-only 降级。"""
    try:
        with urllib.request.urlopen(_ollama_url() + "/api/tags", timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        models = [str(m.get("name", "")) for m in data.get("models", [])]
        return any(m == _ollama_model() or m.split(":")[0] == _ollama_model().split(":")[0] for m in models)
    except Exception:
        return False


def _ollama_answer(system: str, prompt: str, timeout: int) -> str:
    """调用本地 Ollama 生成引用式回答；任何异常抛给上层降级。"""
    payload = {
        "model": _ollama_model(),
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        "max_tokens": max(512, min(_MAX_ANSWER_CHARS, _MAX_ANSWER_CHARS)),
        "options": {"temperature": 0.2, "num_predict": _MAX_ANSWER_CHARS},
    }
    req = urllib.request.Request(
        _ollama_url() + "/v1/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Ollama 回答失败（HTTP {e.code}）: {e.reason}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise RuntimeError(f"Ollama 回答失败（网络）: {e}") from e
    msg = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    return (msg or "").strip()


def _context_for(hit: dict, query: str) -> str:
    doc = _read_doc_file(_SHARED_ROOT / hit["path"])
    if doc:
        return _passage(doc["body"], query)
    return hit.get("snippet") or ""


def _retrieval_only_answer(hits: list[dict], contexts: list[str]) -> str:
    """无 LLM 时只呈现原文片段，不冒充模型生成。"""
    lines = [
        "未调用本地 LLM；以下为检索到的资料原文片段，请根据来源自行核对。",
    ]
    for i, (hit, ctx) in enumerate(zip(hits, contexts), 1):
        lines.append(f"\n[{i}] {hit['title']}（{hit['path']}）\n{ctx or hit['snippet']}")
    return "\n".join(lines)


async def _semantic_hits(query: str, target_dir: str, limit: int, min_score: float):
    raw = await _semantic_mod.semantic_search(
        query=query,
        target_dir=target_dir,
        limit=limit,
        min_score=min_score,
        rebuild=False,
    )
    data = _parse_response(raw)
    if data.get("success"):
        return data.get("results") or [], ""
    return [], data.get("error") or "semantic_search 返回失败"


async def _lexical_hits(query: str, target_dir: str, limit: int):
    raw = await _search_mod.search_documents(query, target_dir, limit)
    data = _parse_response(raw)
    if data.get("success"):
        return data.get("results") or [], ""
    return [], data.get("error") or "search_documents 返回失败"


async def rag_query(
    query: str = "",
    target_dir: str = "",
    limit: int = 8,
    min_score: float = 0.3,
    use_llm: bool = True,
    llm_timeout: int = 120,
) -> str:
    """本地 RAG：混合召回（关键词 + 向量）+ 引用式回答。

    把 query 同时交给 search_documents（FTS5 关键词）与 semantic_search
    （Ollama embedding 向量），RRF 去重排序后读取原文片段；若本地 Ollama
    可用则生成引用式回答，否则返回 retrieval-only 片段。目标是复用已有
    摄入/检索能力，不再引入新库或新服务。

    Args:
        query: 问题或检索意图（必填）
        target_dir: 可选，限定共享根内子目录，默认 "documents"
        limit: 可选，最终资料条数上限，默认 8，夹取 [1, 12]
        min_score: 可选，语义相似度下限（0~1，默认 0.3）；对关键词检索不生效
        use_llm: 可选，是否尝试本地 Ollama 生成回答（失败自动降级）
        llm_timeout: 可选，Ollama 回答超时秒数，默认 120
    """
    denied = assert_identity_allowed("rag_query")
    if denied:
        return fail(denied)
    q = (query or "").strip()
    if not q:
        return fail("rag_query 需要非空 query")
    limit = _clamp_int(limit, 1, 12, 8)
    try:
        min_score = max(0.0, min(1.0, float(min_score)))
    except (TypeError, ValueError):
        min_score = 0.3
    timeout = _clamp_int(llm_timeout, 10, 300, _LLM_TIMEOUT)
    scope = (target_dir or "").strip() or _DEFAULT_SCOPE
    scope_path = (_SHARED_ROOT / scope).resolve()
    if not scope_path.is_relative_to(_SHARED_ROOT.resolve()):
        return fail(f"target_dir 超出共享目录范围: {target_dir}")

    lexical, lexical_err = await _lexical_hits(q, scope, max(2, min(limit * 2, 100)))
    if lexical_err:
        return fail(lexical_err)
    semantic, semantic_err = await _semantic_hits(q, scope, max(2, min(limit * 2, 50)), min_score)

    hits = _rrf_merge(semantic, lexical)
    if not hits:
        return ok({
            "query": q,
            "scope": scope,
            "answer": "没有在共享知识库里检索到相关资料；请确认文档已通过 add_document 摄入，"
                      "或尝试更换关键词/目标目录。",
            "citations": [],
            "retrieved": 0,
            "semantic_ok": bool(semantic),
            "semantic_error": semantic_err,
            "answering_mode": "empty",
        })

    hits = hits[:limit]
    contexts = [_context_for(h, q) for h in hits]
    citations = []
    for i, h in enumerate(hits, 1):
        citations.append({
            "index": i,
            "path": h["path"],
            "title": h["title"],
            "score": round(h["score"], 4),
            "sources": h["sources"],
            "snippet": h["snippet"][:600],
        })

    answering_mode = "retrieval_only"
    answer = _retrieval_only_answer(hits, contexts)
    llm_backend = ""
    if use_llm and _ollama_available():
        prompt_parts = [
            "请依据下面的资料直接回答问题，不要使用外部知识。",
            "",
            f"问题：{q}",
            "",
            "资料：",
        ]
        for i, (h, ctx) in enumerate(zip(hits, contexts), 1):
            prompt_parts.append(
                f"[{i}] 标题《{h['title']}》\n路径：{h['path']}\n正文：\n{ctx}"
            )
        prompt = "\n".join(prompt_parts)
        system = (
            "你是本机私有知识库的引用式问答助手。只能依据用户消息中“资料”部分回答，"
            "不要使用外部知识。回答语言与资料语言保持一致：资料是英文就用英文并按原文措辞复述，"
            "资料是中文就用中文；不要擅自翻译或改写专有名词。若资料确实没有相关信息，"
            "写“资料未说明”，不要编造；回答末尾用 [来源 n] 标注依据。"
        )
        try:
            answer = await asyncio.to_thread(_ollama_answer, system, prompt, timeout)
            if answer:
                answering_mode = "llm"
                llm_backend = self_ollama_note()
        except RuntimeError as e:
            logger.warning(f"⚠️ rag_query LLM 不可用，降级 retrieval-only: {e}")
    logger.info(
        f"🤖 rag_query: query={q[:120]} scope={scope} mode={answering_mode} "
        f"retrieved={len(hits)}"
    )
    return ok({
        "query": q,
        "scope": scope,
        "answer": answer[:_MAX_ANSWER_CHARS],
        "citations": citations,
        "retrieved": len(hits),
        "semantic_ok": bool(semantic),
        "semantic_error": semantic_err,
        "answering_mode": answering_mode,
        "llm_backend": llm_backend,
    })


def self_ollama_note() -> str:
    """返回当前回答后端标识，供响应和日志使用。"""
    return f"ollama {_ollama_model()}"
