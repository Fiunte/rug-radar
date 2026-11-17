"""Third-party scanner clients. One query per (mint, scanner), ever (config scanners.one_shot).

`score_norm` is defined here per scanner, higher = riskier, and frozen in M1 before any outcome
is viewed (HANDOFF 6 item 7; docs/decisions.md D-15).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx


@dataclass
class ScannerScore:
    score_raw: float | None
    score_norm: float | None
    flags: list[str] = field(default_factory=list)


class Scanner(Protocol):
    name: str

    def request(self, mint: str) -> tuple[str, dict[str, str]]: ...

    def normalise(self, payload: Any) -> ScannerScore: ...


@dataclass
class ScannerResponse:
    status: str  # ok | error | timeout
    payload: Any
    observed_at: float
    latency_ms: int


async def query_scanner(scanner: Scanner, http: httpx.AsyncClient, mint: str, timeout_s: float) -> ScannerResponse:
    """One GET. Never raises (except cancellation); the message of an exception is not kept."""
    url, headers = scanner.request(mint)
    started = time.monotonic()
    status: str
    payload: Any
    try:
        resp = await http.get(url, headers=headers, timeout=timeout_s)
        if resp.status_code == 200:
            status, payload = "ok", resp.json()
        else:
            status, payload = "error", {"error": f"http_{resp.status_code}"}
    except httpx.TimeoutException:
        status, payload = "timeout", {"error": "timeout"}
    except ValueError:
        status, payload = "error", {"error": "bad_json"}
    except httpx.HTTPError as exc:
        status, payload = "error", {"error": type(exc).__name__}
    return ScannerResponse(status, payload, time.time(), int((time.monotonic() - started) * 1000))


def build_scanners(names: list[str]) -> list[Scanner]:
    from rugwatch.collector.scanners.goplus import GoPlus
    from rugwatch.collector.scanners.rugcheck import RugCheck
    from rugwatch.collector.scanners.solanatracker import SolanaTracker

    table: dict[str, type[Any]] = {"rugcheck": RugCheck, "goplus": GoPlus, "solanatracker": SolanaTracker}
    return [table[name]() for name in names]
