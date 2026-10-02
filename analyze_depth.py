#!/usr/bin/env python3
"""Measure realised edge per unit of capital at each ladder depth.

Settles the "concentrate shallow vs layer deep" question with data instead of
intuition. For each market, takes fair as the current mid, then for each depth
d (a fraction of fair) asks: how much volume actually traded at or below d*fair
since the open, and what would a fixed amount of capital resting there have
earned?

Equal capital C at every depth, so the comparison is apples to apples:
    shares  = C / price
    filled  = min(shares, volume traded at or below price)
    profit  = (fair - price) * filled
    ROC     = profit / C

Shallow rungs fill often at a thin margin; deep rungs fill rarely at a huge one.
This prints which actually won over the observed window.

    python3 analyze_depth.py --sample 20 --capital 4000
"""

from __future__ import annotations

import argparse

from sigcup.catalog import load_catalog
from sigcup.client import ApiError, SuperMarketClient, load_env

DEPTHS = [0.95, 0.90, 0.85, 0.78, 0.70, 0.60, 0.48, 0.36, 0.25]


def full_tape(c, exid: str, frm: str, max_pages: int = 6) -> list[dict]:
    out, cursor = [], None
    for _ in range(max_pages):
        p = c._get(f"/exchanges/{exid}/trades", tournamentId=c.tournament_id,
                   limit=200, cursor=cursor, **{"from": frm})
        out += p.get("data") or []
        pg = p.get("pagination") or {}
        cursor = pg.get("nextCursor")
        if not pg.get("hasMore") or not cursor:
            break
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=20)
    ap.add_argument("--capital", type=float, default=4000.0,
                    help="capital resting at each depth, per market")
    ap.add_argument("--min-fair", type=float, default=0.90)
    ap.add_argument("--since", default="2026-10-01T16:00:00Z")
    args = ap.parse_args()

    load_env()
    c = SuperMarketClient()
    cat = [r for r in load_catalog() if r.get("exchangeId") and r.get("raceId")]

    # Pick the high-fair legs we would actually quote.
    ids = [r["exchangeId"] for r in cat]
    px = {}
    for i in range(0, len(ids), 100):
        for row in c.prices(ids[i:i + 100]).get("data") or []:
            px[row["exchangeId"]] = row
    cands = []
    for r in cat:
        p = px.get(r["exchangeId"]) or {}
        if p.get("bestBid") is None or p.get("bestAsk") is None:
            continue
        fair = (p["bestBid"] + p["bestAsk"]) / 2
        if fair >= args.min_fair:
            cands.append((r, fair))
    cands.sort(key=lambda t: -t[1])
    cands = cands[: args.sample]
    print(f"{len(cands)} markets sampled (fair >= {args.min_fair}), "
          f"{args.capital:,.0f} capital per depth per market\n")

    agg = {d: {"profit": 0.0, "filled": 0, "capital": 0.0, "markets_hit": 0} for d in DEPTHS}
    for r, fair in cands:
        try:
            tape = full_tape(c, r["exchangeId"], args.since)
        except ApiError as e:
            print(f"  {r['marketId']}: {e.code}")
            continue
        prices = [(t["price"], t["size"]) for t in tape if t.get("price") is not None]
        for d in DEPTHS:
            p = round(fair * d, 3)
            if p <= 0:
                continue
            vol = sum(sz for pr, sz in prices if pr <= p)
            shares = args.capital / p
            filled = min(shares, vol)
            profit = (fair - p) * filled
            a = agg[d]
            a["profit"] += profit
            a["filled"] += filled
            a["capital"] += args.capital
            if filled > 0:
                a["markets_hit"] += 1

    print(f"{'depth':<8} {'as % fair':<10} {'markets filled':<15} {'shares filled':<15} "
          f"{'profit':<12} {'ROC on capital'}")
    for d in DEPTHS:
        a = agg[d]
        roc = a["profit"] / a["capital"] * 100 if a["capital"] else 0
        print(f"{d:<8.2f} {d*100:>6.0f}%    {a['markets_hit']:>6}/{len(cands):<8} "
              f"{a['filled']:>14,.0f} {a['profit']:>11,.0f} {roc:>14.2f}%")

    best = max(DEPTHS, key=lambda d: agg[d]["profit"] / max(agg[d]["capital"], 1))
    print(f"\nBest ROC at depth {best:.2f} ({best*100:.0f}% of fair).")
    tot = sum(agg[d]["profit"] for d in DEPTHS)
    cap = sum(agg[d]["capital"] for d in DEPTHS)
    print(f"Laddering all depths equally: {tot:,.0f} profit on {cap:,.0f} "
          f"= {tot/cap*100:.2f}% ROC")
    print(f"Concentrating the same total capital at {best:.2f} alone: "
          f"{agg[best]['profit']/agg[best]['capital']*cap:,.0f} "
          f"(scaling linearly is optimistic — it assumes the extra size still fills)")
    print("\nCaveat: volume traded at or below a price is an UPPER bound on what we would")
    print("have filled. We would queue behind existing bids at that level, and our own")
    print("fills would have absorbed flow that then never reached deeper rungs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
