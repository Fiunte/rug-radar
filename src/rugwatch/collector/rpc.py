"""JSON-RPC client: rate limit, provider fallback, raw record for every attempt.

Order: Helius (primary) -> Alchemy (overflow on error) -> emergency tier. API keys live only in
the provider URL held in memory; they are never written to records, logs or exceptions.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from rugwatch.collector.budget import Budget
from rugwatch.storage.raw import RawWriter, make_record

log = logging.getLogger("rugwatch.rpc")
# httpx logs full request URLs at INFO; provider URLs contain API keys.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

CALL_TIMEOUT_S = 10.0
BREAKER_FAILS = 5
BREAKER_COOLOFF_S = 30.0
RETRIES_429 = 2  # rate-limited attempts are retried after 1 s, then 2 s (Helius guidance)
RETRY_WAIT_S = 1.0
AUTH_COOLOFF_S = 3600.0  # 401/403: key missing or network not enabled; retry hourly
PUBLIC_RPC_URL = "https://api.mainnet-beta.solana.com"


@dataclass
class Provider:
    name: str
    url: str = field(repr=False)  # contains the API key: never log or record it
    max_rps: float = 8.0
    _next_slot: float = field(default=0.0, repr=False)
    _fails: int = field(default=0, repr=False)
    _skip_until: float = field(default=0.0, repr=False)

    async def wait_turn(self) -> None:
        now = time.monotonic()
        slot = max(now, self._next_slot)
        self._next_slot = slot + 1.0 / self.max_rps
        if slot > now:
            await asyncio.sleep(slot - now)

    def available(self) -> bool:
        return time.monotonic() >= self._skip_until

    def report(self, ok: bool, auth_error: bool = False) -> None:
        if ok:
            self._fails = 0
            return
        if auth_error:
            self._skip_until = time.monotonic() + AUTH_COOLOFF_S
            return
        self._fails += 1
        if self._fails >= BREAKER_FAILS:
            self._skip_until = time.monotonic() + BREAKER_COOLOFF_S
            self._fails = 0


def providers_from_env(max_rps: float) -> list[Provider]:
    """Build the provider chain from environment variables. Missing keys drop that tier."""
    out: list[Provider] = []
    if key := os.environ.get("HELIUS_API_KEY"):
        out.append(Provider("helius", f"https://mainnet.helius-rpc.com/?api-key={key}", max_rps))
    if key := os.environ.get("ALCHEMY_API_KEY"):
        out.append(Provider("alchemy", f"https://solana-mainnet.g.alchemy.com/v2/{key}", max_rps))
    if key := os.environ.get("FLUXRPC_API_KEY"):
        out.append(Provider("fluxrpc", f"https://eu.fluxrpc.com?key={key}", max_rps))
    else:
        out.append(Provider("public", PUBLIC_RPC_URL, 2.0))
    return out


@dataclass
class RpcResult:
    ok: bool
    result: Any
    observed_at: float
    source: str | None
    slot: int | None


def params_hash(method: str, params: list[Any]) -> str:
    return hashlib.sha256(json.dumps([method, params], sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class RpcClient:
    def __init__(self, providers: list[Provider], writer: RawWriter, budget: Budget,
                 client: httpx.AsyncClient | None = None) -> None:
        self.providers = providers
        self.writer = writer
        self.budget = budget
        self.http = client or httpx.AsyncClient(timeout=CALL_TIMEOUT_S,
                                                limits=httpx.Limits(max_connections=32))
        self._id = 0

    async def aclose(self) -> None:
        await self.http.aclose()

    async def call(self, method: str, params: list[Any], *, kind: str, purpose: str,
                   mint: str | None = None, tick_s: int | None = None,
                   request_extra: dict[str, Any] | None = None, only: str | None = None) -> RpcResult:
        """One logical call. Each provider is tried in order; a rate-limited (429) attempt is retried on
        the same provider after a short wait. Every attempt is written to raw. `only` restricts the
        call to one provider (for provider-specific methods)."""
        request: dict[str, Any] = {"method": method, "params_hash": params_hash(method, params),
                                   **(request_extra or {})}
        observed = time.time()
        candidates = [p for p in self.providers if p.available()] or self.providers[:1]
        if only is not None:
            candidates = [p for p in self.providers if p.name == only]
        for provider in candidates:
            for attempt in range(1 + RETRIES_429):
                status, payload, result, observed = await self._attempt(
                    provider, method, params, kind, purpose, mint, tick_s, request)
                if status == "ok":
                    return RpcResult(True, result, observed, provider.name, _slot_of(result))
                log.warning("rpc %s via %s failed: %s", method, provider.name, payload)
                if payload.get("error") != "http_429" or attempt == RETRIES_429:
                    break
                await asyncio.sleep(RETRY_WAIT_S * 2**attempt)
        return RpcResult(False, None, observed, None, None)

    async def _attempt(self, provider: Provider, method: str, params: list[Any], kind: str, purpose: str,
                       mint: str | None, tick_s: int | None,
                       request: dict[str, Any]) -> tuple[str, dict[str, Any], Any, float]:
        await provider.wait_turn()
        self._id += 1
        body = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}
        started = time.monotonic()
        status = "error"
        payload: dict[str, Any] = {"error": "unknown"}
        result: Any = None
        try:
            resp = await self.http.post(provider.url, json=body)
            if resp.status_code != 200:
                payload = {"error": f"http_{resp.status_code}"}
            else:
                data = resp.json()
                if isinstance(data, dict) and "result" in data and "error" not in data:
                    status, result = "ok", data["result"]
                else:
                    err = data.get("error") if isinstance(data, dict) else None
                    code = err.get("code") if isinstance(err, dict) else None
                    msg = str(err.get("message"))[:200] if isinstance(err, dict) else "bad_response"
                    payload = {"error": "rpc_error", "code": code, "message": msg}
        except (httpx.TimeoutException, TimeoutError):
            status, payload = "timeout", {"error": "timeout"}
        except asyncio.CancelledError:
            # Deadline hit (e.g. snapshot batch): record the attempt as a timeout, then propagate.
            self._record(provider.name, kind, purpose, "timeout", {"error": "cancelled_at_deadline"},
                         time.time(), started, mint, tick_s, None, request)
            raise
        except Exception as exc:  # network errors; the message may contain the URL, keep the type only
            payload = {"error": type(exc).__name__}
        observed = time.time()
        self._record(provider.name, kind, purpose, status, result if status == "ok" else payload, observed,
                     started, mint, tick_s, _slot_of(result), request)
        provider.report(status == "ok", payload.get("error") in ("http_401", "http_403"))
        return status, payload, result, observed

    def _record(self, source: str, kind: str, purpose: str, status: str, payload: Any, observed_at: float,
                started: float, mint: str | None, tick_s: int | None, slot: int | None,
                request: dict[str, Any]) -> None:
        latency_ms = int((time.monotonic() - started) * 1000)
        self.writer.write(make_record(observed_at=observed_at, source=source, kind=kind, purpose=purpose,
                                      status=status, payload=payload, mint=mint, tick_s=tick_s, slot=slot,
                                      latency_ms=latency_ms, request=request))
        self.budget.record(source, purpose, method=request.get("method"), error=status != "ok", now=observed_at)

    # ---------------------------------------------------------- shortcuts
    async def get_multiple_accounts(self, accounts: list[str], *, purpose: str, commitment: str,
                                    items: list[dict[str, Any]] | None = None, mint: str | None = None,
                                    tick_s: int | None = None, part: str | None = None,
                                    data_slice: tuple[int, int] | None = None) -> RpcResult:
        cfg: dict[str, Any] = {"encoding": "base64", "commitment": commitment}
        if data_slice is not None:
            cfg["dataSlice"] = {"offset": data_slice[0], "length": data_slice[1]}
        extra: dict[str, Any] = {"accounts": accounts}
        if items is not None:
            extra["items"] = items
        if part is not None:
            extra["part"] = part
        return await self.call("getMultipleAccounts", [accounts, cfg], kind="accounts", purpose=purpose,
                               mint=mint, tick_s=tick_s, request_extra=extra)

    async def get_signatures(self, address: str, *, purpose: str, commitment: str, mint: str | None,
                             before: str | None = None, limit: int = 1000,
                             part: str | None = None) -> RpcResult:
        cfg: dict[str, Any] = {"limit": limit, "commitment": commitment}
        if before:
            cfg["before"] = before
        return await self.call("getSignaturesForAddress", [address, cfg], kind="signatures", purpose=purpose,
                               mint=mint, request_extra={"address": address, "before": before, "part": part})

    async def get_transaction(self, sig: str, *, purpose: str, commitment: str, mint: str | None,
                              part: str | None = None) -> RpcResult:
        cfg = {"encoding": "jsonParsed", "commitment": commitment, "maxSupportedTransactionVersion": 1}
        return await self.call("getTransaction", [sig, cfg], kind="transaction", purpose=purpose, mint=mint,
                               request_extra={"signature": sig, "part": part})

    async def get_oldest_transactions(self, address: str, *, limit: int, purpose: str, commitment: str,
                                      mint: str | None, part: str | None = None) -> RpcResult:
        """Helius only: the oldest successful transactions of an address, oldest first, in one call."""
        cfg = {"transactionDetails": "full", "sortOrder": "asc", "limit": limit, "commitment": commitment,
               "encoding": "jsonParsed", "maxSupportedTransactionVersion": 1, "filters": {"status": "succeeded"}}
        return await self.call("getTransactionsForAddress", [address, cfg], kind="transaction", purpose=purpose,
                               mint=mint, request_extra={"address": address, "part": part}, only="helius")

    async def get_largest_accounts(self, mint: str, *, commitment: str, tick_s: int,
                                   part: str | None = None) -> RpcResult:
        return await self.call("getTokenLargestAccounts", [mint, {"commitment": commitment}],
                               kind="largest_accounts", purpose="holders", mint=mint, tick_s=tick_s,
                               request_extra={"part": part})


def _slot_of(result: Any) -> int | None:
    if isinstance(result, dict):
        ctx = result.get("context")
        if isinstance(ctx, dict) and isinstance(ctx.get("slot"), int):
            return int(ctx["slot"])
        if isinstance(result.get("slot"), int):
            return int(result["slot"])
    return None
