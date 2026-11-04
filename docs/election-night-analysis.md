# Election night: how the big players earned (written 2026-11-04 ~13:30Z)

Sources:
- the leaderboard at 11-03 12:47Z and at 11-04 ~13:00Z (P&L, plus 1-day trade count and volume);
- platform hourly candles;
- Polymarket price histories (CLOB `prices-history`, keeping only points at or before the requested end time; for open markets the API appends the current price).

Other accounts' positions are not visible: no public profile or positions endpoint answered.

## What happened
**A Democratic wave.**
- Of 36 Democratic legs priced 0.2–0.8 before the vote, 28 won.
- Buying every one of them at the 11-03 12:47Z price made **+0.24 per share** (about +50% on cost).
- The cheapest winners roughly tripled: SC Senate D 0.33, CO-04 D 0.315, FL-09 D 0.395, TX-23 D 0.395, Kansas Gov D 0.40, Texas Gov D 0.435.

**Before the vote, the platform matched Polymarket.**
- On 79 toss-up legs the mean absolute gap was 3.5c.
- The two were about equally accurate: Brier 0.175 for the platform, 0.170 for Polymarket.
- Buying where Polymarket was 3c+ higher made +13% on cost over 18 legs, a small edge.
- **The overnight money came from direction, not from a gap between the two markets.**

**During the count, the platform did not lag Polymarket.**
- On the eventual winners, the platform was *ahead* of Polymarket at 00–01Z (by −6c and −12c on average), and level after that.
- About 14.9M was traded on those 37 legs between 22Z and 13Z.

## Who earned what (change from 11-03 12:47Z; 1-day trades)
1. **Positions held into the night, no trading since:**
   - steady-ember **+941K** (0 trades) went from #11 to #3. quick-otter +122K, tidy-dawn +104K and happy-maple +74K also made 0 trades.
   - About 15 accounts entered the top 45 with 0 trades on the day.
   - This was the largest source of gains: a concentrated directional bet on the wave, placed before 11-03 12:45Z.
   - Some lost on the other side: bright-finch **−246K**, kind-brook −37K, noble-birch −25K.
2. **Few, large trades on the day:**
   - steady-finch +384K (50 trades, 0.32M volume) took #1.
   - merry-birch +107K from **one** trade of 105K notional, which roughly doubled.
   - clever-pebble +100K (3 trades); quick-finch +227K (6 trades).
3. **Trading the count, high activity:**
   - nimble-beacon +198K (1,085 trades), merry-dawn +125K (5,740 trades, 7.7M volume), warm-beacon +134K (880).
   - noble-willow +73K (6,039), happy-coral +108K (1,364), bright-dawn-8174 +73K (1,200).
   - Since the platform led Polymarket, their edge was reacting quickly to the count and to race calls, not copying Polymarket.

## Caveats
The leaderboard is at market prices, and those prices are chaotic this morning: our own positions are marked −7.0K although every resolved leg went our way. Expect changes at settlement. The leaderboard lists **413 ranked accounts**.

## What it means for us
- Our approach was close to market-neutral: providing liquidity, then near-certain holds to settlement. By design it sat out the one big directional event.
- Top places were decided by early-October sweep capture at scale (the top 3 were about +1M by 11-02) and by election-night directional risk.
- Before the vote, a directional bet had about zero expected value (the platform matched Polymarket). So that route works like a lottery among risk-takers: for each steady-ember there is a bright-finch.
- The one replicable edge was **speed on the count**: buying called winners within minutes of the result. Our 04:10Z round caught only what was left, about +1K.
