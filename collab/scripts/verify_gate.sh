#!/usr/bin/env bash
# verify_gate.sh — PC-C 验证闸门一键脚本（在 hub VM 上运行）
#
# 背景：VM 通过 VMware 共享文件夹挂载宿主 repo（/mnt/hgfs/myshare = 宿主目录，同一 .git），
#       因此无需 git pull（VM 上 git pull 必因 known_hosts 缺失报错），用 rev-parse 核对即可。
#
# 用法：
#   bash collab/scripts/verify_gate.sh                                            # 全量测试（T1 闸门）
#   bash collab/scripts/verify_gate.sh test_server.TestWebFetchWigoloV32          # 只跑新测试类（T2 闸门）
#
# 输出：repo 核对（HEAD vs origin/master）、工作区 diff -w --stat、测试统计；退出码=测试退出码。
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
COLLAB_DIR="$REPO_ROOT/collab"
cd "$COLLAB_DIR" || exit 2

echo "== 1) repo 核对（共享 repo，无需 git pull）=="
HEAD="$(git rev-parse HEAD 2>/dev/null)"
ORIGIN="$(git rev-parse origin/master 2>/dev/null)"
echo "HEAD         = ${HEAD:-<no HEAD>}"
echo "origin/master= ${ORIGIN:-<no origin/master>}"
if [ -n "$HEAD" ] && [ "$HEAD" = "$ORIGIN" ]; then
  echo "OK: HEAD == origin/master"
else
  echo "WARN: HEAD != origin/master（工作区可能落后；验证对象以 HEAD 为准，报告中注明）"
fi

echo
echo "== 2) 工作区 =="
git status --short | head -30
echo "--- git diff -w --stat（容忍存量 CRLF/LF 噪声）---"
git diff -w --stat | tail -25

echo
echo "== 3) 测试 =="
PY="$(command -v python3.11 || command -v python3)"
if [ -z "$PY" ]; then
  echo "ERROR: 未找到 python3.11/python3"
  exit 2
fi
echo "python: $PY ($("$PY" --version 2>&1))"
if [ "$#" -ge 1 ]; then
  echo "targeted: $*"
  # 中1/中2（PC-C 闸门 8ef9a726bdc2 / 1d8b49c7aee7）：tests/ 无 __init__.py，用 PYTHONPATH=tests 使 test_server 可 import
  PYTHONPATH="$COLLAB_DIR/tests" "$PY" -m unittest "$@" -v
else
  "$PY" -m unittest discover -s tests
fi
RC=$?
echo
echo "== 4) 完成：测试退出码=$RC（0=全绿）=="
echo "把 1/2 步核对输出 + 测试统计抄入 results/pc-c-<版本>-verify-<日期>.md"
exit $RC