#!/bin/bash
# setup-tailscale-vm.sh
# 在 CentOS 7 VM 上安装并配置 Tailscale
# 用法: sudo bash setup-tailscale-vm.sh

set -e
echo "=== Tailscale VM 安装脚本 ==="

# 检查是否已安装
if command -v tailscale &> /dev/null; then
    echo "Tailscale 已安装，版本: $(tailscale version)"
    echo "跳过安装，直接启动..."
    sudo systemctl enable --now tailscaled
    echo ""
    echo "执行 sudo tailscale up --ssh 完成登录"
    exit 0
fi

echo "[1/4] 安装 yum-config-manager..."
sudo yum install -y yum-utils

echo "[2/4] 添加 Tailscale 仓库..."
sudo yum-config-manager --add-repo https://pkgs.tailscale.com/stable/centos/7/tailscale.repo

echo "[3/4] 安装 Tailscale..."
sudo yum install -y tailscale

echo "[4/4] 启动守护进程..."
sudo systemctl enable --now tailscaled

echo ""
echo "=== 安装完成 ==="
echo ""
echo "下一步："
echo "  sudo tailscale up --ssh"
echo ""
echo "会给你一个登录 URL，在浏览器里完成认证即可。"
echo "登录后记录 VM 的 Tailscale IP（100.x.x.x）。"
