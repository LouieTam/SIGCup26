#!/usr/bin/env python3
"""Dutch-book scanner: find races whose best bids sum above 1.

For n mutually exclusive legs, buying NO on every leg costs n - sum(bestBid)
and pays at least n - 1 (every leg but the winner pays 1; if nobody wins, all
pay). So locked profit per share >= sum(bestBid) - 1, independent of n.

Read-only by default. Reports executable size and, importantly, return on the
capital the trade locks up until settlement -- which is the number that decides
whether this is worth doing at all.

    python3 scan_arb.py                      # top-of-book scan, all races
    python3 scan_arb.py --deep --min-edge 0.01   # fetch books to size the best
"""

from __future__ import annotations

import argparse

from sigcup.catalog import group_by_race, load_catalog
from sigcup.client import SuperMarketClient, load_env


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-edge", type=float, default=0.0,
                    help="only report races with at least this edge per share")
    ap.add_argument("--deep", action="store_true",
                    help="fetch full books for hits to measure executable size (2 reads/race)")
    ap.add_argument("--max-deep", type=int, default=10, help="cap deep reads (default 10 races)")
    args = ap.parse_args()

    load_env()
    c = SuperMarketClient()
    cat = load_catalog()
    races = group_by_race(cat)
    ex_ids = [r["exchangeId"] for r in cat if r.get("exchangeId") and r.get("raceId")]

    # Bulk top-of-book: 100 ids per read, so the whole board costs ~3 reads.
    px: dict[str, dict] = {}
    for i in range(0, len(ex_ids), 100):
        for row in c.prices(ex_ids[i : i + 100]).get("data") or []:
            px[row["exchangeId"]] = row

    hits = []
    for rid, legs in races.items():
        bids = [px.get(l["exchangeId"], {}).get("bestBid") for l in legs]
        if any(b is None for b in bids):
            continue
        edge = sum(bids) - 1
        if edge > args.min_edge or (args.min_edge == 0 and edge > 0):
            hits.append((edge, rid, legs, bids))
    hits.sort(key=lambda h: -h[0])

    print(f"{len(races)} races scanned, {len(hits)} with bid-sum > 1 "
          f"(edge > {args.min_edge})\n")
    if not hits:
        print("No dutch book available at this instant.")
        return 0

    print(f"{'race':<8} {'legs':<5} {'edge':<9} name")
    for edge, rid, legs, bids in hits:
        print(f"{rid:<8} {len(legs):<5} {edge:>+8.4f} {legs[0].get('raceName')}")

    if not args.deep:
        print("\nTop-of-book only — sizes unknown. Re-run with --deep to size these.")
        return 0

    print(f"\n--- sizing the top {min(args.max_deep, len(hits))} ---")
    total_profit = total_capital = 0.0
    for edge, rid, legs, bids in hits[: args.max_deep]:
        sizes = []
        for l in legs:
            b = c.exchange_orderbook(l["exchangeId"], depth=5)
            top = [x for x in b["bids"] if x["price"] == b["bestBid"]]
            sizes.append(sum(x["quantity"] for x in top) if top else 0)
        q = min(sizes) if sizes else 0
        # Re-read bids from the books we just fetched would be more precise; the
        # bulk snapshot can be seconds stale, which at one tick of edge matters.
        capital = (len(legs) - sum(bids)) * q
        profit = edge * q
        roc = (profit / capital * 100) if capital else 0.0
        total_profit += profit
        total_capital += capital
        print(f"  race {rid:<8} {legs[0].get('raceName')[:34]:<34} "
              f"edge {edge:+.4f} x {q:>7,} sh = {profit:>9,.2f} SUSQies "
              f"| capital {capital:>10,.0f} | ROC {roc:.2f}%")
    print(f"\n  TOTAL available now: {total_profit:,.2f} SUSQies profit "
          f"for {total_capital:,.0f} locked until settlement "
          f"({total_profit / total_capital * 100 if total_capital else 0:.2f}% ROC)")
    print("\n  Capital is locked until 2026-11-04: buying NO on every leg only pays")
    print("  out at settlement. Unwinding earlier means selling back into the")
    print("  spread, which is no longer riskless.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
