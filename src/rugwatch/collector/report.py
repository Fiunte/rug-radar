"""`make status`, `make report-day1`, `make report-week1`. Read-only over the collector state."""

from __future__ import annotations

import argparse
import math
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from rugwatch.chain.pump_layout import LAMPORTS_PER_SOL, SOL_QUOTE_MINTS
from rugwatch.collector.budget import METERED_SOURCE, cycle_start, hour_key
from rugwatch.collector.main import billing_day, raw_root, state_path
from rugwatch.config import REPO_ROOT, Config, load_config
from rugwatch.storage.raw import dir_size_bytes

SERVICE = "rugwatch-collector"
TRACKED_PURPOSES = ("snapshot", "price", "balance", "creator", "holders", "scanner")
OVERHEAD_PURPOSES = ("filter", "launch_log")


def _connect(cfg: Config) -> sqlite3.Connection | None:
    path = state_path(cfg)
    if not path.exists():
        return None
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def _cmd(*args: str) -> str:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=10, check=False).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"


CLOCK_ALERT_S = 2.0  # HANDOFF 6 item 11


def ntp_offset_s(text: str) -> float | None:
    """Clock offset from `timedatectl timesync-status` output ("Offset: -6.745ms"), in seconds."""
    m = re.search(r"Offset:\s*([+-]?[\d.]+)\s*(us|ms|s)\b", text)
    if not m:
        return None
    return float(m.group(1)) * {"us": 1e-6, "ms": 1e-3, "s": 1.0}[m.group(2)]


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d")


def quantiles(values: list[float], qs: tuple[float, ...] = (0.1, 0.25, 0.5, 0.75, 0.9, 0.99)) -> dict[str, float]:
    if not values:
        return {}
    xs = sorted(values)
    return {f"p{round(q * 100)}": xs[min(len(xs) - 1, math.floor(q * len(xs)))] for q in qs}


def _fmt_q(q: dict[str, float], scale: float = 1.0, digits: int = 1) -> str:
    return "  ".join(f"{k}={v * scale:.{digits}f}" for k, v in q.items()) if q else "no data"


def uptime(db: sqlite3.Connection, now: float, window_s: float) -> tuple[float, float] | None:
    """(share of minutes alive, share of minutes with the websocket connected) over the window."""
    first = db.execute("SELECT MIN(minute) FROM heartbeats").fetchone()[0]
    if first is None:
        return None
    start = max(first, int((now - window_s) // 60))
    total = int(now // 60) - start + 1
    alive, ws = db.execute("SELECT COUNT(*), COALESCE(SUM(ws_connected),0) FROM heartbeats WHERE minute>=?",
                           (start,)).fetchone()
    return alive / total, ws / total


def projection(db: sqlite3.Connection, now: float) -> float | None:
    first = db.execute("SELECT MIN(hour) FROM credit_ledger WHERE source=?", (METERED_SOURCE,)).fetchone()[0]
    if first is None:
        return None
    first_ts = datetime.strptime(first, "%Y-%m-%dT%H").replace(tzinfo=UTC).timestamp()
    window = min(86400.0, now - first_ts)
    if window < 3600:
        return None
    used = db.execute("SELECT COALESCE(SUM(credits),0) FROM credit_ledger WHERE source=? AND hour>=?",
                      (METERED_SOURCE, hour_key(now - window))).fetchone()[0]
    return float(used) / window * 86400 * 30


# ------------------------------------------------------------------- status
def status(cfg: Config) -> list[str]:
    now = time.time()
    out = [f"rugwatch status at {datetime.fromtimestamp(now, UTC):%Y-%m-%d %H:%M:%S} UTC"]
    out.append(f"service: active={_cmd('systemctl', '--user', 'is-active', SERVICE) or 'unknown'} "
               f"enabled={_cmd('systemctl', '--user', 'is-enabled', SERVICE) or 'unknown'} "
               f"linger={_cmd('loginctl', 'show-user', str(Path.home().name), '-p', 'Linger', '--value') or 'unknown'}")
    offset = ntp_offset_s(_cmd("timedatectl", "timesync-status"))
    out.append(f"clock: ntp_synchronized={_cmd('timedatectl', 'show', '-p', 'NTPSynchronized', '--value')}  "
               f"offset={'unknown' if offset is None else f'{offset * 1000:+.1f} ms'}"
               + ("   <-- ALERT: clock off by more than 2 s" if offset is not None and abs(offset) > CLOCK_ALERT_S
                  else ""))
    free = shutil.disk_usage(REPO_ROOT).free
    raw_bytes = sum(dir_size_bytes(raw_root(cfg)).values()) if raw_root(cfg).exists() else 0
    out.append(f"disk: free={free / 1e9:.1f} GB  raw_data={raw_bytes / 1e6:.1f} MB"
               + ("   <-- LOW: collector stops below 0.5 GB" if free < 2e9 else ""))
    db = _connect(cfg)
    if db is None:
        return [*out, "no collector state yet (collector has never run)"]
    beat = db.execute("SELECT MAX(minute) FROM heartbeats").fetchone()[0]
    for label, window in (("24h", 86400.0), ("10d", 864000.0)):
        up = uptime(db, now, window)
        if up:
            out.append(f"uptime {label}: alive={up[0]:.1%}  websocket_connected={up[1]:.1%}")
    last_launch = db.execute("SELECT MAX(launch_obs) FROM tokens").fetchone()[0]
    out.append(f"last heartbeat age: {now - beat * 60:.0f} s" if beat else "last heartbeat: never")
    out.append(f"last launch event age: {now - last_launch:.0f} s" if last_launch else "last launch event: never")
    bday = billing_day()
    used = db.execute("SELECT COALESCE(SUM(credits),0) FROM credit_ledger WHERE source=? AND hour>=?",
                      (METERED_SOURCE, hour_key(cycle_start(now, bday)))).fetchone()[0]
    proj = projection(db, now)
    level = db.execute("SELECT value FROM meta WHERE key='throttle_level'").fetchone()
    out.append(f"helius credits: this cycle (resets day {bday})={used:,}  projected/30d="
               f"{'n/a (<1 h of data)' if proj is None else f'{proj:,.0f}'}"
               f"  target={cfg.collector.budget.target_credits:,}"
               f"  throttle_level={level[0] if level else 0}")
    errs = db.execute("SELECT source, SUM(calls), SUM(errors) FROM credit_ledger WHERE hour>=? GROUP BY source",
                      (hour_key(now - 86400),)).fetchall()
    out.append("calls 24h: " + ("  ".join(f"{s}={c:,} (errors {e:,})" for s, c, e in errs) or "none"))
    drops, drops24 = (db.execute("SELECT COUNT(*) FROM events WHERE kind='ws_drop' AND ts>=?", (t,)).fetchone()[0]
                      for t in (0, now - 86400))
    gap = db.execute("SELECT detail FROM events WHERE kind='ws_connect' AND ts>=?", (now - 86400,)).fetchall()
    gap_s = sum(float(_json(g[0]).get("gap_s") or 0) for g in gap)
    out.append(f"websocket drops: total={drops}  last_24h={drops24}  disconnected_24h={gap_s:.0f} s")
    row = db.execute("SELECT COUNT(*), COALESCE(SUM(tracked),0), COALESCE(SUM(snapshot_complete),0), "
                     "COALESCE(SUM(filter_pass),0) FROM tracking WHERE decided_at>=?", (now - 86400,)).fetchone()
    launches = db.execute("SELECT COUNT(*) FROM tokens WHERE launch_obs>=?", (now - 86400,)).fetchone()[0]
    out.append(f"last 24h: launches={launches:,}  decided={row[0]:,}  filter_pass={row[3]:,}  tracked={row[1]:,}"
               f"  snapshots_complete={row[2]:,}")
    bad = db.execute("SELECT kind, COUNT(*) FROM events WHERE ts>=? AND kind IN "
                     "('crash','disk_full','step_error','task_error','hard_stop_on','throttle_level') GROUP BY kind",
                     (now - 86400,)).fetchall()
    if bad:
        out.append("events 24h: " + "  ".join(f"{k}={n}" for k, n in bad))
    stops = db.execute("SELECT detail FROM events WHERE kind='service_stop' AND ts>=?", (now - 864000,)).fetchall()
    stalls = db.execute("SELECT COUNT(*) FROM events WHERE kind='stall_restart' AND ts>=?",
                        (now - 864000,)).fetchone()[0]
    reasons: dict[str, int] = {}
    for (detail,) in stops:
        d = _json(detail)
        key = f"{d.get('service_result')}/{d.get('exit_status')}"
        reasons[key] = reasons.get(key, 0) + 1
    out.append(f"service stops 10d: {len(stops)} " + (" ".join(f"{k}={n}" for k, n in sorted(reasons.items())))
               + f"  stall_restarts 10d: {stalls}")
    return out


def _json(text: str | None) -> dict[str, Any]:
    import json

    try:
        value = json.loads(text or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


# -------------------------------------------------------------------- day 1
def report_day1(cfg: Config) -> list[str]:
    db = _connect(cfg)
    if db is None:
        return ["no collector state yet"]
    flt = cfg.activity_filter
    now = time.time()
    sol = ",".join(f"'{m}'" for m in sorted(SOL_QUOTE_MINTS))
    out = ["# report-day1", f"generated {datetime.fromtimestamp(now, UTC):%Y-%m-%d %H:%M} UTC", ""]
    span = db.execute("SELECT MIN(launch_obs), MAX(launch_obs), COUNT(*) FROM tokens").fetchone()
    if span[2] == 0:
        return [*out, "no launches recorded yet"]
    hours = max((span[1] - span[0]) / 3600, 1e-9)
    scale = 24 / hours
    out += [f"Coverage: {hours:.1f} h of launches ({_day(span[0])} to {_day(span[1])}); "
            f"per-day figures below are scaled to 24 h (x{scale:.2f}).",
            f"Launches: {span[2]:,} observed = {span[2] * scale:,.0f} per day "
            "(independent public figure: ~26,000/day).", ""]

    out += ["## Pass counts per threshold (per day, SOL-quoted, not graduated)", "",
            "| threshold SOL | " + " | ".join(f"t={t}s" for t in flt.tick_s) + " |",
            "|---|" + "---|" * len(flt.tick_s)]
    pass_300: dict[float, float] = {}
    for thr in flt.threshold_grid_sol:
        cells = []
        for tick in flt.tick_s:
            n = db.execute(
                f"SELECT COUNT(*) FROM filter_ticks f JOIN tokens t USING(mint) WHERE f.tick_s=? AND f.status='ok' "
                f"AND f.complete=0 AND f.real_sol>=? AND (t.quote_mint IS NULL OR t.quote_mint IN ({sol}))",
                (tick, thr * LAMPORTS_PER_SOL)).fetchone()[0]
            cells.append(f"{n * scale:,.0f}")
            if tick == flt.decide_at_s:
                pass_300[thr] = n * scale
        out.append(f"| {thr:g} | " + " | ".join(cells) + " |")
    cap = cfg.tracking.filter_pass_cap_per_day
    ok = [thr for thr in flt.threshold_grid_sol if thr >= flt.floor_sol and pass_300.get(thr, 0) <= cap]
    out.append("")
    if ok:
        out.append(f"Rule 5.1: smallest grid value with <= {cap} passes/day at {flt.decide_at_s} s is "
                   f"**{min(ok):g} SOL** (config now: {flt.threshold_sol:g}).")
    else:
        out.append(f"Rule 5.1: even {max(flt.threshold_grid_sol):g} SOL passes more than {cap}/day; "
                   "use it and hash-sample down.")
    floor_pass = pass_300.get(flt.floor_sol, 0.0)
    if floor_pass < 150:
        out.append(f"Rule 5.1.4: the {flt.floor_sol:g} SOL floor passes only {floor_pass:,.0f}/day (< 150): "
                   "the timeline to enough data roughly doubles.")

    d = flt.decide_at_s
    tot = db.execute("SELECT COUNT(*) FROM filter_ticks WHERE tick_s=?", (d,)).fetchone()[0]
    out += ["", f"## Outcome of the {d} s tick (share of launches that reached it: n={tot:,})", ""]
    if tot:
        rows = {
            "graduated before the tick (outside the universe)":
                "f.status='ok' AND f.complete=1",
            "empty curve, real SOL = 0 (dead; outside the universe)":
                "f.status='ok' AND f.complete=0 AND f.real_sol=0",
            f"0 < real SOL < {flt.floor_sol:g}":
                f"f.status='ok' AND f.complete=0 AND f.real_sol>0 AND f.real_sol<{flt.floor_sol * LAMPORTS_PER_SOL}",
            "quoted in another token, not SOL (outside the universe)":
                f"f.status='ok' AND t.quote_mint IS NOT NULL AND t.quote_mint NOT IN ({sol})",
            "tick missed by the collector": "f.status='missed'",
            "read failed or account missing": "f.status NOT IN ('ok','missed')",
        }
        for label, cond in rows.items():
            n = db.execute(f"SELECT COUNT(*) FROM filter_ticks f JOIN tokens t USING(mint) "
                           f"WHERE f.tick_s=? AND {cond}", (d,)).fetchone()[0]
            out.append(f"- {label}: {n:,} ({n / tot:.1%})")
    trk = db.execute("SELECT reason, COUNT(*) FROM tracking WHERE tracked=1 GROUP BY reason").fetchall()
    snap = db.execute("SELECT COUNT(*), COALESCE(SUM(snapshot_complete),0) FROM tracking WHERE tracked=1").fetchone()
    out += ["", "## Tracking", "",
            "- tracked: " + ("  ".join(f"{r}={n:,}" for r, n in trk) or "none"),
            f"- snapshots complete: {snap[1]:,} of {snap[0]:,}"]
    miss = db.execute("SELECT missing_parts, COUNT(*) FROM tracking WHERE tracked=1 AND missing_parts<>'' "
                      "GROUP BY missing_parts ORDER BY 2 DESC LIMIT 8").fetchall()
    out += [f"  - missing {m}: {n}" for m, n in miss]

    out += ["", "## Credits", ""]
    for src, purpose, calls, credits, errors in db.execute(
            "SELECT source, purpose, SUM(calls), SUM(credits), SUM(errors) FROM credit_ledger "
            "GROUP BY source, purpose ORDER BY source, 4 DESC"):
        out.append(f"- {src} / {purpose}: calls={calls:,} credits={credits:,} errors={errors:,}")
    proj = projection(db, now)
    out.append(f"- Helius projection: {'n/a' if proj is None else f'{proj:,.0f}'} credits per 30 days "
               f"(target {cfg.collector.budget.target_credits:,}). Reconcile with the Helius dashboard.")
    drops = db.execute("SELECT COUNT(*) FROM events WHERE kind='ws_drop'").fetchone()[0]
    gaps = [float(_json(r[0]).get("gap_s") or 0) for r in
            db.execute("SELECT detail FROM events WHERE kind='ws_connect'")]
    up = uptime(db, now, 864000.0)
    out += ["", "## Reliability", "",
            f"- websocket drops: {drops}; total time disconnected: {sum(gaps):.0f} s; longest gap: "
            f"{max(gaps, default=0):.0f} s",
            f"- uptime: alive {up[0]:.1%}, websocket connected {up[1]:.1%}" if up else "- uptime: n/a"]
    skew = [r[0] for r in db.execute("SELECT launch_obs - creation_block_time FROM tracking "
                                     "WHERE creation_block_time IS NOT NULL")]
    out.append(f"- launch observed_at minus creation block_time (s): {_fmt_q(quantiles(skew), digits=2)}")
    return out


# ------------------------------------------------------------------- week 1
def report_week1(cfg: Config) -> list[str]:
    db = _connect(cfg)
    if db is None:
        return ["no collector state yet"]
    now = time.time()
    out = ["# report-week1", f"generated {datetime.fromtimestamp(now, UTC):%Y-%m-%d %H:%M} UTC", ""]
    first = db.execute("SELECT MIN(hour) FROM credit_ledger WHERE source=?", (METERED_SOURCE,)).fetchone()[0]
    if first is None:
        return [*out, "no credits recorded yet"]
    days = max((now - datetime.strptime(first, "%Y-%m-%dT%H").replace(tzinfo=UTC).timestamp()) / 86400, 1e-9)

    def credits(purposes: tuple[str, ...]) -> int:
        marks = ",".join("?" * len(purposes))
        return int(db.execute(f"SELECT COALESCE(SUM(credits),0) FROM credit_ledger WHERE source=? "
                              f"AND purpose IN ({marks})", (METERED_SOURCE, *purposes)).fetchone()[0])

    tracked = db.execute("SELECT COUNT(*) FROM tracking WHERE tracked=1").fetchone()[0]
    b = credits(OVERHEAD_PURPOSES) / days
    c = credits(TRACKED_PURPOSES) / tracked if tracked else float("nan")
    out += ["## Credits (Helius)", "",
            f"- period: {days:.1f} days; tracked tokens: {tracked:,} ({tracked / days:,.0f}/day)",
            f"- fixed daily overhead b (launch log + filter ticks): {b:,.0f} credits/day",
            f"- credits per tracked token-day c: {c:,.1f}",
            f"- monthly projection: {projection(db, now) or float('nan'):,.0f} "
            f"(target {cfg.collector.budget.target_credits:,})"]
    if tracked and c > 0:
        cap = min(cfg.tracking.tracked_cap_per_day,
                  math.floor((cfg.collector.budget.target_credits / 30 - b) / c))
        out.append(f"- rule 5.1.5: tracked_cap = min({cfg.tracking.tracked_cap_per_day}, "
                   f"floor(({cfg.collector.budget.target_credits}/30 - b) / c)) = **{cap}**")
    for src, calls, errors in db.execute("SELECT source, SUM(calls), SUM(errors) FROM credit_ledger "
                                         "WHERE source<>? GROUP BY source", (METERED_SOURCE,)):
        out.append(f"- {src}: calls={calls:,} errors={errors:,}")

    sizes = dir_size_bytes(raw_root(cfg))
    out += ["", "## Storage", ""]
    out += [f"- {dt}: {size / 1e6:,.1f} MB" for dt, size in sorted(sizes.items())]
    full = sorted(sizes.items())[1:-1] or sorted(sizes.items())
    if full:
        per_day = sum(s for _d, s in full) / len(full)
        free = shutil.disk_usage(REPO_ROOT).free
        out.append(f"- average {per_day / 1e6:,.0f} MB/day; free disk {free / 1e9:.1f} GB = "
                   f"{free / per_day:,.0f} days at this rate")

    out += ["", "## Scanners (on-time = answered ok inside the snapshot batch)", ""]
    scan = db.execute("SELECT scanner, COUNT(*), SUM(status='ok'), SUM(status='timeout'), SUM(status='error') "
                      "FROM scanner_done GROUP BY scanner").fetchall()
    for name, n, ok, timeouts, errors in scan:
        lat = [r[0] for r in db.execute("SELECT latency_ms FROM scanner_done WHERE scanner=? AND status='ok'",
                                        (name,))]
        out.append(f"- {name}: queried={n:,} on-time coverage={ok / n:.1%} timeouts={timeouts} errors={errors} "
                   f"latency ms: {_fmt_q(quantiles(lat), digits=0)}")
    if not scan:
        out.append("- no scanner queries yet (scanners.enabled is false until day 3)")

    out += ["", "## Costs observed in parsed early trades", ""]
    fees = [r[0] for r in db.execute("SELECT fee_bps FROM early_trades WHERE fee_bps IS NOT NULL")]
    paid = [f for f in fees if f > 0]
    out.append(f"- trades parsed: {len(fees):,}; zero-fee trades: {len(fees) - len(paid):,}")
    out.append(f"- fee bps per side (fee-paying trades): {_fmt_q(quantiles(paid))}")
    per_token = [r[0] for r in db.execute("SELECT AVG(fee_bps) FROM early_trades WHERE fee_bps>0 GROUP BY mint")]
    out.append(f"- per-token mean fee bps: {_fmt_q(quantiles(per_token))}")
    txs = db.execute("SELECT MAX(priority_fee_lamports), MAX(tip_lamports) FROM early_trades GROUP BY sig").fetchall()
    prio = [r[0] / LAMPORTS_PER_SOL for r in txs if r[0] is not None]
    tips = [r[1] / LAMPORTS_PER_SOL for r in txs if r[1] is not None]
    both = [p + t for p, t in zip(prio, tips, strict=False)]
    out.append(f"- priority fee SOL/tx: {_fmt_q(quantiles(prio), digits=6)}")
    out.append(f"- Jito tip SOL/tx: {_fmt_q(quantiles(tips), digits=6)} (share with a tip: "
               f"{sum(1 for t in tips if t > 0) / len(tips):.1%})" if tips else "- Jito tip: no data")
    out.append(f"- priority + tip SOL/tx: {_fmt_q(quantiles(both), digits=6)}  "
               f"(config priority_fee_sol_per_tx = median)")
    skew = [r[0] for r in db.execute("SELECT launch_obs - creation_block_time FROM tracking "
                                     "WHERE creation_block_time IS NOT NULL")]
    out += ["", "## Clock", "",
            f"- launch observed_at minus creation block_time (s): {_fmt_q(quantiles(skew), digits=2)}"]
    up = uptime(db, now, 864000.0)
    out.append(f"- uptime (10 d window): alive {up[0]:.1%}, websocket connected {up[1]:.1%}" if up else "- uptime n/a")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rugwatch-report")
    parser.add_argument("command", choices=["status", "day1", "week1"])
    args = parser.parse_args(argv)
    load_dotenv(REPO_ROOT / ".env")  # only HELIUS_BILLING_DAY is read; no value is ever printed
    cfg = load_config()
    lines = {"status": status, "day1": report_day1, "week1": report_week1}[args.command](cfg)
    print("\n".join(lines))
    if args.command != "status":
        path = REPO_ROOT / "reports" / f"report-{args.command}.md"
        path.parent.mkdir(exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"\n(saved to {path.relative_to(REPO_ROOT)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
