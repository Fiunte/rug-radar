# rug-radar (package: rugwatch)

Spec: @HANDOFF.md and `config.yaml`. Follow them exactly. Where they are silent, pick the simplest
option and log it in `docs/decisions.md`. Change `config.yaml` values only via HANDOFF section 5.

## Session protocol
- Start: read `docs/status.md` first; tell the user where we are in 3 lines.
- End: update `docs/status.md` (single source of truth): what's done, current milestone, next
  steps, exactly what the user must do (and when), date of the next checkpoint.
- Version control is Claude's job: `git pull --ff-only` before work; commit each working change
  with a clear message and push right after. Never force-push or rewrite history.
- Machine: the laptop has `~/.config/systemd/user/rugwatch-collector.service` and runs only the
  collector. The desktop (WSL) runs parsing, training, simulator, dashboard (`docs/runbook.md`).

## Hard rules (HANDOFF section 0)
- Paper trading only. No private keys, wallet/keypair objects, `sendTransaction` or signing code
  anywhere in the repo. `make test` greps `src/` for them and fails on a hit.
- Time-based splits only. Never a random split. Never plain accuracy as a headline.
- No leakage: a feature for snapshot S uses only rows with `available_at <= snapshot_ts(S)`, via
  the as-of loader; the four leakage tests must pass.
- Never open, print, log or commit `.env`. API keys never appear in logs, raw records or errors.
- Free tiers only. Data: PumpPortal free streams + public-RPC create-log stream (FluxRPC ws as
  auto fallback; coverage >= 95%) + RPC polling; no trade streams, no Helius websocket.
- Stop and ask the user before: spending money, changing anything in section 0, changing the
  claim sentence, or any step towards real trading.

## Working notes
- Python 3.12 via `uv` (never touch the system python3); use the HANDOFF section 2 `make` targets.
- A negative result is acceptable; report measured values only.
