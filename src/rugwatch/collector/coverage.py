"""Daily launch-coverage check (decision D-31, asked by the user): of the pump.fun creates on chain in
one sampled hour, what share reached the collector, overall and per source. Alert below 95%.

Chain side: every successful transaction that mentions the pump.fun mint authority, from
`getSignaturesForAddress` (about 2 pages for an hour, 1 credit each). Those transactions are creates,
plus a few others (e.g. PumpSwap pool creation at graduation). A signature we recorded as a launch is
a create by construction. Of the signatures we did not see, up to MAX_CLASSIFY are fetched and
classified (has a CreateEvent or not); the missed-create count is extrapolated from that sample.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import defaultdict
from typing import Any

from rugwatch.chain.tx_parse import parse_transaction
from rugwatch.collector.budget import Budget
from rugwatch.collector.launchlogs import MINT_AUTHORITY
from rugwatch.collector.rpc import RpcClient
from rugwatch.collector.state import State
from rugwatch.storage.raw import DiskFullError, RawWriter, make_record

log = logging.getLogger("rugwatch.coverage")

COVERAGE_ALERT = 0.95  # the user's alert level
WINDOW_S = 3600.0  # one sampled hour a day
LAG_S = 300.0  # the window ends this long ago, so every source has reported
CHECK_EVERY_S = 86400.0
RETRY_AFTER_S = 3600.0
POLL_S = 600.0
MAX_PAGES = 10
MAX_CLASSIFY = 60
SEED = 17


class CoverageError(RuntimeError):
    pass


async def check_coverage(rpc: RpcClient, state: State, window_start: float, window_end: float,
                         commitment: str = "confirmed") -> dict[str, Any]:
    chain: dict[str, int] = {}
    before: str | None = None
    pages, truncated = 0, False
    while True:
        res = await rpc.get_signatures(MINT_AUTHORITY, purpose="coverage", commitment=commitment, mint=None,
                                       before=before, limit=1000, part="coverage")
        if not res.ok or not isinstance(res.result, list):
            raise CoverageError("getSignaturesForAddress failed")
        page: list[dict[str, Any]] = res.result
        pages += 1
        for entry in page:
            bt = entry.get("blockTime")
            if bt is not None and window_start <= bt < window_end and entry.get("err") is None:
                chain[entry["signature"]] = int(bt)
        if not page or (page[-1].get("blockTime") or 0) < window_start:
            break
        if pages >= MAX_PAGES:
            truncated = True
            break
        before = page[-1]["signature"]

    sigs = sorted(chain)
    seen_by: dict[str, set[str]] = defaultdict(set)
    for i in range(0, len(sigs), 500):
        chunk = sigs[i:i + 500]
        marks = ",".join("?" * len(chunk))
        for row in state.q(f"SELECT source, sig FROM launch_sources WHERE sig IN ({marks})", tuple(chunk)):
            seen_by[row["source"]].add(row["sig"])
    seen = set().union(*seen_by.values()) if seen_by else set()
    unseen = [s for s in sigs if s not in seen]
    sample = unseen if len(unseen) <= MAX_CLASSIFY else random.Random(SEED).sample(unseen, MAX_CLASSIFY)
    creates = others = failed = 0
    for sig in sample:
        res = await rpc.get_transaction(sig, purpose="coverage", commitment=commitment, mint=None, part="coverage")
        if not res.ok or not isinstance(res.result, dict):
            failed += 1
            continue
        if parse_transaction(res.result, sig).creates:
            creates += 1
        else:
            others += 1
    classified = creates + others
    missed = len(unseen) * creates / classified if classified else float(len(unseen))  # unknown -> all missed
    total = len(seen) + missed
    return {
        "window_start": window_start, "window_end": window_end, "chain_signatures": len(chain),
        "seen": len(seen), "unseen": len(unseen), "classified": classified, "classified_creates": creates,
        "classify_failed": failed, "missed_creates_est": round(missed, 1),
        "chain_creates_est": round(total, 1), "pages": pages, "pages_truncated": truncated,
        "coverage": round(len(seen) / total, 4) if total else None,
        "by_source": {src: round(len(v) / total, 4) if total else None for src, v in sorted(seen_by.items())},
    }


async def run_check(rpc: RpcClient, state: State, writer: RawWriter, window_start: float, window_end: float,
                    commitment: str = "confirmed") -> dict[str, Any]:
    """Check one window, then log the result as an event (for `make status`) and a raw record."""
    result = await check_coverage(rpc, state, window_start, window_end, commitment)
    state.event("coverage_check", result)
    writer.write(make_record(observed_at=time.time(), source="collector", kind="coverage", purpose="coverage",
                             status="ok", payload=result))
    cov = result["coverage"]
    log.info("launch coverage %s (chain creates ~%s): %s", "n/a" if cov is None else f"{cov:.1%}",
             result["chain_creates_est"], result["by_source"])
    return result


def due_window(state: State, now: float) -> tuple[float, float] | None:
    """The hour to check now, or None: once a day, and only once the public-RPC source has been
    running for the whole window."""
    since = state.get_meta("publicrpc_since")
    next_at = state.get_meta("coverage_next_ts")
    window_end = now - LAG_S
    window_start = window_end - WINDOW_S
    if since is None or float(since) > window_start:
        return None
    if next_at is not None and now < float(next_at):
        return None
    return window_start, window_end


async def coverage_loop(rpc: RpcClient, state: State, writer: RawWriter, budget: Budget,
                        commitment: str = "confirmed") -> None:
    while True:
        await asyncio.sleep(POLL_S)
        now = time.time()
        window = due_window(state, now)
        if window is None or budget.hard_stop:
            continue
        try:
            await run_check(rpc, state, writer, *window, commitment=commitment)
            state.set_meta("coverage_next_ts", repr(now + CHECK_EVERY_S))
        except (asyncio.CancelledError, DiskFullError):
            raise
        except Exception as exc:
            state.event("coverage_error", {"error": type(exc).__name__})
            state.set_meta("coverage_next_ts", repr(now + RETRY_AFTER_S))
            log.warning("coverage check failed (%s); retrying in 1 h", type(exc).__name__)
