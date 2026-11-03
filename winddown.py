#!/usr/bin/env python3
"""Wind the book down to flat, then stop. Places NO buys.

Each cycle:
  1. cancel every resting BUY (cancel-all on exchanges we hold nothing on,
     order by order where we still hold shares so the exits stay up);
  2. run the normal exit pass (run_quoter.unwind): cost-floored exits plus the
     stop-loss v2 rules, with stop clocks carried over in data/stop_state.json.
     min_topup is 1 so odd-lot leftovers get an exit too;
  3. when no position is left: cancel-all globally and exit.

Run INSTEAD of the supervisor (stop it first: touch data/STOP):
    nohup caffeinate -ims python3 -u winddown.py >> data/winddown.log 2>&1 &
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

import run_quoter as rq
from sigcup.catalog import group_by_race, load_catalog
from sigcup.client import SuperMarketClient, load_env

INTERVAL = 120


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def pull_buys(c: SuperMarketClient, held_ex: set[str]) -> int:
    orders = [o for o in c.open_orders() if o.get("open")]
    buys = [o for o in orders if o.get("action") == "buy"]
    n = 0
    for x in sorted({str(o["exchangeId"]) for o in buys}):
        try:
            if x in held_ex:
                for o in buys:
                    if str(o["exchangeId"]) == x:
                        c.cancel(str(o["id"]))
                        n += 1
            else:
                c.cancel_all(exchange_id=x)
                n += 1
        except Exception as e:  # noqa: BLE001 - keep going, retry next cycle
            print(f"  cancel on ex {x}: {type(e).__name__}: {e}", flush=True)
    print(f"{now()} {len(buys)} resting buys found, {n} cancel calls", flush=True)
    return len(buys)


def main() -> int:
    load_env()
    c = SuperMarketClient(allow_trading=True)
    for legs in group_by_race([r for r in load_catalog()
                               if r.get("exchangeId") and r.get("raceId")]).values():
        xs = [str(l["exchangeId"]) for l in legs]
        for x in xs:
            rq._race_legs[x] = xs
    print(f"{now()} wind-down started", flush=True)
    while True:
        try:
            pos = [p for p in c.positions() if abs(p["quantity"]) > 0]
            held_ex = {str(p["exchangeId"]) for p in pos}
            pull_buys(c, held_ex)
            if not pos:
                c.cancel_all()
                left = [o for o in c.open_orders() if o.get("open")]
                if not left:
                    print(f"{now()} FLAT: no positions, no open orders. cash {c.balance():,.2f}. "
                          f"Wind-down complete.", flush=True)
                    return 0
                print(f"{now()} flat but {len(left)} orders still open; retrying", flush=True)
            else:
                print(f"{now()} {len(pos)} positions left: "
                      + ", ".join(f"{p['exchangeId']}:{p['quantity']}" for p in pos), flush=True)
                rq.unwind(c, True, min_edge=0.005, reprice_ticks=2, min_topup=1)
        except Exception as e:  # noqa: BLE001 - outages: just try again
            print(f"{now()} cycle failed: {type(e).__name__}: {e}", flush=True)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    raise SystemExit(main())
