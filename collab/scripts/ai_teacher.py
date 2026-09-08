"""AI 老师 CLI（explain_work 纯只读讲解，v3.22.0）。

用法示例：
    python collab/scripts/ai_teacher.py --milestone v3.21.0
    python collab/scripts/ai_teacher.py --task 0095655d3aea
    python collab/scripts/ai_teacher.py --path "/path/to/some-agent/logs" --title "某助手近况"
"""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

os.environ.setdefault("COLLAB_DIR", str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from collab_mcp.ai_teacher import explain_work  # noqa: E402


async def _main(args: argparse.Namespace) -> None:
    r = json.loads(
        await explain_work(
            milestone=args.milestone or "",
            task_id=args.task or "",
            path=args.path or "",
            title=args.title or "",
            out_path=args.out or "",
            use_llm=not args.no_llm,
            llm_timeout=args.llm_timeout,
        )
    )
    if not r.get("success"):
        print(json.dumps(r, ensure_ascii=False, indent=2), file=sys.stderr)
        sys.exit(1)
    print(r.get("lesson", ""))
    print(f"\n--- 讲解已保存: {r.get('output_path')} ---")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(
        description="AI 老师：通俗讲解 agent 工作成果（纯只读，四段式：总结/讲解/证据/检查清单）"
    )
    parser.add_argument("--milestone", help="git 版本区间（tag 或 commit，如 v3.21.0）")
    parser.add_argument("--task", help="collab 任务 id")
    parser.add_argument("--path", help="文件/目录（如 WorkBuddy 日志或办公产物）")
    parser.add_argument("--title", default="", help="可选，path 模式自定义标题")
    parser.add_argument("--out", default="", help="可选，输出文件路径")
    parser.add_argument("--no-llm", action="store_true", help="禁用 ollama，只用提取式")
    parser.add_argument("--llm-timeout", type=int, default=180, help="LLM 超时秒数")
    asyncio.run(_main(parser.parse_args()))
