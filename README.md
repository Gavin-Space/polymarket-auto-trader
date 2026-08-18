# Polymarket Auto-Trader

![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)
![Python](https://img.shields.io/badge/Python-3.10%2B-blue?logo=python&logoColor=white)
![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20Linux%20%7C%20macOS-lightgrey)
![Last commit](https://img.shields.io/github/last-commit/gaofeird/polymarket-auto-trader)
![PRs welcome](https://img.shields.io/badge/PRs-welcome-brightgreen)

一个用于 **Polymarket 预测市场**的全自动交易系统：自动扫描市场 → 多策略筛选正期望机会 → 限价单下单 → 智能止盈止损 → Web 仪表盘监控。

> ⚠️ **风险提示**：预测市场是零和博弈，多数参与者亏钱（研究显示 top 1% 拿走 ~76% 利润）。本项目仅用于研究/教育，交易风险自负。请只用可承受损失的金额，并注意当地法规与 Polymarket 的地区限制。

---

## ✨ 功能特性

- **6 大策略**（`strategy_engine.py`）：
  - `ExpiryYield+` 临期"确定性理财"（买高概率临期确定结果吃年化）
  - `Arbitrage+` YES+NO 等股数套利（保底利润）
  - `TweetArb+` 马斯克推文计数桶套利（买全桶→必中）
  - `Momentum` / `MeanReversion` / `SmartMoney`（方向性策略，**默认关闭**——研究证实无稳健 edge）
- **多因子评分**：订单簿价差/深度、价格历史动量/波动率/回归信号、鲸鱼大单、流动性 → `ConfidenceScorer` 0-100 分
- **正期望过滤**：`EVCalculator`（Kelly 仓位）、置信度门槛、流动性门槛、相对价差保护
- **智能风控**：策略专属止盈止损（百分比）、硬止损、每日交易上限、连续亏损冷却、按策略连亏自动暂停
- **可调参数面板**：初始资金、扫描间隔、最大持仓、最小置信度/EV、电竞/加密边界过滤开关、做市偏向、止盈目标等，全部在 Web 界面实时调整（存 `trading_config.json`）
- **网络韧性**：API 熔断器 + 60 秒扫描硬时限 + 网络诊断工具（适配代理/VPN 不稳定环境）
- **Web 仪表盘**：资金曲线、策略表现、持仓/机会/市场浏览、亮暗主题、推送通知、CSV 导出
- **Telegram 推送**：开仓（含年化/到期/EV）、平仓（含账户摘要）全中文通知
- **安全存储**：私钥用 AES+PBKDF2 加密（`cryptography` Fernet），密码不落盘；可设仪表盘访问密码（`web_password`）

---

## 🏗 架构

```
AutoTraderEngine (后台线程，5 分钟一个周期)
 ├─ GammaAPI        拉取 300+ 活跃 + 100 临期市场
 ├─ EnhancedScanner 6 策略 × 多因子分析 → 高置信正 EV 机会
 ├─ CLOBTrader      限价单（可做市偏向）下单/止盈止损
 ├─ RiskManager     Kelly 仓位 / 日亏上限 / 现金储备 / 每日交易上限
 ├─ SmartExitManager 策略专属止盈/止损/跟踪止损/持有到期
 └─ Flask Web 服务  授权 / 设置 / 监控仪表盘
```

```
auto_trader.py        交易引擎 + Web 仪表盘（HTML 内嵌）
strategy_engine.py    增强策略引擎（评分/EV/仓位/退出/订单簿分析）
polymarket_bot.py     第一代机器人（3 策略）
elon_tweet_trader.py  马斯克推文专项脚本
deploy/               远程服务器部署包（Docker / systemd / OCI 指南）
RESEARCH-POLYMARKET-EDGE.md  深度研究报告（含证据与来源）
```

---

## 🚀 快速开始（本地）

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 启动
python auto_trader.py

# 3. 浏览器打开 http://localhost:5000
#    「配置」→ 输钱包凭证 → 加密保存
#    「授权并启动」→ 输密码 → 自动开始交易
```

Windows 也可用 `run_auto.bat` / `run_bot.bat` / `run_dashboard.bat` 一键启动。

> 首次建议用 **模拟盘（dry_run）** 观察；确认策略符合预期再切实盘。

---

## ⚙️ 配置

运行时参数存于 `trading_config.json`（首次启动自动生成，也可在「⚙️ 设置」面板调整）：

| 参数 | 默认 | 说明 |
|------|------|------|
| `bankroll_usdc` | 200 | 初始资金（驱动仓位与盈亏基准，改动自动重置统计） |
| `scan_interval` | 300 | 扫描间隔（秒） |
| `max_positions` / `max_daily_trades` | 10 / 8 | 持仓与每日交易上限 |
| `min_confidence` / `min_ev_pct` | 75 / 1.5 | 置信度与期望收益门槛 |
| `filter_speculative` | true | 过滤电竞/比赛等难预测市场（提高胜率） |
| `filter_crypto_boundary` | true | 加密价格边界市场需更高概率 |
| `tweetarb_tp_roi` | 1.0 | 推文桶达到该倍收益即提前止盈 |
| `maker_bias_pct` | 0 | 做市偏向：买单挂 ask 下方该比例（研究证实做市是唯一稳健 edge） |
| `web_password` | "" | 仪表盘访问密码（远程必设） |

钱包私钥等密钥存于 `.encrypted_credentials`（加密），**不**进仓库。

---

## 🌍 远程部署

见 `deploy/README-DEPLOY.md`（Docker / systemd / 通用）与 `deploy/README-ORACLE-CLOUD.md`（甲骨文免费层 Ampere ARM 专项）。

- Docker：`docker compose up -d --build`
- 原生：`./install.sh /opt/polymarket-bot` + `sudo systemctl start polymarket-bot`
- 数据都在 `data/` 目录，备份即拷贝该目录

---

## 📚 研究依据

`RESEARCH-POLYMARKET-EDGE.md` 总结了 2024-2026 年经对抗验证的公开研究（SSRN / arXiv / IMDEA 等）：
- 唯一被量化的 edge 是**做市型限价单**（赚钱者提供流动性、亏钱者吃单）
- 方向性/跟鲸鱼没有稳健 edge（本项目默认关闭）
- 临期"确定性理财"真实但温和（按到期时间归一化收益）
- 低价市场价差高达 13-18%，避免吃单

---

## 🔒 安全与合规

- 私钥 AES 加密存储、密码不落盘、所有操作本地执行
- 请勿提交 `.env` / `.encrypted_credentials` / `bot_state.json` 等敏感文件（已在 `.gitignore`）
- Polymarket 对中国大陆用户有限制；请遵守当地法律与平台规则

---

## 📄 License

本项目基于 **MIT License** 开源。

**Copyright (c) 2026 Gavin**

MIT 许可证授予任何人**免费使用、复制、修改、合并、发布、分发、再许可和/或出售**本软件副本的权利，包括闭源与商业用途；唯一要求是**在所有副本或实质性部分中保留上述版权声明与本许可声明**（完整条款见根目录 [`LICENSE`](LICENSE) 文件）。

```
MIT License

Copyright (c) 2026 Gavin

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
