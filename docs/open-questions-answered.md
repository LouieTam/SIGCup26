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

## 3. Do resting bids lock capital? — NOT ANSWERED (needs a live test)

Not documented. The evidence is indirect and points toward **yes**:

- The portfolio guide lists "Available SUSQie balance" as distinct from
  position value, which implies a reserved bucket.
- `POST /orders` returns `400` for "insufficient balance".
- `GET /portfolio/collateral` exists but only reports *ALL-relationship*
  collateral advances — and we have **no ALL relationships** (see Q9), so it
  returns nothing useful for us.

No read endpoint exposes a free-vs-reserved split. **This needs one cheap
empirical test:** read `myBalance`, rest a far-from-market bid (e.g. 1,000
shares at 0.01 = 10 SUSQies notional), re-read `myBalance`, then cancel. If the
balance drops by 10, bids reserve `price × qty` and the ladder's total notional
is capped by cash. This is the single most important unknown for ladder sizing,
and it's blocked only because no orders were to be placed in this step.

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

## 6. How the leaderboard marks positions — partly answered

`GET /tournaments/{slug}/leaderboard` ranks by `pnl` (realized + unrealized) and
returns `rank, profileId, username, pnl, tradesCount, volume, winRate, roi`.
Ranking is standard competition ranking (1-1-3).

Unrealized P&L uses the **tournament `valuation_price`** — the spec names it but
never defines it. Two data points:

- `Position.currentPrice` = "tournament valuation price".
- `CombinedOrderbookExchange.impliedProbability` = "**best ask** price, or
  `latestPrice` if no asks exist" — so at least the market-card probability is
  an *ask*, not a last trade.

If `valuation_price` follows the same rule, HANDOFF §3.2's claim that displayed
probabilities are last-trade prices is **wrong for the book-derived field** and
right only for `latestPrice`. Confirming requires holding one position and
comparing `currentPrice` against that exchange's book — do this alongside the
Q3 test.

Either way the strategic conclusion in HANDOFF §2 stands: only the **final
settled balance** scores, so do not optimize the displayed mark.

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
4. **The leader was at +374,645 on a 100,000 start (ROI 500%) ~7h after open**,
   on 150 trades and only 49,902 volume. Several top accounts show exactly
   `roi: 500`, which looks like a cap or a mark artifact rather than realized
   cash. Worth understanding before treating leaderboard P&L as a target.
