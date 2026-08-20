#!/usr/bin/env bash
# ============================================================
# Polymarket Auto-Trader — Ubuntu/Debian native install
#   ./install.sh [/opt/polymarket-bot]
# Installs deps, creates a venv, and installs a systemd service.
# ============================================================
set -euo pipefail

APP_DIR="${1:-/opt/polymarket-bot}"

echo "==> System packages"
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip

echo "==> Installing to $APP_DIR"
sudo mkdir -p "$APP_DIR/data"
sudo cp auto_trader.py strategy_engine.py requirements.txt run_server.sh "$APP_DIR/"
sudo chmod +x "$APP_DIR/run_server.sh"

echo "==> Creating virtualenv"
sudo python3 -m venv "$APP_DIR/venv"
sudo "$APP_DIR/venv/bin/pip" install --upgrade pip >/dev/null
sudo "$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt"

echo "==> Installing systemd service"
sudo tee /etc/systemd/system/polymarket-bot.service >/dev/null <<EOF
[Unit]
Description=Polymarket Auto-Trader
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR
Environment=PYTHONUNBUFFERED=1
Environment=POLY_WORKSPACE=$APP_DIR/data
Environment=PORT=5001
ExecStart=$APP_DIR/venv/bin/python $APP_DIR/auto_trader.py
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable polymarket-bot

echo ""
echo "=============================================================="
echo "安装完成！"
echo "  启动:  sudo systemctl start polymarket-bot"
echo "  状态:  sudo systemctl status polymarket-bot"
echo "  日志:  sudo journalctl -u polymarket-bot -f"
echo "  界面:  http://服务器IP:5001"
echo ""
echo "安全建议（重要）:"
echo "  1. 首次访问界面 → 配置 → 输入钱包凭证，加密保存后授权"
echo "  2. 为保护远程界面，请设置访问密码:"
echo "     sudo nano $APP_DIR/data/trading_config.json"
echo "     在文件中把 web_password 改为你的密码，然后重启服务"
echo "  3. 不要将 5001 端口直接暴露到公网；建议用 SSH 隧道或 nginx+认证"
echo "=============================================================="
