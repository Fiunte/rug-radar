"""Health checks, phone alerts and reminders, run every 10 minutes by a systemd user timer (D-35).

- Each check compares one value that `make status` shows with a fixed threshold. An alert goes out once a
  check has failed on 2 runs in a row; one recovery message once it has passed on 2 runs in a row; nothing
  in between. A deploy restart therefore never alerts.
- Every run pings an outside dead-man's switch (healthchecks.io, `HEALTHCHECKS_PING_URL`). It alerts by
  itself when the pings stop: laptop off, asleep, crashed or offline, which this laptop cannot report.
- A daily summary at 12:30 local time ("all OK" or the warnings) shows the alerting itself is alive.
- Reminders: each row of the "Checkpoints and reminders" table in docs/status.md is sent once, when due.
- Delivery: ntfy (`NTFY_TOPIC`, optional `NTFY_SERVER`). Messages carry numbers only: no keys, no URLs,
  no project name. The topic and the ping URL are secrets and are never printed or logged.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

from rugwatch.collector import report
from rugwatch.collector.budget import hour_key
from rugwatch.collector.coverage import COVERAGE_ALERT
from rugwatch.collector.launchlogs import FLUXRPC_ALERT_SHARE, FLUXRPC_MONTHLY_LIMIT_BYTES
from rugwatch.collector.main import state_path
from rugwatch.collector.state import month_key
from rugwatch.config import REPO_ROOT, Config, load_config

log = logging.getLogger("rugwatch.health")

# Thresholds (D-35). Normal ranges are from 2026-11-17/18; recheck at day 7 (report-week1).
FAILS_TO_ALERT = 2
PASSES_TO_RECOVER = 2
HEARTBEAT_MAX_S = 300.0          # the collector writes one heartbeat a minute
LAUNCH_AGE_MAX_S = 900.0         # the stall watchdog restarts after 600 s without a launch
LAUNCH_RATE_MIN_PER_H = 700.0    # lowest full hour so far: 1,323
RATE_MIN_ALIVE_MIN = 20          # the hourly rate needs at least 20 alive minutes in the last hour
CHAIN_SHARE_MIN = 0.90           # launches seen by the create-log stream; normal 99.8%
PUMPPORTAL_SHARE_MIN = 0.10      # normal 23-26%; PumpPortal also carries the migrations
COVERAGE_MAX_AGE_S = 26 * 3600.0  # the coverage check runs daily
PROJECTION_MAX = 850_000         # the throttle holds the projection near the 800k target
DISK_MIN_BYTES = 10e9            # raw data grows about 0.1 GB a day
STALL_RESTARTS_MAX_24H = 2       # alert from the 3rd stall restart in 24 h
INTERNAL_ERRORS_MAX_24H = 4      # step/task errors; alert from the 5th in 24 h
RPC_ERROR_SHARE_MAX = 0.05       # over the current and previous hour, if >= 100 calls
SUMMARY_AT = (12, 30)            # local time
REMINDER_MAX_LATE_S = 7 * 86400.0
STATUS_MD = REPO_ROOT / "docs" / "status.md"
REMINDER_SECTION = "## Checkpoints and reminders"
ACTION = "Open Claude Code and say 'run make status'."
NTFY_DEFAULT_SERVER = "https://ntfy.sh"
STALL_EXIT_STATUS = "70"


@dataclass(frozen=True)
class Check:
    name: str
    label: str
    ok: bool
    value: str
    normal: str


@dataclass(frozen=True)
class Probe:
    """System facts that do not come from the collector database."""
    service_active: str
    ntp_synchronized: str
    clock_offset_s: float | None
    disk_free_bytes: int


def probe() -> Probe:
    return Probe(service_active=report._cmd("systemctl", "--user", "is-active", report.SERVICE) or "unknown",
                 ntp_synchronized=report._cmd("timedatectl", "show", "-p", "NTPSynchronized", "--value"),
                 clock_offset_s=report.ntp_offset_s(report._cmd("timedatectl", "timesync-status")),
                 disk_free_bytes=shutil.disk_usage(REPO_ROOT).free)


def _one(db: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> Any:
    row = db.execute(sql, params).fetchone()
    return None if row is None else row[0]


def _count_events(db: sqlite3.Connection, kind: str, since: float) -> int:
    return int(_one(db, "SELECT COUNT(*) FROM events WHERE kind=? AND ts>=?", (kind, since)) or 0)


# ------------------------------------------------------------------ checks
def run_checks(cfg: Config, db: sqlite3.Connection | None, p: Probe, now: float) -> list[Check]:
    out = [Check("service", "collector service", p.service_active == "active", p.service_active, "active")]
    off = p.clock_offset_s
    clock_ok = p.ntp_synchronized == "yes" and off is not None and abs(off) <= report.CLOCK_ALERT_S
    out.append(Check("clock", "clock", clock_ok,
                     f"synced={p.ntp_synchronized}, offset {'unknown' if off is None else f'{off * 1000:+.1f} ms'}",
                     "synced, offset within 2 s (seen: under 10 ms)"))
    out.append(Check("disk", "free disk", p.disk_free_bytes >= DISK_MIN_BYTES, f"{p.disk_free_bytes / 1e9:.1f} GB",
                     f">= {DISK_MIN_BYTES / 1e9:.0f} GB (raw data grows ~0.1 GB/day)"))
    if db is None:
        return [*out, Check("state", "collector database", False, "missing or unreadable", "present")]
    out += _launch_checks(db, now)
    out += _rpc_checks(cfg, db, now)
    out += _restart_checks(db, now)
    return out


def _launch_checks(db: sqlite3.Connection, now: float) -> list[Check]:
    out = []
    beat = _one(db, "SELECT MAX(minute) FROM heartbeats")
    age = None if beat is None else now - float(beat) * 60
    out.append(Check("heartbeat", "collector heartbeat", age is not None and age <= HEARTBEAT_MAX_S,
                     "never" if age is None else f"{age:.0f} s old", f"under {HEARTBEAT_MAX_S:.0f} s (once a minute)"))
    last = _one(db, "SELECT MAX(launch_obs) FROM tokens")
    lage = None if last is None else max(0.0, now - float(last))
    out.append(Check("launch_age", "last launch seen", lage is not None and lage <= LAUNCH_AGE_MAX_S,
                     "never" if lage is None else f"{lage:.0f} s ago",
                     f"under {LAUNCH_AGE_MAX_S:.0f} s (usually seconds)"))
    alive = int(_one(db, "SELECT COUNT(*) FROM heartbeats WHERE minute>=?", (int((now - 3600) // 60),)) or 0)
    n = int(_one(db, "SELECT COUNT(*) FROM tokens WHERE launch_obs>=?", (now - 3600,)) or 0)
    if alive >= RATE_MIN_ALIVE_MIN:
        rate = n * 60.0 / min(alive, 60)
        out.append(Check("launch_rate", "launches per hour", rate >= LAUNCH_RATE_MIN_PER_H, f"{rate:,.0f}",
                         f">= {LAUNCH_RATE_MIN_PER_H:,.0f} (seen: 1,323-2,931)"))
        if n >= 100:
            by_src = dict(db.execute(
                "SELECT CASE WHEN s.source='pumpportal' THEN 'pumpportal' ELSE 'chain' END, COUNT(DISTINCT s.mint) "
                "FROM launch_sources s JOIN tokens t USING(mint) WHERE t.launch_obs>=? GROUP BY 1",
                (now - 3600,)).fetchall())
            chain, pp = by_src.get("chain", 0) / n, by_src.get("pumpportal", 0) / n
            out.append(Check("chain_share", "launches seen by the create-log stream", chain >= CHAIN_SHARE_MIN,
                             f"{chain:.1%} of {n:,} in the last hour", f">= {CHAIN_SHARE_MIN:.0%} (seen: 99.8%)"))
            out.append(Check("pumpportal_share", "launches seen by PumpPortal", pp >= PUMPPORTAL_SHARE_MIN,
                             f"{pp:.1%} of {n:,} in the last hour", f">= {PUMPPORTAL_SHARE_MIN:.0%} (seen: 20-26%)"))
    row = db.execute("SELECT ts, detail FROM events WHERE kind='coverage_check' ORDER BY ts DESC LIMIT 1").fetchone()
    since = _one(db, "SELECT value FROM meta WHERE key='publicrpc_since'")
    running_s = now - float(since) if since else 0.0
    if row is not None:
        cov = report._json(row[1]).get("coverage")
        cage = now - float(row[0])
        ok = cage <= COVERAGE_MAX_AGE_S and (cov is None or cov >= COVERAGE_ALERT)
        value = f"{'n/a' if cov is None else f'{cov:.1%}'}, checked {cage / 3600:.0f} h ago"
        out.append(Check("coverage", "launch coverage", ok, value,
                         f">= {COVERAGE_ALERT:.0%}, daily (seen: 99.6-99.8%)"))
    elif running_s > COVERAGE_MAX_AGE_S:
        out.append(Check("coverage", "launch coverage", False, "no check has run", "one check a day"))
    return out


def _rpc_checks(cfg: Config, db: sqlite3.Connection, now: float) -> list[Check]:
    out = []
    proj = report.projection(db, now)
    level = int(_one(db, "SELECT value FROM meta WHERE key='throttle_level'") or 0)
    max_level = len(cfg.collector.budget.throttle_order)
    hard = db.execute("SELECT kind FROM events WHERE kind IN ('hard_stop_on','hard_stop_off') "
                      "ORDER BY ts DESC LIMIT 1").fetchone()
    hard_on = hard is not None and hard[0] == "hard_stop_on"
    ok = (proj is None or proj <= PROJECTION_MAX) and level < max_level and not hard_on
    value = (f"{'n/a' if proj is None else f'{proj:,.0f}'}/30 d, throttle {level} of {max_level}"
             + (", HARD STOP" if hard_on else ""))
    out.append(Check("credits", "Helius credit projection", ok, value,
                     f"<= {PROJECTION_MAX:,} and throttle below {max_level} (seen: 715k-815k, throttle 0-2)"))
    calls, errors = db.execute("SELECT COALESCE(SUM(calls),0), COALESCE(SUM(errors),0) FROM credit_ledger "
                               "WHERE hour>=?", (hour_key(now - 3600),)).fetchone()
    if calls >= 100:
        share = errors / calls
        out.append(Check("rpc_errors", "RPC error rate", share <= RPC_ERROR_SHARE_MAX,
                         f"{share:.1%} of {calls:,} calls", f"<= {RPC_ERROR_SHARE_MAX:.0%} (seen: ~0%)"))
    limited = _count_events(db, "rpcws_rate_limited", now - 86400)
    out.append(Check("rate_limited", "public RPC rate limiting", limited == 0, f"{limited} in 24 h", "0"))
    endpoint = _one(db, "SELECT value FROM meta WHERE key='launch_ws_endpoint'") or "publicrpc"
    out.append(Check("fallback", "create-log stream endpoint", endpoint == "publicrpc", str(endpoint),
                     "publicrpc (fluxrpc = the automatic fallback is in use)"))
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    used = 0
    if "bandwidth_ledger" in tables:
        used = int(_one(db, "SELECT COALESCE(SUM(bytes),0) FROM bandwidth_ledger WHERE month=? AND channel LIKE "
                            "'fluxrpc%'", (month_key(now),)) or 0)
    share = used / FLUXRPC_MONTHLY_LIMIT_BYTES
    out.append(Check("fluxrpc_bandwidth", "FluxRPC bandwidth this month", share < FLUXRPC_ALERT_SHARE,
                     f"{used / 1e9:.2f} GB ({share:.0%})", f"under {FLUXRPC_ALERT_SHARE:.0%} of 10 GB (seen: 0%)"))
    return out


def _restart_checks(db: sqlite3.Connection, now: float) -> list[Check]:
    day = now - 86400
    crashes = _count_events(db, "crash", day) + _count_events(db, "disk_full", day)
    for (detail,) in db.execute("SELECT detail FROM events WHERE kind='service_stop' AND ts>=?", (day,)):
        d = report._json(detail)
        if d.get("service_result") not in (None, "success") and d.get("exit_status") != STALL_EXIT_STATUS:
            crashes += 1
    stalls = _count_events(db, "stall_restart", day)
    errors = _count_events(db, "step_error", day) + _count_events(db, "task_error", day)
    return [
        Check("crashes", "collector crashes", crashes == 0, f"{crashes} in 24 h", "0 (deploy restarts exit cleanly)"),
        Check("stall_restarts", "stall restarts", stalls <= STALL_RESTARTS_MAX_24H, f"{stalls} in 24 h",
              f"<= {STALL_RESTARTS_MAX_24H} (seen: 0; one is the watchdog doing its job)"),
        Check("internal_errors", "collector step errors", errors <= INTERNAL_ERRORS_MAX_24H, f"{errors} in 24 h",
              f"<= {INTERNAL_ERRORS_MAX_24H} (seen: 0)"),
    ]


# --------------------------------------------------------------- reminders
@dataclass(frozen=True)
class Reminder:
    due: float
    who: str
    what: str
    say: str

    @property
    def key(self) -> str:
        return hashlib.sha256(f"{self.due}|{self.who}|{self.what}".encode()).hexdigest()[:16]

    def message(self) -> tuple[str, str]:
        if self.who.lower() == "claude":
            return f"{self.what} due", f"Open Claude Code and say '{self.say}'."
        return f"To do: {self.what}", self.say


_ROW = re.compile(r"^\|\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\s*\|\s*(Claude|you)\s*\|\s*(.+?)\s*\|\s*(.+?)\s*\|\s*$")


def parse_reminders(text: str) -> list[Reminder]:
    """Rows of the reminders table: | 2026-11-20 12:15 | Claude or you | what | what to say or do |."""
    out, inside = [], False
    for line in text.splitlines():
        if line.startswith("## "):
            inside = line.strip().startswith(REMINDER_SECTION)
            continue
        if inside and (m := _ROW.match(line.strip())):
            due = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M").replace(tzinfo=UTC).timestamp()
            out.append(Reminder(due, m.group(2), m.group(3).strip(), m.group(4).strip()))
    return out


# ------------------------------------------------------------------- state
def health_state_path(cfg: Config) -> Path:
    return state_path(cfg).parent / "health.json"


def load_state(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        value = {}
    if not isinstance(value, dict):
        value = {}
    value.setdefault("checks", {})
    value.setdefault("reminders_sent", {})
    value.setdefault("summary_day", "")
    return value


def save_state(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, indent=1, sort_keys=True))
    tmp.replace(path)


# ---------------------------------------------------------------- delivery
Sender = Callable[[str, str, int, list[str]], bool]


def ntfy_sender(topic: str, server: str = NTFY_DEFAULT_SERVER) -> Sender:
    def send(title: str, message: str, priority: int, tags: list[str]) -> bool:
        body = {"topic": topic, "title": title, "message": message, "priority": priority, "tags": tags}
        try:
            resp = httpx.post(server.rstrip("/"), json=body, timeout=15.0)
        except httpx.HTTPError as exc:  # the message would contain the URL with the topic: type only
            log.warning("ntfy send failed: %s", type(exc).__name__)
            return False
        if resp.status_code != 200:
            log.warning("ntfy send failed: HTTP %s", resp.status_code)
            return False
        return True
    return send


def ping_dead_mans_switch(url: str | None) -> bool | None:
    if not url:
        return None
    try:
        return httpx.get(url, timeout=10.0).status_code == 200
    except httpx.HTTPError as exc:
        log.warning("healthchecks ping failed: %s", type(exc).__name__)
        return False


# --------------------------------------------------------------------- run
def evaluate(state: dict[str, Any], checks: list[Check], send: Sender, now: float,
             local: time.struct_time, reminders: list[Reminder]) -> list[str]:
    """Update the per-check counters, send what is due, return the titles sent (for logs and tests)."""
    sent: list[str] = []
    new_fail, recovered = [], []
    for c in checks:
        st = state["checks"].setdefault(c.name, {"fails": 0, "passes": 0, "alerted": False})
        if c.ok:
            st["fails"], st["passes"] = 0, st["passes"] + 1
            if st["alerted"] and st["passes"] >= PASSES_TO_RECOVER:
                recovered.append(c)
        else:
            st["fails"], st["passes"] = st["fails"] + 1, 0
            if not st["alerted"] and st["fails"] >= FAILS_TO_ALERT:
                new_fail.append(c)
    if new_fail:
        title = f"Collector alert: {new_fail[0].label}" + (f" (+{len(new_fail) - 1} more)" if len(new_fail) > 1 else "")
        lines = [f"{c.label}: {c.value} (normal: {c.normal})" for c in new_fail]
        if send(title, "\n".join([*lines, "", ACTION]), 4, ["warning"]):
            for c in new_fail:
                state["checks"][c.name]["alerted"] = True
            sent.append(title)
    if recovered:
        title = "Recovered: " + ", ".join(c.label for c in recovered)
        lines = [f"{c.label}: {c.value} (normal: {c.normal})" for c in recovered]
        if send(title, "\n".join(lines), 3, ["white_check_mark"]):
            for c in recovered:
                state["checks"][c.name]["alerted"] = False
            sent.append(title)
    today = time.strftime("%Y-%m-%d", local)
    if (local.tm_hour, local.tm_min) >= SUMMARY_AT and state["summary_day"] != today:
        failing = [c for c in checks if not c.ok]
        title = "Daily check: all OK" if not failing else f"Daily check: {len(failing)} warning(s)"
        body = [f"{c.label}: {c.value} (normal: {c.normal})" for c in failing] or ["Every check passed."]
        body += ["", summary_numbers(checks)]
        upcoming = sorted((r for r in reminders if r.due > now), key=lambda r: r.due)
        if upcoming:
            body.append(f"Next: {upcoming[0].what} ({datetime.fromtimestamp(upcoming[0].due, UTC):%a %d %b %H:%M} UTC)")
        if failing:
            body += ["", ACTION]
        if send(title, "\n".join(body), 3 if failing else 2, ["calendar"]):
            state["summary_day"] = today
            sent.append(title)
    for r in reminders:
        if r.key in state["reminders_sent"] or not (now - REMINDER_MAX_LATE_S <= r.due <= now):
            continue
        title, message = r.message()
        if send(title, message, 3, ["bell"]):
            state["reminders_sent"][r.key] = now
            sent.append(title)
    return sent


def summary_numbers(checks: list[Check]) -> str:
    by = {c.name: c.value for c in checks}
    parts = [f"{k}: {by[n]}" for n, k in (("launch_rate", "launches/h"), ("coverage", "coverage"),
                                          ("credits", "credits"), ("disk", "disk")) if n in by]
    return " | ".join(parts)


def _open_db(cfg: Config) -> sqlite3.Connection | None:
    try:
        return report._connect(cfg)
    except sqlite3.Error:
        return None


def cmd_run(cfg: Config) -> int:
    now = time.time()
    path = health_state_path(cfg)
    state = load_state(path)
    db = _open_db(cfg)
    try:
        checks = run_checks(cfg, db, probe(), now)
    finally:
        if db is not None:
            db.close()
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    pinged = ping_dead_mans_switch(os.environ.get("HEALTHCHECKS_PING_URL", "").strip() or None)
    try:
        reminders = parse_reminders(STATUS_MD.read_text())
    except OSError:
        reminders = []
    if not topic:
        log.warning("NTFY_TOPIC is not set: checks ran, nothing sent")
        sent: list[str] = []
    else:
        sent = evaluate(state, checks, ntfy_sender(topic, os.environ.get("NTFY_SERVER") or NTFY_DEFAULT_SERVER),
                        now, time.localtime(now), reminders)
    state["last_run"] = now
    save_state(path, state)
    failing = [c.name for c in checks if not c.ok]
    log.info("health: %d checks, failing=%s, sent=%s, ping=%s", len(checks), failing or "none", sent or "none",
             {None: "not configured", True: "ok", False: "failed"}[pinged])
    return 0


def cmd_check(cfg: Config) -> int:
    """Print every check (what the timer would evaluate); sends nothing, changes nothing."""
    now = time.time()
    db = _open_db(cfg)
    checks = run_checks(cfg, db, probe(), now)
    state = load_state(health_state_path(cfg))
    for c in checks:
        alerted = " (alert sent)" if state["checks"].get(c.name, {}).get("alerted") else ""
        print(f"{'ok  ' if c.ok else 'FAIL'}  {c.label}: {c.value}   normal: {c.normal}{alerted}")
    last = state.get("last_run")
    print(f"timer last ran: {'never' if not last else f'{(now - float(last)) / 60:.0f} min ago'};  "
          f"ntfy topic {'set' if os.environ.get('NTFY_TOPIC') else 'MISSING'};  "
          f"healthchecks ping URL {'set' if os.environ.get('HEALTHCHECKS_PING_URL') else 'MISSING'}")
    try:
        reminders = parse_reminders(STATUS_MD.read_text())
    except OSError:
        reminders = []
    for r in sorted(reminders, key=lambda r: r.due):
        mark = "sent" if r.key in state["reminders_sent"] else ("due" if r.due <= now else "upcoming")
        print(f"reminder {datetime.fromtimestamp(r.due, UTC):%Y-%m-%d %H:%M} UTC [{mark}] {r.who}: {r.what}")
    return 0


def cmd_test(cfg: Config) -> int:
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        print("NTFY_TOPIC is not set in .env")
        return 1
    ok = ntfy_sender(topic, os.environ.get("NTFY_SERVER") or NTFY_DEFAULT_SERVER)(
        "Test notification", "If you can read this, collector alerts reach your phone.", 3, ["test_tube"])
    pinged = ping_dead_mans_switch(os.environ.get("HEALTHCHECKS_PING_URL", "").strip() or None)
    ping_txt = {None: "not configured", True: "ok", False: "FAILED"}[pinged]
    print(f"ntfy: {'sent' if ok else 'FAILED'};  healthchecks ping: {ping_txt}")
    return 0 if ok and pinged is not False else 1


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    load_dotenv(REPO_ROOT / ".env")
    parser = argparse.ArgumentParser(prog="rugwatch.collector.health")
    parser.add_argument("command", choices=["run", "check", "test"])
    args = parser.parse_args(argv)
    cfg = load_config()
    return {"run": cmd_run, "check": cmd_check, "test": cmd_test}[args.command](cfg)


if __name__ == "__main__":
    sys.exit(main())
