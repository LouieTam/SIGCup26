#!/usr/bin/env python3
"""Measure cross-leg arbitrage from live pushed books — no read budget spent.

Re-check of docs/dutch-book-assessment.md, which sampled top-of-book over REST
every ~2 minutes and so could not see how long an arb lasts or how much size
sits behind it. Here every race's books arrive over the Realtime market
channels (free against the rate limit) and each arb is tracked as an EPISODE
from the moment it opens to the moment it closes.

Two kinds are recorded per race:

    bid  Σ best bid > 1   buy NO on every leg; locked >= Σbid - 1 per share
                          (riskless only if the race resolves normally)
    ask  Σ best ask < 1   buy YES on every leg; a short on "nobody wins",
                          NOT riskless, logged for completeness

Executable size walks the books level by level while the marginal sum still
clears 1, so `profit` is what one perfectly timed taker could actually have
locked, and `capital` what it would have tied up until settlement.

`catchable` counts episodes lasting longer than --latency seconds: the API
answers in 2-17s, so anything shorter closes before a REST order lands.

    python3 monitor_arb_ws.py --minutes 30
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time

from sigcup.catalog import group_by_race, load_catalog
from sigcup.client import SuperMarketClient, load_env
from sigcup.realtime import RealtimeClient

CHUNK = 50  # topics per socket; stay well under per-connection channel limits


def walk(ladders: list[list[dict]], better) -> tuple[float, float, int, float]:
    """Walk n legs' levels together while the marginal edge is positive.
    Returns (top edge, profit, shares, capital)."""
    idx = [0] * len(ladders)
    left = [l[0]["quantity"] if l else 0 for l in ladders]
    if not all(ladders):
        return 0.0, 0.0, 0, 0.0
    top = better([l[0]["price"] for l in ladders])
    profit = capital = 0.0
    shares = 0
    while True:
        prices = [ladders[i][idx[i]]["price"] for i in range(len(ladders))]
        edge = better(prices)
        if edge <= 1e-9:
            break
        q = min(left)
        profit += edge * q
        shares += q
        capital += q * (len(prices) - sum(prices)) if better is bid_edge else q * sum(prices)
        for i in range(len(ladders)):
            left[i] -= q
            if left[i] == 0:
                idx[i] += 1
                if idx[i] >= len(ladders[i]):
                    return top, profit, shares, capital
                left[i] = ladders[i][idx[i]]["quantity"]
    return top, profit, shares, capital


def bid_edge(prices):  # NO on every leg
    return sum(prices) - 1


def ask_edge(prices):  # YES on every leg
    return 1 - sum(prices)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=30)
    ap.add_argument("--latency", type=float, default=10.0,
                    help="episodes shorter than this are uncatchable over REST")
    ap.add_argument("--out", default="data/arb_ws.jsonl")
    args = ap.parse_args()

    load_env()
    c = SuperMarketClient(allow_trading=True)  # minting a realtime token is a POST
    cat = [r for r in load_catalog() if r.get("exchangeId") and r.get("raceId")]
    races = {rid: legs for rid, legs in group_by_race(cat).items() if len(legs) >= 2}
    ex_race = {l["exchangeId"]: rid for rid, legs in races.items() for l in legs}
    mk_ex = {l["marketId"]: l["exchangeId"] for legs in races.values() for l in legs}
    books: dict[str, dict] = {}
    seq: dict[str, int] = {}
    open_ep: dict[tuple, dict] = {}
    closed: list[dict] = []
    out = open(args.out, "a", buffering=1)
    stats = {"batches": 0, "books": 0}

    # Our own resting orders sit in these books, and self-trade prevention means
    # we can never take them (our MA Governor exit showed up as a 64K "ask arb").
    # (exid, "bids"|"asks", price) -> our quantity; refreshed every few minutes.
    # Swapped whole (never mutated in place): the refresh runs on another thread.
    own_ref: list[dict[tuple, int]] = [{}]

    def refresh_own() -> None:
        m: dict[tuple, int] = {}
        for o in c.open_orders():
            if not o.get("open") or o.get("priceLimit") is None:
                continue
            p = float(o["priceLimit"])
            yes_bid = (o["side"] == "yes") == (o["action"] == "buy")
            key = (str(o["exchangeId"]), "bids" if yes_bid else "asks",
                   round(p if o["side"] == "yes" else 1 - p, 3))
            m[key] = m.get(key, 0) + int(o["quantity"])
        own_ref[0] = m

    def others(exid: str, key: str) -> list[dict]:
        lv = []
        for x in books[exid].get(key) or []:
            q = x["quantity"] - own_ref[0].get((exid, key, round(x["price"], 3)), 0)
            if q > 0 or x["quantity"] == 0:  # size-0 levels are seeds; keep them
                lv.append({"price": x["price"], "quantity": max(q, 0)})
        return lv

    def evaluate(rid: str, now: float) -> None:
        legs = races[rid]
        if not all(l["exchangeId"] in books for l in legs):
            return
        for kind, fn, key in (("bid", bid_edge, "bids"), ("ask", ask_edge, "asks")):
            ladders = [others(l["exchangeId"], key) for l in legs]
            ladders = [sorted(l, key=lambda x: -x["price"] if key == "bids" else x["price"])
                       for l in ladders]
            top, profit, shares, capital = walk(ladders, fn)
            k = (rid, kind)
            if top > 1e-9:
                ep = open_ep.get(k)
                if ep is None:
                    ep = open_ep[k] = {"race": rid, "name": legs[0].get("raceName"),
                                       "legs": len(legs), "kind": kind, "start": now,
                                       "max_edge": 0.0, "max_profit": 0.0,
                                       "shares_at_max": 0, "capital_at_max": 0.0}
                ep["max_edge"] = max(ep["max_edge"], top)
                if profit > ep["max_profit"]:
                    ep.update(max_profit=profit, shares_at_max=shares, capital_at_max=capital)
            elif k in open_ep:
                ep = open_ep.pop(k)
                ep["end"] = now
                ep["duration"] = now - ep["start"]
                closed.append(ep)
                out.write(json.dumps(ep) + "\n")

    async def on_event(kind: str, payload: dict) -> None:
        if kind == "_status" and payload.get("status") not in ("ok",):
            print("status", json.dumps(payload)[:200], flush=True)
            return
        if kind != "market_batch":
            return
        stats["batches"] += 1
        now = time.time()
        touched = set()
        for b in payload.get("books") or []:
            exid = str(b.get("exchangeId") or mk_ex.get(str(b.get("marketId")), ""))
            if exid not in ex_race:
                continue
            s = (b.get("asOf") or {}).get("sequence") or 0
            if s < seq.get(exid, -1):
                continue  # older than what we hold; docs: apply only newer asOf
            seq[exid] = s
            books[exid] = b
            stats["books"] += 1
            touched.add(ex_race[exid])
        for rid in touched:
            evaluate(rid, now)

    # Books are pushed only when they change, so a quiet market would never be
    # seen. Seed every leg's top of book from bulk prices (3 reads) with size 0:
    # the edge is still measured, executable size waits for a real push.
    ids = list(ex_race)
    for i in range(0, len(ids), 100):
        for row in c.prices(ids[i:i + 100]).get("data") or []:
            exid = str(row["exchangeId"])
            books[exid] = {
                "bids": [{"price": row["bestBid"], "quantity": 0}] if row.get("bestBid") is not None else [],
                "asks": [{"price": row["bestAsk"], "quantity": 0}] if row.get("bestAsk") is not None else [],
            }
    refresh_own()
    seeded_at = time.time()
    for rid in races:
        evaluate(rid, seeded_at)
    print(f"seeded {len(books)} books; {len(open_ep)} arbs open at start", flush=True)

    topics = [f"tournament:{c.tournament_id}:market:{m}" for m in mk_ex]
    chunks = [topics[i:i + CHUNK] for i in range(0, len(topics), CHUNK)]
    tok = c.realtime_token()  # one token (one write) shared by every socket
    clients = []
    for _ in chunks:
        rt = RealtimeClient(c, on_event)
        rt._token, rt._minted_at = tok, time.time()
        clients.append(rt)
    print(f"{len(races)} races, {len(topics)} market channels over {len(chunks)} sockets, "
          f"{args.minutes:.0f} min", flush=True)

    async def run():
        tasks = [asyncio.create_task(rt.listen(ch)) for rt, ch in zip(clients, chunks)]
        t_end = time.time() + args.minutes * 60
        n = 0
        while time.time() < t_end:
            await asyncio.sleep(60)
            n += 1
            if n % 5 == 0:
                try:
                    await asyncio.to_thread(refresh_own)
                except Exception as e:  # noqa: BLE001 - keep measuring on a failed read
                    print(f"  own-order refresh failed: {e}", flush=True)
            print(f"  {time.strftime('%H:%M:%S')} batches {stats['batches']} books "
                  f"{stats['books']} | races with books on every leg "
                  f"{sum(all(l['exchangeId'] in books for l in races[r]) for r in races)} | "
                  f"open {len(open_ep)} closed {len(closed)}", flush=True)
        for t in tasks:
            t.cancel()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    now = time.time()
    for ep in open_ep.values():  # still open at the end: censored durations
        ep["end"], ep["duration"], ep["censored"] = now, now - ep["start"], True
        closed.append(ep)
        out.write(json.dumps(ep) + "\n")
    out.close()

    for kind in ("bid", "ask"):
        eps = [e for e in closed if e["kind"] == kind]
        print(f"\n=== {kind} episodes: {len(eps)} "
              f"({'riskless-ish NO-all' if kind == 'bid' else 'YES-all, NOT riskless'}) ===")
        if not eps:
            continue
        catch = [e for e in eps if e["duration"] >= args.latency]
        print(f"  races affected {len({e['race'] for e in eps})}; "
              f"median duration {statistics.median(e['duration'] for e in eps):.1f}s; "
              f"edges: max {max(e['max_edge'] for e in eps):.3f}, "
              f"median {statistics.median(e['max_edge'] for e in eps):.3f}")
        for label, group in (("all", eps), (f">= {args.latency:.0f}s (catchable)", catch)):
            p = sum(e["max_profit"] for e in group)
            k = sum(e["capital_at_max"] for e in group)
            print(f"  {label:<20} n={len(group):<4} profit {p:>9,.1f} on capital {k:>11,.0f} "
                  f"({p / k * 100 if k else 0:.2f}% ROC)")
        for e in sorted(eps, key=lambda e: -e["max_profit"])[:8]:
            print(f"    {str(e['name'])[:30]:<30} legs {e['legs']} edge {e['max_edge']:.3f} "
                  f"profit {e['max_profit']:>8,.1f} on {e['capital_at_max']:>9,.0f} "
                  f"lasted {e['duration']:>6.1f}s{' (open at end)' if e.get('censored') else ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
