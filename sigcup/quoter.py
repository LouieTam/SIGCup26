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

from .client import TICK, SuperMarketClient, round_to_tick

# Rung depth as a fraction of fair value, with relative size weights.
# Depth matters more than tightness: in the Alaska sweep the 0.30 rung beat the
# 0.60 rung outright, and the deepest rungs had the best return on capital.
DEFAULT_RUNGS: list[tuple[float, float]] = [
    # (fraction of fair, size weight)
    (0.88, 1.0),
    (0.75, 1.5),
    (0.60, 2.5),
    (0.45, 4.0),
    (0.33, 6.0),
    (0.25, 8.0),
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

    @property
    def notional(self) -> float:
        return self.price * self.quantity

    def to_order(self) -> dict:
        return {
            "exchangeId": self.exchange_id,
            "side": "yes",
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


def plan_races(
    c: SuperMarketClient,
    races: dict[str, list[dict]],
    cash: float,
    min_fair: float = 0.90,
    max_coherence_gap: float = 0.08,
    total_budget_pct: float = 0.50,
    per_race_pct: float = 0.10,
    rungs: list[tuple[float, float]] = None,
) -> tuple[list[RacePlan], dict]:
    """Decide what to rest, for which races, inside the exposure caps."""
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
        "races_quoted": sum(1 for p in plans if p.quotes),
        "races_skipped": sum(1 for p in plans if not p.quotes),
    }
    return plans, stats


def current_ladder(c: SuperMarketClient) -> dict[str, list[dict]]:
    """Our resting buy orders, grouped by exchange."""
    out: dict[str, list[dict]] = {}
    for o in c.open_orders():
        if o.get("open") and o.get("action") == "buy":
            out.setdefault(str(o["exchangeId"]), []).append(o)
    return out


def ladder_matches(desired: list[Quote], resting: list[dict], tol_qty: float = 0.10) -> bool:
    """True if what rests already matches the plan, so we can skip the writes.

    Quantity is compared with tolerance because the engine shrinks resting
    orders to fit a reduced balance — a shrunken rung is not a reason to churn
    the whole ladder, but a missing or mispriced one is.
    """
    if len(desired) != len(resting):
        return False
    rest_by_price = {round(float(o["priceLimit"] or -1), 3): o for o in resting}
    for q in desired:
        o = rest_by_price.get(round(q.price, 3))
        if o is None:
            return False
        have = float(o["quantity"])
        if have < q.quantity * (1 - tol_qty):
            return False
    return True


def apply_plan(
    c: SuperMarketClient, plans: list[RacePlan], live: bool, verbose: bool = True
) -> dict:
    """Re-post ladders that have drifted. 2 writes per changed exchange."""
    resting = current_ladder(c) if live else {}
    writes = placed = skipped_same = 0
    for p in plans:
        if not p.quotes:
            continue
        exid = p.leg.exchange_id
        if live and ladder_matches(p.quotes, resting.get(exid, [])):
            skipped_same += 1
            continue
        if not live:
            placed += len(p.quotes)
            continue
        if resting.get(exid):
            c.cancel_all(exchange_id=exid)
            writes += 1
        key = f"ladder-{exid}-{uuid.uuid4()}"
        res = c.place_batch([q.to_order() for q in p.quotes], idempotency_key=key)
        writes += 1
        results = res.get("results") or res.get("data") or []
        ok = sum(1 for r in results if str(r.get("status", "")).startswith("2")) or len(p.quotes)
        placed += ok
        if verbose:
            print(f"    posted {len(p.quotes)} rungs on ex {exid} "
                  f"({p.notional:,.0f} notional), writes used {writes}")
    return {"writes": writes, "placed": placed, "unchanged": skipped_same}
