# Current strategy — living document

> **FINAL RESULT (all markets settled, checked 2026-11-11): rank 43 of 1,969, balance 198,939.93
> (+98,939.93, +98.9% on 100K).** Every election-day leg settled our way.
>
> **Status as of 2026-11-03: ladders done; one hold-to-settlement trade open.** The ladders were
> flat at **184,815.42** (+84.8%) from 2026-11-02 00:13Z; rank 28 on 11-03. At 12:58Z on 11-03,
> `sell_longshots.py` bought NO on 21 overpriced longshot legs (150.1K cost, 163,240 shares), cross-checked
> against Polymarket, and holds them to settlement: **+13.1K if all resolve NO**, about +9.8K expected. On 11-04 04:10Z the spare
> 32.9K bought YES on 7 called winners below Polymarket (+977 if all win).
> Nothing else is trading. Full timeline, account curve, incidents and lessons: **`docs/run-history.md`**.
> The sections below are the strategy as it ran. Final ladder flags:
> `--min-fair 0.70 --race-pct 0.85 --budget-pct 135 --mirror 0.5 --cheap 0`.

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
- **2026-10-02 22:09Z:** cash **129,998** (+30.0K realised overall).
  The run was then restarted with `--max-hours 48`, ending ~2026-10-04 22:09Z.
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

- **Where we quote:** the favoured leg of every race with fair ≥ 0.70
  (`--min-fair`). It was 0.90 until 2026-10-02 ~20:15Z, then 0.75 until
  2026-10-08 03:24Z. Lowered after a backtest showed the 0.70–0.75 band earns
  as much per race or more (`docs/mirror-and-arb-recheck.md` §8). On trial:
  compare a day's fills, stops and refusals, and revert if worse.
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
  `--race-pct 0.85 --budget-pct 135` (since 2026-10-06): each race may rest up to
  85% of cash, and the total resting can reach 135x cash. That was 1.0/120 before the cheap side. It was raised from 60x on 2026-10-02
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
- **Cheap side OFF since 2026-10-22 (`--cheap 0`).** First it was capped at NO < 0.10 (2026-10-21, `--cheap-max-no 0.10`). From 10-12 to 10-21, the cheap
  rungs at NO 0.10-0.25 took most of the big stop losses: 50-180K shares, 1-2 ticks each.
  Only rungs below 0.10 made money. Each pass also pulls buy rungs on races that
  no longer qualify (`pull_orphans`). See docs/loss-analysis-1013-1020.md.
- **Undocumented total cap at ~129x cash (found 2026-10-06).** When the cheap side
  went live at `--race-pct 1.0 --budget-pct 135`, only 22 of 357 cheap rungs
  rested. Past ~129x cash in total resting notional, the engine refuses new
  buys with 200/ok, `orderId: null`, `open: false`, and no error. Fix: we now run
  `--race-pct 0.85 --budget-pct 135` (~119x), and `apply_plan` counts refusals.
  The pass line shows `!! N REFUSED` and the jsonl has `refused`. After the fix,
  0 orders were refused and 244 cheap rungs rested on 55 favourites.
  - **Back at the cap, 2026-10-07 (~pass 128+, cash 178.6K).** Rung sizes grow
    with cash, so refusals returned: 1–2 rungs per race, 9 in pass 131 out of
    ~1,700 resting. Total resting buys were ~19.4M at price (~109x cash), so
    the cap is NOT simply 129x cash. It may be absolute, or tied to something
    else; this is unresolved. Decision: **keep race-pct 0.85**. Dropping to 0.80
    would shrink every rung ~6%, far more size than the few refused deep rungs.
    Revisit if refusals exceed ~5% of rungs per pass. `monitor_alerts.py` now
    alerts only when a pass total is ≥ 10.
- **Ladder matching uses the closest price (fixed 2026-10-06 17:53Z).**
  `ladder_matches` used to pair each wanted rung with the first resting order
  within 3 ticks. Cheap rungs (0.04/0.03/0.02/0.01/0.005) sit 1–2 ticks apart,
  so they were mis-paired, the last rung went unmatched, and ~80 ladders were
  reposted every pass: ~200 writes and 12-minute passes. It now pairs each
  rung with the closest-priced candidate. The pass after the fix: 25 writes,
  142 unchanged, 268s (before: 199 writes, 77 unchanged, 687s).
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

- **Cheap side: LIVE since 2026-10-06 (`--cheap 0.1`).** NO bids on each
  favourite at 0.005–0.05 (`CHEAP_RUNGS` in `sigcup/quoter.py`), i.e. YES offers
  above the ask. The idea came from `docs/leader-analysis.md`: rank 1 made +753K
  on 19.6M of volume in one day at a flat 3.84%. The first fills were Georgia D
  (ex947, 17,141 NO @0.045) and Delaware D (ex1042, 12,962 NO @0.095).
- **Chamber markets:** m151 was dumped to 0.64 and 0.72 today (~1M volume each).
- **Deeper mirror sizing:** longshot spikes reached 0.30–0.60 YES today, where
  the mirror's deep rungs carry little size.

- **Chamber-control markets (151–154).** They had the biggest sweeps on record
  (87K and 41K to the resting side), but they are excluded today: they have no
  `raceId` and settle manually.
- **Missing-third-leg view trades:** SD, ID and MS Senate have no Independent
  market, so an Independent win resolves both listed legs NO.
- **Fill-driven re-arming:** `sigcup/realtime.py` pushes fills in about 1s for
  free; today `rearm` polls positions.

## 6. Known risks and gaps

- **Genuine repricings now stop out** (see `docs/exit-management.md`, Stop-loss):
  underwater ≥3h with coherent legs, or ≥8h, drops the cost floor and pulls the race
  from the ladders until the position has fully closed, then the race is
  re-admitted automatically (since 2026-10-06). Manual control:
  `data/overrides.json` (hot-reloaded). **Since 2026-10-11: stop-loss v2**: no
  adds to positions ≥2 ticks under cost, stop exits sell in slices at the bid,
  stops cancel on recovery, tiny stops keep their race, `permanent_exclude`
  (RI Gov Independent). See `docs/exit-management.md`. 2026-10-06 stops: CO-08 R ≈ −450, SD
  Senate R ≈ −300 (8h cap), TN Senate R −4, NH Senate D ≈ 0.

- **Platform outages (2026-10-02/03).**
  - The fills endpoint froze at 21:48Z (see `docs/exit-management.md`).
  - At ~03:15Z a ladder post got a 404 whose body was `"error": "Not Found"`
    (a bare string). The client crashed parsing it (AttributeError), and the
    main loop caught only ApiError/OSError, so **the process died, exit thread
    included**. Fixed: the client normalises any error body, and both loops now
    catch every Exception.
  - Then the whole API went down: `sig.` returned 404 on every endpoint (site
    500), and `www.` returned 500 with our key.
  - Resting orders stay on the exchange through an outage, but nothing manages
    them. A watchdog (`scratchpad/wait_and_relaunch.sh`) polls once a minute and
    relaunches the quoter with the same flags when authenticated reads work.
  - Note: the quoter's startup `c.balance()` sits outside the retry loop, so it
    exits if the API is down at launch. That is why it needs a watchdog.

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

**Runs non-stop under a supervisor** (since 2026-10-06 ~03:30Z):

```bash
nohup caffeinate -ims ./supervise.sh >> data/supervisor.log 2>&1 &   # start
touch data/STOP                                                      # stop
```

- `supervise.sh` starts `run_quoter.py` with the live flags and **no
  `--max-hours`**, and relaunches it 30s after any exit (crash, hard stop, API
  down at startup), each time into a fresh `data/quoterN.log`.
- `touch data/STOP` asks the running quoter to finish its pass and stops the
  supervisor for good. Resting orders stay on the book.
- Alongside it: `watch_losses.py` (alerts to `data/watch_losses.log`) and
  `monitor_alerts.py` (one-process alert stream for monitoring sessions).
- Manual control without a restart: `data/overrides.json` (exit floors,
  `no_ladder`).
- **The Mac must stay awake and online.** On battery with the lid closed it
  sleeps whatever caffeinate says, and the bot pauses (2026-10-03 commute,
  2026-10-05 evening). An always-on server would remove this.

## Doc index

| File | What it holds |
|---|---|
| `HANDOFF.md` | Original brief: rules, payoff, pricing relationships |
| `docs/open-questions-answered.md` | API facts: auth, orders, capital, limits, marks, websocket, self-trade |
| `docs/sweep-opportunity.md` | Why sweeps are the opportunity; the Alaska event |
| `docs/dutch-book-assessment.md` | Cross-leg arb, assessed and rejected |
| `docs/exit-management.md` | Exit design, write priority, the lag and cancel-all bugs |
| `docs/leader-analysis.md` | How rank 1 made +753K in a day (inferred from market data) |
| `docs/mirror-and-arb-recheck.md` | Mirror ladder, arb re-check, lead-lag, min-fair 0.70 backtest |
| `docs/loss-analysis-1013-1020.md` | Losses 10-13→10-21, cheap-side cap/off, pre-election stop, Nov 2 review |
| `docs/run-history.md` | **Whole-run record: result, daily account values, timeline, lessons** |
| `docs/election-night-analysis.md` | How the big players earned on election night (direction vs. speed vs. Polymarket gap) |
| `docs/api-overview.md`, `docs/guide-*.md` | Platform reference |
