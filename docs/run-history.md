# Run history: SIG Predictions Cup, Midterm Elections (2026-10-01 → 11-04)

This is the record of the whole run. The evidence behind each step lives in the linked docs.
`STRATEGY.md` describes the strategy and its rules.

## Result
- **Ladders' final balance: 184,815.42 SUSQies**, from 100,000 at the start. That is **+84,815 (+84.8%)**, in cash at 11-02 00:13Z.
- **Final trade (11-03, held to settlement):** NO on 21 overpriced longshot legs, costing 150,093 for 163,240 shares.
  - If all resolve NO: about **197,960 (+97.96K)**.
  - Expected at Polymarket's prices: about +9.8K on the ladder balance.
  - One upset costs that leg's cost, at most 12K.
  - See "11-03: selling longshots" below.
- **Leaderboard on 2026-11-03 12:47Z: rank 28** at +84,815, before the longshot trade.
  - The paid top 3 were at +994K to +1,000K.
  - Neighbours: #23 +98.1K, #24 +95.3K, #25 +91.2K, #26 +86.6K, #27 +85.3K.
  - Final standings are locked only after every market settles.
- **Highest real value: 198,136** on 2026-10-13 21:51Z. That was cash while nearly flat (cost basis 13), so it is not a mark.

## Account value by day (end of day, UTC; cash + cost basis from data/quoter.jsonl)

Cost basis uses the platform's avgCost, which is sticky. When the basis is large the number is approximate. Rows with basis about 0 are exact.

| Day | Value | | Day | Value |
|---|---|---|---|---|
| 10-01 | 100,000 (start, live 22:08Z) | | 10-17 | 189,667 |
| 10-02 | 130,572 | | 10-18 | 187,945 |
| 10-03 | 157,019 | | 10-19 | 189,155 |
| 10-04 | 178,724 | | 10-20 | 186,637 |
| 10-05 | 183,855 | | 10-21 | 178,215 |
| 10-06 | 185,249 | | 10-22 | 180,302 (flat) |
| 10-07 | 187,903 | | 10-23 | 180,486 (flat) |
| 10-08 | 190,155 | | 10-25 | 181,030 (flat) |
| 10-09 | 192,739 | | 10-27 | 181,238 (flat) |
| 10-10 | 193,304 | | 10-28 | 184,806 (flat) |
| 10-11 | 189,542 | | 10-29 | 185,683 (flat) |
| 10-12 | 189,553 | | 10-30 | 186,680 (flat) |
| 10-13 | 198,331 | | 10-31 | 187,980 |
| 10-14 | 196,978 | | 11-01 | 185,058 → **184,815 flat at 11-02 00:13Z** |
| 10-15 | ~200,436 (at cost, 66K held; real value lower) | | 11-02, 11-03 | 184,815 (flat; 11-03 12:58Z longshot NO trade, 150.1K cost) |

**Shape of the run:**
- **10-01 to 10-04:** +79K. Large sweeps reverted fast, and early on few others were resting liquidity.
- **10-05 to 10-13:** slow climb to the 198K high.
- **10-14 to 10-21:** −20K. Network outages and a hung bot, then losses on the cheap side.
- **10-22 to 10-31:** +8.6K with the cheap side off.
- **11-01:** −3.8K pre-election repricing, then we stopped buying.

## Timeline

### 10-01: research, build, go live
- **API client, market catalog, book collector, trading probes.** Open questions answered: `docs/open-questions-answered.md`.
- **Arbs rejected.** Dutch book across legs, and buying YES and NO in one market (`docs/dutch-book-assessment.md`).
- **Market making as the main engine rejected.** The spread pool was too small.
- **Sweeps found to be the real opportunity.** Large market orders walk thin books and revert within ~75s (`docs/sweep-opportunity.md`).
- **Ladder quoter built** (resting YES bids below fair on race favourites), hardened for unattended running, and live at 22:08Z.
- **Exit rule settled:** sell one tick inside the ask, floored at cost + 0.005. Exits run on their own thread (`docs/exit-management.md`).

### 10-02 to 10-04: mirror ladder, more markets, first big gains
- **Mirror ladder live ~17:05Z** (`--mirror 0.5`): NO bids on longshot legs to catch aggressive longshot buying. Its first fills were exited within ~3h for +18.7K (`docs/mirror-and-arb-recheck.md` §1, §4).
- **`--min-fair` 0.90 → 0.75.** The 0.60–0.90 band had about 2.2x the flow (§3).
- **Leader analysis.** Rank 1 made +753K in a day at ~3.8% on 19.6M volume, which inspired the cheap side (`docs/leader-analysis.md`).
- **Fixed:** platform outages (500-storms), the exit pass flipping us short, and the cancel-all bug wiping exits.
- **Stop-loss and `data/overrides.json` added on 10-04** for genuine repricings.

### 10-06 to 10-10: cheap side, cap discovery, fine-tuning
- **Cheap side live** (`--cheap 0.1`): NO bids on favourites at 0.005–0.05 (YES offers above the ask).
- **Undocumented engine cap found:** at ~129x cash in resting notional, the engine silently refuses buys. Moved to `--race-pct 0.85 --budget-pct 135`, and refusals are now counted.
- **`ladder_matches` fixed** to pair rungs by closest price. Reposting had been costing ~200 writes and 12-minute passes.
- **Lead-lag between legs:** no exploitable lag (`record_tape_ws.py`, `analyze_leadlag.py`; mirror doc §7).
- **`--min-fair` 0.75 → 0.70 on 10-08** after a backtest (mirror doc §8).
- **Peak at cost: ~193K on 10-10.**

### 10-11 to 10-12: stop-loss v2, safe-mode, rejected backtests
- **Losing stops triggered a rework** (Rhode Island Gov −1,745, Illinois, CO-04, New York Gov ~−1,470). Stop-loss v2:
  - fires after 3h underwater if the race's legs are coherent, or after 8h regardless;
  - exits in slices into the bid (max 10K per pass);
  - "tiny" stops (< 50 at the bid) keep the race laddered;
  - add-cap: no new buys on a position ≥ 2 ticks under cost;
  - stopped races are re-admitted automatically once the position closes.
- **Outage safe-mode:** after 2 failed passes, pull buy rungs and keep exits. A URLError in it once killed the process; now contained.
- **Backtests rejected:** volatility-scaled stop, time-based exit (−2K to −4K), smaller `--race-pct` (−9K at 0.5).
- **Loss watcher fixed** (`watch_losses.py`). False LOSS SELL alerts came from sticky avgCost and odd-priced aggregated fills; it now keeps its own running-average book.
- **External-price market-making plan drafted, not built** (Kalshi/Polymarket fair value, pilot on 10–15 markets). Its Phase 0 gate: does the platform converge toward external prices? The user put it on hold, and it was never started.

### 10-13 to 10-21: outages, a hung bot, losses on the cheap side (`docs/loss-analysis-1013-1020.md`)
- **10-13:** real high of **198.1K**, including an ~8K reversion on Wyoming Senate R (ex1055).
- **Mac network outages, all DNS failures:**
  - 10-12/13 overnight, 16h;
  - 10-14/15, 17h;
  - 10-15 → 10-16, the quoter **hung ~16h** in outage recovery and ignored SIGTERM;
  - 10-18, 11h.

  While unmanaged, resting rungs kept filling (about 100K of positions at peak), and the later stops cost about **−10.5K**. Worst: ex1068 MA Senate D NO about −3.3K, ex1027 MN-05 D NO −2.7K.
- **10-16 → 10-17:** `winddown.py` (no buys, exits only, stop when flat) cleared the book. The user then resumed trading.
- **10-17:** `supervise.sh` gained a **watchdog**: kill and relaunch if no pass is logged for 90 min. It worked on 10-18.
- **Cheap side diagnosed as the losing ladder:**
  - 50–180K-share NO positions at 0.10–0.25, where one or two ticks cost 0.5–1.8K;
  - realised by ladder since 10-12 (indicative): cheap −10K, main −1K, mirror +7K;
  - only positions closed within 3h were winners (84/84, +8.2K).
- **10-21:** cheap side capped at NO < 0.10 (`--cheap-max-no`). The day before the cap lost a further −8.4K.
- **Bug fixed, `pull_orphans`:** races that stopped qualifying kept their old buy rungs resting indefinitely. 63 orphan rungs were pulled on the first pass.

### 10-22 to 10-31: main + mirror only
- **10-22 02:33Z: cheap side switched off** (`--cheap 0`). Even below 0.10 it lost (South Dakota Gov R −996, Idaho Gov R −625): it sold favourites' snap-back recoveries.
- **10-22 → 10-31: 178.2K → 186.8K (+8.6K).** No losing days from 10-23 to 10-31, one process for 10+ days, and only ~12 failed passes out of ~4,500.
- **10-28 and 10-30: textbook sweeps.** On 10-30, about 94K across 7 markets was filled and exited at a profit within hours.

### 11-01 to 11-03: pre-election stop
- **11-01: −3.8K in ~5h** (189.1K → 185.3K) on safe-R favourites that sold off together and stayed down: Alabama Gov about −2.0K, Arkansas Senate −0.8K, Kentucky Senate −0.26K.
- **11-01 19:27Z: buying stopped.** The supervisor was stopped and `winddown.py` cancelled 1,407 buys.
- **11-02 00:13Z: flat at 184,815.42.** The wind-down cost about −0.5K.
- **11-02 review:**
  - of 93 favourites, 20 dipped and recovered, 28 dipped and stayed down;
  - flash crashes reverted (Arkansas Senate R 0.90 → 0.14 → 0.855);
  - the top 3 were out of reach, so we **stayed flat for election day**.

### 11-03: selling longshots against Polymarket (`sell_longshots.py`, log `data/longshots.jsonl`)
- **Why:** rank had slipped from 23 to 28 while we were flat, and neighbours were gaining 5–13K a day.
  - The user asked about selling very unlikely events, and pointed out that Polymarket had Rhode Island at 99% D against about 92% here.
  - This trade is unlike the dip-buying stopped on 11-01: it needs no reversion and no stop, it is held to settlement, and the laptop need not stay online. It is close to the mirror ladder, the one ladder that made money.
- **Cross-check (12:50Z), Polymarket winner-market ask against our YES bid:**
  - Most deep-safe longshots were 3–9c rich here: RI Senate R 0.8% vs 8%, SD Senate D 0.05% vs 7%, ID Senate D 0.05% vs 8.5%, DE Senate R 1.2% vs 9%, MA Senate R 0.9% vs 8%, VA Senate R 0.7% vs 6.5%.
  - It also exposed traps: **Louisiana Senate D was 13% on Polymarket** (bid 10% here), Montana Senate I 8.9%, VA-02 R 9.5%, TN-05 D 12.8% and SC Gov D 4.85%. These were all excluded.
- **Rule:**
  - p = Polymarket best ask.
  - Sell YES (buy NO into resting YES bids) only down to max(p + 2.5c, 3.5c).
  - Risk cap per leg: 12K if p ≤ 1.5%, 8K if ≤ 3%, 5K if ≤ 5.5%.
  - Legs ordered by edge; 150K total.
  - A 100-share test confirmed that a NO buy at 0.95 fills against the 0.07 YES bid at 0.93.
- **Filled 12:58Z: 21 legs, all fully, no orders left.**
  - Biggest legs: RI Gov R, RI Senate R, SD Senate D, MA Senate R (about 12K each); ID Senate D (9.7K); DE Senate R (8.6K).
  - Also Idaho Gov D, Colorado Senate R, Tennessee Senate D and Oklahoma Senate D.
  - Smaller 5K legs where p is 3–5%: NM Gov R, NY Gov R, AR Senate D, KY-04 D, MN-02 R, CA-48 R, NH-01 R.
  - Cash after: 34,722.88.
- **Payoff:** +13,147 if all NO. Expected about +9.8K at Polymarket's prices.
  - The leaderboard marks the positions at about −1.7K (the spread) until they settle.

- **Second round, 11-04 04:10Z (user approved): buy YES on called winners with the spare cash.**
  - At 04:05Z our 21 legs were +2.2K at the marks. Polymarket had about 15 of them at 0.1–0.4%, and none had settled.
  - The platform still offered winners Polymarket treated as decided at 0.95–0.975, so `sell_longshots.py --favourites --max-p 0.003 --min-edge 0.02 --cap 8000 --budget 33000` bought YES:
    - New York Gov D, South Dakota Gov R, Wyoming Senate R, Maine Gov D, Arizona Gov D, AZ-06 D, TX-15 D;
    - 33,898 shares for 32,921, so **+977 if all win**.
  - A second longshot round had only +0.9K available by then; bids had decayed to 2.5–4c.
  - Cash after: 1,801.93.
  - **Bug found and fixed first:** the Polymarket matcher accepted any slug that contained the state, so "Virginia Senate" matched `west-virginia-senate-…` (Kansas/Arkansas could collide the same way). It now requires the exact state prefix and the office. All 21 first-round legs had been matched correctly (checked against the logged slugs).
- **Combined payoff if every leg goes our way: about +14.1K**, i.e. about 198.9K.

## Lessons
1. **The edge was speed of reversion, not price level.** Positions exited within 3h were essentially always winners; positions held longer lost. Every rule that kept us in longer (deep rungs, cheap side, waiting for cost) cost money.
2. **Infrastructure losses dwarfed strategy losses.** The single worst episode (−10.5K) was a laptop's Wi-Fi plus a hung process, not a bad trade. An always-on server and a watchdog from day one would have been worth more than any parameter tweak.
3. **Share count is the risk, not notional.** Cheap-side positions at 0.05–0.25 carried 50–180K shares, so one tick cost 250–900.
4. **Check that the data you analyse is correct.** The platform's aggregated, off-tick fills (e.g. "BUY −199,246 @ 0.5167") and its sticky avgCost made per-trade P&L wrong. Cash at near-flat moments was the only reliable anchor.
5. **Silent engine behaviour:** orders were shrunk at placement, buys silently refused past ~129x cash, and a 503 sometimes still executed writes. Count refusals and re-read state; never trust a 200.
6. **Close to the event, dips are information.** In the last week, favourites' dips increasingly did not revert, so the dip-buying edge disappeared.
7. **The leaders' scale (~+960K) came from something we never replicated.** It may have been far larger volume and deeper capture of the early-October dumps, and the cheap-side attempt to copy rank 1 did not work for us.

## What runs and how (reference)
- **Trading:**
  - `supervise.sh` runs `run_quoter.py` with the final flags: `--min-fair 0.70 --race-pct 0.85 --budget-pct 135 --mirror 0.5 --cheap 0`, stop-loss v2, watchdog.
  - Stop: `touch data/STOP`.
- **Wind-down:** `winddown.py`. Cancels buys and runs exits until flat, then exits.
- **Longshot sale:** `sell_longshots.py` (dry run by default; `--live` places). One shot, holds to settlement.
- **Monitoring:**
  - `watch_losses.py`: loss and underwater alerts.
  - `monitor_alerts.py`: one alert stream for the bot plus the watcher.
- **Analysis and backtests:** `analyze_realized.py`, `analyze_today.py`, `analyze_mirror.py`, `analyze_leadlag.py` (with `record_tape_ws.py`), `backtest_volstop.py`, `backtest_size.py`.
- **Not in git:** `.env` (API key) and `data/` (logs, state and caches: ~1.7 GB; `quoter.jsonl` is the per-pass account log).
