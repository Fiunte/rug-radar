"""Scheduler: sampling, cadences, dead rule, tracking decision, filter and price steps."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

import pytest

from rugwatch.chain import pump_layout as pl
from rugwatch.collector.scheduler import Scheduler, cadence_every, hash_unit, is_dead
from rugwatch.collector.snapshot import SnapshotCtx, Snapshotter
from tests.conftest import CREATOR, CURVE, Env, account_value, curve_bytes, make_cfg

SOL = 10**9
T0 = 1_794_873_600.0


class NoSnapshots(Snapshotter):
    """Records which tokens would be snapshotted instead of running the batch."""

    def __init__(self) -> None:
        self.started: list[SnapshotCtx] = []

    async def run(self, ctx: SnapshotCtx) -> None:
        self.started.append(ctx)


def make_scheduler(env: Env) -> tuple[Scheduler, NoSnapshots]:
    snaps = NoSnapshots()
    return Scheduler(env.cfg, env.state, env.rpc, env.writer, env.budget, snaps), snaps


def bc(real_sol: float, **kw: Any) -> pl.BondingCurve:
    out = pl.decode_bonding_curve(curve_bytes(rq=int(real_sol * SOL), **kw))
    assert out is not None
    return out


def test_hash_unit_is_deterministic_and_uniform() -> None:
    assert hash_unit("abc") == hash_unit("abc")
    values = [hash_unit(f"mint{i}") for i in range(4000)]
    assert all(0 <= v < 1 for v in values)
    assert 0.45 < sum(values) / len(values) < 0.55
    assert hash_unit("abc") == int("ba7816bf", 16) / 2**32  # sha256("abc")[:8]


def test_cadence_every_follows_config() -> None:
    cfg = make_cfg().tracking
    price = [cadence_every(cfg.price_cadence, e) for e in (0, 899, 900, 3599, 3600, 21599, 21600, 87299, 87300)]
    assert price == [15, 15, 30, 30, 300, 300, 1800, 1800, None]
    assert [cadence_every(cfg.balance_cadence, e) for e in (0, 3600, 21600, 90000)] == [60, 600, 3600, None]


def test_is_dead() -> None:
    assert is_dead(0.04, 1.0, 10, 0.05, 1800)  # under 5% of peak
    assert not is_dead(0.06, 1.0, 10, 0.05, 1800)
    assert is_dead(1.0, 1.0, 1800, 0.05, 1800)  # no reserve change for 30 min
    assert not is_dead(None, None, 0, 0.05, 1800)


# ----------------------------------------------------------------- decision
def test_decide_filter_pass_random_rest_and_exclusions(env_factory: Callable[..., Env]) -> None:
    env = env_factory(lambda m, p: None)
    sched, _ = make_scheduler(env)
    thr = env.cfg.activity_filter.threshold_sol

    d = sched.decide("mintA", bc(thr + 1), "ok", T0)
    assert (d["filter_pass"], d["tracked"], d["reason"], d["sample_prob"]) == (1, 1, "filter_pass", 1.0)
    assert d["filter_real_sol_300"] == pytest.approx(thr + 1) and d["benchmark"] == 0  # scanners disabled

    d = sched.decide("mintB", bc(thr - 0.5), "ok", T0)  # first token ever: rate estimate is tiny, so sampled in
    assert (d["filter_pass"], d["eligible_rest"], d["reason"]) == (0, 1, "random_rest")

    d = sched.decide("mintC", bc(50, quote_mint=CREATOR), "ok", T0)
    assert (d["tracked"], d["flags"], d["filter_pass"], d["eligible_rest"]) == (0, "non_sol_quote", 0, 0)
    d = sched.decide("mintD", bc(85, complete=True), "ok", T0)
    assert (d["tracked"], d["flags"]) == (0, "graduated_before_decision")
    d = sched.decide("mintE", None, "missed", T0)
    assert (d["tracked"], d["reason"], d["flags"]) == (0, "not_tracked", "tick300_missed")
    d = sched.decide("mintF", None, "missing_account", T0)
    assert d["flags"] == "tick300_missing_account"


def _seed_decisions(env: Env, n_pass: int, n_rest: int, start: float) -> None:
    for i in range(n_pass + n_rest):
        env.state.put_tracking({"mint": f"old{i}", "decided_at": start + i, "launch_obs": start,
                                "filter_pass": int(i < n_pass), "eligible_rest": int(i >= n_pass),
                                "tracked": 0, "reason": "not_tracked"})


def test_hash_sampling_holds_the_daily_caps(env_factory: Callable[..., Env]) -> None:
    env = env_factory(lambda m, p: None)
    sched, _ = make_scheduler(env)
    _seed_decisions(env, n_pass=2000, n_rest=26000, start=T0 - 86400)  # a full day of history
    now = T0 + 1
    thr = env.cfg.activity_filter.threshold_sol
    passes = [sched.decide(f"p{i}", bc(thr + 1), "ok", now) for i in range(2000)]
    assert passes[0]["sample_prob"] == pytest.approx(500 / 2001, rel=1e-3)
    assert sum(d["tracked"] for d in passes) == pytest.approx(500, abs=60)
    assert all(d["flags"] == "cap_sampled_out" for d in passes if not d["tracked"])
    rest = [sched.decide(f"r{i}", bc(0.1), "ok", now) for i in range(26000)]
    assert sum(d["tracked"] for d in rest) == pytest.approx(100, abs=35)
    # Sampling is by hash: the same mint always gets the same answer at the same probability.
    assert sched.decide("p7", bc(thr + 1), "ok", now) == passes[7]


def test_first_day_rate_is_extrapolated(env_factory: Callable[..., Env]) -> None:
    env = env_factory(lambda m, p: None)
    sched, _ = make_scheduler(env)
    _seed_decisions(env, n_pass=60, n_rest=0, start=T0)  # 60 passes in the first hour = 1440/day
    d = sched.decide("x", bc(99), "ok", T0 + 3600)
    assert d["sample_prob"] == pytest.approx(500 / (61 * 24), rel=1e-3)


def test_throttle_and_hard_stop_change_the_caps(env_factory: Callable[..., Env]) -> None:
    env = env_factory(lambda m, p: None)
    sched, _ = make_scheduler(env)
    assert (sched.caps().filter_pass, sched.caps().random_rest) == (500, 100)
    assert sched.balance_multiplier(4000) == 1
    env.budget.level = 1
    assert sched.balance_multiplier(3599) == 1 and sched.balance_multiplier(3600) == 2
    assert sched.caps().random_rest == 100
    env.budget.level = 2
    assert (sched.caps().filter_pass, sched.caps().random_rest) == (500, 50)
    env.budget.level = 3
    assert (sched.caps().filter_pass, sched.caps().random_rest) == (400, 40)
    env.budget._hard_stop = True
    d = sched.decide("m", bc(99), "ok", T0)
    assert (d["tracked"], d["filter_pass"], d["flags"]) == (0, 1, "hard_stop")
    # A lower tracked cap (rule 5.1.5) scales both groups and keeps their ratio.
    env2 = env_factory(lambda m, p: None, make_cfg({"tracking": {"tracked_cap_per_day": 300}}))
    sched2, _ = make_scheduler(env2)
    assert (sched2.caps().filter_pass, sched2.caps().random_rest) == (250, 50)


def test_benchmark_sampling_when_scanners_are_enabled(env_factory: Callable[..., Env]) -> None:
    env = env_factory(lambda m, p: None, make_cfg({"scanners": {"enabled": True}}))
    sched, _ = make_scheduler(env)
    _seed_decisions(env, n_pass=400, n_rest=0, start=T0 - 86400)
    ds = [sched.decide(f"b{i}", bc(99), "ok", T0 + 1) for i in range(400)]
    assert all(d["tracked"] for d in ds)  # 400/day is under the cap
    assert sum(d["benchmark"] for d in ds) == pytest.approx(80, abs=30)
    assert sched.decide("rest", bc(0.1), "ok", T0 + 1)["benchmark"] == 0  # only filter-pass tokens


# -------------------------------------------------------------- filter step
def _launch(env: Env, mint: str, t: float, curve: str = CURVE) -> None:
    env.state.add_token(mint, t, {"signature": f"sig-{mint}", "traderPublicKey": CREATOR,
                                  "bondingCurveKey": curve, "name": "n", "symbol": "s", "uri": "u"}, t + 60)


def test_filter_ticks_decision_and_snapshot_start(env_factory: Callable[..., Env]) -> None:
    reserves = {"poor": SOL // 10}  # "rich" is set from the configured threshold below

    def handler(method: str, params: list[Any]) -> Any:
        assert method == "getMultipleAccounts" and params[1]["commitment"] == "confirmed"
        return {"context": {"slot": 5}, "value": [account_value(curve_bytes(rq=reserves[a])) if a in reserves
                                                 else None for a in params[0]]}

    env = env_factory(handler)
    reserves["rich"] = int(env.cfg.activity_filter.threshold_sol + 1) * SOL
    sched, snaps = make_scheduler(env)
    # Decisions are stamped with the RPC response time (the real clock), so the history must be
    # recent relative to the real clock: random-rest sampling is then ~0.1%.
    _seed_decisions(env, n_pass=0, n_rest=30000, start=time.time() - 30000)
    _launch(env, "mintRich", T0, "rich")
    _launch(env, "mintPoor", T0, "poor")
    _launch(env, "mintGone", T0, "gone")

    async def go() -> None:
        await sched.step(T0 + 30)  # nothing due yet
        assert env.fake.calls == []
        for t in (61, 181, 301):
            await sched.step(T0 + t)
        await asyncio.sleep(0)

    asyncio.run(go())
    assert len(env.fake.calls) == 3  # one batched call per grid step
    ticks = env.state.q("SELECT mint, tick_s, status, real_sol, complete FROM filter_ticks ORDER BY mint, tick_s")
    assert [(r["mint"], r["tick_s"], r["status"]) for r in ticks] == [
        ("mintGone", 60, "missing_account"), ("mintGone", 180, "missing_account"), ("mintGone", 300, "missing_account"),
        ("mintPoor", 60, "ok"), ("mintPoor", 180, "ok"), ("mintPoor", 300, "ok"),
        ("mintRich", 60, "ok"), ("mintRich", 180, "ok"), ("mintRich", 300, "ok")]
    assert env.state.one("SELECT COUNT(*) FROM tokens WHERE next_tick_due IS NOT NULL") == 0
    assert env.state.one("SELECT creator FROM tokens WHERE mint='mintRich'") == CREATOR
    track = {r["mint"]: r for r in env.state.q("SELECT * FROM tracking WHERE mint LIKE 'mint%'")}
    assert (track["mintRich"]["tracked"], track["mintRich"]["reason"]) == (1, "filter_pass")
    assert (track["mintPoor"]["tracked"], track["mintPoor"]["eligible_rest"]) == (0, 1)
    assert track["mintGone"]["flags"] == "tick300_missing_account"
    assert [c.mint for c in snaps.started] == ["mintRich"]
    assert snaps.started[0].creation_sig == "sig-mintRich" and snaps.started[0].creators == {CREATOR}
    # Every decision is written to raw, tracked or not; nothing is dropped silently.
    decisions = {r["mint"]: r for r in env.records("collector", "tracking")}
    assert set(decisions) == {"mintRich", "mintPoor", "mintGone"}
    assert decisions["mintRich"]["payload"]["threshold_sol"] == env.cfg.activity_filter.threshold_sol
    rec = env.records("helius", "accounts")[0]
    assert rec["purpose"] == "filter" and {"mint": "mintRich", "tick_s": 60} in rec["request"]["items"]


def test_late_ticks_are_recorded_as_missed_not_polled(env_factory: Callable[..., Env]) -> None:
    env = env_factory(lambda m, p: {"context": {"slot": 5}, "value": [account_value(curve_bytes(rq=9 * SOL))]})
    sched, snaps = make_scheduler(env)
    _launch(env, "mintLate", T0)
    asyncio.run(sched.step(T0 + 1000))  # collector was down: every tick is long overdue
    for _ in range(2):
        asyncio.run(sched.step(T0 + 1000))
    assert env.fake.calls == []
    assert [r["status"] for r in env.state.q("SELECT status FROM filter_ticks")] == ["missed"] * 3
    row = env.state.q("SELECT tracked, flags FROM tracking")[0]
    assert (row["tracked"], row["flags"]) == (0, "tick300_missed") and snaps.started == []


def test_batches_are_capped_at_batch_accounts(env_factory: Callable[..., Env]) -> None:
    env = env_factory(lambda m, p: {"context": {"slot": 5}, "value": [None] * len(p[0])})
    sched, _ = make_scheduler(env)
    for i in range(250):
        _launch(env, f"m{i}", T0, f"curve{i}")
    asyncio.run(sched.step(T0 + 61))
    assert sorted(len(p[0]) for _h, _m, p in env.fake.calls) == [50, 100, 100]


# --------------------------------------------------------------- price step
def _tracked(env: Env, mint: str = "M", snapshot_ts: float = T0, **over: Any) -> None:
    price = pl.price_from_reserves(30 * SOL, 10**15)
    row = {"mint": mint, "decided_at": snapshot_ts, "launch_obs": snapshot_ts - 300, "bonding_curve": CURVE,
           "filter_pass": 1, "eligible_rest": 0, "tracked": 1, "reason": "filter_pass", "snapshot_ts": snapshot_ts,
           "p0": price, "peak_price": price, "last_price": price, "last_vq": 30 * SOL, "last_vt": 10**15,
           "last_change_obs": snapshot_ts, "price_nominal": snapshot_ts, "next_price_due": snapshot_ts + 15,
           "snapshot_complete": 1}
    env.state.put_tracking({**row, **over})
    env.state.x("INSERT INTO balance_accounts VALUES(?,?,?,?,?)", (mint, "acc1", "owner1", "early_buyer", 0))


def _row(env: Env, mint: str = "M") -> Any:
    return env.state.q("SELECT * FROM tracking WHERE mint=?", (mint,))[0]


def test_price_tick_updates_peak_and_schedule(env_factory: Callable[..., Env]) -> None:
    curve = {"data": curve_bytes(vq=40 * SOL, vt=8 * 10**14)}
    env = env_factory(lambda m, p: {"context": {"slot": 9}, "value": [account_value(curve["data"])]})
    sched, _ = make_scheduler(env)
    _tracked(env)
    asyncio.run(sched.step(T0 + 15.2))
    row = _row(env)
    assert row["peak_price"] == row["last_price"] == pytest.approx(40 / 8e8)
    assert row["next_price_due"] == T0 + 30 and row["dead"] == 0 and row["last_vq"] == 40 * SOL
    rec = env.records("helius", "accounts")[0]
    assert rec["purpose"] == "price" and rec["request"]["items"] == [{"mint": "M", "tick_s": 15}]
    # Balance polling starts once the token is alive and has accounts.
    assert row["next_balance_due"] is not None


def test_dead_rule_keeps_first_hour_cadence_then_slows(env_factory: Callable[..., Env]) -> None:
    crashed = curve_bytes(vq=1 * SOL, vt=10**15)  # price fell to ~3% of P0
    env = env_factory(lambda m, p: {"context": {"slot": 9}, "value": [account_value(crashed)]})
    sched, _ = make_scheduler(env)
    _tracked(env, next_price_due=T0 + 1200, next_balance_due=T0 + 1200)
    asyncio.run(sched.step(T0 + 1200.5))
    row = _row(env)
    assert row["dead"] == 1
    assert row["next_price_due"] == T0 + 1230  # still 30 s: price ticks of the first 3600 s are never slowed
    assert row["next_balance_due"] is None  # balance polling stops for a dead token
    env.state.update_tracking("M", next_price_due=T0 + 4000)
    asyncio.run(sched.step(T0 + 4000.5))
    assert _row(env)["next_price_due"] == T0 + 4000 + 1800  # after the first hour: slowest cadence


def test_quiet_token_is_dead_and_revives_on_a_reserve_change(env_factory: Callable[..., Env]) -> None:
    curve = {"data": curve_bytes(vq=30 * SOL, vt=10**15)}
    env = env_factory(lambda m, p: {"context": {"slot": 9}, "value": [account_value(curve["data"])]})
    sched, _ = make_scheduler(env)
    _tracked(env, next_price_due=T0 + 4000)
    asyncio.run(sched.step(T0 + 4000.5))  # no reserve change for > 1800 s
    assert _row(env)["dead"] == 1 and _row(env)["next_balance_due"] is None
    curve["data"] = curve_bytes(vq=31 * SOL, vt=97 * 10**13)  # someone traded
    env.state.update_tracking("M", next_price_due=T0 + 5800)
    asyncio.run(sched.step(T0 + 5800.5))
    row = _row(env)
    assert row["dead"] == 0 and row["next_price_due"] == T0 + 5800 + 300
    assert row["next_balance_due"] == pytest.approx(T0 + 5800.5 + 600)


def test_graduation_is_flagged_and_horizon_ends_polling(env_factory: Callable[..., Env]) -> None:
    done = curve_bytes(vq=115 * SOL, vt=2 * 10**14, complete=True)
    env = env_factory(lambda m, p: {"context": {"slot": 9}, "value": [account_value(done)]})
    sched, _ = make_scheduler(env)
    _tracked(env)
    asyncio.run(sched.step(T0 + 15.5))
    row = _row(env)
    assert row["graduated_ts"] is not None and row["dead"] == 1 and row["next_balance_due"] is None
    env.state.update_tracking("M", next_price_due=T0 + 87300)
    asyncio.run(sched.step(T0 + 87300.5))
    assert _row(env)["next_price_due"] is None  # 24 h horizon reached


def test_failed_price_read_keeps_the_schedule_and_downtime_skips_ahead(env_factory: Callable[..., Env]) -> None:
    import httpx

    env = env_factory(lambda m, p: httpx.Response(500))
    sched, _ = make_scheduler(env)
    _tracked(env)
    asyncio.run(sched.step(T0 + 15.5))
    row = _row(env)
    assert row["next_price_due"] == T0 + 30 and row["last_vq"] == 30 * SOL
    assert [r["status"] for r in env.records("helius", "accounts")] == ["error"]
    asyncio.run(sched.step(T0 + 500))  # 470 s behind: do not replay 31 stale ticks
    assert _row(env)["next_price_due"] == T0 + 500


def test_balance_step_batches_accounts_and_respects_hard_stop(env_factory: Callable[..., Env]) -> None:
    env = env_factory(lambda m, p: {"context": {"slot": 9}, "value": [None] * len(p[0])})
    sched, _ = make_scheduler(env)
    _tracked(env, next_price_due=None, next_balance_due=T0 + 60, balance_nominal=T0 + 60)
    env.state.x("INSERT INTO balance_accounts VALUES('M','acc2','owner2','creator',1)")
    asyncio.run(sched._balance_step(T0 + 60.5))
    (call,) = env.fake.calls
    assert sorted(call[2][0]) == ["acc1", "acc2"] and call[2][1]["dataSlice"] == {"offset": 0, "length": 72}
    rec = env.records("helius", "accounts")[0]
    assert rec["purpose"] == "balance" and rec["request"]["items"][0]["tick_s"] == 60
    assert _row(env)["next_balance_due"] == T0 + 120
    env.budget._hard_stop = True
    asyncio.run(sched._balance_step(T0 + 200))
    assert len(env.fake.calls) == 1


def test_holders_poll_at_1800s(env_factory: Callable[..., Env]) -> None:
    env = env_factory(lambda m, p: {"context": {"slot": 9}, "value": []})
    sched, _ = make_scheduler(env)
    _tracked(env, next_price_due=None, holders_due=T0 - 300 + 1800)
    asyncio.run(sched._holders_step(T0 + 1500.5))
    assert env.fake.methods() == ["getTokenLargestAccounts"]
    assert _row(env)["holders_due"] is None
    rec = env.records("helius", "largest_accounts")[0]
    assert (rec["purpose"], rec["tick_s"], rec["mint"]) == ("holders", 1800, "M")


def test_recover_flags_interrupted_snapshots(env_factory: Callable[..., Env]) -> None:
    env = env_factory(lambda m, p: None)
    sched, _ = make_scheduler(env)
    _tracked(env, mint="X", snapshot_complete=None, next_price_due=None)
    env.state.update_tracking("X", snapshot_ts=None)
    sched.recover(T0)
    row = _row(env, "X")
    assert (row["snapshot_complete"], row["flags"]) == (0, "snapshot_interrupted")
    assert env.records("collector", "snapshot_summary")[0]["payload"]["missing_parts"] == ["interrupted"]
