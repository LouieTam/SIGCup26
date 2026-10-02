# Mirror ladder and arb re-check (2026-10-02)

Two candidate strategies, tested with new tools against live data.

| Strategy | Verdict |
|---|---|
| Mirror ladder (NO bids on longshots) | **Go.** Flow is larger than the main ladder's: 1.3x over the full window and 2.8x in the last 12h (§1.4). Built behind `--mirror`, off until enabled. |
| Cross-leg dutch book (Σ bids > 1) | **Still rejected.** It is not too fast to catch, as first claimed. It loses on cash efficiency against the sweep ladder. |

---

## 1. Mirror ladder

### 1.1 Idea

The main ladder rests YES bids on a race's favourite, so it is filled when
someone **dumps the favourite**. The mirror rests NO bids on the race's
longshot legs, so it is filled when someone **aggressively buys a longshot**:
YES/NO confusion, or a fat-fingered market buy. Both pay if the favourite wins,
so they are one view on the race and fall under the same per-race budget.

### 1.2 Method (`analyze_mirror.py`)

- **Scope:** the races the bot quotes (favourite fair ≥ 0.90). Full trade tapes
  for the favourite and every longshot, from 2026-10-01 19:00Z.
- **Same depths on both sides:**
  - main: a YES bid at `d × fair_fav` fills on favourite prints ≤ that price;
  - mirror: a NO bid at `q = d × (1 − fair_ls)` is a YES offer at `1 − q`, and
    fills on longshot prints ≥ `1 − q`.
- **Capital:** 4,000 at every depth on each side. Profit = (fair − price) ×
  filled.
- **Caveats (they apply equally to both sides):**
  - volume traded past a price is an *upper bound* on what we would fill;
  - fair is today's mid, so a genuine repricing during the window counts as a
    fill.

### 1.3 Result (2026-10-01 19:00Z → 10-02 ~16:00Z, 54 races / 55 longshot legs)

| depth | main hit | main ROC | mirror hit | mirror ROC |
|---|---|---|---|---|
| 0.95 | 26/54 | 2.41% | 45/55 | 3.58% |
| 0.92 | 23/54 | 3.59% | 34/55 | 4.28% |
| 0.90 | 22/54 | 4.20% | 24/55 | 4.71% |
| 0.87 | 17/54 | 3.91% | 19/55 | 4.84% |
| 0.84 | 9/54 | 2.41% | 13/55 | 3.69% |
| 0.70 | 2/54 | 0.96% | 5/55 | 3.89% |
| 0.45 | 2/54 | 1.16% | 0/55 | 0 |
| 0.25 | 1/54 | 0.71% | 0/55 | 0 |

- **Totals:** main 41,827, mirror 54,984.
- **Sweeps that reverted:**
  - longshots: **94 up-sweeps, ~378K** handed to resting orders;
  - favourites: 66 down-sweeps, ~128K.
- **Takeaways:**
  - The mirror wins at every depth from 0.70 to 0.95 and fills in more markets.
  - It does not reach as deep: nothing filled at 0.45 or 0.25 of NO fair.
  - Buying pressure on longshots is the larger and more frequent flow.

### 1.4 Recent-window check (2026-10-02 04:00Z →)

This re-run tests whether the flow persists after day one, and rules out
first-day repricing dominating the result. Last 12h only; 50 races / 51
longshot legs; same method.

| depth | main hit | main ROC | mirror hit | mirror ROC |
|---|---|---|---|---|
| 0.95 | 12/50 | 0.82% | 33/51 | 2.25% |
| 0.92 | 3/50 | 0.52% | 14/51 | 1.44% |
| 0.90 | 2/50 | 0.45% | 7/51 | 1.34% |
| 0.87 | 2/50 | 0.43% | 5/51 | 1.46% |
| 0.84 | 1/50 | 0.38% | 2/51 | 0.75% |
| ≤ 0.70 | 0/50 | 0 | 0/51 | 0 |

- **Totals:** main 5,208, mirror **14,754**. That is 2.8x; over the full window
  it was 1.3x.
- **Sweeps that reverted:**
  - longshots: 46 up-sweeps, ~206K to resting orders;
  - favourites: 21 down-sweeps, ~67K.
- **Conclusions:**
  - **The mirror's lead holds and widens.** Longshot buying has faded less than
    favourite dumping.
  - **Flow is decaying on both sides** compared with day one, as HANDOFF §4
    predicted.
  - **Fills now cluster at 0.95 of fair.** Nothing filled at or below 0.70 on
    either side in this window, so size belongs at the shallow rungs.

### 1.5 Implementation (built, OFF by default)

- `run_quoter.py --mirror F` rests NO bids on every longshot leg of each quoted
  race, sized at `F × race budget`, split across the longshots.
- `sigcup/quoter.py` `build_mirror_ladder()` reuses `build_ladder` in NO terms:
  - NO fair = 1 − longshot fair;
  - best NO bid = 1 − best YES ask;
  - every rung strictly below it, i.e. every YES-equivalent offer strictly above
    the best ask.
- **Exits for NO holdings:** `unwind()` now exits each position on its own side.
  - Position side comes from `option`.
  - Book in NO terms: NO bid = 1 − YES ask, NO ask = 1 − YES bid.
  - The fills ledger gives NO held = −(net YES).
  - The rule is the same as for YES: one tick inside the ask, floored at cost.
- **Safety:** before this change, a NO holding would have received a YES sell.
  The engine canonicalizes that into a NO *buy*, so the position would have
  grown on every pass. `rearm()` and `held()` now track both sides, so a ladder
  repost never cancel-alls an exchange carrying a NO exit.
- **Offline tests passed:**
  - mirror rungs stay strictly inside the book;
  - a 1,000 NO holding @ 0.90 with YES 0.04/0.06 posts `sell no 1000 @ 0.955`;
  - when positions and the fills ledger disagree in sign, nothing is sold.
- **Went live 2026-10-02 ~17:05Z at `--mirror 0.5`.** Pass 1 posted 360
  rungs (785 resting, up from 458). Confirmed that resting NO orders report
  `side: no`, `action: buy`, `priceLimit` in NO terms. Pass 2: 71 unchanged,
  23 writes, no churn.
- **Unverified live (originally both; the first is now confirmed above):**
  - whether `priceLimit` on resting NO orders is reported in NO terms. Inferred
    from canonicalized order 1466649 (`buy no lim 0.05`), so `ladder_matches`
    should compare like for like;
  - the `option` value the positions endpoint uses for NO holdings.
  Check both on the first live mirror fill.

---

## 2. Cross-leg arb re-check (`monitor_arb_ws.py`)

### 2.1 Method

- **Data:** all 230 race markets subscribed over the Realtime websocket (free
  against the read limit), 30 minutes, 2026-10-02 ~15:45–16:15Z. Every quiet
  leg was seeded from bulk prices.
- **Episodes:** every period where Σ best bid > 1 was tracked from open to
  close. Executable size comes from walking the books while the marginal sum
  still clears 1.
- **Our own resting orders are subtracted.** Self-trade prevention means we can
  never take them, and the first smoke run showed our 64K Massachusetts
  Governor exit as an "arb".

### 2.2 Result (bid side: buy NO on every leg)

- **Volume:** 152 episodes across 57 races. Median edge 0.005 (one tick),
  maximum 0.045.
- **Duration:** median **79s**; 143 of 152 lasted ≥ 10s; several were still
  open after 30 minutes (Kansas, Maine, FL-13 Senate/House).
- **Best episode per race** (repeat episodes reuse the same liquidity):

| edge ≥ | races | profit | capital locked to 11-04 | return |
|---|---|---|---|---|
| any | 57 | 2,439 | 176,112 | 1.38% |
| 0.01 | 35 | 2,335 | 153,560 | 1.52% |
| 0.02 | 17 | 2,061 | 111,697 | 1.85% |
| 0.03 | 5 | 1,241 | 50,013 | 2.48% |

### 2.3 Verdict: still rejected, for a different reason

The earlier write-up (`docs/dutch-book-assessment.md`) gave latency as its main
reason: "one tick cannot survive 2–17s". **That was wrong.** Arbs last a median
79s, easily long enough for a REST order to land.

What rules it out is cash. Arbitrage cash is locked until 2026-11-04 at 1.4–2.5%.
Sweep-ladder cash is recycled within minutes: Massachusetts Governor D made
~7K on 93K in ~30 minutes, and that cash was then free for the next sweep.
Because the engine shrinks every order to available cash, a sweep fills only
as far as our cash covers. Every unit of cash locked in an arb comes out of the
next sweep's fill.

Revisit only if cash sits idle, which it doesn't under the current overbooked
configuration, or if an edge appears that is a dislocation, not a tick (≥ 0.05).

### 2.4 Ask side (buy YES on every leg): not an arb

45 episodes, but they are not riskless, and the largest are artifacts:
- **Massachusetts Governor:** our own exit, repricing faster than the 5-minute
  own-order refresh.
- **South Dakota Senate:** has a live Independent (Bengs) with no market of
  their own, so Σ YES < 1 is a fair price for "nobody listed wins". See the
  missing-third-leg note in `docs/dutch-book-assessment.md`.

---

## 3. More markets: the 0.60–0.90 band (2026-10-02)

The ladder only quotes races whose favourite is at fair ≥ 0.90 (`--min-fair`).
`analyze_mirror.py --min-fair 0.60 --max-fair 0.90` uses the same method over
the last 12h (from 04:00Z), for 56 races and 57 longshot legs.

| favourite fair | races | races with any fill | main | mirror | per race |
|---|---|---|---|---|---|
| 0.60–0.75 | 24 | 13 | 8,075 | 8,839 | 705 |
| **0.75–0.90** | 32 | 22 | 11,292 | 16,675 | **874** |
| ≥ 0.90 (quoted today) | 50 | – | 5,208 | 14,754 | ~400 |

**Sweeps that reverted:**

| band | favourite dumps | longshot buying |
|---|---|---|
| 0.60–0.90 | 42, ~199K | 54, ~185K |
| ≥ 0.90 | 21, ~67K | 46, ~206K |

**By depth (0.60–0.90):**
- **Main:** ROC 1.5–2.0% at every rung from 0.84 to 0.95, against 0.4–0.8%
  at fair ≥ 0.90.
- **Mirror:** 1.5–2.3%.
- Nothing filled at or below 0.70, apart from one mirror fill.

**Conclusions:**
- The mid band has about **2.2x the measured flow** of the band we quote, and
  favourite dumps there are ~3x larger. That fits the leaderboard lesson: the
  big dumps happen where the price has room to fall.
- 0.75–0.90 earns the most per race and carries less news risk than
  0.60–0.75. Profit is spread across many races (the top race contributed
  ~5.7K of 44.9K).
- **Cost:** more news risk. A real repricing looks identical to a dump at
  fill time, and the cost floor then holds the position, possibly to
  settlement. More ladders also use more writes: about one extra batch per new
  leg on the first pass, then drift reposts.
- **Constraint:** cash, not coverage. Orders are shrunk to available cash, so
  more markets raise the *chance* of catching a dump, but not the size of any
  one fill. Fast exits stay as important as coverage.

**Decision:** went live at `--min-fair 0.75` on 2026-10-02 ~20:15Z, with all
positions closed and cash at 121,979.

## 4. Live mirror lessons (2026-10-02)

- **NO positions come back as `option: "YES"` with a NEGATIVE `quantity`.**
  The lots carry `side: "NO"`, and `avgCost` is in NO terms. The first mirror
  fill (ex991, Hawaii Governor R, 52,695 NO @ 0.872) was mis-sided as YES.
  Because positions and the fills ledger disagreed in sign, the exit pass
  correctly refused to sell, so nothing was oversold, but the position had no
  exit for ~1.5h. `pos_side()` now uses the sign of `quantity`.
- **After the fix:** the first live NO exits filled at once: 3,000 @ 0.90 on
  ex991 and 322 @ 0.93 on ex1069, both above cost.
- **First hour of the mirror:** NO fills on ex991 (52,695), ex870 (27,325)
  and ex1069 (1,296), about 81K of shares altogether. The mirror catches flow
  quickly, as the tape replay predicted.
