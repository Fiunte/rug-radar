"""RPC client (fallback, records, key hygiene) and the credit budget."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from rugwatch.collector.budget import REVIEW_INTERVAL_S, Budget, cycle_start, hour_key
from rugwatch.collector.rpc import params_hash, providers_from_env
from rugwatch.collector.state import State
from tests.conftest import SECRET, Env, make_cfg


def _ts(text: str) -> float:
    return datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=UTC).timestamp()


# ---------------------------------------------------------------------- rpc
def test_success_writes_one_record_and_one_credit(env_factory: Callable[..., Env]) -> None:
    env = env_factory(lambda m, p: {"context": {"slot": 77}, "value": [None]})
    res = asyncio.run(env.rpc.get_multiple_accounts(["A"], purpose="price", commitment="confirmed",
                                                    items=[{"mint": "M", "tick_s": 15}]))
    assert res.ok and res.source == "helius" and res.slot == 77
    (rec,) = env.records("helius", "accounts")
    assert rec["status"] == "ok" and rec["purpose"] == "price" and rec["slot"] == 77
    assert rec["request"]["accounts"] == ["A"] and rec["request"]["items"] == [{"mint": "M", "tick_s": 15}]
    assert rec["request"]["params_hash"] == params_hash("getMultipleAccounts", env.fake.calls[0][2])
    assert env.fake.calls[0][2][1] == {"encoding": "base64", "commitment": "confirmed"}
    assert env.state.q("SELECT source,purpose,calls,credits,errors FROM credit_ledger")[0][:] == \
        ("helius", "price", 1, 1, 0)


def test_rate_limited_call_is_retried_on_the_same_provider(env_factory: Callable[..., Env],
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("rugwatch.collector.rpc.RETRY_WAIT_S", 0.01)
    env = env_factory(lambda m, p: httpx.Response(429) if len(env.fake.calls) < 3 else {"context": {"slot": 1},
                                                                                         "value": []})
    res = asyncio.run(env.rpc.get_multiple_accounts([], purpose="price", commitment="confirmed"))
    assert res.ok and res.source == "helius"
    assert [h for h, _m, _p in env.fake.calls] == ["helius.test"] * 3
    assert [r["status"] for r in env.records("helius", "accounts")] == ["error", "error", "ok"]


def test_falls_back_to_second_provider_and_records_both(env_factory: Callable[..., Env]) -> None:
    def handler(method: str, params: list[Any]) -> Any:
        return httpx.Response(503) if len(env.fake.calls) == 1 else [{"signature": "s", "err": None}]

    env = env_factory(handler)
    res = asyncio.run(env.rpc.get_signatures("ADDR", purpose="snapshot", commitment="confirmed", mint="M"))
    assert res.ok and res.source == "alchemy"
    assert [h for h, _m, _p in env.fake.calls] == ["helius.test", "alchemy.test"]
    (bad,) = env.records("helius", "signatures")
    assert bad["status"] == "error" and bad["payload"] == {"error": "http_503"} and bad["mint"] == "M"
    (good,) = env.records("alchemy", "signatures")
    assert good["status"] == "ok"
    ledger = {r["source"]: (r["calls"], r["credits"], r["errors"]) for r in env.state.q("SELECT * FROM credit_ledger")}
    assert ledger == {"helius": (1, 1, 1), "alchemy": (1, 0, 0)}


def test_rpc_error_timeout_and_total_failure(env_factory: Callable[..., Env]) -> None:
    def handler(method: str, params: list[Any]) -> Any:
        if len(env.fake.calls) == 1:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "error": {"code": -32015, "message": "nope"}})
        return httpx.ReadTimeout("slow")

    env = env_factory(handler)
    res = asyncio.run(env.rpc.get_transaction("SIG", purpose="snapshot", commitment="confirmed", mint="M"))
    assert not res.ok and res.result is None
    assert env.records("helius", "transaction")[0]["payload"]["code"] == -32015
    assert env.records("alchemy", "transaction")[0]["status"] == "timeout"
    assert env.fake.calls[0][2][1]["maxSupportedTransactionVersion"] == 1


def test_auth_error_parks_the_provider(env_factory: Callable[..., Env]) -> None:
    def handler(method: str, params: list[Any]) -> Any:
        host = env.fake.calls[-1][0]
        return httpx.Response(403) if host == "helius.test" else {"context": {"slot": 1}, "value": []}

    env = env_factory(handler)
    for _ in range(3):
        asyncio.run(env.rpc.get_multiple_accounts([], purpose="price", commitment="confirmed"))
    assert [h for h, _m, _p in env.fake.calls] == ["helius.test", "alchemy.test", "alchemy.test", "alchemy.test"]


def test_api_keys_never_reach_records_or_logs(env_factory: Callable[..., Env],
                                              caplog: pytest.LogCaptureFixture) -> None:
    def handler(method: str, params: list[Any]) -> Any:
        return httpx.ConnectError(f"cannot reach https://helius.test/?api-key={SECRET}")

    env = env_factory(handler)
    with caplog.at_level(logging.DEBUG):
        asyncio.run(env.rpc.get_multiple_accounts(["A"], purpose="filter", commitment="confirmed"))
    env.writer.flush()
    blob = "".join(json.dumps(r) for src in ("helius", "alchemy") for r in env.records(src, "accounts"))
    assert blob and SECRET not in blob
    assert SECRET not in caplog.text
    assert SECRET not in repr(env.providers)
    assert logging.getLogger("httpx").level >= logging.WARNING


def test_providers_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("HELIUS_API_KEY", "ALCHEMY_API_KEY", "FLUXRPC_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    assert [p.name for p in providers_from_env(8)] == ["public"]
    monkeypatch.setenv("HELIUS_API_KEY", "h")
    monkeypatch.setenv("ALCHEMY_API_KEY", "a")
    assert [p.name for p in providers_from_env(8)] == ["helius", "alchemy", "public"]
    monkeypatch.setenv("FLUXRPC_API_KEY", "f")
    assert [p.name for p in providers_from_env(8)] == ["helius", "alchemy", "fluxrpc"]


# ------------------------------------------------------------------- budget
def test_cycle_start() -> None:
    assert cycle_start(_ts("2026-11-17 12:00"), 1) == _ts("2026-11-01 00:00")
    assert cycle_start(_ts("2026-11-17 12:00"), 17) == _ts("2026-11-17 00:00")
    assert cycle_start(_ts("2026-11-16 23:00"), 17) == _ts("2026-10-17 00:00")
    assert cycle_start(_ts("2027-01-05 00:00"), 20) == _ts("2026-12-20 00:00")
    assert cycle_start(_ts("2027-03-30 00:00"), 31) == _ts("2027-03-28 00:00")  # clamped to day 28


def _spend(budget: Budget, start: float, hours: int, per_hour: int) -> None:
    for h in range(hours):
        budget.state.x("INSERT INTO credit_ledger VALUES(?,?,?,?,?,0)",
                       (hour_key(start + h * 3600), "helius", "price", per_hour, per_hour))


def test_projection_uses_trailing_24h() -> None:
    budget = Budget(State(":memory:"), make_cfg().collector.budget)
    t0 = _ts("2026-11-17 00:00")
    assert budget.projection(t0) is None
    _spend(budget, t0, 48, 1000)
    assert budget.projection(t0 + 1800) is None  # under an hour of data
    assert budget.projection(t0 + 48 * 3600) == pytest.approx(1000 * 24 * 30, rel=0.05)
    budget.record("alchemy", "price", now=t0)  # other providers are not metered
    assert budget.cycle_used(t0 + 48 * 3600) == 48_000


def test_throttle_steps_escalate_one_at_a_time_and_release() -> None:
    cfg = make_cfg().collector.budget
    state = State(":memory:")
    budget = Budget(state, cfg)
    t0 = _ts("2026-11-17 00:00")
    _spend(budget, t0, 72, 2000)  # 1.44M / 30 d: above the 800k target
    now = t0 + 30 * 3600
    for expected in (1, 2, 3, 3):
        budget.review(now)
        assert budget.level == expected
        budget.review(now + 60)  # no second change inside the review interval
        assert budget.level == expected
        now += REVIEW_INTERVAL_S
    assert [budget.active(s) for s in cfg.throttle_order] == [True, True, True]
    assert Budget(state, cfg).level == 3  # persisted across restarts
    state.x("UPDATE credit_ledger SET credits=100")  # 72k / 30 d
    budget.review(now)
    assert budget.level == 2
    assert not budget.hard_stop


def test_ascending_history_call_costs_ten_credits_and_is_helius_only(env_factory: Callable[..., Env]) -> None:
    env = env_factory(lambda m, p: httpx.Response(500))
    res = asyncio.run(env.rpc.get_oldest_transactions("CURVE", limit=21, purpose="snapshot",
                                                      commitment="confirmed", mint="M"))
    assert not res.ok and [h for h, _m, _p in env.fake.calls] == ["helius.test"]  # never sent to the fallback
    assert env.state.q("SELECT credits FROM credit_ledger")[0]["credits"] == 10


def test_hard_stop_at_cycle_limit() -> None:
    cfg = make_cfg().collector.budget
    budget = Budget(State(":memory:"), cfg)
    t0 = _ts("2026-11-02 00:00")
    _spend(budget, t0, 10, cfg.hard_stop_credits // 10)
    budget.review(t0 + 11 * 3600)
    assert budget.hard_stop
    budget.review(_ts("2026-12-01 01:00") + 120)  # new cycle
    assert not budget.hard_stop
