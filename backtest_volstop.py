#!/usr/bin/env python3
"""Backtest a volatility-scaled stop-loss against what actually happened.

Every position we held since --since is rebuilt from our own fills (running
average cost, open -> flat). For each one, 5-minute candles of its market give
a price path on our side (NO = 1 - YES), and sigma = std of 15-minute changes
over the trailing 24h (floored at one tick).

Rule V(k, m): sell everything held the first time the price has stayed at or
below  cost - max(k * sigma, 2 ticks)  for m consecutive minutes.

Counterfactual P&L of an episode under V = realised P&L of our actual trades up
to the trigger + (trigger price - 1 tick - avg cost) * shares held then. Buys
after the trigger are dropped (the race would have been pulled). Delta = that
minus the episode's actual realised P&L (open episodes are marked at the last
price). A positive delta means the rule would have helped. Episodes where V
fires and the price then recovers count against it: that is the cost of
stopping out a dip.

Data is cached in data/volstop_cache.json, so the grid can be re-run offline:
    python3 backtest_volstop.py --since 2026-10-04T00:00:00Z            # fetch + run
    python3 backtest_volstop.py --offline                               # re-run grid
"""
from __future__ import annotations

import argparse
import json
import statistics
from collections import defaultdict
from datetime import datetime, timedelta

TICK = 0.005
CACHE = "data/volstop_cache.json"


def ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def episodes(trades: list[dict]) -> list[dict]:
    """Split our trades into (exid, side) holding episodes with running avg cost."""
    book: dict[tuple, dict] = {}
    out = []
    for t in sorted(trades, key=lambda t: t["createdAt"]):
        q = int(t["quantity"]); side = "no" if q < 0 else "yes"; n = abs(q); px = float(t["price"])
        k = (str(t["exchangeId"]), side)
        e = book.get(k)
        if t["orderType"] == "BUY":
            if e is None:
                e = book[k] = {"exid": k[0], "side": side, "start": t["createdAt"], "qty": 0,
                               "avg": 0.0, "events": [], "realised": 0.0}
            e["avg"] = (e["avg"] * e["qty"] + px * n) / (e["qty"] + n)
            e["qty"] += n
        elif e is not None:
            n = min(n, e["qty"])
            e["realised"] += (px - e["avg"]) * n
            e["qty"] -= n
        else:
            continue
        e["events"].append((t["createdAt"], t["orderType"], n, px, e["qty"], e["avg"]))
        if e["qty"] == 0:
            e["end"] = t["createdAt"]
            out.append(book.pop(k))
    for e in book.values():  # still open
        e["end"] = None
        out.append(e)
    return out


def fetch(since: str, rpm: int) -> dict:
    from sigcup.client import SuperMarketClient, _Budget, load_env
    load_env()
    c = SuperMarketClient()
    c._reads = _Budget(rpm, headroom=0)
    trades = []
    for t in c._paged(f"/tournaments/{c.tournament_slug}/portfolio/transactions", limit=100):
        if t["createdAt"] < since:
            break
        if t.get("event_type") == "trade":
            trades.append(t)
    print(f"{len(trades):,} trades since {since}", flush=True)
    eps = episodes(trades)
    candles = {}
    by_ex = defaultdict(list)
    for e in eps:
        by_ex[e["exid"]].append(e)
    for i, (x, es) in enumerate(by_ex.items()):
        frm = min(ts(e["start"]) for e in es) - timedelta(hours=24)
        rows, cur = [], frm
        while True:  # page forward in time
            h = c._get(f"/exchanges/{x}/price-history", tournamentId=c.tournament_id,
                       resolution="5m", limit=1000, **{"from": cur.isoformat().replace("+00:00", "Z")})
            got = h.get("candles") or []
            rows += got
            if len(got) < 1000:
                break
            cur = ts(got[-1]["time"]) + timedelta(minutes=5)
        candles[x] = [(r["time"], r.get("close")) for r in rows if r.get("close") is not None]
        if i % 20 == 0:
            print(f"  candles {i + 1}/{len(by_ex)}", flush=True)
    data = {"since": since, "trades": trades, "candles": candles}
    json.dump(data, open(CACHE, "w"))
    return data


def run(data: dict, ks, ms, show_top: int = 12) -> None:
    eps = episodes(data["trades"])
    results = {}
    detail = {}
    for e in eps:
        series = data["candles"].get(e["exid"]) or []
        if not series:
            continue
        times = [ts(t) for t, _ in series]
        px = [c if e["side"] == "yes" else 1 - c for _, c in series]
        start = ts(e["start"]); end = ts(e["end"]) if e["end"] else times[-1]
        # actual outcome: realised, plus open remainder marked at the last price
        actual = e["realised"] + (e["qty"] * (px[-1] - e["avg"]) if e["end"] is None else 0)
        ev = [(ts(a), b, n, p, q, avg) for a, b, n, p, q, avg in e["events"]]
        for k in ks:
            for m in ms:
                need = m // 5
                run_len = 0
                trig = None
                for i, t in enumerate(times):
                    if t < start or t > end:
                        continue
                    # state as of t
                    held, avg, real = 0, 0.0, 0.0
                    for (et, kind, n, p, q, a) in ev:
                        if et > t:
                            break
                        held, avg = q, a
                    if held == 0:
                        run_len = 0
                        continue
                    lo = t - timedelta(hours=24)
                    w = [px[j] for j in range(max(0, i - 288), i + 1) if times[j] >= lo]
                    ch = [w[j] - w[j - 3] for j in range(3, len(w))]
                    sig = max(statistics.pstdev(ch) if len(ch) > 5 else TICK, TICK)
                    thresh = avg - max(k * sig, 2 * TICK)
                    run_len = run_len + 1 if px[i] <= thresh + 1e-9 else 0
                    if run_len >= need:
                        trig = (t, px[i], held, avg, sig)
                        break
                key = (k, m)
                r = results.setdefault(key, {"fired": 0, "delta": 0.0, "help": 0, "hurt": 0})
                if trig is None:
                    continue
                t, p, held, avg, sig = trig
                # realised P&L of actual sells strictly before the trigger
                real_pre, run_avg, run_q = 0.0, 0.0, 0
                for (et, kind, n, pp, q, a) in ev:
                    if et >= t:
                        break
                    if kind == "BUY":
                        run_avg, run_q = a, q
                    else:
                        real_pre += (pp - run_avg) * n
                        run_q = q
                variant = real_pre + (p - TICK - avg) * held
                d = variant - actual
                r["fired"] += 1; r["delta"] += d
                r["help" if d > 0 else "hurt"] += 1
                detail.setdefault(key, []).append((d, e["exid"], e["side"], e["start"][:16], t.isoformat()[:16],
                                                    held, avg, p, sig, actual))
    print(f"\n{len(eps)} holding episodes since {data['since']}")
    print(f"{'k':>3} {'m':>4} {'fired':>6} {'helped':>7} {'hurt':>5} {'net vs actual':>14}")
    for (k, m), r in sorted(results.items()):
        print(f"{k:>3} {m:>4} {r['fired']:>6} {r['help']:>7} {r['hurt']:>5} {r['delta']:>+14,.0f}")
    best = max(results, key=lambda kk: results[kk]["delta"])
    print(f"\nbest: k={best[0]} m={best[1]}; largest effects:")
    for d, x, sd, st, tt, held, avg, p, sig, act in sorted(detail.get(best, []), key=lambda r: -abs(r[0]))[:show_top]:
        print(f"  ex {x}/{sd.upper():<3} opened {st} trigger {tt} held {held:>7,} avg {avg:.3f} "
              f"px {p:.3f} sigma {sig:.4f} | actual {act:>+8,.0f} delta {d:>+8,.0f}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-10-04T00:00:00Z")
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--reads-per-min", type=int, default=15)
    args = ap.parse_args()
    data = json.load(open(CACHE)) if args.offline else fetch(args.since, args.reads_per_min)
    run(data, ks=[2, 3, 4], ms=[15, 30, 60])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
