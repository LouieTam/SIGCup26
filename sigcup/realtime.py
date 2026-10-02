"""Supabase Realtime (Phoenix) client for instant fill notifications.

Why this exists: polling tells us about a fill up to a whole pass later, and a
sweep reverts in ~75 seconds. The `user:{profile_id}` channel pushes
`account_batch` with `fills[]` as it happens, and open WebSocket connections do
not count against the rate limit — so this is the only way to react inside the
window that matters.

Implements the Phoenix wire protocol directly (one dependency: `websockets`)
rather than pulling in supabase-py.

Delivery is best-effort with no replay buffer, so every consumer MUST treat REST
as authoritative and refetch after any gap, reconnect, or token refresh. This
module reports those conditions rather than hiding them.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Awaitable, Callable

import websockets

from .client import _ssl_context

HEARTBEAT_S = 25.0
TOKEN_REFRESH_MARGIN_S = 600.0  # tokens live 3h; re-mint well before expiry


class RealtimeClient:
    def __init__(self, client, on_event: Callable[[str, dict], Awaitable[None] | None]):
        """`client` is a SuperMarketClient (used to mint/refresh tokens).
        `on_event(kind, payload)` is called for each event; `kind` is the Phoenix
        event name, e.g. "account_batch", plus synthetic "_resync" / "_status".
        """
        self.c = client
        self.on_event = on_event
        self._ref = 0
        self._token: dict | None = None
        self._minted_at = 0.0

    # ------------------------------------------------------------------ tokens

    def _mint(self) -> dict:
        self._token = self.c.realtime_token()
        self._minted_at = time.time()
        return self._token

    def _fresh_token(self) -> dict:
        if self._token is None or time.time() - self._minted_at > 3 * 3600 - TOKEN_REFRESH_MARGIN_S:
            return self._mint()
        return self._token

    # ------------------------------------------------------------------- wire

    def _next_ref(self) -> str:
        self._ref += 1
        return str(self._ref)

    async def _emit(self, kind: str, payload: dict) -> None:
        r = self.on_event(kind, payload)
        if asyncio.iscoroutine(r):
            await r

    async def _heartbeat(self, ws) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_S)
            await ws.send(json.dumps({
                "topic": "phoenix", "event": "heartbeat",
                "payload": {}, "ref": self._next_ref(),
            }))

    async def _run_once(self, topics: list[str]) -> None:
        tok = self._fresh_token()
        host = tok["supabaseUrl"].rstrip("/")
        ws_url = host.replace("https://", "wss://").replace("http://", "ws://")
        url = f"{ws_url}/realtime/v1/websocket?apikey={tok['anonKey']}&vsn=1.0.0"

        # Same CA-bundle fallback as the REST client; verification stays on.
        async with websockets.connect(
            url, max_size=8 * 1024 * 1024, ssl=_ssl_context()
        ) as ws:
            for topic in topics:
                await ws.send(json.dumps({
                    "topic": f"realtime:{topic}",
                    "event": "phx_join",
                    "payload": {
                        "config": {
                            "broadcast": {"self": False},
                            "private": True,
                        },
                        "access_token": tok["token"],
                    },
                    "ref": self._next_ref(),
                }))
            hb = asyncio.create_task(self._heartbeat(ws))
            # A fresh subscription is a gap by definition: state before this
            # moment was never delivered, so the consumer must reconcile.
            await self._emit("_resync", {"reason": "subscribed", "topics": topics})
            try:
                async for raw in ws:
                    msg = json.loads(raw)
                    ev, payload = msg.get("event"), msg.get("payload") or {}
                    if ev == "phx_reply":
                        status = (payload or {}).get("status")
                        await self._emit("_status", {"topic": msg.get("topic"), "status": status,
                                                     "response": payload.get("response")})
                    elif ev == "broadcast":
                        inner = payload.get("payload") or {}
                        await self._emit(payload.get("event") or "broadcast", inner)
                    elif ev in ("phx_error", "phx_close"):
                        await self._emit("_status", {"topic": msg.get("topic"), "status": ev})
                        return
            finally:
                hb.cancel()

    async def listen(self, topics: list[str], max_backoff: float = 60.0) -> None:
        """Reconnect forever. Every reconnect emits `_resync` first."""
        backoff = 1.0
        while True:
            try:
                await self._run_once(topics)
                backoff = 1.0
            except Exception as e:  # noqa: BLE001 - any socket/auth failure must retry
                await self._emit("_status", {"status": "error", "error": f"{type(e).__name__}: {e}"})
            self._token = None  # force a fresh token on reconnect
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)


def user_topic(client) -> str:
    return client.realtime_token()["channels"]["user"]
