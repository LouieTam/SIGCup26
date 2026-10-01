#!/usr/bin/env python3
"""One-screen status of the live quoter. Read-only — safe to run any time.

    python3 status.py
    python3 status.py --log data/quoter.jsonl
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess

from sigcup.catalog import load_catalog
from sigcup.client import ApiError, SuperMarketClient, load_env

START = 100_000.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default="data/quoter.jsonl")
    args = ap.parse_args()
    load_env()
    c = SuperMarketClient()

    alive = subprocess.run(["pgrep", "-f", "run_quoter.py"], capture_output=True).returncode == 0
    print(f"quoter process: {'RUNNING' if alive else 'NOT RUNNING'}")

    if os.path.exists(args.log):
        lines = [json.loads(l) for l in open(args.log) if l.strip()]
        ok = [r for r in lines if "cash" in r]
        errs = [r for r in lines if "error" in r]
        print(f"log: {len(lines)} entries, {len(errs)} errors")
        if errs:
            print(f"  last error: {errs[-1].get('ts')} {errs[-1].get('error')}")
        if ok:
            last = ok[-1]
            print(f"  last pass {last.get('pass')} at {last.get('ts')}: "
                  f"halted={last.get('halted')} quoted={last.get('racesQuoted')}")

    labels = {r["exchangeId"]: (r.get("winnerName"), r.get("raceName"))
              for r in load_catalog() if r.get("exchangeId")}

    cash = c.balance()
    print(f"\ncash {cash:,.2f}   (started {START:,.0f}, delta {cash - START:+,.2f})")

    orders = c.open_orders()
    by_ex: dict[str, list[dict]] = {}
    for o in orders:
        by_ex.setdefault(str(o["exchangeId"]), []).append(o)
    notional = sum((o["priceLimit"] or 0) * o["quantity"] for o in orders)
    buys = [o for o in orders if o.get("action") == "buy"]
    sells = [o for o in orders if o.get("action") == "sell"]
    print(f"resting: {len(orders)} orders ({len(buys)} buy / {len(sells)} sell) "
          f"across {len(by_ex)} exchanges, {notional:,.0f} notional")

    try:
        pos = c.positions()
    except ApiError as e:
        print(f"positions unavailable: {e.code}")
        pos = []
    if pos:
        print(f"\npositions ({len(pos)}):")
        tot_basis = 0.0
        for p in sorted(pos, key=lambda p: -(p.get("costBasis") or 0)):
            w, race = labels.get(str(p["exchangeId"]), (None, None))
            tot_basis += p.get("costBasis") or 0
            print(f"  ex {p['exchangeId']:<6} {str(w)[:12]:<12} {str(race)[:30]:<30} "
                  f"qty {p['quantity']:>8,.0f} avg {p['avgCost']:.3f} "
                  f"basis {p.get('costBasis', 0):>9,.0f}")
        print(f"  total cost basis {tot_basis:,.0f} "
              f"({tot_basis / START * 100:.1f}% of starting capital)")
        print("  NB: ignore currentPrice/unrealizedPnl — they mark at cumulative VWAP,")
        print("      which is unrelated to fair value (docs/open-questions-answered.md Q6)")
    else:
        print("\nno positions — nothing has been swept into yet")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
