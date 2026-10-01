#!/usr/bin/env python3
"""Order-book / trade-tape collector. Read-only — places nothing.

Two tiers, because the account gets only 100 reads/min:

  WIDE  `GET /exchanges/prices` takes 100 ids per call, so top-of-book for all
        237 markets costs 3 reads. Run it often; it catches a sweep anywhere.
  DEEP  Full `GET /markets/{id}/orderbook` + `GET /exchanges/{id}/trades` for a
        focus set. 2 reads per market per cycle, so keep the set small.

Output is newline-delimited JSON under data/, one record per observation:
  data/wide.jsonl    {ts, rows:[{exchangeId, latestPrice, bestBid, bestAsk, ...}]}
  data/books.jsonl   {ts, marketId, exchangeId, asOf, bids, asks}
  data/trades.jsonl  {ts, marketId, exchangeId, trade:{id, createdAt, price, size, side, volume}}

Why both books and trades: the trade tape has no aggressor flag (`side` is
YES/NO space, not buy/sell), so "impatient selling" can only be inferred by
comparing a print against the book that was resting just before it. Keeping
both in one timeline makes that classification possible offline.

Examples
  python3 collect.py --state RI --interval 10
  python3 collect.py --state RI --title-contains "Rhode Island" --duration 3600
  python3 collect.py --market 387 --market 388 --interval 5 --no-wide
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from sigcup.catalog import load_catalog, select
from sigcup.client import ApiError, SuperMarketClient, load_env

DATA_DIR = "data"
_stop = False


def _on_signal(signum, frame):
    global _stop
    _stop = True
    print("\n[stopping after current cycle — ctrl-c again to force]", flush=True)
    signal.signal(signum, signal.SIG_DFL)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Writer:
    """Append-only JSONL sink, flushed per record so a kill loses nothing.
    Thread-safe: the deep tier fetches markets concurrently."""

    def __init__(self, name: str):
        os.makedirs(DATA_DIR, exist_ok=True)
        self.path = os.path.join(DATA_DIR, f"{name}.jsonl")
        self._f = open(self.path, "a", buffering=1)
        self._lock = threading.Lock()
        self.count = 0

    def write(self, rec: dict) -> None:
        line = json.dumps(rec, separators=(",", ":")) + "\n"
        with self._lock:
            self._f.write(line)
            self.count += 1

    def close(self) -> None:
        with self._lock:
            self._f.close()


def collect_wide(c: SuperMarketClient, exchange_ids: list[str], w: Writer) -> int:
    """Top-of-book for every exchange, 100 ids per read."""
    reads = 0
    for i in range(0, len(exchange_ids), 100):
        chunk = exchange_ids[i : i + 100]
        resp = c.prices(chunk)
        reads += 1
        w.write({"ts": now_iso(), "rows": resp.get("data") or []})
        missing = resp.get("missingIds") or []
        if missing:
            print(f"  [wide] missingIds: {missing}", flush=True)
    return reads


def collect_deep(
    c: SuperMarketClient,
    focus: list[dict],
    books: Writer,
    trades: Writer,
    last_trade: dict[str, int],
    depth: int,
    first_pass_lookback_min: int,
    workers: int = 6,
) -> tuple[int, int]:
    """Full book + new trades for each focus market, fetched concurrently.

    Concurrency is about latency, not throughput: this API answers in 2-17s, so
    a sequential pass over the focus set would take most of a minute and miss
    the sweeps we are trying to catch. The client's rate limiter is shared and
    thread-safe, so workers still respect the one account budget.
    """
    totals = [0, 0]  # reads, new trades
    tally = threading.Lock()
    watermark = threading.Lock()

    def one(row: dict) -> None:
        reads, new_trades = _collect_one(
            c, row, books, trades, last_trade, depth,
            first_pass_lookback_min, watermark,
        )
        with tally:
            totals[0] += reads
            totals[1] += new_trades

    with ThreadPoolExecutor(max_workers=min(workers, max(1, len(focus)))) as pool:
        list(pool.map(one, focus))
    return totals[0], totals[1]


def _collect_one(
    c: SuperMarketClient,
    row: dict,
    books: Writer,
    trades: Writer,
    last_trade: dict[str, int],
    depth: int,
    first_pass_lookback_min: int,
    watermark: threading.Lock,
) -> tuple[int, int]:
    reads = 0
    new_trades = 0
    mid, exid = row["marketId"], row["exchangeId"]
    ts = now_iso()

    try:
        book = c.market_orderbook(mid, depth=depth)
        reads += 1
    except ApiError as e:
        print(f"  [book {mid}] {e.code}: {e.message}", flush=True)
        return reads, new_trades

    for ex in book.get("exchanges") or []:
        books.write(
            {
                "ts": ts,
                "marketId": mid,
                "exchangeId": ex["exchangeId"],
                "asOf": ex.get("asOf"),
                "impliedProbability": ex.get("impliedProbability"),
                "bids": ex.get("bids"),
                "asks": ex.get("asks"),
            }
        )

    # Trade tape. On the first pass look back a window; after that ask only for
    # prints newer than the watermark we stored for this exchange.
    params: dict = {}
    with watermark:
        first_pass = exid not in last_trade
    if first_pass and first_pass_lookback_min > 0:
        start = time.time() - first_pass_lookback_min * 60
        params["from"] = datetime.fromtimestamp(start, timezone.utc).isoformat()

    try:
        tape = c._get(
            f"/exchanges/{exid}/trades",
            tournamentId=c.tournament_id,
            limit=200,
            **params,
        )
        reads += 1
    except ApiError as e:
        print(f"  [tape {exid}] {e.code}: {e.message}", flush=True)
        return reads, new_trades

    with watermark:
        seen = last_trade.get(exid, -1)
    high = seen
    for t in tape.get("data") or []:
        try:
            tid = int(t["id"])
        except (TypeError, ValueError):
            tid = -1  # legacy id space; store it, but never use it as a watermark
        if tid > seen or tid < 0:
            trades.write({"ts": ts, "marketId": mid, "exchangeId": exid, "trade": t})
            new_trades += 1
        high = max(high, tid)
    if high >= 0:
        with watermark:
            last_trade[exid] = max(last_trade.get(exid, -1), high)
    return reads, new_trades


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--state", help="focus on markets in this US state, e.g. RI")
    p.add_argument("--race-id", help="focus on one raceId (pairs all its legs)")
    p.add_argument("--market", action="append", default=[], help="focus marketId (repeatable)")
    p.add_argument("--title-contains", help="focus markets whose title contains this")
    p.add_argument("--interval", type=float, default=10.0, help="deep-tier seconds (default 10)")
    p.add_argument("--wide-every", type=int, default=3, help="run the wide tier every N deep cycles (default 3)")
    p.add_argument("--no-wide", action="store_true", help="skip the all-market price tier")
    p.add_argument("--depth", type=int, default=200, help="order-book depth (default 200)")
    p.add_argument("--duration", type=float, help="stop after this many seconds")
    p.add_argument("--lookback", type=int, default=120, help="first-pass trade lookback, minutes (default 120)")
    args = p.parse_args()

    load_env()
    c = SuperMarketClient()  # read-only: allow_trading stays False

    try:
        rows = load_catalog()
    except FileNotFoundError:
        print("data/catalog.json missing — run `python3 build_catalog.py` first.")
        return 1

    focus = select(
        rows,
        state=args.state,
        race_id=args.race_id,
        market_ids=args.market or None,
        title_contains=args.title_contains,
    )
    if not focus:
        print("No markets matched the focus filters.")
        return 1
    focus.sort(key=lambda r: int(r["marketId"]))
    all_exchange_ids = [r["exchangeId"] for r in rows if r.get("exchangeId")]

    deep_reads = 2 * len(focus)
    wide_reads = 0 if args.no_wide else -(-len(all_exchange_ids) // 100)
    cycles_per_min = 60.0 / args.interval
    est = deep_reads * cycles_per_min + (
        0 if args.no_wide else wide_reads * cycles_per_min / args.wide_every
    )

    print(f"Focus set ({len(focus)} markets, {deep_reads} reads/cycle):")
    for r in focus:
        print(
            f"  m={r['marketId']:>5} ex={str(r.get('exchangeId')):>5} "
            f"{str(r.get('winnerName')):<18} {r.get('raceName') or r.get('title')}"
        )
    print(f"\nWide tier: {'off' if args.no_wide else f'{len(all_exchange_ids)} exchanges, {wide_reads} reads every {args.wide_every} cycles'}")
    print(f"Estimated {est:.0f} reads/min against a 100/min budget.")
    if est > 90:
        print("  !! over budget — the client will throttle and cycles will stretch.")
        print(f"  !! raise --interval to >= {deep_reads * 60 / 85:.0f}s, or shrink the focus set.")
    print()

    signal.signal(signal.SIGINT, _on_signal)
    books, trades, wide = Writer("books"), Writer("trades"), Writer("wide")
    last_trade: dict[str, int] = {}
    started = time.time()
    cycle = 0

    try:
        while not _stop:
            cycle += 1
            t0 = time.time()
            reads, new_trades = collect_deep(
                c, focus, books, trades, last_trade, args.depth,
                args.lookback if cycle == 1 else 0,
            )
            if not args.no_wide and cycle % args.wide_every == 1:
                reads += collect_wide(c, all_exchange_ids, wide)
            elapsed = time.time() - t0
            print(
                f"[{now_iso()}] cycle {cycle}: {reads} reads, "
                f"{new_trades} new trades, {elapsed:.1f}s  "
                f"(books={books.count} trades={trades.count} wide={wide.count})",
                flush=True,
            )
            if args.duration and time.time() - started >= args.duration:
                break
            time.sleep(max(0.0, args.interval - elapsed))
    finally:
        for w in (books, trades, wide):
            w.close()
        print(
            f"\nStopped after {cycle} cycles / {time.time() - started:.0f}s.\n"
            f"  data/books.jsonl  +{books.count} snapshots\n"
            f"  data/trades.jsonl +{trades.count} trades\n"
            f"  data/wide.jsonl   +{wide.count} price sweeps"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
