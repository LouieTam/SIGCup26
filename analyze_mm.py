#!/usr/bin/env python3
"""Measure the market-making opportunity, market by market.

The key quantity is not the spread in absolute terms but the spread RELATIVE to
price. A 0.03 spread on a 0.10 contract is a 30% round-trip margin; the same
0.03 on a 0.95 contract is 3%. Cheap, thin books therefore pay far more per unit
of capital turned over — which is the likely explanation for a leaderboard P&L
of 0.16x volume, far above anything spread capture on liquid 0.95 markets
could produce.

For each market this reports:
  spread, spread as a fraction of mid, traded volume, and the "spread pool" —
  spread x shares traded, i.e. the total value available to whoever was the
  passive counterparty to every trade. That is an upper bound on capture, not a
  forecast: in reality you win some fraction of it and pay adverse selection on
  the rest.

    python3 analyze_mm.py --sample 60
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor

from sigcup.catalog import load_catalog
from sigcup.client import ApiError, SuperMarketClient, load_env


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=60)
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()

    load_env()
    c = SuperMarketClient()
    cat = [r for r in load_catalog() if r.get("exchangeId")]

    ids = [r["exchangeId"] for r in cat]
    px = {}
    for i in range(0, len(ids), 100):
        for row in c.prices(ids[i:i + 100]).get("data") or []:
            px[row["exchangeId"]] = row

    sample = sorted(cat, key=lambda r: int(r["marketId"]))[: args.sample]

    def vol_of(r):
        try:
            ph = c._get(f"/exchanges/{r['exchangeId']}/price-history",
                        tournamentId=c.tournament_id, limit=100)
        except ApiError:
            return None
        shares = sum(k["volume"] for k in ph.get("candles") or [])
        return shares

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        vols = list(pool.map(vol_of, sample))

    rows = []
    for r, shares in zip(sample, vols):
        p = px.get(r["exchangeId"]) or {}
        bb, ba = p.get("bestBid"), p.get("bestAsk")
        if bb is None or ba is None or shares is None or shares <= 0:
            continue
        mid = (bb + ba) / 2
        spread = ba - bb
        if mid <= 0:
            continue
        rows.append({
            "r": r, "mid": mid, "spread": spread, "rel": spread / mid,
            "shares": shares, "pool": spread * shares,
        })

    rows.sort(key=lambda x: -x["pool"])
    print(f"{len(rows)} markets measured, ranked by spread pool "
          f"(spread x shares traded since open)\n")
    print(f"{'mid':>7} {'spread':>7} {'rel':>7} {'shares':>12} {'pool':>11}  market")
    for x in rows[:18]:
        r = x["r"]
        print(f"{x['mid']:>7.3f} {x['spread']:>7.3f} {x['rel']*100:>6.0f}% "
              f"{x['shares']:>12,.0f} {x['pool']:>11,.0f}  "
              f"m{r['marketId']} {str(r.get('winnerName'))[:10]:<10} "
              f"{str(r.get('raceName'))[:24]}")

    tot_pool = sum(x["pool"] for x in rows)
    print(f"\nspread pool over the sample: {tot_pool:,.0f} SUSQies")
    print(f"extrapolated to all {len(cat)} exchanges: "
          f"~{tot_pool/len(rows)*len(cat):,.0f}")

    print("\n--- by price bucket: where is the relative spread widest? ---")
    buckets = [(0.0, 0.10), (0.10, 0.25), (0.25, 0.50), (0.50, 0.75), (0.75, 0.90), (0.90, 1.0)]
    print(f"{'price band':<14} {'markets':>8} {'median rel spread':>19} {'shares':>14} {'pool':>12}")
    for lo, hi in buckets:
        sub = [x for x in rows if lo <= x["mid"] < hi]
        if not sub:
            continue
        rel = sorted(x["rel"] for x in sub)[len(sub) // 2]
        print(f"{lo:.2f}-{hi:.2f}      {len(sub):>8} {rel*100:>18.0f}% "
              f"{sum(x['shares'] for x in sub):>14,.0f} {sum(x['pool'] for x in sub):>12,.0f}")

    print("\nRound-trip margin is `rel` — buy the bid, sell the ask, keep the spread.")
    print("A 30% relative spread turns capital over at 30% per round trip; the same")
    print("absolute spread on a 0.95 contract returns ~2%. That asymmetry, not volume,")
    print("is what makes a 0.16x P&L-to-volume ratio achievable.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
