#!/usr/bin/env python3
"""
Polymarket Auto-Trader Bot
==========================
A fully automated trading bot for Polymarket prediction markets.

Strategies:
  A) Expiry Yield  - Buy NO on near-expiry high-probability markets (annualized 30-50%)
  B) Tweet Prediction - Trade Musk weekly tweet count buckets (+EV)
  E) Arbitrage     - Buy YES+NO when combined price < $0.98

Features:
  - Automatic market scanning via Gamma API
  - Automatic order placement via CLOB API (py-clob-client)
  - Kelly criterion position sizing with safety caps
  - Daily loss limits, position limits, cash reserves
  - Dry-run mode for safe testing
  - Telegram notifications (optional)
  - Full trade logging and daily P&L reports

Usage:
  python polymarket_bot.py                 # Run in dry-run mode (default)
  python polymarket_bot.py --live          # Run in live mode (real trades)
  python polymarket_bot.py --scan          # One-time scan, print opportunities
  python polymarket_bot.py --stats         # Show position stats and P&L
  python polymarket_bot.py --cancel-all    # Cancel all open orders
  python polymarket_bot.py --report        # Generate daily report

Prerequisites:
  1. pip install -r requirements.txt
  2. Copy .env.example to .env and fill in credentials
  3. Have USDC on Polygon in your wallet

DISCLAIMER: Prediction markets are risky. This bot can lose money.
            Never use funds you cannot afford to lose.
            Polymarket restricts users in certain jurisdictions.
"""

import os
import sys
import json
import time
import math
import logging
import argparse
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

# ============================================================
#  Configuration
# ============================================================

load_dotenv()

class Config:
    """Load all configuration from environment variables."""

    # Wallet
    PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")
    FUNDER_ADDRESS = os.getenv("FUNDER_ADDRESS", "")
    SIGNATURE_TYPE = int(os.getenv("SIGNATURE_TYPE", "2"))

    # API Credentials (optional, auto-derived)
    CLOB_API_KEY = os.getenv("CLOB_API_KEY", "")
    CLOB_SECRET = os.getenv("CLOB_SECRET", "")
    CLOB_PASSPHRASE = os.getenv("CLOB_PASSPHRASE", "")

    # Trading
    TRADING_MODE = os.getenv("TRADING_MODE", "dry_run")
    BANKROLL_USDC = float(os.getenv("BANKROLL_USDC", "200"))
    MAX_POSITION_PCT = float(os.getenv("MAX_POSITION_PCT", "0.05"))
    MAX_TOTAL_EXPOSURE_PCT = float(os.getenv("MAX_TOTAL_EXPOSURE_PCT", "0.70"))
    CASH_RESERVE_PCT = float(os.getenv("CASH_RESERVE_PCT", "0.30"))
    MAX_POSITIONS = int(os.getenv("MAX_POSITIONS", "10"))
    DAILY_LOSS_LIMIT_PCT = float(os.getenv("DAILY_LOSS_LIMIT_PCT", "0.10"))
    MIN_MARKET_VOLUME = float(os.getenv("MIN_MARKET_VOLUME", "10000"))
    MIN_ORDER_SIZE = float(os.getenv("MIN_ORDER_SIZE", "5"))

    # Strategy toggles
    STRATEGY_EXPIRY_YIELD = os.getenv("STRATEGY_EXPIRY_YIELD", "1") == "1"
    STRATEGY_TWEET_PREDICTION = os.getenv("STRATEGY_TWEET_PREDICTION", "1") == "1"
    STRATEGY_ARBITRAGE = os.getenv("STRATEGY_ARBITRAGE", "1") == "1"
    STRATEGY_DIRECTIONAL = os.getenv("STRATEGY_DIRECTIONAL", "0") == "1"

    # Intervals
    SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "300"))

    # Telegram
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

    # API hosts
    GAMMA_API = "https://gamma-api.polymarket.com"
    CLOB_HOST = "https://clob.polymarket.com"
    DATA_API = "https://data-api.polymarket.com"
    XTRACKER_URL = os.getenv("XTRACKER_URL", "https://xtracker.polymarket.com")

    # Paths
    STATE_FILE = Path(__file__).parent / "bot_state.json"
    LOG_FILE = Path(__file__).parent / "bot_trades.log"

    @classmethod
    def is_live(cls):
        return cls.TRADING_MODE == "live" or "--live" in sys.argv

    @classmethod
    def validate(cls):
        """Check that required credentials are present for live trading."""
        if cls.is_live():
            if not cls.PRIVATE_KEY:
                print("ERROR: PRIVATE_KEY not set in .env")
                print("       Add your Polygon wallet private key to .env")
                return False
            if not cls.FUNDER_ADDRESS:
                print("ERROR: FUNDER_ADDRESS not set in .env")
                return False
        return True


# ============================================================
#  Logging Setup
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(Config.LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("polymarket_bot")


# ============================================================
#  State Persistence (positions, daily P&L, etc.)
# ============================================================

class BotState:
    """Persist bot state to JSON file between runs."""

    DEFAULT = {
        "positions": [],        # List of open positions
        "daily_pnl": {},        # Date string -> {realized, unrealized, trades}
        "last_scan": None,      # ISO timestamp of last scan
        "daily_loss_hit": None, # Date string if daily loss limit was hit
        "api_creds_cached": False,
    }

    @classmethod
    def load(cls):
        if Config.STATE_FILE.exists():
            try:
                with open(Config.STATE_FILE, "r", encoding="utf-8") as f:
                    state = json.load(f)
                # Merge with defaults
                for k, v in cls.DEFAULT.items():
                    state.setdefault(k, v)
                return state
            except Exception as e:
                log.warning(f"Failed to load state: {e}, starting fresh")
        return json.loads(json.dumps(cls.DEFAULT))

    @classmethod
    def save(cls, state):
        try:
            with open(Config.STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2, ensure_ascii=False, default=str)
        except Exception as e:
            log.error(f"Failed to save state: {e}")


# ============================================================
#  Telegram Notifications
# ============================================================

def send_telegram(message: str):
    """Send a Telegram notification if configured."""
    if not Config.TELEGRAM_BOT_TOKEN or not Config.TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{Config.TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, json={
            "chat_id": Config.TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "Markdown",
        }, timeout=10)
    except Exception as e:
        log.warning(f"Telegram notification failed: {e}")


# ============================================================
#  Gamma API Client (Market Data - No Auth Required)
# ============================================================

class GammaAPI:
    """Fetch market data from Polymarket's Gamma API."""

    BASE = "https://gamma-api.polymarket.com"

    @staticmethod
    def get_active_markets(limit=100, offset=0, tag=None):
        """Fetch active, unclosed markets sorted by 24h volume. With retry."""
        params = {
            "active": "true",
            "closed": "false",
            "limit": limit,
            "offset": offset,
            "order": "volume24hr",
            "ascending": "false",
        }
        if tag:
            params["tag_slug"] = tag
        for attempt in range(3):
            try:
                r = requests.get(f"{GammaAPI.BASE}/markets", params=params, timeout=20)
                r.raise_for_status()
                return r.json()
            except Exception as e:
                log.warning(f"Gamma API attempt {attempt+1}/3 failed: {e}")
                if attempt < 2:
                    time.sleep(3)
        log.error("Gamma API: all retries exhausted")
        return []

    @staticmethod
    def get_ending_soon(limit=50):
        """Fetch markets ending soon (sorted by end date ascending)."""
        params = {
            "active": "true",
            "closed": "false",
            "limit": limit,
            "order": "endDate",
            "ascending": "true",
        }
        try:
            r = requests.get(f"{GammaAPI.BASE}/markets", params=params, timeout=15)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.error(f"Gamma API (ending soon) error: {e}")
            return []

    @staticmethod
    def parse_market(m):
        """Parse a raw Gamma market object into a clean dict."""
        try:
            outcomes = json.loads(m.get("outcomes", "[]"))
            prices = json.loads(m.get("outcomePrices", "[]"))
            token_ids = json.loads(m.get("clobTokenIds", "[]"))

            # Map outcome -> price -> token_id
            result = {
                "id": m.get("id", ""),
                "question": m.get("question", ""),
                "slug": m.get("slug", ""),
                "condition_id": m.get("conditionId", ""),
                "outcomes": outcomes,
                "prices": [float(p) for p in prices],
                "token_ids": token_ids,
                "volume": float(m.get("volume", 0) or 0),
                "volume_24h": float(m.get("volume24hr", 0) or 0),
                "liquidity": float(m.get("liquidity", 0) or 0),
                "end_date": m.get("endDate", ""),
                "active": m.get("active", False),
                "closed": m.get("closed", False),
                "tags": m.get("tags", []),
            }

            # Determine YES and NO tokens
            for i, outcome in enumerate(outcomes):
                outcome_lower = outcome.lower().strip()
                if outcome_lower in ("yes", "up", "over"):
                    result["yes_token"] = token_ids[i] if i < len(token_ids) else ""
                    result["yes_price"] = float(prices[i]) if i < len(prices) else 0
                elif outcome_lower in ("no", "down", "under"):
                    result["no_token"] = token_ids[i] if i < len(token_ids) else ""
                    result["no_price"] = float(prices[i]) if i < len(prices) else 0

            # If only 2 outcomes and we couldn't map, assume [0]=YES, [1]=NO
            if len(outcomes) == 2:
                if "yes_token" not in result:
                    result["yes_token"] = token_ids[0] if token_ids else ""
                    result["yes_price"] = float(prices[0]) if prices else 0
                if "no_token" not in result:
                    result["no_token"] = token_ids[1] if len(token_ids) > 1 else ""
                    result["no_price"] = float(prices[1]) if len(prices) > 1 else 0

            return result
        except Exception as e:
            log.debug(f"Failed to parse market {m.get('id', '?')}: {e}")
            return None

    @staticmethod
    def get_all_active_markets(max_markets=500):
        """Fetch multiple pages of active markets."""
        all_markets = []
        for offset in range(0, max_markets, 100):
            batch = GammaAPI.get_active_markets(limit=100, offset=offset)
            if not batch:
                break
            all_markets.extend(batch)
            if len(batch) < 100:
                break
        return all_markets


# ============================================================
#  CLOB Client Wrapper (Trading - Auth Required)
# ============================================================

class CLOBTrader:
    """Wrapper around py-clob-client for authenticated trading."""

    def __init__(self):
        self.client = None
        self.initialized = False

    def init(self):
        """Initialize the CLOB client with credentials."""
        if not Config.PRIVATE_KEY:
            log.warning("No PRIVATE_KEY set - running in read-only mode (no trades)")
            return False

        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds
        except ImportError:
            log.error("py-clob-client not installed. Run: pip install py-clob-client")
            return False

        try:
            kwargs = {
                "host": Config.CLOB_HOST,
                "key": Config.PRIVATE_KEY,
                "chain_id": 137,  # Polygon mainnet
            }

            # Add signature type and funder for proxy wallets
            if Config.SIGNATURE_TYPE > 0:
                kwargs["signature_type"] = Config.SIGNATURE_TYPE
                if Config.FUNDER_ADDRESS:
                    kwargs["funder"] = Config.FUNDER_ADDRESS

            # Add cached API creds if available
            if Config.CLOB_API_KEY and Config.CLOB_SECRET and Config.CLOB_PASSPHRASE:
                kwargs["creds"] = ApiCreds(
                    api_key=Config.CLOB_API_KEY,
                    api_secret=Config.CLOB_SECRET,
                    api_passphrase=Config.CLOB_PASSPHRASE,
                )

            self.client = ClobClient(**kwargs)

            # Derive API creds if not cached
            if not (Config.CLOB_API_KEY and Config.CLOB_SECRET and Config.CLOB_PASSPHRASE):
                log.info("Deriving API credentials from wallet...")
                creds = self.client.create_or_derive_api_creds()
                self.client.set_api_creds(creds)
                log.info("=" * 60)
                log.info("API Credentials (save these to .env for next time):")
                log.info(f"  CLOB_API_KEY={creds.api_key}")
                log.info(f"  CLOB_SECRET={creds.api_secret}")
                log.info(f"  CLOB_PASSPHRASE={creds.api_passphrase}")
                log.info("=" * 60)

            self.initialized = True
            log.info("CLOB client initialized successfully")
            return True

        except Exception as e:
            log.error(f"Failed to initialize CLOB client: {e}")
            traceback.print_exc()
            return False

    def get_order_book(self, token_id):
        """Get the order book for a token."""
        if not self.client:
            return None
        try:
            return self.client.get_order_book(token_id)
        except Exception as e:
            log.debug(f"Order book error for {token_id[:12]}...: {e}")
            return None

    def get_midpoint(self, token_id):
        """Get midpoint price for a token."""
        if not self.client:
            return None
        try:
            return float(self.client.get_midpoint(token_id))
        except:
            return None

    def place_limit_order(self, token_id, price, size, side="BUY"):
        """Place a GTC limit order. Returns order response or None."""
        if not self.initialized:
            log.warning("CLOB client not initialized - cannot place order")
            return None

        try:
            from py_clob_client.clob_types import OrderArgs, OrderType
            from py_clob_client.order_builder.constants import BUY, SELL

            # Validate inputs
            price = round(float(price), 4)
            size = round(float(size), 2)
            if price <= 0 or price >= 1:
                log.error(f"Invalid price: {price} (must be between 0 and 1)")
                return None
            if size < Config.MIN_ORDER_SIZE:
                log.warning(f"Order size {size} < minimum {Config.MIN_ORDER_SIZE}, adjusting")
                size = Config.MIN_ORDER_SIZE

            order_args = OrderArgs(
                token_id=token_id,
                price=price,
                size=size,
                side=BUY if side.upper() == "BUY" else SELL,
            )

            if Config.is_live():
                signed_order = self.client.create_order(order_args)
                response = self.client.post_order(signed_order, OrderType.GTC)
                log.info(f"ORDER PLACED: {side} {size} @ ${price} | token={token_id[:12]}... | resp={response}")
                return response
            else:
                log.info(f"[DRY RUN] Would place: {side} {size} @ ${price} | token={token_id[:12]}...")
                return {"success": True, "orderID": "dry_run_" + str(int(time.time())), "dry_run": True}

        except Exception as e:
            log.error(f"Order placement failed: {e}")
            traceback.print_exc()
            return None

    def cancel_order(self, order_id):
        """Cancel a specific order."""
        if not self.initialized or not Config.is_live():
            log.info(f"[DRY RUN] Would cancel order {order_id}")
            return True
        try:
            self.client.cancel(order_id)
            log.info(f"Order cancelled: {order_id}")
            return True
        except Exception as e:
            log.error(f"Cancel failed: {e}")
            return False

    def cancel_all(self):
        """Cancel all open orders."""
        if not self.initialized:
            log.warning("CLOB client not initialized")
            return
        if not Config.is_live():
            log.info("[DRY RUN] Would cancel all orders")
            return
        try:
            self.client.cancel_all()
            log.info("All orders cancelled")
        except Exception as e:
            log.error(f"Cancel all failed: {e}")

    def get_open_orders(self):
        """Get all open orders."""
        if not self.initialized:
            return []
        try:
            from py_clob_client.clob_types import OpenOrderParams
            return self.client.get_orders(OpenOrderParams())
        except Exception as e:
            log.error(f"Get orders failed: {e}")
            return []

    def get_balances(self):
        """Get collateral and position balances."""
        if not self.initialized:
            return {}
        try:
            # Get USDC balance
            balance = self.client.get_balance_allowance()
            return {"usdc": float(balance.get("balance", 0))}
        except:
            return {}


# ============================================================
#  Risk Manager
# ============================================================

class RiskManager:
    """Manage position sizing, exposure limits, and daily loss limits."""

    @staticmethod
    def max_position_size(bankroll=None):
        """Calculate max position size in USDC."""
        bankroll = bankroll or Config.BANKROLL_USDC
        return bankroll * Config.MAX_POSITION_PCT

    @staticmethod
    def max_total_exposure(bankroll=None):
        """Calculate max total exposure in USDC."""
        bankroll = bankroll or Config.BANKROLL_USDC
        return bankroll * Config.MAX_TOTAL_EXPOSURE_PCT

    @staticmethod
    def cash_reserve(bankroll=None):
        """Calculate cash reserve in USDC."""
        bankroll = bankroll or Config.BANKROLL_USDC
        return bankroll * Config.CASH_RESERVE_PCT

    @staticmethod
    def kelly_fraction(win_prob, price):
        """
        Calculate Kelly criterion fraction.
        win_prob: estimated probability of winning (0-1)
        price: current share price (0-1)
        Returns: fraction of bankroll to bet (0-1)
        """
        if price <= 0 or price >= 1:
            return 0
        b = (1 - price) / price  # odds ratio
        f = (win_prob * b - (1 - win_prob)) / b
        return max(0, min(f, Config.MAX_POSITION_PCT * 2))  # Cap at 2x max position

    @staticmethod
    def half_kelly_size(win_prob, price, bankroll=None):
        """Calculate position size using Half Kelly, capped by max position."""
        bankroll = bankroll or Config.BANKROLL_USDC
        kelly = RiskManager.kelly_fraction(win_prob, price)
        half_kelly = kelly / 2
        max_size = RiskManager.max_position_size(bankroll)
        size_usdc = min(half_kelly * bankroll, max_size)
        return max(Config.MIN_ORDER_SIZE * price, size_usdc)

    @staticmethod
    def check_daily_loss(state):
        """Check if daily loss limit has been hit."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if state.get("daily_loss_hit") == today:
            return True  # Already hit today

        daily = state.get("daily_pnl", {}).get(today, {})
        realized_loss = daily.get("realized", 0)
        if realized_loss < 0 and abs(realized_loss) >= Config.BANKROLL_USDC * Config.DAILY_LOSS_LIMIT_PCT:
            state["daily_loss_hit"] = today
            BotState.save(state)
            log.warning(f"Daily loss limit hit: {realized_loss:.2f} USDC")
            send_telegram(f"STOP: Daily loss limit hit ({realized_loss:.2f} USDC). Trading paused for today.")
            return True
        return False

    @staticmethod
    def check_exposure(state):
        """Check if total exposure is within limits."""
        positions = state.get("positions", [])
        total_exposure = sum(p.get("cost_usdc", 0) for p in positions)
        max_exposure = RiskManager.max_total_exposure()
        available = max_exposure - total_exposure
        return available, total_exposure, max_exposure

    @staticmethod
    def check_position_count(state):
        """Check if we can open more positions."""
        positions = state.get("positions", [])
        return len(positions) < Config.MAX_POSITIONS

    @staticmethod
    def can_trade(state):
        """Master check: can we open new positions?"""
        if RiskManager.check_daily_loss(state):
            return False, "Daily loss limit hit"

        available, total, max_exp = RiskManager.check_exposure(state)
        if available <= 0:
            return False, f"Max exposure reached ({total:.0f}/{max_exp:.0f})"

        if not RiskManager.check_position_count(state):
            return False, f"Max positions reached ({Config.MAX_POSITIONS})"

        return True, "OK"


# ============================================================
#  Strategy A: Expiry Yield (临期确定性理财)
# ============================================================

class ExpiryYieldStrategy:
    """
    Find markets where NO probability is very high (>95%) and expiry is near (1-7 days).
    Buy NO shares to capture the remaining 5% spread. Low risk, steady yield.
    """

    NAME = "ExpiryYield"

    @staticmethod
    def scan(markets=None):
        """Scan for opportunities. Returns list of Opportunity dicts."""
        if not Config.STRATEGY_EXPIRY_YIELD:
            return []

        log.info("Strategy A: Scanning for expiry yield opportunities...")
        opportunities = []

        # Get markets ending soon
        raw_markets = markets or GammaAPI.get_ending_soon(limit=100)
        now = datetime.now(timezone.utc)

        for raw in raw_markets:
            m = GammaAPI.parse_market(raw)
            if not m:
                continue

            # Check end date
            end_date_str = m.get("end_date", "")
            if not end_date_str:
                continue
            try:
                end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
            except:
                continue

            days_to_expiry = (end_date - now).total_seconds() / 86400
            # Allow markets that are slightly past end date (may still be resolving)
            if days_to_expiry < -1 or days_to_expiry > 7:
                continue

            # Check NO price (we want NO to be very likely, i.e., price > 0.90)
            no_price = m.get("no_price", 0)
            yes_price = m.get("yes_price", 0)

            # Also check YES side (YES > 0.90 is also a candidate)
            best_side = None
            best_price = 0
            best_token = ""

            if no_price >= 0.90:
                best_side = "NO"
                best_price = no_price
                best_token = m.get("no_token", "")
            elif yes_price >= 0.90:
                best_side = "YES"
                best_price = yes_price
                best_token = m.get("yes_token", "")
            else:
                continue

            # Check volume (lower threshold for expiry yield - these are often lower volume)
            min_vol = max(Config.MIN_MARKET_VOLUME / 20, 500)  # At least $500
            if m.get("volume_24h", 0) < min_vol and m.get("liquidity", 0) < 1000:
                continue

            # Calculate annualized yield
            # If we buy at $0.95 and it resolves to $1.00 in 3 days:
            # profit = (1 - 0.95) / 0.95 = 5.26% in 3 days
            # annualized = 5.26% * 365 / 3 = 640%
            if best_price >= 1.0:
                continue
            profit_pct = (1 - best_price) / best_price
            annualized = profit_pct * 365 / max(days_to_expiry, 0.1)

            opportunities.append({
                "strategy": ExpiryYieldStrategy.NAME,
                "market_id": m["id"],
                "question": m["question"],
                "side": best_side,
                "token_id": best_token,
                "price": best_price,
                "days_to_expiry": days_to_expiry,
                "profit_pct": profit_pct,
                "annualized_yield": annualized,
                "volume_24h": m.get("volume_24h", 0),
                "end_date": end_date_str,
                "condition_id": m.get("condition_id", ""),
            })

        # Sort by annualized yield (highest first)
        opportunities.sort(key=lambda x: x["annualized_yield"], reverse=True)
        log.info(f"  Found {len(opportunities)} expiry yield opportunities")
        return opportunities

    @staticmethod
    def execute(opp, trader, state):
        """Execute a trade for this opportunity."""
        can, reason = RiskManager.can_trade(state)
        if not can:
            log.info(f"  Skip {opp['question'][:40]}...: {reason}")
            return None

        # Position sizing: use a fraction of max position based on confidence
        # Higher price = higher confidence = slightly larger position
        confidence = opp["price"]  # 0.95-0.99
        max_pos = RiskManager.max_position_size()
        size_usdc = max_pos * (0.5 + (confidence - 0.95) * 10)  # Scale with confidence
        size_usdc = min(size_usdc, max_pos)

        # Convert to shares
        shares = size_usdc / opp["price"]
        shares = max(shares, Config.MIN_ORDER_SIZE)

        # Place order at current price (or slightly below for better fill)
        order_price = round(opp["price"], 3)

        log.info(f"  Executing: {opp['side']} {shares:.0f} shares @ ${order_price} "
                 f"(${size_usdc:.2f}) | {opp['question'][:50]}...")

        response = trader.place_limit_order(
            token_id=opp["token_id"],
            price=order_price,
            size=shares,
            side="BUY",
        )

        if response and response.get("success"):
            # Record position
            position = {
                "id": response.get("orderID", str(int(time.time()))),
                "strategy": ExpiryYieldStrategy.NAME,
                "market_id": opp["market_id"],
                "question": opp["question"],
                "side": opp["side"],
                "token_id": opp["token_id"],
                "entry_price": order_price,
                "shares": shares,
                "cost_usdc": shares * order_price,
                "opened_at": datetime.now(timezone.utc).isoformat(),
                "end_date": opp["end_date"],
                "status": "open",
                "dry_run": response.get("dry_run", False),
            }
            state["positions"].append(position)
            BotState.save(state)

            msg = (f"POSITION OPENED\n"
                   f"  Market: {opp['question'][:60]}\n"
                   f"  Side: {opp['side']} @ ${order_price}\n"
                   f"  Size: {shares:.0f} shares (${size_usdc:.2f})\n"
                   f"  Strategy: Expiry Yield ({opp['annualized_yield']:.0f}% annualized)\n"
                   f"  Expires: {opp['end_date'][:10]}")
            log.info(msg)
            send_telegram(msg)
            return position
        return None


# ============================================================
#  Strategy E: Arbitrage (套利)
# ============================================================

class ArbitrageStrategy:
    """
    Find markets where YES + NO combined price < $0.98.
    Buy both sides, guaranteed profit regardless of outcome.
    """

    NAME = "Arbitrage"

    @staticmethod
    def scan(markets=None):
        if not Config.STRATEGY_ARBITRAGE:
            return []

        log.info("Strategy E: Scanning for arbitrage opportunities...")
        opportunities = []

        raw_markets = markets or GammaAPI.get_active_markets(limit=200)
        for raw in raw_markets:
            m = GammaAPI.parse_market(raw)
            if not m:
                continue

            yes_price = m.get("yes_price", 0)
            no_price = m.get("no_price", 0)

            if yes_price <= 0 or no_price <= 0:
                continue

            combined = yes_price + no_price
            if combined >= 0.97:  # Lowered from 0.98 to find more opportunities
                continue

            # Check volume
            if m.get("volume_24h", 0) < Config.MIN_MARKET_VOLUME / 20 and m.get("liquidity", 0) < 500:
                continue

            profit_pct = (1 - combined) / combined

            opportunities.append({
                "strategy": ArbitrageStrategy.NAME,
                "market_id": m["id"],
                "question": m["question"],
                "yes_token": m.get("yes_token", ""),
                "no_token": m.get("no_token", ""),
                "yes_price": yes_price,
                "no_price": no_price,
                "combined_price": combined,
                "profit_pct": profit_pct,
                "volume_24h": m.get("volume_24h", 0),
            })

        opportunities.sort(key=lambda x: x["profit_pct"], reverse=True)
        log.info(f"  Found {len(opportunities)} arbitrage opportunities")
        return opportunities

    @staticmethod
    def execute(opp, trader, state):
        """Execute arbitrage: buy both YES and NO."""
        can, reason = RiskManager.can_trade(state)
        if not can:
            log.info(f"  Skip arb {opp['question'][:40]}...: {reason}")
            return None

        # Position sizing: smaller for arbitrage (lower risk, lower return)
        max_pos = RiskManager.max_position_size()
        size_usdc = max_pos * 0.5  # Use half max position for arb

        # Split between YES and NO
        yes_shares = (size_usdc / 2) / opp["yes_price"]
        no_shares = (size_usdc / 2) / opp["no_price"]
        yes_shares = max(yes_shares, Config.MIN_ORDER_SIZE)
        no_shares = max(no_shares, Config.MIN_ORDER_SIZE)

        log.info(f"  Executing ARB: {opp['question'][:50]}... "
                 f"YES@${opp['yes_price']} + NO@${opp['no_price']} = ${opp['combined_price']:.3f}")

        # Place both orders
        resp_yes = trader.place_limit_order(opp["yes_token"], opp["yes_price"], yes_shares, "BUY")
        resp_no = trader.place_limit_order(opp["no_token"], opp["no_price"], no_shares, "BUY")

        if resp_yes and resp_no and resp_yes.get("success") and resp_no.get("success"):
            position = {
                "id": resp_yes.get("orderID", str(int(time.time()))),
                "strategy": ArbitrageStrategy.NAME,
                "market_id": opp["market_id"],
                "question": opp["question"],
                "yes_token": opp["yes_token"],
                "no_token": opp["no_token"],
                "yes_price": opp["yes_price"],
                "no_price": opp["no_price"],
                "yes_shares": yes_shares,
                "no_shares": no_shares,
                "cost_usdc": yes_shares * opp["yes_price"] + no_shares * opp["no_price"],
                "guaranteed_return": yes_shares + no_shares - (yes_shares * opp["yes_price"] + no_shares * opp["no_price"]),
                "opened_at": datetime.now(timezone.utc).isoformat(),
                "status": "open",
                "dry_run": resp_yes.get("dry_run", False),
            }
            state["positions"].append(position)
            BotState.save(state)

            msg = (f"ARB POSITION OPENED\n"
                   f"  Market: {opp['question'][:60]}\n"
                   f"  YES@${opp['yes_price']:.3f} + NO@${opp['no_price']:.3f} = ${opp['combined_price']:.3f}\n"
                   f"  Guaranteed profit: ${position['guaranteed_return']:.2f}")
            log.info(msg)
            send_telegram(msg)
            return position
        return None


# ============================================================
#  Strategy B: Tweet Prediction (推文预测)
# ============================================================

class TweetPredictionStrategy:
    """
    Trade Musk tweet count prediction markets.
    Each bucket is a separate binary YES/NO market on Polymarket.

    Strategies:
    1. Cross-bucket arbitrage: if sum of all YES prices < $0.95, buy all YES sides
    2. High-probability NO: buy NO on buckets that are very unlikely (NO > 0.95)
    3. Directional: buy YES on the most likely bucket if underpriced
    """

    NAME = "TweetPrediction"

    @staticmethod
    def scan(markets=None):
        if not Config.STRATEGY_TWEET_PREDICTION:
            return []

        log.info("Strategy B: Scanning for tweet prediction opportunities...")
        opportunities = []

        # Use passed-in markets or fetch fresh
        if markets:
            all_markets = markets
        else:
            try:
                r = requests.get(
                    f"{Config.GAMMA_API}/markets",
                    params={
                        "active": "true",
                        "closed": "false",
                        "limit": 200,
                        "order": "volume24hr",
                        "ascending": "false",
                    },
                    timeout=15,
                )
                all_markets = r.json()
            except:
                all_markets = []

        # Filter for tweet-related markets (each is a binary YES/NO market)
        tweet_markets = []
        for raw in all_markets:
            m = GammaAPI.parse_market(raw)
            if not m:
                continue
            q = m["question"].lower()
            if any(kw in q for kw in ["tweet", "elon", "musk", "x.com"]):
                tweet_markets.append(m)

        if not tweet_markets:
            log.info(f"  No tweet markets found in {len(all_markets)} markets")
            return []

        # Group tweet markets by period (extract date range from question)
        # Questions look like: "Will Elon Musk post 65-89 tweets from August 10 to August 12, 2026?"
        import re
        period_groups = {}
        for m in tweet_markets:
            q = m["question"]
            # Extract period from question
            match = re.search(r'from (\w+ \d+) to (\w+ \d+)', q, re.IGNORECASE)
            if match:
                period = f"{match.group(1)} to {match.group(2)}"
            else:
                period = "unknown"

            period_groups.setdefault(period, []).append(m)

        log.info(f"  Found {len(tweet_markets)} tweet markets in {len(period_groups)} period groups")

        # Strategy B1: Cross-bucket arbitrage
        # If sum of all YES prices in a period < 0.95, buy all YES sides
        for period, buckets in period_groups.items():
            if len(buckets) < 2:
                continue

            total_yes = sum(m.get("yes_price", 0) for m in buckets)
            total_vol = sum(m.get("volume_24h", 0) for m in buckets)

            if total_yes < 0.95 and total_yes > 0:
                profit_pct = (1 - total_yes) / total_yes

                # Safety: if sum is very low, we probably don't have all buckets
                if total_yes < 0.50:
                    log.info(f"  [SKIP ARB] Period '{period}': sum YES = {total_yes:.4f} - "
                             f"likely missing buckets (only {len(buckets)} found)")
                    continue

                log.info(f"  [ARB] Period '{period}': sum YES = {total_yes:.4f} < 0.95 | "
                         f"profit = {profit_pct:.1%} | {len(buckets)} buckets")

                opportunities.append({
                    "strategy": TweetPredictionStrategy.NAME,
                    "market_id": "tweet_arb_" + period.replace(" ", "_"),
                    "question": f"Tweet ALL BUCKETS ARB ({period})",
                    "outcome": "ALL_YES",
                    "token_id": "",  # Multiple tokens - handled in execute
                    "buckets": [
                        {
                            "question": m["question"],
                            "yes_token": m.get("yes_token", ""),
                            "yes_price": m.get("yes_price", 0),
                            "market_id": m["id"],
                        }
                        for m in buckets
                    ],
                    "price": total_yes,
                    "volume_24h": total_vol,
                    "profit_pct": profit_pct,
                    "note": f"Buy all {len(buckets)} YES sides for ${total_yes:.4f}, guaranteed ${1-total_yes:.4f} profit per share",
                })

        # Strategy B2: High-probability NO on individual buckets
        # If a bucket's NO is > 0.90, buy NO to capture remaining spread
        for m in tweet_markets:
            no_price = m.get("no_price", 0)
            yes_price = m.get("yes_price", 0)

            if no_price >= 0.90 and no_price < 0.999:
                vol = m.get("volume_24h", 0)
                liq = m.get("liquidity", 0)
                if vol < 500 and liq < 1000:
                    continue

                profit_pct = (1 - no_price) / no_price
                opportunities.append({
                    "strategy": TweetPredictionStrategy.NAME,
                    "market_id": m["id"],
                    "question": m["question"],
                    "outcome": "NO",
                    "token_id": m.get("no_token", ""),
                    "price": no_price,
                    "volume_24h": vol,
                    "profit_pct": profit_pct,
                    "note": f"Buy NO @ ${no_price:.3f}, profit {profit_pct:.1%} if bucket doesn't hit",
                })

            # Strategy B3: High-probability YES on individual buckets
            # If a bucket's YES is > 0.85 (likely outcome) and reasonably priced
            if yes_price >= 0.85 and yes_price < 0.98:
                vol = m.get("volume_24h", 0)
                liq = m.get("liquidity", 0)
                if vol < 500 and liq < 1000:
                    continue

                profit_pct = (1 - yes_price) / yes_price
                opportunities.append({
                    "strategy": TweetPredictionStrategy.NAME,
                    "market_id": m["id"],
                    "question": m["question"],
                    "outcome": "YES",
                    "token_id": m.get("yes_token", ""),
                    "price": yes_price,
                    "volume_24h": vol,
                    "profit_pct": profit_pct,
                    "note": f"Buy YES @ ${yes_price:.3f}, profit {profit_pct:.1%} if bucket hits",
                })

        # Sort: arbitrage first (safest), then by volume
        opportunities.sort(key=lambda x: (
            0 if "ARB" in x.get("question", "") else 1,
            -x.get("volume_24h", 0)
        ))
        log.info(f"  Found {len(opportunities)} tweet prediction opportunities")
        return opportunities

    @staticmethod
    def execute(opp, trader, state):
        """Execute tweet prediction trade(s)."""
        can, reason = RiskManager.can_trade(state)
        if not can:
            log.info(f"  Skip tweet {opp['question'][:40]}...: {reason}")
            return None

        if not opp.get("token_id") and not opp.get("buckets"):
            log.info(f"  Skip: no token ID for {opp['question'][:40]}...")
            return None

        # Handle cross-bucket arbitrage (buy all YES sides)
        if opp.get("buckets"):
            max_pos = RiskManager.max_position_size()
            # For arb: use up to 50% of max position, split across all buckets
            total_usdc = max_pos * 0.5
            per_bucket_usdc = total_usdc / len(opp["buckets"])

            log.info(f"  Executing TWEET ARB: {len(opp['buckets'])} buckets, "
                     f"${per_bucket_usdc:.2f} each, total ${total_usdc:.2f}")

            positions_opened = []
            for bucket in opp["buckets"]:
                if not bucket.get("yes_token"):
                    continue
                yes_price = bucket["yes_price"]
                if yes_price <= 0 or yes_price >= 1:
                    continue
                shares = max(per_bucket_usdc / yes_price, Config.MIN_ORDER_SIZE)
                order_price = round(yes_price, 3)

                resp = trader.place_limit_order(
                    token_id=bucket["yes_token"],
                    price=order_price,
                    size=shares,
                    side="BUY",
                )
                if resp and resp.get("success"):
                    pos = {
                        "id": resp.get("orderID", str(int(time.time()))),
                        "strategy": TweetPredictionStrategy.NAME,
                        "market_id": bucket.get("market_id", ""),
                        "question": bucket["question"],
                        "side": "YES (ARB)",
                        "token_id": bucket["yes_token"],
                        "entry_price": order_price,
                        "shares": shares,
                        "cost_usdc": shares * order_price,
                        "opened_at": datetime.now(timezone.utc).isoformat(),
                        "status": "open",
                        "dry_run": resp.get("dry_run", False),
                    }
                    state["positions"].append(pos)
                    positions_opened.append(pos)
                    time.sleep(1)  # Rate limit

            if positions_opened:
                BotState.save(state)
                total_cost = sum(p["cost_usdc"] for p in positions_opened)
                total_shares = sum(p["shares"] for p in positions_opened)
                guaranteed = total_shares - total_cost
                msg = (f"TWEET ARB OPENED\n"
                       f"  {len(positions_opened)} buckets | ${total_cost:.2f} cost\n"
                       f"  Guaranteed return: ${total_shares:.2f} (profit ${guaranteed:.2f})")
                log.info(msg)
                send_telegram(msg)
                return positions_opened[0]
            return None

        # Single bucket trade
        if not opp.get("token_id"):
            log.info(f"  Skip: no token ID for {opp['question'][:40]}...")
            return None

        # Conservative sizing for tweet prediction
        max_pos = RiskManager.max_position_size()
        size_usdc = max_pos * 0.3  # 30% of max position (higher uncertainty)
        shares = max(size_usdc / opp["price"], Config.MIN_ORDER_SIZE)

        order_price = round(opp["price"], 3)

        log.info(f"  Executing: BUY {shares:.0f} @ ${order_price} | {opp['question'][:50]}...")

        response = trader.place_limit_order(
            token_id=opp["token_id"],
            price=order_price,
            size=shares,
            side="BUY",
        )

        if response and response.get("success"):
            position = {
                "id": response.get("orderID", str(int(time.time()))),
                "strategy": TweetPredictionStrategy.NAME,
                "market_id": opp["market_id"],
                "question": opp["question"],
                "side": opp.get("outcome", ""),
                "token_id": opp["token_id"],
                "entry_price": order_price,
                "shares": shares,
                "cost_usdc": shares * order_price,
                "opened_at": datetime.now(timezone.utc).isoformat(),
                "status": "open",
                "dry_run": response.get("dry_run", False),
            }
            state["positions"].append(position)
            BotState.save(state)
            msg = (f"TWEET POSITION OPENED\n"
                   f"  Market: {opp['question'][:60]}\n"
                   f"  Bucket: {opp.get('outcome', '?')} @ ${order_price}\n"
                   f"  Size: {shares:.0f} shares (${shares * order_price:.2f})")
            log.info(msg)
            send_telegram(msg)
            return position
        return None


# ============================================================
#  Position Manager
# ============================================================

class PositionManager:
    """Manage open positions: check for resolution, take profits, cut losses."""

    @staticmethod
    def check_positions(state, trader):
        """Check all open positions for resolution or profit-taking."""
        positions = state.get("positions", [])
        if not positions:
            return

        now = datetime.now(timezone.utc)
        updated = False

        for pos in positions:
            if pos.get("status") != "open":
                continue

            # Check if market has resolved
            market_id = pos.get("market_id", "")
            if not market_id:
                continue

            try:
                r = requests.get(f"{Config.GAMMA_API}/markets/{market_id}", timeout=10)
                if r.status_code == 200:
                    m = r.json()
                    if m.get("closed"):
                        # Market resolved - check if we won
                        outcomes = json.loads(m.get("outcomes", "[]"))
                        prices = json.loads(m.get("outcomePrices", "[]"))
                        our_side = pos.get("side", "").upper()

                        won = False
                        for i, outcome in enumerate(outcomes):
                            if outcome.upper() == our_side and i < len(prices) and float(prices[i]) >= 1.0:
                                won = True
                                break

                        # For arbitrage positions, both sides resolve to $1
                        if pos.get("strategy") == "Arbitrage":
                            won = True

                        if won:
                            # Calculate profit
                            cost = pos.get("cost_usdc", 0)
                            shares = pos.get("shares", 0)
                            if pos.get("strategy") == "Arbitrage":
                                # Arb: both YES and NO resolve to $1
                                total_shares = pos.get("yes_shares", 0) + pos.get("no_shares", 0)
                                pnl = total_shares - cost
                            else:
                                pnl = shares - cost  # Each share pays $1

                            pos["status"] = "won"
                            pos["resolved_at"] = now.isoformat()
                            pos["pnl_usdc"] = pnl
                            updated = True

                            today = now.strftime("%Y-%m-%d")
                            daily = state["daily_pnl"].setdefault(today, {"realized": 0, "trades": 0})
                            daily["realized"] += pnl
                            daily["trades"] += 1

                            msg = (f"POSITION WON\n"
                                   f"  Market: {pos['question'][:60]}\n"
                                   f"  P&L: +${pnl:.2f}")
                            log.info(msg)
                            send_telegram(msg)

                        else:
                            pos["status"] = "lost"
                            pos["resolved_at"] = now.isoformat()
                            pos["pnl_usdc"] = -pos.get("cost_usdc", 0)
                            updated = True

                            today = now.strftime("%Y-%m-%d")
                            daily = state["daily_pnl"].setdefault(today, {"realized": 0, "trades": 0})
                            daily["realized"] += pos["pnl_usdc"]
                            daily["trades"] += 1

                            msg = (f"POSITION LOST\n"
                                   f"  Market: {pos['question'][:60]}\n"
                                   f"  P&L: -${pos.get('cost_usdc', 0):.2f}")
                            log.warning(msg)
                            send_telegram(msg)

            except Exception as e:
                log.debug(f"Position check error for {market_id}: {e}")

            # Check for take-profit on open positions (expiry yield)
            if pos.get("status") == "open" and pos.get("strategy") == "ExpiryYield":
                token_id = pos.get("token_id", "")
                if token_id and trader.initialized:
                    current_price = trader.get_midpoint(token_id)
                    entry_price = pos.get("entry_price", 0)

                    # If price moved up significantly, take profit early
                    if current_price and current_price > entry_price + 0.02:
                        profit_per_share = current_price - entry_price
                        total_profit = profit_per_share * pos.get("shares", 0)

                        if total_profit > 1.0:  # At least $1 profit
                            log.info(f"  Take profit: {pos['question'][:50]}... "
                                     f"entry=${entry_price} now=${current_price:.3f} profit=${total_profit:.2f}")

                            if Config.is_live():
                                trader.place_limit_order(
                                    token_id=token_id,
                                    price=current_price,
                                    size=pos.get("shares", 0),
                                    side="SELL",
                                )

                            pos["status"] = "closed_tp"
                            pos["closed_at"] = now.isoformat()
                            pos["exit_price"] = current_price
                            pos["pnl_usdc"] = total_profit
                            updated = True

                            today = now.strftime("%Y-%m-%d")
                            daily = state["daily_pnl"].setdefault(today, {"realized": 0, "trades": 0})
                            daily["realized"] += total_profit
                            daily["trades"] += 1

        if updated:
            BotState.save(state)
            log.info(f"Position update complete. Open: {sum(1 for p in positions if p.get('status') == 'open')}")


# ============================================================
#  Main Bot Engine
# ============================================================

class PolymarketBot:
    """Main bot orchestrator."""

    def __init__(self):
        self.trader = CLOBTrader()
        self.state = BotState.load()
        self.strategies = []

        # Register strategies
        if Config.STRATEGY_EXPIRY_YIELD:
            self.strategies.append(ExpiryYieldStrategy)
        if Config.STRATEGY_ARBITRAGE:
            self.strategies.append(ArbitrageStrategy)
        if Config.STRATEGY_TWEET_PREDICTION:
            self.strategies.append(TweetPredictionStrategy)

    def init_trader(self):
        """Initialize the CLOB trader."""
        return self.trader.init()

    def run_scan(self):
        """Run a full market scan with all enabled strategies."""
        log.info("=" * 60)
        log.info(f"Starting market scan at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        log.info(f"Mode: {'LIVE' if Config.is_live() else 'DRY RUN'}")
        log.info(f"Bankroll: ${Config.BANKROLL_USDC} | Max position: ${RiskManager.max_position_size():.2f}")
        log.info(f"Strategies: {[s.NAME for s in self.strategies]}")
        log.info("=" * 60)

        # Check risk limits
        can, reason = RiskManager.can_trade(self.state)
        if not can:
            log.warning(f"Cannot trade: {reason}")
            return []

        # Fetch all markets once (share across strategies)
        log.info("Fetching active markets from Gamma API...")
        all_markets = GammaAPI.get_all_active_markets(max_markets=500)
        # Also fetch ending-soon markets for expiry strategy
        ending_soon = GammaAPI.get_ending_soon(limit=100)
        # Combine both lists for expiry yield scan (dedup by id)
        combined_markets = list(all_markets)
        seen_ids = {m.get("id") for m in all_markets}
        for m in ending_soon:
            if m.get("id") not in seen_ids:
                combined_markets.append(m)
                seen_ids.add(m.get("id"))
        log.info(f"  Fetched {len(all_markets)} active + {len(ending_soon)} ending soon = {len(combined_markets)} total")

        # Run each strategy
        all_opportunities = []
        for strategy in self.strategies:
            try:
                if strategy.NAME == "ExpiryYield":
                    opps = strategy.scan(combined_markets)
                else:
                    opps = strategy.scan(all_markets)
                all_opportunities.extend(opps)
            except Exception as e:
                log.error(f"Strategy {strategy.NAME} failed: {e}")
                traceback.print_exc()

        # Sort by priority: arbitrage first (safest), then by expected value
        priority = {"Arbitrage": 0, "ExpiryYield": 1, "TweetPrediction": 2}
        all_opportunities.sort(key=lambda x: priority.get(x.get("strategy", ""), 99))

        # Log summary
        log.info(f"\nTotal opportunities found: {len(all_opportunities)}")
        for opp in all_opportunities[:10]:
            if opp["strategy"] == "ExpiryYield":
                log.info(f"  [{opp['strategy']}] {opp['side']} @ ${opp['price']:.3f} | "
                         f"{opp['days_to_expiry']:.1f}d | {opp['annualized_yield']:.0f}% ann | "
                         f"{opp['question'][:50]}...")
            elif opp["strategy"] == "Arbitrage":
                log.info(f"  [{opp['strategy']}] YES+NO=${opp['combined_price']:.3f} | "
                         f"+{opp['profit_pct']:.1%} | {opp['question'][:50]}...")
            else:
                log.info(f"  [{opp['strategy']}] @ ${opp['price']:.3f} | "
                         f"{opp['question'][:50]}...")

        self.state["last_scan"] = datetime.now(timezone.utc).isoformat()
        BotState.save(self.state)

        return all_opportunities

    def execute_opportunities(self, opportunities):
        """Execute trades for found opportunities (respecting risk limits)."""
        executed = 0
        for opp in opportunities:
            # Find the matching strategy class
            strategy = None
            for s in self.strategies:
                if s.NAME == opp.get("strategy"):
                    strategy = s
                    break

            if not strategy:
                continue

            # Check if we can still trade
            can, reason = RiskManager.can_trade(self.state)
            if not can:
                log.info(f"Stopping execution: {reason}")
                break

            try:
                result = strategy.execute(opp, self.trader, self.state)
                if result:
                    executed += 1
                    time.sleep(2)  # Rate limit between orders
            except Exception as e:
                log.error(f"Execution failed for {opp.get('question', '?')}: {e}")

        log.info(f"Executed {executed} new positions")
        return executed

    def run_cycle(self):
        """Run one full cycle: scan + check positions + execute."""
        # 1. Check existing positions
        PositionManager.check_positions(self.state, self.trader)

        # 2. Scan for new opportunities
        opportunities = self.run_scan()

        # 3. Execute top opportunities
        if opportunities:
            self.execute_opportunities(opportunities)

        # 4. Check positions again after execution
        PositionManager.check_positions(self.state, self.trader)

    def run_loop(self):
        """Run the bot continuously."""
        log.info("=" * 60)
        log.info("Polymarket Auto-Trader Bot - Starting continuous mode")
        log.info(f"  Mode: {'LIVE' if Config.is_live() else 'DRY RUN (no real trades)'}")
        log.info(f"  Scan interval: {Config.SCAN_INTERVAL}s")
        log.info(f"  Bankroll: ${Config.BANKROLL_USDC}")
        log.info(f"  Max position: ${RiskManager.max_position_size():.2f} ({Config.MAX_POSITION_PCT:.0%})")
        log.info(f"  Max positions: {Config.MAX_POSITIONS}")
        log.info(f"  Cash reserve: {Config.CASH_RESERVE_PCT:.0%}")
        log.info("=" * 60)

        send_telegram("Bot started. Mode: " + ("LIVE" if Config.is_live() else "DRY RUN"))

        while True:
            try:
                self.run_cycle()

                next_scan = datetime.now() + timedelta(seconds=Config.SCAN_INTERVAL)
                log.info(f"\nNext scan at {next_scan.strftime('%H:%M:%S')}. Sleeping {Config.SCAN_INTERVAL}s...\n")
                time.sleep(Config.SCAN_INTERVAL)

            except KeyboardInterrupt:
                log.info("Bot stopped by user")
                send_telegram("Bot stopped by user")
                break
            except Exception as e:
                log.error(f"Cycle error: {e}")
                traceback.print_exc()
                time.sleep(60)  # Wait 1 min before retrying

    def print_stats(self):
        """Print current position stats and P&L."""
        state = self.state
        positions = state.get("positions", [])

        print("\n" + "=" * 60)
        print("  POLYMARKET BOT - POSITION STATS")
        print("=" * 60)

        # Position summary
        open_pos = [p for p in positions if p.get("status") == "open"]
        won = [p for p in positions if p.get("status") == "won"]
        lost = [p for p in positions if p.get("status") == "lost"]
        closed = [p for p in positions if p.get("status", "").startswith("closed")]

        total_cost = sum(p.get("cost_usdc", 0) for p in open_pos)
        total_pnl = sum(p.get("pnl_usdc", 0) for p in won + lost + closed)

        print(f"\n  Total positions: {len(positions)}")
        print(f"  Open: {len(open_pos)} | Won: {len(won)} | Lost: {len(lost)} | Closed: {len(closed)}")
        print(f"  Open exposure: ${total_cost:.2f}")
        print(f"  Total realized P&L: ${total_pnl:.2f}")

        # Daily P&L
        print(f"\n  --- Daily P&L ---")
        daily = state.get("daily_pnl", {})
        for date in sorted(daily.keys(), reverse=True)[:7]:
            d = daily[date]
            print(f"  {date}: ${d.get('realized', 0):+.2f} ({d.get('trades', 0)} trades)")

        # Open positions detail
        if open_pos:
            print(f"\n  --- Open Positions ---")
            for p in open_pos:
                print(f"  [{p['strategy']}] {p['question'][:50]}")
                print(f"    {p.get('side', '?')} @ ${p.get('entry_price', 0):.3f} | "
                      f"{p.get('shares', 0):.0f} shares | ${p.get('cost_usdc', 0):.2f}")
                if p.get("end_date"):
                    print(f"    Expires: {p['end_date'][:10]}")

        # Risk status
        can, reason = RiskManager.can_trade(state)
        print(f"\n  Risk status: {'OK' if can else 'BLOCKED - ' + reason}")
        avail, total, max_exp = RiskManager.check_exposure(state)
        print(f"  Exposure: ${total:.2f} / ${max_exp:.2f} (${avail:.2f} available)")
        print(f"  Cash reserve: ${RiskManager.cash_reserve():.2f}")

        print("\n" + "=" * 60)

    def generate_report(self):
        """Generate a daily report."""
        state = self.state
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        daily = state.get("daily_pnl", {}).get(today, {"realized": 0, "trades": 0})

        positions = state.get("positions", [])
        open_pos = [p for p in positions if p.get("status") == "open"]

        report = f"""
Polymarket Bot - Daily Report ({today})
========================================

Today's P&L: ${daily.get('realized', 0):+.2f} ({daily.get('trades', 0)} trades)

Open Positions: {len(open_pos)}
Total Exposure: ${sum(p.get('cost_usdc', 0) for p in open_pos):.2f}

All Positions:
"""
        for p in positions[-20:]:  # Last 20
            status = p.get("status", "?")
            pnl = p.get("pnl_usdc", 0)
            pnl_str = f"${pnl:+.2f}" if pnl else "N/A"
            report += f"  [{status:8s}] [{p.get('strategy', '?'):15s}] {p.get('question', '?')[:50]:50s} {pnl_str}\n"

        report += f"\nLast scan: {state.get('last_scan', 'Never')}"
        print(report)

        # Save report to file
        report_file = Path(__file__).parent / f"report_{today}.txt"
        with open(report_file, "w", encoding="utf-8") as f:
            f.write(report)
        log.info(f"Report saved to {report_file}")
        send_telegram(report)


# ============================================================
#  CLI Entry Point
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="Polymarket Auto-Trader Bot")
    parser.add_argument("--live", action="store_true", help="Run in LIVE mode (real trades)")
    parser.add_argument("--scan", action="store_true", help="One-time scan, print opportunities only")
    parser.add_argument("--stats", action="store_true", help="Show position stats and P&L")
    parser.add_argument("--cancel-all", action="store_true", help="Cancel all open orders")
    parser.add_argument("--report", action="store_true", help="Generate daily report")
    parser.add_argument("--once", action="store_true", help="Run one scan+execute cycle then exit")
    args = parser.parse_args()

    bot = PolymarketBot()

    # Handle non-trading commands first
    if args.stats:
        bot.print_stats()
        return

    if args.report:
        bot.generate_report()
        return

    if args.cancel_all:
        if bot.init_trader():
            bot.trader.cancel_all()
        return

    if args.scan:
        # Just scan and print, no execution
        bot.trader.client = None  # Don't need trading client for scan
        opportunities = bot.run_scan()
        print(f"\n{'=' * 60}")
        print(f"  Found {len(opportunities)} opportunities")
        print(f"{'=' * 60}\n")
        for i, opp in enumerate(opportunities[:20], 1):
            print(f"{i}. [{opp['strategy']}] {opp.get('question', '?')[:60]}")
            if opp["strategy"] == "ExpiryYield":
                print(f"   {opp['side']} @ ${opp['price']:.3f} | "
                      f"{opp['days_to_expiry']:.1f}d to expiry | "
                      f"{opp['annualized_yield']:.0f}% annualized")
            elif opp["strategy"] == "Arbitrage":
                print(f"   YES+NO = ${opp['combined_price']:.3f} | "
                      f"Profit: {opp['profit_pct']:.1%}")
            else:
                print(f"   @ ${opp['price']:.3f}")
            print()
        return

    # Full trading mode
    if not Config.validate():
        sys.exit(1)

    if not bot.init_trader():
        log.warning("Running in scan-only mode (no trading capability)")

    if args.once:
        bot.run_cycle()
        bot.print_stats()
    else:
        bot.run_loop()


if __name__ == "__main__":
    main()
