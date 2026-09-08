#!/usr/bin/env python3
"""CLI：生成静态只读看板 HTML（P2-2）。

用法：
    python dashboard.py                     # 输出到共享根目录 dashboard.html
    python dashboard.py --out C:\\tmp\\d.html

纯 stdlib，无第三方依赖；适合挂 cron 定期刷新看板。
"""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from collab_mcp.config import COLLAB_DIR  # noqa: E402
from collab_mcp.dashboard import render_dashboard_html  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 Claude 协作静态看板 HTML")
    parser.add_argument("--out", default="", help="输出路径（默认: 共享根目录/dashboard.html）")
    args = parser.parse_args()

    target = Path(args.out) if args.out else COLLAB_DIR.parent / "dashboard.html"
    if not target.is_absolute():
        target = COLLAB_DIR.parent / target
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_dashboard_html(), encoding="utf-8")
    print(f"看板已生成: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
