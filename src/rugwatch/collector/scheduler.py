"""Polling scheduler: filter ticks for every launch, the tracking decision, then price, balance
and holder polls for tracked tokens. Everything is batched on one time grid (the shortest price
cadence) so that calls are shared across tokens.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import re
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

from rugwatch.chain import pump_layout as pl
from rugwatch.collector.budget import Budget
from rugwatch.collector.rpc import RpcClient, RpcResult
from rugwatch.collector.snapshot import TOKEN_ACCOUNT_SLICE, SnapshotCtx, Snapshotter
from rugwatch.collector.state import State
from rugwatch.config import Config
from rugwatch.storage.raw import DiskFullError, RawWriter, make_record

log = logging.getLogger("rugwatch.scheduler")

LATE_TOLERANCE_S = 15.0  # a tick later than grid + this is recorded as missed, not polled
MIN_RATE_WINDOW_S = 600.0
RATE_CACHE_S = 60.0  # the 24 h counts move slowly; recount at most once a minute


def hash_unit(mint: str) -> float:
    """sha256(mint)[:8] / 2^32, in [0, 1) (config tracking.sampling)."""
    return int(hashlib.sha256(mint.encode()).hexdigest()[:8], 16) / 2**32


def cadence_every(cadence: list[tuple[int, int]], elapsed: float) -> int | None:
    """Poll interval at `elapsed` seconds after the snapshot; None once the horizon is over."""
    for until, every in cadence:
        if elapsed < until:
            return every
    return None


def is_dead(price: float | None, peak: float | None, since_change_s: float, frac: float, quiet_s: float) -> bool:
    """config tracking.dead_rule: price under frac x peak, or no reserve change for quiet_s."""
    if since_change_s >= quiet_s:
        return True
    return price is not None and peak is not None and peak > 0 and price < frac * peak


def _trailing_int(step: str) -> int:
    m = re.search(r"(\d+)(?:pct|s)?$", step)
    if not m:
        raise ValueError(f"no number in throttle step {step!r}")
    return int(m.group(1))


@dataclass
class Caps:
    filter_pass: float
    random_rest: float
    benchmark: float


class Scheduler:
    def __init__(self, cfg: Config, state: State, rpc: RpcClient, writer: RawWriter, budget: Budget,
                 snapshotter: Snapshotter) -> None:
        self.cfg = cfg
        self.flt = cfg.activity_filter
        self.tr = cfg.tracking
        self.commitment = cfg.collector.commitment
        self.batch = cfg.collector.rpc.batch_accounts
        self.state = state
        self.rpc = rpc
        self.writer = writer
        self.budget = budget
        self.snapshotter = snapshotter
        self.grid = float(min(every for _until, every in self.tr.price_cadence))
        self.ticks = sorted(self.flt.tick_s)
        self.slow_every = max(every for _until, every in self.tr.price_cadence)
        # never_throttle "price_ticks_first_3600s": the dead rule may not slow price ticks before that.
        self.protect_s = max((float(_trailing_int(x)) for x in cfg.collector.budget.never_throttle
                              if x.startswith("price_ticks_first_")), default=0.0)
        self._tasks: set[asyncio.Task[None]] = set()
        self._rate_cache: dict[str, tuple[float, float]] = {}
        self.fatal: BaseException | None = None

    # ----------------------------------------------------------------- caps
    def caps(self) -> Caps:
        fp, rr = float(self.tr.filter_pass_cap_per_day), float(self.tr.random_rest_per_day)
        total = fp + rr
        if total > self.tr.tracked_cap_per_day:  # keep the filter-pass : random-rest ratio
            fp, rr = (x * self.tr.tracked_cap_per_day / total for x in (fp, rr))
        for step in self.cfg.collector.budget.throttle_order:
            if not self.budget.active(step):
                continue
            if step.startswith("random_rest_per_day"):
                rr = min(rr, float(_trailing_int(step)))
            elif step.startswith("tracked_cap_minus"):
                fp, rr = (x * (1 - _trailing_int(step) / 100) for x in (fp, rr))
        return Caps(fp, rr, float(self.cfg.scanners.benchmark_tokens_per_day))

    def balance_multiplier(self, elapsed: float) -> int:
        for step in self.cfg.collector.budget.throttle_order:
            if step.startswith("balance_cadence_x") and self.budget.active(step):
                m = re.match(r"balance_cadence_x(\d+)_after_(\d+)s", step)
                if m and elapsed >= int(m.group(2)):
                    return int(m.group(1))
        return 1

    def _rate_24h(self, column: str, now: float) -> float:
        """Estimated count per 24 h, extrapolated while the collector has less than a day of data."""
        cached = self._rate_cache.get(column)
        if cached is None or not 0 <= now - cached[0] < RATE_CACHE_S:
            # Counts restart with the data clock (D-32): before it, only ~22% of launches were seen, so
            # mixing in that period would overstate every sampling probability for a day.
            clock = self.state.data_clock_start() or 0.0
            count = self.state.count_decided(max(now - 86400, clock), column,
                                             self.flt.threshold_sol) + 1  # + the token being decided
            first = self.state.first_decision_ts()
            start = None if first is None else max(first, clock)
            elapsed = 86400.0 if start is None else min(86400.0, max(now - start, MIN_RATE_WINDOW_S))
            cached = (now, count * 86400.0 / elapsed)
            self._rate_cache[column] = cached
        return cached[1]

    # ------------------------------------------------------------- decision
    def decide(self, mint: str, bc: pl.BondingCurve | None, status: str, now: float) -> dict[str, Any]:
        """Tracking decision at the decide_at_s tick. Pure given the state counts."""
        flags: list[str] = []
        filter_pass = eligible_rest = tracked = benchmark = 0
        reason, prob, real_sol = "not_tracked", None, None
        u = hash_unit(mint)
        if status != "ok" or bc is None:
            flags.append(f"tick{self.flt.decide_at_s}_{status}")
        elif not bc.is_sol_quoted:
            flags.append("non_sol_quote")
        elif bc.complete:
            real_sol = bc.real_sol
            flags.append("graduated_before_decision")
        else:
            real_sol = bc.real_sol
            caps = self.caps()
            if real_sol >= self.flt.threshold_sol:
                filter_pass = 1
                n = self._rate_24h("filter_pass", now)
                prob = min(1.0, caps.filter_pass / n)
                if self.budget.hard_stop:
                    flags.append("hard_stop")
                elif u < prob:
                    tracked, reason = 1, "filter_pass"
                    benchmark = int(self.cfg.scanners.enabled and u < min(1.0, caps.benchmark / n))
                else:
                    flags.append("cap_sampled_out")
            else:
                eligible_rest = 1
                prob = min(1.0, caps.random_rest / self._rate_24h("eligible_rest", now))
                if self.budget.hard_stop:
                    flags.append("hard_stop")
                elif u < prob:
                    tracked, reason = 1, "random_rest"
        return {"mint": mint, "decided_at": now, "filter_pass": filter_pass, "eligible_rest": eligible_rest,
                "tracked": tracked, "reason": reason, "flags": ",".join(flags), "filter_real_sol_300": real_sol,
                "sample_prob": prob, "benchmark": benchmark}

    def _record_decision(self, row: sqlite3.Row, decision: dict[str, Any], bc: pl.BondingCurve | None) -> None:
        self.state.put_tracking({**decision, "launch_obs": row["launch_obs"], "bonding_curve": row["bonding_curve"]})
        self.writer.write(make_record(
            observed_at=decision["decided_at"], source="collector", kind="tracking", purpose="filter",
            status="ok", mint=decision["mint"], tick_s=self.flt.decide_at_s,
            payload={**decision, "hash_unit": hash_unit(decision["mint"]), "threshold_sol": self.flt.threshold_sol,
                     "universe_version": self.cfg.universe_version, "throttle_level": self.budget.level}))
        if decision["tracked"] and bc is not None:
            creators = {c for c in (bc.creator, row["launcher"]) if c}
            sig = self.state.one("SELECT sig FROM tokens WHERE mint=?", (decision["mint"],))
            ctx = SnapshotCtx(mint=decision["mint"], launch_obs=row["launch_obs"], curve=row["bonding_curve"],
                              creators=creators, creator=bc.creator or row["launcher"], creation_sig=sig,
                              benchmark=bool(decision["benchmark"]))
            self._spawn(self.snapshotter.run(ctx))

    def _spawn(self, coro: Any) -> None:
        task: asyncio.Task[None] = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if isinstance(exc, DiskFullError):
            self.fatal = exc
        elif exc is not None:
            log.error("background task failed: %r", exc)
            self.state.event("task_error", {"error": type(exc).__name__})

    # ----------------------------------------------------------------- loop
    async def run(self) -> None:
        while True:
            now = time.time()
            await asyncio.sleep((math.floor(now / self.grid) + 1) * self.grid - now)
            await self.step(time.time())
            if self.fatal is not None:
                raise self.fatal

    async def step(self, now: float) -> None:
        results = await asyncio.gather(self._filter_step(now), self._price_step(now), self._balance_step(now),
                                       self._holders_step(now), return_exceptions=True)
        for res in results:
            if isinstance(res, DiskFullError):
                raise res
            if isinstance(res, asyncio.CancelledError):
                raise res
            if isinstance(res, BaseException):
                log.error("scheduler step failed: %r", res)
                self.state.event("step_error", {"error": type(res).__name__})

    def _chunks(self, items: list[Any]) -> list[list[Any]]:
        return [items[i:i + self.batch] for i in range(0, len(items), self.batch)]

    # --------------------------------------------------------------- filter
    def _advance(self, row: sqlite3.Row) -> None:
        idx = row["next_tick_idx"] + 1
        due = row["launch_obs"] + self.ticks[idx] if idx < len(self.ticks) else None
        self.state.advance_tick(row["mint"], idx, due)

    async def _filter_step(self, now: float) -> None:
        to_poll: list[sqlite3.Row] = []
        for row in self.state.due_filter_ticks(now):
            tick_s = self.ticks[row["next_tick_idx"]]
            if now - row["next_tick_due"] > self.grid + LATE_TOLERANCE_S or not row["bonding_curve"]:
                self.state.put_filter_tick(row["mint"], tick_s, "missed")
                self._advance(row)
                if tick_s == self.flt.decide_at_s:
                    self._record_decision(row, self.decide(row["mint"], None, "missed", now), None)
            else:
                to_poll.append(row)
        await asyncio.gather(*(self._filter_batch(chunk) for chunk in self._chunks(to_poll)))

    async def _filter_batch(self, rows: list[sqlite3.Row]) -> None:
        items = [{"mint": r["mint"], "tick_s": self.ticks[r["next_tick_idx"]]} for r in rows]
        res = await self.rpc.get_multiple_accounts([r["bonding_curve"] for r in rows], purpose="filter",
                                                   commitment=self.commitment, items=items)
        values = _values(res, len(rows))
        for row, item, value in zip(rows, items, values, strict=True):
            tick_s, bc, status = item["tick_s"], None, "error"
            if res.ok:
                data = pl.decode_account_b64(value)
                bc = pl.decode_bonding_curve(data) if data else None
                status = "ok" if bc else ("missing_account" if data is None else "not_curve")
            if bc is not None:
                self.state.put_filter_tick(row["mint"], tick_s, status, res.observed_at, res.slot,
                                           bc.virtual_quote, bc.virtual_token, bc.real_quote, bc.real_token,
                                           bc.complete)
                self.state.x("UPDATE tokens SET creator=?, quote_mint=? WHERE mint=? AND creator IS NULL",
                             (bc.creator, bc.quote_mint, row["mint"]))
            else:
                self.state.put_filter_tick(row["mint"], tick_s, status, res.observed_at, res.slot)
            self._advance(row)
            if tick_s == self.flt.decide_at_s:
                self._record_decision(row, self.decide(row["mint"], bc, status, res.observed_at), bc)

    # ---------------------------------------------------------------- price
    async def _price_step(self, now: float) -> None:
        rows = self.state.q("SELECT * FROM tracking WHERE next_price_due IS NOT NULL AND next_price_due<=?", (now,))
        await asyncio.gather(*(self._price_batch(chunk, now) for chunk in self._chunks(rows)))

    async def _price_batch(self, rows: list[sqlite3.Row], now: float) -> None:
        items = [{"mint": r["mint"], "tick_s": round(r["next_price_due"] - r["snapshot_ts"])} for r in rows]
        res = await self.rpc.get_multiple_accounts([r["bonding_curve"] for r in rows], purpose="price",
                                                   commitment=self.commitment, items=items)
        for row, value in zip(rows, _values(res, len(rows)), strict=True):
            data = pl.decode_account_b64(value) if res.ok else None
            bc = pl.decode_bonding_curve(data) if data else None
            self.state.update_tracking(row["mint"], **self.price_update(row, bc, res.observed_at, now))

    def price_update(self, row: sqlite3.Row, bc: pl.BondingCurve | None, observed_at: float,
                     now: float) -> dict[str, Any]:
        """New tracking fields after one price tick (bc is None when the read failed)."""
        dr = self.tr.dead_rule
        f: dict[str, Any] = {}
        peak, last_change, graduated = row["peak_price"], row["last_change_obs"], row["graduated_ts"]
        price = row["last_price"]
        if bc is not None:
            price = bc.price
            if (bc.virtual_quote, bc.virtual_token) != (row["last_vq"], row["last_vt"]):
                last_change = observed_at
            if price is not None and (peak is None or price > peak):
                peak = price
            if bc.complete and graduated is None:
                graduated = observed_at
            f.update(last_price=price, last_vq=bc.virtual_quote, last_vt=bc.virtual_token,
                     last_change_obs=last_change, peak_price=peak, graduated_ts=graduated)
        dead = is_dead(price, peak, observed_at - (last_change or observed_at), dr.price_below_frac_of_peak,
                       dr.no_reserve_change_s) or graduated is not None
        f["dead"] = int(dead)

        elapsed = row["next_price_due"] - row["snapshot_ts"]
        every = cadence_every(self.tr.price_cadence, elapsed)
        if every is not None and dead and elapsed >= self.protect_s:
            every = self.slow_every
        if every is None:
            f.update(price_nominal=None, next_price_due=None)
        else:
            nxt = row["next_price_due"] + every
            if nxt < now - every:  # fell behind (downtime): skip the ticks that can no longer be taken
                nxt = now
            f.update(price_nominal=nxt, next_price_due=nxt)

        # Balance polling stops while the token is dead and resumes if it comes back.
        bal_elapsed = now - row["snapshot_ts"]
        bal_every = cadence_every(self.tr.balance_cadence, bal_elapsed)
        if dead or bal_every is None:
            f.update(balance_nominal=None, next_balance_due=None)
        elif row["next_balance_due"] is None and self._has_accounts(row["mint"]):
            f.update(balance_nominal=now, next_balance_due=now + bal_every * self.balance_multiplier(bal_elapsed))
        return f

    def _has_accounts(self, mint: str) -> bool:
        return self.state.one("SELECT 1 FROM balance_accounts WHERE mint=? LIMIT 1", (mint,)) is not None

    # -------------------------------------------------------------- balance
    async def _balance_step(self, now: float) -> None:
        if self.budget.hard_stop:
            return
        rows = self.state.q("SELECT mint, snapshot_ts, next_balance_due FROM tracking "
                            "WHERE next_balance_due IS NOT NULL AND next_balance_due<=?", (now,))
        items: list[dict[str, Any]] = []
        for row in rows:
            elapsed = row["next_balance_due"] - row["snapshot_ts"]
            tick = round(elapsed)
            for acc in self.state.q("SELECT token_account FROM balance_accounts WHERE mint=?", (row["mint"],)):
                items.append({"mint": row["mint"], "token_account": acc["token_account"], "tick_s": tick})
            every = cadence_every(self.tr.balance_cadence, elapsed)
            if every is None:
                self.state.update_tracking(row["mint"], balance_nominal=None, next_balance_due=None)
            else:
                nxt = row["next_balance_due"] + every * self.balance_multiplier(elapsed)
                if nxt < now:
                    nxt = now + every
                self.state.update_tracking(row["mint"], balance_nominal=nxt, next_balance_due=nxt)
        await asyncio.gather(*(
            self.rpc.get_multiple_accounts([i["token_account"] for i in chunk], purpose="balance",
                                           commitment=self.commitment, items=chunk, data_slice=TOKEN_ACCOUNT_SLICE)
            for chunk in self._chunks(items)))

    # -------------------------------------------------------------- holders
    async def _holders_step(self, now: float) -> None:
        if self.budget.hard_stop:
            return
        rows = self.state.q("SELECT mint, launch_obs, holders_due FROM tracking "
                            "WHERE holders_due IS NOT NULL AND holders_due<=?", (now,))
        for row in rows:
            tick_s = round(row["holders_due"] - row["launch_obs"])
            later = [s for s in self.tr.largest_holders_at_s if s > tick_s]
            self.state.update_tracking(row["mint"], holders_due=row["launch_obs"] + later[0] if later else None)
        await asyncio.gather(*(
            self.rpc.get_largest_accounts(row["mint"], commitment=self.commitment,
                                          tick_s=round(row["holders_due"] - row["launch_obs"]))
            for row in rows))

    # -------------------------------------------------------------- startup
    def recover(self, now: float) -> None:
        """After a restart: snapshots that were in flight can never complete; flag them."""
        rows = self.state.q("SELECT mint FROM tracking WHERE tracked=1 AND snapshot_complete IS NULL")
        for row in rows:
            self.state.update_tracking(row["mint"], snapshot_complete=0, missing_parts="interrupted",
                                       flags="snapshot_interrupted")
            self.writer.write(make_record(
                observed_at=now, source="collector", kind="snapshot_summary", purpose="snapshot", status="error",
                mint=row["mint"], payload={"mint": row["mint"], "snapshot_complete": False,
                                           "missing_parts": ["interrupted"], "snapshot_ts": None, "p0": None}))

    async def drain(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)


def _values(res: RpcResult, n: int) -> list[Any]:
    if res.ok and isinstance(res.result, dict):
        value = res.result.get("value")
        if isinstance(value, list) and len(value) == n:
            return value
    return [None] * n
