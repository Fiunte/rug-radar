# rug-radar

Research project: at ~5 minutes after a pump.fun launch, predict a sustained 50% collapse within
60 minutes better than RugCheck / GoPlus / Solana Tracker, on later unseen tokens, then test a
**paper-traded** strategy after costs. No keys, no signing, no real trades.

- Spec: [HANDOFF.md](HANDOFF.md) and [config.yaml](config.yaml)
- Progress and what to do next: [docs/status.md](docs/status.md)
- Decisions and measurements: [docs/decisions.md](docs/decisions.md)

## Milestone 1: the collector (laptop)

```
make setup            # venv from uv.lock (Python 3.12), .env from .env.example if missing
make check-env        # which variables are set (names only) and whether each RPC tier answers
make test             # unit tests + no-signing check
make lint             # ruff + mypy
make install-service  # systemd user unit, starts now, survives logout and reboot
make status           # uptime, last event age, credits, websocket drops, disk
make report-day1      # after 24 h: launches/day, pass counts per threshold, credits
make report-week1     # after 7 days: credits per tracked token, storage/day, fees, scanners
make backup        # on the DESKTOP: pull data/ from the laptop over SSH (docs/runbook.md)
```

Environment variables (in `.env`, never committed): `HELIUS_API_KEY` (required), `ALCHEMY_API_KEY`
(fallback), `FLUXRPC_API_KEY` (optional: emergency RPC tier and the websocket fallback of the create-log stream; `FLUXRPC_RPC_URL` / `FLUXRPC_WS_URL` override the URLs), `SOLANATRACKER_API_KEY` (from day 3),
`RUGCHECK_API_KEY` (optional), `HELIUS_BILLING_DAY` (optional, day of month the credits reset).

Service control:

```
systemctl --user status rugwatch-collector
systemctl --user restart rugwatch-collector
journalctl --user -u rugwatch-collector -f
```

Data lands in `data/raw/{source}/{kind}/dt=YYYY-MM-DD/part-N.jsonl.zst` (append-only) and the
collector's working state in `data/state/collector.sqlite`.
