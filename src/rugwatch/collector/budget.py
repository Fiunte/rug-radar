"""Credit ledger, monthly projection and throttling (config collector.budget).

Projection = Helius credits in the trailing 24 h x 30. Throttle steps from `throttle_order` are
applied one at a time (at most one change per REVIEW_INTERVAL_S) while the projection is above
`target_credits`, and released one at a time when it falls below RELEASE_FRAC of the target.
Hard stop = credits used in the current billing cycle >= `hard_stop_credits`.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

from rugwatch.collector.state import State
from rugwatch.config import BudgetCfg

# Provider facts, not study thresholds (docs/decisions.md D-07). Helius: 1 credit per standard call;
# getTransactionsForAddress costs 10 credits per 100 returned transactions (we never ask for more).
CREDITS_PER_CALL = {"helius": 1}
METHOD_CREDITS = {("helius", "getTransactionsForAddress"): 10}
REVIEW_INTERVAL_S = 6 * 3600.0
RELEASE_FRAC = 0.85
MIN_WINDOW_S = 3600.0
METERED_SOURCE = "helius"


def hour_key(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H")


def cycle_start(now: float, billing_day: int) -> float:
    """Start (epoch) of the billing cycle containing `now`; the cycle resets on `billing_day`."""
    d = datetime.fromtimestamp(now, UTC)
    day = min(max(billing_day, 1), 28)
    start = d.replace(day=day, hour=0, minute=0, second=0, microsecond=0)
    if start > d:
        prev_month_end = start.replace(day=1) - timedelta(days=1)
        start = prev_month_end.replace(day=day)
    return start.timestamp()


class Budget:
    def __init__(self, state: State, cfg: BudgetCfg, billing_day: int = 1) -> None:
        self.state = state
        self.cfg = cfg
        self.billing_day = billing_day
        self.level = int(state.get_meta("throttle_level", "0") or 0)
        self._last_review = float(state.get_meta("throttle_reviewed_at", "0") or 0)
        self._hard_stop = False
        self._hard_checked = 0.0

    # -------------------------------------------------------------- ledger
    def record(self, source: str, purpose: str, *, method: str | None = None, error: bool = False,
               now: float | None = None) -> None:
        ts = now if now is not None else time.time()
        credits = METHOD_CREDITS.get((source, method or ""), CREDITS_PER_CALL.get(source, 0))
        self.state.ledger_add(hour_key(ts), source, purpose, credits, error)

    def cycle_used(self, now: float) -> int:
        return self.state.credits_since(METERED_SOURCE, hour_key(cycle_start(now, self.billing_day)))

    def projection(self, now: float) -> float | None:
        """Projected credits per 30 days from the trailing 24 h; None with under an hour of data."""
        first = self.state.one("SELECT MIN(hour) FROM credit_ledger WHERE source=?", (METERED_SOURCE,))
        if first is None:
            return None
        first_ts = datetime.strptime(first, "%Y-%m-%dT%H").replace(tzinfo=UTC).timestamp()
        window = min(86400.0, now - first_ts)
        if window < MIN_WINDOW_S:
            return None
        used = self.state.credits_since(METERED_SOURCE, hour_key(now - window))
        return used / window * 86400.0 * 30.0

    # ------------------------------------------------------------ throttle
    def review(self, now: float) -> None:
        """Re-evaluate the throttle level; call it regularly."""
        if now - self._hard_checked >= 60.0:
            self._hard_checked = now
            hard = self.cycle_used(now) >= self.cfg.hard_stop_credits
            if hard != self._hard_stop:
                self._hard_stop = hard
                self.state.event("hard_stop_on" if hard else "hard_stop_off", {"used": self.cycle_used(now)})
        if now - self._last_review < REVIEW_INTERVAL_S:
            return
        proj = self.projection(now)
        if proj is None:
            return
        self._last_review = now
        self.state.set_meta("throttle_reviewed_at", str(now))
        new = self.level
        if proj > self.cfg.target_credits and self.level < len(self.cfg.throttle_order):
            new = self.level + 1
        elif proj < RELEASE_FRAC * self.cfg.target_credits and self.level > 0:
            new = self.level - 1
        if new != self.level:
            self.state.event("throttle_level", {"from": self.level, "to": new, "projection": round(proj)})
            self.level = new
            self.state.set_meta("throttle_level", str(new))

    @property
    def hard_stop(self) -> bool:
        return self._hard_stop

    def active(self, step: str) -> bool:
        return step in self.cfg.throttle_order[: self.level]
