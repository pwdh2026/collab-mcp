#!/bin/bash
# check_ports.sh — Claude 协作平台连通性测试
# 用法: bash check_ports.sh [中枢IP]
# 默认中枢 IP: 127.0.0.1（本地；跨机请传入你的中枢地址，例如 192.168.x.x）

HUB_IP="${1:-127.0.0.1}"
ERRORS=0

echo ""
echo "╔══════════════════════════════════════╗"
echo "║  Claude 协作平台 — 连通性自检 v2.0  ║"
echo "╠══════════════════════════════════════╣"
echo "║  中枢 IP: ${HUB_IP}"
echo "║  时间:    $(date '+%Y-%m-%d %H:%M:%S')"
echo "╚══════════════════════════════════════╝"
echo ""

# [1/6] Ping
echo -n "[1/6] Ping ${HUB_IP} ... "
if ping -n 1 "$HUB_IP" > /dev/null 2>&1 || ping -c 1 "$HUB_IP" > /dev/null 2>&1; then
    echo "✅ 可达"
else
    echo "❌ 不可达 — 检查是否同一局域网，路由器是否开启 AP 隔离"
    ((ERRORS++))
fi

# [2/6] TCP 端口扫描
echo ""
echo "[2/6] TCP 端口扫描 ..."
for PORT in 8022 80 9000 8080; do
    echo -n "  ${PORT}: "
    if timeout 3 bash -c "echo > /dev/tcp/${HUB_IP}/${PORT}" 2>/dev/null; then
        echo "✅ 开放"
    else
        echo "❌ 不通 — 通知中枢管理员检查防火墙 + VMware NAT"
        ((ERRORS++))
    fi
done

# [3/6] SSH 密钥认证
echo ""
echo -n "[3/6] SSH 密钥认证 (端口 8022) ... "
SSH_RESULT=$(ssh -i ~/.ssh/collab_key -p 8022 \
    -o ConnectTimeout=5 \
    -o StrictHostKeyChecking=no \
    -o BatchMode=yes \
    root@"$HUB_IP" "echo OK" 2>&1)

if echo "$SSH_RESULT" | grep -q "OK"; then
    echo "✅ 认证成功"
elif echo "$SSH_RESULT" | grep -qi "Permission denied"; then
    echo "❌ 密钥被拒 — 通知管理员添加你的公钥到 authorized_keys"
    ((ERRORS++))
else
    echo "❌ 连接失败: $(echo "$SSH_RESULT" | head -1)"
    ((ERRORS++))
fi

# [4/6] MCP Server 版本
echo ""
echo -n "[4/6] MCP Server 状态 ... "
MCP_VER=$(ssh -i ~/.ssh/collab_key -p 8022 \
    -o ConnectTimeout=5 \
    -o StrictHostKeyChecking=no \
    root@"$HUB_IP" \
    "/usr/local/bin/python3.11 /mnt/hgfs/myshare/collab/server.py --version" 2>/dev/null)

if echo "$MCP_VER" | grep -q "Claude"; then
    echo "✅ $(echo "$MCP_VER" | head -1)"
else
    echo "❌ Server 无响应"
    ((ERRORS++))
fi

# [5/6] 协作目录
echo ""
echo -n "[5/6] 协作目录 ... "
DIR_STATUS=$(ssh -i ~/.ssh/collab_key -p 8022 \
    -o ConnectTimeout=5 \
    -o StrictHostKeyChecking=no \
    root@"$HUB_IP" \
    "ls /mnt/hgfs/myshare/collab/inbox/ /mnt/hgfs/myshare/collab/done/ /mnt/hgfs/myshare/collab/chat/ 2>/dev/null && echo DIR_OK" 2>/dev/null)

if echo "$DIR_STATUS" | grep -q "DIR_OK"; then
    echo "✅ 可访问"
else
    echo "⚠️  共享文件夹可能未挂载 — 不影响 SSH 直连"
fi

# [6/6] 环境健康检查
echo ""
echo -n "[6/6] 环境健康检查 ... "
HEALTH=$(ssh -i ~/.ssh/collab_key -p 8022 \
    -o ConnectTimeout=5 \
    -o StrictHostKeyChecking=no \
    root@"$HUB_IP" \
    "/usr/local/bin/python3.11 /mnt/hgfs/myshare/collab/server.py --health" 2>/dev/null)

if echo "$HEALTH" | grep -q "可读写"; then
    echo "✅ 全部通过"
    echo "$HEALTH" | sed 's/^/  /'
else
    echo "⚠️  部分检查失败"
    echo "$HEALTH" | sed 's/^/  /'
fi

# ===================================================================
# 总结
# ===================================================================
echo ""
echo "═══════════════════════════════════════"
if [ $ERRORS -eq 0 ]; then
    echo "  🎉 全部通过！MCP 配置可以正常使用。"
    echo ""
    echo "  在 Claude 中试试:"
    echo "    \"查看协作状态\""
    echo "    应该返回 pending/done/chat 计数"
else
    echo "  ⚠️  发现 ${ERRORS} 个问题，请按序号排查。"
    echo ""
    echo "  常见修复:"
    echo "    [1] → 检查路由器/WiFi 连接"
    echo "    [2] → 通知中枢管理员: netsh advfirewall ..."
    echo "    [3] → 发送公钥给管理员: cat ~/.ssh/collab_key.pub"
    echo "    [4-6] → 通知中枢管理员检查 VM 状态"
fi
echo "═══════════════════════════════════════"
echo ""
