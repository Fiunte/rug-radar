# report-day1
generated 2026-11-18 22:13 UTC

Data clock start: 2026-11-17 12:11 UTC (both launch sources). Launches observed earlier are pre-fix data and are excluded below.

Coverage: 34.0 h of launches (2026-11-17 to 2026-11-18); per-day figures below are scaled to 24 h (x0.71).
Launches: 71,374 observed = 50,345 per day (independent public figure: ~26,000/day).

## Pass counts per threshold (per day, SOL-quoted, not graduated)

| threshold SOL | t=60s | t=180s | t=300s |
|---|---|---|---|
| 4 | 2,114 | 1,303 | 957 |
| 5 | 1,877 | 1,146 | 864 |
| 7.5 | 1,531 | 935 | 712 |
| 10 | 1,322 | 807 | 602 |
| 15 | 1,014 | 655 | 463 |
| 20 | 774 | 478 | 351 |
| 30 | 356 | 229 | 188 |

Rule 5.1: smallest grid value with <= 500 passes/day at 300 s is **15 SOL** (config now: 5).

## Outcome of the 300 s tick (share of launches that reached it: n=71,185)

- quoted in another token, not SOL (outside the universe): 17,258 (24.2%)
- graduated before the tick (outside the universe): 716 (1.0%)
- empty curve, real SOL = 0 (dead; outside the universe): 2,563 (3.6%)
- 0 < real SOL < 4: 49,195 (69.1%)
- real SOL >= 4 (candidates for the filter): 1,357 (1.9%)
- tick missed by the collector: 0 (0.0%)
- read failed or account missing: 96 (0.1%)

## Tracking

- tracked: filter_pass=579  random_rest=101
- snapshots complete: 680 of 680

## Credits

- alchemy / price: calls=1 credits=0 errors=0
- helius / snapshot: calls=3,275 credits=9,521 errors=0
- helius / balance: calls=8,659 credits=8,659 errors=0
- helius / price: calls=8,215 credits=8,215 errors=1
- helius / filter: calls=8,214 credits=8,214 errors=0
- helius / holders: calls=1,384 credits=1,384 errors=0
- helius / creator: calls=1,358 credits=1,358 errors=0
- helius / coverage: calls=35 credits=35 errors=0
- Helius projection: 757,380 credits per 30 days (target 800,000). Reconcile with the Helius dashboard.

## Reliability

- websocket drops: 13; total time disconnected: 69 s; longest gap: 15 s
- uptime: alive 100.0%, PumpPortal connected 100.0%, public RPC stream connected 100.0%

## Launch sources (D-31)

- public rpc launch stream drops: total=1  last_24h=1  disconnected_24h=1 s  rate_limited_24h=0
- create-log stream: via publicrpc since 11-17 13:27 UTC  switches_24h=0  fluxrpc bandwidth 2026-11: 0.00 of 10 GB (ws 0.00, http 0.00)
- launch coverage (check 11-18 13:27 UTC, 60 min window, ~2529.0 creates on chain): all=99.8%  publicrpc=99.8%  pumpportal=22.9%
- launch observed_at minus creation block_time (s): p10=0.41  p25=0.62  p50=0.90  p75=1.14  p90=1.36  p99=1.86
