"""PumpPortal listener: free streams only (new token, migration), one websocket connection."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from websockets.asyncio.client import connect

from rugwatch.collector.state import State
from rugwatch.config import ActivityFilterCfg, PumpPortalCfg
from rugwatch.storage.raw import DiskFullError, RawWriter, make_record

log = logging.getLogger("rugwatch.pumpportal")

STALE_AFTER_S = 60.0  # no message for this long = dead connection (launches arrive ~every 3 s)
ALLOWED_STREAMS = frozenset({"subscribeNewToken", "subscribeMigration"})
PUMP_POOL = "pump"


def classify(msg: dict[str, Any]) -> str:
    """'create' (pump.fun launch), 'create_other' (another launchpad), 'migrate', or 'other'."""
    tx_type = msg.get("txType")
    if tx_type == "create":
        return "create" if msg.get("pool") == PUMP_POOL and msg.get("bondingCurveKey") else "create_other"
    if tx_type == "migrate":
        return "migrate"
    return "other"


class PumpPortalListener:
    def __init__(self, cfg: PumpPortalCfg, flt: ActivityFilterCfg, state: State, writer: RawWriter) -> None:
        bad = set(cfg.streams) - ALLOWED_STREAMS
        if bad:
            raise ValueError(f"metered PumpPortal streams are forbidden: {sorted(bad)}")
        self.cfg = cfg
        self.first_tick_s = min(flt.tick_s)
        self.state = state
        self.writer = writer
        self.connected = False
        self.last_message_at: float | None = None
        self._dropped_at: float | None = None

    def handle(self, msg: dict[str, Any], observed_at: float) -> str:
        kind = classify(msg)
        if kind == "other":
            return kind
        mint = msg.get("mint")
        self.writer.write(make_record(
            observed_at=observed_at, source="pumpportal",
            kind="migration" if kind == "migrate" else "new_token", purpose="launch_log",
            status="ok", payload=msg, mint=mint if isinstance(mint, str) else None))
        if kind == "create" and isinstance(mint, str):
            self.state.add_token(mint, observed_at, msg, observed_at + self.first_tick_s)
        elif kind == "migrate" and isinstance(mint, str):
            self.state.x("UPDATE tracking SET graduated_ts=COALESCE(graduated_ts,?) WHERE mint=?",
                         (observed_at, mint))
        return kind

    async def run(self) -> None:
        attempt = 0
        backoff = self.cfg.reconnect_backoff_s
        while True:
            try:
                async with connect(self.cfg.url, ping_interval=20, ping_timeout=20, max_size=2**20) as ws:
                    for stream in self.cfg.streams:
                        await ws.send(json.dumps({"method": stream}))
                    now = time.time()
                    gap = (now - self._dropped_at) if self._dropped_at else None
                    self.state.event("ws_connect", {"gap_s": round(gap, 1) if gap else None})
                    log.info("pumpportal connected (gap %s s)", f"{gap:.1f}" if gap else "-")
                    self.connected = True
                    while True:
                        raw = await asyncio.wait_for(ws.recv(), STALE_AFTER_S)
                        observed_at = time.time()
                        try:
                            msg = json.loads(raw)
                        except ValueError:
                            continue
                        if isinstance(msg, dict):
                            self.last_message_at = observed_at
                            if self.handle(msg, observed_at) != "other":
                                attempt = 0
            except (asyncio.CancelledError, DiskFullError):
                raise
            except Exception as exc:
                reason = "stale" if isinstance(exc, TimeoutError) else type(exc).__name__
                if self.connected or self._dropped_at is None:
                    self._dropped_at = time.time()
                    self.state.event("ws_drop", {"reason": reason})
                self.connected = False
                delay = backoff[min(attempt, len(backoff) - 1)]
                attempt += 1
                log.warning("pumpportal dropped (%s); reconnecting in %s s", reason, delay)
                await asyncio.sleep(delay)
