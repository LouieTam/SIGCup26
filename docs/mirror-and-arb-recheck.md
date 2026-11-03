# Mirror ladder and arb re-check (2026-10-02)

> **Caveat found later the same day:** every replay in §1 and §3 read at most 5
> pages (1,000 trades) per leg, and the trades endpoint pages newest first. On
> busy legs the OLDEST part of each window was silently dropped (ex1072 missed a
> 00:27Z sweep to 0.46). Main-versus-mirror comparisons are biased alike on both
> sides. Depth profiles, especially "nothing fills below 0.80", are biased
> against deep rungs. `analyze_mirror.py` now warns on truncation; see §5 for
> the full-depth re-run.

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

## 5. Full-depth re-run and the new rung weights (2026-10-02 00:00–22:00Z)

`analyze_mirror.py --min-fair 0.75 --max-pages 15`: 84 favourites, 86
longshot legs, fine depth grid.

| depth | main hit | main ROC | mirror hit | mirror ROC |
|---|---|---|---|---|
| 0.95 | 47/84 | 2.37% | 67/86 | 3.38% |
| 0.92 | 23/84 | 1.90% | 45/86 | 3.40% |
| 0.90 | 17/84 | 1.74% | 28/86 | 3.04% |
| 0.87 | 9/84 | 1.39% | 16/86 | 2.62% |
| 0.84 | 5/84 | 1.13% | 10/86 | 2.21% |
| 0.80 | 2/84 | 0.60% | 6/86 | 1.74% |
| 0.75–0.55 | 0 | 0 | 1–2/86 each | 0.8–1.6% (best 0.60) |
| ≤ 0.50 | 0 | 0 | 0 | 0 |

**Totals** (the truncated run had main 30.2K, mirror 35.8K):
- main 30,677;
- **mirror 75,408**.

**Reverting sweeps:**
- longshot buying: 173, worth 679K;
- favourite dumps: 121, worth 303K.

**New `MIRROR_RUNGS`** (`sigcup/quoter.py`):
- weight moves to the 0.84–0.95 band (3/3/3/2.5/2);
- 0.80 at 1.5;
- the dead 0.45/0.25 tail (zero fills in every window) moves to 0.70/0.60
  (0.7/0.8), which is where the big longshot spikes landed.

The main ladder's `DEFAULT_RUNGS` stay as they are: below 0.80 the main side
filled nothing.

**Built-in bias of every replay (not only truncation).** Rung prices are set
relative to the *current* fair, so a fill counts only when the price has come
back past it. A fill that was followed by a lasting move against us is never
counted. The replays therefore cannot measure adverse selection. Treat them as
a ranking of where flow is, not as a P&L forecast. Live fills so far have been
mostly profitable, but that is the real test.

## 6. Where the realised P&L came from (replayed 2026-10-03 14:20Z)

`analyze_realized.py` replays the complete transaction history
(`coverage.complete: true`; 2,970 trades, 2026-10-01 19:52Z → 10-03 14:20Z)
with average cost per exchange.

| source | realised | share |
|---|---|---|
| direct sells of YES holdings (main ladder) | +27,777 | 51.3% |
| direct sells of NO holdings (mirror ladder) | +26,343 | 48.7% |
| YES/NO conversions (buying the other side, pairs paid 1) | −10 | ~0% (4 trades) |
| **total** | **+54,110** | |

- **Cash reconciliation (2026-10-03 ~11:50Z): exact to 2 SUSQies.**
  100,000 opening deposit − 1,616,508 buys + 1,579,853 sells + 1,938
  explicit conversion payouts = 65,283. Less 12,687 of fills the transaction
  feed had not yet recorded (mostly ex1054, 14,058 NO) gives 52,596, against
  52,598 actual. The history holds no other events (the only non-trade is the
  OPENING_BALANCE deposit). Nothing is missing from the P&L.
- **"Conversion" is inside the sells.** We hold only one side, so an exit
  "sell NO @ 0.89" rests as a YES bid at 0.11. When it is hit, we receive YES,
  and the YES+NO pair pays 1. The platform records that as SELL @ 0.89, with
  no separate payout event. So essentially all realised P&L is pair-conversion
  profit labelled as sells. The explicit 1,938 of payouts is exactly the
  ex1059 short (night of 10-01) unwinding against YES.
- **Exits are direct sells, not conversions.** The exit is economically the
  same as "bid the other side and redeem the pair" (`STRATEGY.md` §2), but it
  is submitted as a sell of the held side, and it settles as one. The 4
  conversions came from incidental overlaps and net −10.
- **The mirror earned as much as the main ladder in ~21 hours live.** It went
  live 2026-10-02 17:05Z.
- **One sell below average cost in the whole history:** ex971, 10 YES @ 0.955
  against an average of 0.97, −0.15, on 2026-10-02 00:02Z. That was before the
  current exit rule.
- **Best markets:**
  - ex1059 MA Governor D +7.0K;
  - ex1076 RI Senate D +4.4K;
  - ex1071 NH Senate R (mirror) +4.2K;
  - ex1051 TN Senate D +4.2K;
  - ex1074 RI Governor R (mirror) +3.9K.
- **Reconciliation:** the replay's +54.1K is ~2.5K above the account's gain at
  platform cost basis (+51.6K). History is complete; the gap is the cost-basis
  method on the ~84K still open (average cost here, the platform's lots
  there). It vanishes as those positions close.

`watch_losses.py` now runs alongside the quoter. It alerts on any sell or
conversion below average cost, any open position with its bid more than 3
ticks under cost, and the quoter process dying.

## 7. Lead-lag between legs (2026-10-07 09:50–10:55Z)

**Question (user):** e.g. Vermont Governor: the NO on R and the YES on D should
price the same event. Is there an arb, or does one leg lag the other in a way a
taker could trade within minutes, without holding to settlement?

**Vermont snapshot (09:4xZ):** R bid/ask 0.735/0.74, D 0.275/0.28.
- Σ bids = 1.010. Buying NO on both costs 0.99 and pays ≥ 1, but only 307
  shares are available at +0.01, and the payout waits until 11-04.
- The web UI's "R No 26%" vs "D Yes 28%" compares a NO *ask* with a YES *ask*.
  The difference is mostly spread.
- The verdict in §2.3 stands: the edge is real but tiny and locks cash.

**Method:**
- `record_tape_ws.py` records every top-of-book change on all 233 race legs
  (115 races) from the Realtime market channels. It is read-only and spends no
  REST budget.
- `analyze_leadlag.py`:
  - **Episode:** the race's Σ mids moves ≥ `--dev` from its 30-min median, with
    one leg (the leader) having moved first in that direction.
  - **Trade:** the lagging sibling, toward coherence, entered 15s later at the
    touch.
  - **Exit:** at 1/2/5/10/30 min, at the opposite touch (taker) and at mid.

**Result (65 min, 25,929 updates):**
- |dev| ≥ 0.02: **1 episode** (Alaska Senate, +0.020). The lagger never moved;
  the taker result was −0.005 at 1–2 min and 0 at 5–10 min.
- |dev| ≥ 0.01: 14 episodes.
  - **The lagger followed in 7%; the leader reverted in 29%** at 1–5 min, and
    67% at 30 min.
  - Median taker P&L was **−0.005 a share at every horizon**: the spread, with
    no catch-up to pay for it. Even exiting at mid was −0.0025.
  - On min(touch, 5K) shares per episode, that totals −246.

**Reading:**
- In this hour, siblings do not follow a jump. When the race stops adding up,
  the gap persists, or the jumping leg comes back.
- So the cross-leg gap is not a lead-lag signal. It is mostly noise of a tick
  or two, and the leg that moved is as likely to be the "wrong" one.
- **Caveat:** this was one quiet hour, at about half the 10-02/04 volume. The
  big dislocations (sweeps) come a few times a day and are already harvested
  passively by the ladders; this tape caught none.
- A 24h tape would settle whether lead-lag appears around sweeps. Recording is
  free (websocket), so it is the cheap next step if this is revisited.

## 8. Backtest: lowering `--min-fair` to 0.70 (2026-10-07, window 10-05 00:00Z → 10-07 ~23:00Z)

**Setup:**
- `analyze_mirror.py`, comparing the two bands over the same window, with
  4,000 cash per rung per leg. Profit = (current fair − rung price) × volume
  traded through the rung.
- Each run: `--min-fair/--max-fair`, `--max-pages 8`.
- **New band 0.70–0.75: 9 races.** Current band 0.75–0.90: 41 races.

| per leg | 0.70–0.75 (new) | 0.75–0.90 (current) |
|---|---|---|
| main ladder | 0 hits (0/9) | 633 total, 4/41 hits, all at 0.95 |
| mirror | **98** (980 / 10 legs) | 20 (860 / 42 legs) |
| cheap side, live rungs (+3,4,6,8,10 ticks) | **400** (3,604 / 9) | 332 (13,593 / 41) |
| reverting sweeps (value to the passive side) | main DOWN 2 (3.5K), mirror UP 4 (10.6K) | main DOWN 1 (1.0K), UP 1 (9.9K); mirror UP 3 (6.4K) |

**Reading:**
- Per leg, the new band earns about as much as the current one, or more:
  - the cheap side is similar (+20%);
  - the mirror is ~5x;
  - there are more reverting sweeps per race.
- Main-ladder fills were rare in both bands: this was the quiet, half-volume period.

**Caveats:**
- These are upper bounds: queue position is ignored and all volume past a
  price counts as ours.
- Fair is TODAY's mid, so a race that genuinely repriced shows no loss here.
  The stop-loss costs seen live are mostly cheap-side fills on 0.80–0.85
  favourites, and they are NOT in these numbers.
- Truncation (8 pages) dropped the oldest trades on 12 + 26 busy legs, which
  undercounts both bands.
- Only 9 races, so this is a small sample.
- **Capacity:** the bot is already at the engine's resting cap (refusals since
  10-07, §STRATEGY). Nine more races add rungs that compete for that cap, so
  some may be refused unless `--race-pct` is trimmed.

**Live result after ~23h at 0.70** (from `data/quoter.jsonl`, value at cost =
cash + cost basis):

| window (23.3h each) | value at cost | change | buy fills | buy notional |
|---|---|---|---|---|
| before: 10-07 04:02Z → 10-08 03:21Z (0.75) | 185,199 → 187,385 | **+2,186** | 318 | 176.6K |
| after: 10-08 03:21Z → 10-09 02:42Z (0.70) | 187,385 → 190,284 | **+2,899** | 271 | 86.0K |

- About a third more gain on less notional.
- The before window held both the West Virginia sweep (+~1.7K) and the two
  big stop-losses: Iowa −483 and Rhode Island Senate −633.
- The after window's stops were all small: Iowa −~95, Georgia −~115, PA-07
  −34, Texas Governor (a new-band race) ~0, and the rest ~0.
- Refusals: 2 small passes (3 orders) in 23h.
- One day each, dominated by single events, so this is not a statistically
  meaningful A/B test. But no sign the new band hurts. **Decision: keep 0.70.**
