"""日志 — 同时输出到 stderr（实时）和文件（持久化），自动大小轮转。"""

import logging
import sys
from logging.handlers import RotatingFileHandler

from .config import LOG_FILE

# 确保日志目录存在（COLLAB_DIR 被指到新位置时也能直接启动）
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

# RotatingFileHandler：超过 10MB 自动归档 .1/.2/.3，且轮转后自动指向新文件
# （修复旧实现中"轮转后 FileHandler 仍写归档文件"的问题）
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stderr),
        RotatingFileHandler(
            LOG_FILE,
            maxBytes=10 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        ),
    ],
)
logger = logging.getLogger("collab-mcp")
