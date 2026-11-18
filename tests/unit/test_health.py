"""Health checks, alert de-duplication, the daily summary and reminders (D-35)."""

from __future__ import annotations

import time
from typing import Any

from rugwatch.collector import health
from rugwatch.collector.health import Check, Probe, Reminder, evaluate, parse_reminders, run_checks
from rugwatch.collector.state import State
from tests.conftest import make_cfg

GOOD = Probe(service_active="active", ntp_synchronized="yes", clock_offset_s=0.001, disk_free_bytes=int(30e9))
NOON = time.struct_time((2026, 11, 19, 11, 0, 0, 3, 323, 0))
AFTER = time.struct_time((2026, 11, 19, 12, 30, 0, 3, 323, 0))


class Outbox:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.sent: list[tuple[str, str, int]] = []

    def __call__(self, title: str, message: str, priority: int, tags: list[str]) -> bool:
        self.sent.append((title, message, priority))
        return self.ok


def _state() -> dict[str, Any]:
    return health.load_state(health.Path("/nonexistent/health.json"))


def _healthy_db(now: float, launches_per_min: int = 30) -> State:
    st = State(":memory:")
    for m in range(70):
        st.heartbeat(now - m * 60, ws_connected=True, rpcws_connected=True)
    for i in range(60 * launches_per_min):
        t = now - 3590 + i * (3590 / (60 * launches_per_min))
        st.add_token(f"m{i}", t, {"signature": f"s{i}", "bondingCurveKey": f"c{i}"}, t + 60)
        st.add_launch_source(f"m{i}", "publicrpc", t, f"s{i}")
        if i % 4 == 0:
            st.add_launch_source(f"m{i}", "pumpportal", t, f"s{i}")
    st.event("coverage_check", {"coverage": 0.998}, ts=now - 3600)
    return st


def _by_name(checks: list[Check]) -> dict[str, Check]:
    return {c.name: c for c in checks}


def test_healthy_collector_passes_every_check() -> None:
    now = time.time()
    checks = run_checks(make_cfg(), _healthy_db(now).db, GOOD, now)
    assert [c.name for c in checks if not c.ok] == []
    names = _by_name(checks)
    assert {"launch_rate", "chain_share", "pumpportal_share", "coverage", "credits"} <= set(names)
    assert names["pumpportal_share"].value.startswith("25.0%")


def test_failures_are_detected() -> None:
    now = time.time()
    st = _healthy_db(now, launches_per_min=5)  # 300/h: below the 700/h floor
    st.event("stall_restart", {"gap_s": 600}, ts=now - 100)
    st.event("stall_restart", {"gap_s": 600}, ts=now - 200)
    st.event("stall_restart", {"gap_s": 600}, ts=now - 300)
    st.event("service_stop", {"service_result": "exit-code", "exit_status": "70"}, ts=now - 100)  # a stall exit
    st.event("rpcws_rate_limited", {"error": "InvalidStatus"}, ts=now - 50)
    st.set_meta("launch_ws_endpoint", "fluxrpc")
    st.event("coverage_check", {"coverage": 0.91}, ts=now - 60)
    bad = Probe(service_active="failed", ntp_synchronized="yes", clock_offset_s=2.5, disk_free_bytes=int(5e9))
    failing = {c.name for c in run_checks(make_cfg(), st.db, bad, now) if not c.ok}
    assert failing == {"service", "clock", "disk", "launch_rate", "stall_restarts", "rate_limited", "fallback",
                       "coverage"}
    st.event("service_stop", {"service_result": "watchdog", "exit_status": "6"}, ts=now - 10)  # a hung loop
    assert not _by_name(run_checks(make_cfg(), st.db, GOOD, now))["crashes"].ok


def test_launch_rate_uses_alive_minutes_only() -> None:
    """After the laptop was off, the hourly rate is scaled to the minutes the collector was alive."""
    now = time.time()
    st = State(":memory:")
    for m in range(25):
        st.heartbeat(now - m * 60, ws_connected=True, rpcws_connected=True)
    for i in range(500):  # 500 launches in 25 alive minutes = 1,200/h
        t = now - 1490 + i * 2.9
        st.add_token(f"m{i}", t, {"signature": f"s{i}", "bondingCurveKey": f"c{i}"}, t + 60)
    check = _by_name(run_checks(make_cfg(), st.db, GOOD, now))["launch_rate"]
    assert check.ok and check.value == "1,200"


def test_alert_after_two_failures_once_then_one_recovery() -> None:
    state, box = _state(), Outbox()
    bad = [Check("disk", "free disk", False, "5.0 GB", ">= 10 GB")]
    good = [Check("disk", "free disk", True, "30.0 GB", ">= 10 GB")]
    now = time.time()
    assert evaluate(state, bad, box, now, NOON, []) == []  # 1st failure: maybe a deploy restart
    assert evaluate(state, bad, box, now, NOON, []) == ["Collector alert: free disk"]
    assert evaluate(state, bad, box, now, NOON, []) == []  # no repeats
    title, message, priority = box.sent[0]
    assert "5.0 GB (normal: >= 10 GB)" in message and message.endswith(health.ACTION) and priority == 4
    assert evaluate(state, good, box, now, NOON, []) == []
    assert evaluate(state, good, box, now, NOON, []) == ["Recovered: free disk"]
    assert evaluate(state, good, box, now, NOON, []) == []


def test_failed_delivery_is_retried() -> None:
    state, box = _state(), Outbox(ok=False)
    bad = [Check("clock", "clock", False, "3 s", "2 s")]
    evaluate(state, bad, box, 0, NOON, [])
    evaluate(state, bad, box, 0, NOON, [])
    assert state["checks"]["clock"]["alerted"] is False
    box.ok = True
    assert evaluate(state, bad, box, 0, NOON, []) == ["Collector alert: clock"]


def test_daily_summary_once_after_1230_local() -> None:
    state, box = _state(), Outbox()
    ok = [Check("disk", "free disk", True, "30.0 GB", ">= 10 GB")]
    rem = [Reminder(time.time() + 3600, "Claude", "Day-3 checkpoint", "run the day-3 checkpoint")]
    assert evaluate(state, ok, box, time.time(), NOON, rem) == []
    assert evaluate(state, ok, box, time.time(), AFTER, rem) == ["Daily check: all OK"]
    assert "Next: Day-3 checkpoint" in box.sent[0][1]
    assert evaluate(state, ok, box, time.time(), AFTER, rem) == []
    warn = [Check("disk", "free disk", False, "5.0 GB", ">= 10 GB")]
    state["summary_day"] = ""
    assert evaluate(state, warn, box, time.time(), AFTER, []) == ["Daily check: 1 warning(s)"]


STATUS = """# Status

## Checkpoints and reminders

| Due (UTC) | Who | What | Say or do |
| --- | --- | --- | --- |
| 2026-11-19 12:00 | you | Solana Tracker key | Add SOLANATRACKER_API_KEY to .env. |
| 2026-11-20 12:15 | Claude | Day-3 checkpoint | run the day-3 checkpoint |

## Other

| 2026-11-21 10:00 | you | not a reminder | outside the section |
"""


def test_reminders_parsed_and_sent_once_when_due() -> None:
    rem = parse_reminders(STATUS)
    assert [(r.who, r.what) for r in rem] == [("you", "Solana Tracker key"), ("Claude", "Day-3 checkpoint")]
    assert rem[1].message() == ("Day-3 checkpoint due", "Open Claude Code and say 'run the day-3 checkpoint'.")
    state, box = _state(), Outbox()
    t = rem[1].due + 60  # both are due
    assert evaluate(state, [], box, t, NOON, rem) == ["To do: Solana Tracker key", "Day-3 checkpoint due"]
    assert evaluate(state, [], box, t + 600, NOON, rem) == []
    late = [Reminder(t - 8 * 86400, "you", "old", "x")]  # more than 7 days late: skipped
    assert evaluate(state, [], box, t, NOON, late) == []
