# Status (single source of truth for progress)

Last updated: 2026-11-17 12:05 UTC (end of session 3).

## Where we are

- Milestone: **M1 collector** (2026-11-17 to 2026-12-01). M1 is done only when the exit checks
  below pass.
- Collector running as systemd user unit `rugwatch-collector` since 2026-11-17 02:49 UTC.
  Redeployed 10:56 UTC with the new raw writer, the stall watchdog and the systemd watchdog; restarted
  11:59 UTC with the full RPC chain (Helius -> Alchemy -> FluxRPC all `ok`) and Helius billing day 17.
- **Waiting for the user: launch discovery.** PumpPortal's free stream misses about 78% of
  pump.fun launches (v1 transactions). The free fix changes section 0 and needs approval
  (`docs/decisions.md`, section C and the first open item).
- Next checkpoint: **2026-11-18 after 03:00 UTC** -> `make status` and `make report-day1`.

## Done

- Spec files and CLAUDE.md pushed (`9a52fc9`); M1 code pushed (`11c50ae`). Every working change is
  now committed and pushed.
- Session 2 (2026-11-17):
  - D-13 accepted.
  - Scanner policy: raw scores stay private; GoPlus permission email drafted.
  - Disk growth analysed and the raw writer fixed (-29%, D-27).
  - Wall-clock bug in a scheduler test fixed.
  - Watchdogs added: stall restart after 600 s with no launch, systemd `WatchdogSec`, every
    stop logged (D-26).
  - PumpPortal silence timeout raised to 180 s (D-28).
  - NTP offset shown in `make status` (-6.7 ms, so timesyncd is kept).
  - `make backup` now pulls from the desktop (D-29).
  - `docs/runbook.md` written: disk cleanup, reboot test, SSH, fixed IP, desktop, Claude Code.
  - Entry-point question recorded (D-30).
  - CLAUDE.md now has the version-control rule.
- Session 3 (2026-11-17): FluxRPC and Alchemy verified working; optional `FLUXRPC_RPC_URL` added;
  `make status` and the reports now read `HELIUS_BILLING_DAY` (they always assumed day 1).
- 80 unit tests pass; ruff and mypy clean.

## M1 exit checks (HANDOFF section 6) and how they will be met

| Check | How | Date |
| --- | --- | --- |
| Uptime >= 95% over 10 days | heartbeats + logged stops (`make status`) | first possible 2026-11-27 |
| Helius projection <= 800,000/month | `make status`, `report-week1` | 2026-11-24 |
| Labels v0 | `make parse` + `make labels` on the desktop | 2026-11-25 |
| 50-token audit (rule 5.2) | `make audit-sheet` 11-25; user marks it; `make audit-score` 11-28; if it fails, one adjustment round and a fresh 50 by 12-01 | 2026-11-25 to 12-01 (days 8-14) |
| Every PLACEHOLDER replaced | from `report-week1` | 2026-11-24 |
| Scanner terms checked | done, section A (GoPlus permission pending) | done |
| Backup working | desktop set up, first `make backup`, weekly task | by 2026-11-22 |

## Known problems and open decisions

- **Launch coverage (needs the user's decision):** about 22% of launches reach us. See above.
- **Disk: 6.5 GB free.** Raw data grows about 80-100 MB/day after the fix; the main cost is the
  signature pages set by `max_signature_pages: 3`. Full launch coverage would add only launch logs
  and filter reads (a few MB/day), because the tracked tokens stay capped.
- Credits: projection about 705k/30 d (target 800k); the throttle steps may start within days.
- Observed curve fee 125-203 bps per side (mean 173), above the 125 bps placeholder.

## What the user needs to do

1. **Now: decide on launch discovery.** Approve or refuse adding the public-RPC `logsSubscribe`
   launch source (free; a change to section 0).
2. **Now: send the GoPlus permission email** (draft in the session-2 answer).
3. Soon: free disk space: `docs/runbook.md` step 1 (about 19.7 GB of safe deletions).
4. Done: RPC fallback (Alchemy and FluxRPC `ok`) and `HELIUS_BILLING_DAY` (17).
5. Fix the allow rule: `/permissions` shows `Bash(got push origin main)` (typo); it should be
   `Bash(git push origin main)`.
6. **By 2026-11-22:** runbook steps 3 and 4 (SSH, fixed IP, desktop, first backup, weekly task,
   Claude Code), then step 2 (reboot test, checked from the desktop).
7. **2026-11-18 after 03:00 UTC:** run `make status` and `make report-day1`; paste both, plus the
   Helius dashboard credit count.
8. **By 2026-11-20 (day 3):** Solana Tracker account; add `SOLANATRACKER_API_KEY` to `.env`.
   Say whether GoPlus gave a key only or a key plus secret.
9. Keep the laptop on power and network; do not suspend it.

## Next steps (assistant)

1. If approved: build the second launch source, record per token which source saw it, and deploy.
   The universe is not frozen yet, so `universe_version` stays.
2. Day 1 (2026-11-18): rule 5.1 from `report-day1` (threshold); check credits against the
   dashboard, 429s, throttle level and the launch count against the independent on-chain count.
3. Day 3 (2026-11-20): scanners on; check the Solana Tracker response shape; scanner limits and
   on-time coverage (extend `deadline_s` to 60 once if below 80%).
4. Week 2 on the desktop: `make parse`, `make labels` (labels v0), `make audit-sheet`,
   `make audit-score`.
5. Day 7 (2026-11-24): `report-week1`; tracked cap by rule 5.1.5; replace the PLACEHOLDERs.
6. Day 14 (2026-12-01): universe freeze; M1 exit checks; then M2.

## Checkpoints

| Date (UTC) | What |
| --- | --- |
| 2026-11-18 | report-day1, threshold by rule 5.1 |
| 2026-11-20 | scanners on (needs the Solana Tracker key) |
| 2026-11-22 | backup working, desktop set up, reboot test done |
| 2026-11-24 | report-week1, tracked cap, placeholders |
| 2026-11-25 | labels v0, audit sheet out |
| 2026-11-27 | earliest 10-day uptime check (>= 95%) |
| 2026-11-28 | audit scored (rule 5.2) |
| 2026-12-01 | universe freeze, M1 exit |
