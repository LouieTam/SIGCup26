#!/usr/bin/env python3
"""Where did the day's volume and price dislocations happen? One read per market.

Pulls hourly candles for every tournament market since --since and ranks them by
volume, by intraday range, and by how far price traded from where it ended the
window. Written to explain a single account doing 19.6M of volume at +753K in
one day (2026-10-02): a strategy that size leaves a footprint in market volume.

Throttled because the live quoter shares the account's 100 reads/min.

    python3 analyze_today.py --since 2026-10-02T00:00:00Z --reads-per-min 15
"""

from __future__ import annotations

import argparse
import json

from sigcup.catalog import load_catalog
from sigcup.client import ApiError, SuperMarketClient, _Budget, load_env


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-10-02T00:00:00Z")
    ap.add_argument("--reads-per-min", type=int, default=15)
    ap.add_argument("--out", default="data/today_markets.json")
    args = ap.parse_args()

    load_env()
    c = SuperMarketClient()
    c._reads = _Budget(args.reads_per_min, headroom=0)
    cat = {r["marketId"]: r for r in load_catalog()}
    markets = c.tournament_markets(limit=100)

    rows = []
    for i, m in enumerate(markets):
        ex = (m.get("exchanges") or [{}])[0]
        exid = str(ex.get("id"))
        try:
            h = c._get(f"/exchanges/{exid}/price-history", tournamentId=c.tournament_id,
                       resolution="1h", limit=1000, **{"from": args.since})
        except ApiError as e:
            print(f"  m{m['id']}: {e.code}", flush=True)
            continue
        cs = h.get("candles") or []
        vol = sum(x.get("volume") or 0 for x in cs)
        notional = sum((x.get("vwap") or 0) * (x.get("volume") or 0) for x in cs)
        meta = cat.get(str(m["id"]), {})
        rows.append({
            "marketId": str(m["id"]), "exchangeId": exid, "title": m.get("title"),
            "raceId": meta.get("raceId"), "winner": meta.get("winnerName"),
            "volume": vol, "notional": notional,
            "trades": sum(x.get("tradeCount") or 0 for x in cs),
            "lo": min((x["low"] for x in cs), default=None),
            "hi": max((x["high"] for x in cs), default=None),
            "close": cs[-1]["close"] if cs else None,
            "latest": ex.get("latestPrice"),
            "hours": [{"t": x["time"][11:16], "v": x.get("volume"), "lo": x.get("low"),
                       "hi": x.get("high"), "vwap": x.get("vwap")} for x in cs],
        })
        if (i + 1) % 25 == 0:
            print(f"  {i + 1}/{len(markets)} markets read", flush=True)

    rows.sort(key=lambda r: -r["notional"])
    total = sum(r["notional"] for r in rows)
    print(f"\n{len(rows)} markets, total notional since {args.since}: {total:,.0f}\n")
    print("TOP BY NOTIONAL")
    for r in rows[:20]:
        print(f"  m{r['marketId']:<4} {str(r['title'])[:52]:<52} notional {r['notional']:>12,.0f} "
              f"({r['notional'] / total * 100:4.1f}%) trades {r['trades']:>5} "
              f"range {r['lo']}-{r['hi']} close {r['close']}")
    print("\nWIDEST INTRADAY RANGE (notional > 20K)")
    wide = [r for r in rows if r["notional"] > 20000 and r["lo"] is not None]
    for r in sorted(wide, key=lambda r: -(r["hi"] - r["lo"]))[:15]:
        print(f"  m{r['marketId']:<4} {str(r['title'])[:52]:<52} {r['lo']}-{r['hi']} "
              f"(range {r['hi'] - r['lo']:.3f}) close {r['close']} notional {r['notional']:>11,.0f}")
    with open(args.out, "w") as f:
        json.dump(rows, f, indent=1)
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
