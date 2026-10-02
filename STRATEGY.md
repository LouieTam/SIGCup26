# Current strategy — living document

**Read this first.** `HANDOFF.md` is the original brief from 2026-10-01 and is
kept for its rules and pricing background. This file is the current state of
play. The measured evidence behind each decision lives in `docs/` (index at the
bottom). Last updated 2026-10-02 ~16:00Z.

---

## 1. Objective

- Final rank is the **settled cash balance** after every market resolves
  (2026-11-04). Only the top 3 are paid, so the goal is P(top 3), not expected
  value.
- **The leaderboard is a valid reference** (verified 2026-10-02).
  `pnl` = realised trading (cash) + open positions at the exchange's mark −
  100K. For accounts that recycle their gains it is essentially real, so the
  leaders' +400K–670K is real trading. For our own sizing we still mark
  positions from the book, never from `currentPrice`. See
  `docs/open-questions-answered.md` Q6.
- **Lesson from rank 5** (+500K on only 270K volume): deep fills on dumps,
  closed out by buying the opposite side (paid 1 per pair, with only the cheap
  purchase counting as volume), can earn many times their volume. That argues
  for **depth on the sides where dumps actually go deep**, which is where the
  mirror ladder and lower-fair markets come in.
- **Target:** the top 10 were at +243K to +673K on 2026-10-02 ~16:30Z; we were
  rank 35 at +17.6K.
- **Plan (user decision 2026-10-02): realised trading gains, not directional
  bets.**
  - Every market resolves on the final day, so a directional bet realises
    nothing before 11-04 and cannot explain the leaders' gains.
  - We grow cash by capturing dislocations and recycling capital: sweep ladder,
    mirror ladder, fast exits.
  - Positions still held on 11-04 do pay at settlement, so holding is an option
    at the end, not a strategy.

**Where we are:** started with 100,000.
- **Overnight 10-01 → 10-02:** +8,488 realised.
- **2026-10-02 ~16:00Z:** 107,185.
- **Mirror live from 17:05Z** (cash 103,318). Its first fills (ex991 52.7K NO,
  ex870 27.3K NO, ex1069 1.3K NO) and a 44K YES fill on ex1025 were all
  exited by ~20:00Z.
- **2026-10-02 ~20:00Z:** cash **121,979**, no open positions. That is
  **+22.0K realised** overall, of which **+18.7K came in the ~3h after the
  mirror went live**.

---

## 2. What runs, in priority order

### P1 — Sweep ladder (core, live)
**Why it works:** large market sells walk thin books and revert within ~75s,
transferring value to whoever was already resting. For example, Alaska Senate
D went from 0.68 to 0.165 and back in 75s (~52K to the resting side), and about
226K moved this way in 30 markets in 5h. It is only capturable passively: the
API takes 2–17s per call, so we cannot react in time (`docs/sweep-opportunity.md`).

- **Where we quote:** the favoured leg of every race with fair ≥ 0.75
  (`--min-fair`; 0.90 until 2026-10-02 ~20:15Z).
  - Fair is the leg's mid, averaged with 1 − Σ(sibling mids).
  - A race is skipped if the legs disagree by more than 0.08.
  - Fair is snapped to a 0.02 grid, so normal book wobble doesn't trigger
    reposts.
- **Rungs** (fraction of fair, size weight):
  - 0.95/2, 0.92/3, 0.90/4, 0.87/3, 0.84/2: about 90% of the size, in the band
    where flow was measured. 0.90 of fair had the best ROC (4.4%).
  - Tail rungs 0.70/0.4, 0.45/0.3, 0.25/0.3: in fair ≥ 0.90 races nothing
    traded below 0.78 of fair, so these are cheap lottery tickets only.
  - Every rung sits strictly below the best bid, so nothing crosses.
- **Overbooked on purpose (user decision).** The current flags are
  `--race-pct 1.0 --budget-pct 120`: each race may rest up to 100% of cash, and
  the total resting can reach 120x cash. It was raised from 60x on 2026-10-02
  ~20:30Z: with the mirror at 0.5, each race uses ~1.5x cash, and 60x had been
  silently capping us at ~40 races (see the lesson below). Measured engine behaviour (10-01):
  - each order is **shrunk at placement** to at most 100% of cash, and the
    response still reports the requested quantity;
  - there is **no aggregate admission limit**;
  - once total resting notional exceeds cash, **buys that need to execute are
    silently blocked** (`orderId: null`, no error);
  - **sells are unaffected** (verified at 72.8x overbooking), so exits always
    work.
  - So a sweep hitting several races at once is rationed by the engine.
- **Proof it works:** 2026-10-02 15:26Z on Massachusetts Governor D. 110,324
  shares filled at an average of 0.842 against a recovered price of about 0.905,
  roughly +7K, and nearly all of it was exited within 30 minutes
  (`docs/exit-management.md`).

### P2 — Exits (live)
**Why:** sell into the recovered book rather than holding to settlement. That
turns paper gains into cash, which is the only thing that scores, and frees the
ladder for the next sweep.

- **Price:** a YES sell one tick inside the best ask, floored at cost + 0.005,
  so we never realise a loss.
- **Same trade as a NO bid.** This order is economically identical to "bid NO
  one tick above the best NO bid, then the YES+NO pair pays out 1". The engine
  canonicalizes `sell yes @ p` ≡ `buy no @ 1−p`. We submit the YES-sell form
  because selling held shares needs no cash, and sells are never blocked by
  overbooking, whereas buys are. (Commits f29ca72, 889d4e3.)
- **No separate hybrid.** The old `--cross-exit` hybrid (cross the bids above
  cost, rest the remainder) was replaced by this single rule. When the spread
  is one tick, `ask − tick` equals the best bid, so the exit sells immediately;
  otherwise it rests as the best offer.
- **When:**
  - right after every ladder post, if some position grew (`rearm`);
  - every 120s on the exit thread.
- **Size:** `min(positions, fills ledger) − resting sells`, which allows safe
  top-ups.
- **Write priority:** exits get it; ladder writes leave 4 of the 30
  writes/min free.

Full design and the bugs behind it are in `docs/exit-management.md`.

### P3 — Unattended operation (live)
- **Errors:** transient API errors never end the run. It backs off to a 300s
  cap and retries forever. (A 500-storm once took the API down for 8 hours.)
- **Circuit breakers:**
  - a hard cash floor (`--cash-floor-pct`, now 2%);
  - a deploy cap (`--max-deploy-pct`, now 1.0, i.e. effectively off).
- **Rate limits:** per account, **100 reads + 30 writes per minute** shared
  across every process.
  - The live bot uses about 80 reads/min.
  - Research scripts must throttle (`c._reads = _Budget(12)`) or use the
    Realtime websocket, which is free.
  - A 429 means a 60s `Retry-After`, which stalls the bot's exits.

---

## 3. Evaluated and rejected (don't re-litigate without new data)

| Idea | Verdict | Evidence |
|---|---|---|
| Dutch book (Σ bids > 1 across a race's legs) | Edges are ~1 tick. Capital is locked until 11-04, so the ceiling is ~500 total. Not riskless (refunds on N/A). Re-checked 2026-10-02 with live books: catchable (median 79s), but locked cash at 1.4–2.5% loses to recycling it through sweeps. | `docs/mirror-and-arb-recheck.md` §2 |
| Buying YES and NO in the same market | Doesn't exist: one YES-denominated book, so taking both sides costs 1 + spread. Scanned all 237 markets, minimum 1.0050. | commit f7a8eaa |
| Market making as the main engine | The whole platform's spread pool was ~281K over the first 10h, less than rank 4's P&L alone. The leaders' gains came from dumps, not spread. Relative spread is 33% below price 0.10 but 1% above 0.90. | `analyze_mm.py`, f7a8eaa |
| Crossing to exit | Gives up ~75% of the edge: bid depth above our cost is a few hundred shares. Exit passively. | d217942, 6d35315 |
| Deep rungs in high-fair races | Dead capital: nothing traded below 0.78 of fair in 20 markets. | `analyze_depth.py`, 10c79eb |
| Directional / concentrated bets as the way to climb | Rejected by the user 2026-10-02: markets resolve on the final day, so they realise nothing before 11-04 and cannot be what drives the leaders. | §1 |

---

## 4. In progress

1. **Mirror ladder:** resting NO bids on the longshot legs of the same races,
   to catch someone aggressively *buying* a longshot. Tape replay
   (`analyze_mirror.py`) shows mirror flow larger than main-ladder flow: 1.3x
   over the full window and **2.8x over the last 12h**. Overall flow is fading,
   and it now fills mostly at 0.95 of fair. **LIVE since 2026-10-02 ~17:05Z at `--mirror 0.5`** (`docs/mirror-and-arb-recheck.md`).
   Verified live: resting NO orders report `priceLimit` in NO terms, so there
   is no repost churn (pass 2: 71 unchanged). Still to verify on the first
   mirror fill: how a NO *position* is reported, and its NO-side exit. Implementing it needs exits for NO positions. The exit pass now
   refuses to touch non-YES positions, because a YES sell against a NO holding
   is canonicalized into a NO *buy*.
2. **Arb re-check: done, still rejected.** Arbs last a median 79s, so they are
   catchable, which corrects the old latency argument. But they lock cash until
   11-04 at 1.4–2.5%, and the sweep ladder recycles that same cash within
   minutes (`docs/mirror-and-arb-recheck.md` §2).

3. **More markets.** Races with favourite fair 0.60–0.90 showed ~2.2x the flow
   of the ≥ 0.90 band over the last 12h. 0.75–0.90 earns the most per race
   (`docs/mirror-and-arb-recheck.md` §3). **LIVE since 2026-10-02 ~20:15Z at `--min-fair 0.75`** (it was 0.90), started
   with all cash free.

## 5. Not yet built: candidates for what comes next

- **Chamber-control markets (151–154).** They had the biggest sweeps on record
  (87K and 41K to the resting side), but they are excluded today: they have no
  `raceId` and settle manually.
- **Missing-third-leg view trades:** SD, ID and MS Senate have no Independent
  market, so an Independent win resolves both listed legs NO.
- **Fill-driven re-arming:** `sigcup/realtime.py` pushes fills in about 1s for
  free; today `rearm` polls positions.

## 6. Known risks and gaps

- **The global budget cap can silently undo other settings.** It admits races
  by descending fair until full. When the mirror went live, the cap trimmed
  the main ladder from ~50 races to ~40, which dropped Massachusetts Governor,
  the market that had just given us the 110K sweep. Lowering `--min-fair` to
  0.75 then added nothing. After any sizing change, check the
  `global budget … exhausted` count in the skip list.

- **News vs liquidity:** a fill can't be classified as one or the other when it
  happens. Mitigations:
  - only quote fair ≥ 0.90;
  - the exit floor holds a position that moved against us rather than selling
    at a loss (e.g. South Dakota Senate R, filled at 0.895 and later marked
    0.85).
- **Restarts reset the deploy-cap baseline.** `start_cash` is read at launch,
  so `deployed` is relative to that restart. This doesn't matter while the cap
  is 1.0.
- **Settlement:** an N/A or cancelled outcome refunds cost rather than paying 1.
  Chamber markets and runoffs settle manually.
- **Self-trade prevention:** our orders never fill each other. A crossing
  remainder is silently cancelled (`terminalReasonCode: SelfTradePrevented`).

## 7. Operations

```bash
# live command (logs: data/quoterN.log; per-pass JSON: data/quoter.jsonl)
nohup caffeinate -ims python3 -u run_quoter.py --live --race-pct 1.0 --budget-pct 120 \
  --max-deploy-pct 1.0 --cash-floor-pct 0.02 --shrink-tol 0.95 --min-fair 0.75 \
  --mirror 0.5 --unwind --unwind-interval 120 --min-exit-edge 0.005 --reprice-ticks 2 \
  --interval 180 --max-hours 24 --log data/quoter.jsonl > data/quoterN.log 2>&1 &
```

- **Stopping:** send SIGTERM. It finishes the current pass, then exits.
  Resting orders stay on the book, and the next process picks them up.
- **Health:** `grep -E "^pass|rearm|TOP-UP|OVER|FAIL" data/quoterN.log`
- **Fills in real time:** `watch_fills.py` (Realtime websocket, observe-only)

## Doc index

| File | What it holds |
|---|---|
| `HANDOFF.md` | Original brief: rules, payoff, pricing relationships |
| `docs/open-questions-answered.md` | API facts: auth, orders, capital, limits, marks, websocket, self-trade |
| `docs/sweep-opportunity.md` | Why sweeps are the opportunity; the Alaska event |
| `docs/dutch-book-assessment.md` | Cross-leg arb, assessed and rejected |
| `docs/exit-management.md` | Exit design, write priority, the lag and cancel-all bugs |
| `docs/mirror-and-arb-recheck.md` | Mirror ladder + arb re-check results (pending) |
| `docs/api-overview.md`, `docs/guide-*.md` | Platform reference |
