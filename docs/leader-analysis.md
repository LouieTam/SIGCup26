# How did rank 1 make +753K in one day? (2026-10-02)

The API exposes no per-trader trades, so this is inference from market-level
data: the leaderboard plus `analyze_today.py` (hourly candles for all 237
markets since 00:00Z, saved to `data/today_markets.json`).

## Facts

**Today's leaderboard (`period=1d`), 2026-10-02 ~20:45Z:**

| rank | account | pnl | volume | trades | pnl / volume |
|---|---|---|---|---|---|
| 1 | sunny-acorn-8216 | **753,582** | **19,613,038** | 2,228 | 3.84% |
| 2 | nimble-beacon-4953 | 281,747 | 2,447,239 | 684 | 11.5% |
| 3 | bright-finch-0400 | 226,049 | 4,077,963 | 3,647 | 5.5% |

We were rank 17 today and rank 31 all-time. Rank 1's all-time total
(761,976) is almost entirely from today.

**Platform notional today: 49.0M.** Rank 1's 19.6M is about **40%**: one account
was on one side of roughly 40% of all trading.

**61% of notional was in markets closing ≥ 0.85.** Large-ticket bursts, mostly
overnight:

| market | notional | trades (avg size) | when / range |
|---|---|---|---|
| m338 MN-05 D | 2.34M | 321 (7,300) | 2.1M in the 07:00 hour; 0.895–0.99 |
| m286 OK Senate R | 2.40M | 1,232 (1,949) | 01:00–04:00; 0.895–0.99 |
| m295 VA Senate D | 0.93M | 247 (3,766) | 02:00–03:00 only; 0.945–0.995 |
| m282 NJ Senate D | 0.79M | 171 (4,645) | 00:00–01:00 only; 0.91–0.99 |

**Big dislocations today:**

| kind | market | move |
|---|---|---|
| Dump | m151 Dem win U.S. House | 0.64 at 12:00 (926K that hour); 0.72 at 20:00 (985K). Fair ~0.87 |
| Up-spike on a longshot | m383 OK Senate D | 0.03 → 0.46 |
| Up-spike on a longshot | m369 AK Gov R | 0.22 → 0.60 |
| Up-spike on a longshot | m380 MA Senate R | 0.01 → 0.30 (our mirror caught only 1,296 sh) |
| Up-spike on a longshot | m316 WY Gov D | 0.005 → 0.275 |

**Cross-market checks:** no gap explains it.
- Race leg mids sum to 1 ± 0.06.
- Senate control (m153) at 0.68, against 0.49–0.53 implied by the 34 race
  markets. The Ohio special has no market here, which accounts for most of the
  difference.

## Inference

19.6M of volume on 100K of starting cash is only possible where volume is
cheap in cash. **In a market at p ≈ 0.97, selling YES (= buying NO) costs
1 − p ≈ 0.03 per share but counts ~0.97 of notional.** A trader who:
- provides the 0.97–0.99 offers (NO bids at 0.01–0.03) on near-certain markets;
- sells into overnight buyers walking the book;
- recycles that position closer to fair;

turns small cash into huge notional at a few cents per share. That matches the
flat **3.84% of volume**. Some of the P&L may be unrealised marks; the board
does not show the split.

## Implications for us

1. **Cheap-side leverage.** Our ladders buy the expensive side: YES at
   0.84–0.95 on favourites, and NO at 0.84–0.95 on longshots. The leverage is
   at the other end: NO at 0.01–0.10 on near-certain markets, i.e. resting YES
   offers at 0.90–0.99 above the market. Each fill there carries 10–30x the
   notional per unit of cash. **Needs measuring before building:** how often
   YES prints at 0.97–0.995 above fair in near-certain markets, and whether it
   reverts.
2. **Chamber markets.** m151 was dumped to 0.64 and 0.72 today, each in about
   1M of hourly volume. We do not quote 151–154 (no raceId; manual settlement).
3. **Mirror depth.** Longshot spikes reached 0.30–0.60 YES, which is NO at
   0.40–0.70, the mirror's deep rungs. Those carry only ~10% of the mirror's
   size (weights 0.4 / 0.3 / 0.3).

## Cheap-side measurement (2026-10-02 00:00–22:00Z, full depth)

YES offers k ticks above each favourite's fair, i.e. NO bids at 1 − offer,
with 4,000 cash per level per leg across 84 favourites at fair ≥ 0.75:

| ticks | +1 | +2 | +3 | +4 | +6 | +8 | +10 | +14 | +20 |
|---|---|---|---|---|---|---|---|---|---|
| hit | 83/84 | 82/84 | 78/84 | 68/84 | 47/83 | 23/73 | 10/65 | 4/47 | 0/31 |
| ROC on cash | 4.3% | 7.4% | 9.4% | 10.3% | 10.9% | **12.5%** | 7.1% | 3.8% | 0 |

That is 3–5x the main ladder's best rung (2.4% at 0.95 of fair). The flow is
broad: almost every favourite traded at least 3 ticks above today's fair.

**Built:** `--cheap F` adds NO bids on each favourite at `CHEAP_RUNGS` =
+3/+4/+6/+8/+10 ticks (weights 1/1/1.5/1.5/0.5), sized at F × the race budget.
They ride in the favourite's main plan, so one cancel/repost covers both, and
`ladder_matches` is side-aware. Every cheap offer sits strictly above the best
ask, so it can never cross our YES bids.

**Tests** (real snapshot of all books, 2026-10-02 ~22:30Z):
- 75 races / 77 mirror legs / 329 cheap rungs;
- no crossing anywhere;
- re-applying an unchanged plan costs 0 writes;
- a 20,000 NO @ 0.03 cheap fill exits as `sell no 20000 @ 0.07`.

**Risks specific to this side:**
1. **Adverse drift.** If a favourite firms toward 0.99, our NO bought at
   0.02–0.05 falls toward 0.01. The cost floor then holds it, and if the
   favourite wins it expires worthless. The loss is bounded by the cash paid,
   which is small per share.
2. **Queue.** At +3/+4 ticks we sit just behind the existing offers, so the
   measured volume is an upper bound.
3. **Size.** Positions are tiny in cash but large in shares (Alabama Governor
   R: 118K NO at 0.03).
