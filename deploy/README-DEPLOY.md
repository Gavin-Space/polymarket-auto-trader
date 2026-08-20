# Polymarket Auto-Trader — 远程服务器部署指南

把本 `deploy/` 目录上传到你的 Linux 服务器（Ubuntu/Debian 推荐），按下面的方式运行。
完整的仪表盘（授权、交易、监控、设置）会自动运行在 **5001 端口**（A股主界面 AShareAuto 占用 5000）。

> ⚠️ **重要安全提示**：仪表盘没有内置登录，请务必配合下面的防护措施，
> 不要直接把 5001 端口暴露到公网。

---

## 一、方式 A：Docker（推荐）

前提：服务器已安装 Docker 与 Docker Compose。

```bash
cd deploy
docker compose up -d --build        # 构建并启动
docker compose logs -f              # 查看日志
docker compose down                 # 停止
```

数据（交易状态、加密凭证、配置、日志）持久化在 `deploy/data/`。

访问方式（二选一）：
1. **SSH 隧道（最简单）**——在你的电脑上：
   ```bash
   ssh -L 5001:127.0.0.1:5001 用户名@服务器IP
   ```
   然后浏览器打开 `http://localhost:5001`。
2. 用 nginx 反代并加 Basic Auth（见文末）。

---

## 二、方式 B：原生 systemd（不需要 Docker）

```bash
cd deploy
chmod +x install.sh
./install.sh /opt/polymarket-bot     # 安装到 /opt/polymarket-bot
```

管理命令：

```bash
sudo systemctl start polymarket-bot   # 启动
sudo systemctl status polymarket-bot  # 状态
sudo journalctl -u polymarket-bot -f  # 实时日志
sudo systemctl stop polymarket-bot    # 停止
```

也可用 `run_server.sh`（nohup 方式，不用 systemd）。

---

## 三、首次使用

1. 打开仪表盘（通过 SSH 隧道或本地访问）。
2. 点 **「配置」**：输入你的 Polygon 钱包私钥、钱包地址、**初始资金**、Telegram 推送等，点「加密保存」（私钥用密码 AES 加密存储）。
3. 点 **「授权并启动」**：输入加密密码，机器人自动开始扫描、分析、下单。
4. **「设置」** 面板可随时调整：初始资金、扫描间隔、最大持仓、最小置信度、最小 EV、**电竞过滤开关**、加密边界过滤、各策略开关等。修改初始资金会自动重置统计，保证数据一致。

> 建议先用 **模拟盘（dry_run）** 观察一段时间，确认策略符合预期再切换实盘。

---

## 四、保护远程仪表盘（务必阅读）

### 1) 设置访问密码（内置 Basic Auth）
编辑数据目录下的 `trading_config.json`（Docker 在 `deploy/data/`，systemd 在 `/opt/polymarket-bot/data/`），把：

```json
"web_password": ""
```

改成你的密码，例如：

```json
"web_password": "my-secret-123"
```

重启服务后，访问任何页面都会要求输入用户名 `admin` + 该密码。

### 2) 不要直接暴露 5001 端口
Docker 方式已绑定 `127.0.0.1`，公网访问不到，只能通过 SSH 隧道。原生 systemd 方式 Flask 监听 `0.0.0.0:5001`，请用防火墙限制：

```bash
sudo ufw deny 5001/tcp        # 或配置只允许你的 IP
```

### 3) （可选）nginx 反代 + Basic Auth
```nginx
server {
    listen 443 ssl;
    server_name bot.example.com;
    # ssl_certificate ...;

    auth_basic "PolyAuto";
    auth_basic_user_file /etc/nginx/.htpasswd;   # htpasswd -c .htpasswd admin

    location / {
        proxy_pass http://127.0.0.1:5001;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_read_timeout 3600;
    }
}
```

---

## 五、常见问题

- **交易一直失败 / 网络错误**：本机代理（Clash 等）可能导致 HTTPS 握手失败。
  仪表盘的授权弹窗里有「网络诊断」按钮可定位；服务器上如需代理请设置 `HTTP_PROXY`/`HTTPS_PROXY`。
- **端口被占用**：`sudo ss -lntp | grep 5001` 找到占用进程。
- **防火墙**：SSH 隧道不受影响，但直连需要放行端口（不建议）。
- **数据备份**：Docker 备份 `deploy/data/`，systemd 备份 `/opt/polymarket-bot/data/`（含 `bot_state.json`、`trading_config.json`、`.encrypted_credentials`）。

---

## 六、文件说明

| 文件 | 作用 |
|------|------|
| `auto_trader.py` | 主程序（交易引擎 + Web 仪表盘，HTML 内嵌） |
| `strategy_engine.py` | 增强策略引擎（6 策略 + 订单簿/鲸鱼/价格分析） |
| `requirements.txt` | Linux 依赖（从 PyPI 安装） |
| `Dockerfile` / `docker-compose.yml` | Docker 一键部署 |
| `install.sh` | 原生 systemd 一键安装 |
| `run_server.sh` | 无 systemd 时的 nohup 启动脚本 |
| `.env.example` | 配置参考（实际参数以 `trading_config.json` 为准） |

> 注意：Windows 本地使用的 `libs/` 目录包含 `.pyd` 二进制，仅适用于 Windows；
> Linux 服务器请用 pip 安装依赖，本包已为你准备好 `requirements.txt`。
