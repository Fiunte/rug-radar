"""Collector entrypoint: `python -m rugwatch.collector.main run | check-env | record-stop | snapshot-state`."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import socket
import sqlite3
import sys
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

from rugwatch import __version__
from rugwatch.collector.budget import Budget
from rugwatch.collector.pumpportal import PumpPortalListener
from rugwatch.collector.rpc import RpcClient, providers_from_env
from rugwatch.collector.scanners import build_scanners
from rugwatch.collector.scheduler import Scheduler
from rugwatch.collector.snapshot import Snapshotter
from rugwatch.collector.state import State
from rugwatch.config import REPO_ROOT, Config, load_config
from rugwatch.storage.raw import DiskFullError, RawWriter

log = logging.getLogger("rugwatch.main")

EXIT_DISK_FULL = 75  # systemd: RestartPreventExitStatus, so a full disk does not restart-loop
EXIT_CONFIG = 78
EXIT_STALL = 70  # no launch for STALL_RESTART_S: systemd restarts the collector (Restart=always)
STALL_RESTART_S = 600.0  # decision D-26: ~5x the longest gap between launches seen on day 1
HOUSEKEEPING_S = 5.0
STATE_KEEP_DAYS = 21  # untracked launches older than this leave the state database (raw keeps them)
REQUIRED_ENV = ("HELIUS_API_KEY",)
OPTIONAL_ENV = ("ALCHEMY_API_KEY", "FLUXRPC_API_KEY", "SOLANATRACKER_API_KEY", "RUGCHECK_API_KEY",
                "GOPLUS_API_KEY", "HELIUS_BILLING_DAY")


def data_base() -> Path:
    """Repo root, unless RUGWATCH_DATA_BASE redirects data/ elsewhere (tests, trial runs)."""
    return Path(os.environ.get("RUGWATCH_DATA_BASE") or REPO_ROOT)


def raw_root(cfg: Config) -> Path:
    return data_base() / cfg.storage.raw.split("{")[0]


def state_path(cfg: Config) -> Path:
    return data_base() / cfg.storage.state


def setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s", stream=sys.stdout)
    # httpx logs full request URLs at INFO, and provider URLs contain API keys.
    for name in ("httpx", "httpcore", "websockets"):
        logging.getLogger(name).setLevel(logging.WARNING)


def billing_day() -> int:
    try:
        return int(os.environ.get("HELIUS_BILLING_DAY") or 1)
    except ValueError:
        return 1


class StallError(RuntimeError):
    """No pump.fun launch arrived for STALL_RESTART_S; the process exits so systemd restarts it."""


def sd_notify(message: str) -> bool:
    """Send a systemd notification (e.g. WATCHDOG=1). No-op outside systemd."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return False
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(addr)
            sock.sendall(message.encode())
    except OSError:
        return False
    return True


def watchdog_tick(state: State, listener: PumpPortalListener, now: float) -> None:
    """Ping the systemd watchdog (a hung event loop stops pinging and systemd kills it), and raise
    StallError when no launch has arrived for STALL_RESTART_S (logged first, for uptime)."""
    sd_notify("WATCHDOG=1")
    gap = now - listener.last_launch_at
    if gap > STALL_RESTART_S:
        last_msg = listener.last_message_at
        state.event("stall_restart", {"gap_s": round(gap, 1), "ws_connected": listener.connected,
                                      "last_message_age_s": None if last_msg is None else round(now - last_msg, 1)})
        raise StallError(f"no launch for {gap:.0f} s")


async def housekeeping(state: State, writer: RawWriter, budget: Budget, listener: PumpPortalListener,
                       rpc: RpcClient) -> None:
    last_log = last_prune = 0.0
    while True:
        await asyncio.sleep(HOUSEKEEPING_S)
        now = time.time()
        watchdog_tick(state, listener, now)
        writer.maybe_flush()
        state.heartbeat(now, listener.connected)
        budget.review(now)
        if now - last_prune >= 3600:
            last_prune = now
            state.prune(now - STATE_KEEP_DAYS * 86400)
        if now - last_log >= 300:
            last_log = now
            proj = budget.projection(now)
            log.info("alive: ws=%s records=%d cycle_credits=%d projection_30d=%s throttle=%d hard_stop=%s free_gb=%.1f",
                     listener.connected, writer.records_written, budget.cycle_used(now),
                     "n/a" if proj is None else round(proj), budget.level, budget.hard_stop,
                     writer.free_bytes() / 1e9)


async def run_collector(cfg: Config) -> int:
    missing = [name for name in REQUIRED_ENV if not os.environ.get(name)]
    if missing:
        log.error("missing required environment variables: %s", ", ".join(missing))
        return EXIT_CONFIG
    state = State(state_path(cfg))
    writer = RawWriter(raw_root(cfg))
    budget = Budget(state, cfg.collector.budget, billing_day())
    http = httpx.AsyncClient(limits=httpx.Limits(max_connections=32))
    rpc = RpcClient(providers_from_env(cfg.collector.rpc.max_rps), writer, budget)
    scanners = build_scanners(list(cfg.scanners.list_)) if cfg.scanners.enabled else []
    snapshotter = Snapshotter(cfg, state, rpc, writer, scanners, http)
    scheduler = Scheduler(cfg, state, rpc, writer, budget, snapshotter)
    listener = PumpPortalListener(cfg.collector.pumpportal, cfg.activity_filter, state, writer)

    now = time.time()
    last_beat = state.one("SELECT MAX(minute) FROM heartbeats")
    state.event("start", {"version": __version__,
                          "down_s": round(now - last_beat * 60) if last_beat is not None else None})
    scheduler.recover(now)
    sd_notify("READY=1")
    log.info("collector %s started; providers=%s scanners=%s threshold_sol=%s", __version__,
             [p.name for p in rpc.providers], [s.name for s in scanners], cfg.activity_filter.threshold_sol)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    tasks = [asyncio.ensure_future(c) for c in (
        listener.run(), scheduler.run(), housekeeping(state, writer, budget, listener, rpc))]
    stopper = asyncio.ensure_future(stop.wait())
    done, _pending = await asyncio.wait([*tasks, stopper], return_when=asyncio.FIRST_COMPLETED)

    code = 0
    for task in done:
        if task is stopper or task.cancelled():
            continue
        exc = task.exception()
        if isinstance(exc, DiskFullError):
            log.error("disk almost full: stopping. Free space, then `systemctl --user start rugwatch-collector`.")
            state.event("disk_full", {})
            code = EXIT_DISK_FULL
        elif isinstance(exc, StallError):
            log.warning("stall watchdog: %s; exiting so systemd restarts the collector", exc)
            code = EXIT_STALL
        elif exc is not None:
            log.error("collector task crashed: %r", exc)
            state.event("crash", {"error": type(exc).__name__})
            code = 1
    for task in [*tasks, stopper]:
        task.cancel()
    await asyncio.gather(*tasks, stopper, return_exceptions=True)
    await scheduler.drain()
    state.event("stop", {"code": code})
    try:
        writer.close()
    except DiskFullError:
        code = EXIT_DISK_FULL
    await rpc.aclose()
    await http.aclose()
    state.close()
    log.info("collector stopped (exit %d)", code)
    return code


def record_stop(cfg: Config) -> int:
    """ExecStopPost hook: log why the service stopped (systemd passes SERVICE_RESULT, EXIT_CODE,
    EXIT_STATUS), so watchdog kills and crashes that the process could not log itself are counted."""
    state = State(state_path(cfg))
    state.event("service_stop", {k.lower(): os.environ.get(k) for k in ("SERVICE_RESULT", "EXIT_CODE", "EXIT_STATUS")})
    state.close()
    return 0


def snapshot_state(cfg: Config) -> int:
    """Consistent copy of the live SQLite state (online backup API) for `make backup` to pull:
    rsync of a database that is being written could copy a torn file."""
    src_path = state_path(cfg)
    if not src_path.exists():
        print("no state database yet")
        return 0
    dest = src_path.parent / "backup" / "collector.sqlite"
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp")
    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
    dst = sqlite3.connect(tmp)
    with dst:
        src.backup(dst)  # overwrites whatever a failed earlier run left in tmp
    src.close()
    dst.close()
    os.replace(tmp, dest)
    print(f"state snapshot: {dest.relative_to(data_base())}")
    return 0


async def check_env(cfg: Config) -> int:
    """Print which variables are set (names only, never values) and whether each RPC tier answers."""
    for name in (*REQUIRED_ENV, *OPTIONAL_ENV):
        print(f"{name:24s} {'set' if os.environ.get(name) else 'MISSING'}"
              f"{'  (required)' if name in REQUIRED_ENV else ''}")
    ok = True
    async with httpx.AsyncClient(timeout=10) as http:
        for provider in providers_from_env(cfg.collector.rpc.max_rps):
            body = {"jsonrpc": "2.0", "id": 1, "method": "getSlot", "params": []}
            try:
                resp = await http.post(provider.url, json=body)
                good = resp.status_code == 200 and "result" in resp.json()
                detail = "ok" if good else f"http_{resp.status_code}"
            except Exception as exc:  # the message may contain the URL
                good, detail = False, type(exc).__name__
            print(f"rpc {provider.name:10s} {detail}")
            if provider.name == cfg.collector.rpc.primary and not good:
                ok = False
    return 0 if ok else EXIT_CONFIG


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rugwatch-collector")
    parser.add_argument("command", choices=["run", "check-env", "record-stop", "snapshot-state"])
    args = parser.parse_args(argv)
    load_dotenv(REPO_ROOT / ".env")
    setup_logging()
    cfg = load_config()
    if args.command == "snapshot-state":
        return snapshot_state(cfg)
    if args.command == "record-stop":
        return record_stop(cfg)
    if args.command == "check-env":
        return asyncio.run(check_env(cfg))
    return asyncio.run(run_collector(cfg))


if __name__ == "__main__":
    sys.exit(main())
