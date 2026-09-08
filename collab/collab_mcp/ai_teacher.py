"""AI 老师：explain_work —— 纯只读讲解工具（v3.22.0）。

设计来源：results/handoff-20260808-v321-ai-teacher.md §4
- 输入三通道：milestone（git 版本区间）/ task_id（collab 任务）/ path（文件或目录，
  例如 WorkBuddy 日志、办公产物）。
- 输出四段：① 一句话总结 ② 分块人话讲解（改了什么/为什么/怎么验证/影响）
  ③ 证据引用 ④ 检查清单（3-5 条）+ 下一步方向选项。
- 三个防反噬原则（实现内强制）：
  1) 引用式讲解：每条说法必须能挂到具体证据（文件/commit/测试/报告）；
     证据里没有的就明说"材料未说明/这里没证据"，禁止编造。
  2) 讲完带作业：始终生成 3-5 个用户可亲自核实的检查点，判断权留给人。
  3) 纯只读：只收集产物 → 引用式讲解；不执行、不修改被讲解对象；
     唯一写操作 = 把讲解存到 results/（UTF-8，避免控制台 GBK）。
- 技术底座：ollama qwen2.5:3b（可选 LLM）+ 提取式模板兜底；零新依赖。
- 隐私：预览内容自动脱敏（uuid / userId / deviceId / 长 hex）。
"""

import asyncio
import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from .config import COLLAB_DIR, DONE_DIR, INBOX_DIR
from .identity import assert_identity_allowed
from .logging_setup import logger
from .utils import fail, ok

_PREVIEW_CHARS = 6000          # 单文件内容预览上限
_DIGEST_CHARS = 12000          # 喂 LLM 的证据摘要上限
_MAX_LLM_SUMMARY = 60          # LLM 一句话总结上限（CPU 3b 提速）
_MAX_LLM_BLOCKS = 300          # LLM 分块讲解上限（CPU 3b 提速）
_LOG_TAIL_CHARS = 6000         # .log 文件取尾部
_MAX_FILE_BYTES = 5 * 1024 * 1024

_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I
)
_HEXID_RE = re.compile(r"\b[0-9a-f]{16,40}\b", re.I)
_IDFIELD_RE = re.compile(
    r"(userId|deviceId|deviceName|sessionId|token|secret|api[_-]?key)"
    r"([\"':= ]+)[^,\"}\s]+",
    re.I,
)
_ERR_RE = re.compile(r"error|fail|exception|watchdog|denied|404|timeout", re.I)
_TESTNAME_RE = re.compile(r"\bTest[A-Za-z0-9_]+\b")

_TEXT_EXTS = {
    ".md", ".txt", ".log", ".json", ".jsonl", ".py", ".html", ".csv",
    ".ini", ".cfg", ".yaml", ".yml", ".toml", ".bat", ".ps1",
    ".c", ".h", ".js", ".ts", ".rst", ".xml",
}


def _redact(text: str) -> str:
    """脱敏：uuid / 身份字段 / 长 hex（用于内容预览，不用于 git 证据）。"""
    t = _UUID_RE.sub("<uuid>", text)
    t = _IDFIELD_RE.sub(r"\1\2<redacted>", t)
    t = _HEXID_RE.sub("<hex-id>", t)
    return t


def _git(repo: Path, *args: str) -> str | None:
    """只读 git 调用；失败返回 None（不抛异常）。"""
    try:
        p = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return p.stdout.strip() if p.returncode == 0 else None


def _read_preview(path: Path, cap: int = _PREVIEW_CHARS) -> str:
    """读文本预览：二进制/超大跳过；.log 取尾部（近期活动）。"""
    try:
        st = path.stat()
        if st.st_size > _MAX_FILE_BYTES:
            return f"[文件 {st.st_size} 字节超过预览上限，未读入；建议缩小范围或用日志尾部]"
        raw = path.read_bytes()
        if b"\x00" in raw[:2048]:
            return "[二进制文件，跳过内容预览]"
        text = raw.decode("utf-8", errors="replace")
    except OSError:
        return "[无法读取]"
    if path.suffix.lower() == ".log" and len(text) > cap:
        return text[-cap:]
    return text[:cap]


def _collect_gate_reports(repo_dir: str, milestone: str) -> list[dict]:
    """从 results/ 找包含该 milestone 的闸门/验证报告（最新 2 份）。"""
    out: list[dict] = []
    results = Path(repo_dir) / "results"
    if not results.is_dir():
        return out
    candidates = sorted(results.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    for f in candidates:
        name = f.name.lower()
        if "verify" not in name and "gate" not in name:
            continue
        preview = _read_preview(f, 8000)
        if milestone.lower() in preview.lower() or milestone.lower() in name:
            out.append({"path": str(f), "preview": _redact(preview)})
        if len(out) >= 2:
            break
    return out


def collect_milestone_evidence(repo_dir: str, milestone: str) -> dict:
    """收集 git 版本区间证据：commit / 变更统计 / 闸门报告 / release notes。"""
    repo = Path(repo_dir)
    if ".." in milestone:
        rng = milestone
        left, right = (s.strip() for s in milestone.split("..", 1))
        prev = left
        ms = right
    else:
        ms = milestone
        verify = _git(repo, "rev-parse", "--verify", "--short", f"{ms}^{{commit}}")
        if verify is None:
            return {"error": f"milestone 无法解析为 tag/commit: {milestone}"}
        tags = [
            t.strip()
            for t in (_git(repo, "tag", "--sort=-v:refname") or "").splitlines()
            if t.strip()
        ]
        prev = ""
        if ms in tags:
            i = tags.index(ms)
            prev = tags[i + 1] if i + 1 < len(tags) else ""
        rng = f"{prev}..{ms}" if prev else f"{ms}^..{ms}"
    log = _git(repo, "log", "--format=%h|%an|%s", rng) or ""
    stat = _git(repo, "diff", "--stat", rng) or ""
    commits: list[dict] = []
    for line in log.splitlines():
        parts = line.split("|", 2)
        if len(parts) == 3:
            commits.append({"hash": parts[0], "author": parts[1], "subject": parts[2]})
    release_notes = ""
    rn_path = Path(repo_dir) / "documents" / f"release-notes-{ms}.md"
    if rn_path.exists():
        release_notes = _redact(_read_preview(rn_path, 3000))
    return {
        "kind": "milestone",
        "milestone": ms,
        "prev_tag": prev,
        "range": rng,
        "commits": commits,
        "diff_stat": stat[:2000],
        "head": _git(repo, "rev-parse", "--short", "HEAD") or "",
        "origin_master": _git(repo, "rev-parse", "--short", "origin/master") or "",
        "release_notes": release_notes,
        "gate_reports": _collect_gate_reports(repo_dir, ms),
        "title": f"版本 {ms}",
    }


def collect_task_evidence(task_id: str) -> dict:
    """收集 collab 任务证据：任务文件 + journal 时间线 + result_path 预览。"""
    task = None
    source = None
    for d in (DONE_DIR, INBOX_DIR):
        p = d / f"{task_id}.json"
        if p.exists():
            try:
                task = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            source = p
            break
    if task is None:
        return {"error": f"任务 {task_id} 未找到（inbox/done 均无）"}
    journal_path = COLLAB_DIR / "journal" / f"{task_id}.jsonl"
    history: list[str] = []
    if journal_path.exists():
        lines = journal_path.read_text(encoding="utf-8", errors="replace").splitlines()
        history = [_redact(l) for l in lines[-20:]]
    rp = task.get("result_path") or ""
    result_preview = ""
    if rp and Path(rp).exists():
        result_preview = _redact(_read_preview(Path(rp), 3000))
    title = task.get("title") or f"任务 {task_id}"
    return {
        "kind": "task",
        "task_id": task_id,
        "title": title,
        "assignee": task.get("assignee", ""),
        "status": task.get("status", ""),
        "content": (task.get("content") or "")[:3000],
        "evidence": (task.get("evidence") or "")[:4000],
        "result_path": rp,
        "result_preview": result_preview,
        "history": history,
        "source": str(source) if source else "",
    }


def collect_path_evidence(path_str: str, title: str = "") -> dict:
    """收集文件/目录证据：元数据 + 文本预览（脱敏、限长、日志取尾部）。"""
    p = Path(path_str).expanduser()
    if not p.exists():
        return {"error": f"路径不存在: {path_str}"}
    entries: list[dict] = []
    previews: list[dict] = []
    if p.is_dir():
        try:
            items = sorted(
                p.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True
            )
        except OSError:
            items = []
        for it in items[:14]:
            try:
                st = it.stat()
                entries.append({
                    "name": it.name,
                    "size": st.st_size,
                    "mtime": datetime.fromtimestamp(st.st_mtime).isoformat(
                        timespec="seconds"
                    ),
                    "is_dir": it.is_dir(),
                })
            except OSError:
                continue
        # 递归收集文本/日志预览（深度≤3，按 mtime 取最新 3 个），避免只看到顶层目录
        walk: list[Path] = []
        try:
            for root, dirs, files in os.walk(p):
                depth = root[len(str(p)) :].count(os.sep)
                if depth > 3:
                    dirs[:] = []
                    continue
                for fn in files:
                    walk.append(Path(root) / fn)
        except OSError:
            walk = []
        cands = [
            it for it in walk
            if it.suffix.lower() in _TEXT_EXTS
            and it.stat().st_size <= _MAX_FILE_BYTES
        ]
        cands.sort(key=lambda x: x.stat().st_mtime, reverse=True)
        cands = cands[:3]
        for it in cands:
            previews.append({
                "path": str(it),
                "preview": _redact(_read_preview(it)),
            })
    else:
        try:
            st = p.stat()
            entries.append({
                "name": p.name,
                "size": st.st_size,
                "mtime": datetime.fromtimestamp(st.st_mtime).isoformat(
                    timespec="seconds"
                ),
                "is_dir": False,
            })
        except OSError:
            pass
        if p.suffix.lower() in _TEXT_EXTS:
            previews.append({"path": str(p), "preview": _redact(_read_preview(p))})
    return {
        "kind": "path",
        "path": path_str,
        "title": title or p.name,
        "entries": entries,
        "previews": previews,
    }


def _digest(ev: dict) -> str:
    """把证据压缩成喂 LLM 的摘要（限长、已脱敏）。"""
    parts: list[str] = []
    kind = ev.get("kind")
    if kind == "milestone":
        parts.append(f"里程碑 {ev.get('milestone')}，区间 {ev.get('range')}")
        for c in ev.get("commits", [])[:8]:
            parts.append(f"commit {c['hash']} by {c['author']}: {c['subject']}")
        if ev.get("diff_stat"):
            parts.append("变更统计:\n" + ev["diff_stat"])
        if ev.get("release_notes"):
            parts.append("Release Notes 摘录:\n" + ev["release_notes"])
        for g in ev.get("gate_reports", [])[:1]:
            parts.append("闸门报告摘录:\n" + g["preview"])
    elif kind == "task":
        parts.append(f"任务：{ev.get('title')}（assignee={ev.get('assignee')}，status={ev.get('status')}）")
        if ev.get("content"):
            parts.append("任务内容:\n" + ev["content"])
        if ev.get("evidence"):
            parts.append("evidence:\n" + ev["evidence"])
        if ev.get("result_preview"):
            parts.append("result_path 预览:\n" + ev["result_preview"])
    else:
        parts.append(f"对象：{ev.get('title')}（{ev.get('path')}）")
        for e in ev.get("entries", []):
            parts.append(
                f"- {e['name']} {e['size']}B mtime={e['mtime']}"
                + (" [目录]" if e["is_dir"] else "")
            )
        for pv in ev.get("previews", []):
            parts.append("内容预览:\n" + pv["preview"])
    text = "\n".join(parts)
    return text[: _DIGEST_CHARS]


def _ollama_chat(prompt: str, system: str, max_tokens: int, timeout: int) -> str:
    """调用本地 ollama（OpenAI-compatible），零新依赖；失败抛异常由上层降级。"""
    base = (os.environ.get("OLLAMA_BASE_URL") or "http://127.0.0.1:11434").rstrip("/")
    model = os.environ.get("OLLAMA_MODEL") or "qwen2.5:3b"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        "options": {"temperature": 0.3},
    }
    req = urllib.request.Request(
        base + "/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8", errors="replace"))
    msg = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    return msg.strip()


def _extractive_summary(ev: dict, digest: str) -> str:
    """提取式兜底总结：优先任务标题/里程碑+首条 commit 主题。"""
    if ev.get("kind") == "milestone":
        commits = ev.get("commits", [])
        if commits:
            # 挑最新的实质功能提交（跳过 docs/release 前缀），比区间最老提交更贴题
            head = next(
                (
                    c.get("subject", "")
                    for c in commits
                    if not c.get("subject", "").startswith(
                        ("docs", "release", "chore", "refactor")
                    )
                ),
                commits[0].get("subject", ""),
            )
            return f"本次围绕「{ev.get('milestone')}」收尾发布：{head}（详见证据清单）"[:120]
        return f"本次围绕「{ev.get('milestone')}」的变更与验证（详见证据清单）"[:120]
    if ev.get("kind") == "task":
        return f"任务「{ev.get('title')}」的当前状态与交付证据如下（判断请以检查清单为准）。"[:120]
    first_line = ""
    for line in digest.splitlines():
        if line.strip() and not line.strip().startswith("-"):
            first_line = line.strip()
            break
    return (first_line or f"对象「{ev.get('title')}」概况如下（详见证据清单）。")[:120]


def _error_lines(ev: dict) -> list[str]:
    """从路径预览中统计错误/异常行（去重，最多 5 条）。"""
    out: list[str] = []
    seen = set()
    for pv in ev.get("previews", []):
        for line in pv["preview"].splitlines():
            if _ERR_RE.search(line):
                key = line[:80]
                if key in seen:
                    continue
                seen.add(key)
                out.append(f"- {pv['path']}: {line[:140]}")
                if len(out) >= 5:
                    return out
    return out


def _blocks_deterministic(ev: dict) -> str:
    """无 LLM 时的确定性分块讲解：全部内容可逐条指到证据。"""
    kind = ev.get("kind")
    lines: list[str] = []
    if kind == "milestone":
        commits = ev.get("commits", [])
        if commits:
            lines.append("**改动内容（来自 git log）**")
            for c in commits:
                lines.append(f"- commit `{c['hash']}`（{c['author']}）：{c['subject']}")
        if ev.get("diff_stat"):
            lines.append("**变更面（来自 git diff --stat）**")
            lines.append(f"```{ev['diff_stat']}```")
        if ev.get("release_notes"):
            lines.append("**为什么/影响（来自 release notes 摘录）**")
            lines.append(ev["release_notes"][:800])
        for g in ev.get("gate_reports", [])[:1]:
            lines.append(f"**怎么验证（闸门报告 {Path(g['path']).name}）**")
            lines.append(g["preview"][:600])
        if not ev.get("gate_reports"):
            lines.append("**怎么验证**：材料中未找到对应闸门报告——这里没证据，需要你亲自核对。")
    elif kind == "task":
        lines.append(f"**任务内容**\n{ev.get('content') or '（任务文件无 content 字段）'}")
        lines.append(
            f"**状态与交付**\n- 状态：{ev.get('status') or '?'}；指派：{ev.get('assignee') or '?'}"
        )
        if ev.get("evidence"):
            lines.append(f"**evidence（来自任务文件）**\n{ev['evidence'][:1000]}")
        if ev.get("result_preview"):
            lines.append(f"**result_path 预览（{ev.get('result_path')}）**\n{ev['result_preview'][:600]}")
        if ev.get("history"):
            lines.append("**时间线（journal 尾部）**")
            lines.append("\n".join(f"- {h[:150]}" for h in ev["history"][-6:]))
    else:
        lines.append(f"**对象概况**\n- 路径：`{ev.get('path')}`")
        for e in ev.get("entries", [])[:8]:
            lines.append(
                f"- {e['name']}：{e['size']} 字节，mtime {e['mtime']}"
                + ("（目录）" if e["is_dir"] else "")
            )
        if ev.get("previews"):
            lines.append("**内容要点（预览，已脱敏）**")
            for pv in ev["previews"][:1]:
                text = pv["preview"][:500]
                lines.append(f"`{pv['path']}`\n{text}")
        else:
            lines.append(
                "**内容预览**：该路径下没有可预览的文本文件（可能只有目录/二进制）"
                "——这里没证据，需要你指定具体文件后我再讲。"
            )
        errs = _error_lines(ev)
        if errs:
            lines.append("**值得注意的异常行（日志/内容里出现 error/fail/watchdog 等）**")
            lines.extend(errs)
        else:
            lines.append("**异常扫描**：预览范围内未发现 error/fail 类字样——仅限预览范围，不代表全局无异常。")
    return "\n\n".join(lines)


def _draft(ev: dict, use_llm: bool, llm_timeout: int) -> tuple[str, str, str]:
    """生成 ① 一句话总结 + ② 分块讲解；LLM 失败/禁用 → 提取式，诚实标注。"""
    digest = _digest(ev)
    if not use_llm:
        return (
            _extractive_summary(ev, digest),
            _blocks_deterministic(ev),
            "extractive",
        )
    # 路径模式但没有任何内容预览：材料不足，禁止 LLM 编造细节（防反噬原则 1）
    if ev.get("kind") == "path" and not ev.get("previews"):
        return (
            "材料不足：该路径下没有可预览的文本内容（可能只有目录/二进制），"
            "需要你指定具体文件或扩大范围——这里没证据，老师不猜。",
            _blocks_deterministic(ev),
            "extractive",
        )
    try:
        summary = _ollama_chat(
            "下面是一次工作的证据摘要。请用一句话（不超过 60 字）总结这次工作干了什么。"
            "只依据材料，材料没有的就写'材料未说明'，不要编造。\n\n证据摘要:\n" + digest,
            "你是严谨的 AI 老师：只讲材料里有的，证据不足就明说。",
            _MAX_LLM_SUMMARY,
            llm_timeout,
        )
        blocks = _ollama_chat(
            "下面是一次工作的证据摘要。请用通俗中文分 2-4 块讲解：每块包含"
            "「改了什么/做了什么」「为什么」「怎么验证」「影响」。只依据材料："
            "日志/材料只能证明'发生了什么'，不能证明'为什么'——没有依据的'为什么'"
            "一律写'日志未说明原因'，禁止推测；禁止编造材料里没有的细节。"
            "每块不超过 120 字，用要点式短句，不要展开长篇。\n\n证据摘要:\n" + digest,
            "你是严谨的 AI 老师：引用式讲解，禁止编造。",
            _MAX_LLM_BLOCKS,
            llm_timeout,
        )
        if not summary or not blocks:
            raise RuntimeError("LLM 返回空内容")
        return summary[:200], blocks[:_MAX_LLM_BLOCKS * 3], "llm"
    except Exception as e:  # noqa: BLE001 —— 任何 LLM 故障都降级
        logger.warning(f"⚠️ explain_work LLM 不可用，降级提取式: {e}")
        return (
            _extractive_summary(ev, digest),
            _blocks_deterministic(ev),
            "extractive",
        )


def _checklist(ev: dict) -> list[str]:
    """③④ 之外的检查清单：3-5 条用户可亲自核实的点。"""
    kind = ev.get("kind")
    if kind == "milestone":
        return [
            f"打开 {ev.get('range')} 的 git log，逐条看 commit 主题是否与讲解一致。",
            f"核对 HEAD={ev.get('head')} 与 origin/master={ev.get('origin_master')} 是否一致。",
            "打开证据里的闸门报告全文，核对测试数与五轴结论。",
            "挑你最关心的一个变更，在代码里找到对应文件亲自看。",
            "如涉及功能改动，按 release notes 里的方法复跑一次真实验证。",
        ]
    if kind == "task":
        return [
            f"打开任务文件 {ev.get('source')}，核对标题/内容/状态与讲解一致。",
            f"查看 evidence 原文（{ev.get('result_path') or '任务文件内'}），确认交付物确实存在。",
            "对照 journal 时间线检查事件顺序（认领/完成/审批等）。",
            "如有 result_path，打开产物复跑其中的关键验证。",
        ]
    return [
        f"打开 {ev.get('path')} 查看原始内容，确认讲解的要点与原文一致。",
        "核对条目的大小/时间是否与你预期一致（判断是否最新版本）。",
        "若含日志且讲解标了异常行：确认这些异常是否影响你的实际使用。",
        "若内容含个人信息：确认讲解产物已脱敏，且不要外发本机 results/ 文件。",
        "决定下一步：继续深挖某一块，还是就此打住由你接手。",
    ]


_NEXT_OPTIONS = [
    "让它再讲细一点（点名某一块/某个文件）",
    "换一个对象讲（另一个版本区间/任务/文件）",
    "固化成更顺手的入口（桌面快捷方式 / 挂进办公流程）",
    "停止讲解，由你按检查清单亲自接手",
]


def _evidence_section(ev: dict) -> str:
    """③ 证据引用：所有说法对应的可定位来源。"""
    lines: list[str] = []
    kind = ev.get("kind")
    if kind == "milestone":
        for c in ev.get("commits", []):
            lines.append(f"- commit `{c['hash']}` {c['subject']}（{c['author']}）")
        if ev.get("diff_stat"):
            lines.append("- 变更统计：`git diff --stat " + ev["range"] + "`")
        if ev.get("release_notes"):
            lines.append(
                f"- Release Notes：`documents/release-notes-{ev.get('milestone')}.md`"
            )
        for g in ev.get("gate_reports", []):
            lines.append(f"- 闸门报告：{g['path']}")
        tests = set()
        for g in ev.get("gate_reports", []):
            tests.update(_TESTNAME_RE.findall(g["preview"]))
        for t in sorted(tests)[:8]:
            lines.append(f"- 测试类：`{t}`")
    elif kind == "task":
        lines.append(f"- 任务文件：{ev.get('source')}")
        if ev.get("result_path"):
            lines.append(f"- 交付产物：{ev.get('result_path')}")
        lines.append(f"- journal：`collab/journal/{ev.get('task_id')}.jsonl`")
    else:
        for e in ev.get("entries", []):
            lines.append(f"- {e['name']}（{e['size']} B，mtime {e['mtime']}）")
        for pv in ev.get("previews", []):
            lines.append(f"- 预览来源：{pv['path']}")
    lines.append("")
    lines.append("> 防反噬约定：本讲解没有引用任何无法指向上述证据的说法；证据不足处已标注。")
    return "\n".join(lines)


def render_lesson(ev: dict, summary: str, blocks: str, method: str) -> str:
    """拼装四段式讲解 Markdown。"""
    kind_label = {
        "milestone": f"里程碑 {ev.get('milestone')}",
        "task": f"任务 {ev.get('task_id')}",
        "path": "文件/目录",
    }.get(ev.get("kind"), "对象")
    head = [
        f"# AI 老师讲：{ev.get('title')}",
        f"> 对象：{kind_label}｜生成方式：{method}"
        f"（{ 'ollama qwen2.5:3b' if method == 'llm' else '提取式模板（无 LLM）' }）"
        f"｜纯只读：本次只收集证据，未修改任何被讲解对象。",
        "",
        "## 一、一句话总结",
        summary or "（未能生成总结——这里没证据，需要你亲自看。）",
        "",
        "## 二、分块人话讲解",
        blocks or "（未能生成讲解——这里没证据，需要你亲自看。）",
        "",
        "## 三、证据引用",
        _evidence_section(ev),
        "",
        "## 四、你应该亲自检查的点",
    ]
    for i, item in enumerate(_checklist(ev), 1):
        head.append(f"{i}. {item}")
    head.append("")
    head.append("**下一步方向（只给选项，不替你决定）**")
    for i, opt in enumerate(_NEXT_OPTIONS, 1):
        head.append(f"- {opt}")
    return "\n".join(head)


async def explain_work(
    milestone: str = "",
    task_id: str = "",
    path: str = "",
    title: str = "",
    out_path: str = "",
    use_llm: bool = True,
    llm_timeout: int = 180,
) -> str:
    """AI 老师：通俗讲解 agent 工作成果（纯只读，四段式输出）。

    Args:
        milestone: git 版本区间（tag 或 commit，如 "v3.21.0" / "v3.20.2..v3.21.0"）
        task_id: collab 任务 id（读 inbox/done + journal + result_path）
        path: 文件或目录（如 WorkBuddy 日志目录、办公产物）
        title: 可选，path 模式自定义标题
        out_path: 可选，讲解输出路径（默认 results/ai-teacher-<slug>-<时间>.md）
        use_llm: 是否尝试 ollama qwen2.5:3b（失败自动降级提取式）
        llm_timeout: LLM 调用超时秒数
    """
    denied = assert_identity_allowed("explain_work")
    if denied:
        return fail(denied)
    provided = sum(bool(x) for x in (milestone, task_id, path))
    if provided != 1:
        return fail("explain_work 需要且仅需要 milestone / task_id / path 之一")
    if milestone:
        repo = Path(os.environ.get("EXPLAIN_REPO_DIR") or Path(COLLAB_DIR).parent)
        ev = collect_milestone_evidence(str(repo), milestone)
    elif task_id:
        ev = collect_task_evidence(task_id)
    else:
        ev = collect_path_evidence(path, title)
    if ev.get("error"):
        return fail(ev["error"])

    summary, blocks, method = _draft(ev, use_llm, max(2, llm_timeout))
    lesson = render_lesson(ev, summary, blocks, method)

    if out_path:
        dest = Path(out_path)
    else:
        results_dir = Path(
            os.environ.get("EXPLAIN_RESULTS_DIR") or Path(COLLAB_DIR).parent / "results"
        )
        results_dir.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_-]+", "-", str(ev.get("title", "work")))
        dest = results_dir / f"ai-teacher-{slug}-{datetime.now():%Y%m%d-%H%M%S}.md"
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(lesson, encoding="utf-8")
    except OSError as e:
        return fail(f"无法写入讲解文件: {e}")

    logger.info(
        f"📖 AI 老师讲解完成 [{ev.get('kind')}] → {dest}（method={method}）"
    )
    return ok({
        "lesson": lesson,
        "output_path": str(dest),
        "method": method,
        "kind": ev.get("kind"),
        "read_only": True,
    })
