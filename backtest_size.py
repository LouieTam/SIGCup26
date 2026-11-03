#!/usr/bin/env python3
"""What would a smaller per-race size (--race-pct) have done to realised P&L?

Every holding episode since --since is rebuilt from our fills (backtest_volstop
episodes()). The ladder caps how many shares one sweep can fill, so the largest
position we ever held per (exchange, side) is taken as that race's full-ladder
capacity Q_cap. Scaling the ladder by f caps every episode at f * Q_cap:

    scaled P&L = P&L * min(1, f * Q_cap / Q_peak)

Episodes that never came near the cap (small sweeps, partial fills) are left
unchanged; those that ate the whole ladder (the big sweeps, both the reverting
winners and the RI/NY/Illinois-style losers) shrink with f. A first-order
model: it ignores that a smaller ladder also changes which rungs fill first.

    python3 backtest_size.py --refresh        # re-read fills (writes the cache)
    python3 backtest_size.py                  # use data/volstop_cache.json
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict

from backtest_volstop import CACHE, episodes


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--since", default="2026-10-04T00:00:00Z")
    args = ap.parse_args()
    if args.refresh:
        from sigcup.client import SuperMarketClient, _Budget, load_env
        load_env()
        c = SuperMarketClient()
        c._reads = _Budget(15, headroom=0)
        trades = []
        for t in c._paged(f"/tournaments/{c.tournament_slug}/portfolio/transactions", limit=100):
            if t["createdAt"] < args.since:
                break
            if t.get("event_type") == "trade":
                trades.append(t)
        data = json.load(open(CACHE)) if args.refresh is False else {"since": args.since, "candles": {}}
        try:
            data["candles"] = json.load(open(CACHE)).get("candles", {})
        except (OSError, ValueError):
            pass
        data["trades"] = trades
        json.dump(data, open(CACHE, "w"))
    data = json.load(open(CACHE))
    eps = [e for e in episodes(data["trades"]) if e["end"]]  # closed episodes only
    for e in eps:
        e["peak"] = max(q for *_, q, _a in e["events"])
    cap = defaultdict(int)
    for e in eps:
        cap[(e["exid"], e["side"])] = max(cap[(e["exid"], e["side"])], e["peak"])
    total = sum(e["realised"] for e in eps)
    gains = sum(e["realised"] for e in eps if e["realised"] > 0)
    losses = sum(e["realised"] for e in eps if e["realised"] < 0)
    print(f"{len(eps)} closed episodes since {data['since']} | last fill "
          f"{max(t['createdAt'] for t in data['trades'])[:16]}Z")
    print(f"actual realised: {total:+,.0f}  (gains {gains:+,.0f}, losses {losses:+,.0f})\n")
    print(f"{'f':>5} {'realised':>10} {'gains':>10} {'losses':>10} {'vs actual':>10}")
    for f in (1.0, 0.8, 0.6, 0.5, 0.4, 0.3):
        g = l = 0.0
        for e in eps:
            k = min(1.0, f * cap[(e["exid"], e["side"])] / e["peak"]) if e["peak"] else 1.0
            v = e["realised"] * k
            g += max(v, 0); l += min(v, 0)
        print(f"{f:>5.1f} {g + l:>+10,.0f} {g:>+10,.0f} {l:>+10,.0f} {g + l - total:>+10,.0f}")
    print("\nlargest episodes (actual):")
    for e in sorted(eps, key=lambda e: -abs(e["realised"]))[:10]:
        print(f"  ex {e['exid']}/{e['side'].upper():<3} {e['start'][:16]} peak {e['peak']:>7,} "
              f"(cap {cap[(e['exid'], e['side'])]:>7,}) realised {e['realised']:>+8,.0f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
