"""文档摄入工具：add_document / list_documents（v2.6.0，v2.9.0 增强）。

借鉴 markitdown / opendataloader-pdf 的文档摄入思路：把 PDF/Word/HTML/EPUB/RTF/
纯文本等附件转成 Markdown 存入共享目录 documents/（首块 JSON 元数据 + 正文），
让平台可检索、可引用、可与 find_skill / search_memory 联动。

转换策略（按可用性降级，零硬依赖）：
- anydoc（v3.16，可选）：firecrawl-anydoc 优先覆盖全部 office 格式（Word/PPT/Excel/ODF/CSV）
  + docx/pdf/epub/rtf 提质；未安装时自动跳过，新格式明确报错、旧格式走原降级链
- PDF：pdftotext（poppler，最快）→ markitdown → PyPDF2 → pdfminer.six
- DOCX：python-docx（段落 + 表格 → Markdown）
- HTML：bs4（标题/段落/列表/代码 → Markdown）
- EPUB：ebooklib（各章 → 标题 + 正文）
- RTF：striprtf
- MD/TXT/RST/ADOC：直通（去 BOM、规范化换行）

安全约定：
- 输入 source_path 允许执行端本机绝对路径或共享根相对路径（要转换的文件）；
- 输出 target_dir 必须解析到共享根内（默认 documents/），防目录穿越；
- 输出文件名由原文件名生成安全 slug，不接受用户指定文件名 → 无路径穿越面；
- 单文件大小上限 100MB、输出字符上限 1500000；
- 身份闸门：REQUIRE_IDENTITY=1 时未绑定身份被拒。
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import unicodedata
import uuid
from pathlib import Path

from .config import COLLAB_DIR, DOCS_DIR
from .identity import assert_identity_allowed, current_identity
from .logging_setup import logger
from .utils import fail, normalize_url, now_iso, ok

_META_SEP = "---"
# v2.9.0：本轮（进程内）去重计数——记录本进程跳过同一内容指纹的次数
_SESSION_DEDUP: dict[str, int] = {}
_INPUT_MAX_BYTES = 100 * 1024 * 1024  # 100MB
_OUTPUT_MAX_CHARS = 1_500_000
_SHARED_ROOT = COLLAB_DIR.parent
_TEXT_EXTS = {".md", ".markdown", ".txt", ".text", ".rst", ".adoc", ".asciidoc"}
_HTML_EXTS = {".html", ".htm", ".xhtml"}
# v3.16：office 扩展名 → 格式（anydoc 覆盖；新增 PPT/Excel/ODF/CSV 支持）
_OFFICE_EXTS = {
    ".doc": "doc", ".docm": "docm",
    ".ppt": "ppt", ".pps": "pps", ".pot": "pot", ".pptx": "pptx",
    ".pptm": "pptm", ".ppsx": "ppsx", ".ppsm": "ppsm",
    ".xls": "xls", ".xlsx": "xlsx", ".xlsm": "xlsm", ".xlsb": "xlsb",
    ".odt": "odt", ".ods": "ods", ".odp": "odp",
    ".csv": "csv",
}
# v3.16：anydoc 可用性惰性探测缓存（未安装=不启用，零硬依赖）
_ANYDOC_AVAILABLE: bool | None = None
# anydoc 独占格式（无其他转换器）；docx/pdf/epub/rtf 已有降级链可回退
_ANYDOC_FMTS = set(_OFFICE_EXTS.values())
_ANYDOC_FALLBACK_FMTS = {"docx", "pdf", "epub", "rtf"}


def _slugify(name: str) -> str:
    """文件名 → 安全 slug（小写字母/数字/连字符）。"""
    stem = Path(name).stem.lower()
    stem = re.sub(r"[^a-z0-9]+", "-", stem).strip("-")
    return (stem or "document")[:80]


def _read_doc_meta(file_path: Path) -> dict | None:
    """读取带元数据头的 .md 文档的元数据 JSON；无法解析返回 None。"""
    try:
        t = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    lines = t.splitlines()
    if not lines or lines[0].strip() != _META_SEP:
        return None
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == _META_SEP:
            end = i
            break
    if end is None:
        return None
    try:
        meta = json.loads("\n".join(lines[1:end]))
    except json.JSONDecodeError:
        return None
    return meta if isinstance(meta, dict) else None


def _atomic_write_text(file_path: Path, text: str) -> bool:
    """原子写文本：临时文件 + fsync + os.replace（与 memory/skills 同策略）。"""
    tmp_path = file_path.with_name(
        f".{file_path.name}.{os.getpid()}.{uuid.uuid4().hex[:6]}.tmp"
    )
    try:
        tmp_path.write_text(text, encoding="utf-8")
        try:
            with open(tmp_path, "rb+") as f:
                os.fsync(f.fileno())
        except OSError:
            pass
        os.replace(tmp_path, file_path)
        return True
    except (IOError, OSError) as e:
        logger.error(f"写入文档失败 {file_path}: {e}")
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def _extract_pdf(path: Path) -> str:
    """PDF → 文本。pdftotext → markitdown → PyPDF2 → pdfminer 逐级降级。"""
    if shutil.which("pdftotext"):
        try:
            result = subprocess.run(
                ["pdftotext", "-layout", str(path), "-"],
                capture_output=True, timeout=120,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.decode("utf-8", errors="replace")
        except Exception:
            pass
    try:
        from markitdown import MarkItDown
        md = MarkItDown().convert(str(path))
        text = getattr(md, "text_content", "") or ""
        if text.strip():
            return text
    except Exception:
        pass
    try:
        import PyPDF2
        parts = []
        with open(path, "rb") as f:
            reader = PyPDF2.PdfReader(f)
            for page in reader.pages:
                try:
                    parts.append(page.extract_text() or "")
                except Exception:
                    parts.append("")
        return "\n".join(parts)
    except Exception:
        pass
    try:
        from pdfminer.high_level import extract_text
        return extract_text(str(path))
    except Exception:
        return ""


def _extract_docx(path: Path) -> str:
    """DOCX → Markdown（python-docx：段落 + 表格）。"""
    from docx import Document
    doc = Document(str(path))
    lines: list[str] = []
    for para in doc.paragraphs:
        style = (para.style.name or "").lower()
        text = para.text.strip()
        if not text:
            continue
        if style.startswith("heading"):
            level = min(6, int(re.sub(r"\D", "", style) or 1))
            lines.append(f"{'#' * level} {text}")
        elif style == "list bullet" or para._p.pPr is not None and para._p.pPr.find(
            "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}numPr"
        ) is not None:
            lines.append(f"- {text}")
        else:
            lines.append(text)
    for table in doc.tables:
        lines.append("")
        for row in table.rows:
            cells = [c.text.strip().replace("\n", " ") for c in row.cells]
            lines.append("| " + " | ".join(cells) + " |")
    return "\n\n".join(lines).strip()


def _extract_html(path: Path) -> str:
    """HTML → Markdown（bs4 简化转换：标题/段落/列表/代码/链接）。"""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(path.read_text(encoding="utf-8", errors="replace"), "html.parser")
    lines: list[str] = []
    for el in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p", "ol", "ul", "pre", "code", "table"]):
        tag = el.name
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            level = int(tag[1])
            lines.append(f"{'#' * level} {el.get_text(' ', strip=True)}")
        elif tag == "p":
            if el.find_parent(["li", "pre"]):
                continue
            lines.append(el.get_text(" ", strip=True))
        elif tag in ("ol", "ul"):
            if el.find_parent("li"):
                continue
            for idx, li in enumerate(el.find_all("li", recursive=False)):
                text = li.get_text(" ", strip=True)
                if not text:
                    continue
                lines.append(f"{idx + 1}. {text}" if tag == "ol" else f"- {text}")
        elif tag == "pre":
            lines.append("```\n" + el.get_text("\n", strip=False).strip() + "\n```")
        elif tag == "code":
            if el.find_parent("pre"):
                continue
            lines.append(f"`{el.get_text(' ', strip=True)}`")
        elif tag == "table":
            for tr in el.find_all("tr"):
                cells = [td.get_text(" ", strip=True) for td in tr.find_all(["td", "th"])]
                if cells:
                    lines.append("| " + " | ".join(cells) + " |")
    return "\n\n".join(l for l in lines if l).strip()


def _extract_epub(path: Path) -> str:
    """EPUB → Markdown（ebooklib：各章标题 + 正文）。"""
    import ebooklib
    from bs4 import BeautifulSoup
    from ebooklib import epub
    book = epub.read_epub(str(path))
    lines: list[str] = []
    for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
        soup = BeautifulSoup(item.get_content(), "html.parser")
        title = soup.find("title")
        if title:
            lines.append(f"# {title.get_text(' ', strip=True)}")
        for el in soup.find_all(["h1", "h2", "h3", "p", "li"]):
            text = el.get_text(" ", strip=True)
            if not text:
                continue
            if el.name.startswith("h"):
                level = int(el.name[1])
                lines.append(f"{'#' * level} {text}")
            elif el.name == "li":
                lines.append(f"- {text}")
            else:
                lines.append(text)
    return "\n\n".join(l for l in lines if l).strip()


def _extract_rtf(path: Path) -> str:
    """RTF → 纯文本（striprtf）。"""
    from striprtf.striprtf import rtf_to_text
    raw = path.read_text(encoding="utf-8", errors="replace")
    return rtf_to_text(raw).strip()


def _anydoc_available() -> bool:
    """anydoc（firecrawl-anydoc）可用性惰性探测缓存；未安装=不启用。"""
    global _ANYDOC_AVAILABLE
    if _ANYDOC_AVAILABLE is None:
        try:
            import anydoc  # noqa: F401
            _ANYDOC_AVAILABLE = True
        except ImportError:
            _ANYDOC_AVAILABLE = False
    return _ANYDOC_AVAILABLE


def _extract_anydoc(path: Path, fmt: str) -> str:
    """anydoc → GFM Markdown（v3.16 office 全家桶 + 统一高质量输出；可选依赖）。

    CSV 无内容签名，需显式传格式名；其余格式 anydoc 从字节内容识别。
    """
    import anydoc
    if fmt == "csv":
        return anydoc.to_markdown_bytes(path.read_bytes(), "csv")
    return anydoc.to_markdown(str(path))


def _detect_format(path: Path, ext: str, format_hint: str) -> str:
    """确定文档格式：hint 优先，其次扩展名，再其次魔数嗅探。"""
    fmt = (format_hint or "").strip().lower().lstrip(".")
    if fmt:
        return fmt
    if ext in _TEXT_EXTS:
        return "md"
    if ext in _HTML_EXTS:
        return "html"
    if ext == ".pdf":
        return "pdf"
    if ext == ".docx":
        return "docx"
    if ext == ".epub":
        return "epub"
    if ext == ".rtf":
        return "rtf"
    if ext in _OFFICE_EXTS:
        return _OFFICE_EXTS[ext]
    # 魔数嗅探
    try:
        with open(path, "rb") as f:
            header = f.read(8)
        if header[:4] == b"%PDF":
            return "pdf"
        if header[:8] == bytes.fromhex("D0CF11E0A1B11AE1"):
            # OLE 复合文档（doc/xls/ppt 通用签名；anydoc 按内容识别具体格式）
            return "doc"
        if header[:2] == b"PK":
            import zipfile
            with zipfile.ZipFile(path) as zf:
                names = set(zf.namelist())
                if "word/document.xml" in names:
                    return "docx"
                if "ppt/presentation.xml" in names:
                    return "pptx"
                if "xl/workbook.xml" in names:
                    return "xlsx"
                if "mimetype" in names:
                    return "epub"
    except Exception:
        pass
    return ext.lstrip(".") or "unknown"


def _extract_md(path: Path, fmt: str) -> tuple[str, str]:
    """按格式提取 Markdown，返回 (文本, 提取方式)。"""
    # v3.16：anydoc 可选首选——覆盖全部 office 格式 + docx/pdf/epub/rtf 提质；
    # 未安装时新格式明确报错、旧格式走原有降级链（零硬依赖不破坏）
    if fmt in _ANYDOC_FMTS or fmt in _ANYDOC_FALLBACK_FMTS:
        if _anydoc_available():
            try:
                text = _extract_anydoc(path, fmt)
                if text and text.strip():
                    return text, "anydoc"
            except Exception as e:
                logger.warning(f"anydoc 提取失败 {path.name} ({fmt}): {e}")
                if fmt in _ANYDOC_FMTS:
                    raise RuntimeError(f"anydoc 转换失败（{fmt}）: {e}") from e
            if fmt in _ANYDOC_FMTS:
                # PC-C 闸门 LOW1：anydoc 可用但返回空文本（非异常）→ 显式报错，
                # 避免 office 二进制回退 passthrough 被当文本读（乱码摄入）
                raise RuntimeError(f"anydoc 未从文档提取到文本（{fmt}）")
        elif fmt in _ANYDOC_FMTS:
            raise RuntimeError(
                f"格式 {fmt} 需要安装 firecrawl-anydoc（pip install firecrawl-anydoc）"
            )
    if fmt == "pdf":
        return _extract_pdf(path), "pdftotext/markitdown/pypdf2/pdfminer"
    if fmt == "docx":
        return _extract_docx(path), "python-docx"
    if fmt == "html":
        return _extract_html(path), "bs4"
    if fmt == "epub":
        return _extract_epub(path), "ebooklib"
    if fmt == "rtf":
        return _extract_rtf(path), "striprtf"
    # 纯文本/Markdown 直通
    return path.read_text(encoding="utf-8", errors="replace"), "passthrough"


async def add_document(
    source_path: str,
    target_dir: str = "",
    format_hint: str = "",
    skip_duplicates: bool = False,
    dedup_key: str = "",
    dedup_url: str = "",
    extra_meta: dict | None = None,
) -> str:
    """把一份文档/附件转成 Markdown 摄入共享目录 documents/。

    支持 PDF / DOCX / PPT / Excel / OpenDocument / CSV / HTML / EPUB / RTF /
    MD / TXT / RST / ADOC。转换器按可用性自动降级（anydoc 优先，未安装时
    PDF=pdftotext → markitdown → PyPDF2 → pdfminer 等），零硬依赖。
    输出文件带 JSON 元数据头（源文件、格式、提取方式、字符数、token 估算、摄入人）。

    Args:
        source_path: 源文档路径——执行端本机绝对路径，或相对共享根（如 "results/x.pdf"）
        target_dir: 可选，输出目录（相对共享根），默认 "documents"
        format_hint: 可选，强制指定格式（如 "pdf"/"docx"/"html"），缺省自动检测
        skip_duplicates: 可选，为 True 时若目标目录已有相同内容则跳过并返回已有文档
        dedup_key: 可选，内容指纹（如清洗后正文的 sha256_16）；与已有文档 dedup_key
            一致即视为重复（旧文档无 dedup_key 时用 sha256_16 兜底）
        dedup_url: 可选，归一化 URL；与已有文档 url（归一后）一致即视为重复
        extra_meta: 可选，并入元数据 JSON 的附加字段（如 title/url/host/provider）；
            不覆盖 source/source_path/format/method/chars/sha256_16/ingested_by/ingested_at
    """
    denied = assert_identity_allowed("add_document")
    if denied:
        return fail(denied)
    src = (source_path or "").strip()
    if not src:
        return fail("add_document 需要非空 source_path")
    p = Path(src)
    if not p.is_absolute():
        p = (_SHARED_ROOT / p).resolve()
    if not p.exists() or not p.is_file():
        return fail(f"源文件不存在: {source_path}")
    size = p.stat().st_size
    if size > _INPUT_MAX_BYTES:
        return fail(f"源文件超过上限 {_INPUT_MAX_BYTES // (1024 * 1024)}MB")

    fmt = _detect_format(p, p.suffix.lower(), format_hint)
    if fmt == "unknown":
        return fail(f"无法识别的文档格式: {p.suffix or '<无扩展名>'}")
    if fmt == "pdf":
        # v2.6.1（本机 Claude P1）：PDF 必须先过 %PDF- 魔数校验，伪装 .pdf 的文本文件拒绝摄入
        try:
            with open(p, "rb") as f:
                head = f.read(5)
        except OSError as e:
            return fail(f"读取源文件失败: {e}")
        if not head.startswith(b"%PDF"):
            return fail("文件不是有效的 PDF（缺少 %PDF- 文件头）")
    try:
        text, method = _extract_md(p, fmt)
    except Exception as e:
        logger.warning(f"文档提取失败 {p.name} ({fmt}): {e}")
        return fail(f"文档提取失败（{fmt}）: {e}")
    if not text or not text.strip():
        return fail(f"未能从文档提取到文本（{fmt}；可能是扫描版/无文本层）")
    # v2.7.1（本机 Claude L1）：NFKC 规范化，消除 PDF 提取层的 Unicode 连字
    # （如 ﬁ U+FB01 → fi），避免 "artificial" 等检索词因字形不匹配而漏命中
    text = unicodedata.normalize("NFKC", text)
    if len(text) > _OUTPUT_MAX_CHARS:
        text = text[:_OUTPUT_MAX_CHARS]

    rel = (target_dir or "").strip() or "documents"
    target = (_SHARED_ROOT / rel).resolve()
    if not target.is_relative_to(_SHARED_ROOT.resolve()):
        return fail(f"target_dir 超出共享目录范围: {target_dir}")
    target.mkdir(parents=True, exist_ok=True)

    ident = current_identity() or "local"
    sha = hashlib.sha256(p.read_bytes()).hexdigest()[:16]
    out_name = f"{_slugify(p.name)}.md"
    out_path = target / out_name
    # v2.9.0：去重增强——内容指纹（dedup_key）/ 归一 URL（dedup_url）/ 文件字节
    # （sha256_16）任一命中即视为重复，并统计累计与本轮计数
    dup_key = (dedup_key or "").strip()
    dup_url = (dedup_url or "").strip()
    if skip_duplicates:
        matches = []
        for f in target.glob("*.md"):
            meta = _read_doc_meta(f)
            if meta is None:
                continue
            sha_hit = str(meta.get("sha256_16", "")) == sha
            key_hit = False
            if dup_key:
                existing_key = str(meta.get("dedup_key", "") or "").strip()
                # 旧版文档无 dedup_key：文件字节 sha256_16 兜底
                key_hit = existing_key == dup_key if existing_key else sha_hit
            url_hit = False
            if dup_url:
                existing_url = str(meta.get("url", "") or "").strip()
                url_hit = bool(existing_url) and normalize_url(existing_url) == dup_url
            if sha_hit or key_hit or url_hit:
                matches.append(f)
        if matches:
            first = matches[0]
            dup_count = len(matches)
            session_key = dup_key or f"url:{dup_url}"
            _SESSION_DEDUP[session_key] = _SESSION_DEDUP.get(session_key, 0) + 1
            session_count = _SESSION_DEDUP[session_key]
            logger.info(
                f"📄 文档去重命中（内容/URL 一致），跳过摄入: "
                f"{first.relative_to(_SHARED_ROOT)}（累计 {dup_count}，本轮 {session_count}）"
            )
            return ok({
                "message": (
                    f"已存在相同内容文档（内容指纹/URL 一致），跳过: "
                    f"{first.relative_to(_SHARED_ROOT)}"
                ),
                "path": str(first.relative_to(_SHARED_ROOT)),
                "filename": first.name,
                "duplicate": True,
                "duplicate_count": dup_count,
                "session_duplicates": session_count,
                "ingested_by": ident,
            })
    meta = {
        "source": p.name,
        "source_path": str(p),
        "format": fmt,
        "method": method,
        "chars": len(text),
        "estimated_tokens": max(1, round(len(text) / 4)),
        "sha256_16": sha,
        "ingested_by": ident,
        "ingested_at": now_iso(),
    }
    if dup_key:
        meta["dedup_key"] = dup_key
    if extra_meta:
        if not isinstance(extra_meta, dict):
            return fail("extra_meta 必须是 JSON 对象")
        # PC-C 验证 L4：非 JSON 序列化值会令写入阶段 json.dumps 抛异常 → 提前校验
        try:
            json.dumps(extra_meta, ensure_ascii=False)
        except (TypeError, ValueError):
            return fail("extra_meta 必须可 JSON 序列化")
        merged = dict(meta)
        merged.update(extra_meta)
        # 核心字段不允许被 extra_meta 覆盖（search_documents 已读取的契约；
        # PC-C 验证 M1：estimated_tokens 一并保护）
        for _core in (
            "source", "source_path", "format", "method", "chars",
            "estimated_tokens", "sha256_16", "dedup_key",
            "ingested_by", "ingested_at",
        ):
            # dedup_key 仅在传入时存在；其余核心字段恒存在
            if _core in meta:
                merged[_core] = meta[_core]
        meta = merged
    body = (
        f"{_META_SEP}\n"
        f"{json.dumps(meta, ensure_ascii=False, indent=2)}\n"
        f"{_META_SEP}\n\n"
        f"{text.strip()}\n"
    )
    if not _atomic_write_text(out_path, body):
        return fail(f"无法写入文档: {out_path.name}")
    logger.info(f"📄 文档摄入 [{out_path.relative_to(_SHARED_ROOT)}] {p.name} ({fmt}, {len(text)} 字符) by {ident}")
    result = {
        "message": f"文档已摄入: {rel}/{out_name}",
        "path": str(out_path.relative_to(_SHARED_ROOT)),
        "filename": out_name,
        "format": fmt,
        "method": method,
        "chars": len(text),
        "estimated_tokens": meta["estimated_tokens"],
        "ingested_by": ident,
    }
    if dup_key:
        result["dedup_key"] = dup_key
        result["duplicate_count"] = 0
        result["session_duplicates"] = 0
    return ok(result)


async def list_documents(target_dir: str = "", limit: int = 50) -> str:
    """列出共享目录中已摄入的文档（含元数据摘要）。

    Args:
        target_dir: 可选，限定子目录（相对共享根），默认全部 documents/ 树
        limit: 可选，返回条数上限，默认 50，夹取 [1, 200]
    """
    denied = assert_identity_allowed("list_documents")
    if denied:
        return fail(denied)
    try:
        limit = max(1, min(int(limit), 200))
    except (TypeError, ValueError):
        limit = 50
    base = (_SHARED_ROOT / (target_dir or "documents")).resolve()
    if not base.is_relative_to(_SHARED_ROOT.resolve()):
        return fail(f"target_dir 超出共享目录范围: {target_dir}")
    if not base.is_dir():
        return ok({"count": 0, "documents": []})
    docs = []
    for f in sorted(base.rglob("*.md")):
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lines = text.splitlines()
        if not lines or lines[0].strip() != _META_SEP:
            continue
        end = None
        for i in range(1, len(lines)):
            if lines[i].strip() == _META_SEP:
                end = i
                break
        if end is None:
            continue
        try:
            meta = json.loads("\n".join(lines[1:end]))
        except json.JSONDecodeError:
            continue
        if not isinstance(meta, dict):
            continue
        item = {
            "path": str(f.relative_to(_SHARED_ROOT)),
            "source": str(meta.get("source", "")),
            "format": str(meta.get("format", "")),
            "method": str(meta.get("method", "")),
            "chars": meta.get("chars"),
            "estimated_tokens": meta.get("estimated_tokens"),
            "ingested_by": str(meta.get("ingested_by", "")),
            "ingested_at": str(meta.get("ingested_at", "")),
        }
        # v2.9.0：web 摄入附加元数据（additive，不影响既有字段）
        for _k in ("url", "host", "provider", "body_length", "dedup_key"):
            if _k in meta:
                item[_k] = meta[_k]
        docs.append(item)
    docs.sort(key=lambda d: d.get("ingested_at", ""), reverse=True)
    docs = docs[:limit]
    logger.info(f"📄 文档列表: {len(docs)} 个")
    return ok({"count": len(docs), "documents": docs})
