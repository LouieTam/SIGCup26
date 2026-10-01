#!/usr/bin/env python3
"""Read-only smoke test for the Super Market API client.

Places NO orders. Verifies: auth, tournament context, market list, one
orderbook, and the tournament balance / positions / P&L.

    python3 smoke_test.py
"""

import json
import sys

from sigcup.client import ApiError, SuperMarketClient, best_levels, load_env


def h(title: str) -> None:
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


def main() -> int:
    load_env()
    c = SuperMarketClient()  # allow_trading=False — writes are impossible here
    print(f"base url: {c.base_url}")

    h("1. GET /account — auth check")
    acct = c.account()
    print(f"  id        {acct['id']}")
    print(f"  username  {acct['username']}")
    print(f"  email     {acct['email']}")
    print(f"  balance   {acct['balance']}  (default-context balance)")

    h("2. GET /tournaments — which contexts can this key see?")
    tours = c.tournaments()
    rows = tours.get("data") if isinstance(tours, dict) else tours
    for t in rows or []:
        print(
            f"  {t['slug']:<24} {t.get('status', '?'):<10} "
            f"bal={t.get('myBalance', 0):>12,.2f} {t.get('currencyName', ''):<10} "
            f"init={t.get('initialBalance', 0):>10,.0f} pending={t.get('isPendingEnrolment')}"
        )

    h(f"3. GET /tournaments/{c.tournament_slug}")
    t = c.tournament()
    print(f"  id         {t['id']}")
    print(f"  name       {t['name']}")
    print(f"  status     {t['status']}")
    print(f"  window     {t['startDate']} -> {t['endDate']}")
    print(f"  myBalance  {t['myBalance']:,.2f} {t['currencyName']}")
    print(f"  joinedAt   {t.get('joinedAt')}  pendingEnrolment={t.get('isPendingEnrolment')}")
    extra = set(t) - {
        "id", "slug", "name", "description", "status", "startDate", "endDate",
        "initialBalance", "currencyName", "myBalance", "joinedAt", "isPendingEnrolment",
    }
    missing = {"joinedAt", "isPendingEnrolment"} - set(t)
    if extra or missing:
        print(f"  (schema drift: undocumented={sorted(extra)} absent={sorted(missing)})")

    h("4. Market list (tournament-scoped)")
    markets = c.tournament_markets()
    print(f"  {len(markets)} markets")
    multi = [m for m in markets if len(m.get("exchanges") or []) > 1]
    print(f"  {len(multi)} with more than one exchange (multi-outcome)")
    print("\n  first 15:")
    for m in markets[:15]:
        exs = m.get("exchanges") or []
        opts = ",".join(str(e.get("option")) for e in exs[:4])
        print(f"    id={str(m['id']):>5}  ex={len(exs)} [{opts}]  {m.get('title', '')[:68]}")

    h("5. One order book")
    target = next((m for m in markets if (m.get("exchanges") or [])), None)
    if target is None:
        print("  no market with an exchange found — skipping")
        return 1
    print(f"  market {target['id']}: {target.get('title')}")
    book = c.market_orderbook(target["id"], depth=20)
    print(f"  overround={book.get('overround')}  arb={book.get('hasArbitrageOpportunity')}")
    for ex in book.get("exchanges") or []:
        bb, ba = best_levels(ex)
        print(f"\n    exchange {ex['exchangeId']} option={ex.get('option')} "
              f"impliedProb={ex.get('impliedProbability')} asOf={ex.get('asOf')}")
        print(f"      bestBid={bb}  bestAsk={ba}")
        print(f"      asks (low->high): {[(l['price'], l['quantity']) for l in sorted(ex['asks'], key=lambda l: l['price'])[:6]]}")
        print(f"      bids (high->low): {[(l['price'], l['quantity']) for l in sorted(ex['bids'], key=lambda l: -l['price'])[:6]]}")

    ex0 = (book.get("exchanges") or [{}])[0].get("exchangeId")
    if ex0:
        h(f"5b. GET /exchanges/{ex0}/orderbook (single-exchange shape)")
        eb = c.exchange_orderbook(ex0, depth=10)
        print(f"  bestBid={eb['bestBid']} bestAsk={eb['bestAsk']} spread={eb['spread']} asOf={eb['asOf']}")

    h("6. Balance / positions / P&L")
    print(f"  tournament balance: {c.balance():,.2f}")
    pos = c.positions()
    print(f"  open positions: {len(pos)}")
    for p in pos[:10]:
        print(f"    ex={p['exchangeId']} qty={p['quantity']} avg={p['avgCost']} "
              f"px={p['currentPrice']} uPnl={p['unrealizedPnl']} {p['marketTitle'][:40]}")
    try:
        print(f"  pnl: {json.dumps(c.pnl())}")
    except ApiError as e:
        print(f"  pnl unavailable: {e}")
    orders = c.open_orders()
    print(f"  open orders: {len(orders)}")

    h("7. Write guard")
    try:
        c.place_order(ex0 or "1", "yes", "buy", 1, 0.01)
        print("  FAIL — a write got through with allow_trading=False")
        return 1
    except PermissionError as e:
        print(f"  OK — blocked: {e}")

    print("\nSmoke test complete. No orders were placed.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ApiError as e:
        print(f"\nAPI ERROR {e.status} {e.code}: {e.message}")
        if e.details:
            print(f"details: {json.dumps(e.details, indent=2)[:1500]}")
        sys.exit(1)
