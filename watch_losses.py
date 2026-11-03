#!/usr/bin/env python3
"""Alert on losing trades and positions going underwater. Prints ALERTS only.

Every --interval seconds:
  * newest transactions: any SELL below the position's average cost, or a
    conversion (BUY of the opposite side) that realises below cost -> LOSS
  * open positions: side's best bid more than --underwater below avg cost
    -> UNDERWATER (repeated at most every 30 min per position)
  * quoter process missing -> QUOTER DOWN

Conventions as in analyze_realized.py: `quantity` signed by side (negative =
NO), `price` in that side's terms. Average cost comes from the positions
endpoint, refreshed every cycle and remembered after a position closes.

About 2 + (open positions) reads per cycle; run alongside the quoter.
    python3 -u watch_losses.py --interval 180
"""
from __future__ import annotations

import argparse
import json
import subprocess
import time

from sigcup.client import SuperMarketClient, _Budget, load_env


_seen_stopped: dict[str, float] = {}  # exid -> last time seen stopped (tiny stops never hit no_ladder)


def intended_cuts() -> set[str]:
    """Exchanges we are deliberately exiting below cost: an exit_floor override or
    a triggered stop-loss. Their below-cost fills are expected, not alarms."""
    cuts: set[str] = set()
    try:
        with open("data/overrides.json") as f:
            ov = json.load(f)
            cuts |= {str(k) for k in (ov.get("exit_floor") or {})}
            # no_ladder survives the position closing (stop_state is cleared on
            # close), so final fills of a stop-loss are still recognised as cuts.
            cuts |= {str(k) for k in (ov.get("no_ladder") or [])}
            # Re-admitted after the stop closed: last fills may still be in view.
            cutoff = time.time() - 6 * 3600
            for k, ts in (ov.get("recent_cuts") or {}).items():
                try:
                    from datetime import datetime
                    if datetime.fromisoformat(ts).timestamp() >= cutoff:
                        cuts.add(str(k))
                except ValueError:
                    pass
    except (OSError, ValueError):
        pass
    try:
        with open("data/stop_state.json") as f:
            for k, v in json.load(f).items():
                if v.get("stopped"):
                    _seen_stopped[k.split("/")[0]] = time.time()
    except (OSError, ValueError):
        pass
    # A stop's last fills can land after its state is cleared on close. A tiny
    # stop is never in no_ladder/recent_cuts, so remember it here for 6h (ex890,
    # 2026-10-11: its final 7,485 @0.095 was flagged LOSS SELL instead of a cut).
    cuts |= {x for x, t0 in _seen_stopped.items() if time.time() - t0 < 6 * 3600}
    return cuts


def now_ok(memo: dict, key, every: float = 1800) -> bool:
    """True at most once per `every` seconds for `key` (alert de-duplication)."""
    t = time.time()
    if t - memo.get(key, 0) > every:
        memo[key] = t
        return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=180)
    ap.add_argument("--underwater", type=float, default=0.015, help="alert when bid < avg - this")
    args = ap.parse_args()
    load_env()
    c = SuperMarketClient()
    c._reads = _Budget(15, headroom=0)
    book: dict[tuple[str, str], list] = {}  # (exid, side) -> [qty held, running avg cost]
    cut_tally: dict[str, list] = {}
    seen: set[str] = set()
    last_uw: dict[tuple[str, str], float] = {}
    first = True
    while True:
        try:
            if subprocess.run(["pgrep", "-f", "Python -u run_quoter.py"],
                              capture_output=True).returncode != 0:
                print(f"{time.strftime('%H:%M:%S')} QUOTER DOWN", flush=True)
            positions = c.positions()
            for p in positions:
                side = "no" if p["quantity"] < 0 else "yes"
                k = (str(p["exchangeId"]), side)
                # Seed only positions the stream hasn't built (opened before we
                # started, or missed): the platform's avgCost is sticky across a
                # position closing and reopening (ex898, 2026-10-07: reported 0.202
                # for a fresh 381 bought @0.195 and sold @0.200 -> false LOSS SELL;
                # ex1075 the same). Our own running average resets at zero.
                q = abs(int(p["quantity"]))
                if book.get(k, [0, 0])[0] <= 0:
                    book[k] = [q, float(p["avgCost"])]
                else:
                    # Never believe we hold MORE than the exchange says: a missed
                    # sell (100-row page overflow, read lag) would otherwise leave
                    # phantom old shares blended into the next lot's cost (ex1042,
                    # 2026-10-07: ~1,400 phantom @0.09 made a 0.080 -> 0.085 exit
                    # look like a loss).
                    book[k][0] = min(book[k][0], q)
            live = {(str(p["exchangeId"]), "no" if p["quantity"] < 0 else "yes") for p in positions}
            for k in book:
                if k not in live:
                    book[k][0] = 0  # closed on the exchange
            # Page back (newest first) until we reach a trade already seen. One
            # 100-row page per cycle missed buys during a fast fill (ex1069,
            # 2026-10-09: 23,859 NO filled in many lots; the running average saw
            # only the dearer ones, 0.895 vs the true 0.880, and flagged a +245
            # exit at 0.890 as a loss). First cycle: one page as the baseline.
            rows = []
            for i, t in enumerate(c._paged(f"/tournaments/{c.tournament_slug}/portfolio/transactions",
                                           limit=100)):
                tid0 = str(t.get("event_id") or (t["createdAt"], t["exchangeId"], t["quantity"]))
                if tid0 in seen or (first and i >= 100) or i >= 2000:
                    break
                rows.append(t)
            rows.reverse()
            for t in rows:
                tid = str(t.get("event_id") or (t["createdAt"], t["exchangeId"], t["quantity"]))
                if tid in seen or t.get("event_type") != "trade":
                    continue
                seen.add(tid)
                if first:
                    continue  # baseline: the positions seed above already includes these
                x = str(t["exchangeId"]); q = int(t["quantity"]); px = float(t["price"])
                side = "no" if q < 0 else "yes"; other = "yes" if side == "no" else "no"
                k = (x, side); n = abs(q)
                if t["orderType"] == "BUY":
                    held, a0 = book.get(k, [0, 0.0])
                    if held <= 0:
                        held, a0 = 0, 0.0
                    book[k] = [held + n, (held * a0 + n * px) / (held + n)]
                    oth = book.get((x, other))
                    if oth and oth[0] > 0 and 1 - px - oth[1] < -1e-9:
                        print(f"{t['createdAt'][11:19]} LOSS CONVERSION ex {x}: bought {side.upper()} "
                              f"@ {px:.3f} against {other.upper()} avg {oth[1]:.3f} "
                              f"({(1 - px - oth[1]) * n:+,.0f})", flush=True)
                    continue
                if t["orderType"] != "SELL" or k not in book:
                    continue
                held, a = book[k]
                book[k] = [max(0, held - n), a]
                if px < a - 1e-9:
                    if x in intended_cuts():
                        # A cut we chose (exit_floor override or triggered stop-loss):
                        # summarise every 30 min instead of alerting per fill.
                        agg = cut_tally.setdefault(x, [0, 0.0])
                        agg[0] += abs(q); agg[1] += (px - a) * abs(q)
                        continue
                    print(f"{t['createdAt'][11:19]} LOSS SELL ex {x} {side.upper()} {abs(q):,} @ {px:.3f} "
                          f"< avg {a:.3f} ({(px - a) * abs(q):+,.0f})", flush=True)
            first = False
            now = time.time()
            if cut_tally and now_ok(last_uw, ("cutsummary",)):
                for x, (n, pnl) in cut_tally.items():
                    if n < 100 and abs(pnl) < 5:
                        continue  # a few stray shares on a slow stop: not worth an alert
                    print(f"{time.strftime('%H:%M:%S')} STOP-LOSS SELLS (intended) ex {x}: "
                          f"{n:,} shares in the last 30 min, {pnl:+,.0f}", flush=True)
                cut_tally.clear()
            for p in positions:
                x = str(p["exchangeId"]); side = "no" if p["quantity"] < 0 else "yes"
                # Same lag-safe reading as the quoter: an exit filling between reads
                # shrinks `offered` before the positions count catches up, so trust
                # the lower of positions and the fills ledger (ex1065 false alarm).
                try:
                    from run_quoter import fills_net
                    led = fills_net(c, x); led = led if side == "yes" else -led
                    held = max(0, min(abs(int(p["quantity"])), led))
                except Exception:  # noqa: BLE001
                    held = abs(int(p["quantity"]))
                offered = sum(int(o["quantity"]) for o in c.open_orders(exchangeId=x)
                              if o.get("action") == "sell" and o.get("side") == side)
                # A position is "short of exits" only if it stays short across two
                # cycles: the quoter legitimately pauses a few seconds mid-reprice.
                k = ("short", x, side)
                if held - offered >= 10:  # the quoter skips top-ups under 10 shares
                    # Must persist 3 cycles (~9 min): an exit filling between reads
                    # briefly shows offered < a positions count that hasn't caught up.
                    streak = last_uw.get(k, 0) + 1
                    if streak >= 3 and now_ok(last_uw, ("shortmsg", x, side)):
                        print(f"{time.strftime('%H:%M:%S')} EXIT SHORT ex {x} {p['marketTitle'][:44]} "
                              f"held {held:,} {side.upper()} but only {offered:,} offered "
                              f"(for {streak} checks)", flush=True)
                    last_uw[k] = streak
                else:
                    last_uw.pop(k, None)
                b = c.exchange_orderbook(x, depth=1)
                bid = b.get("bestBid") if side == "yes" else (
                    round(1 - b["bestAsk"], 3) if b.get("bestAsk") is not None else None)
                a = float(p["avgCost"])
                # A stopped-out position is being sold on purpose; its fills are
                # summarised as STOP-LOSS SELLS, so don't repeat UNDERWATER for it.
                if (bid is not None and bid < a - args.underwater and x not in intended_cuts()
                        and now - last_uw.get((x, side), 0) > 1800):
                    last_uw[(x, side)] = now
                    q = abs(p["quantity"])
                    print(f"{time.strftime('%H:%M:%S')} UNDERWATER ex {x} {p['marketTitle'][:44]} "
                          f"{q:,} {side.upper()} avg {a:.3f} bid {bid:.3f} ({(bid - a) * q:+,.0f} if sold now)",
                          flush=True)
        except Exception as e:  # noqa: BLE001 - a watcher must keep watching
            print(f"{time.strftime('%H:%M:%S')} watcher read failed: {type(e).__name__}: {str(e)[:80]}",
                  flush=True)
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
