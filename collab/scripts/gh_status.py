#!/usr/bin/env python3
"""GitHub 自动化巡检脚本（独立 CLI，非 MCP 工具）。

聚合账号下仓库的 issue/PR/release/最近提交 + 主仓库 CI 状态，
生成 Markdown 状态报告到 results/gh-status-YYYYMMDD.md。
配合 add_document 可摄入团队知识库（documents/）供 search_documents 检索。

用法：
    python collab/scripts/gh_status.py [YYYYMMDD]

依赖：gh CLI 已登录（gh auth status 确认）；网络可达 github.com。
"""

import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

# 要巡检的仓库列表：用环境变量注入（逗号分隔），如
#   GH_STATUS_REPOS="octocat/Hello-World,you/your-repo" python scripts/gh_status.py
REPOS = [r.strip() for r in os.environ.get("GH_STATUS_REPOS", "").split(",") if r.strip()]
SHARED_ROOT = Path(__file__).resolve().parent.parent.parent
RESULTS_DIR = SHARED_ROOT / "results"


def gh_api(path: str):
    p = subprocess.run(
        ["gh", "api", path],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    if p.returncode != 0:
        return None
    try:
        return json.loads(p.stdout)
    except json.JSONDecodeError:
        return None


def main() -> None:
    day = sys.argv[1] if len(sys.argv) > 1 else date.today().strftime("%Y%m%d")
    lines = [f"# GitHub 自动化状态报告（{day[:4]}-{day[4:6]}-{day[6:]}）", ""]
    lines.append("> 生成方式：gh API 聚合仓库 issue/PR/release/提交/CI 状态。")
    lines.append("")
    for repo in REPOS:
        lines.append(f"## {repo}")
        lines.append("")
        issues = gh_api(f"repos/{repo}/issues?state=open&per_page=20") or []
        pulls = gh_api(f"repos/{repo}/pulls?state=open&per_page=20") or []
        releases = gh_api(f"repos/{repo}/releases?per_page=5") or []
        commits = gh_api(f"repos/{repo}/commits?per_page=5") or []

        lines.append(f"**Open Issues: {len(issues)}**")
        for i in issues or []:
            labels = ",".join(l["name"] for l in (i.get("labels") or [])) or "-"
            lines.append(f"- #{i['number']} {i['title']}（labels: {labels}，{i['created_at'][:10]}）")
        if not issues:
            lines.append("- 无")
        lines.append("")

        lines.append(f"**Open PRs: {len(pulls)}**")
        for p in pulls or []:
            lines.append(f"- #{p['number']} {p['title']}（{p['user']['login']}，{p['created_at'][:10]}）")
        if not pulls:
            lines.append("- 无")
        lines.append("")

        lines.append(f"**Releases: {len(releases)}**")
        for r in releases or []:
            lines.append(f"- {r['tag_name']}（{r.get('name') or '-'}，{r['published_at'][:10]}）")
        if not releases:
            lines.append("- 无")
        lines.append("")

        lines.append(f"**最近提交（{len(commits)}）**")
        for c in commits or []:
            msg = (c.get("commit") or {}).get("message", "").splitlines()[0]
            lines.append(f"- {c['sha'][:7]} {msg[:80]}")
        lines.append("")

    run = subprocess.run(
        ["gh", "run", "list", "-R", REPOS[0], "--limit", "3",
         "--json", "databaseId,status,conclusion,headSha,createdAt"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    lines.append(f"## {REPOS[0]} CI 最近 3 次")
    if run.returncode == 0:
        for r in json.loads(run.stdout):
            lines.append(f"- run {r['databaseId']} {r['status']} {r['conclusion'] or '-'} @ {r['headSha'][:7]}")
    else:
        lines.append("- 查询失败")
    lines.append("")
    lines.append("---")
    lines.append("报告由 gh API 自动化生成（collab/scripts/gh_status.py）。")

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = RESULTS_DIR / f"gh-status-{day}.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"written: {out}")
    print("摄入命令：add_document(source_path=<上述路径>, skip_duplicates=true)")


if __name__ == "__main__":
    main()
