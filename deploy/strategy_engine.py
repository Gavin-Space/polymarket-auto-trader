#!/usr/bin/env python3
"""
Enhanced Strategy Engine for Polymarket Auto-Trader
====================================================
Multi-factor opportunity discovery with positive-EV filtering.

Key improvements over the base scanner:
  1. OrderBookAnalyzer  - Real spread, liquidity depth, slippage estimation
  2. PriceHistoryAnalyzer - Momentum, volatility, mean-reversion signals
  3. SmartMoneyTracker  - Whale positions and large trade detection
  4. ConfidenceScorer   - 0-100 multi-factor confidence model
  5. EVCalculator       - Expected value filter (only trade positive-EV)
  6. Six strategies     - ExpiryYield+, Arb+, TweetArb+, Momentum, MeanReversion, SmartMoney

All public API calls use Gamma + CLOB + Data APIs (no auth needed for reads).
"""

import json
import time
import math
import logging
import re
from datetime import datetime, timedelta, timezone
from collections import defaultdict

import requests

log = logging.getLogger("strategy_engine")

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
DATA_API = "https://data-api.polymarket.com"

# Cache for API responses to avoid rate limits
_price_history_cache = {}
_orderbook_cache = {}
_cache_timestamp = {}


# Circuit breaker: when individual order-book / history fetches keep failing
# (flaky proxy/VPN), flip _degraded so the rest of the scan stops hammering the
# network and falls back to market-price-only evaluation.
_fail_streak = 0
_degraded = False

# Hard per-scan time budget: once exceeded, _cached_get stops fetching so the
# scan always completes in bounded time (falls back to market-price data).
_scan_deadline = None


def reset_network_state():
    """Clear the per-scan network circuit breaker (called at scan start)."""
    global _fail_streak, _degraded
    _fail_streak = 0
    _degraded = False


def set_scan_deadline(seconds: float):
    """Set the per-scan time budget (0 / negative clears it)."""
    global _scan_deadline
    _scan_deadline = time.time() + seconds if seconds and seconds > 0 else None


def _cached_get(url, params, cache_key, ttl=60, timeout=4):
    """Fetch with simple in-memory cache, a fail-fast circuit breaker, and a
    per-scan deadline so a scan always completes in bounded time."""
    global _fail_streak, _degraded
    now = time.time()
    if _scan_deadline and now > _scan_deadline:
        return None  # scan budget exceeded → stop fetching (use market data)
    if cache_key in _cache_timestamp and now - _cache_timestamp[cache_key] < ttl:
        return _orderbook_cache.get(cache_key)
    try:
        r = requests.get(url, params=params, timeout=timeout)
        r.raise_for_status()
        data = r.json()
        _orderbook_cache[cache_key] = data
        _cache_timestamp[cache_key] = now
        _fail_streak = 0
        return data
    except Exception as e:
        _fail_streak += 1
        if _fail_streak >= 3:
            _degraded = True
        log.debug(f"API fetch failed for {cache_key}: {e}")
        return None


# ============================================================
#  Order Book Analyzer
# ============================================================

class OrderBookAnalyzer:
    """Fetch and analyze order book depth for a token."""

    @staticmethod
    def get_book(token_id):
        if not token_id:
            return None
        return _cached_get(
            f"{CLOB_API}/book",
            {"token_id": token_id},
            f"book_{token_id}",
            ttl=30,
        )

    @staticmethod
    def get_midpoint(token_id):
        if not token_id:
            return None
        data = _cached_get(
            f"{CLOB_API}/midpoint",
            {"token_id": token_id},
            f"mid_{token_id}",
            ttl=30,
        )
        if data and "mid" in data:
            return float(data["mid"])
        return None

    @staticmethod
    def get_spread(token_id):
        if not token_id:
            return None
        data = _cached_get(
            f"{CLOB_API}/spread",
            {"token_id": token_id},
            f"spread_{token_id}",
            ttl=30,
        )
        if data and "spread" in data:
            return float(data["spread"])
        return None

    @staticmethod
    def analyze(token_id, order_size_usdc=20):
        """
        Full order book analysis.
        Returns dict with: best_bid, best_ask, mid, spread, bid_depth, ask_depth,
                           est_fill_price, slippage, liquidity_score
        """
        book = OrderBookAnalyzer.get_book(token_id)
        if not book:
            return None

        bids = book.get("bids", [])
        asks = book.get("asks", [])

        # Parse and sort
        bid_list = []
        for b in bids:
            try:
                bid_list.append((float(b["price"]), float(b["size"])))
            except:
                pass
        ask_list = []
        for a in asks:
            try:
                ask_list.append((float(a["price"]), float(a["size"])))
            except:
                pass

        bid_list.sort(key=lambda x: -x[0])  # Highest bid first
        ask_list.sort(key=lambda x: x[0])   # Lowest ask first

        best_bid = bid_list[0][0] if bid_list else 0
        best_ask = ask_list[0][0] if ask_list else 1
        mid = (best_bid + best_ask) / 2 if best_bid > 0 and best_ask < 1 else None
        spread = best_ask - best_bid

        # Calculate depth at different levels
        bid_depth_05 = sum(s for p, s in bid_list if p >= best_bid - 0.05)
        ask_depth_05 = sum(s for p, s in ask_list if p <= best_ask + 0.05)
        bid_depth_10 = sum(s for p, s in bid_list if p >= best_bid - 0.10)
        ask_depth_10 = sum(s for p, s in ask_list if p <= best_ask + 0.10)

        # Estimate fill price for our order size
        est_fill_price = None
        slippage = 0
        if order_size_usdc > 0 and ask_list:
            remaining = order_size_usdc
            total_shares = 0
            total_cost = 0
            for price, size in ask_list:
                cost_at_level = price * min(size, remaining / price)
                shares_at_level = min(size, remaining / price)
                total_cost += cost_at_level
                total_shares += shares_at_level
                remaining -= cost_at_level
                if remaining <= 0:
                    break
            if total_shares > 0:
                est_fill_price = total_cost / total_shares
                slippage = est_fill_price - best_ask

        # Liquidity score 0-100 (higher = more liquid)
        total_depth = bid_depth_05 + ask_depth_05
        liquidity_score = min(100, total_depth / 50)  # 5000 shares = 100

        return {
            "best_bid": best_bid,
            "best_ask": best_ask,
            "mid": mid,
            "spread": spread,
            "bid_depth_5pct": bid_depth_05,
            "ask_depth_5pct": ask_depth_05,
            "bid_depth_10pct": bid_depth_10,
            "ask_depth_10pct": ask_depth_10,
            "est_fill_price": est_fill_price,
            "slippage": slippage,
            "liquidity_score": liquidity_score,
            "last_trade_price": float(book.get("last_trade_price", 0) or 0),
        }


# ============================================================
#  Price History Analyzer
# ============================================================

class PriceHistoryAnalyzer:
    """Analyze price history for momentum, volatility, and mean reversion."""

    @staticmethod
    def get_history(token_id, interval="1d"):
        if not token_id:
            return None
        cache_key = f"hist_{token_id}_{interval}"
        data = _cached_get(
            f"{CLOB_API}/prices-history",
            {"market": token_id, "interval": interval},
            cache_key,
            ttl=120,
        )
        if data and "history" in data:
            return data["history"]
        return None

    @staticmethod
    def analyze(token_id, interval="1d"):
        """
        Returns momentum (-1 to +1), volatility (0-1), trend_strength (0-100),
        mean_reversion_signal (-1 to +1), price_change_pct
        """
        history = PriceHistoryAnalyzer.get_history(token_id, interval)
        if not history or len(history) < 3:
            return None

        prices = [float(h.get("p", 0)) for h in history if h.get("p")]
        if len(prices) < 3:
            return None

        current = prices[-1]
        start = prices[0]

        # Price change
        price_change_pct = (current - start) / start if start > 0 else 0

        # Momentum: compare recent prices to older prices
        n = len(prices)
        recent_avg = sum(prices[max(0, n-5):]) / max(1, min(5, n))
        older_avg = sum(prices[:max(1, n-5)]) / max(1, n-5)
        momentum = (recent_avg - older_avg) / older_avg if older_avg > 0 else 0
        momentum = max(-1, min(1, momentum * 5))  # Clamp to -1..1

        # Volatility: standard deviation of prices
        if len(prices) > 2:
            avg = sum(prices) / len(prices)
            variance = sum((p - avg) ** 2 for p in prices) / len(prices)
            volatility = math.sqrt(variance)
        else:
            volatility = 0

        # Trend strength: R² of linear regression
        if len(prices) > 5:
            x = list(range(len(prices)))
            x_avg = sum(x) / len(x)
            y_avg = sum(prices) / len(prices)
            numerator = sum((xi - x_avg) * (yi - y_avg) for xi, yi in zip(x, prices))
            denominator_x = sum((xi - x_avg) ** 2 for xi in x)
            denominator_y = sum((yi - y_avg) ** 2 for yi in prices)
            if denominator_x > 0 and denominator_y > 0:
                r_squared = (numerator ** 2) / (denominator_x * denominator_y)
            else:
                r_squared = 0
            trend_direction = 1 if numerator > 0 else -1
            trend_strength = r_squared * 100 * trend_direction
        else:
            trend_strength = 0

        # Mean reversion: if current price is far from average, expect reversion
        avg_price = sum(prices) / len(prices)
        if avg_price > 0:
            deviation = (current - avg_price) / avg_price
            # Strong deviation signals mean reversion (opposite direction)
            mean_reversion_signal = -deviation * 5
            mean_reversion_signal = max(-1, min(1, mean_reversion_signal))
        else:
            mean_reversion_signal = 0

        # Recent high/low
        recent_prices = prices[-10:] if len(prices) >= 10 else prices
        recent_high = max(recent_prices)
        recent_low = min(recent_prices)

        return {
            "current_price": current,
            "price_change_pct": price_change_pct,
            "momentum": momentum,
            "volatility": volatility,
            "trend_strength": trend_strength,
            "mean_reversion_signal": mean_reversion_signal,
            "avg_price": avg_price,
            "recent_high": recent_high,
            "recent_low": recent_low,
            "history_length": len(prices),
        }


# ============================================================
#  Smart Money Tracker
# ============================================================

class SmartMoneyTracker:
    """Detect whale activity and smart money flows."""

    @staticmethod
    def get_top_holders(market_id, limit=10):
        """Get top holders for a market."""
        try:
            r = requests.get(
                f"{DATA_API}/holders",
                params={"market": market_id, "limit": limit},
                timeout=10,
            )
            if r.status_code == 200:
                return r.json()
        except:
            pass
        return []

    @staticmethod
    def get_recent_trades(token_id, limit=20):
        """Get recent trades for a token."""
        try:
            r = requests.get(
                f"{DATA_API}/trades",
                params={"asset_id": token_id, "limit": limit},
                timeout=10,
            )
            if r.status_code == 200:
                return r.json()
        except:
            pass
        return []

    @staticmethod
    def analyze_market(market_id, token_id):
        """
        Analyze smart money activity for a market.
        Returns: whale_buying, whale_selling, net_flow, large_trades, confidence_boost
        """
        trades = SmartMoneyTracker.get_recent_trades(token_id, limit=30)
        if not trades:
            return None

        # Filter for large trades (whale activity)
        large_trades = []
        buy_volume = 0
        sell_volume = 0
        total_volume = 0

        for t in trades:
            try:
                size = float(t.get("size", 0) or 0)
                price = float(t.get("price", 0) or 0)
                side = t.get("side", "").upper()
                value = size * price

                total_volume += value
                if value > 500:  # >$500 is whale-sized
                    large_trades.append({
                        "side": side,
                        "size": size,
                        "price": price,
                        "value": value,
                        "timestamp": t.get("timestamp", ""),
                    })

                if side in ("BUY", "B"):
                    buy_volume += value
                elif side in ("SELL", "S"):
                    sell_volume += value
            except:
                pass

        net_flow = buy_volume - sell_volume
        total = buy_volume + sell_volume
        buy_ratio = buy_volume / total if total > 0 else 0.5

        # Confidence boost: if whales are buying the side we want
        whale_buying = buy_ratio > 0.65 and len(large_trades) > 0
        whale_selling = buy_ratio < 0.35 and len(large_trades) > 0

        # Confidence boost from whale alignment
        confidence_boost = 0
        if whale_buying:
            confidence_boost = min(15, (buy_ratio - 0.5) * 30)
        elif whale_selling:
            confidence_boost = min(15, (0.5 - buy_ratio) * 30)

        return {
            "buy_volume": round(buy_volume, 2),
            "sell_volume": round(sell_volume, 2),
            "net_flow": round(net_flow, 2),
            "total_volume": round(total_volume, 2),
            "buy_ratio": round(buy_ratio, 3),
            "large_trades": len(large_trades),
            "whale_buying": whale_buying,
            "whale_selling": whale_selling,
            "confidence_boost": round(confidence_boost, 1),
            "recent_large_trades": large_trades[:3],
        }


# ============================================================
#  Confidence Scorer
# ============================================================

class ConfidenceScorer:
    """
    Multi-factor confidence scoring (0-100).
    Combines: probability gap, time decay, liquidity, momentum, whale signals, volatility.
    """

    @staticmethod
    def score(opp_data):
        """
        opp_data should contain:
        - market_price: current price of the side we want to buy
        - estimated_prob: our estimate of the true probability
        - days_to_expiry: time until resolution
        - liquidity_score: 0-100 from order book analysis
        - momentum: -1 to +1 from price history
        - whale_signal: confidence_boost from smart money
        - volatility: price volatility
        - mean_reversion: -1 to +1
        """
        score = 50  # Start at neutral

        # 1. Probability gap (biggest factor: 30 points max)
        market_price = opp_data.get("market_price", 0)
        est_prob = opp_data.get("estimated_prob", market_price)
        prob_edge = est_prob - market_price
        if prob_edge > 0:
            score += min(20, prob_edge * 100)
        else:
            score += max(-15, prob_edge * 50)

        # For high-probability expiry yield, reward confidence
        if est_prob > 0.95:
            score += min(10, (est_prob - 0.95) * 200)

        # 2. Time to expiry (15 points max)
        days = opp_data.get("days_to_expiry", 7)
        if 0 < days <= 1:
            score += 12  # Very close to expiry = high certainty
        elif 1 < days <= 3:
            score += 8
        elif 3 < days <= 7:
            score += 4
        elif days > 7:
            score -= min(5, (days - 7))

        # 3. Liquidity (15 points max)
        liq = opp_data.get("liquidity_score", 0)
        score += min(15, liq * 0.15)
        if liq < 10:
            score -= 10  # Penalize illiquid markets

        # 4. Momentum alignment (10 points max) — a rising own-token price is
        # favorable for BOTH YES and NO positions (a NO position profits when
        # the NO token rises toward $1). Fixes the old double-negation that
        # penalized favorable NO momentum (review M4).
        momentum = opp_data.get("momentum", 0)
        score += min(8, momentum * 8)

        # 5. Whale signal (10 points max)
        whale_boost = opp_data.get("whale_signal", 0)
        score += min(10, whale_boost)

        # 6. Volatility penalty (5 points max)
        vol = opp_data.get("volatility", 0)
        if vol > 0.15:
            score -= min(8, (vol - 0.15) * 40)

        # 7. Mean reversion bonus — an UNDERVALUED token (below its average,
        # positive reversion signal) is favorable for BOTH YES and NO positions
        # (each profits when its own token recovers toward $1). The old branch
        # rewarded a falling NO token, which is the wrong direction (review M5).
        mr = opp_data.get("mean_reversion", 0)
        if mr > 0:
            score += min(5, mr * 5)

        # 8. Spread penalty
        spread = opp_data.get("spread", 0)
        if spread > 0.03:
            score -= min(5, (spread - 0.03) * 50)

        return max(0, min(100, round(score, 1)))


# ============================================================
#  EV Calculator
# ============================================================

class EVCalculator:
    """
    Calculate expected value for a trade.
    EV = (win_prob * payoff) - (lose_prob * cost) - slippage_cost
    Only trade when EV > 0 (ideally EV > 1.5% of trade size).
    """

    # Minimum EV % to accept a trade; overridden at runtime by auto_trader
    # from TradingConfig.min_ev_pct (trading_config.json).
    MIN_EV_PCT = 1.5

    @staticmethod
    def calculate(buy_price, estimated_prob, order_size_usdc, slippage=0, days_to_expiry=1, min_ev_pct=None):
        """
        Returns: ev_usdc, ev_pct, roi_if_win, risk_if_lose, kelly_fraction
        min_ev_pct overrides the default MIN_EV_PCT (e.g. expiry yield trades a
        near-certain outcome whose edge is the annualized yield, not a large
        per-trade EV).
        """
        # Adjust for slippage
        actual_price = buy_price + slippage
        if actual_price >= 1:
            return {"ev_usdc": -order_size_usdc, "ev_pct": -100, "kelly": 0, "skip": True}

        shares = order_size_usdc / actual_price
        win_prob = estimated_prob
        lose_prob = 1 - win_prob

        # Payoff if win: shares * $1 - cost
        payoff = shares - order_size_usdc
        # Loss if lose: -cost
        loss = -order_size_usdc

        ev_usdc = win_prob * payoff + lose_prob * loss
        ev_pct = (ev_usdc / order_size_usdc) * 100 if order_size_usdc > 0 else 0

        roi_if_win = (payoff / order_size_usdc) * 100 if order_size_usdc > 0 else 0
        risk_if_lose = 100  # Lose everything

        # Kelly criterion: f = (bp - q) / b
        # b = payoff/cost ratio = (1-price)/price
        b = (1 - actual_price) / actual_price if actual_price > 0 else 0
        p = win_prob
        q = lose_prob
        kelly = (b * p - q) / b if b > 0 else 0
        kelly = max(0, min(0.25, kelly))  # Cap at 25% (quarter Kelly)

        return {
            "ev_usdc": round(ev_usdc, 2),
            "ev_pct": round(ev_pct, 2),
            "roi_if_win": round(roi_if_win, 2),
            "risk_if_lose": risk_if_lose,
            "kelly_fraction": round(kelly, 4),
            "shares": round(shares, 1),
            "actual_price": round(actual_price, 4),
            "skip": ev_usdc <= 0 or ev_pct < (EVCalculator.MIN_EV_PCT if min_ev_pct is None else min_ev_pct),  # Skip if EV below threshold
        }


# ============================================================
#  Market-quality filters
# ============================================================

# Esports / match-outcome markets are notoriously hard to predict and were the
# source of the dry-run losses (Dota2/CS mean-reversion trades). Exclude them
# from the speculative strategies to raise win rate.
_SPECULATIVE_EXCLUDE = (
    "dota", "counter-strike", "league of legends", "lol:", "valorant",
    "overwatch", "rocket league", "apex legends", "fortnite", "esports",
    "world cup group", "bo1", "bo3", "bo5", "team spirit", "team liquid",
    "natus vincere", "fnatic", " g2 ", "team falcons",
)

# Crypto price-boundary markets ("Bitcoin above $64,000") can flip if the price
# sits near the line; they need an even higher implied probability to trade.
# Word-boundary regexes avoid false positives from short substrings ("sol" in
# "solar", "eth" in "method", "doge" in "dogecoin/other") — review M10.
_CRYPTO_BOUNDARY = (
    r"\bbitcoin\b", r"\bethereum\b", r"\bbtc\b", r"\beth\b",
    r"\bsolana\b", r"\bsol\b", r"\bdogecoin\b", r"\bdoge\b",
    r"\bbnb\b", r"\bxrp\b",
)


def is_speculative_market(question: str) -> bool:
    """True if the market is an esports / match-outcome bet (hard to predict)."""
    q = (question or "").lower()
    return any(k in q for k in _SPECULATIVE_EXCLUDE)


def is_crypto_boundary(question: str) -> bool:
    """True for crypto price-above/below-boundary markets (flip risk)."""
    q = (question or "").lower()
    if not any(re.search(p, q) for p in _CRYPTO_BOUNDARY):
        return False
    return any(w in q for w in ("above", "below", "over", "under", "exceed", " > ", " < "))


# ============================================================
#  Enhanced Scanner
# ============================================================

class EnhancedScanner:
    """
    Enhanced market scanner that replaces the basic scan in auto_trader.py.
    Uses multi-factor analysis to find only positive-EV opportunities.
    """

    # Minimum confidence score to present an opportunity
    MIN_CONFIDENCE = 75  # Raised from 65: only high-confidence trades
    # Minimum EV percentage to trade
    MIN_EV_PCT = 1.5  # Raised from 1.0: require at least 1.5% EV
    # Minimum liquidity score
    MIN_LIQUIDITY = 10  # Raised from 5
    # Maximum spread (don't trade if spread too wide)
    MAX_SPREAD = 0.04  # Tightened from 0.05
    # Minimum price - don't buy very cheap shares (too risky, likely to go to zero)
    MIN_PRICE = 0.15
    # Maximum trades per day (prevent overtrading)
    MAX_DAILY_TRADES = 8

    def __init__(self):
        self.price_history_interval = "1d"
        # Runtime-tunable thresholds — auto_trader syncs these from TradingConfig
        # (trading_config.json) before each scan.
        self.min_confidence = self.MIN_CONFIDENCE
        self.filter_speculative = True
        self.filter_crypto_boundary = True
        # Research-driven quality gates: illiquid books and relative spreads
        # that eat the edge (cheap markets quote 13-18% spreads — arXiv 2604.24366).
        self.min_liquidity = self.MIN_LIQUIDITY
        self.max_relative_spread = 0.05  # skip non-arb if spread > 5% of price
        self.expiry_annualized_floor = 20  # require ≥20% annualized for expiry yield
        # Flaky network → skip per-market order-book/history analysis and
        # evaluate on market-price data alone (set by auto_trader).
        self.skip_orderbook = False
        # Which strategies to run (synced from TradingConfig.strategy_* — these
        # were dead before; the scanner ran all six unconditionally, review S7).
        self.strategy_toggles = {
            "expiry": True,
            "arb": True,
            "tweet": True,
            "directional": True,
        }

    @staticmethod
    def parse_market(raw):
        """Parse a raw Gamma API market response."""
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
                "volume_1h": float(raw.get("volume1hr", 0) or 0),
                "liquidity": float(raw.get("liquidity", 0) or 0),
                "end_date": raw.get("endDate", ""),
                "active": raw.get("active", False),
                "closed": raw.get("closed", False),
                "image": raw.get("image", "") or raw.get("icon", ""),
                "tags": raw.get("tags", []),
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

    def scan(self, markets, now=None):
        """
        Scan all markets and return only high-confidence, positive-EV opportunities.
        """
        if now is None:
            now = datetime.now(timezone.utc)

        opportunities = []
        toggles = self.strategy_toggles

        # Strategy A+: Enhanced Expiry Yield
        if toggles.get("expiry", True):
            opportunities.extend(self._scan_expiry_yield(markets, now))

        # Strategy B+: Enhanced Arbitrage (with order book check)
        if toggles.get("arb", True):
            opportunities.extend(self._scan_arbitrage(markets))

        # Strategy C+: Tweet Bucket Arbitrage
        if toggles.get("tweet", True):
            opportunities.extend(self._scan_tweet_arb(markets))

        # Strategy D/E/F: Momentum / Mean Reversion / Smart Money (directional)
        if toggles.get("directional", True):
            opportunities.extend(self._scan_momentum(markets, now))
            opportunities.extend(self._scan_mean_reversion(markets, now))
            opportunities.extend(self._scan_smart_money(markets, now))

        # Sort by confidence score (highest first), then by EV
        opportunities.sort(
            key=lambda x: (-x.get("confidence", 0), -x.get("ev", {}).get("ev_pct", 0))
        )

        # Filter: positive EV, sufficient confidence, adequate liquidity, and
        # spreads that don't eat the edge (relative to price). Cheap markets
        # quote 13-18% spreads (research finding 4); arb strategies are exempt
        # because their profit is guaranteed by construction.
        filtered = []
        for o in opportunities:
            if o.get("confidence", 0) < self.min_confidence:
                continue
            if o.get("ev", {}).get("skip", True):
                continue
            analysis = o.get("analysis") or {}
            if (analysis.get("liquidity_score", 99) or 99) < self.min_liquidity:
                continue
            strat = o.get("strategy", "")
            price = o.get("price", 0) or 0
            spread = analysis.get("spread", 0) or 0
            if strat not in ("Arbitrage+", "Arbitrage", "TweetArb+", "TweetArb") and price > 0:
                if spread / price > self.max_relative_spread:
                    continue
            filtered.append(o)

        log.info(f"  Enhanced scan: {len(opportunities)} raw -> {len(filtered)} filtered opportunities")
        return filtered

    def _scan_expiry_yield(self, markets, now):
        """Strategy A+: Enhanced expiry yield with multi-factor analysis."""
        opps = []

        for m in markets:
            end_str = m.get("end_date", "")
            if not end_str:
                continue
            try:
                end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                days = (end_dt - now).total_seconds() / 86400
            except:
                continue

            # Only look at markets expiring within 7 days
            if days < -0.5 or days > 7:
                continue

            # Check both YES and NO sides
            for side, price_key, token_key, buying_yes in [
                ("NO", "no_price", "no_token", False),
                ("YES", "yes_price", "yes_token", True),
            ]:
                price = m.get(price_key, 0)
                token_id = m.get(token_key, "")

                # Only interested in high-probability outcomes (>= 95%, raised from 94%)
                # — higher probability → higher win rate on the main strategy.
                if price < 0.95 or price >= 1.0:
                    continue

                # Crypto price-boundary markets can flip near the line; require
                # an even higher probability to trade them.
                if self.filter_crypto_boundary and is_crypto_boundary(m.get("question", "")) and price < 0.96:
                    continue

                # Basic volume/liquidity filter (tighter: require meaningful liquidity)
                vol = m.get("volume_24h", 0)
                liq = m.get("liquidity", 0)
                if liq < 1000 and vol < 2000:
                    continue

                # Estimate true probability
                # For expiry yield, the market price IS our probability estimate
                # But we adjust based on time remaining and market type
                est_prob = price
                # Win-rate lever: the closer to resolution, the less time there
                # is for the price to flip — so require even higher certainty
                # within the last ~6 hours.
                if days < 0.25 and price < 0.96:
                    continue
                # Small adjustment: markets very close to expiry with high prob
                # are almost certain
                if days < 0.5 and price > 0.95:
                    est_prob = min(0.999, price + 0.01)
                elif days < 1 and price > 0.97:
                    est_prob = min(0.998, price + 0.005)

                # Gather analysis data
                analysis_data = {
                    "market_price": price,
                    "estimated_prob": est_prob,
                    "days_to_expiry": max(days, 0.1),
                    "liquidity_score": min(100, (vol + liq) / 100),
                    "momentum": 0,
                    "whale_signal": 0,
                    "volatility": 0,
                    "mean_reversion": 0,
                    "spread": 0,
                    "buying_yes": buying_yes,
                }

                # Fetch order book analysis (limit API calls to high-value candidates)
                if price >= 0.95 and token_id and not self.skip_orderbook and not _degraded:
                    book = OrderBookAnalyzer.analyze(token_id, order_size_usdc=20)
                    if book:
                        analysis_data["spread"] = book["spread"]
                        analysis_data["liquidity_score"] = book["liquidity_score"]
                        # Use actual ask price if available
                        if book["best_ask"] < 1:
                            analysis_data["market_price"] = book["best_ask"]

                    # Fetch price history for momentum
                    hist = None if (self.skip_orderbook or _degraded) else PriceHistoryAnalyzer.analyze(token_id, "1d")
                    if hist:
                        analysis_data["momentum"] = hist["momentum"]
                        analysis_data["volatility"] = hist["volatility"]
                        analysis_data["mean_reversion"] = hist["mean_reversion_signal"]

                # Calculate confidence score
                confidence = ConfidenceScorer.score(analysis_data)

                # Skip if confidence too low
                if confidence < self.min_confidence:
                    continue

                # Calculate EV
                actual_price = analysis_data["market_price"]
                ev = EVCalculator.calculate(
                    buy_price=actual_price,
                    estimated_prob=est_prob,
                    order_size_usdc=20,
                    slippage=0,  # actual_price is already the executable best_ask
                    days_to_expiry=days,
                    min_ev_pct=0.3,  # expiry edge = annualized yield, not per-trade EV
                )

                if ev.get("skip"):
                    continue

                # Skip if spread too wide
                if analysis_data["spread"] > self.MAX_SPREAD:
                    continue

                profit_pct = (1 - actual_price) / actual_price
                annualized = profit_pct * 365 / max(days, 0.1)
                # Yield too thin to bother (research: near-certainty fades are
                # real but modest — focus on genuine annualized returns)
                if annualized < self.expiry_annualized_floor:
                    continue

                opps.append({
                    "strategy": "ExpiryYield+",
                    "market_id": m["id"],
                    "question": m["question"],
                    "side": side,
                    "token_id": token_id,
                    "price": round(actual_price, 4),
                    "days_to_expiry": round(days, 2),
                    "profit_pct": profit_pct,
                    "annualized_yield": annualized,
                    "volume_24h": vol,
                    "end_date": end_str,
                    "image": m.get("image", ""),
                    "confidence": confidence,
                    "ev": ev,
                    "analysis": {
                        "liquidity_score": round(analysis_data["liquidity_score"], 1),
                        "momentum": round(analysis_data["momentum"], 3),
                        "volatility": round(analysis_data["volatility"], 4),
                        "spread": round(analysis_data["spread"], 4),
                        "est_prob": round(est_prob, 4),
                    },
                    "priority": 2,
                })

        return opps

    def _scan_arbitrage(self, markets):
        """Strategy B+: Arbitrage with order book verification."""
        opps = []

        for m in markets:
            yes_price = m.get("yes_price", 0)
            no_price = m.get("no_price", 0)
            if yes_price <= 0 or no_price <= 0:
                continue

            combined = yes_price + no_price
            if combined >= 0.97:
                continue

            vol = m.get("volume_24h", 0)
            liq = m.get("liquidity", 0)
            if vol < 500 and liq < 500:
                continue

            # Verify with order book (check actual executable prices)
            yes_token = m.get("yes_token", "")
            no_token = m.get("no_token", "")

            actual_yes = yes_price
            actual_no = no_price
            liquidity_score = 0
            spread_total = 0

            if yes_token and no_token and not self.skip_orderbook and not _degraded:
                book_yes = OrderBookAnalyzer.analyze(yes_token, order_size_usdc=15)
                book_no = OrderBookAnalyzer.analyze(no_token, order_size_usdc=15)

                if book_yes and book_no:
                    actual_yes = book_yes["best_ask"] if book_yes["best_ask"] < 1 else yes_price
                    actual_no = book_no["best_ask"] if book_no["best_ask"] < 1 else no_price
                    liquidity_score = (book_yes["liquidity_score"] + book_no["liquidity_score"]) / 2
                    spread_total = book_yes["spread"] + book_no["spread"]

            actual_combined = actual_yes + actual_no
            if actual_combined >= 0.98:
                continue  # No real arb after slippage

            profit_pct = (1 - actual_combined) / actual_combined
            ev = EVCalculator.calculate(
                buy_price=actual_combined,
                estimated_prob=1.0,  # Arbitrage is guaranteed
                order_size_usdc=30,
                slippage=0,
                days_to_expiry=1,
            )

            confidence = 95  # Arbitrage is very high confidence
            if liquidity_score < 10:
                confidence -= 20
            if spread_total > 0.05:
                confidence -= 15

            opps.append({
                "strategy": "Arbitrage+",
                "market_id": m["id"],
                "question": m["question"],
                "yes_token": yes_token,
                "no_token": no_token,
                "yes_price": round(actual_yes, 4),
                "no_price": round(actual_no, 4),
                "price": round(actual_combined, 4),
                "combined_price": round(actual_combined, 4),
                "profit_pct": profit_pct,
                "annualized_yield": 0,
                "volume_24h": vol,
                "end_date": m.get("end_date", ""),
                "image": m.get("image", ""),
                "confidence": confidence,
                "ev": ev,
                "analysis": {
                    "liquidity_score": round(liquidity_score, 1),
                    "spread": round(spread_total, 4),
                },
                "priority": 0,
            })

        return opps

    def _scan_tweet_arb(self, markets):
        """Strategy C+: Tweet bucket arbitrage with enhanced detection."""
        opps = []

        # Only genuine Musk tweet-COUNT bucket markets. Substring filters like
        # "elon" wrongly match "barcelona" (barca-ELON-a) and "musk" matches
        # long-term election markets — neither is an arbitrage bucket.
        # A valid bucket must mention "Elon Musk" (as a phrase) AND a
        # "post N-M tweets" count range.
        tweet_markets = [
            m for m in markets
            if re.search(r'\belon\s+musk\b', m["question"], re.IGNORECASE)
            and re.search(r'\bpost\s+[\d,+\-]+(?:\s+tweets?)?', m["question"], re.IGNORECASE)
        ]

        period_groups = {}
        for m in tweet_markets:
            match = re.search(r'from (\w+ \d+) to (\w+ \d+)', m["question"], re.IGNORECASE)
            if not match:
                continue  # only dated weekly buckets are tradable arbs
            period = f"{match.group(1)} to {match.group(2)}"
            period_groups.setdefault(period, []).append(m)

        for period, buckets in period_groups.items():
            if len(buckets) < 2:
                continue

            # Only the buckets we actually BUY count toward the "buy all → one
            # must hit" guarantee (review S4: total_yes previously summed every
            # bucket, including ones we'd never buy).
            valid_buckets = [b for b in buckets if b.get("yes_token") and b.get("yes_price", 0) > 0]
            if len(valid_buckets) < 2:
                continue

            total_yes = sum(b.get("yes_price", 0) for b in valid_buckets)
            if total_yes <= 0 or total_yes >= 0.95:
                continue

            # Sanity-check coverage: ranges must NOT overlap (mutual exclusivity)
            # and an open-ended "+" bucket must cap the set, otherwise
            # "buy all YES → one wins" does not hold.
            ranges = []
            for b in valid_buckets:
                m2 = re.search(r'post\s+([\d,]+)\s*[-–]\s*([\d,]+)\s+tweets', b["question"], re.IGNORECASE)
                if m2:
                    ranges.append((int(m2.group(1).replace(",", "")), int(m2.group(2).replace(",", ""))))
                else:
                    m3 = re.search(r'post\s+([\d,]+)\+\s*tweets', b["question"], re.IGNORECASE)
                    if m3:
                        ranges.append((int(m3.group(1).replace(",", "")), 10 ** 9))
            ranges.sort()
            covered = False
            if ranges:
                prev_hi = -1
                for lo, hi in ranges:
                    if lo <= prev_hi:
                        break  # overlapping ranges → not mutually exclusive
                    prev_hi = hi
                else:
                    if ranges[-1][1] >= 10 ** 9:  # open-ended top bucket present
                        covered = True
            if not covered:
                continue

            total_volume = sum(b.get("volume_24h", 0) for b in valid_buckets)

            profit_pct = (1 - total_yes) / total_yes
            ev = EVCalculator.calculate(
                buy_price=total_yes,
                estimated_prob=1.0,  # One bucket must hit
                order_size_usdc=25,
                slippage=0.01,
                days_to_expiry=3,
            )

            confidence = 88
            if total_volume < 5000:
                confidence -= 15
            if total_yes > 0.90:
                confidence += 5  # Very close to certain

            opps.append({
                "strategy": "TweetArb+",
                "market_id": f"tweet_arb_{period.replace(' ', '_')}",
                "question": f"Tweet ALL BUCKETS ({period})",
                "side": "BUY ALL YES",
                "price": round(total_yes, 4),
                "profit_pct": profit_pct,
                "annualized_yield": 0,
                "volume_24h": total_volume,
                "buckets": [
                    {
                        "question": b["question"],
                        "yes_token": b.get("yes_token", ""),
                        "yes_price": b.get("yes_price", 0),
                        "market_id": b["id"],
                        "end_date": b.get("end_date", ""),
                    }
                    for b in valid_buckets
                ],
                "image": "",
                "confidence": confidence,
                "ev": ev,
                "analysis": {
                    "bucket_count": len(valid_buckets),
                    "total_volume": round(total_volume, 0),
                },
                "priority": 1,
            })

        return opps

    def _scan_momentum(self, markets, now):
        """Strategy D: Follow strong price momentum. More selective."""
        opps = []

        # Only scan top volume markets for momentum (limit API calls)
        top_markets = sorted(markets, key=lambda m: m.get("volume_24h", 0), reverse=True)[:30]

        for m in top_markets:
            end_str = m.get("end_date", "")
            if end_str:
                try:
                    end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                    days = (end_dt - now).total_seconds() / 86400
                    if days < 0 or days > 14:  # Reduced from 30 to 14
                        continue
                except:
                    continue
            else:
                continue  # Require end date

            # Require minimum volume
            vol = m.get("volume_24h", 0)
            if vol < 3000:
                continue
            # Skip speculative esports / match-outcome markets (hard to predict)
            if self.filter_speculative and is_speculative_market(m.get("question", "")):
                continue

            # Check YES side momentum
            for side, price_key, token_key, buying_yes in [
                ("YES", "yes_price", "yes_token", True),
                ("NO", "no_price", "no_token", False),
            ]:
                price = m.get(price_key, 0)
                token_id = m.get(token_key, "")

                # Raised minimum price from 0.05 to 0.20
                if price < 0.20 or price > 0.85:
                    continue
                if not token_id:
                    continue

                hist = None if (self.skip_orderbook or _degraded) else PriceHistoryAnalyzer.analyze(token_id, "1d")
                if not hist:
                    continue

                momentum = hist["momentum"]
                trend = hist["trend_strength"]
                volatility = hist["volatility"]

                # Require stronger momentum — buy the token that is itself
                # RISING (positive own-token momentum is favorable for both
                # sides). The old NO branch bought a FALLING token (review M4).
                if momentum < 0.4:
                    continue

                # Require stronger trend (40 instead of 30)
                if abs(trend) < 40:
                    continue

                # Skip if too volatile (momentum can reverse)
                if volatility > 0.16:
                    continue

                # More conservative probability estimate
                # Add less from momentum than before (0.10 instead of 0.15)
                est_prob = min(0.85, price + abs(momentum) * 0.10)

                # Fetch order book
                book = None if (self.skip_orderbook or _degraded) else OrderBookAnalyzer.analyze(token_id, order_size_usdc=15)
                spread = 0
                liquidity_score = min(100, vol / 100)
                if book:
                    spread = book["spread"]
                    liquidity_score = book["liquidity_score"]
                    if spread > self.MAX_SPREAD:
                        continue

                analysis_data = {
                    "market_price": price,
                    "estimated_prob": est_prob,
                    "days_to_expiry": min(days, 14) if days > 0 else 7,
                    "liquidity_score": liquidity_score,
                    "momentum": momentum,
                    "whale_signal": 0,
                    "volatility": volatility,
                    "mean_reversion": hist["mean_reversion_signal"],
                    "spread": spread,
                    "buying_yes": buying_yes,
                }

                confidence = ConfidenceScorer.score(analysis_data)
                if confidence < self.min_confidence:
                    continue

                ev = EVCalculator.calculate(
                    buy_price=price,
                    estimated_prob=est_prob,
                    order_size_usdc=15,
                    slippage=max(spread / 2, 0.01),
                    days_to_expiry=min(days, 14) if days > 0 else 7,
                )

                if ev.get("skip"):
                    continue

                opps.append({
                    "strategy": "Momentum",
                    "market_id": m["id"],
                    "question": m["question"],
                    "side": side,
                    "token_id": token_id,
                    "price": round(price, 4),
                    "profit_pct": (1 - price) / price,
                    "annualized_yield": 0,
                    "volume_24h": vol,
                    "end_date": end_str,
                    "image": m.get("image", ""),
                    "confidence": confidence,
                    "ev": ev,
                    "analysis": {
                        "momentum": round(momentum, 3),
                        "trend_strength": round(trend, 1),
                        "volatility": round(volatility, 4),
                        "est_prob": round(est_prob, 4),
                        "spread": round(spread, 4),
                    },
                    "priority": 3,
                })

        return opps

    def _scan_mean_reversion(self, markets, now):
        """Strategy E: Find overreactions likely to revert. More conservative."""
        opps = []

        top_markets = sorted(markets, key=lambda m: m.get("volume_24h", 0), reverse=True)[:20]

        for m in top_markets:
            end_str = m.get("end_date", "")
            if end_str:
                try:
                    end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                    days = (end_dt - now).total_seconds() / 86400
                    if days < 0 or days > 14:  # Reduced from 30 to 14 days
                        continue
                except:
                    continue
            else:
                continue  # Require end date for mean reversion

            for side, price_key, token_key, buying_yes in [
                ("YES", "yes_price", "yes_token", True),
                ("NO", "no_price", "no_token", False),
            ]:
                price = m.get(price_key, 0)
                token_id = m.get(token_key, "")

                # Require a healthy price band (avoid near-worthless / near-certain shares)
                if price < 0.25 or price > 0.75:
                    continue
                if not token_id:
                    continue

                # Require minimum volume for mean reversion
                vol = m.get("volume_24h", 0)
                if vol < 2000:
                    continue
                # Skip speculative esports / match-outcome markets (dry-run loss source)
                if self.filter_speculative and is_speculative_market(m.get("question", "")):
                    continue

                hist = None if (self.skip_orderbook or _degraded) else PriceHistoryAnalyzer.analyze(token_id, "1d")
                if not hist:
                    continue

                mr_signal = hist["mean_reversion_signal"]
                current = hist["current_price"]
                avg = hist["avg_price"]

                # Require the token to be UNDERVALUED (price below its average,
                # positive reversion signal). This holds for BOTH sides: a NO
                # position profits when the NO token recovers toward $1. The old
                # NO branch bought an overvalued NO that was expected to fall
                # (review M5).
                if mr_signal < 0.5:
                    continue

                # Token must sit below its average (undervalued → expect reversion up)
                if current >= avg:
                    continue

                # Require minimum deviation from average (at least 15%)
                deviation = abs(current - avg) / avg if avg > 0 else 0
                if deviation < 0.15:
                    continue

                # More conservative probability estimate:
                # Instead of assuming full reversion to average, estimate partial reversion
                # Target price = midpoint between current and average
                target = (current + avg) / 2
                # Probability that price reaches target before expiry
                est_prob = min(0.75, target)  # Cap at 75% (was 0.85)

                # Check volatility - high volatility means less reliable reversion
                vol_penalty = hist["volatility"]
                if vol_penalty > 0.18:
                    continue  # Skip if too volatile
                est_prob -= vol_penalty * 0.2  # Reduce prob by volatility

                # Fetch order book for better analysis
                book = None if (self.skip_orderbook or _degraded) else OrderBookAnalyzer.analyze(token_id, order_size_usdc=15)
                spread = 0
                liquidity_score = min(100, vol / 100)
                if book:
                    spread = book["spread"]
                    liquidity_score = book["liquidity_score"]
                    if spread > self.MAX_SPREAD:
                        continue

                analysis_data = {
                    "market_price": price,
                    "estimated_prob": est_prob,
                    "days_to_expiry": min(days, 14) if days > 0 else 7,
                    "liquidity_score": liquidity_score,
                    "momentum": hist["momentum"],
                    "whale_signal": 0,
                    "volatility": hist["volatility"],
                    "mean_reversion": mr_signal,
                    "spread": spread,
                    "buying_yes": buying_yes,
                }

                confidence = ConfidenceScorer.score(analysis_data)
                if confidence < self.min_confidence:
                    continue

                ev = EVCalculator.calculate(
                    buy_price=price,
                    estimated_prob=est_prob,
                    order_size_usdc=15,
                    slippage=max(spread / 2, 0.01),
                    days_to_expiry=min(days, 14) if days > 0 else 7,
                )

                if ev.get("skip"):
                    continue

                opps.append({
                    "strategy": "MeanReversion",
                    "market_id": m["id"],
                    "question": m["question"],
                    "side": side,
                    "token_id": token_id,
                    "price": round(price, 4),
                    "profit_pct": (1 - price) / price,
                    "annualized_yield": 0,
                    "volume_24h": vol,
                    "end_date": end_str,
                    "image": m.get("image", ""),
                    "confidence": confidence,
                    "ev": ev,
                    "analysis": {
                        "mean_reversion": round(mr_signal, 3),
                        "price_vs_avg": round(deviation, 3),
                        "current_price": round(current, 4),
                        "avg_price": round(avg, 4),
                        "est_prob": round(est_prob, 4),
                        "volatility": round(hist["volatility"], 4),
                        "spread": round(spread, 4),
                    },
                    "priority": 3,
                })

        return opps

    def _scan_smart_money(self, markets, now):
        """Strategy F: Follow whale activity. More selective."""
        opps = []

        # Look at markets with recent high volume (likely to have whale activity)
        top_markets = sorted(markets, key=lambda m: m.get("volume_1h", 0) or m.get("volume_24h", 0), reverse=True)[:15]

        for m in top_markets:
            end_str = m.get("end_date", "")
            if end_str:
                try:
                    end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                    days = (end_dt - now).total_seconds() / 86400
                    if days < 0 or days > 14:
                        continue
                except:
                    continue
            else:
                continue

            vol = m.get("volume_24h", 0)
            if vol < 5000:  # Require higher volume for smart money
                continue
            # Skip speculative esports / match-outcome markets (hard to predict)
            if self.filter_speculative and is_speculative_market(m.get("question", "")):
                continue

            for side, price_key, token_key, buying_yes in [
                ("YES", "yes_price", "yes_token", True),
                ("NO", "no_price", "no_token", False),
            ]:
                price = m.get(price_key, 0)
                token_id = m.get(token_key, "")

                # Raised minimum price from 0.05 to 0.20
                if price < 0.20 or price > 0.85:
                    continue
                if not token_id:
                    continue

                whale = SmartMoneyTracker.analyze_market(m["id"], token_id)
                if not whale:
                    continue

                # Require stronger whale signal: buy_ratio > 0.80 (was 0.70)
                if buying_yes and not (whale.get("whale_buying") and whale.get("buy_ratio", 0) > 0.80):
                    continue
                if not buying_yes and not (whale.get("whale_selling") and whale.get("buy_ratio", 0) < 0.20):
                    continue

                # Require at least 3 large trades (was 2)
                if whale.get("large_trades", 0) < 3:
                    continue

                # Also check price history for confirmation
                hist = None if (self.skip_orderbook or _degraded) else PriceHistoryAnalyzer.analyze(token_id, "1d")
                volatility = 0
                momentum = 0
                if hist:
                    volatility = hist["volatility"]
                    momentum = hist["momentum"]
                    # Skip if too volatile
                    if volatility > 0.15:
                        continue
                    # For YES buy, momentum should not be strongly negative
                    if buying_yes and momentum < -0.2:
                        continue
                    # For NO buy, momentum should not be strongly positive
                    if not buying_yes and momentum > 0.2:
                        continue

                # Fetch order book
                book = None if (self.skip_orderbook or _degraded) else OrderBookAnalyzer.analyze(token_id, order_size_usdc=15)
                spread = 0
                liquidity_score = min(100, vol / 100)
                if book:
                    spread = book["spread"]
                    liquidity_score = book["liquidity_score"]
                    if spread > self.MAX_SPREAD:
                        continue

                # More conservative probability estimate
                est_prob = min(0.80, price + whale.get("confidence_boost", 0) * 0.005)

                analysis_data = {
                    "market_price": price,
                    "estimated_prob": est_prob,
                    "days_to_expiry": min(days, 14) if days > 0 else 7,
                    "liquidity_score": liquidity_score,
                    "momentum": momentum,
                    "whale_signal": whale.get("confidence_boost", 0),
                    "volatility": volatility,
                    "mean_reversion": 0,
                    "spread": spread,
                    "buying_yes": buying_yes,
                }

                confidence = ConfidenceScorer.score(analysis_data)
                if confidence < self.min_confidence:
                    continue

                ev = EVCalculator.calculate(
                    buy_price=price,
                    estimated_prob=est_prob,
                    order_size_usdc=15,
                    slippage=max(spread / 2, 0.01),
                    days_to_expiry=min(days, 14) if days > 0 else 7,
                )

                if ev.get("skip"):
                    continue

                opps.append({
                    "strategy": "SmartMoney",
                    "market_id": m["id"],
                    "question": m["question"],
                    "side": side,
                    "token_id": token_id,
                    "price": round(price, 4),
                    "profit_pct": (1 - price) / price,
                    "annualized_yield": 0,
                    "volume_24h": vol,
                    "end_date": end_str,
                    "image": m.get("image", ""),
                    "confidence": confidence,
                    "ev": ev,
                    "analysis": {
                        "whale_buying": whale.get("whale_buying", False),
                        "buy_ratio": whale.get("buy_ratio", 0),
                        "net_flow": whale.get("net_flow", 0),
                        "large_trades": whale.get("large_trades", 0),
                        "est_prob": round(est_prob, 4),
                        "spread": round(spread, 4),
                    },
                    "priority": 2,
                })

        return opps


# ============================================================
#  Smart Position Sizing (Kelly with caps)
# ============================================================

class SmartPositionSizer:
    """Calculate optimal position size using Kelly criterion with safety caps."""

    @staticmethod
    def calculate(ev_data, bankroll, max_pct=0.05, min_usdc=5):
        """
        Returns recommended position size in USDC.
        Uses Half-Kelly with hard caps.
        """
        kelly = ev_data.get("kelly_fraction", 0)
        if kelly <= 0:
            return min_usdc

        # Half Kelly for safety
        half_kelly = kelly / 2

        # Calculate position size
        kelly_size = bankroll * half_kelly

        # Apply caps
        max_size = bankroll * max_pct
        position_size = min(kelly_size, max_size)

        # Apply minimum
        position_size = max(position_size, min_usdc)

        return round(position_size, 2)

    @staticmethod
    def calculate_with_confidence(ev_data, confidence, bankroll, max_pct=0.05, min_usdc=5, strategy=""):
        """
        Adjust Kelly by confidence score.
        Higher confidence = closer to full Kelly.
        Lower confidence = more conservative.
        Speculative strategies (Momentum, MeanReversion, SmartMoney) get reduced sizing.
        """
        kelly = ev_data.get("kelly_fraction", 0)
        if kelly <= 0:
            return min_usdc

        # Scale Kelly by confidence (0-100)
        confidence_factor = max(0.3, min(1.0, confidence / 100))

        # Strategy-specific Kelly fraction (more conservative for speculative)
        if strategy in ("Momentum", "MeanReversion", "SmartMoney"):
            kelly_fraction = 0.15  # 15% of Kelly (was 25%)
        elif strategy in ("ExpiryYield+", "ExpiryYield"):
            kelly_fraction = 0.30  # 30% of Kelly (safer strategy)
        else:
            kelly_fraction = 0.25  # Default 25%

        adjusted_kelly = kelly * kelly_fraction * confidence_factor

        # Strategy-specific max position percentage
        if strategy in ("Momentum", "MeanReversion", "SmartMoney"):
            effective_max_pct = min(max_pct, 0.03)  # Cap at 3% for speculative
        else:
            effective_max_pct = max_pct

        kelly_size = bankroll * adjusted_kelly
        max_size = bankroll * effective_max_pct
        position_size = min(kelly_size, max_size)
        position_size = max(position_size, min_usdc)

        return round(position_size, 2)


# ============================================================
#  Smart Exit Manager
# ============================================================

class SmartExitManager:
    """
    Intelligent exit strategy for open positions.
    - Take profit early when price moves favorably
    - Trailing stop for momentum positions
    - Hold expiry yield to resolution (high confidence)
    """

    # Take profit thresholds by strategy (percentage of entry price)
    TP_THRESHOLDS = {
        "ExpiryYield+": 0.02,   # Take profit if price moved 2% in our favor (hold to expiry is main goal)
        "Arbitrage+": 0.00,     # Hold to resolution (guaranteed)
        "TweetArb+": 0.00,      # Hold to resolution (guaranteed)
        "Momentum": 0.06,       # Take profit at 6% (was 5%)
        "MeanReversion": 0.05,  # Take profit at 5% (was 4%)
        "SmartMoney": 0.05,     # Take profit at 5% (was 4%)
    }

    # Trailing stop percentages (stop loss from peak)
    TRAILING_STOP = {
        "Momentum": 0.025,      # Tighter: 2.5% (was 3%)
        "MeanReversion": 0.02,  # Tighter: 2% (was 2.5%)
        "SmartMoney": 0.02,     # Tighter: 2% (was 2.5%)
        "ExpiryYield+": 0.008,  # Very tight: 0.8% (was 1%)
    }

    # Stop loss: maximum loss as fraction of entry price before cutting
    STOP_LOSS = {
        "ExpiryYield+": 0.03,   # 3% max loss (high confidence, tight stop)
        "Momentum": 0.04,       # 4% max loss
        "MeanReversion": 0.05,  # 5% max loss (wider room for reversion)
        "SmartMoney": 0.04,     # 4% max loss
    }

    # TweetArb+ buckets can take profit early once they reach this ROI
    # (e.g. 1.0 = sell after a +100% gain) — cheap buckets can spike, and
    # locking the gain beats holding a specific bucket to expiry.
    # Overridden at runtime from trading_config.json via auto_trader.
    TWEETARB_TP_ROI = 1.0

    # Ignore take-profit / exit signals worth less than this in absolute profit
    MIN_PROFIT_TO_EXIT = 0.50

    # Free capital stuck in long-open positions (directional trades) whose
    # market hasn't resolved and won't resolve soon — better "sell" control.
    MAX_HOLD_DAYS = 14

    @staticmethod
    def should_exit(position, current_price, peak_price=None):
        """
        Determine if a position should be exited.
        Returns: (should_exit, reason, exit_price)
        """
        strategy = position.get("strategy", "")
        entry_price = position.get("entry_price", 0)
        side = position.get("side", "").upper()

        # Arbitrage strategies: hold to resolution by default. BUT TweetArb+
        # buckets may take profit early once they hit the target ROI, so a
        # surging bucket is realized instead of being held to expiry.
        if strategy in ("Arbitrage+", "TweetArb+"):
            if strategy == "TweetArb+" and entry_price > 0 and current_price > entry_price:
                gain_pct = (current_price - entry_price) / entry_price
                profit = (current_price - entry_price) * position.get("shares", 0)
                if gain_pct >= SmartExitManager.TWEETARB_TP_ROI and profit >= SmartExitManager.MIN_PROFIT_TO_EXIT:
                    return True, "take_profit", current_price
            return False, "hold_to_resolution", None

        # ExpiryYield+ is held to resolution: its edge is the annualized yield,
        # and taking profit at +2% forfeits the near-$1 payoff (review M14).
        # Only a hard stop-loss protects against a broken market.
        if strategy == "ExpiryYield+":
            if entry_price > 0 and current_price < entry_price * (1 - SmartExitManager.STOP_LOSS.get(strategy, 0.03)):
                return True, "stop_loss", current_price
            return False, "hold_to_resolution", None

        # Price change as a FRACTION of entry price — the TP / STOP thresholds
        # are stated as percentages of entry (review M13: previously these were
        # absolute price deltas, so a 0.02 threshold meant 2% at $1 but 10% at
        # $0.20, and the same nominal threshold behaved differently per price).
        if entry_price > 0:
            price_change = (current_price - entry_price) / entry_price
        else:
            price_change = 0.0

        # Take profit check
        tp_threshold = SmartExitManager.TP_THRESHOLDS.get(strategy, 0.03)
        if price_change >= tp_threshold:
            profit = price_change * position.get("shares", 0) * entry_price
            if profit > SmartExitManager.MIN_PROFIT_TO_EXIT:
                return True, "take_profit", current_price

        # Trailing stop check (drop measured as % of peak)
        if peak_price and strategy in SmartExitManager.TRAILING_STOP:
            trail_pct = SmartExitManager.TRAILING_STOP[strategy]
            drop_from_peak = (peak_price - current_price) / peak_price if peak_price > 0 else 0
            if drop_from_peak >= trail_pct and peak_price > entry_price:
                # Only trigger trailing stop if we're still in profit
                if current_price > entry_price:
                    return True, "trailing_stop", current_price

        # Stop loss: strategy-specific threshold (percentage of entry)
        stop_loss_threshold = SmartExitManager.STOP_LOSS.get(strategy, 0.04)
        if price_change < -stop_loss_threshold:
            return True, "stop_loss", current_price

        # Additional: hard stop if price dropped below 50% of entry (catastrophic)
        if current_price < entry_price * 0.5:
            return True, "stop_loss", current_price

        # Time-based exit: free capital stuck in a long-open position whose
        # market hasn't resolved and won't resolve soon. Applies to directional
        # trades (expiry/arb already return hold_to_resolution above).
        end_str = position.get("end_date", "")
        resolves_soon = False
        if end_str:
            try:
                end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                resolves_soon = (end_dt - datetime.now(timezone.utc)) <= timedelta(days=2)
            except Exception:
                resolves_soon = False
        if not resolves_soon:
            opened_at = position.get("opened_at", "")
            if opened_at:
                try:
                    opened_dt = datetime.fromisoformat(opened_at.replace("Z", "+00:00"))
                    days_open = (datetime.now(timezone.utc) - opened_dt).total_seconds() / 86400
                    if days_open >= SmartExitManager.MAX_HOLD_DAYS:
                        return True, "time_exit", current_price
                except Exception:
                    pass

        # Hold
        return False, "hold", None


# ============================================================
#  Performance Tracker
# ============================================================

class PerformanceTracker:
    """Track strategy performance and adjust parameters."""

    def __init__(self):
        self.stats = {
            "ExpiryYield+": {"trades": 0, "wins": 0, "losses": 0, "total_pnl": 0},
            "Arbitrage+": {"trades": 0, "wins": 0, "losses": 0, "total_pnl": 0},
            "TweetArb+": {"trades": 0, "wins": 0, "losses": 0, "total_pnl": 0},
            "Momentum": {"trades": 0, "wins": 0, "losses": 0, "total_pnl": 0},
            "MeanReversion": {"trades": 0, "wins": 0, "losses": 0, "total_pnl": 0},
            "SmartMoney": {"trades": 0, "wins": 0, "losses": 0, "total_pnl": 0},
        }

    def record_trade(self, strategy, pnl):
        if strategy not in self.stats:
            self.stats[strategy] = {"trades": 0, "wins": 0, "losses": 0, "total_pnl": 0}
        self.stats[strategy]["trades"] += 1
        self.stats[strategy]["total_pnl"] += pnl
        if pnl > 0:
            self.stats[strategy]["wins"] += 1
        else:
            self.stats[strategy]["losses"] += 1

    def get_win_rate(self, strategy):
        s = self.stats.get(strategy, {})
        total = s.get("trades", 0)
        if total == 0:
            return 0.5  # Default
        return s.get("wins", 0) / total

    def get_avg_pnl(self, strategy):
        s = self.stats.get(strategy, {})
        trades = s.get("trades", 0)
        if trades == 0:
            return 0
        return s.get("total_pnl", 0) / trades

    def get_strategy_weights(self):
        """Return weights for each strategy based on performance."""
        weights = {}
        for strategy, s in self.stats.items():
            total = s.get("trades", 0)
            if total < 3:
                weights[strategy] = 1.0  # Default weight
            else:
                win_rate = s.get("wins", 0) / total
                avg_pnl = s.get("total_pnl", 0) / total
                # Weight = win_rate * (1 + avg_pnl / bankroll)
                weights[strategy] = max(0.1, min(2.0, win_rate * (1 + avg_pnl / 100)))
        return weights

    def summary(self):
        """Return performance summary."""
        lines = []
        for strategy, s in sorted(self.stats.items()):
            total = s.get("trades", 0)
            if total == 0:
                continue
            win_rate = s.get("wins", 0) / total * 100
            avg_pnl = s.get("total_pnl", 0) / total
            lines.append(
                f"  {strategy}: {total} trades, {win_rate:.0f}% win, avg ${avg_pnl:.2f}"
            )
        return "\n".join(lines) if lines else "  No trades yet"
