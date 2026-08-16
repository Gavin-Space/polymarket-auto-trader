#!/usr/bin/env python3
"""Check dashboard data."""
import requests, json

d = requests.get("http://localhost:5000/api/dashboard", timeout=10).json()
print(f"Markets: {d['markets_count']}")
print(f"Opportunities: {len(d['opportunities'])}")
print(f"Scanning: {d['status']['scanning']}")
print(f"Action: {d['status'].get('current_action', '?')}")
if d['opportunities']:
    print()
    for o in d['opportunities'][:10]:
        ev = o.get('ev', {})
        conf = o.get('confidence', 0)
        print(f"  [{o['strategy']}] Conf:{conf:.0f} EV:${ev.get('ev_usdc',0):.2f} ({ev.get('ev_pct',0):.1f}%)")
        print(f"    {o.get('side','?')} @ ${o['price']:.4f} | {o['question'][:55]}")
        a = o.get('analysis', {})
        if a:
            print(f"    Analysis: {a}")
        print()
else:
    print("No opportunities yet - scan may still be running")
    # Show last few log entries
    logs = d.get('logs', [])
    print(f"\nLast 5 log entries:")
    for l in logs[-5:]:
        print(f"  {l}")
