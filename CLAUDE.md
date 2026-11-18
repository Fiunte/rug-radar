# rug-radar (package: rugwatch)

Spec: @HANDOFF.md and `config.yaml`. Follow them exactly. Where they are silent, pick the simplest
option and log it in `docs/decisions.md`. Change `config.yaml` values only via HANDOFF section 5.

## Session protocol
- Start: read `docs/status.md` first; tell the user where we are in 3 lines.
- End: update `docs/status.md` (single source of truth): done, milestone, next steps, what the user
  must do and when, and its reminders table (ntfy sends each row when due).
- Checkpoints: run the make commands yourself; ask the user only for what you can't see (Helius dashboard).
- Version control is Claude's job: `git pull --ff-only` before work; commit each working change
  with a clear message and push right after. Never force-push or rewrite history.
- Machine: the laptop runs only the collector, plus its health timer and the Remote Control server
  (systemd user units). The desktop (WSL) runs parsing, training, simulator, dashboard (runbook).

## Hard rules (HANDOFF section 0)
- Paper trading only: no private keys, wallet/keypair objects, `sendTransaction` or signing code (`make test` greps).
- Time-based splits only. Never a random split. Never plain accuracy as a headline.
- No leakage: a feature for snapshot S uses only rows with `available_at <= snapshot_ts(S)`, via
  the as-of loader; the four leakage tests must pass.
- Never open, print, log or commit `.env`. API keys never appear in logs, raw records or errors.
- Free tiers only. Data: PumpPortal free streams + public-RPC create-log stream (FluxRPC ws as
  auto fallback; coverage >= 95%) + RPC polling; no trade streams, no Helius websocket.
- Stop and ask the user before: spending money, changing anything in section 0, changing the
  claim sentence, or any step towards real trading.

## Working notes
- Python 3.12 via `uv` (never the system python3); HANDOFF section 2 `make` targets. A negative
  result is acceptable; report measured values only.
