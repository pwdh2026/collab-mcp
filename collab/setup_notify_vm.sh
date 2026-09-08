#!/bin/bash
# setup_notify_vm.sh — 在 VM 上安装 notify_daemon 每分钟扫描（crontab）
# 用法: bash setup_notify_vm.sh

COLLAB_DIR="/mnt/hgfs/myshare/collab"
PY="/usr/local/bin/python3.11"

if [ ! -f "$PY" ]; then
    PY="python3"
fi

# 追加 cron 行（先去掉旧的 notify_daemon / dashboard 行，避免重复）
(
  crontab -l 2>/dev/null | grep -v "notify_daemon" | grep -v "dashboard.py" ;
  echo "* * * * * $PY $COLLAB_DIR/notify_daemon.py --once >> $COLLAB_DIR/notifications/cron.log 2>&1" ;
  echo "*/5 * * * * $PY $COLLAB_DIR/dashboard.py --out /mnt/hgfs/myshare/dashboard.html > /dev/null 2>&1"
) | crontab -

echo "已安装：notify_daemon 每 1 分钟 + 看板每 5 分钟"
crontab -l | grep -E "notify_daemon|dashboard.py"
