# rug-radar (package: rugwatch)

Spec: @HANDOFF.md and `config.yaml`. Follow them exactly. Where they are silent, pick the
simplest option and log it in `docs/decisions.md`. Never change a value in `config.yaml`
except through the rules in HANDOFF section 5.

## Session protocol
- Start of every session: read `docs/status.md` first and tell the user where we are in 3 lines.
- End of every session: update `docs/status.md` (single source of truth for progress) with what's
  done, the current milestone, the next steps, exactly what the user must do (and when), and the
  date of the next checkpoint.

## Hard rules (HANDOFF section 0)
- Paper trading only. No private keys, wallet/keypair objects, `sendTransaction` or signing code
  anywhere in the repo. `make test` greps `src/` for them and fails on a hit.
- Time-based splits only. Never a random split. Never plain accuracy as a headline.
- No leakage: a feature for snapshot S uses only rows with `available_at <= snapshot_ts(S)`;
  feature code goes through the as-of loader; the four leakage tests must pass.
- Never open, print, log or commit `.env`. API keys never appear in logs, raw records or errors.
- Free tiers only. Data access: PumpPortal free streams + RPC polling. No trade streams, no
  Helius websocket.
- Laptop runs the collector only; the desktop runs parsing, training, simulator, dashboard.
- Stop and ask the user before: spending money, changing anything in section 0, changing the
  claim sentence, or any step towards real trading.

## Working notes
- Python 3.12 via `uv` (never touch the system python3). Use the `make` targets in HANDOFF section 2.
- A negative result is acceptable; report measured values only.
