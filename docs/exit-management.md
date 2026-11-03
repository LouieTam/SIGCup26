# Exit management — how swept fills get recycled

Written 2026-10-02. Covers `unwind()` / `rearm()` in `run_quoter.py` and the
write scheduling in `sigcup/client.py` + `sigcup/quoter.py`. Read with
`docs/sweep-opportunity.md` (§"Recycle, do not hold" is the reason exits exist).

## The rule

Post a YES sell one tick inside the best ask, floored at `avgCost + min_edge`
(never realise a loss). Identical to a NO bid one tick above the best NO bid.
Exits are sells of shares we hold, so they need no cash — they still execute
when resting ladder notional exceeds cash, which silently blocks buys.

## When exits are posted

1. **After every ladder post** (`apply_plan(after_write=rearm)`). One positions
   read; the full exit pass runs only if some position grew. The ladder phase
   takes minutes, and a sweep reverts in ~75s, so waiting for the timer was too
   slow. First live trigger: `[rearm] new fills on ['892']`, 2026-10-02.
2. **Every `--unwind-interval` seconds** on the exit thread (forced; this is
   what reprices stale exits).

Both paths share `unwind_lock`, taken non-blocking. Two concurrent passes can
each see the same uncovered position and both post a sell, which flips us short.

## Write priority

30 writes/min per account. Ladder writes are marked low-priority
(`c.low_priority.on`) and leave `WRITE_RESERVE = 4` slots per minute free, so an
exit never queues behind a ladder repost.

## Sizing: `uncovered = held - resting sells`

`held = min(positions endpoint, fills ledger)`. **Neither source is reliable
alone:**

- The positions endpoint lags fills. It caused the ex1059 short (2026-10-02
  ~04:00Z): positions read 2,236 while the exit had already sold down to 1,638,
  and a top-up sized from it oversold. The engine canonicalizes an oversized
  YES sell into a complement NO buy, so the excess became a 1,938-share short.
- The fills ledger lags too. Observed on ex1059 at 2026-10-02 ~15:40Z: positions
  76,241 against a ledger of 76,841.

- The fills ledger can also miss a **fresh** fill entirely for a while: ex880
  NO, 2026-10-02 ~20:20Z, positions 3,249 against a ledger of 0. The exit
  waited one timed pass (≤ 120s) and then posted. That is the cost of the min()
  rule: a new fill can sit uncovered for up to one `--unwind-interval`.

- **The ledger can stall for much longer.** On ex963, 2026-10-02 ~23:00Z, the
  newest fill the endpoint returned was from 21:27, ~90 minutes earlier, while
  positions showed 6,366 new NO. Under a strict min() that position had no
  exit at all.

**Fallback (`held_qty`, added 2026-10-02):** positions only reads stale-HIGH
right after one of *our* exit sells fills, because exits are the only sells we
make. So when nothing is resting on that exchange side, and we have not placed
or cancelled a sell there for `QUIET_S` = 10 minutes, positions is trusted
over a lower ledger. A fresh process counts its start time as the last
activity, so it stays cautious for its first 10 minutes. Tested: a stalled
ledger on a quiet exchange exits in full; a recent sell of ours, a fresh
process, or an exit already resting all keep the min().

**The stall was account-wide, and it broke reprices too.** By 23:44Z the
fills endpoint's newest fill on *any* market was from 21:48Z, two hours stale.
A reprice cancels the old exit, which makes the exchange "not quiet". Then
min(positions, ledger = 0) = 0, so nothing was re-posted: ex1077 sat on 16,908
NO with no exit. **Reprice carry-over** fixes this. Shares cancelled for a
reprice were legitimately offered a moment earlier, so they are re-offered at
the new price, capped by positions re-read 3s after the cancel (in case part of
the old order filled just before the cancel landed). Tested: stalled ledger →
re-offers all 16,908; 400 filled in the gap → re-offers 16,508; healthy ledger
→ unchanged.

**Two more holes found live on 2026-10-03 ~01:20Z, both fixed:**

1. **A ladder repost wiped an exit (ex1071, 14,875 NO).** `held()` comes from
   position reads, and today those were inconsistent. One stale read missed the
   market, and the repost used `cancel_all`, which took the exit with it. The
   repost now reads the live open orders on that exchange (one read) right
   before deciding. Any sell of ours there → cancel the buy rungs by id only.
   Tested with a stale `held()`: the exit survives. Without a sell,
   `cancel_all` is still used.
2. **The quiet rule could oversell after a full fill.** An exit posted long ago
   that fills completely leaves nothing resting, while positions may still read
   high. "Quiet" now also counts the last time an exit was *seen* resting, not
   only when one was placed or cancelled. Tested: an old exit fills fully →
   positions stale-high → nothing is sold.

3. **The over-offer re-read used the wrong yardstick (ex1033, ~02:10Z).**
   After re-reading the resting sells, it compared them against `held`, which
   is ~0 under a stalled ledger, instead of max(positions, ledger) as the first
   check does. An ordinary partial fill between the reads (53,510 → 52,536)
   therefore cancelled the whole 52,536 NO exit. Fixed and tested. The exit was
   re-placed by hand straight away (sell NO 52,536 @ 0.715, the bot's own rule
   after re-checking position and resting sells; 3,557 filled at once), so the
   position was uncovered for only a few minutes.

4. **Exits priced into our own bid were killed by self-trade prevention
   (ex1074, ~02:25Z).** A longshot spike ate the rest of the NO book, which left
   one of our mirror rungs as the best NO bid at 0.91. With a one-tick spread
   the exit price (`ask − tick`) equalled that bid, and the engine cancelled
   the 30,339 NO exit (`terminalReasonCode: SelfTradePrevented`, traded 0).
   The exit pass now reads our own highest resting bid on that side and never
   prices an exit at or below it (it rests one tick above), and an STP cancel is
   logged loudly. The main ladder is exposed to the same thing after a dump.
   The ex1074 exit was re-placed by hand with the same rule
   (sell NO 55,707 @ 0.88).

5. **A carried exit hid the lots bought before it (ex1071, ~03:27Z).** The
   lot rule counted lots opened after the *newest* exit. But a carry-over
   re-offers only the old order's remaining shares, so 9,277 NO bought during
   the platform outage, before the carried exit's `createdAt`, were never
   counted. The process now tracks `_covered_until`, the last time an exit was
   sized from a real position read. Fresh exits and top-ups advance it;
   carry-overs leave it alone (or seed it with the oldest stale exit's time).
   Tested: carry → next pass tops up the 9,277 → offered = held.
   **Limit:** a fresh process cannot know which earlier exits were carries, so
   it falls back to the newest exit. It does NOT use the oldest exit, because
   over-counting lots would leave only the positions − resting cap as a guard,
   and that cap is weak exactly when positions reads stale-high. After a
   restart, compare held with offered once; ex1071 needed one manual top-up of
   9,277 @ 0.77.

6. **One failing read aborted every market's exits (~03:40Z).** The exit pass
   looks up each unknown fill's order (`GET /orders/{id}`) to get its
   direction, and that endpoint returned 503 for stretches. One failed lookup
   raised out of `unwind()`, so the whole pass failed for every market, five
   times in a row, while the ladder loop kept working. Now a failed lookup
   marks that market's ledger unavailable (0, handled like a stall), and each
   market runs in its own try/except (`_unwind_one`). Tested: market A's book
   returns 503 → skipped; market B's lookup returns 503 → B still gets its exit.

Verified the account was never actually short: on ex1071 the YES bid level
showed 15,260 (our 14,860 + ~400 others), not the ~30K that a hidden duplicate
exit would show.

**Top-ups under a stalled ledger: covered from position lots (2026-10-03).**
A new fill on an exchange that already had an exit was invisible to
min(positions, ledger = 0): ex1051 had 9,147 NO and ex875 had 320 NO
unoffered. Each buy creates a lot with its own `openedAt`. An exit is sized
from the position as it stood when it was posted, so lots opened after the
newest resting exit's `createdAt` are uncovered. The top-up is the sum of those
lots, **capped at positions − resting, and that cap is the real guard**. Lot
consumption is not reliably oldest-first: on ex875 a sold top-up left the older
01:49:52 lot intact, so the lots only show *that* new buys happened, never how
much is covered. Live 2026-10-03 ~02:02Z: held 397 = offered 397. Tested: a new lot is topped up; a stale-high position with no new lot
sells nothing; a stale read that misses the lot sells nothing.

Either one reading stale-high oversells. Only a reading both agree on is trusted.

**Read order matters.** Resting sells are read *before* holdings. If an exit
fills in between, `resting` is stale-high, so the error under-covers (safe). The
reverse order over-covers by exactly that fill. After a reprice cancel, both are
re-read in the same order, because fills can land up to the instant of cancel.

If both sources say we have offered more than we hold, the resting sells are
**re-read first**. By design, the read order errs in exactly this direction
when an exit fills between the two reads. Exits are cancelled (and re-posted
next pass) only if the over-offer survives the re-read.

Without the re-read this fired falsely live: ex870, 2026-10-02 18:55Z, 579
shares sold at 0.87 between the reads. The bot cancelled a working exit and
left 4,025 NO uncovered until the next pass. If only one says so, it is
treated as lag and left alone. Top-ups smaller than 10 shares wait for the next
reprice, to save writes.

This replaced a "never top up" rule. That rule avoided the short, but left any
ladder fill landing on an already-covered exchange with no exit until the next
reprice.

**Each holding is exited on its own side** (`pos_side()`). A YES sell against
a NO holding is canonicalized into a NO *buy*, which would grow the position
on every pass. The side comes from the **sign of `quantity`**: the API reports a
NO holding as `option: "YES"` with a negative quantity, NO lots and an
`avgCost` in NO terms (found on the first mirror fill; see
`docs/mirror-and-arb-recheck.md` §4). NO holdings are exited by a NO sell one
tick inside the NO ask (= 1 − YES bid), floored at cost.

### API facts this depends on

- **Fills ledger:** `/tournaments/{slug}/portfolio/fills?exchangeId=` returns
  newest first. Fills carry `orderId` but no direction, so direction comes from
  the order (cached per process).
- **Canonicalized sells:** a sell the engine canonicalized into a NO buy shows
  up as fills with `side: "no"` and a **negative quantity** (ex1059:
  `fill no -1446 @ 0.05000000000000004`). Use magnitudes, and take the sign from
  action × side.
- **`Order.quantity` changes meaning:**
  - while the order is open, it is the **remaining** quantity;
  - once the order is closed (filled or cancelled), it is the **original**
    quantity.
  For exact lifetime totals use `/orders/{id}/fills` → `totalQuantityFilled`.
- There is no reduce-only or close-only order flag (`OrderInput` has only
  exchangeId, side, action, quantity, price, expirationDate). So oversell
  protection has to live in the client.

## The cancel-all bug (fixed 2026-10-02)

Ladder reposts used `cancel_all(exchange_id=…)`, which also cancelled **our
exit** on that exchange. Live:
- ex966: 11,201 shares had sold at 0.89, then the repost wiped the exit for the
  remaining 3,753.
- ex852: the same thing happened.

Now `apply_plan(held=…)` cancels only the ladder's own buy orders, by id, on
any exchange where we hold a position. That costs ~9 writes instead of 2, so
`cancel_all` is still used everywhere else.

## First large sweep captured with this machinery

2026-10-02 15:26:00Z, "Will the Democratic Party win the Massachusetts
Governor?" (ex1059):
- **The fill:** one atomic order filled five of our rungs, 110,324 shares at an
  average of 0.842 (cost ~92.9K, leaving 17K cash).
- **Recovery:** the book recovered to 0.905 / 0.91 within minutes.
- **Exits:** they rested at 0.91 and then 0.905. They had sold 26K by 15:32 and
  ~53K by 15:45 (cash back to 55.8K).

Edge at the recovered price is about 0.065 × 110K ≈ **+7K**.


## Stop-loss and manual overrides (added 2026-10-04)

The cost floor means we never sell below cost, which is right for dislocations
(RI/NH/CT/PA Governor and Senate R legs, 2026-10-03: underwater for 30 minutes
to a few hours, legs summing to 1.08–1.14, all reverted into profit). It is
wrong for a genuine repricing: TX-28 D drifted 0.87 → 0.80 over 10h+ with legs
summing to ~0.99, and a cost floor would hold it to settlement for −465 in
expectation, with 11K of cash idle and a −11K tail.

**Rule (`stop_check`):** a position is stopped out when it has been
underwater (best bid of its side below average cost)
- for ≥ `--stop-hours` (3) **and** its race's leg mids sum to within
  `--stop-coherence` (0.03) of 1, i.e. the market agrees with itself; or
- for ≥ `--stop-max-hours` (8), regardless.

**On a stop:**
- the cost floor is dropped, so the exit rests one tick inside the ask and
  follows the market (it does not dump into thin bids);
- the race is added to `no_ladder`, so we stop buying into a repricing market;
- the clock and the stop flag persist in `data/stop_state.json` (survive
  restarts), and are cleared when the position closes or recovers above cost;
- the log prints `!! STOP-LOSS`.

**"Underwater" means below the exit floor (cost + `min_exit_edge`), not below raw
cost.** First version compared with raw cost; on ex941 (wide book 0.105/0.125)
a one-tick flicker put the NO bid exactly at cost (0.895) while the exit floor
was 0.90, which reset the clock though no exit could fill. Fixed 2026-10-04.

**Stop-loss v2 (user-approved, live 2026-10-11 13:50Z).** Why: from the 10-10
peak (193.4K at cost) to 10-11 13:40Z (190.7K), stops cost ~3.6K. Three large NO
positions accounted for most of it: RI Gov Independent −1,745, Illinois Senate D
−788 and CO-04 D −788. Three mechanisms made them worse:
- **Ladders kept buying into underwater positions.** RI went from 29,297 to
  36,609 NO and MT-01 from 3,926 to 9,515.
- **The stopped exit sold the whole remainder into the bid at once** when the
  spread was one tick: RI 19,254 @~0.89, CO-04 14,851 @0.65.
- **With a wide spread the stopped exit sat at the ask for days.** Georgia D was
  excluded for 44h over 2,454 shares.
- Also: stops never cancelled. Florida Gov R stayed excluded for 32h while back
  above cost.

The changes (`run_quoter.py`, constants in `STOP`):
1. **Add-cap** (`addcap_ticks` = 2). The exit thread records any position whose
   bid is ≥ 2 ticks under cost in `_underwater`. Before each ladder post,
   `apply_addcap()` drops that exchange+side's buy rungs (main, mirror or cheap)
   and cancels the resting ones once. It lifts automatically when the bid recovers.
   - Measured on real fills: the add-cap would have avoided buying 7,522 NO on
     RI (≈ +158) and 5,589 NO on MT-01 (≈ +63).
   - This reverses the 2026-10-06 decision not to cap adds.
2. **Slice exit once stopped** (`slice_cap` = 10,000). Each exit pass cancels the
   resting stop exit, sells min(size at the best bid, 10,000) into the bid, and
   rests the rest one tick above the bid. The existing cancel/carry/held logic
   sizes it, so it can never over-offer.
3. **Stop cancels on recovery.** If a stopped position's bid gets back to the exit
   floor (cost + 0.005), the stop state is cleared, the normal never-below-cost
   exit returns, and the race is re-admitted.
4. **Tiny stops keep the race laddered.** If (cost − bid) × qty < `tiny_loss` (50),
   the stop still sells, but the race is not added to `no_ladder`. Example: the
   1-share Oklahoma stop had pulled that race for hours.
4b. **A tiny stop clears if the position grows** (fix 2026-10-11 ~23:20Z). A
   tiny stop keeps the race laddered, so new fills can arrive before the stopped
   position closes. ex947 sliced 30,880 out, then refilled 3,296 @0.060 while
   still "stopped", i.e. with no cost floor. `stop_check` now tracks `min_qty`
   since the stop; if qty rises above it, the stop is cleared and the normal
   cost floor applies. Excluded (non-tiny) stops are unaffected. 24 offline
   checks pass.
5. **`permanent_exclude`** in overrides.json is never re-admitted. RI Gov
   Independent (880) is on it: its stops cost ~−2.5K in total.

Tested offline (scratchpad `test_stopfix.py`, 22 checks, all pass). The cases:
- tiny vs big stop; recovery and re-admit; permanent exclusion;
- RI book replay (6,159 @0.91 slice + 30,450 rest, then a 10,000 slice @0.905);
- Georgia closes in one slice; a non-stopped exit is unchanged;
- the add-cap triggers at ≥2 ticks only, filters only that side, and releases on recovery.

First live pass: MT-01 add-capped (5 rungs pulled); Florida sliced 570 @0.225
(above cost); Massachusetts sliced 10,000 @0.045.

**Manual overrides: `data/overrides.json`, re-read every pass (no restart).**

```json
{"exit_floor": {"936": 0.80}, "no_ladder": ["936"]}
```

- `exit_floor` replaces the cost floor for that exchange's exit (an explicit
  value always wins over the stop logic).
- `no_ladder` removes the whole race containing that exchange from both
  ladders, and pulls its resting rungs by id (exits stay). Remove an entry to
  re-enable the race.
- **Auto re-admit (user decision 2026-10-06).** When a stopped-out position has
  fully closed, `readmit_races()` takes its exchange off `no_ladder`, drops any
  `exit_floor` for it, and records it in `recent_cuts` with a timestamp. The race
  then quotes around its NEW fair like any other. Before this, stops only ever
  added races, and 9 had piled up (865, 880, 899, 936, 941, 981, 995, 1052,
  1070). All 9 were re-admitted by hand on 2026-10-06 21:07Z, after confirming none
  still held a position. The backup is `data/overrides.bak-20261006.json`.
- `recent_cuts` is ignored by the quoter. `watch_losses.py` treats entries
  newer than 6h as intended cuts, so a stop's last fills after the re-admit
  don't raise LOSS SELL.
- Per-race add-cap for underwater races: considered and **declined** by the
  user (2026-10-06). The ladder keeps quoting a race whose position is
  underwater until the stop fires.
- **Watcher false alarm, fixed 2026-10-06.** When a position opened and closed
  between two watcher reads (ex1075: bought @0.855, sold @0.865, +130), its
  sells were judged against a remembered cost from an EARLIER position (0.875).
  Root cause (confirmed on ex898, 2026-10-07 01:22Z: 381 bought @0.195, sold
  @0.200 = +2, but the platform reported avgCost 0.202): **the platform's
  `avgCost` is sticky across a position closing and reopening.** It blends in
  the old closed position. The watcher now keeps its own running average per
  (exchange, side) from the trade stream. It resets when the position reaches
  zero and is seeded from `avgCost` only for positions it hasn't built itself.
  Replayed offline: the ex898 sequence no longer alerts, and a genuine sell
  below cost still does. The quoter's exit floor was not affected: it logged
  avg 0.195 for that lot.
  Follow-up (ex1042, 2026-10-07 03:46Z): the watcher's share count drifted. A
  missed sell left ~1,400 phantom shares @0.09 blended into a fresh 0.080 lot,
  so a 0.085 exit looked like a loss. Each cycle now caps the held count at
  the exchange's position and zeroes positions that are no longer listed.
  Replayed: no false alarm, and a real loss is still caught.
  Second follow-up (ex1069, 2026-10-09 ~00:20Z): a fast fill of 23,859 NO came
  in many lots, and one 100-row transactions page per 3-min cycle missed the
  cheaper ones. The running average read 0.895 against the true 0.880, so a
  +245 exit at 0.890 was flagged as a loss. The watcher now pages back until it
  reaches a trade it has already seen (capped at 2,000 rows).

Tested offline: fresh dip → clock only; 4h + coherent → stop; 4h + incoherent
(sum 1.10) → hold; 9h incoherent → cap stop; manual floor → exit repriced
0.84 → 0.80; recovery → clock reset; closed → state cleared. Live: TX-28
(2026-10-04 ~01:45Z) had its ladder pulled on both legs and its exit repriced
to 0.80 (250 sold at once, 13,030 resting).

## Volatility-scaled stop: backtest (2026-10-11)

**Question (user):** with volume low, is a timed stop the wrong tool? Should the
stop fire on distance from entry, scaled by the market's volatility?

**Method (`backtest_volstop.py`, data cached in `data/volstop_cache.json`):**
- Data: 6,370 of our fills since 2026-10-04, giving 304 holding episodes, and
  5-minute candles for each of the 95 markets.
- sigma = std of 15-min price changes over the trailing 24h, floored at one tick.
- Rule V(k, m): sell everything the first time the price stays ≤ cost −
  max(k·σ, 2 ticks) for m minutes.
- The counterfactual sells at the trigger price − 1 tick and drops later buys.
  It is compared with what actually happened, including dips that later
  recovered.
- Caveats: candle closes are trade prices, not our bid; there is no coherence
  check; the backtest is applied on top of what really happened.

| k | m (min) | fired | helped | hurt | net vs actual |
|---|---|---|---|---|---|
| 2 | 15 | 18 | 2 | 16 | **−1,842** |
| 2 | 30 | 14 | 2 | 12 | −1,473 |
| 2 | 60 | 5 | 1 | 4 | +440 |
| 3 | 15 | 6 | 1 | 5 | +407 |
| 3 | 30 | 3 | 1 | 2 | +728 |
| 3 | 60 | 2 | 1 | 1 | +729 |
| 4 | 15 | 2 | 1 | 1 | +330 |
| 4 | 30 | 2 | 1 | 1 | +380 |
| 4 | 60 | 1 | 1 | 0 | +421 |

**Reading:**
- **Tight settings lose money.** At 2σ, most dips that look like a real move
  still recover: 16 of 18 triggers hurt. The timed + coherence rule is doing its
  job of not cutting reverting dips, even at today's lower volume.
- **Every positive result rests on ONE episode: RI Gov Independent (880).** A 3σ/60m
  rule would have fired at 10-10 23:20Z, about 3h before the 8h cap, saving
  about +770 of its −1,746. The other triggers were neutral or negative (TX-15
  D, −41).
- 880 is now on `permanent_exclude`, so even this gain would not repeat there.

**Decision (pending the user):** no switch to a vol stop. At most, add a
conservative k=4, m=60 trigger as an extra backstop for RI-sized trending moves.
In this sample it fired once and helped once, but n=1.

## Outage safe-mode (live 2026-10-11 19:29Z)

After 2 consecutive failed passes, `outage_pull()` cancels our resting BUY
rungs and keeps every exit:
- exchanges we hold nothing on: one `cancel-all` each;
- held exchanges: buys cancelled one by one.

It runs at low write priority, once per outage. Ladders re-post on the first
successful pass. Why: on 2026-10-11 a 503 outage (15:03–15:18Z) froze the
ladders.
- RI Senate D YES rose 0.85 → 0.865 on 132K volume. Stale cheap rungs bought
  63K NO; the stop cost −487.
- NY Gov filled 36K at 0.895 on the first pass back; −1,053 after its stops.
- Only the balance read failed; order writes worked. Tested offline: held
  exchanges keep exits, others are cancel-all'ed, and empty plans are skipped.
- `monitor_alerts.py` alerts on `OUTAGE SAFE-MODE`.
- **Fix 2026-10-11 20:37Z.** In the 19:40–20:33Z outage the cancel endpoint
  itself returned INTERNAL_ERROR for every exchange. The first version still
  marked the pull done (and logged "rungs pulled"), so it never retried.
  - It now counts as done only if a cancel succeeded, retries on the next failed
    pass, and says plainly when cancels fail.
  - That outage showed the matching engine kept running: an exit filled at
    16:04 local. Stale rungs CAN fill while the API is down, which is why the
    retry matters.
  - **Fix 2026-10-12 19:4xZ.** A local network/DNS failure (`URLError`) inside
    `outage_pull` was not an ApiError, escaped the main loop's except block, and
    ended the process at 19:36:40Z. The supervisor relaunched it 30s later.
    The pull now catches every exception per exchange, and the call itself is
    wrapped, so the error handler can never kill the run. Tested with a client
    that raises URLError on every call.
  - Do not restart during an outage. A fresh process has no `last_plans` until
    one pass succeeds, so it cannot pull.

## Per-race size backtest (2026-10-11, `backtest_size.py`)

Model: the largest position ever held per (exchange, side) = full-ladder
capacity. Scaling the ladder by f caps each episode at f × that, and P&L scales
with it; episodes below the cap are unchanged. Data: 311 closed episodes since
10-04, realised +27,244 (gains +37,808, losses −10,563).

| episodes starting | actual | f 0.8 | f 0.6 | f 0.5 |
|---|---|---|---|---|
| since 10-04 | +27,244 | −3,467 | −6,978 | −9,163 |
| since 10-08 | +1,940 | −12 | +128 | +52 |
| since 10-10 | −3,545 | +602 | +1,386 | +1,784 |

**Reading:** size cuts both ways. The biggest winners also ate the whole ladder,
e.g. ex1076 YES on 10-04, +3,469 at 68,790 shares. Over the full period a
smaller ladder costs more than it saves. It only helps in a losing stretch like
10-10/11, where f 0.5 would have halved the loss. Decision for the user:
**keep race-pct 0.85**, unless we prefer lower variance over expected P&L.

## Wind-down mode (2026-10-16)
The user asked for the bot to stop once every position is cleared, because they may be offline.
- **Why the plain supervisor was stopped first:** stopping the quoter alone would leave about 1,800 buy rungs resting with nobody to exit their fills. During the 10-14 and 10-15 Mac network outages, exactly that happened; ex1027/NO was filled during the outage and cost about −2.5K.
- **The hung quoter:** the quoter's main loop also hung after the 10-15 outage. It sat in outage recovery for about 15h after the network came back, and ignored SIGTERM, so it had to be SIGKILLed.
- **`winddown.py`** replaces the supervisor. Each 2-minute cycle:
  1. cancels every resting BUY (cancel-all per exchange where nothing is held, order by order where we still hold shares);
  2. runs the normal `run_quoter.unwind` exit pass, with `min_topup=1` so odd lots get exits. Stop-loss v2 still applies, and stop clocks carry over via `data/stop_state.json`;
  3. once no positions are left, runs a global cancel-all and exits.
- **It places no buys.** Log: `data/winddown.log`.
- **To resume normal trading:** `nohup caffeinate -ims ./supervise.sh >> data/supervisor.log 2>&1 &` (supervise.sh removes `data/STOP` itself).
- **Result (2026-10-17 08:57 EDT):** the wind-down took about 28h to get down to one 138-share position (ex861). It placed no buys. Account at cost went from 194.2K to **189.9K (−4.3K)**. Almost all of that was stop-losses on positions filled while the quoter was hung: ex1068 about −3.3K, ex1057 −488, ex1058 −474, ex1052 −405, ex966 −340, and many small ones.
- **The user then asked to keep trading,** without stopping when flat. `winddown.py` was stopped and `supervise.sh` relaunched (quoter45.log). Normal flags apply, and ex861 is handled by the normal exits.
- **New watchdog in `supervise.sh`:** if `data/quoter.jsonl` has not been written for 90 min (and the quoter has run for over 90 min), the supervisor SIGKILLs the quoter and relaunches it. Every pass, including a failed one, appends to that file. This is the fix for the 15h hang.
