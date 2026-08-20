#!/usr/bin/env bash
# ============================================================
# Polymarket Auto-Trader — 一键在线安装（curl | bash）
#
# 用法（在云服务器上执行，无需先上传任何文件）：
#   bash <(curl -sSL https://raw.githubusercontent.com/Gavin-Space/polymarket-auto-trader/main/deploy/install-online.sh)
#
# 可选环境变量：
#   POLY_REPO_URL  仓库地址（默认官方仓库，fork 后改这里）
#   POLY_APP_DIR   安装目录（默认 $HOME/polymarket-auto-trader）
#   POLY_PORT      服务端口（默认 5000）
#   POLY_TAG       固定安装到某个版本标签（如 v1.0.0，便于云服务器锁版本）
# ============================================================
set -euo pipefail

REPO_URL="${POLY_REPO_URL:-https://github.com/Gavin-Space/polymarket-auto-trader.git}"
APP_DIR="${POLY_APP_DIR:-$HOME/polymarket-auto-trader}"
PORT="${POLY_PORT:-5000}"
TAG="${POLY_TAG:-}"
RUN_USER="$(id -un)"
PYTHON="${PYTHON:-python3}"

echo "=============================================================="
echo "  Polymarket Auto-Trader 一键安装"
echo "  用户: $RUN_USER"
echo "  目录: $APP_DIR"
echo "  端口: $PORT"
echo "=============================================================="

# ---------- 1. 系统依赖 ----------
echo "==> 安装系统依赖"
sudo apt-get update -y
sudo apt-get install -y git python3 python3-venv python3-pip build-essential cargo 2>/dev/null || \
sudo apt-get install -y git python3 python3-venv python3-pip

# ---------- 2. 克隆 / 更新代码 ----------
if [ -d "$APP_DIR/.git" ]; then
  echo "==> 仓库已存在，git pull 更新..."
  (cd "$APP_DIR" && git fetch --tags && git pull --ff-only) || true
else
  echo "==> 克隆代码"
  git clone "$REPO_URL" "$APP_DIR"
fi
cd "$APP_DIR"
# 固定到指定版本标签（如 v1.0.0），便于云服务器锁定稳定版本
if [ -n "$TAG" ]; then
  echo "==> 固定到版本 $TAG"
  (git checkout "$TAG" 2>/dev/null) || echo "警告：标签 $TAG 不存在，保持 main"
fi

# ---------- 3. 虚拟环境 + 依赖 ----------
echo "==> 创建虚拟环境并安装依赖（deploy/requirements.txt，含全部依赖）"
if [ ! -d venv ]; then
  "$PYTHON" -m venv venv
fi
./venv/bin/pip install --upgrade pip >/dev/null
./venv/bin/pip install -r deploy/requirements.txt

# ---------- 4. 数据目录 ----------
mkdir -p data

# ---------- 5. systemd 服务 ----------
echo "==> 安装 systemd 服务"
sudo tee /etc/systemd/system/polymarket-bot.service >/dev/null <<EOF
[Unit]
Description=Polymarket Auto-Trader
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$APP_DIR
Environment=PYTHONUNBUFFERED=1
Environment=POLY_WORKSPACE=$APP_DIR/data
ExecStart=$APP_DIR/venv/bin/python $APP_DIR/auto_trader.py
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable polymarket-bot >/dev/null 2>&1 || true

echo ""
echo "=============================================================="
echo "  安装完成 ✅"
echo ""
echo "  启动服务:"
echo "    sudo systemctl start polymarket-bot"
echo "  查看状态:"
echo "    sudo systemctl status polymarket-bot"
echo "  实时日志:"
echo "    sudo journalctl -u polymarket-bot -f"
echo ""
echo "  访问界面:"
echo "    http://<服务器IP>:$PORT"
echo ""
echo "  安全配置（重要）:"
echo "    sudo nano $APP_DIR/data/trading_config.json"
echo "    把 \"web_password\": \"\" 改成你的密码，然后: sudo systemctl restart polymarket-bot"
echo ""
echo "  推荐用 SSH 隧道访问（不暴露端口）:"
echo "    在本机执行: ssh -L 5000:127.0.0.1:5000 $RUN_USER@<服务器IP>"
echo "    然后浏览器打开 http://localhost:5000"
echo ""
echo "  升级（以后）:"
echo "    cd $APP_DIR && git pull && sudo systemctl restart polymarket-bot"
echo "=============================================================="
