# Status (single source of truth for progress)

Last updated: 2026-11-18 22:40 UTC (end of session 6).

## Where we are

- Milestone: **M1 collector**, day 1 done. M1 is done only when the exit checks below pass.
- **Data clock started 2026-11-17 12:11:51 UTC** (D-32). "Day N" counts from it.
- Collector 0.2.2: `threshold_sol` = **15 SOL** by rule 5.1 (D-34). RPC chain Helius -> Alchemy ->
  FluxRPC, PumpPortal + public-RPC create-log stream (FluxRPC websocket as automatic fallback, D-33).
- Health timer every 10 min with phone alerts, daily summary and reminders (D-35). It waits for
  `NTFY_TOPIC` and `HEALTHCHECKS_PING_URL` in `.env`.
- Remote Control server for the phone installed (D-36). It waits for the user's one-time `y`.
- Next checkpoint: **2026-11-20 12:15 UTC, day 3** (scanners on; needs the Solana Tracker key).

## Done

- Sessions 1-5: M1 collector, tests, Makefile, systemd unit, watchdogs, backup pull, runbook;
  second launch source and daily coverage check; FluxRPC websocket fallback; decisions up to D-33.
- Session 6 (2026-11-18):
  - Day-1 checkpoint. Rule 5.1 set the threshold to 15 SOL (462 passes/day at 300 s). Credits
    reconcile with the dashboard (47,184 vs 47,395, 0.4%). Coverage 99.8%; 0 rate limits; 0 Helius
    errors.
  - Report fixes: the outcome rows no longer overlap; uptime counts from the data clock. The
    sampling rate is recounted at the new threshold.
  - Alerts (`make health`, `make notify-test`), reminders from the table below, runbook section 7.
  - Remote Control unit (`make install-remote`), runbook section 8.
  - Two date-dependent raw-writer tests fixed. 101 tests pass; ruff and mypy clean.

## M1 exit checks (HANDOFF section 6) and how they will be met

| Check | How | Date (UTC) |
| --- | --- | --- |
| Launch coverage >= 95% (user's condition) | daily check; alert below 95% | daily; 99.6%, 99.8% so far |
| Uptime >= 95% over 10 days | heartbeats + logged stops; alerts + healthchecks.io | first possible 2026-11-27 12:15 |
| Helius projection <= 800,000/month | `make status`, `report-week1` | 2026-11-24 12:15 (now 755k, throttle 2) |
| Labels v0 | `make parse` + `make labels` on the desktop | 2026-11-25 |
| 50-token audit (rule 5.2) | `make audit-sheet` 11-25; user marks it; `make audit-score` 11-28; if it fails, one adjustment round and a fresh 50 by 12-01 | 2026-11-25 to 12-01 |
| Every PLACEHOLDER replaced | `threshold_sol` done; the rest from `report-week1` | 2026-11-24 12:15 |
| Scanner terms checked | done (section A); GoPlus permission pending | done |
| Backup working | desktop set up, first `make backup`, weekly task | by 2026-11-22 |

## Known problems and open decisions

- **Collector location (open, user's decision):** taking the laptop to class means data gaps. The
  10-day uptime rule allows 12 h off in total. Moving the collector changes HANDOFF section 0.
- Throttle at level 2 since 2026-11-18 09:00 UTC (balance cadence x2 after 1 h, random rest
  50/day). The tracked count stays about the same at 15 SOL, so it may stay on; rule 5.1.5 sets
  the tracked cap at day 7.
- Launches are about 50k/day, twice the HANDOFF's public figure; the chain check agrees. 24% are
  quoted in a token other than SOL (outside the universe).
- Public RPC endpoint: free, not meant for sustained use; automatic FluxRPC fallback (D-33).
- Launches created while a stream reconnects are not backfilled; the daily coverage check measures
  the loss.
- Observed curve fee 125-203 bps per side (mean 173), above the 125 bps placeholder.

## What the user needs to do

1. **Now:** accept Remote Control once. Run `tmux -L claude-rc attach -t rc`, press `y`, then
   Ctrl+b, d. Connect from the Claude app via **Code** (runbook section 8).
2. **Now:** phone alerts setup, runbook section 7 steps 1-4 (ntfy app, `NTFY_TOPIC`,
   healthchecks.io, `HEALTHCHECKS_PING_URL`). Then tell Claude "alerts are set up".
3. **Decide:** where the collector runs (options in the session-6 answer; open item above).
4. **When GoPlus replies:** paste the answer into a session.
5. The rest arrives as reminders (table below). Keep the laptop on power and network meanwhile.

## Next steps (assistant)

1. After the user's alert setup: `make notify-test`, confirm the test arrived, check `make health`.
2. Day 3 (2026-11-20 12:15): scanners on; Solana Tracker response shape; scanner limits and
   on-time coverage (extend `deadline_s` to 60 once if below 80%).
3. Week 2 on the desktop: `make parse`, `make labels` (labels v0), `make audit-sheet`,
   `make audit-score`.
4. Day 7 (2026-11-24 12:15): `report-week1`; rule 5.1.6 recheck; tracked cap by rule 5.1.5;
   PLACEHOLDERs; health thresholds rechecked.
5. Day 14 (2026-12-01 12:15): universe freeze; M1 exit checks; then M2.

## Checkpoints and reminders

The health timer sends each row once, when due, through ntfy (times are UTC = Lisbon in winter).
Claude rows: open Claude Code (or the Claude app) and say the text in the last column. Remove
rows when done; add rows for new to-dos.

| Due (UTC) | Who | What | Say or do |
| --- | --- | --- | --- |
| 2026-11-19 18:00 | you | Solana Tracker key | Create the free Solana Tracker account and add SOLANATRACKER_API_KEY to .env before the day-3 checkpoint (11-20 12:15 UTC). |
| 2026-11-20 12:15 | Claude | Day-3 checkpoint (scanners on) | run the day-3 checkpoint |
| 2026-11-21 10:00 | you | Desktop and first backup (due 11-22) | Runbook steps 3-4 (SSH, fixed IP, desktop, first backup, weekly task), then the reboot test (step 2). |
| 2026-11-24 12:15 | Claude | Day-7 checkpoint (report-week1, tracked cap) | run the day-7 checkpoint |
| 2026-11-25 12:15 | Claude | Labels v0 and the audit sheet | run the labels checkpoint |
| 2026-11-27 12:15 | Claude | 10-day uptime check | run the uptime checkpoint |
| 2026-11-27 18:00 | you | Audit sheet marked | Mark agree/disagree and your class for each of the 50 tokens; Claude scores it on 11-28. |
| 2026-11-28 12:15 | Claude | Audit scoring (rule 5.2) | run the audit checkpoint |
| 2026-12-01 12:15 | Claude | Day-14 checkpoint (freeze, M1 exit) | run the day-14 checkpoint |
