# §6 open questions — answered

Answered 2026-10-01 from the platform OpenAPI spec (`docs/openapi.json`,
prose extracted to `docs/api-overview.md`), the product guide pages
(`docs/guide-*.md`), and live read-only API probes.

**Note on provenance:** `docs/` was empty when this work started — the API
reference HANDOFF.md told the reader to consult had never been saved. Everything
here was re-derived from `https://sig.thesuper.market/api/v1/openapi.json` and
`/docs/*`, which are now committed.

---

## 1. Auth and endpoints — answered

- Base URL: `https://sig.thesuper.market/api/v1`. (The spec also lists
  `https://www.thesuper.market/api/v1` as "Production", but our key is bound to
  the SIG organization, so use the `sig.` host.)
- Auth: `Authorization: Bearer <key>` or `X-API-Key: <key>`. Scopes are
  `read` / `trade` / `admin`; `admin` (`/dmm/*`) is unavailable to participants.
- `/api/v1` is the **only** supported programmatic surface. The site's other
  `/api/*` routes may actively block bots.

| Need | Endpoint |
|---|---|
| Market list | `GET /tournaments/{slug}/markets` (tournament-scoped) or `GET /markets` |
| Market detail | `GET /markets/{id}` |
| **Resolution spec** | `GET /markets/{id}/nodes` ← see Q10 |
| Order book (whole market) | `GET /markets/{id}/orderbook?depth=200` |
| Order book (one exchange) | `GET /exchanges/{id}/orderbook` |
| Bulk prices | `GET /exchanges/prices?ids=…` (≤100 ids) |
| Trade tape | `GET /exchanges/{id}/trades` |
| Place order | `POST /orders`, `POST /orders/batch` (≤50), `POST /orders/multi-leg` (≤10, atomic) |
| Cancel | `DELETE /orders/{id}`, `POST /orders/cancel-all` |
| Balance | `GET /tournaments/{slug}` → `myBalance` |
| Positions / P&L | `GET /tournaments/{slug}/portfolio/positions` / `/pnl` |

Orders target an **`exchangeId`**, never a market id. Every context-sensitive
read and every order must carry `tournamentId` (UUID
`bda92870-621e-47b0-bc3c-3602c5c26f55`), or it silently answers for a different
context.

## 2. Order types, tick, scale — answered

- **Limit and market only.** No IOC, no FOK, no post-only, no `timeInForce` —
  these words do not appear anywhere in the spec. A limit order rests until
  filled, cancelled, or expired.
- There *is* an optional `expirationDate` (future ISO-8601) on limit orders —
  a GTD order. Not supported on market orders. **No event fires on expiry**;
  each pushed book carries `nextExpiryAt`, refetch after it.
- **Price scale is 0–1** (probability), not cents. Tick is **0.005**, valid
  range **[0.005, 0.995]**. Off-tick → `400 VALIDATION_ERROR`.
  `price: 1` on a buy, `price: 0` on a sell, or an omitted price = market order.
- `quantity` is a positive integer, max 2^31−1. **No minimum size** is
  documented, so 1 share is legal.
- Direction is `side` (`yes`/`no`) × `action` (`buy`/`sell`) — not a signed
  quantity.
- The engine **canonicalizes** a naked or oversized `sell` into its complement
  buy (`sell yes q@p` ≡ `buy no q@(1−p)`) and reports the canonical form back.
  Log the response's canonical fields, not what you sent.
- An opposite-signed `buy` **nets against** the existing position at FIFO lot
  cost rather than opening a new one.

## 3. Do resting bids lock capital? — tested: `myBalance` does NOT move

Measured 2026-10-01 via `probe_trading.py` on exchange 1077 (market 388):

```
balance before:          100,000.0000
placed 1000 YES @ 0.01   open=True traded=0     (10.00 notional)
balance while resting:   100,000.0000   delta +0.0000
expected if reserved:         10.0000
after cancel-all:        100,000.0000
```

So a resting bid does not reduce `myBalance`, and `GET /portfolio/collateral`
returns `{"data": []}` (it only ever reports ALL-relationship advances, and we
have no ALL relationships — see Q9).

**Caveat, and it matters.** This proves `myBalance` is unaffected; it does *not*
prove there is no reservation. The product guide shows an "Available SUSQie
balance" in the web portfolio, and `POST /orders` can fail `400` for
insufficient balance — so the engine may well track an available figure that
`myBalance` does not expose. Two readings fit the evidence:

- **No reservation at all.** The ladder can be arbitrarily deep, and the engine
  must therefore reject or cancel at *fill* time if cash runs short.
- **Reservation against a hidden available balance.** `myBalance` is total cash,
  and the ladder is still capped — we just can't see the cap.

The discriminating test is to rest a bid whose notional *exceeds* the balance
(e.g. 150,000) and see whether it is accepted. **Not run:** on a real book that
means briefly resting an order we could not pay for, and a sweep into it during
the few seconds before cancellation would open a position worth more than half
the account. That's a sizing decision, not a technical one — needs a human call
on acceptable exposure.

Until it's settled, **cap total resting ladder notional at the cash balance**.
That is correct under both readings and costs nothing if reservation turns out
to be absent.

## 4. Fees — answered: none

No trading fee anywhere in the spec. The only `fee` references are an AI
`video_upgrade_fee` on market creation (participants can't create markets) and a
`fee` value in the transaction-type enum. The official rules list no fee either.
Treat execution as frictionless; the spread is the whole cost.

## 5. Rate limits and position limits — partly answered

**Rate limits (clear):** enforced **per account**, not per key — all of an
account's keys share one budget, so extra keys buy no capacity. Standard
accounts get **100 reads + 30 writes per minute**. `GET`/`HEAD` are reads;
everything else is a write, so a 50-order batch is *one* write. Open WebSocket
connections are free. `429` carries `Retry-After: 60`.

> This is the binding constraint on the whole design. 30 writes/min means the
> ladder quoter cannot re-post order-by-order: batch and `cancel-all` are
> mandatory, not optimizations. `POST /realtime/token` also costs a write.

**Position limits: none found.** The official rules say bots are "subject to
… position limits … in the API reference", but the API reference specifies no
position limit at any scope — per market, per account, or otherwise. The only
cap is `quantity ≤ 2^31−1` and your cash balance. Either the rules reference
something unpublished, or limits are enforced server-side without being
documented. Assume our own risk caps are the only ones and watch for an
undocumented rejection code on the first large order.

## 6. How the leaderboard marks positions — ANSWERED: cumulative VWAP

**`valuation_price` is the volume-weighted average price of every trade in the
market's entire history.** Not last trade, not mid, not bid, not ask.

Measured on exchange 1077 by taking a 1-share position and reconstructing the
VWAP from `GET /exchanges/{id}/price-history`:

```
cumulative VWAP over all candles (3,019,992 shares) = 0.310033
Position.currentPrice (valuation_price)             = 0.310033
difference                                          = 0.000000
executable book at that moment: bestBid 0.04 / bestAsk 0.05
```

**The mark was 6.2x the best ask.** It drifts slowly downward as new prints
accumulate into the average (0.310075 → 0.310033 over ~40s).

The cause is the chaotic open. Market 388's hourly candles:

| hour (UTC) | open | high | low | close | vwap | volume |
|---|---|---|---|---|---|---|
| 16:00 | 0.05 | **0.995** | 0.005 | 0.03 | 0.544 | 1,062,038 |
| 17:00 | 0.035 | 0.945 | 0.005 | 0.05 | 0.206 | 1,004,806 |
| 18:00 | 0.05 | 0.745 | 0.015 | 0.045 | 0.211 | 595,795 |
| 19:00 | 0.03 | 0.225 | 0.01 | 0.035 | 0.073 | 356,954 |

The first hour printed between 0.005 and 0.995 on a contract now worth ~0.045.
Those prints are permanently baked into the cumulative average, and because the
denominator is already ~3M shares, **no realistic amount of honest trading will
pull the mark back to fair value before 2026-11-04.**

### Consequences

1. **The leaderboard is largely fictional.** Unrealized P&L marks every position
   at this inflated VWAP. The leader's +374,645 on a 100,000 start, and the
   cluster of accounts at exactly `roi: 500`, are consistent with mark-to-VWAP
   artifacts rather than realized cash. HANDOFF §4's "leaders up +359k in ~2h"
   is most likely the same artifact, not evidence of a skill edge we need to
   match. **The real competition is weaker than the board suggests.**
2. **Only the final settled balance scores** (rules §Scoring), and settlement
   pays 1 or 0 per share — never the VWAP. So the inflated mark converts to
   nothing. HANDOFF §2's "do not optimize the displayed balance" is now
   quantitatively justified, not just prudent.
3. **Never use `currentPrice`, `marketValue`, or `unrealizedPnl` for risk or
   sizing decisions.** They are off by multiples. Compute our own mark from the
   order book. This applies to `GET /portfolio/pnl` and the P&L fields on every
   portfolio read.
4. There is an obvious rank-inflation trade available to anyone who notices
   (buy cheap in a high-VWAP market, book an instant paper gain). It yields no
   settled SUSQies, so it is not worth doing — but expect others to be doing it,
   which further decouples the board from reality.
5. Three different "prices" coexist and must not be confused:
   `latestPrice` (last trade), `impliedProbability` (**best ask**, per the
   schema), and `valuation_price` (cumulative VWAP). HANDOFF §3.2's rule "always
   compute from the order book" is right, and now has a sharper reason.

## 7. WebSocket / streaming — answered: yes

Supabase Realtime **private broadcast** channels. `POST /realtime/token`
returns `{token, expiresAt, supabaseUrl, anonKey, channels.user}` — no separate
Supabase config. Token lives 3 hours; refresh with `setAuth`.

Channels we want:

- `tournament:{tournamentId}:market:{marketId}` → `market_batch`, and this one
  **carries full versioned order books** (`books[]`, versioned by
  `asOf = {sequence, at}`). A bot can hold every book current without spending
  read budget. This is the answer to "re-post quickly after sweeps".
- `user:{profileId}` → `account_batch` with `fills[]`, `orderUpdates[]`,
  `settlements[]`, `collateralChanges[]`.
- `leaderboard:all` → debounced `tick`.

Caveats that must be designed in, not bolted on: changes are coalesced into
**1-second batches**, so this is not tick-by-tick; delivery is best-effort with
**no replay buffer**. Track `revision` / `previousRevision` and refetch via REST
on a gap, on `resyncRequired`, on a null trade `sequence`, and after every
reconnect, token refresh, or socket error. Apply a pushed book when its `asOf`
is newer, **even on a duplicate revision**.

Requires the `@supabase/supabase-js` client (or a raw Phoenix-protocol
WebSocket implementation) — the only external dependency the project needs.

## 8. Self-trade prevention — answered

Resting quotes on both sides are allowed. An order that would cross your own
resting order **fills other counterparties first**, then its crossing remainder
is cancelled with `terminalReasonCode: "SelfTradePrevented"`. It is not a
rejection, so a partial fill plus a silent cancel is the normal outcome — check
`terminalReasonCode` on every response or the bot will believe a quote is still
resting when it isn't.

Re-quote via `POST /orders/cancel-all` scoped to `exchangeId`, and **re-post only
after a 200**. A `503` from cancel-all means it stopped partway;
`error.details.cancelled` counts what it got, and some orders may still rest.
Retry the same cancel-all with backoff until 200.

## 9. R/D pairs — answered, and better than hoped

**Every market is a separate single-exchange binary.** All 237 tournament
markets have exactly one exchange, `option: "YES"`; 0 are multi-outcome. There
is no multi-outcome container to arb within, and `hasArbitrageOpportunity` /
`overround` on the combined book are therefore useless to us (one outcome ⇒
overround = that outcome's own implied probability).

**But `GET /markets/{id}/nodes` returns `contract_details.raceId`, which pairs
markets exactly — no title parsing.** Verified:

| Market | raceId | winnerName | raceName |
|---|---|---|---|
| 388 | 62978 | Republican Party | U.S. Senate Rhode Island |
| 387 | 62978 | Democratic Party | U.S. Senate Rhode Island |
| 281 | 62971 | Republican Party | U.S. Senate Nebraska |
| 280 | 62971 | **Nonpartisan** | U.S. Senate Nebraska |
| 279 | 62971 | Democratic Party | U.S. Senate Nebraska |

Two consequences for HANDOFF §3:

1. **The Osborn wording trap is resolved.** `resolutionType` is
   `"Party Winner"` and the market resolves on `winnerName`. Nebraska has a
   *third* market (`winnerName: "Nonpartisan"`) for exactly this case, so an
   Osborn win resolves market 280 YES and leaves **both** 279 and 281 at NO.
   Osborn's Democratic backing does not make market 279 pay. Races with three
   markets are near-exhaustive, so the three-way bid-sum arb
   ($b_R + b_D + b_I > 1$) is available on top of the two-way check.
2. **`GET /relationships` returns an empty list.** The platform has defined no
   ALL relationships, which means: (a) the engine does **not** know R and D are
   related, so `/relationships/constraints` will never flag our arbs — we must
   compute them ourselves, as planned; and (b) more importantly, there is **no
   collateral netting**. A hedged R/D pair ties up capital on both legs
   independently. Budget for the gross notional of both legs, not the net risk.

## 10. Market list and resolution text — answered via a better route

237 open markets, all settling **2026-11-04T17:00:00Z** (the competition
deadline), with `electionDate: 2026-11-03`.

`GET /markets/{id}` has **no** rules/info/description field — the Info-tab prose
HANDOFF §5.1 planned to scrape is not in the API. `GET /markets/{id}/nodes` is
strictly better: a structured `contract_details` with `raceId`, `stageId`,
`usState`, `raceName`, `raceStage`, `winnerName`, `officeLevel`,
`electionDate`, `contractType`, and `resolutionType`.

That turns step 2 of the build order from fuzzy text-flagging into a join:
group by `raceId`, read `winnerName` per leg, and flag any market whose
`resolutionType` is not `"Party Winner"` (margin questions, chamber control, and
the four manually-settled chamber markets) as needing human review. 237 markets
is ~3 pages of list plus 237 node reads — about 3 minutes under the 100 read/min
budget.

---

## Risks surfaced that HANDOFF.md does not account for

1. **Refunds break the "riskless" arb.** Settlement docs: a cancelled or N/A
   outcome "refunds the refundable cost of the held shares" instead of paying a
   winner. HANDOFF §3.1 assumes at least one NO leg always pays 1. Under a
   refund both legs return cost, not 1, so $b_R + b_D > 1$ is riskless *only*
   if the race resolves normally. Force majeure explicitly covers recounts and
   certification delays, and the four chamber markets settle manually. Size the
   "riskless" book as if it carries real tail risk.
2. **The write budget, not capital, limits the quoter.** 30 writes/min across
   all bots on the account. A ladder across 5 races × 6 rungs × 2 legs is 60
   resting orders; a single sweep-and-re-post cycle costs 1 cancel-all + 2
   batches per race. Re-posting on every fill across many races will exhaust
   the budget — the quoter needs a global write scheduler from day one.
3. **Markets settle at the deadline, not on election night.** Settlement date
   is 2026-11-04T17:00Z, the same instant trading closes, and autosettlement
   workers only run every 4 hours. Final rank depends on settlement that
   happens after we can no longer act.
4. **The leaderboard is marked at cumulative VWAP, not executable prices** — see
   Q6. Resolved, and the answer is that leaderboard P&L should be ignored
   entirely.
5. **The API is slow: 2–17s per call** (`/account` took 17.5s, `/nodes` ~5s,
   market list 7.6s). The 100 reads/min budget is *not* the binding constraint
   on reaction speed — latency is. A quoter that re-reads a book over REST
   before re-posting is already many seconds stale, so the Realtime websocket
   feed (which pushes versioned books) is **mandatory for the quoter, not an
   optimization**. Concurrency helps throughput: the catalog build went from
   ~20 min sequential to 2m31s with 8 threads sharing the rate limiter.
6. **`GET /tournaments/{slug}/portfolio/positions` does not use the standard
   `data`/`pagination` envelope** — it returns `{positions, summary}`. Routing it
   through a generic pager silently yields an empty list (this bit us once; the
   client now special-cases it). Verify envelope shape per endpoint rather than
   assuming the documented pagination contract applies.
7. **`TournamentSummary` omits documented required fields.** `joinedAt` is
   absent and `marketCount` / `memberCount` are present but undocumented. Treat
   the spec's `required` arrays as advisory and use `.get()`.
