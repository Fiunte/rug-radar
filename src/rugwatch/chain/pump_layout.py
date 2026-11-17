"""pump.fun account layouts and constants.

Decoded against the official IDL (github.com/pump-fun/pump-public-docs, idl/pump.json,
commit of 2026-10-13). Fields were appended over time and old accounts can be shorter, so the
decoder reads as many trailing fields as the account length allows.
"""

from __future__ import annotations

import base64
import struct
from dataclasses import dataclass

PUMP_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
SYSTEM_PROGRAM_ID = "11111111111111111111111111111111"  # also Pubkey::default()
WSOL_MINT = "So11111111111111111111111111111111111111112"
TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM_ID = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"

# quote_mint is Pubkey::default() for every SOL-paired coin; wSOL is accepted as an alias.
SOL_QUOTE_MINTS = frozenset({SYSTEM_PROGRAM_ID, WSOL_MINT})

TOKEN_DECIMALS = 6  # every pump.fun coin
LAMPORTS_PER_SOL = 1_000_000_000

BONDING_CURVE_DISCRIMINATOR = bytes([23, 183, 248, 55, 96, 216, 172, 96])
# Anchor event discriminators (first 8 bytes of the event payload).
TRADE_EVENT_DISCRIMINATOR = bytes([189, 219, 127, 211, 78, 230, 97, 238])
CREATE_EVENT_DISCRIMINATOR = bytes([27, 114, 169, 77, 222, 235, 99, 118])
COMPLETE_EVENT_DISCRIMINATOR = bytes([95, 114, 97, 156, 212, 46, 152, 8])
# Prefix of the self-CPI instruction that carries an Anchor event (emit_cpi!).
EVENT_CPI_PREFIX = bytes([228, 69, 165, 46, 81, 203, 154, 29])

_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {c: i for i, c in enumerate(_B58_ALPHABET)}


def b58encode(data: bytes) -> str:
    n = int.from_bytes(data, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = _B58_ALPHABET[r] + out
    pad = len(data) - len(data.lstrip(b"\0"))
    return "1" * pad + out


def b58decode(text: str) -> bytes:
    n = 0
    for ch in text:
        n = n * 58 + _B58_INDEX[ch]
    pad = len(text) - len(text.lstrip("1"))
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\0" * pad + body


@dataclass(frozen=True)
class BondingCurve:
    virtual_token: int
    virtual_quote: int
    real_token: int
    real_quote: int
    token_total_supply: int
    complete: bool
    creator: str | None
    is_mayhem_mode: bool | None
    quote_mint: str | None  # None on accounts older than the quote-mint upgrade
    data_len: int

    @property
    def is_sol_quoted(self) -> bool:
        return self.quote_mint is None or self.quote_mint in SOL_QUOTE_MINTS

    @property
    def real_sol(self) -> float:
        """Real SOL reserves in SOL. Only meaningful when is_sol_quoted."""
        return self.real_quote / LAMPORTS_PER_SOL

    @property
    def price(self) -> float | None:
        """Quote units (SOL) per whole token; None when the curve is empty."""
        return price_from_reserves(self.virtual_quote, self.virtual_token)


def price_from_reserves(virtual_quote: int, virtual_token: int) -> float | None:
    if virtual_token <= 0:
        return None
    return float(virtual_quote / LAMPORTS_PER_SOL) / float(virtual_token / 10**TOKEN_DECIMALS)


def decode_bonding_curve(data: bytes) -> BondingCurve | None:
    """Decode a BondingCurve account. Returns None if it is not one."""
    if len(data) < 49 or data[:8] != BONDING_CURVE_DISCRIMINATOR:
        return None
    vt, vq, rt, rq, supply = struct.unpack_from("<5Q", data, 8)
    complete = data[48] != 0
    creator = b58encode(data[49:81]) if len(data) >= 81 else None
    mayhem = (data[81] != 0) if len(data) >= 82 else None
    quote_mint = b58encode(data[83:115]) if len(data) >= 115 else None
    return BondingCurve(vt, vq, rt, rq, supply, complete, creator, mayhem, quote_mint, len(data))


def decode_account_b64(value: dict[str, object] | None) -> bytes | None:
    """Raw bytes of an account from an RPC `value` item with base64 encoding."""
    if not value:
        return None
    data = value.get("data")
    if isinstance(data, list) and data and isinstance(data[0], str):
        return base64.b64decode(data[0])
    return None


@dataclass(frozen=True)
class TokenAccount:
    mint: str
    owner: str
    amount: int


def decode_token_account(data: bytes) -> TokenAccount | None:
    """First 72 bytes of an SPL / Token-2022 token account: mint, owner, amount."""
    if len(data) < 72:
        return None
    (amount,) = struct.unpack_from("<Q", data, 64)
    return TokenAccount(b58encode(data[0:32]), b58encode(data[32:64]), amount)
