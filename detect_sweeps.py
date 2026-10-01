#!/usr/bin/env python3
"""Find book sweeps in trade history and value what resting bids would have won.

A sweep is one aggressive order consuming several resting price levels. The
engine stamps every fill of one execution with the *same* `createdAt`, so a
sweep shows up as a group of prints sharing a timestamp and spanning multiple
prices. That signature is what makes this detectable from the tape alone, with
no book snapshot needed -- so it works retroactively over full history.

Fair value is taken as the median print price in a window after the sweep
(the market's own recovery level), which is why only mean-reverting sweeps
score: if the price never comes back, the move was information and the "gift"
collapses toward zero.

    python3 detect_sweeps.py --market 377
    python3 detect_sweeps.py --sample 25 --min-levels 5 --min-shares 5000
"""

from __future__ import annotations

import argparse
import statistics
from collections import defaultdict
from datetime import datetime

from sigcup.catalog import load_catalog
from sigcup.client import ApiError, SuperMarketClient, load_env

OPEN_UTC = "2026-10-01T16:00:00Z"


def parse(ts: str) -> float:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()


def full_tape(c, exid: str, frm: str, max_pages: int = 8) -> list[dict]:
    out: list[dict] = []
    cursor = None
    for _ in range(max_pages):
        p = c._get(f"/exchanges/{exid}/trades", tournamentId=c.tournament_id,
                   limit=200, cursor=cursor, **{"from": frm})
        out += p.get("data") or []
        pg = p.get("pagination") or {}
        cursor = pg.get("nextCursor")
        if not pg.get("hasMore") or not cursor:
            break
    out.sort(key=lambda t: t["createdAt"])
    return out


def find_sweeps(tape: list[dict], min_levels: int, min_shares: int,
                recovery_window_s: float = 600.0) -> list[dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for t in tape:
        if t.get("price") is not None:
            groups[t["createdAt"]].append(t)

    times = [parse(t["createdAt"]) for t in tape if t.get("price") is not None]
    prices = [t["price"] for t in tape if t.get("price") is not None]

    sweeps = []
    for ts, rows in groups.items():
        px = [r["price"] for r in rows]
        levels = len(set(px))
        shares = sum(r["size"] for r in rows)
        if levels < min_levels or shares < min_shares:
            continue
        t0 = parse(ts)
        after = [p for tt, p in zip(times, prices) if 0 < tt - t0 <= recovery_window_s]
        if not after:
            continue
        recovery = statistics.median(after)
        notional = sum(r["price"] * r["size"] for r in rows)
        vwap = notional / shares
        direction = "DOWN" if vwap < recovery else "UP"
        # Value transferred to the passive side, marked at the recovery level.
        gift = abs(recovery - vwap) * shares
        sweeps.append({
            "ts": ts, "levels": levels, "shares": shares, "vwap": vwap,
            "lo": min(px), "hi": max(px), "recovery": recovery,
            "direction": direction, "gift": gift,
            "reverted": abs(recovery - vwap) > 0.02,
        })
    sweeps.sort(key=lambda s: -s["gift"])
    return sweeps


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", action="append", default=[], help="marketId (repeatable)")
    ap.add_argument("--sample", type=int, help="scan this many markets by id order")
    ap.add_argument("--state", help="scan one state")
    ap.add_argument("--min-levels", type=int, default=4)
    ap.add_argument("--min-shares", type=int, default=2000)
    ap.add_argument("--since", default=OPEN_UTC)
    args = ap.parse_args()

    load_env()
    c = SuperMarketClient()
    cat = [r for r in load_catalog() if r.get("exchangeId")]
    if args.market:
        rows = [r for r in cat if r["marketId"] in set(args.market)]
    elif args.state:
        rows = [r for r in cat if (r.get("usState") or "").upper() == args.state.upper()]
    else:
        rows = sorted(cat, key=lambda r: int(r["marketId"]))[: args.sample or 10]

    print(f"Scanning {len(rows)} markets since {args.since} "
          f"(sweep = same-timestamp fills across >= {args.min_levels} levels, "
          f">= {args.min_shares:,} shares)\n")

    grand = 0.0
    n_sweeps = 0
    per_market = []
    for r in rows:
        try:
            tape = full_tape(c, r["exchangeId"], args.since)
        except ApiError as e:
            print(f"  m{r['marketId']}: {e.code}")
            continue
        sweeps = find_sweeps(tape, args.min_levels, args.min_shares)
        rev = [s for s in sweeps if s["reverted"]]
        tot = sum(s["gift"] for s in rev)
        grand += tot
        n_sweeps += len(rev)
        per_market.append((tot, len(rev), len(tape), r))
        if rev:
            print(f"m{r['marketId']:<5} {str(r.get('winnerName'))[:12]:<12} "
                  f"{str(r.get('raceName'))[:30]:<30} {len(tape):>5} prints, "
                  f"{len(rev):>2} reverting sweeps, {tot:>10,.0f} SUSQies to passive side")
            for s in rev[:3]:
                print(f"      {s['ts']} {s['direction']:<4} {s['shares']:>8,} sh "
                      f"{s['lo']}->{s['hi']} VWAP {s['vwap']:.3f} "
                      f"recovered {s['recovery']:.3f}  gift {s['gift']:>9,.0f}")

    print(f"\n{'=' * 72}")
    print(f"{n_sweeps} reverting sweeps across {len(rows)} markets")
    print(f"~{grand:,.0f} SUSQies transferred to the passive (resting-bid) side")
    if rows:
        print(f"average {grand / len(rows):,.0f} per market scanned, over ~5 hours of trading")
    print("\nThis is the money a ladder quoter competes for. It is only capturable")
    print("passively: every sweep above executed as ONE atomic order, so there is no")
    print("window in which a reacting bot could buy the dip.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
