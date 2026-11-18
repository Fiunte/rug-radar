"""Second launch source (public-RPC logsSubscribe), launch coverage check, data clock (D-31, D-32)."""

from __future__ import annotations

import asyncio
import copy
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest
from websockets.asyncio.server import ServerConnection, serve
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed, InvalidStatus
from websockets.frames import Close
from websockets.http11 import Response

from rugwatch.collector import coverage, report
from rugwatch.collector.launchlogs import (
    ChainLaunchListener,
    SubscriptionError,
    is_rate_limited,
    launch_message,
    launches_from_logs,
)
from rugwatch.collector.main import start_data_clock
from rugwatch.collector.pumpportal import PumpPortalListener
from rugwatch.collector.state import State
from rugwatch.storage.raw import RawWriter, iter_raw
from tests.conftest import FIXTURES, Env, make_cfg
from tests.unit.test_scheduler import make_scheduler

NOTIFICATIONS: list[dict[str, Any]] = json.loads((FIXTURES / "rpc_logs.json").read_text())


def _value(i: int) -> dict[str, Any]:
    return copy.deepcopy(NOTIFICATIONS[i]["params"]["result"]["value"])


# ------------------------------------------------------------------ decoding
def test_launches_from_logs_on_real_notifications() -> None:
    for i in range(3):  # three real creates
        launches = launches_from_logs(_value(i)["logs"])
        assert len(launches) == 1
        c, buy = launches[0].create, launches[0].dev_buy
        assert c.mint and c.bonding_curve and c.user and c.name is not None
        assert buy is not None and buy.mint == c.mint and buy.is_buy and buy.token_amount > 0
        msg = launch_message(_value(i)["signature"], launches[0])
        assert msg["initialBuy"] == buy.token_amount / 1e6 and msg["solAmount"] == buy.sol_amount / 1e9
        assert msg["bondingCurveKey"] == c.bonding_curve and msg["traderPublicKey"] == c.user
    assert launches_from_logs(_value(3)["logs"]) == []  # PumpSwap CreatePool: not a launch
    assert launches_from_logs(["Program data: !!!not base64", "Program log: x"]) == []


def test_chain_listener_merges_with_pumpportal(tmp_path: Path) -> None:
    cfg = make_cfg()
    state = State(":memory:")
    writer = RawWriter(tmp_path, min_free_bytes=0)
    pp = PumpPortalListener(cfg.collector.pumpportal, cfg.activity_filter, state, writer)
    chain = ChainLaunchListener(state, writer, 60)
    first = launches_from_logs(_value(0)["logs"])[0].create
    t = time.time()
    pp.handle({"txType": "create", "pool": "pump", "mint": first.mint, "bondingCurveKey": first.bonding_curve,
               "signature": _value(0)["signature"], "traderPublicKey": first.user}, t)
    assert chain.handle(_value(0), 5, t + 1.5) == 1  # same launch, seen later by the second source
    assert chain.handle(_value(1), 6, t + 2) == 1
    assert chain.handle(_value(3), 7, t + 3) == 0  # not a launch
    failed = _value(2)
    failed["err"] = {"InstructionError": [0, "Custom"]}
    assert chain.handle(failed, 8, t + 4) == 0  # failed create: no token
    rows = {r["mint"]: r for r in state.q("SELECT * FROM tokens")}
    assert len(rows) == 2 and rows[first.mint]["launch_obs"] == t  # the earliest source sets t0
    second = launches_from_logs(_value(1)["logs"])[0]
    assert rows[second.create.mint]["launch_obs"] == t + 2 and rows[second.create.mint]["next_tick_due"] == t + 62
    assert rows[second.create.mint]["launcher"] == second.create.user
    assert rows[second.create.mint]["sig"] == _value(1)["signature"]
    src = state.q("SELECT mint, source, observed_at FROM launch_sources ORDER BY observed_at")
    assert [(r["mint"], r["source"]) for r in src] == [(first.mint, "pumpportal"), (first.mint, "publicrpc"),
                                                      (second.create.mint, "publicrpc")]
    writer.flush()
    recs = list(iter_raw(tmp_path, "publicrpc", "new_token"))
    assert len(recs) == 4 and recs[0]["slot"] == 5 and recs[0]["payload"]["signature"] == _value(0)["signature"]
    assert recs[2]["mint"] is None  # the CreatePool notification is kept in raw too
    assert chain.last_launch_at == t + 2


def test_rate_limit_classification() -> None:
    assert is_rate_limited(ConnectionClosed(Close(1013, "try again later"), None))
    assert is_rate_limited(ConnectionClosed(Close(4000, "Rate limit exceeded"), None))
    assert not is_rate_limited(ConnectionClosed(Close(1000, ""), None))
    assert not is_rate_limited(ConnectionClosed(None, None))
    assert is_rate_limited(InvalidStatus(Response(429, "Too Many Requests", Headers())))
    assert not is_rate_limited(InvalidStatus(Response(503, "Unavailable", Headers())))
    assert is_rate_limited(SubscriptionError({"code": -32005, "message": "rate limit exceeded"}))
    assert not is_rate_limited(SubscriptionError({"code": -32601, "message": "method not found"}))
    assert not is_rate_limited(TimeoutError())


def test_chain_listener_against_a_fake_server(tmp_path: Path) -> None:
    state = State(":memory:")
    writer = RawWriter(tmp_path, min_free_bytes=0)
    subscriptions: list[dict[str, Any]] = []

    async def handler(ws: ServerConnection) -> None:
        subscriptions.append(json.loads(await ws.recv()))
        await ws.send(json.dumps({"jsonrpc": "2.0", "result": 7, "id": 1}))
        await ws.send(json.dumps(NOTIFICATIONS[0]))
        await ws.close(1013, "try again later")  # the public endpoint pushing back

    async def go() -> None:
        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            listener = ChainLaunchListener(state, writer, 60, url=f"ws://127.0.0.1:{port}")
            task = asyncio.create_task(listener.run())
            for _ in range(200):
                await asyncio.sleep(0.02)
                if state.one("SELECT COUNT(*) FROM events WHERE kind='rpcws_rate_limited'"):
                    break
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            assert not listener.connected

    asyncio.run(go())
    assert subscriptions[0]["method"] == "logsSubscribe"
    assert subscriptions[0]["params"][0] == {"mentions": ["TSLvdd1pWpHVjahSpsvCXUbgwsL3JAcvokwaKt1eokM"]}
    assert state.one("SELECT COUNT(*) FROM tokens") == 1
    kinds = [r["kind"] for r in state.q("SELECT kind FROM events ORDER BY id")]
    assert kinds == ["rpcws_connect", "rpcws_rate_limited", "rpcws_drop"]
    assert '"reason": "rate_limited"' in state.one("SELECT detail FROM events WHERE kind='rpcws_drop'")
    assert state.get_meta("publicrpc_since") is not None


def test_state_migration_adds_the_new_heartbeat_column(tmp_path: Path) -> None:
    path = tmp_path / "old.sqlite"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE heartbeats (minute INTEGER PRIMARY KEY, ws_connected INTEGER NOT NULL)")
    db.execute("INSERT INTO heartbeats VALUES (1, 1)")
    db.commit()
    db.close()
    state = State(path)
    state.heartbeat(120.0, True, True)
    assert [tuple(r) for r in state.q("SELECT * FROM heartbeats ORDER BY minute")] == [(1, 1, 0), (2, 1, 1)]
    state.close()
    State(path).close()  # idempotent


# ------------------------------------------------------------------ coverage
def test_coverage_check(env_factory: Any, pump_fixture: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
                        tmp_path: Path) -> None:
    create_sig = next(s for s in pump_fixture["txs"] if s.startswith("2NxiAR"))
    other_sig = next(s for s in pump_fixture["txs"] if s.startswith("sdkma"))
    seen = [f"S{i}" for i in range(8)]
    page = ([{"signature": s, "blockTime": 3000, "err": None} for s in [*seen, create_sig, other_sig]]
            + [{"signature": "F", "blockTime": 2500, "err": {"x": 1}},  # failed: not counted
               {"signature": "OLD", "blockTime": 500, "err": None}])  # before the window: stop paging

    def handler(method: str, params: list[Any]) -> Any:
        if method == "getSignaturesForAddress":
            return page
        return pump_fixture["txs"][params[0]]

    env: Env = env_factory(handler)
    for i, s in enumerate(seen):
        if i < 6:
            env.state.add_launch_source(f"m{i}", "publicrpc", 3000.0, s)
        if i in (0, 1, 6, 7):
            env.state.add_launch_source(f"m{i}", "pumpportal", 3000.0, s)
    result = asyncio.run(coverage.run_check(env.rpc, env.state, env.writer, 1000.0, 5000.0))
    assert (result["chain_signatures"], result["seen"], result["unseen"]) == (10, 8, 2)
    assert (result["classified"], result["classified_creates"], result["missed_creates_est"]) == (2, 1, 1.0)
    assert result["coverage"] == round(8 / 9, 4)
    assert result["by_source"] == {"publicrpc": round(6 / 9, 4), "pumpportal": round(4 / 9, 4)}
    assert env.fake.methods().count("getSignaturesForAddress") == 1
    assert len(env.records("collector", "coverage")) == 1

    db_path = tmp_path / "state.sqlite"
    env.state.db.execute(f"VACUUM INTO '{db_path}'")
    lines = report.launch_source_lines(sqlite3.connect(db_path), time.time())
    cov_line = next(line for line in lines if line.startswith("launch coverage"))
    assert "all=88.9%" in cov_line and "publicrpc=66.7%" in cov_line and "ALERT: below 95%" in cov_line


def test_coverage_due_window() -> None:
    state = State(":memory:")
    now = 1_000_000.0
    assert coverage.due_window(state, now) is None  # public-RPC source never ran
    state.set_meta("publicrpc_since", repr(now - 1800))
    assert coverage.due_window(state, now) is None  # not running for the whole window yet
    state.set_meta("publicrpc_since", repr(now - 7200))
    assert coverage.due_window(state, now) == (now - 3900, now - 300)
    state.set_meta("coverage_next_ts", repr(now + 10))
    assert coverage.due_window(state, now) is None
    assert coverage.due_window(state, now + 10) is not None


# ---------------------------------------------------------------- data clock
def test_data_clock_starts_once_and_resets_the_rate_window(env_factory: Any) -> None:
    env: Env = env_factory(lambda m, p: None)
    now = time.time()
    clock = start_data_clock(env.state, env.writer, env.cfg, now - 3600)
    assert clock == now - 3600 and start_data_clock(env.state, env.writer, env.cfg, now) == clock
    assert env.records("collector", "data_clock")[0]["payload"]["data_clock_start"] == clock
    rows = [(f"pre{i}", now - 40000 + i) for i in range(30000)] + [(f"post{i}", now - 3000 + i) for i in range(100)]
    env.state.db.executemany(
        "INSERT INTO tracking(mint,decided_at,launch_obs,filter_pass,eligible_rest,tracked,reason) "
        "VALUES(?,?,?,0,1,0,'not_tracked')", [(m, t, t) for m, t in rows])
    sched, _ = make_scheduler(env)
    # Only the 100 decisions after the clock count, over the hour since the clock started.
    assert sched._rate_24h("eligible_rest", now) == pytest.approx(101 * 86400 / 3600, rel=1e-6)


def test_report_day1_counts_only_launches_after_the_data_clock(tmp_path: Path,
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUGWATCH_DATA_BASE", str(tmp_path))
    cfg = make_cfg()
    state = State(tmp_path / cfg.storage.state)
    now = time.time()
    clock = now - 7200
    state.set_meta("data_clock_start", repr(clock))
    for i in range(30):  # 10 launches before the clock, 20 after
        t = clock - 3600 + i * 360
        state.add_token(f"m{i}", t, {"bondingCurveKey": f"c{i}"}, t + 60)
        state.put_filter_tick(f"m{i}", 300, "ok", t + 300, 1, 40 * 10**9, 10**15, 10 * 10**9, 1, complete=False)
    state.close()
    day1 = "\n".join(report.report_day1(cfg))
    assert "Data clock start:" in day1 and "Launches: 20 observed" in day1
    assert "share of launches that reached it: n=20" in day1


# ------------------------------------------------------- FluxRPC fallback (D-33)
def test_rate_limited_public_endpoint_switches_to_fluxrpc_and_back(tmp_path: Path,
                                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    import http

    from rugwatch.collector import launchlogs

    monkeypatch.setattr(launchlogs, "RETRY_PUBLIC_AFTER_S", 0.3)  # retry the public endpoint quickly
    state = State(":memory:")
    writer = RawWriter(tmp_path, min_free_bytes=0)

    def reject(connection: Any, request: Any) -> Any:
        return connection.respond(http.HTTPStatus.TOO_MANY_REQUESTS, "rate limited\n")

    async def public_handler(ws: ServerConnection) -> None:
        await ws.wait_closed()

    async def fallback_handler(ws: ServerConnection) -> None:
        await ws.recv()
        await ws.send(json.dumps({"jsonrpc": "2.0", "result": 3, "id": 1}))
        for i in range(3):
            await ws.send(json.dumps(NOTIFICATIONS[i]))
        while True:
            await asyncio.sleep(0.05)
            await ws.send(json.dumps(NOTIFICATIONS[3]))  # keeps messages flowing (not a launch)

    async def go() -> None:
        async with serve(public_handler, "127.0.0.1", 0, process_request=reject) as pub, \
                serve(fallback_handler, "127.0.0.1", 0) as flux:
            urls = [f"ws://127.0.0.1:{s.sockets[0].getsockname()[1]}" for s in (pub, flux)]
            listener = ChainLaunchListener(state, writer, 60, url=urls[0], fallback_url=urls[1])
            task = asyncio.create_task(listener.run())
            for _ in range(300):
                await asyncio.sleep(0.02)
                if state.one("SELECT COUNT(*) FROM events WHERE kind='rpcws_switch'") >= 3:
                    break
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(go())
    switches = [json.loads(r["detail"]) for r in state.q("SELECT detail FROM events WHERE kind='rpcws_switch' "
                                                         "ORDER BY id")]
    assert switches[0] == {"from": "publicrpc", "to": "fluxrpc", "reason": "rate_limited"}
    assert switches[1] == {"from": "fluxrpc", "to": "publicrpc", "reason": "retry_public"}
    assert switches[2] == {"from": "publicrpc", "to": "fluxrpc", "reason": "rate_limited"}
    assert state.one("SELECT COUNT(*) FROM tokens") == 3
    assert {r["source"] for r in state.q("SELECT source FROM launch_sources")} == {"fluxrpc"}
    writer.flush()
    assert len(list(iter_raw(tmp_path, "fluxrpc", "new_token"))) >= 3
    assert state.bandwidth_month("fluxrpc", time.time()) > 30_000  # three ~9 KB notifications at least
    assert state.get_meta("launch_ws_endpoint") in ("fluxrpc", "publicrpc")


def test_fallback_respects_the_bandwidth_budget() -> None:
    from rugwatch.collector import launchlogs

    state = State(":memory:")
    now = time.time()
    no_fallback = ChainLaunchListener(state, RawWriter.__new__(RawWriter), 60)
    assert not no_fallback.fallback_allowed(now)
    listener = ChainLaunchListener(state, RawWriter.__new__(RawWriter), 60, fallback_url="wss://x")
    assert listener.fallback_allowed(now)
    state.bandwidth_add(now, "fluxrpc_ws", int(0.85 * launchlogs.FLUXRPC_MONTHLY_LIMIT_BYTES))
    assert listener.fallback_allowed(now)
    state.bandwidth_add(now, "fluxrpc_http", int(0.06 * launchlogs.FLUXRPC_MONTHLY_LIMIT_BYTES))
    assert not listener.fallback_allowed(now)  # ws + http over 90%
    assert state.bandwidth_month("fluxrpc", now + 40 * 86400) == 0  # a new month starts from zero


def test_status_flags_the_fallback_and_bandwidth(tmp_path: Path) -> None:
    state = State(tmp_path / "s.sqlite")
    now = time.time()
    db = sqlite3.connect(tmp_path / "s.sqlite")
    assert "via publicrpc" in report.stream_endpoint_line(db, now)
    assert "FALLBACK" not in report.stream_endpoint_line(db, now)
    state.set_meta("launch_ws_endpoint", "fluxrpc")
    state.set_meta("launch_ws_endpoint_since", repr(now - 60))
    state.event("rpcws_switch", {"from": "publicrpc", "to": "fluxrpc", "reason": "rate_limited"})
    state.bandwidth_add(now, "fluxrpc_ws", 8_100_000_000)
    line = report.stream_endpoint_line(db, now)
    assert "via fluxrpc" in line and "switches_24h=1" in line and "ON THE FLUXRPC FALLBACK" in line
    assert "8.10 of 10 GB" in line and "ALERT: FluxRPC at 81%" in line


def test_fluxrpc_ws_url_and_http_metering(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import httpx

    from rugwatch.collector.budget import Budget
    from rugwatch.collector.launchlogs import fluxrpc_ws_url
    from rugwatch.collector.rpc import Provider, RpcClient

    for name in ("FLUXRPC_WS_URL", "FLUXRPC_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    assert fluxrpc_ws_url() is None
    monkeypatch.setenv("FLUXRPC_API_KEY", "k")
    assert fluxrpc_ws_url() == "wss://ws.eu.fluxrpc.com?key=k"
    monkeypatch.setenv("FLUXRPC_WS_URL", " wss://dash.example/ws ")
    assert fluxrpc_ws_url() == "wss://dash.example/ws"

    state = State(":memory:")
    budget = Budget(state, make_cfg().collector.budget)
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": 5}))

    async def go() -> None:
        async with httpx.AsyncClient(transport=transport) as http_client:
            rpc = RpcClient([Provider("fluxrpc", "https://flux.test/?key=k", max_rps=1000)],
                            RawWriter(tmp_path, min_free_bytes=0), budget, client=http_client)
            assert (await rpc.call("getSlot", [], kind="accounts", purpose="filter")).ok

    asyncio.run(go())
    assert state.one("SELECT messages FROM bandwidth_ledger WHERE channel='fluxrpc_http'") == 1
    assert state.bandwidth_month("fluxrpc", time.time()) > 50


def test_threshold_change_recounts_the_24h_pass_rate(env_factory: Any) -> None:
    """Rule 5.1 can raise the threshold mid-day; the sampling rate must use the new threshold at once."""
    env: Env = env_factory(lambda m, p: None)
    now = time.time()
    rows = [(f"a{i}", 6.0, 1, 0) for i in range(300)] + [(f"b{i}", 20.0, 1, 0) for i in range(100)] \
        + [(f"c{i}", 1.0, 0, 1) for i in range(50)]
    env.state.db.executemany(
        "INSERT INTO tracking(mint,decided_at,launch_obs,filter_real_sol_300,filter_pass,eligible_rest,tracked,reason) "
        "VALUES(?,?,?,?,?,?,0,'not_tracked')", [(m, now - 100, now - 400, sol, fp, er) for m, sol, fp, er in rows])
    assert env.state.count_decided(now - 86400, "filter_pass") == 400  # as decided at the old 5 SOL threshold
    assert env.state.count_decided(now - 86400, "filter_pass", 15.0) == 100
    assert env.state.count_decided(now - 86400, "eligible_rest", 15.0) == 350
