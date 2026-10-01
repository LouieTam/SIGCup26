# SIG Predictions Cup — Midterm Elections: Handoff

Context for building a trading bot for the SIG Predictions Cup (2026 Midterm Elections competition) on https://sig.thesuper.market. Written 2026-10-01, ~2.5h after the competition opened.

Read this first, then the API reference and guide pages saved in `docs/`.

---

## 1. Competition rules that matter

Source: Official Rules, https://predictionscup.com/rules (last updated 2026-09-23).

- **Window:** 2026-10-01 12:00pm ET → **2026-11-04 12:00pm ET**. All trades must be *received and confirmed* by the platform before the deadline. After that, positions are locked.
- **Capital:** 100,000 SUSQies per participant at registration. Non-transferable, no top-ups.
- **Markets:** YES/NO binary markets on 2026 US midterm outcomes, traded on electronic order books (resting bids/offers, trading against other participants). No "mention markets."
- **Resolution:** each market's "Info" tab gives its rules and data source. **Sponsor may change rules, outcomes, or resolution at any time, including after the resolution date.** The platform operator (The Super Market) resolves.
- **Scoring:** final rank = total SUSQies balance **after all markets resolve**. The live leaderboard is provisional and mark-to-market, and only the final balance counts.
- **Prizes:** 1st $30,000 / 2nd $5,000 / 3rd $2,500. Only the top 3 are paid. No tiebreakers: tied participants pool and split the prizes for the positions they occupy.
- **API / bots:** **unlimited bots allowed on one account** (one account per person). Subject to auth, rate limits, **position limits**, and technical controls in the API reference. All bot activity counts as the participant's. Sponsor can throttle, suspend, or remove bots. **No correction of erroneous bot orders.**
- **SIG may act as market maker** in any market and can withdraw liquidity at any time.
- **No trading on MNPI.** This includes anyone who works for, interns for, volunteers for, or advises a campaign whose results decide a market.
- **Force majeure** explicitly covers recounts, challenges, and certification delays.

---

## 2. Payoff structure → strategy implications

- The payoff is extremely convex: only the top 3 of (likely) thousands are paid. **The goal is to maximize P(top 3), not expected value.** A diversified, Kelly-style book almost surely finishes outside the money.
- Plan:
  1. **Phase 1, accumulate (now → as long as the flow lasts):** near-riskless edge from liquidity provision and arbitrage against novice and fat-finger flow.
  2. **Phase 2, concentrate (later):** deploy the accumulated balance into high-variance positions on real toss-ups, where we have a view or a structural edge.
- Leaderboard marks are noisy, especially in thin books. Do **not** optimize for the displayed balance.

---

## 3. Pricing relationships

Notation: per-share payout is 1. $b_X$ and $a_X$ are the best bid and ask for YES in market $X$. $p_X$ is fair probability.

### 3.1 R vs D markets for the same race

Outcomes are mutually exclusive but **not necessarily exhaustive** (independent wins, uncalled race at the resolution date, resolution quirks):

$$p_R + p_D + p_{\text{other}} = 1 \;\Rightarrow\; p_R + p_D \le 1.$$

- **$b_R + b_D > 1$ → riskless arb.** Sell YES in both, i.e. buy NO in both. Cost is $2 - (b_R + b_D) < 1$. At least one NO always pays 1, and if neither party wins, both pay. Locked profit $\ge b_R + b_D - 1$.
- **$a_R + a_D < 1$ → NOT riskless.** Buying YES in both is a short position on "neither" at price $1 - a_R - a_D$. Only take it when $p_{\text{other}}$ is clearly below that.
- **Dominance:** under exclusivity, NO on D weakly dominates YES on R. If YES on R trades above NO on D, buy NO on D and sell YES on R.

### 3.2 Use executable quotes only

The "Market Price" and the YES% / NO% on the market cards are **last trade prices**. They are often stale or reflect a panic print.

Example: Rhode Island showed R YES 3% and D YES 16%, which would imply 81% "neither." That's nonsense. Always compute from the order book.

### 3.3 Races where an independent breaks R + D ≈ 1 (US Senate, as of 2026-10-01)

| State | Situation | Implication |
|---|---|---|
| Nebraska | Ricketts (R) vs Osborn (I). No Democrat running. Polls around R+4 to R+6. | $p_R + p_I \approx 1$, $p_D \approx 0$ |
| Montana | Open seat, three-way: Alme (R), Bankhead (D), Bodnar (I) | All three outcomes are live |
| South Dakota | Rounds (R) vs Bengs (I). Democrat dropped out. Rated Likely/Solid R, but one aggregate showed R+3. | $p_D \approx 0$ |
| Idaho | Risch (R) vs Achilles (I). Democrat dropped out. External markets about 90/10. | $p_D \approx 0$ |
| Mississippi | Pinkins (I) is running. Status not checked. | Verify |

**Resolution-wording trap:** Osborn is backed by the Nebraska Democratic Party but says he will not caucus with either party. Whether "Democrats win NE" resolves YES on an Osborn win depends entirely on the Info-tab wording. Read every market's Info tab.

"State Senate" in a market title may mean the **state legislature's upper chamber**, not a US Senate seat. For chambers, "neither" comes from ties and coalitions (e.g., Alaska's bipartisan coalition), not independents.

---

## 4. Observed market behavior (first ~2 hours)

- **Rhode Island "Will the Democratic Party win the Rhode Island Senate?"** Sat at about 0.95 with no trading for about 20h before the open. After the open it fell to about 0.39, bounced to about 0.78, and last printed at **0.115**.
  - Book snapshot afterward: asks 0.98 (7,922) and 0.995 (2,065). Bids 0.89 (773), 0.69 (1,014), 0.125 (~80k). Displayed spread 9%.
  - Interpretation: large market sells walked a thin book. Fair value is about 0.97+. Deep resting bids were filled at a fraction of fair value.
- Leaders were up about **+359k SUSQies** within about 2h. Likely sources:
  1. Deep stub bids absorbing fat-finger and YES/NO-confused sells.
  2. Mark-to-market on stale or nudged last prices.
  3. Cross-market arb.
- **The flow is heaviest early.** It will decay as novices learn or quit, so speed matters.

---

## 5. Strategy to implement

### 5.1 Fair-value layer
- For every market, set a fair $p$ from external references: Polymarket, Kalshi, forecaster ratings (Cook, Sabato, Inside Elections, Silver Bulletin, Split Ticket), and polling averages.
- Tag each market as `safe` / `lean` / `tossup`, and flag races where an independent changes the identity (§3.3).
- Store the Info-tab resolution text per market. Flag anything nonstandard: margins, specific data sources, deadlines, party-definition wording.

### 5.2 Ladder quoter (Phase 1 core)
- On the near-certain side of every `safe` market, post laddered YES bids. Example for fair ≈ 0.98: 0.92 / 0.85 / 0.70 / 0.50 / 0.25 / 0.10, with **size increasing as price drops**.
- Do the mirror trade on the complementary market: for a safe-D race, bid NO on the R market at 0.90–0.95. This is the same as offering R YES at 0.05–0.10.
- The legs are correlated (same race outcome), so cap **total exposure per race**.
- **Re-post immediately after any fill or sweep.** Fresh bids appeared within minutes of the RI dump, so competitors are already doing this.
- Edge on a fill at price $x$ is about $p - x$ per share.
- Main risk is adverse selection when real news breaks. Restrict to races where no plausible news moves the outcome. Add a kill switch per race and a global one.

### 5.3 Arb scanner
- Continuously check $b_R + b_D > 1$ (riskless) across all paired markets, and any dominance violation (§3.1). Execute when the edge exceeds the threshold.
- Log every $a_R + a_D < 1$ case as a candidate, but **don't auto-trade it**. It is a short on "neither."

### 5.4 Risk / ops
- Hard caps per market, per race, and total. Respect platform position limits.
- Persist all orders and fills locally. The platform's records control, and it will not fix bot errors.
- Back off cleanly on rate-limit responses.
- Keep the API key in an environment variable. Never commit it or paste it into any chat.

---

## 6. Open questions — answer from the API docs / guide before coding

1. Auth scheme, base URL, endpoints for market list, order book, order placement and cancellation, positions, and balance.
2. Order types: limit only? IOC? post-only? Min tick and min size? Is the price scale 0–1 or cents?
3. **Do resting bids lock capital** (reserve $price \times qty$)? This decides how deep and wide the ladder can be.
4. Fees, if any.
5. **Rate limits and position limits** (per market? per account?).
6. **How the leaderboard marks positions:** last trade, mid, or bid?
7. Is there a websocket or streaming feed, or polling only? This matters for re-posting quickly after sweeps.
8. Self-trade prevention behavior, if one account's bots cross each other.
9. Does each R/D pair exist as separate binary markets, or are some multi-outcome?
10. Full market list, and the Info-tab resolution text for each market.

---

## 7. Suggested build order

1. Minimal API client: auth, list markets, fetch books, place and cancel orders, fetch positions and balance. Smoke-test with one tiny order.
2. Market catalog: dump every market plus its Info text to disk, pair R/D markets by race, flag independents and nonstandard wording.
3. Arb scanner, starting read-only, then executing.
4. Ladder quoter on 3–5 safe markets with small size, then scale out.
5. Logging, P&L by race, exposure dashboard, kill switches.
6. Later: Phase 2 tooling for toss-up modelling and concentrated positions.
