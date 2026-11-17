"""Append-only raw writer: one JSON object per line, zstd (HANDOFF 3.1).

Records are buffered per (source, kind, UTC date) and appended as complete zstd frames, so a
crash loses at most the unflushed buffer and never corrupts earlier data. A new part file is
started on every process start and when a part exceeds MAX_PART_BYTES.
"""

from __future__ import annotations

import io
import json
import re
import shutil
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import zstandard

from rugwatch import __version__

SCHEMA = 1
FLUSH_INTERVAL_S = 15.0
MAX_BUFFER_BYTES = 4 * 1024 * 1024
MAX_PART_BYTES = 256 * 1024 * 1024
MIN_FREE_BYTES = 500 * 1024 * 1024
ZSTD_LEVEL = 10

_PART_RE = re.compile(r"^part-(\d+)\.jsonl\.zst$")


class DiskFullError(RuntimeError):
    """Raised when free disk space is below MIN_FREE_BYTES; the collector must stop."""


def utc_iso(ts: float) -> str:
    """Epoch seconds -> '2026-11-17T00:00:00.123456Z' (microseconds, UTC)."""
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_iso(text: str) -> float:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC).timestamp()


def make_record(
    *,
    observed_at: float,
    source: str,
    kind: str,
    purpose: str,
    status: str,
    payload: Any,
    mint: str | None = None,
    tick_s: int | None = None,
    slot: int | None = None,
    latency_ms: int | None = None,
    request: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "observed_at": utc_iso(observed_at),
        "source": source,
        "kind": kind,
        "mint": mint,
        "purpose": purpose,
        "tick_s": tick_s,
        "slot": slot,
        "status": status,
        "latency_ms": latency_ms,
        "request": request or {},
        "payload": payload,
        "collector_version": __version__,
    }


class RawWriter:
    def __init__(self, root: Path, *, min_free_bytes: int = MIN_FREE_BYTES) -> None:
        self.root = root
        self.min_free_bytes = min_free_bytes
        self._buffers: dict[tuple[str, str, str], list[bytes]] = {}
        self._sizes: dict[tuple[str, str, str], int] = {}
        self._parts: dict[tuple[str, str, str], Path] = {}
        self._last_flush = time.monotonic()
        self._cctx = zstandard.ZstdCompressor(level=ZSTD_LEVEL)
        self.records_written = 0
        root.mkdir(parents=True, exist_ok=True)

    def write(self, record: dict[str, Any]) -> None:
        key = (record["source"], record["kind"], record["observed_at"][:10])
        line = json.dumps(record, separators=(",", ":")).encode() + b"\n"
        self._buffers.setdefault(key, []).append(line)
        self._sizes[key] = self._sizes.get(key, 0) + len(line)
        self.records_written += 1
        if self._sizes[key] >= MAX_BUFFER_BYTES:
            self._flush_key(key)

    def maybe_flush(self) -> None:
        if time.monotonic() - self._last_flush >= FLUSH_INTERVAL_S:
            self.flush()

    def flush(self) -> None:
        self._last_flush = time.monotonic()
        for key in list(self._buffers):
            self._flush_key(key)
        # Drop handles of past dates so the dict does not grow without bound.
        today = utc_iso(time.time())[:10]
        for key in [k for k in self._parts if k[2] < today and k not in self._buffers]:
            del self._parts[key]

    def close(self) -> None:
        self.flush()

    def free_bytes(self) -> int:
        return shutil.disk_usage(self.root).free

    def _flush_key(self, key: tuple[str, str, str]) -> None:
        lines = self._buffers.pop(key, None)
        self._sizes.pop(key, None)
        if not lines:
            return
        if self.free_bytes() < self.min_free_bytes:
            raise DiskFullError(f"free disk space below {self.min_free_bytes} bytes")
        path = self._part_path(key)
        frame = self._cctx.compress(b"".join(lines))
        with open(path, "ab") as fh:
            fh.write(frame)
        if path.stat().st_size >= MAX_PART_BYTES:
            self._parts[key] = self._next_part(path.parent)

    def _part_path(self, key: tuple[str, str, str]) -> Path:
        path = self._parts.get(key)
        if path is None:
            source, kind, dt = key
            directory = self.root / source / kind / f"dt={dt}"
            directory.mkdir(parents=True, exist_ok=True)
            path = self._next_part(directory)
            self._parts[key] = path
        return path

    @staticmethod
    def _next_part(directory: Path) -> Path:
        nums = [int(m.group(1)) for p in directory.iterdir() if (m := _PART_RE.match(p.name))]
        return directory / f"part-{(max(nums) + 1) if nums else 0}.jsonl.zst"


def read_raw(path: Path) -> Iterator[dict[str, Any]]:
    """Iterate the records of one part file (handles concatenated frames)."""
    dctx = zstandard.ZstdDecompressor()
    with open(path, "rb") as fh, dctx.stream_reader(fh, read_across_frames=True) as reader:
        for line in io.TextIOWrapper(reader, encoding="utf-8"):
            if line.strip():
                yield json.loads(line)


def iter_raw(root: Path, source: str, kind: str, dt: str | None = None) -> Iterator[dict[str, Any]]:
    base = root / source / kind
    if not base.exists():
        return
    for directory in sorted(base.iterdir()):
        if dt is not None and directory.name != f"dt={dt}":
            continue
        parts = sorted(directory.glob("part-*.jsonl.zst"), key=lambda p: int(p.name.split("-")[1].split(".")[0]))
        for part in parts:
            yield from read_raw(part)


def dir_size_bytes(root: Path) -> dict[str, int]:
    """Bytes on disk per UTC date partition, summed over sources and kinds."""
    out: dict[str, int] = {}
    for part in root.glob("*/*/dt=*/part-*.jsonl.zst"):
        dt = part.parent.name[3:]
        out[dt] = out.get(dt, 0) + part.stat().st_size
    return out
