# Loss analysis, 2026-10-13 to 10-20

**Peak to now: 198.15K → 187.9K (−10.3K).** The peak is the cash at 10-13 21:41Z, when we were nearly flat (basis 13), so it is a real figure. "Now" is 10-20 16:10Z: cash 176.9K plus basis 11.0K, or 187.5K at market.

## Method: what is reliable
- **Reliable:** the account value at cost from `data/quoter.jsonl` (cash + costBasis), cross-checked against cash at near-flat moments: 10-13 198.1K, 10-17 189.9K, 10-19 188.2K, 10-20 187.7K.
- **Not reliable per trade:** transaction records contain aggregated fills at off-tick prices (e.g. ex1055 "BUY −199,246 @ 0.5167"). 41 such records appeared on 26 exchanges since 10-12. They make per-position realised P&L from fills wrong.
  - Example: ex1055 (Wyoming Senate R) on 10-13 was actually a **+8K** day by cash. The fill book showed a loss, and the watcher alert "LOSS SELL ex1055 −21.8K" was wrong.
  - Per-ladder totals below are therefore indicative only.

## Account value by window (from quoter.jsonl)
| Window (Z) | Bot state | Change |
|---|---|---|
| 10-13 21:41 → 10-14 10:05 | healthy | −1.2K |
| 10-14 10:05 → 10-15 03:27 | network outage 1 (17h) | +0.7K |
| 10-15 03:27 → 10-15 13:22 | flaky network | +2.7K (at cost; positions ~100K) |
| 10-15 13:22 → 10-17 12:57 | **quoter hung ~16h, then wind-down** | **−10.5K** |
| 10-17 12:57 → 10-18 12:56 | healthy | −2.0K (ex964 Montana Senate R NO −1.6K) |
| 10-18 12:56 → 10-19 00:19 | network outage 3 | +0.1K |
| 10-19 00:19 → 10-19 18:00 | healthy | +4.6K (ex896 mirror +5K) |
| 10-19 18:00 → 10-20 16:10 | healthy | **−4.8K** (stops: Mississippi Senate 961/962 −1.9K, ex873 −1.4K, ex850 −0.9K, ex1057 −0.8K, ex1014 −0.4K) |

## Causes
1. **Outage plus hang, 10-15 to 10-17: −10.5K, the single largest cause.**
   - Ladders kept resting and filling while the bot couldn't manage them. At its peak we held about 100K at cost.
   - The positions were then cut by stop-loss: ex1068 (Massachusetts Senate D NO) about −3.3K, ex1027 (MN-05 D NO) −2.7K, ex1057, ex1058, ex1052 and others.
   - Fixed since: the supervisor watchdog. Not fixed: the Mac's network.
2. **The cheap side (NO bids on favourites, NO 0.05–0.25) is the losing ladder.** Most big stop losses since 10-17 were cheap-side NO positions of 50K–180K shares.
   - At those sizes, 1–2 ticks under cost costs 0.5–1.8K.
   - Indicative fill-book totals since 10-12: cheap −10.2K, main −1.0K, mirror +7.1K.
   - Clean-episode subset: cheap at NO <0.10 is +0.4K; at 0.10–0.20 −1.2K; at ≥0.20 −0.3K. This is consistent with the earlier finding that the cheap side only earns below 0.10.
3. **Holding time** (fill book): positions closed within 3h were all winners (+8.2K, 84 of 84). Those held 3–12h lost a net −9.0K. The same pattern as before 10-12.

## Was the stop the problem?
Hold-instead check: the current price versus our stop sale price (10-20 ~16:30Z).
- **Stop hurt:** ex1068 +6.0K and ex1027 +2.1K would have recovered (both from the outage).
- **Stop helped:** ex964 −3.6K and ex850 −1.3K kept falling.
- **Smaller effects:** ex873 −0.2K, ex1057 +0.4K, ex962 +0.7K, ex961 +0.2K, ex1014 −0.1K.
- **Net +4.3K if held,** driven by two names. This is not evidence for changing the stop rule.

## Options (not yet decided by the user)
1. Run on an always-on server; the outage-related losses were the biggest bucket.
2. Restrict the cheap side to NO < 0.10, or switch it off.
3. Cap shares per cheap rung, so one tick is not worth 500+.

## Decisions (2026-10-21)
- **Cheap side limited to NO < 0.10** (user decision). New flag `--cheap-max-no`, default 0.10 (`CHEAP_MAX_NO` in `sigcup/quoter.py`).
  - Rung weights are normalised before the cut, so a dropped rung's budget is not moved onto the cheaper rungs.
  - Favourites at about 0.95 are unaffected (all their cheap rungs are already below 0.10).
  - Deployed in quoter57; the first pass planned 0 cheap rungs at ≥ 0.10.
- **The day before the cap (10-20 16:00Z → 10-21 07:00Z) lost another −8.4K at cost.** Stops on cheap-side NO positions: ex848 (Arizona Governor D) about −2.9K, ex860 −1.3K, ex1027 −0.9K, ex850 −0.55K.
  - ex860 was bought at NO 0.086, below the new cap, so the cap would not have prevented that one.
- **Bug found: orphan rungs.** `apply_plan` skips plans with no quotes. So a race that stopped qualifying (favourite below `--min-fair`, incoherent legs) kept its old buy rungs resting indefinitely.
  - Found as 18 cheap NO bids at 0.245–0.285 on ex1008/1010/885/897; I cancelled them by hand.
  - Fix: `run_quoter.pull_orphans()` runs after `apply_plan` each pass. It cancels buys on in-scope exchanges that no plan quotes: by id where an exit sell rests, otherwise with cancel-all.
  - Unit-tested. Deployed in quoter58; pass 2 pulled 63 orphan rungs on 8 exchanges (1007/1008/1009/1010/884/885/896/897).
- quoter58 pass 1 failed once with `AttributeError: 'NoneType' object has no attribute 'get'`, before planning, so not in the new code. It did not reproduce in a dry run, and pass 2 was clean. Watch for recurrence.
- **Cheap side switched off (2026-10-22 02:33Z, user decision): `--cheap 0` in supervise.sh** (quoter59).
  - In the first 10h with the NO < 0.10 cap, it still lost −1.6K: ex998 SD Gov R NO 43K @ 0.060 → 0.037 (−996), and ex860 Idaho Gov R NO 70K @ 0.040 → 0.031 (−625).
  - The mechanism: a favourite dips for days, fair follows it down, then the favourite snaps back to about 0.965. The snap-back fills our NO bids (k ticks above the depressed fair), the favourite stays up, and the stop cuts the position. The ladder ends up selling recoveries.
  - After pass 2, 0 cheap NO bids were resting. Main and mirror ladders are unchanged.

## Pre-election stop of buying (2026-11-01 19:27Z, user decision)
- **What happened:** the account peaked at 189.1K at 14:29Z on 11-01, then lost about 3.8K in ~5h to stops on safe-R favourites that sold off together and stayed down: Alabama Gov (844/845) about −2.0K, Arkansas Senate D NO (943) −0.8K, Kentucky Senate R (956) −0.26K. Two days before the election, such moves are likely to be real information, so the dip-buying edge no longer applies.
- **What changed:**
  - The supervisor was stopped (`data/STOP`) and `winddown.py` started (log `data/winddown.log`).
  - Its first cycle cancelled all 1,407 resting buys; 0 buys remain.
  - Exits and stop-loss v2 continue on the 6 open positions (865, 990, 994, 995 NO, 998, 1048 NO; about 33K at cost), all fully offered.
- **When it ends:** winddown.py exits by itself once flat. Restarting the ladders needs `nohup caffeinate -ims ./supervise.sh >> data/supervisor.log 2>&1 &`.
- **Account at the switch:** about 185.3K at market (186.8K on 10-31, 178.2K on 10-22).
- **Wind-down complete, 2026-11-02 00:13Z:** no positions, no open orders, cash **184,815.42**. It cost about −0.5K versus the 185.3K mark at the switch. The last position, ex998 South Dakota Governor R YES, was cut by the 8h stop. Nothing is running against the exchange (the watch_losses reader aside).

## Nov 2 review: should we trade on Nov 3? (written 2026-11-03 ~02:45Z)
- **Our activity:** we did not trade on Nov 2; we have been flat since 11-02 00:13Z. Balance 184,815.42, i.e. P&L +84,815, **rank 26**.
- **Leaderboard:**
  - Top 3 at +963K to +980K P&L. **The paid places are out of reach** (that would need roughly +880K in 1.5 days).
  - Around us: #23 +90.6K, #24 +89.4K, #25 +87.3K, #27 +80.8K, #28 +79.0K.
  - We dropped from 23 to 26 because others gained while we were flat. On the day, #23 (jolly-grove) made +3.1K and #27 (witty-harbor) +6.4K.
- **Market on Nov 2 (EST day)** was very active, with flash crashes and broad repricing:
  - Flash crashes that reverted: Arkansas Senate R 0.90 → 0.14 → 0.855; Texas Senate D 0.725 → 0.145 → 0.71; Massachusetts Gov R, Delaware Senate R and Oklahoma Senate D all spiked from about 0.03–0.07 to about 0.85 and came back.
  - Of 93 favourites ≥ 0.70 at the start of the day: 45 calm, **20 dipped ≥5c and recovered** (ladder-friendly), **28 dipped and stayed down** by 2–11c (ladder-hostile: our 3h stop would cut them).
  - Volume concentrated in the chamber-control markets and Texas, Kansas and Maine Senate.
- **Assessment:** dip-buying has a roughly 40/60 recovered/stayed-down split on the eve of the election. The rare deep flash crashes are big wins, but the broad drift lower in favourites gives a steady drip of stop losses. On election night (Nov 3 evening EST), moves are results, i.e. permanent. Expected value is around zero or slightly negative, and the variance is several K. Rank 23 needs +5.8K, about one of our best days. The rank has no prize value.
