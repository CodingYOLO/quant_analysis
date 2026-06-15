#!/bin/bash
# 部署到国内云服务器（阿里云/腾讯云轻量）
# 使用前修改 SERVER_IP 和 SERVER_USER
#
# 服务器要求：Ubuntu 22.04，2核4G，国内节点（解决VPN问题）
# 预计费用：~80元/月（阿里云轻量应用服务器）

SERVER_IP="YOUR_SERVER_IP"        # ← 修改为你的服务器IP
SERVER_USER="ubuntu"               # ← 修改为服务器用户名
PROJECT_DIR="/home/$SERVER_USER/astock-agent"

echo "=== 部署到 $SERVER_USER@$SERVER_IP ==="

# 1. 同步代码
rsync -avz --exclude='.venv' --exclude='data_cache' --exclude='reports' --exclude='logs' \
  /Users/vivianjin/Documents/quant/quant_stock_analysis/astock-agent/ \
  "$SERVER_USER@$SERVER_IP:$PROJECT_DIR/"

# 2. 服务器上安装依赖 & 配置
ssh "$SERVER_USER@$SERVER_IP" << 'REMOTE'
cd ~/astock-agent

# 安装 Python 3.11（如未安装）
if ! command -v python3.11 &> /dev/null; then
    sudo apt-get update -y
    sudo apt-get install -y python3.11 python3.11-venv python3.11-pip
fi

# 创建虚拟环境
python3.11 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e ".[dev]" --quiet

# 创建必要目录
mkdir -p logs data_cache reports

echo "✅ 依赖安装完成"
REMOTE

echo "=== 部署完成 ==="
echo ""
echo "接下来手动操作："
echo "1. scp .env $SERVER_USER@$SERVER_IP:$PROJECT_DIR/.env  （上传密钥配置）"
echo "2. ssh $SERVER_USER@$SERVER_IP"
echo "3. crontab -e  （添加定时任务）"
echo "   5 16 * * 1-5 $PROJECT_DIR/scripts/run_daily.sh"
echo ""
echo "测试运行："
echo "ssh $SERVER_USER@$SERVER_IP '$PROJECT_DIR/.venv/bin/python -m app.run run --no-notify'"
