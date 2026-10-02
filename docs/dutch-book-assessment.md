# Is dutch-booking opposite legs a viable strategy here?

Assessed 2026-10-01 against live books. **Short answer: not as a primary
strategy. It is capital-bound to roughly a 0.5% total return, and one tick of
edge cannot survive this API's latency.** It stays worth running as a *detector*
for dislocations, where both edge and size get large.

## The arithmetic that kills it

Selling YES at `b` without holding YES *is* buying NO at `1 - b` — identical
cash flows, and the engine canonicalizes it that way
(`sell yes q@p ≡ buy no q@(1−p)`).

So hit the best YES bid on each of n mutually exclusive legs:

- **cost** = `Σ(1 - bᵢ)` = `n - Σb`
- **payout**: each NOᵢ pays 1 unless outcome i occurs. At most one can occur, so
  payout is `n - 1` when exactly one wins and `n` when none does — never less
  than `n - 1`.
- **profit** ≥ `(n - 1) - (n - Σb)` = **`Σb - 1`**, independent of n.

Worked example, Governor of Georgia (`b_D = 0.49`, `b_R = 0.515`, `Σb = 1.005`):
buy NO on D at 0.51 and NO on R at 0.485 for 0.995. D wins → 0 + 1 = 1. R wins →
1 + 0 = 1. Neither → 1 + 1 = 2. Worst case 1 against a cost of 0.995, so +0.005
locked, which is `Σb - 1`.

### Return on capital

The capital locked per share is the cost, `n - Σb`, which with `Σb ≈ 1` is
**about n - 1** — so:

$$\text{ROC} \approx \frac{\text{edge}}{n-1}$$

For a two-leg race that is the edge itself (0.5% per tick). **For a three-leg
race it is half that**, because the same one-tick profit requires locking ~2
SUSQies per share instead of ~1. Montana Senate, Nebraska Senate and RI Governor
are therefore the *least* attractive dutch books on the board, not equal ones.

Either way it is earned *once*: the position only pays at settlement on
2026-11-04, so the capital does not recycle.

That gives a hard ceiling. 112 of the 115 races are two-leg, so each share of
paired position consumes ~1 SUSQie, and 100,000 SUSQies holds at most ~100,000
shares of dutch book at a time:

$$\text{max total profit} \approx 100{,}000 \times \overline{\text{edge}} \approx 100{,}000 \times 0.005 = \mathbf{500\ \text{SUSQies}}$$

**0.5% on the whole account, for the entire competition**, assuming you could
find enough simultaneous arbs to deploy all of it — which you cannot.

## What the books actually offer

Scan of all 115 races (`scan_arb.py`), plus ~25 minutes of all-market
top-of-book sampling (`wide.jsonl`, 13 sweeps):

- **18 of 107 priced races** showed `Σbid > 1` at least once.
- **6.0%** of race-sweep observations (83/1373) had any arb present.
- Edges: almost all **exactly one tick (0.005)**; best observed 0.020 (FL Senate).
- A representative instantaneous snapshot:

| race | edge | size | profit | capital locked | ROC |
|---|---|---|---|---|---|
| Governor of Georgia | +0.0050 | 292 sh | 1.46 | 291 | 0.50% |
| U.S. House NY-17 | +0.0050 | 2,356 sh | 11.78 | 2,344 | 0.50% |
| **total available** | | | **13.24** | **2,635** | **0.50%** |

Thirteen SUSQies. Earlier sizing of the then-live hits gave the same picture:
Georgia 3.96, Texas Governor 7.59, both at 0.50% ROC.

## Four reasons it is worse than those numbers suggest

1. **[Corrected 2026-10-02: wrong. See `docs/mirror-and-arb-recheck.md` §2.
   Live websocket tracking found a median episode length of 79s, and many lasted
   minutes. The verdict stands for capital-efficiency reasons (§2.3 there).]**
   **Latency makes one tick unwinnable.** This API answers in **2–17 seconds**
   (measured: `/account` 17.5s, orderbook reads 2–5s, collector cycles swinging
   13–56s). Taking a one-tick arb means crossing to hit resting bids. By the
   time an order lands, a faster participant has pulled or improved them. On a
   one-tick edge there is no margin to absorb that — we would be the
   adverse-selected side, filling only when the quote was stale *against* us.
   This alone disqualifies steady-state tick-scraping.
2. **It is not actually riskless.** Settlement docs: a cancelled or N/A outcome
   **refunds the refundable cost** rather than paying a winner. The proof that
   "every leg but the winner pays 1" assumes normal resolution. Force majeure
   explicitly covers recounts and certification delays, and the four
   chamber-control markets settle manually. A 0.5% edge does not pay for that
   tail.
3. **Taking the arb destroys it.** Hitting those bids consumes exactly the
   liquidity that created the mispricing, so the opportunity does not repeat in
   that race until someone re-posts a crossed quote.
4. **It does not serve the objective.** Only the top 3 of 396 are paid, so the
   goal is maximising P(top 3), not expected value. Turning 100,000 into 100,500
   leaves you nowhere near the money. A strategy whose ceiling is 0.5% cannot
   win a convex-payoff contest regardless of how safe it is.

## Where the idea still earns its keep

- **Dislocation detection, not tick-scraping.** The structure is identical but
  the magnitude is not. When a thin book gets swept — HANDOFF §4's Rhode Island
  dump, where the D side printed 0.115 against a ~0.97 fair value — the
  cross-market sums blow out by *tens* of points, not one tick. A 0.10 edge is
  20x the ROC and the size is whatever the panic leaves behind. Run the scanner
  continuously with `--min-edge 0.05` and treat a hit as an alarm, not a routine
  trade.
- **Dominance violations** (HANDOFF §3.1) are the same family and sometimes
  wider: if YES on R trades above NO on D, buy NO on D and sell YES on R.
- **Races with a missing third leg** are a better structural edge than any bid
  sum. SD, ID and MS Senate have only R and D markets, yet an independent is
  live (and in SD/Idaho the Democrat withdrew). With `resolutionType: "Party
  Winner"`, an independent win resolves **both** legs NO and there is no third
  market to price it. So `p_R + p_D` is genuinely well below 1 there, and NO on
  the vacated-Democrat leg should price far above where a naive
  complement-of-R model puts it. That is a view trade with real edge, not a
  one-tick scrape.
- **Post, don't take.** The ladder quoter (§5.2) is the same risk appetite
  pointed the right way: resting a bid at 0.70 on a contract worth 0.97 earns
  27 points per fill, not half a point. Edges of that size are the only thing
  that scales to a winning balance, and they are immune to the latency problem
  because we are the resting side.

## Verdict

Use `scan_arb.py` as a monitor with a high threshold. Do not build a trading
loop around one-tick dutch books: the ROC is pinned to the edge, the capital
locks until November, the latency guarantees adverse selection, and the ceiling
is ~500 SUSQies against a field where the top 3 of 396 are paid.
