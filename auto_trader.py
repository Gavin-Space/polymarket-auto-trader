#!/usr/bin/env python3
"""
Polymarket Auto-Trader - Full Integration
==========================================
One-click authorize -> Fully automated trading -> Web dashboard shows results.

Architecture:
  1. SecureCredentialManager  - AES-encrypted private key storage
  2. AutoTraderEngine         - Background scan + trade + manage loop
  3. WebDashboard             - Flask frontend (authorize, monitor, stop)

Security:
  - Private key encrypted with user password (PBKDF2 + Fernet)
  - Key only decrypted in memory when trading is active
  - No credentials in .env, no credentials sent externally
  - Emergency stop kills all trading instantly
  - All operations happen locally on your machine

Usage:
  python auto_trader.py
  # Open http://localhost:5000
  # Enter credentials -> Click "Authorize & Start" -> Done
"""

import os
import sys
import json
import time
import math
import logging
import hashlib
import traceback
import threading
import base64
from datetime import datetime, timedelta, timezone
from pathlib import Path
from collections import deque

# Add libs to path
LIBS_DIR = Path(__file__).parent / "libs"
sys.path.insert(0, str(LIBS_DIR))

import requests
from flask import Flask, jsonify, request, Response

# Enhanced strategy engine
from strategy_engine import (
    EnhancedScanner,
    SmartPositionSizer,
    SmartExitManager,
    PerformanceTracker,
    OrderBookAnalyzer,
    PriceHistoryAnalyzer,
    SmartMoneyTracker,
    ConfidenceScorer,
    EVCalculator,
    reset_network_state,
    set_scan_deadline,
)

# ============================================================
#  Paths & Constants
# ============================================================

# Data directory can be overridden (e.g. POLY_WORKSPACE=/data in Docker) so
# state files are persisted to a mounted volume while code stays in /app.
WORKSPACE = Path(os.environ.get("POLY_WORKSPACE", str(Path(__file__).parent)))
WORKSPACE.mkdir(parents=True, exist_ok=True)
CRED_FILE = WORKSPACE / ".encrypted_credentials"   # Encrypted credentials storage
STATE_FILE = WORKSPACE / "bot_state.json"
LOG_FILE = WORKSPACE / "bot_trades.log"
CONFIG_FILE = WORKSPACE / "trading_config.json"    # Tunable runtime parameters
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"

# ============================================================
#  Runtime Configuration Store (trading_config.json)
# ============================================================
# trading_config.json is the single source of truth for tunable parameters
# (bankroll, thresholds, strategy filters, scan interval...). This fixes the
# "different initial capital -> nonsense P&L" problem by keeping one coherent
# bankroll value that drives position sizing AND the equity baseline.
# Secrets (private key etc.) stay in the encrypted credential file.

DEFAULT_CONFIG = {
    "bankroll_usdc": 200.0,
    "max_position_pct": 0.05,
    "max_total_exposure_pct": 0.70,
    "cash_reserve_pct": 0.30,
    "max_positions": 10,
    "daily_loss_limit_pct": 0.10,
    "min_market_volume": 500.0,
    "min_order_size": 5.0,
    "scan_interval": 300,
    "min_confidence": 75,
    "min_ev_pct": 1.5,
    "min_price": 0.15,
    "max_daily_trades": 8,
    "filter_speculative": True,
    "filter_crypto_boundary": True,
    "tweetarb_tp_roi": 1.0,  # TweetArb 桶达到 +100% 收益即提前止盈（1.0=翻倍）
    "maker_bias_pct": 0.002, # 做市偏向：买单挂在 ask 下方该比例（0.002=挂低0.2%做市吃价差，研究证实做市是唯一稳健edge）
    "min_liquidity": 10,     # 跳过流动性分低于此的机会（研究：流动性差=吃单成本高）
    "expiry_annualized_floor": 20,  # ExpiryYield 要求的年化%下限（研究：临期理财真实但温和）
    "trading_mode": "dry_run",
    "strategy_expiry": True,
    "strategy_arb": True,
    "strategy_tweet": True,
    "strategy_directional": False,
    # Optional dashboard access password (protect a remote dashboard).
    # Empty = no auth (safe for localhost only). Set on remote servers.
    "web_password": "",
}

_BOOL_KEYS = {"filter_speculative", "filter_crypto_boundary", "strategy_expiry",
              "strategy_arb", "strategy_tweet", "strategy_directional"}
_SECRET_KEYS = {"web_password"}
_INT_KEYS = {"scan_interval", "max_positions", "max_daily_trades", "min_confidence"}


class ConfigStore:
    @classmethod
    def load(cls) -> dict:
        cfg = dict(DEFAULT_CONFIG)
        if CONFIG_FILE.exists():
            try:
                data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                for k in DEFAULT_CONFIG:
                    if k in data:
                        cfg[k] = data[k]
            except Exception as e:
                log.warning(f"配置读取失败，使用默认值：{e}")
        return cfg

    @classmethod
    def save(cls, cfg: dict) -> bool:
        try:
            CONFIG_FILE.write_text(
                json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            return True
        except Exception as e:
            log.error(f"配置保存失败：{e}")
            return False

# ============================================================
#  Logging
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("auto_trader")

# In-memory log ring buffer for the web UI
_log_buffer = deque(maxlen=200)

class BufferHandler(logging.Handler):
    """Ring buffer handler with polling-noise filtering.

    The Flask dev server emits an access-log line for every request, and the
    dashboard polls /api/* every few seconds — that would drown the in-app log
    stream in identical HTTP lines. Skip those so only meaningful events
    (scans, trades, errors) reach the UI.
    """
    _POLLING = ("/api/dashboard", "/api/status", "/api/markets",
                "/api/logs", "/api/network-check", "/api/scan")

    def emit(self, record):
        msg = record.getMessage()
        if record.name == "werkzeug" and ("GET " in msg or "POST " in msg):
            if any(p in msg for p in self._POLLING):
                return
        _log_buffer.append(self.format(record))

logging.getLogger().addHandler(BufferHandler())

# ============================================================
#  Secure Credential Manager
# ============================================================

class SecureCredentialManager:
    """
    Encrypt and store wallet credentials using a user-provided password.
    Uses PBKDF2 key derivation + Fernet symmetric encryption.
    The private key is NEVER stored in plain text on disk.
    """

    SALT_FILE = WORKSPACE / ".cred_salt"
    ITERATIONS = 600000

    @classmethod
    def has_credentials(cls):
        return CRED_FILE.exists() and cls.SALT_FILE.exists()

    @classmethod
    def _derive_key(cls, password: str) -> bytes:
        """Derive an encryption key from password + salt using PBKDF2."""
        try:
            from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
            from cryptography.hazmat.primitives import hashes
            from cryptography.hazmat.backends import default_backend
        except ImportError:
            # Fallback: use hashlib if cryptography not installed
            salt = cls._get_or_create_salt()
            key = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, cls.ITERATIONS)
            return base64.urlsafe_b64encode(key)

        salt = cls._get_or_create_salt()
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=cls.ITERATIONS,
            backend=default_backend(),
        )
        key = kdf.derive(password.encode())
        return base64.urlsafe_b64encode(key)

    @classmethod
    def _get_or_create_salt(cls) -> bytes:
        if cls.SALT_FILE.exists():
            return cls.SALT_FILE.read_bytes()
        salt = os.urandom(32)
        cls.SALT_FILE.write_bytes(salt)
        # Set restrictive permissions on salt file
        try:
            os.chmod(str(cls.SALT_FILE), 0o600)
        except:
            pass
        return salt

    @classmethod
    def save_credentials(cls, password: str, credentials: dict) -> bool:
        """
        Encrypt and save credentials.
        credentials = {
            'private_key': '0x...',
            'funder_address': '0x...',
            'signature_type': 2,
            'bankroll_usdc': 200,
            'telegram_token': '',  # optional
            'telegram_chat_id': '',  # optional
        }
        """
        try:
            from cryptography.fernet import Fernet
        except ImportError:
            # Fallback: simple XOR-based encryption (less secure, but works)
            return cls._save_credentials_fallback(password, credentials)

        key = cls._derive_key(password)
        f = Fernet(key)
        data = json.dumps(credentials).encode()
        encrypted = f.encrypt(data)

        CRED_FILE.write_bytes(encrypted)
        try:
            os.chmod(str(CRED_FILE), 0o600)
        except:
            pass
        log.info("凭证已加密保存成功")
        return True

    @classmethod
    def load_credentials(cls, password: str) -> dict:
        """Decrypt and return credentials. Returns None if wrong password."""
        if not cls.has_credentials():
            return None

        try:
            from cryptography.fernet import Fernet
        except ImportError:
            return cls._load_credentials_fallback(password)

        key = cls._derive_key(password)
        f = Fernet(key)

        try:
            encrypted = CRED_FILE.read_bytes()
            decrypted = f.decrypt(encrypted)
            return json.loads(decrypted.decode())
        except Exception:
            return None

    @classmethod
    def delete_credentials(cls):
        """Permanently delete stored credentials."""
        for f in [CRED_FILE, cls.SALT_FILE]:
            if f.exists():
                f.unlink()
        log.info("凭证已永久删除")

    @classmethod
    def _save_credentials_fallback(cls, password: str, credentials: dict) -> bool:
        """Fallback encryption using XOR + base64 (when cryptography lib not available)."""
        salt = cls._get_or_create_salt()
        key = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, cls.ITERATIONS)
        data = json.dumps(credentials).encode()
        # XOR with key (repeating key as needed)
        xored = bytes(d ^ key[i % len(key)] for i, d in enumerate(data))
        encoded = base64.urlsafe_b64encode(xored)
        CRED_FILE.write_bytes(encoded)
        try:
            os.chmod(str(CRED_FILE), 0o600)
        except:
            pass
        return True

    @classmethod
    def _load_credentials_fallback(cls, password: str) -> dict:
        """Fallback decryption."""
        try:
            salt = cls._get_or_create_salt()
            key = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, cls.ITERATIONS)
            encoded = CRED_FILE.read_bytes()
            xored = base64.urlsafe_b64decode(encoded)
            data = bytes(x ^ key[i % len(key)] for i, x in enumerate(xored))
            return json.loads(data.decode())
        except Exception:
            return None


# ============================================================
#  Trading Configuration (runtime, from decrypted credentials)
# ============================================================

class TradingConfig:
    """Runtime trading configuration.

    Secrets (private_key, funder, telegram) come from the encrypted credential
    file; all tunable parameters come from trading_config.json via load_from_file().
    """
    private_key = ""
    funder_address = ""
    signature_type = 2
    bankroll_usdc = 200.0
    max_position_pct = 0.05
    max_total_exposure_pct = 0.70
    cash_reserve_pct = 0.30
    max_positions = 10
    daily_loss_limit_pct = 0.10
    min_market_volume = 500.0
    min_order_size = 5.0
    scan_interval = 300  # 5 minutes
    min_confidence = 75
    min_ev_pct = 1.5
    min_price = 0.15
    max_daily_trades = 8
    filter_speculative = True
    filter_crypto_boundary = True
    tweetarb_tp_roi = 1.0
    maker_bias_pct = 0.002
    min_liquidity = 10
    expiry_annualized_floor = 20
    telegram_token = ""
    telegram_chat_id = ""
    strategy_expiry = True
    strategy_arb = True
    strategy_tweet = True
    strategy_directional = False
    trading_mode = "dry_run"  # "dry_run" or "live"
    web_password = ""  # optional dashboard Basic-Auth password for remote use

    @classmethod
    def load_from_file(cls):
        """Load all tunable parameters from trading_config.json."""
        cfg = ConfigStore.load()
        cls.bankroll_usdc = float(cfg["bankroll_usdc"])
        cls.max_position_pct = float(cfg["max_position_pct"])
        cls.max_total_exposure_pct = float(cfg["max_total_exposure_pct"])
        cls.cash_reserve_pct = float(cfg["cash_reserve_pct"])
        cls.max_positions = int(cfg["max_positions"])
        cls.daily_loss_limit_pct = float(cfg["daily_loss_limit_pct"])
        cls.min_market_volume = float(cfg["min_market_volume"])
        cls.min_order_size = float(cfg["min_order_size"])
        cls.scan_interval = int(cfg["scan_interval"])
        cls.min_confidence = float(cfg["min_confidence"])
        cls.min_ev_pct = float(cfg["min_ev_pct"])
        cls.min_price = float(cfg["min_price"])
        cls.max_daily_trades = int(cfg["max_daily_trades"])
        cls.filter_speculative = bool(cfg["filter_speculative"])
        cls.filter_crypto_boundary = bool(cfg["filter_crypto_boundary"])
        cls.tweetarb_tp_roi = float(cfg.get("tweetarb_tp_roi", 1.0))
        cls.maker_bias_pct = float(cfg.get("maker_bias_pct", 0.0))
        cls.min_liquidity = int(cfg.get("min_liquidity", 10))
        cls.expiry_annualized_floor = float(cfg.get("expiry_annualized_floor", 20))
        cls.trading_mode = cfg["trading_mode"]
        cls.strategy_expiry = bool(cfg["strategy_expiry"])
        cls.strategy_arb = bool(cfg["strategy_arb"])
        cls.strategy_tweet = bool(cfg["strategy_tweet"])
        cls.strategy_directional = bool(cfg["strategy_directional"])
        cls.web_password = str(cfg.get("web_password", ""))

    @classmethod
    def load_from_credentials(cls, creds: dict):
        """Load ONLY secrets from the encrypted credential file."""
        cls.private_key = creds.get("private_key", "")
        cls.funder_address = creds.get("funder_address", "")
        cls.signature_type = int(creds.get("signature_type", 2))
        cls.telegram_token = creds.get("telegram_token", "")
        cls.telegram_chat_id = creds.get("telegram_chat_id", "")

    @classmethod
    def is_live(cls):
        return cls.trading_mode == "live"


# ============================================================
#  Telegram Notifications
# ============================================================

def send_telegram(message: str):
    """Send a plain-text Telegram message. Plain text (no parse_mode) is used
    so `$`, `*`, `_` etc. inside market questions can never break parsing."""
    if not TradingConfig.telegram_token or not TradingConfig.telegram_chat_id:
        return
    try:
        url = f"https://api.telegram.org/bot{TradingConfig.telegram_token}/sendMessage"
        requests.post(url, json={
            "chat_id": TradingConfig.telegram_chat_id,
            "text": message,
        }, timeout=10)
    except Exception as e:
        log.warning(f"Telegram推送失败：{e}")


def _fmt_account(state) -> str:
    """Compact account summary line for push messages."""
    open_n = sum(1 for p in state.get("positions", []) if p.get("status") == "open")
    total = state.get("total_pnl", 0) or 0
    sign = "+" if total >= 0 else ""
    return f"持仓 {open_n} 个｜累计盈亏 {sign}${total:.2f}"


def strategy_cn(name: str) -> str:
    """Map internal strategy codes to Chinese display names (push + UI)."""
    return {
        "ExpiryYield": "临期理财",
        "ExpiryYield+": "临期理财+",
        "Arbitrage": "套利",
        "Arbitrage+": "套利+",
        "TweetPrediction": "推文预测",
        "TweetArb": "推文套利",
        "TweetArb+": "推文套利+",
        "Momentum": "动量策略",
        "MeanReversion": "均值回归",
        "SmartMoney": "聪明钱跟单",
    }.get(name, name)


def reason_cn(reason: str) -> str:
    """Translate SmartExitManager exit reasons to Chinese for push messages."""
    return {
        "take_profit": "止盈",
        "stop_loss": "止损",
        "expiry": "到期结算",
        "time_exit": "时间退出",
    }.get(reason, reason)


def mode_cn(mode: str) -> str:
    """Translate trading mode to Chinese display text."""
    if mode == "live":
        return "实盘"
    if mode == "dry_run":
        return "模拟盘"
    return mode or "未知"


# ============================================================
#  Gamma API (Market Data - No Auth Required)
# ============================================================

class GammaAPI:
    BASE = GAMMA_API

    # Set to True whenever a market fetch has to retry — i.e. the network is
    # flaky. The scanner then runs a "lite" pass that skips the expensive
    # per-market order-book / price-history analysis, so a scan still finishes
    # quickly and evaluates candidates on market-price data alone.
    network_retried = False

    @staticmethod
    def get_active_markets(limit=100, offset=0):
        params = {
            "active": "true", "closed": "false",
            "limit": limit, "offset": offset,
            "order": "volume24hr", "ascending": "false",
        }
        for attempt in range(3):
            try:
                r = requests.get(f"{GammaAPI.BASE}/markets", params=params, timeout=20)
                r.raise_for_status()
                return r.json()
            except Exception as e:
                GammaAPI.network_retried = True
                log.warning(f"Gamma API 第{attempt+1}/3次尝试失败：{e}")
                if attempt < 2:
                    time.sleep(3)
        return []

    @staticmethod
    def get_ending_soon(limit=100):
        params = {
            "active": "true", "closed": "false",
            "limit": limit, "order": "endDate", "ascending": "true",
        }
        try:
            r = requests.get(f"{GammaAPI.BASE}/markets", params=params, timeout=15)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.error(f"Gamma API（即将到期市场）错误：{e}")
            return []

    @staticmethod
    def get_all_active_markets(max_markets=500):
        GammaAPI.network_retried = False  # reset before this fetch cycle
        all_markets = []
        for offset in range(0, max_markets, 100):
            batch = GammaAPI.get_active_markets(limit=100, offset=offset)
            if not batch:
                break
            all_markets.extend(batch)
            if len(batch) < 100:
                break
        return all_markets

    @staticmethod
    def get_market(market_id):
        try:
            r = requests.get(f"{GammaAPI.BASE}/markets/{market_id}", timeout=10)
            if r.status_code == 200:
                return r.json()
        except:
            pass
        return None

    @staticmethod
    def parse_market(raw):
        try:
            outcomes = json.loads(raw.get("outcomes", "[]"))
            prices = json.loads(raw.get("outcomePrices", "[]"))
            token_ids = json.loads(raw.get("clobTokenIds", "[]"))

            result = {
                "id": raw.get("id", ""),
                "question": raw.get("question", ""),
                "slug": raw.get("slug", ""),
                "condition_id": raw.get("conditionId", ""),
                "outcomes": outcomes,
                "prices": [float(p) for p in prices],
                "token_ids": token_ids,
                "volume": float(raw.get("volume", 0) or 0),
                "volume_24h": float(raw.get("volume24hr", 0) or 0),
                "liquidity": float(raw.get("liquidity", 0) or 0),
                "end_date": raw.get("endDate", ""),
                "active": raw.get("active", False),
                "closed": raw.get("closed", False),
                "image": raw.get("image", "") or raw.get("icon", ""),
            }

            for i, outcome in enumerate(outcomes):
                ol = outcome.lower().strip()
                if ol in ("yes", "up", "over"):
                    result["yes_price"] = float(prices[i]) if i < len(prices) else 0
                    result["yes_token"] = token_ids[i] if i < len(token_ids) else ""
                elif ol in ("no", "down", "under"):
                    result["no_price"] = float(prices[i]) if i < len(prices) else 0
                    result["no_token"] = token_ids[i] if i < len(token_ids) else ""

            if len(outcomes) == 2:
                if "yes_price" not in result:
                    result["yes_price"] = float(prices[0]) if prices else 0
                    result["yes_token"] = token_ids[0] if token_ids else ""
                if "no_price" not in result:
                    result["no_price"] = float(prices[1]) if len(prices) > 1 else 0
                    result["no_token"] = token_ids[1] if len(token_ids) > 1 else ""

            return result
        except:
            return None


# ============================================================
#  Network Diagnostics
# ============================================================

def network_diagnostic() -> dict:
    """Run a full network diagnostic and return results.
    Checks: proxy env, proxy reachability, DNS, Polymarket connectivity.
    """
    import socket

    results = {
        "timestamp": datetime.now().strftime("%H:%M:%S"),
        "proxy": {"configured": False, "address": None, "reachable": False},
        "dns": {"clob": None, "gamma": None},
        "polymarket": {"clob_direct": False, "clob_proxy": False, "gamma_direct": False, "gamma_proxy": False},
        "diagnosis": "",
        "suggestion": "",
    }

    # 1. Check proxy environment variables
    proxy_url = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") or \
                os.environ.get("https_proxy") or os.environ.get("http_proxy")
    if proxy_url:
        results["proxy"]["configured"] = True
        results["proxy"]["address"] = proxy_url
        # Parse host:port
        try:
            # Remove protocol prefix
            addr = proxy_url.replace("http://", "").replace("https://", "")
            host, port_str = addr.split(":")
            port = int(port_str)
            # Test TCP connection to proxy
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(2)
            try:
                sock.connect((host, port))
                results["proxy"]["reachable"] = True
            except (socket.timeout, ConnectionRefusedError, OSError):
                results["proxy"]["reachable"] = False
            finally:
                sock.close()
        except Exception:
            results["proxy"]["reachable"] = False

    # 2. DNS resolution
    try:
        results["dns"]["clob"] = socket.gethostbyname("clob.polymarket.com")
    except Exception:
        results["dns"]["clob"] = None
    try:
        results["dns"]["gamma"] = socket.gethostbyname("gamma-api.polymarket.com")
    except Exception:
        results["dns"]["gamma"] = None

    # 3. Test Polymarket connectivity (both direct and via proxy)
    test_urls = [
        ("clob_direct", f"{CLOB_HOST}/time", None),
        ("gamma_direct", f"{GAMMA_API}/markets?limit=1", None),
    ]
    if proxy_url:
        test_urls.extend([
            ("clob_proxy", f"{CLOB_HOST}/time", proxy_url),
            ("gamma_proxy", f"{GAMMA_API}/markets?limit=1", proxy_url),
        ])

    for key, url, proxy in test_urls:
        try:
            proxies = {"https": proxy, "http": proxy} if proxy else None
            r = requests.get(url, timeout=8, proxies=proxies)
            results["polymarket"][key] = (r.status_code == 200)
        except Exception:
            results["polymarket"][key] = False

    # 4. Generate diagnosis
    proxy_cfg = results["proxy"]["configured"]
    proxy_ok = results["proxy"]["reachable"]
    dns_ok = results["dns"]["clob"] is not None
    direct_ok = results["polymarket"]["clob_direct"] or results["polymarket"]["gamma_direct"]
    proxy_polymarket_ok = results["polymarket"].get("clob_proxy", False) or results["polymarket"].get("gamma_proxy", False)

    if not dns_ok:
        results["diagnosis"] = "DNS 解析失败——无法解析 Polymarket 域名"
        results["suggestion"] = "检查网络连接或 DNS 设置，可能需要配置代理/VPN"
    elif proxy_cfg and not proxy_ok:
        results["diagnosis"] = f"代理已配置 ({results['proxy']['address']}) 但代理端口不可达——代理软件未运行"
        results["suggestion"] = "请启动你的代理软件（如 Clash/v2ray），确保监听端口正确，然后重试授权"
    elif proxy_cfg and proxy_ok and not proxy_polymarket_ok:
        results["diagnosis"] = f"代理可达但无法通过代理访问 Polymarket——代理节点可能不支持或被限制"
        results["suggestion"] = "尝试切换代理节点，或检查代理规则是否放行了 polymarket.com"
    elif not proxy_cfg and not direct_ok:
        results["diagnosis"] = "未配置代理且直连无法访问 Polymarket——网络被限制"
        results["suggestion"] = "需要配置代理/VPN 来访问 Polymarket。设置 HTTP_PROXY 和 HTTPS_PROXY 环境变量"
    elif direct_ok or proxy_polymarket_ok:
        results["diagnosis"] = "网络连接正常——Polymarket 可访问"
        results["suggestion"] = "如果授权仍失败，请检查私钥和钱包地址是否正确"
    else:
        results["diagnosis"] = "网络诊断 inconclusive——请检查所有连接"
        results["suggestion"] = "尝试启动代理后重试，或使用仅查看模式进入仪表盘"

    return results


# ============================================================
#  CLOB Trader (Authenticated Order Placement)
# ============================================================

class CLOBTrader:
    """Wrapper around py-clob-client for authenticated trading."""

    def __init__(self):
        self.client = None
        self.initialized = False
        self.api_creds = None

    def init(self):
        if not TradingConfig.private_key:
            log.warning("无可用私钥——以只读模式运行")
            return False

        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds
        except ImportError:
            log.error("py-clob-client 未安装。请运行：pip install py-clob-client")
            return False

        try:
            kwargs = {
                "host": CLOB_HOST,
                "key": TradingConfig.private_key,
                "chain_id": 137,
            }

            if TradingConfig.signature_type > 0:
                kwargs["signature_type"] = TradingConfig.signature_type
                if TradingConfig.funder_address:
                    kwargs["funder"] = TradingConfig.funder_address

            self.client = ClobClient(**kwargs)

            log.info("正在从钱包派生 API 凭证...")
            creds = self.client.create_or_derive_api_creds()
            self.client.set_api_creds(creds)
            self.api_creds = creds

            self.initialized = True
            log.info("CLOB 客户端初始化成功")
            log.info(f"  API Key: {creds.api_key[:8]}...")
            log.info(f"  钱包: {TradingConfig.funder_address[:10]}...")
            return True

        except Exception as e:
            log.error(f"CLOB 客户端初始化失败：{e}")
            traceback.print_exc()
            return False

    def get_midpoint(self, token_id):
        if not self.client:
            return None
        try:
            return float(self.client.get_midpoint(token_id))
        except:
            return None

    def place_limit_order(self, token_id, price, size, side="BUY"):
        if not self.initialized:
            log.warning("CLOB 客户端未初始化——无法下单")
            return None

        try:
            from py_clob_client.clob_types import OrderArgs, OrderType
            from py_clob_client.order_builder.constants import BUY, SELL

            price = round(float(price), 4)
            size = round(float(size), 2)
            if price <= 0 or price >= 1:
                log.error(f"无效价格：{price}")
                return None
            if size < TradingConfig.min_order_size:
                size = TradingConfig.min_order_size

            order_args = OrderArgs(
                token_id=token_id, price=price, size=size,
                side=BUY if side.upper() == "BUY" else SELL,
            )

            if TradingConfig.is_live():
                signed_order = self.client.create_order(order_args)
                response = self.client.post_order(signed_order, OrderType.GTC)
                log.info(f"订单已提交：{side} {size} @ ${price} | token={token_id[:12]}... | resp={response}")
                return response
            else:
                log.info(f"[模拟运行] 将下单：{side} {size} @ ${price} | token={token_id[:12]}...")
                return {"success": True, "orderID": "dry_" + str(int(time.time())), "dry_run": True}

        except Exception as e:
            log.error(f"下单失败：{e}")
            traceback.print_exc()
            return None

    def cancel_all(self):
        if not self.initialized or not TradingConfig.is_live():
            return
        try:
            self.client.cancel_all()
            log.info("所有挂单已取消")
        except Exception as e:
            log.error(f"取消挂单失败：{e}")

    def get_balances(self):
        if not self.initialized:
            return {}
        try:
            balance = self.client.get_balance_allowance()
            return {"usdc": float(balance.get("balance", 0))}
        except:
            return {}


# ============================================================
#  Risk Manager
# ============================================================

class RiskManager:

    @staticmethod
    def max_position_size():
        return TradingConfig.bankroll_usdc * TradingConfig.max_position_pct

    @staticmethod
    def max_total_exposure():
        return TradingConfig.bankroll_usdc * TradingConfig.max_total_exposure_pct

    @staticmethod
    def cash_reserve():
        return TradingConfig.bankroll_usdc * TradingConfig.cash_reserve_pct

    @staticmethod
    def check_daily_loss(state):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if state.get("daily_loss_hit") == today:
            return True
        daily = state.get("daily_pnl", {}).get(today, {})
        realized_loss = daily.get("realized", 0)
        if realized_loss < 0 and abs(realized_loss) >= TradingConfig.bankroll_usdc * TradingConfig.daily_loss_limit_pct:
            state["daily_loss_hit"] = today
            BotState.save(state)
            log.warning(f"已触发每日亏损上限：{realized_loss:.2f} USDC")
            send_telegram(f"⛔ 止损触发：每日亏损上限（{realized_loss:.2f} USDC），交易已暂停。")
            return True
        return False

    @staticmethod
    def check_exposure(state):
        positions = state.get("positions", [])
        total_exposure = sum(p.get("cost_usdc", 0) for p in positions if p.get("status") == "open")
        max_exposure = RiskManager.max_total_exposure()
        available = max_exposure - total_exposure
        return available, total_exposure, max_exposure

    @staticmethod
    def check_position_count(state):
        open_count = sum(1 for p in state.get("positions", []) if p.get("status") == "open")
        return open_count < TradingConfig.max_positions

    @staticmethod
    def can_trade(state):
        if RiskManager.check_daily_loss(state):
            return False, "Daily loss limit hit"
        available, total, max_exp = RiskManager.check_exposure(state)
        if available <= 0:
            return False, f"Max exposure reached ({total:.0f}/{max_exp:.0f})"
        if not RiskManager.check_position_count(state):
            return False, f"Max positions reached ({TradingConfig.max_positions})"
        return True, "OK"


# ============================================================
#  Bot State Persistence
# ============================================================

class BotState:
    DEFAULT = {
        "positions": [],
        "daily_pnl": {},
        "last_scan": None,
        "daily_loss_hit": None,
        "total_pnl": 0,
        "total_trades": 0,
        "equity_curve": [],
    }

    # Guard concurrent read/write between the trading thread and Flask handlers
    # (review M9).
    _lock = threading.RLock()

    @classmethod
    def load(cls):
        with cls._lock:
            if STATE_FILE.exists():
                try:
                    with open(STATE_FILE, "r", encoding="utf-8") as f:
                        state = json.load(f)
                    for k, v in cls.DEFAULT.items():
                        state.setdefault(k, v)
                    return state
                except Exception:
                    pass
            return json.loads(json.dumps(cls.DEFAULT))

    @classmethod
    def save(cls, state):
        try:
            tmp = STATE_FILE.with_suffix(".json.tmp")
            with cls._lock:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(state, f, indent=2, ensure_ascii=False, default=str)
                os.replace(tmp, STATE_FILE)  # atomic on the same filesystem
        except Exception as e:
            log.error(f"状态保存失败：{e}")


# ============================================================
#  Auto-Trader Engine (Background Thread)
# ============================================================

class AutoTraderEngine:
    """
    The core engine that runs in a background thread.
    Cycles: scan markets -> find opportunities -> execute trades -> manage positions.
    Uses EnhancedScanner with multi-factor confidence scoring and EV filtering.
    """

    def __init__(self):
        self.trader = CLOBTrader()
        self.state = BotState.load()
        self.is_running = False
        self.is_authorized = False
        self.thread = None
        self.last_scan_time = None
        self.last_error = None
        self.last_auth_error = None
        self.cycle_count = 0
        self.current_action = "idle"
        self.scanner = EnhancedScanner()
        self.perf_tracker = PerformanceTracker()

        # Run time tracking: start_time set on start(), cleared on stop()
        self.run_start_time = None  # epoch seconds or None

        # Consecutive loss tracking for adaptive risk control
        self.consecutive_losses = 0
        self.cooldown_until = None  # epoch seconds; no new trades before this

        # Per-strategy risk control: if a strategy loses 3+ times in a row,
        # pause it for the session so one bad strategy can't drag the P&L down.
        self.strategy_loss_streak = {}  # strategy -> consecutive losses
        self.strategy_blocked = set()   # strategies currently paused

        # Daily Telegram summary (sent once per day)
        self.last_daily_summary_date = None

        # Sleep countdown: epoch seconds when the next scan cycle begins
        self.sleep_until = None

        # Cache for web dashboard
        self.cached_markets = []
        self.cached_opportunities = []
        self.cached_stats = {}

    def get_run_seconds(self):
        """Return elapsed seconds since trading started, or 0 if not running."""
        if self.run_start_time is None:
            return 0
        return int(time.time() - self.run_start_time)

    def get_sleep_remaining(self):
        """Seconds left before the next scan cycle, or 0 if not sleeping."""
        if self.sleep_until is None:
            return 0
        return max(0, int(self.sleep_until - time.time()))

    def _record_equity_point(self):
        """Record current balance to equity curve for charting."""
        positions = self.state.get("positions", [])
        closed = [p for p in positions if p.get("status") in ("won", "lost") or p.get("status", "").startswith("closed")]
        total_pnl = sum(p.get("pnl_usdc", 0) for p in closed)
        open_exposure = sum(p.get("cost_usdc", 0) for p in positions if p.get("status") == "open")
        current_balance = TradingConfig.bankroll_usdc + total_pnl

        curve = self.state.setdefault("equity_curve", [])
        curve.append({
            "t": datetime.now(timezone.utc).isoformat(),
            "balance": round(current_balance, 2),
            "pnl": round(total_pnl, 2),
            "exposure": round(open_exposure, 2),
        })
        # Keep last 500 points to avoid unbounded growth
        if len(curve) > 500:
            self.state["equity_curve"] = curve[-500:]

    def authorize(self, password: str, skip_wallet: bool = False) -> tuple:
        """Load credentials and initialize trader.
        Returns (success: bool, error: str or None).
        """
        self.last_auth_error = None

        # Step 1: Decrypt credentials
        creds = SecureCredentialManager.load_credentials(password)
        if not creds:
            self.last_auth_error = "密码错误，无法解密凭证。请确认你输入的是配置时设置的密码。"
            log.error("授权失败：密码错误")
            return False, self.last_auth_error

        # Runtime tunables come from trading_config.json; secrets from credentials
        TradingConfig.load_from_file()
        TradingConfig.load_from_credentials(creds)

        if not TradingConfig.private_key:
            self.last_auth_error = "凭证中没有私钥，请重新配置。"
            log.error("凭证中无私钥")
            return False, self.last_auth_error

        # Step 2: Initialize CLOB client (requires network)
        if skip_wallet:
            log.info("以仅查看模式授权（跳过钱包初始化）")
            self.is_authorized = True
            self.trader.initialized = False
            log.info("=" * 60)
            log.info(f"已授权（仅查看模式）- 模式：{mode_cn(TradingConfig.trading_mode)}")
            log.info(f"  初始资金：${TradingConfig.bankroll_usdc}")
            log.info(f"  钱包：{TradingConfig.funder_address[:10]}...")
            log.info(f"  交易功能已禁用，直到钱包完全初始化。")
            log.info("=" * 60)
            return True, None

        # Step 2: Pre-check network connectivity to Polymarket
        log.info("钱包初始化前执行网络预检...")
        net_diag = network_diagnostic()
        log.info(f"网络诊断：代理={'已配置' if net_diag['proxy']['configured'] else '无'}, "
                 f"代理可达={net_diag['proxy']['reachable']}, "
                 f"Polymarket直连={net_diag['polymarket']['clob_direct'] or net_diag['polymarket']['gamma_direct']}, "
                 f"诊断={net_diag['diagnosis']}")

        # If network is definitely down, fail fast with a specific message
        net_ok = (net_diag["polymarket"]["clob_direct"] or
                  net_diag["polymarket"]["gamma_direct"] or
                  net_diag["polymarket"].get("clob_proxy", False) or
                  net_diag["polymarket"].get("gamma_proxy", False))

        if not net_ok:
            proxy_info = ""
            if net_diag["proxy"]["configured"]:
                proxy_info = f"\n\n代理地址: {net_diag['proxy']['address']}"
                if not net_diag["proxy"]["reachable"]:
                    proxy_info += "\n⚠️ 代理端口不可达——代理软件未运行！请启动代理后重试。"
                else:
                    proxy_info += "\n代理端口可达，但无法通过代理访问 Polymarket。请尝试切换代理节点。"
            else:
                proxy_info = "\n\n⚠️ 未配置代理环境变量。Polymarket 在中国大陆需要代理/VPN 才能访问。"

            self.last_auth_error = (
                f"网络诊断结果：{net_diag['diagnosis']}{proxy_info}\n\n"
                f"建议：{net_diag['suggestion']}\n\n"
                f"你可以选择「仅查看模式」先进入仪表盘，网络恢复后再启用交易，"
                f"或点击「网络诊断」查看详细连接信息。"
            )
            log.error(f"网络预检失败：{net_diag['diagnosis']}")
            return False, self.last_auth_error

        # Step 3: Initialize CLOB client (requires network)
        log.info("网络正常，正在连接 Polymarket CLOB 钱包...")
        ok = self.trader.init()
        if not ok:
            self.last_auth_error = (
                "钱包初始化失败——已确认网络可达 Polymarket，但 CLOB 客户端初始化失败。\n"
                "可能原因：\n"
                "1. 私钥或钱包地址不正确\n"
                "2. Polymarket CLOB API 临时不可用\n"
                "3. API 凭证派生失败\n\n"
                "你可以选择「仅查看模式」先进入仪表盘，稍后重试。"
            )
            log.error("CLOB 客户端初始化失败——钱包连接错误")
            return False, self.last_auth_error

        self.is_authorized = True
        log.info("=" * 60)
        log.info(f"已授权 - 模式：{mode_cn(TradingConfig.trading_mode)}")
        log.info(f"  初始资金：${TradingConfig.bankroll_usdc}")
        log.info(f"  最大仓位：${RiskManager.max_position_size():.2f}")
        log.info(f"  最大持仓数：{TradingConfig.max_positions}")
        log.info(f"  Cash reserve: {TradingConfig.cash_reserve_pct:.0%}")
        log.info(f"  Strategies: Expiry={'ON' if TradingConfig.strategy_expiry else 'OFF'}, "
                 f"Arb={'ON' if TradingConfig.strategy_arb else 'OFF'}, "
                 f"Tweet={'ON' if TradingConfig.strategy_tweet else 'OFF'}")
        log.info("=" * 60)

        send_telegram(f"✅ 授权成功\n模式：{mode_cn(TradingConfig.trading_mode)}\n初始资金：${TradingConfig.bankroll_usdc}")
        return True, None

    def start(self):
        """Start the auto-trading loop in a background thread."""
        if not self.is_authorized:
            log.error("Cannot start: not authorized")
            return False
        if self.is_running:
            log.warning("Already running")
            return True

        self.is_running = True
        self.run_start_time = time.time()  # Start timing
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()
        log.info("Auto-trading engine STARTED")
        return True

    def stop(self):
        """Emergency stop - halts all trading immediately."""
        self.is_running = False
        self.run_start_time = None  # Reset run timer
        self.sleep_until = None     # Cancel any pending sleep countdown
        self.current_action = "stopped"
        log.warning("EMERGENCY STOP - Trading halted")

        # Cancel all open orders
        if self.trader.initialized:
            self.trader.cancel_all()

        send_telegram("🛑 紧急停止——所有交易已暂停，挂单已取消。")

    def _run_loop(self):
        """Main trading loop."""
        log.info("交易循环已启动")
        send_telegram("🚀 自动交易已启动。系统将自动扫描机会、执行交易、管理持仓。")

        while self.is_running:
            try:
                self._run_cycle()
                # Interruptible sleep: check is_running every second so the
                # emergency stop responds instantly instead of blocking for
                # the whole scan_interval. sleep_until drives the second-level
                # countdown shown on the dashboard.
                self.current_action = "sleeping"
                self.sleep_until = time.time() + TradingConfig.scan_interval
                while self.is_running:
                    remaining = self.sleep_until - time.time()
                    if remaining <= 0:
                        break
                    time.sleep(min(1.0, remaining))
                self.sleep_until = None
            except Exception as e:
                log.error(f"Cycle error: {e}")
                traceback.print_exc()
                self.last_error = str(e)
                self.sleep_until = None
                time.sleep(60)

        self.current_action = "stopped"
        self.sleep_until = None
        log.info("Trading loop stopped")

    def _run_cycle(self):
        """One full cycle: check positions -> scan -> execute -> check again."""
        self.cycle_count += 1
        self.current_action = "checking positions"
        log.info(f"\n{'='*60}")
        log.info(f"Cycle #{self.cycle_count} at {datetime.now().strftime('%H:%M:%S')}")
        log.info(f"{'='*60}")

        # 1. Check existing positions for resolution / take profit
        self._check_positions()

        # 2. Scan for new opportunities
        self.current_action = "scanning markets"
        opportunities = self._scan_markets()

        # 3. Execute trades automatically
        self.current_action = "executing trades"
        if opportunities:
            self._execute_opportunities(opportunities)

        # 4. Final position check
        self.current_action = "updating positions"
        self._check_positions()

        # 5. Update stats
        self._update_stats()

        # 6. Record equity curve point
        self._record_equity_point()

        self.current_action = "idle"
        self.last_scan_time = datetime.now(timezone.utc).isoformat()
        self.state["last_scan"] = self.last_scan_time
        BotState.save(self.state)

        # Daily Telegram summary — once per day
        _today = now.strftime("%Y-%m-%d")
        if _today != self.last_daily_summary_date:
            self.last_daily_summary_date = _today
            self._send_daily_summary()

        open_count = sum(1 for p in self.state.get("positions", []) if p.get("status") == "open")
        log.info(f"Cycle #{self.cycle_count} complete. Open positions: {open_count}")

    def _send_daily_summary(self):
        """Push a one-line daily account summary to Telegram."""
        state = self.state
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        daily = state.get("daily_pnl", {}).get(today, {"realized": 0, "trades": 0})
        total = state.get("total_pnl", 0) or 0
        open_n = sum(1 for p in state.get("positions", []) if p.get("status") == "open")
        sign = "+" if total >= 0 else ""
        send_telegram(
            f"📊 每日总结 {today}\n"
            f"  今日 {daily.get('trades', 0)} 笔｜今日盈亏 {daily.get('realized', 0):+.2f} USD\n"
            f"  累计盈亏 {sign}${total:.2f}｜持仓 {open_n} 个\n"
            f"  模式：{mode_cn(TradingConfig.trading_mode)}"
        )

    def _scan_markets(self):
        """Scan all markets using EnhancedScanner with multi-factor analysis."""
        log.info("Scanning markets with enhanced engine...")
        reset_network_state()  # clear the per-scan circuit breaker
        set_scan_deadline(60)  # hard 60s budget for the whole scan

        now = datetime.now(timezone.utc)
        all_raw = GammaAPI.get_all_active_markets(max_markets=300)
        ending_raw = GammaAPI.get_ending_soon(limit=100)

        # Parse and dedup
        seen_ids = set()
        markets = []
        all_candidates = []

        for raw in all_raw:
            m = self.scanner.parse_market(raw)
            if m and m["id"] not in seen_ids:
                markets.append(m)
                all_candidates.append(m)
                seen_ids.add(m["id"])

        for raw in ending_raw:
            m = self.scanner.parse_market(raw)
            if m and m["id"] not in seen_ids:
                all_candidates.append(m)
                seen_ids.add(m["id"])

        self.cached_markets = markets
        log.info(f"  Fetched {len(markets)} active + {len(ending_raw)} ending soon = {len(all_candidates)} total")

        # Sync tunable thresholds from TradingConfig into the scanner
        self.scanner.min_confidence = TradingConfig.min_confidence
        self.scanner.filter_speculative = TradingConfig.filter_speculative
        self.scanner.filter_crypto_boundary = TradingConfig.filter_crypto_boundary
        self.scanner.min_liquidity = TradingConfig.min_liquidity
        self.scanner.expiry_annualized_floor = TradingConfig.expiry_annualized_floor
        # Flaky network → lite scan (skip order-book / history analysis)
        self.scanner.skip_orderbook = GammaAPI.network_retried
        self.scanner.strategy_toggles = {
            "expiry": TradingConfig.strategy_expiry,
            "arb": TradingConfig.strategy_arb,
            "tweet": TradingConfig.strategy_tweet,
            "directional": TradingConfig.strategy_directional,
        }
        EVCalculator.MIN_EV_PCT = TradingConfig.min_ev_pct

        # Run enhanced scanner
        opportunities = self.scanner.scan(all_candidates, now)

        # Filter out already-held positions
        held_ids = {
            p.get("market_id") for p in self.state.get("positions", [])
            if p.get("status") == "open"
        }
        opportunities = [o for o in opportunities if o.get("market_id") not in held_ids]

        self.cached_opportunities = opportunities
        log.info(f"  Found {len(opportunities)} high-confidence opportunities (positive EV only)")

        # Log top opportunities with confidence and EV
        for o in opportunities[:8]:
            ev = o.get("ev", {})
            conf = o.get("confidence", 0)
            ev_pct = ev.get("ev_pct", 0)
            log.info(
                f"    [{o['strategy']}] {o.get('side', '?')} @ ${o['price']:.4f} "
                f"| Conf: {conf:.0f} | EV: ${ev.get('ev_usdc', 0):.2f} ({ev_pct:.1f}%) "
                f"| {o['question'][:45]}..."
            )

        return opportunities

    def _execute_opportunities(self, opportunities):
        """Automatically execute trades for found opportunities."""
        executed = 0
        max_per_cycle = 3  # Don't open too many positions at once

        # Check cooldown period
        now_ts = time.time()
        if self.cooldown_until is not None and now_ts < self.cooldown_until:
            remaining = int((self.cooldown_until - now_ts) / 60)
            log.info(f"  [COOLDOWN] Skipping new trades. {remaining} minutes remaining "
                     f"({self.consecutive_losses} consecutive losses)")
            return 0

        # Enforce daily trade cap
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        trades_today = self.state.get("daily_pnl", {}).get(today, {}).get("trades", 0)
        if TradingConfig.max_daily_trades > 0 and trades_today >= TradingConfig.max_daily_trades:
            log.info(f"  [DAILY CAP] {trades_today}/{TradingConfig.max_daily_trades} trades today reached.")
            return 0

        for opp in opportunities:
            if executed >= max_per_cycle:
                log.info(f"  Reached max {max_per_cycle} new positions per cycle")
                break

            strategy = opp.get("strategy", "")
            # Skip strategies paused for repeated losses
            if strategy in self.strategy_blocked:
                continue

            can, reason = RiskManager.can_trade(self.state)
            if not can:
                log.info(f"  Cannot trade: {reason}")
                break

            try:
                before = sum(1 for p in self.state.get("positions", []) if p.get("status") == "open")
                result = self._execute_single(opp)
                if result:
                    executed += 1
                    after = sum(1 for p in self.state.get("positions", []) if p.get("status") == "open")
                    opened_n = max(after - before, 1)  # TweetArb opens N buckets at once
                    _today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                    _d = self.state["daily_pnl"].setdefault(_today, {"realized": 0, "trades": 0})
                    _d["trades"] = _d.get("trades", 0) + opened_n
                    time.sleep(2)  # Rate limit
            except Exception as e:
                log.error(f"  Execution failed: {e}")

        log.info(f"  Executed {executed} new positions this cycle")
        return executed

    def _execute_single(self, opp):
        """Execute a single opportunity with smart position sizing."""
        strategy = opp.get("strategy", "")
        max_pos = RiskManager.max_position_size()
        ev = opp.get("ev", {})
        confidence = opp.get("confidence", 50)

        # Use SmartPositionSizer for optimal sizing
        if ev and not ev.get("skip"):
            size_usdc = SmartPositionSizer.calculate_with_confidence(
                ev, confidence, TradingConfig.bankroll_usdc,
                max_pct=TradingConfig.max_position_pct,
                strategy=strategy,
            )
        else:
            log.info(f"  Skip {strategy}: negative EV")
            return None

        log.info(f"  [EXEC] {strategy} {opp.get('side', '?')} | "
                 f"Conf: {confidence:.0f} | EV: ${ev.get('ev_usdc', 0):.2f} ({ev.get('ev_pct', 0):.1f}%) | "
                 f"Size: ${size_usdc:.2f} | {opp['question'][:45]}...")

        if strategy in ("ExpiryYield+", "ExpiryYield"):
            token_id = opp.get("token_id", "")
            if not token_id:
                return None

            order_price = round(opp["price"], 3)
            # Maker bias: post slightly below the ask to capture the spread.
            # Research (SSRN 6443103): the only robust Polymarket edge is
            # providing liquidity with limit orders; takers systematically lose.
            # Default 0 = buy at the ask (unchanged behaviour). Applies only to
            # non-arbitrage strategies — arbs need certain fills.
            if TradingConfig.maker_bias_pct > 0:
                order_price = round(opp["price"] * (1 - TradingConfig.maker_bias_pct), 4)
            shares = max(size_usdc / order_price, TradingConfig.min_order_size)

            resp = self.trader.place_limit_order(token_id, order_price, shares, "BUY")
            if resp and resp.get("success"):
                pos = {
                    "id": resp.get("orderID", str(int(time.time()))),
                    "strategy": strategy,
                    "market_id": opp["market_id"],
                    "question": opp["question"],
                    "side": opp["side"],
                    "token_id": token_id,
                    "entry_price": order_price,
                    "shares": shares,
                    "cost_usdc": round(shares * order_price, 2),
                    "opened_at": datetime.now(timezone.utc).isoformat(),
                    "end_date": opp.get("end_date", ""),
                    "status": "open",
                    "dry_run": resp.get("dry_run", False),
                    "confidence": confidence,
                    "ev_pct": ev.get("ev_pct", 0),
                    "peak_price": order_price,
                }
                self.state["positions"].append(pos)
                BotState.save(self.state)

                pnl_text = f"年化{opp.get('annualized_yield', 0):.0f}%" if opp.get("annualized_yield") else f"利润{opp.get('profit_pct', 0):.1%}"
                log.info(f"  [开仓] {opp['side']} {shares:.0f}份 @ ${order_price} (${shares*order_price:.2f}) | {pnl_text} | 置信度{confidence:.0f}")
                _days = opp.get("days_to_expiry", 0)
                _ann = opp.get("annualized_yield", 0) or 0
                send_telegram(
                    f"📈 开仓·{strategy_cn(strategy)}\n"
                    f"  {opp['question'][:55]}\n"
                    f"  方向：{opp['side']} @ ${order_price:.3f}｜{shares:.0f}份（${shares*order_price:.2f}）\n"
                    f"  置信度 {confidence:.0f}｜EV +${ev.get('ev_usdc', 0):.2f}（{ev.get('ev_pct',0):.1f}%）\n"
                    f"  ⏳ 到期 {_days:.1f} 天｜年化 {_ann:.0f}%"
                )
                return pos
            return None

        elif strategy in ("Arbitrage+", "Arbitrage"):
            # Buy both YES and NO with EQUAL SHARES. At resolution the larger
            # share count pays $1 each → payout = Q, so profit = Q*(1-yes-no)
            # is genuinely guaranteed whenever yes+no < 1 (the old equal-DOLLAR
            # sizing did NOT guarantee a profit — see review S2).
            size_usdc = min(size_usdc, max_pos * 0.5)
            _q = size_usdc / (opp["yes_price"] + opp["no_price"])
            yes_shares = no_shares = max(_q, TradingConfig.min_order_size)

            log.info(f"  [EXEC] ARB: YES@${opp['yes_price']:.3f} + NO@${opp['no_price']:.3f} = ${opp['price']:.3f}")

            resp_yes = self.trader.place_limit_order(opp["yes_token"], round(opp["yes_price"], 3), yes_shares, "BUY")
            resp_no = self.trader.place_limit_order(opp["no_token"], round(opp["no_price"], 3), no_shares, "BUY")

            if resp_yes and resp_no and resp_yes.get("success") and resp_no.get("success"):
                cost = round(yes_shares * opp["yes_price"] + no_shares * opp["no_price"], 2)
                # Equal shares → whichever side pays, payout = shares (Q)
                guaranteed = round(yes_shares * (1 - opp["yes_price"] - opp["no_price"]), 2)
                pos = {
                    "id": resp_yes.get("orderID", str(int(time.time()))),
                    "strategy": strategy,
                    "market_id": opp["market_id"],
                    "question": opp["question"],
                    "yes_token": opp["yes_token"],
                    "no_token": opp["no_token"],
                    "yes_price": opp["yes_price"],
                    "no_price": opp["no_price"],
                    "yes_shares": yes_shares,
                    "no_shares": no_shares,
                    "cost_usdc": cost,
                    "guaranteed_return": guaranteed,
                    "opened_at": datetime.now(timezone.utc).isoformat(),
                    "status": "open",
                    "dry_run": resp_yes.get("dry_run", False),
                    "confidence": confidence,
                    "ev_pct": ev.get("ev_pct", 0),
                }
                self.state["positions"].append(pos)
                BotState.save(self.state)
                log.info(f"  [套利开仓] 成本${cost:.2f}，保底利润${guaranteed:.2f}")
                send_telegram(f"🔀 套利开仓\n  {opp['question'][:60]}\n  成本：${cost:.2f}，保底利润：${guaranteed:.2f}")
                return pos
            return None

        elif strategy in ("TweetArb+", "TweetArb"):
            if not opp.get("buckets"):
                return None
            total_usdc = min(size_usdc, max_pos * 0.5)
            # EQUAL SHARES across buckets → payout = Q (whichever bucket wins),
            # so arb profit = Q − total_cost is genuinely guaranteed. The old
            # per-DOLLAR sizing ($per bucket) broke the guarantee (review S4).
            total_yes = opp.get("price", 0) or sum(b.get("yes_price", 0) for b in opp["buckets"])
            _q = (total_usdc / total_yes) if total_yes > 0 else 0
            shares = max(_q, TradingConfig.min_order_size)
            log.info(f"  [EXEC] Tweet ARB: {len(opp['buckets'])} buckets, {shares:.0f} shares each (equal-Q)")

            # Skip buckets already held — the opp uses a synthetic market_id
            # ("tweet_arb_{period}") that can never match a real held bucket,
            # so dedup must be by token (review M8).
            held_tokens = {
                p.get("token_id") for p in self.state.get("positions", [])
                if p.get("status") == "open" and p.get("token_id")
            }

            positions_opened = []
            for bucket in opp["buckets"]:
                if not bucket.get("yes_token"):
                    continue
                if bucket["yes_token"] in held_tokens:
                    continue  # already held
                yp = bucket["yes_price"]
                if yp <= 0 or yp >= 1:
                    continue
                # Respect max_positions per bucket — can_trade() is checked once
                # per opportunity, so a multi-bucket arb would otherwise blow past
                # the position cap (observed: 33 open vs max_positions=10).
                open_count = sum(1 for p in self.state.get("positions", []) if p.get("status") == "open")
                if open_count >= TradingConfig.max_positions:
                    log.info(f"  [MAX POS] 已达最大持仓数 {TradingConfig.max_positions}，停止开仓")
                    break
                resp = self.trader.place_limit_order(bucket["yes_token"], round(yp, 3), shares, "BUY")
                if resp and resp.get("success"):
                    pos = {
                        "id": resp.get("orderID", str(int(time.time()))),
                        "strategy": strategy,
                        "market_id": bucket.get("market_id", ""),
                        "question": bucket["question"],
                        "side": "YES (ARB)",
                        "token_id": bucket["yes_token"],
                        "entry_price": round(yp, 3),
                        "shares": shares,
                        "cost_usdc": round(shares * round(yp, 3), 2),
                        "opened_at": datetime.now(timezone.utc).isoformat(),
                        "end_date": bucket.get("end_date", ""),
                        "status": "open",
                        "dry_run": resp.get("dry_run", False),
                        "confidence": confidence,
                        "ev_pct": ev.get("ev_pct", 0),
                        "peak_price": round(yp, 3),
                    }
                    self.state["positions"].append(pos)
                    positions_opened.append(pos)
                    time.sleep(1)

            if positions_opened:
                BotState.save(self.state)
                total_cost = sum(p["cost_usdc"] for p in positions_opened)
                _guaranteed = max(shares - total_cost, 0)
                log.info(f"  [推文套利] 开仓{len(positions_opened)}个桶，成本${total_cost:.2f}，保底${_guaranteed:.2f}")
                send_telegram(f"🔀 推文套利开仓\n  {len(positions_opened)}个桶，成本：${total_cost:.2f}，保底利润：${_guaranteed:.2f}")
                return positions_opened[0]
            return None

        elif strategy in ("Momentum", "MeanReversion", "SmartMoney"):
            token_id = opp.get("token_id", "")
            if not token_id:
                return None

            order_price = round(opp["price"], 3)
            # Maker bias: post slightly below the ask to capture the spread.
            # Research (SSRN 6443103): the only robust Polymarket edge is
            # providing liquidity with limit orders; takers systematically lose.
            # Default 0 = buy at the ask (unchanged behaviour). Applies only to
            # non-arbitrage strategies — arbs need certain fills.
            if TradingConfig.maker_bias_pct > 0:
                order_price = round(opp["price"] * (1 - TradingConfig.maker_bias_pct), 4)
            shares = max(size_usdc / order_price, TradingConfig.min_order_size)

            resp = self.trader.place_limit_order(token_id, order_price, shares, "BUY")
            if resp and resp.get("success"):
                pos = {
                    "id": resp.get("orderID", str(int(time.time()))),
                    "strategy": strategy,
                    "market_id": opp["market_id"],
                    "question": opp["question"],
                    "side": opp.get("side", "YES"),
                    "token_id": token_id,
                    "entry_price": order_price,
                    "shares": shares,
                    "cost_usdc": round(shares * order_price, 2),
                    "opened_at": datetime.now(timezone.utc).isoformat(),
                    "end_date": opp.get("end_date", ""),
                    "status": "open",
                    "dry_run": resp.get("dry_run", False),
                    "confidence": confidence,
                    "ev_pct": ev.get("ev_pct", 0),
                    "peak_price": order_price,
                    "analysis": opp.get("analysis", {}),
                }
                self.state["positions"].append(pos)
                BotState.save(self.state)
                log.info(f"  [开仓] {strategy}：{opp.get('side', '?')} {shares:.0f}份 @ ${order_price} | 置信度{confidence:.0f} | 期望收益${ev.get('ev_usdc', 0):.2f}")
                send_telegram(
                    f"📈 开仓·{strategy_cn(strategy)}\n"
                    f"  {opp['question'][:55]}\n"
                    f"  方向：{opp.get('side', '?')} @ ${order_price:.3f}｜{shares:.0f}份（${shares*order_price:.2f}）\n"
                    f"  置信度 {confidence:.0f}｜EV +${ev.get('ev_usdc', 0):.2f}（{ev.get('ev_pct',0):.1f}%）"
                )
                return pos
            return None

        return None

    def _check_positions(self):
        """Check open positions for resolution and smart exit."""
        # Sync tunable exit threshold from TradingConfig
        SmartExitManager.TWEETARB_TP_ROI = TradingConfig.tweetarb_tp_roi

        positions = self.state.get("positions", [])
        if not positions:
            return

        now = datetime.now(timezone.utc)
        updated = False

        for pos in positions:
            if pos.get("status") != "open":
                continue

            market_id = pos.get("market_id", "")
            if not market_id:
                continue

            # Optimization: only query resolution once a position is near its end
            # date (a weekly market can't resolve mid-week), cutting API load.
            _near_expiry = True
            _end_str = pos.get("end_date", "")
            if _end_str:
                try:
                    _end_dt = datetime.fromisoformat(_end_str.replace("Z", "+00:00"))
                    _near_expiry = (_end_dt - now) <= timedelta(days=1)
                except Exception:
                    _near_expiry = True

            # Check if market resolved
            m_raw = GammaAPI.get_market(market_id) if _near_expiry else None
            if m_raw and m_raw.get("closed"):
                outcomes = json.loads(m_raw.get("outcomes", "[]"))
                prices = json.loads(m_raw.get("outcomePrices", "[]"))
                tokens = json.loads(m_raw.get("clobTokenIds", "[]"))
                our_token = pos.get("token_id", "")

                # Determine the winning outcome by TOKEN ID (the token that
                # settled at $1.00), NOT by outcome name. This fixes:
                #  - TweetArb+ buckets (side="YES (ARB)" never matched "Yes")
                #  - Up/Down crypto markets (outcome "Up" vs stored side "YES")
                won = False
                for i in range(len(outcomes)):
                    price = float(prices[i]) if i < len(prices) else 0
                    if price < 0.99:
                        continue
                    tok = tokens[i] if i < len(tokens) else ""
                    if our_token and tok:
                        won = (tok == our_token)
                    else:
                        # Fallback (legacy data): match by outcome name
                        won = outcomes[i].upper() in (pos.get("side", "").upper(), "YES (ARB)")
                    break

                # Arbitrage+ holds BOTH sides; one side always pays $1 → won.
                # (TweetArb+ buckets are NOT this case — they are individual
                # YES markets and must be judged by their own token.)
                is_arb = pos.get("strategy", "") in ("Arbitrage+", "Arbitrage")
                if is_arb:
                    won = True

                if won:
                    cost = pos.get("cost_usdc", 0)
                    if is_arb:
                        # True arb settlement: the LARGER share count pays $1 each
                        # (max(Y,N)); the other side is worthless.
                        pnl = max(pos.get("yes_shares", 0), pos.get("no_shares", 0)) - cost
                    else:
                        pnl = pos.get("shares", 0) - cost

                    pos["status"] = "won"
                    pos["resolved_at"] = now.isoformat()
                    pos["pnl_usdc"] = round(pnl, 2)
                    updated = True

                    today = now.strftime("%Y-%m-%d")
                    daily = self.state["daily_pnl"].setdefault(today, {"realized": 0, "trades": 0})
                    daily["realized"] += pnl
                    # daily["trades"] (opens) is counted in _execute_opportunities
                    self.state["total_pnl"] = self.state.get("total_pnl", 0) + pnl
                    self.state["total_trades"] = self.state.get("total_trades", 0) + 1

                    # Track performance
                    self.perf_tracker.record_trade(pos.get("strategy", ""), pnl)

                    log.info(f"  [盈利] {pos['question'][:50]}... 盈亏：+${pnl:.2f}")
                    send_telegram(f"🎉 持仓盈利\n  {pos['question'][:55]}\n  盈亏：+${pnl:.2f}\n  📊 {_fmt_account(self.state)}")

                    # Reset consecutive losses on win
                    self.consecutive_losses = 0
                    self.cooldown_until = None
                    self._record_strategy_result(pos.get("strategy", ""), True)
                else:
                    pos["status"] = "lost"
                    pos["resolved_at"] = now.isoformat()
                    pos["pnl_usdc"] = -pos.get("cost_usdc", 0)
                    updated = True

                    today = now.strftime("%Y-%m-%d")
                    daily = self.state["daily_pnl"].setdefault(today, {"realized": 0, "trades": 0})
                    daily["realized"] += pos["pnl_usdc"]
                    # daily["trades"] (opens) is counted in _execute_opportunities
                    self.state["total_pnl"] = self.state.get("total_pnl", 0) + pos["pnl_usdc"]
                    self.state["total_trades"] = self.state.get("total_trades", 0) + 1

                    self.perf_tracker.record_trade(pos.get("strategy", ""), pos["pnl_usdc"])

                    log.warning(f"  [亏损] {pos['question'][:50]}... 盈亏：-${pos.get('cost_usdc', 0):.2f}")
                    send_telegram(f"❌ 持仓亏损\n  {pos['question'][:55]}\n  盈亏：-${pos.get('cost_usdc', 0):.2f}\n  📊 {_fmt_account(self.state)}")

                    # Consecutive loss tracking
                    self.consecutive_losses += 1
                    self._record_strategy_result(pos.get("strategy", ""), False)
                    if self.consecutive_losses >= 3:
                        cooldown_mins = 30 * self.consecutive_losses  # 90min for 3, 120min for 4...
                        self.cooldown_until = time.time() + cooldown_mins * 60
                        log.warning(f"  [冷却期] 连续亏损{self.consecutive_losses}次，"
                                    f"暂停新交易{cooldown_mins}分钟。")
                        send_telegram(f"⚠️ 冷却期触发\n连续亏损{self.consecutive_losses}次，"
                                      f"新交易暂停{cooldown_mins}分钟。")
                continue

            # Smart exit check using SmartExitManager
            token_id = pos.get("token_id", "")
            if token_id:
                # Live mode uses the authenticated CLOB client midpoint; dry-run /
                # view mode (trader not initialized) falls back to the public
                # order-book midpoint so take-profit & stop-loss still simulate.
                current_price = None
                if self.trader.initialized:
                    current_price = self.trader.get_midpoint(token_id)
                if current_price is None:
                    current_price = OrderBookAnalyzer.get_midpoint(token_id)
                if not current_price:
                    current_price = pos.get("entry_price", 0)

                peak_price = pos.get("peak_price", pos.get("entry_price", 0))
                # Update peak price
                if current_price > peak_price:
                    pos["peak_price"] = current_price
                    peak_price = current_price
                    updated = True

                should_exit, reason, exit_price = SmartExitManager.should_exit(
                    pos, current_price, peak_price
                )

                if should_exit and exit_price:
                    entry_price = pos.get("entry_price", 0)
                    shares = pos.get("shares", 0)
                    profit = (exit_price - entry_price) * shares

                    if profit > 0:
                        log.info(f"  [{reason.upper()}] {pos['question'][:50]}... +${profit:.2f}")
                        if TradingConfig.is_live():
                            self.trader.place_limit_order(token_id, round(exit_price, 3), shares, "SELL")

                        pos["status"] = f"closed_{reason}"
                        pos["closed_at"] = now.isoformat()
                        pos["exit_price"] = round(exit_price, 4)
                        pos["pnl_usdc"] = round(profit, 2)
                        updated = True

                        today = now.strftime("%Y-%m-%d")
                        daily = self.state["daily_pnl"].setdefault(today, {"realized": 0, "trades": 0})
                        daily["realized"] += profit
                        # daily["trades"] (opens) is counted in _execute_opportunities
                        self.state["total_pnl"] = self.state.get("total_pnl", 0) + profit
                        self.state["total_trades"] = self.state.get("total_trades", 0) + 1

                        self.perf_tracker.record_trade(pos.get("strategy", ""), profit)

                        send_telegram(f"💰 持仓平仓（{reason_cn(reason)}）\n  {pos['question'][:55]}\n  盈亏：+${profit:.2f}\n  📊 {_fmt_account(self.state)}")

                        # Reset consecutive losses on profitable close
                        self.consecutive_losses = 0
                        self.cooldown_until = None
                        self._record_strategy_result(pos.get("strategy", ""), True)
                    elif reason == "stop_loss":
                        log.warning(f"  [STOP LOSS] {pos['question'][:50]}... -${abs(profit):.2f}")
                        if TradingConfig.is_live():
                            self.trader.place_limit_order(token_id, round(exit_price, 3), shares, "SELL")

                        pos["status"] = "closed_stop_loss"
                        pos["closed_at"] = now.isoformat()
                        pos["exit_price"] = round(exit_price, 4)
                        pos["pnl_usdc"] = round(profit, 2)
                        updated = True

                        today = now.strftime("%Y-%m-%d")
                        daily = self.state["daily_pnl"].setdefault(today, {"realized": 0, "trades": 0})
                        daily["realized"] += profit
                        # daily["trades"] (opens) is counted in _execute_opportunities
                        self.state["total_pnl"] = self.state.get("total_pnl", 0) + profit
                        self.state["total_trades"] = self.state.get("total_trades", 0) + 1

                        self.perf_tracker.record_trade(pos.get("strategy", ""), profit)

                        send_telegram(f"🛑 止损平仓\n  {pos['question'][:55]}\n  盈亏：${profit:.2f}\n  📊 {_fmt_account(self.state)}")

                        # Track consecutive losses for stop-loss exits
                        self.consecutive_losses += 1
                        self._record_strategy_result(pos.get("strategy", ""), False)
                        if self.consecutive_losses >= 3:
                            cooldown_mins = 30 * self.consecutive_losses
                            self.cooldown_until = time.time() + cooldown_mins * 60
                            log.warning(f"  [冷却期] 连续亏损{self.consecutive_losses}次，"
                                        f"暂停新交易{cooldown_mins}分钟。")

        if updated:
            BotState.save(self.state)

        open_count = sum(1 for p in positions if p.get("status") == "open")
        log.info(f"  Positions: {open_count} open, {len(positions)} total")
        if self.state.get("total_trades", 0) > 0:
            log.info(f"  Performance:\n{self.perf_tracker.summary()}")

    def _record_strategy_result(self, strategy, won):
        """Track per-strategy win/loss streaks; auto-pause a strategy after 3
        consecutive losses so one bad strategy can't drag the P&L down."""
        if not strategy:
            return
        if won:
            self.strategy_loss_streak.pop(strategy, None)
            self.strategy_blocked.discard(strategy)
        else:
            streak = self.strategy_loss_streak.get(strategy, 0) + 1
            self.strategy_loss_streak[strategy] = streak
            if streak >= 3 and strategy not in self.strategy_blocked:
                self.strategy_blocked.add(strategy)
                log.warning(f"  [策略风控] {strategy_cn(strategy)} 连续亏损{streak}次，"
                            f"本会话暂停该策略（避免拖累盈亏）")
                send_telegram(f"⛔ 策略风控：{strategy_cn(strategy)} 连续亏损{streak}次，"
                              f"已暂停该策略。可在「设置」中重置统计后恢复。")

    def reset_stats(self):
        """Clear trading stats & positions (stale test data with mismatched
        bankroll makes P&L meaningless). Seeded with the current bankroll."""
        self.state["positions"] = []
        self.state["daily_pnl"] = {}
        self.state["daily_loss_hit"] = None
        self.state["total_pnl"] = 0
        self.state["total_trades"] = 0
        self.state["equity_curve"] = []
        self.consecutive_losses = 0
        self.cooldown_until = None
        self.strategy_loss_streak = {}
        self.strategy_blocked = set()
        self._record_equity_point()  # seed the curve at the current bankroll
        BotState.save(self.state)    # save AFTER seeding so the file keeps the first point
        log.info("统计数据已重置（以当前配置的初始资金为基准）")

    def _update_stats(self):
        """Update cached statistics for the dashboard."""
        positions = self.state.get("positions", [])
        open_pos = [p for p in positions if p.get("status") == "open"]
        won = [p for p in positions if p.get("status") == "won"]
        lost = [p for p in positions if p.get("status") == "lost"]
        closed = [p for p in positions if p.get("status", "").startswith("closed")]

        total_exposure = sum(p.get("cost_usdc", 0) for p in open_pos)
        total_pnl = sum(p.get("pnl_usdc", 0) for p in won + lost + closed)

        # FIX: current balance = initial bankroll + realized P&L (never below 0)
        current_balance = max(TradingConfig.bankroll_usdc + total_pnl, 0)

        # Win rate
        total_closed = len(won) + len(lost) + len(closed)
        wins = len(won) + sum(1 for p in closed if p.get("pnl_usdc", 0) > 0)
        losses = len(lost) + sum(1 for p in closed if p.get("pnl_usdc", 0) <= 0)
        win_rate = (wins / total_closed * 100) if total_closed > 0 else 0

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        daily = self.state.get("daily_pnl", {}).get(today, {"realized": 0, "trades": 0})

        # Check cooldown
        now_ts = time.time()
        in_cooldown = self.cooldown_until is not None and now_ts < self.cooldown_until
        cooldown_remaining = max(0, int(self.cooldown_until - now_ts)) if in_cooldown else 0

        self.cached_stats = {
            "mode": TradingConfig.trading_mode,
            "bankroll": TradingConfig.bankroll_usdc,  # Initial bankroll (reference)
            "current_balance": round(current_balance, 2),  # Actual current balance
            "total_exposure": round(total_exposure, 2),
            "available_cash": round(current_balance - total_exposure, 2),
            "open_positions": len(open_pos),
            "won_positions": len(won),
            "lost_positions": len(lost),
            "max_positions": TradingConfig.max_positions,
            "daily_pnl": daily.get("realized", 0),
            "daily_trades": daily.get("trades", 0),
            "total_pnl": round(total_pnl, 2),
            "total_trades": self.state.get("total_trades", 0),
            "daily_loss_hit": self.state.get("daily_loss_hit"),
            "win_rate": round(win_rate, 1),
            "consecutive_losses": self.consecutive_losses,
            "in_cooldown": in_cooldown,
            "cooldown_remaining": cooldown_remaining,
            "run_seconds": self.get_run_seconds(),
        }


# ============================================================
#  Global Engine Instance
# ============================================================

# Load runtime configuration (trading_config.json) before the engine starts
TradingConfig.load_from_file()
# Ensure the config file exists on disk as a reference / for editing
if not CONFIG_FILE.exists():
    ConfigStore.save(ConfigStore.load())

engine = AutoTraderEngine()


# ============================================================
#  Flask Web App
# ============================================================

app = Flask(__name__)


@app.before_request
def _require_web_auth():
    """Protect the dashboard with Basic Auth when web_password is configured
    (recommended when exposing the dashboard on a remote server)."""
    pw = TradingConfig.web_password
    if not pw:
        return None
    auth = request.authorization
    if auth and auth.username == "admin" and auth.password == pw:
        return None
    return Response("PolyAuto 需要访问密码（请在设置中配置 web_password）",
                    401, {"WWW-Authenticate": 'Basic realm="PolyAuto"'})




@app.route("/")
def index():
    return HTML_TEMPLATE


@app.route("/api/network-check")
def api_network_check():
    """Run network diagnostic and return results."""
    try:
        diag = network_diagnostic()
        return jsonify({"success": True, "diagnostic": diag})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/status")
def api_status():
    """Get current system status."""
    return jsonify({
        "has_credentials": SecureCredentialManager.has_credentials(),
        "is_authorized": engine.is_authorized,
        "is_running": engine.is_running,
        "current_action": engine.current_action,
        "cycle_count": engine.cycle_count,
        "last_scan": engine.last_scan_time or engine.state.get("last_scan"),
        "last_error": engine.last_error,
        "mode": TradingConfig.trading_mode if engine.is_authorized else "not_authorized",
        "run_seconds": engine.get_run_seconds(),
        "is_sleeping": engine.current_action == "sleeping",
        "sleep_remaining": engine.get_sleep_remaining(),
        "scan_interval": TradingConfig.scan_interval,
        "network_ok": not GammaAPI.network_retried,
    })


@app.route("/api/authorize", methods=["POST"])
def api_authorize():
    """Authorize the bot with encrypted credentials."""
    data = request.json
    password = data.get("password", "")
    skip_wallet = data.get("skip_wallet", False)

    if not password:
        return jsonify({"success": False, "error": "请输入密码"}), 400

    if not SecureCredentialManager.has_credentials():
        return jsonify({"success": False, "error": "未找到凭证，请先配置。"}), 400

    success, error = engine.authorize(password, skip_wallet=skip_wallet)
    if success:
        if not skip_wallet:
            engine.start()
        return jsonify({"success": True, "message": "授权成功，交易已自动启动" if not skip_wallet else "已进入查看模式"})
    else:
        return jsonify({"success": False, "error": error or "授权失败", "can_view_only": True}), 401


@app.route("/api/setup", methods=["POST"])
def api_setup():
    """First-time setup: save encrypted credentials."""
    data = request.json
    password = data.get("password", "")
    private_key = data.get("private_key", "")
    funder_address = data.get("funder_address", "")
    bankroll = data.get("bankroll_usdc", 200)
    signature_type = data.get("signature_type", 2)
    trading_mode = data.get("trading_mode", "dry_run")
    telegram_token = data.get("telegram_token", "")
    telegram_chat_id = data.get("telegram_chat_id", "")

    if not password or len(password) < 6:
        return jsonify({"success": False, "error": "密码至少6位"}), 400
    if not private_key or not private_key.startswith("0x"):
        return jsonify({"success": False, "error": "私钥格式错误（必须以0x开头）"}), 400
    if not funder_address or not funder_address.startswith("0x"):
        return jsonify({"success": False, "error": "钱包地址格式错误（必须以0x开头）"}), 400

    credentials = {
        "private_key": private_key,
        "funder_address": funder_address,
        "signature_type": signature_type,
        "bankroll_usdc": float(bankroll),
        "trading_mode": trading_mode,
        "telegram_token": telegram_token,
        "telegram_chat_id": telegram_chat_id,
    }

    if SecureCredentialManager.save_credentials(password, credentials):
        return jsonify({"success": True, "message": "凭证已加密保存。请输入密码授权以启动自动交易。"})
    else:
        return jsonify({"success": False, "error": "凭证保存失败"}), 500


@app.route("/api/start", methods=["POST"])
def api_start():
    """Start the auto-trading engine."""
    if not engine.is_authorized:
        return jsonify({"success": False, "error": "尚未授权"}), 401
    if engine.start():
        return jsonify({"success": True, "message": "交易已启动"})
    return jsonify({"success": False, "error": "已在运行或启动失败"}), 400


@app.route("/api/stop", methods=["POST"])
def api_stop():
    """Emergency stop - halt all trading immediately."""
    engine.stop()
    return jsonify({"success": True, "message": "紧急停止——所有交易已暂停，挂单已取消"})


@app.route("/api/delete-credentials", methods=["POST"])
def api_delete_credentials():
    """Permanently delete stored credentials."""
    engine.stop()
    engine.is_authorized = False
    SecureCredentialManager.delete_credentials()
    return jsonify({"success": True, "message": "凭证已永久删除"})


@app.route("/api/config", methods=["GET"])
def api_config_get():
    """Return the current runtime configuration (secrets masked)."""
    cfg = ConfigStore.load()
    for k in _SECRET_KEYS:
        cfg[k] = ""  # never leak the dashboard password back to the client
    return jsonify({"success": True, "config": cfg})


@app.route("/api/config", methods=["POST"])
def api_config_set():
    """Update tunable runtime parameters (saved to trading_config.json)."""
    data = request.json or {}
    cfg = ConfigStore.load()
    old_bankroll = float(cfg.get("bankroll_usdc", 200))
    bankroll_changed = False

    for k in DEFAULT_CONFIG:
        if k in data:
            v = data[k]
            if k in _BOOL_KEYS:
                v = bool(v)
            elif k in _INT_KEYS:
                v = int(v)
            elif isinstance(cfg[k], float):
                v = float(v)
            cfg[k] = v
            if k == "bankroll_usdc":
                bankroll_changed = abs(float(v) - old_bankroll) > 1e-9

    if not ConfigStore.save(cfg):
        return jsonify({"success": False, "error": "配置保存失败"}), 500

    TradingConfig.load_from_file()

    reset_msg = ""
    if bankroll_changed:
        # Position sizing & equity baseline are driven by bankroll; a change
        # invalidates historical equity data, so reset to a clean baseline.
        engine.reset_stats()
        reset_msg = "（初始资金已变更，统计已重置）"

    log.info(f"配置已更新并保存：{json.dumps(cfg, ensure_ascii=False)}")
    return jsonify({
        "success": True,
        "message": f"配置已保存{reset_msg}",
        "config": cfg,
    })


@app.route("/api/reset-stats", methods=["POST"])
def api_reset_stats():
    """Reset trading stats & positions (used after changing initial capital)."""
    engine.stop()
    engine.reset_stats()
    return jsonify({"success": True, "message": "统计与持仓已重置（以当前初始资金为基准）"})


@app.route("/api/sell", methods=["POST"])
def api_sell():
    """Manually sell an open position at the current mid price."""
    data = request.json or {}
    pos_id = data.get("id", "")
    now = datetime.now(timezone.utc)
    for pos in engine.state.get("positions", []):
        if pos.get("id") == pos_id and pos.get("status") == "open":
            token_id = pos.get("token_id", "")
            shares = pos.get("shares", 0)
            # Live uses the authenticated CLOB midpoint; dry-run uses public
            price = None
            if engine.trader.initialized:
                price = engine.trader.get_midpoint(token_id)
            if price is None:
                price = OrderBookAnalyzer.get_midpoint(token_id)
            if price is None or price <= 0:
                price = pos.get("entry_price", 0) or 0

            if TradingConfig.is_live() and token_id:
                engine.trader.place_limit_order(token_id, round(price, 3), shares, "SELL")

            entry = pos.get("entry_price", 0) or 0
            pnl = (price - entry) * shares
            pos["status"] = "closed_manual"
            pos["exit_price"] = round(price, 4)
            pos["pnl_usdc"] = round(pnl, 2)
            pos["closed_at"] = now.isoformat()

            today = now.strftime("%Y-%m-%d")
            daily = engine.state["daily_pnl"].setdefault(today, {"realized": 0, "trades": 0})
            daily["realized"] += pnl
            engine.state["total_pnl"] = engine.state.get("total_pnl", 0) + pnl
            engine.state["total_trades"] = engine.state.get("total_trades", 0) + 1
            engine.perf_tracker.record_trade(pos.get("strategy", ""), pnl)
            engine._record_strategy_result(pos.get("strategy", ""), pnl > 0)
            BotState.save(engine.state)

            sign = "+" if pnl >= 0 else ""
            send_telegram(f"🖐️ 手动卖出\n  {pos['question'][:55]}\n  价格 @${price:.3f}｜盈亏 {sign}${pnl:.2f}\n  📊 {_fmt_account(engine.state)}")
            log.info(f"  [手动卖出] {pos['question'][:50]}... @${price:.3f} 盈亏 {sign}${pnl:.2f}")
            return jsonify({"success": True, "message": f"已卖出 @${price:.3f}，盈亏 {sign}${pnl:.2f}"})

    return jsonify({"success": False, "error": "未找到该持仓或已平仓"}), 404


def _build_strategy_breakdown(positions):
    """Build per-strategy performance breakdown."""
    breakdown = {}
    for p in positions:
        strat = p.get("strategy", "Unknown")
        if strat not in breakdown:
            breakdown[strat] = {"trades": 0, "wins": 0, "losses": 0, "pnl": 0, "exposure": 0}
        b = breakdown[strat]
        pnl = p.get("pnl_usdc", 0)
        b["pnl"] += pnl
        if p.get("status") == "open":
            b["exposure"] += p.get("cost_usdc", 0)
            continue  # open positions don't count toward win rate / trade count
        b["trades"] += 1
        if pnl > 0:
            b["wins"] += 1
        elif pnl < 0:
            b["losses"] += 1
    # Round and add win rate
    result = []
    for strat, b in sorted(breakdown.items(), key=lambda x: x[1]["pnl"], reverse=True):
        wr = (b["wins"] / b["trades"] * 100) if b["trades"] > 0 else 0
        result.append({
            "strategy": strat,
            "trades": b["trades"],
            "wins": b["wins"],
            "losses": b["losses"],
            "win_rate": round(wr, 1),
            "pnl": round(b["pnl"], 2),
            "exposure": round(b["exposure"], 2),
        })
    return result


@app.route("/api/dashboard")
def api_dashboard():
    """Return all dashboard data."""
    state = engine.state
    logs = list(_log_buffer)[-100:]
    positions = state.get("positions", [])

    if engine.is_authorized:
        stats = engine.cached_stats or {}
        stats["mode"] = TradingConfig.trading_mode
        stats["bankroll"] = TradingConfig.bankroll_usdc
        # Always return LIVE run_seconds / sleep countdown, not the cached value
        stats["run_seconds"] = engine.get_run_seconds()
        stats["is_sleeping"] = engine.current_action == "sleeping"
        stats["sleep_remaining"] = engine.get_sleep_remaining()
        stats["scan_interval"] = TradingConfig.scan_interval
    else:
        stats = {
            "mode": "not_authorized",
            "bankroll": 0,
            "current_balance": 0,
            "total_exposure": 0,
            "available_cash": 0,
            "open_positions": 0,
            "won_positions": 0,
            "lost_positions": 0,
            "max_positions": 10,
            "daily_pnl": 0,
            "daily_trades": 0,
            "total_pnl": 0,
            "total_trades": 0,
            "win_rate": 0,
            "consecutive_losses": 0,
            "in_cooldown": False,
            "cooldown_remaining": 0,
            "run_seconds": 0,
            "is_sleeping": False,
            "sleep_remaining": 0,
            "scan_interval": 300,
        }

    return jsonify({
        "status": {
            **stats,
            "scanning": engine.current_action in ("scanning markets", "checking positions", "executing trades", "updating positions"),
            "is_running": engine.is_running,
            "is_authorized": engine.is_authorized,
            "current_action": engine.current_action,
            "cycle_count": engine.cycle_count,
            "last_scan": engine.last_scan_time or state.get("last_scan"),
            "has_credentials": SecureCredentialManager.has_credentials(),
            "network_ok": not GammaAPI.network_retried,
        },
        "positions": positions,
        "opportunities": engine.cached_opportunities,
        "markets_count": len(engine.cached_markets),
        "logs": logs,
        "equity_curve": state.get("equity_curve", []),
        "strategy_breakdown": _build_strategy_breakdown(positions),
    })


@app.route("/api/markets")
def api_markets():
    q = request.args.get("q", "").lower()
    limit = int(request.args.get("limit", 50))
    markets = engine.cached_markets
    if q:
        markets = [m for m in markets if q in m["question"].lower()]
    return jsonify(markets[:limit])


@app.route("/api/scan", methods=["POST"])
def api_scan():
    """Trigger a manual scan (even without authorization)."""
    if engine.is_authorized and engine.is_running:
        return jsonify({"status": "already_running"})
    # Run a one-time scan in background
    def _quick_scan():
        engine.current_action = "scanning markets"
        try:
            engine._scan_markets()
        finally:
            engine.current_action = "idle"
    t = threading.Thread(target=_quick_scan, daemon=True)
    t.start()
    return jsonify({"status": "scan_started"})


# ============================================================
#  HTML Template
# ============================================================

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>PolyAuto - 全自动交易系统</title>
<style>
/* ===== Theme System ===== */
:root {
  /* Dark theme (default) — softened with gray, less stark contrast */
  --bg: #14181f;
  --bg2: #1b2028;
  --card: #212732;
  --card-hover: #262d38;
  --border: #333b47;
  --border-light: #262d37;
  --text: #d7dee8;
  --text-secondary: #aeb6c2;
  --muted: #7d8794;
  --primary: #2f81f7;
  --primary-d: #1f6feb;
  --primary-l: rgba(47,129,247,0.12);
  --green: #3fb950;
  --green-l: rgba(63,185,80,0.12);
  --red: #f85149;
  --red-l: rgba(248,81,73,0.12);
  --orange: #d29922;
  --orange-l: rgba(210,153,34,0.12);
  --purple: #a371f7;
  --purple-l: rgba(163,113,247,0.12);
  --teal: #2dd4bf;
  --teal-l: rgba(45,212,191,0.12);
  --radius: 12px;
  --radius-sm: 8px;
  --shadow: 0 2px 12px rgba(0,0,0,0.3);
  --shadow-lg: 0 8px 30px rgba(0,0,0,0.4);
  --log-bg: #0d1117;
  --modal-overlay: rgba(0,0,0,0.7);
  --gradient-header: linear-gradient(135deg, #161b22 0%, #0d1117 100%);
}

[data-theme="light"] {
  /* Light theme — softened with gray, less stark white */
  --bg: #eef0f3;
  --bg2: #f8f9fb;
  --card: #f8f9fb;
  --card-hover: #eef1f4;
  --border: #d4dae1;
  --border-light: #e3e7ec;
  --text: #262c34;
  --text-secondary: #4a525d;
  --muted: #6d7682;
  --primary: #0969da;
  --primary-d: #0550ae;
  --primary-l: rgba(9,105,218,0.08);
  --green: #1a7f37;
  --green-l: rgba(26,127,55,0.1);
  --red: #cf222e;
  --red-l: rgba(207,34,46,0.08);
  --orange: #9a6700;
  --orange-l: rgba(154,103,0,0.1);
  --purple: #8250df;
  --purple-l: rgba(130,80,223,0.1);
  --teal: #0d7d6c;
  --teal-l: rgba(13,125,108,0.1);
  --shadow: 0 1px 6px rgba(0,0,0,0.08);
  --shadow-lg: 0 8px 24px rgba(0,0,0,0.12);
  --log-bg: #f6f8fa;
  --modal-overlay: rgba(0,0,0,0.4);
  --gradient-header: linear-gradient(135deg, #ffffff 0%, #f6f8fa 100%);
}

/* Auto theme: follow system */
@media (prefers-color-scheme: light) {
  :root:not([data-theme="dark"]):not([data-theme="light"]) {
    --bg: #eef0f3;
    --bg2: #f8f9fb;
    --card: #f8f9fb;
    --card-hover: #eef1f4;
    --border: #d4dae1;
    --border-light: #e3e7ec;
    --text: #262c34;
    --text-secondary: #4a525d;
    --muted: #6d7682;
    --primary: #0969da;
    --primary-d: #0550ae;
    --primary-l: rgba(9,105,218,0.08);
    --green: #1a7f37;
    --green-l: rgba(26,127,55,0.1);
    --red: #cf222e;
    --red-l: rgba(207,34,46,0.08);
    --orange: #9a6700;
    --orange-l: rgba(154,103,0,0.1);
    --purple: #8250df;
    --purple-l: rgba(130,80,223,0.1);
    --teal: #0d7d6c;
    --teal-l: rgba(13,125,108,0.1);
    --shadow: 0 1px 6px rgba(0,0,0,0.08);
    --shadow-lg: 0 8px 24px rgba(0,0,0,0.12);
    --log-bg: #f6f8fa;
    --modal-overlay: rgba(0,0,0,0.4);
    --gradient-header: linear-gradient(135deg, #ffffff 0%, #f6f8fa 100%);
  }
}

* { margin:0; padding:0; box-sizing:border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
  background: var(--bg);
  color: var(--text);
  min-height: 100vh;
  transition: background 0.3s ease, color 0.3s ease;
}
/* Header */
.hdr {
  background: var(--gradient-header);
  border-bottom: 1px solid var(--border);
  padding: 0 24px; height: 60px;
  display: flex; align-items: center; justify-content: space-between;
  position: sticky; top: 0; z-index: 100;
  backdrop-filter: blur(12px);
}
.hdr::before {
  content: ''; position: absolute; top: 0; left: 0; right: 0; height: 3px;
  background: linear-gradient(90deg, var(--primary), var(--purple), var(--teal));
}
.hdr-l { display: flex; align-items: center; gap: 16px; }
.logo {
  font-size: 20px; font-weight: 800; letter-spacing: -0.5px;
  background: linear-gradient(135deg, var(--primary), var(--purple));
  -webkit-background-clip: text; background-clip: text;
  -webkit-text-fill-color: transparent;
}
.logo span {
  background: linear-gradient(135deg, var(--teal), var(--green));
  -webkit-background-clip: text; background-clip: text;
  -webkit-text-fill-color: transparent;
}
.badge {
  padding: 4px 12px; border-radius: 20px; font-size: 12px; font-weight: 600;
}
.badge-live { background: var(--green-l); color: var(--green); border: 1px solid var(--green); }
.badge-dry { background: var(--orange-l); color: var(--orange); border: 1px solid var(--orange); }
.badge-off { background: var(--primary-l); color: var(--muted); border: 1px solid var(--border); }
.hdr-r { display: flex; align-items: center; gap: 10px; }
/* Theme switcher */
.theme-switch {
  display: flex; align-items: center; gap: 2px;
  background: var(--bg); border: 1px solid var(--border);
  border-radius: 20px; padding: 3px;
}
.theme-btn {
  width: 30px; height: 30px; border-radius: 50%; border: none; cursor: pointer;
  background: transparent; color: var(--muted); font-size: 14px;
  display: flex; align-items: center; justify-content: center;
  transition: all 0.2s;
}
.theme-btn:hover { color: var(--text); }
.theme-btn.active { background: var(--primary); color: #fff; }
/* Buttons */
.btn {
  padding: 8px 18px; border-radius: var(--radius-sm); border: none; cursor: pointer;
  font-size: 14px; font-weight: 600; transition: all 0.2s;
}
.btn-primary { background: linear-gradient(135deg, var(--primary), var(--primary-d)); color: #fff; }
.btn-primary:hover { background: linear-gradient(135deg, var(--primary-d), var(--primary)); transform: translateY(-1px); box-shadow: var(--shadow); }
.btn-danger { background: var(--red); color: #fff; }
.btn-danger:hover { opacity: 0.85; transform: translateY(-1px); }
.btn-ghost { background: transparent; color: var(--text); border: 1px solid var(--border); }
.btn-ghost:hover { border-color: var(--primary); color: var(--primary); }
.btn:disabled { opacity: 0.4; cursor: not-allowed; transform: none !important; }
/* Layout */
.container { max-width: 1400px; margin: 0 auto; padding: 24px; }
/* Tabs */
.tabs { display: flex; gap: 4px; margin-bottom: 20px; background: var(--bg2); border-radius: 10px; padding: 4px; border: 1px solid var(--border-light); }
.tab {
  padding: 10px 20px; border-radius: var(--radius-sm); cursor: pointer; font-size: 14px;
  font-weight: 500; color: var(--muted); transition: all 0.2s;
}
.tab.active { background: linear-gradient(135deg, var(--primary), var(--primary-d)); color: #fff; }
.tab:hover:not(.active) { color: var(--text); background: var(--card-hover); }
.tab-badge {
  display: inline-block; min-width: 20px; height: 20px; line-height: 20px; padding: 0 6px;
  border-radius: 10px; background: var(--red); color: #fff; font-size: 11px; font-weight: 700;
  text-align: center; margin-left: 4px;
}
.tab.active .tab-badge { background: #fff; color: var(--primary); }
/* Stats bar */
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 16px; margin-bottom: 24px; }
.stat {
  background: var(--card); border: 1px solid var(--border); border-radius: var(--radius);
  padding: 16px 20px; transition: all 0.2s; box-shadow: var(--shadow);
}
.stat:hover { border-color: var(--primary); transform: translateY(-2px); }
.stat-label { font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 6px; }
.stat-value { font-size: 24px; font-weight: 700; font-variant-numeric: tabular-nums; }
.stat-value.green { color: var(--green); }
.stat-value.red { color: var(--red); }
.stat-sub { font-size: 12px; color: var(--muted); margin-top: 4px; }
/* Sleep countdown card */
.stat-scan { position: relative; border-color: var(--teal); background: linear-gradient(135deg, var(--teal-l), var(--card)); }
.stat-scan .stat-value { color: var(--teal); }
.scan-progress { height: 5px; border-radius: 3px; background: var(--border); overflow: hidden; margin-top: 8px; }
.scan-progress-fill { display: block; height: 100%; width: 0%; border-radius: 3px; background: linear-gradient(90deg, var(--primary), var(--teal)); transition: width 1s linear; }
/* Cards */
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(380px, 1fr)); gap: 16px; }
.card {
  position: relative; overflow: hidden;
  background: var(--card); border: 1px solid var(--border); border-radius: var(--radius);
  padding: 18px; transition: all 0.2s; box-shadow: var(--shadow);
}
.card:hover { border-color: var(--primary); transform: translateY(-3px); box-shadow: var(--shadow-lg); }
.card::after { content: ''; position: absolute; top: 0; left: 0; right: 0; height: 2px; background: linear-gradient(90deg, var(--primary), var(--purple), var(--teal)); opacity: 0; transition: opacity 0.2s; }
.card:hover::after { opacity: 1; }
.card-q { font-size: 15px; font-weight: 600; margin-bottom: 12px; line-height: 1.4; color: var(--text); }
.card-meta { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 10px; }
.tag { padding: 3px 10px; border-radius: 6px; font-size: 11px; font-weight: 600; }
.tag-expiry { background: var(--primary-l); color: var(--primary); }
.tag-arb { background: var(--purple-l); color: var(--purple); }
.tag-tweet { background: var(--teal-l); color: var(--teal); }
.tag-momentum { background: var(--green-l); color: var(--green); }
.tag-mr { background: var(--orange-l); color: var(--orange); }
.tag-sm { background: var(--purple-l); color: var(--purple); }
.tag-dry { background: var(--orange-l); color: var(--orange); }
.prob-bar { height: 8px; border-radius: 4px; background: var(--border); overflow: hidden; margin-bottom: 10px; display: flex; }
.prob-yes { background: var(--green); height: 100%; transition: width 0.3s; }
.prob-no { background: var(--red); height: 100%; transition: width 0.3s; }
.card-row { display: flex; justify-content: space-between; align-items: center; font-size: 13px; margin-bottom: 4px; }
.card-row span:first-child { color: var(--muted); }
.card-row span:last-child { font-weight: 600; }
.card-profit { color: var(--green); font-size: 18px; font-weight: 700; }
.card-link { display: inline-block; margin-top: 8px; color: var(--primary); font-size: 13px; text-decoration: none; }
.card-link:hover { text-decoration: underline; }
/* Table */
.table { width: 100%; border-collapse: collapse; background: var(--card); border-radius: var(--radius); overflow: hidden; box-shadow: var(--shadow); }
.table th { background: var(--bg2); padding: 12px 16px; text-align: left; font-size: 12px; text-transform: uppercase; color: var(--muted); letter-spacing: 0.5px; }
.table td { padding: 12px 16px; border-top: 1px solid var(--border-light); font-size: 14px; }
.table tr:hover td { background: var(--card-hover); }
.status-pill { padding: 3px 10px; border-radius: 6px; font-size: 11px; font-weight: 600; }
.st-open { background: var(--primary-l); color: var(--primary); }
.st-won { background: var(--green-l); color: var(--green); }
.st-lost { background: var(--red-l); color: var(--red); }
.st-tp { background: var(--teal-l); color: var(--teal); }
/* Log */
.log-box { background: var(--log-bg); border: 1px solid var(--border); border-radius: var(--radius); padding: 16px; font-family: 'Consolas', 'Monaco', monospace; font-size: 13px; max-height: 500px; overflow-y: auto; }
.log-box::-webkit-scrollbar { width: 8px; }
.log-box::-webkit-scrollbar-track { background: transparent; }
.log-box::-webkit-scrollbar-thumb { background: var(--border); border-radius: 4px; }
.log-line { padding: 2px 0; white-space: pre-wrap; word-break: break-all; }
.log-info { color: var(--text-secondary); }
.log-warn { color: var(--orange); }
.log-error { color: var(--red); }
.log-ok { color: var(--green); }
/* Modal */
.modal-overlay {
  position: fixed; top: 0; left: 0; right: 0; bottom: 0;
  background: var(--modal-overlay); display: flex; align-items: center; justify-content: center;
  z-index: 1000; backdrop-filter: blur(4px);
}
.modal {
  background: var(--card); border: 1px solid var(--border); border-radius: 16px;
  padding: 32px; width: 90%; max-width: 520px; max-height: 90vh; overflow-y: auto;
  box-shadow: var(--shadow-lg); animation: modalIn 0.25s ease;
}
@keyframes modalIn { from { opacity:0; transform: scale(0.95) translateY(10px); } to { opacity:1; transform: scale(1) translateY(0); } }
.modal h2 { font-size: 22px; margin-bottom: 8px; }
.modal p { color: var(--muted); font-size: 14px; margin-bottom: 20px; }
.field { margin-bottom: 16px; }
.field label { display: block; font-size: 13px; font-weight: 600; margin-bottom: 6px; }
.field input, .field select {
  width: 100%; padding: 10px 14px; background: var(--bg); border: 1px solid var(--border);
  border-radius: var(--radius-sm); color: var(--text); font-size: 14px; font-family: monospace;
  transition: border-color 0.2s;
}
.field input:focus, .field select:focus { outline: none; border-color: var(--primary); box-shadow: 0 0 0 3px var(--primary-l); }
.field-hint { font-size: 12px; color: var(--muted); margin-top: 4px; }
.security-note {
  background: var(--green-l); border: 1px solid var(--green);
  border-radius: var(--radius-sm); padding: 12px 16px; margin: 16px 0;
  font-size: 13px; color: var(--green);
}
.warn-note {
  background: var(--red-l); border: 1px solid var(--red);
  border-radius: var(--radius-sm); padding: 12px 16px; margin: 16px 0;
  font-size: 13px; color: var(--red);
}
.auth-error-box {
  background: var(--red-l); border: 1px solid var(--red);
  border-radius: var(--radius-sm); padding: 14px 16px; margin-bottom: 16px;
  font-size: 13px; color: var(--red); line-height: 1.6;
  white-space: pre-line;
}
/* Network diagnostic box */
.network-diag-box {
  background: var(--card-2); border: 1px solid var(--border);
  border-radius: var(--radius-sm); padding: 16px; margin-bottom: 16px;
  font-size: 13px; line-height: 1.8;
}
.network-diag-box .diag-title { font-weight: 700; margin-bottom: 8px; color: var(--text); }
.network-diag-box .diag-row { display: flex; justify-content: space-between; padding: 4px 0; border-bottom: 1px solid var(--border); }
.network-diag-box .diag-row:last-child { border-bottom: none; }
.network-diag-box .diag-label { color: var(--muted); }
.network-diag-box .diag-value-ok { color: var(--green); font-weight: 600; }
.network-diag-box .diag-value-fail { color: var(--red); font-weight: 600; }
.network-diag-box .diag-value-warn { color: var(--orange); font-weight: 600; }
.network-diag-box .diag-suggestion {
  margin-top: 10px; padding: 10px 12px; border-radius: var(--radius-sm);
  background: var(--blue-l); color: var(--blue); font-size: 12px; line-height: 1.6;
}
/* Empty state */
.grid > .empty { grid-column: 1 / -1; display: flex; flex-direction: column; align-items: center; justify-content: center; min-height: 340px; }
.empty { text-align: center; padding: 60px 20px; color: var(--muted); }
.empty-icon { font-size: 56px; margin-bottom: 16px; }
.empty-title { font-size: 16px; font-weight: 600; color: var(--text-secondary); margin-bottom: 8px; }
.empty-hint { font-size: 13px; color: var(--muted); margin-bottom: 18px; line-height: 1.7; }
.empty .btn { margin-top: 2px; }
/* Toggle switch */
.field-row { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 14px; }
.field-row label { flex: 1; font-weight: 600; font-size: 14px; }
.field-row .field-hint { display: block; font-weight: 400; }
.switch { position: relative; width: 46px; height: 26px; flex-shrink: 0; }
.switch input { opacity: 0; width: 0; height: 0; }
.switch .slider { position: absolute; inset: 0; background: var(--border); border-radius: 26px; transition: 0.2s; cursor: pointer; }
.switch .slider::before { content: ''; position: absolute; height: 20px; width: 20px; left: 3px; top: 3px; background: #fff; border-radius: 50%; transition: 0.2s; }
.switch input:checked + .slider { background: var(--green); }
.switch input:checked + .slider::before { transform: translateX(20px); }
/* Loading spinner */
.spinner {
  display: inline-block; width: 16px; height: 16px;
  border: 2px solid var(--border); border-top-color: var(--primary);
  border-radius: 50%; animation: spin 0.8s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }
.pulse { animation: pulse 2s ease-in-out infinite; }
@keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.5; } }
/* Section title */
.section-title { font-size: 18px; font-weight: 700; margin-bottom: 16px; display: flex; align-items: center; gap: 8px; }
.section-title .count { font-size: 14px; color: var(--muted); font-weight: 400; }
/* Footer */
.footer {
  margin-top: 40px; padding: 18px 24px 26px;
  border-top: 1px solid var(--border);
  display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap;
  font-size: 13px; color: var(--muted); background: var(--bg2);
}
.footer .footer-brand {
  background: linear-gradient(135deg, var(--primary), var(--purple), var(--teal));
  -webkit-background-clip: text; background-clip: text;
  -webkit-text-fill-color: transparent; font-weight: 800;
}
/* Scrollbar */
::-webkit-scrollbar { width: 10px; height: 10px; }
::-webkit-scrollbar-track { background: var(--bg); }
::-webkit-scrollbar-thumb { background: var(--border); border-radius: 5px; }
::-webkit-scrollbar-thumb:hover { background: var(--muted); }
/* Toast animation */
@keyframes slideIn { from { transform: translateX(100%); opacity: 0; } to { transform: translateX(0); opacity: 1; } }
/* ===== v10 美化 ===== */
@keyframes fadeUp { from { opacity: 0; transform: translateY(12px); } to { opacity: 1; transform: translateY(0); } }
/* Only animate elements that DON'T re-render every refresh (cards/tables do →
   would flicker). Stats & section titles render once. */
.stat, .section-title { animation: fadeUp 0.4s ease both; }
.stat:nth-child(2) { animation-delay: 0.05s; }
.stat:nth-child(3) { animation-delay: 0.10s; }
.stat:nth-child(4) { animation-delay: 0.15s; }
.stat:nth-child(5) { animation-delay: 0.20s; }
.stat:nth-child(6) { animation-delay: 0.25s; }
.stat:nth-child(7) { animation-delay: 0.30s; }
.stat:nth-child(8) { animation-delay: 0.35s; }
.stat { position: relative; overflow: hidden; }
.stat::after { content: ''; position: absolute; top: 0; left: 0; right: 0; height: 2px; background: linear-gradient(90deg, var(--primary), var(--teal)); opacity: 0.55; }
.stat-icon { font-size: 20px; margin-right: 7px; vertical-align: -2px; }
.stat-label { display: flex; align-items: center; font-weight: 700; }
.stat-value.grad { background: linear-gradient(135deg, var(--text), var(--primary)); -webkit-background-clip: text; background-clip: text; -webkit-text-fill-color: transparent; }
/* confidence ring */
.ring { display: inline-block; width: 46px; height: 46px; position: relative; vertical-align: middle; }
.ring svg { transform: rotate(-90deg); }
.ring .ring-bg { stroke: var(--border); }
.ring .ring-fg { stroke-linecap: round; transition: stroke-dashoffset 0.6s ease; }
.ring span { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center; font-size: 12px; font-weight: 800; }
/* P&L bar in positions table */
.pnl-cell { display: flex; align-items: center; gap: 8px; }
.pnl-bar { height: 6px; border-radius: 3px; background: var(--border); width: 56px; overflow: hidden; flex-shrink: 0; }
.pnl-bar > span { display: block; height: 100%; border-radius: 3px; transition: width 0.5s ease; }
/* zebra rows */
.table tbody tr:nth-child(even) td { background: rgba(127,127,127,0.045); }
/* network status dot */
.net-dot { display: inline-block; width: 9px; height: 9px; border-radius: 50%; margin-right: 7px; vertical-align: 1px; }
.net-dot.ok { background: var(--green); box-shadow: 0 0 6px var(--green); animation: pulse 2s ease-in-out infinite; }
.net-dot.warn { background: var(--orange); box-shadow: 0 0 6px var(--orange); }
.net-dot.bad { background: var(--red); box-shadow: 0 0 6px var(--red); }
/* card hover lift */
.grid .card { transition: transform 0.18s ease, box-shadow 0.18s ease, border-color 0.18s ease; }
</style>
</head>
<body>

<div class="hdr">
  <div class="hdr-l">
    <div class="logo">Poly<span>Auto</span></div>
    <div id="modeBadge" class="badge badge-off">未授权</div>
  </div>
  <div class="hdr-r">
    <div style="font-size:13px;color:var(--muted);display:flex;align-items:center;"><span id="netDot" class="net-dot ok" title="网络状态"></span><span id="actionStatus"></span></div>
    <div class="theme-switch">
      <button class="theme-btn" onclick="setTheme('light')" title="亮色" data-theme-btn="light">&#9728;</button>
      <button class="theme-btn" onclick="setTheme('dark')" title="暗色" data-theme-btn="dark">&#9789;</button>
      <button class="theme-btn active" onclick="setTheme('auto')" title="跟随系统" data-theme-btn="auto">&#9881;</button>
    </div>
    <button id="setupBtn" class="btn btn-ghost" onclick="showSetup()">配置</button>
    <button id="settingsBtn" class="btn btn-ghost" onclick="showSettings()">⚙️ 设置</button>
    <button id="authBtn" class="btn btn-primary" onclick="showAuthorize()">授权并启动</button>
    <button id="stopBtn" class="btn btn-danger" onclick="emergencyStop()" style="display:none;">紧急停止</button>
  </div>
</div>

<div class="container">
  <!-- Stats Bar -->
  <div class="stats" id="statsBar">
    <div class="stat"><div class="stat-label"><span class="stat-icon">⏱️</span>运行时间</div><div class="stat-value" id="statRunTime" style="font-size:20px;">00:00:00</div><div class="stat-sub" id="statRunStatus">未运行</div></div>
    <div class="stat stat-scan"><div class="stat-label"><span class="stat-icon">🔄</span>下次扫描</div><div class="stat-value" id="statNextScan">--:--</div><div class="scan-progress"><span class="scan-progress-fill" id="scanProgressFill"></span></div><div class="stat-sub" id="statNextScanSub">等待启动</div></div>
    <div class="stat"><div class="stat-label"><span class="stat-icon">💰</span>当前余额</div><div class="stat-value grad" id="statBalance">$0</div><div class="stat-sub" id="statBankrollSub">初始 $0</div></div>
    <div class="stat"><div class="stat-label"><span class="stat-icon">📊</span>已投入</div><div class="stat-value" id="statExposure">$0</div></div>
    <div class="stat"><div class="stat-label"><span class="stat-icon">💵</span>可用现金</div><div class="stat-value green" id="statCash">$0</div></div>
    <div class="stat"><div class="stat-label"><span class="stat-icon">📦</span>持仓数</div><div class="stat-value" id="statPositions">0</div><div class="stat-sub" id="statPositionsSub">最大 10</div></div>
    <div class="stat"><div class="stat-label"><span class="stat-icon">📈</span>今日盈亏</div><div class="stat-value" id="statDailyPnl">$0</div><div class="stat-sub" id="statDailyTrades">0 笔交易</div></div>
    <div class="stat"><div class="stat-label"><span class="stat-icon">🏆</span>累计盈亏</div><div class="stat-value" id="statTotalPnl">$0</div><div class="stat-sub" id="statTotalTrades">0 笔 | 胜率 0%</div></div>
  </div>

  <!-- Tabs -->
  <div class="tabs">
    <div class="tab active" onclick="switchTab('opps', event)">🎯 交易机会 <span class="tab-badge" id="badgeOpps" style="display:none;">0</span></div>
    <div class="tab" onclick="switchTab('positions', event)">📦 我的持仓 <span class="tab-badge" id="badgePos" style="display:none;">0</span></div>
    <div class="tab" onclick="switchTab('equity', event)">📈 资金曲线</div>
    <div class="tab" onclick="switchTab('strategy', event)">🧠 策略表现</div>
    <div class="tab" onclick="switchTab('markets', event)">🌐 市场浏览</div>
    <div class="tab" onclick="switchTab('logs', event)">📜 运行日志</div>
  </div>

  <!-- Tab: Opportunities -->
  <div id="tab-opps">
    <div class="section-title">
      自动发现的交易机会 <span class="count" id="oppCount"></span>
      <button class="btn btn-ghost" onclick="manualScan()" id="scanBtn" style="margin-left:auto;font-size:13px;padding:6px 14px;">手动扫描</button>
      <span id="lastScanTime" style="font-size:12px;color:var(--muted);"></span>
    </div>
    <div class="grid" id="oppGrid"></div>
  </div>

  <!-- Tab: Positions -->
  <div id="tab-positions" style="display:none;">
    <div class="section-title">
      持仓管理
      <button class="btn btn-ghost" onclick="exportPositionsCSV()" style="margin-left:auto;font-size:13px;padding:6px 14px;">导出 CSV</button>
    </div>
    <table class="table" id="posTable">
      <thead><tr><th>策略</th><th>市场</th><th>方向</th><th>入场价</th><th>数量</th><th>成本</th><th>置信度</th><th>状态</th><th>盈亏</th><th>操作</th></tr></thead>
      <tbody id="posBody"></tbody>
    </table>
  </div>

  <!-- Tab: Equity Curve -->
  <div id="tab-equity" style="display:none;">
    <div class="section-title">资金曲线 <span class="count" id="equityCount"></span></div>
    <div id="equityCooldownAlert" style="display:none;background:rgba(248,81,73,0.08);border:1px solid rgba(248,81,73,0.3);border-radius:8px;padding:12px 16px;margin-bottom:16px;font-size:13px;color:var(--red);"></div>
    <div style="background:var(--card);border:1px solid var(--border);border-radius:var(--radius);padding:20px;margin-bottom:16px;">
      <canvas id="equityChart" width="1300" height="400"></canvas>
    </div>
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:16px;">
      <div class="stat"><div class="stat-label">胜率</div><div class="stat-value" id="statWinRate">0%</div></div>
      <div class="stat"><div class="stat-label">连续亏损</div><div class="stat-value" id="statConsecLoss">0</div></div>
      <div class="stat"><div class="stat-label">最大回撤</div><div class="stat-value red" id="statMaxDD">0%</div></div>
      <div class="stat"><div class="stat-label">收益率</div><div class="stat-value" id="statROI">0%</div></div>
    </div>
  </div>

  <!-- Tab: Strategy Performance -->
  <div id="tab-strategy" style="display:none;">
    <div class="section-title">策略表现分析
      <button class="btn btn-ghost" onclick="exportPositionsCSV()" style="margin-left:auto;font-size:13px;padding:6px 14px;">导出 CSV</button>
    </div>
    <div id="strategyBreakdown" style="margin-bottom:24px;"></div>
    <div class="section-title" style="font-size:15px;">风险指标</div>
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:16px;">
      <div class="stat"><div class="stat-label">最大回撤</div><div class="stat-value red" id="statMaxDrawdown">-</div></div>
      <div class="stat"><div class="stat-label">收益率(ROI)</div><div class="stat-value" id="statROI2">-</div></div>
      <div class="stat"><div class="stat-label">利润因子</div><div class="stat-value" id="statProfitFactor">-</div></div>
      <div class="stat"><div class="stat-label">平均交易</div><div class="stat-value" id="statAvgTrade">-</div></div>
      <div class="stat"><div class="stat-label">最佳交易</div><div class="stat-value green" id="statBestTrade">-</div></div>
      <div class="stat"><div class="stat-label">最差交易</div><div class="stat-value red" id="statWorstTrade">-</div></div>
      <div class="stat"><div class="stat-label">总交易额</div><div class="stat-value" id="statTotalVolume">-</div></div>
      <div class="stat"><div class="stat-label">持仓周期</div><div class="stat-value" id="statAvgHoldTime">-</div></div>
    </div>
  </div>

  <!-- Tab: Markets -->
  <div id="tab-markets" style="display:none;">
    <div class="section-title">活跃市场 <span class="count" id="marketCount"></span></div>
    <div style="margin-bottom:16px;">
      <input type="text" placeholder="搜索市场..." style="width:100%;max-width:400px;padding:10px 14px;background:var(--card);border:1px solid var(--border);border-radius:8px;color:var(--text);font-size:14px;" onkeyup="searchMarkets(this.value)">
    </div>
    <div class="grid" id="marketGrid"></div>
  </div>

  <!-- Tab: Logs -->
  <div id="tab-logs" style="display:none;">
    <div class="section-title">实时运行日志</div>
    <div class="log-box" id="logBox"></div>
  </div>
</div>

<footer class="footer">
  <span>PolyAuto · 全自动预测市场交易系统</span>
  <span>© 2026 <span class="footer-brand">Gavin</span> · 谨慎交易，风险自负</span>
</footer>

<!-- Setup Modal -->
<div id="setupModal" class="modal-overlay" style="display:none;">
  <div class="modal">
    <h2>首次配置</h2>
    <p>输入你的 Polymarket 钱包凭证。所有信息将使用你设置的密码进行 AES 加密存储，绝不会以明文形式保存。</p>
    <div class="security-note">
      <strong>安全承诺：</strong>私钥使用 PBKDF2 + Fernet 加密，密码不存储。即使文件被盗，没有密码也无法解密。
    </div>
    <div class="field">
      <label>加密密码（至少6位）</label>
      <input type="password" id="setupPassword" placeholder="设置一个强密码">
      <div class="field-hint">用于加密保护你的私钥，每次授权时需要输入</div>
    </div>
    <div class="field">
      <label>钱包私钥 (PRIVATE_KEY)</label>
      <input type="password" id="setupPrivateKey" placeholder="0x..." style="font-family:monospace;">
      <div class="field-hint">Polymarket Settings -> Export Wallet 获取</div>
    </div>
    <div class="field">
      <label>钱包地址 (FUNDER_ADDRESS)</label>
      <input type="text" id="setupFunder" placeholder="0x..." style="font-family:monospace;">
      <div class="field-hint">你的 Polygon 钱包地址</div>
    </div>
    <div class="field">
      <label>交易模式</label>
      <select id="setupMode">
        <option value="dry_run">Dry Run（模拟交易，推荐先用）</option>
        <option value="live">Live（实盘交易）</option>
      </select>
    </div>
    <div class="field">
      <label>初始资金 (USDC)</label>
      <input type="number" id="setupBankroll" value="200" min="10">
    </div>
    <div class="field">
      <label>签名类型</label>
      <select id="setupSigType">
        <option value="2">2 - Proxy Wallet（Polymarket 默认）</option>
        <option value="0">0 - EOA Wallet</option>
        <option value="1">1 - Safe Wallet</option>
      </select>
    </div>
    <div class="field">
      <label>Telegram Bot Token（可选）</label>
      <input type="text" id="setupTgToken" placeholder="留空则不推送通知">
    </div>
    <div class="field">
      <label>Telegram Chat ID（可选）</label>
      <input type="text" id="setupTgChat" placeholder="留空则不推送通知">
    </div>
    <div class="warn-note">
      <strong>风险提示：</strong>预测市场是零和博弈，可能亏损全部资金。只用你能承受损失的金额。Polymarket 限制中国大陆用户使用。
    </div>
    <div style="display:flex;gap:12px;justify-content:flex-end;">
      <button class="btn btn-ghost" onclick="closeModal('setupModal')">取消</button>
      <button class="btn btn-primary" onclick="submitSetup()">加密保存</button>
    </div>
  </div>
</div>

<!-- Authorize Modal -->
<div id="authModal" class="modal-overlay" style="display:none;">
  <div class="modal">
    <h2>一键授权</h2>
    <p>输入你的加密密码，系统将自动解密凭证、初始化钱包、并启动全自动交易。授权后你什么都不用做，机器人会自动扫描、分析、下单、管理仓位。</p>
    <div class="security-note">
      <strong>授权后机器人将自动：</strong><br>
      1. 每 5 分钟扫描 300+ 市场<br>
      2. 发现符合策略的机会自动下单<br>
      3. 监控仓位到期、自动止盈<br>
      4. 日亏损达 10% 自动停止<br>
      5. 每笔交易推送 Telegram 通知
    </div>
    <div class="field">
      <label>加密密码</label>
      <input type="password" id="authPassword" placeholder="输入你设置的密码" onkeydown="if(event.key==='Enter')doAuthorize()">
    </div>
    <div id="authError" class="auth-error-box" style="display:none;"></div>
    <div id="networkDiagBox" class="network-diag-box" style="display:none;"></div>
    <div style="display:flex;gap:12px;justify-content:flex-end;flex-wrap:wrap;">
      <button class="btn btn-ghost" onclick="closeModal('authModal')">取消</button>
      <button class="btn btn-ghost" onclick="runNetworkDiag()" id="authDiagBtn">网络诊断</button>
      <button class="btn btn-ghost" onclick="doAuthorize(true)" id="authViewBtn" style="display:none;">仅查看模式</button>
      <button class="btn btn-primary" onclick="doAuthorize()" id="authSubmitBtn">授权并启动</button>
    </div>
  </div>
</div>

<!-- Settings Modal -->
<div id="settingsModal" class="modal-overlay" style="display:none;">
  <div class="modal">
    <h2>⚙️ 交易设置</h2>
    <p>运行时参数保存在 <code style="font-size:12px;">trading_config.json</code>，改动即时生效（下次扫描采用）。修改初始资金会自动重置统计以保持数据一致。</p>
    <div class="field-row">
      <label>初始资金 (USDC)<span class="field-hint">驱动仓位计算与盈亏基准</span></label>
      <input type="number" id="cfgBankroll" value="200" min="10" style="width:140px;">
    </div>
    <div class="field-row">
      <label>交易模式</label>
      <select id="cfgMode" style="width:150px;">
        <option value="dry_run">模拟盘</option>
        <option value="live">实盘</option>
      </select>
    </div>
    <div class="field-row">
      <label>扫描间隔 (秒)<span class="field-hint">默认 300 秒（5 分钟）</span></label>
      <input type="number" id="cfgScanInterval" value="300" min="30" style="width:140px;">
    </div>
    <div class="field-row">
      <label>最大持仓数</label>
      <input type="number" id="cfgMaxPositions" value="10" min="1" style="width:140px;">
    </div>
    <div class="field-row">
      <label>每日交易上限<span class="field-hint">达到后当日停止开新仓</span></label>
      <input type="number" id="cfgMaxDaily" value="8" min="1" style="width:140px;">
    </div>
    <div class="field-row">
      <label>最小置信度<span class="field-hint">默认 75</span></label>
      <input type="number" id="cfgMinConf" value="75" min="50" max="100" style="width:140px;">
    </div>
    <div class="field-row">
      <label>最小期望收益 EV%<span class="field-hint">默认 1.5%</span></label>
      <input type="number" id="cfgMinEv" value="1.5" min="0.5" step="0.1" style="width:140px;">
    </div>
    <div class="field-row">
      <label>推文套利止盈目标<span class="field-hint">桶价涨到此倍数即提前止盈（1.0=翻倍，0=只持有到期）</span></label>
      <input type="number" id="cfgTweetTp" value="1.0" min="0" step="0.1" style="width:140px;">
    </div>
    <div class="field-row">
      <label>做市偏向<span class="field-hint">买单挂在 ask 下方比例（0=按 ask 吃单；0.001=挂低0.1%做市吃价差，研究证实做市是唯一稳健 edge）</span></label>
      <input type="number" id="cfgMakerBias" value="0" min="0" step="0.0005" style="width:140px;">
    </div>
    <div class="field-row">
      <label>最小流动性分<span class="field-hint">跳过流动性分低于此的机会（默认 10）</span></label>
      <input type="number" id="cfgMinLiq" value="10" min="0" style="width:140px;">
    </div>
    <div class="field-row">
      <label>临期年化下限%<span class="field-hint">ExpiryYield 要求的最低年化收益（默认 20）</span></label>
      <input type="number" id="cfgAnnualFloor" value="20" min="5" step="5" style="width:140px;">
    </div>
    <div class="field-row">
      <label>过滤电竞/比赛市场<span class="field-hint">排除 Dota2/CS/LoL 等难预测市场，提高胜率</span></label>
      <label class="switch"><input type="checkbox" id="cfgFilterSpec" checked><span class="slider"></span></label>
    </div>
    <div class="field-row">
      <label>收紧加密边界市场<span class="field-hint">BTC/ETH above/below 需更高概率</span></label>
      <label class="switch"><input type="checkbox" id="cfgFilterCrypto" checked><span class="slider"></span></label>
    </div>
    <div class="field-row">
      <label>临期理财策略</label>
      <label class="switch"><input type="checkbox" id="cfgStrategyExpiry" checked><span class="slider"></span></label>
    </div>
    <div class="field-row">
      <label>套利策略</label>
      <label class="switch"><input type="checkbox" id="cfgStrategyArb" checked><span class="slider"></span></label>
    </div>
    <div class="field-row">
      <label>推文套利策略</label>
      <label class="switch"><input type="checkbox" id="cfgStrategyTweet" checked><span class="slider"></span></label>
    </div>
    <div class="security-note">
      <strong>数据一致性：</strong>修改初始资金将自动重置统计与持仓（以新资金为基准重新开始），避免历史数据混乱。
    </div>
    <div style="display:flex;gap:12px;justify-content:space-between;flex-wrap:wrap;">
      <button class="btn btn-danger" onclick="resetStats()" style="background:var(--red);">重置统计</button>
      <div style="display:flex;gap:12px;">
        <button class="btn btn-ghost" onclick="closeModal('settingsModal')">取消</button>
        <button class="btn btn-primary" onclick="saveSettings()">保存配置</button>
      </div>
    </div>
  </div>
</div>

<script>
let currentTab = 'opps';
let allMarkets = [];
let autoRefresh = null;
let runTimer = null;
let sleepTimer = null;
let lastRunSeconds = 0;
let engineRunning = false;
let sleepRemaining = 0;   // seconds until next scan (drives the countdown)
let scanInterval = 300;   // total sleep duration for the progress bar
let lastAction = '';      // latest current_action from the server

// ===== Number Formatting =====
function fmtMoney(n, decimals) {
  if (n === undefined || n === null || isNaN(n)) return '$0.00';
  const d = decimals === undefined ? 2 : decimals;
  return '$' + Number(n).toLocaleString('en-US', {minimumFractionDigits: d, maximumFractionDigits: d});
}
function fmtNum(n, decimals) {
  if (n === undefined || n === null || isNaN(n)) return '0';
  const d = decimals === undefined ? 0 : decimals;
  return Number(n).toLocaleString('en-US', {minimumFractionDigits: d, maximumFractionDigits: d});
}
function fmtSigned(n, decimals) {
  if (n === undefined || n === null || isNaN(n)) return '$0.00';
  const d = decimals === undefined ? 2 : decimals;
  const abs = Math.abs(n).toLocaleString('en-US', {minimumFractionDigits: d, maximumFractionDigits: d});
  return (n >= 0 ? '+$' : '-$') + abs;
}
function fmtPct(n, decimals) {
  if (n === undefined || n === null || isNaN(n)) return '0%';
  const d = decimals === undefined ? 1 : decimals;
  return Number(n).toLocaleString('en-US', {minimumFractionDigits: d, maximumFractionDigits: d}) + '%';
}
// Chinese strategy names for the UI
const STRATEGY_CN = {
  'ExpiryYield': '临期理财', 'ExpiryYield+': '临期理财+',
  'Arbitrage': '套利', 'Arbitrage+': '套利+',
  'TweetPrediction': '推文预测', 'TweetArb': '推文套利', 'TweetArb+': '推文套利+',
  'Momentum': '动量策略', 'MeanReversion': '均值回归', 'SmartMoney': '聪明钱跟单'
};
function strategyCN(s) { return STRATEGY_CN[s] || s; }

function fmtCountdown(secs) {
  const m = Math.floor(secs / 60);
  const s = secs % 60;
  return String(m).padStart(2, '0') + ':' + String(s).padStart(2, '0');
}

// Run time as "N天 HH:MM:SS" (days shown once >= 1)
function fmtRunTime(secs) {
  secs = Math.max(0, Math.floor(secs || 0));
  const d = Math.floor(secs / 86400);
  const h = Math.floor((secs % 86400) / 3600);
  const m = Math.floor((secs % 3600) / 60);
  const s = secs % 60;
  const hms = String(h).padStart(2, '0') + ':' + String(m).padStart(2, '0') + ':' + String(s).padStart(2, '0');
  return d > 0 ? d + '天 ' + hms : hms;
}

function translateAction(action, sleepRemaining) {
  const map = {
    'idle': '待命',
    'stopped': '已停止',
    'scanning markets': '扫描市场中',
    'checking positions': '检查持仓中',
    'executing trades': '执行交易中',
    'updating positions': '更新持仓中',
    'running': '运行中',
    'sleeping': '休眠中'
  };
  if (action === 'sleeping') {
    const rem = (sleepRemaining !== undefined && sleepRemaining !== null) ? sleepRemaining : 0;
    return rem > 0 ? '休眠中 · 剩余 ' + fmtCountdown(rem) : '休眠中 · 即将扫描';
  }
  if (!action) return '运行中';
  return map[action] || action;
}

// Update the "下次扫描" countdown card + progress bar
function updateScanCountdown() {
  const el = document.getElementById('statNextScan');
  const fill = document.getElementById('scanProgressFill');
  const sub = document.getElementById('statNextScanSub');
  if (!el) return;
  if (lastAction === 'sleeping' && sleepRemaining > 0) {
    el.textContent = fmtCountdown(sleepRemaining);
    if (fill) fill.style.width = (sleepRemaining / (scanInterval || 300) * 100) + '%';
    if (sub) sub.textContent = '距离下次扫描';
    return;
  }
  const busy = ['scanning markets', 'checking positions', 'executing trades', 'updating positions'].includes(lastAction);
  if (busy) {
    el.textContent = '扫描中';
    if (fill) fill.style.width = '100%';
    if (sub) sub.textContent = '正在执行扫描';
  } else {
    el.textContent = '--:--';
    if (fill) fill.style.width = '100%';
    if (sub) sub.textContent = '等待启动';
  }
}

// ===== Theme System =====
function setTheme(theme) {
  localStorage.setItem('polyauto-theme', theme);
  applyTheme(theme);
  // Update active button
  document.querySelectorAll('[data-theme-btn]').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.themeBtn === theme);
  });
}

function applyTheme(theme) {
  if (theme === 'light') {
    document.documentElement.setAttribute('data-theme', 'light');
  } else if (theme === 'dark') {
    document.documentElement.setAttribute('data-theme', 'dark');
  } else {
    // auto: remove attribute to let @media handle it
    document.documentElement.removeAttribute('data-theme');
  }
}

// Load saved theme on startup
(function() {
  const saved = localStorage.getItem('polyauto-theme') || 'auto';
  applyTheme(saved);
  document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('[data-theme-btn]').forEach(btn => {
      btn.classList.toggle('active', btn.dataset.themeBtn === saved);
    });
  });
})();

// ===== Init =====
document.addEventListener('DOMContentLoaded', () => {
  checkStatus();
  autoRefresh = setInterval(loadDashboard, 5000);
  loadDashboard();
  // Local 1-second timer for run time display
  runTimer = setInterval(() => {
    if (engineRunning && lastRunSeconds >= 0) {
      lastRunSeconds++;
      const el = document.getElementById('statRunTime');
      if (el) el.textContent = fmtRunTime(lastRunSeconds);
    }
  }, 1000);
  // Local 1-second timer for the sleep countdown (ticks down to next scan)
  sleepTimer = setInterval(() => {
    if (lastAction === 'sleeping' && sleepRemaining > 0) {
      sleepRemaining--;
      updateScanCountdown();
      const as = document.getElementById('actionStatus');
      if (as) as.innerHTML = '<span class="pulse">' + translateAction('sleeping', sleepRemaining) + '</span>';
    }
  }, 1000);
});

// ===== Status Check =====
async function checkStatus() {
  try {
    const r = await fetch('/api/status');
    const s = await r.json();
    updateHeader(s);
  } catch(e) { console.error(e); }
}

function updateHeader(s) {
  const badge = document.getElementById('modeBadge');
  const authBtn = document.getElementById('authBtn');
  const setupBtn = document.getElementById('setupBtn');
  const stopBtn = document.getElementById('stopBtn');
  const actionStatus = document.getElementById('actionStatus');
  // Network status dot (green OK / orange degraded / red failed)
  const netDot = document.getElementById('netDot');
  if (netDot) {
    if (s.network_ok === false) { netDot.className = 'net-dot bad'; netDot.title = '网络异常（Gamma API 连接失败）'; }
    else if (s.network_ok === undefined) { netDot.className = 'net-dot warn'; netDot.title = '网络状态未知'; }
    else { netDot.className = 'net-dot ok'; netDot.title = '网络正常'; }
  }

  if (s.is_running) {
    badge.className = 'badge ' + (s.mode === 'live' ? 'badge-live' : 'badge-dry');
    badge.textContent = s.mode === 'live' ? '实盘运行中' : '模拟盘运行中';
    authBtn.style.display = 'none';
    stopBtn.style.display = '';
    setupBtn.style.display = 'none';
    actionStatus.innerHTML = '<span class="pulse">' + translateAction(s.current_action, s.sleep_remaining) + '</span>';
  } else if (s.is_authorized) {
    badge.className = 'badge badge-dry';
    badge.textContent = '已授权';
    authBtn.style.display = '';
    authBtn.textContent = '启动交易';
    stopBtn.style.display = 'none';
    setupBtn.style.display = '';
    actionStatus.textContent = '已授权，等待启动';
  } else if (s.has_credentials) {
    badge.className = 'badge badge-off';
    badge.textContent = '待授权';
    authBtn.style.display = '';
    authBtn.textContent = '授权并启动';
    stopBtn.style.display = 'none';
    setupBtn.style.display = '';
    actionStatus.textContent = '';
  } else {
    badge.className = 'badge badge-off';
    badge.textContent = '未配置';
    authBtn.style.display = 'none';
    stopBtn.style.display = 'none';
    setupBtn.style.display = '';
    actionStatus.textContent = '请先配置凭证';
  }
}

// ===== Dashboard Load =====
async function loadDashboard() {
  try {
    const r = await fetch('/api/dashboard');
    const d = await r.json();
    updateStats(d.status);
    updateOpportunities(d.opportunities);
    updatePositions(d.positions);
    updateLogs(d.logs);
    updateEquityCurve(d.equity_curve || [], d.status);
    updateStrategyBreakdown(d.strategy_breakdown || []);
    updateRiskMetrics(d.positions || [], d.status);
    updateTabBadges(d.opportunities.length, (d.positions||[]).filter(p=>p.status==='open').length);
    document.getElementById('marketCount').textContent = `(${fmtNum(d.markets_count)} 个市场)`;
    document.getElementById('oppCount').textContent = `(${d.opportunities.length} 个机会)`;
    document.getElementById('equityCount').textContent = `(${fmtNum((d.equity_curve||[]).length)} 个数据点)`;
    // Last scan time
    if (d.status.last_scan) {
      const dt = new Date(d.status.last_scan);
      document.getElementById('lastScanTime').textContent = '上次扫描: ' + dt.toLocaleTimeString('zh-CN');
    }
    updateHeader(d.status);
    if (d.markets_count > 0 && allMarkets.length === 0) {
      loadMarkets();
    }
  } catch(e) { console.error(e); }
}

function updateStats(s) {
  // Run time - store for local ticker
  engineRunning = s.is_running || false;
  lastRunSeconds = s.run_seconds || 0;
  // Sleep countdown - store for local ticker
  sleepRemaining = s.sleep_remaining || 0;
  scanInterval = s.scan_interval || 300;
  lastAction = s.current_action || '';
  updateScanCountdown();
  // Update immediately (local timer will tick it every second)
  document.getElementById('statRunTime').textContent = fmtRunTime(lastRunSeconds);
  document.getElementById('statRunStatus').textContent = s.is_running ? translateAction(s.current_action, s.sleep_remaining) : '已停止';

  // Current balance (initial + P&L)
  const balance = s.current_balance !== undefined ? s.current_balance : (s.bankroll || 0);
  document.getElementById('statBalance').textContent = fmtMoney(balance);
  document.getElementById('statBalance').className = 'stat-value ' + (balance >= (s.bankroll||0) ? 'green' : 'red');
  document.getElementById('statBankrollSub').textContent = '初始 ' + fmtMoney(s.bankroll || 0, 0);

  document.getElementById('statExposure').textContent = fmtMoney(s.total_exposure || 0);
  document.getElementById('statCash').textContent = fmtMoney(s.available_cash || 0);
  document.getElementById('statPositions').textContent = s.open_positions || 0;
  document.getElementById('statPositionsSub').textContent = '最大 ' + (s.max_positions || 10);

  const dpnl = s.daily_pnl || 0;
  const dpnlEl = document.getElementById('statDailyPnl');
  dpnlEl.textContent = fmtSigned(dpnl);
  dpnlEl.className = 'stat-value ' + (dpnl >= 0 ? 'green' : 'red');
  document.getElementById('statDailyTrades').textContent = fmtNum(s.daily_trades || 0) + ' 笔交易';

  const tpnl = s.total_pnl || 0;
  const tpnlEl = document.getElementById('statTotalPnl');
  tpnlEl.textContent = fmtSigned(tpnl);
  tpnlEl.className = 'stat-value ' + (tpnl >= 0 ? 'green' : 'red');
  document.getElementById('statTotalTrades').textContent = fmtNum(s.total_trades || 0) + ' 笔 | 胜率 ' + fmtPct(s.win_rate || 0, 0);

  // Cooldown alert
  const cdAlert = document.getElementById('equityCooldownAlert');
  if (cdAlert) {
    if (s.in_cooldown) {
      cdAlert.style.display = '';
      cdAlert.textContent = '⚠️ 冷却期中：连续亏损 ' + (s.consecutive_losses||0) + ' 次，'
        + Math.ceil((s.cooldown_remaining||0)/60) + ' 分钟后恢复交易';
    } else {
      cdAlert.style.display = 'none';
    }
  }

  // Equity tab stats
  if (document.getElementById('statWinRate')) {
    document.getElementById('statWinRate').textContent = (s.win_rate || 0).toFixed(1) + '%';
    document.getElementById('statConsecLoss').textContent = s.consecutive_losses || 0;
    const consecEl = document.getElementById('statConsecLoss');
    consecEl.className = 'stat-value ' + ((s.consecutive_losses||0) >= 2 ? 'red' : '');
  }
}

// ===== Opportunities =====
let lastOppsKey = '';
function updateOpportunities(opps) {
  const grid = document.getElementById('oppGrid');
  if (!grid) return;
  // Skip re-render if the data didn't actually change — otherwise the 5s
  // dashboard refresh replaces innerHTML every time and causes a visible
  // flicker.
  const key = JSON.stringify(opps);
  if (key === lastOppsKey) return;
  lastOppsKey = key;
  if (!opps || opps.length === 0) {
    grid.innerHTML = '<div class="empty"><div class="empty-icon">🔍</div><div class="empty-title">等待扫描发现机会...</div><div class="empty-hint">增强引擎正在分析订单簿、价格历史和聪明钱信号<br>点击下方按钮立即扫描一次</div><button class="btn btn-primary" onclick="manualScan()">立即扫描</button></div>';
    return;
  }
  grid.innerHTML = opps.slice(0, 30).map(o => {
    const stratMap = {
      'ExpiryYield+': 'tag-expiry', 'ExpiryYield': 'tag-expiry',
      'Arbitrage+': 'tag-arb', 'Arbitrage': 'tag-arb',
      'TweetArb+': 'tag-tweet', 'TweetArb': 'tag-tweet',
      'Momentum': 'tag-momentum', 'MeanReversion': 'tag-mr', 'SmartMoney': 'tag-sm'
    };
    const tagClass = stratMap[o.strategy] || 'tag-expiry';
    const ev = o.ev || {};
    const conf = o.confidence || 0;
    const confColor = conf >= 80 ? 'var(--green)' : conf >= 65 ? 'var(--orange)' : 'var(--red)';
    const annStr = o.annualized_yield > 0 ? ` | 年化 ${o.annualized_yield.toFixed(0)}%` : '';
    const daysStr = o.days_to_expiry !== undefined ? ` | ${o.days_to_expiry}天到期` : '';
    const yesPct = (o.yes_price !== undefined ? o.yes_price : (o.side === 'YES' ? o.price : 1 - o.price)) * 100;
    const noPct = 100 - yesPct;
    const analysis = o.analysis || {};
    let analysisHtml = '';
    if (analysis.liquidity_score !== undefined) analysisHtml += `<span>流动性: ${analysis.liquidity_score.toFixed(0)}</span>`;
    if (analysis.momentum !== undefined) analysisHtml += `<span>动量: ${analysis.momentum > 0 ? '+' : ''}${analysis.momentum.toFixed(2)}</span>`;
    if (analysis.volatility !== undefined) analysisHtml += `<span>波动率: ${(analysis.volatility*100).toFixed(1)}%</span>`;
    if (analysis.spread !== undefined && analysis.spread > 0) analysisHtml += `<span>价差: ${(analysis.spread*100).toFixed(1)}%</span>`;
    if (analysis.est_prob !== undefined) analysisHtml += `<span>估算概率: ${(analysis.est_prob*100).toFixed(1)}%</span>`;
    if (analysis.buy_ratio !== undefined) analysisHtml += `<span>鲸鱼买比: ${(analysis.buy_ratio*100).toFixed(0)}%</span>`;
    return `<div class="card">
      <div class="card-q">${o.question}</div>
      <div class="card-meta">
        <span class="tag ${tagClass}">${strategyCN(o.strategy)}</span>
        ${o.dry_run ? '<span class="tag tag-dry">模拟盘</span>' : ''}
        <span class="ring" title="置信度 ${conf.toFixed(0)}"><svg width="46" height="46"><circle class="ring-bg" cx="23" cy="23" r="19" fill="none" stroke-width="4"/><circle class="ring-fg" cx="23" cy="23" r="19" fill="none" stroke-width="4" stroke="${confColor}" stroke-dasharray="${(2*Math.PI*19).toFixed(1)}" stroke-dashoffset="${(2*Math.PI*19*(1-conf/100)).toFixed(1)}"/></svg><span style="color:${confColor}">${conf.toFixed(0)}</span></span>
      </div>
      <div class="prob-bar"><div class="prob-yes" style="width:${yesPct}%"></div><div class="prob-no" style="width:${noPct}%"></div></div>
      <div class="card-row"><span>方向</span><span>${o.side || 'BUY'}</span></div>
      <div class="card-row"><span>价格</span><span>$${o.price.toFixed(4)}</span></div>
      <div class="card-row"><span>利润率</span><span class="card-profit">+${(o.profit_pct * 100).toFixed(2)}%</span></div>
      <div class="card-row"><span>期望收益(EV)</span><span style="color:var(--green);font-weight:700;">${fmtSigned(ev.ev_usdc || 0)} (${fmtPct(ev.ev_pct || 0)})</span></div>
      <div class="card-row"><span>24h交易量</span><span>${fmtMoney(o.volume_24h || 0, 0)}</span></div>
      ${analysisHtml ? `<div style="display:flex;gap:10px;flex-wrap:wrap;font-size:11px;color:var(--muted);margin-top:8px;padding-top:8px;border-top:1px solid var(--border);">${analysisHtml}</div>` : ''}
      <div style="font-size:12px;color:var(--muted);margin-top:6px;">${annStr}${daysStr}</div>
    </div>`;
  }).join('');
}

// ===== Positions =====
// Format an ISO datetime as "MM-DD HH:MM" (local time)
function fmtDt(iso) {
  if (!iso) return '-';
  const d = new Date(iso);
  if (isNaN(d)) return '-';
  const M = String(d.getMonth()+1).padStart(2,'0');
  const D = String(d.getDate()).padStart(2,'0');
  const h = String(d.getHours()).padStart(2,'0');
  const m = String(d.getMinutes()).padStart(2,'0');
  return M + '-' + D + ' ' + h + ':' + m;
}

// Manual sell of an open position (dry-run simulates / live places a real order)
async function sellPosition(id) {
  if (!confirm('确定手动卖出该持仓？')) return;
  try {
    const r = await fetch('/api/sell', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({id: id})
    });
    const d = await r.json();
    if (d.success) {
      showToast(d.message || '已卖出', 'success');
      loadDashboard();
    } else {
      showToast(d.error || '卖出失败', 'error');
    }
  } catch(e) { showToast('网络错误: ' + e, 'error'); }
}

function updatePositions(positions) {
  const body = document.getElementById('posBody');
  if (!positions || positions.length === 0) {
    body.innerHTML = '<tr><td colspan="9" style="text-align:center;color:var(--muted);padding:40px;">暂无持仓</td></tr>';
    return;
  }
  body.innerHTML = positions.map(p => {
    const stratMap = {
      'ExpiryYield+': 'tag-expiry', 'ExpiryYield': 'tag-expiry',
      'Arbitrage+': 'tag-arb', 'Arbitrage': 'tag-arb',
      'TweetArb+': 'tag-tweet', 'TweetArb': 'tag-tweet',
      'Momentum': 'tag-momentum', 'MeanReversion': 'tag-mr', 'SmartMoney': 'tag-sm'
    };
    const tagClass = stratMap[p.strategy] || 'tag-expiry';
    const stMap = {
      'open': ['st-open', '持仓中'], 'won': ['st-won', '已赢'], 'lost': ['st-lost', '已输'],
      'closed_tp': ['st-tp', '止盈'], 'closed_take_profit': ['st-tp', '止盈'],
      'closed_trailing_stop': ['st-tp', '移动止损'], 'closed_stop_loss': ['st-lost', '止损']
    };
    const [stClass, stText] = stMap[p.status] || ['st-open', p.status];
    const pnl = p.pnl_usdc || 0;
    const pnlPct = p.cost_usdc ? (pnl / p.cost_usdc) * 100 : 0;
    const pnlColor = pnl > 0 ? 'var(--green)' : pnl < 0 ? 'var(--red)' : 'var(--muted)';
    const pnlBarW = Math.min(100, Math.abs(pnlPct));
    const pnlStr = `<div class="pnl-cell"><div class="pnl-bar"><span style="width:${pnlBarW}%;background:${pnlColor};"></span></div><span style="color:${pnlColor};font-weight:700;">${pnl !== 0 ? fmtSigned(pnl) : '-'}</span></div>`;
    const confStr = p.confidence ? `<span style="font-size:11px;color:${p.confidence>=75?'var(--green)':'var(--orange)'};">${p.confidence.toFixed(0)}</span>` : '-';
    return `<tr>
      <td><span class="tag ${tagClass}">${strategyCN(p.strategy)}</span></td>
      <td style="max-width:240px;">
        <div title="${p.question}" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${p.question}</div>
        <div style="font-size:11px;color:var(--muted);margin-top:2px;">${p.end_date ? '到期 ' + fmtDt(p.end_date) : '未设到期'}｜开仓 ${fmtDt(p.opened_at)}</div>
      </td>
      <td>${p.side || '-'}</td>
      <td>$${(p.entry_price || 0).toFixed(4)}</td>
      <td>${fmtNum(p.shares || 0)}</td>
      <td>${fmtMoney(p.cost_usdc || 0)}</td>
      <td>${confStr}</td>
      <td><span class="status-pill ${stClass}">${stText}</span></td>
      <td>${pnlStr}</td>
      <td>${p.status === 'open' && !['Arbitrage+','Arbitrage'].includes(p.strategy) ? `<button class="btn btn-ghost" style="font-size:11px;padding:3px 10px;" onclick="sellPosition('${p.id}')">卖出</button>` : '-'}</td>
    </tr>`;
  }).join('');
}

// ===== Equity Curve =====
// Compact money for axis labels: $123, $12K, $1.2M — always fits the canvas.
function compactMoney(n) {
  if (n === undefined || n === null || isNaN(n)) return '$0';
  if (Math.abs(n) >= 1000000) return '$' + (n / 1000000).toFixed(2) + 'M';
  if (Math.abs(n) >= 1000) return '$' + (n / 1000).toFixed(0) + 'K';
  return '$' + Math.round(n);
}

function updateEquityCurve(curve, status) {
  if (!curve || curve.length === 0) {
    const canvas = document.getElementById('equityChart');
    if (canvas) {
      const ctx = canvas.getContext('2d');
      ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.fillStyle = getComputedStyle(document.documentElement).getPropertyValue('--muted').trim() || '#8b949e';
    ctx.font = '16px sans-serif';
      ctx.textAlign = 'center';
      ctx.fillText('暂无资金曲线数据（启动交易后自动记录）', canvas.width/2, canvas.height/2);
    }
    if (document.getElementById('statMaxDD')) document.getElementById('statMaxDD').textContent = '-';
    if (document.getElementById('statROI')) document.getElementById('statROI').textContent = '-';
    return;
  }

  const canvas = document.getElementById('equityChart');
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  const W = canvas.width, H = canvas.height;
  const pad = {l: 56, r: 20, t: 24, b: 40};
  const cw = W - pad.l - pad.r, ch = H - pad.t - pad.b;

  ctx.clearRect(0, 0, W, H);

  // Extract balances and PnL
  const balances = curve.map(p => p.balance);
  const pnls = curve.map(p => p.pnl);
  const minBal = Math.min(...balances);
  const maxBal = Math.max(...balances);
  const range = Math.max(maxBal - minBal, 1);
  const yMin = minBal - range * 0.1;
  const yMax = maxBal + range * 0.1;

  // Grid lines
  ctx.strokeStyle = getComputedStyle(document.documentElement).getPropertyValue('--border').trim() || '#30363d';
  ctx.lineWidth = 1;
  ctx.font = '11px monospace';
  ctx.fillStyle = getComputedStyle(document.documentElement).getPropertyValue('--muted').trim() || '#8b949e';
  for (let i = 0; i <= 4; i++) {
    const y = pad.t + (ch / 4) * i;
    ctx.beginPath();
    ctx.moveTo(pad.l, y);
    ctx.lineTo(W - pad.r, y);
    ctx.stroke();
    const val = yMax - (yMax - yMin) * (i / 4);
    ctx.textAlign = 'right';
    ctx.fillText(compactMoney(val), pad.l - 8, y + 4);
  }

  // Zero P&L line (baseline = the balance recorded when the run started, so the
  // chart stays self-consistent even if the configured bankroll changes).
  const bankroll = (curve[0] && curve[0].balance) || status.bankroll || 0;
  if (bankroll > 0 && bankroll >= yMin && bankroll <= yMax) {
    const zeroY = pad.t + ch * (1 - (bankroll - yMin) / (yMax - yMin));
    ctx.strokeStyle = getComputedStyle(document.documentElement).getPropertyValue('--muted').trim() || '#8b949e';
    ctx.setLineDash([4, 4]);
    ctx.beginPath();
    ctx.moveTo(pad.l, zeroY);
    ctx.lineTo(W - pad.r, zeroY);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = getComputedStyle(document.documentElement).getPropertyValue('--muted').trim() || '#8b949e';
    ctx.textAlign = 'left';
    // Clamp the baseline label inside the canvas (avoid overflow at top edge)
    const zeroLblY = zeroY - 6 < pad.t + 6 ? zeroY + 16 : zeroY - 6;
    ctx.fillText('初始 ' + compactMoney(bankroll), pad.l + 4, zeroLblY);
  }

  // Draw balance line
  ctx.strokeStyle = getComputedStyle(document.documentElement).getPropertyValue('--primary').trim() || '#2f81f7';
  ctx.lineWidth = 2;
  ctx.beginPath();
  curve.forEach((p, i) => {
    const x = pad.l + (cw / Math.max(curve.length - 1, 1)) * i;
    const y = pad.t + ch * (1 - (p.balance - yMin) / (yMax - yMin));
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.stroke();

  // Fill area under curve
  ctx.lineTo(pad.l + cw, pad.t + ch);
  ctx.lineTo(pad.l, pad.t + ch);
  ctx.closePath();
  const grad = ctx.createLinearGradient(0, pad.t, 0, pad.t + ch);
  grad.addColorStop(0, 'rgba(47,129,247,0.2)');
  grad.addColorStop(1, 'rgba(47,129,247,0.01)');
  ctx.fillStyle = grad;
  ctx.fill();

  // Draw P&L line (secondary)
  const minPnl = Math.min(...pnls, 0);
  const maxPnl = Math.max(...pnls, 0);
  const pnlRange = Math.max(maxPnl - minPnl, 1);
  ctx.strokeStyle = getComputedStyle(document.documentElement).getPropertyValue('--purple').trim() || '#a371f7';
  ctx.lineWidth = 1.5;
  ctx.setLineDash([3, 3]);
  ctx.beginPath();
  curve.forEach((p, i) => {
    const x = pad.l + (cw / Math.max(curve.length - 1, 1)) * i;
    const y = pad.t + ch * (1 - (p.pnl - minPnl) / pnlRange);
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.stroke();
  ctx.setLineDash([]);

  // Last point marker
  const lastPt = curve[curve.length - 1];
  const lastX = pad.l + cw;
  const lastY = pad.t + ch * (1 - (lastPt.balance - yMin) / (yMax - yMin));
  ctx.fillStyle = lastPt.balance >= bankroll
    ? (getComputedStyle(document.documentElement).getPropertyValue('--green').trim() || '#3fb950')
    : (getComputedStyle(document.documentElement).getPropertyValue('--red').trim() || '#f85149');
  ctx.beginPath();
  ctx.arc(lastX, lastY, 5, 0, Math.PI * 2);
  ctx.fill();
  ctx.fillStyle = getComputedStyle(document.documentElement).getPropertyValue('--text').trim() || '#e6edf3';
  ctx.textAlign = 'right';
  ctx.font = '12px monospace';
  // Clamp the marker label inside the canvas: if it would clip the top,
  // draw it below the point instead
  const lblY = lastY - 10 < pad.t + 8 ? lastY + 18 : lastY - 10;
  ctx.fillText('$' + lastPt.balance.toFixed(2), lastX - 8, lblY);

  // X-axis labels (first and last timestamps)
  ctx.fillStyle = getComputedStyle(document.documentElement).getPropertyValue('--muted').trim() || '#8b949e';
  ctx.font = '10px monospace';
  ctx.textAlign = 'left';
  if (curve.length > 0) {
    const d0 = new Date(curve[0].t);
    ctx.fillText(d0.toLocaleTimeString(), pad.l, H - 10);
  }
  ctx.textAlign = 'right';
  if (curve.length > 0) {
    const dN = new Date(curve[curve.length-1].t);
    ctx.fillText(dN.toLocaleTimeString(), W - pad.r, H - 10);
  }

  // Calculate max drawdown and ROI
  let maxDD = 0, peak = balances[0];
  for (const b of balances) {
    if (b > peak) peak = b;
    const dd = (peak - b) / peak * 100;
    if (dd > maxDD) maxDD = dd;
  }
  const roi = bankroll > 0 ? ((balances[balances.length-1] - bankroll) / bankroll * 100) : 0;
  if (document.getElementById('statMaxDD')) {
    document.getElementById('statMaxDD').textContent = '-' + maxDD.toFixed(1) + '%';
  }
  if (document.getElementById('statROI')) {
    const roiEl = document.getElementById('statROI');
    roiEl.textContent = (roi >= 0 ? '+' : '') + roi.toFixed(2) + '%';
    roiEl.className = 'stat-value ' + (roi >= 0 ? 'green' : 'red');
  }
}

// ===== Manual Scan =====
async function manualScan() {
  const btn = document.getElementById('scanBtn');
  if (btn) { btn.textContent = '扫描中...'; btn.disabled = true; }
  try {
    const r = await fetch('/api/scan', {method: 'POST'});
    const d = await r.json();
    if (d.success) {
      showToast('扫描完成，发现 ' + (d.opportunities_count || 0) + ' 个机会', 'success');
      loadDashboard();
    } else {
      showToast('扫描失败: ' + (d.error || '未知错误'), 'error');
    }
  } catch(e) {
    showToast('网络错误: ' + e, 'error');
  }
  if (btn) { btn.textContent = '手动扫描'; btn.disabled = false; }
}

// ===== Toast Notifications =====
function showToast(msg, type) {
  let toast = document.getElementById('toastContainer');
  if (!toast) {
    toast = document.createElement('div');
    toast.id = 'toastContainer';
    toast.style.cssText = 'position:fixed;top:20px;right:20px;z-index:9999;display:flex;flex-direction:column;gap:8px;';
    document.body.appendChild(toast);
  }
  const el = document.createElement('div');
  const bg = type === 'success' ? 'var(--green)' : type === 'error' ? 'var(--red)' : 'var(--primary)';
  el.style.cssText = `background:${bg};color:#fff;padding:12px 20px;border-radius:8px;font-size:14px;font-weight:600;box-shadow:0 4px 12px rgba(0,0,0,0.3);animation:slideIn 0.3s ease;max-width:400px;`;
  el.textContent = msg;
  toast.appendChild(el);
  setTimeout(() => { el.style.opacity = '0'; el.style.transition = 'opacity 0.3s'; setTimeout(() => el.remove(), 300); }, 4000);
}

// ===== Export CSV =====
function exportPositionsCSV() {
  fetch('/api/dashboard').then(r => r.json()).then(d => {
    const positions = d.positions || [];
    if (positions.length === 0) {
      showToast('暂无持仓数据可导出', 'error');
      return;
    }
    const headers = ['策略', '市场', '方向', '入场价', '数量', '成本', '置信度', '状态', '盈亏'];
    const rows = positions.map(p => [
      strategyCN(p.strategy) || '', (p.question || '').replace(/,/g, ';'), p.side || '',
      p.entry_price || 0, p.shares || 0, p.cost_usdc || 0,
      p.confidence || 0, p.status || '', p.pnl_usdc || 0
    ]);
    const csv = '\uFEFF' + headers.join(',') + '\n' + rows.map(r => r.join(',')).join('\n');
    const blob = new Blob([csv], {type: 'text/csv;charset=utf-8'});
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'polymarket_positions_' + new Date().toISOString().slice(0,10) + '.csv';
    a.click();
    URL.revokeObjectURL(url);
    showToast('已导出 ' + positions.length + ' 条持仓记录', 'success');
  });
}

// ===== Strategy Breakdown =====
function updateStrategyBreakdown(breakdown) {
  const el = document.getElementById('strategyBreakdown');
  if (!el) return;
  if (!breakdown || breakdown.length === 0) {
    el.innerHTML = '<div class="empty"><div class="empty-icon">📈</div><div class="empty-title">暂无策略表现数据</div><div class="empty-hint">启动交易后将自动统计</div></div>';
    return;
  }
  el.innerHTML = `<table class="table">
    <thead><tr><th>策略</th><th>交易次数</th><th>胜</th><th>负</th><th>胜率</th><th>总盈亏</th><th>持仓投入</th></tr></thead>
    <tbody>
    ${breakdown.map(b => {
      const pnlColor = b.pnl >= 0 ? 'var(--green)' : 'var(--red)';
      return `<tr>
        <td><span class="tag ${(b.strategy||'').includes('Expiry')?'tag-expiry':(b.strategy||'').includes('Arb')?'tag-arb':(b.strategy||'').includes('Tweet')?'tag-tweet':(b.strategy||'').includes('Momentum')?'tag-momentum':(b.strategy||'').includes('Mean')?'tag-mr':'tag-sm'}">${strategyCN(b.strategy)}</span></td>
        <td>${fmtNum(b.trades)}</td>
        <td style="color:var(--green)">${fmtNum(b.wins)}</td>
        <td style="color:var(--red)">${fmtNum(b.losses)}</td>
        <td>${fmtPct(b.win_rate, 0)}</td>
        <td style="color:${pnlColor};font-weight:700;">${fmtSigned(b.pnl)}</td>
        <td>${fmtMoney(b.exposure, 0)}</td>
      </tr>`;
    }).join('')}
    </tbody>
  </table>`;
}

// ===== Risk Metrics =====
function updateRiskMetrics(positions, status) {
  if (!positions || positions.length === 0) {
    ['statMaxDrawdown','statROI2','statProfitFactor','statAvgTrade','statBestTrade','statWorstTrade','statTotalVolume','statAvgHoldTime'].forEach(id => {
      const el = document.getElementById(id);
      if (el) el.textContent = '-';
    });
    return;
  }
  // Calculate metrics from closed positions
  const closed = positions.filter(p => p.status !== 'open');
  const pnls = closed.map(p => p.pnl_usdc || 0);
  const wins = pnls.filter(p => p > 0);
  const losses = pnls.filter(p => p < 0);
  const totalWin = wins.reduce((a,b) => a+b, 0);
  const totalLoss = Math.abs(losses.reduce((a,b) => a+b, 0));
  const totalPnl = pnls.reduce((a,b) => a+b, 0);
  const totalVolume = positions.reduce((a,p) => a + (p.cost_usdc || 0), 0);

  // Profit factor = gross profit / gross loss
  const profitFactor = totalLoss > 0 ? (totalWin / totalLoss) : (totalWin > 0 ? 999 : 0);
  // ROI = total P&L / total invested
  const roi = totalVolume > 0 ? (totalPnl / totalVolume * 100) : 0;
  // Avg trade
  const avgTrade = pnls.length > 0 ? (totalPnl / pnls.length) : 0;
  // Best/worst
  const best = pnls.length > 0 ? Math.max(...pnls) : 0;
  const worst = pnls.length > 0 ? Math.min(...pnls) : 0;

  // Max drawdown from equity curve
  let maxDD = 0;
  const curve = status.equity_curve || [];

  const setText = (id, val, isPct) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = val;
  };
  setText('statProfitFactor', profitFactor >= 999 ? '\u221E' : profitFactor.toFixed(2));
  setText('statROI2', fmtPct(roi, 1));
  setText('statAvgTrade', fmtSigned(avgTrade));
  setText('statBestTrade', fmtSigned(best));
  setText('statWorstTrade', fmtSigned(worst));
  setText('statTotalVolume', fmtMoney(totalVolume, 0));
  setText('statAvgHoldTime', '-');

  // Max drawdown calculation
  if (curve.length >= 2) {
    let peak = curve[0].balance;
    let maxDrawdown = 0;
    for (const pt of curve) {
      if (pt.balance > peak) peak = pt.balance;
      const dd = (peak - pt.balance) / peak * 100;
      if (dd > maxDrawdown) maxDrawdown = dd;
    }
    setText('statMaxDrawdown', fmtPct(maxDrawdown, 1));
  } else {
    setText('statMaxDrawdown', '-');
  }
}

// ===== Tab Badges =====
function updateTabBadges(oppsCount, posCount) {
  const badgeOpps = document.getElementById('badgeOpps');
  const badgePos = document.getElementById('badgePos');
  if (badgeOpps) {
    if (oppsCount > 0) {
      badgeOpps.textContent = oppsCount;
      badgeOpps.style.display = 'inline-block';
    } else {
      badgeOpps.style.display = 'none';
    }
  }
  if (badgePos) {
    if (posCount > 0) {
      badgePos.textContent = posCount;
      badgePos.style.display = 'inline-block';
    } else {
      badgePos.style.display = 'none';
    }
  }
}


// ===== Markets =====
async function loadMarkets() {
  try {
    const r = await fetch('/api/markets?limit=300');
    allMarkets = await r.json();
    renderMarkets(allMarkets);
  } catch(e) { console.error(e); }
}

function renderMarkets(markets) {
  const grid = document.getElementById('marketGrid');
  if (!markets || markets.length === 0) {
    grid.innerHTML = '<div class="empty"><div class="empty-icon">📊</div><div class="empty-title">等待加载市场数据...</div><div class="empty-hint">首次扫描完成后将在此显示市场列表</div></div>';
    return;
  }
  grid.innerHTML = markets.slice(0, 60).map(m => {
    const yesP = (m.yes_price || 0) * 100;
    const noP = (m.no_price || 0) * 100;
    return `<div class="card">
      <div class="card-q">${m.question}</div>
      <div class="prob-bar"><div class="prob-yes" style="width:${yesP}%"></div><div class="prob-no" style="width:${noP}%"></div></div>
      <div class="card-row"><span>YES</span><span style="color:var(--green)">${yesP.toFixed(1)}%</span></div>
      <div class="card-row"><span>NO</span><span style="color:var(--red)">${noP.toFixed(1)}%</span></div>
      <div class="card-row"><span>24h交易量</span><span>${fmtMoney(m.volume_24h || 0, 0)}</span></div>
    </div>`;
  }).join('');
}

function searchMarkets(q) {
  if (!q) { renderMarkets(allMarkets); return; }
  const filtered = allMarkets.filter(m => m.question.toLowerCase().includes(q.toLowerCase()));
  renderMarkets(filtered);
}

// ===== Logs =====
function updateLogs(logs) {
  const box = document.getElementById('logBox');
  if (!logs || logs.length === 0) {
    box.innerHTML = '<div class="log-line log-info">等待日志...</div>';
    return;
  }
  box.innerHTML = logs.map(l => {
    let cls = 'log-info';
    if (l.includes('[WARNING]')) cls = 'log-warn';
    else if (l.includes('[ERROR]')) cls = 'log-error';
    else if (l.includes('[OK]') || l.includes('WON') || l.includes('OPENED')) cls = 'log-ok';
    return `<div class="log-line ${cls}">${l}</div>`;
  }).join('');
  box.scrollTop = box.scrollHeight;
}

// ===== Tabs =====
function switchTab(tab, evt) {
  currentTab = tab;
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  if (evt) {
    const tabEl = evt.target.closest('.tab');
    if (tabEl) tabEl.classList.add('active');
  }
  ['opps', 'positions', 'equity', 'strategy', 'markets', 'logs'].forEach(t => {
    const el = document.getElementById('tab-' + t);
    if (el) el.style.display = t === tab ? '' : 'none';
  });
}

// ===== Keyboard Shortcuts =====
document.addEventListener('keydown', (e) => {
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
  const tabs = ['opps', 'positions', 'equity', 'strategy', 'markets', 'logs'];
  const key = parseInt(e.key);
  if (key >= 1 && key <= tabs.length) {
    const tabEls = document.querySelectorAll('.tab');
    if (tabEls[key-1]) {
      tabEls[key-1].click();
    }
  }
});

// ===== Modals =====
async function showSetup() {
  // Prefill bankroll / mode from the current config (single source of truth)
  try {
    const r = await fetch('/api/config');
    const d = await r.json();
    if (d.config) {
      document.getElementById('setupBankroll').value = d.config.bankroll_usdc || 200;
      document.getElementById('setupMode').value = d.config.trading_mode || 'dry_run';
    }
  } catch(e) {}
  document.getElementById('setupModal').style.display = 'flex';
}
function showAuthorize() {
  if (!SecureCredentialManager_hasCreds()) {
    showSetup();
    return;
  }
  document.getElementById('authModal').style.display = 'flex';
  document.getElementById('authPassword').focus();
}
function SecureCredentialManager_hasCreds() {
  // We check via API
  return fetch('/api/status').then(r => r.json()).then(s => s.has_credentials);
}
function closeModal(id) { document.getElementById(id).style.display = 'none'; }

async function submitSetup() {
  const data = {
    password: document.getElementById('setupPassword').value,
    private_key: document.getElementById('setupPrivateKey').value,
    funder_address: document.getElementById('setupFunder').value,
    trading_mode: document.getElementById('setupMode').value,
    bankroll_usdc: parseFloat(document.getElementById('setupBankroll').value),
    signature_type: parseInt(document.getElementById('setupSigType').value),
    telegram_token: document.getElementById('setupTgToken').value,
    telegram_chat_id: document.getElementById('setupTgChat').value,
  };
  if (data.password.length < 6) { alert('密码至少6位'); return; }
  if (!data.private_key.startsWith('0x')) { alert('私钥格式错误'); return; }
  if (!data.funder_address.startsWith('0x')) { alert('钱包地址格式错误'); return; }

  const btn = document.querySelector('#setupModal .btn-primary');
  if (btn) { btn.textContent = '保存中...'; btn.disabled = true; }

  try {
    const r = await fetch('/api/setup', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(data)
    });
    const result = await r.json();
    if (result.success) {
      // Also persist bankroll / mode into trading_config.json (single source)
      try {
        await fetch('/api/config', {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({bankroll_usdc: data.bankroll_usdc, trading_mode: data.trading_mode})
        });
      } catch(e) {}
      closeModal('setupModal');
      alert('凭证已加密保存！现在输入密码授权即可启动全自动交易。');
      // Auto-open authorize modal
      document.getElementById('authPassword').value = data.password;
      showAuthorize();
    } else {
      alert('保存失败: ' + result.error);
    }
  } catch(e) { alert('网络错误: ' + e); }
  if (btn) { btn.textContent = '加密保存'; btn.disabled = false; }
}

// ===== Settings Panel =====
async function showSettings() {
  try {
    const r = await fetch('/api/config');
    const d = await r.json();
    const c = d.config || {};
    document.getElementById('cfgBankroll').value = c.bankroll_usdc || 200;
    document.getElementById('cfgMode').value = c.trading_mode || 'dry_run';
    document.getElementById('cfgScanInterval').value = c.scan_interval || 300;
    document.getElementById('cfgMaxPositions').value = c.max_positions || 10;
    document.getElementById('cfgMaxDaily').value = c.max_daily_trades || 8;
    document.getElementById('cfgMinConf').value = c.min_confidence || 75;
    document.getElementById('cfgMinEv').value = c.min_ev_pct || 1.5;
    document.getElementById('cfgTweetTp').value = c.tweetarb_tp_roi !== undefined ? c.tweetarb_tp_roi : 1.0;
    document.getElementById('cfgMakerBias').value = c.maker_bias_pct !== undefined ? c.maker_bias_pct : 0;
    document.getElementById('cfgMinLiq').value = c.min_liquidity !== undefined ? c.min_liquidity : 10;
    document.getElementById('cfgAnnualFloor').value = c.expiry_annualized_floor !== undefined ? c.expiry_annualized_floor : 20;
    document.getElementById('cfgFilterSpec').checked = !!c.filter_speculative;
    document.getElementById('cfgFilterCrypto').checked = !!c.filter_crypto_boundary;
    document.getElementById('cfgStrategyExpiry').checked = !!c.strategy_expiry;
    document.getElementById('cfgStrategyArb').checked = !!c.strategy_arb;
    document.getElementById('cfgStrategyTweet').checked = !!c.strategy_tweet;
    document.getElementById('settingsModal').style.display = 'flex';
  } catch(e) { showToast('网络错误: ' + e, 'error'); }
}

async function saveSettings() {
  const payload = {
    bankroll_usdc: parseFloat(document.getElementById('cfgBankroll').value),
    trading_mode: document.getElementById('cfgMode').value,
    scan_interval: parseInt(document.getElementById('cfgScanInterval').value),
    max_positions: parseInt(document.getElementById('cfgMaxPositions').value),
    max_daily_trades: parseInt(document.getElementById('cfgMaxDaily').value),
    min_confidence: parseInt(document.getElementById('cfgMinConf').value),
    min_ev_pct: parseFloat(document.getElementById('cfgMinEv').value),
    tweetarb_tp_roi: parseFloat(document.getElementById('cfgTweetTp').value),
    maker_bias_pct: parseFloat(document.getElementById('cfgMakerBias').value),
    min_liquidity: parseInt(document.getElementById('cfgMinLiq').value),
    expiry_annualized_floor: parseFloat(document.getElementById('cfgAnnualFloor').value),
    filter_speculative: document.getElementById('cfgFilterSpec').checked,
    filter_crypto_boundary: document.getElementById('cfgFilterCrypto').checked,
    strategy_expiry: document.getElementById('cfgStrategyExpiry').checked,
    strategy_arb: document.getElementById('cfgStrategyArb').checked,
    strategy_tweet: document.getElementById('cfgStrategyTweet').checked,
  };
  const btn = document.querySelector('#settingsModal .btn-primary');
  if (btn) { btn.textContent = '保存中...'; btn.disabled = true; }
  try {
    const r = await fetch('/api/config', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload)
    });
    const d = await r.json();
    if (d.success) {
      showToast(d.message || '配置已保存', 'success');
      closeModal('settingsModal');
      loadDashboard();
    } else {
      showToast(d.error || '保存失败', 'error');
    }
  } catch(e) { showToast('网络错误: ' + e, 'error'); }
  if (btn) { btn.textContent = '保存配置'; btn.disabled = false; }
}

async function resetStats() {
  if (!confirm('确定重置所有交易统计与持仓？历史盈亏将清空，并以当前初始资金重新开始。')) return;
  try {
    const r = await fetch('/api/reset-stats', {method: 'POST'});
    const d = await r.json();
    showToast(d.message || '已重置', 'success');
    closeModal('settingsModal');
    loadDashboard();
  } catch(e) { showToast('网络错误: ' + e, 'error'); }
}

async function runNetworkDiag() {
  const btn = document.getElementById('authDiagBtn');
  const box = document.getElementById('networkDiagBox');
  if (btn) { btn.innerHTML = '<span class="spinner"></span> 诊断中...'; btn.disabled = true; }
  box.style.display = 'none';

  try {
    const r = await fetch('/api/network-check');
    const result = await r.json();
    if (result.success && result.diagnostic) {
      const d = result.diagnostic;
      let html = '<div class="diag-title">网络诊断结果</div>';

      // Proxy
      const proxyStatus = d.proxy.configured
        ? (d.proxy.reachable
          ? '<span class="diag-value-ok">可达</span>'
          : '<span class="diag-value-fail">端口不可达</span>')
        : '<span class="diag-value-warn">未配置</span>';
      html += `<div class="diag-row"><span class="diag-label">代理状态</span><span>${proxyStatus}</span></div>`;
      if (d.proxy.configured) {
        html += `<div class="diag-row"><span class="diag-label">代理地址</span><span>${d.proxy.address}</span></div>`;
      }

      // DNS
      const dnsStatus = d.dns.clob
        ? '<span class="diag-value-ok">正常</span>'
        : '<span class="diag-value-fail">解析失败</span>';
      html += `<div class="diag-row"><span class="diag-label">DNS 解析</span><span>${dnsStatus}</span></div>`;
      if (d.dns.clob) {
        html += `<div class="diag-row"><span class="diag-label">CLOB IP</span><span>${d.dns.clob}</span></div>`;
      }

      // Polymarket connectivity
      const directOk = d.polymarket.clob_direct || d.polymarket.gamma_direct;
      const proxyOk = d.polymarket.clob_proxy || d.polymarket.gamma_proxy;
      const polyStatus = directOk
        ? '<span class="diag-value-ok">直连可达</span>'
        : (proxyOk
          ? '<span class="diag-value-ok">代理可达</span>'
          : '<span class="diag-value-fail">不可达</span>');
      html += `<div class="diag-row"><span class="diag-label">Polymarket 连通性</span><span>${polyStatus}</span></div>`;
      html += `<div class="diag-row"><span class="diag-label">CLOB 直连</span><span>${d.polymarket.clob_direct ? '<span class="diag-value-ok">OK</span>' : '<span class="diag-value-fail">FAIL</span>'}</span></div>`;
      html += `<div class="diag-row"><span class="diag-label">Gamma 直连</span><span>${d.polymarket.gamma_direct ? '<span class="diag-value-ok">OK</span>' : '<span class="diag-value-fail">FAIL</span>'}</span></div>`;
      if (d.proxy.configed) {
        html += `<div class="diag-row"><span class="diag-label">CLOB 代理</span><span>${d.polymarket.clob_proxy ? '<span class="diag-value-ok">OK</span>' : '<span class="diag-value-fail">FAIL</span>'}</span></div>`;
        html += `<div class="diag-row"><span class="diag-label">Gamma 代理</span><span>${d.polymarket.gamma_proxy ? '<span class="diag-value-ok">OK</span>' : '<span class="diag-value-fail">FAIL</span>'}</span></div>`;
      }

      // Diagnosis & suggestion
      html += `<div class="diag-row"><span class="diag-label">诊断结论</span><span>${d.diagnosis}</span></div>`;
      html += `<div class="diag-suggestion">建议：${d.suggestion}</div>`;

      box.innerHTML = html;
      box.style.display = 'block';
    } else {
      box.innerHTML = '<div class="diag-title">诊断失败</div><div>' + (result.error || '未知错误') + '</div>';
      box.style.display = 'block';
    }
  } catch(e) {
    box.innerHTML = '<div class="diag-title">诊断请求失败</div><div>' + e.message + '</div>';
    box.style.display = 'block';
  }
  if (btn) { btn.textContent = '网络诊断'; btn.disabled = false; }
}

async function doAuthorize(skipWallet) {
  const password = document.getElementById('authPassword').value;
  if (!password) { alert('请输入密码'); return; }
  const btn = document.getElementById('authSubmitBtn');
  const viewBtn = document.getElementById('authViewBtn');
  const errEl = document.getElementById('authError');
  const diagBox = document.getElementById('networkDiagBox');
  errEl.style.display = 'none';
  errEl.textContent = '';
  if (diagBox) diagBox.style.display = 'none';
  if (viewBtn) viewBtn.style.display = 'none';
  btn.innerHTML = '<span class="spinner"></span> 授权中...'; btn.disabled = true;

  try {
    const r = await fetch('/api/authorize', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ password, skip_wallet: !!skipWallet })
    });
    const result = await r.json();
    if (result.success) {
      closeModal('authModal');
      document.getElementById('authPassword').value = '';
      checkStatus();
      loadDashboard();
    } else {
      errEl.textContent = result.error || '授权失败';
      errEl.style.display = 'block';
      // Show "view-only" button if wallet init failed (not password error)
      if (result.can_view_only && !skipWallet) {
        if (viewBtn) viewBtn.style.display = '';
      }
    }
  } catch(e) {
    errEl.textContent = '网络错误: ' + e.message;
    errEl.style.display = 'block';
  }
  btn.innerHTML = '授权并启动'; btn.disabled = false;
}

async function emergencyStop() {
  if (!confirm('确认紧急停止？所有交易将立即停止，挂单将被取消。')) return;
  try {
    await fetch('/api/stop', { method: 'POST' });
    checkStatus();
  } catch(e) { alert('错误: ' + e); }
}

// Override showAuthorize to be async-aware
window.showAuthorize = async function() {
  const r = await fetch('/api/status');
  const s = await r.json();
  if (!s.has_credentials) {
    showSetup();
  } else {
    document.getElementById('authModal').style.display = 'flex';
    document.getElementById('authPassword').focus();
  }
};
</script>
</body>
</html>
"""


# ============================================================
#  Main Entry Point
# ============================================================

if __name__ == "__main__":
    # Install cryptography if not available
    try:
        import cryptography
    except ImportError:
        log.info("Installing cryptography library for secure encryption...")
        import subprocess
        subprocess.check_call([
            sys.executable, "-m", "pip", "install", "--target", str(LIBS_DIR),
            "cryptography"
        ], env={**os.environ, "PYTHONPATH": str(LIBS_DIR)})

    print("\n" + "=" * 60)
    print("  PolyAuto - 全自动交易系统")
    print("=" * 60)
    print()
    print("  浏览器打开: http://localhost:5000")
    print()
    print("  首次使用:")
    print("    1. 点击「配置」输入钱包凭证和密码")
    print("    2. 点击「授权并启动」输入密码")
    print("    3. 机器人自动开始扫描和交易")
    print("    4. 在网页上查看所有结果")
    print()
    print("  安全机制:")
    print("    - 私钥使用 AES + PBKDF2 加密存储")
    print("    - 密码不保存，每次授权需要输入")
    print("    - 紧急停止按钮一键终止所有交易")
    print("    - 所有操作在本地执行，不发送给任何第三方")
    print()
    print("=" * 60)
    print()

    log.info("Starting PolyAuto web server on http://localhost:5000")
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
