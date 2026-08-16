#!/usr/bin/env python3
"""Test the enhanced strategy engine with live market data."""
import sys, os, json
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "libs"))
sys.path.insert(0, os.path.dirname(__file__))

print("=== Enhanced Strategy Engine Live Test ===")

from strategy_engine import EnhancedScanner
from auto_trader import GammaAPI

now = datetime.now(timezone.utc)
print("Fetching markets...")
all_raw = GammaAPI.get_all_active_markets(max_markets=200)
ending_raw = GammaAPI.get_ending_soon(limit=100)
print(f"  Fetched {len(all_raw)} active + {len(ending_raw)} ending soon")

scanner = EnhancedScanner()
seen_ids = set()
markets = []
for raw in all_raw:
    m = scanner.parse_market(raw)
    if m and m["id"] not in seen_ids:
        markets.append(m)
        seen_ids.add(m["id"])
for raw in ending_raw:
    m = scanner.parse_market(raw)
    if m and m["id"] not in seen_ids:
        markets.append(m)
        seen_ids.add(m["id"])

print(f"  Total unique markets: {len(markets)}")
print("Running enhanced scan (may take a minute for order book API calls)...")
opps = scanner.scan(markets, now)

print(f"\n=== Found {len(opps)} high-confidence positive-EV opportunities ===\n")
for i, o in enumerate(opps[:15]):
    ev_data = o.get("ev", {})
    conf = o.get("confidence", 0)
    ev_usdc = ev_data.get("ev_usdc", 0)
    ev_pct = ev_data.get("ev_pct", 0)
    ann = o.get("annualized_yield", 0)
    ann_str = f" | Ann:{ann:.0f}%" if ann > 0 else ""
    print(f"  [{i+1}] [{o['strategy']}] {o.get('side','?')} @ ${o['price']:.4f}")
    print(f"      Conf:{conf:.0f} | EV:${ev_usdc:.2f} ({ev_pct:.1f}%){ann_str}")
    print(f"      {o['question'][:65]}")
    analysis = o.get("analysis", {})
    if analysis:
        details = []
        for k, v in analysis.items():
            details.append(f"{k}={v}")
        print(f"      Analysis: {', '.join(details)}")
    print()

print("=== Test Complete ===")
