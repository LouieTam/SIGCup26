#!/usr/bin/env python3
"""Classify collected trades as impatient selling vs buying.

The trade tape carries no aggressor flag — `side` is YES/NO space, not
buy/sell — so aggression has to be inferred by comparing each print against the
book that was resting just before it:

    print <= prevailing bestBid   -> SELLER crossed (impatient sell)
    print >= prevailing bestAsk   -> BUYER crossed  (impatient buy)
    strictly between              -> inside spread / ambiguous

Hard limitation, stated up front: books are snapshotted every `--interval`
seconds, so the "prevailing" book is up to that stale. A print is only
classified when a snapshot exists within `--max-staleness` seconds before it;
everything else is counted as UNCLASSIFIED rather than guessed at. The trade
backfill from the collector's first cycle has no preceding snapshot at all and
will land there by design.

    python3 analyze_flow.py
    python3 analyze_flow.py --max-staleness 30 --min-size 100
"""

from __future__ import annotations

import argparse
import json
import os
from bisect import bisect_right
from collections import defaultdict
from datetime import datetime

DATA_DIR = "data"


def parse_ts(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def best_of(levels: list[dict], side: str) -> float | None:
    if not levels:
        return None
    return (max if side == "bid" else min)(l["price"] for l in levels)


def load_books() -> dict[str, tuple[list[float], list[dict]]]:
    """exchangeId -> (sorted snapshot times, snapshots). Snapshot time is the
    collector's read time, which is when the book was observed."""
    raw: dict[str, list[dict]] = defaultdict(list)
    path = os.path.join(DATA_DIR, "books.jsonl")
    with open(path) as f:
        for line in f:
            if not line.strip():
                continue
            r = json.loads(line)
            bids, asks = r.get("bids") or [], r.get("asks") or []
            raw[str(r["exchangeId"])].append(
                {
                    "t": parse_ts(r["ts"]),
                    "bestBid": best_of(bids, "bid"),
                    "bestAsk": best_of(asks, "ask"),
                    "bids": sorted(bids, key=lambda l: -l["price"]),
                }
            )
    out = {}
    for exid, snaps in raw.items():
        snaps.sort(key=lambda s: s["t"])
        out[exid] = ([s["t"] for s in snaps], snaps)
    return out


def load_trades() -> list[dict]:
    seen: set[tuple[str, str]] = set()
    rows = []
    with open(os.path.join(DATA_DIR, "trades.jsonl")) as f:
        for line in f:
            if not line.strip():
                continue
            r = json.loads(line)
            t = r["trade"]
            key = (str(r["exchangeId"]), str(t["id"]))
            if key in seen:  # a restarted collector can re-emit a backfill
                continue
            seen.add(key)
            rows.append(
                {
                    "exchangeId": str(r["exchangeId"]),
                    "marketId": str(r["marketId"]),
                    "t": parse_ts(t["createdAt"]),
                    "createdAt": t["createdAt"],
                    "price": t["price"],
                    "size": t["size"],
                    "side": t["side"],
                }
            )
    rows.sort(key=lambda r: r["t"])
    return rows


def prior_snapshot(books, exid: str, t: float, max_stale: float):
    entry = books.get(exid)
    if not entry:
        return None
    times, snaps = entry
    i = bisect_right(times, t) - 1
    if i < 0:
        return None
    snap = snaps[i]
    if t - snap["t"] > max_stale:
        return None
    return snap


def depth_consumed(snap: dict, price: float) -> int:
    """Shares resting at or above `price` on the bid side — how far a seller
    had to walk the book to print here."""
    return sum(l["quantity"] for l in snap["bids"] if l["price"] >= price)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-staleness", type=float, default=30.0,
                    help="max seconds a book snapshot may predate a print (default 30)")
    ap.add_argument("--min-size", type=int, default=0, help="ignore prints smaller than this")
    ap.add_argument("--catalog", action="store_true", default=True)
    args = ap.parse_args()

    try:
        books = load_books()
        trades = load_trades()
    except FileNotFoundError as e:
        print(f"missing {e.filename} — run collect.py first")
        return 1

    labels = {}
    try:
        from sigcup.catalog import load_catalog
        for r in load_catalog():
            if r.get("exchangeId"):
                labels[r["exchangeId"]] = f"{r.get('winnerName')} / {r.get('raceName')}"
    except Exception:
        pass

    print(f"{len(trades)} unique prints, {sum(len(v[1]) for v in books.values())} book snapshots "
          f"across {len(books)} exchanges\n")

    stats: dict[str, dict] = defaultdict(
        lambda: {"sell": 0, "buy": 0, "inside": 0, "unclassified": 0,
                 "sell_sh": 0, "buy_sh": 0, "sell_notional": 0.0, "sweeps": []}
    )

    for tr in trades:
        if tr["size"] < args.min_size:
            continue
        s = stats[tr["exchangeId"]]
        snap = prior_snapshot(books, tr["exchangeId"], tr["t"], args.max_staleness)
        if snap is None or tr["price"] is None:
            s["unclassified"] += 1
            continue
        bb, ba = snap["bestBid"], snap["bestAsk"]
        px = tr["price"]
        if bb is not None and px <= bb:
            s["sell"] += 1
            s["sell_sh"] += tr["size"]
            s["sell_notional"] += px * tr["size"]
            through = depth_consumed(snap, px)
            # A print below the resting best bid means the seller ate through it.
            if px < bb:
                s["sweeps"].append(
                    {"createdAt": tr["createdAt"], "price": px, "size": tr["size"],
                     "prevBid": bb, "drop": bb - px, "depthAtOrAbove": through}
                )
        elif ba is not None and px >= ba:
            s["buy"] += 1
            s["buy_sh"] += tr["size"]
        else:
            s["inside"] += 1

    order = sorted(stats.items(), key=lambda kv: -(kv[1]["sell"] + kv[1]["buy"]))
    for exid, s in order:
        total = s["sell"] + s["buy"] + s["inside"]
        print(f"exchange {exid}  {labels.get(exid, '')}")
        print(f"  classified {total}, unclassified {s['unclassified']} "
              f"(no book within {args.max_staleness:.0f}s — includes the first-cycle backfill)")
        if total:
            print(f"  seller-crossed {s['sell']:>4} prints / {s['sell_sh']:>8,} sh "
                  f"/ {s['sell_notional']:>10,.1f} notional")
            print(f"  buyer-crossed  {s['buy']:>4} prints / {s['buy_sh']:>8,} sh")
            print(f"  inside spread  {s['inside']:>4}")
            aggr = s["sell"] / total * 100
            print(f"  seller aggression: {aggr:.0f}% of classified prints")
        if s["sweeps"]:
            worst = sorted(s["sweeps"], key=lambda x: -x["drop"])[:5]
            print(f"  {len(s['sweeps'])} prints BELOW the resting best bid — top by drop:")
            for w in worst:
                print(f"    {w['createdAt']} {w['size']:>7,} sh @ {w['price']:<6} "
                      f"(bid was {w['prevBid']}, drop {w['drop']:.3f}, "
                      f"{w['depthAtOrAbove']:,} sh had been resting at/above)")
        print()

    tot_sell = sum(s["sell"] for s in stats.values())
    tot_buy = sum(s["buy"] for s in stats.values())
    tot_unc = sum(s["unclassified"] for s in stats.values())
    print(f"TOTAL seller-crossed {tot_sell}, buyer-crossed {tot_buy}, unclassified {tot_unc}")
    if tot_sell + tot_buy:
        print(f"Overall seller aggression: {tot_sell / (tot_sell + tot_buy) * 100:.0f}%")
    print("\nCaveat: classification is only as good as snapshot cadence. Prints that "
          "landed between snapshots are attributed to the last book seen, so a fast "
          "quote update inside the gap can mislabel a print. Treat the sweep list as "
          "the reliable signal and the percentages as indicative.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
