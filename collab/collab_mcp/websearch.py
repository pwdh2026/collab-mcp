"""外部互联网检索工具：web_search / web_fetch（v2.8.0，v2.9.0 正文清洗+去重增强）。

设计来源：progress/collab-v2.8.0-external-search-design.md
- web_search：多后端降级链（默认 Bing HTML 免费解析 → DuckDuckGo Instant
  Answer → Firecrawl Search；v3.1.0 起可选用 wigolo：本地多引擎聚合 + ML 重排，
  探测到 wigolo serve 时自动成为首选），返回与 search_documents 风格一致的结果
  摘要（title/url/snippet/source/rank）。可用环境变量 WEB_SEARCH_PROVIDER
  （wigolo|bing|duckduckgo|firecrawl）强制首选后端。
  实测（2026-08-05）：本机网络 DuckDuckGo 不可达（连接挂起），Bing/Firecrawl
  可达，因此默认链以 Bing 优先；Firecrawl 仅在有 FIRECRAWL_API_KEY 时加入。
- web_fetch：抓取单页转文本。优先 Firecrawl API（环境变量 FIRECRAWL_API_KEY
  已设置时，返回高质量 markdown）；否则 stdlib 快速路径（v2.9.0 打分式正文抽取，
  纯 stdlib，普通页秒级）；stdlib 失败/空正文 → scrapling 兜底（v3.3.0：HTTP 层
  反爬，curl_cffi TLS 指纹伪装，秒级，可选后端）；→ wigolo 兜底（v3.2.0：本地
  反爬/动态页/PDF，REST /v1/fetch，tiered router 信号驱动升级 headless）。产物
  默认入库
  documents/web/（复用 add_document，带元数据头 + 内容指纹/URL 归一去重 +
  计数 + NFKC 规范化）。

安全约定：
- 身份闸门：REQUIRE_IDENTITY=1 时未绑定身份被拒（与其他工具一致）；
- SSRF 防护：仅 http/https，解析后拒绝私有/环回/链路本地/保留地址，
  并复核重定向后的最终 URL；
- 限流：进程内 asyncio.Semaphore(4) + 超时上限 + 单次响应 10MB 上限；
- 日志脱敏：记 query 与 URL（截断），不记 API key。
"""

import asyncio
import gzip
import hashlib
import html
import importlib
import ipaddress
import json
import os
import re
import socket
import tempfile
import unicodedata
import urllib.error
import zlib
import urllib.parse
import urllib.request
from pathlib import Path

from .documents import add_document
from .identity import assert_identity_allowed
from .logging_setup import logger
from .search import _is_pure_punct
from .utils import fail, normalize_url, now_iso, ok

_DDG_API = "https://api.duckduckgo.com/"
_FIRECRAWL_API = "https://api.firecrawl.dev/v1/scrape"
_FIRECRAWL_SEARCH_API = "https://api.firecrawl.dev/v1/search"
_BING_SEARCH = "https://www.bing.com/search"
_UA = (
    "Mozilla/5.0 (compatible; claude-collab/2.8; "
    "+https://github.com/pwdh2026/collab-mcp)"
)
_FETCH_MAX_BYTES = 10 * 1024 * 1024  # 10MB，与 add_document 上限同量级
_PREVIEW_CHARS = 5000
# v2.9.0：正文抽取（打分式，纯 stdlib）参数
_JUNK_TAGS = (
    "script", "style", "noscript", "template", "svg", "canvas", "iframe",
    "form", "nav", "footer", "aside", "select", "button", "input", "textarea",
    "dialog", "menu", "object", "embed", "figure", "video", "audio", "source",
    "picture", "math",
)
_JUNK_TAG_RE = re.compile(
    r"(?is)<(" + "|".join(_JUNK_TAGS) + r")\b[^>]*>.*?</\1>"
)
_HIDDEN_EL_RE = re.compile(
    r"(?is)<(div|section|span|p|li|ul|ol|h[1-6]|table|tr|td|a|article|main)\b[^>]*"
    r'(?:\bhidden\b|style\s*=\s*["\'][^"\']*(?:display\s*:\s*none|visibility\s*:\s*hidden)[^"\']*["\'])'
    r"[^>]*>.*?</\1>"
)
_BLOCK_TAG_RE = re.compile(r"(?is)<(h[1-6]|p|li|blockquote|pre|td|dd|dt)\b[^>]*>(.*?)</\1>")
_LINK_TEXT_RE = re.compile(r"(?is)<a\b[^>]*>(.*?)</a>")
_PUNCT_RE = re.compile(r"[，。；！？、,.!?;:]")
_GAP_LIMIT = 2000        # 正文块最大允许空隙（字符）
_FALLBACK_MIN_LEN = 40   # 打分正文总长低于该值回退全文本
_MAX_RESULTS_MAX = 20
_DEFAULT_MAX_RESULTS = 10
_PROVIDER_ORDER = ("bing", "duckduckgo", "firecrawl")
# v3.1.0：wigolo 可选 provider（本地多引擎聚合 + ML 重排；零硬依赖，探测到才用；
# 默认 serve 地址 http://127.0.0.1:3333，未设 WIGOLO_REST_URL = 不启用）
_WIGOLO_PROBE_TIMEOUT_DEFAULT = 2
_TIMEOUT_MIN = 5
_TIMEOUT_MAX = 60

# 进程内并发闸：防止多个检索同时发起拖垮 VM / 打爆外部配额
_SEM = asyncio.Semaphore(4)
# 低2（PC-C v3.4）：浏览器子步独立小信号量（chromium 进程吃内存，防极端并发）
_SEM_BROWSER = asyncio.Semaphore(2)
# v3.1.0：wigolo 探测结果进程内缓存（key=base_url；env 变更后自动重新探测）
_wigolo_probed: set[str] = set()
_wigolo_available_cache: dict[str, bool] = {}
# v3.3.0：scrapling 可选抓取后端（HTTP 层反爬，curl_cffi TLS 指纹伪装；零硬依赖，探测到才用）
_scrapling_probed = False
_scrapling_available_cache = False
_scrapling_browser_probed = False
_scrapling_browser_cache = False
_SCRAPLING_IMPERSONATE_DEFAULT = "chrome"
_SCRAPLING_FETCHER: object | None = None  # 懒加载（未安装=保持 None）


class _NetworkError(Exception):
    """外部网络调用的统一错误（kind: timeout/rate_limited/http/network/too_large/provider）。"""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


def _clamp_int(value, lo: int, hi: int, default: int) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def _host_of(url: str) -> str:
    try:
        return urllib.parse.urlparse(url).netloc
    except ValueError:
        return ""


def _clean_text(raw: str) -> str:
    """剥标签后归一化空白（PC-B 验证 L1：Bing title 内部多空格未压缩）。"""
    return re.sub(r"\s+", " ", raw).strip()


def _check_url(url: str) -> str | None:
    """SSRF 前置校验：返回 None 表示放行，否则返回拒绝原因。"""
    try:
        p = urllib.parse.urlparse(url)
    except ValueError:
        return "无效 URL"
    if p.scheme not in ("http", "https"):
        return f"仅支持 http/https 协议: {p.scheme or '<无协议>'}"
    host = p.hostname
    if not host:
        return "URL 缺少主机名"
    port = p.port or (443 if p.scheme == "https" else 80)
    # IP 字面量直接判定，无需 DNS；主机名才解析
    try:
        ip = ipaddress.ip_address(host)
        return _reject_if_internal(ip, host)
    except ValueError:
        pass
    low = host.lower()
    if low == "localhost" or low.endswith(".localhost") or low.endswith(".local"):
        return f"目标地址被拒绝（内网/保留地址）: {host}"
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return f"无法解析主机: {host}"
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        err = _reject_if_internal(ip, host)
        if err:
            return err
    # PC-B 验证 L3（信息级）：前置 DNS 校验与真实连接之间理论上存在
    # TOCTOU（DNS 重绑定），标准库 urllib 不便连接后对端 IP 复核；
    # 当前 web_fetch 面向可信外部抓取，风险可接受。若未来开放任意 URL
    # 抓取，建议改 httpx + 连接后对端 IP 校验。
    return None


def _reject_if_internal(ip: ipaddress.IPv4Address | ipaddress.IPv6Address, host: str) -> str | None:
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        return f"目标地址被拒绝（内网/保留地址）: {host} -> {ip}"
    return None


def _decompress_stream(raw: bytes, decomp, limit: int) -> bytes:
    """流式解压（PC-C 验证 M2：防解压炸弹）——累计解压量超过 limit 立即中止。"""
    out = bytearray()
    chunk = 64 * 1024
    for i in range(0, len(raw), chunk):
        out.extend(decomp.decompress(raw[i:i + chunk]))
        if len(out) > limit:
            raise _NetworkError("too_large", "解压后响应超过上限（10MB）")
    out.extend(decomp.flush())
    if len(out) > limit:
        raise _NetworkError("too_large", "解压后响应超过上限（10MB）")
    return bytes(out)


def _decompress_bytes(raw: bytes, encoding: str, limit: int) -> bytes:
    """按 Content-Encoding 解压响应体；未知编码原样返回（记警告）。"""
    enc = (encoding or "").strip().lower()
    if enc in ("", "identity"):
        return raw
    if enc not in ("gzip", "x-gzip", "deflate"):
        logger.warning(f"未知 Content-Encoding: {enc}，按原样返回")
        return raw
    try:
        if enc in ("gzip", "x-gzip"):
            return _decompress_stream(raw, zlib.decompressobj(16 + zlib.MAX_WBITS), limit)
        # deflate：先试 zlib 封装（带头），失败回退原始 deflate（-MAX_WBITS）
        try:
            return _decompress_stream(raw, zlib.decompressobj(), limit)
        except zlib.error:
            return _decompress_stream(raw, zlib.decompressobj(-zlib.MAX_WBITS), limit)
    except _NetworkError:
        raise
    except (zlib.error, OSError, EOFError) as e:
        raise _NetworkError("network", f"响应解压失败: {e}")


async def _fetch_raw(
    url: str,
    timeout: int,
    data: bytes | None = None,
    headers: dict | None = None,
) -> tuple[str, bytes]:
    """执行 HTTP 请求（受信号量限制），返回 (最终 URL, 响应体)。"""
    req = urllib.request.Request(
        url,
        data=data,
        headers=headers or {},
        method="POST" if data is not None else "GET",
    )
    async with _SEM:
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw_body = resp.read(_FETCH_MAX_BYTES + 1)
                final_url = resp.geturl() or url
                # v2.9.0：部分站点（如 python.org）对无 Accept-Encoding 的请求
                # 也返回 gzip/deflate，urllib 不自动解压 → 流式解压并累计 10MB 上限
                # （PC-C 验证 M2：全量解压后再复核有解压炸弹内存风险）
                body = _decompress_bytes(
                    raw_body,
                    resp.headers.get("Content-Encoding") or "",
                    _FETCH_MAX_BYTES,
                )
        except urllib.error.HTTPError as e:
            if e.code == 429:
                raise _NetworkError("rate_limited", "外部服务限流（HTTP 429），请稍后重试")
            raise _NetworkError("http", f"外部服务返回 HTTP {e.code}")
        except (urllib.error.URLError, socket.timeout, TimeoutError) as e:
            reason = getattr(e, "reason", e)
            if isinstance(reason, (socket.timeout, TimeoutError)) or isinstance(
                e, (socket.timeout, TimeoutError)
            ):
                raise _NetworkError("timeout", f"外部请求超时（{timeout}s）")
            raise _NetworkError("network", f"网络请求失败: {reason}")
        except OSError as e:
            raise _NetworkError("network", f"网络请求失败: {e}")
        if len(body) > _FETCH_MAX_BYTES:
            raise _NetworkError("too_large", "外部响应超过上限（10MB）")
        return final_url, body


async def _ddg_json(query: str, timeout: int) -> dict:
    """调用 DuckDuckGo Instant Answer API，返回解析后的 JSON。"""
    params = urllib.parse.urlencode(
        {"q": query, "format": "json", "no_html": "1", "skip_disambig": "1"}
    )
    _, body = await _fetch_raw(
        f"{_DDG_API}?{params}",
        timeout,
        headers={"User-Agent": _UA, "Accept": "application/json"},
    )
    try:
        return json.loads(body.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return {}


def _wigolo_base_url() -> str:
    """wigolo REST 基址；未设置 WIGOLO_REST_URL = 未启用（零硬依赖默认关闭）。

    信任边界（PC-C L1 吸收）：仅连接运维显式配置的地址（默认 loopback），
    该地址属配置信任范围，不经过 _check_url SSRF 校验（与 FIRECRAWL_API_KEY 同语义）。
    """
    return os.environ.get("WIGOLO_REST_URL", "").strip().rstrip("/")


def _wigolo_token() -> str:
    return os.environ.get("WIGOLO_API_TOKEN", "").strip()


def _wigolo_probe_timeout() -> int:
    return _clamp_int(
        os.environ.get("WIGOLO_PROBE_TIMEOUT", ""), 1, 5, _WIGOLO_PROBE_TIMEOUT_DEFAULT
    )


def _reset_wigolo_cache() -> None:
    """测试/运维用：清空 wigolo 探测缓存，强制下次调用重新探测。"""
    _wigolo_probed.clear()
    _wigolo_available_cache.clear()


async def _wigolo_available() -> bool:
    """探测 wigolo serve 是否健康可用（进程内缓存；探测失败静默降级，不阻断）。"""
    base = _wigolo_base_url()
    if not base:
        return False
    if base in _wigolo_probed:
        return _wigolo_available_cache.get(base, False)
    _wigolo_probed.add(base)
    available = False
    try:
        _, body = await _fetch_raw(
            f"{base}/health",
            _wigolo_probe_timeout(),
            headers={"Accept": "application/json"},
        )
        data = json.loads(body.decode("utf-8", errors="replace"))
        available = bool(data.get("status") == "healthy")
    except (json.JSONDecodeError, _NetworkError, OSError):
        available = False
    _wigolo_available_cache[base] = available
    if available:
        logger.info(f"🌐 wigolo provider 探测成功: {base}")
    else:
        logger.info(f"🌐 wigolo provider 探测失败（保持现有搜索链）: {base}")
    return available


async def _wigolo_search(query: str, limit: int, timeout: int) -> list[dict]:
    """wigolo 本地多引擎搜索（REST /v1/search），返回规范化结果列表。"""
    base = _wigolo_base_url()
    if not base:
        raise _NetworkError("provider", "wigolo 未配置（WIGOLO_REST_URL）")
    payload = json.dumps(
        {
            "query": query,
            "max_results": min(limit, 20),
            "search_depth": "fast",
        }
    ).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": _UA,
    }
    token = _wigolo_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    _, body = await _fetch_raw(
        f"{base}/v1/search", timeout, data=payload, headers=headers
    )
    try:
        data = json.loads(body.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        raise _NetworkError("provider", "wigolo 响应解析失败")
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        err = (data or {}).get("error") if isinstance(data, dict) else None
        raise _NetworkError("provider", f"wigolo 调用失败: {err or '响应缺少 results'}")
    results: list[dict] = []
    for item in data["results"]:
        if not isinstance(item, dict):
            continue
        url = (item.get("url") or "").strip()
        if not url:
            continue
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            continue
        title = _clean_text(item.get("title") or "")
        snippet = _clean_text(item.get("snippet") or "")
        if not snippet:
            ev = item.get("evidence")
            if isinstance(ev, list) and ev and isinstance(ev[0], dict):
                snippet = _clean_text(ev[0].get("excerpt") or "")
        results.append(
            {
                "title": title,
                "url": url,
                "snippet": snippet,
                "source": _host_of(url),
            }
        )
    return results


async def _wigolo_fetch(url: str, timeout: int) -> tuple[str, str]:
    """wigolo 本地抓取（REST /v1/fetch），返回 (title, markdown)。失败抛 _NetworkError。

    v3.2.0：SSRF 由调用方 _check_url 前置（仅 http/https + 拒绝私有/环回/保留地址）；
    wigolo 响应 url 非最终重定向 URL，无法事后复查（已知边界，见设计文档 §3.3）。
    """
    base = _wigolo_base_url()
    if not base:
        raise _NetworkError("provider", "wigolo 未配置（WIGOLO_REST_URL）")
    payload = json.dumps({"url": url, "render_js": "auto"}).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": _UA,
    }
    token = _wigolo_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    _, body = await _fetch_raw(
        f"{base}/v1/fetch", timeout, data=payload, headers=headers
    )
    try:
        data = json.loads(body.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        raise _NetworkError("provider", "wigolo fetch 响应解析失败")
    if not isinstance(data, dict):
        raise _NetworkError("provider", "wigolo fetch 响应格式异常")
    status = data.get("http_status")
    if isinstance(status, int) and status >= 400:
        raise _NetworkError("http", f"wigolo fetch 目标返回 HTTP {status}")
    err = data.get("error")
    if err:
        raise _NetworkError("provider", f"wigolo fetch 失败: {err}")
    markdown = (data.get("markdown") or "").strip()
    if not markdown:
        raise _NetworkError("provider", "wigolo fetch 未返回文本（可能空页/纯 JS 未渲染）")
    title = (data.get("title") or "").strip()
    if not title and markdown:
        # 中1（PC-C）：title 空时从 markdown 首 # 标题兜底（wigolo 路径无原始 HTML）
        m = re.search(r"(?m)^#\s+(.+)$", markdown)
        if m:
            title = _clean_text(m.group(1))
    return title, markdown




async def _wigolo_post_json(endpoint: str, payload: dict, timeout: int) -> dict:
    """wigolo REST POST（token 注入），返回解析后的 JSON dict；失败抛 _NetworkError。

    v3.5.0：research/agent 复用；schema 校验 400 映射为清晰契约错误（_fetch_raw 不保留 4xx body）。
    """
    base = _wigolo_base_url()
    if not base:
        raise _NetworkError("provider", "wigolo 未配置（WIGOLO_REST_URL）")
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": _UA,
    }
    token = _wigolo_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        _, resp = await _fetch_raw(f"{base}{endpoint}", timeout, data=body, headers=headers)
    except _NetworkError as e:
        if e.kind == "http" and "HTTP 400" in e.message:
            raise _NetworkError("provider", "wigolo 请求参数被拒（HTTP 400，请核对参数契约）")
        raise
    try:
        data = json.loads(resp.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        raise _NetworkError("provider", f"wigolo {endpoint} 响应解析失败")
    if not isinstance(data, dict):
        raise _NetworkError("provider", f"wigolo {endpoint} 响应格式异常")
    if data.get("ok") is False:
        raise _NetworkError("provider", f"wigolo {endpoint} 失败: {data.get('error', 'unknown')}")
    return data


async def _wigolo_research(
    question: str, depth: str, max_sources: int,
    include_domains: list[str], exclude_domains: list[str], timeout: int,
) -> dict:
    """wigolo 多步研究（REST /v1/research），返回原始 JSON（report/citations/sources）。"""
    payload: dict = {"question": question, "depth": depth, "max_sources": max_sources}
    if include_domains:
        payload["include_domains"] = include_domains
    if exclude_domains:
        payload["exclude_domains"] = exclude_domains
    return await _wigolo_post_json("/v1/research", payload, timeout)


async def _wigolo_agent(
    prompt: str, urls: list[str], max_pages: int, max_time_ms: int, timeout: int,
) -> dict:
    """wigolo 自主数据收集（REST /v1/agent），返回原始 JSON。"""
    payload: dict = {"prompt": prompt, "max_pages": max_pages, "max_time_ms": max_time_ms}
    if urls:
        payload["urls"] = urls
    return await _wigolo_post_json("/v1/agent", payload, timeout)


def _wigolo_mode_meta(data: dict) -> dict:
    """提取 research/agent 响应标准化元数据（report/citations/sources/heuristic）。"""
    # 冒烟发现：research 用 report 字段，agent 用 result 字段——两者兼容
    report = str(data.get("report") or data.get("result") or "")
    citations = data.get("citations") if isinstance(data.get("citations"), list) else []
    sources = data.get("sources") if isinstance(data.get("sources"), list) else []
    # 低3（PC-C）：优先显式字段；字符串回退用精确标记 "Summary (heuristic):" 避免误标
    heuristic = bool(data.get("heuristic")) or "summary (heuristic)" in report.lower()
    return {
        "report": report,
        "citations": citations,
        "sources": sources,
        "heuristic": heuristic,
        "sources_count": len(sources),
    }


def _wigolo_sanitize_citations(citations: list) -> tuple[list, int]:
    """响应层 SSRF 纵深防御：剔除内网/保留/非 http(s) 的 citations。

    v3.6.0：wigolo 抓取侧已内建 guardFetchUrl（默认拒私网），此处防结果回传内网地址
    （含 wigolo 放行的 loopback 与搜索源带回的内网链接）。兼容 dict（{"url":...}）与字符串元素。
    """
    clean: list = []
    removed = 0
    for c in citations or []:
        url = c.get("url") if isinstance(c, dict) else c
        if not isinstance(url, str):
            clean.append(c)  # 无 URL 的条目不参与校验，保留
            continue
        if _check_url(url):
            removed += 1
            continue
        clean.append(c)
    return clean, removed


# v3.17：Firecrawl v2 /agent 可选后端（锦上添花：结构化输出 + 模型档位；
# 默认 auto 优先本地免费的 wigolo，仅 wigolo 不可用或显式指定时启用）
_FIRECRAWL_AGENT_API = "https://api.firecrawl.dev/v2/agent"
_FIRECRAWL_AGENT_MODEL_DEFAULT = "spark-1-mini"


def _firecrawl_key() -> str:
    return os.environ.get("FIRECRAWL_API_KEY", "").strip()


def _firecrawl_agent_available() -> bool:
    return bool(_firecrawl_key())


def _agent_provider() -> str:
    p = (os.environ.get("WEB_AGENT_PROVIDER") or "auto").strip().lower()
    return p if p in ("auto", "wigolo", "firecrawl") else "auto"


async def _firecrawl_agent(prompt: str, urls: list[str], timeout: int) -> dict:
    """Firecrawl v2 /agent：返回 {result, sources}；失败抛 _NetworkError。"""
    key = _firecrawl_key()
    if not key:
        raise _NetworkError("provider", "Firecrawl 未配置（FIRECRAWL_API_KEY）")
    payload: dict = {"prompt": prompt, "model": _FIRECRAWL_AGENT_MODEL_DEFAULT}
    if urls:
        payload["urls"] = urls
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": _UA,
    }
    try:
        _, resp = await _fetch_raw(
            _FIRECRAWL_AGENT_API, timeout,
            data=json.dumps(payload).encode("utf-8"), headers=headers,
        )
    except _NetworkError as e:
        if e.kind == "http":
            raise _NetworkError("provider", f"Firecrawl agent HTTP 失败: {e.message}") from e
        raise
    try:
        data = json.loads(resp.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        raise _NetworkError("provider", "Firecrawl agent 响应解析失败") from None
    if not isinstance(data, dict) or data.get("success") is not True:
        msg = data.get("message", "unknown") if isinstance(data, dict) else "bad response"
        raise _NetworkError("provider", f"Firecrawl agent 失败: {msg}")
    d = data.get("data")
    if not isinstance(d, dict):
        raise _NetworkError("provider", "Firecrawl agent 响应缺少 data")
    result = str(d.get("result") or "")
    sources = d.get("sources") if isinstance(d.get("sources"), list) else []
    return {"result": result, "sources": sources}


def _firecrawl_sanitize_sources(sources: list) -> tuple[list, int]:
    """响应层 SSRF 纵深防御：仅保留带合法公开 http(s) URL 的 sources（PC-C LOW1：畸形元素统一剔除）。"""
    clean: list = []
    removed = 0
    for s in sources or []:
        url = s.get("url") if isinstance(s, dict) else s
        if not isinstance(url, str) or _check_url(url):
            removed += 1
            continue
        clean.append(s)
    return clean, removed


def _scrapling_fetcher():
    """懒加载 scrapling Fetcher；未安装返回 None（零硬依赖）。"""
    global _SCRAPLING_FETCHER
    if _SCRAPLING_FETCHER is None:
        try:
            from scrapling.fetchers import Fetcher
        except ImportError:
            return None
        _SCRAPLING_FETCHER = Fetcher
    return _SCRAPLING_FETCHER


def _reset_scrapling_cache() -> None:
    """测试/运维用：清空 scrapling 探测缓存（含浏览器路径），强制下次调用重新探测。"""
    global _scrapling_probed, _scrapling_browser_probed
    _scrapling_probed = False
    _scrapling_available_cache = False
    _scrapling_browser_probed = False
    _scrapling_browser_cache = False


async def _scrapling_available() -> bool:
    """探测 scrapling 是否可 import（进程内缓存；未安装=False，零硬依赖）。"""
    global _scrapling_probed
    if _scrapling_probed:
        return _scrapling_available_cache
    _scrapling_probed = True
    available = _scrapling_fetcher() is not None
    _scrapling_available_cache = available
    if available:
        logger.info("🌐 scrapling provider 探测成功（Fetcher 可用）")
    else:
        logger.info("🌐 scrapling provider 探测失败（保持现有链，零硬依赖）")
    return available


async def _scrapling_browser_available() -> bool:
    """探测 scrapling 浏览器路径是否可用（chromium 已装；进程内缓存，不启动浏览器）。"""
    global _scrapling_browser_probed
    if _scrapling_browser_probed:
        return _scrapling_browser_cache
    _scrapling_browser_probed = True
    available = False
    try:
        env = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()
        root = Path(env) if env else Path.home() / "AppData" / "Local" / "ms-playwright"
        if root.is_dir():
            for d in root.glob("chromium-*"):
                if os.name == "nt":
                    exe = d / "chrome-win64" / "chrome.exe"
                    if not exe.exists():
                        exe = d / "chrome-win" / "chrome.exe"
                else:
                    exe = d / "chrome-linux" / "chrome"
                if exe.exists():
                    available = True
                    break
        if available:
            # 低3（PC-C）：二进制存在 ≠ 可导入——同时校验 StealthyFetcher/DynamicFetcher
            try:
                from scrapling.fetchers import DynamicFetcher, StealthyFetcher  # noqa: F401
            except (ImportError, OSError):
                available = False
    except OSError:
        available = False
    _scrapling_browser_cache = available
    if available:
        logger.info("🌐 scrapling 浏览器路径探测成功（chromium 已装）")
    else:
        logger.info("🌐 scrapling 浏览器路径不可用（chromium 未装，保持 HTTP 层）")
    return available


def _scrapling_parse_page(page, url: str) -> tuple[str, str, str]:
    """解析 scrapling Page/Response → (final_url, title, markdown)。

    冒烟发现（2026-08-06）：status 类型不稳定（200=str('200')、4xx=int）；
    page.text 恒为空，原始 HTML 在 page.body（bytes）/ page.html_content（str）。
    """
    status = getattr(page, "status", None)
    status_int = None
    if isinstance(status, int):
        status_int = status
    elif isinstance(status, str) and status.strip().isdigit():
        status_int = int(status.strip())
    if status_int is not None and status_int >= 400:
        kind = "rate_limited" if status_int == 429 else "http"
        raise _NetworkError(kind, f"scrapling fetch 目标返回 HTTP {status_int}")
    raw = ""
    html_content = getattr(page, "html_content", None)
    if isinstance(html_content, str) and html_content.strip():
        raw = html_content
    else:
        body = getattr(page, "body", None)
        if isinstance(body, (bytes, bytearray)):
            enc = getattr(page, "encoding", None) or "utf-8"
            try:
                raw = bytes(body).decode(enc, errors="replace")
            except (LookupError, TypeError):
                raw = bytes(body).decode("utf-8", errors="replace")
    if not raw.strip():
        raise _NetworkError("provider", "scrapling fetch 未返回内容（可能空页）")
    if len(raw.encode("utf-8")) > _FETCH_MAX_BYTES:
        raise _NetworkError("too_large", "scrapling fetch 页面超过 10MB 上限")
    page_url = getattr(page, "url", None)
    if page_url:
        final_url = page_url
    else:
        logger.warning(f"⚠️ scrapling page.url 不可得，无法重定向复查（回退原 URL）: {url[:120]}")
        final_url = url
    title = _extract_title(raw) or _extract_h1(raw)
    markdown = _extract_body(raw)
    if not markdown:
        raise _NetworkError("provider", "scrapling fetch 未提取到正文（可能纯 JS/空壳）")
    return final_url, title, markdown


async def _scrapling_browser_fetch(url: str, timeout: int) -> tuple[str, str, str]:
    """scrapling 浏览器路径抓取（StealthyFetcher → DynamicFetcher 回退），返回 (final_url, title, markdown)。

    v3.4.0：补 HTTP 层过不了的 JS 挑战/动态渲染；预算 max(timeout,45)s（浏览器慢，实测 12-45s；
    scrapling timeout 单位=毫秒）。注意：浏览器执行页面 JS，页面可对任意地址发请求
    （JS 驱动 SSRF 残余，见设计 §3.3）。
    """
    budget_ms = max(timeout, 45) * 1000
    first_err: Exception | None = None
    try:
        from scrapling.fetchers import StealthyFetcher

        # 低2（PC-C）：浏览器进程隔离，独立小信号量防多 chromium 吃内存
        async with _SEM_BROWSER:
            page = await asyncio.to_thread(
                StealthyFetcher.fetch, url,
                headless=True, network_idle=True, timeout=budget_ms, solve_cloudflare=True,
            )
        return _scrapling_parse_page(page, url)
    except _NetworkError:
        raise
    except Exception as e:
        first_err = e
        logger.warning(f"🔁 StealthyFetcher 失败，回退 DynamicFetcher: {str(e)[:150]}")
    try:
        from scrapling.fetchers import DynamicFetcher

        async with _SEM_BROWSER:
            page = await asyncio.to_thread(
                DynamicFetcher.fetch, url,
                headless=True, network_idle=True, timeout=budget_ms,
            )
        return _scrapling_parse_page(page, url)
    except _NetworkError:
        raise
    except Exception as e:
        raise _NetworkError("network", f"scrapling 浏览器路径抓取失败: {str(first_err or e)[:200]}")


async def _scrapling_fetch(url: str, timeout: int) -> tuple[str, str, str, str]:
    """scrapling 抓取（HTTP 层 → 浏览器升级），返回 (final_url, title, markdown, fetch_method)。

    v3.3.0：SSRF 由调用方 _check_url 前置；curl_cffi 跟随重定向，page.url 应为最终 URL，
    调用方据此做重定向后复查（优于 wigolo 无法复查的已知边界）。
    v3.4.0：HTTP 失败（403/空正文/网络错）→ 浏览器可用时升级 StealthyFetcher → DynamicFetcher。
    """
    fetcher = _scrapling_fetcher()
    if fetcher is None:
        raise _NetworkError("provider", "scrapling 未安装（需 pip install \"scrapling[fetchers]\"）")
    impersonate = (
        os.environ.get("SCRAPLING_IMPERSONATE", "").strip()
        or _SCRAPLING_IMPERSONATE_DEFAULT
    )
    last_err: _NetworkError | None = None
    try:
        # 低1（PC-C）：纳入 Semaphore(4)，与 stdlib/wigolo 并发闸一致
        async with _SEM:
            page = await asyncio.to_thread(
                fetcher.get, url, timeout=timeout, impersonate=impersonate
            )
        final_url, title, markdown = _scrapling_parse_page(page, url)
        return final_url, title, markdown, "http"
    except _NetworkError as e:
        last_err = e
        logger.info(f"🔁 scrapling HTTP 层失败: {e.message}")
    except Exception as e:
        msg = str(e) or type(e).__name__
        kind = "timeout" if "timeout" in msg.lower() else "network"
        last_err = _NetworkError(kind, f"scrapling fetch 网络失败: {msg[:200]}")
        logger.warning(f"🔁 scrapling HTTP 层异常: {last_err.message}")
    # v3.4.0：浏览器升级（StealthyFetcher → DynamicFetcher）
    if await _scrapling_browser_available():
        try:
            final_url, title, markdown = await _scrapling_browser_fetch(url, timeout)
            return final_url, title, markdown, "browser"
        except _NetworkError as e:
            # 中1（PC-C）：浏览器失败不覆盖 last_err——保留 HTTP 层原始错误优先
            if last_err is None:
                last_err = e
            logger.warning(f"🔁 scrapling 浏览器路径失败: {e.message}")
    if last_err is not None:
        raise last_err
    raise _NetworkError("network", "scrapling 抓取失败")



async def _bing_search(query: str, limit: int, timeout: int) -> list[dict]:
    """Bing 网页搜索（免费、无 key；HTML 轻量解析，结果块 <h2><a>）。"""
    params = urllib.parse.urlencode({"q": query, "count": limit})
    _, body = await _fetch_raw(
        f"{_BING_SEARCH}?{params}",
        timeout,
        headers={
            "User-Agent": _UA,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        },
    )
    raw = body.decode("utf-8", errors="replace")
    results: list[dict] = []
    seen: set[str] = set()
    for m in re.finditer(
        r'(?is)<h2[^>]*><a[^>]*href="(https?://[^"]+)"[^>]*>(.*?)</a>',
        raw,
    ):
        url = m.group(1)
        if url in seen:
            continue
        seen.add(url)
        title = _clean_text(html.unescape(re.sub(r"<[^>]+>", " ", m.group(2))))
        block_end = raw.find("<li", m.end())
        block = raw[m.start(): block_end if block_end != -1 else m.end() + 2000]
        snip_m = re.search(r"(?is)<p[^>]*>(.*?)</p>", block)
        snippet = _clean_text(html.unescape(re.sub(r"<[^>]+>", " ", snip_m.group(1)))) if snip_m else ""
        cite_m = re.search(r"(?is)<cite[^>]*>(.*?)</cite>", block)
        source = _clean_text(html.unescape(re.sub(r"<[^>]+>", " ", cite_m.group(1)))) if cite_m else _host_of(url)
        results.append({
            "title": title,
            "url": url,
            "snippet": snippet,
            "source": source,
        })
        if len(results) >= limit:
            break
    return results


async def _ddg_search(query: str, limit: int, timeout: int) -> list[dict]:
    """DuckDuckGo Instant Answer（部分网络可达）；把 JSON 规范化为统一结果结构。"""
    data = await _ddg_json(query, timeout)
    results: list[dict] = []
    abstract_url = (data.get("AbstractURL") or "").strip()
    if abstract_url:
        results.append({
            "title": (data.get("Heading") or "").strip() or abstract_url,
            "url": abstract_url,
            "snippet": (data.get("AbstractText") or "").strip(),
            "source": _host_of(abstract_url),
        })
    for item in data.get("RelatedTopics") or []:
        if not isinstance(item, dict):
            continue
        u = (item.get("FirstURL") or "").strip()
        if not u:
            continue
        text = (item.get("Text") or "").strip()
        title, sep, snippet = text.partition(" - ")
        if not sep:
            title, snippet = _host_of(u), text
        results.append({
            "title": (title or _host_of(u)).strip(),
            "url": u,
            "snippet": (snippet or "").strip(),
            "source": _host_of(u),
        })
    return results[:limit]


async def _firecrawl_search(query: str, limit: int, timeout: int) -> list[dict]:
    """Firecrawl /v1/search（按量付费，需 FIRECRAWL_API_KEY）。"""
    api_key = os.environ.get("FIRECRAWL_API_KEY", "").strip()
    if not api_key:
        raise _NetworkError("provider", "未配置 FIRECRAWL_API_KEY")
    payload = json.dumps({"query": query, "limit": limit}).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": _UA,
    }
    _, body = await _fetch_raw(_FIRECRAWL_SEARCH_API, timeout, data=payload, headers=headers)
    try:
        data = json.loads(body.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        raise _NetworkError("provider", "Firecrawl 响应解析失败")
    if not data.get("success"):
        err = data.get("error") or data.get("message") or "unknown"
        raise _NetworkError("provider", f"Firecrawl 调用失败: {err}")
    results: list[dict] = []
    for item in (data.get("data") or [])[:limit]:
        url = (item.get("url") or "").strip()
        if not url:
            continue
        results.append({
            "title": (item.get("title") or "").strip(),
            "url": url,
            "snippet": (item.get("description") or "").strip(),
            "source": _host_of(url),
        })
    return results


async def _search_with_fallback(query: str, limit: int, timeout: int) -> tuple[str, list[dict]]:
    """按降级链依次尝试搜索后端，返回 (实际 provider, 规范化结果)。"""
    forced = os.environ.get("WEB_SEARCH_PROVIDER", "").strip().lower()
    all_providers = ("wigolo",) + _PROVIDER_ORDER
    if forced in all_providers:
        order = [forced] + [p for p in all_providers if p != forced]
    else:
        order = list(all_providers)
    # v3.1.0：wigolo 可选 provider——探测不可用/未配置即从链中移除（与 firecrawl 无 key 语义一致）
    if "wigolo" in order and not await _wigolo_available():
        order.remove("wigolo")
    if "firecrawl" in order and not os.environ.get("FIRECRAWL_API_KEY", "").strip():
        order.remove("firecrawl")
    errors: list[str] = []
    last_provider = ""
    for prov in order:
        try:
            if prov == "bing":
                results = await _bing_search(query, limit, timeout)
            elif prov == "duckduckgo":
                results = await _ddg_search(query, limit, timeout)
            elif prov == "wigolo":
                results = await _wigolo_search(query, limit, timeout)
            else:
                results = await _firecrawl_search(query, limit, timeout)
            last_provider = prov
            if results:
                return prov, results
            # PC-B 验证 L2：空结果视为「无命中」，继续降级到下一个后端
            logger.info(f"🔁 搜索后端 {prov} 返回空结果，尝试下一个")
        except _NetworkError as e:
            errors.append(f"{prov}: {e.message}")
            logger.warning(f"🔁 搜索后端 {prov} 不可用，降级到下一个: {e.message}")
    if not last_provider:
        raise _NetworkError(
            "network",
            "所有搜索后端均失败: " + " | ".join(errors),
        )
    return last_provider, []


async def _firecrawl_markdown(url: str, timeout: int, api_key: str) -> dict:
    """调用 Firecrawl /v1/scrape 抓取网页转 markdown。"""
    payload = json.dumps({"url": url, "formats": ["markdown"]}).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": _UA,
    }
    _, body = await _fetch_raw(_FIRECRAWL_API, timeout, data=payload, headers=headers)
    try:
        data = json.loads(body.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        raise _NetworkError("provider", "Firecrawl 响应解析失败")
    if not data.get("success"):
        err = data.get("error") or data.get("message") or "unknown"
        raise _NetworkError("provider", f"Firecrawl 调用失败: {err}")
    d = data.get("data") or {}
    meta = d.get("metadata") or {}
    return {
        "title": (meta.get("title") or d.get("title") or "").strip(),
        "markdown": (d.get("markdown") or "").strip(),
    }


async def _stdlib_fetch(url: str, timeout: int) -> tuple[str, bytes]:
    """标准库抓取（零硬依赖兜底），返回 (最终 URL, 响应体)。"""
    return await _fetch_raw(
        url,
        timeout,
        headers={
            "User-Agent": _UA,
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        },
    )


def _extract_title(raw: str) -> str:
    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", raw)
    if not m:
        return ""
    return html.unescape(re.sub(r"\s+", " ", m.group(1))).strip()


def _extract_text(raw: str) -> str:
    """轻量 HTML → 文本：去 script/style、剥标签、解实体、压空白。"""
    s = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", raw)
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def _extract_h1(raw: str) -> str:
    """从 <h1> 提取标题（<title> 缺失时的兜底）。"""
    m = re.search(r"(?is)<h1[^>]*>(.*?)</h1>", raw)
    if not m:
        return ""
    return _clean_text(html.unescape(re.sub(r"(?s)<[^>]+>", " ", m.group(1))))


def _strip_junk(raw: str) -> str:
    """去除非正文噪声：脚本/样式/导航/页脚/侧栏/表单/广告容器/隐藏元素/注释。

    保留 <header>（可能含文章 h1），交给打分阶段决定取舍。
    """
    s = re.sub(r"(?is)<!--.*?-->", " ", raw)
    s = _JUNK_TAG_RE.sub(" ", s)
    # 隐藏元素（hidden 属性 / display:none / visibility:hidden）；循环兜嵌套
    for _ in range(3):
        before = s
        s = _HIDDEN_EL_RE.sub(" ", s)
        if s == before:
            break
    return s


def _score_blocks(cleaned: str) -> list[dict]:
    """按块级标签提取候选正文块并打分（文档序）。

    value = 文本长度 × (1 − 链接密度) × (1 + min(标点密度, 0.2))
    - 链接密度高（导航/侧栏）→ value 趋近 0；
    - 散文标点多 → 加权。
    """
    blocks: list[dict] = []
    for m in _BLOCK_TAG_RE.finditer(cleaned):
        tag = m.group(1).lower()
        inner = m.group(2)
        if tag == "pre":
            # 代码块保留换行
            text = html.unescape(re.sub(r"(?s)<[^>]+>", "", inner)).strip()
            link_len = 0
        else:
            text = _clean_text(html.unescape(re.sub(r"(?s)<[^>]+>", " ", inner)))
            link_len = 0
            for lm in _LINK_TEXT_RE.finditer(inner):
                lt = _clean_text(html.unescape(re.sub(r"(?s)<[^>]+>", " ", lm.group(1))))
                link_len += len(lt)
        if not text:
            continue
        text_len = len(text)
        link_density = link_len / max(1, text_len)
        punct_density = len(_PUNCT_RE.findall(text)) / max(1, text_len)
        value = text_len * (1 - link_density) * (1 + min(punct_density, 0.2))
        blocks.append({
            "tag": tag,
            "text": text,
            "text_len": text_len,
            "link_density": link_density,
            "value": value,
            "start": m.start(),
            "end": m.end(),
        })
    return blocks


def _best_run(blocks: list[dict]) -> list[dict]:
    """取文档序中「累计 value 最高」的连续块区间（块间空隙 < _GAP_LIMIT）。

    低价值块（value<=0，多为导航/广告）打断区间。
    """
    best: list[dict] = []
    best_value = 0.0
    cur: list[dict] = []
    cur_value = 0.0
    prev_end: int | None = None
    for b in blocks:
        if b["value"] <= 0:
            if cur_value > best_value:
                best, best_value = cur, cur_value
            cur, cur_value = [], 0.0
            prev_end = None
            continue
        if prev_end is not None and b["start"] - prev_end > _GAP_LIMIT:
            if cur_value > best_value:
                best, best_value = cur, cur_value
            cur, cur_value = [], 0.0
        cur.append(b)
        cur_value += b["value"]
        prev_end = b["end"]
    if cur_value > best_value:
        best, best_value = cur, cur_value
    return best


def _prepend_heading(run: list[dict], blocks: list[dict]) -> list[dict]:
    """区间前最近的低链接 h1/h2 前置为正文首标题（供 search_documents 取 title）。"""
    if not run:
        return run
    run_start = run[0]["start"]
    cands = [
        b for b in blocks
        if b["tag"] in ("h1", "h2")
        and b["link_density"] < 0.5
        and b["end"] <= run_start
        and run_start - b["end"] < 5000
    ]
    if not cands:
        return run
    best = max(cands, key=lambda b: (b["tag"] == "h1", b["end"]))
    if any(b is best for b in run):
        return run
    return [best] + run


def _compose_body(run: list[dict]) -> str:
    """把选中的块组成 Markdown：标题 → #、列表 → -、代码 → 围栏、段落空行分隔。"""
    lines: list[str] = []
    for b in run:
        tag = b["tag"]
        if tag.startswith("h"):
            level = int(tag[1])
            lines.append(f"{'#' * level} {b['text']}")
        elif tag == "li":
            lines.append(f"- {b['text']}")
        elif tag == "pre":
            lines.append("```\n" + b["text"] + "\n```")
        else:
            lines.append(b["text"])
    return "\n\n".join(lines).strip()


def _extract_body(raw: str) -> str:
    """打分式正文抽取（v2.9.0）：去噪 → 分块打分 → 取最优连续区间 → 组 Markdown。

    打分结果为空/总长过短（< _FALLBACK_MIN_LEN）时回退旧 _extract_text
    （全文本剥标签），兼容简单页面。
    """
    cleaned = _strip_junk(raw)
    blocks = _score_blocks(cleaned)
    run = _best_run(blocks)
    run = _prepend_heading(run, blocks)
    body = _compose_body(run)
    total_len = sum(b["text_len"] for b in run)
    if total_len < _FALLBACK_MIN_LEN or not body:
        return _extract_text(raw)
    return body


async def web_search(
    query: str = "",
    max_results: int = _DEFAULT_MAX_RESULTS,
    timeout: int = 15,
) -> str:
    """外部互联网搜索（多后端降级链），返回规范化摘要结果。"""
    denied = assert_identity_allowed("web_search")
    if denied:
        return fail(denied)
    q = (query or "").strip()
    if not q:
        return fail("web_search 需要非空 query")
    q = unicodedata.normalize("NFKC", q)
    terms = [t for t in q.split() if t.strip()]
    terms = [t for t in terms if not _is_pure_punct(t)]
    if not terms:
        return fail("query 不含有效检索词（仅标点）")
    limit = _clamp_int(max_results, 1, _MAX_RESULTS_MAX, _DEFAULT_MAX_RESULTS)
    timeout = _clamp_int(timeout, _TIMEOUT_MIN, _TIMEOUT_MAX, 15)

    try:
        provider, results = await _search_with_fallback(q, limit, timeout)
    except _NetworkError as e:
        return fail(e.message)

    results = results[:limit]  # 防御性截断：后端实现差异时保持 max_results 契约
    ranked = []
    for i, item in enumerate(results, 1):
        ranked.append({**item, "rank": i})
    logger.info(f"🌐 外部检索: query={q!r} provider={provider} 命中 {len(ranked)} 条")
    return ok({
        "count": len(ranked),
        "query": q,
        "provider": provider,
        "results": ranked,
    })


async def web_fetch(
    url: str = "",
    timeout: int = 20,
    save: bool = True,
) -> str:
    """抓取网页转文本；有 FIRECRAWL_API_KEY 用 Firecrawl，否则 stdlib → scrapling → wigolo 兜底。产物默认入库 documents/web/。"""
    denied = assert_identity_allowed("web_fetch")
    if denied:
        return fail(denied)
    u = (url or "").strip()
    if not u:
        return fail("web_fetch 需要非空 url")
    err = _check_url(u)
    if err:
        return fail(err)
    timeout = _clamp_int(timeout, _TIMEOUT_MIN, _TIMEOUT_MAX, 20)

    api_key = os.environ.get("FIRECRAWL_API_KEY", "").strip()
    forced_fetch = os.environ.get("WEB_FETCH_PROVIDER", "").strip().lower()
    fetch_method = "http"  # v3.4.0：scrapling 浏览器升级后为 "browser"，extra_meta 审计
    if forced_fetch and forced_fetch not in ("firecrawl", "wigolo", "stdlib", "scrapling"):
        return fail(f"未知 WEB_FETCH_PROVIDER: {forced_fetch}")
    try:
        if forced_fetch:
            if forced_fetch == "firecrawl":
                if not api_key:
                    return fail("web_fetch 强制 firecrawl 但未设置 FIRECRAWL_API_KEY")
                meta = await _firecrawl_markdown(u, timeout, api_key)
                title, markdown, provider = meta["title"], meta["markdown"], "firecrawl"
            elif forced_fetch == "wigolo":
                if not await _wigolo_available():
                    return fail("web_fetch 强制 wigolo 但服务不可用（WIGOLO_REST_URL 未配置或 /health 失败）")
                # 强制/兜底统一放宽预算（wigolo 对普通页也约 33s 实测）
                title, markdown = await _wigolo_fetch(u, max(timeout, 45))
                provider = "wigolo"
            elif forced_fetch == "scrapling":
                if not await _scrapling_available():
                    return fail("web_fetch 强制 scrapling 但不可用（未安装 scrapling[fetchers]）")
                final_url, title, markdown, fetch_method = await _scrapling_fetch(u, timeout)
                # 重定向后的最终 URL 也要过 SSRF 校验（curl_cffi 跟随重定向，page.url=最终 URL）
                err2 = _check_url(final_url)
                if err2:
                    return fail(f"重定向目标被拒绝: {err2}")
                provider = "scrapling"
            else:
                final_url, body = await _stdlib_fetch(u, timeout)
                # 重定向后的最终 URL 也要过 SSRF 校验（防跳转到内网）
                err2 = _check_url(final_url)
                if err2:
                    return fail(f"重定向目标被拒绝: {err2}")
                raw = body.decode("utf-8", errors="replace")
                # v2.9.0：打分式正文抽取（去导航/页脚/广告，保留标题与正文）
                title = _extract_title(raw) or _extract_h1(raw)
                markdown = _extract_body(raw)
                provider = "stdlib"
        elif api_key:
            meta = await _firecrawl_markdown(u, timeout, api_key)
            title, markdown, provider = meta["title"], meta["markdown"], "firecrawl"
        else:
            # 自动顺序（无 firecrawl key）：stdlib 快速路径（v2.9 打分式抽取，秒级）
            # → scrapling 兜底（v3.3.0：HTTP 层反爬，curl_cffi TLS 指纹，秒级）
            # → wigolo 兜底（v3.2.0：反爬/动态页/纯 JS，预算放宽至 max(timeout, 45)s）
            stdlib_err = None
            fallback_err = None
            try:
                final_url, body = await _stdlib_fetch(u, timeout)
                # 重定向后的最终 URL 也要过 SSRF 校验（防跳转到内网）
                err2 = _check_url(final_url)
                if err2:
                    return fail(f"重定向目标被拒绝: {err2}")
                raw = body.decode("utf-8", errors="replace")
                # v2.9.0：打分式正文抽取（去导航/页脚/广告，保留标题与正文）
                title = _extract_title(raw) or _extract_h1(raw)
                markdown = _extract_body(raw)
                provider = "stdlib"
            except _NetworkError as e:
                stdlib_err = e
                title = ""
                markdown = None
            if stdlib_err is not None or not markdown:
                # 第一级兜底：scrapling（HTTP 层反爬，秒级 → v3.4.0 浏览器升级；不可用则跳过）
                scrapling_avail = False
                if await _scrapling_available():
                    scrapling_avail = True
                    try:
                        final_url, title, markdown, fetch_method = await _scrapling_fetch(u, timeout)
                        # 重定向后的最终 URL 复查（curl_cffi page.url=最终 URL，优于 wigolo）
                        err2 = _check_url(final_url)
                        if err2:
                            return fail(f"重定向目标被拒绝: {err2}")
                        provider = "scrapling"
                    except _NetworkError as e:
                        logger.warning(f"🔁 scrapling 兜底失败: {e.message}")
                        fallback_err = e
                        title = ""
                        markdown = None
                # 第二级兜底：wigolo——仅当 scrapling 不可用或缺少浏览器路径时尝试
                # （v3.4.0：scrapling 含浏览器时覆盖浏览器级反爬，避免 4 级串行长尾）
                if not markdown and (not scrapling_avail or not await _scrapling_browser_available()) and await _wigolo_available():
                    try:
                        title, markdown = await _wigolo_fetch(u, max(timeout, 45))
                        provider = "wigolo"
                    except _NetworkError as e:
                        logger.warning(f"🔁 wigolo fetch 兜底失败: {e.message}")
                        if stdlib_err is not None:
                            return fail(stdlib_err.message)
                        return fail(e.message)
            if not markdown:
                if stdlib_err is not None:
                    return fail(stdlib_err.message)
                # 低3（PC-C）：stdlib 空正文 + scrapling 失败时保留 scrapling 具体错误
                if fallback_err is not None:
                    return fail(fallback_err.message)
                return fail("未能从网页提取到文本（可能是空页或纯 JS 页面）")
    except _NetworkError as e:
        return fail(e.message)

    if not markdown:
        return fail("未能从网页提取到文本（可能是空页或纯 JS 页面）")
    markdown = unicodedata.normalize("NFKC", markdown)
    chars = len(markdown)
    preview = markdown[:_PREVIEW_CHARS]
    host = _host_of(u)
    fetched_at = now_iso()
    # v2.9.0：内容指纹 = sha256_16(NFKC 清洗后正文)，而非源文件字节；
    # URL 归一（去 fragment/跟踪参数）用于同页不同参数的重复识别
    dedup_key = hashlib.sha256(markdown.encode("utf-8")).hexdigest()[:16]
    norm_url = normalize_url(u)

    saved = None
    if save:
        safe_host = re.sub(r"[^A-Za-z0-9._-]+", "_", host) or "web"
        tmp_dir = Path(tempfile.mkdtemp(prefix="collab_web_"))
        tmp_file = tmp_dir / f"{safe_host}.md"
        try:
            tmp_file.write_text(markdown, encoding="utf-8")
            res = await add_document(
                str(tmp_file),
                target_dir="documents/web",
                skip_duplicates=True,
                dedup_key=dedup_key,
                dedup_url=norm_url,
                extra_meta={
                    "title": title,
                    "url": u,
                    "host": host,
                    "provider": provider,
                    "fetch_method": fetch_method,  # v3.4.0：http|browser（scrapling 浏览器升级审计）
                    "fetched_at": fetched_at,
                    "body_length": chars,
                },
            )
            parsed = json.loads(res) if isinstance(res, str) else res
            if not parsed.get("success"):
                return fail(f"网页入库失败: {parsed.get('error', '未知错误')}")
            saved = {
                "path": str(parsed.get("path") or "").replace("\\", "/"),
                "filename": parsed.get("filename"),
                "duplicate": bool(parsed.get("duplicate", False)),
                "duplicate_count": int(parsed.get("duplicate_count") or 0),
                "session_duplicates": int(parsed.get("session_duplicates") or 0),
                "dedup_key": dedup_key,
            }
        finally:
            try:
                tmp_file.unlink(missing_ok=True)
                tmp_dir.rmdir()
            except OSError:
                pass

    logger.info(f"🌐 网页抓取: url={u[:120]} provider={provider} chars={chars} saved={bool(saved)}")
    return ok({
        "url": u,
        "normalized_url": norm_url,
        "title": title,
        "provider": provider,
        "fetch_method": fetch_method,  # v3.4.0：scrapling 浏览器升级审计
        "host": host,
        "fetched_at": fetched_at,
        "chars": chars,
        "body_length": chars,
        "estimated_tokens": max(1, round(chars / 4)),
        "truncated": chars > _PREVIEW_CHARS,
        "markdown_preview": preview,
        "dedup_key": dedup_key,
        "saved": saved,
    })
async def web_research(
    question: str = "",
    depth: str = "standard",
    max_sources: int = 10,
    include_domains: str = "",
    exclude_domains: str = "",
    timeout: int = 90,
) -> str:
    """wigolo 多步研究简报（agentic research）。

    需要 wigolo serve（WIGOLO_REST_URL 或默认 127.0.0.1:3333）。无 LLM 配置时返回 heuristic 兜底简报
    （零 API 成本）；配 ANTHROPIC_API_KEY/OPENAI_API_KEY 等或 WIGOLO_LOCAL_LLM 后为 LLM 综合简报
    （会消耗 LLM token，wigolo 有缓存、per-request ≤1 call）。
    """
    denied = assert_identity_allowed("web_research")
    if denied:
        return fail(denied)
    q = (question or "").strip()
    if not q:
        return fail("web_research 需要非空 question")
    depth = (depth or "standard").strip().lower()
    if depth not in ("quick", "standard", "comprehensive"):
        depth = "standard"
    max_sources = _clamp_int(max_sources, 1, 50, 10)
    include = [d.strip() for d in (include_domains or "").split(",") if d.strip()]
    exclude = [d.strip() for d in (exclude_domains or "").split(",") if d.strip()]
    budget = {"quick": 45, "standard": 90, "comprehensive": 150}[depth]  # 冒烟：中文/百度源可 >60s
    # v3.5.0：预算为生效下限，调用方可加长不可压短
    timeout = max(_clamp_int(timeout, 20, 240, 60), budget)
    # v3.17：Firecrawl v2 /agent 可选后端（显式 WEB_AGENT_PROVIDER=firecrawl 才启用；
    # 默认 auto 保持 wigolo 行为不变）
    provider = _agent_provider()
    if provider == "firecrawl" and not _firecrawl_agent_available():
        return fail("provider=firecrawl 但未配置 FIRECRAWL_API_KEY")
    if provider == "firecrawl":
        try:
            fdata = await _firecrawl_agent(q, [], timeout)
        except _NetworkError as e:
            return fail(e.message)
        freport = fdata["result"]
        if not freport:
            return fail("Firecrawl agent 返回空结果（无报告）")
        citations, ssrf_removed = _firecrawl_sanitize_sources(fdata["sources"])
        if ssrf_removed:
            logger.warning(f"🛡️ SSRF 防护: web_research 剔除 {ssrf_removed} 条内网 sources")
        logger.info(f"🔍 web_research: question={q[:120]} provider=firecrawl depth={depth}")
        return ok({
            "provider": "firecrawl",
            "mode": "research",
            "depth": depth,
            "report": freport[:_PREVIEW_CHARS],
            "truncated": len(freport) > _PREVIEW_CHARS,
            "citations": citations[:20],
            "ssrf_removed": ssrf_removed,
            "sources_count": len(fdata["sources"]),
            "heuristic": False,
            "chars": len(freport),
            "estimated_tokens": max(1, round(len(freport) / 4)),
        })
    if not await _wigolo_available():
        return fail("web_research 需要 wigolo serve（WIGOLO_REST_URL 未配置或 /health 失败）")
    try:
        data = await _wigolo_research(q, depth, max_sources, include, exclude, timeout)
    except _NetworkError as e:
        return fail(e.message)
    meta = _wigolo_mode_meta(data)
    report = meta["report"]
    # 低1（PC-C）：空 report 且非 heuristic 兜底 → 明确失败，不静默空成功
    if not report and not meta["heuristic"]:
        return fail("wigolo 返回空结果（无报告且非 heuristic 兜底，可能无匹配来源或服务异常）")
    # 低2（PC-C）：activity 日志（脱敏截断 120）
    citations, ssrf_removed = _wigolo_sanitize_citations(meta["citations"])
    if ssrf_removed:
        logger.warning(f"🛡️ SSRF 防护: web_research 剔除 {ssrf_removed} 条内网/保留 citations")
    logger.info(f"🔍 web_research: question={q[:120]} depth={depth} sources={meta['sources_count']}")
    return ok({
        "provider": "wigolo",
        "mode": "research",
        "depth": depth,
        "report": report[:_PREVIEW_CHARS],
        "truncated": len(report) > _PREVIEW_CHARS,
        "citations": citations[:20],
        "ssrf_removed": ssrf_removed,
        "sources_count": meta["sources_count"],
        "heuristic": meta["heuristic"],
        "chars": len(report),
        "estimated_tokens": max(1, round(len(report) / 4)),
    })


async def web_agent(
    prompt: str = "",
    urls: str = "",
    max_pages: int = 5,
    max_time_ms: int = 30000,
    timeout: int = 90,
) -> str:
    """wigolo 自主数据收集（agentic gathering）。

    需要 wigolo serve。urls 为可选种子 URL（逗号分隔，逐个过 SSRF 校验）；agent 内部会自主抓取页面
    （自主爬取 SSRF 残余见设计 §3.4）。无 LLM 配置时返回 heuristic 兜底结果（零 API 成本）。
    """
    denied = assert_identity_allowed("web_agent")
    if denied:
        return fail(denied)
    p = (prompt or "").strip()
    if not p:
        return fail("web_agent 需要非空 prompt")
    max_pages = _clamp_int(max_pages, 1, 100, 5)
    max_time_ms = _clamp_int(max_time_ms, 5000, 120000, 30000)
    seed_urls: list[str] = []
    for u in (urls or "").split(","):
        u = u.strip()
        if not u:
            continue
        err = _check_url(u)
        if err:
            return fail(f"web_agent 种子 URL 被拒绝: {err}")
        seed_urls.append(u)
    budget = max(max_time_ms // 1000 + 10, 60)
    # v3.5.0：预算为生效下限，调用方可加长不可压短
    timeout = max(_clamp_int(timeout, 20, 240, 60), budget)
    # v3.17：Firecrawl v2 /agent 可选后端（显式 WEB_AGENT_PROVIDER=firecrawl 才启用）
    provider = _agent_provider()
    if provider == "firecrawl" and not _firecrawl_agent_available():
        return fail("provider=firecrawl 但未配置 FIRECRAWL_API_KEY")
    if provider == "firecrawl":
        try:
            fdata = await _firecrawl_agent(p, seed_urls, timeout)
        except _NetworkError as e:
            return fail(e.message)
        freport = fdata["result"]
        if not freport:
            return fail("Firecrawl agent 返回空结果（无报告）")
        citations, ssrf_removed = _firecrawl_sanitize_sources(fdata["sources"])
        if ssrf_removed:
            logger.warning(f"🛡️ SSRF 防护: web_agent 剔除 {ssrf_removed} 条内网 sources")
        logger.info(f"🔍 web_agent: prompt={p[:120]} provider=firecrawl pages={max_pages}")
        return ok({
            "provider": "firecrawl",
            "mode": "agent",
            "report": freport[:_PREVIEW_CHARS],
            "truncated": len(freport) > _PREVIEW_CHARS,
            "citations": citations[:20],
            "ssrf_removed": ssrf_removed,
            "sources_count": len(fdata["sources"]),
            "heuristic": False,
            "chars": len(freport),
            "estimated_tokens": max(1, round(len(freport) / 4)),
        })
    if not await _wigolo_available():
        return fail("web_agent 需要 wigolo serve（WIGOLO_REST_URL 未配置或 /health 失败）")
    try:
        data = await _wigolo_agent(p, seed_urls, max_pages, max_time_ms, timeout)
    except _NetworkError as e:
        return fail(e.message)
    meta = _wigolo_mode_meta(data)
    report = meta["report"]
    # 低1（PC-C）：空 report 且非 heuristic 兜底 → 明确失败，不静默空成功
    if not report and not meta["heuristic"]:
        return fail("wigolo 返回空结果（无报告且非 heuristic 兜底，可能无匹配来源或服务异常）")
    # 低2（PC-C）：activity 日志（脱敏截断 120）
    citations, ssrf_removed = _wigolo_sanitize_citations(meta["citations"])
    if ssrf_removed:
        logger.warning(f"🛡️ SSRF 防护: web_agent 剔除 {ssrf_removed} 条内网/保留 citations")
    logger.info(f"🔍 web_agent: prompt={p[:120]} pages={max_pages} sources={meta['sources_count']}")
    return ok({
        "provider": "wigolo",
        "mode": "agent",
        "report": report[:_PREVIEW_CHARS],
        "truncated": len(report) > _PREVIEW_CHARS,
        "citations": citations[:20],
        "ssrf_removed": ssrf_removed,
        "sources_count": meta["sources_count"],
        "heuristic": meta["heuristic"],
        "chars": len(report),
        "estimated_tokens": max(1, round(len(report) / 4)),
    })
