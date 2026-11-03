#!/usr/bin/env python3
"""Election-day trade: sell overpriced longshots, hold to settlement. One shot.

For every leg whose YES bid is far above Polymarket's price for the same
outcome, buy NO into the resting YES bids (a NO buy at 1-p fills against a YES
bid at p), walking down the book to a floor, then cancel any remainder.
No stop-loss, no exits: these are held until the market settles.

Per leg:
  p      = Polymarket best ask for the outcome (a conservative probability)
  floor  = max(p + MIN_EDGE, MIN_PRICE): we never sell YES below this
  risk   = shares * (1 - fill price), capped by p (CAPS) and by --budget overall
Legs are taken in order of edge (our best bid - p). Legs without a matching
Polymarket winner market, or listed in EXCLUDE, are skipped.

    python3 sell_longshots.py              # dry run: print the plan
    python3 sell_longshots.py --live       # place the orders
"""
from __future__ import annotations

import argparse
import json
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from sigcup.catalog import load_catalog
from sigcup.client import SuperMarketClient, _ssl_context, load_env

MIN_EDGE = 0.025
MIN_PRICE = 0.035
MAX_BID = 0.12           # only legs priced as longshots here
CAPS = [(0.015, 12_000), (0.03, 8_000), (0.055, 5_000)]   # (max p, risk cap)
# Checked by hand: the Polymarket price makes these fair or worse for a seller.
EXCLUDE = {"957", "965", "939", "1029", "881"}
LOG = "data/longshots.jsonl"
PARTY = {"Republican": "republican", "Democratic": "democrat", "Independent": "independent"}


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def poly_prob(title: str) -> tuple[float, str] | None:
    """Polymarket best ask for 'party wins this race', or None if not found."""
    m = re.match(r"Will the (\w+) Party win the (.+?) (Senate|Governor|House race)\?", title)
    if not m:
        return None
    party, place, office = m.groups()
    q = f"{place} {office.replace(' race', '')} election winner"
    url = "https://gamma-api.polymarket.com/public-search?" + urllib.parse.urlencode(
        {"q": q, "limit_per_type": 8})
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    d = json.load(urllib.request.urlopen(req, timeout=20, context=_ssl_context()))
    for e in d.get("events", []):
        slug = e.get("slug", "")
        if e.get("closed") or "winner" not in slug or place.lower().replace(" ", "-") not in slug:
            continue
        for mk in e.get("markets", []):
            if PARTY[party] in mk.get("question", "").lower() and mk.get("outcomePrices"):
                ask = mk.get("bestAsk") or json.loads(mk["outcomePrices"])[0]
                return float(ask), slug
    return None


def risk_cap(p: float) -> float:
    for max_p, cap in CAPS:
        if p <= max_p:
            return cap
    return 0.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--budget", type=float, default=150_000, help="total risk (cash) to commit")
    args = ap.parse_args()
    load_env()
    c = SuperMarketClient(allow_trading=args.live)
    cat = {str(r["exchangeId"]): r for r in load_catalog() if r.get("exchangeId")}
    ids = list(cat)
    snap = {}
    for i in range(0, len(ids), 100):
        for it in c.prices(ids[i:i + 100]).get("data", []):
            snap[str(it["exchangeId"])] = it

    cands = []
    for x, p in snap.items():
        bid = p.get("bestBid")
        if bid is None or not (MIN_PRICE <= bid <= MAX_BID) or x in EXCLUDE:
            continue
        pp = poly_prob(cat[x]["title"])
        if not pp:
            continue
        prob, slug = pp
        floor = round(max(prob + MIN_EDGE, MIN_PRICE) + 0.0049, 3)
        floor = round(round(floor / 0.005) * 0.005, 3)
        cap = risk_cap(prob)
        if cap and bid >= floor:
            cands.append(dict(x=x, title=cat[x]["title"], bid=bid, poly=prob, slug=slug,
                              floor=floor, cap=cap, edge=bid - prob))
    cands.sort(key=lambda d: -d["edge"])

    left = args.budget
    plan = []
    for d in cands:
        if left <= 100:
            break
        ob = c.exchange_orderbook(d["x"], depth=60)
        qty, risk, prem, low = 0, 0.0, 0.0, None
        for lv in ob["bids"]:
            px = lv["price"]
            if px < d["floor"] - 1e-9:
                break
            room = min(d["cap"], left) - risk
            q = min(lv["quantity"], int(room / (1 - px)))
            if q <= 0:
                break
            qty += q; risk += q * (1 - px); prem += q * px; low = px
        if qty < 100:
            continue
        left -= risk
        d.update(qty=qty, risk=round(risk), prem=round(prem), limit_no=round(1 - low, 3),
                 ev=round(prem - qty * d["poly"]))
        plan.append(d)

    tr, tp, te = sum(d["risk"] for d in plan), sum(d["prem"] for d in plan), sum(d["ev"] for d in plan)
    for d in plan:
        print(f"{d['x']:>5} {d['title'][9:62]:<53} bid {d['bid']:.3f} poly {d['poly']:.4f} "
              f"floor {d['floor']:.3f}  NO {d['qty']:>7,} @<= {d['limit_no']:.3f}  "
              f"risk {d['risk']:>6,}  prem {d['prem']:>5,}  EV {d['ev']:>5,}")
    print(f"{len(plan)} legs: risk {tr:,}  premium if all NO {tp:,}  EV vs Polymarket {te:,}")
    if not args.live:
        return 0

    with open(LOG, "a") as f:
        for d in plan:
            try:
                r = c.place_order(d["x"], "no", "buy", d["qty"], price=d["limit_no"])
            except Exception as e:  # noqa: BLE001 - log and move on
                r = {"error": f"{type(e).__name__}: {e}"}
            if r.get("open"):
                try:
                    c.cancel(str(r["orderId"]))
                except Exception as e:  # noqa: BLE001
                    r["cancelError"] = str(e)
            print(f"{now()} {d['x']}: traded {r.get('quantityTraded')} of {d['qty']} "
                  f"@ {r.get('fillPrice')} cost {r.get('totalCost')} {r.get('error', '')}", flush=True)
            f.write(json.dumps({"at": now(), "plan": d, "result": r}) + "\n")
            time.sleep(1)
    print(f"{now()} done. cash {c.balance():,.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
