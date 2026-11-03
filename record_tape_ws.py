#!/usr/bin/env python3
"""Record every race leg's top of book from the Realtime market channels.

Read-only and free against the REST read budget: books are pushed over the
websocket. One JSON line per top-of-book change:

    {"t": 1759830000.12, "x": "1002", "b": 0.735, "a": 0.74, "bq": 307, "aq": 25680}

Feeds analyze_leadlag.py (does a lagging sibling catch up after one leg of a
race jumps?). Every leg is seeded once from bulk prices so quiet markets have a
known price before their first push.

    python3 -u record_tape_ws.py --minutes 60 --out data/tape.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time

from sigcup.catalog import group_by_race, load_catalog
from sigcup.client import SuperMarketClient, load_env
from sigcup.realtime import RealtimeClient

CHUNK = 50  # topics per socket, as in monitor_arb_ws.py


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=60)
    ap.add_argument("--out", default="data/tape.jsonl")
    args = ap.parse_args()

    load_env()
    c = SuperMarketClient(allow_trading=True)  # minting a realtime token is a POST; no orders are sent
    cat = [r for r in load_catalog() if r.get("exchangeId") and r.get("raceId")]
    races = {rid: legs for rid, legs in group_by_race(cat).items() if len(legs) >= 2}
    mk_ex = {l["marketId"]: l["exchangeId"] for legs in races.values() for l in legs}
    ex_set = set(mk_ex.values())
    out = open(args.out, "a", buffering=1)
    last: dict[str, tuple] = {}
    seq: dict[str, int] = {}
    stats = {"batches": 0, "lines": 0}

    # Race membership travels with the tape so the analysis needs no catalog.
    out.write(json.dumps({"meta": "races", "t": time.time(),
                          "races": {rid: [{"x": l["exchangeId"], "name": l.get("raceName"),
                                           "party": l.get("party") or l.get("title")}
                                          for l in legs] for rid, legs in races.items()}}) + "\n")

    def emit(exid: str, t: float, bids: list, asks: list) -> None:
        b = max(bids, key=lambda l: l["price"]) if bids else None
        a = min(asks, key=lambda l: l["price"]) if asks else None
        top = (b and b["price"], a and a["price"], b and b.get("quantity"), a and a.get("quantity"))
        if last.get(exid) == top:
            return
        last[exid] = top
        out.write(json.dumps({"t": round(t, 3), "x": exid, "b": top[0], "a": top[1],
                              "bq": top[2], "aq": top[3]}) + "\n")
        stats["lines"] += 1

    ids = sorted(ex_set)
    now = time.time()
    for i in range(0, len(ids), 100):
        for row in c.prices(ids[i:i + 100]).get("data") or []:
            exid = str(row["exchangeId"])
            emit(exid, now,
                 [{"price": row["bestBid"], "quantity": None}] if row.get("bestBid") is not None else [],
                 [{"price": row["bestAsk"], "quantity": None}] if row.get("bestAsk") is not None else [])
    print(f"seeded {len(last)} legs in {len(races)} races", flush=True)

    async def on_event(kind: str, payload: dict) -> None:
        if kind == "_status" and payload.get("status") not in ("ok",):
            print("status", json.dumps(payload)[:200], flush=True)
            return
        if kind != "market_batch":
            return
        stats["batches"] += 1
        t = time.time()
        for b in payload.get("books") or []:
            exid = str(b.get("exchangeId") or mk_ex.get(str(b.get("marketId")), ""))
            if exid not in ex_set:
                continue
            s = (b.get("asOf") or {}).get("sequence") or 0
            if s < seq.get(exid, -1):
                continue  # older than what we hold
            seq[exid] = s
            emit(exid, t, b.get("bids") or [], b.get("asks") or [])

    topics = [f"tournament:{c.tournament_id}:market:{m}" for m in mk_ex]
    chunks = [topics[i:i + CHUNK] for i in range(0, len(topics), CHUNK)]
    tok = c.realtime_token()
    clients = []
    for _ in chunks:
        rt = RealtimeClient(c, on_event)
        rt._token, rt._minted_at = tok, time.time()
        clients.append(rt)
    print(f"{len(topics)} market channels over {len(chunks)} sockets, {args.minutes:.0f} min", flush=True)

    async def run():
        tasks = [asyncio.create_task(rt.listen(ch)) for rt, ch in zip(clients, chunks)]
        t_end = time.time() + args.minutes * 60
        while time.time() < t_end:
            await asyncio.sleep(300)
            print(f"  {time.strftime('%H:%M:%S')} batches {stats['batches']} lines {stats['lines']}",
                  flush=True)
        for t in tasks:
            t.cancel()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    out.close()
    print(f"done: {stats['lines']} lines", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
