# Polymarket 全自动交易机器人 - 启动指南

## 系统总览

```
你只需要做 3 件事：
  1. 注册 Polymarket 账户 + 充值 USDC
  2. 把钱包私钥填进 .env
  3. 运行 python polymarket_bot.py --live

机器人自动完成：
  ✅ 扫描全市场（Gamma API，500+ 市场实时数据）
  ✅ 筛选机会（3 大策略：临期理财、套利、推文预测）
  ✅ 计算仓位（Kelly 公式 + 风控限制）
  ✅ 自动下单（CLOB API 限价单，0 手续费）
  ✅ 管理仓位（监控到期、自动止盈、记录 P&L）
  ✅ 风险控制（日亏上限、仓位上限、现金储备）
  ✅ Telegram 通知（每笔交易实时推送）

可视化监控台（新增）：
  ✅ Polymarket 风格 Web 界面（http://localhost:5000）
  ✅ 实时市场卡片、概率条、策略标签
  ✅ 交易机会列表（按年化收益排序）
  ✅ 持仓管理表、运行日志流
  ✅ 每 30 秒自动刷新 + 每 5 分钟自动扫描
```

---

## 第一步：安装环境（5 分钟）

### 1.1 安装 Python 依赖

```bash
cd C:\Users\mrgao\WorkBuddy\2026-08-12-23-04-24
pip install -r requirements.txt
```

依赖包：
- `py-clob-client` — Polymarket 官方 Python SDK（交易用）
- `python-dotenv` — 读取配置文件
- `requests` — HTTP 请求

### 1.2 验证安装

```bash
python -c "from py_clob_client.client import ClobClient; print('OK')"
```

如果输出 `OK` 就成功了。如果报错，试：
```bash
pip install py-clob-client --upgrade
```

---

## 第二步：注册 Polymarket 并获取钱包凭证（10 分钟）

### 2.1 注册 Polymarket

1. 访问 https://polymarket.com
2. 点击 "Sign Up"，选择 "Connect Wallet"
3. 推荐使用 **Email 注册**（Polymarket 会自动为你创建一个 Proxy 钱包）
4. 完成注册后，你会有一个 Polygon 链上的钱包地址

### 2.2 获取钱包私钥

**方式 A：如果你用 Email 注册（Proxy 钱包）**

1. 登录 Polymarket
2. 进入 Settings → Export Wallet
3. 按提示获取你的私钥（以 `0x` 开头的字符串）
4. 同时记下你的钱包地址（funder address）

**方式 B：如果你用 MetaMask 连接**

1. 打开 MetaMask
2. 选择你连接 Polymarket 的账户
3. 点击账户详情 → 导出私钥
4. 输入 MetaMask 密码获取私钥

### 2.3 充值 USDC

1. 在 Polymarket 点击 "Deposit"
2. 选择充值方式：
   - **从 Polygon 直接转入 USDC**（推荐，手续费最低）
   - **从 Ethereum 跨链**（通过 Polygon Bridge）
   - **信用卡购买**（手续费较高）
3. 建议先充 $200-500 练手

⚠️ **安全提醒**：
- 私钥永远不要告诉任何人
- 不要把 .env 文件上传到任何公开仓库
- 建议用一个新的、只放交易资金的钱包

---

## 第三步：配置机器人（2 分钟）

### 3.1 创建配置文件

```bash
cp .env.example .env
```

### 3.2 编辑 .env 文件

用文本编辑器打开 `.env`，填入以下内容：

```env
# === 必填 ===
PRIVATE_KEY=0x你的私钥
FUNDER_ADDRESS=0x你的钱包地址
SIGNATURE_TYPE=2

# === 交易参数 ===
TRADING_MODE=dry_run
BANKROLL_USDC=200
MAX_POSITION_PCT=0.05
MAX_TOTAL_EXPOSURE_PCT=0.70
CASH_RESERVE_PCT=0.30
MAX_POSITIONS=10
DAILY_LOSS_LIMIT_PCT=0.10
MIN_MARKET_VOLUME=10000

# === 策略开关 ===
STRATEGY_EXPIRY_YIELD=1
STRATEGY_TWEET_PREDICTION=1
STRATEGY_ARBITRAGE=1
STRATEGY_DIRECTIONAL=0

# === 扫描间隔 ===
SCAN_INTERVAL=300

# === Telegram 通知（可选）===
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

### 3.3 确认 signature_type

| 你的注册方式 | SIGNATURE_TYPE |
|---|---|
| Email 注册（Proxy 钱包） | 2 |
| MetaMask EOA 钱包 | 0 |
| Magic Link | 1 |

大多数用 Email 注册的用户选 **2**。

---

## 第四步：试运行（5 分钟）

### 4.1 Dry Run 模式（不花钱，先测试）

```bash
python polymarket_bot.py --scan
```

这会扫描市场并打印机会，不下单。你应该看到类似输出：

```
[INFO] Strategy A: Scanning for expiry yield opportunities...
[INFO]   Found 8 expiry yield opportunities
[INFO] Strategy E: Scanning for arbitrage opportunities...
[INFO]   Found 2 arbitrage opportunities

Found 10 opportunities

1. [ExpiryYield] Will BTC close above $100k on Aug 15?
   NO @ $0.970 | 3.2d to expiry | 376% annualized

2. [ExpiryYield] Will LeBron retire before 2027?
   NO @ $0.960 | 5.0d to expiry | 292% annualized
...
```

### 4.2 运行一个完整周期（Dry Run）

```bash
python polymarket_bot.py --once
```

这会扫描 + 模拟下单 + 检查仓位，但不会真正花钱。

### 4.3 查看统计

```bash
python polymarket_bot.py --stats
```

---

## 第五步：正式上线（实盘交易）

### 5.1 切换到 Live 模式

编辑 `.env`：
```env
TRADING_MODE=live
```

或者直接用命令行参数（覆盖 .env）：
```bash
python polymarket_bot.py --live --once
```

### 5.2 首次运行

首次运行时，机器人会：
1. 用你的私钥派生 API 凭证
2. 打印出 API Key、Secret、Passphrase
3. **把这些值复制到 .env 文件中**（下次启动就不用重新派生）

### 5.3 启动持续运行

```bash
python polymarket_bot.py --live
```

机器人会：
- 每 5 分钟扫描一次市场
- 发现机会自动下单
- 监控已有仓位
- 到期自动结算
- 所有操作记录到 `bot_trades.log`
- 通过 Telegram 推送通知（如配置）

### 5.4 后台运行（保持 24/7）

**Windows（推荐用 PowerShell）：**
```powershell
Start-Process python -ArgumentList "polymarket_bot.py","--live" -WindowStyle Hidden
```

**或者用 nssm 注册为 Windows 服务：**
```bash
nssm install PolymarketBot "C:\Users\mrgao\WorkBuddy\2026-08-12-23-04-24\python.exe" "polymarket_bot.py --live"
nssm start PolymarketBot
```

---

## 第六步：配置 Telegram 通知（可选但强烈推荐）

### 6.1 创建 Telegram Bot

1. 在 Telegram 搜索 @BotFather
2. 发送 `/newbot`
3. 按提示设置名称和用户名
4. 获取 Bot Token

### 6.2 获取你的 Chat ID

1. 给你的新 Bot 发一条消息
2. 访问 `https://api.telegram.org/bot<TOKEN>/getUpdates`
3. 找到 `"chat":{"id":123456789}` 中的数字

### 6.3 填入 .env

```env
TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
TELEGRAM_CHAT_ID=123456789
```

之后每笔交易、每次止盈止损、每日报告都会推送到你的 Telegram。

---

## 命令速查

| 命令 | 作用 |
|---|---|
| `python polymarket_bot.py --scan` | 扫描市场，只看机会不下单 |
| `python polymarket_bot.py --once` | 运行一个完整周期（扫描+下单+管理） |
| `python polymarket_bot.py --live --once` | 同上，但实盘交易 |
| `python polymarket_bot.py --live` | 持续运行，实盘交易 |
| `python polymarket_bot.py --stats` | 查看仓位和 P&L |
| `python polymarket_bot.py --report` | 生成日报 |
| `python polymarket_bot.py --cancel-all` | 撤销所有挂单 |

---

## 风控参数说明

| 参数 | 默认值 | 含义 |
|---|---|---|
| `BANKROLL_USDC` | 200 | 总资金（机器人不会超过这个额度管理） |
| `MAX_POSITION_PCT` | 5% | 单笔最大仓位 = 资金 × 5% = $10 |
| `MAX_TOTAL_EXPOSURE_PCT` | 70% | 总持仓上限 = $140 |
| `CASH_RESERVE_PCT` | 30% | 始终保留 $60 现金 |
| `MAX_POSITIONS` | 10 | 最多同时持有 10 个仓位 |
| `DAILY_LOSS_LIMIT_PCT` | 10% | 当日亏损达 $20 自动停止 |
| `MIN_MARKET_VOLUME` | $10K | 只做交易量 > $10K 的市场 |
| `SCAN_INTERVAL` | 300s | 每 5 分钟扫描一次 |

---

## 策略说明

### 策略 A：临期确定性理财（ExpiryYield）
- **逻辑**：找 NO 概率 > 95%、1-7 天到期的市场，买入 NO
- **收益**：年化 30-50%（短期可能更高）
- **风险**：极低（5% 以内的下行风险）
- **类比**：类似低风险理财，赚的是时间价值

### 策略 E：套利（Arbitrage）
- **逻辑**：找 YES + NO 总价 < $0.98 的市场，同时买入两边
- **收益**：每笔 2-5% 无风险利润
- **风险**：接近零（无论结果都赚）
- **注意**：机会较少，需要快速执行

### 策略 B：推文预测（TweetPrediction）
- **逻辑**：监控马斯克推文市场，寻找定价偏差
- **收益**：每笔 10-50%
- **风险**：中等（需要正确判断推文数量区间）
- **建议**：先用 Dry Run 观察，熟悉后再启用

---

## 常见问题

### Q: 机器人报错 "Failed to initialize CLOB client"
检查：
1. PRIVATE_KEY 是否正确（以 0x 开头）
2. SIGNATURE_TYPE 是否匹配你的钱包类型
3. FUNDER_ADDRESS 是否正确（Proxy 钱包需要）

### Q: 机器人运行但没有下单
可能原因：
1. 处于 Dry Run 模式（检查 TRADING_MODE）
2. 触发风控限制（用 --stats 查看）
3. 没有符合条件的市场（正常，等待机会）
4. USDC 余额不足

### Q: 如何停止机器人
按 `Ctrl+C` 即可。所有仓位会保留在链上，下次启动会自动恢复。

### Q: Polymarket 限制中国用户怎么办
需要使用海外网络环境访问。机器人本身在海外服务器上运行不受影响。

### Q: 如何调整策略参数
编辑 `.env` 文件，修改对应参数，重启机器人即可。

---

## 文件清单

```
polymarket_bot.py      — 主程序（交易机器人）
.env                   — 你的配置（含私钥，不要泄露！）
.env.example           — 配置模板
requirements.txt       — Python 依赖
bot_state.json         — 运行状态（仓位、P&L，自动生成）
bot_trades.log         — 交易日志（自动生成）
report_YYYY-MM-DD.txt  — 日报（自动生成）
```

---

## 最后的安全提醒

1. **私钥安全**：.env 文件包含你的私钥，绝对不要上传到 GitHub 或分享给任何人
2. **小资金起步**：先用 $200 测试 2 周，确认策略有效再加资金
3. **Dry Run 优先**：先用 dry_run 模式跑几天，观察机器人的决策是否合理
4. **监控运行**：即使全自动，也建议每天用 --stats 检查一次
5. **止损纪律**：DAILY_LOSS_LIMIT_PCT 是你的最后一道防线，不要调太高
6. **合规提醒**：Polymarket 限制部分地区用户，请确保你在合法地区操作
