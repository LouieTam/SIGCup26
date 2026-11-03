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
from sigcup.quoter import MIRROR_RUNGS, apply_plan, current_ladder, plan_races

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
            if q.side == "no" and not p.mirror:
                print(f"      CHEAP NO {q.price:<6} x {q.quantity:>8,} sh = {q.notional:>8,.0f} "
                      f"(YES offer {1 - q.price:.3f}, {(1 - q.price - p.fair) / 0.005:+.0f} ticks vs fair)")
                continue
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
            try:
                o = c.order(str(oid))
            except ApiError as e:
                # GET /orders/{id} has been returning 503 for stretches. Without
                # a direction the ledger cannot be summed; report it as
                # unavailable (0), which the callers already treat as "stalled".
                print(f"  ex {exid}: order {oid} lookup failed ({e.code}); ledger unavailable",
                      flush=True)
                return 0
            o = o.get("data", o)
            _order_dir[oid] = (o["action"], o["side"])
        action, _ = _order_dir[oid]
        q = abs(int(f["quantity"]))
        sign = 1 if action == "buy" else -1
        net += sign * q if f["side"] == "yes" else -sign * q
    return net


# (exid, side) -> monotonic time we last placed or cancelled a sell there. Seeded
# lazily to process start, so a fresh process is conservative for a while.
_last_sell_activity: dict[tuple[str, str], float] = {}
# (exid, side) -> server-time ISO up to which our exits were sized from a real
# position read. A reprice carry-over re-offers old shares without looking at
# newer lots, so it must NOT advance this. Fresh exits and top-ups do.
_covered_until: dict[tuple[str, str], str] = {}
# (exid, side) -> the raw position record from held_qty's read (for its lots).
_last_position: dict[tuple[str, str], dict | None] = {}


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def new_lots_qty(rec: dict | None, side: str, after: str) -> int:
    """Shares in lots opened after `after` (an exit's createdAt, server clock).

    Exits are sized from the position as it stood when they were posted, so a lot
    opened later is not covered by any resting exit. Our sells consume the OLDEST
    lots first (FIFO), so these newer lots are still intact. Works when the
    fills ledger is stalled, because it only needs the positions record."""
    if not rec:
        return 0
    cut = _ts(after)
    return sum(abs(int(l["quantity"])) for l in rec.get("lots") or []
               if str(l.get("side", "")).lower() == side and _ts(l["openedAt"]) > cut)
_STARTED = time.monotonic()
QUIET_S = 600.0


def _touch_sells(exid: str, side: str) -> None:
    _last_sell_activity[(exid, side)] = time.monotonic()


def held_qty(c: SuperMarketClient, exid: str, side: str = "yes",
             resting: int | None = None) -> tuple[int, int, int]:
    """(held, positions qty, fills-ledger qty) of `side` shares.

    Normally `held` = min(positions, ledger): either source running stale-high
    oversells us into the opposite side, so only a reading both agree on is
    trusted.

    Exception: the positions endpoint only runs stale-HIGH right after OUR
    exit sells fill, because exits are the only sells we make. With nothing
    resting (`resting == 0`), and no sell placed, cancelled or even SEEN resting
    on this exchange side for QUIET_S, no exit of ours can have just filled, so
    positions is trusted over a lower ledger. Without this, a stalled ledger blocks exits outright:
    ex963, 2026-10-02 ~23:00Z, the ledger stuck at 21:27 while 6,366 NO sat
    unprotected.
    """
    rec = next((p for p in c.positions()
                if str(p["exchangeId"]) == exid and pos_side(p) == side), None)
    pos = abs(int(rec["quantity"])) if rec else 0
    _last_position[(exid, side)] = rec
    net_yes = fills_net(c, exid)
    led = net_yes if side == "yes" else -net_yes
    quiet = time.monotonic() - _last_sell_activity.get((exid, side), _STARTED) >= QUIET_S
    if resting == 0 and quiet and pos > led:
        return pos, pos, led
    return max(0, min(pos, led)), pos, led


# (exid, side) -> our highest resting BUY price on that side, from the last read.
_own_bid: dict[tuple[str, str], float | None] = {}


def resting_sells(c: SuperMarketClient, exid: str, side: str = "yes") -> list[dict]:
    orders = c.open_orders(exchangeId=exid)
    _remember(orders)
    buys = [float(o["priceLimit"]) for o in orders
            if o.get("open") and o.get("action") == "buy" and o.get("side") == side
            and o.get("priceLimit") is not None]
    _own_bid[(exid, side)] = max(buys) if buys else None
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
    live_keys = {f"{x}/{sd}" for x, sd in candidates}
    for k in [k for k in _underwater if k not in candidates]:
        _underwater.pop(k, None)
    if any(k not in live_keys for k in _stop_state):
        readmit = []
        for k in [k for k in _stop_state if k not in live_keys]:
            # Position closed: its stop clock no longer applies.
            if _stop_state.pop(k).get("stopped"):
                readmit.append(k.split("/")[0])
        _save_json(STOP_STATE_PATH, _stop_state)
        if readmit:
            readmit_races(readmit)
    if not candidates:
        print("  no positions")
        return

    for (exid, side), p in candidates.items():
        try:
            _unwind_one(c, live, exid, side, p, min_edge, reprice_ticks, min_topup)
        except Exception as e:  # noqa: BLE001 - one market must not block the others
            print(f"  ex {exid}/{side}: exit pass failed: {type(e).__name__}: {e}", flush=True)
    if not live:
        print("  DRY RUN — no exits placed.")


# ---------------------------------------------------------------- overrides / stops
# data/overrides.json, re-read every pass (no restart needed):
#   {"exit_floor": {"936": 0.80}, "no_ladder": ["936"]}
# exit_floor replaces the cost floor for that exchange's exit (to cut a loss).
# no_ladder removes the race containing that exchange from both ladders.
OVERRIDES_PATH = "data/overrides.json"
STOP_STATE_PATH = "data/stop_state.json"
STOP = {"coherent_h": 3.0, "max_h": 8.0, "coherence": 0.03,  # set from CLI in main()
        # 2026-10-11 (user-approved fixes after the RI Gov -1,745 / Illinois / CO-04 stops):
        "slice_cap": 10_000,   # max shares sold INTO the bid per exit pass once stopped
        "tiny_loss": 50.0,     # a stop losing less than this at the bid keeps its race laddered
        "addcap_ticks": 2}     # no new buys on a position this many ticks under cost
# (exid, side) -> best bid, for positions >= STOP["addcap_ticks"] ticks under cost.
# Written by the exit thread, read by the ladder thread before posting rungs.
_underwater: dict[tuple[str, str], float] = {}
_race_legs: dict[str, list[str]] = {}  # exid -> every exid in its race
_overrides_good: dict = {}


def load_overrides() -> dict:
    global _overrides_good
    try:
        with open(OVERRIDES_PATH) as f:
            _overrides_good = json.load(f)
    except FileNotFoundError:
        _overrides_good = {}
    except (OSError, ValueError) as e:
        print(f"  overrides unreadable ({e}); keeping the last good copy", flush=True)
    return _overrides_good


def _save_json(path: str, obj) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1)
    os.replace(tmp, path)


def _load_stop_state() -> dict:
    try:
        with open(STOP_STATE_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


_stop_state: dict[str, dict] = _load_stop_state()


def readmit_races(exids: list[str]) -> None:
    """A stopped-out position has fully closed: put its race back on the ladders
    (user decision 2026-10-06). The stop-loss only removed the race so we would
    not keep buying into a falling position; once nothing is held, the ladder
    quotes around the NEW fair like any other race. The exchange is recorded in
    `recent_cuts` so watch_losses.py still recognises the stop's last fills as
    intended rather than alerting LOSS SELL."""
    ov = load_overrides()
    keep = {str(x) for x in ov.get("permanent_exclude", [])}  # never re-admitted
    exids = [x for x in exids if x not in keep]
    nl = [x for x in ov.get("no_ladder", []) if x not in exids]
    if len(nl) == len(ov.get("no_ladder", [])):
        return
    ov["no_ladder"] = nl
    now = datetime.now(timezone.utc).isoformat()
    ov["recent_cuts"] = {**(ov.get("recent_cuts") or {}), **{x: now for x in exids}}
    for x in exids:
        (ov.get("exit_floor") or {}).pop(x, None)
    _save_json(OVERRIDES_PATH, ov)
    print(f"  stop-loss closed on {', '.join('ex ' + x for x in exids)}: race(s) back on the ladders",
          flush=True)


def race_coherent(c: SuperMarketClient, exid: str) -> bool | None:
    """True if the race's leg mids sum to within STOP['coherence'] of 1, i.e.
    the market is consistent with itself, so a price that left us underwater is
    a genuine repricing rather than one leg pushed out of line. None if unknown."""
    legs = _race_legs.get(exid)
    if not legs or len(legs) < 2:
        return None
    total = 0.0
    for x in legs:
        b = c.exchange_orderbook(x, depth=1)
        if b.get("bestBid") is None or b.get("bestAsk") is None:
            return None
        total += (b["bestBid"] + b["bestAsk"]) / 2
    return abs(total - 1) <= STOP["coherence"]


def stop_check(c: SuperMarketClient, exid: str, side: str, bid: float | None, avg: float,
               label: str, qty: int | None = None, cost: float | None = None) -> bool:
    """Track how long a position has been underwater; return True once stopped.

    Stop when underwater >= STOP['coherent_h'] AND its race is coherent (a real
    change in probability), or >= STOP['max_h'] regardless. Last night's
    dislocations (RI/NH/CT/PA) reverted inside a few hours with incoherent legs;
    TX-28 drifted for 10h+ with legs summing to ~0.99. The clock persists across
    restarts in data/stop_state.json.
    """
    key = f"{exid}/{side}"
    st = _stop_state.get(key)
    # `avg` here is the exit FLOOR (cost + min_edge), not raw cost: a position is
    # only "recovered" once an exit could actually fill. Comparing with raw cost
    # let a one-tick flicker in a wide book reset the clock (ex941, 2026-10-04:
    # NO bid touched 0.895 = cost while the exit floor was 0.90).
    underwater = bid is not None and bid < avg - 1e-9
    if st and st.get("stopped"):
        # A tiny stop keeps its race laddered, so new fills can land before the
        # stopped position closes; they would then be sold with no cost floor
        # (ex947, 2026-10-11 23:0xZ: 30,880 sliced out, 3,296 refilled at 0.060
        # while still "stopped"). If the position GREW since the stop, treat it
        # as a fresh one: clear the stop, normal floor applies.
        if qty is not None and not st.get("excluded", True):
            low = min(st.get("min_qty", qty), qty)
            if qty > low:
                _stop_state.pop(key, None)
                _save_json(STOP_STATE_PATH, _stop_state)
                print(f"  ex {label}: new fills after a tiny stop ({low:,} -> {qty:,}); stop "
                      f"cleared, normal cost floor applies", flush=True)
                return False
            if st.get("min_qty") != low:
                st["min_qty"] = low
                _save_json(STOP_STATE_PATH, _stop_state)
        if bid is not None and not underwater:
            # Recovered: an exit at the normal floor could fill again. Clear the
            # stop and put the race back (Florida Gov R sat stopped and excluded
            # for 32h while back above cost, 2026-10-10/11).
            _stop_state.pop(key, None)
            _save_json(STOP_STATE_PATH, _stop_state)
            if st.get("excluded", True):
                readmit_races([exid])
            print(f"  ex {label}: STOP CANCELLED, bid {bid} back at the exit floor {avg:.3f}; "
                  f"normal exit restored", flush=True)
            return False
        return True
    if not underwater:
        if st:
            _stop_state.pop(key, None)
            _save_json(STOP_STATE_PATH, _stop_state)
        return False
    now = datetime.now(timezone.utc)
    if not st:
        _stop_state[key] = {"since": now.isoformat()}
        _save_json(STOP_STATE_PATH, _stop_state)
        return False
    hours = (now - _ts(st["since"])).total_seconds() / 3600
    if hours < STOP["coherent_h"]:
        return False
    coherent = race_coherent(c, exid) if hours < STOP["max_h"] else None
    if hours >= STOP["max_h"] or coherent:
        why = (f"underwater {hours:.1f}h >= {STOP['max_h']}h cap" if hours >= STOP["max_h"]
               else f"underwater {hours:.1f}h and race coherent (legs sum to 1 ± {STOP['coherence']})")
        loss = (cost - bid) * qty if (qty and cost is not None and bid is not None) else None
        tiny = loss is not None and loss < STOP["tiny_loss"]
        st["stopped"] = True
        st["stopped_at"] = now.isoformat()
        st["why"] = why
        st["excluded"] = not tiny
        if qty is not None:
            st["min_qty"] = qty
        _save_json(STOP_STATE_PATH, _stop_state)
        if not tiny:
            ov = load_overrides()
            nl = set(ov.get("no_ladder", []))
            if exid not in nl:
                ov["no_ladder"] = sorted(nl | {exid})
                _save_json(OVERRIDES_PATH, ov)
        print(f"  !! STOP-LOSS ex {label}: {why}. Cost floor dropped; "
              + (f"race removed from the ladders." if not tiny else
                 f"tiny (~{loss:,.0f} at the bid), race stays on the ladders."), flush=True)
        return True
    return False


def _unwind_one(c, live, exid, side, p, min_edge, reprice_ticks, min_topup) -> None:
    if True:
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
            return
        fair = ((bid + ask) / 2) if bid is not None else ask

        # Add-cap: a position this far under cost gets no new buys from the
        # ladders (main, mirror or cheap rungs on this exchange and side). RI Gov
        # Independent grew 29,297 -> 36,609 NO while underwater before its stop
        # (2026-10-10/11, -1,745 in all).
        if bid is not None and bid <= avg - STOP["addcap_ticks"] * TICK + 1e-9:
            _underwater[(exid, side)] = bid
        else:
            _underwater.pop((exid, side), None)

        improve = round_to_tick(ask - TICK)
        floor = round_to_tick(avg + min_edge)
        stopped = stop_check(c, exid, side, bid, floor, label,
                             qty=abs(int(p["quantity"])), cost=avg)
        if stopped:
            floor = TICK  # stopped out: no cost floor, follow the market down
        manual = (load_overrides().get("exit_floor") or {}).get(exid)
        if manual is not None:
            floor = round_to_tick(float(manual))  # an explicit override always wins
        target = max(improve, floor)
        crosses = bid is not None and target <= bid

        # 1. Sells first, then holdings (see docstring).
        existing = resting_sells(c, exid, side)
        # Never price the exit into our OWN bid. After a sweep eats the rest of
        # the book, one of our ladder rungs can become the best bid, and with a
        # one-tick spread `ask - TICK` is that bid. The engine then cancels the
        # exit as SelfTradePrevented (ex1074, 2026-10-03 02:2xZ: a 30,339 NO
        # exit died that way and the position sat unoffered). Rest one tick above
        # our highest bid instead.
        own = _own_bid.get((exid, side))
        if own is not None and target <= own + 1e-9:
            target = round_to_tick(own + TICK)
            crosses = bid is not None and target <= bid
        # Stopped: sell in SLICES. Each pass, at most the size showing at the best
        # bid (capped) is sold into it; the rest rests one tick above the bid.
        # Before this, a one-tick spread put the whole position into the bid at
        # once and walked the book (RI Gov 19,254 @~0.89, CO-04 14,851 @0.65,
        # Illinois 41,148 @0.075). With a wide spread the old exit instead sat
        # at the ask for days (Georgia D, 44h); the slice keeps it moving.
        slice_q = 0
        if stopped and manual is None and bid is not None and not (own is not None and own >= bid - 1e-9):
            lv = (book.get("bids") if side == "yes" else book.get("asks")) or []
            slice_q = min(int(lv[0]["quantity"]) if lv else 0, STOP["slice_cap"])
            if slice_q > 0:
                if target <= bid + 1e-9:
                    target = round_to_tick(bid + TICK)
                crosses = False
        carry = 0  # shares cancelled for repricing, re-offered at the new price
        key = (exid, side)
        # Re-price stale exits. Cancel by order id, never cancel-all — that would
        # also wipe the sweep ladder resting on the same exchange.
        stale = [o for o in existing
                 if o.get("priceLimit") is not None
                 and abs(float(o["priceLimit"]) - target) > reprice_ticks * TICK]
        if slice_q > 0 and existing:
            stale = list(existing)  # re-split every pass: next slice + the rest
        if stale:
            print(f"  ex {label}: repricing {len(stale)} stale exit(s) "
                  f"{[o['priceLimit'] for o in stale]} -> {target}")
            if not live:
                existing = [o for o in existing if o not in stale]
            else:
                if key not in _covered_until and stale:
                    # The carried shares were covered as of the oldest stale exit.
                    _covered_until[key] = min(o["createdAt"] for o in stale)
                for o in stale:
                    try:
                        c.cancel(str(o["id"]))
                        _touch_sells(exid, side)
                        carry += int(o["quantity"])  # remaining at the pre-cancel read
                    except ApiError as e:
                        print(f"    cancel {o['id']}: {e.code}")
                # Fills may have landed up to the moment of cancel: give the
                # positions endpoint a moment to reflect them, then re-read.
                if carry:
                    time.sleep(3)
                existing = resting_sells(c, exid, side)
        resting = sum(int(o["quantity"]) for o in existing)
        if resting:
            # Seeing an exit resting counts as sell activity too. An exit posted
            # long ago that fills completely leaves nothing resting while positions
            # may still read high, so the quiet window must run from the last
            # time an exit was seen, not just from when we placed it.
            _touch_sells(exid, side)

        # 2. Holdings, read after the sells.
        held, pos_q, led_q = held_qty(c, exid, side, resting)
        uncovered = held - resting
        if resting and uncovered < 0 or (resting and uncovered < min_topup):
            # Top-up from lots. Under a stalled ledger min() reads ~0, so new
            # fills on an exchange that already has an exit were never added
            # (ex1051, 2026-10-03 ~02:00Z, 9,147 NO left unoffered).
            newest = _covered_until.get(key) or max(o["createdAt"] for o in existing)
            fresh = new_lots_qty(_last_position.get((exid, side)), side, newest)
            fresh = min(fresh, max(0, pos_q - resting))
            if fresh >= min_topup:
                print(f"  ex {label}: {fresh:,} in lots opened after the newest exit "
                      f"({newest[11:19]}) are uncovered; topping up")
                uncovered = fresh
        if carry and uncovered < carry:
            # Reprice carry-over. The cancelled shares were legitimately offered
            # a moment ago, so re-offer them even when min(positions, ledger)
            # cannot confirm it (a stalled ledger reads 0). Capped by positions
            # re-read after the cancel, in case part of the old order filled just
            # before the cancel landed. Without this, every reprice under a
            # stalled ledger deleted the exit: ex1077, 2026-10-02 ~23:40Z,
            # 16,908 NO left unoffered.
            allowed = max(0, min(carry, pos_q - resting))
            if allowed > uncovered:
                print(f"  ex {label}: re-offering {allowed:,} carried from the reprice "
                      f"(positions {pos_q:,}, ledger {led_q:,})")
                uncovered = allowed
        if pos_q != led_q:
            why = (" (no sell activity here for 10 min, so positions cannot be stale-high)"
                   if held == pos_q and pos_q > led_q else "")
            print(f"  ex {label}: positions {pos_q:,} vs fills ledger {led_q:,} disagree "
                  f"— using {held:,}{why}")
        if uncovered < 0:
            if max(pos_q, led_q) >= resting:
                # Only one source reads low; most likely it is lagging a buy.
                print(f"  ex {label}: offered {resting:,}, sources read {pos_q:,}/{led_q:,}"
                      f" — waiting for them to agree")
                return
            # The usual cause is benign: an exit filled between the sells read and
            # the holdings read (by design that order errs this way). Re-read the
            # sells; a fill now lowers `resting`, so only a real over-offer survives.
            # (False alarm seen live on ex870, 2026-10-02 18:55Z: 579 shares sold
            # between the reads, and the old code cancelled a working exit.)
            existing = resting_sells(c, exid, side)
            resting = sum(int(o["quantity"]) for o in existing)
            # Compare with the same yardstick as the first check. `held` is ~0
            # under a stalled ledger, which turned an ordinary mid-read fill into
            # a cancel of a 52,536 NO exit (ex1033, 2026-10-03 ~02:10Z).
            if resting <= max(pos_q, led_q):
                print(f"  ex {label}: offer {resting:,} vs held {held:,} after re-read; "
                      f"an exit was filling mid-read, nothing to do")
                return
            # Both agree we offered more than we hold. The excess would be
            # canonicalized into a buy of the other side if it filled, so pull the
            # exits; the next pass re-posts at the right size.
            print(f"  ex {label}: OVER-OFFERED {resting:,} vs held {held:,}; cancelling exits")
            if live:
                for o in existing:
                    try:
                        c.cancel(str(o["id"]))
                        _touch_sells(exid, side)
                    except ApiError as e:
                        print(f"    cancel {o['id']}: {e.code}")
            return
        if uncovered == 0:
            print(f"  ex {label}: {held:,} held, fully offered")
            return
        if resting > 0 and uncovered < min_topup:
            print(f"  ex {label}: {uncovered:,} uncovered below top-up minimum {min_topup}; "
                  f"leaving for the next reprice")
            return

        tag = "CROSSES now" if crosses else "rests as best offer"
        if target > ask:
            tag = "rests ABOVE market (optionality; floor is above the ask)"
        kind = f"TOP-UP ({resting:,} already offered)" if resting else "exit"
        print(f"  ex {label} held {held:,} avg {avg:.3f} bid {bid} ask {ask} fair {fair:.3f}"
              f" -> {kind} {uncovered:,} @ {target} ({tag}), "
              f"edge {(target - avg) * uncovered:+,.0f}")
        if live and slice_q > 0:
            _touch_sells(exid, side)
            s_q = min(slice_q, uncovered)
            r = c.place_order(exid, side, "sell", s_q, bid)
            print(f"    stop slice: {s_q:,} into the bid @ {bid} (top of book {slice_q:,}): traded "
                  f"{r.get('quantityTraded') or 0:,} open={r.get('open')}")
            rest_q = uncovered - s_q
            if rest_q > 0:
                r = c.place_order(exid, side, "sell", rest_q, target)
                print(f"    stop rest: {rest_q:,} @ {target} open={r.get('open')}")
            _covered_until[key] = datetime.now(timezone.utc).isoformat()
            return
        if live:
            _touch_sells(exid, side)
            carried_only = carry and uncovered <= carry and held < uncovered
            r = c.place_order(exid, side, "sell", uncovered, target)
            if not carried_only:
                # Sized from a real position read (fresh exit or top-up).
                _covered_until[key] = datetime.now(timezone.utc).isoformat()
            print(f"    placed: traded {r.get('quantityTraded') or 0:,} @ "
                  f"{r.get('fillPrice')} open={r.get('open')} "
                  f"terminalReasonCode={r.get('terminalReasonCode')}")
            if r.get("terminalReasonCode") == "SelfTradePrevented":
                print(f"  !! ex {label}: exit cancelled by self-trade prevention; "
                      f"own bid {own}, target {target}", flush=True)


def apply_addcap(c: SuperMarketClient, plans, live: bool, done: set) -> None:
    """Drop and cancel buy rungs that would add to a position already
    STOP['addcap_ticks'] ticks under cost (see _underwater). `done` remembers
    which (exid, side) were already cancelled, so each is pulled once; a key that
    recovers leaves `done` and its rungs come back on the next post."""
    under = set(_underwater)
    for pl in plans:
        keep = [q for q in pl.quotes if (str(q.exchange_id), q.side) not in under]
        if len(keep) != len(pl.quotes):
            pl.quotes = keep
    for exid, side in sorted(under - done):
        n = 0
        if live:
            for o in c.open_orders(exchangeId=exid):
                if o.get("open") and o.get("action") == "buy" and o.get("side") == side:
                    try:
                        c.cancel(str(o["id"]))
                        n += 1
                    except ApiError as e:
                        print(f"   add-cap cancel {o['id']}: {e.code}")
        print(f"   ex {exid}/{side.upper()}: >= {STOP['addcap_ticks']} ticks under cost (bid "
              f"{_underwater.get((exid, side))}); {n} buy rung(s) pulled, no adds until it recovers")
        done.add((exid, side))
    done.intersection_update(under)


def pull_orphans(c: SuperMarketClient, plans, scope_ex: set[str], live: bool) -> int:
    """Cancel buy rungs on in-scope exchanges that no plan quotes any more.

    apply_plan only touches exchanges with quotes, so a race that drops out
    (favourite below --min-fair, incoherent legs, cheap rungs over the cap) kept
    its old rungs resting indefinitely: 18 cheap NO bids at 0.245-0.285 were
    found on ex1008/1010/885/897 on 2026-10-21, their favourites down at ~0.70.
    Exits are sells and are never touched: where one rests, buys go by id.
    """
    desired = {str(p.leg.exchange_id) for p in plans if p.quotes}
    n = 0
    for exid, buys in current_ladder(c).items():
        if exid not in scope_ex or exid in desired:
            continue
        print(f"   ex {exid}: {len(buys)} rung(s) no longer planned; pulling", flush=True)
        if not live:
            continue
        c.low_priority.on = True
        try:
            if any(o.get("action") == "sell" for o in c.open_orders(exchangeId=exid)):
                for o in buys:
                    try:
                        c.cancel(str(o["id"]))
                        n += 1
                    except ApiError as e:
                        print(f"    cancel {o['id']}: {e.code}")
            else:
                c.cancel_all(exchange_id=exid)
                n += 1
        finally:
            c.low_priority.on = False
    return n


def outage_pull(c: SuperMarketClient, plans, held_ex: set[str]) -> int:
    """Outage safe-mode: cancel our resting BUY rungs, keep every exit.

    2026-10-11: during a 503 outage (15:03-15:18Z) the ladders could not be
    re-anchored while RI Senate D's YES rose 0.85 -> 0.865; stale cheap rungs
    sat a tick from the market and bought 63K NO (stop: -487), and NY Gov filled
    36K at 0.895 on the first pass back (-1,053). Only the balance read was
    failing; order writes worked. Exchanges we hold nothing on get one
    cancel-all (it would also wipe exits, so never where we hold); held ones are
    cancelled buy-by-buy. Low priority, so exits keep their write slots."""
    n = 0
    c.low_priority.on = True
    try:
        for x in sorted({str(p.leg.exchange_id) for p in plans if p.quotes}):
            try:
                if x in held_ex:
                    for o in c.open_orders(exchangeId=x):
                        if o.get("open") and o.get("action") == "buy":
                            c.cancel(str(o["id"]))
                            n += 1
                else:
                    c.cancel_all(exchange_id=x)
                    n += 1
            except Exception as e:  # noqa: BLE001 - network errors too (URLError), never escape
                print(f"   outage pull ex {x}: {getattr(e, 'code', type(e).__name__)}", flush=True)
    finally:
        c.low_priority.on = False
    return n


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
    ap.add_argument("--stop-hours", type=float, default=3.0,
                    help="stop-loss: exit below cost once underwater this long AND the race's "
                         "legs are coherent (a real repricing)")
    ap.add_argument("--stop-max-hours", type=float, default=8.0,
                    help="stop-loss: exit below cost once underwater this long, regardless")
    ap.add_argument("--stop-coherence", type=float, default=0.03,
                    help="legs whose mids sum within this of 1 count as coherent")
    ap.add_argument("--cheap", type=float, default=0.0,
                    help="also rest NO bids on each favourite k ticks above fair (YES "
                         "offers above the ask), sized at this fraction of the race "
                         "budget (0 = off; see docs/leader-analysis.md)")
    ap.add_argument("--cheap-max-no", type=float, default=0.10,
                    help="cheap side: only NO bids strictly below this price")
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
    STOP.update(coherent_h=args.stop_hours, max_h=args.stop_max_hours,
                coherence=args.stop_coherence)
    for legs in races.values():
        xs = [str(l["exchangeId"]) for l in legs]
        for x in xs:
            _race_legs[x] = xs
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
        except Exception as e:  # noqa: BLE001 - the exit thread must never die
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

    pulled: set[str] = set()  # no_ladder exchanges whose rungs we already cancelled
    capped: set[tuple[str, str]] = set()  # add-capped (exid, side) whose rungs we already cancelled
    npass = 0
    consecutive_errors = 0
    outage_pulled = False
    last_plans: list = []  # last successfully built plans: which exchanges carry rungs
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
            ov = load_overrides()
            no_ladder = {str(x) for x in ov.get("no_ladder", []) + ov.get("permanent_exclude", [])}
            races_now = {rid: legs for rid, legs in races.items()
                         if not any(str(l["exchangeId"]) in no_ladder for l in legs)}
            # Pull rungs already resting on excluded races (by id: exits stay).
            excluded = {y for x in no_ladder for y in _race_legs.get(x, [x])}
            pulled &= excluded  # a re-admitted race must be pulled again if it is re-excluded
            for x in sorted(excluded - pulled):
                if args.live:
                    for o in c.open_orders(exchangeId=x):
                        if o.get("open") and o.get("action") == "buy":
                            try:
                                c.cancel(str(o["id"]))
                            except ApiError as e:
                                print(f"   no_ladder cancel {o['id']}: {e.code}")
                print(f"   ex {x}: excluded by overrides; ladder rungs pulled")
                pulled.add(x)
            if not halted:
                plans, stats = plan_races(
                    c, races_now, cash,
                    min_fair=args.min_fair,
                    max_coherence_gap=args.coherence,
                    total_budget_pct=args.budget_pct,
                    per_race_pct=args.race_pct,
                    mirror_frac=args.mirror,
                    mirror_rungs=MIRROR_RUNGS,
                    cheap_frac=args.cheap, cheap_max_no=args.cheap_max_no,
                )
                last_plans = plans
                if args.unwind:
                    apply_addcap(c, plans, args.live, capped)
                show(plans, stats, args.live)
                res = apply_plan(c, plans, live=args.live, shrink_tol=args.shrink_tol,
                                 after_write=rearm if args.unwind else None,
                                 held=(lambda: {k.split("/")[0] for k in last_seen}) if args.unwind else None)
                scope_ex = {str(l["exchangeId"]) for legs in races_now.values() for l in legs}
                res["writes"] += pull_orphans(c, plans, scope_ex, args.live)

            open_orders = len(c.open_orders())
            print(f"\npass {npass}: {res['placed']} rungs, {res['writes']} writes, "
                  f"{res['unchanged']} unchanged"
                  + (f", !! {res['refused']} REFUSED" if res.get("refused") else "")
                  + f" | cash {cash:,.2f} "
                  f"deployed {deployed:,.0f}/{deploy_cap:,.0f} | {len(positions)} positions "
                  f"(basis {cost_basis:,.0f}) | {open_orders} resting | "
                  f"{time.time() - t0:.0f}s | uptime {(time.time() - started) / 3600:.2f}h",
                  flush=True)
            log.write(json.dumps({
                "ts": datetime.now(timezone.utc).isoformat(), "pass": npass,
                "cash": cash, "deployed": deployed, "costBasis": cost_basis,
                "positions": len(positions), "openOrders": open_orders,
                "halted": halted, "placed": res["placed"], "writes": res["writes"],
                "refused": res.get("refused", 0),
                "committed": stats.get("committed"), "racesQuoted": stats.get("races_quoted"),
            }) + "\n")
            consecutive_errors = 0
            outage_pulled = False  # next outage gets its own pull

        except Exception as e:  # noqa: BLE001 - nothing may end an unattended run
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
            if args.live and consecutive_errors >= 2 and not outage_pulled and last_plans:
                held_ex = {k.split("/")[0] for k in last_seen} if args.unwind else set()
                try:
                    n = outage_pull(c, last_plans, held_ex)
                except Exception as e2:  # noqa: BLE001 - the handler must never kill the run
                    # 2026-10-12 19:36Z: a DNS failure inside the pull escaped this
                    # except block and ended the process (supervisor relaunched it).
                    print(f"   outage pull failed: {type(e2).__name__}: {e2}", flush=True)
                    n = 0
                # Only done once something was actually cancelled: in the 19:40Z
                # outage the cancel endpoint itself returned INTERNAL_ERROR for
                # every exchange, and marking it done meant never retrying.
                outage_pulled = n > 0
                print(f"!! OUTAGE SAFE-MODE: {consecutive_errors} failed passes; "
                      + (f"{n} cancel(s) sent, buy rungs pulled, exits kept. Ladders re-post when a "
                         f"pass succeeds." if n else
                         "cancels FAILED too (platform rejecting writes); orders still resting, "
                         "retrying next failed pass."), flush=True)
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
