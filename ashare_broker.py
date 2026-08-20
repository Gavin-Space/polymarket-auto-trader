# -*- coding: utf-8 -*-
"""
ashare_broker.py — A股 券商适配层
================================
镜像 PolyAuto 的 CLOBTrader 接缝: 引擎只依赖 BrokerAdapter 抽象接口,
从不直接 import 券商客户端。默认 PaperBroker(仿真撮合), QMTBroker(实盘, 可选)。

仿真撮合规则(与回测引擎共用, 保证行为一致):
  - 品种判定: 代码 11x/12x → 可转债(T+0, 无涨跌停, 10张/手);
              300/301/688 → ±20%; 含 ST → ±5%; 其余主板 ±10%
  - 手数: 股票 100 股/手(卖出允许余股); 可转债 10 张/手
  - 费用: 股票 佣金万2.5 双边 最低¥5 + 印花税万5 仅卖出 + 过户费万0.1 双边
          转债 佣金万2 双边 沪最低¥1/深无 + 免印花税/过户费
  - T+1: 股票当日买入次一交易日才能卖出; 可转债 T+0
  - 涨跌停: 买入价 > 昨收×(1+limit) 拒单; 卖出价 < 昨收×(1−limit) 拒单
  - 成交: 限价单在 ref_price 穿越限价时成交; 市价单按 ref_price

双保险: 引擎仅当 broker=="qmt" AND trading_mode=="live" 才走实盘, 否则一律仿真。
"""

import json
import os
import threading
import time
import uuid
from abc import ABC, abstractmethod
from datetime import date, datetime, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
STATE_PATH = os.path.join(DATA_DIR, "ashare_state.json")

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
_SH = ("600", "601", "603", "605", "688")
_SZ = ("000", "001", "002", "003", "300", "301")
_CB_SH = ("11",)
_CB_SZ = ("12",)
_GEM = ("300", "301")
_STAR = ("688",)

DEFAULT_FEES = {
    "stock_commission_bps": 0.25,      # 佣金万2.5
    "stock_commission_min": 5.0,       # 最低 ¥5
    "stamp_bps": 5.0,                  # 印花税万5, 仅卖出
    "transfer_bps": 0.1,               # 过户费万0.1 双边
    "cb_commission_bps": 0.2,          # 转债佣金万2
    "cb_commission_min_sh": 1.0,       # 沪转债最低 ¥1
    "cb_commission_min_sz": 0.0,       # 深转债无最低
}


def _lot(symbol6):
    """手数规格: 转债 10 张/手, 股票 100 股/手。"""
    s = symbol6.strip()
    if s[:2] in _CB_SH or s[:2] in _CB_SZ:
        return 10
    return 100


def is_cb(symbol6):
    s = symbol6.strip()
    return s[:2] in _CB_SH or s[:2] in _CB_SZ


def price_limit_pct(symbol6, name=""):
    """涨跌停幅度: 创业/科创 ±20%, ST ±5%, 主板 ±10%, 转债无限制。"""
    s = symbol6.strip()
    if is_cb(s):
        return 1.0
    if s.startswith(_GEM) or s.startswith(_STAR):
        return 0.20
    if "ST" in (name or "").upper():
        return 0.05
    return 0.10


def _fmt_amt(v):
    return round(v, 2)


def round_lot(qty, symbol6, side="buy"):
    """按手数取整到可下单量(买必须整手, 卖允许余股)。"""
    s = symbol6.strip()
    lot = _lot(s)
    qty = float(qty)
    if side == "sell":
        # 卖出允许非整手(余股), 但至少 1 手等值
        if qty < lot:
            return 0.0
        return qty
    return int(qty // lot) * lot


# ---------------------------------------------------------------------------
# 抽象接口
# ---------------------------------------------------------------------------
class BrokerAdapter(ABC):
    @abstractmethod
    def get_account(self):
        """账户: {cash, frozen, market_value, total_asset}"""

    @abstractmethod
    def place_order(self, symbol, side, price, quantity, order_type="limit", strategy="", name="", prev_close=None, ref_price=None):
        """下单。返回订单 dict。"""

    @abstractmethod
    def cancel(self, order_id):
        """撤单。"""

    @abstractmethod
    def get_positions(self):
        """持仓列表。"""

    @abstractmethod
    def get_orders(self, status=None):
        """订单列表。"""


# ---------------------------------------------------------------------------
# 状态持久化(BotState 风格: RLock + 原子写)
# ---------------------------------------------------------------------------
class PaperState:
    def __init__(self, path=STATE_PATH, seed_cash=100000.0):
        self.path = path
        self._lock = threading.RLock()
        os.makedirs(DATA_DIR, exist_ok=True)
        self._data = self._load() or self._defaults(seed_cash)

    def _defaults(self, cash):
        return {
            "cash": float(cash),
            "frozen": 0.0,
            "market_value": 0.0,
            "positions": {},      # symbol -> pos dict
            "orders": {},         # order_id -> order dict
            "order_seq": 0,
            "total_fees": 0.0,
            "realized_pnl": 0.0,
        }

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError, TypeError):
            return None

    def _save(self):
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def get(self, key, default=None):
        with self._lock:
            return self._data.get(key, default)

    def set(self, key, val):
        with self._lock:
            self._data[key] = val
            self._save()

    def update(self, **kw):
        with self._lock:
            self._data.update(kw)
            self._save()

    def mutate(self, fn):
        """在锁内修改数据字典并持久化。"""
        with self._lock:
            fn(self._data)
            self._save()

    def next_order_id(self):
        with self._lock:
            self._data["order_seq"] += 1
            self._save()
            return f"P{self._data['order_seq']:06d}"


# ---------------------------------------------------------------------------
# 仿真券商
# ---------------------------------------------------------------------------
class PaperBroker(BrokerAdapter):
    def __init__(self, cfg=None, state=None, price_source=None, prev_close_source=None):
        self.cfg = cfg or {}
        self.fees = dict(DEFAULT_FEES)
        fees_cfg = (cfg or {}).get("fees")
        if isinstance(fees_cfg, dict):
            self.fees.update(fees_cfg)
        self.state = state or PaperState(seed_cash=(cfg or {}).get("bankroll_cny", 100000.0))
        # price_source(symbol)->last; prev_close_source(symbol)->prev_close
        self.price_source = price_source
        self.prev_close_source = prev_close_source
        self._fill_lock = threading.Lock()

    # -- 外部数据接入 ------------------------------------------------------
    def set_price_source(self, price_source, prev_close_source=None):
        self.price_source = price_source
        self.prev_close_source = prev_close_source

    def _last_price(self, symbol):
        if self.price_source:
            return self.price_source(symbol)
        return None

    def _prev_close(self, symbol):
        if self.prev_close_source:
            return self.prev_close_source(symbol)
        return None

    # -- 账户 --------------------------------------------------------------
    def get_account(self):
        with self._fill_lock:
            pos = self.state.get("positions", {})
            cash = self.state.get("cash", 0.0)
            frozen = self._frozen_est()
            # 更新市值(有价格源时)
            if self.price_source:
                for p in pos.values():
                    last = self.price_source(p["symbol"])
                    if last:
                        p["last_price"] = last
            mv = sum(p.get("qty", 0) * (p.get("last_price") or p.get("avg_cost") or 0) for p in pos.values())
            total = cash + mv
            self.state.update(market_value=round(mv, 2), frozen=round(frozen, 2))
            return {
                "cash": round(cash, 2), "frozen": round(frozen, 2),
                "market_value": round(mv, 2), "total_asset": round(total, 2),
            }

    # -- 交易 --------------------------------------------------------------
    def place_order(self, symbol, side, price, quantity, order_type="limit",
                    strategy="", name="", prev_close=None, ref_price=None):
        """下单并立即尝试撮合。返回订单 dict。

        symbol6: 6 位代码。side: buy/sell。order_type: limit/market。
        price: 限价(limit) 或 忽略(market)。quantity: 股数/张数(自动整手)。
        prev_close: 昨收(涨跌停校验); ref_price: 参考价(撮合)。
        引擎在交易时段传入昨日收盘价与最新价。
        """
        symbol = str(symbol).strip()
        ref_price = ref_price if ref_price is not None else self._last_price(symbol)
        prev_close = prev_close if prev_close is not None else self._prev_close(symbol)
        side = side.lower()
        order_type = order_type.lower()

        order_id = self.state.next_order_id()
        order = {
            "order_id": order_id, "symbol": symbol, "side": side,
            "price": float(price) if price else None, "quantity": float(quantity),
            "order_type": order_type, "status": "open", "filled_qty": 0.0,
            "avg_price": None, "fee": 0.0, "strategy": strategy or "",
            "name": name or "", "created": datetime.now().isoformat(timespec="seconds"),
            "last_update": datetime.now().isoformat(timespec="seconds"),
            "reject_reason": None, "dry_run": True,
        }

        # 手数取整
        qty = round_lot(quantity, symbol, side)
        if qty < _lot(symbol) if side == "buy" else qty <= 0:
            order.update(status="rejected", reject_reason=f"数量不足一手({int(_lot(symbol))})")
            self._store_order(order)
            return order

        # 涨跌停校验(有昨收时)
        if prev_close and order_type in ("limit", "market"):
            lim = price_limit_pct(symbol, name)
            if lim < 1.0:
                if side == "buy" and price and price > prev_close * (1 + lim):
                    order.update(status="rejected", reject_reason=f"买入价超涨停({prev_close*(1+lim):.2f})")
                    self._store_order(order)
                    return order
                if side == "sell" and price and price < prev_close * (1 - lim):
                    order.update(status="rejected", reject_reason=f"卖出价低于跌停({prev_close*(1-lim):.2f})")
                    self._store_order(order)
                    return order

        # T+1 校验(卖出时)
        if side == "sell" and not is_cb(symbol):
            pos = self.state.get("positions", {}).get(symbol)
            if pos and pos.get("t_plus1_date"):
                if datetime.now().date().isoformat() < str(pos["t_plus1_date"]):
                    order.update(status="rejected",
                                 reject_reason=f"T+1: {symbol} 当日买入次日才能卖出")
                    self._store_order(order)
                    return order

        # 资金/持仓校验 + 撮合
        with self._fill_lock:
            frozen = self._frozen_est()
            if side == "buy":
                cash = self.state.get("cash", 0.0)
                cost = price * qty if price and order_type == "limit" else (ref_price or 0) * qty
                if ref_price is None:
                    order.update(status="open", reject_reason="无价格源, 挂单待成交")
                    self._store_order(order)
                    return order
                if cost > cash - frozen:
                    order.update(status="rejected",
                                 reject_reason=f"资金不足(需¥{cost:.0f}, 可用¥{cash-frozen:.0f})")
                    self._store_order(order)
                    return order
            else:  # sell
                pos = self.state.get("positions", {}).get(symbol)
                if not pos or pos.get("qty", 0) < qty:
                    order.update(status="rejected", reject_reason=f"持仓不足")
                    self._store_order(order)
                    return order

            # 撮合: 限价穿越 or 市价; 未达限价 → 保持挂单(open)
            filled = self._match(symbol, side, price, qty, ref_price, order_type)
            if not filled:
                order.update(status="open",
                             reject_reason="未达限价, 挂单待成交" if ref_price is not None else "无价格源")
                self._store_order(order)
                return order

            fill_price, fill_qty = filled
            self._execute(symbol, side, fill_price, fill_qty, order, name)
            self._store_order(order)
        return order

    def _frozen_est(self):
        """冻结估算: 所有 open 买单的名义金额之和。"""
        total = 0.0
        for o in self.state.get("orders", {}).values():
            if o.get("status") == "open" and o.get("side") == "buy":
                p = o.get("price") or 0
                total += p * o.get("quantity", 0)
        return total

    def _match(self, symbol, side, price, qty, ref_price, order_type):
        """撮合规则: 买 limit 在 ref<=price 成交, 卖 limit 在 ref>=price 成交。"""
        if ref_price is None:
            return None
        if order_type == "market":
            return (float(ref_price), qty)
        if side == "buy":
            return (float(ref_price), qty) if float(ref_price) <= float(price) else None
        else:
            return (float(ref_price), qty) if float(ref_price) >= float(price) else None

    def _execute(self, symbol, side, fill_price, fill_qty, order, name=""):
        fee = self._calc_fee(symbol, side, fill_price, fill_qty)
        order.update(status="filled", filled_qty=fill_qty, avg_price=round(fill_price, 3),
                     fee=round(fee, 2), last_update=datetime.now().isoformat(timespec="seconds"))

        def mut(data):
            data["total_fees"] = round(data.get("total_fees", 0.0) + fee, 2)
            if side == "buy":
                pos = data.setdefault("positions", {}).get(symbol)
                if pos:
                    new_qty = pos["qty"] + fill_qty
                    pos["avg_cost"] = (pos["avg_cost"] * pos["qty"] + fill_price * fill_qty) / new_qty
                    pos["qty"] = new_qty
                else:
                    data["positions"][symbol] = {
                        "symbol": symbol, "name": name, "qty": fill_qty,
                        "avg_cost": round(fill_price, 3), "strategy": order["strategy"],
                        "t_plus1_date": None if is_cb(symbol)
                        else (datetime.now().date() + timedelta(days=1)).isoformat(),
                    }
                data["cash"] = round(data.get("cash", 0.0) - fill_price * fill_qty - fee, 2)
            else:
                pos = data["positions"].get(symbol)
                if pos:
                    avg = pos.get("avg_cost", 0)
                    pos["qty"] = pos["qty"] - fill_qty
                    # 已实现盈亏 = (卖价 − 均价) × 数量 − 费用
                    pnl = (fill_price - avg) * fill_qty - fee
                    data["realized_pnl"] = round(data.get("realized_pnl", 0.0) + pnl, 2)
                    if pos["qty"] <= 0:
                        del data["positions"][symbol]
                    data["cash"] = round(data.get("cash", 0.0) + fill_price * fill_qty - fee, 2)

        self.state.mutate(mut)

    def _calc_fee(self, symbol, side, price, qty):
        f = self.fees
        notional = price * qty
        if is_cb(symbol):
            min_fee = f["cb_commission_min_sh"] if symbol[:2] in _CB_SH else f["cb_commission_min_sz"]
            commission = max(notional * f["cb_commission_bps"] / 10000.0, min_fee)
            return commission  # 转债免印花税/过户费
        commission = max(notional * f["stock_commission_bps"] / 10000.0, f["stock_commission_min"])
        stamp = notional * f["stamp_bps"] / 10000.0 if side == "sell" else 0.0
        transfer = notional * f["transfer_bps"] / 10000.0
        return commission + stamp + transfer

    # -- 持仓/订单 ----------------------------------------------------------
    def get_positions(self):
        with self._fill_lock:
            if self.price_source:
                for p in self.state.get("positions", {}).values():
                    last = self.price_source(p["symbol"])
                    if last:
                        p["last_price"] = last
            return list(self.state.get("positions", {}).values())

    def get_orders(self, status=None):
        orders = list(self.state.get("orders", {}).values())
        if status:
            orders = [o for o in orders if o.get("status") == status]
        return sorted(orders, key=lambda o: o.get("created", ""), reverse=True)

    def cancel(self, order_id):
        def mut(data):
            o = data.get("orders", {}).get(order_id)
            if o and o.get("status") == "open":
                o["status"] = "cancelled"
                o["last_update"] = datetime.now().isoformat(timespec="seconds")
        self.state.mutate(mut)
        return self.state.get("orders", {}).get(order_id, {}).get("status") == "cancelled"

    def _store_order(self, order):
        def mut(data):
            data.setdefault("orders", {})[order["order_id"]] = order
        self.state.mutate(mut)


# ---------------------------------------------------------------------------
# 实盘券商(QMT / xtquant, 可选)
# ---------------------------------------------------------------------------
class QMTBroker(BrokerAdapter):
    """连接 miniQMT 客户端(极简模式)。xtquant 随 QMT 客户端附带, 不在 PyPI。

    引擎双保险: 仅 broker=="qmt" AND trading_mode=="live" 时由引擎使用本类。
    未安装 xtquant 时构造抛错, 上层强制回退 PaperBroker。
    """

    def __init__(self, cfg=None):
        cfg = cfg or {}
        self.qmt_path = cfg.get("qmt", {}).get("path", "D:/qmt")
        self.account_id = cfg.get("qmt", {}).get("account_id", "")
        self._ok = False
        try:
            from xtquant.xttrader import XtQuantTrader  # noqa
            from xtquant.xttype import StockAccount  # noqa
            self._have_xt = True
        except ImportError:
            self._have_xt = False
            raise RuntimeError("xtquant 未安装: 请先启动 QMT 客户端并在极简模式下运行, 再安装其自带 xtquant")

    def get_account(self):
        return {"cash": 0, "frozen": 0, "market_value": 0, "total_asset": 0, "error": "xtquant未初始化"}

    def place_order(self, *a, **kw):
        raise RuntimeError("实盘下单需先通过 QMT 初始化连接")


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------
def BrokerFactory(cfg=None):
    """按配置创建券商。默认 PaperBroker。cfg["broker"]=="qmt" → QMTBroker。"""
    cfg = cfg or {}
    if str(cfg.get("broker", "paper")).lower() == "qmt":
        try:
            return QMTBroker(cfg)
        except RuntimeError:
            return PaperBroker(cfg)
    return PaperBroker(cfg)


# ---------------------------------------------------------------------------
# 单元测试断言
# ---------------------------------------------------------------------------
def _self_test():
    import tempfile
    tmpdir = tempfile.mkdtemp()
    state = PaperState(os.path.join(tmpdir, "state.json"), seed_cash=500000)
    prices = {
        "600519": (1300.0, 1290.0),   # (last, prev_close)
        "000001": (10.0, 9.9),
        "113537": (110.0, 109.0),
        "123456": (105.0, 104.0),
        "300750": (60.0, 59.0),       # 创业板 ±20%
        "600001": (5.0, 5.1),         # 假设 ST
        "688001": (40.0, 39.0),       # 科创 ±20%
    }
    broker = PaperBroker({"bankroll_cny": 500000}, state=state)
    broker.set_price_source(lambda s: prices[s][0], lambda s: prices[s][1])

    fails = []

    def check(desc, cond):
        print(("PASS" if cond else "FAIL") + "  " + desc)
        if not cond:
            fails.append(desc)

    # 1) 买入 100 股贵州茅台整手
    o = broker.place_order("600519", "buy", 1300.0, 100, name="贵州茅台")
    check("买入 100 股整手成交", o["status"] == "filled" and o["filled_qty"] == 100)
    acc = broker.get_account()
    check("现金扣减正确", abs(acc["cash"] - (500000 - 1300*100 - o["fee"])) < 0.01)
    check("佣金≥5元", o["fee"] >= 5.0)

    # 2) T+1: 当日买股票不能卖
    o = broker.place_order("600519", "sell", 1310.0, 100)
    check("T+1 拒绝当日卖出股票", o["status"] == "rejected" and "T+1" in (o["reject_reason"] or ""))

    # 3) 可转债 T+0: 当日可卖(市价单)
    o1 = broker.place_order("113537", "buy", 110.0, 10, name="某转债")
    check("转债买入 10 张成交", o1["status"] == "filled" and o1["filled_qty"] == 10)
    o2 = broker.place_order("113537", "sell", None, 10, order_type="market")
    check("转债 T+0 当日可卖", o2["status"] == "filled")

    # 4) 手数取整: 买入 350 股 → 300 股; 转债 35 张 → 30 张
    o = broker.place_order("000001", "buy", 10.0, 350, name="平安银行")
    check("股票手数取整到 300", o["filled_qty"] == 300)
    o = broker.place_order("123456", "buy", 105.0, 35, name="深市转债")
    check("转债手数取整到 30 张", o["filled_qty"] == 30)
    # 模拟 000001 为"昨日已买入"(跳过 T+1, 以便测印花税/余股卖出)
    state.mutate(lambda d: d["positions"]["000001"].update(t_plus1_date="2020-01-01"))

    # 5) 沪转债最低佣金 ¥1
    o = broker.place_order("113537", "buy", 110.0, 10, name="某转债")
    check("沪转债佣金≥1元", o["fee"] >= 1.0)
    # 深转债无最低(实际佣金 = 105*10*0.0002 = 0.21)
    o = broker.place_order("123456", "buy", 105.0, 10, name="深市转债")
    check("深转债佣金可<1元", 0 < o["fee"] < 1.0)

    # 6) 涨停拒买: 主板 10% → 涨停价 9.9*1.1=10.89, 挂 10.9 拒
    o = broker.place_order("000001", "buy", 10.90, 100)
    check("涨停价以上买入被拒", o["status"] == "rejected" and "涨停" in (o["reject_reason"] or ""))
    # 创业板 ±20%: 59*1.2=70.8, 挂 71 拒
    o = broker.place_order("300750", "buy", 71.0, 100, name="宁德时代")
    check("创业板+20%涨停拒买", o["status"] == "rejected")

    # 7) 印花税仅卖出
    o = broker.place_order("000001", "sell", 10.0, 300)
    # 卖出费用含印花税(万5) = 10*300*0.0005=1.5 + 佣金5 + 过户费0.03
    check("卖出含印花税", o["status"] == "filled" and o["fee"] >= 5.0 + 1.5)

    # 8) 跌停拒卖: 主板 9.9*(1-0.1)=8.91, 挂 8.9 拒
    # 先买入 000001 才有持仓(前面已卖光), 重新买
    broker.place_order("000001", "buy", 10.0, 100)
    o = broker.place_order("000001", "sell", 8.90, 100)
    check("跌停价以下卖出被拒", o["status"] == "rejected" and "跌停" in (o["reject_reason"] or ""))

    # 9) 市场单按 last 成交
    o = broker.place_order("600519", "buy", None, 100, order_type="market")
    check("市价单成交", o["status"] == "filled" and o["avg_price"] == 1300.0)

    # 10) ST ±5%: 5.1*1.05=5.355, 挂 5.36 拒
    o = broker.place_order("600001", "buy", 5.36, 100, name="ST某某")
    check("ST ±5% 拒买", o["status"] == "rejected")

    # 11) 卖出手数: 允许余股但至少一手
    o = broker.place_order("000001", "buy", 10.0, 350)  # 300
    state.mutate(lambda d: d["positions"]["000001"].update(t_plus1_date="2020-01-01"))
    o = broker.place_order("000001", "sell", 9.5, 150)   # 卖 150(非整手 但≥一手)
    check("卖出允许余股", o["status"] == "filled" and o["filled_qty"] == 150)

    # 12) 未达限价 → 保持挂单(open), 不是拒单
    o = broker.place_order("600519", "buy", 1290.0, 100)  # ref 1300 > limit 1290, 买未达
    check("未达限价保持挂单", o["status"] == "open")
    acc = broker.get_account()
    check("挂单冻结资金", acc["frozen"] >= 1290.0 * 100)

    # 13) 资金不足拒单(现有挂单冻结后更严格)
    o = broker.place_order("600519", "buy", 1300.0, 1000)  # 130万 > 可用
    check("资金不足拒单", o["status"] == "rejected")

    print("\n%d 项断言, %d 失败" % (13, len(fails)))
    if fails:
        raise SystemExit("FAIL: " + "; ".join(fails))
    print("ALL PAPER BROKER TESTS PASSED")


if __name__ == "__main__":
    _self_test()
