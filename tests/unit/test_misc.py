"""Scanner normalisation, the PumpPortal listener, reports."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from rugwatch.collector import report
from rugwatch.collector.pumpportal import PumpPortalListener, classify
from rugwatch.collector.scanners import build_scanners, query_scanner
from rugwatch.collector.scanners.goplus import GoPlus
from rugwatch.collector.scanners.rugcheck import RugCheck
from rugwatch.collector.scanners.solanatracker import SolanaTracker
from rugwatch.collector.state import State
from rugwatch.storage.raw import RawWriter, iter_raw
from tests.conftest import SECRET, make_cfg

SOL = 10**9


# ----------------------------------------------------------------- scanners
def test_rugcheck_normalise() -> None:
    s = RugCheck().normalise({"score": 101, "score_normalised": 7, "risks": [{"name": "Mutable metadata"}]})
    assert (s.score_raw, s.score_norm, s.flags) == (101.0, 7.0, ["Mutable metadata"])
    assert RugCheck().normalise({"error": "x"}).score_norm is None
    assert RugCheck().normalise(None).score_norm is None


def test_goplus_normalise_counts_risk_flags() -> None:
    token = {"mintable": {"status": "1"}, "freezable": {"status": "0"}, "metadata_mutable": {"status": "1"},
             "transfer_fee": {}, "transfer_hook": [{"address": "x"}], "none_transferable": "0",
             "default_account_state": "2", "creators": [{"address": "c", "malicious_address": 1}],
             "trusted_token": 0}
    s = GoPlus().normalise({"code": 1, "result": {"MINT": token}})
    assert s.flags == ["default_account_frozen", "malicious_creator", "metadata_mutable", "mintable", "transfer_hook"]
    assert s.score_raw == s.score_norm == 5.0
    clean = GoPlus().normalise({"code": 1, "result": {"MINT": {"mintable": {"status": "0"}}}})
    assert clean.score_norm == 0.0 and clean.flags == []
    assert GoPlus().normalise({"code": 1, "result": {}}).score_norm is None  # token unknown to GoPlus: no score


def test_solanatracker_normalise() -> None:
    s = SolanaTracker().normalise({"risk": {"score": 8, "rugged": True, "risks": [{"name": "Bundled"}]}})
    assert (s.score_raw, s.score_norm, s.flags) == (8.0, 8.0, ["Bundled", "rugged"])
    assert SolanaTracker().normalise({}).score_norm is None


def test_scanner_requests_and_key_hygiene(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOLANATRACKER_API_KEY", SECRET)
    monkeypatch.delenv("RUGCHECK_API_KEY", raising=False)
    names = [s.name for s in build_scanners(["rugcheck", "goplus", "solanatracker"])]
    assert names == ["rugcheck", "goplus", "solanatracker"]
    url, headers = SolanaTracker().request("MINT")
    assert url == "https://data.solanatracker.io/tokens/MINT" and headers["x-api-key"] == SECRET
    assert SECRET not in url
    url, headers = RugCheck().request("MINT")
    assert url.endswith("/v1/tokens/MINT/report/summary") and "X-API-KEY" not in headers
    assert "contract_addresses=MINT" in GoPlus().request("MINT")[0]


def test_query_scanner_statuses() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/ok/report/summary"):
            return httpx.Response(200, json={"score": 1})
        if request.url.path.endswith("/bad/report/summary"):
            return httpx.Response(200, text="<html>")
        if request.url.path.endswith("/slow/report/summary"):
            raise httpx.ReadTimeout("slow")
        return httpx.Response(429)

    async def go() -> list[tuple[str, Any]]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            return [((r := await query_scanner(RugCheck(), http, m, 1.0)).status, r.payload)
                    for m in ("ok", "bad", "slow", "limited")]

    assert asyncio.run(go()) == [("ok", {"score": 1}), ("error", {"error": "bad_json"}),
                                 ("timeout", {"error": "timeout"}), ("error", {"error": "http_429"})]


# --------------------------------------------------------------- pumpportal
CREATE = {"signature": "sig", "mint": "MINT", "traderPublicKey": "creator", "txType": "create", "pool": "pump",
          "bondingCurveKey": "curve", "name": "n", "symbol": "s", "uri": "u", "initialBuy": 1.0, "solAmount": 0.5}


def test_classify() -> None:
    assert classify(CREATE) == "create"
    assert classify({**CREATE, "pool": "bonk"}) == "create_other"
    assert classify({"txType": "migrate", "mint": "M", "pool": "pump-amm"}) == "migrate"
    assert classify({"message": "Successfully subscribed"}) == "other"


def test_listener_logs_everything_and_tracks_only_pump_launches(tmp_path: Path) -> None:
    cfg = make_cfg()
    state = State(":memory:")
    writer = RawWriter(tmp_path, min_free_bytes=0)
    listener = PumpPortalListener(cfg.collector.pumpportal, cfg.activity_filter, state, writer)
    t = 1_794_873_600.0
    listener.handle(CREATE, t)
    listener.handle(CREATE, t + 1)  # duplicate message: one token
    listener.handle({**CREATE, "mint": "OTHER", "pool": "bonk", "bondingCurveKey": None}, t + 2)
    listener.handle({"message": "Subscribed"}, t + 3)
    state.put_tracking({"mint": "MINT", "decided_at": t, "launch_obs": t, "filter_pass": 1, "eligible_rest": 0,
                        "tracked": 1, "reason": "filter_pass"})
    listener.handle({"txType": "migrate", "mint": "MINT", "pool": "pump-amm", "signature": "x"}, t + 900)
    writer.flush()
    row = state.q("SELECT * FROM tokens")
    assert len(row) == 1 and row[0]["bonding_curve"] == "curve" and row[0]["next_tick_due"] == t + 60
    assert row[0]["launch_obs"] == t and row[0]["launcher"] == "creator"
    new = list(iter_raw(tmp_path, "pumpportal", "new_token"))
    assert len(new) == 3 and all(r["purpose"] == "launch_log" for r in new)
    assert new[0]["observed_at"] == "2026-11-17T00:00:00.000000Z" and new[0]["payload"] == CREATE
    assert len(list(iter_raw(tmp_path, "pumpportal", "migration"))) == 1
    assert state.one("SELECT graduated_ts FROM tracking WHERE mint='MINT'") == t + 900


def test_state_prune_keeps_tracked_tokens_and_recent_rows() -> None:
    state = State(":memory:")
    for mint, age in (("old", 30), ("old_tracked", 30), ("new", 1)):
        state.add_token(mint, 1000.0 - age, {"bondingCurveKey": "c"}, 0)
        state.advance_tick(mint, 3, None)
        state.put_filter_tick(mint, 300, "ok")
        state.put_tracking({"mint": mint, "decided_at": 1000.0 - age, "launch_obs": 1000.0 - age, "filter_pass": 0,
                            "eligible_rest": 1, "tracked": int(mint == "old_tracked"), "reason": "x"})
    assert state.prune(1000.0 - 10) == 2
    assert [r["mint"] for r in state.q("SELECT mint FROM tokens")] == ["new"]
    assert [r["mint"] for r in state.q("SELECT mint FROM filter_ticks")] == ["new"]
    assert sorted(r["mint"] for r in state.q("SELECT mint FROM tracking")) == ["new", "old_tracked"]


# ------------------------------------------------------------------ reports
def test_ntp_offset_parse() -> None:
    assert report.ntp_offset_s("       Offset: -6.745ms\n") == pytest.approx(-0.006745)
    assert report.ntp_offset_s("Offset: +2.5s") == 2.5 and report.ntp_offset_s("Offset: 120us") == pytest.approx(1.2e-4)
    assert report.ntp_offset_s("no sync") is None


def test_quantiles() -> None:
    q = report.quantiles([float(i) for i in range(1, 101)])
    assert q["p50"] == 51.0 and q["p99"] == 100.0
    assert report.quantiles([]) == {}


def test_reports_run_on_collector_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUGWATCH_DATA_BASE", str(tmp_path))
    cfg = make_cfg()
    assert report.report_day1(cfg) == ["no collector state yet"]
    assert any("never run" in line for line in report.status(cfg))

    state = State(tmp_path / cfg.storage.state)
    now = time.time()
    t0 = now - 7200
    for i in range(200):  # 200 launches in 2 h; 10 pass 5 SOL, 4 pass 10 SOL, 2 graduated, 1 not SOL-quoted
        mint = f"m{i}"
        state.add_token(mint, t0 + i * 36, {"bondingCurveKey": f"c{i}"}, t0 + i * 36 + 60)
        real = 12 * SOL if i < 4 else 6 * SOL if i < 10 else 0 if i < 100 else SOL
        for tick in (60, 180, 300):
            state.put_filter_tick(mint, tick, "ok", t0 + i * 36 + tick, 1, 30 * SOL + real, 10**15, real, 1,
                                  complete=10 <= i < 12)
        state.advance_tick(mint, 3, None)
    state.x("UPDATE tokens SET quote_mint='pumpCmXqMfrsAkQ5r49WcJnRayYRqmXz6ae8H7H9Dfn' WHERE mint='m3'")
    for i in range(10):
        state.put_tracking({"mint": f"m{i}", "decided_at": t0 + i, "launch_obs": t0, "filter_pass": 1,
                            "eligible_rest": 0, "tracked": 1, "reason": "filter_pass", "snapshot_complete": int(i < 8),
                            "missing_parts": "" if i < 8 else "creator_history", "creation_block_time": int(t0) - 1})
        state.x("INSERT INTO early_trades VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"m{i}", f"s{i}", 0, 1, 1, "u", "a", 1, SOL, 5, 12_500_000, 125.0, 500_000, 1_000_000 * (i % 2), 0))
    from rugwatch.collector.budget import hour_key
    for h in range(3):
        state.ledger_add(hour_key(t0 + h * 3600), "helius", "filter", 1, False)
        state.x("UPDATE credit_ledger SET calls=240, credits=240 WHERE purpose='filter'")
        state.ledger_add(hour_key(t0 + h * 3600), "helius", "snapshot", 1, False)
    state.x("UPDATE credit_ledger SET calls=100, credits=100 WHERE purpose='snapshot'")
    state.heartbeat(now, True)
    state.event("ws_drop", {"reason": "stale"})
    state.event("ws_connect", {"gap_s": 12.5})
    state.x("INSERT INTO scanner_done VALUES('m0','rugcheck',?, 'ok', 800)", (now,))
    state.x("INSERT INTO scanner_done VALUES('m1','rugcheck',?, 'timeout', 25000)", (now,))
    state.close()

    day1 = "\n".join(report.report_day1(cfg))
    scale = 24 / (199 * 36 / 3600)
    assert f"| 5 | {9 * scale:,.0f} | {9 * scale:,.0f} | {9 * scale:,.0f} |" in day1  # m3 is not SOL-quoted
    assert f"| 10 | {3 * scale:,.0f} |" in day1
    assert "graduated before the tick (outside the universe): 2 (1.0%)" in day1
    assert "empty curve, real SOL = 0 (dead; outside the universe): 88 (44.0%)" in day1
    assert "quoted in another token, not SOL (outside the universe): 1 (0.5%)" in day1
    assert "Rule 5.1: smallest grid value with <= 500 passes/day at 300 s is **4 SOL**" in day1
    assert "snapshots complete: 8 of 10" in day1 and "missing creator_history: 2" in day1
    assert "websocket drops: 1" in day1

    week1 = "\n".join(report.report_week1(cfg))
    assert "fee bps per side (fee-paying trades): p10=125.0" in week1
    assert "credits per tracked token-day c: 30.0" in week1
    assert "rugcheck: queried=2 on-time coverage=50.0%" in week1
    assert "share with a tip: 50.0%" in week1
    status = "\n".join(report.status(cfg))
    assert "launches=200" in status and "websocket drops: total=1" in status


# ----------------------------------------------------------------- watchdog
def test_stall_watchdog_logs_and_raises(tmp_path: Path) -> None:
    from rugwatch.collector import main

    cfg = make_cfg()
    state = State(":memory:")
    listener = PumpPortalListener(cfg.collector.pumpportal, cfg.activity_filter, state,
                                  RawWriter(tmp_path, min_free_bytes=0))
    t = time.time()
    listener.handle(CREATE, t)
    main.watchdog_tick(state, listener, t + main.STALL_RESTART_S - 1)  # quiet but within the limit
    assert state.one("SELECT COUNT(*) FROM events WHERE kind='stall_restart'") == 0
    listener.handle({**CREATE, "pool": "bonk", "mint": "B"}, t + 300)  # other launchpads do not count
    with pytest.raises(main.StallError):
        main.watchdog_tick(state, listener, t + main.STALL_RESTART_S + 1)
    detail = state.one("SELECT detail FROM events WHERE kind='stall_restart'")
    assert '"gap_s": 601.0' in detail


def test_sd_notify_sends_to_the_systemd_socket(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import socket

    from rugwatch.collector import main

    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    assert main.sd_notify("WATCHDOG=1") is False
    path = str(tmp_path / "notify.sock")
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as server:
        server.bind(path)
        monkeypatch.setenv("NOTIFY_SOCKET", path)
        assert main.sd_notify("WATCHDOG=1") is True
        assert server.recv(64) == b"WATCHDOG=1"


def test_record_stop_logs_the_systemd_reason(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from rugwatch.collector import main

    monkeypatch.setenv("RUGWATCH_DATA_BASE", str(tmp_path))
    monkeypatch.setenv("SERVICE_RESULT", "watchdog")
    monkeypatch.setenv("EXIT_CODE", "killed")
    monkeypatch.setenv("EXIT_STATUS", "ABRT")
    cfg = make_cfg()
    assert main.record_stop(cfg) == 0
    state = State(tmp_path / cfg.storage.state)
    assert '"service_result": "watchdog"' in state.one("SELECT detail FROM events WHERE kind='service_stop'")
    state.close()
    assert "service stops 10d: 1 watchdog/ABRT=1  stall_restarts 10d: 0" in "\n".join(report.status(cfg))


def test_snapshot_state_copies_a_live_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from rugwatch.collector import main

    monkeypatch.setenv("RUGWATCH_DATA_BASE", str(tmp_path))
    cfg = make_cfg()
    live = State(tmp_path / cfg.storage.state)  # stays open, in WAL mode, like the collector
    live.event("start", {})
    assert main.snapshot_state(cfg) == 0
    backup = State((tmp_path / cfg.storage.state).parent / "backup" / "collector.sqlite")
    assert backup.one("SELECT kind FROM events") == "start"
    backup.close()
    live.close()
