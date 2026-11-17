"""Transaction parsing against transactions recorded from mainnet (tests/fixtures/pump_txs.json)."""

from __future__ import annotations

from typing import Any

import pytest

from rugwatch.chain import pump_layout as pl
from rugwatch.chain.tx_parse import (
    JITO_TIP_ACCOUNTS,
    ParsedTx,
    TokenTransfer,
    TradeEvent,
    decode_trade_event,
    parse_transaction,
)
from rugwatch.collector.snapshot import build_balance_accounts, build_early_trades, select_early_sigs


def _parsed(fx: dict[str, Any]) -> dict[str, ParsedTx]:
    return {sig[:6]: parse_transaction(tx, sig) for sig, tx in fx["txs"].items()}


def test_creation_tx_has_create_event_and_creator_buy(pump_fixture: dict[str, Any]) -> None:
    token = pump_fixture["token"]
    tx = _parsed(pump_fixture)["2NxiAR"]
    assert tx.ok and tx.sig == token["signature"]
    assert len(tx.creates) == 1
    create = tx.creates[0]
    assert create.mint == token["mint"]
    assert create.bonding_curve == token["bondingCurveKey"]
    assert create.creator == token["traderPublicKey"]
    assert create.token_program == pl.TOKEN_2022_PROGRAM_ID
    assert create.quote_mint == pl.SYSTEM_PROGRAM_ID
    assert len(tx.trades) == 1
    buy = tx.trades[0]
    assert buy.is_buy and buy.user == token["traderPublicKey"] and buy.mint == token["mint"]
    assert buy.ix_name == "buy"
    assert (buy.fee_bps, buy.creator_fee_bps) == (95, 30)
    assert buy.total_fee_bps == pytest.approx(125.0, abs=0.01)
    # The creator's token account comes from postTokenBalances.
    assert (token["traderPublicKey"], token["mint"]) in tx.post_token_accounts
    assert tx.priority_fee_lamports == tx.fee_lamports - 5000 * tx.n_signatures


def test_trader_is_the_event_user_not_the_fee_payer(pump_fixture: dict[str, Any]) -> None:
    """Mayhem-mode agent trades: the fee payer differs from the trading account, and the fee is zero."""
    tx = _parsed(pump_fixture)["3KuUoh"]
    trade = tx.trades[0]
    assert trade.is_buy and trade.user != tx.signer
    assert trade.total_fee_lamports == 0 and trade.total_fee_bps == 0
    assert (trade.user, trade.mint) in tx.post_token_accounts


def test_version_1_transaction_and_sell(pump_fixture: dict[str, Any]) -> None:
    parsed = _parsed(pump_fixture)
    buy = parsed["D4qVcz"].trades[0]
    assert buy.is_buy and buy.ix_name == "buy_exact_quote_in"
    assert buy.total_fee_bps == pytest.approx(125.0, abs=0.01)
    sell = parsed["sdkmaF"].trades[0]
    assert not sell.is_buy and sell.total_fee_bps == pytest.approx(125.0, abs=0.05)
    # A sale moves tokens from the seller to the curve.
    assert any(t.authority == sell.user and t.amount == sell.token_amount for t in parsed["sdkmaF"].transfers)


def test_truncated_trade_event_keeps_core_fields(pump_fixture: dict[str, Any]) -> None:
    tx = next(iter(pump_fixture["txs"].values()))
    payloads = [pl.b58decode(ix["data"])[8:] for grp in tx["meta"]["innerInstructions"]
                for ix in grp["instructions"]
                if ix.get("programId") == pl.PUMP_PROGRAM_ID and pl.b58decode(ix["data"])[:8] == pl.EVENT_CPI_PREFIX]
    full = decode_trade_event(payloads[0])
    short = decode_trade_event(payloads[0][:129])  # the original, short TradeEvent layout
    assert full is not None and short is not None
    assert (short.mint, short.sol_amount, short.user) == (full.mint, full.sol_amount, full.user)
    assert short.fee == 0 and short.ix_name is None
    assert decode_trade_event(payloads[0][:40]) is None
    assert decode_trade_event(b"\0" * 200) is None


def test_log_fallback_when_inner_instructions_are_missing(pump_fixture: dict[str, Any]) -> None:
    sig, tx = next((s, t) for s, t in pump_fixture["txs"].items() if s.startswith("D4qVcz"))
    stripped = {**tx, "meta": {**tx["meta"], "innerInstructions": []}}
    assert [t.sol_amount for t in parse_transaction(stripped, sig).trades] == \
        [t.sol_amount for t in parse_transaction(tx, sig).trades]


def test_jito_tip_is_counted() -> None:
    tip_account = sorted(JITO_TIP_ACCOUNTS)[0]
    tx = {"slot": 1, "blockTime": 2, "transaction": {"signatures": ["s"], "message": {
        "accountKeys": [{"pubkey": "payer"}],
        "instructions": [
            {"program": "system", "programId": pl.SYSTEM_PROGRAM_ID,
             "parsed": {"type": "transfer", "info": {"source": "payer", "destination": tip_account, "lamports": 777}}},
            {"program": "system", "programId": pl.SYSTEM_PROGRAM_ID,
             "parsed": {"type": "transfer", "info": {"source": "payer", "destination": "someone", "lamports": 5}}},
        ]}}, "meta": {"err": None, "fee": 15000}}
    parsed = parse_transaction(tx)
    assert parsed.tip_lamports == 777
    assert parsed.priority_fee_lamports == 10000


# ------------------------------------------------------- snapshot builders
def test_select_early_sigs_puts_creation_first_and_skips_failed() -> None:
    sigs = [{"signature": s, "err": e} for s, e in
            [("e", None), ("d", {"x": 1}), ("c", None), ("b", None), ("create", None)]]  # newest first
    assert select_early_sigs(sigs, "create", 2) == ["create", "b", "c"]
    assert select_early_sigs(sigs, None, 2) == ["create", "b", "c"]
    assert select_early_sigs(sigs[:-1], "create", 9) == ["create", "b", "c", "e"]  # creation beyond the pages


def test_build_early_trades_from_fixture(pump_fixture: dict[str, Any]) -> None:
    token = pump_fixture["token"]
    wanted = select_early_sigs(pump_fixture["sigs_newest_first"], token["signature"], 20)
    assert wanted[0] == token["signature"]
    txs = [parse_transaction(pump_fixture["txs"][s], s) for s in wanted]
    rows = build_early_trades(token["mint"], txs, 20)
    assert [r["seq"] for r in rows] == list(range(len(rows))) and len(rows) == 5
    assert rows[0]["slot_offset_from_creation"] == 0 and rows[0]["signer"] == token["traderPublicKey"]
    assert all(r["slot_offset_from_creation"] >= 0 for r in rows)
    assert len(build_early_trades(token["mint"], txs, 2)) == 2
    assert build_early_trades("another-mint", txs, 20) == []

    accounts = build_balance_accounts(token["mint"], token["bondingCurveKey"], {token["traderPublicKey"]}, txs,
                                      rows, max_buyers=20, slot_window=1, direct_transfers=True)
    by_owner = {a.owner: a for a in accounts}
    creator = by_owner[token["traderPublicKey"]]
    assert creator.role == "creator" and creator.is_insider
    buyers = [a for a in accounts if a.role == "early_buyer"]
    assert len(buyers) == 2 and not any(b.is_insider for b in buyers)  # both bought > 1 slot after creation
    assert token["bondingCurveKey"] not in by_owner  # the curve's own account is never tracked
    capped = build_balance_accounts(token["mint"], token["bondingCurveKey"], {token["traderPublicKey"]}, txs,
                                    rows, max_buyers=1, slot_window=1, direct_transfers=True)
    assert len([a for a in capped if a.role == "early_buyer"]) == 1


def _tx(slot: int, trades: list[TradeEvent], accounts: dict[tuple[str, str], tuple[str, int]],
        transfers: list[TokenTransfer] | None = None) -> ParsedTx:
    return ParsedTx(sig=f"sig{slot}", slot=slot, block_time=slot, ok=True, signer="payer", n_signatures=1,
                    fee_lamports=5000, priority_fee_lamports=0, tip_lamports=0, trades=trades,
                    transfers=transfers or [], post_token_accounts=accounts)


def _buy(user: str) -> TradeEvent:
    return TradeEvent(mint="M", sol_amount=10**9, token_amount=5, is_buy=True, user=user, timestamp=0,
                      virtual_sol=1, virtual_token=1, real_sol=1, real_token=1)


def test_insider_set_v1_rules() -> None:
    """creator + buyers in slot <= creation_slot + 1 + direct transfers from the creator."""
    txs = [
        _tx(100, [_buy("creator")], {("creator", "M"): ("acc_creator", 5), ("curve", "M"): ("acc_curve", 9)}),
        _tx(101, [_buy("sniper")], {("sniper", "M"): ("acc_sniper", 5)}),
        _tx(105, [_buy("late")], {("late", "M"): ("acc_late", 5)}),
        _tx(106, [], {("friend", "M"): ("acc_friend", 3), ("curve", "M"): ("acc_curve", 9)},
            [TokenTransfer("acc_creator", "acc_friend", "creator", 3, "M"),
             TokenTransfer("acc_creator", "acc_curve", "creator", 1, "M")]),  # the second one is a sale
    ]
    rows = build_early_trades("M", txs, 20)
    accounts = {a.token_account: a for a in build_balance_accounts(
        "M", "curve", {"creator"}, txs, rows, max_buyers=20, slot_window=1, direct_transfers=True)}
    assert set(accounts) == {"acc_creator", "acc_sniper", "acc_late", "acc_friend"}
    assert accounts["acc_creator"].role == "creator" and accounts["acc_creator"].is_insider
    assert accounts["acc_sniper"].is_insider and not accounts["acc_late"].is_insider
    assert accounts["acc_friend"].is_insider and accounts["acc_friend"].owner == "friend"
    without = build_balance_accounts("M", "curve", {"creator"}, txs, rows, 20, 1, direct_transfers=False)
    assert "acc_friend" not in {a.token_account for a in without}
