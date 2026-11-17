# Status (single source of truth for progress)

Last updated: 2026-11-17 12:35 UTC (end of session 4).

## Where we are

- Milestone: **M1 collector**. M1 is done only when the exit checks below pass.
- **Data clock restarted at 2026-11-17 12:11:51 UTC** (D-32). That is when the collector 0.2.0
  started with both launch sources: PumpPortal plus the public-RPC create-log stream (D-31,
  approved). Launches observed earlier are pre-fix data: kept, flagged, never used for models or
  claims. "Day N" counts from the restart.
- Collector: systemd user unit `rugwatch-collector`, RPC chain Helius -> Alchemy -> FluxRPC,
  Helius billing day 17, stall and systemd watchdogs on.
- Next checkpoint: **2026-11-18 after 12:15 UTC** -> `make status` and `make report-day1`.

## Done

- Sessions 1-2: M1 collector, tests, Makefile, systemd unit; raw writer compression (-29%);
  watchdogs; backup pull; runbook; decisions up to D-30.
- Session 3: RPC fallback verified (Alchemy, FluxRPC `ok`); billing day read by the reports.
- Session 4 (2026-11-17):
  - Second launch source built and deployed (0.2.0), with a daily coverage check against the chain
    (alert below 95%), rate-limit detection and per-source reconnects.
  - HANDOFF section 0 updated with the user's approval (D-31).
  - Pre-fix data policy and data clock (D-32).
  - GoPlus: permission email sent by the user; GoPlus stays in private analysis until they reply.
  - Disk: pip and JetBrains caches deleted (11.5 GB free).
- 89 unit tests pass; ruff and mypy clean. Every change is committed and pushed.

## M1 exit checks (HANDOFF section 6) and how they will be met

| Check | How | Date (UTC) |
| --- | --- | --- |
| Launch coverage >= 95% (user's condition) | daily check in `make status` | daily from 2026-11-17 |
| Uptime >= 95% over 10 days | heartbeats + logged stops (`make status`) | first possible 2026-11-27 |
| Helius projection <= 800,000/month | `make status`, `report-week1` | 2026-11-24 12:15 |
| Labels v0 | `make parse` + `make labels` on the desktop | 2026-11-25 |
| 50-token audit (rule 5.2) | `make audit-sheet` 11-25; user marks it; `make audit-score` 11-28; if it fails, one adjustment round and a fresh 50 by 12-01 | 2026-11-25 to 12-01 (days 8-14) |
| Every PLACEHOLDER replaced | from `report-week1` | 2026-11-24 12:15 |
| Scanner terms checked | done (section A); GoPlus permission pending | done |
| Backup working | desktop set up, first `make backup`, weekly task | by 2026-11-22 |

## Known problems and open decisions

- Public RPC endpoint: free, not meant for sustained use. If `make status` shows
  `rate_limited_24h` > 0, the proposed fallback is FluxRPC's websocket (needs the user's approval
  and the dashboard's "WebSocket" value; D-31).
- Launches created while a stream reconnects are not backfilled; the daily coverage check
  measures the loss.
- With full coverage, 5 SOL passes about 4-5x more often than before; the tracked cap hash-samples
  for now, and rule 5.1 sets the threshold on day 1.
- Disk: 11.5 GB free; the user's sudo cleanup will add about 9.7 GB, and the Chrome and VS Code
  caches another 5.2 GB.
- Credits: projection about 715k/30 d (target 800k). Recheck after a day at full coverage.
- Observed curve fee 125-203 bps per side (mean 173), above the 125 bps placeholder.

## What the user needs to do

1. **Now:** paste the sudo cleanup block from the session-4 answer (journal, apt, old snaps).
   Later, close Chrome and VS Code and run `rm -rf ~/.cache/google-chrome ~/.cache/vscode-cpptools`,
   or tell Claude to do it.
2. **When GoPlus replies:** paste the answer into a session.
3. **By 2026-11-20 12:15 UTC (day 3):** Solana Tracker account; add `SOLANATRACKER_API_KEY` to
   `.env`. Say whether GoPlus gave a key only or a key plus secret.
4. **By 2026-11-22:** runbook steps 3 and 4 (SSH, fixed IP, desktop, first backup, weekly task,
   Claude Code), then step 2 (reboot test, checked from the desktop).
5. **2026-11-18 after 12:15 UTC:** run `make status` and `make report-day1`; paste both, plus the
   Helius dashboard credit count.
6. Keep the laptop on power and network; do not suspend it.

## Next steps (assistant)

1. Day 1 (2026-11-18 12:15 UTC): rule 5.1 from `report-day1` (threshold); check the coverage
   check, rate limits, credits against the dashboard, 429s, throttle level.
2. Day 3 (2026-11-20): scanners on; check the Solana Tracker response shape; scanner limits and
   on-time coverage (extend `deadline_s` to 60 once if below 80%).
3. Week 2 on the desktop: `make parse`, `make labels` (labels v0), `make audit-sheet`,
   `make audit-score`.
4. Day 7 (2026-11-24 12:15): `report-week1`; tracked cap by rule 5.1.5; replace the PLACEHOLDERs.
5. Day 14 (2026-12-01 12:15): universe freeze; M1 exit checks; then M2.

## Checkpoints

| Date (UTC) | What |
| --- | --- |
| 2026-11-18 12:15 | report-day1, threshold by rule 5.1, first daily coverage check reviewed |
| 2026-11-20 12:15 | scanners on (needs the Solana Tracker key) |
| 2026-11-22 | backup working, desktop set up, reboot test done |
| 2026-11-24 12:15 | report-week1, tracked cap, placeholders |
| 2026-11-25 | labels v0, audit sheet out |
| 2026-11-27 | earliest 10-day uptime check (>= 95%) |
| 2026-11-28 | audit scored (rule 5.2) |
| 2026-12-01 12:15 | universe freeze, M1 exit |
