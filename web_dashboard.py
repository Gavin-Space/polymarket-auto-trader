#!/usr/bin/env python3
"""
Polymarket Monitor Dashboard
=============================
A Flask web app that provides a Polymarket-style UI for monitoring
the trading bot's scans, opportunities, positions, and logs.

Run:
  python web_dashboard.py
  # Then open http://localhost:5000 in your browser
"""

import os
import sys
import json
import time
import threading
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Add libs to path
LIBS_DIR = Path(__file__).parent / "libs"
sys.path.insert(0, str(LIBS_DIR))

import requests
from flask import Flask, jsonify, request, Response

app = Flask(__name__)

# ============================================================
#  Config
# ============================================================

WORKSPACE = Path(__file__).parent
STATE_FILE = WORKSPACE / "bot_state.json"
LOG_FILE = WORKSPACE / "bot_trades.log"
GAMMA_API = "https://gamma-api.polymarket.com"

# In-memory cache for scan results
_scan_cache = {
    "opportunities": [],
    "markets": [],
    "last_scan": None,
    "scanning": False,
}

# Background scan thread
_scan_thread = None


# ============================================================
#  Helpers
# ============================================================

def load_state():
    """Load bot state from JSON file."""
    try:
        if STATE_FILE.exists():
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {"positions": [], "daily_pnl": {}, "last_scan": None, "daily_loss_hit": None}


def read_recent_logs(n=50):
    """Read the last N lines from the log file."""
    try:
        if LOG_FILE.exists():
            with open(LOG_FILE, "r", encoding="utf-8") as f:
                lines = f.readlines()
            return [l.strip() for l in lines[-n:] if l.strip()]
    except Exception:
        pass
    return []


def parse_market(raw):
    """Parse a raw Gamma market object."""
    try:
        outcomes = json.loads(raw.get("outcomes", "[]"))
        prices = json.loads(raw.get("outcomePrices", "[]"))
        token_ids = json.loads(raw.get("clobTokenIds", "[]"))

        result = {
            "id": raw.get("id", ""),
            "question": raw.get("question", ""),
            "slug": raw.get("slug", ""),
            "outcomes": outcomes,
            "prices": [float(p) for p in prices],
            "token_ids": token_ids,
            "volume": float(raw.get("volume", 0) or 0),
            "volume_24h": float(raw.get("volume24hr", 0) or 0),
            "liquidity": float(raw.get("liquidity", 0) or 0),
            "end_date": raw.get("endDate", ""),
            "active": raw.get("active", False),
            "closed": raw.get("closed", False),
            "tags": raw.get("tags", []),
            "image": raw.get("image", "") or raw.get("icon", ""),
            "description": raw.get("description", ""),
        }

        # YES/NO mapping
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
    except Exception:
        return None


def fetch_markets(limit=100, offset=0, order="volume24hr", ascending=False):
    """Fetch markets from Gamma API with retry."""
    params = {
        "active": "true",
        "closed": "false",
        "limit": limit,
        "offset": offset,
        "order": order,
        "ascending": str(ascending).lower(),
    }
    for attempt in range(3):
        try:
            r = requests.get(f"{GAMMA_API}/markets", params=params, timeout=20)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if attempt < 2:
                time.sleep(2)
    return []


def run_scan_background():
    """Run a market scan in background and cache results."""
    if _scan_cache["scanning"]:
        return
    _scan_cache["scanning"] = True

    def _scan():
        try:
            now = datetime.now(timezone.utc)
            # Fetch active markets by volume
            all_raw = []
            for off in range(0, 300, 100):
                batch = fetch_markets(limit=100, offset=off, order="volume24hr", ascending=False)
                if not batch:
                    break
                all_raw.extend(batch)
                if len(batch) < 100:
                    break

            # Fetch ending soon
            ending_raw = fetch_markets(limit=100, order="endDate", ascending=True)

            markets = []
            opportunities = []

            for raw in all_raw:
                m = parse_market(raw)
                if m:
                    markets.append(m)

            # Strategy A: Expiry Yield (scan both ending_raw AND volume-sorted markets)
            seen_ids = set()
            expiry_candidates = []
            for raw in ending_raw:
                m = parse_market(raw)
                if m and m["id"] not in seen_ids:
                    expiry_candidates.append(m)
                    seen_ids.add(m["id"])
            # Also add high-volume markets that are near expiry
            for m in markets:
                if m["id"] not in seen_ids:
                    expiry_candidates.append(m)
                    seen_ids.add(m["id"])

            for m in expiry_candidates:
                end_str = m.get("end_date", "")
                if not end_str:
                    continue
                try:
                    end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                    days = (end_dt - now).total_seconds() / 86400
                except:
                    continue
                if days < -1 or days > 7:
                    continue

                no_price = m.get("no_price", 0)
                yes_price = m.get("yes_price", 0)
                best_side = None
                best_price = 0

                if no_price >= 0.90:
                    best_side = "NO"
                    best_price = no_price
                elif yes_price >= 0.90:
                    best_side = "YES"
                    best_price = yes_price
                else:
                    continue

                min_vol = max(10000 / 20, 500)
                if m.get("volume_24h", 0) < min_vol and m.get("liquidity", 0) < 1000:
                    continue

                if best_price >= 1.0:
                    continue
                profit_pct = (1 - best_price) / best_price
                annualized = profit_pct * 365 / max(days, 0.1)

                opportunities.append({
                    "strategy": "ExpiryYield",
                    "market_id": m["id"],
                    "question": m["question"],
                    "side": best_side,
                    "price": best_price,
                    "days_to_expiry": round(days, 2),
                    "profit_pct": profit_pct,
                    "annualized_yield": annualized,
                    "volume_24h": m.get("volume_24h", 0),
                    "end_date": end_str,
                    "image": m.get("image", ""),
                })

            # Strategy E: Arbitrage
            for m in markets:
                yes_price = m.get("yes_price", 0)
                no_price = m.get("no_price", 0)
                if yes_price <= 0 or no_price <= 0:
                    continue
                combined = yes_price + no_price
                if combined >= 0.97:
                    continue
                if m.get("volume_24h", 0) < 500 and m.get("liquidity", 0) < 500:
                    continue
                profit_pct = (1 - combined) / combined
                opportunities.append({
                    "strategy": "Arbitrage",
                    "market_id": m["id"],
                    "question": m["question"],
                    "side": "BUY BOTH",
                    "price": combined,
                    "yes_price": yes_price,
                    "no_price": no_price,
                    "profit_pct": profit_pct,
                    "annualized_yield": 0,
                    "volume_24h": m.get("volume_24h", 0),
                    "end_date": m.get("end_date", ""),
                    "image": m.get("image", ""),
                })

            # Strategy B: Tweet Prediction
            tweet_markets = [m for m in markets if any(kw in m["question"].lower() for kw in ["tweet", "elon", "musk"])]
            periods = {}
            for m in tweet_markets:
                q = m["question"]
                for period_key in ["Aug 10-12", "Aug 13-19", "Aug 6-12", "Aug 7-13", "this week", "Aug 13"]:
                    if period_key.lower() in q.lower():
                        periods.setdefault(period_key, []).append(m)
                        break
                else:
                    periods.setdefault("Other Tweet", []).append(m)

            for period, buckets in periods.items():
                if len(buckets) < 2:
                    continue
                total_yes = sum(b.get("yes_price", 0) for b in buckets)
                if total_yes > 0 and total_yes < 0.95 and len(buckets) >= 3:
                    profit_pct = (1 - total_yes) / total_yes
                    opportunities.append({
                        "strategy": "TweetArb",
                        "market_id": buckets[0]["id"],
                        "question": f"Musk Tweets {period} - ALL BUCKETS ARB",
                        "side": "BUY ALL YES",
                        "price": total_yes,
                        "profit_pct": profit_pct,
                        "annualized_yield": 0,
                        "volume_24h": sum(b.get("volume_24h", 0) for b in buckets),
                        "end_date": buckets[0].get("end_date", ""),
                        "bucket_count": len(buckets),
                        "image": "",
                    })

            # Sort: ExpiryYield by annualized, then others
            opportunities.sort(key=lambda x: x.get("annualized_yield", x.get("profit_pct", 0)), reverse=True)

            _scan_cache["opportunities"] = opportunities
            _scan_cache["markets"] = markets
            _scan_cache["last_scan"] = datetime.now(timezone.utc).isoformat()

        except Exception as e:
            print(f"Scan error: {e}")
        finally:
            _scan_cache["scanning"] = False

    t = threading.Thread(target=_scan, daemon=True)
    t.start()


# ============================================================
#  API Routes
# ============================================================

@app.route("/")
def index():
    return HTML_TEMPLATE


@app.route("/api/dashboard")
def api_dashboard():
    """Return all dashboard data in one call."""
    state = load_state()
    logs = read_recent_logs(50)

    # Calculate stats
    positions = state.get("positions", [])
    open_positions = [p for p in positions if p.get("status") == "open"]
    total_exposure = sum(p.get("cost_usdc", 0) for p in open_positions)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    daily = state.get("daily_pnl", {}).get(today, {"realized": 0, "trades": 0})

    # Get mode
    env_mode = "dry_run"
    env_path = WORKSPACE / ".env"
    if env_path.exists():
        try:
            with open(env_path, "r") as f:
                for line in f:
                    if "TRADING_MODE" in line and "=" in line:
                        env_mode = line.split("=")[1].strip().strip('"').strip("'")
        except:
            pass

    bankroll = 200.0
    if env_path.exists():
        try:
            with open(env_path, "r") as f:
                for line in f:
                    if "BANKROLL_USDC" in line and "=" in line:
                        bankroll = float(line.split("=")[1].strip())
        except:
            pass

    return jsonify({
        "status": {
            "mode": env_mode,
            "bankroll": bankroll,
            "scanning": _scan_cache["scanning"],
            "last_scan": _scan_cache["last_scan"] or state.get("last_scan"),
            "total_exposure": round(total_exposure, 2),
            "available_cash": round(bankroll - total_exposure, 2),
            "open_positions": len(open_positions),
            "max_positions": 10,
            "daily_pnl": daily.get("realized", 0),
            "daily_trades": daily.get("trades", 0),
            "daily_loss_hit": state.get("daily_loss_hit"),
        },
        "positions": positions,
        "opportunities": _scan_cache["opportunities"],
        "markets_count": len(_scan_cache["markets"]),
        "logs": logs,
    })


@app.route("/api/markets")
def api_markets():
    """Return cached markets with optional search/filter."""
    q = request.args.get("q", "").lower()
    strategy = request.args.get("strategy", "")
    limit = int(request.args.get("limit", 50))

    markets = _scan_cache["markets"]

    if q:
        markets = [m for m in markets if q in m["question"].lower()]

    if strategy == "tweet":
        markets = [m for m in markets if any(kw in m["question"].lower() for kw in ["tweet", "elon", "musk"])]
    elif strategy == "expiry":
        now = datetime.now(timezone.utc)
        filtered = []
        for m in markets:
            end_str = m.get("end_date", "")
            if not end_str:
                continue
            try:
                end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                days = (end_dt - now).total_seconds() / 86400
                if 0 < days <= 7:
                    m_copy = dict(m)
                    m_copy["days_to_expiry"] = round(days, 1)
                    filtered.append(m_copy)
            except:
                pass
        markets = filtered
    elif strategy == "highvol":
        markets = sorted(markets, key=lambda x: x.get("volume_24h", 0), reverse=True)

    return jsonify(markets[:limit])


@app.route("/api/scan", methods=["POST"])
def api_scan():
    """Trigger a new scan."""
    if _scan_cache["scanning"]:
        return jsonify({"status": "already_scanning"})
    run_scan_background()
    return jsonify({"status": "scan_started"})


@app.route("/api/logs")
def api_logs():
    """Return recent logs."""
    n = int(request.args.get("n", 100))
    return jsonify(read_recent_logs(n))


# ============================================================
#  HTML Template (Polymarket-style UI)
# ============================================================

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>PolyMonitor - 预测市场监控台</title>
<style>
:root {
  --bg: #f8f9fa;
  --card-bg: #ffffff;
  --border: #e9ecef;
  --text: #1a1a2e;
  --text-muted: #6c757d;
  --primary: #1652f0;
  --primary-light: #e8f0fe;
  --green: #00b8a9;
  --red: #ff5c5c;
  --orange: #ff9f43;
  --purple: #6c5ce7;
  --shadow: 0 2px 8px rgba(0,0,0,0.06);
  --radius: 12px;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
  background: var(--bg);
  color: var(--text);
  min-height: 100vh;
}

/* Header */
.header {
  background: var(--card-bg);
  border-bottom: 1px solid var(--border);
  padding: 0 24px;
  height: 64px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  position: sticky;
  top: 0;
  z-index: 100;
  box-shadow: var(--shadow);
}
.header-left {
  display: flex;
  align-items: center;
  gap: 16px;
}
.logo {
  font-size: 22px;
  font-weight: 800;
  color: var(--primary);
  letter-spacing: -0.5px;
}
.logo span { color: var(--text); }
.mode-badge {
  padding: 4px 10px;
  border-radius: 20px;
  font-size: 12px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.5px;
}
.mode-badge.live { background: #fee; color: var(--red); }
.mode-badge.dry_run { background: #fff3e0; color: var(--orange); }
.mode-badge.scanning { background: var(--primary-light); color: var(--primary); }
.header-right {
  display: flex;
  align-items: center;
  gap: 24px;
}
.stat-pill {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
}
.stat-pill .label {
  font-size: 11px;
  color: var(--text-muted);
  text-transform: uppercase;
  letter-spacing: 0.5px;
}
.stat-pill .value {
  font-size: 16px;
  font-weight: 700;
}
.stat-pill .value.positive { color: var(--green); }
.stat-pill .value.negative { color: var(--red); }
.btn-scan {
  background: var(--primary);
  color: white;
  border: none;
  padding: 8px 18px;
  border-radius: 8px;
  font-size: 14px;
  font-weight: 600;
  cursor: pointer;
  transition: opacity 0.2s;
}
.btn-scan:hover { opacity: 0.85; }
.btn-scan:disabled { opacity: 0.5; cursor: not-allowed; }

/* Layout */
.container {
  display: flex;
  max-width: 1400px;
  margin: 0 auto;
  padding: 24px;
  gap: 24px;
}

/* Sidebar */
.sidebar {
  width: 220px;
  flex-shrink: 0;
}
.sidebar-section {
  background: var(--card-bg);
  border-radius: var(--radius);
  padding: 16px;
  margin-bottom: 16px;
  box-shadow: var(--shadow);
}
.sidebar-title {
  font-size: 12px;
  font-weight: 700;
  text-transform: uppercase;
  color: var(--text-muted);
  margin-bottom: 12px;
  letter-spacing: 0.5px;
}
.nav-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 12px;
  border-radius: 8px;
  cursor: pointer;
  font-size: 14px;
  font-weight: 500;
  transition: background 0.15s;
  margin-bottom: 2px;
}
.nav-item:hover { background: var(--bg); }
.nav-item.active { background: var(--primary-light); color: var(--primary); }
.nav-item .count {
  background: var(--bg);
  padding: 2px 8px;
  border-radius: 12px;
  font-size: 12px;
  font-weight: 600;
}
.nav-item.active .count { background: white; color: var(--primary); }

/* Strategy filter */
.strategy-filter {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 16px;
}
.chip {
  padding: 6px 14px;
  border-radius: 20px;
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  border: 2px solid var(--border);
  background: var(--card-bg);
  transition: all 0.15s;
}
.chip:hover { border-color: var(--primary); }
.chip.active {
  background: var(--primary);
  color: white;
  border-color: var(--primary);
}
.chip .dot {
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  margin-right: 6px;
  vertical-align: middle;
}

/* Main content */
.main {
  flex: 1;
  min-width: 0;
}
.tab-content { display: none; }
.tab-content.active { display: block; }

/* Section title */
.section-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 16px;
}
.section-title {
  font-size: 20px;
  font-weight: 700;
}
.section-subtitle {
  font-size: 13px;
  color: var(--text-muted);
  margin-top: 2px;
}

/* Market cards grid */
.cards-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(340px, 1fr));
  gap: 16px;
}
.market-card {
  background: var(--card-bg);
  border-radius: var(--radius);
  padding: 20px;
  box-shadow: var(--shadow);
  transition: transform 0.15s, box-shadow 0.15s;
  border: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.market-card:hover {
  transform: translateY(-2px);
  box-shadow: 0 4px 16px rgba(0,0,0,0.1);
}
.card-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}
.card-question {
  font-size: 15px;
  font-weight: 600;
  line-height: 1.4;
  flex: 1;
}
.card-image {
  width: 44px;
  height: 44px;
  border-radius: 8px;
  object-fit: cover;
  flex-shrink: 0;
  background: var(--bg);
}
.card-image-placeholder {
  width: 44px;
  height: 44px;
  border-radius: 8px;
  background: var(--primary-light);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 20px;
  flex-shrink: 0;
}

/* Probability bar */
.prob-bar-container {
  display: flex;
  height: 40px;
  border-radius: 8px;
  overflow: hidden;
  border: 1px solid var(--border);
}
.prob-bar {
  display: flex;
  align-items: center;
  justify-content: center;
  font-weight: 700;
  font-size: 14px;
  color: white;
  transition: flex 0.3s;
  min-width: 30px;
}
.prob-bar.yes { background: var(--primary); }
.prob-bar.no { background: #e2e8f0; color: var(--text); }
.prob-bar.multi { background: var(--purple); }

/* Strategy badge */
.strategy-badge {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 3px 10px;
  border-radius: 12px;
  font-size: 11px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.5px;
}
.strategy-badge.ExpiryYield { background: #e8f5e9; color: #2e7d32; }
.strategy-badge.Arbitrage { background: #fff3e0; color: #e65100; }
.strategy-badge.TweetArb { background: #f3e5f5; color: #7b1fa2; }
.strategy-badge.TweetPrediction { background: #e3f2fd; color: #1565c0; }

/* Card stats */
.card-stats {
  display: flex;
  gap: 16px;
  font-size: 13px;
}
.card-stat {
  display: flex;
  flex-direction: column;
}
.card-stat .stat-label {
  color: var(--text-muted);
  font-size: 11px;
  text-transform: uppercase;
}
.card-stat .stat-value {
  font-weight: 700;
  font-size: 15px;
}
.stat-value.positive { color: var(--green); }
.stat-value.negative { color: var(--red); }
.stat-value.highlight { color: var(--primary); }

/* Card footer */
.card-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding-top: 12px;
  border-top: 1px solid var(--border);
}
.card-meta {
  font-size: 12px;
  color: var(--text-muted);
}
.btn-trade {
  background: var(--primary);
  color: white;
  border: none;
  padding: 6px 16px;
  border-radius: 6px;
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  text-decoration: none;
  transition: opacity 0.2s;
}
.btn-trade:hover { opacity: 0.85; }

/* Positions table */
.positions-table {
  width: 100%;
  background: var(--card-bg);
  border-radius: var(--radius);
  overflow: hidden;
  box-shadow: var(--shadow);
  border-collapse: collapse;
}
.positions-table th {
  background: var(--bg);
  padding: 12px 16px;
  text-align: left;
  font-size: 12px;
  font-weight: 700;
  text-transform: uppercase;
  color: var(--text-muted);
  letter-spacing: 0.5px;
  border-bottom: 1px solid var(--border);
}
.positions-table td {
  padding: 12px 16px;
  font-size: 14px;
  border-bottom: 1px solid var(--border);
}
.positions-table tr:last-child td { border-bottom: none; }
.positions-table tr:hover td { background: var(--bg); }
.status-badge {
  padding: 3px 10px;
  border-radius: 12px;
  font-size: 12px;
  font-weight: 600;
}
.status-badge.open { background: var(--primary-light); color: var(--primary); }
.status-badge.won { background: #e8f5e9; color: #2e7d32; }
.status-badge.lost { background: #fee; color: #c62828; }

/* Log feed */
.log-container {
  background: #1a1a2e;
  border-radius: var(--radius);
  padding: 16px;
  font-family: 'Fira Code', 'Courier New', monospace;
  font-size: 13px;
  line-height: 1.6;
  max-height: 600px;
  overflow-y: auto;
  box-shadow: var(--shadow);
}
.log-line {
  color: #a0a0b8;
  word-break: break-all;
}
.log-line .ts { color: #555; }
.log-line .level-INFO { color: #4fc3f7; }
.log-line .level-WARNING { color: #ffb74d; }
.log-line .level-ERROR { color: #ff5c5c; }
.log-line .level-SCAN { color: #81c784; }

/* Stats overview cards */
.stats-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
  gap: 16px;
  margin-bottom: 24px;
}
.stat-card {
  background: var(--card-bg);
  border-radius: var(--radius);
  padding: 20px;
  box-shadow: var(--shadow);
  border: 1px solid var(--border);
}
.stat-card .stat-icon {
  font-size: 24px;
  margin-bottom: 8px;
}
.stat-card .stat-label {
  font-size: 12px;
  color: var(--text-muted);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  margin-bottom: 4px;
}
.stat-card .stat-num {
  font-size: 28px;
  font-weight: 800;
}
.stat-card .stat-sub {
  font-size: 12px;
  color: var(--text-muted);
  margin-top: 4px;
}

/* Search bar */
.search-bar {
  width: 100%;
  padding: 10px 16px;
  border: 2px solid var(--border);
  border-radius: 8px;
  font-size: 14px;
  outline: none;
  transition: border-color 0.2s;
  margin-bottom: 16px;
  background: var(--card-bg);
}
.search-bar:focus { border-color: var(--primary); }

/* Empty state */
.empty-state {
  text-align: center;
  padding: 60px 20px;
  color: var(--text-muted);
}
.empty-state .icon { font-size: 48px; margin-bottom: 12px; }
.empty-state .title { font-size: 18px; font-weight: 600; margin-bottom: 4px; }
.empty-state .desc { font-size: 14px; }

/* Loading spinner */
.spinner {
  display: inline-block;
  width: 16px;
  height: 16px;
  border: 2px solid var(--border);
  border-top-color: var(--primary);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }

/* Scrollbar */
::-webkit-scrollbar { width: 8px; height: 8px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: #ccc; border-radius: 4px; }
::-webkit-scrollbar-thumb:hover { background: #aaa; }
.log-container::-webkit-scrollbar-thumb { background: #333; }

/* Responsive */
@media (max-width: 768px) {
  .sidebar { display: none; }
  .container { padding: 12px; }
  .header { padding: 0 12px; }
  .header-right { gap: 12px; }
  .stat-pill .value { font-size: 13px; }
  .cards-grid { grid-template-columns: 1fr; }
}
</style>
</head>
<body>

<!-- Header -->
<div class="header">
  <div class="header-left">
    <div class="logo">Poly<span>Monitor</span></div>
    <div id="modeBadge" class="mode-badge dry_run">DRY RUN</div>
  </div>
  <div class="header-right">
    <div class="stat-pill">
      <span class="label">总资金</span>
      <span class="value" id="statBankroll">$200</span>
    </div>
    <div class="stat-pill">
      <span class="label">已投入</span>
      <span class="value" id="statExposure">$0</span>
    </div>
    <div class="stat-pill">
      <span class="label">可用</span>
      <span class="value" id="statAvailable">$200</span>
    </div>
    <div class="stat-pill">
      <span class="label">今日盈亏</span>
      <span class="value" id="statPnl">$0</span>
    </div>
    <div class="stat-pill">
      <span class="label">持仓数</span>
      <span class="value" id="statPositions">0/10</span>
    </div>
    <button class="btn-scan" id="btnScan" onclick="triggerScan()">
      <span id="scanBtnText">扫描市场</span>
    </button>
  </div>
</div>

<div class="container">
  <!-- Sidebar -->
  <div class="sidebar">
    <div class="sidebar-section">
      <div class="sidebar-title">导航</div>
      <div class="nav-item active" data-tab="opportunities" onclick="switchTab('opportunities')">
        🎯 交易机会 <span class="count" id="navOppCount">0</span>
      </div>
      <div class="nav-item" data-tab="markets" onclick="switchTab('markets')">
        📊 市场浏览 <span class="count" id="navMarketCount">0</span>
      </div>
      <div class="nav-item" data-tab="positions" onclick="switchTab('positions')">
        💼 我的持仓 <span class="count" id="navPosCount">0</span>
      </div>
      <div class="nav-item" data-tab="logs" onclick="switchTab('logs')">
        📋 运行日志
      </div>
    </div>
    <div class="sidebar-section">
      <div class="sidebar-title">策略说明</div>
      <div style="font-size: 13px; line-height: 1.8; color: var(--text-muted);">
        <div><span class="strategy-badge ExpiryYield">临期</span> 买NO吃差价，年化30-50%</div>
        <div style="margin-top:8px"><span class="strategy-badge Arbitrage">套利</span> YES+NO总价&lt;0.97，无风险</div>
        <div style="margin-top:8px"><span class="strategy-badge TweetArb">推文</span> 马斯克推文桶跨桶套利</div>
      </div>
    </div>
    <div class="sidebar-section">
      <div class="sidebar-title">扫描状态</div>
      <div id="scanStatus" style="font-size: 13px; color: var(--text-muted);">
        等待首次扫描...
      </div>
    </div>
  </div>

  <!-- Main -->
  <div class="main">

    <!-- Tab: Opportunities -->
    <div class="tab-content active" id="tab-opportunities">
      <div class="section-header">
        <div>
          <div class="section-title">交易机会</div>
          <div class="section-subtitle">机器人自动扫描发现的高概率交易机会</div>
        </div>
      </div>
      <div class="strategy-filter">
        <div class="chip active" data-filter="all" onclick="filterStrategy('all')">全部</div>
        <div class="chip" data-filter="ExpiryYield" onclick="filterStrategy('ExpiryYield')">
          <span class="dot" style="background:#2e7d32"></span>临期理财
        </div>
        <div class="chip" data-filter="Arbitrage" onclick="filterStrategy('Arbitrage')">
          <span class="dot" style="background:#e65100"></span>套利
        </div>
        <div class="chip" data-filter="TweetArb" onclick="filterStrategy('TweetArb')">
          <span class="dot" style="background:#7b1fa2"></span>推文套利
        </div>
      </div>
      <div class="cards-grid" id="opportunitiesGrid">
        <div class="empty-state">
          <div class="icon">🔍</div>
          <div class="title">暂无机会数据</div>
          <div class="desc">点击右上角"扫描市场"开始扫描</div>
        </div>
      </div>
    </div>

    <!-- Tab: Markets -->
    <div class="tab-content" id="tab-markets">
      <div class="section-header">
        <div>
          <div class="section-title">市场浏览</div>
          <div class="section-subtitle">Polymarket 活跃市场（按交易量排序）</div>
        </div>
      </div>
      <input class="search-bar" id="marketSearch" placeholder="搜索市场关键词..." oninput="renderMarkets()">
      <div class="strategy-filter">
        <div class="chip active" data-mfilter="all" onclick="filterMarket('all')">全部</div>
        <div class="chip" data-mfilter="highvol" onclick="filterMarket('highvol')">高交易量</div>
        <div class="chip" data-mfilter="expiry" onclick="filterMarket('expiry')">即将到期</div>
        <div class="chip" data-mfilter="tweet" onclick="filterMarket('tweet')">推文相关</div>
      </div>
      <div class="cards-grid" id="marketsGrid">
        <div class="empty-state">
          <div class="icon">📊</div>
          <div class="title">暂无市场数据</div>
          <div class="desc">扫描后将显示市场列表</div>
        </div>
      </div>
    </div>

    <!-- Tab: Positions -->
    <div class="tab-content" id="tab-positions">
      <div class="section-header">
        <div>
          <div class="section-title">我的持仓</div>
          <div class="section-subtitle">机器人自动管理的仓位</div>
        </div>
      </div>
      <div class="stats-grid" id="positionStats"></div>
      <table class="positions-table" id="positionsTable">
        <thead>
          <tr>
            <th>市场</th>
            <th>策略</th>
            <th>方向</th>
            <th>入场价</th>
            <th>数量</th>
            <th>成本</th>
            <th>状态</th>
            <th>盈亏</th>
            <th>开仓时间</th>
          </tr>
        </thead>
        <tbody id="positionsBody">
          <tr><td colspan="9" style="text-align:center; padding:40px; color:var(--text-muted);">暂无持仓</td></tr>
        </tbody>
      </table>
    </div>

    <!-- Tab: Logs -->
    <div class="tab-content" id="tab-logs">
      <div class="section-header">
        <div>
          <div class="section-title">运行日志</div>
          <div class="section-subtitle">机器人实时操作记录</div>
        </div>
      </div>
      <div class="log-container" id="logContainer">
        <div class="log-line" style="color:#555;">等待日志...</div>
      </div>
    </div>

  </div>
</div>

<script>
// ============================================================
//  State
// ============================================================
let dashboardData = null;
let currentStrategyFilter = 'all';
let currentMarketFilter = 'all';
let autoRefreshTimer = null;

// ============================================================
//  API calls
// ============================================================
async function fetchDashboard() {
  try {
    const res = await fetch('/api/dashboard');
    dashboardData = await res.json();
    renderDashboard();
  } catch (e) {
    console.error('Fetch error:', e);
  }
}

async function fetchMarkets() {
  try {
    const filter = currentMarketFilter !== 'all' ? '&strategy=' + currentMarketFilter : '';
    const res = await fetch('/api/markets?limit=60' + filter + '&q=' + document.getElementById('marketSearch').value);
    const markets = await res.json();
    renderMarketCards(markets);
  } catch (e) {
    console.error('Markets fetch error:', e);
  }
}

async function triggerScan() {
  try {
    document.getElementById('scanBtnText').innerHTML = '<span class="spinner"></span> 扫描中...';
    document.getElementById('btnScan').disabled = true;
    await fetch('/api/scan', { method: 'POST' });
    // Poll for completion
    setTimeout(() => {
      fetchDashboard();
      fetchMarkets();
      document.getElementById('scanBtnText').textContent = '扫描市场';
      document.getElementById('btnScan').disabled = false;
    }, 8000);
  } catch (e) {
    console.error('Scan error:', e);
    document.getElementById('scanBtnText').textContent = '扫描市场';
    document.getElementById('btnScan').disabled = false;
  }
}

// ============================================================
//  Rendering
// ============================================================
function renderDashboard() {
  if (!dashboardData) return;
  const s = dashboardData.status;

  // Header
  const modeEl = document.getElementById('modeBadge');
  if (s.scanning) {
    modeEl.className = 'mode-badge scanning';
    modeEl.textContent = '扫描中...';
  } else if (s.mode === 'live') {
    modeEl.className = 'mode-badge live';
    modeEl.textContent = 'LIVE';
  } else {
    modeEl.className = 'mode-badge dry_run';
    modeEl.textContent = 'DRY RUN';
  }

  document.getElementById('statBankroll').textContent = '$' + s.bankroll.toFixed(0);
  document.getElementById('statExposure').textContent = '$' + s.total_exposure.toFixed(2);
  document.getElementById('statAvailable').textContent = '$' + s.available_cash.toFixed(2);

  const pnlEl = document.getElementById('statPnl');
  pnlEl.textContent = (s.daily_pnl >= 0 ? '+$' : '-$') + Math.abs(s.daily_pnl).toFixed(2);
  pnlEl.className = 'value ' + (s.daily_pnl > 0 ? 'positive' : s.daily_pnl < 0 ? 'negative' : '');

  document.getElementById('statPositions').textContent = s.open_positions + '/' + s.max_positions;

  // Nav counts
  document.getElementById('navOppCount').textContent = dashboardData.opportunities.length;
  document.getElementById('navMarketCount').textContent = dashboardData.markets_count;
  document.getElementById('navPosCount').textContent = s.open_positions;

  // Scan status
  const scanEl = document.getElementById('scanStatus');
  if (s.last_scan) {
    const dt = new Date(s.last_scan);
    scanEl.innerHTML = '上次扫描: ' + dt.toLocaleTimeString('zh-CN') + '<br>市场数: ' + dashboardData.markets_count;
  }
  if (s.scanning) {
    scanEl.innerHTML = '<span class="spinner"></span> 正在扫描...';
  }

  // Render opportunities
  renderOpportunities();

  // Render positions
  renderPositions();

  // Render logs
  renderLogs();
}

function renderOpportunities() {
  const grid = document.getElementById('opportunitiesGrid');
  let opps = dashboardData.opportunities;

  if (currentStrategyFilter !== 'all') {
    opps = opps.filter(o => o.strategy === currentStrategyFilter);
  }

  if (opps.length === 0) {
    grid.innerHTML = '<div class="empty-state"><div class="icon">🔍</div><div class="title">暂无机会</div><div class="desc">当前筛选条件下没有交易机会</div></div>';
    return;
  }

  grid.innerHTML = opps.map(o => {
    const price = o.price;
    const yesPct = Math.round(price * 100);
    const noPct = 100 - yesPct;

    let probBar = '';
    if (o.strategy === 'ExpiryYield') {
      const isYes = o.side === 'YES';
      probBar = `<div class="prob-bar-container">
        <div class="prob-bar ${isYes ? 'yes' : 'no'}" style="flex:${isYes ? yesPct : noPct}">${isYes ? yesPct + '% YES' : noPct + '% NO'}</div>
        <div class="prob-bar ${isYes ? 'no' : 'yes'}" style="flex:${isYes ? noPct : yesPct}">${isYes ? noPct + '% NO' : yesPct + '% YES'}</div>
      </div>`;
    } else if (o.strategy === 'Arbitrage') {
      const yPct = Math.round(o.yes_price * 100);
      const nPct = Math.round(o.no_price * 100);
      probBar = `<div class="prob-bar-container">
        <div class="prob-bar yes" style="flex:${yPct}">YES ${yPct}%</div>
        <div class="prob-bar no" style="flex:${nPct}">NO ${nPct}%</div>
      </div>
      <div style="font-size:12px;color:var(--orange);font-weight:600;">组合价: $${o.price.toFixed(4)} (套利空间: ${(o.profit_pct*100).toFixed(2)}%)</div>`;
    } else if (o.strategy === 'TweetArb') {
      probBar = `<div class="prob-bar-container">
        <div class="prob-bar multi" style="flex:100">${o.bucket_count || 0} 个推文桶 | 总价 $${o.price.toFixed(4)}</div>
      </div>
      <div style="font-size:12px;color:var(--purple);font-weight:600;">跨桶套利: 买全桶 ${(o.profit_pct*100).toFixed(2)}% 利润</div>`;
    }

    const annualized = o.annualized_yield > 0 ?
      `<div class="card-stat"><span class="stat-label">年化收益</span><span class="stat-value highlight">${(o.annualized_yield*100).toFixed(0)}%</span></div>` : '';
    const days = o.days_to_expiry != null ?
      `<div class="card-stat"><span class="stat-label">到期天数</span><span class="stat-value">${o.days_to_expiry}天</span></div>` : '';
    const profit = o.profit_pct != null ?
      `<div class="card-stat"><span class="stat-label">预期利润</span><span class="stat-value positive">+${(o.profit_pct*100).toFixed(2)}%</span></div>` : '';

    let volStr = '$' + (o.volume_24h / 1000).toFixed(1) + 'K';
    if (o.volume_24h > 1000000) volStr = '$' + (o.volume_24h / 1000000).toFixed(2) + 'M';

    const endDate = o.end_date ? new Date(o.end_date).toLocaleDateString('zh-CN') : '-';

    return `<div class="market-card">
      <div class="card-header">
        <div class="card-question">${escapeHtml(o.question)}</div>
        <div class="card-image-placeholder">📊</div>
      </div>
      <span class="strategy-badge ${o.strategy}">${strategyLabel(o.strategy)}</span>
      ${probBar}
      <div class="card-stats">
        <div class="card-stat"><span class="stat-label">方向</span><span class="stat-value">${o.side}</span></div>
        <div class="card-stat"><span class="stat-label">价格</span><span class="stat-value">$${price.toFixed(4)}</span></div>
        ${profit}
        ${annualized}
        ${days}
        <div class="card-stat"><span class="stat-label">24h量</span><span class="stat-value">${volStr}</span></div>
      </div>
      <div class="card-footer">
        <span class="card-meta">到期: ${endDate}</span>
        <a class="btn-trade" href="https://polymarket.com/event/${o.market_id}" target="_blank">前往交易 →</a>
      </div>
    </div>`;
  }).join('');
}

function renderMarketCards(markets) {
  const grid = document.getElementById('marketsGrid');
  if (!markets || markets.length === 0) {
    grid.innerHTML = '<div class="empty-state"><div class="icon">📊</div><div class="title">暂无市场</div><div class="desc">扫描后将显示市场列表</div></div>';
    return;
  }

  grid.innerHTML = markets.map(m => {
    const yesPrice = m.yes_price || (m.prices && m.prices[0]) || 0;
    const noPrice = m.no_price || (m.prices && m.prices.length > 1 ? m.prices[1] : 0) || 0;
    const yesPct = Math.round(yesPrice * 100);
    const noPct = 100 - yesPct;

    let volStr = '$' + (m.volume_24h / 1000).toFixed(1) + 'K';
    if (m.volume_24h > 1000000) volStr = '$' + (m.volume_24h / 1000000).toFixed(2) + 'M';

    const endDate = m.end_date ? new Date(m.end_date).toLocaleDateString('zh-CN') : '-';
    const daysLeft = m.days_to_expiry != null ? `(${m.days_to_expiry}天)` : '';

    return `<div class="market-card">
      <div class="card-header">
        <div class="card-question">${escapeHtml(m.question)}</div>
        <div class="card-image-placeholder">📈</div>
      </div>
      <div class="prob-bar-container">
        <div class="prob-bar yes" style="flex:${yesPct}">${yesPct}%</div>
        <div class="prob-bar no" style="flex:${noPct}">${noPct}%</div>
      </div>
      <div class="card-stats">
        <div class="card-stat"><span class="stat-label">YES</span><span class="stat-value" style="color:var(--primary)">$${yesPrice.toFixed(4)}</span></div>
        <div class="card-stat"><span class="stat-label">NO</span><span class="stat-value">$${noPrice.toFixed(4)}</span></div>
        <div class="card-stat"><span class="stat-label">24h量</span><span class="stat-value">${volStr}</span></div>
        <div class="card-stat"><span class="stat-label">流动性</span><span class="stat-value">$${(m.liquidity/1000).toFixed(1)}K</span></div>
      </div>
      <div class="card-footer">
        <span class="card-meta">到期: ${endDate} ${daysLeft}</span>
        <a class="btn-trade" href="https://polymarket.com/event/${m.slug || m.id}" target="_blank">查看 →</a>
      </div>
    </div>`;
  }).join('');
}

function renderPositions() {
  const positions = dashboardData.positions || [];
  const tbody = document.getElementById('positionsBody');

  if (positions.length === 0) {
    tbody.innerHTML = '<tr><td colspan="9" style="text-align:center; padding:40px; color:var(--text-muted);">暂无持仓记录</td></tr>';
    return;
  }

  tbody.innerHTML = positions.map(p => {
    const pnl = p.pnl_usdc || 0;
    const pnlClass = pnl > 0 ? 'positive' : pnl < 0 ? 'negative' : '';
    const pnlStr = pnl !== 0 ? (pnl > 0 ? '+$' : '-$') + Math.abs(pnl).toFixed(2) : '-';
    const openedAt = p.opened_at ? new Date(p.opened_at).toLocaleString('zh-CN', {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}) : '-';

    return `<tr>
      <td style="max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${escapeHtml(p.question)}">${escapeHtml(p.question)}</td>
      <td><span class="strategy-badge ${p.strategy}">${strategyLabel(p.strategy)}</span></td>
      <td><strong>${p.side || '-'}</strong></td>
      <td>$${(p.entry_price || 0).toFixed(4)}</td>
      <td>${(p.shares || 0).toFixed(1)}</td>
      <td>$${(p.cost_usdc || 0).toFixed(2)}</td>
      <td><span class="status-badge ${p.status || 'open'}">${p.status || 'open'}</span></td>
      <td class="${pnlClass}" style="font-weight:700;">${pnlStr}</td>
      <td style="font-size:12px;color:var(--text-muted);">${openedAt}</td>
    </tr>`;
  }).join('');
}

function renderLogs() {
  const logs = dashboardData.logs || [];
  const container = document.getElementById('logContainer');

  if (logs.length === 0) {
    container.innerHTML = '<div class="log-line" style="color:#555;">等待日志...</div>';
    return;
  }

  container.innerHTML = logs.map(line => {
    // Parse log format: 2026-08-12 23:47:11,812 [INFO] message
    const match = line.match(/^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ \[(\w+)\] (.*)$/);
    if (match) {
      const [, ts, level, msg] = match;
      return `<div class="log-line"><span class="ts">${ts}</span> [<span class="level-${level}">${level}</span>] ${escapeHtml(msg)}</div>`;
    }
    return `<div class="log-line">${escapeHtml(line)}</div>`;
  }).join('');

  // Auto-scroll to bottom
  container.scrollTop = container.scrollHeight;
}

// ============================================================
//  Helpers
// ============================================================
function escapeHtml(str) {
  if (!str) return '';
  return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function strategyLabel(s) {
  const labels = {
    'ExpiryYield': '临期理财',
    'Arbitrage': '套利',
    'TweetArb': '推文套利',
    'TweetPrediction': '推文预测',
    'Directional': '方向性',
  };
  return labels[s] || s;
}

function switchTab(tab) {
  document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(el => el.classList.remove('active'));
  document.getElementById('tab-' + tab).classList.add('active');
  document.querySelector(`.nav-item[data-tab="${tab}"]`).classList.add('active');

  if (tab === 'markets') {
    fetchMarkets();
  }
}

function filterStrategy(s) {
  currentStrategyFilter = s;
  document.querySelectorAll('.chip[data-filter]').forEach(el => el.classList.remove('active'));
  document.querySelector(`.chip[data-filter="${s}"]`).classList.add('active');
  renderOpportunities();
}

function filterMarket(s) {
  currentMarketFilter = s;
  document.querySelectorAll('.chip[data-mfilter]').forEach(el => el.classList.remove('active'));
  document.querySelector(`.chip[data-mfilter="${s}"]`).classList.add('active');
  fetchMarkets();
}

function renderMarkets() {
  fetchMarkets();
}

// ============================================================
//  Auto-refresh
// ============================================================
fetchDashboard();
// Trigger initial scan
fetch('/api/scan', { method: 'POST' });

// Auto-refresh every 30 seconds
autoRefreshTimer = setInterval(() => {
  fetchDashboard();
  if (document.getElementById('tab-markets').classList.contains('active')) {
    fetchMarkets();
  }
}, 30000);

// Auto-scan every 5 minutes
setInterval(() => {
  fetch('/api/scan', { method: 'POST' });
}, 300000);
</script>

</body>
</html>"""


# ============================================================
#  Main
# ============================================================

if __name__ == "__main__":
    print("=" * 60)
    print("  PolyMonitor - 预测市场监控仪表盘")
    print("  正在启动...")
    print("  打开浏览器访问: http://localhost:5000")
    print("  按 Ctrl+C 停止")
    print("=" * 60)

    # Trigger initial scan on startup
    run_scan_background()

    app.run(host="0.0.0.0", port=5000, debug=False)
