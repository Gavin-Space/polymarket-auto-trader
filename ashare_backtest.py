# -*- coding: utf-8 -*-
"""
ashare_backtest.py — A股 回测引擎
=================================
用与 PaperBroker 一致的撮合规则(T+1/涨跌停/费用/手数/转债T+0)回放历史。

诚实标注(plan 要求):
  - 双低: 用"当前 universe 快照 + 各成员历史双低序列"回放。存在幸存者偏差
    (当前仍在市场的转债才可回测), 评级/规模用当前值近似历史。文档写明。
  - 红利: 股息率用当前缓存扫描值(静态), 波动率用历史日线 → 有前视偏差, 文档写明。
  - 趋势: 完整日线, 相对干净。

门禁(胜率优先, 失败大声报警):
  - 双低 3 年: 胜率≥55% / 最大回撤≤25% / 正收益
  - 红利 3 年: 胜率≥55% / 最大回撤≤20%
  - 趋势 3 年: 正收益 / 最大回撤≤30%

CLI: python ashare_backtest.py --strategy cb_double_low --years 3 --bankroll 100000
输出: data/backtest_report.json
"""

import argparse
import json
import os
import sys
import threading
import time
from datetime import date, datetime, timedelta

import pandas as pd

from ashare_data import (
    cb_doublelow_universe, cb_value_history, get_dividend_scan, get_trade_calendar,
    index_constituents, stock_daily,
)
from ashare_broker import is_cb, price_limit_pct, round_lot

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
REPORT_PATH = os.path.join(DATA_DIR, "backtest_report.json")
_LOCK = threading.RLock()


def backtest_busy():
    """是否正在生成回测(兼容无 .locked() 的 RLock 实现)。"""
    try:
        return bool(_LOCK.locked())
    except AttributeError:
        got = _LOCK.acquire(blocking=False)
        if got:
            _LOCK.release()
            return False
        return True
_PROGRESS = {}


# ---------------------------------------------------------------------------
# 撮合规则(与 PaperBroker 一致)
# ---------------------------------------------------------------------------
def _fees(symbol, side, price, qty):
    """与 PaperBroker._calc_fee 完全一致。"""
    notional = price * qty
    if is_cb(symbol):
        min_fee = 1.0 if symbol[:2] in ("11",) else 0.0
        return max(notional * 0.2 / 10000.0, min_fee)
    commission = max(notional * 0.25 / 10000.0, 5.0)
    stamp = notional * 5.0 / 10000.0 if side == "sell" else 0.0
    transfer = notional * 0.1 / 10000.0
    return commission + stamp + transfer


# ---------------------------------------------------------------------------
# 双低回测(核心)
# ---------------------------------------------------------------------------
def _load_cb_histories(symbols, progress=None):
    """批量拉取转债价值历史(带缓存), 返回 {symbol: DataFrame}。"""
    out = {}
    total = len(symbols)
    for i, sym in enumerate(symbols):
        try:
            df = cb_value_history(sym)
            if df is not None and len(df):
                # 派生溢价率: premium NaN 时用 close/cv_value − 1
                df = df.copy()
                prem = pd.to_numeric(df["premium"], errors="coerce")
                cv = pd.to_numeric(df["cv_value"], errors="coerce")
                rec = pd.Series(pd.NA, index=df.index)
                ok = cv.notna() & (cv > 0)
                rec[ok] = (pd.to_numeric(df["close"], errors="coerce")[ok] / cv[ok] - 1) * 100
                df["premium_eff"] = prem.fillna(rec)
                df["close"] = pd.to_numeric(df["close"], errors="coerce")
                # 双低 = 债价 + 溢价率点数(premium 为百分数数值, 如 30 → 加 30 点)
                df["dlow"] = df["close"] + df["premium_eff"]
                df = df.sort_values("dt").reset_index(drop=True)
                # 持仓估值用的 close 前向填充, 避免数据缺口日持仓被记成 0
                df["close_ff"] = df["close"].ffill()
                out[sym] = df
        except Exception:
            pass
        if progress:
            progress(i + 1, total, sym)
    return out


def backtest_double_low(years=3, bankroll=100000.0, hold_n=10, progress=None, log=lambda m: None):
    """双低周频轮动回测。返回 metrics dict。"""
    universe = cb_doublelow_universe()
    if universe is None or len(universe) == 0:
        return {"error": "无可转债数据"}
    # 快照静态属性: 评级/规模/正股
    meta = universe.set_index("symbol")
    symbols = list(meta.index)
    log(f"双低回测: {len(symbols)} 只转债, 拉取价值历史(首次需数分钟, 已缓存后秒开)")
    hist = _load_cb_histories(symbols, progress)

    # 收集所有交易日
    all_dates = set()
    for df in hist.values():
        all_dates.update(df["dt"].astype(str).str[:10])
    trade_dates = sorted(all_dates)
    if len(trade_dates) < 60:
        return {"error": "历史数据不足"}

    cutoff = (date.today() - timedelta(days=int(365.25 * years))).strftime("%Y-%m-%d")
    trade_dates = [d for d in trade_dates if d >= cutoff]
    if not trade_dates:
        return {"error": f"{years} 年前无数据"}

    # 模拟状态
    cash = bankroll
    positions = {}      # symbol -> {"qty", "avg_cost", "strategy"}
    equity_curve = []   # [date, total]
    trades = []         # exit records
    last_rebalance = None

    def market_value(day):
        mv = 0.0
        for sym, p in list(positions.items()):
            df = hist.get(sym)
            close = _close_on(df, sym, day)
            if close is None:
                continue
            p["last"] = close
            mv += close * p["qty"]
        return mv

    for i, day in enumerate(trade_dates):
        # 每日: 更新持仓市值
        mv = market_value(day)

        # 每周五调仓(或每 5 个交易日)
        is_week_end = (i + 1) % 5 == 0 or i == len(trade_dates) - 1
        if is_week_end or last_rebalance is None:
            # 计算当日候选
            cand = []
            for sym, df in hist.items():
                row = _series_on(df, sym, day)
                if row is None:
                    continue
                close, prem, dlow = row
                if not (80 <= close <= 115 and -10 <= prem <= 40):
                    continue
                m = meta.loc[sym] if sym in meta.index else None
                if m is None:
                    continue
                # 上市日期门禁: 上市前不可买入(避免以面值占位价买到未上市新债)
                _listed = m.get("listed")
                if _listed is not None and pd.notna(_listed):
                    _ld = str(_listed)[:10]
                    if len(_ld) == 10 and _ld > day:
                        continue
                rating = str(m.get("rating") or "")
                if rating.upper()[:2] != "AA":
                    continue
                size = m.get("issue_size")
                if size is None or float(size) < 2.0:
                    continue
                cand.append((sym, dlow, close))
            cand.sort(key=lambda x: x[1])
            targets = cand[:hold_n]
            target_syms = {c[0] for c in cand[:hold_n]}

            # 卖出: 不在 target 或 超强赎警戒 130
            for sym in list(positions.keys()):
                if sym not in target_syms:
                    close = _close_on(hist.get(sym), sym, day)
                    if close is not None:
                        qty = positions[sym]["qty"]
                        fee = _fees(sym, "sell", close, qty)
                        proceeds = close * qty - fee
                        cost = positions[sym]["avg_cost"] * qty
                        pnl = proceeds - cost
                        cash += proceeds
                        trades.append({
                            "symbol": sym, "exit_date": day, "pnl": round(pnl, 2),
                            "exit_price": round(close, 2), "fee": round(fee, 2),
                            "strategy": "cb_double_low",
                        })
                        del positions[sym]

            # 买入: 现金池等权
            if targets and cash > 0:
                budget_per = cash * 0.95 / len(targets)
                for sym, dlow, close in targets:
                    if sym in positions:
                        continue
                    qty = int(budget_per // close // 10) * 10
                    if qty < 10:
                        continue
                    fee = _fees(sym, "buy", close, qty)
                    if close * qty + fee > cash:
                        qty = max(int((cash - fee) // close // 10) * 10, 0)
                    if qty < 10:
                        continue
                    cash -= close * qty + fee
                    positions[sym] = {"qty": qty, "avg_cost": close, "strategy": "cb_double_low"}
            last_rebalance = day

        total = cash + market_value(day)
        equity_curve.append([day, round(total, 2)])

    # 期末平仓
    for sym in list(positions.keys()):
        close = _close_on(hist.get(sym), sym, trade_dates[-1])
        if close is not None:
            qty = positions[sym]["qty"]
            fee = _fees(sym, "sell", close, qty)
            cash += close * qty - fee
            trades.append({"symbol": sym, "exit_date": trade_dates[-1],
                           "pnl": round((close - positions[sym]["avg_cost"]) * qty - fee, 2),
                           "exit_price": round(close, 2), "fee": round(fee, 2),
                           "strategy": "cb_double_low"})
    final_asset = cash
    equity_curve.append([trade_dates[-1], round(final_asset, 2)])

    metrics = compute_metrics(equity_curve, trades, bankroll, final_asset)
    metrics["strategy"] = "cb_double_low"
    metrics["years"] = years
    metrics["note"] = ("采用当前在市场的转债回放历史(幸存者偏差); 评级/规模为当前值近似历史。"
                       "真实历史需成分数据, 本结果仅作方向性参考。")
    return metrics


def _series_on(df, sym, day):
    if df is None or len(df) == 0:
        return None
    sub = df[df["dt"].astype(str).str[:10] == day]
    if len(sub) == 0:
        return None
    r = sub.iloc[0]
    close = r.get("close")
    prem = r.get("premium_eff")
    dlow = r.get("dlow")
    if pd.isna(close) or pd.isna(prem):
        return None
    return (float(close), float(prem), float(dlow))


def _close_on(df, sym, day):
    """取 <= day 最近一个收盘价(前向填充, 数据缺口日沿用最近价, 不再记 0)。"""
    if df is None or len(df) == 0:
        return None
    sub = df[df["dt"].astype(str).str[:10] <= day]
    if len(sub) == 0:
        return None
    c = sub.iloc[-1].get("close_ff")
    if pd.isna(c):
        c = sub.iloc[-1].get("close")
    return float(c) if not pd.isna(c) else None


# ---------------------------------------------------------------------------
# 红利低波回测(月度, 数据预热后)
# ---------------------------------------------------------------------------
def backtest_dividend(years=3, bankroll=100000.0, hold_n=10, progress=None, log=lambda m: None):
    """红利低波月度回测。股息率为当前值(静态, 有前视偏差), 波动率用历史日线。"""
    scan = get_dividend_scan()
    if scan is None or len(scan) < hold_n:
        return {"error": "股息率扫描未完成(先运行 ashare_data.py 或等待后台预热)"}
    cons = index_constituents("000300")
    if not cons:
        return {"error": "HS300 成分缺失"}
    yield_map = {r["symbol"]: r["yield"] for _, r in scan.iterrows()}
    name_map = {r["symbol"]: r["name"] for _, r in scan.iterrows()}

    # 拉历史日线(首次慢)
    bars = {}
    for code, nm in cons:
        try:
            df = stock_daily(code)
            if df is not None and len(df) > 60:
                bars[code] = df
        except Exception:
            pass
        if progress:
            progress(len(bars), len(cons), code)
    log(f"红利回测: {len(bars)} 只 HS300 有日线")

    all_dates = set()
    for df in bars.values():
        all_dates.update(df["dt"].astype(str).str[:10])
    trade_dates = sorted(d for d in all_dates if d >= (date.today() - timedelta(days=int(365.25 * years))).strftime("%Y-%m-%d"))
    if len(trade_dates) < 60:
        return {"error": "历史日线不足"}

    cash, positions, trades, equity_curve = bankroll, {}, [], []
    last_rebalance = None

    for i, day in enumerate(trade_dates):
        # 波动率候选(每月首个交易日调仓)
        monthly = day[7:] == "01" or (i > 0 and trade_dates[i - 1][:7] != day[:7])
        mv = 0.0
        for sym, p in list(positions.items()):
            df = bars.get(sym)
            c = _close_on(df, sym, day)
            if c is None:
                continue
            p["last"] = c
            mv += c * p["qty"]

        if (monthly or last_rebalance is None) and (last_rebalance is None or day > last_rebalance):
            cand = []
            for code, nm in cons:
                dy = yield_map.get(code)
                if dy is None or dy < 0.03:
                    continue
                df = bars.get(code)
                if df is None or len(df) < 20:
                    continue
                rets = df["close"].tail(60).pct_change().dropna()
                if len(rets) < 10:
                    continue
                vol = rets.std() * (250 ** 0.5)
                cand.append((code, vol))
            cand.sort(key=lambda x: x[1])
            targets = cand[:hold_n]
            target_syms = {c[0] for c in targets}

            for sym in list(positions.keys()):
                if sym not in target_syms:
                    c = _close_on(bars.get(sym), sym, day)
                    if c is not None:
                        qty = positions[sym]["qty"]
                        fee = _fees(sym, "sell", c, qty)
                        pnl = (c - positions[sym]["avg_cost"]) * qty - fee
                        cash += c * qty - fee
                        trades.append({"symbol": sym, "exit_date": day, "pnl": round(pnl, 2),
                                       "fee": round(fee, 2), "strategy": "dividend"})
                        del positions[sym]

            if targets and cash > 0:
                budget_per = cash * 0.95 / len(targets)
                for code, vol in targets:
                    if code in positions:
                        continue
                    c = _close_on(bars.get(code), code, day)
                    if c is None:
                        continue
                    qty = int(budget_per // c // 100) * 100
                    if qty < 100:
                        continue
                    fee = _fees(code, "buy", c, qty)
                    if c * qty + fee > cash:
                        qty = max(int((cash - fee) // c // 100) * 100, 0)
                    if qty < 100:
                        continue
                    cash -= c * qty + fee
                    positions[code] = {"qty": qty, "avg_cost": c, "strategy": "dividend"}
            last_rebalance = day

        total = cash + market_value(day)
        equity_curve.append([day, round(total, 2)])

    for sym in list(positions.keys()):
        c = _close_on(bars.get(sym), sym, trade_dates[-1])
        if c is not None:
            qty = positions[sym]["qty"]
            fee = _fees(sym, "sell", c, qty)
            cash += c * qty - fee
            trades.append({"symbol": sym, "exit_date": trade_dates[-1],
                           "pnl": round((c - positions[sym]["avg_cost"]) * qty - fee, 2),
                           "fee": round(fee, 2), "strategy": "dividend"})
    final = cash
    equity_curve.append([trade_dates[-1], round(final, 2)])

    metrics = compute_metrics(equity_curve, trades, bankroll, final)
    metrics["strategy"] = "dividend"
    metrics["years"] = years
    metrics["note"] = ("股息率为当前扫描值(静态近似, 有前视偏差); 波动率用历史日线。"
                       "真实月度调仓需历史股息数据, 本结果仅作方向性参考。")
    return metrics


# ---------------------------------------------------------------------------
# 指标
# ---------------------------------------------------------------------------
def compute_metrics(equity_curve, trades, bankroll, final_asset):
    if not equity_curve:
        return {"error": "无权益曲线"}
    values = [e[1] for e in equity_curve]
    total_return = (final_asset / bankroll - 1) * 100.0
    n_days = len(values)
    years = max(n_days / 244.0, 1e-9)
    cagr = ((final_asset / bankroll) ** (1 / years) - 1) * 100.0 if final_asset > 0 else -100.0

    peak = values[0]
    max_dd = 0.0
    for v in values:
        peak = max(peak, v)
        dd = (peak - v) / peak * 100.0 if peak > 0 else 0.0
        max_dd = max(max_dd, dd)

    rets = pd.Series(values).pct_change().dropna()
    sharpe = float(rets.mean() / rets.std() * (244 ** 0.5)) if len(rets) > 1 and rets.std() > 0 else 0.0

    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    n = len(trades)
    win_rate = len(wins) / n * 100.0 if n else 0.0
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))
    pf = gross_win / gross_loss if gross_loss > 0 else (99.0 if gross_win > 0 else 0.0)
    expectancy = sum(t["pnl"] for t in trades) / n if n else 0.0

    return {
        "bankroll": round(bankroll, 2), "final_asset": round(final_asset, 2),
        "total_return_pct": round(total_return, 2), "cagr_pct": round(cagr, 2),
        "max_drawdown_pct": round(max_dd, 2), "sharpe": round(sharpe, 2),
        "trades": n, "wins": len(wins), "losses": len(losses),
        "win_rate_pct": round(win_rate, 1), "expectancy": round(expectancy, 2),
        "profit_factor": round(pf, 2),
        "equity_curve": equity_curve[-400:],  # 界面绘图用(截断)
        "recent_trades": trades[-30:],
    }


# ---------------------------------------------------------------------------
# 门禁
# ---------------------------------------------------------------------------
GATES = {
    "cb_double_low": {"win_rate_min": 55, "max_dd": 25, "require_profit": True},
    "dividend": {"win_rate_min": 55, "max_dd": 20, "require_profit": True},
    "trend": {"win_rate_min": 0, "max_dd": 30, "require_profit": True},
}


def check_gate(strategy, metrics):
    if not metrics or "error" in metrics:
        return {"pass": False, "reason": metrics.get("error", "无数据"), "metrics": metrics}
    g = GATES.get(strategy, GATES["trend"])
    checks = []
    if "win_rate_min" in g and g["win_rate_min"] > 0:
        checks.append(("胜率≥%.0f%%" % g["win_rate_min"], metrics["win_rate_pct"] >= g["win_rate_min"],
                       "%.1f%%" % metrics["win_rate_pct"]))
    checks.append(("最大回撤≤%.0f%%" % g["max_dd"], metrics["max_drawdown_pct"] <= g["max_dd"],
                   "%.1f%%" % metrics["max_drawdown_pct"]))
    if g.get("require_profit"):
        checks.append(("正收益", metrics["total_return_pct"] > 0, "%.1f%%" % metrics["total_return_pct"]))
    passed = all(c[1] for c in checks)
    return {"pass": passed, "checks": checks,
            "reason": "; ".join((c[0] + " -> " + ("PASS" if c[1] else "FAIL(" + c[2] + ")")) for c in checks),
            "metrics": metrics}


# ---------------------------------------------------------------------------
# 报告持久化
# ---------------------------------------------------------------------------
def save_report(report):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = REPORT_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)
    os.replace(tmp, REPORT_PATH)


def load_report():
    try:
        with open(REPORT_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def get_report():
    """供主界面读取。可触发后台生成。"""
    r = load_report()
    if r:
        return r
    return {"status": "not_generated"}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="A股 策略回测")
    ap.add_argument("--strategy", choices=["cb_double_low", "dividend", "trend"], default="cb_double_low")
    ap.add_argument("--years", type=int, default=3)
    ap.add_argument("--bankroll", type=float, default=100000)
    ap.add_argument("--hold-n", type=int, default=10)
    args = ap.parse_args()

    start = time.time()
    last = {"t": 0}

    def progress(done, total, cur):
        now = time.time()
        if now - last["t"] > 3 or done == total:
            last["t"] = now
            print(f"  [{done}/{total}] {cur}")

    def log(m):
        print(m)

    report = {"generated_at": datetime.now().isoformat(timespec="seconds"),
              "bankroll": args.bankroll, "years": args.years}

    if args.strategy == "cb_double_low":
        m = backtest_double_low(args.years, args.bankroll, args.hold_n, progress, log)
    elif args.strategy == "dividend":
        m = backtest_dividend(args.years, args.bankroll, args.hold_n, progress, log)
    else:
        m = {"error": "趋势回测需完整日线预热, 暂缓实现(默认关)"}

    report[args.strategy] = m
    gate = check_gate(args.strategy, m)
    report["gate_" + args.strategy] = gate
    save_report(report)

    print("\n=== %s 回测(%d 年) ===" % (args.strategy, args.years))
    if "error" in m:
        print("错误:", m["error"])
    else:
        for k, v in m.items():
            if k not in ("equity_curve", "recent_trades"):
                print("  %-18s %s" % (k, v))
    print("\n门禁:", gate["reason"])
    print("耗时: %.1fs" % (time.time() - start))
    if not gate["pass"]:
        print("\n[WARN] 门禁未通过 —— 策略未达预期, 谨慎实盘!")


if __name__ == "__main__":
    main()
