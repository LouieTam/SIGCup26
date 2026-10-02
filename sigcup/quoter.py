"""Ladder quoter: rest deep bids to capture book sweeps.

Rationale is in docs/sweep-opportunity.md. The short version: sweeps execute as
one atomic order and revert within ~75s, and this API has 2-17s latency, so the
only way to capture them is to already be resting. Reacting is impossible.

Three invariants this module enforces, in priority order:

1. **Never cross the spread.** Every rung is placed strictly below the best bid.
   A crossing order consumes capital instantly at the ask and is the exact
   opposite of what we want.
2. **Never exceed our own exposure caps.** Resting bids do not reserve capital,
   so the platform will happily let us post more than we can pay for. The engine
   silently SHRINKS resting orders to fit a reduced balance (per the Order
   schema), which means over-posting degrades quietly into sizes we did not
   choose. The governor exists so we never depend on size that is not funded.
3. **Only quote where fair value is defensible.** Fair is cross-checked against
   the complementary leg; an incoherent race is skipped rather than guessed at.

Not implemented yet: the mirror ladder. This quotes YES on the favoured leg,
which captures someone dumping that leg. The mirror trade — resting NO bids on
the *unfavoured* leg — captures someone aggressively buying the longshot, which
`detect_sweeps.py` shows also happens (the UP sweeps). Both express the same view
on the race outcome, so the per-race cap already covers the combined exposure;
adding it would roughly double sweep coverage per unit of conviction.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Callable

from .client import TICK, ApiError, SuperMarketClient, round_to_tick

# Rung depth as a fraction of fair value, with relative size weights.
# Depth matters more than tightness: in the Alaska sweep the 0.30 rung beat the
# 0.60 rung outright, and the deepest rungs had the best return on capital.
DEFAULT_RUNGS: list[tuple[float, float]] = [
    # (fraction of fair, size weight)
    #
    # Dense through the shallow end on purpose. The first version jumped from
    # 0.88 to 0.75 of fair, and a real sweep on NJ Senate D reached only 0.80 of
    # fair (0.78 against a 0.97 contract) — it would have landed in that gap and
    # filled just the 101-share top rung. Observed sweep depths range from 0.80
    # of fair down to 0.016, so the ladder has to span that whole range rather
    # than cluster at the catastrophic end.
    #
    # Reweighted from measurement, not intuition (analyze_depth.py over 20
    # fair>=0.90 markets, full history since the open):
    #     0.95 of fair -> 2.33% ROC, filled in 10/20 markets
    #     0.90 of fair -> 4.37% ROC, filled in  8/20   <- best
    #     0.85 of fair -> 3.58% ROC, filled in  7/20
    #     0.78 and below -> NOTHING traded there, in any of the 20
    # Deep rungs are dead capital in high-fair races: their books are tight and
    # their flow is shallow. The violent sweeps live in mid-probability and
    # chamber markets, which this quoter does not currently cover.
    (0.95, 2.0),
    (0.92, 3.0),
    (0.90, 4.0),
    (0.87, 3.0),
    (0.84, 2.0),
    # Kept small as tail insurance only. Resting costs no capital, so a lottery
    # ticket on an Alaska-scale dislocation is nearly free — but it must not
    # absorb size that the 0.84-0.95 band has demonstrably earned.
    (0.70, 0.4),
    (0.45, 0.3),
    (0.25, 0.3),
]


@dataclass
class Leg:
    market_id: str
    exchange_id: str
    winner: str | None
    race_id: str
    race_name: str | None
    best_bid: float | None = None
    best_ask: float | None = None
    mid: float | None = None


@dataclass
class Quote:
    exchange_id: str
    market_id: str
    price: float
    quantity: int
    side: str = "yes"  # "no" for mirror rungs; price is then a NO price

    @property
    def notional(self) -> float:
        return self.price * self.quantity

    def to_order(self) -> dict:
        return {
            "exchangeId": self.exchange_id,
            "side": self.side,
            "action": "buy",
            "quantity": self.quantity,
            "price": self.price,
        }


@dataclass
class RacePlan:
    race_id: str
    race_name: str | None
    leg: Leg
    fair: float
    coherence: float
    quotes: list[Quote] = field(default_factory=list)
    skipped: str | None = None
    mirror: bool = False  # NO bids on a longshot leg; `fair` is then the NO fair

    @property
    def notional(self) -> float:
        return sum(q.notional for q in self.quotes)


def read_books(c: SuperMarketClient, legs: list[Leg], workers: int = 6) -> None:
    """Fill best bid/ask/mid on each leg, concurrently (latency-bound API)."""
    from concurrent.futures import ThreadPoolExecutor

    def one(leg: Leg) -> None:
        b = c.exchange_orderbook(leg.exchange_id, depth=5)
        leg.best_bid, leg.best_ask = b.get("bestBid"), b.get("bestAsk")
        if leg.best_bid is not None and leg.best_ask is not None:
            leg.mid = (leg.best_bid + leg.best_ask) / 2

    with ThreadPoolExecutor(max_workers=min(workers, max(1, len(legs)))) as pool:
        list(pool.map(one, legs))


def estimate_fair(leg: Leg, siblings: list[Leg]) -> tuple[float | None, float]:
    """Fair value for `leg`, cross-checked against its complementary legs.

    For mutually exclusive legs the midpoints should satisfy
    `mid(leg) + Σ mid(siblings) ≈ 1`. The gap is returned as a coherence
    measure; a large gap means the legs disagree and we should not quote.
    """
    if leg.mid is None:
        return None, 1.0
    sib_mids = [s.mid for s in siblings if s.mid is not None]
    if not sib_mids:
        return leg.mid, 0.0
    implied = 1.0 - sum(sib_mids)
    gap = abs(leg.mid - implied)
    # Average the two views; they are independent estimates of the same number.
    return (leg.mid + implied) / 2, gap


def quantize_fair(fair: float, grid: float = 0.02) -> float:
    """Snap fair onto a coarse grid so small book wobble does not move the rungs.

    Without this, fair is recomputed every pass, a one-tick drift shifts every
    rung, and the ladder is torn down and re-posted continuously — which at ~25
    races would exceed the 30 writes/min budget on its own.
    """
    return round(round(fair / grid) * grid, 4)


def build_ladder(
    leg: Leg,
    fair: float,
    race_notional: float,
    rungs: list[tuple[float, float]] = None,
    min_qty: int = 10,
) -> list[Quote]:
    """Rungs strictly below the best bid, sized by weight within a notional cap."""
    rungs = rungs or DEFAULT_RUNGS
    if leg.best_bid is None:
        return []
    fair = quantize_fair(fair)
    ceiling = round_to_tick(leg.best_bid - TICK)  # never cross, never join the touch

    priced: list[tuple[float, float]] = []
    for frac, weight in rungs:
        p = round_to_tick(fair * frac)
        if p > ceiling:
            continue
        if p < TICK:
            continue
        if any(abs(p - q) < TICK / 2 for q, _ in priced):
            continue  # rungs collapsed onto one tick
        priced.append((p, weight))
    if not priced:
        return []

    # Allocate the race's notional across rungs by weight, then convert to shares.
    total_w = sum(w for _, w in priced)
    quotes: list[Quote] = []
    for p, w in priced:
        share_of_notional = race_notional * (w / total_w)
        qty = int(share_of_notional / p)
        if qty >= min_qty:
            quotes.append(Quote(leg.exchange_id, leg.market_id, p, qty))
    return quotes


def build_mirror_ladder(
    leg: Leg,
    yes_fair: float,
    race_notional: float,
    rungs: list[tuple[float, float]] = None,
) -> list[Quote]:
    """NO bids on a longshot leg: the mirror of build_ladder.

    Catches someone aggressively BUYING the longshot (an up-sweep), which
    analyze_mirror.py measured as larger flow than favourite dumps. A NO bid at
    q is a YES offer at 1 - q, and the best NO bid is 1 - best YES ask, so the
    same never-cross rule applies in NO terms: every rung strictly below
    1 - bestAsk, i.e. every YES-equivalent offer strictly above the best ask.
    """
    if leg.best_ask is None:
        return []
    shadow = Leg(leg.market_id, leg.exchange_id, leg.winner, leg.race_id, leg.race_name,
                 best_bid=round_to_tick(1 - leg.best_ask))
    quotes = build_ladder(shadow, 1 - yes_fair, race_notional, rungs)
    for q in quotes:
        q.side = "no"
    return quotes


def plan_races(
    c: SuperMarketClient,
    races: dict[str, list[dict]],
    cash: float,
    min_fair: float = 0.90,
    max_coherence_gap: float = 0.08,
    total_budget_pct: float = 0.50,
    per_race_pct: float = 0.10,
    rungs: list[tuple[float, float]] = None,
    mirror_frac: float = 0.0,
) -> tuple[list[RacePlan], dict]:
    """Decide what to rest, for which races, inside the exposure caps.

    `mirror_frac` > 0 adds a mirror ladder (NO bids) on each longshot leg of
    every race whose favourite is quoted, sized at `mirror_frac` of the race
    budget split across the longshots. Both ladders pay if the favourite wins,
    so they are one bet on the race, not two.
    """
    legs_by_race: dict[str, list[Leg]] = {}
    all_legs: list[Leg] = []
    for rid, rows in races.items():
        legs = [
            Leg(r["marketId"], r["exchangeId"], r.get("winnerName"), rid, r.get("raceName"))
            for r in rows
            if r.get("exchangeId")
        ]
        if len(legs) < 2:
            continue
        legs_by_race[rid] = legs
        all_legs += legs

    read_books(c, all_legs)

    total_budget = cash * total_budget_pct
    race_budget = cash * per_race_pct
    plans: list[RacePlan] = []

    for rid, legs in legs_by_race.items():
        # Quote the near-certain leg only: the one whose fair sits above min_fair.
        best: tuple[float, Leg, float] | None = None
        for leg in legs:
            siblings = [l for l in legs if l is not leg]
            fair, gap = estimate_fair(leg, siblings)
            if fair is None:
                continue
            if best is None or fair > best[0]:
                best = (fair, leg, gap)
        if best is None:
            plans.append(RacePlan(rid, legs[0].race_name, legs[0], 0.0, 1.0,
                                  skipped="no two-sided book"))
            continue

        fair, leg, gap = best
        plan = RacePlan(rid, leg.race_name, leg, fair, gap)
        if fair < min_fair:
            plan.skipped = f"fair {fair:.3f} below min_fair {min_fair:.2f} (news risk too high)"
        elif gap > max_coherence_gap:
            plan.skipped = f"legs incoherent (gap {gap:.3f} > {max_coherence_gap:.2f})"
        else:
            plan.quotes = build_ladder(leg, fair, race_budget, rungs)
            if not plan.quotes:
                plan.skipped = "no rung fits below the best bid"
        plans.append(plan)

        if mirror_frac > 0 and plan.skipped is None:
            longshots = [l for l in legs if l is not leg]
            for ls in longshots:
                ls_fair, _ = estimate_fair(ls, [l for l in legs if l is not ls])
                if ls_fair is None:
                    continue
                m = RacePlan(rid, ls.race_name, ls, round(1 - ls_fair, 4), gap, mirror=True)
                m.quotes = build_mirror_ladder(
                    ls, ls_fair, race_budget * mirror_frac / len(longshots), rungs)
                if not m.quotes:
                    m.skipped = "no mirror rung fits above the best ask"
                plans.append(m)

    # Global governor: admit races by descending fair until the budget is spent.
    admitted = sorted([p for p in plans if p.quotes], key=lambda p: -p.fair)
    running = 0.0
    for p in admitted:
        if running + p.notional > total_budget:
            p.quotes = []
            p.skipped = f"global budget {total_budget:,.0f} exhausted"
        else:
            running += p.notional

    stats = {
        "cash": cash,
        "total_budget": total_budget,
        "race_budget": race_budget,
        "committed": running,
        "races_quoted": sum(1 for p in plans if p.quotes and not p.mirror),
        "races_skipped": sum(1 for p in plans if not p.quotes and not p.mirror),
        "mirrors_quoted": sum(1 for p in plans if p.quotes and p.mirror),
    }
    return plans, stats


def current_ladder(c: SuperMarketClient) -> dict[str, list[dict]]:
    """Our resting buy orders, grouped by exchange."""
    out: dict[str, list[dict]] = {}
    for o in c.open_orders():
        if o.get("open") and o.get("action") == "buy":
            out.setdefault(str(o["exchangeId"]), []).append(o)
    return out


def ladder_matches(
    desired: list[Quote],
    resting: list[dict],
    tol_qty: float = 0.25,
    tol_ticks: int = 3,
) -> bool:
    """True if what rests is close enough to the plan to leave alone.

    Deliberately tolerant, because re-posting costs 2 writes against a 30/min
    budget and a torn-down ladder is not resting when a sweep arrives:

    * price within `tol_ticks` — fair value wobbles continuously and a rung one
      tick off is worth just as much.
    * quantity shrunk by up to `tol_qty` — the engine shrinks resting orders to
      fit a reduced balance, which is not drift we should fight.

    A missing rung, an extra one, or a materially mispriced one does trigger a
    re-post.
    """
    if len(desired) != len(resting):
        return False
    tol = tol_ticks * TICK + 1e-9
    unmatched = list(resting)
    for q in desired:
        hit = None
        for o in unmatched:
            p = o.get("priceLimit")
            if p is None:
                continue
            if abs(float(p) - q.price) <= tol and float(o["quantity"]) >= q.quantity * (1 - tol_qty):
                hit = o
                break
        if hit is None:
            return False
        unmatched.remove(hit)
    return True


def apply_plan(
    c: SuperMarketClient,
    plans: list[RacePlan],
    live: bool,
    verbose: bool = True,
    shrink_tol: float = 0.25,
    after_write: Callable[[], None] | None = None,
    held: Callable[[], set[str]] | None = None,
) -> dict:
    """Re-post ladders that have drifted. 2 writes per changed exchange.

    `shrink_tol` must be generous when overbooked: the engine shrinks resting
    orders to fit available cash, and treating its rationing as "drift" would
    re-post every ladder every pass and blow the 30 writes/min budget.

    Ladder writes go out low-priority (they leave write slots free for exits),
    and `after_write` runs after each exchange is posted so fills that land
    mid-pass get an exit straight away instead of waiting for the exit thread.

    `held()` returns exchanges we hold a position on. Those may carry an exit
    sell, which cancel_all would wipe along with the ladder, so there we cancel
    the old rungs by id instead (one write per rung rather than one in total).
    """
    resting = current_ladder(c) if live else {}
    writes = placed = skipped_same = 0
    for p in plans:
        if not p.quotes:
            continue
        exid = p.leg.exchange_id
        if live and ladder_matches(p.quotes, resting.get(exid, []), tol_qty=shrink_tol):
            skipped_same += 1
            continue
        if not live:
            placed += len(p.quotes)
            continue
        c.low_priority.on = True
        try:
            if resting.get(exid) and held and exid in held():
                for o in resting[exid]:
                    try:
                        c.cancel(str(o["id"]))
                    except ApiError as e:
                        # Already filled or cancelled; nothing left to remove.
                        print(f"    cancel {o['id']}: {e.code}")
                    writes += 1
            elif resting.get(exid):
                c.cancel_all(exchange_id=exid)
                writes += 1
            key = f"ladder-{exid}-{uuid.uuid4()}"
            res = c.place_batch([q.to_order() for q in p.quotes], idempotency_key=key)
            writes += 1
        finally:
            c.low_priority.on = False
        results = res.get("results") or res.get("data") or []
        ok = sum(1 for r in results if str(r.get("status", "")).startswith("2")) or len(p.quotes)
        placed += ok
        if verbose:
            print(f"    posted {len(p.quotes)} rungs on ex {exid} "
                  f"({p.notional:,.0f} notional), writes used {writes}")
        if after_write:
            after_write()
    return {"writes": writes, "placed": placed, "unchanged": skipped_same}
