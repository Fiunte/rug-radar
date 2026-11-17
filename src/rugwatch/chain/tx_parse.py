"""Parse pump.fun transactions (getTransaction, jsonParsed encoding).

Extracts trade and create events, fees, priority fee, tips, the token accounts of signers
(always from postTokenBalances, never derived) and direct token transfers.
"""

from __future__ import annotations

import base64
import struct
from dataclasses import dataclass, field
from typing import Any

from rugwatch.chain import pump_layout as pl

BASE_FEE_LAMPORTS_PER_SIGNATURE = 5000

# Jito tip accounts. Tips paid to other services are not counted in v1 (docs/decisions.md D-12).
JITO_TIP_ACCOUNTS = frozenset({
    "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5",
    "HFqU5x63VTqvQss8hp11i4wVV8bD44PvwucfZ2bU7gRe",
    "Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY",
    "ADaUMid9yfUytqMBgopwjb2DTLSokTSzL1zt6iGPaS49",
    "DfXygSm4jCyNCybVYYK6DwvWqjKee8pbDmJGcLWNDXjh",
    "ADuUkR4vqLUMWXxW9gh6D6L8pMSawimctcNZ5pGwDcEt",
    "DttWaMuVvTiduZRnguLF7jNxTgiMBZ1hyAumKUiL2KRL",
    "3AVi9Tg9Uo68tJfuvoKvqKNWKkC5wPdSSdeBnizKZ6jT",
})


class _Reader:
    """Sequential little-endian reader; raises IndexError/struct.error past the end."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def u64(self) -> int:
        (v,) = struct.unpack_from("<Q", self.data, self.pos)
        self.pos += 8
        return int(v)

    def i64(self) -> int:
        (v,) = struct.unpack_from("<q", self.data, self.pos)
        self.pos += 8
        return int(v)

    def u32(self) -> int:
        (v,) = struct.unpack_from("<I", self.data, self.pos)
        self.pos += 4
        return int(v)

    def boolean(self) -> bool:
        v = self.data[self.pos]
        self.pos += 1
        return v != 0

    def pubkey(self) -> str:
        if self.pos + 32 > len(self.data):
            raise IndexError("pubkey past end")
        v = pl.b58encode(self.data[self.pos:self.pos + 32])
        self.pos += 32
        return v

    def string(self) -> str:
        n = self.u32()
        if self.pos + n > len(self.data):
            raise IndexError("string past end")
        v = self.data[self.pos:self.pos + n].decode("utf-8", "replace")
        self.pos += n
        return v

    def skip(self, n: int) -> None:
        if self.pos + n > len(self.data):
            raise IndexError("skip past end")
        self.pos += n


@dataclass
class TradeEvent:
    mint: str
    sol_amount: int
    token_amount: int
    is_buy: bool
    user: str
    timestamp: int
    virtual_sol: int
    virtual_token: int
    real_sol: int
    real_token: int
    fee_bps: int = 0
    fee: int = 0
    creator: str | None = None
    creator_fee_bps: int = 0
    creator_fee: int = 0
    ix_name: str | None = None
    cashback_bps: int = 0
    cashback: int = 0
    buyback_bps: int = 0
    buyback_fee: int = 0
    quote_mint: str | None = None
    holder_rewards_bps: int = 0
    holder_rewards: int = 0

    @property
    def total_fee_lamports(self) -> int:
        """Everything charged on top of / out of the quoted SOL amount (D-11)."""
        return self.fee + self.creator_fee + self.buyback_fee + self.cashback + self.holder_rewards

    @property
    def total_fee_bps(self) -> float | None:
        return 1e4 * self.total_fee_lamports / self.sol_amount if self.sol_amount > 0 else None


def decode_trade_event(payload: bytes) -> TradeEvent | None:
    """`payload` starts with the 8-byte event discriminator. Trailing fields are optional."""
    if payload[:8] != pl.TRADE_EVENT_DISCRIMINATOR:
        return None
    r = _Reader(payload[8:])
    try:
        ev = TradeEvent(mint=r.pubkey(), sol_amount=r.u64(), token_amount=r.u64(), is_buy=r.boolean(),
                        user=r.pubkey(), timestamp=r.i64(), virtual_sol=r.u64(), virtual_token=r.u64(),
                        real_sol=r.u64(), real_token=r.u64())
    except (IndexError, struct.error):
        return None
    try:
        r.skip(32)  # fee_recipient
        ev.fee_bps = r.u64()
        ev.fee = r.u64()
        ev.creator = r.pubkey()
        ev.creator_fee_bps = r.u64()
        ev.creator_fee = r.u64()
        r.skip(1 + 8 + 8 + 8 + 8)  # track_volume, unclaimed, claimed, sol volume, last update
        ev.ix_name = r.string()
        r.skip(1)  # mayhem_mode
        ev.cashback_bps = r.u64()
        ev.cashback = r.u64()
        ev.buyback_bps = r.u64()
        ev.buyback_fee = r.u64()
        r.skip(34 * r.u32())  # shareholders
        ev.quote_mint = r.pubkey()
        r.skip(8 + 8 + 8)  # quote_amount, virtual_quote_reserves, real_quote_reserves
        ev.holder_rewards_bps = r.u64()
        ev.holder_rewards = r.u64()
    except (IndexError, struct.error):
        pass  # older, shorter event layout
    return ev


@dataclass
class CreateEvent:
    name: str
    symbol: str
    uri: str
    mint: str
    bonding_curve: str
    user: str
    creator: str | None = None
    token_program: str | None = None
    quote_mint: str | None = None


def decode_create_event(payload: bytes) -> CreateEvent | None:
    if payload[:8] != pl.CREATE_EVENT_DISCRIMINATOR:
        return None
    r = _Reader(payload[8:])
    try:
        ev = CreateEvent(name=r.string(), symbol=r.string(), uri=r.string(), mint=r.pubkey(),
                         bonding_curve=r.pubkey(), user=r.pubkey())
    except (IndexError, struct.error):
        return None
    try:
        ev.creator = r.pubkey()
        r.skip(8 + 8 + 8 + 8 + 8)  # timestamp, virtual token/sol, real token, total supply
        ev.token_program = r.pubkey()
        r.skip(2)  # is_mayhem_mode, is_cashback_enabled
        ev.quote_mint = r.pubkey()
    except (IndexError, struct.error):
        pass
    return ev


@dataclass
class TokenTransfer:
    source: str | None
    destination: str | None
    authority: str | None
    amount: int
    mint: str | None


@dataclass
class ParsedTx:
    sig: str
    slot: int | None
    block_time: int | None
    ok: bool
    signer: str | None
    n_signatures: int
    fee_lamports: int
    priority_fee_lamports: int
    tip_lamports: int
    trades: list[TradeEvent] = field(default_factory=list)
    creates: list[CreateEvent] = field(default_factory=list)
    transfers: list[TokenTransfer] = field(default_factory=list)
    # (owner, mint) -> (token_account, post amount raw)
    post_token_accounts: dict[tuple[str, str], tuple[str, int]] = field(default_factory=dict)


def _all_instructions(tx: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = list(tx["transaction"]["message"].get("instructions") or [])
    for group in (tx.get("meta") or {}).get("innerInstructions") or []:
        out.extend(group.get("instructions") or [])
    return out


def _event_payloads(tx: dict[str, Any]) -> list[bytes]:
    """Anchor event payloads of the pump program: inner self-CPI first, log lines as fallback."""
    out: list[bytes] = []
    for ix in _all_instructions(tx):
        if ix.get("programId") == pl.PUMP_PROGRAM_ID and isinstance(ix.get("data"), str):
            try:
                raw = pl.b58decode(ix["data"])
            except KeyError:
                continue
            if raw[:8] == pl.EVENT_CPI_PREFIX:
                out.append(raw[8:])
    if out:
        return out
    for line in (tx.get("meta") or {}).get("logMessages") or []:
        if line.startswith("Program data: "):
            try:
                out.append(base64.b64decode(line[14:]))
            except ValueError:
                continue
    return out


def parse_transaction(tx: dict[str, Any], sig: str | None = None) -> ParsedTx:
    meta = tx.get("meta") or {}
    message = tx["transaction"]["message"]
    signatures = tx["transaction"].get("signatures") or []
    keys = message.get("accountKeys") or []
    key_names = [k["pubkey"] if isinstance(k, dict) else str(k) for k in keys]
    signer = key_names[0] if key_names else None
    fee = int(meta.get("fee") or 0)
    n_sig = len(signatures)
    parsed = ParsedTx(sig=sig or (signatures[0] if signatures else ""), slot=tx.get("slot"),
                      block_time=tx.get("blockTime"), ok=meta.get("err") is None, signer=signer,
                      n_signatures=n_sig, fee_lamports=fee,
                      priority_fee_lamports=max(0, fee - BASE_FEE_LAMPORTS_PER_SIGNATURE * n_sig),
                      tip_lamports=0)

    for payload in _event_payloads(tx):
        if (trade := decode_trade_event(payload)) is not None:
            parsed.trades.append(trade)
        elif (create := decode_create_event(payload)) is not None:
            parsed.creates.append(create)

    account_mint: dict[str, str] = {}
    for bal in meta.get("postTokenBalances") or []:
        idx, owner, mint = bal.get("accountIndex"), bal.get("owner"), bal.get("mint")
        if not isinstance(idx, int) or idx >= len(key_names) or not owner or not mint:
            continue
        amount = int((bal.get("uiTokenAmount") or {}).get("amount") or 0)
        parsed.post_token_accounts[(owner, mint)] = (key_names[idx], amount)
        account_mint[key_names[idx]] = mint
    for bal in meta.get("preTokenBalances") or []:
        idx, mint = bal.get("accountIndex"), bal.get("mint")
        if isinstance(idx, int) and idx < len(key_names) and mint:
            account_mint.setdefault(key_names[idx], mint)

    for ix in _all_instructions(tx):
        info = ix.get("parsed", {}).get("info") if isinstance(ix.get("parsed"), dict) else None
        if not isinstance(info, dict):
            continue
        kind = ix["parsed"].get("type")
        if ix.get("program") == "system" and kind == "transfer":
            if info.get("destination") in JITO_TIP_ACCOUNTS:
                parsed.tip_lamports += int(info.get("lamports") or 0)
        elif ix.get("program") in ("spl-token", "spl-token-2022") and kind in ("transfer", "transferChecked"):
            amount = info.get("amount") or (info.get("tokenAmount") or {}).get("amount") or 0
            source = info.get("source")
            parsed.transfers.append(TokenTransfer(
                source=source, destination=info.get("destination"),
                authority=info.get("authority") or info.get("multisigAuthority"), amount=int(amount),
                mint=info.get("mint") or account_mint.get(str(source))
                or account_mint.get(str(info.get("destination")))))
    return parsed
