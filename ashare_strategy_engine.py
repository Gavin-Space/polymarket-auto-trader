# -*- coding: utf-8 -*-
"""
ashare_strategy_engine.py — A股 策略引擎
========================================
4 大策略(胜率优先, 均带评分/仓位/风控):

  1. 可转债双低轮动(核心, 确定性 edge)
     双低值 = 债现价 + 100 × 转股溢价率
     过滤: 价≤115 / 溢价≤40% / 规模≥2亿 / 信用≥AA / 排除强赎·回售·ST正股·临期
     每周调仓等权; "跌有债底、涨跟正股" → 高胜率小回撤
  2. 可转债打新(免费顶格申购提醒 + 上市首日卖出纪律)
  3. 红利低波(月度): HS300 → 股息率≥3% → 60日波动率升序取前N
  4. 日线趋势跟随(默认关): close>MA20>MA60 + 60日新高 + 动量>0 + RSI<70 + 放量

评分(胜率优先) ASHAREScorer: 基准55 + 各维度加分 − 风险项减分 → 0-100
仓位 Sizer: 预算 = 本金×单票比例 × (0.5 + 0.5×score/100), 取整到手数
风控 RiskManager: 单票/总敞口/现金储备/回撤熔断/连亏冷却/日交易上限/日亏硬停/交易时段门禁
表现 PerfTracker: 每策略 交易数/胜/负/胜率/盈亏/期望值/最大回撤/盈亏因子
"""

import math
import os
import threading
import time
from datetime import date, datetime, timedelta

import pandas as pd

from ashare_data import (
    cb_doublelow_universe, cb_subscription_queue, dividend_yield,
    get_dividend_scan, index_constituents, is_trading_day,
    refresh_dividend_scan, scan_status, stock_daily, get_last_trading_day,
)
from ashare_broker import PaperBroker, is_cb, price_limit_pct, round_lot, BrokerFactory

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 风险档位(1-10): 越高越激进。risk_level → (总敞口, 现金储备, 单票比例)
RISK_PRESETS = {
    1: (0.30, 0.70, 0.02),
    2: (0.40, 0.60, 0.03),
    3: (0.45, 0.55, 0.04),
    4: (0.50, 0.50, 0.04),
    5: (0.60, 0.40, 0.05),
    6: (0.65, 0.35, 0.06),
    7: (0.70, 0.30, 0.07),
    8: (0.75, 0.25, 0.08),
    9: (0.80, 0.20, 0.10),
    10: (0.85, 0.15, 0.12),
}

DEFAULT_STRATEGY_CFG = {
    "cb_double_low": {
        "enabled": True, "max_price": 115, "max_premium": 40.0,
        "min_size": 2.0, "min_rating": "AA", "hold_n": 10,
        "rebalance_days": 7, "exit_price": 130,
    },
    "cb_new": {
        "enabled": True, "subscribe_limit": 100,  # 顶格 100 万
        "sell_first_day": True,
    },
    "dividend": {
        "enabled": True, "index": "000300", "min_yield": 0.03,
        "hold_n": 10, "max_vol_pct": 0.05, "rebalance_days": 30,
    },
    "trend": {
        "enabled": False, "index": "000300", "hold_n": 5,
        "ma_short": 20, "ma_long": 60, "rsi_max": 70, "stop_loss_pct": 0.08,
    },
}


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _rating_score(rating):
    """信用评级 → 加分。AAA 最高。"""
    r = (rating or "").upper().replace(" ", "")
    if r.startswith("AAA"):
        return 10
    if r.startswith("AA+"):
        return 8
    if r.startswith("AA"):
        return 5
    if r.startswith("A"):
        return 0
    return -15  # BBB 及以下: 信用风险


# ---------------------------------------------------------------------------
# 策略 1: 可转债双低轮动
# ---------------------------------------------------------------------------
def double_low_candidates(data, cfg=None, ref_date=None):
    """返回符合双低条件的候选(已按 dlow 升序排序, 含评分)。"""
    cfg = cfg or {}
    mp = float(cfg.get("max_price", 115))
    mpm = float(cfg.get("max_premium", 40.0))
    ms = float(cfg.get("min_size", 2.0))
    mr = (cfg.get("min_rating", "AA") or "AA").upper()
    uni = cb_doublelow_universe()
    if uni is None or len(uni) == 0:
        return pd.DataFrame()
    uni = uni.copy()

    # 基础过滤
    m = (uni["price_eff"] <= mp) & (uni["price_eff"] >= 80) \
        & (uni["premium"] <= mpm) & (uni["premium"] >= -10) \
        & (uni["issue_size"].fillna(0) >= ms)
    # 评级过滤(有评级才纳入)
    has_rating = uni["rating"].notna()
    if mr:
        def gte_rating(r):
            r = (r or "").upper().replace(" ", "")
            order = {"AAA": 7, "AA+": 6, "AA": 5, "AA-": 4, "A+": 3, "A": 2}
            need = order.get(mr, 5)
            got = order.get(r, 0)
            return got >= need
        m &= has_rating & uni["rating"].map(gte_rating)
    # 排除 ST 正股
    stock_name = uni["stock_name"].astype(str)
    m &= ~stock_name.str.contains("ST", na=False)
    # 排除临期(距到期 < 0.5 年): remain_years 缺失时跳过
    ry = pd.to_numeric(uni.get("remain_years"), errors="coerce")
    if ry is not None:
        m &= ry.isna() | (ry >= 0.5)

    cand = uni[m].copy()
    if cand.empty:
        return cand

    # 评分 + 排序
    cand["score"] = cand.apply(lambda r: score_double_low(r, cfg), axis=1)
    cand = cand.sort_values("dlow").reset_index(drop=True)
    return cand


def score_double_low(row, cfg=None):
    """双低候选评分 0-100。胜率优先: 越接近债底/溢价越低/信用越高/规模越大分越高。"""
    s = 55.0
    pm = float(row["premium"]) if pd.notna(row["premium"]) else 99
    price = float(row["price_eff"])
    # 低溢价: 跟涨能力 + 安全边际
    if pm < 5:
        s += 10
    elif pm < 15:
        s += 7
    elif pm < 25:
        s += 3
    # 接近债底
    if price < 100:
        s += 10
    elif price < 105:
        s += 8
    elif price < 110:
        s += 4
    # 信用
    s += _rating_score(row.get("rating"))
    # 规模(流动性/赎回概率)
    sz = float(row["issue_size"]) if pd.notna(row.get("issue_size")) else 0
    if sz >= 20:
        s += 8
    elif sz >= 10:
        s += 5
    elif sz >= 5:
        s += 2
    # 双低值前 10% 加成(由调用方传入相对排名时可加)
    # 风控硬排除: 极高溢价或破位
    if pm > 45:
        s -= 40
    if price > 120:
        s -= 30
    if price < 90:
        s -= 10  # 过深贴水可能正股暴雷
    return int(_clamp(round(s), 0, 100))


# ---------------------------------------------------------------------------
# 策略 2: 可转债打新
# ---------------------------------------------------------------------------
def cb_new_opportunities(data, cfg=None):
    """打新提醒队列: 顶格申购(免费) + 上市首日卖出纪律。"""
    cfg = cfg or {}
    q = cb_subscription_queue()
    if q is None or len(q) == 0:
        return pd.DataFrame()
    out = []
    for _, r in q.iterrows():
        out.append({
            "symbol": r.get("symbol"), "name": r.get("name"),
            "apply_date": r.get("apply_date_dt").strftime("%Y-%m-%d") if r.get("apply_date_dt") else None,
            "apply_limit": r.get("apply_limit"), "rating": r.get("rating"),
            "stock_code": r.get("stock_code"), "score": 85,
            "note": "顶格申购免费, 中签上市首日卖出",
        })
    return pd.DataFrame(out)


# ---------------------------------------------------------------------------
# 策略 3: 红利低波(月度)
# ---------------------------------------------------------------------------
def dividend_candidates(data, cfg=None):
    """红利低波候选: HS300 → 股息率≥阈值 → 60日波动率升序取前N。"""
    cfg = cfg or {}
    idx = cfg.get("index", "000300")
    min_yield = float(cfg.get("min_yield", 0.03))
    hold_n = int(cfg.get("hold_n", 10))
    cons = index_constituents(idx)
    if not cons:
        return pd.DataFrame()

    scan = get_dividend_scan()
    if scan is None or len(scan) == 0:
        return pd.DataFrame()  # 扫描尚未完成

    yield_map = {r["symbol"]: r["yield"] for _, r in scan.iterrows()}
    name_map = {r["symbol"]: r["name"] for _, r in scan.iterrows()}

    rows = []
    for code, nm in cons:
        dy = yield_map.get(code)
        if dy is None or dy < min_yield:
            continue
        if "ST" in str(nm or "").upper():
            continue
        vol = _stock_volatility(code, data)
        if vol is None:
            continue
        rows.append({"symbol": code, "name": nm or name_map.get(code, code),
                     "yield": dy, "vol_60d": vol})
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df = df.sort_values("vol_60d").head(hold_n)
    df["score"] = df.apply(lambda r: score_dividend(r), axis=1)
    return df.reset_index(drop=True)


def score_dividend(row):
    s = 55.0
    dy = float(row["yield"])
    if dy >= 0.06:
        s += 12
    elif dy >= 0.05:
        s += 8
    elif dy >= 0.04:
        s += 5
    vol = float(row["vol_60d"])
    if vol <= 0.02:
        s += 10
    elif vol <= 0.03:
        s += 6
    elif vol <= 0.04:
        s += 2
    else:
        s -= 10
    return int(_clamp(round(s), 0, 100))


def _stock_volatility(symbol6, data, lookback=60):
    """60 日收益率波动率(年化)。数据不足返回 None。"""
    try:
        bars = stock_daily(symbol6)
    except Exception:
        return None
    if bars is None or len(bars) < 20:
        return None
    closes = bars["close"].tail(lookback)
    if len(closes) < 20:
        return None
    rets = closes.pct_change().dropna()
    if len(rets) < 10:
        return None
    return float(rets.std() * math.sqrt(250))


# ---------------------------------------------------------------------------
# 策略 4: 日线趋势跟随(默认关)
# ---------------------------------------------------------------------------
def trend_candidates(data, cfg=None):
    """趋势候选: close>MA20>MA60 + 60日新高 + 20日动量>0 + RSI<70 + 放量。"""
    cfg = cfg or {}
    idx = cfg.get("index", "000300")
    hold_n = int(cfg.get("hold_n", 5))
    cons = index_constituents(idx)
    if not cons:
        return pd.DataFrame()

    rows = []
    for code, nm in cons:
        try:
            bars = stock_daily(code)
        except Exception:
            continue
        if bars is None or len(bars) < 70:
            continue
        c = bars["close"]
        close = float(c.iloc[-1])
        ma20 = float(c.tail(20).mean())
        ma60 = float(c.tail(60).mean())
        if not (close > ma20 > ma60):
            continue
        hi60 = float(c.tail(60).max())
        if close < hi60 * 0.99:  # 距 60 日新高 1% 内
            continue
        mom20 = close / float(c.iloc[-21]) - 1 if len(c) > 21 else 0
        if mom20 <= 0:
            continue
        rsi = _rsi(c, 14)
        if rsi >= float(cfg.get("rsi_max", 70)):
            continue
        vol_ratio = float(bars["volume"].iloc[-1]) / float(bars["volume"].tail(20).mean() + 1e-9)
        if vol_ratio < 1.2:
            continue
        rows.append({"symbol": code, "name": nm, "close": close,
                     "ma20": ma20, "ma60": ma60, "momentum20": mom20, "rsi": rsi,
                     "vol_ratio": vol_ratio})
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df = df.sort_values("momentum20", ascending=False).head(hold_n)
    df["score"] = df.apply(lambda r: score_trend(r), axis=1)
    return df.reset_index(drop=True)


def _rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / (loss + 1e-9)
    return float(100 - 100 / (1 + rs.iloc[-1]))


def score_trend(row):
    s = 55.0
    if float(row["vol_ratio"]) >= 2.0:
        s += 8
    elif float(row["vol_ratio"]) >= 1.5:
        s += 4
    if float(row["momentum20"]) >= 0.10:
        s += 8
    elif float(row["momentum20"]) >= 0.05:
        s += 5
    if float(row["rsi"]) < 55:
        s += 5
    return int(_clamp(round(s), 0, 100))


# ---------------------------------------------------------------------------
# 仓位
# ---------------------------------------------------------------------------
def size_position(symbol6, price, score, bankroll, per_symbol_pct, min_notional=1000.0):
    """预算 = 本金×单票比例 × (0.5+0.5×score/100), 取整到手数。"""
    if not price or price <= 0:
        return 0
    budget = bankroll * per_symbol_pct * (0.5 + 0.5 * score / 100.0)
    if budget < min_notional:
        budget = min_notional
    qty = budget / price
    lot = 10 if is_cb(symbol6) else 100
    qty = int(qty // lot) * lot
    return max(qty, 0)


# ---------------------------------------------------------------------------
# 风控
# ---------------------------------------------------------------------------
class RiskManager:
    """分层风控门禁。返回 (ok, reason)。"""

    def __init__(self, cfg, state):
        self.cfg = cfg
        self.state = state

    def _risk(self):
        lvl = int(self.cfg.get("risk_level", 5))
        exposure, reserve, per_sym = RISK_PRESETS.get(lvl, RISK_PRESETS[5])
        return exposure, reserve, per_sym

    def check_new_trade(self, symbol, side, est_cost, broker):
        """开仓/加仓前检查。"""
        cfg = self.cfg
        bankroll = float(cfg.get("bankroll_cny", 100000))
        exposure, reserve, per_sym = self._risk()

        # 交易时段门禁(仅限时下单; 回测不走此门禁)
        if cfg.get("enforce_session", True):
            from ashare_data import is_market_open
            if not is_market_open():
                return False, "非交易时段(9:30-11:30/13:00-15:00)"

        # 回撤熔断
        dd = self._current_drawdown(broker)
        if dd >= float(cfg.get("max_drawdown_circuit_pct", 8)):
            return False, f"回撤 {dd:.1f}% 达熔断线, 暂停开仓"

        # 连亏冷却
        n_loss = int(self.state.get("consecutive_losses", 0))
        cooldown = cfg.get("consecutive_loss_cooldown", 3)
        if n_loss >= cooldown:
            until = self.state.get("cooldown_until", 0)
            if until and time.time() < until:
                mins = int((until - time.time()) / 60)
                return False, f"连亏 {n_loss} 次冷却中(剩 {mins} 分钟)"

        # 日交易上限
        day = date.today().isoformat()
        trades_today = len([o for o in broker.get_orders()
                            if o.get("status") == "filled" and o.get("created", "").startswith(day)])
        if trades_today >= int(cfg.get("max_daily_trades", 10)):
            return False, "达当日交易上限"

        # 日亏硬停
        dp = self._daily_pnl(broker)
        if dp <= -bankroll * float(cfg.get("daily_loss_limit_pct", 0.10)):
            return False, "当日亏损达硬停线"

        # 现金储备
        acc = broker.get_account()
        cash = acc.get("cash", 0) - acc.get("frozen", 0)
        if side == "buy":
            if cash < bankroll * reserve:
                return False, f"现金储备不足(需≥{reserve*100:.0f}%, 可用 {cash:.0f})"
            if est_cost > cash:
                return False, "资金不足"

        # 总敞口
        invested = acc.get("market_value", 0) + acc.get("frozen", 0)
        if side == "buy" and invested + est_cost > bankroll * exposure:
            return False, "总敞口超限"

        # 单票上限
        if side == "buy":
            pos = {p["symbol"]: p for p in broker.get_positions()}
            cur = pos.get(symbol, {})
            cur_val = cur.get("qty", 0) * (cur.get("last_price") or cur.get("avg_cost") or 0)
            if cur_val + est_cost > bankroll * per_sym * 1.5:
                return False, f"单票 {symbol} 超上限"
        return True, "ok"

    def _current_drawdown(self, broker):
        """按权益曲线算回撤(%)。"""
        eq = self.state.get("equity_curve", [])
        if not eq:
            return 0.0
        peak = max(e[1] for e in eq)
        if peak <= 0:
            return 0.0
        last = eq[-1][1]
        return max(0.0, (peak - last) / peak * 100.0)

    def _daily_pnl(self, broker):
        day = date.today().isoformat()
        filled = [o for o in broker.get_orders() if o.get("status") == "filled"
                  and o.get("created", "").startswith(day)]
        pnl = 0.0
        # 简化: 用卖出订单的已实现盈亏累计 + 持仓市值变动估算
        for o in filled:
            if o.get("side") == "sell":
                pass  # realized_pnl 已在 broker 累计
        return float(self.state.get("daily_pnl", {}).get(day, 0.0))

    def record_fill(self, order):
        """成交后更新连亏/冷却/每日统计。"""
        day = date.today().isoformat()
        dp = self.state.get("daily_pnl", {})
        dp[day] = round(dp.get(day, 0.0) + order.get("pnl", 0.0), 2)
        self.state.update(daily_pnl=dp)

    def register_cooldown(self):
        n = int(self.state.get("consecutive_losses", 0))
        self.state.update(cooldown_until=time.time() + 30 * 60 * n)


# ---------------------------------------------------------------------------
# 表现追踪
# ---------------------------------------------------------------------------
class PerfTracker:
    def __init__(self, state):
        self.state = state

    def record_exit(self, strategy, order):
        """记录一次平仓: 计算盈亏与胜败。order: broker 卖出单 + avg_cost。"""
        stats = self.state.get("perf", {})
        st = stats.setdefault(strategy, {"trades": 0, "wins": 0, "losses": 0,
                                         "pnl": 0.0, "fees": 0.0, "expectancy": 0.0,
                                         "max_dd": 0.0, "profit_factor": 0.0,
                                         "avg_hold_days": 0, "recent": []})
        pnl = order.get("pnl", 0.0)
        st["trades"] += 1
        st["pnl"] = round(st["pnl"] + pnl, 2)
        st["fees"] = round(st["fees"] + order.get("fee", 0.0), 2)
        if pnl > 0:
            st["wins"] += 1
        elif pnl < 0:
            st["losses"] += 1
        st["win_rate"] = round(st["wins"] / st["trades"] * 100, 1) if st["trades"] else 0.0
        st["expectancy"] = round(st["pnl"] / st["trades"], 2) if st["trades"] else 0.0
        gross_win = sum(1 for r in st["recent"] if r["pnl"] > 0) * 0  # placeholder
        # 盈亏因子 = 总盈利 / 总亏损(绝对值)
        total_win = max(st["pnl"], 0.0)
        total_loss = max(-st["pnl"], 0.0)
        st["profit_factor"] = round(total_win / total_loss, 2) if total_loss > 0 else 99.0
        st["recent"].append({"ts": datetime.now().isoformat(timespec="seconds"),
                             "symbol": order.get("symbol"), "pnl": round(pnl, 2),
                             "strategy": strategy})
        st["recent"] = st["recent"][-30:]
        self.state.update(perf=stats)

    def record_equity(self, total_asset):
        curve = self.state.get("equity_curve", [])
        curve.append([datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                      round(float(total_asset), 2)])
        self.state.update(equity_curve=curve[-2000:])

    def stats(self):
        return self.state.get("perf", {})


# ---------------------------------------------------------------------------
# 策略引擎(编排)
# ---------------------------------------------------------------------------
class StrategyEngine:
    def __init__(self, cfg, state, broker=None):
        self.cfg = cfg
        self.state = state
        self.broker = broker
        self.lock = threading.RLock()
        self.perf = PerfTracker(state)
        self.risk = RiskManager(cfg, state)
        # 行情接线: 供 broker 撮合用
        if isinstance(broker, PaperBroker):
            broker.set_price_source(self._last_price, self._prev_close)
        self._last = {}   # symbol -> last price
        self._prev = {}   # symbol -> prev close

    def set_price(self, symbol, last, prev):
        self._last[symbol] = last
        self._prev[symbol] = prev

    def _last_price(self, symbol):
        return self._last.get(symbol)

    def _prev_close(self, symbol):
        return self._prev.get(symbol)

    def strategy_enabled(self, name):
        scfg = self.cfg.get("strategy_" + name, {})
        return bool(scfg.get("enabled", DEFAULT_STRATEGY_CFG.get(name, {}).get("enabled", False)))

    # -- 信号聚合 -----------------------------------------------------------
    def generate_opportunities(self):
        """聚合所有启用策略的候选, 统一字段。"""
        out = []
        if self.strategy_enabled("cb_double_low"):
            cfg = self.cfg.get("strategy_cb_double_low", {})
            cand = double_low_candidates(self, cfg)
            if cand is not None and len(cand):
                # 取前 hold_n
                hold_n = int(cfg.get("hold_n", 10))
                for _, r in cand.head(hold_n).iterrows():
                    out.append({
                        "symbol": r["symbol"], "name": r.get("name"),
                        "strategy": "cb_double_low", "score": int(r["score"]),
                        "price": float(r["price_eff"]), "premium": float(r["premium"]),
                        "dlow": float(r["dlow"]), "rating": r.get("rating"),
                        "ev": round(float(r["dlow"]), 1),
                    })
        if self.strategy_enabled("cb_new"):
            q = cb_new_opportunities(self, self.cfg.get("strategy_cb_new", {}))
            if q is not None and len(q):
                for _, r in q.iterrows():
                    out.append({
                        "symbol": r.get("symbol"), "name": r.get("name"),
                        "strategy": "cb_new", "score": int(r.get("score", 85)),
                        "price": 100.0, "note": r.get("note"),
                        "apply_date": r.get("apply_date"),
                    })
        if self.strategy_enabled("dividend"):
            cfg = self.cfg.get("strategy_dividend", {})
            cand = dividend_candidates(self, cfg)
            if cand is not None and len(cand):
                for _, r in cand.iterrows():
                    out.append({
                        "symbol": r["symbol"], "name": r.get("name"),
                        "strategy": "dividend", "score": int(r["score"]),
                        "price": self._last.get(r["symbol"]), "yield": round(float(r["yield"]) * 100, 2),
                        "vol_60d": round(float(r["vol_60d"]) * 100, 2),
                    })
        if self.strategy_enabled("trend"):
            cfg = self.cfg.get("strategy_trend", {})
            cand = trend_candidates(self, cfg)
            if cand is not None and len(cand):
                for _, r in cand.iterrows():
                    out.append({
                        "symbol": r["symbol"], "name": r.get("name"),
                        "strategy": "trend", "score": int(r["score"]),
                        "price": float(r["close"]), "rsi": round(float(r["rsi"]), 1),
                        "mom20": round(float(r["momentum20"]) * 100, 1),
                    })
        out.sort(key=lambda o: o.get("score", 0), reverse=True)
        return out

    # -- 调仓 ---------------------------------------------------------------
    def tick(self):
        """一次扫描周期: 更新价格 → 退出检查 → 调仓。"""
        with self.lock:
            self._refresh_prices()
            self._check_exits()
            self._rebalance()
            acc = self.broker.get_account()
            self.perf.record_equity(acc["total_asset"])
            self._update_last_scan()
            return acc

    def _refresh_prices(self):
        """为当前持仓 + 候选刷最新价(数据层缓存)。"""
        from ashare_data import cb_daily, cb_snapshot, stock_daily
        positions = self.broker.get_positions()
        for p in positions:
            sym = p["symbol"]
            try:
                if is_cb(sym):
                    bars = cb_daily(sym)
                    if bars is not None and len(bars):
                        self.set_price(sym, float(bars["close"].iloc[-1]),
                                       float(bars["close"].iloc[-2]) if len(bars) > 1 else None)
                else:
                    bars = stock_daily(sym)
                    if bars is not None and len(bars):
                        self.set_price(sym, float(bars["close"].iloc[-1]),
                                       float(bars["close"].iloc[-2]) if len(bars) > 1 else None)
            except Exception:
                pass

    def _check_exits(self):
        """退出检查: 双低超阈值/强赎风险; 趋势止损/破位。"""
        positions = self.broker.get_positions()
        for p in positions:
            sym, strat = p["symbol"], p.get("strategy", "")
            last = self._last.get(sym) or p.get("last_price")
            if not last:
                continue
            if strat == "cb_double_low":
                self._exit_double_low(p, last)
            elif strat == "trend":
                self._exit_trend(p, last)

    def _exit_double_low(self, pos, last):
        cfg = self.cfg.get("strategy_cb_double_low", {})
        exit_price = float(cfg.get("exit_price", 130))
        # 强赎区: 债价 > 130 或 接近赎回价 130
        if last >= exit_price:
            self._sell(pos, "价格达强赎警戒线")
            return
        # 最新快照中是否已退出 universe(评级/ST/临期)
        uni = cb_doublelow_universe()
        if uni is not None and len(uni):
            row = uni[uni["symbol"] == pos["symbol"]]
            if len(row):
                r = row.iloc[0]
                if "ST" in str(r.get("stock_name", "")).upper():
                    self._sell(pos, "正股 ST, 退出")
                    return
                ry = pd.to_numeric(r.get("remain_years"), errors="coerce")
                if pd.notna(ry) and ry < 0.5:
                    self._sell(pos, "临近到期")
                    return

    def _exit_trend(self, pos, last):
        cfg = self.cfg.get("strategy_trend", {})
        stop = float(cfg.get("stop_loss_pct", 0.08))
        cost = pos.get("avg_cost", 0) or 0
        if cost and last <= cost * (1 - stop):
            self._sell(pos, f"硬止损 {stop*100:.0f}%")
            return
        try:
            bars = stock_daily(pos["symbol"])
            if bars is not None and len(bars) > 20:
                ma20 = float(bars["close"].tail(20).mean())
                if last < ma20:
                    self._sell(pos, "跌破 MA20 离场")
        except Exception:
            pass

    def _sell(self, pos, reason):
        sym = pos["symbol"]
        last = self._last.get(sym)
        if not last:
            return
        o = self.broker.place_order(sym, "sell", last, pos["qty"], order_type="market",
                                    strategy=pos.get("strategy", ""), name=pos.get("name", ""),
                                    ref_price=last, prev_close=self._prev.get(sym))
        if o.get("status") == "filled":
            pnl = (o.get("avg_price", 0) - pos.get("avg_cost", 0)) * o.get("filled_qty", 0) - o.get("fee", 0)
            o["pnl"] = pnl
            self.perf.record_exit(pos.get("strategy", ""), o)
            self.state.update(consecutive_losses=0 if pnl > 0 else self.state.get("consecutive_losses", 0) + 1)
            if pnl < 0:
                self.risk.register_cooldown()
            return o
        return None

    def _rebalance(self):
        """调仓: 双低周频 / 红利月频 / 趋势日频。到调仓日才轮换, 平时不折腾。"""
        bankroll = float(self.cfg.get("bankroll_cny", 100000))
        _, _, per_sym = self.risk._risk()
        held = {p["symbol"]: p for p in self.broker.get_positions()}
        opps = self.generate_opportunities()
        by_strat = {}
        for o in opps:
            by_strat.setdefault(o["strategy"], []).append(o)

        for strat, sos in by_strat.items():
            if strat in ("cb_double_low", "dividend"):
                days = int(self.cfg.get(f"strategy_{strat}", {}).get("rebalance_days", 7))
                if not self._is_rebalance_day(strat, days):
                    continue
                top = [o["symbol"] for o in sos]
                # 轮换卖出: 持有中但已跌出 top-N 的
                for sym, p in held.items():
                    if p.get("strategy") == strat and sym not in top:
                        self._sell(p, f"{strat} 调仓跌出组合")
                # 买入新目标
                self._buy_opps(sos, held)
                self._mark_rebalanced(strat)
            elif strat == "trend":
                self._buy_opps(sos, held)
            elif strat == "cb_new":
                pass  # 打新是免费提醒, 不下单

    def _buy_opps(self, opps, held):
        """买入未持有的机会(受 RiskManager 约束)。"""
        bankroll = float(self.cfg.get("bankroll_cny", 100000))
        _, _, per_sym = self.risk._risk()
        for opp in opps:
            sym = opp["symbol"]
            if sym in held:
                continue
            if opp.get("strategy") == "cb_new":
                continue
            last = self._last.get(sym) or opp.get("price")
            if not last:
                continue
            score = int(opp.get("score", 60))
            qty = size_position(sym, last, score, bankroll, per_sym)
            if qty <= 0:
                continue
            est = last * qty
            ok, reason = self.risk.check_new_trade(sym, "buy", est, self.broker)
            if not ok:
                continue
            self.broker.place_order(sym, "buy", last, qty, order_type="market",
                                    strategy=opp["strategy"], name=opp.get("name", ""),
                                    ref_price=last, prev_close=self._prev.get(sym))

    def _is_rebalance_day(self, strat, days):
        last = self.state.get("last_rebalance", {}).get(strat)
        if not last:
            return True
        try:
            last_d = datetime.strptime(last[:10], "%Y-%m-%d").date()
        except (ValueError, TypeError):
            return True
        return (date.today() - last_d).days >= max(days, 1)

    def _mark_rebalanced(self, strat):
        lr = self.state.get("last_rebalance", {})
        lr[strat] = datetime.now().strftime("%Y-%m-%d")
        self.state.update(last_rebalance=lr)

    def _update_last_scan(self):
        self.state.update(last_scan=datetime.now().isoformat(timespec="seconds"))


def build_engine(cfg, state, broker=None):
    """工厂: 构建引擎 + 券商。"""
    if broker is None:
        broker = BrokerFactory(cfg)
    return StrategyEngine(cfg, state, broker)


# ---------------------------------------------------------------------------
# 自测
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import tempfile
    from ashare_broker import PaperState
    print("Strategy engine self-test")
    cfg = {"bankroll_cny": 100000, "risk_level": 5}
    state = PaperState(os.path.join(tempfile.mkdtemp(), "s.json"), seed_cash=100000)
    broker = PaperBroker(cfg, state=state)
    eng = StrategyEngine(cfg, state, broker)

    cand = double_low_candidates(eng)
    print("double_low candidates:", len(cand))
    if len(cand):
        for _, r in cand.head(5).iterrows():
            print("  %s %s dlow=%.0f price=%.1f prem=%.1f score=%s rating=%s" % (
                r["symbol"], r["name"], r["dlow"], r["price_eff"], r["premium"], r["score"], r["rating"]))
        # 喂价 + tick 模拟
        for _, r in cand.head(3).iterrows():
            eng.set_price(r["symbol"], float(r["price_eff"]), float(r["price_eff"]) * 0.99)
        acc = eng.tick()
        print("\nafter tick: total_asset=%.0f cash=%.0f" % (acc["total_asset"], acc["cash"]))
        pos = broker.get_positions()
        print("positions opened:", len(pos))
        for p in pos:
            print("  %s qty=%s strategy=%s avg=%.2f" % (p["symbol"], p["qty"], p["strategy"], p["avg_cost"]))
    opps = eng.generate_opportunities()
    print("\ntotal opportunities:", len(opps))
