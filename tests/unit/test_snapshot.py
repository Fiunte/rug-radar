"""Snapshot batch: complete run on recorded transactions, deadline, one-shot scanners."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from rugwatch.chain import pump_layout as pl
from rugwatch.collector.scanners import build_scanners
from rugwatch.collector.snapshot import SnapshotCtx, Snapshotter
from tests.conftest import Env, account_value, curve_bytes, make_cfg

SOL = 10**9
T0 = 1_794_873_600.0


def _handler(fx: dict[str, Any], *, slow: set[str] | None = None, fail: set[str] | None = None,
             curve: bytes | None = None) -> Callable[[str, list[Any]], Any]:
    token = fx["token"]
    data = curve if curve is not None else curve_bytes(vq=36 * SOL, vt=9 * 10**14, rq=6 * SOL)

    def handler(method: str, params: list[Any]) -> Any:
        if fail and method in fail:
            return httpx.Response(500)
        if method == "getSignaturesForAddress":
            if params[0] == token["bondingCurveKey"]:
                return fx["sigs_newest_first"]
            return [{"signature": "creator-first-tx", "err": None, "slot": 1, "blockTime": 1}]
        if method == "getTransactionsForAddress":
            assert params[1]["sortOrder"] == "asc" and params[1]["filters"] == {"status": "succeeded"}
            order = [s["signature"] for s in reversed(fx["sigs_newest_first"])]
            return {"data": [fx["txs"][s] for s in order][: params[1]["limit"]], "paginationToken": None}
        if method == "getTransaction":
            return fx["txs"].get(params[0], {"slot": 1, "blockTime": 1, "meta": {"err": None, "fee": 5000},
                                             "transaction": {"signatures": [params[0]],
                                                             "message": {"accountKeys": [], "instructions": []}}})
        if method == "getTokenLargestAccounts":
            return {"context": {"slot": 3}, "value": [{"address": "a", "amount": "1"}]}
        if method == "getMultipleAccounts":
            if params[0] == [token["bondingCurveKey"]]:
                return {"context": {"slot": 4}, "value": [account_value(data)]}
            return {"context": {"slot": 4}, "value": [None] * len(params[0])}
        raise AssertionError(method)

    return handler


def _ctx(fx: dict[str, Any], benchmark: bool = False) -> SnapshotCtx:
    token = fx["token"]
    return SnapshotCtx(mint=token["mint"], launch_obs=T0, curve=token["bondingCurveKey"],
                       creators={token["traderPublicKey"]}, creator=token["traderPublicKey"],
                       creation_sig=token["signature"], benchmark=benchmark)


def _start(env: Env, fx: dict[str, Any]) -> None:
    env.state.put_tracking({"mint": fx["token"]["mint"], "decided_at": T0 + 300, "launch_obs": T0,
                            "bonding_curve": fx["token"]["bondingCurveKey"], "filter_pass": 1, "eligible_rest": 0,
                            "tracked": 1, "reason": "filter_pass"})


def _snapshotter(env: Env, scanner_http: httpx.AsyncClient | None = None) -> Snapshotter:
    scanners = build_scanners(list(env.cfg.scanners.list_)) if env.cfg.scanners.enabled else []
    return Snapshotter(env.cfg, env.state, env.rpc, env.writer, scanners, scanner_http or env.http)


def test_complete_snapshot(env_factory: Callable[..., Env], pump_fixture: dict[str, Any]) -> None:
    env = env_factory(_handler(pump_fixture))
    _start(env, pump_fixture)
    mint = pump_fixture["token"]["mint"]
    asyncio.run(_snapshotter(env).run(_ctx(pump_fixture)))

    row = env.state.q("SELECT * FROM tracking WHERE mint=?", (mint,))[0]
    assert row["snapshot_complete"] == 1 and row["missing_parts"] == ""
    assert row["p0"] == pytest.approx(pl.price_from_reserves(36 * SOL, 9 * 10**14))
    assert row["snapshot_slot"] == 4 and row["peak_price"] == row["p0"]
    assert row["next_price_due"] == row["snapshot_ts"] + 15
    assert row["next_balance_due"] == row["snapshot_ts"] + 60
    assert row["holders_due"] == T0 + 1800
    assert row["creation_slot"] == pump_fixture["txs"][pump_fixture["token"]["signature"]]["slot"]

    # snapshot_ts is the observed_at of the closing reserve read, which is the last RPC call of the batch.
    last = env.fake.calls[-1]
    assert last[1] == "getMultipleAccounts" and last[2][0] == [pump_fixture["token"]["bondingCurveKey"]]
    accounts = env.records("helius", "accounts")
    closing = [r for r in accounts if r["request"].get("part") == "closing_reserve_read"]
    assert len(closing) == 1 and closing[0]["purpose"] == "snapshot" and closing[0]["tick_s"] == 300
    from rugwatch.storage.raw import parse_iso
    assert parse_iso(closing[0]["observed_at"]) == pytest.approx(row["snapshot_ts"], abs=1e-5)
    early = [r for r in env.records("helius", "transaction") if r["request"].get("part") == "early_trades"]
    assert len(early) == 1 and early[0]["request"]["method"] == "getTransactionsForAddress"
    assert summary_of(env)["early_trades_source"] == "ascending"
    others = [r for src, kind in (("helius", "signatures"), ("helius", "transaction"), ("helius", "largest_accounts"))
              for r in env.records(src, kind)] + [r for r in accounts if r is not closing[0]]
    assert others and all(r["observed_at"] <= closing[0]["observed_at"] for r in others)

    balances = [r for r in accounts if r["request"].get("part") == "balances"]
    roles = {i["role"] for i in balances[0]["request"]["items"]}
    assert roles == {"creator", "early_buyer"} and balances[0]["request"]["method"] == "getMultipleAccounts"
    assert env.state.one("SELECT COUNT(*) FROM early_trades WHERE mint=?", (mint,)) == 5
    assert env.state.one("SELECT COUNT(*) FROM balance_accounts WHERE mint=?", (mint,)) == 3
    purposes = {r["purpose"] for r in env.records("helius", "signatures")}
    assert purposes == {"snapshot", "creator"}
    (summary,) = env.records("collector", "snapshot_summary")
    assert summary["payload"]["snapshot_complete"] is True and summary["payload"]["creator_cache_hit"] is False
    assert summary["payload"]["n_sigs_to_snapshot"] == len(pump_fixture["sigs_newest_first"])


def summary_of(env: Env) -> dict[str, Any]:
    payload: dict[str, Any] = env.records("collector", "snapshot_summary")[-1]["payload"]
    return payload


def test_page_walk_fallback_when_the_ascending_call_fails(env_factory: Callable[..., Env],
                                                          pump_fixture: dict[str, Any]) -> None:
    """Without the Helius-only method the batch follows the original spec: creation tx + oldest signatures."""
    env = env_factory(_handler(pump_fixture, fail={"getTransactionsForAddress"}))
    _start(env, pump_fixture)
    asyncio.run(_snapshotter(env).run(_ctx(pump_fixture)))
    row = env.state.q("SELECT * FROM tracking")[0]
    assert row["snapshot_complete"] == 1
    assert summary_of(env)["early_trades_source"] == "page_walk"
    assert env.fake.methods().count("getTransaction") == 5 + 1  # 5 curve transactions + the creator's oldest
    assert env.state.one("SELECT COUNT(*) FROM early_trades") == 5
    assert all(h == "helius.test" for h, m, _p in env.fake.calls if m == "getTransactionsForAddress")


def test_ascending_result_not_starting_at_creation_is_rejected(env_factory: Callable[..., Env],
                                                               pump_fixture: dict[str, Any]) -> None:
    base = _handler(pump_fixture)

    def handler(method: str, params: list[Any]) -> Any:
        result = base(method, params)
        if method == "getTransactionsForAddress":
            result["data"] = result["data"][1:]  # index lag: the creation transaction is missing
        return result

    env = env_factory(handler)
    _start(env, pump_fixture)
    asyncio.run(_snapshotter(env).run(_ctx(pump_fixture)))
    assert summary_of(env)["early_trades_source"] == "page_walk"
    assert env.state.q("SELECT * FROM tracking")[0]["snapshot_complete"] == 1


def test_truncated_pages_do_not_matter_when_ascending_works(env_factory: Callable[..., Env],
                                                            pump_fixture: dict[str, Any]) -> None:
    """Busy tokens have > 3 pages of (mostly failed) signatures by minute 5; early trades stay complete."""
    base = _handler(pump_fixture)
    page = [{"signature": f"spam{i}", "err": {"x": 1}, "slot": 9, "blockTime": 9} for i in range(1000)]

    def handler(method: str, params: list[Any]) -> Any:
        if method == "getSignaturesForAddress" and params[0] == pump_fixture["token"]["bondingCurveKey"]:
            return page
        return base(method, params)

    env = env_factory(handler)
    _start(env, pump_fixture)
    asyncio.run(_snapshotter(env).run(_ctx(pump_fixture)))
    row = env.state.q("SELECT * FROM tracking")[0]
    assert row["snapshot_complete"] == 1 and env.state.one("SELECT COUNT(*) FROM early_trades") == 5
    payload = summary_of(env)
    assert payload["sigs_truncated"] is True and payload["n_sigs_to_snapshot"] == 3000
    assert payload["early_trades_truncated"] is False
    assert env.fake.methods().count("getSignaturesForAddress") == 3 + 1  # 3 curve pages + 1 creator page

    env2 = env_factory(lambda m, p: httpx.Response(500) if m == "getTransactionsForAddress" else handler(m, p))
    _start(env2, pump_fixture)
    asyncio.run(_snapshotter(env2).run(_ctx(pump_fixture)))
    row2 = env2.state.q("SELECT * FROM tracking")[0]
    assert row2["missing_parts"] == "early_trades" and summary_of(env2)["early_trades_truncated"] is True


def test_creator_history_is_cached(env_factory: Callable[..., Env], pump_fixture: dict[str, Any]) -> None:
    env = env_factory(_handler(pump_fixture))
    _start(env, pump_fixture)
    snap = _snapshotter(env)
    asyncio.run(snap.run(_ctx(pump_fixture)))
    first = len([c for c in env.fake.calls if c[2][0] == pump_fixture["token"]["traderPublicKey"]])
    asyncio.run(snap.run(_ctx(pump_fixture)))
    second = len([c for c in env.fake.calls if c[2][0] == pump_fixture["token"]["traderPublicKey"]])
    assert first == 1 and second == 1  # second token of the same creator: no new history calls
    assert env.records("collector", "snapshot_summary")[-1]["payload"]["creator_cache_hit"] is True


def test_failed_parts_are_listed_and_snapshot_still_closes(env_factory: Callable[..., Env],
                                                           pump_fixture: dict[str, Any]) -> None:
    env = env_factory(_handler(pump_fixture, fail={"getTransactionsForAddress", "getTransaction",
                                                   "getTokenLargestAccounts"}))
    _start(env, pump_fixture)
    asyncio.run(_snapshotter(env).run(_ctx(pump_fixture)))
    row = env.state.q("SELECT * FROM tracking")[0]
    assert row["snapshot_complete"] == 0
    assert row["missing_parts"] == "early_trades,creator_history,largest_holders,balances"
    assert row["snapshot_ts"] is not None and row["p0"] is not None  # the token stays tracked, flagged
    assert row["next_price_due"] is not None and row["next_balance_due"] is None
    assert env.records("collector", "snapshot_summary")[0]["status"] == "error"


def test_no_closing_read_means_no_snapshot(env_factory: Callable[..., Env], pump_fixture: dict[str, Any]) -> None:
    env = env_factory(_handler(pump_fixture, curve=b"\0" * 166))
    _start(env, pump_fixture)
    asyncio.run(_snapshotter(env).run(_ctx(pump_fixture)))
    row = env.state.q("SELECT * FROM tracking")[0]
    assert row["snapshot_ts"] is None and row["p0"] is None and row["flags"] == "snapshot_failed"
    assert row["next_price_due"] is None and "closing_reserve_read" in row["missing_parts"]
    assert row["tracked"] == 1  # kept in tracking with flags, never dropped silently


def test_parts_past_the_deadline_are_null(env_factory: Callable[..., Env], pump_fixture: dict[str, Any]) -> None:
    cfg = make_cfg({"tracking": {"snapshot_batch": {"deadline_s": 3.3}}})  # 0.3 s for the parts
    base = _handler(pump_fixture)
    env = env_factory(base, cfg)
    _start(env, pump_fixture)

    async def slow_post(url: Any, json: dict[str, Any]) -> httpx.Response:  # noqa: A002
        if json["method"] == "getTokenLargestAccounts":
            await asyncio.sleep(30)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": json["id"],
                                         "result": base(json["method"], json["params"])})

    env.http.post = slow_post  # type: ignore[method-assign,assignment]
    asyncio.run(asyncio.wait_for(_snapshotter(env).run(_ctx(pump_fixture)), 5))
    row = env.state.q("SELECT * FROM tracking")[0]
    assert row["missing_parts"] == "largest_holders" and row["snapshot_complete"] == 0
    assert row["snapshot_ts"] is not None
    (rec,) = env.records("helius", "largest_accounts")
    assert rec["status"] == "timeout"  # the abandoned call is still written to raw


# ----------------------------------------------------------------- scanners
def _scanner_transport(calls: list[str], slow_host: str | None = None) -> httpx.MockTransport:
    async def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        if request.url.host == slow_host:
            await asyncio.sleep(30)
        if "rugcheck" in request.url.host:
            return httpx.Response(200, json={"score": 501, "score_normalised": 12, "risks": [{"name": "X"}]})
        if "gopluslabs" in request.url.host:
            return httpx.Response(200, json={"code": 1, "result": {"m": {"mintable": {"status": "1"}}}})
        return httpx.Response(401)

    return httpx.MockTransport(handle)


def test_scanners_are_queried_once_per_mint(env_factory: Callable[..., Env], pump_fixture: dict[str, Any]) -> None:
    env = env_factory(_handler(pump_fixture), make_cfg({"scanners": {"enabled": True}}))
    _start(env, pump_fixture)
    calls: list[str] = []
    snap = _snapshotter(env, httpx.AsyncClient(transport=_scanner_transport(calls)))
    asyncio.run(snap.run(_ctx(pump_fixture, benchmark=True)))
    assert sorted(calls) == ["api.gopluslabs.io", "api.rugcheck.xyz", "data.solanatracker.io"]
    row = env.state.q("SELECT * FROM tracking")[0]
    assert row["missing_parts"] == "scanners"  # Solana Tracker failed (no key): the part is incomplete
    done = {r["scanner"]: r["status"] for r in env.state.q("SELECT * FROM scanner_done")}
    assert done == {"rugcheck": "ok", "goplus": "ok", "solanatracker": "error"}
    recs = {s: env.records(s, "scanner") for s in done}
    assert all(len(v) == 1 for v in recs.values())
    assert recs["solanatracker"][0]["payload"] == {"error": "http_401"}
    assert recs["rugcheck"][0]["purpose"] == "scanner" and recs["rugcheck"][0]["tick_s"] == 300
    summary_ts = env.records("collector", "snapshot_summary")[0]["payload"]["snapshot_ts"]
    assert all(v[0]["observed_at"] <= summary_ts for v in recs.values())  # never late by construction

    asyncio.run(snap.run(_ctx(pump_fixture, benchmark=True)))  # a second batch must not re-query, even failures
    assert len(calls) == 3
    assert all(len(env.records(s, "scanner")) == 1 for s in done)


def test_scanners_not_queried_for_non_benchmark_tokens(env_factory: Callable[..., Env],
                                                       pump_fixture: dict[str, Any]) -> None:
    env = env_factory(_handler(pump_fixture), make_cfg({"scanners": {"enabled": True}}))
    _start(env, pump_fixture)
    calls: list[str] = []
    asyncio.run(_snapshotter(env, httpx.AsyncClient(transport=_scanner_transport(calls))).run(_ctx(pump_fixture)))
    assert calls == [] and env.state.q("SELECT * FROM tracking")[0]["snapshot_complete"] == 1


def test_scanner_slower_than_the_deadline_is_recorded_as_timeout(env_factory: Callable[..., Env],
                                                                 pump_fixture: dict[str, Any]) -> None:
    cfg = make_cfg({"scanners": {"enabled": True}, "tracking": {"snapshot_batch": {"deadline_s": 3.3}}})
    env = env_factory(_handler(pump_fixture), cfg)
    _start(env, pump_fixture)
    calls: list[str] = []
    transport = _scanner_transport(calls, slow_host="api.rugcheck.xyz")
    asyncio.run(asyncio.wait_for(
        _snapshotter(env, httpx.AsyncClient(transport=transport)).run(_ctx(pump_fixture, benchmark=True)), 5))
    (rec,) = env.records("rugcheck", "scanner")
    assert rec["status"] == "timeout"
    assert env.state.one("SELECT status FROM scanner_done WHERE scanner='rugcheck'") == "timeout"
    assert "scanners" in env.state.q("SELECT * FROM tracking")[0]["missing_parts"]
