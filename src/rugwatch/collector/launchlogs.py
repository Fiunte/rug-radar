"""Second launch source (decision D-31): the public Solana RPC websocket, `logsSubscribe` on
transactions that mention the pump.fun mint authority, i.e. every create. Free, no key, one
connection, not a trade stream and not Helius. Approved by the user on 2026-11-17 (HANDOFF
section 0), because PumpPortal's free stream misses launches made in version-1 transactions.

The create transaction's logs carry the CreateEvent (mint, bonding curve, creator, ...) and the dev
buy's TradeEvent, so no extra RPC call is needed per launch. Both sources write into the same
`tokens` table; the first to report a mint sets `launch_obs` (t0), and `launch_sources` records
every source that saw it, for the daily coverage check.

Fallback (decision D-33, pre-approved by the user on 2026-11-17): when the public endpoint rate-limits
us, the same subscription moves to FluxRPC's websocket at once. Every hour on the fallback the
listener tries the public endpoint again. FluxRPC's free plan allows 10 GB a month, so every byte
received from FluxRPC is counted (`bandwidth_ledger`), and the fallback is not used above 90% of it.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from rugwatch.chain import pump_layout as pl
from rugwatch.chain.tx_parse import CreateEvent, TradeEvent, decode_create_event, decode_trade_event
from rugwatch.collector.pumpportal import LAUNCH_STALL_S, LaunchStall
from rugwatch.collector.state import State
from rugwatch.storage.raw import DiskFullError, RawWriter, make_record

log = logging.getLogger("rugwatch.launchlogs")

PUBLIC_WS_URL = "wss://api.mainnet-beta.solana.com"
MINT_AUTHORITY = "TSLvdd1pWpHVjahSpsvCXUbgwsL3JAcvokwaKt1eokM"  # signs every pump.fun create
SOURCE = "publicrpc"
COMMITMENT = "confirmed"
STALE_AFTER_S = 120.0  # creates arrive every few seconds; no message at all for this long = reconnect
BACKOFF_S = (1.0, 2.0, 5.0, 10.0, 30.0, 60.0)
RATE_LIMIT_BACKOFF_S = (60.0, 300.0, 900.0)  # back off hard when the public endpoint pushes back
RATE_LIMIT_CLOSE_CODES = frozenset({1008, 1013})  # policy violation, try again later
FALLBACK = "fluxrpc"
FLUXRPC_MONTHLY_LIMIT_BYTES = 10 * 10**9  # FluxRPC free plan: 10 GB bandwidth a month
FLUXRPC_STOP_SHARE = 0.9  # never use the fallback above this share of the monthly limit
FLUXRPC_ALERT_SHARE = 0.8  # `make status` alerts from here
RETRY_PUBLIC_AFTER_S = 3600.0  # on the fallback, try the public endpoint again after this long
FALLBACK_MAX_FAILS = 3  # consecutive fallback failures before going back to the public endpoint
SUBSCRIBE = {"jsonrpc": "2.0", "id": 1, "method": "logsSubscribe",
             "params": [{"mentions": [MINT_AUTHORITY]}, {"commitment": COMMITMENT}]}


def fluxrpc_ws_url() -> str | None:
    """FluxRPC websocket: the dashboard's "WebSocket" value if set, else the documented HTTP endpoint's
    documented websocket host (fluxrpc.com/docs/websocket) with the API key as `key`. The URL contains
    the key: never log it."""
    if url := os.environ.get("FLUXRPC_WS_URL", "").strip():
        return url
    if key := os.environ.get("FLUXRPC_API_KEY", "").strip():
        return f"wss://ws.eu.fluxrpc.com?key={key}"
    return None


class PlannedSwitch(Exception):
    """Leave the fallback on purpose: the retry interval passed or its bandwidth budget is used up."""


class SubscriptionError(Exception):
    """The endpoint answered the subscription (or later) with a JSON-RPC error."""

    def __init__(self, error: Any) -> None:
        super().__init__(str(error)[:200])
        self.error = error


@dataclass
class Launch:
    create: CreateEvent
    dev_buy: TradeEvent | None


def launches_from_logs(logs: list[str]) -> list[Launch]:
    """Every CreateEvent in a transaction's logs, with the creator's first buy of that mint if any."""
    events: list[bytes] = []
    for line in logs:
        if line.startswith("Program data: "):
            try:
                events.append(base64.b64decode(line[14:], validate=True))
            except (ValueError, binascii.Error):
                continue
    creates = [c for c in map(decode_create_event, events) if c is not None]
    trades = [t for t in map(decode_trade_event, events) if t is not None]
    return [Launch(c, next((t for t in trades if t.mint == c.mint and t.is_buy), None)) for c in creates]


def launch_message(sig: str | None, launch: Launch) -> dict[str, Any]:
    """The launch in PumpPortal's field names and units (initialBuy in whole tokens, solAmount in SOL),
    so both sources fill `tokens` the same way."""
    c, buy = launch.create, launch.dev_buy
    return {"signature": sig, "mint": c.mint, "traderPublicKey": c.user, "bondingCurveKey": c.bonding_curve,
            "name": c.name, "symbol": c.symbol, "uri": c.uri,
            "initialBuy": buy.token_amount / 10**pl.TOKEN_DECIMALS if buy else None,
            "solAmount": buy.sol_amount / pl.LAMPORTS_PER_SOL if buy else None}


def is_rate_limited(exc: BaseException) -> bool:
    if isinstance(exc, InvalidStatus):
        return exc.response.status_code == 429
    if isinstance(exc, ConnectionClosed):
        rcvd = getattr(exc, "rcvd", None)
        if rcvd is None:
            return False
        reason = (rcvd.reason or "").lower()
        return rcvd.code in RATE_LIMIT_CLOSE_CODES or "rate" in reason or "limit" in reason or "429" in reason
    if isinstance(exc, SubscriptionError):
        text = json.dumps(exc.error).lower()
        return "rate" in text or "limit" in text or "429" in text
    return False




class ChainLaunchListener:
    def __init__(self, state: State, writer: RawWriter, first_tick_s: int, url: str = PUBLIC_WS_URL,
                 fallback_url: str | None = None) -> None:
        self.state = state
        self.writer = writer
        self.first_tick_s = first_tick_s
        self.urls = {SOURCE: url, **({FALLBACK: fallback_url} if fallback_url else {})}
        self.endpoint = SOURCE  # where the stream currently comes from; also the raw `source`
        self.connected = False
        self.last_message_at: float | None = None
        self.last_launch_at = time.time()
        self._connected_at = time.time()
        self._endpoint_since = time.time()
        self._dropped_at: float | None = None

    def handle(self, value: dict[str, Any], slot: int | None, observed_at: float) -> int:
        """One logsNotification value ({signature, err, logs}). Every notification is written to raw;
        successful creates become tokens. Returns the number of launches it carried."""
        sig = value.get("signature")
        launches = [] if value.get("err") is not None else launches_from_logs(value.get("logs") or [])
        self.writer.write(make_record(
            observed_at=observed_at, source=self.endpoint, kind="new_token", purpose="launch_log", status="ok",
            payload=value, mint=launches[0].create.mint if len(launches) == 1 else None, slot=slot))
        for launch in launches:
            mint = launch.create.mint
            self.state.add_token(mint, observed_at, launch_message(sig, launch), observed_at + self.first_tick_s)
            self.state.add_launch_source(mint, self.endpoint, observed_at, sig)
            self.last_launch_at = max(self.last_launch_at, observed_at)
        return len(launches)

    def fallback_allowed(self, now: float) -> bool:
        return FALLBACK in self.urls and (self.state.bandwidth_month(FALLBACK, now)
                                          < FLUXRPC_STOP_SHARE * FLUXRPC_MONTHLY_LIMIT_BYTES)

    def _switch(self, to: str, reason: str) -> None:
        now = time.time()
        self.state.event("rpcws_switch", {"from": self.endpoint, "to": to, "reason": reason})
        log.warning("launch log stream: switching %s -> %s (%s)", self.endpoint, to, reason)
        self.endpoint = to
        self._endpoint_since = now
        self.state.set_meta("launch_ws_endpoint", to)
        self.state.set_meta("launch_ws_endpoint_since", repr(now))

    def _mark_drop(self, reason: str) -> None:
        if self.connected or self._dropped_at is None:
            self._dropped_at = time.time()
            self.state.event("rpcws_drop", {"reason": reason, "endpoint": self.endpoint})
        self.connected = False

    async def run(self) -> None:
        if self.state.get_meta("launch_ws_endpoint") != self.endpoint:
            self.state.set_meta("launch_ws_endpoint", self.endpoint)
            self.state.set_meta("launch_ws_endpoint_since", repr(time.time()))
        attempt = fallback_fails = 0
        while True:
            name = self.endpoint
            try:
                async with connect(self.urls[name], ping_interval=20, ping_timeout=20, open_timeout=20,
                                   max_size=2**23) as ws:
                    request = json.dumps(SUBSCRIBE)
                    await ws.send(request)
                    raw_reply = await asyncio.wait_for(ws.recv(), 20)
                    self.state.bandwidth_add(time.time(), f"{name}_ws", len(request) + len(raw_reply))
                    reply = json.loads(raw_reply)
                    if not isinstance(reply, dict) or "error" in reply or "result" not in reply:
                        raise SubscriptionError(reply.get("error") if isinstance(reply, dict) else reply)
                    now = time.time()
                    gap = (now - self._dropped_at) if self._dropped_at else None
                    self.state.event("rpcws_connect", {"gap_s": round(gap, 1) if gap else None, "endpoint": name})
                    if self.state.get_meta("publicrpc_since") is None:
                        self.state.set_meta("publicrpc_since", repr(now))
                    log.info("launch log stream connected via %s (gap %s s)", name, f"{gap:.1f}" if gap else "-")
                    self.connected = True
                    self._connected_at = now
                    while True:
                        raw = await asyncio.wait_for(ws.recv(), STALE_AFTER_S)
                        observed_at = time.time()
                        self.state.bandwidth_add(observed_at, f"{name}_ws", len(raw))
                        if name != SOURCE:
                            if observed_at - self._endpoint_since >= RETRY_PUBLIC_AFTER_S:
                                raise PlannedSwitch("retry_public")
                            if not self.fallback_allowed(observed_at):
                                raise PlannedSwitch("fluxrpc_bandwidth")
                        if observed_at - max(self.last_launch_at, self._connected_at) > LAUNCH_STALL_S:
                            raise LaunchStall
                        try:
                            msg = json.loads(raw)
                        except ValueError:
                            continue
                        if not isinstance(msg, dict):
                            continue
                        self.last_message_at = observed_at
                        if msg.get("method") == "logsNotification":
                            result = (msg.get("params") or {}).get("result") or {}
                            self.handle(result.get("value") or {}, (result.get("context") or {}).get("slot"),
                                        observed_at)
                            attempt = fallback_fails = 0
                        elif "error" in msg:
                            raise SubscriptionError(msg["error"])
            except (asyncio.CancelledError, DiskFullError):
                raise
            except PlannedSwitch as switch:
                self._mark_drop(str(switch))
                self._switch(SOURCE, str(switch))
                attempt = 0
                continue  # reconnect at once
            except Exception as exc:
                now = time.time()
                limited = is_rate_limited(exc)
                reason = ("rate_limited" if limited else "stale" if isinstance(exc, TimeoutError)
                          else "no_launches" if isinstance(exc, LaunchStall) else type(exc).__name__)
                if limited:
                    self.state.event("rpcws_rate_limited", {"error": type(exc).__name__, "endpoint": name})
                self._mark_drop(reason)
                if name == SOURCE and limited and self.fallback_allowed(now):
                    self._switch(FALLBACK, "rate_limited")
                    attempt = fallback_fails = 0
                    continue  # move to the fallback at once
                if name != SOURCE:
                    fallback_fails += 1
                    if limited or fallback_fails >= FALLBACK_MAX_FAILS or not self.fallback_allowed(now):
                        self._switch(SOURCE, "fallback_rate_limited" if limited else "fallback_failing")
                        fallback_fails = 0
                delays = RATE_LIMIT_BACKOFF_S if limited else BACKOFF_S
                delay = delays[min(attempt, len(delays) - 1)]
                attempt += 1
                log.warning("launch log stream via %s dropped (%s); reconnecting in %s s", name, reason, delay)
                await asyncio.sleep(delay)
