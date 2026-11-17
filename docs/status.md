# Status (single source of truth for progress)

Last updated: 2026-11-17 06:10 UTC (end of session 1).

## Where we are

- Milestone: **M1 collector** (weeks 1-2: 2026-11-17 to 2026-11-30).
- The collector is **running** as systemd user unit `rugwatch-collector` since 2026-11-17 02:49 UTC
  (enabled, lingering on: survives logout and reboot). Day 0 of the data clock.
- Next checkpoint: **2026-11-18 after 03:00 UTC** -> `make status` and `make report-day1`.

## Done

- HANDOFF.md, config.yaml, CLAUDE.md committed and pushed (`9a52fc9`, on origin/main).
  The M1 code is **not committed** (not asked).
- Scanner terms read; findings in `docs/decisions.md` section A.
- M1 code: typed config, PumpPortal listener, RPC client (rate limit, fallback, 429 retry),
  scheduler (filter ticks, tracking decision, price/balance/holder polls), snapshot batch, scanner
  clients (disabled until day 3), budget/throttle, raw writer, status and reports, systemd unit,
  Makefile targets (setup, test, lint, check-env, install-service, status, report-day1,
  report-week1, backup).
- 73 unit tests pass; ruff and mypy clean. Leakage tests come with M2.
- Live check after 3.2 h: 1,519 launches logged, 87 tokens tracked, 87 of 87 snapshots complete,
  0 websocket drops, 0 RPC errors. Numbers in `docs/decisions.md` section C.

## Known problems and open decisions

- **No working RPC fallback.** Alchemy: HTTP 403 "SOLANA_MAINNET is not enabled for this app".
  FluxRPC: HTTP 401 "invalid api key". Helius alone is carrying everything.
- **Disk: 6.4 GB free.** Raw data grows about 140 MB/day so far and is still rising. The collector
  stops itself below 0.5 GB free.
- **Spec deviation D-13 (user can veto):** early trades are read oldest-first with one Helius call,
  because busy tokens have more than 3,000 signatures by minute 5 (31% of tracked tokens so far).
  Switch: `EARLY_TRADES_ASCENDING` in `src/rugwatch/collector/snapshot.py`.
- Launch rate in the first hours was about 0.13/s (about 11k/day pace), below the ~26k/day public
  figure. Could be time of day; judge on the 24 h count in `report-day1`.
- Credits are at about 1,140/hour and rising; the throttle steps will probably start within a day.
  The tracked cap is recomputed on day 7 (rule 5.1.5).
- Observed curve fee is 125 to 203 bps per side (mean 173), above the 125 bps placeholder.
- Chrony is not installed; systemd-timesyncd keeps the clock (decision D-21).

## What the user needs to do

1. Done: `git push origin main` (2026-11-17).
2. Now: say whether the M1 code should be committed and pushed.
3. Soon: Alchemy dashboard -> enable Solana Mainnet for the app. Re-check the FluxRPC key. Then
   `make check-env` should show `ok` for both (no restart needed; the collector retries hourly).
4. Soon: free disk space on the laptop (20 GB or more), and set up `make backup DEST=...` weekly.
5. Helius dashboard: note the day of month the credits reset; add `HELIUS_BILLING_DAY=<day>` to
   `.env`, then `systemctl --user restart rugwatch-collector`.
6. **2026-11-18 after 03:00 UTC**: run `make status` and `make report-day1`, start a session and
   paste both, plus the credit count shown on the Helius dashboard.
7. **By 2026-11-20 (day 3)**: create the Solana Tracker account, add `SOLANATRACKER_API_KEY` to
   `.env`. Say whether GoPlus gave a key only or a key plus secret.
8. Read the scanner terms and decide whether to ask RugCheck / GoPlus / Solana Tracker for
   permission to publish scores (decisions section A).
9. Do not put the laptop to sleep or close it into suspend; leave it on power and network.

## Next steps (assistant)

1. Day 1: apply rule 5.1 to `report-day1` (set `threshold_sol`); check missing parts, 429s,
   throttle level, launch count against the public figure, credit ledger against the dashboard.
2. Day 3: set `scanners.enabled`, verify the Solana Tracker response shape, measure scanner limits,
   on-time coverage (rule: extend `deadline_s` to 60 once if below 80%) and Alchemy cost.
3. Day 7 (2026-11-24): `make report-week1`; tracked cap by rule 5.1.5; replace PLACEHOLDER values
   (`fee_bps_per_side`, `priority_fee_sol_per_tx`, `tracked_cap_per_day`).
4. Day 14 (2026-12-01): freeze the universe. M1 exit checks (HANDOFF section 6).
5. Then M2: `make parse`, labels, features with the as-of loader, the four leakage tests.

## Checkpoints

| Date (UTC) | What |
| --- | --- |
| 2026-11-18 | report-day1, threshold by rule 5.1 |
| 2026-11-20 | scanners on (needs the Solana Tracker key) |
| 2026-11-24 | report-week1, tracked cap, placeholders |
| 2026-11-27 | earliest 10-day uptime check (>= 95%) |
| 2026-12-01 | universe freeze, M1 exit |
