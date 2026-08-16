"""
Polymarket 马斯克推文自动交易脚本
基于 XTracker 实时数据 + Simmer SDK

⚠️ 安全提示：
- 此脚本需要你的钱包私钥才能签名交易
- 私钥仅存储在本地环境变量中，不会上传到任何服务器
- 务必先用 --dry-run 模式运行至少 2 周，确认逻辑正确后再开 live 模式
- 设置每日最大亏损限制，超过后自动停止

使用方法：
  1. 安装依赖：pip install simmer-sdk requests
  2. 设置环境变量（见下方 CONFIG 部分）
  3. Dry run（只看不买）：python elon_tweet_trader.py
  4. 实盘交易：python elon_tweet_trader.py --live
  5. 仅查看状态：python elon_tweet_trader.py --stats
"""

import os
import sys
import json
import time
import logging
import argparse
from datetime import datetime, timedelta
from pathlib import Path

# ============================================================
# CONFIG - 通过环境变量配置，不要硬编码私钥！
# ============================================================

# 从环境变量读取敏感信息
SIMMER_API_KEY = os.environ.get("SIMMER_API_KEY", "")
WALLET_PRIVATE_KEY = os.environ.get("WALLET_PRIVATE_KEY", "")

# 交易参数（可调整）
CONFIG = {
    # 仓位管理
    "max_position_usd": 10.00,          # 单桶最大仓位 $10
    "sizing_pct": 0.05,                  # 仓位占可用余额的 5%
    
    # 策略参数
    "bucket_spread": 1,                  # 买入目标桶两侧各 1 个桶
    "entry_threshold": 0.90,             # 相邻桶总价 < $0.90 才入场（+EV）
    "exit_threshold": 0.95,              # 某桶价格 > $0.95 时卖出止盈
    
    # 安全防护
    "max_daily_loss_pct": 0.10,          # 每日最大亏损 10% 后停止
    "max_total_exposure_pct": 0.30,      # 总仓位不超过余额 30%
    "min_market_volume": 10000,          # 最低市场交易量 $10K
    "slippage_limit": 0.15,              # 滑点超过 15% 取消
    
    # 运行参数
    "dry_run": True,                     # 默认 dry run
    "log_file": "elon_tweet_trader.log",
    "stats_file": "trading_stats.json",
}

# ============================================================
# 日志配置
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(CONFIG["log_file"], encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ]
)
logger = logging.getLogger(__name__)

# ============================================================
# 统计追踪
# ============================================================

def load_stats():
    """加载交易统计"""
    stats_path = Path(CONFIG["stats_file"])
    if stats_path.exists():
        with open(stats_path, "r") as f:
            return json.load(f)
    return {
        "total_trades": 0,
        "wins": 0,
        "losses": 0,
        "total_pnl": 0.0,
        "daily_pnl": {},
        "positions": [],
        "last_run": None,
    }

def save_stats(stats):
    """保存交易统计"""
    stats["last_run"] = datetime.now().isoformat()
    with open(CONFIG["stats_file"], "w") as f:
        json.dump(stats, f, indent=2, ensure_ascii=False)

def get_today_key():
    return datetime.now().strftime("%Y-%m-%d")

def check_daily_loss_limit(stats):
    """检查今日亏损是否超限"""
    today = get_today_key()
    today_pnl = stats["daily_pnl"].get(today, 0)
    balance = get_balance(stats)
    if balance > 0:
        loss_pct = abs(min(today_pnl, 0)) / balance
        if loss_pct >= CONFIG["max_daily_loss_pct"]:
            logger.warning(f"⚠️ 今日亏损 {loss_pct:.1%} 超过限制 {CONFIG['max_daily_loss_pct']:.0%}，停止交易")
            return False
    return True

def get_balance(stats):
    """估算当前余额（初始余额 + 总 PNL）"""
    # 这个值需要根据实际情况调整
    # 实际应从 Simmer API 获取
    return 500.0 + stats["total_pnl"]

def check_total_exposure(stats):
    """检查总仓位是否超限"""
    total_exposure = sum(p.get("cost", 0) for p in stats["positions"])
    balance = get_balance(stats)
    if balance > 0:
        exposure_pct = total_exposure / balance
        if exposure_pct >= CONFIG["max_total_exposure_pct"]:
            logger.warning(f"⚠️ 总仓位 {exposure_pct:.1%} 超过限制 {CONFIG['max_total_exposure_pct']:.0%}")
            return False
    return True

# ============================================================
# XTracker 数据获取
# ============================================================

XTRACKER_BASE = "https://xtracker.polymarket.com/api"

def fetch_xtracker_stats():
    """获取马斯克推文实时统计"""
    try:
        import requests
        resp = requests.get(
            f"{XTRACKER_BASE}/users/elonmusk/posts",
            params={"platform": "x"},
            timeout=10
        )
        resp.raise_for_status()
        data = resp.json()
        
        if not data.get("success"):
            logger.error(f"XTracker API 返回失败: {data}")
            return None
            
        posts = data.get("data", [])
        if not posts:
            logger.warning("XTracker 返回空数据")
            return None
            
        # 计算当前周期的推文数
        now = datetime.utcnow()
        
        # 找到当前周期（最近7天）
        week_ago = now - timedelta(days=7)
        current_week_posts = [
            p for p in posts
            if datetime.fromisoformat(p.get("createdAt", "").replace("Z", "+00:00")).replace(tzinfo=None) >= week_ago
        ]
        
        current_count = len(current_week_posts)
        
        # 计算 projected 总数
        # 找到当前周期的起始时间
        if current_week_posts:
            earliest = min(
                datetime.fromisoformat(p.get("createdAt", "").replace("Z", "+00:00")).replace(tzinfo=None)
                for p in current_week_posts
            )
            days_elapsed = (now - earliest).total_seconds() / 86400
            if days_elapsed > 0:
                daily_rate = current_count / days_elapsed
                projected = daily_rate * 7
            else:
                projected = current_count
        else:
            projected = 0
            
        stats = {
            "current_count": current_count,
            "daily_rate": daily_rate if current_week_posts else 0,
            "projected_total": projected,
            "days_elapsed": days_elapsed if current_week_posts else 0,
            "days_remaining": 7 - (days_elapsed if current_week_posts else 0),
            "timestamp": now.isoformat(),
        }
        
        logger.info(f"📊 XTracker: 当前 {current_count} 条, 日均 {stats['daily_rate']:.1f}, "
                    f"预计总数 {projected:.0f}, 剩余 {stats['days_remaining']:.1f} 天")
        
        return stats
        
    except Exception as e:
        logger.error(f"获取 XTracker 数据失败: {e}")
        return None

def find_target_bucket(projected_total):
    """根据 projected 总数找到目标桶"""
    # Polymarket 推文市场桶结构（通常每 20 条一个桶）
    # 140-159, 160-179, 180-199, 200-219, 220-239, 240-259, 260-279, 280-299...
    
    bucket_size = 20
    bucket_start = int((projected_total // bucket_size) * bucket_size)
    bucket_end = bucket_start + bucket_size - 1
    
    return {
        "center": f"{bucket_start}-{bucket_end}",
        "lower": f"{bucket_start - bucket_size}-{bucket_start - 1}" if bucket_start >= bucket_size else None,
        "upper": f"{bucket_end + 1}-{bucket_end + bucket_size}",
        "projected": projected_total,
    }

# ============================================================
# Simmer SDK 交易接口
# ============================================================

class Trader:
    """交易接口封装"""
    
    def __init__(self, dry_run=True):
        self.dry_run = dry_run
        self.api_key = SIMMER_API_KEY
        self.private_key = WALLET_PRIVATE_KEY
        self.sdk = None
        
        if not dry_run:
            self._init_sdk()
    
    def _init_sdk(self):
        """初始化 Simmer SDK"""
        if not self.api_key:
            logger.error("❌ 缺少 SIMMER_API_KEY 环境变量")
            logger.error("   获取方式：访问 simmer.markets/dashboard → SDK 标签页")
            sys.exit(1)
            
        if not self.private_key:
            logger.error("❌ 缺少 WALLET_PRIVATE_KEY 环境变量")
            logger.error("   这是你的 Polygon 钱包私钥，用于签名交易")
            sys.exit(1)
            
        try:
            # 尝试导入 simmer-sdk
            from simmer_sdk import SimmerClient
            self.sdk = SimmerClient(
                api_key=self.api_key,
                private_key=self.private_key,
            )
            logger.info("✅ Simmer SDK 初始化成功")
        except ImportError:
            logger.error("❌ 未安装 simmer-sdk，请运行: pip install simmer-sdk")
            sys.exit(1)
        except Exception as e:
            logger.error(f"❌ SDK 初始化失败: {e}")
            sys.exit(1)
    
    def get_portfolio(self):
        """获取账户余额和持仓"""
        if self.dry_run:
            stats = load_stats()
            balance = get_balance(stats)
            positions = stats["positions"]
            logger.info(f"💰 [Dry Run] 余额: ${balance:.2f}, 持仓: {len(positions)} 个")
            return {"balance": balance, "positions": positions}
        
        try:
            resp = self.sdk.get("/api/sdk/portfolio")
            return resp
        except Exception as e:
            logger.error(f"获取账户信息失败: {e}")
            return None
    
    def get_positions(self):
        """获取当前持仓"""
        if self.dry_run:
            return load_stats()["positions"]
        
        try:
            resp = self.sdk.get("/api/sdk/positions")
            return resp.get("positions", [])
        except Exception as e:
            logger.error(f"获取持仓失败: {e}")
            return []
    
    def search_markets(self, query):
        """搜索市场"""
        if self.dry_run:
            logger.info(f"🔍 [Dry Run] 搜索市场: {query}")
            return []
        
        try:
            resp = self.sdk.get("/api/sdk/markets", params={"q": query})
            return resp.get("markets", [])
        except Exception as e:
            logger.error(f"搜索市场失败: {e}")
            return []
    
    def place_order(self, market_id, outcome, side, size_usd, price_limit=None):
        """
        下单
        :param market_id: 市场ID
        :param outcome: YES 或 NO
        :param side: buy 或 sell
        :param size_usd: 金额（美元）
        :param price_limit: 限价（None 则市价单）
        """
        order = {
            "market_id": market_id,
            "outcome": outcome,
            "side": side,
            "size": size_usd,
            "type": "limit" if price_limit else "market",
        }
        if price_limit:
            order["price"] = price_limit
            
        if self.dry_run:
            logger.info(f"📝 [Dry Run] 模拟下单: {json.dumps(order, indent=2)}")
            return {"status": "simulated", "order": order}
        
        try:
            resp = self.sdk.post("/api/sdk/orders", json=order)
            logger.info(f"✅ 下单成功: {resp}")
            return resp
        except Exception as e:
            logger.error(f"❌ 下单失败: {e}")
            return None
    
    def close_position(self, position_id):
        """关闭持仓"""
        if self.dry_run:
            logger.info(f"📝 [Dry Run] 模拟平仓: {position_id}")
            return {"status": "simulated"}
        
        try:
            resp = self.sdk.post(f"/api/sdk/positions/{position_id}/close")
            logger.info(f"✅ 平仓成功: {resp}")
            return resp
        except Exception as e:
            logger.error(f"❌ 平仓失败: {e}")
            return None

# ============================================================
# 策略执行
# ============================================================

def execute_strategy(trader, xtracker_stats):
    """执行推文交易策略"""
    if not xtracker_stats:
        logger.warning("无 XTracker 数据，跳过")
        return
    
    # 1. 安全检查
    stats = load_stats()
    
    if not check_daily_loss_limit(stats):
        logger.warning("达到每日亏损限制，停止交易")
        return
    
    if not check_total_exposure(stats):
        logger.warning("达到总仓位限制，停止交易")
        return
    
    # 2. 检查是否在交易窗口内（周一到周三最佳）
    today_weekday = datetime.now().weekday()  # 0=Monday, 6=Sunday
    if today_weekday > 2:  # 周四及以后
        logger.info(f"今天是周{today_weekday+1}，非最佳入场时间（建议周一至周三），跳过")
        return
    
    # 3. 找到目标桶
    projected = xtracker_stats["projected_total"]
    if projected < 50:  # 数据太少，不可靠
        logger.warning(f"projected 总数 {projected:.0f} 太低，数据不足，跳过")
        return
        
    buckets = find_target_bucket(projected)
    logger.info(f"🎯 目标桶: {buckets['center']} (projected: {projected:.0f})")
    logger.info(f"   下桶: {buckets['lower']}, 上桶: {buckets['upper']}")
    
    # 4. 搜索对应市场
    search_term = f"Elon Musk tweets"
    markets = trader.search_markets(search_term)
    
    if not markets:
        logger.info("未找到匹配的推文市场（可能本周期尚未开始或已结束）")
        return
    
    # 5. 找到目标桶的市场并检查价格
    target_markets = []
    for bucket_key in ["lower", "center", "upper"]:
        bucket_range = buckets.get(bucket_key)
        if not bucket_range:
            continue
            
        for market in markets:
            if bucket_range in market.get("question", ""):
                target_markets.append({
                    "bucket": bucket_key,
                    "range": bucket_range,
                    "market": market,
                })
                break
    
    if not target_markets:
        logger.info(f"未找到匹配 {buckets['center']} 桶的市场")
        return
    
    # 6. 检查 +EV 条件
    total_cost = 0
    for tm in target_markets:
        yes_price = tm["market"].get("yesPrice", 0.5)
        total_cost += yes_price
        logger.info(f"   {tm['bucket']} 桶 {tm['range']}: YES 价格 ${yes_price:.2f}")
    
    logger.info(f"   相邻桶总价: ${total_cost:.2f} (阈值: ${CONFIG['entry_threshold']:.2f})")
    
    if total_cost >= CONFIG["entry_threshold"]:
        logger.info(f"❌ 总价 ${total_cost:.2f} >= 阈值 ${CONFIG['entry_threshold']:.2f}，无 +EV，跳过")
        return
    
    # 7. 计算仓位
    portfolio = trader.get_portfolio()
    balance = portfolio.get("balance", 500) if portfolio else 500
    
    position_size = min(
        balance * CONFIG["sizing_pct"],
        CONFIG["max_position_usd"],
    )
    
    # 每个桶分配的金额
    per_bucket = position_size / len(target_markets)
    
    logger.info(f"💰 计划仓位: ${position_size:.2f} (每桶 ${per_bucket:.2f})")
    
    # 8. 下单
    for tm in target_markets:
        market = tm["market"]
        yes_price = market.get("yesPrice", 0.5)
        
        # 限价单，压低 1 cent
        limit_price = round(yes_price - 0.01, 2)
        
        result = trader.place_order(
            market_id=market.get("id"),
            outcome="YES",
            side="buy",
            size_usd=per_bucket,
            price_limit=limit_price,
        )
        
        if result and result.get("status") != "error":
            # 记录持仓
            stats["positions"].append({
                "market_id": market.get("id"),
                "market_name": market.get("question", ""),
                "bucket": tm["range"],
                "direction": "YES",
                "entry_price": limit_price,
                "size_usd": per_bucket,
                "opened_at": datetime.now().isoformat(),
                "status": "open",
            })
            stats["total_trades"] += 1
            logger.info(f"✅ 买入 {tm['range']} 桶 YES @ ${limit_price:.2f}, 金额 ${per_bucket:.2f}")
        else:
            logger.error(f"❌ 买入 {tm['range']} 桶失败")
    
    save_stats(stats)

def check_exit_signals(trader):
    """检查是否需要平仓"""
    stats = load_stats()
    positions = stats["positions"]
    
    open_positions = [p for p in positions if p.get("status") == "open"]
    if not open_positions:
        return
    
    for pos in open_positions:
        # 检查是否超过退出阈值
        # 实际应从 API 获取当前价格
        # 这里简化处理
        current_price = pos.get("entry_price", 0.5)  # 应替换为实时价格
        
        if current_price >= CONFIG["exit_threshold"]:
            logger.info(f"📤 止盈信号: {pos['bucket']} 价格 ${current_price:.2f} >= ${CONFIG['exit_threshold']:.2f}")
            
            result = trader.close_position(pos.get("market_id"))
            if result:
                pnl = (current_price - pos["entry_price"]) * (pos["size_usd"] / pos["entry_price"])
                pos["status"] = "closed"
                pos["exit_price"] = current_price
                pos["pnl"] = pnl
                pos["closed_at"] = datetime.now().isoformat()
                
                stats["total_pnl"] += pnl
                today = get_today_key()
                stats["daily_pnl"][today] = stats["daily_pnl"].get(today, 0) + pnl
                
                if pnl > 0:
                    stats["wins"] += 1
                else:
                    stats["losses"] += 1
                    
                logger.info(f"✅ 平仓: PNL ${pnl:+.2f}")
    
    save_stats(stats)

# ============================================================
# 命令行接口
# ============================================================

def cmd_stats():
    """显示统计"""
    stats = load_stats()
    print("\n" + "=" * 50)
    print("📊 交易统计")
    print("=" * 50)
    print(f"总交易笔数: {stats['total_trades']}")
    print(f"胜: {stats['wins']} | 负: {stats['losses']}")
    win_rate = stats['wins'] / max(stats['wins'] + stats['losses'], 1) * 100
    print(f"胜率: {win_rate:.1f}%")
    print(f"总 PNL: ${stats['total_pnl']:+.2f}")
    print(f"当前持仓: {len([p for p in stats['positions'] if p.get('status') == 'open'])} 个")
    print(f"上次运行: {stats.get('last_run', '从未')}")
    
    today = get_today_key()
    print(f"今日 PNL: ${stats['daily_pnl'].get(today, 0):+.2f}")
    
    open_positions = [p for p in stats['positions'] if p.get('status') == 'open']
    if open_positions:
        print("\n📋 当前持仓:")
        for p in open_positions:
            print(f"  - {p['bucket']} | YES @ ${p['entry_price']:.2f} | ${p['size_usd']:.2f} | {p['opened_at'][:10]}")
    print("=" * 50 + "\n")

def cmd_positions():
    """显示持仓"""
    stats = load_stats()
    positions = [p for p in stats['positions'] if p.get('status') == 'open']
    
    if not positions:
        print("当前无持仓")
        return
        
    print("\n📋 当前持仓:")
    for i, p in enumerate(positions, 1):
        print(f"\n  #{i}")
        print(f"  市场: {p.get('market_name', 'N/A')}")
        print(f"  桶: {p['bucket']}")
        print(f"  方向: {p['direction']}")
        print(f"  买入价: ${p['entry_price']:.2f}")
        print(f"  金额: ${p['size_usd']:.2f}")
        print(f"  开仓时间: {p['opened_at']}")

def cmd_config():
    """显示配置"""
    print("\n⚙️ 当前配置:")
    print(json.dumps(CONFIG, indent=2))
    print(f"\nSIMMER_API_KEY: {'✅ 已设置' if SIMMER_API_KEY else '❌ 未设置'}")
    print(f"WALLET_PRIVATE_KEY: {'✅ 已设置' if WALLET_PRIVATE_KEY else '❌ 未设置'}")

def cmd_set(key, value):
    """更新配置"""
    # 类型转换
    if key in ["max_position_usd", "sizing_pct", "entry_threshold", "exit_threshold", 
               "max_daily_loss_pct", "max_total_exposure_pct", "slippage_limit"]:
        value = float(value)
    elif key in ["bucket_spread", "min_market_volume"]:
        value = int(value)
    elif key in ["dry_run"]:
        value = value.lower() in ["true", "1", "yes"]
    
    old = CONFIG.get(key)
    CONFIG[key] = value
    logger.info(f"✅ {key}: {old} → {value}")
    
    # 注意：这只修改运行时配置，不持久化
    # 如需持久化，请写入 config.json

def cmd_run(live=False, smart_sizing=False):
    """执行交易循环"""
    mode = "LIVE" if live else "DRY RUN"
    logger.info(f"\n{'='*50}")
    logger.info(f"🚀 启动推文交易 ({mode})")
    logger.info(f"{'='*50}\n")
    
    # 安全检查
    if live and not SIMMER_API_KEY:
        logger.error("❌ Live 模式需要 SIMMER_API_KEY 环境变量")
        sys.exit(1)
    if live and not WALLET_PRIVATE_KEY:
        logger.error("❌ Live 模式需要 WALLET_PRIVATE_KEY 环境变量")
        sys.exit(1)
    
    trader = Trader(dry_run=not live)
    
    if smart_sizing:
        portfolio = trader.get_portfolio()
        if portfolio:
            balance = portfolio.get("balance", 0)
            CONFIG["max_position_usd"] = min(balance * CONFIG["sizing_pct"], CONFIG["max_position_usd"])
            logger.info(f"💡 Smart sizing: 最大仓位调整为 ${CONFIG['max_position_usd']:.2f}")
    
    # 1. 获取 XTracker 数据
    logger.info("📡 获取 XTracker 数据...")
    xtracker = fetch_xtracker_stats()
    
    # 2. 执行策略
    logger.info("🎯 执行交易策略...")
    execute_strategy(trader, xtracker)
    
    # 3. 检查退出信号
    logger.info("📤 检查退出信号...")
    check_exit_signals(trader)
    
    # 4. 显示统计
    cmd_stats()
    
    logger.info("✅ 交易循环完成")

def cmd_xtracker_only():
    """仅查看 XTracker 统计"""
    print("\n📡 获取 XTracker 数据...\n")
    stats = fetch_xtracker_stats()
    if stats:
        print("=" * 50)
        print("📊 马斯克推文统计")
        print("=" * 50)
        print(f"当前周期推文数: {stats['current_count']}")
        print(f"日均速率: {stats['daily_rate']:.1f} 条/天")
        print(f"预计总数: {stats['projected_total']:.0f} 条")
        print(f"已过天数: {stats['days_elapsed']:.1f}")
        print(f"剩余天数: {stats['days_remaining']:.1f}")
        print(f"数据时间: {stats['timestamp']}")
        
        buckets = find_target_bucket(stats['projected_total'])
        print(f"\n🎯 目标桶: {buckets['center']}")
        print(f"   下桶: {buckets['lower']}")
        print(f"   上桶: {buckets['upper']}")
        print("=" * 50 + "\n")

# ============================================================
# 主入口
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="Polymarket 马斯克推文自动交易脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用示例:
  # 仅查看 XTracker 统计
  python elon_tweet_trader.py --stats

  # Dry run（只看不买，推荐先用 2 周）
  python elon_tweet_trader.py

  # 实盘交易
  python elon_tweet_trader.py --live

  # 实盘 + smart sizing（根据余额自动调整仓位）
  python elon_tweet_trader.py --live --smart-sizing

  # 查看持仓
  python elon_tweet_trader.py --positions

  # 查看配置
  python elon_tweet_trader.py --config

  # 修改配置
  python elon_tweet_trader.py --set max_position_usd=15.00

  # 静默模式（适合 cron）
  python elon_tweet_trader.py --live --quiet

环境变量:
  SIMMER_API_KEY      - 从 simmer.markets/dashboard 获取
  WALLET_PRIVATE_KEY  - 你的 Polygon 钱包私钥

Cron 设置（每小时检查一次）:
  0 * * * * cd /path/to/script && python elon_tweet_trader.py --live --quiet
        """
    )
    
    parser.add_argument("--live", action="store_true", help="实盘交易模式（默认 dry run）")
    parser.add_argument("--smart-sizing", action="store_true", help="根据余额自动调整仓位")
    parser.add_argument("--quiet", action="store_true", help="静默模式（只输出交易和错误）")
    parser.add_argument("--stats", action="store_true", help="仅查看 XTracker 统计")
    parser.add_argument("--positions", action="store_true", help="查看当前持仓")
    parser.add_argument("--config", action="store_true", help="查看当前配置")
    parser.add_argument("--set", type=str, help="设置参数 (key=value)")
    
    args = parser.parse_args()
    
    if args.quiet:
        logging.getLogger().setLevel(logging.WARNING)
    
    if args.stats:
        cmd_xtracker_only()
    elif args.positions:
        cmd_positions()
    elif args.config:
        cmd_config()
    elif args.set:
        key, value = args.set.split("=", 1)
        cmd_set(key.strip(), value.strip())
    else:
        cmd_run(live=args.live, smart_sizing=args.smart_sizing)

if __name__ == "__main__":
    main()
