Programmatic access to the Super Market prediction-market platform: browse markets, read orderbooks, place and cancel orders, track positions and P&L, and manage tournament-scoped trading.

---

## Data Model

```text
Market (container)
├── exchanges[]
│   ├── Binary: 1 exchange (YES/NO)
│   ├── Multi-outcome: N exchanges (may be mutually exclusive or independent;
│   │   imported range outcomes settle independently)
│   └── Each exchange: option, orderbook, trades
└── market_nodes[] (the market's contract/operator structure)
│   ├── operator node (AND / OR / NOT / IF) → children
│   └── contract node (leaf) → maps to an Exchange
│       └── contract_details: contract-type-specific data, or null
```

- A **Market** is a container for one or more tradable outcomes.
- Exchange is the API name for a tradable contract. All orders, trades, and positions reference an `exchangeId`; each exchange has an `option` label (for example `"YES"`, `"Trump"`, or `"Over 3.5"`).
- **Market Nodes** form a tree enabling boolean logic. Operators (AND, OR, NOT, IF) combine leaf contract nodes. Simple and multi-outcome markets have flat node structures.
- Cross-market logical links are served by the **Relationships (ALL)** endpoints: `GET /relationships` lists the canonical engine-owned graph, `GET /relationships/graph` traverses it from a market, and `GET /relationships/constraints` reports engine-evaluated price constraints with suggested corrective trades.

---

## Authentication

All endpoints require an API key. Pass it in one of two ways:

```text
Authorization: Bearer <key>
X-API-Key: <key>
```

Keys are hashed with SHA-256 before storage — the raw key is never persisted and cannot be recovered after creation.

Keys owned by non-platform organization members are automatically bound to that organization. Members do not need to select or send an organization identifier, and cannot create a public-global key; removing their organization membership revokes the bound keys. Platform administrators may deliberately create either global keys or keys bound to a specific organization.

A key works only while the account that owns it is in good standing. While the account is banned, its keys return `403 ACCOUNT_BANNED`; once it is deleted, they return `401 INVALID_API_KEY`; until its email address is confirmed, they return `403 FORBIDDEN` with the message "Confirm the email address on this API key's account to use the API."

This API (`/api/v1`) is the only supported programmatic interface. The site's other `/api/*` endpoints serve the website only: they are not a supported API and may block automated clients.

### Generating an API Key

1. Log in to your account
2. Open **My Profile** and select the **API Keys** tab
3. Under **Create New API Key**, enter a name, select the required scopes, and choose a **Validity** (no expiry, 30 days, 90 days, 6 months, or 1 year)
4. Click **Create Key**
5. Copy the key immediately — it will not be shown again

Organization owners and admins can also create organization-owned **Integration API keys**, which keep integrations independent of personal accounts, from the organization's **Admin → Settings** tab.

### Scopes

| Scope | Endpoints |
|-------|-----------|
| `read` | `GET` — market data, prices, orderbooks, positions, fills, transaction history |
| `trade` | `POST / DELETE` — place and cancel orders |
| `admin` | `/dmm/*` — designated-market-maker management. The key owner must additionally be a platform administrator or an owner/admin of the target resource's organization. An explicitly organization-bound key is restricted to that organization. |

A key can have any combination of scopes. Requests with insufficient scopes return `403 Forbidden`.

### Error Responses

Top-level error responses use a stable shape:

```text
{ "error": { "code": "<CODE>", "message": "<human-readable>", "details"?: { ... } } }
```

`details` is optional structured context; its values are not limited to field-error lists. Per-item `data` inside a `POST /orders/batch` result follows the batch result contract instead of this envelope.

Branch on `code` (stable), not on `message` (free-form, may be tweaked).

| Code | Status | Meaning |
|------|--------|---------|
| `MISSING_API_KEY`     | 401 | No `Authorization` / `X-API-Key` header present |
| `INVALID_API_KEY`     | 401 | Key not recognised, or the account that owns it was deleted |
| `API_KEY_REVOKED`     | 401 | Key was revoked |
| `API_KEY_EXPIRED`     | 401 | Key past its `expires_at` |
| `UNAUTHORIZED`        | 401 | Upstream auth failed (engine call lost auth context); not a client-key problem |
| `INSUFFICIENT_SCOPES` | 403 | Key lacks a required scope (`details.required`, `details.missing`) |
| `ACCOUNT_BANNED`      | 403 | The key owner's account is banned |
| `RESIDENCE_UPDATE_REQUIRED` | 403 | The key owner's Predictions Cup registration is missing a required state or territory, or lists a country, state or territory that is not eligible; update it on the site |
| `ADMIN_REQUIRED`      | 403 | Endpoint requires platform-admin privileges or owner/admin membership in an organization; explicit key binding further restricts the allowed organization |
| `FORBIDDEN`           | 403 | Generic forbidden (e.g. not a member of the requested tournament, your own order outside the API key's organization or tournament context, or the key owner's email address is unconfirmed) |
| `TERMS_NOT_ACKNOWLEDGED` | 403 | The key owner has not accepted the current Terms & Conditions (`details.missingDocuments`); accept them on the site |
| `RATE_LIMITED`        | 429 | Per-account API rate limit exceeded, or the trading engine throttled the request — retry after `Retry-After` when present |
| `INVALID_JSON`        | 400 | Request body is not valid JSON |
| `INVALID_ID`          | 400 | A path-parameter ID failed validation |
| `INVALID_CURSOR`      | 400 | Pagination cursor could not be decoded |
| `VALIDATION_ERROR`    | 400 | Body or query validation failed (`details` may carry field errors) |
| `ORDER_CONFLICT`      | 400 | The order conflicts with the account's existing holdings |
| `NOT_FOUND`           | 404 | Resource does not exist or is not accessible to the caller |
| `CONFLICT`           | 409 | Resource state conflicts with the requested change |
| `REQUEST_IN_FLIGHT`  | 409 | The same idempotency key is still executing — retry the identical request with the same key after 90 seconds |
| `TERMINAL_ENGINE_SCOPE` | 409 | One or more exchange scopes are terminal (for example settled) and cannot be attached |
| `RELATIONSHIP_VIOLATION` | 422 | Requested legs do not satisfy an active ALL relationship's membership or price rule |
| `DATABASE_ERROR`      | 500 | Underlying Prisma error other than serialization conflict |
| `INTERNAL_ERROR`      | 500 | Unexpected server-side failure |
| `ORDER_STATUS_UNKNOWN` | 502 | The engine may have accepted the order but did not confirm it in time |
| `TX_CONFLICT`         | 503 | Serializable transaction conflict — safe to retry with exponential backoff |
| `SERVICE_UNAVAILABLE` | 503 | Authoritative reporting, the trading engine, or another required service is temporarily unavailable, or rate limiting is temporarily unavailable — safe to retry with exponential backoff |

A `502 ORDER_STATUS_UNKNOWN` placement may already have gone through. Check positions,
or retry the identical request with the same idempotency key; that retry is safe and will not double-place.

---

## Quickstart

This walks a bot from zero to a placed order. Examples use `curl`; `$KEY` is your API key and `$BASE` is the API base URL (`https://<host>/api/v1`).

**1. Verify the key.** Confirms the key works and the `read` scope is present. For an organization-bound key, `balance` comes from the organization's default tournament — which may be a finite tournament, not Ongoing Play — and is that tournament's initial virtual balance before enrolment; for an unbound key, it is the public-global cash balance. Use `GET /tournaments/{slug}` for an explicit tournament balance.

```bash
curl -s "$BASE/account" -H "Authorization: Bearer $KEY"
```

**2. Find a market and choose its trading context.** Use `GET /markets`, the canonical discovery endpoint. An unbound API key receives public global markets. An organization-bound key receives one deduplicated row per market across every accessible organization tournament, with tournament metadata, currency, and exchange prices in `contexts`. Choose the intended `contexts[]` entry and carry its `tournament.id` through every context-sensitive read and write. Every order targets an `exchangeId`, not a market.

```bash
MARKETS="$(curl -s "$BASE/markets?limit=20" -H "Authorization: Bearer $KEY")"
MARKET_ID="$(printf '%s' "$MARKETS" | jq -r '.data[0].id')"
EXCHANGE_ID="$(printf '%s' "$MARKETS" | jq -r '.data[0].exchanges[0].id')"
TOURNAMENT_ID="$(printf '%s' "$MARKETS" | jq -r '.data[0].contexts[0].tournament.id')"
TOURNAMENT_SLUG="$(printf '%s' "$MARKETS" | jq -r '.data[0].contexts[0].tournament.slug')"

curl -s "$BASE/markets/$MARKET_ID?tournamentId=$TOURNAMENT_ID" -H "Authorization: Bearer $KEY"
curl -s "$BASE/tournaments/$TOURNAMENT_SLUG"                   -H "Authorization: Bearer $KEY"
```

The commands above show the organization-bound flow. For an unbound key's public context, `tournament` is `null`; omit `tournamentId` from subsequent calls.

**3. Check the price before quoting.** Read the orderbook (or a price snapshot) for the exchange you picked.

```bash
curl -s "$BASE/exchanges/$EXCHANGE_ID/orderbook?tournamentId=$TOURNAMENT_ID" -H "Authorization: Bearer $KEY"
curl -s "$BASE/exchanges/$EXCHANGE_ID/price?tournamentId=$TOURNAMENT_ID"     -H "Authorization: Bearer $KEY"
```

**4. Place an order.** Requires the `trade` scope. `quantity` is always positive; `side` (`yes`/`no`) sets direction. Omit `price` for a market order.

```bash
ORDER_KEY="$(uuidgen)"
curl -s -X POST "$BASE/orders" \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d "{ \"idempotencyKey\": \"$ORDER_KEY\", \"exchangeId\": \"$EXCHANGE_ID\", \"side\": \"yes\", \"action\": \"buy\", \"quantity\": 100, \"price\": 0.42, \"tournamentId\": \"$TOURNAMENT_ID\" }"
```

The response includes `orderId`, `open` (true if resting on the book), `quantityTraded`, and `fillPrice`.

**5. Track execution.** Poll the order and its fills, or cancel if it is still resting.

```bash
curl -s "$BASE/orders/1001"          -H "Authorization: Bearer $KEY"
curl -s "$BASE/orders/1001/fills"    -H "Authorization: Bearer $KEY"
curl -s -X DELETE "$BASE/orders/1001" -H "Authorization: Bearer $KEY"
```

Historical list endpoints share one pagination contract: every response is a bounded page (`limit` capped per endpoint, typically 200) with `pagination.hasMore` and an opaque `pagination.nextCursor` to pass back as `?cursor=`. Where present, the `coverage` object reports `complete` for the requested window and `projectedThroughSequence` — the newest engine event reflected by the read models at response time. Price history defaults to the NEWEST `limit` buckets when `from` is omitted and serves explicit windows forward from `from`, signalling truncation via `coverage.complete` instead of an error.

For low-latency updates between authoritative reads, use the Realtime feeds below. Realtime delivery is best-effort and has no replay buffer: clients must establish initial state and recover after every reconnect, token refresh, socket error, topic revision gap, or batch flagged `resyncRequired` by refetching the corresponding REST resources.

---

## Realtime (WebSocket) feeds

Polling `GET /orders` and `GET /portfolio/*` in a tight loop is slow and the fastest way to hit the rate limit. Use Realtime WebSocket feeds for low-latency updates (versioned order books on tournament markets, invalidation hints elsewhere), then reconcile decisions against the authoritative REST reads.

Realtime runs on Supabase Realtime **private broadcast** channels. Payloads are server-signed. Market and account changes are grouped into one-second batches that retain the actual trade, fill, and order payloads while sharply reducing message count. Tournament market batches also carry `books`: the full order book of each exchange they name, versioned by `asOf`, so a bot can keep a book current without refetching it. Each batch carries `revision` and `previousRevision` and covers the topic's revisions between them; an overdue backlog can arrive merged into fewer batches (as few as the provider's message size limit allows), and a retried send can repeat revisions. A batch whose `revision` is at or below the last accepted one is a duplicate. A batch was missed only when `previousRevision` is greater than the last accepted revision: refetch the corresponding REST resources before acting. A batch flagged `resyncRequired` was too large to carry all its content and also requires a refetch. Engine sequence bounds are provenance, not the topic gap counter. Delivery remains best-effort, so reconnects, token refreshes, and socket errors also require a refetch. There are four things a bot typically wants:

- **Live market batches** — `market:{exchange_id}` (or `market:{market_id}:combined`) emits `market_batch`
  with `trades[]`, `bookDirty[]`, and `marketSettled[]`. Each market-settlement item is `{ marketId, tournamentId, settledWith, at }`, where `settledWith` is the market's stamped settlement value and is `REFUND` for refunds. Every trade carries `id` and `sequence`, the same id `GET /exchanges/{id}/trades` gives it. Public read: the connection's `anonKey` alone can subscribe, without `setAuth`. Tournament markets publish the same batch on the tournament-gated `tournament:{tournament_id}:market:{market_id}` instead, adding the versioned `books` of the exchanges each batch names.
- **Your own account batches** — `user:{profile_id}` emits `account_batch` with `fills[]`, `orderUpdates[]`, `settlements[]`, `refunds[]`, and `collateralChanges[]`, plus a standalone `notification` event `{ notificationId, type }` whenever an in-app notification is created for you. Private: the token carries `realtime_profile_id`, and the user channel is authorized against that claim.
- **Leaderboard invalidation** — `leaderboard:all` emits a `{ at }` `tick` after the global standings are recomputed, with no revision tracking; debounce it, then refetch `GET /leaderboards`. Public read, like the market channels.
- **Constraint monitoring** — `relationships:violations` emits a full public constraint evaluation after a fill leaves a touched ALL relationship violated in the global scope; tournament-scope violations broadcast on the membership-gated `relationships:violations:{tournamentId}`, which an organization-default-tournament subdomain token cannot read without membership. A batch emits at most once per relationship id per scope. Satisfied constraints emit nothing, so confirm clearance with `GET /relationships/constraints`.

The full channel matrix and every event payload live under the **Realtime** tag (`POST /realtime/token`).

### Setup

One authenticated call returns everything needed to connect — no separate Supabase credentials to configure:

```bash
curl -s -X POST "$BASE/realtime/token" -H "Authorization: Bearer $KEY"
# → { "token": "<token>", "expiresAt": "<timestamp>", "supabaseUrl": "<url>", "anonKey": "<anon-key>", "channels": { "user": "user:<profile_id>" } }
```

- `supabaseUrl` + `anonKey` → the connection (`createClient(supabaseUrl, anonKey)`).
- `token` → the per-user access token. Authorize **once** with `setAuth(token)` before subscribing to a tournament or user channel; the public market, leaderboard, and global violation channels need only the `anonKey`.
- `channels.user` → your ready-made `user:{profile_id}` channel name.

Tokens expire after **3 hours** — call `POST /realtime/token` again before expiry and `setAuth` the new token to refresh.

### Connect and subscribe

```ts
import { createClient } from "@supabase/supabase-js";

const { token, anonKey, supabaseUrl, channels } = await fetch("/api/v1/realtime/token", {
  method: "POST",
  headers: { Authorization: `Bearer ${API_KEY}` },
}).then((r) => r.json());

const supabase = createClient(supabaseUrl, anonKey);
await supabase.realtime.setAuth(token); // required for tournament and user channels

// React to every account change in the one-second window.
supabase
  .channel(channels.user, { config: { private: true } })
  .on("broadcast", { event: "account_batch" }, ({ payload }) => {
    payload.fills.forEach(onFill);
    payload.orderUpdates.forEach(onOrderUpdate);
    payload.settlements.forEach(onSettled);
  })
  .subscribe();

// Keep a tournament market's books and trade tape current.
let lastRevision = null;
supabase
  .channel(`tournament:${tournamentId}:market:${marketId}`, { config: { private: true } })
  .on("broadcast", { event: "market_batch" }, ({ payload }) => {
    (payload.books ?? []).forEach(applyBookIfNewer); // by asOf, even on a duplicate revision
    const { revision, previousRevision } = payload.delivery;
    // Check resyncRequired before anything else: a compact one carries no arrays.
    if (payload.resyncRequired) {
      if (lastRevision === null || revision > lastRevision) lastRevision = revision;
      return resyncFromRest();
    }
    if (lastRevision !== null && revision <= lastRevision) return; // duplicate
    const missed = lastRevision !== null && previousRevision > lastRevision;
    lastRevision = revision;
    if (missed || payload.trades.some((trade) => trade.sequence === null)) return resyncFromRest();
    payload.trades.forEach(onTrade);
  })
  .subscribe();
```

`price` in every `trades[]` item, and in every `fills[]` item where it is not null, is the **YES-denominated** execution price in `[0, 1]` — one consistent series regardless of which side traded. For a NO position the effective per-share cost is `1 - price`. The per-trade and per-order events (`trade`, `fill`, `order_update`, `collateral`) are retired; the batches carry them. Some admin and settlement actions still send `book_dirty`, `market_settled`, `position_settled`, and `refund` individually, with the same payload as the matching batch item; see the Realtime token endpoint. A fill against your resting order appears in `fills[]` with no `orderUpdates[]` entry; `orderUpdates[]` reports orders placed or cancelled in the window.

### Keeping a book in sync

1. Subscribe to `tournament:{tournament_id}:market:{market_id}`. Once subscribed, read `GET /markets/{id}/orderbook?tournamentId=...&depth=200`; each exchange's book carries its own `asOf` version `{ sequence, at }`.
2. For each pushed book, replace the book you hold for that exchange when the pushed `asOf` is newer: compare `sequence`, then `at`, and keep the higher. A held book with a null `asOf` was served without an engine version, so any pushed book replaces it. Books apply even when their batch's revision is a duplicate.
3. Read the REST orderbook again after a revision gap, a batch flagged `resyncRequired`, a trade whose `sequence` is null, or a reconnect, token refresh, or socket error.
4. No event announces an order expiry. Each pushed book carries `nextExpiryAt`, the soonest expiry among its resting orders; refetch that exchange's book after it.

`market:*` topics carry no books: refetch the books their `bookDirty[]` entries name.

---

## Common bot workflows

- **Market-making / quoting.** Place a YES and a NO order to bracket the spread with `POST /orders/multi-leg` (atomic — all legs land or none do). Resting quotes on both sides are allowed, but an order that would cross your own resting order is stopped by self-trade prevention: it fills other counterparties, then its crossing remainder is cancelled with `terminalReasonCode: "SelfTradePrevented"`. Re-quote by calling `POST /orders/cancel-all` scoped to the `exchangeId` (or `marketId`), then re-posting. Use `/orders/cancel-all` with `{ "exchangeId": "36" }` rather than cancelling order-by-order. Re-post quotes only after a `200`, which confirms no targeted order is still resting. If `POST /orders/cancel-all` returns `503 SERVICE_UNAVAILABLE`, cancellation paused before it finished (the trading engine was briefly overloaded, or the request ran out of time): `error.details.cancelled` reports how many orders it cancelled so far, and some targeted orders may still be resting. Retry the same cancel-all with backoff until it returns `200`; each retry keeps cancelling whatever is still open. A `503` without `error.details.cancelled` was refused before anything was cancelled and is safe to retry.
- **Bulk entry.** Use `POST /orders/batch` (up to 50 orders) when partial success is acceptable — each order is attempted independently, and a 207 means at least one item succeeded and at least one failed (not partial fills). Orders are placed serially under a bounded budget, so a large batch under load may return 503 partway through; retry with the same `idempotencyKey` to resume it, and size batches for the latency you observe rather than always sending 50. Use `POST /orders/multi-leg` (up to 10) when you need an all-or-nothing guarantee, e.g. a hedged pair.
- **Position monitoring.** Reconcile with `GET /portfolio/positions` for open exposure and `GET /portfolio/pnl` for realized/unrealized P&L. Page through `GET /portfolio/fills` (or `GET /orders/{id}/fills`) to confirm executions. On an organization-bound key, every account and portfolio read resolves to the organization's default tournament when no explicit context is selected — whichever tournament the organization has designated, falling back to its Ongoing Play tournament when none is designated, so the default may be finite. Account reads need no enrolment; the five default-scoped portfolio reads below require membership in that default. Fills require membership in the requested accessible tournament, or the default when omitted; collateral only requires readable scope. Automatic signup enrollment may already satisfy membership, and eligible participants can still enrol just in time on their first order in tournaments without automatic signup enrollment. Only `/portfolio/collateral` and `/portfolio/fills` take a `tournamentId`; `/portfolio/positions`, `/portfolio/pnl`, `/portfolio/history`, `/portfolio/settlements`, and `/portfolio/transactions` always report that default. When you trade in an explicit non-default `tournamentId`, reconcile that exposure through `GET /tournaments/{slug}/portfolio/positions` and its sibling `/tournaments/{slug}/portfolio/*` reads — the no-argument calls will answer for the default instead. Unbound keys keep the public global context.
- **Tournament trading.** Resolve a slug with `GET /tournaments/{slug}`, then pass its UUID `id` as `tournamentId` on market discovery, exchange price/orderbook reads, and order placement. This keeps discovery, quotes, orders, and balances in the same isolated tournament context. On organization-bound keys, omitting `tournamentId` from collection discovery returns all accessible tournament contexts; singular exchange reads and orders remain deterministic by using the organization's default tournament. Unbound keys use the public context.
- **Event-driven loop.** Subscribe to `user:{profile_id}` for `account_batch` and `market:{exchange_id}` (or, for a tournament market, `tournament:{tournament_id}:market:{market_id}`) for `market_batch`. Establish initial state through REST and perform mandatory authoritative REST reconciliation on reconnect, token refresh, socket error, any detected revision gap, or a batch flagged `resyncRequired`.

---

## Rate limits & retries

- Rate limits are enforced **per account**: all of an account's API keys share one budget, so creating more keys does not add capacity. Exceeding it returns `429` with code `RATE_LIMITED` and `Retry-After: 60` — back off and retry rather than hammering.
- The trading engine applies its own throttle to order placement. An engine-throttled order also returns `429 RATE_LIMITED`; the response forwards the engine's `Retry-After` when it supplied one, and that header may be absent. A throttled single or multi-leg order placed nothing; in a batch, each throttled item reports status `429` (see `POST /orders/batch`). Retry the identical request with the same idempotency key after the wait.
- Each account has a per-minute budget. Standard accounts get 100 reads and 30 writes per minute. DMM accounts (platform administrators, or owners/admins of the key's organization) get 600 reads and 200 writes per minute on all of their keys. `GET` and `HEAD` are reads; every other method is a write, so one batch or multi-leg order request is one write and `POST /realtime/token` is one write. Open Realtime connections do not count against the budget.
- `POST /orders`, `POST /orders/batch`, and `POST /orders/multi-leg` require a client-supplied `idempotencyKey`. Generate one key per logical operation and reuse it only when retrying the identical payload; completed retries replay the stored response, while changed payload reuse returns `409 CONFLICT`. Batch retries preserve item order and resume only indexes whose prior result was `429` or `5xx`. A batch also returns `503` if it reaches its execution time budget before issuing every order — orders already placed are kept, so retry with the same key to resume rather than resubmitting under a new one.
- Newly bound single orders and completed best-effort batches or atomic multi-leg requests covered by the original-routing rollout replay their original tournament scopes after an organization default changes. Completed envelopes preserve the original ordered success or terminal-failure envelope across backend changes. Historical requests without routing evidence and incomplete requests retain existing scope/backend conflict fences.
- DMM liquidity `append` and `replace` each require an `operationKey` with the same retry rule on both engine and legacy paths. Keys are scoped per operation type, so the same value may identify one append and one replace independently. A completed request replays its stored status and body, including a stored `400`, `404`, or `409` error, so after fixing the cause of a failure send a new `operationKey`.
- The DMM liquidity sweep (`DELETE`) accepts an optional `Idempotency-Key` header. On engine-routed tournaments, retrying with the same key replays the original sweep, including its `sweptOrders`, rather than removing orders placed since; a retry still passes the same state checks as a new sweep, so if the market settled, the tournament ended, or synchronization is pending in between, the retry gets that `409` or `503` instead. Omitting the header makes each request a new sweep, and reusing a key for a different sweep returns `409 CONFLICT`.
- `409 REQUEST_IN_FLIGHT` means another request with the same idempotency key still owns its 90-second execution lease; wait, then retry the identical payload with the same key.
- Any key-authenticated endpoint may answer `503 SERVICE_UNAVAILABLE` with a `Retry-After` header before it runs when rate limiting is temporarily unavailable: the request was refused, not processed. Wait at least `Retry-After` seconds, then retry with backoff.
- Treat `503 TX_CONFLICT` and `503 SERVICE_UNAVAILABLE` as transient and safe to retry with **exponential backoff** (e.g. 100ms, 200ms, 400ms with jitter). `TX_CONFLICT` means a serializable transaction conflict; `SERVICE_UNAVAILABLE` means authoritative reporting, the trading engine, or another required service is temporarily unavailable.
- Do **not** auto-retry `4xx` responses other than `429` or `409 REQUEST_IN_FLIGHT` — the remaining client errors indicate a problem (bad input, insufficient balance, scope, or membership) that a retry will not fix.
- Prefer Realtime channels (`POST /realtime/token`) for updates between authoritative REST reads; never use them as a substitute for initial or recovery synchronization. Tight polling is the fastest way to hit the rate limit.

---

All responses are JSON. Timestamps are ISO-8601 UTC. Fresh limit-order prices must be on the `0.005` tick between `0.005` and `0.995`; `price: 1` for a buy and `price: 0` for a sell encode market orders, and price may also be omitted for a market order. Off-tick values return `400 VALIDATION_ERROR`. Completed idempotent retries still replay their stored response.