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
