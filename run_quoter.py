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
import threading
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
    print(f"quoting {stats['races_quoted']} races (+{stats.get('mirrors_quoted', 0)} mirror legs), "
          f"skipping {stats['races_skipped']}, "
          f"committing {stats['committed']:,.0f} notional\n")

    quoted = [p for p in plans if p.quotes]
    for p in sorted(quoted, key=lambda p: -p.fair):
        tag = f"MIRROR NO on [{p.leg.winner}]" if p.mirror else f"[{p.leg.winner}]"
        print(f"  {p.race_name}  {tag}  fair {p.fair:.3f} "
              f"(coherence gap {p.coherence:.3f}, YES bid/ask {p.leg.best_bid}/{p.leg.best_ask})")
        for q in p.quotes:
            pct = q.price / p.fair * 100
            print(f"      bid {q.price:<6} x {q.quantity:>7,} sh = {q.notional:>9,.0f} "
                  f"({pct:>5.1f}% of fair)   payoff if it reverts: "
                  f"{(p.fair - q.price) * q.quantity:>9,.0f}")
        print(f"      race notional {p.notional:>,.0f}\n")

    skipped = [p for p in plans if not p.quotes and not p.mirror]
    if skipped:
        print(f"  skipped ({len(skipped)}):")
        for p in sorted(skipped, key=lambda p: -(p.fair or 0))[:14]:
            print(f"    {str(p.race_name)[:40]:<40} fair {p.fair:>6.3f}  {p.skipped}")
        if len(skipped) > 14:
            print(f"    … and {len(skipped) - 14} more")

    if not live:
        print("\nDRY RUN — nothing was placed. Add --live to post these.")


# orderId -> (action, side). Order direction never changes, so this is cached for
# the life of the process; most ids arrive free from open_orders reads.
_order_dir: dict[int, tuple[str, str]] = {}


def _remember(orders: list[dict]) -> None:
    for o in orders:
        _order_dir[o["id"]] = (o["action"], o["side"])


def pos_side(p: dict) -> str:
    """"yes" or "no": which side's shares this position holds. Exits must sell
    that side. Selling the OTHER side is canonicalized into a buy of this one,
    which grows the position instead of closing it.

    The API reports a NO holding as `option: "YES"` with a NEGATIVE quantity
    (lots carry side "NO"; avgCost is in NO terms). Verified on the first live
    mirror fill, ex991, 2026-10-02. So the sign decides, not `option`.
    """
    if str(p.get("option") or "").upper() == "NO":
        return "no"
    return "no" if int(p["quantity"]) < 0 else "yes"


def fills_net(c: SuperMarketClient, exid: str) -> int:
    """Net YES shares held on `exid`, summed from the fills ledger.

    An independent second source for the position, because the positions
    endpoint lags fills. Fills carry no direction, so each fill's order is
    looked up (cached). A sell the engine canonicalized into a complement NO buy
    shows up as side "no" with a negative quantity; a NO buy always reduces YES,
    so magnitudes are used and the sign comes from action x side.
    """
    net = 0
    for f in c.fills(exchange_id=exid):
        oid = f["orderId"]
        if oid not in _order_dir:
            o = c.order(str(oid))
            o = o.get("data", o)
            _order_dir[oid] = (o["action"], o["side"])
        action, _ = _order_dir[oid]
        q = abs(int(f["quantity"]))
        sign = 1 if action == "buy" else -1
        net += sign * q if f["side"] == "yes" else -sign * q
    return net


def held_qty(c: SuperMarketClient, exid: str, side: str = "yes") -> tuple[int, int, int]:
    """(held, positions qty, fills-ledger qty) of `side` shares. `held` is the
    smaller of the two: either source running stale-high is what oversells us
    into the opposite side, so only a reading BOTH agree on is trusted."""
    pos = next((abs(int(p["quantity"])) for p in c.positions()
                if str(p["exchangeId"]) == exid and pos_side(p) == side), 0)
    net_yes = fills_net(c, exid)
    led = net_yes if side == "yes" else -net_yes
    return max(0, min(pos, led)), pos, led


def resting_sells(c: SuperMarketClient, exid: str, side: str = "yes") -> list[dict]:
    orders = c.open_orders(exchangeId=exid)
    _remember(orders)
    return [o for o in orders
            if o.get("open") and o.get("action") == "sell" and o.get("side") == side
            and str(o["exchangeId"]) == exid]


def unwind(
    c: SuperMarketClient,
    live: bool,
    min_edge: float = 0.005,
    reprice_ticks: int = 2,
    min_topup: int = 10,
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

    Sizing (this replaced a "never top up" rule that left ladder fills landing on
    an already-covered exchange with no exit until the next reprice):

        uncovered = held - resting sells

    where `held` is min(positions endpoint, fills ledger) — see held_qty(). Read
    ORDER matters: resting sells are read BEFORE holdings. If an exit fills in
    between, `resting` is stale high and `held` is current, so the error
    under-covers (safe). The reverse order would over-cover by exactly that fill
    and flip us short, which is the ex1059 failure. After any reprice cancel,
    both are re-read in that order before posting.
    """
    print("\n=== unwind pass ===")
    # Each position is exited on its own side: YES holdings (main ladder) by a
    # YES sell, NO holdings (mirror ladder) by a NO sell. Everything below is in
    # that side's price terms.
    candidates = {(str(p["exchangeId"]), pos_side(p)): p for p in c.positions()
                  if abs(p["quantity"]) > 0}
    if not candidates:
        print("  no positions")
        return

    for (exid, side), p in candidates.items():
        avg = p["avgCost"]
        book = c.exchange_orderbook(exid, depth=5)
        ybid, yask = book.get("bestBid"), book.get("bestAsk")
        if side == "yes":
            bid, ask = ybid, yask
        else:  # NO bid = 1 - YES ask, NO ask = 1 - YES bid
            bid = round_to_tick(1 - yask) if yask is not None else None
            ask = round_to_tick(1 - ybid) if ybid is not None else None
        label = exid if side == "yes" else f"{exid}/NO"
        if ask is None:
            print(f"  ex {label}: no offer side, skipping")
            continue
        fair = ((bid + ask) / 2) if bid is not None else ask

        improve = round_to_tick(ask - TICK)
        floor = round_to_tick(avg + min_edge)
        target = max(improve, floor)
        crosses = bid is not None and target <= bid

        # 1. Sells first, then holdings (see docstring).
        existing = resting_sells(c, exid, side)
        # Re-price stale exits. Cancel by order id, never cancel-all — that would
        # also wipe the sweep ladder resting on the same exchange.
        stale = [o for o in existing
                 if o.get("priceLimit") is not None
                 and abs(float(o["priceLimit"]) - target) > reprice_ticks * TICK]
        if stale:
            print(f"  ex {label}: repricing {len(stale)} stale exit(s) "
                  f"{[o['priceLimit'] for o in stale]} -> {target}")
            if not live:
                existing = [o for o in existing if o not in stale]
            else:
                for o in stale:
                    try:
                        c.cancel(str(o["id"]))
                    except ApiError as e:
                        print(f"    cancel {o['id']}: {e.code}")
                # Fills may have landed up to the moment of cancel: re-read.
                existing = resting_sells(c, exid, side)
        resting = sum(int(o["quantity"]) for o in existing)

        # 2. Holdings, read after the sells.
        held, pos_q, led_q = held_qty(c, exid, side)
        uncovered = held - resting
        if pos_q != led_q:
            print(f"  ex {label}: positions {pos_q:,} vs fills ledger {led_q:,} disagree "
                  f"— using {held:,}")
        if uncovered < 0:
            if max(pos_q, led_q) >= resting:
                # Only one source reads low; most likely it is lagging a buy.
                print(f"  ex {label}: offered {resting:,}, sources read {pos_q:,}/{led_q:,}"
                      f" — waiting for them to agree")
                continue
            # The usual cause is benign: an exit filled between the sells read and
            # the holdings read (by design that order errs this way). Re-read the
            # sells; a fill now lowers `resting`, so only a real over-offer survives.
            # (False alarm seen live on ex870, 2026-10-02 18:55Z: 579 shares sold
            # between the reads, and the old code cancelled a working exit.)
            existing = resting_sells(c, exid, side)
            resting = sum(int(o["quantity"]) for o in existing)
            if resting <= held:
                print(f"  ex {label}: offer {resting:,} vs held {held:,} after re-read; "
                      f"an exit was filling mid-read, nothing to do")
                continue
            # Both agree we offered more than we hold. The excess would be
            # canonicalized into a buy of the other side if it filled, so pull the
            # exits; the next pass re-posts at the right size.
            print(f"  ex {label}: OVER-OFFERED {resting:,} vs held {held:,}; cancelling exits")
            if live:
                for o in existing:
                    try:
                        c.cancel(str(o["id"]))
                    except ApiError as e:
                        print(f"    cancel {o['id']}: {e.code}")
            continue
        if uncovered == 0:
            print(f"  ex {label}: {held:,} held, fully offered")
            continue
        if resting > 0 and uncovered < min_topup:
            print(f"  ex {label}: {uncovered:,} uncovered below top-up minimum {min_topup}; "
                  f"leaving for the next reprice")
            continue

        tag = "CROSSES now" if crosses else "rests as best offer"
        if target > ask:
            tag = "rests ABOVE market (optionality; floor is above the ask)"
        kind = f"TOP-UP ({resting:,} already offered)" if resting else "exit"
        print(f"  ex {label} held {held:,} avg {avg:.3f} bid {bid} ask {ask} fair {fair:.3f}"
              f" -> {kind} {uncovered:,} @ {target} ({tag}), "
              f"edge {(target - avg) * uncovered:+,.0f}")
        if live:
            r = c.place_order(exid, side, "sell", uncovered, target)
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
    ap.add_argument("--unwind-interval", type=float, default=120.0,
                    help="seconds between exit passes; runs in its own thread so it is "
                         "never starved by the ladder phase (default 120)")
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
    ap.add_argument("--mirror", type=float, default=0.0,
                    help="also rest NO bids on each quoted race's longshot legs, sized at "
                         "this fraction of the race budget (0 = off; see "
                         "docs/mirror-and-arb-recheck.md)")
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

    # Exits run in their own thread. The ladder phase takes ~45 min (batch orders
    # are placed serially server-side), so an exit pass sequenced after it would
    # only run hourly — far too slow when a swept fill reverts in ~75s. Exits are
    # cheap (a dozen reads, a few writes) and the two paths touch disjoint orders:
    # exits are sells cancelled by id, ladders are buys.
    #
    # The ladder phase also calls rearm() after every exchange it posts, so a fill
    # landing mid-pass gets its exit within seconds rather than up to
    # --unwind-interval later. The lock is essential: two concurrent unwinds can
    # both see the same uncovered position, both post a sell, and flip us short.
    unwind_lock = threading.Lock()
    # Replaced wholesale (never mutated) so the ladder thread can read it unlocked.
    last_seen: dict[str, int] = {}

    def rearm(force: bool = False) -> None:
        nonlocal last_seen
        # Non-blocking: if the other thread is mid-unwind it will cover the fill.
        if not unwind_lock.acquire(blocking=False):
            return
        try:
            snap = {f'{p["exchangeId"]}/{pos_side(p)}': abs(int(p["quantity"]))
                    for p in c.positions() if abs(p["quantity"]) > 0}
            grew = [x for x, q in snap.items() if q > last_seen.get(x, 0)]
            if force or grew:
                if grew and not force:
                    print(f"[rearm] new fills on {grew}; posting exits", flush=True)
                unwind(c, args.live, min_edge=args.min_exit_edge,
                       reprice_ticks=args.reprice_ticks)
            last_seen = snap
        except (ApiError, OSError) as e:
            print(f"[exits] failed: {type(e).__name__}: {e}", flush=True)
        finally:
            unwind_lock.release()

    def exit_loop() -> None:
        while not _stop:
            rearm(force=True)
            for _ in range(int(max(1.0, args.unwind_interval))):
                if _stop:
                    return
                time.sleep(1)

    if args.unwind:
        threading.Thread(target=exit_loop, name="exits", daemon=True).start()
        print(f"exit thread started, every {args.unwind_interval:.0f}s")

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
                    mirror_frac=args.mirror,
                )
                show(plans, stats, args.live)
                res = apply_plan(c, plans, live=args.live, shrink_tol=args.shrink_tol,
                                 after_write=rearm if args.unwind else None,
                                 held=(lambda: {k.split("/")[0] for k in last_seen}) if args.unwind else None)

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
            # Never exit on transient failures. A platform 500-storm once took the
            # API down for hours; giving up after 15 errors left us offline for 8
            # hours while the ladders sat unmanaged. Back off hard and keep waiting.
            if consecutive_errors in (5, 15, 30) or consecutive_errors % 60 == 0:
                print(f"!! {consecutive_errors} consecutive failures — still retrying "
                      f"(API may be down; resting orders are unaffected)", flush=True)
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
