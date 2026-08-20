# -*- coding: utf-8 -*-
"""
ashare_data.py — A股 数据层
============================
akshare 封装 + SQLite 持久缓存 + 断线重试 + 熔断降级。

已验证的数据源(2026-08 实测通过):
  trade_calendar      tool_trade_date_hist_sina()          sina     (8797 行, 至 2026-12-31)
  stock_daily         stock_zh_a_daily(symbol, qfq)        sina     (备用 tencent stock_zh_a_hist_tx)
  cb_snapshot         bond_zh_cov()                        eastmoney (1050 行, 全市场可转债)
  cb_daily            bond_zh_hs_daily(symbol)             sina
  cb_value_history    bond_zh_cov_value_analysis(symbol)   eastmoney (回测双低的每日转股溢价/纯债价值)
  index_constituents  index_stock_cons_csindex(symbol)     csindex
  stock_dividends     stock_dividend_cninfo(symbol)        cninfo    (股息率)

已知被拦截的端点(本机 eastmoney push2* 被 TLS 指纹重置, 不采用):
  stock_zh_a_hist / stock_zh_a_spot_em / bond_cov_comparison / stock_zh_a_spot

设计要点(镜像 PolyAuto 网络韧性):
  - 每个端点独立重试(3 次指数退避) + 独立熔断器(连续失败 >=3 切降级, 返回缓存)
  - SQLite 单文件缓存 data/ashare_cache.db, 以 symbol+adjust 为主键, 记录 asof
  - 全部网络调用带 timeout; 交易日历/分红/成分股低频刷新, 行情按鲜度刷新
"""

import json
import os
import sqlite3
import threading
import time
import traceback
from datetime import date, datetime, timedelta

try:
    import akshare as ak
    _AK_IMPORTED = True
except Exception:  # 数据层降级: 无 akshare 时仅能从缓存读
    ak = None
    _AK_IMPORTED = False

import pandas as pd

# 本机有全局代理(Clash 127.0.0.1:7890), 这些数据源必须直连
_NOPROXY_HOSTS = (
    "eastmoney.com,sina.com.cn,10jqka.com.cn,126.net,gtimg.cn,qq.com,163.com,"
    "csindex.com.cn,cninfo.com.cn"
)
_ensure_noproxy_done = False


def _ensure_noproxy():
    global _ensure_noproxy_done
    if _ensure_noproxy_done:
        return
    _ensure_noproxy_done = True
    for key in ("NO_PROXY", "no_proxy"):
        cur = os.environ.get(key, "")
        missing = [d for d in _NOPROXY_HOSTS.split(",") if d and d not in cur]
        if missing:
            os.environ[key] = (cur + "," + ",".join(missing)).strip(",")


_ensure_noproxy()

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
CACHE_DB = os.path.join(DATA_DIR, "ashare_cache.db")

# 每端点 (endpoint, source) -> 默认鲜度阈值(秒)。过鲜返回缓存, 过期触发刷新
FRESHNESS = {
    "trade_calendar": 24 * 3600 * 7,       # 交易日历每周刷一次
    "stock_daily": 2 * 3600,               # 日线盘中 2h 鲜度
    "cb_snapshot": 2 * 3600,               # 转债快照盘中 2h
    "cb_daily": 2 * 3600,
    "cb_value_history": 24 * 3600,         # 价值历史日级
    "index_constituents": 24 * 3600 * 30,  # 成分股月度
    "stock_dividends": 24 * 3600 * 30,     # 分红月度
}

# 交易时段
_SESSION_START = (9, 30)
_SESSION_END = (15, 0)

_INDEX_NAME = {"000300": "沪深300", "000905": "中证500", "000852": "中证1000", "000922": "中证红利"}

# 转债/股票代码判定
_SH_PREFIXES = ("600", "601", "603", "605", "688", "900")
_SZ_PREFIXES = ("000", "001", "002", "003", "300", "301", "200")
_CB_PREFIXES_SH = ("11",)   # 沪市可转债 110xxx / 113xxx
_CB_PREFIXES_SZ = ("12",)   # 深市可转债 123xxx / 127xxx / 128xxx
_GEM_PREFIXES = ("300", "301")   # 创业板 ±20%
_STAR_PREFIXES = ("688",)        # 科创板 ±20%


def is_cb(symbol6):
    """判断 6 位代码是否为可转债(沪 11x / 深 12x)。"""
    return symbol6[:2] in _CB_PREFIXES_SH or symbol6[:2] in _CB_PREFIXES_SZ


def to_akshare(symbol6):
    """6 位代码 -> akshare sina 风格 (sh600519 / sz000001 / sh113537)。"""
    s = symbol6.strip()
    if s[0] in "sz" or s[0] in "SHsh":
        return s.lower()
    if is_cb(s):
        return ("sh" + s) if s.startswith("11") else ("sz" + s)
    if s.startswith(_SH_PREFIXES):
        return "sh" + s
    return "sz" + s


def to_qmt(symbol6):
    """6 位代码 -> QMT 风格 (600519.SH / 000001.SZ)。"""
    s = symbol6.strip()
    return (s + ".SH") if s.startswith(_SH_PREFIXES) else (s + ".SZ")


def price_limit_pct(symbol6, name=""):
    """涨跌停幅度(小数): 创业板/科创 0.20, ST 0.05, 主板 0.10。转债无涨跌停限制。"""
    s = symbol6.strip()
    if is_cb(s):
        return 1.0  # 转债无涨跌停(有 20% 异动但制度上无限制)
    if s.startswith(_GEM_PREFIXES) or s.startswith(_STAR_PREFIXES):
        return 0.20
    if "ST" in (name or "").upper():
        return 0.05
    return 0.10


# ---------------------------------------------------------------------------
# 通用网络韧性: 重试 + 熔断
# ---------------------------------------------------------------------------
class CircuitBreaker:
    """单端点熔断器: 连续失败 >= threshold 次进入 open, 只允许返回缓存; 成功后复位。"""

    def __init__(self, threshold=3):
        self.threshold = threshold
        self._lock = threading.Lock()
        self.failures = 0
        self.open_until = 0.0

    def allow(self):
        with self._lock:
            if self.open_until and time.time() < self.open_until:
                return False
            return True

    def ok(self):
        with self._lock:
            self.failures = 0
            self.open_until = 0.0

    def fail(self):
        with self._lock:
            self.failures += 1
            if self.failures >= self.threshold:
                self.open_until = time.time() + 300  # open 5 分钟
        return self.failures >= self.threshold

    @property
    def is_degraded(self):
        with self._lock:
            return self.failures >= self.threshold or (self.open_until and time.time() < self.open_until)


class RetryPolicy:
    def __init__(self, tries=3, base_wait=1.5, max_wait=8.0, timeout=20):
        self.tries = tries
        self.base_wait = base_wait
        self.max_wait = max_wait
        self.timeout = timeout


def _call_with_retry(fn, *args, breaker=None, policy=None, **kwargs):
    """带重试+熔断的调用包装。网络错误重试; 熔断 open 时立刻抛 NotAvailable。"""
    policy = policy or RetryPolicy()
    last_exc = None
    for attempt in range(policy.tries):
        if breaker is not None and not breaker.allow():
            raise DataUnavailable("circuit open for endpoint")
        try:
            result = fn(*args, **kwargs)
            if breaker is not None:
                breaker.ok()
            return result
        except (ConnectionError, TimeoutError, OSError) as e:
            last_exc = e
            if breaker is not None and breaker.fail():
                break
            if attempt < policy.tries - 1:
                time.sleep(min(policy.base_wait * (2 ** attempt), policy.max_wait))
        except Exception as e:  # 非网络错误(参数/解析)不重试
            if breaker is not None:
                breaker.fail()
            raise DataError(str(e)) from e
    raise DataUnavailable(str(last_exc) if last_exc else "network failed")


class DataUnavailable(Exception):
    """网络不可用 / 熔断中 —— 上层切换到缓存精简模式。"""


class DataError(Exception):
    """数据源返回异常(非网络)。"""


# ---------------------------------------------------------------------------
# 数据库
# ---------------------------------------------------------------------------
def _connect():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(CACHE_DB, timeout=20)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


_SCHEMA = """
CREATE TABLE IF NOT EXISTS trade_calendar(
    trade_date TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS stock_daily(
    symbol TEXT NOT NULL, dt TEXT NOT NULL, open REAL, high REAL, low REAL,
    close REAL, volume REAL, amount REAL, turnover REAL,
    asof TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY(symbol, dt)
);
CREATE TABLE IF NOT EXISTS cb_snapshot(
    symbol TEXT PRIMARY KEY, name TEXT, price REAL, premium REAL, cv REAL,
    cv_price REAL, issue_size REAL, rating TEXT, listed TEXT, apply_date TEXT,
    win_rate REAL, apply_limit REAL, stock_code TEXT, stock_name TEXT,
    redeem_notice TEXT, maturity REAL, remain_years REAL, price_eff REAL, asof TEXT
);
CREATE TABLE IF NOT EXISTS cb_daily(
    symbol TEXT NOT NULL, dt TEXT NOT NULL, open REAL, high REAL, low REAL,
    close REAL, volume REAL, asof TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY(symbol, dt)
);
CREATE TABLE IF NOT EXISTS cb_value_history(
    symbol TEXT NOT NULL, dt TEXT NOT NULL, close REAL, pure_value REAL,
    cv_value REAL, pure_premium REAL, premium REAL, asof TEXT,
    PRIMARY KEY(symbol, dt)
);
CREATE TABLE IF NOT EXISTS index_constituents(
    idx TEXT NOT NULL, stock_code TEXT NOT NULL, stock_name TEXT, asof TEXT,
    PRIMARY KEY(idx, stock_code)
);
CREATE TABLE IF NOT EXISTS stock_dividends(
    symbol TEXT NOT NULL, ex_div_date TEXT, pay_ratio REAL, report TEXT,
    asof TEXT, PRIMARY KEY(symbol, ex_div_date)
);
CREATE TABLE IF NOT EXISTS dividend_scan(
    symbol TEXT PRIMARY KEY, name TEXT, yield REAL, asof TEXT
);
CREATE INDEX IF NOT EXISTS idx_stock_daily ON stock_daily(symbol, dt);
CREATE INDEX IF NOT EXISTS idx_cb_value ON cb_value_history(symbol, dt);
"""


def _init_schema():
    conn = _connect()
    try:
        conn.executescript(_SCHEMA)
        # 增量迁移: 老库补 price_eff 列
        try:
            conn.execute("ALTER TABLE cb_snapshot ADD COLUMN price_eff REAL")
        except sqlite3.OperationalError:
            pass
        conn.commit()
    finally:
        conn.close()


_init_schema()

# 内存表时间戳: (endpoint, symbol) -> last_updated_epoch
_MEM_FRESH = {}
_MEM_FRESH_LOCK = threading.Lock()
_BREAKERS = {name: CircuitBreaker() for name in FRESHNESS}


def _mark_fresh(key):
    with _MEM_FRESH_LOCK:
        _MEM_FRESH[key] = time.time()


def _is_fresh(key, endpoint):
    with _MEM_FRESH_LOCK:
        ts = _MEM_FRESH.get(key)
    if ts is None:
        return False
    return (time.time() - ts) < FRESHNESS[endpoint]


def is_market_open(now=None):
    """当前是否处于交易时段(供刷新/下单门禁)。"""
    now = now or datetime.now()
    if now.weekday() >= 5:
        return False
    hm = (now.hour, now.minute)
    return _SESSION_START <= hm < _SESSION_END


def is_trading_day(d=None):
    """查缓存交易日历判断是否交易日; 缓存缺失时按周一到周五兜底。"""
    d = d or date.today()
    conn = _connect()
    try:
        cur = conn.execute("SELECT 1 FROM trade_calendar WHERE trade_date=?", (d.isoformat(),))
        hit = cur.fetchone() is not None
    finally:
        conn.close()
    if hit:
        return True
    # 缓存中没有该日(可能还没刷新) —— 用最近缓存判断其前后趋势: 若当天是周六日返回 False
    if d.weekday() >= 5:
        return False
    return True  # 周中且日历缓存暂无 → 按交易日兜底(刷新日历后自愈)


# ---------------------------------------------------------------------------
# 数据访问
# ---------------------------------------------------------------------------
def get_trade_calendar(force=False):
    """交易日历(全量)。sina 源。"""
    if not force and _is_fresh(("cal", ""), "trade_calendar"):
        pass
    conn = _connect()
    try:
        cur = conn.execute("SELECT trade_date FROM trade_calendar ORDER BY trade_date")
        rows = [r[0] for r in cur.fetchall()]
    finally:
        conn.close()
    if force or len(rows) < 100 or (rows and rows[-1] < (date.today() + timedelta(days=7)).isoformat()):
        if _AK_IMPORTED:
            try:
                df = _call_with_retry(
                    ak.tool_trade_date_hist_sina,
                    breaker=_BREAKERS["trade_calendar"],
                )
                cal = [str(x) if not isinstance(x, str) else x for x in df["trade_date"].tolist()]
                conn = _connect()
                try:
                    conn.execute("DELETE FROM trade_calendar")
                    conn.executemany(
                        "INSERT OR IGNORE INTO trade_calendar(trade_date) VALUES(?)",
                        [(c,) for c in cal],
                    )
                    conn.commit()
                finally:
                    conn.close()
                rows = cal
                _mark_fresh(("cal", ""))
            except (DataUnavailable, DataError):
                pass  # 网络挂了用现有缓存(可能 2026 前)
    return rows


def get_last_trading_day(n=1, ref=None):
    """距今最近的第 n 个交易日(date)。"""
    cal = get_trade_calendar()
    if not cal:
        return (ref or date.today()) - timedelta(days=n)
    ref = ref or date.today()
    past = [c for c in cal if c <= ref.isoformat()]
    if not past:
        return (ref or date.today()) - timedelta(days=n)
    past.sort()
    return datetime.strptime(past[-n], "%Y-%m-%d").date()


def stock_daily(symbol6, adjust="qfq", start=None, end=None, force=False, allow_network=True):
    """股票日线。sina 源(备用 tencent)。缓存到 stock_daily 表。

    symbol6: 6 位代码。返回列: dt, open, high, low, close, volume, amount, turnover。
    """
    key = ("stock_daily", symbol6, adjust)
    start = start or "20000101"
    end = end or date.today().strftime("%Y%m%d")

    # 1) 缓存命中且新鲜 → 直接返回
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT dt,open,high,low,close,volume,amount,turnover FROM stock_daily "
            "WHERE symbol=? AND dt>=? AND dt<=? ORDER BY dt",
            (symbol6, start, end),
        ).fetchall()
    finally:
        conn.close()
    if rows and _is_fresh(key, "stock_daily") and not force:
        return _rows_to_df(rows, ("dt", "open", "high", "low", "close", "volume", "amount", "turnover"))

    # 2) 需要网络刷新
    if _AK_IMPORTED and allow_network:
        try:
            df = _call_with_retry(
                lambda: ak.stock_zh_a_daily(
                    symbol=to_akshare(symbol6), adjust=adjust,
                    start_date=start, end_date=end,
                ),
                breaker=_BREAKERS["stock_daily"],
            )
            if df is not None and len(df):
                _upsert_stock_daily(symbol6, df)
                rows = _read_stock_daily(symbol6, start, end)
                _mark_fresh(key)
        except (DataUnavailable, DataError):
            pass  # 网络失败 → 用缓存(可能缺最新)
    elif not rows:
        raise DataUnavailable(f"no cache for {symbol6} and akshare unavailable")
    return _rows_to_df(rows, ("dt", "open", "high", "low", "close", "volume", "amount", "turnover"))


def _upsert_stock_daily(symbol6, df):
    conn = _connect()
    try:
        for _, r in df.iterrows():
            conn.execute(
                "INSERT OR REPLACE INTO stock_daily"
                "(symbol,dt,open,high,low,close,volume,amount,turnover,asof) "
                "VALUES(?,?,?,?,?,?,?,?,?,datetime('now'))",
                (symbol6, str(r["date"]), _f(r.get("open")), _f(r.get("high")),
                 _f(r.get("low")), _f(r.get("close")), _f(r.get("volume")),
                 _f(r.get("amount")), _f(r.get("turnover"))),
            )
        conn.commit()
    finally:
        conn.close()


def _read_stock_daily(symbol6, start, end):
    conn = _connect()
    try:
        return conn.execute(
            "SELECT dt,open,high,low,close,volume,amount,turnover FROM stock_daily "
            "WHERE symbol=? AND dt>=? AND dt<=? ORDER BY dt", (symbol6, start, end),
        ).fetchall()
    finally:
        conn.close()


def cb_snapshot(force=False, allow_network=True):
    """全市场可转债快照(债现价/转股溢价/规模/评级/打新字段)。bond_zh_cov。"""
    key = ("cb_snap", "")
    conn = _connect()
    try:
        rows = conn.execute("SELECT * FROM cb_snapshot ORDER BY symbol").fetchall()
        cols = [d[0] for d in conn.execute("SELECT * FROM cb_snapshot LIMIT 0").description]
    finally:
        conn.close()
    if rows and _is_fresh(key, "cb_snapshot") and not force:
        return _rows_to_df(rows, cols)
    if _AK_IMPORTED and allow_network:
        try:
            df = _call_with_retry(ak.bond_zh_cov, breaker=_BREAKERS["cb_snapshot"])
            if df is not None and len(df):
                _replace_cb_snapshot(df)
                rows = _read_cb_snapshot()
                cols = _read_cb_snapshot_cols()
                _mark_fresh(key)
        except (DataUnavailable, DataError):
            pass
    return _rows_to_df(rows, cols)


def _replace_cb_snapshot(df):
    # 列名可能变化 → 全部映射为统一英文
    def g(*names, default=None):
        for n in names:
            if n in df.columns:
                return df[n]
        return pd.Series(default, index=df.index)

    sym = g("债券代码", "symbol")
    name = g("债券简称", "name")
    price = g("债现价", "price", "现价")
    premium = g("转股溢价率", "premium")
    cv = g("转股价值", "cv_value", "cv")
    cv_price = g("转股价", "convert_price", "转股价格")
    size = g("发行规模", "issue_size", "规模")
    rating = g("信用评级", "rating", "评级")
    listed = g("上市时间", "listed_date", "上市日期")
    apply_date = g("申购日期", "apply_date")
    win_rate = g("中签率", "win_rate")
    apply_limit = g("申购上限", "apply_limit")
    stock_code = g("正股代码", "stock_code")
    stock_name = g("正股名称", "stock_name")
    redeem = g("赎回", "redeem_notice", "强赎")
    maturity = g("到期日", "maturity_date", "到期时间")
    remain_years = g("剩余年限", "remain_years")

    # 债现价字段对大部分行返回面值 100 占位(akshare/eastmoney 数据问题)。
    # 用 转股价值 × (1+溢价率) 重建真实价(残差中位数 0.002, 95% 在 0.5 元内)。
    def eff_price(p, cv_v, pm_v):
        if p is not None and p != 100:
            return p
        if cv_v is not None and pm_v is not None:
            return cv_v * (1 + pm_v / 100.0)
        return p

    conn = _connect()
    try:
        conn.execute("DELETE FROM cb_snapshot")
        for i in range(len(df)):
            p_i = _f(price.iloc[i]) if price is not None else None
            cv_i = _f(cv.iloc[i]) if cv is not None else None
            pm_i = _f(premium.iloc[i]) if premium is not None else None
            conn.execute(
                "INSERT OR REPLACE INTO cb_snapshot"
                "(symbol,name,price,premium,cv,cv_price,issue_size,rating,listed,apply_date,"
                "win_rate,apply_limit,stock_code,stock_name,redeem_notice,maturity,remain_years,price_eff,asof) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))",
                (
                    str(sym.iloc[i]), _txt(name.iloc[i]) if name is not None else None,
                    p_i, pm_i, cv_i,
                    _f(cv_price.iloc[i]) if cv_price is not None else None,
                    _f(size.iloc[i]) if size is not None else None,
                    _txt(rating.iloc[i]) if rating is not None else None,
                    _txt(listed.iloc[i]) if listed is not None else None,
                    _txt(apply_date.iloc[i]) if apply_date is not None else None,
                    _f(win_rate.iloc[i]) if win_rate is not None else None,
                    _f(apply_limit.iloc[i]) if apply_limit is not None else None,
                    _txt(stock_code.iloc[i]) if stock_code is not None else None,
                    _txt(stock_name.iloc[i]) if stock_name is not None else None,
                    _txt(redeem.iloc[i]) if redeem is not None else None,
                    _txt(maturity.iloc[i]) if maturity is not None else None,
                    _f(remain_years.iloc[i]) if remain_years is not None else None,
                    eff_price(p_i, cv_i, pm_i),
                ),
            )
        conn.commit()
    finally:
        conn.close()


def _read_cb_snapshot():
    conn = _connect()
    try:
        return conn.execute("SELECT * FROM cb_snapshot ORDER BY symbol").fetchall()
    finally:
        conn.close()


def _read_cb_snapshot_cols():
    conn = _connect()
    try:
        return [d[0] for d in conn.execute("SELECT * FROM cb_snapshot LIMIT 0").description]
    finally:
        conn.close()


def cb_daily(symbol6, force=False, allow_network=True):
    """单只可转债日线。sina 源, 返回全历史。"""
    key = ("cb_daily", symbol6)
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT dt,open,high,low,close,volume FROM cb_daily WHERE symbol=? ORDER BY dt",
            (symbol6,),
        ).fetchall()
    finally:
        conn.close()
    if rows and _is_fresh(key, "cb_daily") and not force:
        return _rows_to_df(rows, ("dt", "open", "high", "low", "close", "volume"))
    if _AK_IMPORTED and allow_network:
        try:
            df = _call_with_retry(
                lambda: ak.bond_zh_hs_daily(symbol=to_akshare(symbol6)),
                breaker=_BREAKERS["cb_daily"],
            )
            if df is not None and len(df):
                conn = _connect()
                try:
                    conn.execute("DELETE FROM cb_daily WHERE symbol=?", (symbol6,))
                    for _, r in df.iterrows():
                        conn.execute(
                            "INSERT OR REPLACE INTO cb_daily(symbol,dt,open,high,low,close,volume,asof) "
                            "VALUES(?,?,?,?,?,?,?,datetime('now'))",
                            (symbol6, str(r["date"]), _f(r.get("open")), _f(r.get("high")),
                             _f(r.get("low")), _f(r.get("close")), _f(r.get("volume"))),
                        )
                    conn.commit()
                finally:
                    conn.close()
                rows = _read_cb_daily(symbol6)
                _mark_fresh(key)
        except (DataUnavailable, DataError):
            pass
    return _rows_to_df(rows, ("dt", "open", "high", "low", "close", "volume"))


def _read_cb_daily(symbol6):
    conn = _connect()
    try:
        return conn.execute(
            "SELECT dt,open,high,low,close,volume FROM cb_daily WHERE symbol=? ORDER BY dt", (symbol6,)
        ).fetchall()
    finally:
        conn.close()


def cb_value_history(symbol6, force=False, allow_network=True):
    """单只转债每日 双低分量(收盘价/纯债价值/转股价值/纯债溢价率/转股溢价率)。
    回测双低策略的核心数据源。eastmoney 源(bond_zh_cov_value_analysis)。"""
    key = ("cb_value", symbol6)
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT dt,close,pure_value,cv_value,pure_premium,premium FROM cb_value_history "
            "WHERE symbol=? ORDER BY dt", (symbol6,),
        ).fetchall()
    finally:
        conn.close()
    if rows and _is_fresh(key, "cb_value_history") and not force:
        return _rows_to_df(rows, ("dt", "close", "pure_value", "cv_value", "pure_premium", "premium"))
    if _AK_IMPORTED and allow_network:
        try:
            df = _call_with_retry(
                lambda: ak.bond_zh_cov_value_analysis(symbol=symbol6),
                breaker=_BREAKERS["cb_value_history"],
            )
            if df is not None and len(df):
                conn = _connect()
                try:
                    conn.execute("DELETE FROM cb_value_history WHERE symbol=?", (symbol6,))
                    for _, r in df.iterrows():
                        conn.execute(
                            "INSERT OR REPLACE INTO cb_value_history"
                            "(symbol,dt,close,pure_value,cv_value,pure_premium,premium,asof) "
                            "VALUES(?,?,?,?,?,?,?,datetime('now'))",
                            (symbol6, str(r["日期"]), _f(r.get("收盘价")), _f(r.get("纯债价值")),
                             _f(r.get("转股价值")), _f(r.get("纯债溢价率")), _f(r.get("转股溢价率"))),
                        )
                    conn.commit()
                finally:
                    conn.close()
                rows = _read_cb_value(symbol6)
                _mark_fresh(key)
        except (DataUnavailable, DataError):
            pass
    return _rows_to_df(rows, ("dt", "close", "pure_value", "cv_value", "pure_premium", "premium"))


def _read_cb_value(symbol6):
    conn = _connect()
    try:
        return conn.execute(
            "SELECT dt,close,pure_value,cv_value,pure_premium,premium FROM cb_value_history "
            "WHERE symbol=? ORDER BY dt", (symbol6,),
        ).fetchall()
    finally:
        conn.close()


def index_constituents(idx="000300", force=False, allow_network=True):
    """指数成分股列表。csindex 源。"""
    key = ("idx_cons", idx)
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT stock_code,stock_name FROM index_constituents WHERE idx=? ORDER BY stock_code",
            (idx,),
        ).fetchall()
    finally:
        conn.close()
    if rows and _is_fresh(key, "index_constituents") and not force:
        return [(r[0], r[1]) for r in rows]
    if _AK_IMPORTED and allow_network:
        try:
            df = _call_with_retry(
                lambda: ak.index_stock_cons_csindex(symbol=idx),
                breaker=_BREAKERS["index_constituents"],
            )
            if df is not None and len(df):
                code_col = "成分券代码" if "成分券代码" in df.columns else df.columns[4]
                name_col = "成分券名称" if "成分券名称" in df.columns else df.columns[5]
                conn = _connect()
                try:
                    conn.execute("DELETE FROM index_constituents WHERE idx=?", (idx,))
                    conn.executemany(
                        "INSERT OR REPLACE INTO index_constituents(idx,stock_code,stock_name,asof) "
                        "VALUES(?,?,?,datetime('now'))",
                        [(idx, str(r[code_col]), str(r[name_col])) for _, r in df.iterrows()],
                    )
                    conn.commit()
                finally:
                    conn.close()
                rows = [(str(r[code_col]), str(r[name_col])) for _, r in df.iterrows()]
                _mark_fresh(key)
        except (DataUnavailable, DataError):
            pass
    return rows


def stock_dividends(symbol6, force=False, allow_network=True):
    """单只股票分红历史(cninfo)。返回 (ex_div_date, pay_per_10, report)。"""
    key = ("div", symbol6)
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT ex_div_date,pay_ratio,report FROM stock_dividends WHERE symbol=? ORDER BY ex_div_date",
            (symbol6,),
        ).fetchall()
    finally:
        conn.close()
    if rows and _is_fresh(key, "stock_dividends") and not force:
        return rows
    if _AK_IMPORTED and allow_network:
        try:
            df = _call_with_retry(
                lambda: ak.stock_dividend_cninfo(symbol=symbol6),
                breaker=_BREAKERS["stock_dividends"],
            )
            if df is not None and len(df):
                ex_col = "除息日" if "除息日" in df.columns else ("派息日" if "派息日" in df.columns else None)
                ratio_col = "派息比例" if "派息比例" in df.columns else None
                report_col = "报告期" if "报告期" in df.columns else None
                conn = _connect()
                try:
                    conn.execute("DELETE FROM stock_dividends WHERE symbol=?", (symbol6,))
                    for _, r in df.iterrows():
                        conn.execute(
                            "INSERT OR REPLACE INTO stock_dividends(symbol,ex_div_date,pay_ratio,report,asof) "
                            "VALUES(?,?,?,?,datetime('now'))",
                            (symbol6,
                             str(r[ex_col]).split(" ")[0] if ex_col and not pd.isna(r[ex_col]) else None,
                             _f(r[ratio_col]) if ratio_col is not None and not pd.isna(r[ratio_col]) else None,
                             str(r[report_col]) if report_col is not None and not pd.isna(r[report_col]) else None),
                        )
                    conn.commit()
                finally:
                    conn.close()
                rows = _read_dividends(symbol6)
                _mark_fresh(key)
        except (DataUnavailable, DataError):
            pass
    return rows


def _read_dividends(symbol6):
    conn = _connect()
    try:
        return conn.execute(
            "SELECT ex_div_date,pay_ratio,report FROM stock_dividends WHERE symbol=? ORDER BY ex_div_date",
            (symbol6,),
        ).fetchall()
    finally:
        conn.close()


def dividend_yield(symbol6, price=None, ref=None):
    """近期股息率(近 12 个月每股股息 / 当前价)。返回 float 或 None。"""
    rows = stock_dividends(symbol6)
    if not rows:
        return None
    ref = ref or date.today()
    cut = (ref - timedelta(days=365)).isoformat()
    recent = [r for r in rows if r[0] and r[0] >= cut]
    if not recent:
        return None
    total_per10 = sum(r[1] for r in recent if r[1] is not None)
    if total_per10 <= 0:
        return None
    per_share = total_per10 / 10.0
    if price is None:
        bars = stock_daily(symbol6)
        if bars is None or len(bars) == 0:
            return None
        price = float(bars["close"].iloc[-1])
    if not price:
        return None
    return per_share / price


# ---------------------------------------------------------------------------
# 批量刷新
# ---------------------------------------------------------------------------
def refresh_daily(log=None):
    """交易结束后(或启动时)刷新当日数据: 交易日历 + 转债快照。"""
    log = log or (lambda m: None)
    get_trade_calendar(force=True)
    log("trade calendar refreshed")
    if is_trading_day():
        cb_snapshot(force=True)
        log("cb snapshot refreshed")
    return True


def refresh_universe_bars(symbols, adjust="qfq", progress=None):
    """批量补全股票日线(冷启动用)。逐个拉取缺失数据, 单只失败不中断。"""
    done = 0
    fails = []
    for s in symbols:
        try:
            stock_daily(s, adjust=adjust, force=True)
            done += 1
        except Exception as e:
            fails.append((s, str(e)[:80]))
        if progress:
            progress(done, len(symbols), s)
    return {"done": done, "fails": fails}


_SCAN_STATE = {"running": False, "progress": (0, 0, "")}


def refresh_dividend_scan(symbols, names=None, progress=None):
    """批量扫描股息率(月度)。冷启动慢(逐只拉 cninfo), 结果存 dividend_scan 表。"""
    _SCAN_STATE["running"] = True
    names = names or {}
    done, fails = 0, []
    try:
        for s in symbols:
            try:
                dy = dividend_yield(s)
                nm = names.get(s)
                conn = _connect()
                try:
                    if dy is not None:
                        conn.execute(
                            "INSERT OR REPLACE INTO dividend_scan(symbol,name,yield,asof) "
                            "VALUES(?,?,?,datetime('now'))", (s, nm, dy),
                        )
                    else:
                        conn.execute("DELETE FROM dividend_scan WHERE symbol=?", (s,))
                    conn.commit()
                finally:
                    conn.close()
                done += 1
            except Exception as e:
                fails.append((s, str(e)[:80]))
            _SCAN_STATE["progress"] = (done, len(symbols), s)
            if progress:
                progress(done, len(symbols), s)
    finally:
        _SCAN_STATE["running"] = False
    return {"done": done, "fails": fails}


def scan_status():
    return dict(_SCAN_STATE, is_running=_SCAN_STATE["running"])


def get_dividend_scan():
    """股息率缓存表(monthly refresh)。返回 DataFrame(symbol, name, yield)。"""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT symbol, name, yield, asof FROM dividend_scan ORDER BY yield DESC"
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return pd.DataFrame(columns=["symbol", "name", "yield", "asof"])
    return pd.DataFrame(rows, columns=["symbol", "name", "yield", "asof"])


def dividend_scan_stale(max_age_days=30):
    """股息率缓存是否过期(>max_age_days 或无数据)。"""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT asof FROM dividend_scan ORDER BY asof DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    if not row or not row[0]:
        return True
    try:
        age = (datetime.now() - datetime.strptime(row[0][:19], "%Y-%m-%d %H:%M:%S")).days
    except ValueError:
        return True
    return age > max_age_days


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _f(v):
    """安全转 float; NaN/None → None。"""
    if v is None:
        return None
    try:
        f = float(v)
        if f != f:  # NaN
            return None
        return f
    except (TypeError, ValueError):
        return None


def _txt(v):
    """安全转 str; NaN/None → None。"""
    if v is None:
        return None
    if isinstance(v, float) and v != v:
        return None
    s = str(v)
    if s in ("nan", "None", "NaT", ""):
        return None
    return s


def _rows_to_df(rows, cols):
    df = pd.DataFrame(list(rows), columns=list(cols))
    for c in ("dt", "date"):
        if c in df.columns:
            df[c] = pd.to_datetime(df[c])
    return df


def is_network_degraded():
    """任一关键端点熔断 open → 上层切精简模式。"""
    return any(b.is_degraded for b in _BREAKERS.values())


def cb_doublelow_universe():
    """已上市可转债 universe(双低轮动用)。

    过滤: 代码 11x/12x(有效转债)、上市时间已到、有真实价格(price_eff)。
    返回列含 price_eff(真实价) 与 派生字段。
    """
    df = cb_snapshot()
    if df is None or len(df) == 0:
        return df
    df = df.copy()
    sym = df["symbol"].astype(str)
    listed = df["listed"].astype(str)
    d8 = listed.str[:10].str.replace("-", "", regex=False).str[:8]
    valid_code = sym.str[:2].isin(("11", "12"))
    has_listed = d8.str.match(r"^\d{8}$")
    today = date.today().strftime("%Y%m%d")
    is_listed = has_listed & (d8 <= today)
    # price_eff 缺省时退回 price
    df["price_eff"] = df["price_eff"].fillna(df["price"])
    # 双低值 = 债价 + 转股溢价率点数(premium 为百分数数值, 如 40 = 40% → 加 40 点)
    pm = pd.to_numeric(df["premium"], errors="coerce")
    df["premium"] = pm
    df["dlow"] = df["price_eff"] + pm
    # premium 缺失 = 无实时转股数据(退市/停牌/未上市) → 不可投
    return df[valid_code & is_listed & pm.notna()].reset_index(drop=True)


def cb_subscription_queue():
    """可转债打新队列: 尚未上市、申购日期在未来或近期的转债。"""
    df = cb_snapshot()
    if df is None or len(df) == 0:
        return df
    df = df.copy()
    app = pd.to_datetime(df["apply_date"], errors="coerce")
    today = pd.Timestamp(date.today())
    # 未来 90 天内可申购的(含正在申购窗口的)
    horizon = today + pd.Timedelta(days=90)
    in_window = app.notna() & (app >= today - pd.Timedelta(days=2)) & (app <= horizon)
    df["apply_date_dt"] = app
    return df[in_window].sort_values("apply_date_dt").reset_index(drop=True)


if __name__ == "__main__":
    import sys
    print("akshare imported:", _AK_IMPORTED, "| cache db:", CACHE_DB)
    cal = get_trade_calendar(force=True)
    print("trade calendar rows:", len(cal), "| last:", cal[-1] if cal else None)
    snap = cb_snapshot(force=True)
    print("cb snapshot rows:", len(snap) if snap is not None else 0)
    bars = stock_daily("600519")
    print("600519 bars:", len(bars), "| last close:", bars["close"].iloc[-1] if len(bars) else None)
