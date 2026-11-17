"""Snapshot batch (HANDOFF section 0, config tracking.snapshot_batch).

Fired right after the 300 s filter tick accepts a token. All parts run concurrently; whatever is
not back `deadline_s` after the start is null. The batch ends with the closing reserve read:
snapshot_ts = its observed_at, P0 = its price.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from rugwatch.chain import pump_layout as pl
from rugwatch.chain.tx_parse import ParsedTx, parse_transaction
from rugwatch.collector.rpc import RpcClient
from rugwatch.collector.scanners import Scanner, query_scanner
from rugwatch.collector.state import State
from rugwatch.config import Config
from rugwatch.storage.raw import RawWriter, make_record, utc_iso

log = logging.getLogger("rugwatch.snapshot")

CLOSING_RESERVE_S = 3.0  # kept back from the deadline for the closing reserve read
# Early trades are read oldest-first in one Helius call; the page walk below is the fallback
# (docs/decisions.md D-13). Set to False to use only the page walk of the original spec.
EARLY_TRADES_ASCENDING = True
SIG_PAGE = 1000
TOKEN_ACCOUNT_SLICE = (0, 72)  # mint, owner, amount


@dataclass
class BalanceAccount:
    token_account: str
    owner: str
    role: str  # creator | early_buyer
    is_insider: bool


@dataclass
class SnapshotCtx:
    mint: str
    launch_obs: float
    curve: str
    creators: set[str]  # the curve's creator plus the launching wallet, when they differ
    creator: str | None  # the one whose history is fetched: the curve's creator, else the launcher
    creation_sig: str | None
    benchmark: bool
    done: set[str] = field(default_factory=set)
    notes: dict[str, Any] = field(default_factory=dict)
    accounts: list[BalanceAccount] = field(default_factory=list)


def select_early_sigs(sigs_newest_first: list[dict[str, Any]], creation_sig: str | None, n: int) -> list[str]:
    """Creation transaction first, then the oldest n other successful signatures, oldest first."""
    ok = [s["signature"] for s in reversed(sigs_newest_first) if s.get("err") is None]
    rest = [s for s in ok if s != creation_sig]
    if creation_sig is None:
        return rest[: n + 1]
    return [creation_sig, *rest[:n]]


def build_early_trades(mint: str, txs: list[ParsedTx], n: int) -> list[dict[str, Any]]:
    """Rows for the first n trades of `mint`, in the order given (creation tx first, then oldest)."""
    creation_slot = txs[0].slot if txs else None
    rows: list[dict[str, Any]] = []
    for tx in txs:
        for ev in tx.trades:
            if ev.mint != mint or len(rows) >= n:
                continue
            account = tx.post_token_accounts.get((ev.user, mint))
            offset = tx.slot - creation_slot if tx.slot is not None and creation_slot is not None else None
            rows.append({
                "mint": mint, "sig": tx.sig, "seq": len(rows), "slot": tx.slot, "block_time": tx.block_time,
                "signer": ev.user, "token_account": account[0] if account else None, "is_buy": int(ev.is_buy),
                "sol_lamports": ev.sol_amount, "token_amount": ev.token_amount,
                "fee_lamports": ev.total_fee_lamports, "fee_bps": ev.total_fee_bps,
                "priority_fee_lamports": tx.priority_fee_lamports, "tip_lamports": tx.tip_lamports,
                "slot_offset_from_creation": offset,
            })
    return rows


def build_balance_accounts(mint: str, curve: str, creators: set[str], txs: list[ParsedTx],
                           trades: list[dict[str, Any]], max_buyers: int, slot_window: int,
                           direct_transfers: bool) -> list[BalanceAccount]:
    """Insider set v1 and tracked accounts. Token accounts come from postTokenBalances only."""
    creation_slot = txs[0].slot if txs else None
    out: dict[str, BalanceAccount] = {}
    for tx in txs:  # creator accounts
        for (c_owner, m), (c_account, _amount) in tx.post_token_accounts.items():
            if m == mint and c_owner in creators and c_account not in out:
                out[c_account] = BalanceAccount(c_account, c_owner, "creator", True)
    buyers: dict[str, BalanceAccount] = {}
    for row in trades:  # distinct early buyers, in trade order
        user = row["signer"]
        if not row["is_buy"] or user in creators or user in buyers or len(buyers) >= max_buyers:
            continue
        account: str | None = row["token_account"] or next(
            (tx.post_token_accounts[(user, mint)][0] for tx in txs if (user, mint) in tx.post_token_accounts),
            None)
        if account is None:
            continue  # bought and closed the account inside the parsed window: nothing to poll
        early = (creation_slot is not None and row["slot"] is not None
                 and row["slot"] <= creation_slot + slot_window)
        buyers[user] = BalanceAccount(account, user, "early_buyer", early)
    for buyer in buyers.values():
        out.setdefault(buyer.token_account, buyer)
    if direct_transfers:
        for tx in txs:
            owner_of = {account: owner for (owner, m), (account, _a) in tx.post_token_accounts.items()
                        if m == mint}
            for tr in tx.transfers:
                if tr.mint != mint or tr.authority not in creators or not tr.destination:
                    continue
                dest_owner = owner_of.get(tr.destination)
                if dest_owner is None or dest_owner == curve or dest_owner in creators:
                    continue  # a sale into the curve, or the creator's own account
                existing = out.get(tr.destination)
                if existing is not None:
                    existing.is_insider = True
                else:
                    out[tr.destination] = BalanceAccount(tr.destination, dest_owner, "early_buyer", True)
    return list(out.values())


class Snapshotter:
    def __init__(self, cfg: Config, state: State, rpc: RpcClient, writer: RawWriter,
                 scanners: list[Scanner], http: httpx.AsyncClient) -> None:
        self.cfg = cfg
        self.tr = cfg.tracking
        self.commitment = cfg.collector.commitment
        self.state = state
        self.rpc = rpc
        self.writer = writer
        self.scanners = scanners
        self.http = http

    # ------------------------------------------------------------------ run
    async def run(self, ctx: SnapshotCtx) -> None:
        batch = self.tr.snapshot_batch
        parts = [p for p in batch.parts if p != "scanners" or ctx.benchmark]
        jobs = [self._guard(self._chain(ctx)), self._guard(self._creator(ctx)), self._guard(self._holders(ctx))]
        if ctx.benchmark:
            jobs.append(self._guard(self._scanners(ctx)))
        tasks = [asyncio.ensure_future(j) for j in jobs]
        _done, pending = await asyncio.wait(tasks, timeout=max(0.0, batch.deadline_s - CLOSING_RESERVE_S))
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

        closing = await self.rpc.get_multiple_accounts(
            [ctx.curve], purpose="snapshot", commitment=self.commitment, mint=ctx.mint,
            tick_s=batch.start_at_s, part="closing_reserve_read", items=[{"mint": ctx.mint}])
        bc = None
        if closing.ok:
            data = pl.decode_account_b64((closing.result.get("value") or [None])[0])
            bc = pl.decode_bonding_curve(data) if data else None
        price = bc.price if bc else None
        if bc is not None and price is not None:
            ctx.done.add("closing_reserve_read")
        missing = [p for p in parts if p not in ctx.done]
        summary: dict[str, Any] = {
            "mint": ctx.mint, "snapshot_complete": not missing, "missing_parts": missing,
            "benchmark": ctx.benchmark, "n_balance_accounts": len(ctx.accounts), **ctx.notes,
        }
        fields: dict[str, Any] = {"snapshot_complete": int(not missing), "missing_parts": ",".join(missing)}
        if bc is not None and price is not None:
            ts = closing.observed_at
            summary.update(snapshot_ts=utc_iso(ts), snapshot_slot=closing.slot, p0=price)
            fields.update(
                snapshot_ts=ts, snapshot_slot=closing.slot, p0=price, peak_price=price, last_price=price,
                last_vq=bc.virtual_quote, last_vt=bc.virtual_token, last_change_obs=ts,
                price_nominal=ts, next_price_due=ts + self.tr.price_cadence[0][1],
                graduated_ts=ts if bc.complete else None)
            if ctx.accounts:
                fields.update(balance_nominal=ts, next_balance_due=ts + self.tr.balance_cadence[0][1])
            later = [s for s in self.tr.largest_holders_at_s if s > batch.start_at_s]
            if later:
                fields["holders_due"] = ctx.launch_obs + later[0]
        else:
            summary.update(snapshot_ts=None, snapshot_slot=None, p0=None)
            fields["flags"] = "snapshot_failed"
        self.state.update_tracking(ctx.mint, **fields)
        self.writer.write(make_record(
            observed_at=closing.observed_at, source="collector", kind="snapshot_summary", purpose="snapshot",
            status="ok" if not missing else "error", payload=summary, mint=ctx.mint, tick_s=batch.start_at_s,
            slot=closing.slot))
        log.info("snapshot %s complete=%s missing=%s p0=%s", ctx.mint, not missing, missing, price)

    async def _guard(self, coro: Any) -> None:
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("snapshot part failed")

    # ---------------------------------------------------------------- parts
    async def _sig_pages(self, address: str, mint: str, purpose: str, part: str,
                         max_pages: int) -> tuple[list[dict[str, Any]], bool] | None:
        out: list[dict[str, Any]] = []
        before: str | None = None
        for _ in range(max_pages):
            res = await self.rpc.get_signatures(address, purpose=purpose, commitment=self.commitment, mint=mint,
                                                before=before, limit=SIG_PAGE, part=part)
            if not res.ok or not isinstance(res.result, list):
                return None
            out.extend(res.result)
            if len(res.result) < SIG_PAGE:
                return out, False
            before = res.result[-1]["signature"]
        return out, True

    async def _chain(self, ctx: SnapshotCtx) -> None:
        """signatures and early_trades, then balances (which needs the parsed early trades)."""
        cfg = self.tr.early_trades
        pages, oldest = await asyncio.gather(
            self._sig_pages(ctx.curve, ctx.mint, "snapshot", "signatures", cfg.max_signature_pages),
            self._oldest_txs(ctx, cfg.n + 1))
        truncated = True
        if pages is not None:
            sigs, truncated = pages
            ctx.done.add("signatures")
            ctx.notes.update(n_sigs_to_snapshot=len(sigs), sigs_truncated=truncated)
        ctx.notes["early_trades_source"] = "ascending" if oldest is not None else "page_walk"
        if oldest is not None:
            txs, early_ok = oldest, True
        elif pages is None:
            return
        else:  # page walk: complete only if the pages reach back to the creation transaction
            wanted = select_early_sigs(sigs, ctx.creation_sig, cfg.n)
            results = await asyncio.gather(*(
                self.rpc.get_transaction(sig, purpose="snapshot", commitment=self.commitment, mint=ctx.mint,
                                         part="early_trades") for sig in wanted))
            txs = []
            for sig, res in zip(wanted, results, strict=True):
                if not res.ok or not isinstance(res.result, dict):
                    return  # a missing transaction would silently shift "the first 20 trades"
                txs.append(parse_transaction(res.result, sig))
            early_ok = not truncated
        ctx.notes["early_trades_truncated"] = not early_ok
        trades = build_early_trades(ctx.mint, txs, cfg.n)
        for row in trades:
            self.state.x(
                "INSERT OR REPLACE INTO early_trades VALUES(:mint,:sig,:seq,:slot,:block_time,:signer,"
                ":token_account,:is_buy,:sol_lamports,:token_amount,:fee_lamports,:fee_bps,"
                ":priority_fee_lamports,:tip_lamports,:slot_offset_from_creation)", row)
        if txs and ctx.creation_sig is not None:
            self.state.update_tracking(ctx.mint, creation_slot=txs[0].slot, creation_block_time=txs[0].block_time)
            ctx.notes.update(creation_slot=txs[0].slot, creation_block_time=txs[0].block_time)
        if early_ok:
            ctx.done.add("early_trades")

        ins = self.cfg.labels.insider_set
        ctx.accounts = build_balance_accounts(
            ctx.mint, ctx.curve, ctx.creators, txs, trades, self.tr.early_buyers_tracked,
            ins.slot_window_after_creation, ins.direct_transfer_from_creator)
        for acc in ctx.accounts:
            self.state.x("INSERT OR REPLACE INTO balance_accounts VALUES(?,?,?,?,?)",
                         (ctx.mint, acc.token_account, acc.owner, acc.role, int(acc.is_insider)))
        if not ctx.accounts:
            ctx.done.add("balances")
            return
        res = await self.rpc.get_multiple_accounts(
            [a.token_account for a in ctx.accounts], purpose="snapshot", commitment=self.commitment,
            mint=ctx.mint, tick_s=self.tr.snapshot_batch.start_at_s, part="balances",
            items=[{"mint": ctx.mint, "token_account": a.token_account, "owner": a.owner, "role": a.role,
                    "is_insider": a.is_insider} for a in ctx.accounts],
            data_slice=TOKEN_ACCOUNT_SLICE)
        if res.ok:
            ctx.done.add("balances")

    async def _oldest_txs(self, ctx: SnapshotCtx, limit: int) -> list[ParsedTx] | None:
        """Oldest successful transactions of the curve, oldest first; None if unavailable or inconsistent."""
        if not EARLY_TRADES_ASCENDING:
            return None
        res = await self.rpc.get_oldest_transactions(ctx.curve, limit=limit, purpose="snapshot",
                                                     commitment=self.commitment, mint=ctx.mint, part="early_trades")
        data = res.result.get("data") if res.ok and isinstance(res.result, dict) else None
        if not isinstance(data, list) or not data:
            return None
        try:
            txs = [parse_transaction(entry) for entry in data]
        except (KeyError, TypeError, IndexError):
            return None
        if ctx.creation_sig is not None and txs[0].sig != ctx.creation_sig:
            return None  # the history index does not start at the creation transaction: do not trust it
        return txs

    async def _creator(self, ctx: SnapshotCtx) -> None:
        cfg = self.tr.creator_history
        creator = ctx.creator
        if creator is None:
            return
        fetched = self.state.one("SELECT fetched_at FROM creator_cache WHERE creator=?", (creator,))
        if fetched is not None and time.time() - float(fetched) < cfg.cache_ttl_days * 86400:
            ctx.notes["creator_cache_hit"] = True
            ctx.done.add("creator_history")
            return
        ctx.notes["creator_cache_hit"] = False
        pages = await self._sig_pages(creator, ctx.mint, "creator", "creator_history", cfg.max_signature_pages)
        if pages is None:
            return
        sigs, truncated = pages
        if cfg.fetch_oldest_tx and sigs and not truncated:
            res = await self.rpc.get_transaction(sigs[-1]["signature"], purpose="creator",
                                                 commitment=self.commitment, mint=ctx.mint, part="creator_history")
            if not res.ok:
                return
        self.state.x("INSERT OR REPLACE INTO creator_cache VALUES(?,?)", (creator, time.time()))
        ctx.done.add("creator_history")

    async def _holders(self, ctx: SnapshotCtx) -> None:
        res = await self.rpc.get_largest_accounts(ctx.mint, commitment=self.commitment,
                                                  tick_s=self.tr.snapshot_batch.start_at_s, part="largest_holders")
        if res.ok:
            ctx.done.add("largest_holders")

    async def _scanners(self, ctx: SnapshotCtx) -> None:
        results = await asyncio.gather(*(self._one_scanner(s, ctx.mint) for s in self.scanners))
        if all(results):
            ctx.done.add("scanners")

    async def _one_scanner(self, scanner: Scanner, mint: str) -> bool:
        """Exactly one query per (mint, scanner): the row is claimed before the request is sent."""
        cur = self.state.db.execute("INSERT OR IGNORE INTO scanner_done(mint,scanner) VALUES(?,?)",
                                    (mint, scanner.name))
        if cur.rowcount != 1:
            return False
        started = time.monotonic()
        status, payload, observed_at, latency = "timeout", {"error": "cancelled_at_deadline"}, time.time(), 0
        try:
            resp = await query_scanner(scanner, self.http, mint, self.cfg.scanners.timeout_s)
            status, payload, observed_at, latency = resp.status, resp.payload, resp.observed_at, resp.latency_ms
            return status == "ok"
        finally:
            if status == "timeout" and latency == 0:
                observed_at, latency = time.time(), int((time.monotonic() - started) * 1000)
            self.state.x("UPDATE scanner_done SET observed_at=?, status=?, latency_ms=? WHERE mint=? AND scanner=?",
                         (observed_at, status, latency, mint, scanner.name))
            self.writer.write(make_record(
                observed_at=observed_at, source=scanner.name, kind="scanner", purpose="scanner", status=status,
                payload=payload, mint=mint, tick_s=self.cfg.scanners.query_at_s, latency_ms=latency,
                request={"method": "GET", "part": "scanners"}))
