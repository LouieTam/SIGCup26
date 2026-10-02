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


def unwind(
    c: SuperMarketClient,
    live: bool,
    min_edge: float = 0.005,
    reprice_ticks: int = 2,
) -> None:
    """Exit by improving the offer: post a YES sell one tick inside the best ask.

    Equivalent formulation (and where this rule came from): post a NO bid one
    tick above the best NO bid. Since a NO bid at `n` is a YES offer at `1 - n`,
    and buying NO while long YES cancels the pair for 1 immediately, the profit is
    `1 - avg - n` == `(ask - TICK) - avg`. The two are the same order.

    Why improve rather than join the ask: at the ask we queue behind everyone
    already there; one tick inside, we are the best offer and fill first. In a
    one-tick-spread market `ask - TICK` IS the bid, so the order crosses and fills
    immediately — the rule degenerates gracefully into taking the bid.

    No depth-walking is needed: a limit sell only fills against bids at or above
    its price, so the limit itself prevents walking the book down.

    The floor is `avg + min_edge`, NOT fair. Fair was the wrong guard — it blocks
    the rule whenever the spread is under two ticks (`ask - TICK > fair` requires
    `spread > 2*TICK`), and it is inconsistent with taking the bid at all. Flooring
    at cost is what actually matters: it guarantees we never realise a loss. When
    the floor sits above the ask (a position that moved against us) the order rests
    above the market as free optionality on a rally rather than selling at a loss.
    """
    print("\n=== unwind pass ===")
    pos = [p for p in c.positions() if p["quantity"] > 0]
    if not pos:
        print("  no long positions")
        return

    sells: dict[str, list[dict]] = {}
    for o in c.open_orders():
        if o.get("open") and o.get("action") == "sell":
            sells.setdefault(str(o["exchangeId"]), []).append(o)

    for p in pos:
        exid = str(p["exchangeId"])
        qty = int(p["quantity"])
        avg = p["avgCost"]
        book = c.exchange_orderbook(exid, depth=5)
        bid, ask = book.get("bestBid"), book.get("bestAsk")
        if ask is None:
            print(f"  ex {exid}: no offer side, skipping")
            continue
        fair = ((bid + ask) / 2) if bid is not None else ask

        improve = round_to_tick(ask - TICK)
        floor = round_to_tick(avg + min_edge)
        target = max(improve, floor)
        crosses = bid is not None and target <= bid

        existing = sells.get(exid, [])
        resting = sum(int(o["quantity"]) for o in existing)
        # Re-price stale exits. Cancel by order id, never cancel-all — that would
        # also wipe the sweep ladder resting on the same exchange.
        stale = [o for o in existing
                 if o.get("priceLimit") is not None
                 and abs(float(o["priceLimit"]) - target) > reprice_ticks * TICK]
        if stale:
            print(f"  ex {exid}: repricing {len(stale)} stale exit(s) "
                  f"{[o['priceLimit'] for o in stale]} -> {target}")
            for o in stale:
                if live:
                    try:
                        c.cancel(str(o["id"]))
                        resting -= int(o["quantity"])
                    except ApiError as e:
                        print(f"    cancel {o['id']}: {e.code}")
                else:
                    resting -= int(o["quantity"])

        uncovered = qty - resting
        tag = "CROSSES now" if crosses else "rests as best offer"
        if target > ask:
            tag = "rests ABOVE market (optionality; floor is above the ask)"
        print(f"  ex {exid} qty {qty:,} avg {avg:.3f} bid {bid} ask {ask} fair {fair:.3f}"
              f" -> offer {target} ({tag}), uncovered {uncovered:,}, "
              f"edge {(target-avg)*max(uncovered,0):+,.0f}")
        if uncovered <= 0:
            continue
        if target < floor:
            print(f"    skipped: {target} below cost floor {floor}")
            continue
        if live:
            r = c.place_order(exid, "yes", "sell", uncovered, target)
            print(f"    placed: traded {r.get('quantityTraded') or 0:,} @ "
                  f"{r.get('fillPrice')} open={r.get('open')} "
                  f"terminalReasonCode={r.get('terminalReasonCode')}")
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
    ap.add_argument("--min-exit-edge", type=float, default=0.005,
                    help="minimum profit per share required to post an exit (default one tick)")
    ap.add_argument("--reprice-ticks", type=int, default=2,
                    help="re-price a resting exit once it drifts this many ticks from target")
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
                unwind(c, args.live, min_edge=args.min_exit_edge, reprice_ticks=args.reprice_ticks)

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
