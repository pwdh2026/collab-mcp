#!/bin/bash
# ============================================================
# CentOS 7 协作 MCP Server 环境安装脚本
# 在虚拟机上以 root 运行: bash /mnt/hgfs/myshare/collab/setup_vm.sh
# ============================================================
set -e

echo "========================================"
echo " Claude 协作 MCP Server — 环境安装"
echo "========================================"

# ---- 1. 安装 Python 3.11 (从源码编译, CentOS 7 无高版本 RPM) ----
echo ""
echo "[1/4] 安装编译依赖..."
yum install -y gcc openssl-devel bzip2-devel libffi-devel zlib-devel make wget

if [ ! -f /usr/local/bin/python3.11 ]; then
    echo "[2/4] 下载并编译 Python 3.11..."
    cd /tmp
    wget -q https://www.python.org/ftp/python/3.11.9/Python-3.11.9.tgz
    tar xzf Python-3.11.9.tgz
    cd Python-3.11.9
    ./configure --enable-optimizations --prefix=/usr/local
    make -j$(nproc)
    make altinstall
    cd /tmp
    rm -rf Python-3.11.9*
    echo "Python 3.11 安装完成"
else
    echo "[2/4] Python 3.11 已安装，跳过"
fi

# ---- 3. 安装 MCP SDK + 项目依赖 (v2.7.4: pysqlite3 + 测试可选依赖) ----
echo ""
echo "[3/4] 安装 MCP SDK 与项目依赖..."
/usr/local/bin/python3.11 -m pip install --upgrade pip
/usr/local/bin/python3.11 -m pip install -r /mnt/hgfs/myshare/collab/requirements.txt
/usr/local/bin/python3.11 -m pip install pysqlite3-binary
/usr/local/bin/python3.11 -m pip install -r /mnt/hgfs/myshare/collab/requirements-dev.txt

# ---- 4. 验证 ----
echo ""
echo "[4/4] 验证安装..."
/usr/local/bin/python3.11 --version
/usr/local/bin/python3.11 -c "import mcp; print('MCP SDK', mcp.__version__)"

# ---- 完成 ----
echo ""
echo "========================================"
echo " 安装完成！"
echo ""
echo " 测试运行:"
echo "   /usr/local/bin/python3.11 /mnt/hgfs/myshare/collab/server.py"
echo ""
echo " Claude 客户端配置命令:"
echo "   ssh centos-vm /usr/local/bin/python3.11 /mnt/hgfs/myshare/collab/server.py"
echo "========================================"
