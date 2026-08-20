# -*- coding: utf-8 -*-
"""
ashare_trader.py — A股 自动交易系统主界面 (port 5000)
=====================================================
镜像 PolyAuto 的设计哲学: 单文件 Flask + 内嵌 HTML 主题仪表盘 +
会话 cookie 鉴权 + 后台交易循环 + 分层风控。

策略引擎(ashare_strategy_engine.py): 双低轮动 / 转债打新 / 红利低波 / 趋势跟随
仿真券商(ashare_broker.py): PaperBroker(默认) / QMTBroker(实盘, 可选)
数据层(ashare_data.py): akshare + SQLite 缓存 + 熔断降级
回测(ashare_backtest.py): 3 年分策略回测 + 高胜率门禁

PolyAuto 作为子界面: 头部链接按钮跳转 http://localhost:5001。
"""

import json
import logging
import os
import secrets
import sys
import threading
import time
from collections import deque
from datetime import date, datetime

from flask import Flask, Response, jsonify, request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
CONFIG_FILE = os.path.join(DATA_DIR, "ashare_config.json")
STATE_FILE = os.path.join(DATA_DIR, "ashare_state.json")
LOG_FILE = os.path.join(DATA_DIR, "ashare_trader.log")

os.makedirs(DATA_DIR, exist_ok=True)

import ashare_data as Data
from ashare_broker import PaperBroker, QMTBroker, PaperState, is_cb
from ashare_strategy_engine import StrategyEngine, size_position

VERSION = "1.0.0"

# ============================================================
#  Config
# ============================================================
DEFAULT_CONFIG = {
    "bankroll_cny": 100000,
    "trading_mode": "paper",          # paper | live
    "broker": "paper",                # paper | qmt
    "scan_interval": 300,             # 秒
    "max_positions": 20,
    "max_daily_trades": 10,
    "daily_loss_limit_pct": 0.10,
    "risk_level": 5,                  # 1-10
    "enforce_session": True,          # 仅交易时段下单
    "web_password": "",
    "polyauto_url": "http://localhost:5001",
    "strategy_cb_double_low": {
        "enabled": True, "max_price": 115, "max_premium": 40.0,
        "min_size": 2.0, "min_rating": "AA", "hold_n": 10,
        "rebalance_days": 7, "exit_price": 130,
    },
    "strategy_cb_new": {"enabled": True, "subscribe_limit": 100, "sell_first_day": True},
    "strategy_dividend": {
        "enabled": True, "index": "000300", "min_yield": 0.03,
        "hold_n": 10, "rebalance_days": 30,
    },
    "strategy_trend": {
        "enabled": False, "index": "000300", "hold_n": 5,
        "ma_short": 20, "ma_long": 60, "rsi_max": 70, "stop_loss_pct": 0.08,
    },
    "qmt": {"path": "D:/qmt", "account_id": ""},
    "fees": {
        "stock_commission_bps": 0.25, "stock_commission_min": 5.0,
        "stamp_bps": 5.0, "transfer_bps": 0.1,
        "cb_commission_bps": 0.2, "cb_commission_min_sh": 1.0, "cb_commission_min_sz": 0.0,
    },
}

_BOOL_KEYS = {
    "trading_mode_live": None,
}
_INT_KEYS = {"bankroll_cny", "scan_interval", "max_positions", "max_daily_trades",
             "risk_level"}
_SECRET_KEYS = {"web_password", "qmt", "fees"}


class ConfigStore:
    @classmethod
    def load(cls) -> dict:
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        if os.path.exists(CONFIG_FILE):
            try:
                data = json.loads(open(CONFIG_FILE, encoding="utf-8").read())
                for k in DEFAULT_CONFIG:
                    if k in data:
                        cfg[k] = data[k]
                # 嵌套策略配置 deep-merge
                for s in ("strategy_cb_double_low", "strategy_cb_new",
                          "strategy_dividend", "strategy_trend", "qmt", "fees"):
                    if isinstance(data.get(s), dict) and isinstance(cfg[s], dict):
                        cfg[s].update(data[s])
            except Exception as e:
                logging.getLogger("ashare").warning(f"配置读取失败: {e}")
        return cfg

    @classmethod
    def save(cls, cfg) -> bool:
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2, ensure_ascii=False)
            return True
        except Exception as e:
            logging.getLogger("ashare").error(f"配置保存失败: {e}")
            return False


def apply_risk_presets(cfg: dict, level: int) -> dict:
    """风险档位 → 覆盖 敞口/现金储备/单票 参数(由策略引擎 RISK_PRESETS 使用)。"""
    cfg["risk_level"] = max(1, min(10, int(level)))
    return cfg


# ============================================================
#  Logging (文件 + 环形缓冲)
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("ashare")
_log_buffer = deque(maxlen=300)


class BufferHandler(logging.Handler):
    _POLLING = ("/api/dashboard", "/api/status", "/api/logs", "/api/backtest",
                "/api/markets", "/api/scan")

    def emit(self, record):
        msg = record.getMessage()
        if record.name == "werkzeug" and ("GET " in msg or "POST " in msg):
            if any(p in msg for p in self._POLLING):
                return
        _log_buffer.append(self.format(record))


logging.getLogger().addHandler(BufferHandler())


# ============================================================
#  引擎状态 + 后台线程
# ============================================================
class EngineState:
    """运行状态(线程安全)。"""

    def __init__(self):
        self.lock = threading.RLock()
        self.running = False
        self.is_authorized = False
        self.current_action = "idle"
        self.last_error = ""
        self.last_scan = None
        self.cycle_count = 0
        self.start_time = None
        self.data_status = "未刷新"

    def get_run_seconds(self):
        if not self.start_time:
            return 0
        return int(time.time() - self.start_time)

    def update(self, **kw):
        with self.lock:
            for k, v in kw.items():
                setattr(self, k, v)


class BrokerBridge:
    """按配置构建券商(共享状态)。qmt 不可用时回退 paper。"""

    def __init__(self, cfg, state):
        self.cfg = cfg
        self.state = state
        self.broker = self._build()

    def _build(self):
        if str(self.cfg.get("broker", "paper")).lower() == "qmt":
            try:
                return QMTBroker(self.cfg)
            except RuntimeError as e:
                log.warning(f"QMT 不可用({e}), 回退仿真盘")
                self.cfg["broker"] = "paper"
        return PaperBroker(self.cfg, state=self.state)

    def is_live(self):
        """双保险: 仅 qmt AND live 才是实盘。"""
        return (str(self.cfg.get("broker")) == "qmt"
                and str(self.cfg.get("trading_mode")) == "live")


def build_engine(cfg, state):
    bridge = BrokerBridge(cfg, state)
    engine = StrategyEngine(cfg, state, bridge.broker)
    return bridge, engine


class TradingLoop(threading.Thread):
    """后台交易循环。"""

    def __init__(self, engine, bridge, est, cfg, state):
        super().__init__(daemon=True)
        self.engine = engine
        self.bridge = bridge
        self.est = est
        self.cfg = cfg
        self.state = state
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        while not self._stop.is_set():
            interval = int(self.cfg.get("scan_interval", 300))
            try:
                self._one_cycle()
            except Exception as e:
                self.est.update(last_error=str(e))
                log.error(f"扫描异常: {e}")
            self._stop.wait(max(interval, 15))

    def _one_cycle(self):
        est = self.est
        est.update(current_action="刷新数据")
        try:
            Data.refresh_daily(log=log.info)
            est.update(data_status="已刷新 " + datetime.now().strftime("%H:%M"))
        except Exception as e:
            log.warning(f"数据刷新失败(用缓存): {e}")

        est.update(current_action="扫描信号")
        self.engine._refresh_prices()
        acc = self.engine.tick()
        est.update(cycle_count=est.cycle_count + 1,
                   last_scan=datetime.now().isoformat(timespec="seconds"),
                   current_action="idle")
        # 更新账户快照
        self.state.update(last_account=acc)


# ============================================================
#  Flask App + 会话鉴权
# ============================================================
app = Flask(__name__)

_SESSIONS = {}
_SESSION_MAX_AGE = 12 * 3600
_SESSION_COOKIE = "ashareauth"
_PUBLIC_API = {"/api/setup", "/api/authorize", "/api/logout", "/api/network-check"}

# 全局运行时对象(授权时构建)
_engine = None
_loop = None
_bridge = None
_est = EngineState()
_state = None


def _new_session():
    token = secrets.token_urlsafe(32)
    _SESSIONS[token] = time.time()
    now = time.time()
    for tok, ts in list(_SESSIONS.items()):
        if now - ts > _SESSION_MAX_AGE:
            _SESSIONS.pop(tok, None)
    return token


def _valid_session(token):
    if not token:
        return False
    ts = _SESSIONS.get(token)
    if not ts:
        return False
    if time.time() - ts > _SESSION_MAX_AGE:
        _SESSIONS.pop(token, None)
        return False
    return True


@app.before_request
def _require_web_auth():
    cfg = ConfigStore.load()
    pw = cfg.get("web_password", "")
    if pw:
        auth = request.authorization
        if not (auth and auth.username == "admin" and auth.password == pw):
            return Response("AShareAuto 需要访问密码(请在设置中配置 web_password)",
                            401, {"WWW-Authenticate": 'Basic realm="AShareAuto"'})
    if request.path.startswith("/api/") and request.path not in _PUBLIC_API:
        token = request.cookies.get(_SESSION_COOKIE)
        if not _valid_session(token):
            return jsonify({"error": "unauthorized", "login_required": True}), 401
    return None


@app.route("/")
def index():
    return HTML_TEMPLATE


# ---- 授权 / 状态 --------------------------------------------------------
@app.route("/api/network-check")
def api_network_check():
    try:
        return jsonify({
            "success": True,
            "diagnostic": {
                "akshare_installed": Data._AK_IMPORTED,
                "degraded": Data.is_network_degraded(),
                "cache_db": Data.CACHE_DB,
                "data_status": _est.data_status,
                "proxy": {"configured": bool(os.environ.get("HTTP_PROXY") or os.environ.get("HTTPS_PROXY"))},
            },
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/status")
def api_status():
    cfg = ConfigStore.load()
    return jsonify({
        "is_authorized": _est.is_authorized,
        "is_running": _loop is not None and _loop.is_alive() if _loop else False,
        "current_action": _est.current_action,
        "cycle_count": _est.cycle_count,
        "last_scan": _est.last_scan,
        "last_error": _est.last_error,
        "run_seconds": _est.get_run_seconds(),
        "scan_interval": cfg.get("scan_interval", 300),
        "mode": cfg.get("trading_mode", "paper") if _est.is_authorized else "not_authorized",
        "data_status": _est.data_status,
        "version": VERSION,
    })


@app.route("/api/authorize", methods=["POST"])
def api_authorize():
    data = request.json or {}
    password = data.get("password", "")
    cfg = ConfigStore.load()
    pw = cfg.get("web_password", "")
    if pw and password != pw:
        return jsonify({"success": False, "error": "密码错误"}), 401

    global _engine, _loop, _bridge, _state
    # 重建引擎(应用最新配置)
    state = PaperState(STATE_FILE, seed_cash=cfg.get("bankroll_cny", 100000))
    _state = state
    bridge, engine = build_engine(cfg, state)
    _bridge, _engine = bridge, engine
    _est.update(is_authorized=True, start_time=time.time())

    # 若此前已运行则重启
    if _loop and _loop.is_alive():
        _loop.stop()
    _loop = TradingLoop(engine, bridge, _est, cfg, state)
    _loop.start()

    token = _new_session()
    resp = jsonify({
        "success": True,
        "mode": cfg.get("trading_mode"),
        "broker": cfg.get("broker"),
        "is_live": bridge.is_live(),
        "message": "已授权并启动模拟交易" if not bridge.is_live() else "已授权并连接实盘通道",
    })
    resp.set_cookie(_SESSION_COOKIE, token, httponly=True, samesite="Lax",
                    max_age=12 * 3600)
    log.info(f"授权启动: 模式={cfg.get('trading_mode')} 券商={cfg.get('broker')}")
    return resp


@app.route("/api/logout", methods=["POST"])
def api_logout():
    token = request.cookies.get(_SESSION_COOKIE)
    if token and token in _SESSIONS:
        _SESSIONS.pop(token, None)
    if _loop:
        _loop.stop()
    _est.update(is_authorized=False)
    resp = jsonify({"success": True})
    resp.delete_cookie(_SESSION_COOKIE)
    return resp


@app.route("/api/start-stop", methods=["POST"])
def api_start_stop():
    global _loop
    data = request.json or {}
    want = data.get("running")
    if not _est.is_authorized or _engine is None:
        return jsonify({"success": False, "error": "请先授权启动"}), 400
    if want is False:
        if _loop:
            _loop.stop()
        return jsonify({"success": True, "running": False})
    if want is True:
        if not (_loop and _loop.is_alive()):
            _loop = TradingLoop(_engine, _bridge, _est, ConfigStore.load(), _state)
            _loop.start()
        return jsonify({"success": True, "running": True})
    return jsonify({"success": False, "error": "need running field"}), 400


@app.route("/api/refresh-data", methods=["POST"])
def api_refresh_data():
    if not _est.is_authorized:
        return jsonify({"success": False, "error": "请先授权启动"}), 400
    def job():
        try:
            Data.refresh_daily(log=log.info)
            _est.update(data_status="已刷新 " + datetime.now().strftime("%H:%M"))
        except Exception as e:
            log.error(f"手动刷新失败: {e}")
    threading.Thread(target=job, daemon=True).start()
    return jsonify({"success": True, "message": "数据刷新已启动"})


# ---- 配置 ----------------------------------------------------------------
@app.route("/api/setup", methods=["POST"])
def api_setup():
    data = request.json or {}
    cfg = ConfigStore.load()
    for k in ("bankroll_cny", "scan_interval", "max_positions", "max_daily_trades",
              "daily_loss_limit_pct", "risk_level", "web_password", "polyauto_url",
              "enforce_session"):
        if k in data:
            cfg[k] = data[k]
    for k in ("trading_mode", "broker"):
        if k in data:
            cfg[k] = str(data[k]).lower()
    # 嵌套策略
    for s in ("strategy_cb_double_low", "strategy_cb_new", "strategy_dividend", "strategy_trend"):
        if isinstance(data.get(s), dict):
            cfg[s].update(data[s])
    for f in ("stock_commission_bps", "stock_commission_min", "stamp_bps",
              "transfer_bps", "cb_commission_bps", "cb_commission_min_sh", "cb_commission_min_sz"):
        if f in data:
            cfg["fees"][f] = float(data[f])
    apply_risk_presets(cfg, int(cfg.get("risk_level", 5)))
    ok = ConfigStore.save(cfg)
    log.info(f"配置已保存 (risk={cfg['risk_level']}, mode={cfg['trading_mode']})")
    return jsonify({"success": ok})


@app.route("/api/config")
def api_config():
    if not _est.is_authorized:
        return jsonify({"success": False, "error": "unauthorized"}), 401
    return jsonify({"success": True, "config": ConfigStore.load()})


# ---- 仪表盘 --------------------------------------------------------------
@app.route("/api/dashboard")
def api_dashboard():
    if _engine is None or not _est.is_authorized:
        return jsonify({"error": "unauthorized", "login_required": True}), 401
    cfg = ConfigStore.load()
    broker = _bridge.broker
    acc = broker.get_account()
    positions = broker.get_positions()
    opps = _engine.generate_opportunities()
    orders = broker.get_orders()[:50]
    perf = _engine.perf.stats()
    state = _state

    # 统计
    stats = {
        "run_seconds": _est.get_run_seconds(),
        "cycle_count": _est.cycle_count,
        "last_scan": _est.last_scan,
        "total_asset": acc.get("total_asset", 0),
        "cash": acc.get("cash", 0) - acc.get("frozen", 0),
        "frozen": acc.get("frozen", 0),
        "market_value": acc.get("market_value", 0),
        "invested_pct": round(acc.get("market_value", 0) / max(acc.get("total_asset", 1), 1) * 100, 1),
        "positions": len(positions),
        "max_positions": cfg.get("max_positions", 20),
        "realized_pnl": state.get("realized_pnl", 0.0),
        "total_fees": state.get("total_fees", 0.0),
        "today_pnl": float(state.get("daily_pnl", {}).get(date.today().isoformat(), 0.0) or 0.0),
        "consecutive_losses": state.get("consecutive_losses", 0),
        "mode": cfg.get("trading_mode"),
        "broker": cfg.get("broker"),
        "is_live": _bridge.is_live(),
        "data_status": _est.data_status,
    }
    # 胜率(全策略)
    total_trades = sum(p.get("trades", 0) for p in perf.values())
    total_wins = sum(p.get("wins", 0) for p in perf.values())
    stats["win_rate"] = round(total_wins / total_trades * 100, 1) if total_trades else 0

    return jsonify({
        "success": True,
        "stats": stats,
        "positions": positions,
        "opportunities": opps,
        "orders": orders,
        "perf": perf,
        "equity_curve": state.get("equity_curve", []),
        "config": cfg,
        "log": list(_log_buffer)[-60:],
    })


@app.route("/api/markets")
def api_markets():
    """市场浏览: 转债榜 + 股票榜(股息率)。"""
    if _engine is None or not _est.is_authorized:
        return jsonify({"error": "unauthorized", "login_required": True}), 401
    uni = Data.cb_doublelow_universe()
    cb_list = []
    if uni is not None and len(uni):
        u = uni.sort_values("dlow").head(80)
        for _, r in u.iterrows():
            cb_list.append({
                "symbol": r["symbol"], "name": r.get("name"),
                "price": round(float(r.get("price_eff") or r.get("price") or 0), 2),
                "premium": round(float(r.get("premium") or 0), 1),
                "dlow": round(float(r.get("dlow") or 0), 1),
                "rating": r.get("rating"),
                "size": r.get("issue_size"),
                "stock": r.get("stock_name"),
            })
    scan = Data.get_dividend_scan()
    stocks = []
    if scan is not None and len(scan):
        for _, r in scan.head(40).iterrows():
            stocks.append({"symbol": r["symbol"], "name": r.get("name"),
                           "yield": round(float(r.get("yield") or 0) * 100, 2)})
    return jsonify({"success": True, "cb": cb_list, "stocks": stocks})


@app.route("/api/backtest")
def api_backtest():
    if not _est.is_authorized:
        return jsonify({"error": "unauthorized", "login_required": True}), 401
    report = __import__("ashare_backtest").load_report()
    return jsonify({"success": True, "report": report,
                    "generating": __import__("ashare_backtest").backtest_busy()})


@app.route("/api/generate-backtest", methods=["POST"])
def api_generate_backtest():
    if not _est.is_authorized:
        return jsonify({"error": "unauthorized", "login_required": True}), 401
    import ashare_backtest as B
    data = request.json or {}
    strategy = data.get("strategy", "cb_double_low")
    years = int(data.get("years", 3))

    def job():
        if B._LOCK.locked():
            return
        with B._LOCK:
            log.info(f"回测启动: {strategy} {years} 年(首次需拉历史数据)")
            if strategy == "cb_double_low":
                m = B.backtest_double_low(years, ConfigStore.load().get("bankroll_cny", 100000),
                                          progress=lambda d, t, c: None, log=log.info)
            elif strategy == "dividend":
                m = B.backtest_dividend(years, ConfigStore.load().get("bankroll_cny", 100000),
                                        progress=lambda d, t, c: None, log=log.info)
            else:
                m = {"error": "trend 回测暂未实现(默认关)"}
            gate = B.check_gate(strategy, m)
            report = B.load_report()
            report[strategy] = m
            report["gate_" + strategy] = gate
            report["generated_at"] = datetime.now().isoformat(timespec="seconds")
            report["last_strategy"] = strategy
            B.save_report(report)
            log.info(f"回测完成: {strategy} -> " + (gate.get("reason", "ok")))
    threading.Thread(target=job, daemon=True).start()
    return jsonify({"success": True, "message": f"{strategy} 回测已启动(后台生成)"})


# ---- 手动交易 -------------------------------------------------------------
@app.route("/api/sell", methods=["POST"])
def api_sell():
    if _engine is None or not _est.is_authorized:
        return jsonify({"error": "unauthorized", "login_required": True}), 401
    data = request.json or {}
    symbol = str(data.get("symbol", "")).strip()
    pos = {p["symbol"]: p for p in _bridge.broker.get_positions()}.get(symbol)
    if not pos:
        return jsonify({"success": False, "error": "无此持仓"}), 400
    last = _engine._last.get(symbol) or pos.get("last_price") or pos.get("avg_cost")
    o = _bridge.broker.place_order(symbol, "sell", last, pos["qty"], order_type="market",
                                   strategy=pos.get("strategy", ""), name=pos.get("name", ""),
                                   ref_price=last, prev_close=_engine._prev.get(symbol))
    if o.get("status") == "filled":
        pnl = (o.get("avg_price", 0) - pos.get("avg_cost", 0)) * o.get("filled_qty", 0) - o.get("fee", 0)
        o["pnl"] = pnl
        _engine.perf.record_exit(pos.get("strategy", ""), o)
        log.info(f"手动卖出 {symbol} 数量={o['filled_qty']} 盈亏={pnl:.2f}")
        return jsonify({"success": True, "order": o, "pnl": round(pnl, 2)})
    return jsonify({"success": False, "error": o.get("reject_reason", "下单失败"), "order": o}), 400


@app.route("/api/logs")
def api_logs():
    return jsonify({"success": True, "log": list(_log_buffer)[-100:]})


# ============================================================
#  HTML_TEMPLATE
# ============================================================
HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AShareAuto · A股自动交易系统</title>
<style>
/* ===== Theme System (契约与 PolyAuto 一致) ===== */
:root {
  --bg: #14181f; --bg2: #1b2028; --card: #212732; --card-hover: #262d38;
  --border: #333b47; --border-light: #262d37;
  --text: #d7dee8; --text-secondary: #aeb6c2; --muted: #7d8794;
  --primary: #2f81f7; --primary-d: #1f6feb; --primary-l: rgba(47,129,247,0.12);
  --green: #3fb950; --green-l: rgba(63,185,80,0.12);
  --red: #f85149; --red-l: rgba(248,81,73,0.12);
  --orange: #d29922; --orange-l: rgba(210,153,34,0.12);
  --purple: #a371f7; --purple-l: rgba(163,113,247,0.12);
  --teal: #2dd4bf; --teal-l: rgba(45,212,191,0.12);
  --radius: 12px; --radius-sm: 8px;
  --shadow: 0 2px 12px rgba(0,0,0,0.3); --shadow-lg: 0 8px 30px rgba(0,0,0,0.4);
  --log-bg: #0d1117; --modal-overlay: rgba(0,0,0,0.7);
  --gradient-header: linear-gradient(135deg, #161b22 0%, #0d1117 100%);
}
[data-theme="light"] {
  --bg: #eef0f3; --bg2: #f8f9fb; --card: #f8f9fb; --card-hover: #eef1f4;
  --border: #d4dae1; --border-light: #e3e7ec;
  --text: #262c34; --text-secondary: #4a525d; --muted: #6d7682;
  --primary: #0969da; --primary-d: #0550ae; --primary-l: rgba(9,105,218,0.08);
  --green: #1a7f37; --green-l: rgba(26,127,55,0.1);
  --red: #cf222e; --red-l: rgba(207,34,46,0.08);
  --orange: #9a6700; --orange-l: rgba(154,103,0,0.1);
  --purple: #8250df; --purple-l: rgba(130,80,223,0.1);
  --teal: #0d7d6c; --teal-l: rgba(13,125,108,0.1);
  --shadow: 0 1px 6px rgba(0,0,0,0.08); --shadow-lg: 0 8px 24px rgba(0,0,0,0.12);
  --log-bg: #f6f8fa; --modal-overlay: rgba(0,0,0,0.4);
  --gradient-header: linear-gradient(135deg, #ffffff 0%, #f6f8fa 100%);
}
[data-theme="hermes"] {
  --bg: #0a0f18; --bg2: #0d1420; --card: #111a28; --card-hover: #16222f;
  --border: #1f2e44; --border-light: #182536;
  --text: #d7e2f0; --text-secondary: #a6b6cc; --muted: #64778f;
  --primary: #22d3ee; --primary-d: #0ea5e9; --primary-l: rgba(34,211,238,0.12);
  --green: #34d399; --green-l: rgba(52,211,153,0.12);
  --red: #f87171; --red-l: rgba(248,113,113,0.12);
  --orange: #fbbf24; --orange-l: rgba(251,191,36,0.12);
  --purple: #a78bfa; --purple-l: rgba(167,139,250,0.12);
  --teal: #2dd4bf; --teal-l: rgba(45,212,191,0.12);
  --shadow: 0 2px 12px rgba(0,0,0,0.5); --shadow-lg: 0 8px 30px rgba(0,0,0,0.6);
  --log-bg: #070b12; --modal-overlay: rgba(4,7,12,0.8);
  --gradient-header: linear-gradient(135deg, #0d1420 0%, #0a0f18 100%);
}
[data-theme="hermes"] body {
  background-image:
    linear-gradient(rgba(34,211,238,0.03) 1px, transparent 1px),
    linear-gradient(90deg, rgba(34,211,238,0.03) 1px, transparent 1px);
  background-size: 32px 32px;
}
@media (prefers-color-scheme: light) {
  :root:not([data-theme="dark"]):not([data-theme="light"]) {
    --bg: #eef0f3; --bg2: #f8f9fb; --card: #f8f9fb; --card-hover: #eef1f4;
    --border: #d4dae1; --border-light: #e3e7ec;
    --text: #262c34; --text-secondary: #4a525d; --muted: #6d7682;
    --primary: #0969da; --primary-d: #0550ae; --primary-l: rgba(9,105,218,0.08);
    --green: #1a7f37; --green-l: rgba(26,127,55,0.1);
    --red: #cf222e; --red-l: rgba(207,34,46,0.08);
    --orange: #9a6700; --orange-l: rgba(154,103,0,0.1);
    --purple: #8250df; --purple-l: rgba(130,80,223,0.1);
    --teal: #0d7d6c; --teal-l: rgba(13,125,108,0.1);
    --shadow: 0 1px 6px rgba(0,0,0,0.08); --shadow-lg: 0 8px 24px rgba(0,0,0,0.12);
    --log-bg: #f6f8fa; --modal-overlay: rgba(0,0,0,0.4);
  }
}

* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  background: var(--bg); color: var(--text);
  font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
  font-size: 14px; line-height: 1.5;
  transition: background .2s, color .2s;
}
a { color: var(--primary); text-decoration: none; }
button { font-family: inherit; cursor: pointer; }

/* ===== Header ===== */
.hdr {
  display: flex; align-items: center; gap: 12px;
  padding: 10px 18px; border-bottom: 1px solid var(--border);
  background: var(--gradient-header); position: sticky; top: 0; z-index: 50;
}
.brand { font-size: 19px; font-weight: 700; letter-spacing: .3px; display: flex; align-items: center; gap: 8px; }
.brand span { color: var(--primary); }
.brand .flag { font-size: 18px; }
.hdr-spacer { flex: 1; }
.badge { font-size: 12px; font-weight: 600; padding: 3px 10px; border-radius: 999px; border: 1px solid var(--border); }
.badge.live { background: var(--green-l); color: var(--green); border-color: var(--green); }
.badge.dry { background: var(--orange-l); color: var(--orange); border-color: var(--orange); }
.badge.off { background: var(--red-l); color: var(--red); border-color: var(--red); }
.theme-btn {
  background: var(--card); border: 1px solid var(--border); color: var(--text-secondary);
  width: 30px; height: 30px; border-radius: 8px; font-size: 13px;
}
.theme-btn.active { border-color: var(--primary); color: var(--primary); background: var(--primary-l); }
.hdr-btn {
  background: var(--card); border: 1px solid var(--border); color: var(--text);
  padding: 6px 13px; border-radius: 8px; font-size: 13px; transition: .15s;
}
.hdr-btn:hover { border-color: var(--primary); color: var(--primary); }
.hdr-btn.danger { border-color: var(--red); color: var(--red); }
.hdr-btn.danger:hover { background: var(--red-l); }
.hdr-btn.primary { background: var(--primary); border-color: var(--primary); color: #fff; }
.hdr-btn.primary:hover { background: var(--primary-d); }
.link-btn {
  display: inline-flex; align-items: center; gap: 4px;
  background: var(--purple-l); border: 1px solid var(--purple); color: var(--purple);
  padding: 6px 13px; border-radius: 8px; font-size: 13px; font-weight: 600;
}
.link-btn:hover { background: var(--purple); color: #fff; }

/* ===== Stats ===== */
.stats {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 10px; padding: 14px 18px 6px;
}
.stat { background: var(--card); border: 1px solid var(--border); border-radius: var(--radius); padding: 10px 14px; }
.stat .label { font-size: 11px; color: var(--muted); margin-bottom: 3px; }
.stat .value { font-size: 18px; font-weight: 700; font-variant-numeric: tabular-nums; }
.stat .sub { font-size: 11px; color: var(--muted); margin-top: 2px; }
.pos { color: var(--green); } .neg { color: var(--red); }

/* ===== Tabs ===== */
.tabs { display: flex; gap: 4px; padding: 12px 18px 0; flex-wrap: wrap; }
.tab {
  background: transparent; border: none; color: var(--muted); font-size: 13px;
  padding: 7px 14px; border-radius: 8px 8px 0 0; border-bottom: 2px solid transparent;
}
.tab.active { color: var(--primary); border-bottom-color: var(--primary); font-weight: 600; }
.tab:hover { color: var(--text); }

.panel { display: none; padding: 14px 18px 20px; }
.panel.active { display: block; }

/* ===== Cards / Tables ===== */
.card {
  background: var(--card); border: 1px solid var(--border); border-radius: var(--radius);
  padding: 14px 16px; margin-bottom: 12px;
}
.section-title { font-size: 14px; font-weight: 700; margin-bottom: 10px; display: flex; align-items: center; gap: 8px; }
.section-title .cnt { font-size: 12px; color: var(--muted); font-weight: 400; }
.table { width: 100%; border-collapse: collapse; font-size: 13px; }
.table th {
  text-align: left; color: var(--muted); font-weight: 500; font-size: 11px;
  padding: 6px 8px; border-bottom: 1px solid var(--border); white-space: nowrap;
}
.table td { padding: 7px 8px; border-bottom: 1px solid var(--border-light); white-space: nowrap; }
.table tr:hover td { background: var(--card-hover); }
.status-pill { font-size: 11px; padding: 2px 8px; border-radius: 999px; }
.status-pill.filled { background: var(--green-l); color: var(--green); }
.status-pill.open { background: var(--orange-l); color: var(--orange); }
.status-pill.rejected { background: var(--red-l); color: var(--red); }
.status-pill.cancelled { background: var(--muted); color: var(--text); }
.score-bar {
  display: inline-block; width: 56px; height: 5px; border-radius: 3px;
  background: var(--border); vertical-align: middle; margin-left: 6px; position: relative;
}
.score-bar i { position: absolute; left: 0; top: 0; bottom: 0; border-radius: 3px; background: var(--primary); }
.small-btn {
  background: var(--card); border: 1px solid var(--border); color: var(--text-secondary);
  padding: 3px 9px; border-radius: 6px; font-size: 12px;
}
.small-btn:hover { border-color: var(--primary); color: var(--primary); }
.small-btn.danger:hover { border-color: var(--red); color: var(--red); }

/* ===== Log ===== */
.log-box {
  background: var(--log-bg); border: 1px solid var(--border); border-radius: var(--radius-sm);
  padding: 12px; font-family: "Cascadia Code", Consolas, monospace; font-size: 12px;
  max-height: 420px; overflow-y: auto; line-height: 1.7;
}
.log-line { color: var(--text-secondary); white-space: pre-wrap; word-break: break-all; }
.log-line.err { color: var(--red); }

/* ===== Modal ===== */
.modal-overlay {
  position: fixed; inset: 0; background: var(--modal-overlay); z-index: 100;
  display: none; align-items: center; justify-content: center; padding: 20px;
}
.modal-overlay.show { display: flex; }
.modal {
  background: var(--bg2); border: 1px solid var(--border); border-radius: var(--radius);
  width: 100%; max-width: 720px; max-height: 88vh; overflow-y: auto;
  box-shadow: var(--shadow-lg); animation: slideUp .18s ease;
}
@keyframes slideUp { from { transform: translateY(14px); opacity: 0; } to { transform: none; opacity: 1; } }
.modal-head { display: flex; align-items: center; justify-content: space-between; padding: 16px 20px; border-bottom: 1px solid var(--border); }
.modal-head h3 { font-size: 16px; }
.modal-close { background: none; border: none; color: var(--muted); font-size: 20px; line-height: 1; }
.modal-close:hover { color: var(--text); }
.modal-body { padding: 18px 20px; }
.modal-actions { display: flex; gap: 10px; justify-content: flex-end; padding: 14px 20px; border-top: 1px solid var(--border); }

/* ===== Setting sections ===== */
.setting-section { margin-bottom: 18px; }
.setting-section-title {
  font-size: 12px; font-weight: 700; color: var(--primary); text-transform: uppercase;
  letter-spacing: .5px; margin-bottom: 10px; display: flex; align-items: center; gap: 6px;
}
.field-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 12px; }
.field { display: flex; flex-direction: column; gap: 4px; }
.field label { font-size: 12px; color: var(--muted); }
.field input, .field select {
  background: var(--card); border: 1px solid var(--border); color: var(--text);
  border-radius: var(--radius-sm); padding: 8px 10px; font-size: 13px;
}
.field input:focus, .field select:focus { outline: none; border-color: var(--primary); }
.toggle-item {
  display: flex; align-items: center; justify-content: space-between;
  background: var(--card); border: 1px solid var(--border); border-radius: var(--radius-sm);
  padding: 10px 14px;
}
.toggle-item .t-label { font-size: 13px; font-weight: 500; }
.toggle-item .t-desc { font-size: 11px; color: var(--muted); margin-top: 2px; }
.pill {
  min-width: 52px; text-align: center; font-size: 12px; font-weight: 600;
  padding: 4px 12px; border-radius: 999px; border: 1px solid var(--border); background: var(--card);
  color: var(--muted); cursor: pointer;
}
.pill.on { background: var(--green); border-color: var(--green); color: #fff; }
.pill.off { background: var(--red); border-color: var(--red); color: #fff; }
.range-slider { width: 100%; accent-color: var(--primary); }
.risk-desc { font-size: 11px; color: var(--muted); }

/* ===== Login / gate ===== */
.gate {
  min-height: 100vh; display: flex; align-items: center; justify-content: center;
  background: var(--bg); padding: 20px;
}
.gate-card {
  background: var(--card); border: 1px solid var(--border); border-radius: 16px;
  padding: 40px 44px; max-width: 420px; width: 100%; text-align: center;
  box-shadow: var(--shadow-lg);
}
.gate-card .logo { font-size: 30px; margin-bottom: 8px; }
.gate-card h1 { font-size: 22px; margin-bottom: 6px; }
.gate-card h1 span { color: var(--primary); }
.gate-card .sub { color: var(--muted); font-size: 13px; margin-bottom: 24px; }
.gate-card input {
  width: 100%; background: var(--bg2); border: 1px solid var(--border); color: var(--text);
  border-radius: 10px; padding: 12px 14px; font-size: 14px; margin-bottom: 12px;
}
.gate-card input:focus { outline: none; border-color: var(--primary); }
.gate-card .btn-primary {
  width: 100%; background: var(--primary); border: none; color: #fff;
  padding: 12px; border-radius: 10px; font-size: 15px; font-weight: 600;
}
.gate-card .btn-primary:hover { background: var(--primary-d); }
.gate-card .hint { font-size: 11px; color: var(--muted); margin-top: 14px; line-height: 1.6; }

/* ===== Misc ===== */
.empty { color: var(--muted); text-align: center; padding: 30px; font-size: 13px; }
.spinner {
  display: inline-block; width: 14px; height: 14px; border: 2px solid var(--border);
  border-top-color: var(--primary); border-radius: 50%; animation: spin .8s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }
.toast {
  position: fixed; bottom: 24px; left: 50%; transform: translateX(-50%);
  background: var(--card); border: 1px solid var(--primary); color: var(--text);
  padding: 10px 18px; border-radius: 10px; font-size: 13px; box-shadow: var(--shadow-lg);
  z-index: 200; opacity: 0; transition: opacity .2s; pointer-events: none;
}
.toast.show { opacity: 1; }
.net-dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
.net-dot.ok { background: var(--green); } .net-dot.bad { background: var(--red); } .net-dot.warn { background: var(--orange); }
.footer {
  text-align: center; color: var(--muted); font-size: 11px; padding: 18px; border-top: 1px solid var(--border-light);
}
.footer .footer-brand { font-weight: 700; }
.mt8 { margin-top: 8px; } .mt16 { margin-top: 16px; }
.grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
@media (max-width: 760px) { .grid-2 { grid-template-columns: 1fr; } }
canvas { width: 100%; height: 280px; display: block; }
.legend { font-size: 11px; color: var(--muted); display: flex; gap: 16px; flex-wrap: wrap; }
</style>
</head>
<body>

<!-- ===== Header ===== -->
<div class="hdr">
  <div class="brand"><span class="flag">📈</span> AShare<span>Auto</span></div>
  <span class="badge off" id="modeBadge">未授权</span>
  <span class="hdr-spacer"></span>
  <span class="net-dot warn" id="netDot" title="数据源状态"></span>
  <a class="link-btn" href="http://localhost:5001" target="_blank" id="polyLink">Polymarket 机器人 ↗</a>
  <button class="theme-btn" onclick="setTheme('light')" title="亮色" data-theme-btn="light">☀</button>
  <button class="theme-btn" onclick="setTheme('dark')" title="暗色" data-theme-btn="dark">☾</button>
  <button class="theme-btn" onclick="setTheme('hermes')" title="Hermes AI 终端" data-theme-btn="hermes">⚡</button>
  <button class="theme-btn active" onclick="setTheme('auto')" title="跟随系统" data-theme-btn="auto">⚙</button>
  <button class="hdr-btn" onclick="openSettings()">⚙️ 设置</button>
  <button class="hdr-btn" onclick="forceRefresh()">🔄 刷新数据</button>
  <button class="hdr-btn primary" onclick="openAuthorize()">🚀 授权启动</button>
  <button class="hdr-btn danger" onclick="stopAll()">⏹ 停止</button>
</div>

<!-- ===== Stats ===== -->
<div class="stats" id="statsRow">
  <div class="stat"><div class="label">运行时间</div><div class="value" id="stRun">--</div></div>
  <div class="stat"><div class="label">下次扫描</div><div class="value" id="stNext">--</div></div>
  <div class="stat"><div class="label">总资产</div><div class="value" id="stAsset">--</div></div>
  <div class="stat"><div class="label">已投入</div><div class="value" id="stInvested">--</div></div>
  <div class="stat"><div class="label">可用现金</div><div class="value" id="stCash">--</div></div>
  <div class="stat"><div class="label">持仓数</div><div class="value" id="stPos">--</div></div>
  <div class="stat"><div class="label">今日盈亏</div><div class="value" id="stToday">--</div></div>
  <div class="stat"><div class="label">累计盈亏</div><div class="value" id="stPnl">--</div></div>
  <div class="stat"><div class="label">胜率</div><div class="value" id="stWinrate">--</div></div>
</div>

<!-- ===== Tabs ===== -->
<div class="tabs" id="tabs">
  <button class="tab active" data-tab="opps">🎯 交易机会</button>
  <button class="tab" data-tab="positions">📦 持仓</button>
  <button class="tab" data-tab="equity">📈 资金曲线</button>
  <button class="tab" data-tab="perf">🧩 策略表现</button>
  <button class="tab" data-tab="backtest">🔬 回测报告</button>
  <button class="tab" data-tab="markets">🌐 市场浏览</button>
  <button class="tab" data-tab="log">📜 日志</button>
</div>

<div class="panel active" id="panel-opps">
  <div class="card">
    <div class="section-title">🎯 交易机会 <span class="cnt" id="oppCnt"></span></div>
    <div class="table-wrap" style="overflow-x:auto"><table class="table" id="oppTable">
      <thead><tr><th>策略</th><th>代码</th><th>名称</th><th>价格</th><th>关键指标</th><th>评分</th></tr></thead>
      <tbody id="oppBody"></tbody>
    </table></div>
  </div>
</div>

<div class="panel" id="panel-positions">
  <div class="card">
    <div class="section-title">📦 当前持仓 <span class="cnt" id="posCnt"></span></div>
    <div class="table-wrap" style="overflow-x:auto"><table class="table" id="posTable">
      <thead><tr><th>代码</th><th>名称</th><th>策略</th><th>数量</th><th>成本</th><th>现价</th><th>市值</th><th>浮动盈亏</th><th>操作</th></tr></thead>
      <tbody id="posBody"></tbody>
    </table></div>
  </div>
</div>

<div class="panel" id="panel-equity">
  <div class="card">
    <div class="section-title">📈 资金曲线</div>
    <div class="legend"><span>总资产走势(模拟盘成交价口径)</span></div>
    <canvas id="equityChart"></canvas>
  </div>
  <div class="card">
    <div class="section-title">📋 最近订单</div>
    <div class="table-wrap" style="overflow-x:auto"><table class="table" id="orderTable">
      <thead><tr><th>时间</th><th>代码</th><th>方向</th><th>数量</th><th>价格</th><th>状态</th><th>策略</th></tr></thead>
      <tbody id="orderBody"></tbody>
    </table></div>
  </div>
</div>

<div class="panel" id="panel-perf">
  <div class="card">
    <div class="section-title">🧩 策略表现</div>
    <div class="table-wrap" style="overflow-x:auto"><table class="table" id="perfTable">
      <thead><tr><th>策略</th><th>交易数</th><th>胜</th><th>负</th><th>胜率</th><th>盈亏</th><th>期望/笔</th><th>盈亏因子</th></tr></thead>
      <tbody id="perfBody"></tbody>
    </table></div>
  </div>
</div>

<div class="panel" id="panel-backtest">
  <div class="card">
    <div class="section-title">🔬 回测报告 <span class="cnt" id="btMeta"></span></div>
    <div style="display:flex; gap:8px; margin-bottom:12px; flex-wrap:wrap;">
      <button class="hdr-btn" onclick="genBacktest('cb_double_low')">生成双低回测</button>
      <button class="hdr-btn" onclick="genBacktest('dividend')">生成红利回测</button>
    </div>
    <div id="btBody"><div class="empty">尚未生成回测报告。点击上方按钮开始(首次需数分钟拉取历史数据)。</div></div>
  </div>
</div>

<div class="panel" id="panel-markets">
  <div class="card">
    <div class="section-title">🌐 可转债榜(双低值升序) <span class="cnt" id="mktCbCnt"></span></div>
    <div class="table-wrap" style="overflow-x:auto"><table class="table" id="mktCbTable">
      <thead><tr><th>代码</th><th>名称</th><th>价格</th><th>溢价率%</th><th>双低值</th><th>评级</th><th>规模(亿)</th><th>正股</th></tr></thead>
      <tbody id="mktCbBody"></tbody>
    </table></div>
  </div>
  <div class="card">
    <div class="section-title">🦴 红利股榜(股息率) <span class="cnt" id="mktStockCnt"></span></div>
    <div class="table-wrap" style="overflow-x:auto"><table class="table" id="mktStockTable">
      <thead><tr><th>代码</th><th>名称</th><th>股息率%</th></tr></thead>
      <tbody id="mktStockBody"></tbody>
    </table></div>
  </div>
</div>

<div class="panel" id="panel-log">
  <div class="card">
    <div class="section-title">📜 运行日志</div>
    <div class="log-box" id="logBox"></div>
  </div>
</div>

<div class="footer">
  AShareAuto · A股自动交易系统 · © 2026 Gavin · 谨慎交易, 风险自负
  · v<span id="versionTag" class="footer-brand">1.0.0</span>
</div>

<!-- ===== Settings Modal ===== -->
<div class="modal-overlay" id="settingsModal">
  <div class="modal">
    <div class="modal-head"><h3>⚙️ 系统设置</h3><button class="modal-close" onclick="closeSettings()">×</button></div>
    <div class="modal-body" id="settingsBody"></div>
    <div class="modal-actions">
      <button class="hdr-btn" onclick="closeSettings()">取消</button>
      <button class="hdr-btn primary" onclick="saveSettings()">💾 保存配置</button>
    </div>
  </div>
</div>

<!-- ===== Authorize Modal ===== -->
<div class="modal-overlay" id="authorizeModal">
  <div class="modal" style="max-width:420px">
    <div class="modal-head"><h3>🚀 授权并启动</h3><button class="modal-close" onclick="closeAuthorize()">×</button></div>
    <div class="modal-body">
      <p style="color:var(--muted); font-size:13px; margin-bottom:14px;">
        启动后系统将按策略自动扫描并<b style="color:var(--orange)">模拟盘</b>交易。实盘需在设置中切换到 QMT 通道并确认。
      </p>
      <div class="field" style="margin-bottom:10px">
        <label>访问密码(若已设置)</label>
        <input type="password" id="authPw" placeholder="留空则不校验">
      </div>
    </div>
    <div class="modal-actions">
      <button class="hdr-btn" onclick="closeAuthorize()">取消</button>
      <button class="hdr-btn primary" onclick="doAuthorize()">🚀 授权启动</button>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
// ===== Helpers =====
const $ = id => document.getElementById(id);
let dashTimer = null;
let dashData = null;

function fmt(n, d=2) {
  if (n === null || n === undefined || isNaN(n)) return '--';
  return Number(n).toLocaleString('zh-CN', {minimumFractionDigits: d, maximumFractionDigits: d});
}
function fmtMoney(n) { return '¥' + fmt(n, 0); }
function pnlClass(v) { return v > 0 ? 'pos' : (v < 0 ? 'neg' : ''); }
function esc(s) {
  return String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
function showToast(msg, ms=2600) {
  const t = $('toast'); t.textContent = msg; t.classList.add('show');
  clearTimeout(showToast._t); showToast._t = setTimeout(() => t.classList.remove('show'), ms);
}

// ===== Theme =====
function setTheme(theme) {
  localStorage.setItem('ashare-theme', theme);
  applyTheme(theme);
  document.querySelectorAll('[data-theme-btn]').forEach(b => b.classList.toggle('active', b.dataset.themeBtn === theme));
}
function applyTheme(theme) {
  if (theme === 'light') document.documentElement.setAttribute('data-theme','light');
  else if (theme === 'dark') document.documentElement.setAttribute('data-theme','dark');
  else if (theme === 'hermes') document.documentElement.setAttribute('data-theme','hermes');
  else document.documentElement.removeAttribute('data-theme');
}
(function() {
  const saved = localStorage.getItem('ashare-theme') || 'auto';
  applyTheme(saved);
  document.addEventListener('DOMContentLoaded', () => document.querySelectorAll('[data-theme-btn]').forEach(b => b.classList.toggle('active', b.dataset.themeBtn === saved)));
})();

// ===== Tabs =====
document.querySelectorAll('.tab').forEach(t => t.addEventListener('click', () => {
  document.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
  document.querySelectorAll('.panel').forEach(x => x.classList.remove('active'));
  t.classList.add('active');
  $('panel-' + t.dataset.tab).classList.add('active');
}));

// ===== Status =====
async function checkStatus() {
  try {
    const r = await fetch('/api/status');
    if (r.status === 401) { showGate(); return; }
    const d = await r.json();
    if (d.is_authorized) {
      $('modeBadge').textContent = (d.mode === 'live' ? '实盘' : '模拟盘') + (d.is_running ? ' 运行中' : ' 已暂停');
      $('modeBadge').className = 'badge ' + (d.mode === 'live' ? 'live' : 'dry');
      $('stRun').textContent = d.is_running ? fmtSec(d.run_seconds) : '已暂停';
      $('stNext').textContent = d.is_running ? d.current_action : '待启动';
      startDash();
    } else {
      showGate();
    }
  } catch (e) { showGate(); }
}

function fmtSec(s) {
  s = Number(s) || 0;
  const h = Math.floor(s/3600), m = Math.floor(s%3600/60), ss = s%60;
  return (h? h+'时':'') + (m? m+'分':'') + ss + '秒';
}
function showGate() {
  stopDash();
  document.body.innerHTML = `
    <div class="gate"><div class="gate-card">
      <div class="logo">📈</div>
      <h1>AShare<span>Auto</span></h1>
      <div class="sub">A股自动交易系统 · 主界面</div>
      <input type="password" id="gatePw" placeholder="访问密码(若已设置)" autocomplete="off">
      <button class="btn-primary" onclick="gateAuthorize()">🔓 授权并启动</button>
      <div class="hint">默认模拟盘运行。首次启动将自动应用默认配置(本金¥100,000, 双低/打新/红利策略)。<br>授权即代表已阅读并接受风险提示: A股无稳赚策略, 请只用可承受损失的金额。</div>
    </div></div>`;
}

async function gateAuthorize() {
  const pw = $('gatePw').value;
  const r = await fetch('/api/authorize', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({password: pw})});
  if (r.status === 401) { alert('密码错误'); return; }
  const d = await r.json();
  if (d.success) { location.reload(); } else { alert(d.error || '授权失败'); }
}

// ===== Dashboard poll =====
function startDash() {
  if (dashTimer) return;
  loadDashboard();
  dashTimer = setInterval(loadDashboard, 5000);
}
function stopDash() { if (dashTimer) { clearInterval(dashTimer); dashTimer = null; } }

async function loadDashboard() {
  try {
    const r = await fetch('/api/dashboard');
    if (r.status === 401) { showGate(); return; }
    const d = await r.json();
    dashData = d;
    renderStats(d);
    renderOpps(d);
    renderPositions(d);
    renderOrders(d);
    renderPerf(d);
    renderMarkets();
    renderLog(d);
    updateNetDot(d);
  } catch (e) {}
}

function updateNetDot(d) {
  const ok = d && !(d.config && false);
  $('netDot').className = 'net-dot ' + (d && d.stats ? 'ok' : 'warn');
}

function renderStats(d) {
  const s = d.stats;
  $('stAsset').textContent = fmtMoney(s.total_asset);
  $('stInvested').textContent = fmtMoney(s.market_value) + ' (' + s.invested_pct + '%)';
  $('stCash').textContent = fmtMoney(s.cash) + (s.frozen ? ' /冻结' + fmt(s.frozen,0) : '');
  $('stPos').textContent = s.positions + ' / ' + s.max_positions;
  $('stToday').textContent = fmtMoney(s.today_pnl);
  $('stToday').className = pnlClass(s.today_pnl);
  $('stPnl').textContent = fmtMoney(s.realized_pnl);
  $('stPnl').className = pnlClass(s.realized_pnl);
  $('stWinrate').textContent = s.win_rate + '%';
  $('stNext').textContent = s.last_scan ? '最后扫描 ' + fmtSec(Math.max(0, Date.now()/1000 - new Date(s.last_scan).getTime()/1000)) + '前' : '等待首轮';
}

function stratBadge(s) {
  const map = {'cb_double_low':'双低','cb_new':'打新','dividend':'红利','trend':'趋势'};
  return map[s] || s;
}

function renderOpps(d) {
  const opps = d.opportunities || [];
  $('oppCnt').textContent = opps.length + ' 个';
  const body = $('oppBody');
  if (!opps.length) { body.innerHTML = '<tr><td colspan="6"><div class="empty">暂无机会</div></td></tr>'; return; }
  body.innerHTML = opps.map(o => {
    const key = o.strategy === 'cb_double_low' ? '双低值 ' + o.ev : (o.strategy === 'dividend' ? '股息率 ' + o.yield + '%' : (o.strategy === 'trend' ? '动量 ' + o.mom20 + '%' : (o.apply_date || '')));
    const cls = ['cb_double_low','dividend','trend'].includes(o.strategy) ? '双低值' : '打新';
    return `<tr>
      <td><span class="status-pill filled" style="background:var(--purple-l);color:var(--purple)">${stratBadge(o.strategy)}</span></td>
      <td>${esc(o.symbol)}</td>
      <td>${esc(o.name || '')}</td>
      <td>${o.price ? fmt(o.price) : '--'}</td>
      <td>${esc(key)}</td>
      <td>${fmt(o.score,0)}<span class="score-bar"><i style="width:${Math.min(100,o.score||0)}%"></i></span></td>
    </tr>`;
  }).join('');
}

function renderPositions(d) {
  const pos = d.positions || [];
  $('posCnt').textContent = pos.length + ' 个';
  const body = $('posBody');
  if (!pos.length) { body.innerHTML = '<tr><td colspan="9"><div class="empty">暂无持仓</div></td></tr>'; return; }
  body.innerHTML = pos.map(p => {
    const qty = p.qty || 0, cost = p.avg_cost || 0, last = p.last_price || cost;
    const mv = qty * last, pnl = (last - cost) * qty;
    const pct = cost ? (last/cost-1)*100 : 0;
    return `<tr>
      <td>${esc(p.symbol)}</td>
      <td>${esc(p.name || '')}</td>
      <td><span class="status-pill filled" style="background:var(--purple-l);color:var(--purple)">${stratBadge(p.strategy)}</span></td>
      <td>${fmt(qty,0)}</td>
      <td>${fmt(cost)}</td>
      <td>${fmt(last)}</td>
      <td>${fmtMoney(mv)}</td>
      <td class="${pnlClass(pnl)}">${fmt(pnl)} (${fmt(pct,1)}%)</td>
      <td><button class="small-btn danger" onclick="sellSymbol('${p.symbol}')">卖出</button></td>
    </tr>`;
  }).join('');
}

async function sellSymbol(symbol) {
  if (!confirm('确认卖出 ' + symbol + '?')) return;
  const r = await fetch('/api/sell', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({symbol})});
  const d = await r.json();
  showToast(d.success ? ('已卖出, 盈亏 ' + fmt(d.pnl)) : (d.error || '卖出失败'));
  loadDashboard();
}

function renderOrders(d) {
  const orders = d.orders || [];
  const body = $('orderBody');
  if (!orders.length) { body.innerHTML = '<tr><td colspan="7"><div class="empty">暂无订单</div></td></tr>'; return; }
  body.innerHTML = orders.slice(0,20).map(o => `
    <tr>
      <td>${esc((o.created||'').slice(5,19))}</td>
      <td>${esc(o.symbol)}</td>
      <td style="color:${o.side==='buy'?'var(--green)':'var(--red)'}">${o.side==='buy'?'买入':'卖出'}</td>
      <td>${fmt(o.filled_qty||o.quantity,0)}</td>
      <td>${o.avg_price ? fmt(o.avg_price) : '--'}</td>
      <td><span class="status-pill ${o.status}">${o.status}</span></td>
      <td>${esc(stratBadge(o.strategy))}</td>
    </tr>`).join('');
}

function renderPerf(d) {
  const perf = d.perf || {};
  const keys = Object.keys(perf);
  const body = $('perfBody');
  if (!keys.length) { body.innerHTML = '<tr><td colspan="8"><div class="empty">暂无平仓记录</div></td></tr>'; return; }
  body.innerHTML = keys.map(k => {
    const p = perf[k];
    return `<tr>
      <td><b>${stratBadge(k)}</b></td>
      <td>${p.trades}</td>
      <td style="color:var(--green)">${p.wins}</td>
      <td style="color:var(--red)">${p.losses}</td>
      <td style="font-weight:600">${p.win_rate}%</td>
      <td class="${pnlClass(p.pnl)}">${fmtMoney(p.pnl)}</td>
      <td>${fmt(p.expectancy)}</td>
      <td>${p.profit_factor >= 99 ? '∞' : fmt(p.profit_factor)}</td>
    </tr>`;
  }).join('');
}

// ===== Equity chart =====
function drawEquity(curve) {
  const cv = $('equityChart'); if (!cv) return;
  const ctx = cv.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  const W = cv.parentElement.clientWidth, H = 280;
  cv.width = W * dpr; cv.height = H * dpr; cv.style.width = W+'px'; cv.style.height = H+'px';
  ctx.scale(dpr, dpr);
  ctx.clearRect(0,0,W,H);
  if (!curve || curve.length < 2) {
    ctx.fillStyle = 'var(--muted)'; ctx.font = '13px sans-serif'; ctx.textAlign='center';
    ctx.fillText('等待数据...', W/2, H/2); return;
  }
  const vals = curve.map(c => c[1]);
  const min = Math.min(...vals), max = Math.max(...vals);
  const pad = 14, span = Math.max(max-min, 1);
  const x = i => pad + i * (W - pad*2) / (curve.length - 1);
  const y = v => H - pad - (v - min) / span * (H - pad*2);
  // grid
  ctx.strokeStyle = 'var(--border)'; ctx.lineWidth = 1; ctx.font = '10px sans-serif'; ctx.fillStyle = 'var(--muted)';
  for (let i=0; i<4; i++) {
    const gy = pad + i*(H-pad*2)/3;
    ctx.beginPath(); ctx.moveTo(pad, gy); ctx.lineTo(W-pad, gy); ctx.stroke();
    const val = max - (max-min)*i/3;
    ctx.fillText(fmtMoney(val), 2, gy+3);
  }
  // line
  ctx.strokeStyle = 'var(--primary)'; ctx.lineWidth = 2; ctx.beginPath();
  curve.forEach((c,i) => { i===0 ? ctx.moveTo(x(i), y(c[1])) : ctx.lineTo(x(i), y(c[1])); });
  ctx.stroke();
  // fill
  ctx.lineTo(x(curve.length-1), H-pad); ctx.lineTo(x(0), H-pad); ctx.closePath();
  ctx.fillStyle = 'var(--primary-l)'; ctx.fill();
  // last value
  const last = vals[vals.length-1];
  ctx.fillStyle = 'var(--text)'; ctx.textAlign = 'left';
  ctx.fillText(fmtMoney(last) + ' (最新)', W-pad-130, y(last)-6);
}

// ===== Markets =====
async function renderMarkets() {
  try {
    const r = await fetch('/api/markets');
    if (r.status === 401) return;
    const d = await r.json();
    if (!d.success) return;
    const cb = d.cb || [], st = d.stocks || [];
    $('mktCbCnt').textContent = cb.length + ' 只';
    $('mktCbBody').innerHTML = cb.map(x => `<tr>
      <td>${esc(x.symbol)}</td><td>${esc(x.name||'')}</td><td>${fmt(x.price)}</td>
      <td>${fmt(x.premium,1)}</td><td style="font-weight:600">${fmt(x.dlow,1)}</td>
      <td>${esc(x.rating||'')}</td><td>${x.size!=null?fmt(x.size,1):'--'}</td><td>${esc(x.stock||'')}</td>
    </tr>`).join('');
    $('mktStockCnt').textContent = st.length + ' 只';
    $('mktStockBody').innerHTML = st.map(x => `<tr>
      <td>${esc(x.symbol)}</td><td>${esc(x.name||'')}</td><td style="font-weight:600">${fmt(x.yield,2)}%</td>
    </tr>`).join('');
  } catch (e) {}
}

// ===== Log =====
function renderLog(d) {
  const logs = d.log || [];
  const box = $('logBox');
  box.innerHTML = logs.map(l => `<div class="log-line">${esc(l)}</div>`).join('');
  box.scrollTop = box.scrollHeight;
}

// ===== Settings =====
async function openSettings() {
  try {
    const r = await fetch('/api/config');
    if (r.status === 401) { showGate(); return; }
    const d = await r.json();
    if (!d.success) return;
    buildSettingsForm(d.config);
    $('settingsModal').classList.add('show');
  } catch (e) {}
}
function closeSettings() { $('settingsModal').classList.remove('show'); }

function togglePill(key, scope) {
  const pill = $('pill_' + scope + '_' + key);
  const on = pill.classList.contains('on');
  pill.classList.toggle('on', !on); pill.classList.toggle('off', on);
  pill.textContent = !on ? '开' : '关';
}

function buildSettingsForm(cfg) {
  const c = cfg, dl = cfg.strategy_cb_double_low || {}, dn = cfg.strategy_cb_new || {},
        dv = cfg.strategy_dividend || {}, tr = cfg.strategy_trend || {},
        fees = cfg.fees || {};
  const riskNames = {1:'极保守',2:'保守',3:'偏保守',4:'稳健偏保守',5:'平衡',6:'稳健偏进取',7:'进取',8:'积极',9:'激进',10:'极激进'};
  $('settingsBody').innerHTML = `
  <div class="setting-section">
    <div class="setting-section-title">💰 账户与风控</div>
    <div class="field-grid">
      <div class="field"><label>初始资金 (¥)</label><input type="number" id="set_bankroll" value="${c.bankroll_cny||100000}"></div>
      <div class="field"><label>扫描间隔 (秒)</label><input type="number" id="set_scan" value="${c.scan_interval||300}"></div>
      <div class="field"><label>最大持仓数</label><input type="number" id="set_maxpos" value="${c.max_positions||20}"></div>
      <div class="field"><label>每日交易上限</label><input type="number" id="set_maxday" value="${c.max_daily_trades||10}"></div>
      <div class="field"><label>日亏硬停 (%)</label><input type="number" id="set_daily_loss" value="${(c.daily_loss_limit_pct||0.1)*100}"></div>
      <div class="field"><label>交易模式</label><select id="set_mode">
        <option value="paper" ${c.trading_mode==='paper'?'selected':''}>模拟盘 (Paper)</option>
        <option value="live" ${c.trading_mode==='live'?'selected':''}>实盘 (需 QMT)</option>
      </select></div>
      <div class="field"><label>券商</label><select id="set_broker">
        <option value="paper" ${c.broker==='paper'?'selected':''}>仿真 Paper</option>
        <option value="qmt" ${c.broker==='qmt'?'selected':''}>QMT (实盘)</option>
      </select></div>
      <div class="field"><label>访问密码</label><input type="password" id="set_pw" placeholder="留空不校验" value="${esc(c.web_password||'')}"></div>
    </div>
    <div class="field mt16"><label>风险等级: <b id="riskVal">${c.risk_level||5} · ${riskNames[c.risk_level]||'平衡'}</b></label>
      <input type="range" class="range-slider" id="set_risk" min="1" max="10" value="${c.risk_level||5}"
        oninput="$('riskVal').textContent = this.value + ' · ' + ({1:'极保守',2:'保守',3:'偏保守',4:'稳健偏保守',5:'平衡',6:'稳健偏进取',7:'进取',8:'积极',9:'激进',10:'极激进'}[this.value])">
      <div class="risk-desc">更高风险 = 更大敞口/仓位, 更高波动与回撤。</div></div>
  </div>

  <div class="setting-section">
    <div class="setting-section-title">📐 可转债双低轮动</div>
    <div class="toggle-item" style="margin-bottom:10px">
      <div><div class="t-label">双低轮动策略</div><div class="t-desc">核心策略 · 每周调仓等权</div></div>
      <button class="pill ${dl.enabled?'on':'off'}" id="pill_dl_enabled" onclick="togglePill('enabled','dl')">${dl.enabled?'开':'关'}</button>
    </div>
    <div class="field-grid">
      <div class="field"><label>最大价格 (债价)</label><input type="number" id="set_dl_price" value="${dl.max_price||115}"></div>
      <div class="field"><label>最大溢价率 (%)</label><input type="number" id="set_dl_prem" value="${dl.max_premium||40}"></div>
      <div class="field"><label>最小规模 (亿)</label><input type="number" id="set_dl_size" value="${dl.min_size||2}"></div>
      <div class="field"><label>最低评级</label><select id="set_dl_rating">
        ${['AAA','AA+','AA','AA-','A'].map(x=>`<option ${(dl.min_rating||'AA')===x?'selected':''}>${x}</option>`).join('')}
      </select></div>
      <div class="field"><label>持仓只数</label><input type="number" id="set_dl_n" value="${dl.hold_n||10}"></div>
      <div class="field"><label>调仓周期 (天)</label><input type="number" id="set_dl_days" value="${dl.rebalance_days||7}"></div>
    </div>
  </div>

  <div class="setting-section">
    <div class="setting-section-title">🎯 转债打新 / 📊 红利低波 / 📈 趋势跟随</div>
    <div class="toggle-item" style="margin-bottom:8px">
      <div><div class="t-label">转债打新提醒</div><div class="t-desc">免费顶格申购 · 上市首日卖出纪律</div></div>
      <button class="pill ${dn.enabled?'on':'off'}" id="pill_cn_enabled" onclick="togglePill('enabled','cn')">${dn.enabled?'开':'关'}</button>
    </div>
    <div class="toggle-item" style="margin-bottom:8px">
      <div><div class="t-label">红利低波策略</div><div class="t-desc">月度调仓 · 高股息低波动</div></div>
      <button class="pill ${dv.enabled?'on':'off'}" id="pill_dv_enabled" onclick="togglePill('enabled','dv')">${dv.enabled?'开':'关'}</button>
    </div>
    <div class="field-grid">
      <div class="field"><label>红利最低股息率 (%)</label><input type="number" id="set_dv_yield" value="${(dv.min_yield||0.03)*100}"></div>
      <div class="field"><label>红利持仓只数</label><input type="number" id="set_dv_n" value="${dv.hold_n||10}"></div>
    </div>
    <div class="toggle-item mt8">
      <div><div class="t-label">趋势跟随策略</div><div class="t-desc">默认关闭 · 方向性策略无稳健 edge</div></div>
      <button class="pill ${tr.enabled?'on':'off'}" id="pill_tr_enabled" onclick="togglePill('enabled','tr')">${tr.enabled?'开':'关'}</button>
    </div>
  </div>

  <div class="setting-section">
    <div class="setting-section-title">🖥️ QMT 实盘通道</div>
    <div class="field-grid">
      <div class="field"><label>QMT 安装路径</label><input type="text" id="set_qmt_path" value="${esc(c.qmt?.path||'D:/qmt')}"></div>
      <div class="field"><label>资金账号</label><input type="text" id="set_qmt_acc" value="${esc(c.qmt?.account_id||'')}"></div>
    </div>
  </div>

  <div class="setting-section">
    <div class="setting-section-title">🔗 子界面</div>
    <div class="field"><label>Polymarket 机器人地址</label><input type="text" id="set_polyurl" value="${esc(c.polyauto_url||'http://localhost:5001')}"></div>
  </div>

  <div class="setting-section">
    <div class="setting-section-title">💰 费用设置 (‰ 为万分之)</div>
    <div class="field-grid">
      <div class="field"><label>股票佣金 (万/千?) 默认万2.5 → 0.25</label><input type="number" step="0.01" id="set_fee_scomm" value="${fees.stock_commission_bps||0.25}"></div>
      <div class="field"><label>印花税 (万5 → 5.0)</label><input type="number" step="0.1" id="set_fee_stamp" value="${fees.stamp_bps||5}"></div>
      <div class="field"><label>转债佣金 (万2 → 0.2)</label><input type="number" step="0.01" id="set_fee_cb" value="${fees.cb_commission_bps||0.2}"></div>
    </div>
  </div>`;
}

async function saveSettings() {
  const val = id => $(id).value;
  const pil = (scope, key) => $('pill_' + scope + '_' + key).classList.contains('on');
  const body = {
    bankroll_cny: Number(val('set_bankroll')) || 100000,
    scan_interval: Number(val('set_scan')) || 300,
    max_positions: Number(val('set_maxpos')) || 20,
    max_daily_trades: Number(val('set_maxday')) || 10,
    daily_loss_limit_pct: (Number(val('set_daily_loss')) || 10) / 100,
    trading_mode: val('set_mode'),
    broker: val('set_broker'),
    web_password: val('set_pw'),
    risk_level: Number(val('set_risk')) || 5,
    polyauto_url: val('set_polyurl'),
    strategy_cb_double_low: {
      enabled: pil('dl','enabled'), max_price: Number(val('set_dl_price'))||115,
      max_premium: Number(val('set_dl_prem'))||40, min_size: Number(val('set_dl_size'))||2,
      min_rating: val('set_dl_rating'), hold_n: Number(val('set_dl_n'))||10,
      rebalance_days: Number(val('set_dl_days'))||7,
    },
    strategy_cb_new: { enabled: pil('cn','enabled') },
    strategy_dividend: { enabled: pil('dv','enabled'), min_yield: (Number(val('set_dv_yield'))||3)/100, hold_n: Number(val('set_dv_n'))||10 },
    strategy_trend: { enabled: pil('tr','enabled') },
    qmt: { path: val('set_qmt_path'), account_id: val('set_qmt_acc') },
    fees: {
      stock_commission_bps: Number(val('set_fee_scomm'))||0.25,
      stamp_bps: Number(val('set_fee_stamp'))||5,
      cb_commission_bps: Number(val('set_fee_cb'))||0.2,
    },
  };
  const r = await fetch('/api/setup', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
  const d = await r.json();
  if (d.success) { showToast('配置已保存 ✓ 重启授权后生效'); closeSettings(); }
  else showToast('保存失败');
}

// ===== Authorize / Stop =====
function openAuthorize() { $('authorizeModal').classList.add('show'); }
function closeAuthorize() { $('authorizeModal').classList.remove('show'); }
async function doAuthorize() {
  const pw = $('authPw').value;
  const r = await fetch('/api/authorize', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({password: pw})});
  if (r.status === 401) { alert('密码错误'); return; }
  const d = await r.json();
  closeAuthorize();
  if (d.success) { showToast(d.message); setTimeout(()=>location.reload(), 600); }
  else alert(d.error || '授权失败');
}
async function stopAll() {
  if (!confirm('确认停止交易引擎?')) return;
  const r = await fetch('/api/start-stop', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({running:false})});
  const d = await r.json();
  showToast(d.success ? '引擎已停止' : (d.error || '操作失败'));
  loadDashboard();
}
async function forceRefresh() {
  const r = await fetch('/api/refresh-data', {method:'POST'});
  const d = await r.json();
  showToast(d.success ? '数据刷新已启动' : (d.error || '失败'));
}

// ===== Backtest =====
async function loadBacktest() {
  try {
    const r = await fetch('/api/backtest');
    if (r.status === 401) return;
    const d = await r.json();
    if (!d.success) return;
    const rep = d.report || {};
    if (rep.status === 'not_generated' || !rep.cb_double_low) return;
    const bt = rep.cb_double_low;
    if (bt.error) { $('btBody').innerHTML = '<div class="empty">' + esc(bt.error) + '</div>'; return; }
    const gate = rep.gate_cb_double_low || {};
    const gcls = gate.pass ? 'color:var(--green)' : 'color:var(--red)';
    $('btMeta').textContent = '· ' + (bt.years||3) + ' 年回测 · 生成于 ' + (rep.generated_at||'').slice(0,16);
    $('btBody').innerHTML = `
      <div class="grid-2">
        <div class="stat"><div class="label">总收益</div><div class="value ${bt.total_return_pct>=0?'pos':'neg'}">${fmt(bt.total_return_pct,2)}%</div></div>
        <div class="stat"><div class="label">年化 CAGR</div><div class="value">${fmt(bt.cagr_pct,2)}%</div></div>
        <div class="stat"><div class="label">最大回撤</div><div class="value ${bt.max_drawdown_pct<=25?'pos':'neg'}">${fmt(bt.max_drawdown_pct,2)}%</div></div>
        <div class="stat"><div class="label">胜率</div><div class="value ${bt.win_rate_pct>=55?'pos':'neg'}">${fmt(bt.win_rate_pct,1)}%</div></div>
        <div class="stat"><div class="label">盈亏因子</div><div class="value">${bt.profit_factor>=99?'∞':fmt(bt.profit_factor)}</div></div>
        <div class="stat"><div class="label">交易数</div><div class="value">${bt.trades}</div></div>
      </div>
      <div class="mt16" style="font-size:12px; color:var(--muted)">
        <div><b>门禁:</b> <span style="${gcls}">${esc(gate.reason||'')}</span></div>
        <div class="mt8">⚠️ ${esc(bt.note||'')}</div>
      </div>
      <canvas id="btChart" class="mt16"></canvas>`;
    drawBacktest(bt.equity_curve || []);
  } catch (e) {}
}

function drawBacktest(curve) {
  const cv = $('btChart'); if (!cv) return;
  const ctx = cv.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  const W = cv.parentElement.clientWidth, H = 260;
  cv.width = W*dpr; cv.height = H*dpr; cv.style.width = W+'px'; cv.style.height = H+'px';
  ctx.scale(dpr,dpr); ctx.clearRect(0,0,W,H);
  if (!curve || curve.length < 2) return;
  const vals = curve.map(c=>c[1]), min = Math.min(...vals), max = Math.max(...vals);
  const pad = 14, span = Math.max(max-min,1);
  const x = i => pad + i*(W-pad*2)/(curve.length-1);
  const y = v => H-pad-(v-min)/span*(H-pad*2);
  ctx.strokeStyle = 'var(--border)'; ctx.fillStyle = 'var(--muted)'; ctx.font='10px sans-serif';
  for (let i=0;i<4;i++){ const gy=pad+i*(H-pad*2)/3; ctx.beginPath(); ctx.moveTo(pad,gy); ctx.lineTo(W-pad,gy); ctx.stroke(); ctx.fillText('¥'+fmt(max-(max-min)*i/3,0), 2, gy+3); }
  ctx.strokeStyle = 'var(--green)'; ctx.lineWidth=2; ctx.beginPath();
  curve.forEach((c,i)=> i===0?ctx.moveTo(x(i),y(c[1])):ctx.lineTo(x(i),y(c[1])));
  ctx.stroke();
}

async function genBacktest(strategy) {
  showToast('回测启动中(首次需数分钟拉历史数据)...', 5000);
  const r = await fetch('/api/generate-backtest', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({strategy, years:3})});
  const d = await r.json();
  showToast(d.success ? (d.message) : (d.error||'失败'));
  setTimeout(()=>{ loadBacktest(); setInterval(loadBacktest, 15000); }, 3000);
}

// ===== Init =====
document.addEventListener('DOMContentLoaded', () => {
  checkStatus();
  setInterval(() => { if (dashData) loadBacktest(); }, 20000);
});
</script>
</body>
</html>
"""


if __name__ == "__main__":
    port = int(os.environ.get("ASHARE_PORT", "5000"))
    print(f"AShareAuto v{VERSION} 启动于 http://localhost:{port}  (PolyAuto 子界面: http://localhost:5001)")
    print("首次打开请在浏览器访问 http://localhost:" + str(port) + " 完成配置与授权")
    app.run(host="0.0.0.0", port=port, threaded=True)
