#!/usr/bin/env python3
"""Ladder quoter CLI. DRY RUN unless --live is passed.

    python3 run_quoter.py                          # show what it would rest
    python3 run_quoter.py --state RI --min-fair 0.90
    python3 run_quoter.py --live --once             # post one pass, then stop
    python3 run_quoter.py --live --interval 120     # keep ladders refreshed
    python3 run_quoter.py --unwind                  # post exits for swept fills

Safety: dry run by default; exposure capped by --budget-pct and --race-pct;
every rung is placed strictly below the best bid so nothing crosses.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from datetime import datetime, timezone

from sigcup.catalog import group_by_race, load_catalog
from sigcup.client import TICK, ApiError, SuperMarketClient, load_env, round_to_tick
from sigcup.quoter import apply_plan, plan_races

_stop = False


def _sig(signum, frame):
    global _stop
    _stop = True
    print("\n[finishing this pass, then stopping]", flush=True)
    signal.signal(signum, signal.SIG_DFL)


def show(plans, stats, live: bool) -> None:
    print(f"\ncash {stats['cash']:,.2f} | global cap {stats['total_budget']:,.0f} "
          f"({stats['total_budget'] / stats['cash'] * 100:.0f}%) | per-race cap {stats['race_budget']:,.0f}")
    print(f"quoting {stats['races_quoted']} races, skipping {stats['races_skipped']}, "
          f"committing {stats['committed']:,.0f} notional\n")

    quoted = [p for p in plans if p.quotes]
    for p in sorted(quoted, key=lambda p: -p.fair):
        print(f"  {p.race_name}  [{p.leg.winner}]  fair {p.fair:.3f} "
              f"(coherence gap {p.coherence:.3f}, bestBid {p.leg.best_bid})")
        for q in p.quotes:
            pct = q.price / p.fair * 100
            print(f"      bid {q.price:<6} x {q.quantity:>7,} sh = {q.notional:>9,.0f} "
                  f"({pct:>5.1f}% of fair)   payoff if it reverts: "
                  f"{(p.fair - q.price) * q.quantity:>9,.0f}")
        print(f"      race notional {p.notional:>,.0f}\n")

    skipped = [p for p in plans if not p.quotes]
    if skipped:
        print(f"  skipped ({len(skipped)}):")
        for p in sorted(skipped, key=lambda p: -(p.fair or 0))[:14]:
            print(f"    {str(p.race_name)[:40]:<40} fair {p.fair:>6.3f}  {p.skipped}")
        if len(skipped) > 14:
            print(f"    … and {len(skipped) - 14} more")

    if not live:
        print("\nDRY RUN — nothing was placed. Add --live to post these.")


def sellable_above(book: dict, floor_price: float, cap: float) -> tuple[int, float]:
    """Walk the bid ladder and return (shares, lowest price) sellable at or above
    `floor_price`, capped at `cap` shares.

    Sizing to actual depth is the whole point: a market sell for the full position
    would walk straight through the bids and give back the edge, because depth
    above our entry is typically only a few hundred shares.
    """
    taken = 0
    lowest = None
    for lvl in sorted(book.get("bids") or [], key=lambda l: -l["price"]):
        if lvl["price"] < floor_price:
            break
        take = min(lvl["quantity"], cap - taken)
        if take <= 0:
            break
        taken += take
        lowest = lvl["price"]
    return int(taken), (lowest if lowest is not None else 0.0)


def unwind(
    c: SuperMarketClient,
    live: bool,
    cross: bool = False,
    cross_min_edge: float = 0.005,
) -> None:
    """Post exits only where selling beats holding.

    Our profit is the discount to fair we were filled at. Holding to settlement
    is worth `fair` per share in expectation (it pays 1 with probability ~fair),
    so:

        sell above fair  -> strictly better than holding; post it
        sell below fair  -> hands the edge back; hold instead

    So the exit undercuts the best offer by one tick for queue priority, and is
    posted only when that price still clears both fair and our average cost.

    The earlier version targeted `fair * 0.97`, which was calibrated for deep
    fills (buy at 0.25 of fair). After the ladder was reweighted shallow, that
    target landed on our own entry price and nothing ever exited.
    """
    print("\n=== unwind pass ===")
    pos = [p for p in c.positions() if p["quantity"] > 0]
    if not pos:
        print("  no long positions")
        return

    # Sells already resting, per exchange. Without this the loop re-posts an exit
    # every pass; those stack past the position size, and the engine canonicalizes
    # an oversized sell into a complement buy — i.e. it would quietly open a short.
    resting_sells: dict[str, float] = {}
    for o in c.open_orders():
        if o.get("open") and o.get("action") == "sell":
            exid = str(o["exchangeId"])
            resting_sells[exid] = resting_sells.get(exid, 0.0) + float(o["quantity"])

    for p in pos:
        exid = str(p["exchangeId"])
        already = resting_sells.get(exid, 0.0)
        if already >= p["quantity"]:
            print(f"  ex {exid}: exit already resting for {already:,.0f} of "
                  f"{p['quantity']:,} — leaving it")
            continue
        book = c.exchange_orderbook(exid, depth=50)
        bid, ask = book.get("bestBid"), book.get("bestAsk")
        if bid is None or ask is None:
            print(f"  ex {exid}: one-sided book, skipping")
            continue
        fair = (bid + ask) / 2
        avg = p["avgCost"]
        uncovered = int(p["quantity"] - already)

        # Phase 1 — take the certain profit by crossing, sized to real depth.
        # Equivalent to the pair-cancel framing: buying NO at n while long YES
        # cancels for 1 and nets bestBid - avg, so this is profitable exactly when
        # NO bid + entry < 1. It gives up the gap between bid and fair, in exchange
        # for immediate certainty and recycled capital.
        if cross and uncovered > 0:
            floor_px = round_to_tick(avg + cross_min_edge)
            qty, lowest = sellable_above(book, floor_px, uncovered)
            if qty > 0:
                locked = (lowest - avg) * qty
                print(f"  ex {exid} CROSS {qty:,} sh down to {lowest} "
                      f"(entry {avg:.3f}) -> locks >= {locked:+,.0f}")
                if live:
                    r = c.place_order(exid, "yes", "sell", qty, lowest)
                    done = r.get("quantityTraded") or 0
                    print(f"    traded {done:,} @ {r.get('fillPrice')} "
                          f"terminalReasonCode={r.get('terminalReasonCode')}")
                    uncovered -= int(done)
                else:
                    uncovered -= qty
            else:
                print(f"  ex {exid}: no bid depth at or above {floor_px} "
                      f"(entry {avg:.3f}, bid {bid})")

        if uncovered <= 0:
            continue
        # JOIN the offer rather than undercut it. Undercutting by a tick and then
        # demanding target > fair is unsatisfiable unless the spread exceeds two
        # ticks: ask - TICK > (bid+ask)/2  <=>  spread > 2*TICK. Most books here
        # are 0.005-0.015 wide, which is why no exit ever posted. Sitting AT the
        # ask always clears fair, since ask > mid by construction.
        target = round_to_tick(ask)
        floor = max(fair, avg)                    # must beat holding AND our entry
        gain = (target - avg) * uncovered
        print(f"  ex {exid} qty {uncovered:,} avgCost {avg:.3f} fair {fair:.3f} "
              f"ask {ask} -> offer at {target} for {gain:+,.0f}")
        if target <= floor:
            why = "below fair, so holding is worth more" if target <= fair else "below our cost"
            print(f"    not posting: {target} is {why} (hold EV/share = {fair:.3f})")
            continue
        if live:
            r = c.place_order(exid, "yes", "sell", uncovered, target)
            print(f"    placed: open={r.get('open')} traded={r.get('quantityTraded')} "
                  f"canonical={ {k: r.get(k) for k in ('side', 'action', 'price')} }")
    if not live:
        print("  DRY RUN — no exits placed.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="actually place orders")
    ap.add_argument("--state", help="restrict to one state")
    ap.add_argument("--race-id", action="append", default=[], help="restrict to raceId(s)")
    ap.add_argument("--min-fair", type=float, default=0.90,
                    help="only ladder legs at least this likely (default 0.90)")
    ap.add_argument("--budget-pct", type=float, default=0.50,
                    help="max fraction of cash in resting bids (default 0.50)")
    ap.add_argument("--race-pct", type=float, default=0.10,
                    help="max fraction of cash per race (default 0.10)")
    ap.add_argument("--coherence", type=float, default=0.08,
                    help="max leg disagreement before skipping a race")
    ap.add_argument("--interval", type=float, default=180.0, help="refresh seconds")
    ap.add_argument("--once", action="store_true", help="single pass")
    ap.add_argument("--unwind", action="store_true", help="run the exit pass too")
    ap.add_argument("--unwind-every", type=int, default=1,
                    help="run the exit pass every N passes (default every pass)")
    ap.add_argument("--max-hours", type=float, help="stop after this many hours")
    ap.add_argument("--max-deploy-pct", type=float,
                    help="halt new ladders once this fraction of starting cash has "
                         "actually been spent on fills (defaults to --budget-pct)")
    ap.add_argument("--cash-floor-pct", type=float, default=0.25,
                    help="hard stop: cancel everything if cash falls below this "
                         "fraction of the starting balance (default 0.25)")
    ap.add_argument("--cross-exit", action="store_true",
                    help="also cross into the bids to lock certain profit, sized to "
                         "depth above entry (equivalent to: exit when NO bid + entry < 1)")
    ap.add_argument("--cross-min-edge", type=float, default=0.005,
                    help="minimum profit per share required to cross (default one tick)")
    ap.add_argument("--shrink-tol", type=float, default=0.25,
                    help="fraction a resting rung may be shrunk by the engine before "
                         "we re-post it; set high when overbooked (default 0.25)")
    ap.add_argument("--log", default="data/quoter.jsonl", help="per-pass state log")
    args = ap.parse_args()
    if args.max_deploy_pct is None:
        args.max_deploy_pct = args.budget_pct

    load_env()
    c = SuperMarketClient(allow_trading=args.live)

    rows = [r for r in load_catalog() if r.get("exchangeId") and r.get("raceId")]
    if args.state:
        rows = [r for r in rows if (r.get("usState") or "").upper() == args.state.upper()]
    races = group_by_race(rows)
    if args.race_id:
        races = {k: v for k, v in races.items() if k in set(args.race_id)}
    if not races:
        print("no races matched")
        return 1
    print(f"{len(races)} races in scope  |  mode: {'LIVE' if args.live else 'DRY RUN'}")

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)
    os.makedirs(os.path.dirname(args.log) or ".", exist_ok=True)
    log = open(args.log, "a", buffering=1)

    start_cash = c.balance()
    deploy_cap = start_cash * args.max_deploy_pct
    cash_floor = start_cash * args.cash_floor_pct
    started = time.time()
    print(f"starting cash {start_cash:,.2f} | deploy cap {deploy_cap:,.0f} "
          f"| hard floor {cash_floor:,.0f}")

    npass = 0
    consecutive_errors = 0
    halted = False

    while not _stop:
        npass += 1
        t0 = time.time()
        try:
            cash = c.balance()
            positions = c.positions()
            deployed = start_cash - cash
            cost_basis = sum(p.get("costBasis") or 0 for p in positions)

            # --- circuit breakers, checked before anything is posted ----------
            if cash < cash_floor:
                print(f"!! HARD STOP: cash {cash:,.2f} below floor {cash_floor:,.0f}. "
                      f"Cancelling all orders and exiting.")
                if args.live:
                    for exid in {str(p['exchangeId']) for p in positions} | \
                                {str(o['exchangeId']) for o in c.open_orders()}:
                        try:
                            c.cancel_all(exchange_id=exid)
                        except ApiError as e:
                            print(f"   cancel {exid}: {e.code}")
                break
            if deployed >= deploy_cap and not halted:
                halted = True
                print(f"!! deploy cap reached ({deployed:,.0f} >= {deploy_cap:,.0f}). "
                      f"No new ladders; exit pass continues.")
            elif deployed < deploy_cap * 0.9 and halted:
                halted = False
                print("   deployed capital recovered below cap; resuming ladders.")

            res = {"placed": 0, "writes": 0, "unchanged": 0}
            stats = {}
            if not halted:
                plans, stats = plan_races(
                    c, races, cash,
                    min_fair=args.min_fair,
                    max_coherence_gap=args.coherence,
                    total_budget_pct=args.budget_pct,
                    per_race_pct=args.race_pct,
                )
                show(plans, stats, args.live)
                res = apply_plan(c, plans, live=args.live, shrink_tol=args.shrink_tol)

            if args.unwind and npass % args.unwind_every == 0:
                unwind(c, args.live, cross=args.cross_exit, cross_min_edge=args.cross_min_edge)

            open_orders = len(c.open_orders())
            print(f"\npass {npass}: {res['placed']} rungs, {res['writes']} writes, "
                  f"{res['unchanged']} unchanged | cash {cash:,.2f} "
                  f"deployed {deployed:,.0f}/{deploy_cap:,.0f} | {len(positions)} positions "
                  f"(basis {cost_basis:,.0f}) | {open_orders} resting | "
                  f"{time.time() - t0:.0f}s | uptime {(time.time() - started) / 3600:.2f}h",
                  flush=True)
            log.write(json.dumps({
                "ts": datetime.now(timezone.utc).isoformat(), "pass": npass,
                "cash": cash, "deployed": deployed, "costBasis": cost_basis,
                "positions": len(positions), "openOrders": open_orders,
                "halted": halted, "placed": res["placed"], "writes": res["writes"],
                "committed": stats.get("committed"), "racesQuoted": stats.get("races_quoted"),
            }) + "\n")
            consecutive_errors = 0

        except (ApiError, OSError) as e:
            # Transient failures are expected: the docs call 429/503 normal and we
            # measured 2-17s latency. One of these must never end a 24h run.
            consecutive_errors += 1
            print(f"pass {npass} FAILED ({consecutive_errors} in a row): "
                  f"{type(e).__name__}: {e}", flush=True)
            log.write(json.dumps({
                "ts": datetime.now(timezone.utc).isoformat(), "pass": npass,
                "error": f"{type(e).__name__}: {e}", "consecutive": consecutive_errors,
            }) + "\n")
            if consecutive_errors >= 15:
                print("!! 15 consecutive failures — something is structurally wrong. Exiting.")
                break
            time.sleep(min(30 * consecutive_errors, 300))
            continue

        if args.once:
            break
        if args.max_hours and (time.time() - started) >= args.max_hours * 3600:
            print(f"\nreached --max-hours {args.max_hours}; stopping.")
            break
        time.sleep(max(0.0, args.interval - (time.time() - t0)))

    log.close()
    print(f"\nstopped after {npass} passes / {(time.time() - started) / 3600:.2f}h")
    try:
        print(f"final cash {c.balance():,.2f} (started {start_cash:,.2f}), "
              f"{len(c.open_orders())} orders still resting")
    except ApiError:
        pass
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ApiError as e:
        print(f"\nAPI ERROR {e.status} {e.code}: {e.message}\ndetails={e.details}")
        sys.exit(1)
