"""Config, layouts, raw storage, the no-signing rule."""

from __future__ import annotations

import json
import re
import struct
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from rugwatch.chain import pump_layout as pl
from rugwatch.collector.pumpportal import PumpPortalListener
from rugwatch.collector.state import State
from rugwatch.config import DEFAULT_CONFIG_PATH, REPO_ROOT, Config, load_config
from rugwatch.storage.raw import (
    DiskFullError,
    RawWriter,
    dir_size_bytes,
    iter_raw,
    make_record,
    parse_iso,
    read_raw,
    utc_iso,
)
from tests.conftest import CREATOR, curve_bytes, make_cfg


# ------------------------------------------------------------------- config
def test_real_config_loads() -> None:
    cfg = load_config()
    assert cfg.schema_version == 1
    assert cfg.tracking.price_cadence[0] == (900, 15)
    assert cfg.scanners.list_ == ["rugcheck", "goplus", "solanatracker"]
    assert cfg.collector.rpc.use_websocket is False


def test_unknown_key_is_an_error() -> None:
    raw = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text())
    raw["activity_filter"]["surprise"] = 1
    with pytest.raises(ValidationError):
        Config.model_validate(raw)
    raw = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text())
    raw["new_section"] = {}
    with pytest.raises(ValidationError):
        Config.model_validate(raw)


def test_metered_streams_and_websocket_rpc_are_refused(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        make_cfg({"collector": {"rpc": {"use_websocket": True}}})
    cfg = make_cfg({"collector": {"pumpportal": {"streams": ["subscribeNewToken", "subscribeTokenTrade"]}}})
    with pytest.raises(ValueError, match="metered"):
        PumpPortalListener(cfg.collector.pumpportal, cfg.activity_filter, State(":memory:"),
                           RawWriter(tmp_path, min_free_bytes=0))


# ------------------------------------------------------------------- layout
def test_base58_roundtrip() -> None:
    for text in (pl.PUMP_PROGRAM_ID, pl.SYSTEM_PROGRAM_ID, pl.WSOL_MINT, CREATOR):
        raw = pl.b58decode(text)
        assert len(raw) == 32
        assert pl.b58encode(raw) == text


def test_decode_bonding_curve() -> None:
    bc = pl.decode_bonding_curve(curve_bytes(vq=31_500_000_000, vt=1_000_000_000_000_000, rq=6_500_000_000))
    assert bc is not None
    assert bc.real_sol == pytest.approx(6.5)
    assert bc.creator == CREATOR
    assert bc.is_sol_quoted and not bc.complete
    assert bc.price == pytest.approx(31.5 / 1e9)  # 31.5 SOL per 1e9 whole tokens
    assert bc.data_len == 166


def test_decode_bonding_curve_edge_cases() -> None:
    assert pl.decode_bonding_curve(b"\0" * 166) is None  # wrong discriminator
    assert pl.decode_bonding_curve(pl.BONDING_CURVE_DISCRIMINATOR) is None  # too short
    old = pl.decode_bonding_curve(curve_bytes()[:81])  # layout before the quote-mint upgrade
    assert old is not None and old.quote_mint is None and old.is_sol_quoted
    other = pl.decode_bonding_curve(curve_bytes(quote_mint=CREATOR))
    assert other is not None and not other.is_sol_quoted
    empty = pl.decode_bonding_curve(curve_bytes(vt=0))
    assert empty is not None and empty.price is None
    assert pl.decode_account_b64(None) is None


def test_decode_token_account() -> None:
    data = pl.b58decode(pl.WSOL_MINT) + pl.b58decode(CREATOR) + struct.pack("<Q", 12345)
    acc = pl.decode_token_account(data)
    assert acc is not None and (acc.mint, acc.owner, acc.amount) == (pl.WSOL_MINT, CREATOR, 12345)
    assert pl.decode_token_account(data[:40]) is None


# ---------------------------------------------------------------------- raw
def _rec(ts: float, n: int) -> dict[str, object]:
    return make_record(observed_at=ts, source="helius", kind="accounts", purpose="price", status="ok",
                       payload={"n": n}, mint="M", tick_s=15, slot=1, latency_ms=3, request={"method": "x"})


def test_iso_roundtrip() -> None:
    ts = 1794873600.123456
    assert utc_iso(ts) == "2026-11-17T00:00:00.123456Z"
    assert parse_iso(utc_iso(ts)) == pytest.approx(ts, abs=1e-6)


def test_raw_writer_roundtrip_frames_parts_and_partitions(tmp_path: Path) -> None:
    w = RawWriter(tmp_path, min_free_bytes=0)
    w.write(_rec(1794873600.0, 1))
    w.flush()
    w.write(_rec(1794873601.0, 2))  # second frame, same part
    w.write(_rec(1794873600.0 + 86400, 3))  # next UTC day -> other partition
    w.close()
    w2 = RawWriter(tmp_path, min_free_bytes=0)  # a restart never appends to an old part
    w2.write(_rec(1794873602.0, 4))
    w2.close()
    day1 = tmp_path / "helius" / "accounts" / "dt=2026-11-17"
    assert sorted(p.name for p in day1.iterdir()) == ["part-0.jsonl.zst", "part-1.jsonl.zst"]
    got = [r["payload"]["n"] for r in iter_raw(tmp_path, "helius", "accounts")]
    assert got == [1, 2, 4, 3]
    rec = next(iter_raw(tmp_path, "helius", "accounts"))
    assert set(rec) == {"schema", "observed_at", "source", "kind", "mint", "purpose", "tick_s", "slot", "status",
                        "latency_ms", "request", "payload", "collector_version"}
    assert rec["schema"] == 1
    assert set(dir_size_bytes(tmp_path)) == {"2026-11-17", "2026-11-18"}


def test_raw_part_survives_a_crash_and_old_frame_files_still_read(tmp_path: Path) -> None:
    import zstandard

    w = RawWriter(tmp_path, min_free_bytes=0)
    for i in range(50):
        w.write(_rec(1794873600.0 + i, i))
        w.flush()  # one flushed block per record, frame left open (no close = crash)
    part = tmp_path / "helius" / "accounts" / "dt=2026-11-17" / "part-0.jsonl.zst"
    assert [r["payload"]["n"] for r in read_raw(part)] == list(range(50))
    with open(part, "ab") as fh:  # torn write after the last flushed block
        fh.write(zstandard.ZstdCompressor().compressobj().compress(b"x" * 10))
    assert len(list(read_raw(part))) == 50
    # Files written by the earlier writer: many complete frames, concatenated.
    old = tmp_path / "old.jsonl.zst"
    cctx = zstandard.ZstdCompressor()
    old.write_bytes(b"".join(cctx.compress(b'{"n":%d}\n' % i) for i in range(3)) + cctx.compress(b'{"n":3}\n')[:-4])
    assert [r["n"] for r in read_raw(old)] == [0, 1, 2]


def test_raw_stream_compresses_better_than_separate_frames(tmp_path: Path) -> None:
    import zstandard

    w = RawWriter(tmp_path, min_free_bytes=0)
    lines = []
    for i in range(200):
        rec = _rec(1794873600.0 + i, i)
        lines.append(json.dumps(rec, separators=(",", ":")).encode() + b"\n")
        w.write(rec)
        w.flush()
    w.close()
    stream_size = (tmp_path / "helius" / "accounts" / "dt=2026-11-17" / "part-0.jsonl.zst").stat().st_size
    frames_size = sum(len(zstandard.ZstdCompressor(level=10).compress(x)) for x in lines)
    assert stream_size < frames_size / 3


def test_raw_writer_stops_when_disk_is_low(tmp_path: Path) -> None:
    w = RawWriter(tmp_path, min_free_bytes=10**18)
    w.write(_rec(1794873600.0, 1))
    with pytest.raises(DiskFullError):
        w.flush()


# --------------------------------------------------------------- hard rules
FORBIDDEN = re.compile(r"Keypair|sendTransaction|sign_transaction|private_key")


def test_no_signing_code_in_src() -> None:
    """HANDOFF section 0: paper trading only. No keys, wallets or signing code in src/."""
    hits = []
    for path in sorted((REPO_ROOT / "src").rglob("*")):
        if path.is_file() and path.suffix in {".py", ".toml", ".yaml", ".yml", ".json", ".sh", ".txt", ".md"}:
            for i, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if FORBIDDEN.search(line):
                    hits.append(f"{path.relative_to(REPO_ROOT)}:{i}")
    assert not hits, f"forbidden signing-related code: {hits}"


def test_env_file_is_ignored_by_git() -> None:
    lines = (REPO_ROOT / ".gitignore").read_text().split()
    assert ".env" in lines and "data/" in lines
