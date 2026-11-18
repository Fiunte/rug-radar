"""Collector state in SQLite (config storage.state). Operational state only: the raw files stay
the source of truth for analysis. Everything here can be rebuilt from raw except the schedule.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tokens (
  mint TEXT PRIMARY KEY, launch_obs REAL NOT NULL, sig TEXT, launcher TEXT, bonding_curve TEXT,
  name TEXT, symbol TEXT, uri TEXT, initial_buy_tokens REAL, sol_amount REAL,
  creator TEXT, quote_mint TEXT,
  next_tick_idx INTEGER NOT NULL DEFAULT 0, next_tick_due REAL
);
CREATE INDEX IF NOT EXISTS tokens_due ON tokens(next_tick_due) WHERE next_tick_due IS NOT NULL;
CREATE INDEX IF NOT EXISTS tokens_launch ON tokens(launch_obs);

CREATE TABLE IF NOT EXISTS filter_ticks (
  mint TEXT NOT NULL, tick_s INTEGER NOT NULL, observed_at REAL, slot INTEGER, status TEXT NOT NULL,
  virtual_sol INTEGER, virtual_token INTEGER, real_sol INTEGER, real_token INTEGER, complete INTEGER,
  PRIMARY KEY (mint, tick_s)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS tracking (
  mint TEXT PRIMARY KEY, decided_at REAL NOT NULL, launch_obs REAL NOT NULL, bonding_curve TEXT,
  filter_pass INTEGER NOT NULL, eligible_rest INTEGER NOT NULL,
  tracked INTEGER NOT NULL, reason TEXT NOT NULL, flags TEXT,
  filter_real_sol_300 REAL, sample_prob REAL, benchmark INTEGER NOT NULL DEFAULT 0,
  snapshot_ts REAL, snapshot_slot INTEGER, p0 REAL, snapshot_complete INTEGER, missing_parts TEXT,
  creation_slot INTEGER, creation_block_time INTEGER,
  peak_price REAL, last_price REAL, last_vq INTEGER, last_vt INTEGER, last_change_obs REAL,
  dead INTEGER NOT NULL DEFAULT 0, graduated_ts REAL,
  price_nominal REAL, next_price_due REAL, balance_nominal REAL, next_balance_due REAL,
  holders_due REAL
);
CREATE INDEX IF NOT EXISTS tracking_decided ON tracking(decided_at);
CREATE INDEX IF NOT EXISTS tracking_price ON tracking(next_price_due) WHERE next_price_due IS NOT NULL;
CREATE INDEX IF NOT EXISTS tracking_balance ON tracking(next_balance_due) WHERE next_balance_due IS NOT NULL;
CREATE INDEX IF NOT EXISTS tracking_holders ON tracking(holders_due) WHERE holders_due IS NOT NULL;

CREATE TABLE IF NOT EXISTS balance_accounts (
  mint TEXT NOT NULL, token_account TEXT NOT NULL, owner TEXT, role TEXT, is_insider INTEGER,
  PRIMARY KEY (mint, token_account)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS early_trades (
  mint TEXT NOT NULL, sig TEXT NOT NULL, seq INTEGER NOT NULL, slot INTEGER, block_time INTEGER,
  signer TEXT, token_account TEXT, is_buy INTEGER, sol_lamports INTEGER, token_amount INTEGER,
  fee_lamports INTEGER, fee_bps REAL, priority_fee_lamports INTEGER, tip_lamports INTEGER,
  slot_offset_from_creation INTEGER,
  PRIMARY KEY (mint, sig, seq)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS creator_cache (creator TEXT PRIMARY KEY, fetched_at REAL NOT NULL);

CREATE TABLE IF NOT EXISTS scanner_done (
  mint TEXT NOT NULL, scanner TEXT NOT NULL, observed_at REAL, status TEXT, latency_ms INTEGER,
  PRIMARY KEY (mint, scanner)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS credit_ledger (
  hour TEXT NOT NULL, source TEXT NOT NULL, purpose TEXT NOT NULL,
  calls INTEGER NOT NULL DEFAULT 0, credits INTEGER NOT NULL DEFAULT 0, errors INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (hour, source, purpose)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, ts REAL NOT NULL, kind TEXT NOT NULL, detail TEXT);
CREATE INDEX IF NOT EXISTS events_kind ON events(kind, ts);
CREATE TABLE IF NOT EXISTS heartbeats (minute INTEGER PRIMARY KEY, ws_connected INTEGER NOT NULL,
  rpcws_connected INTEGER NOT NULL DEFAULT 0);

-- Which launch source saw each launch, and when (D-31). tokens.launch_obs = the earliest of these.
CREATE TABLE IF NOT EXISTS launch_sources (
  mint TEXT NOT NULL, source TEXT NOT NULL, observed_at REAL NOT NULL, sig TEXT,
  PRIMARY KEY (mint, source)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS launch_sources_sig ON launch_sources(sig);

-- Bytes received/sent per UTC calendar month and channel (D-33), e.g. FluxRPC's 10 GB/month limit.
CREATE TABLE IF NOT EXISTS bandwidth_ledger (
  month TEXT NOT NULL, channel TEXT NOT NULL, bytes INTEGER NOT NULL DEFAULT 0,
  messages INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (month, channel)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

# Columns added after the first deployment: (table, column, definition). Applied once, idempotent.
MIGRATIONS = (("heartbeats", "rpcws_connected", "INTEGER NOT NULL DEFAULT 0"),)


def month_key(ts: float) -> str:
    return time.strftime("%Y-%m", time.gmtime(ts))


class State:
    def __init__(self, path: Path | str) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        for table, column, definition in MIGRATIONS:
            cols = {r[1] for r in self.db.execute(f"PRAGMA table_info({table})")}
            if cols and column not in cols:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        self.db.executescript(SCHEMA_SQL)

    def close(self) -> None:
        self.db.close()

    # ------------------------------------------------------------- generic
    def q(self, sql: str, params: tuple[Any, ...] | dict[str, Any] = ()) -> list[sqlite3.Row]:
        return self.db.execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple[Any, ...] | dict[str, Any] = ()) -> Any:
        row = self.db.execute(sql, params).fetchone()
        return row[0] if row is not None else None

    def x(self, sql: str, params: tuple[Any, ...] | dict[str, Any] = ()) -> None:
        self.db.execute(sql, params)

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        value = self.one("SELECT value FROM meta WHERE key=?", (key,))
        return default if value is None else str(value)

    def set_meta(self, key: str, value: str) -> None:
        self.x("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (key, value))

    def event(self, kind: str, detail: dict[str, Any] | None = None, ts: float | None = None) -> None:
        self.x("INSERT INTO events(ts,kind,detail) VALUES(?,?,?)",
               (ts if ts is not None else time.time(), kind, json.dumps(detail or {})))

    def heartbeat(self, now: float, ws_connected: bool, rpcws_connected: bool = False) -> None:
        self.x("INSERT INTO heartbeats(minute,ws_connected,rpcws_connected) VALUES(?,?,?) "
               "ON CONFLICT(minute) DO UPDATE SET ws_connected=MAX(ws_connected,excluded.ws_connected), "
               "rpcws_connected=MAX(rpcws_connected,excluded.rpcws_connected)",
               (int(now // 60), int(ws_connected), int(rpcws_connected)))

    def bandwidth_add(self, ts: float, channel: str, nbytes: int) -> None:
        self.x("INSERT INTO bandwidth_ledger(month,channel,bytes,messages) VALUES(?,?,?,1) "
               "ON CONFLICT(month,channel) DO UPDATE SET bytes=bytes+excluded.bytes, messages=messages+1",
               (month_key(ts), channel, nbytes))

    def bandwidth_month(self, prefix: str, ts: float) -> int:
        """Bytes this UTC calendar month over the channels whose name starts with `prefix`."""
        return int(self.one("SELECT COALESCE(SUM(bytes),0) FROM bandwidth_ledger WHERE month=? AND channel LIKE ?",
                            (month_key(ts), prefix + "%")))

    def data_clock_start(self) -> float | None:
        """Start of the current data clock (D-32): launches observed earlier are pre-fix data."""
        value = self.get_meta("data_clock_start")
        return float(value) if value is not None else None

    def prune(self, before: float) -> int:
        """Drop per-launch working rows older than `before`. Raw files keep everything; tracked
        tokens, the ledger, events and heartbeats are never pruned."""
        self.x("DELETE FROM filter_ticks WHERE mint IN (SELECT mint FROM tokens WHERE launch_obs<?)", (before,))
        self.x("DELETE FROM tracking WHERE tracked=0 AND decided_at<?", (before,))
        self.x("DELETE FROM launch_sources WHERE observed_at<? AND mint NOT IN "
               "(SELECT mint FROM tracking WHERE tracked=1)", (before,))
        cur = self.db.execute("DELETE FROM tokens WHERE launch_obs<? AND next_tick_due IS NULL", (before,))
        return cur.rowcount

    # -------------------------------------------------------------- ledger
    def ledger_add(self, hour: str, source: str, purpose: str, credits: int, error: bool) -> None:
        self.x("INSERT INTO credit_ledger(hour,source,purpose,calls,credits,errors) VALUES(?,?,?,1,?,?) "
               "ON CONFLICT(hour,source,purpose) DO UPDATE SET calls=calls+1, "
               "credits=credits+excluded.credits, errors=errors+excluded.errors",
               (hour, source, purpose, credits, int(error)))

    def credits_since(self, source: str, hour_from: str) -> int:
        return int(self.one("SELECT COALESCE(SUM(credits),0) FROM credit_ledger WHERE source=? AND hour>=?",
                            (source, hour_from)))

    # -------------------------------------------------------------- tokens
    def add_token(self, mint: str, launch_obs: float, msg: dict[str, Any], first_due: float) -> bool:
        cur = self.db.execute(
            "INSERT OR IGNORE INTO tokens(mint,launch_obs,sig,launcher,bonding_curve,name,symbol,uri,"
            "initial_buy_tokens,sol_amount,next_tick_idx,next_tick_due) VALUES(?,?,?,?,?,?,?,?,?,?,0,?)",
            (mint, launch_obs, msg.get("signature"), msg.get("traderPublicKey"), msg.get("bondingCurveKey"),
             msg.get("name"), msg.get("symbol"), msg.get("uri"), msg.get("initialBuy"), msg.get("solAmount"),
             first_due))
        return cur.rowcount == 1

    def add_launch_source(self, mint: str, source: str, observed_at: float, sig: str | None) -> None:
        self.x("INSERT OR IGNORE INTO launch_sources(mint,source,observed_at,sig) VALUES(?,?,?,?)",
               (mint, source, observed_at, sig))

    def due_filter_ticks(self, now: float) -> list[sqlite3.Row]:
        return self.q("SELECT mint, launch_obs, bonding_curve, next_tick_idx, next_tick_due, launcher "
                      "FROM tokens WHERE next_tick_due IS NOT NULL AND next_tick_due<=? ORDER BY next_tick_due",
                      (now,))

    def advance_tick(self, mint: str, next_idx: int, next_due: float | None) -> None:
        self.x("UPDATE tokens SET next_tick_idx=?, next_tick_due=? WHERE mint=?", (next_idx, next_due, mint))

    def put_filter_tick(self, mint: str, tick_s: int, status: str, observed_at: float | None = None,
                        slot: int | None = None, vq: int | None = None, vt: int | None = None,
                        rq: int | None = None, rt: int | None = None, complete: bool | None = None) -> None:
        self.x("INSERT OR REPLACE INTO filter_ticks VALUES(?,?,?,?,?,?,?,?,?,?)",
               (mint, tick_s, observed_at, slot, status, vq, vt, rq, rt,
                None if complete is None else int(complete)))

    # ------------------------------------------------------------ tracking
    def count_decided(self, since: float, column: str, threshold_sol: float | None = None) -> int:
        """Decisions since `since`. With `threshold_sol`, filter_pass / eligible_rest are recounted against
        that threshold from the stored real SOL, so a threshold change (rule 5.1) takes effect at once."""
        assert column in ("filter_pass", "eligible_rest", "tracked")
        if threshold_sol is None or column == "tracked":
            return int(self.one(f"SELECT COALESCE(SUM({column}),0) FROM tracking WHERE decided_at>=?", (since,)))
        side = ">=" if column == "filter_pass" else "<"
        return int(self.one("SELECT COUNT(*) FROM tracking WHERE decided_at>=? AND (filter_pass=1 OR eligible_rest=1) "
                            f"AND CASE WHEN filter_real_sol_300 IS NULL THEN {column}=1 "
                            f"ELSE filter_real_sol_300 {side} ? END", (since, threshold_sol)))

    def first_decision_ts(self) -> float | None:
        value = self.one("SELECT MIN(decided_at) FROM tracking")
        return float(value) if value is not None else None

    def put_tracking(self, row: dict[str, Any]) -> None:
        cols = ",".join(row)
        marks = ",".join(f":{k}" for k in row)
        self.x(f"INSERT OR REPLACE INTO tracking({cols}) VALUES({marks})", row)

    def update_tracking(self, mint: str, **fields: Any) -> None:
        sets = ",".join(f"{k}=:{k}" for k in fields)
        self.x(f"UPDATE tracking SET {sets} WHERE mint=:mint", {**fields, "mint": mint})
