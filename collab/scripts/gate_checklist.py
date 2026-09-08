#!/usr/bin/env python3
"""验证闸门确定性层：gate_checklist（v3.11.0）。

设计来源：progress/collab-v3.11-gate-checklist-design.md
对标 alibaba/open-code-review「确定性工程 × Agent 混合」：给 PC-C 验证闸门加程序化核对，
解决通用 agent 审查两大痛点——覆盖不全（漏审）与位置漂移（file:line 对不上）。

用法：
  python gate_checklist.py --changes <repo> <range>        # 生成变更清单（可粘贴进派单 content）
  python gate_checklist.py --verify <repo> <range> <report> # 核对报告覆盖 + 意见位置

git 只读本地操作（diff --name-only / diff -U0），不 fetch、无网络面。
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path

# 模块 → 建议测试类（新增模块时补充；兜底=列出测试文件全部 Test 类）
_MODULE_TESTS = {
    "collab/collab_mcp/websearch.py": ["TestWebSearch", "TestWebSearchWigoloV31", "TestWebFetchWigoloV32",
                                "TestWebFetchScraplingV33", "TestWebFetchScraplingBrowserV34",
                                "TestWebResearchWigoloV35", "TestWebResearchWigoloV36"],
    "collab/collab_mcp/summarize.py": ["TestSummarizeV37"],
    "collab/collab_mcp/semantic.py": ["TestSemanticSearchV39"],
    "collab/collab_mcp/vision.py": ["TestVisionV312"],
    "collab/scripts/vision_capture.py": ["TestVisionCaptureV312"],
    "collab/collab_mcp/search.py": ["TestWebSearch"],
    "collab/collab_mcp/media.py": ["TestMediaTranscribeV300"],
    "collab/collab_mcp/tasks.py": ["TaskLifecycleTest"],
    "collab/collab_mcp/chat.py": ["ChatTest"],
    "collab/scripts/health_monitor.py": ["TestHealthMonitorV38"],
    "collab/scripts/gate_checklist.py": ["TestGateChecklistV311"],
}

_POS_RE = re.compile(r"([A-Za-z0-9_./\\-]+\.(?:py|js|ts|go|rs|java|c|h|md)):(\d+)")


def git_output(repo: str, args: list[str]) -> str:
    """git 只读命令输出；失败抛 RuntimeError（明确）。"""
    try:
        # bytes 捕获 + utf-8 解码（Windows 默认 GBK locale 会解不开 UTF-8 输出）
        # cwd=repo 而非 `git -C`（hub VM git 1.8.3.1 不支持 -C，需 git>=1.8.5；PC-C v3.11 验证确认）
        out = subprocess.run(["git", "-c", "core.quotepath=false"] + args,
                             capture_output=True, timeout=30, cwd=repo)
    except (OSError, subprocess.SubprocessError) as e:
        raise RuntimeError(f"git 命令执行失败: {e}") from e
    if out.returncode != 0:
        err = out.stderr.decode("utf-8", errors="replace").strip()[:200]
        raise RuntimeError(f"git {' '.join(args)} 失败: {err}")
    return out.stdout.decode("utf-8", errors="replace")


def parse_diff_changed_lines(diff_text: str) -> dict[str, set[int]]:
    """git diff -U0 → {path: set(新增行号)}（位置校验用，删除/上下文行不计）。"""
    result: dict[str, set[int]] = {}
    cur_path: str | None = None
    for line in diff_text.splitlines():
        if line.startswith("+++ b/"):
            cur_path = line[6:]
            result.setdefault(cur_path, set())
        elif line.startswith("@@"):
            if cur_path is None:
                continue
            m = re.search(r"\+(\d+)(?:,(\d+))?", line)
            if m:
                start = int(m.group(1))
                count = int(m.group(2) or "1")
                if count > 0:
                    result[cur_path].update(range(start, start + count))
    return result


def extract_positions(report_text: str) -> list[tuple[str, int]]:
    """从报告提取 (path, line) 意见位置。"""
    out = []
    for m in _POS_RE.finditer(report_text):
        path = m.group(1).replace("\\", "/").strip("()[]\"'`")
        out.append((path, int(m.group(2))))
    return out


def check_coverage(changed_files: list[str], report_text: str) -> tuple[list[str], list[str]]:
    """覆盖核对：变更文件在报告中出现的（basename 宽松匹配）vs 缺失。返回 (covered, missing)。"""
    covered = []
    missing = []
    for f in changed_files:
        if f in report_text or Path(f).name in report_text:
            covered.append(f)
        else:
            missing.append(f)
    return covered, missing


def check_positions(changed_lines: dict[str, set[int]],
                    positions: list[tuple[str, int]]) -> tuple[list[tuple[str, int]], list[tuple[str, int]]]:
    """位置校验：意见 (path, line) 的行号 ∈ 该文件变更新增行？返回 (ok, drifted)。"""
    ok_list = []
    drifted = []
    for path, line in positions:
        key = Path(path).name
        hit = any(Path(p).name == key and line in lines for p, lines in changed_lines.items())
        (ok_list if hit else drifted).append((path, line))
    return ok_list, drifted


def categorize(files: list[str]) -> tuple[list[str], list[str], list[str]]:
    """变更文件分类：源码 / 测试 / 文档。"""
    src, tests, docs = [], [], []
    for f in files:
        norm = f.replace("\\", "/")
        if norm.startswith("tests/") or norm.endswith("_test.py") or "test_server" in norm:
            tests.append(norm)
        elif norm.startswith("progress/") or norm.startswith("documents/") or norm.endswith(".md"):
            docs.append(norm)
        else:
            src.append(norm)
    return src, tests, docs


def list_test_classes(test_path: str) -> list[str]:
    try:
        text = Path(test_path).read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        raise RuntimeError(f"无法读取测试文件 {test_path}: {e}") from e
    return re.findall(r"^class (\w+)", text, re.M)


def suggest_tests(changed_files: list[str], test_path: str) -> list[str]:
    """建议测试类：映射表匹配 + 兜底（全部 Test 类）。"""
    suggested = []
    for f in changed_files:
        norm = f.replace("\\", "/")
        if norm in _MODULE_TESTS:
            suggested.extend(_MODULE_TESTS[norm])
    seen = set()
    out = []
    for c in suggested:
        if c not in seen:
            seen.add(c)
            out.append(c)
    if not out:
        out = list_test_classes(test_path)
    return out


def render_changes(repo: str, rev_range: str, test_path: str) -> str:
    """--changes：变更文件分类 + 建议测试类（markdown 片段，可粘贴进派单）。"""
    files = [l for l in git_output(repo, ["diff", "--name-only", rev_range]).splitlines() if l.strip()]
    if not files:
        raise RuntimeError(f"range {rev_range} 无变更文件")
    src, tests, docs = categorize(files)
    suggested = suggest_tests(files, test_path)
    lines = [
        f"### 变更清单（确定性层，git diff --name-only {rev_range}）",
        "",
        f"- 变更文件总数: {len(files)}（源码 {len(src)} / 测试 {len(tests)} / 文档 {len(docs)}）",
        "",
        "**源码变更:**",
    ]
    for f in src:
        lines.append(f"- `{f}`")
    if tests:
        lines.append("")
        lines.append("**测试变更:**")
        for f in tests:
            lines.append(f"- `{f}`")
    lines.append("")
    lines.append("**建议验证测试类（T2 targeted）:**")
    lines.append("```")
    lines.append("bash collab/scripts/verify_gate.sh " + " ".join(suggested[:6]))
    lines.append("```")
    lines.append("")
    lines.append("> 提示：报告需覆盖上述全部源码变更文件；意见请带 `file:line`（须在变更 diff 内）。")
    return "\n".join(lines)


def render_verify(repo: str, rev_range: str, report_path: str) -> str:
    """--verify：报告覆盖核对 + 位置校验。"""
    try:
        report_text = Path(report_path).read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        raise RuntimeError(f"无法读取报告 {report_path}: {e}") from e
    files = [l for l in git_output(repo, ["diff", "--name-only", rev_range]).splitlines() if l.strip()]
    diff_text = git_output(repo, ["diff", "-U0", rev_range])
    changed_lines = parse_diff_changed_lines(diff_text)
    covered, missing = check_coverage(files, report_text)
    positions = extract_positions(report_text)
    ok_pos, drifted = check_positions(changed_lines, positions)
    lines = [
        f"### 核对结果（确定性层，{rev_range} vs {report_path}）",
        "",
        f"- 变更文件: {len(files)}；报告中出现: {len(covered)}；**漏审候选: {len(missing)}**",
    ]
    for f in missing:
        lines.append(f"  - ⚠️ 未在报告中提及: `{f}`")
    lines.append(f"- 意见位置: {len(positions)} 处；在变更 diff 内: {len(ok_pos)}；**漂移候选: {len(drifted)}**")
    for path, line in drifted[:20]:
        lines.append(f"  - ⚠️ `{path}:{line}` 不在变更新增行")
    if not missing and not drifted:
        lines.append("- ✅ 覆盖完整 + 位置无漂移")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="验证闸门确定性层（gate_checklist）")
    parser.add_argument("--changes", nargs=2, metavar=("REPO", "RANGE"), help="生成变更清单")
    parser.add_argument("--verify", nargs=3, metavar=("REPO", "RANGE", "REPORT"), help="核对报告覆盖+位置")
    parser.add_argument("--test-path", default="",
                        help="测试文件路径（默认按 repo 推导：<repo>/collab/tests/test_server.py）")
    args = parser.parse_args(argv)
    try:
        if args.changes:
            repo = args.changes[0]
            test_path = args.test_path or str(Path(repo) / "collab" / "tests" / "test_server.py")
            print(render_changes(repo, args.changes[1], test_path))
        elif args.verify:
            print(render_verify(args.verify[0], args.verify[1], args.verify[2]))
        else:
            parser.print_help()
            return 2
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
