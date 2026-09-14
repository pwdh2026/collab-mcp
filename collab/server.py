#!/usr/bin/env python3
"""Claude 协作 MCP Server — 多 Claude 实例协作消息中枢（入口）。

为多台电脑上的 Claude 提供统一的协作工具接口，
通过共享文件夹实现任务分配、消息传递等功能。

部署位置: CentOS VM 上的 /mnt/hgfs/myshare/collab/server.py
传输方式: stdio（默认，通过 SSH 连接 Claude 客户端）；可选 Streamable HTTP
启动命令: python3 /mnt/hgfs/myshare/collab/server.py

Python 版本要求: >= 3.10
依赖: mcp >= 2.0.0
"""

import asyncio
import os
import sys

# 保证直接以 `python3 <路径>/server.py` 运行时能导入同目录的 collab_mcp 包
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from collab_mcp import __version__
from collab_mcp.config import (
    COLLAB_DIR,
    LOG_FILE,
    ensure_directories,
)
from collab_mcp import http_auth
from collab_mcp.identity import current_identity
from collab_mcp.logging_setup import logger


def _enable_utf8_stdio() -> None:
    """强制 UTF-8 标准流。

    Windows 控制台默认 GBK：输入（MCP 协议报文）和输出（emoji/中文）
    都可能乱码。VM 的 UTF-8 环境无副作用。
    """
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _http_mode_requested() -> bool:
    """HTTP 模式由 --http 参数或 COLLAB_HTTP=1/true/yes/on 显式开启。"""
    env_flag = os.environ.get(http_auth.ENV_HTTP_MODE, "").strip().lower()
    return "--http" in sys.argv[1:] or env_flag in ("1", "true", "yes", "on")


async def main() -> None:
    """启动 MCP Server。

    使用 stdio 传输 — 标准输入/输出用于 MCP 协议通信。
    Claude 客户端通过 SSH 执行此脚本即可建立连接：
        ssh centos-vm python3 /mnt/hgfs/myshare/collab/server.py

    支持 CLI 参数：
        --version     显示版本信息
        --health      运行健康检查后退出（非 MCP 模式）
    """
    _enable_utf8_stdio()

    if len(sys.argv) > 1:
        if sys.argv[1] == "--version":
            print(f"Claude 协作 MCP Server v{__version__}")
            print("兼容 MCP 2.0 协议")
            return
        elif sys.argv[1] == "--health":
            # 非 MCP 模式：复用与 health_check 相同的检查逻辑，避免双份维护
            from collab_mcp.health import collect_health_checks

            checks, all_ok = collect_health_checks()
            print(f"Claude 协作 MCP Server — 环境检查 (v{__version__})")
            print(f"  协作目录: {COLLAB_DIR}")
            for name, value in checks.get("directories", {}).items():
                print(f"    {name}/ {value}")
            print(f"  Python: {checks.get('python')}")
            print(f"  MCP SDK: {checks.get('mcp_sdk')}")
            print(f"  sqlite3: {checks.get('sqlite3')}")
            print(f"  磁盘: {checks.get('disk_free')}")
            if checks.get("log_size"):
                print(f"  日志: {checks['log_size']}")
            print(f"  状态: {'全部通过 ✅' if all_ok else '有问题 ⚠️'}")
            return

    if _http_mode_requested():
        token = http_auth.configured_http_token()
        if not token:
            print(
                "启动 Streamable HTTP 需要设置 COLLAB_HTTP_BEARER_TOKEN，"
                "拒绝在无鉴权状态下开放 HTTP 端口。",
                file=sys.stderr,
            )
            raise SystemExit(2)

        # 白名单校验：token 只能映射到授权身份（COLLAB_HTTP_ALLOWED_IDENTITIES 未设则跳过）。
        denied = http_auth.validate_http_identity()
        if denied:
            print(f"{denied} 请修正配置后重试。", file=sys.stderr)
            raise SystemExit(2)

        # 静态 token 对应一个明确身份；HTTP handler 只读全局环境，启动前先注入。
        http_identity = http_auth.http_identity()
        os.environ["COLLAB_IDENTITY"] = http_identity

    # v3.36.3：app 在 import 期就会按当前环境构建静态 token verifier。
    # 必须在上面（token 必填 + 身份白名单）校验与身份注入之后才 import，
    # 保证 verifier 采用的正是被校验过的身份，消除两处取值的时序漂移。
    from collab_mcp.app import TOOL_COUNT, server

    ensure_directories()
    logger.info("=" * 40)
    logger.info(f"Claude 协作 MCP Server v{__version__} 已启动")
    logger.info(f"协作目录: {COLLAB_DIR}")
    logger.info(f"日志文件: {LOG_FILE}")
    logger.info(f"调用身份: {current_identity() or '未设置（本地/未知）'}")
    logger.info(f"MCP 工具: {TOOL_COUNT} 个")
    logger.info("等待 Claude 客户端连接...")
    logger.info("=" * 40)

    try:
        if _http_mode_requested():
            host = os.environ.get("COLLAB_HTTP_HOST", "127.0.0.1").strip() or "127.0.0.1"
            port = int(os.environ.get("COLLAB_HTTP_PORT", "8000").strip() or "8000")
            path = os.environ.get("COLLAB_HTTP_PATH", "/mcp").strip() or "/mcp"
            logger.info(f"Streamable HTTP 监听: http://{host}:{port}{path}")
            logger.info(f"HTTP 鉴权: 静态 Bearer Token（身份 {current_identity()}）")
            await server.run_streamable_http_async(
                host=host,
                port=port,
                streamable_http_path=path,
            )
        else:
            await server.run_stdio_async()
    except KeyboardInterrupt:
        logger.info("收到中断信号，正在退出...")
    except Exception as e:
        logger.error(f"Server 异常退出: {e}", exc_info=True)
        raise


if __name__ == "__main__":
    asyncio.run(main())
