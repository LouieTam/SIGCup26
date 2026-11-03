#!/usr/bin/env python3
"""Lead-lag between legs of a race, from a tape recorded by record_tape_ws.py.

Question: when one leg of a race jumps and the race's mids no longer sum to
their usual level, does the stale sibling CATCH UP (a taker who trades the
sibling profits within minutes, without holding to settlement) or does the
jump REVERT (the trade loses)?

Episode:
  * race sum S = sum of leg mids (only legs with spread <= --max-spread);
    baseline = median S over the previous 30 min (handles missing-third-leg
    races that sit near 0.96, not 1)
  * starts when |S - baseline| >= --dev; the LEADER is the leg whose mid moved
    most over the previous 60 s, and it must have moved in the direction of the
    deviation (otherwise no leg "jumped first" and the episode is skipped)
  * the LAGGER is the sibling with the largest mid
Trade (toward coherence), entered --delay s after the start at the lagger's
touch: S too high -> sell lagger YES at its bid; too low -> buy at its ask.
Exited at each horizon at the opposite touch (taker both ways, conservative)
and at mid (optimistic: assumes a passive exit fills at mid).

    python3 analyze_leadlag.py data/tape.jsonl
"""

from __future__ import annotations

import argparse
import bisect
import json
import statistics
from collections import defaultdict, deque

HORIZONS = [60, 120, 300, 600, 1800]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("tape")
    ap.add_argument("--dev", type=float, default=0.02, help="deviation of the race sum that opens an episode")
    ap.add_argument("--delay", type=float, default=15.0, help="seconds before our order lands")
    ap.add_argument("--max-spread", type=float, default=0.05)
    ap.add_argument("--cooldown", type=float, default=120.0, help="s after an episode before the race can open another")
    args = ap.parse_args()

    races: dict[str, list[str]] = {}
    names: dict[str, str] = {}
    events: list[tuple] = []
    series: dict[str, list[tuple]] = defaultdict(list)  # x -> [(t, b, a, bq, aq)]
    with open(args.tape) as f:
        for line in f:
            r = json.loads(line)
            if r.get("meta") == "races":
                for rid, legs in r["races"].items():
                    races[rid] = [l["x"] for l in legs]
                    names[rid] = legs[0].get("name") or rid
                continue
            series[r["x"]].append((r["t"], r["b"], r["a"], r.get("bq"), r.get("aq")))
            events.append((r["t"], r["x"]))
    events.sort()
    leg_race = {x: rid for rid, xs in races.items() for x in xs}
    times = {x: [s[0] for s in v] for x, v in series.items()}

    def at(x: str, t: float):
        """Top of book of leg x as of time t (latest record at or before t)."""
        i = bisect.bisect_right(times.get(x, []), t) - 1
        return series[x][i] if i >= 0 else None

    def mid(x: str, t: float):
        s = at(x, t)
        if not s or s[1] is None or s[2] is None or s[2] - s[1] > args.max_spread:
            return None
        return (s[1] + s[2]) / 2

    hist: dict[str, deque] = defaultdict(deque)  # rid -> (t, S) for the baseline
    busy_until: dict[str, float] = {}
    t_end = events[-1][0] if events else 0
    eps = []
    for t, x in events:
        rid = leg_race.get(x)
        if rid is None:
            continue
        mids = {y: mid(y, t) for y in races[rid]}
        if any(m is None for m in mids.values()):
            continue
        S = sum(mids.values())
        h = hist[rid]
        while h and h[0][0] < t - 1800:
            h.popleft()
        base = statistics.median(v for _, v in h) if len(h) >= 5 else None
        h.append((t, S))
        if base is None or t < busy_until.get(rid, 0):
            continue
        dev = S - base
        if abs(dev) < args.dev:
            continue
        moves = {y: (mids[y] - (mid(y, t - 60) or mids[y])) for y in races[rid]}
        leader = max(moves, key=lambda y: abs(moves[y]))
        if moves[leader] * dev <= 0:
            continue  # no leg moved first in the direction of the gap
        others = [y for y in races[rid] if y != leader]
        lag = max(others, key=lambda y: mids[y])
        busy_until[rid] = t + max(HORIZONS) / 3 + args.cooldown
        te = t + args.delay
        if te > t_end:
            continue
        ent = at(lag, te)
        if ent is None or ent[1] is None or ent[2] is None:
            continue
        short = dev > 0
        px_in = ent[1] if short else ent[2]
        size = (ent[3] if short else ent[4]) or 0
        L0, Lpre, lag0 = mids[leader], mids[leader] - moves[leader], mids[lag]
        ep = {"race": names[rid], "t": t, "dev": dev, "leader_move": moves[leader],
              "short": short, "px_in": px_in, "size": size, "h": {}}
        for H in HORIZONS:
            th = t + H
            if th > t_end:
                break
            ex = at(lag, th)
            lm, gm = mid(leader, th), mid(lag, th)
            if ex is None or ex[1] is None or ex[2] is None or lm is None or gm is None:
                continue
            taker = (px_in - ex[2]) if short else (ex[1] - px_in)
            atmid = (px_in - gm) if short else (gm - px_in)
            keep = (lm - Lpre) / moves[leader]          # share of the jump the leader kept
            closed = (lag0 - gm) * (1 if dev > 0 else -1) / abs(dev)  # share of the gap the lagger closed
            ep["h"][H] = {"taker": taker, "mid": atmid, "keep": keep, "closed": closed}
        eps.append(ep)

    print(f"tape: {len(events):,} updates, {len(races)} races, "
          f"{(t_end - events[0][0]) / 60 if events else 0:.0f} min; episodes (|dev| >= {args.dev}): {len(eps)}")
    if not eps:
        return 0
    print(f"\n{'horizon':>8} {'n':>4} {'lagger follows':>15} {'leader reverts':>15} "
          f"{'taker P&L/sh med':>17} {'taker >0':>9} {'mid P&L/sh med':>15} {'taker P&L total':>16}")
    for H in HORIZONS:
        rows = [e for e in eps if H in e["h"]]
        if not rows:
            continue
        hs = [e["h"][H] for e in rows]
        follow = sum(1 for v in hs if v["closed"] >= 0.5 and v["keep"] >= 0.5) / len(hs)
        revert = sum(1 for v in hs if v["keep"] < 0.5) / len(hs)
        tk = [v["taker"] for v in hs]
        md = [v["mid"] for v in hs]
        tot = sum(e["h"][H]["taker"] * min(e["size"], 5000) for e in rows)
        print(f"{H:>7}s {len(rows):>4} {follow:>14.0%} {revert:>15.0%} {statistics.median(tk):>+17.4f} "
              f"{sum(v > 0 for v in tk) / len(tk):>8.0%} {statistics.median(md):>+15.4f} {tot:>+16,.0f}")
    print("\n(total = taker P&L on min(touch size, 5,000) shares per episode)")
    print("\nlargest episodes:")
    for e in sorted(eps, key=lambda e: -abs(e["dev"]))[:12]:
        h5 = e["h"].get(300) or {}
        print(f"  {str(e['race'])[:34]:<34} dev {e['dev']:+.3f} leader moved {e['leader_move']:+.3f} "
              f"{'short' if e['short'] else 'long '} lagger @ {e['px_in']:.3f} x {e['size'] or 0:>6} | "
              f"5m: taker {h5.get('taker', float('nan')):+.3f} keep {h5.get('keep', float('nan')):.2f} "
              f"closed {h5.get('closed', float('nan')):.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
