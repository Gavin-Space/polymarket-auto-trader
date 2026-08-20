# 部署到甲骨文（Oracle Cloud）免费层 — 4 核 × 24GB

> 目标实例：`VM.Standard.A1.Flex`（Ampere ARM 架构，4 OCPU + 24GB RAM），Ubuntu 24.04 或 Debian 12
> 本指南覆盖：创建实例 → 上传代码 → 原生 systemd 安装（推荐）或 Docker → 安全配置 → 首次使用

---

## ⚠️ 部署前必须知道的 3 件事

### 1. ARM 架构（aarch64）
免费层是 **ARM 处理器**。本项目依赖 `py-clob-client` → `ckzg`，PyPI 上有 ARM 版预编译包（manylinux aarch64），`pip install` 直接可用，**无需额外处理**。若个别包缺 ARM 轮子，安装时加上：
```bash
sudo apt-get install -y build-essential cargo   # 需要 Rust 编译回退方案时才需要
```

### 2. 选区（Region）影响能否访问 Polymarket
Polymarket 部分 API 有地区限制。**建议**：
- 选一个能正常访问 Polymarket 的区域（多数区域可直连 gamma-api；日本/新加坡/欧洲区通常更稳）
- 开机后先测连通性（见"常见问题"），不行就换区或走代理

### 3. 安全第一：不要直接暴露 5000 端口
仪表盘没有内置登录保护（除非你设置 `web_password`）。推荐用 **SSH 隧道**访问，见下文。

---

## 一、创建实例（Oracle Cloud 控制台）

1. 登录 OCI 控制台 → **Compute → Instances → Create instance**
2. **Image and shape**：
   - Image：**Ubuntu 24.04**（或 **Debian 12**）— 选 ARM 版本
   - Shape：**Ampere / VM.Standard.A1.Flex**，CPU **4**、内存 **24GB**（免费层配额内）
3. **Networking**：默认 VCN/子网即可；公网 IP 选**临时**或**保留**（保留要小资源）
4. **Add SSH keys**：粘贴你的公钥（`~/.ssh/id_rsa.pub`）
5. **Create**。开机后记录**公网 IP**。

> 如果创建时提示免费配额不足（Out of capacity），多试几次或换个可用区/区域。

---

## 二、从 GitHub 在线安装（推荐，无需手动上传）

代码已发布到 GitHub（公开）：`https://github.com/Gavin-Space/polymarket-auto-trader`

在服务器上直接 `git clone` 即可拿到最新代码：

```bash
# 1. SSH 登录（Ubuntu 用户名为 ubuntu；Debian 为 debian）
ssh ubuntu@<服务器公网IP>

# 2. 安装 git（一般自带）并克隆仓库
sudo apt-get update && sudo apt-get install -y git
git clone https://github.com/Gavin-Space/polymarket-auto-trader.git
cd polymarket-auto-trader
```

之后照常安装（见第三节）。**以后升级**：在仓库目录里
```bash
git pull
sudo systemctl restart polymarket-bot
```
即可拉取最新代码并重启，无需重新上传。

> 备用：如果你偏好手动上传，也可下载 `polymarket-bot-deploy.zip`（GitHub Releases 或本机打包）后用 `scp` 上传解压，步骤相同。

---

## 三、安装

### 方式 0：一键在线安装（最快，推荐）

无需手动 clone / 上传，直接在服务器上执行一行命令即可：

```bash
bash <(curl -sSL https://raw.githubusercontent.com/Gavin-Space/polymarket-auto-trader/main/deploy/install-online.sh)
```

脚本会自动：装依赖 → clone 代码 → 建 venv → 装 Python 依赖 → 装 systemd 服务。
装完按提示 `sudo systemctl start polymarket-bot` 即可。

可选参数（环境变量）：
```bash
POLY_APP_DIR=/opt/pm   bash <(curl -sSL .../install-online.sh)   # 自定义安装目录
POLY_REPO_URL=https://github.com/你的用户名/你的fork.git  bash <(...)  # fork 后改仓库
POLY_TAG=v1.0.0  bash <(curl -sSL .../install-online.sh)          # 固定到 Release 版本（稳定）
```
> GitHub 发布 Release 标签后（如 `v1.0.0`），用 `POLY_TAG=v1.0.0` 即可让服务器锁定该稳定版本；不带 `POLY_TAG` 则始终用 main（最新）。

### 方式 A：原生 systemd（手动版，更轻量）

在克隆的仓库目录里（`install.sh` 在 `deploy/` 中）：

```bash
cd ~/polymarket-auto-trader/deploy
chmod +x install.sh
./install.sh /opt/polymarket-auto-trader    # 安装到 /opt（需要 sudo，脚本内已处理）
```

安装脚本会：装系统包 → 建 Python 虚拟环境 → 装依赖 → 装 systemd 服务（数据在 `/opt/polymarket-auto-trader/data/`）。

管理命令：
```bash
sudo systemctl start polymarket-bot     # 启动
sudo systemctl status polymarket-bot    # 查看状态
sudo journalctl -u polymarket-bot -f    # 实时日志
sudo systemctl restart polymarket-bot   # 重启（改代码/配置后）
```

### 方式 B：Docker

```bash
# 安装 Docker（Ubuntu 24.04）
sudo apt-get update && sudo apt-get install -y docker.io docker-compose-v2
sudo systemctl enable --now docker

# 构建并启动（ARM 自动用 arm64 镜像）；docker-compose.yml 在 deploy/ 中
cd ~/polymarket-auto-trader/deploy
docker compose up -d --build
docker compose logs -f
```

---

## 四、安全配置（务必做）

### 1. 设置仪表盘访问密码
数据目录（原生：`/opt/polymarket-auto-trader/data/`；Docker：`deploy/data/`）下编辑 `trading_config.json`：
```bash
sudo nano /opt/polymarket-auto-trader/data/trading_config.json
```
把 `"web_password": ""` 改成你的密码（如 `"web_password": "MySecret123"`），然后重启服务。之后访问任何页面都要用户名 `admin` + 该密码。

### 2. 用 SSH 隧道访问仪表盘（不暴露 5000 到公网）
在你本机：
```bash
ssh -L 5000:127.0.0.1:5000 ubuntu@<服务器公网IP>
```
然后浏览器打开 `http://localhost:5000`。

> **不要在 OCI 安全列表里对 0.0.0.0/0 开放 5000 端口**（除非配合 web_password + 强密码）。

### 3.（可选）OCI 安全列表规则
如果确实要公网访问：OCI 控制台 → Networking → Security Lists → 入站规则添加 TCP 5000，来源限定你的 IP（`<你的公网IP>/32`），**不要** `0.0.0.0/0`。

---

## 五、首次使用

1. SSH 隧道访问 `http://localhost:5000`（或在 OCI 打开 5000 后访问 `http://<服务器IP>:5000`）。
2. 点 **「配置」**：输入钱包私钥、钱包地址、**初始资金**、Telegram 推送，加密保存。
3. 点 **「授权并启动」**：输入加密密码，机器人开始自动扫描/交易。
4. 点 **「⚙️ 设置」**：按研究建议调参（`web_password`、`maker_bias_pct`、止盈目标、过滤开关等）。

---

## 六、常见问题

**Q: 网络 / API 不通？**
```bash
curl -s -o /dev/null -w "%{http_code}" https://clob.polymarket.com   # 期望 200 或 40x（能连上）
curl -s "https://gamma-api.polymarket.com/markets?limit=1" | head -c 200
```
- 若超时 → 该区域访问 Polymarket 受限，换区域，或在服务器上配代理：编辑 `trading_config.json` 没用，需设环境变量：在 systemd 服务里 `Environment=HTTP_PROXY=http://<代理IP>:<端口>`（或 Docker `environment:`）
- 仪表盘授权弹窗有「网络诊断」按钮，可直接定位

**Q: 依赖装不上（ARM）？**
```bash
cd /opt/polymarket-auto-trader
source venv/bin/activate
pip install -r requirements.txt
```
个别包缺 aarch64 轮子时报编译错误 → `sudo apt-get install -y build-essential cargo` 后重装。

**Q: 数据备份？**
停止服务后拷贝数据目录即可（含 `bot_state.json`、`trading_config.json`、`.encrypted_credentials`、`*.log`）：
```bash
sudo systemctl stop polymarket-bot
tar czf backup.tar.gz /opt/polymarket-auto-trader/data/
sudo systemctl start polymarket-bot
```

**Q: 更新机器人到新版本？**
重新上传新的 `auto_trader.py` / `strategy_engine.py`（或整个 zip），然后 `sudo systemctl restart polymarket-bot`。数据目录里已有状态会保留。

---

*本指南针对 Oracle Cloud Ampere A1（ARM）免费层。若你的甲骨文实例是 AMD/Intel x86 形状，所有步骤同样适用（更无架构顾虑）。*
