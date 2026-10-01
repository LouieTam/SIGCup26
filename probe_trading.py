#!/usr/bin/env python3
"""Answer the two §6 questions that need live orders. Requires a `trade` key.

  Q3  Do resting bids reserve capital (price x qty)?
      Rest a far-from-market bid, diff the balance, cancel.

  Q6  What price does the leaderboard mark positions at?
      Take a 1-share position, then compare `Position.currentPrice` against
      that exchange's bestBid / bestAsk / latestPrice.

Costs at most ~1 SUSQie of real exposure (one share). Prints the plan and
refuses to trade unless --yes is passed.

    python3 probe_trading.py                  # dry run, shows the plan
    python3 probe_trading.py --yes            # actually probe
    python3 probe_trading.py --yes --unwind   # also try to close the share
"""

from __future__ import annotations

import argparse
import sys
import time

from sigcup.catalog import load_catalog, select
from sigcup.client import ApiError, SuperMarketClient, best_levels, load_env

RESERVE_QTY = 1000
RESERVE_PRICE = 0.01  # 10 SUSQies notional — detectable, and far from any book


def h(t: str) -> None:
    print(f"\n{'=' * 70}\n{t}\n{'=' * 70}")


def pick_exchange(c: SuperMarketClient, prefer_state: str | None) -> dict:
    """A liquid-ish market to probe. Prefers the requested state."""
    rows = load_catalog()
    pool = select(rows, state=prefer_state) if prefer_state else []
    if not pool:
        pool = [r for r in rows if r.get("exchangeId")]
    pool.sort(key=lambda r: int(r["marketId"]))
    return pool[0]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true", help="actually place orders")
    ap.add_argument("--unwind", action="store_true", help="close the 1-share position afterwards")
    ap.add_argument("--state", default="RI", help="prefer a market in this state (default RI)")
    args = ap.parse_args()

    load_env()
    target = pick_exchange(SuperMarketClient(), args.state)
    exid, mid = target["exchangeId"], target["marketId"]

    print("PLAN")
    print(f"  market {mid} / exchange {exid}: {target.get('title')}")
    print(f"  Q3: buy YES {RESERVE_QTY} @ {RESERVE_PRICE} (resting, {RESERVE_QTY * RESERVE_PRICE:.2f} notional), "
          f"measure balance, cancel")
    print(f"  Q6: buy YES 1 @ market (<= 1.00 cost), compare currentPrice vs book, "
          f"{'then close it' if args.unwind else 'leave it open'}")
    if not args.yes:
        print("\nDry run. Re-run with --yes to execute.")
        return 0

    c = SuperMarketClient(allow_trading=True)

    # ---------------------------------------------------------------- Q3
    h("Q3 — do resting bids reserve capital?")
    before = c.balance()
    print(f"  balance before:      {before:,.4f}")

    order = c.place_order(exid, "yes", "buy", RESERVE_QTY, RESERVE_PRICE)
    oid = order.get("orderId")
    print(f"  placed order {oid}: open={order.get('open')} "
          f"traded={order.get('quantityTraded')} fillPrice={order.get('fillPrice')} "
          f"terminalReasonCode={order.get('terminalReasonCode')}")
    if order.get("quantityTraded"):
        print("  !! it filled — the balance diff below mixes a fill with any reservation.")

    time.sleep(2)
    after = c.balance()
    delta = before - after
    notional = RESERVE_QTY * RESERVE_PRICE
    print(f"  balance with it resting: {after:,.4f}   (delta {delta:+,.4f})")
    print(f"  expected if reserved:    {notional:,.4f}")
    if abs(delta - notional) < 0.01:
        verdict = "RESERVED — resting bids lock price x qty. Ladder notional is capped by cash."
    elif abs(delta) < 0.01:
        verdict = "NOT RESERVED — resting bids are free. The ladder can be arbitrarily deep."
    else:
        verdict = f"UNCLEAR — delta {delta:.4f} matches neither 0 nor {notional:.4f}."
    print(f"  >>> {verdict}")

    try:
        print(f"  collateral: {c._get('/portfolio/collateral', tournamentId=c.tournament_id)}")
    except ApiError as e:
        print(f"  collateral read: {e.code}")

    print(f"  cancelling… {c.cancel_all(exchange_id=exid)}")
    time.sleep(2)
    restored = c.balance()
    print(f"  balance after cancel: {restored:,.4f} (vs {before:,.4f} at start)")

    # ---------------------------------------------------------------- Q6
    h("Q6 — what price marks a position?")
    bk = c.exchange_orderbook(exid)
    px = c.exchange_price(exid)
    print(f"  book:  bestBid={bk['bestBid']} bestAsk={bk['bestAsk']} spread={bk['spread']}")
    print(f"  price: latestPrice={px['latestPrice']} bestBid={px['bestBid']} bestAsk={px['bestAsk']}")
    if bk["bestAsk"] is None:
        print("  no ask resting — cannot buy a share here. Re-run against another market.")
        return 1

    one = c.place_order(exid, "yes", "buy", 1)  # market order
    print(f"  bought 1 share: traded={one.get('quantityTraded')} fillPrice={one.get('fillPrice')} "
          f"terminalReasonCode={one.get('terminalReasonCode')}")
    time.sleep(3)

    pos = [p for p in c.positions() if str(p["exchangeId"]) == str(exid)]
    if not pos:
        print("  no position reported yet — reporting may lag; re-read in a moment.")
        return 1
    p = pos[0]
    cur = p["currentPrice"]
    bk2 = c.exchange_orderbook(exid)
    px2 = c.exchange_price(exid)
    print(f"  position: qty={p['quantity']} avgCost={p['avgCost']} currentPrice={cur} "
          f"marketValue={p['marketValue']} unrealizedPnl={p['unrealizedPnl']}")
    print(f"  book now: bestBid={bk2['bestBid']} bestAsk={bk2['bestAsk']} latestPrice={px2['latestPrice']}")

    candidates = {
        "latestPrice (last trade)": px2["latestPrice"],
        "bestAsk": bk2["bestAsk"],
        "bestBid": bk2["bestBid"],
    }
    if bk2["bestBid"] is not None and bk2["bestAsk"] is not None:
        candidates["mid"] = (bk2["bestBid"] + bk2["bestAsk"]) / 2
    matches = [k for k, v in candidates.items() if v is not None and abs(v - cur) < 1e-9]
    print(f"  currentPrice={cur} matches: {matches or 'NONE of ' + str(candidates)}")
    print(f"  >>> valuation_price appears to be: {matches[0] if matches else 'undetermined — inspect manually'}")

    if args.unwind:
        h("Unwinding the 1-share position")
        try:
            r = c.place_order(exid, "yes", "sell", 1)
            print(f"  sold: traded={r.get('quantityTraded')} fillPrice={r.get('fillPrice')} "
                  f"canonical={ {k: r.get(k) for k in ('side', 'action', 'price', 'quantity')} }")
        except ApiError as e:
            print(f"  could not close ({e.code}: {e.message}) — 1 share left open, harmless.")
        print(f"  final balance: {c.balance():,.4f}")

    h("Summary")
    print(f"  Q3: {verdict}")
    print(f"  Q6: valuation_price = {matches[0] if matches else 'undetermined'}")
    print("  Record both in docs/open-questions-answered.md.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ApiError as e:
        print(f"\nAPI ERROR {e.status} {e.code}: {e.message}\ndetails={e.details}")
        if e.code == "INSUFFICIENT_SCOPES":
            print("\n>>> This key lacks the `trade` scope. Create a new key with read+trade.")
        sys.exit(1)
