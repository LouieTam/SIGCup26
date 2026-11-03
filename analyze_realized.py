#!/usr/bin/env python3
"""Realised P&L by source: direct sells vs YES/NO conversions, YES vs NO books.

Replays /tournaments/{slug}/portfolio/transactions oldest-first with average
cost per exchange. Conventions (verified 2026-10-03): `orderType` is the
action, `quantity` is signed by side (negative = NO), and `price` is in that
side's terms.

  BUY,  same side or flat  -> opens / adds (average cost updated)
  BUY,  opposite side held -> CONVERSION: min(|q|, held) YES+NO pairs paid 1;
                              realised = (1 - buy price - avg cost) per pair,
                              and any remainder opens the other side
  SELL                     -> direct sale; realised = (price - avg) per share
"""
from __future__ import annotations
import collections, json
from sigcup.client import SuperMarketClient, _Budget, load_env

load_env(); c = SuperMarketClient(); c._reads = _Budget(20, headroom=0)
cat = {r["exchangeId"]: r for r in json.load(open("data/catalog.json"))}
rows = list(c._paged(f"/tournaments/{c.tournament_slug}/portfolio/transactions", limit=100))
trades = sorted((r for r in rows if r.get("event_type") == "trade"), key=lambda r: r["createdAt"])
pos = collections.defaultdict(lambda: [0, 0.0])        # exid -> [signed qty, avg cost in held side's terms]
real = collections.Counter(); n = collections.Counter(); per_ex = collections.Counter()
sells_below = []
for t in trades:
    x = str(t["exchangeId"]); q = int(t["quantity"]); px = float(t["price"]); act = t["orderType"]
    side = 1 if q > 0 else -1; aq = abs(q); held, avg = pos[x]
    book = "YES" if side > 0 else "NO"
    if act == "BUY":
        if held != 0 and (held > 0) != (side > 0):           # conversion
            pairs = min(aq, abs(held)); g = (1 - px - avg) * pairs
            hb = "YES" if held > 0 else "NO"
            real[f"conversion ({hb} held, bought {book})"] += g; n["conversion"] += 1; per_ex[x] += g
            held += side * pairs; rest = aq - pairs
            if held == 0: avg = 0.0
            if rest:
                held, avg = side * rest, px
        else:                                                # open / add
            tot = abs(held) + aq; avg = (avg * abs(held) + px * aq) / tot; held = side * tot
    else:                                                    # SELL of the side held
        g = (px - avg) * aq; real[f"direct sell ({book})"] += g; n[f"sell {book}"] += 1; per_ex[x] += g
        if px < avg - 1e-9: sells_below.append((t["createdAt"][:19], x, book, aq, px, round(avg, 4), round(g, 2)))
        held -= side * aq
        if held == 0: avg = 0.0
    pos[x] = [held, avg]
tot = sum(real.values())
print(f"{len(trades)} trades replayed ({trades[0]['createdAt'][:16]} -> {trades[-1]['createdAt'][:16]})")
print(f"\nREALISED P&L by source (average cost):  total {tot:+,.0f}")
for k, v in real.most_common(): print(f"  {k:<36} {v:>+10,.0f}  ({v / tot * 100:5.1f}%)")
print("  counts:", dict(n))
print(f"\nSELLS BELOW AVERAGE COST: {len(sells_below)}")
for s in sells_below[:15]: print("  ", s)
print("\nTop exchanges by realised:")
for x, v in per_ex.most_common(8): print(f"  ex {x:<5} {cat.get(x, {}).get('title', '')[:50]:<50} {v:>+9,.0f}")
print("Worst:")
for x, v in sorted(per_ex.items(), key=lambda kv: kv[1])[:5]: print(f"  ex {x:<5} {cat.get(x, {}).get('title', '')[:50]:<50} {v:>+9,.0f}")
