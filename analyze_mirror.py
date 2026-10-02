#!/usr/bin/env python3
"""Would a mirror ladder pay? Compare it to the main ladder on the same races.

The main ladder rests YES bids on a race's favoured leg, so it is filled when
someone DUMPS the favourite. The mirror rests NO bids on the race's longshot
legs, so it is filled when someone aggressively BUYS a longshot. Same view on
the race, different flow. This measures, over the same races and window, how
much flow each side would actually have met.

Depths are expressed identically on both sides so the comparison is fair:

    main    YES bid  p = d * fair_fav            fills on fav prints  <= p
    mirror  NO  bid  q = d * (1 - fair_ls)       fills on ls  prints  >= 1 - q
                     (a NO bid at q is a YES offer at 1 - q)

Equal capital C at every depth on each side:
    shares = C / price;  filled = min(shares, volume past the price)
    profit = (fair - price) * filled          (fair of the side we buy)

Volume past a price is an UPPER bound on what we would have filled (queue
position, and our own fills absorbing flow) -- the same caveat as
analyze_depth.py, applied equally to both sides.

Throttled well below the account read budget, because the live quoter shares
it (~80 reads/min) and a 429 there stalls its exits for 60s.

    python3 analyze_mirror.py --since 2026-10-01T19:00:00Z --reads-per-min 12
"""

from __future__ import annotations

import argparse
import json

from detect_sweeps import find_sweeps
from sigcup.catalog import group_by_race, load_catalog
from sigcup.client import ApiError, SuperMarketClient, _Budget, load_env
from sigcup.quoter import DEFAULT_RUNGS

DEPTHS = [d for d, _ in DEFAULT_RUNGS]


def tape(c, exid: str, frm: str, max_pages: int) -> list[dict]:
    out, cursor = [], None
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


def side_stats(prints, fair: float, capital: float, mirror: bool) -> dict:
    """Per depth: filled shares and profit for `capital` resting there."""
    res = {}
    for d in DEPTHS:
        if mirror:
            q = round(d * fair, 3)            # NO bid; fair here is the NO fair
            yes_px = 1 - q
            vol = sum(sz for px, sz in prints if px >= yes_px - 1e-9)
            price = q
        else:
            price = round(d * fair, 3)
            vol = sum(sz for px, sz in prints if px <= price + 1e-9)
        if price <= 0:
            continue
        filled = min(capital / price, vol)
        res[d] = {"filled": filled, "profit": (fair - price) * filled}
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-10-01T19:00:00Z")
    ap.add_argument("--min-fair", type=float, default=0.90)
    ap.add_argument("--max-fair", type=float, default=1.01,
                    help="only races whose favourite is below this (e.g. 0.60-0.90 band)")
    ap.add_argument("--capital", type=float, default=4000.0)
    ap.add_argument("--max-pages", type=int, default=5)
    ap.add_argument("--reads-per-min", type=int, default=12)
    ap.add_argument("--out", default="data/mirror.json")
    args = ap.parse_args()

    load_env()
    c = SuperMarketClient()
    c._reads = _Budget(args.reads_per_min, headroom=0)

    races = group_by_race([r for r in load_catalog() if r.get("exchangeId") and r.get("raceId")])
    ids = [l["exchangeId"] for legs in races.values() for l in legs]
    px = {}
    for i in range(0, len(ids), 100):
        for row in c.prices(ids[i:i + 100]).get("data") or []:
            px[row["exchangeId"]] = row

    def mid(exid):
        p = px.get(exid) or {}
        if p.get("bestBid") is None or p.get("bestAsk") is None:
            return None
        return (p["bestBid"] + p["bestAsk"]) / 2

    plan = []
    for rid, legs in races.items():
        mids = [(mid(l["exchangeId"]), l) for l in legs]
        mids = [(m, l) for m, l in mids if m is not None]
        if len(mids) < 2:
            continue
        mids.sort(key=lambda t: -t[0])
        fav_fair, fav = mids[0]
        if args.min_fair <= fav_fair < args.max_fair:
            plan.append((rid, fav, fav_fair, mids[1:]))
    print(f"{len(plan)} races with a favourite in [{args.min_fair}, {args.max_fair}); "
          f"reading tapes since {args.since} at {args.reads_per_min} reads/min\n", flush=True)

    agg = {s: {d: {"filled": 0.0, "profit": 0.0, "hit": 0} for d in DEPTHS}
           for s in ("main", "mirror")}
    n_main = n_mirror = 0
    sweeps = {"main": {"DOWN": [0, 0.0], "UP": [0, 0.0]},
              "mirror": {"DOWN": [0, 0.0], "UP": [0, 0.0]}}
    per_race = []
    for rid, fav, fav_fair, longshots in plan:
        try:
            t_fav = tape(c, fav["exchangeId"], args.since, args.max_pages)
        except ApiError as e:
            print(f"  {rid}: {e.code}")
            continue
        prints = [(t["price"], t["size"]) for t in t_fav if t.get("price") is not None]
        main_s = side_stats(prints, fav_fair, args.capital, mirror=False)
        n_main += 1
        for s in find_sweeps(t_fav, 3, 1000):
            if s["reverted"]:
                sweeps["main"][s["direction"]][0] += 1
                sweeps["main"][s["direction"]][1] += s["gift"]
        mirror_profit = 0.0
        for ls_fair, ls in longshots:
            try:
                t_ls = tape(c, ls["exchangeId"], args.since, args.max_pages)
            except ApiError as e:
                print(f"  {rid}/{ls['exchangeId']}: {e.code}")
                continue
            lp = [(t["price"], t["size"]) for t in t_ls if t.get("price") is not None]
            mir = side_stats(lp, 1 - ls_fair, args.capital, mirror=True)
            n_mirror += 1
            for d, v in mir.items():
                a = agg["mirror"][d]
                a["filled"] += v["filled"]; a["profit"] += v["profit"]; a["hit"] += v["filled"] > 0
            mirror_profit += sum(v["profit"] for v in mir.values())
            for s in find_sweeps(t_ls, 3, 1000):
                if s["reverted"]:
                    sweeps["mirror"][s["direction"]][0] += 1
                    sweeps["mirror"][s["direction"]][1] += s["gift"]
        for d, v in main_s.items():
            a = agg["main"][d]
            a["filled"] += v["filled"]; a["profit"] += v["profit"]; a["hit"] += v["filled"] > 0
        main_profit = sum(v["profit"] for v in main_s.values())
        per_race.append({"race": rid, "name": fav.get("raceName"), "fav": fav.get("winnerName"),
                         "fav_fair": fav_fair, "main_profit": main_profit,
                         "mirror_profit": mirror_profit})
        print(f"  {str(fav.get('raceName'))[:34]:<34} fav {fav_fair:.3f}  "
              f"main {main_profit:>8,.0f}  mirror {mirror_profit:>8,.0f}", flush=True)

    print(f"\n{'depth':<7}{'main hit':>10}{'main profit':>14}{'main ROC':>10}"
          f"{'mirror hit':>12}{'mirror profit':>15}{'mirror ROC':>12}")
    for d in DEPTHS:
        m, r = agg["main"][d], agg["mirror"][d]
        mroc = m["profit"] / (args.capital * max(n_main, 1)) * 100
        rroc = r["profit"] / (args.capital * max(n_mirror, 1)) * 100
        print(f"{d:<7.2f}{m['hit']:>5}/{n_main:<4}{m['profit']:>14,.0f}{mroc:>9.2f}%"
              f"{r['hit']:>7}/{n_mirror:<4}{r['profit']:>15,.0f}{rroc:>11.2f}%")
    tm = sum(agg["main"][d]["profit"] for d in DEPTHS)
    tr = sum(agg["mirror"][d]["profit"] for d in DEPTHS)
    print(f"\ntotal: main {tm:,.0f} over {n_main} legs | mirror {tr:,.0f} over {n_mirror} legs")
    print("reverting sweeps (count, value to passive side):")
    for s in ("main", "mirror"):
        print(f"  {s:<7} DOWN {sweeps[s]['DOWN'][0]:>3} {sweeps[s]['DOWN'][1]:>10,.0f} | "
              f"UP {sweeps[s]['UP'][0]:>3} {sweeps[s]['UP'][1]:>10,.0f}")
    with open(args.out, "w") as f:
        json.dump({"args": vars(args), "agg": agg, "sweeps": sweeps, "races": per_race,
                   "n_main": n_main, "n_mirror": n_mirror}, f, indent=1)
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
