"""Shared test helpers: real config with overrides, a fake JSON-RPC server, synthetic accounts."""

from __future__ import annotations

import base64
import json
import struct
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from rugwatch.chain import pump_layout as pl
from rugwatch.collector.budget import Budget
from rugwatch.collector.rpc import Provider, RpcClient
from rugwatch.collector.state import State
from rugwatch.config import Config, load_config
from rugwatch.storage.raw import RawWriter, iter_raw

FIXTURES = Path(__file__).parent / "fixtures"
SECRET = "SECRETKEY123"
CREATOR = pl.b58encode(bytes([7]) * 32)
CURVE = pl.b58encode(bytes([9]) * 32)


def _deep_update(base: dict[str, Any], upd: dict[str, Any]) -> dict[str, Any]:
    for key, value in upd.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_update(base[key], value)
        else:
            base[key] = value
    return base


def make_cfg(overrides: dict[str, Any] | None = None) -> Config:
    data = load_config().model_dump(by_alias=True)
    return Config.model_validate(_deep_update(data, overrides or {}))


def curve_bytes(*, vq: int = 30_000_000_000, vt: int = 1_073_000_000_000_000, rq: int = 0,
                rt: int = 793_100_000_000_000, complete: bool = False, creator: str = CREATOR,
                quote_mint: str = pl.SYSTEM_PROGRAM_ID) -> bytes:
    data = pl.BONDING_CURVE_DISCRIMINATOR + struct.pack("<5Q", vt, vq, rt, rq, 10**15)
    data += bytes([int(complete)]) + pl.b58decode(creator).rjust(32, b"\0") + b"\0\0"
    data += pl.b58decode(quote_mint).rjust(32, b"\0")
    return data + b"\0" * (166 - len(data))


def account_value(data: bytes | None) -> dict[str, Any] | None:
    if data is None:
        return None
    return {"data": [base64.b64encode(data).decode(), "base64"], "owner": pl.PUMP_PROGRAM_ID, "lamports": 1}


Handler = Callable[[str, list[Any]], Any]


class FakeRpc:
    """httpx transport that answers JSON-RPC from a handler(method, params) -> result | Exception."""

    def __init__(self, handler: Handler) -> None:
        self.handler = handler
        self.calls: list[tuple[str, str, list[Any]]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.calls.append((request.url.host, body["method"], body["params"]))
        result = self.handler(body["method"], body["params"])
        if isinstance(result, httpx.Response):
            return result
        if isinstance(result, Exception):
            raise result
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    def methods(self) -> list[str]:
        return [m for _h, m, _p in self.calls]


class Env:
    def __init__(self, tmp_path: Path, handler: Handler, cfg: Config | None = None) -> None:
        self.cfg = cfg or make_cfg()
        self.state = State(":memory:")
        self.raw_root = tmp_path / "raw"
        self.writer = RawWriter(self.raw_root, min_free_bytes=0)
        self.budget = Budget(self.state, self.cfg.collector.budget)
        self.fake = FakeRpc(handler)
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(self.fake))
        self.providers = [Provider("helius", f"https://helius.test/?api-key={SECRET}", max_rps=10_000),
                          Provider("alchemy", f"https://alchemy.test/v2/{SECRET}", max_rps=10_000)]
        self.rpc = RpcClient(self.providers, self.writer, self.budget, client=self.http)

    def records(self, source: str, kind: str) -> list[dict[str, Any]]:
        self.writer.flush()
        return list(iter_raw(self.raw_root, source, kind))


@pytest.fixture
def env_factory(tmp_path: Path) -> Callable[..., Env]:
    def make(handler: Handler, cfg: Config | None = None) -> Env:
        return Env(tmp_path, handler, cfg)

    return make


@pytest.fixture(scope="session")
def pump_fixture() -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / "pump_txs.json").read_text())
    return data
