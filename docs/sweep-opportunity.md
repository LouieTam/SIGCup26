# Book sweeps and mean reversion — the actual opportunity

Measured 2026-10-01 from full trade history. **This, not arbitrage, is where the
money is.** ~226,000 SUSQies transferred to passive resting bids across just 30
of 237 markets in the first five hours of trading.

## The Alaska Senate event (20:19–20:22 UTC)

`Will the Democratic Party win the Alaska Senate?` (market 377, exchange 1066)
traded steadily at 0.67–0.69, then:

| time (UTC) | prints | levels | shares | range | VWAP | to passive side |
|---|---|---|---|---|---|---|
| 20:19:39.592 | 22 | 14 | 12,016 | 0.525–0.68 | 0.6162 | 767 |
| 20:19:58.589 | 13 | 11 | 16,794 | 0.375–0.67 | 0.4148 | 4,453 |
| **20:22:05.865** | **20** | **18** | **109,545** | **0.165–0.665** | **0.2492** | **47,192** |

By 20:23:21 it printed 0.68 again — **full reversion inside 75 seconds.**
Total across the episode: ~150,000 shares, **~52,400 SUSQies** handed to whoever
had bids resting.

### It was not news

Four independent checks, all pointing the same way:

1. **Every one of the 109,545 shares printed at a single timestamp**
   (`20:22:05.865`). That is one atomic order execution walking 18 price levels,
   not a crowd re-pricing a view.
2. **The Republican leg never moved.** Market 378 traded 0.315–0.325 before,
   during, and after. Real news that cut D from 0.68 to 0.17 would have pushed R
   toward 0.70. It did nothing.
3. **The pair became incoherent.** At the trough, `p_D + p_R = 0.165 + 0.32 =
   0.485`, implying a 51.5% chance that *neither* party wins a two-leg
   `Party Winner` race. Nonsense — exactly the Rhode Island tell in HANDOFF §3.2.
4. **It reverted completely**, which information does not do.

Diagnosis: a large market-sell order (or a sell with no limit) against a book
that was thin below 0.60. The seller gave away ~47,000 SUSQies in one click.

## How often does this happen?

A sweep has a clean signature in the tape with no book snapshot required: the
engine stamps every fill of one execution with the **same `createdAt`**, so a
sweep is a group of same-timestamp prints spanning multiple price levels. That
makes it detectable **retroactively over full history** — see `detect_sweeps.py`.

Scan of the first 30 markets by id, since the 16:00Z open, counting only sweeps
that reverted by more than 2 points:

```
38 reverting sweeps across 30 markets
~226,358 SUSQies transferred to the passive side
average ~7,545 per market, over ~5 hours
```

Largest single events found:

| market | time | shares | range | VWAP | recovered | to passive side |
|---|---|---|---|---|---|---|
| 151 Dem win U.S. House | 17:21:40 | 164,976 | 0.015–0.915 | 0.385 | 0.915 | **87,488** |
| 151 Dem win U.S. House | 16:20:01 | 50,074 | 0.005–0.915 | 0.117 | 0.933 | **40,848** |
| 153 Dem win U.S. Senate | 19:23:51 | 123,449 | 0.635–0.92 | 0.798 | 0.645 | 18,832 |
| 160 Gov. Arizona (R) | 19:32:35 | 40,311 | 0.13–0.83 | 0.496 | 0.165 | 13,348 |

Note the four chamber-control markets (151–154) are both the most liquid and the
most violently swept, so the 30-market average is biased upward; do not naively
extrapolate to all 237. But the order of magnitude is not in doubt.

## What a ladder would have captured

Replaying the 20:22:05 Alaska sweep against hypothetical resting bids:

| bid resting at | would have filled | cost | value at 0.68 recovery | gain |
|---|---|---|---|---|
| 0.60 | 106,348 sh | 63,809 | 72,317 | +8,508 |
| 0.50 | 101,119 sh | 50,560 | 68,761 | +18,201 |
| 0.40 | 97,890 sh | 39,156 | 66,565 | +27,409 |
| **0.30** | **95,785 sh** | **28,736** | **65,134** | **+36,398** |
| 0.25 | 49,959 sh | 12,490 | 33,972 | +21,482 |
| 0.20 | 47,194 sh | 9,439 | 32,092 | +22,653 |

A single bid at 0.30 would have returned **+36% of the entire account in one
event**, for 28,736 of deployable cash. Compare the dutch book's ceiling of ~500
SUSQies for the whole competition (`docs/dutch-book-assessment.md`).

## Why it is only capturable passively

**There is no reaction window.** The whole 109,545-share sweep executed at one
timestamp, and the price was back at 0.68 within 75 seconds. Against this API's
**2–17 second** latency, a bot that reads a book, notices the dislocation, and
sends an order arrives after the recovery. We cannot buy the dip; we can only
*already be there*.

That single fact determines the architecture: **resting ladders, re-posted
promptly, with capital free to absorb fills.** It also means raw speed buys us
little, which is a relief given the latency we measured.

## Operational consequences

1. **Recycle, do not hold.** After a sweep fill, sell back into the recovered
   book rather than holding to settlement. There was a 7,536-share print at 0.68
   ~75 seconds after the Alaska trough — ample liquidity to exit into. Recycling
   re-arms the ladder for the next event and converts paper gain into cash, which
   is the only thing that scores. Holding 95,785 shares to 2026-11-04 instead
   converts a 36% certain-ish gain into a 68/32 coin flip.
2. **Rung depth should scale to fair value, not be fixed.** The Alaska sweep
   reached 24% of fair value (0.165 / 0.68). An equivalent sweep on a 0.97
   contract would reach ~0.23. Ladder rungs at roughly 0.25–0.85 × fair.
3. **Depth matters more than tightness.** The 0.30 rung beat the 0.60 rung, and
   the 0.25/0.20 rungs captured nearly as much as 0.30 for a quarter of the
   capital. Size should increase as price falls (HANDOFF §5.2 is right) but the
   *deep* rungs are where the return on capital is.
4. **News risk is unhedgeable at fill time.** We cannot tell a liquidity sweep
   from real information before being filled — both look identical to a resting
   bid. Mitigations, in order of value:
   - Put deep rungs only on markets where fair value is robust (`safe`, fair >
     0.95). Alaska D at 0.68 is a *lean*: a genuine news event could justify 0.40
     there, so deep rungs on it carry real risk. The chamber markets (151–154)
     are swept hardest but are also `Freeform` with manual settlement — the worst
     combination for deep exposure.
   - **Check the complementary leg immediately after any fill.** If the other leg
     moved consistently, it was news and we should exit; if it did not move, it
     was liquidity and we should hold for the revert. This is the single most
     valuable post-fill signal and it is cheap — one read.
   - Cap exposure per race, since both legs of a race share one outcome.
5. **The over-commitment question is now urgent.** Resting bids do not reserve
   capital (§6 Q3), so ladders can be posted across many races at once — but a
   simultaneous multi-race sweep could fill past the cash balance, and the
   engine's behaviour then is unknown. With single events filling 28,736 of
   capital, this is no longer theoretical. Resolve it before scaling.

## Correction to the leaderboard reading

`docs/open-questions-answered.md` Q6 called the leaderboard "largely fictional"
on the strength of the VWAP-mark finding. That overstated it in one direction and
understated it in another:

- The mark really is cumulative VWAP (verified to 6 decimals), so it is
  **unrelated to fair value** — it can overstate a position badly (market 388's
  mark was 6.2x its best ask) *or* understate one, depending on whether early
  prints were above or below the current price. It is noise, not a systematic
  inflation.
- But the sweep gains above are **real economic value**, not marking artifacts.
  Buying at 0.117 something that recovers to 0.933 is a genuine profit.

So the leaders' P&L is probably a mix of real sweep capture and arbitrary marks.
The practical rules are unchanged — ignore the board, mark from the book, only
settled cash scores — but competitors who caught these sweeps are genuinely
ahead, not merely flattered by the mark.
