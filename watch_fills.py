#!/usr/bin/env python3
"""Watch for fills in real time and alert on them. Read-only — places nothing.

Subscribes to the private `user:{profile_id}` channel and prints every fill the
instant it lands, with the one diagnostic that matters: whether the
complementary leg of the same race moved too.

    complement moved consistently  -> it was NEWS; the position is a real bet
    complement did NOT move        -> it was LIQUIDITY; expect reversion

That is the check docs/sweep-opportunity.md calls the most valuable post-fill
signal, and it is only actionable if we hear about the fill in seconds.

    python3 watch_fills.py
    python3 watch_fills.py --log data/fills.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timezone

from sigcup.catalog import group_by_race, load_catalog
from sigcup.client import ApiError, SuperMarketClient, load_env
from sigcup.realtime import RealtimeClient


def now() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default="data/fills.jsonl")
    args = ap.parse_args()
    load_env()
    # `trade` scope is needed only to mint the realtime token (a POST); this
    # script never places an order.
    c = SuperMarketClient(allow_trading=True)

    cat = [r for r in load_catalog() if r.get("exchangeId")]
    by_ex = {r["exchangeId"]: r for r in cat}
    races = group_by_race(cat)
    siblings: dict[str, list[dict]] = {}
    for rid, legs in races.items():
        for leg in legs:
            siblings[leg["exchangeId"]] = [l for l in legs if l["exchangeId"] != leg["exchangeId"]]

    logf = open(args.log, "a", buffering=1)
    token = c.realtime_token()
    topic = token["channels"]["user"]
    print(f"subscribing to {topic}")
    print("waiting for fills — Ctrl-C to stop\n")

    def diagnose(exid: str) -> str:
        """Compare the filled leg against its complement to classify the move."""
        try:
            me = c.exchange_price(exid)
            parts = []
            for s in siblings.get(exid, []):
                sp = c.exchange_price(s["exchangeId"])
                mid_me = (me["bestBid"] + me["bestAsk"]) / 2 if me["bestBid"] and me["bestAsk"] else None
                mid_s = (sp["bestBid"] + sp["bestAsk"]) / 2 if sp["bestBid"] and sp["bestAsk"] else None
                if mid_me is None or mid_s is None:
                    parts.append(f"{s.get('winnerName')}: one-sided book")
                    continue
                total = mid_me + mid_s
                parts.append(f"{s.get('winnerName')} mid {mid_s:.3f} (pair sum {total:.3f})")
            return "; ".join(parts) or "no sibling legs"
        except ApiError as e:
            return f"diagnosis unavailable ({e.code})"

    async def on_event(kind: str, payload: dict) -> None:
        if kind == "_status":
            print(f"[{now()}] status: {payload}")
            return
        if kind == "_resync":
            print(f"[{now()}] subscribed — REST is authoritative; reconcile if acting on this")
            return
        if kind != "account_batch":
            print(f"[{now()}] {kind}: {json.dumps(payload)[:200]}")
            return

        fills = payload.get("fills") or []
        orders = payload.get("orderUpdates") or []
        if orders and not fills:
            print(f"[{now()}] {len(orders)} order updates (no fills)")
        for f in fills:
            exid = str(f.get("exchangeId"))
            row = by_ex.get(exid, {})
            price, qty = f.get("price"), f.get("quantity")
            print(f"\n*** [{now()}] FILL  ex {exid}  {row.get('winnerName')} / "
                  f"{row.get('raceName')}")
            print(f"    {qty} sh @ {price}  (YES-denominated)")
            diag = diagnose(exid)
            print(f"    complement: {diag}")
            print("    pair sum well below 1 and complement unmoved => LIQUIDITY, expect reversion")
            print("    complement moved to absorb it                 => NEWS, reassess the position")
            logf.write(json.dumps({
                "ts": datetime.now(timezone.utc).isoformat(), "exchangeId": exid,
                "race": row.get("raceName"), "winner": row.get("winnerName"),
                "price": price, "quantity": qty, "complement": diag, "raw": f,
            }) + "\n")

    rt = RealtimeClient(c, on_event)
    try:
        asyncio.run(rt.listen([topic]))
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
