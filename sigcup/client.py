"""Minimal Super Market API client for the SIG Predictions Cup.

Stdlib only. See docs/api-overview.md (extracted from the platform OpenAPI spec)
for the contract this wraps.

Design notes:
  * Rate limits are per *account*, 100 reads / 30 writes per minute (docs
    "Rate limits & retries"). We throttle locally so we never earn a 429.
  * 429 / 503 / 502 ORDER_STATUS_UNKNOWN / 409 REQUEST_IN_FLIGHT are retried
    with exponential backoff + jitter. Other 4xx are raised immediately.
  * Every order write carries an idempotencyKey so a retry cannot double-place.
  * Writes are gated behind `allow_trading`, which defaults to False. Nothing
    in this module can place an order until a caller opts in explicitly.
"""

from __future__ import annotations

import json
import os
import random
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import deque
from typing import Any, Iterator

DEFAULT_BASE_URL = "https://sig.thesuper.market/api/v1"

# Per-account per-minute budget for a standard (non-DMM) account.
READ_BUDGET = 100
WRITE_BUDGET = 30
# Write slots per minute that low-priority (ladder) writes leave free for exits.
WRITE_RESERVE = 4

RETRY_CODES = {
    "RATE_LIMITED",
    "TX_CONFLICT",
    "SERVICE_UNAVAILABLE",
    "ORDER_STATUS_UNKNOWN",
    "REQUEST_IN_FLIGHT",
}

TICK = 0.005


class ApiError(RuntimeError):
    def __init__(self, status: int, code: str, message: str, details: Any = None):
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.details = details


class RateLimited(ApiError):
    pass


def _ssl_context() -> ssl.SSLContext:
    """Verifying context. python.org builds often ship no CA bundle, so fall
    back to the system one. Verification is never disabled."""
    ctx = ssl.create_default_context()
    if ctx.cert_store_stats().get("x509_ca", 0) == 0:
        for candidate in (os.environ.get("SSL_CERT_FILE"), "/etc/ssl/cert.pem"):
            if candidate and os.path.exists(candidate):
                ctx.load_verify_locations(cafile=candidate)
                break
        else:
            raise RuntimeError(
                "no CA bundle found; run Install Certificates.command or "
                "set SSL_CERT_FILE"
            )
    return ctx


def round_to_tick(price: float) -> float:
    """Snap a price onto the 0.005 grid inside [0.005, 0.995]."""
    p = round(price / TICK) * TICK
    p = min(max(p, TICK), 1 - TICK)
    return round(p, 3)


class _Budget:
    """Sliding-window limiter: at most `limit` events per `window` seconds."""

    def __init__(self, limit: int, window: float = 60.0, headroom: int = 5):
        self.limit = max(1, limit - headroom)
        self.window = window
        self._events: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self, reserve: int = 0) -> None:
        """`reserve` slots are left free for other callers: a low-priority caller
        waits until the window has more than `reserve` slots spare."""
        while True:
            with self._lock:
                now = time.monotonic()
                while self._events and now - self._events[0] >= self.window:
                    self._events.popleft()
                if len(self._events) < self.limit - reserve:
                    self._events.append(now)
                    return
                # Can't index _events[0] if reserve alone blocks an empty window.
                wait = (self.window - (now - self._events[0]) + 0.01) if self._events else 0.25
            time.sleep(wait)


class SuperMarketClient:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        tournament_slug: str = "midterm-elections",
        allow_trading: bool = False,
        timeout: float = 20.0,
        max_retries: int = 5,
    ):
        self.api_key = api_key or os.environ.get("SIG_API_KEY")
        if not self.api_key:
            raise RuntimeError("SIG_API_KEY is not set (see .env)")
        self.base_url = (base_url or os.environ.get("SIG_API_BASE") or DEFAULT_BASE_URL).rstrip("/")
        self.tournament_slug = tournament_slug
        self.allow_trading = allow_trading
        self.timeout = timeout
        self.max_retries = max_retries
        self._ssl = _ssl_context()
        self._reads = _Budget(READ_BUDGET)
        self._writes = _Budget(WRITE_BUDGET, headroom=2)
        # Per-thread flag: writes made while `low_priority.on` is set leave
        # WRITE_RESERVE slots free, so exits never queue behind ladder posts.
        self.low_priority = threading.local()
        self._tournament_id: str | None = None

    # ---------------------------------------------------------------- plumbing

    def _request(self, method: str, path: str, params: dict | None = None, body: dict | None = None) -> Any:
        is_read = method in ("GET", "HEAD")
        url = f"{self.base_url}{path}"
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                url += "?" + urllib.parse.urlencode(clean)
        payload = json.dumps(body).encode() if body is not None else None

        last: Exception | None = None
        for attempt in range(self.max_retries + 1):
            if is_read:
                self._reads.acquire()
            else:
                low = getattr(self.low_priority, "on", False)
                self._writes.acquire(reserve=WRITE_RESERVE if low else 0)
            req = urllib.request.Request(url, data=payload, method=method)
            req.add_header("Authorization", f"Bearer {self.api_key}")
            req.add_header("Accept", "application/json")
            req.add_header("User-Agent", "sigcup-bot/0.1")
            if payload is not None:
                req.add_header("Content-Type", "application/json")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout, context=self._ssl) as r:
                    return json.loads(r.read() or b"null")
            except urllib.error.HTTPError as e:
                raw = e.read()
                try:
                    err = json.loads(raw).get("error", {})
                except Exception:
                    err = {}
                code = err.get("code") or f"HTTP_{e.code}"
                msg = err.get("message") or (raw[:300].decode("utf-8", "replace"))
                exc_cls = RateLimited if e.code == 429 else ApiError
                exc = exc_cls(e.code, code, msg, err.get("details"))
                if code not in RETRY_CODES or attempt == self.max_retries:
                    raise exc
                retry_after = e.headers.get("Retry-After")
                delay = float(retry_after) if retry_after else min(0.1 * 2**attempt, 8.0)
                time.sleep(delay + random.uniform(0, 0.25))
                last = exc
            except (urllib.error.URLError, TimeoutError) as e:
                if attempt == self.max_retries:
                    raise
                time.sleep(min(0.1 * 2**attempt, 8.0) + random.uniform(0, 0.25))
                last = e
        raise last  # pragma: no cover

    def _get(self, path: str, **params) -> Any:
        return self._request("GET", path, params=params)

    def _paged(self, path: str, limit: int = 100, **params) -> Iterator[dict]:
        """Walk a cursor-paginated list endpoint, yielding each row."""
        cursor = None
        while True:
            page = self._get(path, limit=limit, cursor=cursor, **params)
            for row in page.get("data") or []:
                yield row
            pag = page.get("pagination") or {}
            cursor = pag.get("nextCursor")
            if not pag.get("hasMore") or not cursor:
                return

    # ------------------------------------------------------------------- reads

    def account(self) -> dict:
        return self._get("/account")

    def tournaments(self) -> Any:
        return self._get("/tournaments")

    def tournament(self, slug: str | None = None) -> dict:
        return self._get(f"/tournaments/{slug or self.tournament_slug}")

    @property
    def tournament_id(self) -> str:
        """UUID of the competition tournament; cached after first lookup."""
        if self._tournament_id is None:
            self._tournament_id = self.tournament(self.tournament_slug)["id"]
        return self._tournament_id

    def markets(self, limit: int = 100, **params) -> list[dict]:
        return list(self._paged("/markets", limit=limit, **params))

    def tournament_markets(self, slug: str | None = None, limit: int = 100, **params) -> list[dict]:
        return list(self._paged(f"/tournaments/{slug or self.tournament_slug}/markets", limit=limit, **params))

    def market(self, market_id: str, scoped: bool = True) -> dict:
        return self._get(f"/markets/{market_id}", tournamentId=self.tournament_id if scoped else None)

    def market_orderbook(self, market_id: str, depth: int = 200, scoped: bool = True) -> dict:
        """Combined book for every exchange in a market (use for R/D pairs)."""
        return self._get(
            f"/markets/{market_id}/orderbook",
            depth=depth,
            tournamentId=self.tournament_id if scoped else None,
        )

    def exchange_orderbook(self, exchange_id: str, depth: int = 200, scoped: bool = True) -> dict:
        return self._get(
            f"/exchanges/{exchange_id}/orderbook",
            depth=depth,
            tournamentId=self.tournament_id if scoped else None,
        )

    def exchange_price(self, exchange_id: str, scoped: bool = True) -> dict:
        return self._get(
            f"/exchanges/{exchange_id}/price",
            tournamentId=self.tournament_id if scoped else None,
        )

    def prices(self, exchange_ids: list[str], scoped: bool = True) -> dict:
        """Bulk snapshot, max 100 ids per call."""
        if len(exchange_ids) > 100:
            raise ValueError("at most 100 exchange ids per bulk price call")
        return self._get(
            "/exchanges/prices",
            ids=",".join(exchange_ids),
            tournamentId=self.tournament_id if scoped else None,
        )

    def trades(self, exchange_id: str, limit: int = 100, scoped: bool = True) -> Any:
        return self._get(
            f"/exchanges/{exchange_id}/trades",
            limit=limit,
            tournamentId=self.tournament_id if scoped else None,
        )

    def balance(self, slug: str | None = None) -> float:
        """Tournament-scoped cash balance — the number the competition scores."""
        return self.tournament(slug)["myBalance"]

    def positions(self, slug: str | None = None) -> list[dict]:
        """NB: this endpoint does NOT use the data/pagination envelope — it
        returns {positions, summary}. Do not route it through _paged()."""
        r = self._get(f"/tournaments/{slug or self.tournament_slug}/portfolio/positions", limit=100)
        return r.get("positions") or []

    def positions_summary(self, slug: str | None = None) -> dict:
        r = self._get(f"/tournaments/{slug or self.tournament_slug}/portfolio/positions", limit=100)
        return r.get("summary") or {}

    def pnl(self, slug: str | None = None) -> dict:
        return self._get(f"/tournaments/{slug or self.tournament_slug}/portfolio/pnl")

    def fills(self, slug: str | None = None, limit: int = 100,
              exchange_id: str | None = None) -> list[dict]:
        """Newest first. NB: fills of a sell that the engine canonicalized into a
        complement NO buy carry side "no" and a NEGATIVE quantity."""
        return list(self._paged(f"/tournaments/{slug or self.tournament_slug}/portfolio/fills",
                                limit=limit, exchangeId=exchange_id))

    def open_orders(self, **params) -> list[dict]:
        return list(self._paged("/orders", tournamentId=self.tournament_id, **params))

    def order(self, order_id: str) -> dict:
        return self._get(f"/orders/{order_id}")

    def order_fills(self, order_id: str) -> Any:
        return self._get(f"/orders/{order_id}/fills")

    def relationship_constraints(self, **params) -> Any:
        """Engine-evaluated cross-market price constraints — arb candidates."""
        return self._get("/relationships/constraints", tournamentId=self.tournament_id, **params)

    def leaderboard(self, slug: str | None = None, **params) -> Any:
        return self._get(f"/tournaments/{slug or self.tournament_slug}/leaderboard", **params)

    # ------------------------------------------------------------------ writes

    def _require_trading(self) -> None:
        if not self.allow_trading:
            raise PermissionError(
                "trading is disabled on this client; construct with allow_trading=True"
            )

    def place_order(
        self,
        exchange_id: str,
        side: str,
        action: str,
        quantity: int,
        price: float | None = None,
        expiration: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict:
        """Place one limit (price given) or market (price omitted) order.

        `price` is snapped to the 0.005 tick; the engine rejects off-tick values.
        """
        self._require_trading()
        if side not in ("yes", "no"):
            raise ValueError("side must be 'yes' or 'no'")
        if action not in ("buy", "sell"):
            raise ValueError("action must be 'buy' or 'sell'")
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        body: dict[str, Any] = {
            "idempotencyKey": idempotency_key or str(uuid.uuid4()),
            "exchangeId": str(exchange_id),
            "side": side,
            "action": action,
            "quantity": int(quantity),
            "tournamentId": self.tournament_id,
        }
        if price is not None:
            body["price"] = round_to_tick(price)
        if expiration is not None:
            body["expirationDate"] = expiration
        return self._request("POST", "/orders", body=body)

    def place_batch(self, orders: list[dict], idempotency_key: str | None = None) -> dict:
        """Up to 50 independent orders; partial success is possible (207)."""
        self._require_trading()
        if not 1 <= len(orders) <= 50:
            raise ValueError("batch takes 1..50 orders")
        # BatchOrderInput has no top-level tournamentId — it belongs on each
        # order, or the batch silently resolves to the default context.
        scoped = [{**o, "tournamentId": o.get("tournamentId") or self.tournament_id} for o in orders]
        return self._request(
            "POST",
            "/orders/batch",
            body={
                "idempotencyKey": idempotency_key or str(uuid.uuid4()),
                "orders": scoped,
            },
        )

    def place_multi_leg(self, legs: list[dict], idempotency_key: str | None = None) -> dict:
        """Up to 10 legs, all-or-nothing. Use for hedged arb pairs."""
        self._require_trading()
        if not 1 <= len(legs) <= 10:
            raise ValueError("multi-leg takes 1..10 legs")
        return self._request(
            "POST",
            "/orders/multi-leg",
            body={
                "idempotencyKey": idempotency_key or str(uuid.uuid4()),
                "tournamentId": self.tournament_id,
                "legs": legs,
            },
        )

    def cancel(self, order_id: str) -> Any:
        self._require_trading()
        return self._request("DELETE", f"/orders/{order_id}")

    def cancel_all(self, exchange_id: str | None = None, market_id: str | None = None) -> Any:
        """Preferred re-quote path. Retry on 503 until it returns 200 —
        a 503 may have cancelled only some orders (error.details.cancelled)."""
        self._require_trading()
        body: dict[str, Any] = {"tournamentId": self.tournament_id}
        if exchange_id:
            body["exchangeId"] = str(exchange_id)
        if market_id:
            body["marketId"] = str(market_id)
        return self._request("POST", "/orders/cancel-all", body=body)

    def realtime_token(self) -> dict:
        return self._request("POST", "/realtime/token")


# ----------------------------------------------------------------- book helper


def best_levels(book: dict) -> tuple[dict | None, dict | None]:
    """Return (best bid, best ask) levels from an orderbook payload.

    The API returns levels sorted, but we take the extremum explicitly so a
    change in ordering cannot silently give us a wrong top-of-book.
    """
    bids = book.get("bids") or []
    asks = book.get("asks") or []
    best_bid = max(bids, key=lambda l: l["price"]) if bids else None
    best_ask = min(asks, key=lambda l: l["price"]) if asks else None
    return best_bid, best_ask


def load_env(path: str = ".env") -> None:
    """Load KEY=VALUE lines from a .env file into os.environ (no overwrite)."""
    try:
        with open(path) as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip("'\""))
