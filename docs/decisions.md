# Decisions log

Where HANDOFF.md / config.yaml are silent, the simplest option was taken and recorded here.
Dates are UTC. "Verified" means checked against a live source on the date given.

## A. Scanner terms: may we store scores, may we publish them? (2026-11-17)

I am not a lawyer; HANDOFF 6 item 8 still asks you to read the terms yourself. Summary of what the
published terms say:

| Scanner | Store scores for our analysis | Publish raw scores in a public dataset | Key needed |
| --- | --- | --- | --- |
| RugCheck | Nothing forbids it (no terms published) | **No explicit permission: treat as restricted** until RugCheck says yes in writing | No |
| GoPlus | Not addressed; §6 bars "data collection and mining" without permission, so ask | **Restricted**: no grant to redistribute; attribution "Powered by GoPlus" required | No (public tier) |
| Solana Tracker | Not forbidden | **No**: §6.4 prohibits redistribution of raw API data without prior written authorisation | Yes |

Consequence (HANDOFF 6 item 8): all three stay in the analysis; **raw scores of all three stay out
of the public data release** unless written permission arrives. Aggregate results (PR-AUC per
scanner, coverage, latency) are derived data and can be published.

**RugCheck** — https://rugcheck.xyz , https://api.rugcheck.xyz/swagger/doc.json
- No terms of service or API terms are published: the sitemap lists only `/privacy` and `/api`; the
  Swagger document has no `termsOfService` or `license` field and an empty `contact`.
- API key: **not needed** for `GET /v1/tokens/{mint}/report/summary`. Verified live: HTTP 200 without
  a key, response header `x-rate-limit-limit: 15` (window not stated). The site says a personal key
  stops you "sharing the public quota"; `refresh=true` and `/v1/tokens/verified` are paid-key only.
- Response fields: `score`, `score_normalised`, `risks[]`, `lpLockedPct`, `tokenProgram`, `tokenType`.
- To do (user): ask RugCheck for permission to publish scores, or keep them out of the release.

**GoPlus** — https://docs.gopluslabs.io/reference/api-license-agreement-new (last updated 2023-08-21;
the older version is at https://docs.gopluslabs.io/reference/api-license-agreement)
- §2: licence to use the API; the API key may not be shared.
- §3: "Except for personal usage, Your Application should source attribution via a backlink or a
  mention" that it is powered by Go+ Security. §5: display the GoPlus logo with "Powered by GoPlus";
  GoPlus may terminate otherwise.
- §6: not allowed without permission: "receipt collection and data mining", using the service "as
  part of any effort to compete with us", direct commercial use of "our original data" to generate
  revenue, circumventing rate limits.
- Nothing addresses storing/caching or redistributing results. Systematic collection for a benchmark
  is close enough to "data mining" that written permission is the safe route.
- Endpoint: `GET https://api.gopluslabs.io/api/v1/solana/token_security?contract_addresses={mint}`;
  the `Authorization: Bearer <access token>` header is optional.
- To do (user): ask GoPlus for permission (research benchmark, storage, publication); credit
  "Powered by GoPlus" in the write-up.

**User decision (2026-11-17):** aggregates such as PR-AUC are enough for the public claim, so raw
scores of all three scanners stay private.

**GoPlus plan (recommended 2026-11-17, to protect the benchmark):** ask GoPlus for written
permission now (email drafted for the user), and keep GoPlus in the private analysis from day 3
while waiting. Reasons:
- Dropping GoPlus changes the claim sentence (section 0) and weakens "best of three".
- Collecting without asking risks GoPlus ending access mid-study under §6, which would leave holes
  in the paired benchmark weeks.
- Asking first costs nothing.

If GoPlus refuses, stop querying it and ask the user before changing the claim sentence. Credit
"Powered by GoPlus" in the write-up either way.

**Solana Tracker** — https://www.solanatracker.io/terms (last updated 2026-03-23),
https://docs.solanatracker.io/pricing.md
- §6.4: "Redistribution, resale, or sublicensing of raw API data is prohibited without prior written
  authorization." "You may display derived data (charts, analytics, aggregations) in your
  applications", provided Solana Tracker is not presented as endorsing it.
- §5: no products "that compete directly with Solana Tracker without written authorization".
- §3: risk scores are "algorithmic assessments only". No clause on storing or caching.
- Free plan: 2,500 requests/month (80 benchmark tokens/day = 2,400/month fits). An API key is required.

## B. Implementation decisions

- **D-01 Scheduler grid.** All polls run on one 15 s grid (the shortest `price_cadence` interval), so
  calls are batched across tokens. A filter tick is read at the first grid step at or after its
  nominal time: up to 15 s late plus latency. `observed_at` is always the true receive time. A tick
  more than grid + 15 s late (collector down) is written as `missed` and not polled; a missed 300 s
  tick means `not_tracked` with flag `tick300_missed`.
- **D-02 Time origin of filter ticks.** `t` = seconds since the PumpPortal event was received, not
  since the creation block time. Fetching the block time of every launch would cost ~27,000
  credits/day. Measured offset (observed minus block time): median 1.1 s. Tracked tokens get
  `creation_slot` / `creation_block_time` from the parsed creation transaction.
- **D-03 Universe exclusions at the 300 s decision** (each recorded with a flag, never dropped):
  `non_sol_quote` (since October 2026 coins can be paired with USDC or another pump coin; the
  activity filter is defined on SOL reserves), `graduated_before_decision`, `tick300_missing_account`,
  `tick300_missed`, `tick300_error`. State these in the write-up (HANDOFF 6 item 10).
- **D-04 Other launchpads.** PumpPortal's new-token stream also carries other pools (e.g. `bonk`).
  They are written to raw and ignored (`pool != "pump"`).
- **D-05 Modules not in the HANDOFF layout.** `collector/state.py` (SQLite state, config
  `storage.state`) and `collector/report.py` (`make status`, `report-day1`, `report-week1`).
  `make check-env` was added. Reports read the state database; raw files stay the source of truth.
- **D-06 Raw schema additions** (schema stays 1). `source` also takes `collector` (kinds `tracking`
  = the decision for every token, `snapshot_summary` = snapshot_ts, P0, missing parts) and `fluxrpc` /
  `public`, and `publicrpc` (the second launch source, kind `new_token`, payload = the
  `logsNotification` value {signature, err, logs}, `slot` = notification context slot). Collector
  kinds added later: `coverage` (daily coverage check result), `data_clock` (D-32). `request` also carries `accounts`, `items` (per-account mint, tick_s, role), `address`,
  `signature`, `before`, `part` (snapshot part name). For batched reads `mint` and `tick_s` at the top
  level are null and live in `request.items`.
- **D-07 Credit accounting.** Helius charges 1 credit per call for every method used here
  (https://www.helius.dev/docs/billing/credits); `getTransactionsForAddress` costs 10 per 100
  transactions. The ledger also counts failed attempts (conservative). Alchemy / FluxRPC calls are
  counted with 0 credits until their cost is measured (HANDOFF 6 item 5).
- **D-08 Budget.** Month projection = Helius credits of the trailing 24 h x 30. Throttle steps are
  applied one at a time, at most one change per 6 h, and released below 85% of the target. Hard stop
  = credits used in the current billing cycle >= `hard_stop_credits`; the cycle starts on
  `HELIUS_BILLING_DAY` (default 1; set it from the Helius dashboard).
- **D-09 Throttle semantics.** `random_rest_per_day_50` caps random-rest at 50;
  `tracked_cap_minus_20pct` multiplies both caps by 0.8; `balance_cadence_x2_after_3600s` doubles the
  balance interval after 3600 s. If `tracked_cap_per_day` is below `filter_pass_cap_per_day +
  random_rest_per_day`, both are scaled down keeping their ratio (rule 5.1.5).
- **D-10 Hash sampling.** `u = sha256(mint)[:8] / 2^32`; accept if `u < cap / N`, where N is the
  count of the last 24 h including the current token, extrapolated to 24 h while the collector has
  less history (minimum window 10 min). Random-rest uses the count of filter-fail tokens that are in
  the universe. During the first ~10 minutes after the very first start the estimate is low and more
  random-rest tokens are accepted; `sample_prob` is stored per token so base rates can be weighted.
  Benchmark tokens: same hash, `u < benchmark_tokens_per_day / N_filter_pass`.
- **D-11 Fee per trade.** `fee_lamports` = protocol fee + creator fee + buyback fee + cashback +
  holder rewards from the on-chain TradeEvent; `fee_bps` = that / `sol_amount`. Verified on live
  trades: 95 + 30 = 125 bps on ordinary trades (the config default), 0 on "mayhem mode" agent trades.
- **D-12 Tips.** Only transfers to the eight Jito tip accounts are counted as tips. Tips to other
  services are missed; say so when `priority_fee_sol_per_tx` is set.
- **D-13 Early trades (deviation from the letter of the spec; easy to switch off).** Live data
  shows that a token passing the filter already has more than 3,000 signatures on its curve by minute
  5 (mostly failed bot transactions), so `max_signature_pages: 3` never reaches the first trades.
  The collector therefore reads the oldest successful transactions directly with Helius
  `getTransactionsForAddress` (`sortOrder: asc`, `status: succeeded`, limit n+1, verified to work on
  the free plan, 10 credits instead of ~21). The result is used only if it starts with the creation
  signature reported by PumpPortal. If the call fails, the original spec path runs: creation
  transaction + the oldest 20 successful signatures found in the pages, and `early_trades` is marked
  missing when the pages are truncated. The 3 signature pages are still fetched for `sig_stats`
  (`sigs_truncated` is reported in the snapshot summary). Switch: `EARLY_TRADES_ASCENDING` in
  `collector/snapshot.py`. No config value was changed. **Accepted by the user on 2026-11-17.**
- **D-14 Early-trade rows and tracked accounts.** Rows = the first 20 TradeEvents of the mint in
  those transactions. The trader is the event's `user`, not the fee payer (they differ for agent
  trades). Creator set = the curve's `creator` field plus the launching wallet. Token accounts come
  from `postTokenBalances` only; a buyer whose account no longer exists there is not polled. Insider
  set v1 as in section 0; a recipient of a direct transfer from the creator is stored with role
  `early_buyer`, `is_insider = true`. The curve's own token account is never tracked.
- **D-15 `score_norm` per scanner (frozen before any outcome is viewed; higher = riskier).**
  RugCheck: `score_normalised` (`score_raw` = `score`). GoPlus: number of triggered flags among
  `mintable, freezable, closable, balance_mutable_authority, metadata_mutable,
  transfer_fee_upgradable, transfer_hook_upgradable, default_account_state_upgradable` (status "1"),
  `transfer_fee` (non-empty), `transfer_hook` (non-empty), `none_transferable` ("1"),
  `default_account_frozen` (state "2"), `malicious_creator`. Solana Tracker: `risk.score` (1-10).
  The Solana Tracker response shape is from its documentation and must be checked on day 3.
- **D-16 Snapshot batch timing.** Parts run concurrently for `deadline_s - 3 s`; unfinished parts are
  cancelled, written as `timeout`, and listed in `missing_parts`; then the closing reserve read runs.
  If the closing read fails there is no snapshot (`flags = snapshot_failed`, no polling). A scanner
  query is claimed in the state database before it is sent, so a (mint, scanner) pair is queried
  once even after a failure, timeout or restart. No late scanner row can exist by construction.
- **D-17 Dead rule.** Dead = price below 5% of the post-snapshot peak OR no reserve change for
  1800 s, recomputed at every price tick (a token can come back). A graduated curve is treated the
  same way. Dead stops balance polling at once; the price cadence drops to the slowest interval of
  `price_cadence` (1800 s) only after the first 3600 s (`never_throttle: price_ticks_first_3600s`).
- **D-18 Raw writer.** Records are buffered and flushed to disk every 15 s (or 4 MB). Since
  2026-11-17 each part is one zstd stream flushed in blocks (D-27). A new part file is started per
  process start and per 256 MB. The collector stops (exit 75, no restart
  loop) when free disk space falls below 500 MB.
- **D-19 RPC.** `getTransaction` uses `jsonParsed` and `maxSupportedTransactionVersion: 1` (version-1
  transactions exist on mainnet; 0 is rejected). A 429 is retried on the same provider after 1 s and
  2 s. A provider answering 401/403 is skipped for an hour. Token accounts are read with a 72-byte
  `dataSlice` (mint, owner, amount).
- **D-20 FluxRPC (asked by the user).** Useful only as the emergency tier: it replaces the
  unauthenticated public RPC as the third provider (Helius -> Alchemy -> FluxRPC), used when both
  others fail. It cannot be primary or the overflow tier without changing `config.yaml`
  (`rpc.primary: helius`, `rpc.fallback: alchemy`). Endpoint `https://eu.fluxrpc.com?key=...`.
  Status on 2026-11-17: the key in `.env` is rejected (HTTP 401 "invalid api key"). User decision
  (2026-11-17): if it still fails after the user re-checks the key, remove FluxRPC.
  Follow-up (2026-11-17): the user confirmed `FLUXRPC_API_KEY` holds the dashboard's "API Key"
  value, copied correctly, and it uses the documented form (`https://eu.fluxrpc.com?key=...`, per
  fluxrpc.com/docs/rpc), yet it gets 401. The dashboard also lists "RPC", "Shield Key" and
  "WebSocket" values. The code now takes `FLUXRPC_RPC_URL`, the dashboard's "RPC" value used as-is
  as the endpoint, which wins over `FLUXRPC_API_KEY`. If `make check-env` still fails with it,
  FluxRPC is removed. Free plan: 15 req/s, 10 GB bandwidth a month, enough for an emergency tier.
  Then at 11:58 UTC `make check-env` returned `ok` for the existing API-key form (the key probably
  needed time to activate), and Alchemy returned `ok` too. `FLUXRPC_RPC_URL` stays as an optional
  override and is not set. The provider chain Helius -> Alchemy -> FluxRPC is live since the
  11:59 UTC restart.
- **D-21 Clock.** The laptop is synced by systemd-timesyncd, not chrony (HANDOFF 6 item 11). It is
  accurate enough for second-level timestamps and needs no sudo; `make status` shows the sync flag
  and the reports show observed-minus-block-time. Installing chrony stays an option.
  Measured 2026-11-17: offset -6.7 ms, jitter 4.3 ms (ntp.ubuntu.com, stratum 2), about 300 times
  inside the 2 s limit, so timesyncd is kept. `make status` now shows the offset and alerts above 2 s.
- **D-22 GoPlus authentication.** M1 uses GoPlus's public tier without a key. `GOPLUS_API_KEY` is
  not read yet: GoPlus access tokens need an app key and a secret. Decide on day 3 from the measured
  rate limit whether authentication is needed.
- **D-23 `report-day1` wording.** "Dead before 300 s" = real SOL reserves are 0 at the 300 s tick.
  Per-day figures are scaled to 24 h from the hours covered.
- **D-24 Tooling.** uv-managed CPython 3.12 venv (`.venv`), system python3 untouched; ruff line
  length 120; mypy strict on `src/`.
- **D-25 Insider funding link** stays off (section 0). Creator history (3 signature pages + oldest
  transaction, cached 7 days) is still collected; the oldest transaction is skipped when the pages
  are truncated, because it would not be the first one.
- **D-26 Stall watchdog (asked by the user).** If no pump.fun launch arrives for 600 s, the
  collector writes a `stall_restart` event and exits with code 70; systemd (`Restart=always`)
  starts it again 5 s later. Threshold: day-1 launch gaps had p99.9 = 70 s and max = 112 s, so
  600 s is about 5x the longest gap ever seen and should never fire on a quiet but healthy stream.
  Also: `WatchdogSec=120` (the collector pings systemd every 5 s, so a hung event loop is killed and
  restarted), and `ExecStopPost` runs `record-stop`, logging every stop with systemd's reason
  (`success`, `exit-code`, `watchdog`, `signal`). `make status` counts stops and stall restarts over
  10 days; uptime comes from the per-minute heartbeats. Since D-31 the 600 s limit applies to the
  newest launch from any source; a single silent source reconnects itself after 300 s.
- **D-27 Raw compression.** One zstd stream per part file, flushed with `FLUSH_BLOCK` every 15 s,
  and the frame finished on close, rotation or date change. The previous writer made each 15 s
  flush its own frame, so records never shared compression context. Replaying the first 7.9 h:
  38.0 MB -> 26.9 MB (-29%). Level 6 instead of 10: level 10 is only 2% smaller and needs more
  memory per open stream. Crash safety is unchanged: the reader returns everything up to the last
  flushed block and skips a torn last line.
- **D-28 PumpPortal silence timeout 60 s -> 180 s.** Natural gaps between messages reached 112 s on
  day 1. The 60 s timeout caused 7 reconnects in 3 h; after each, the next message took 5-52 s, so
  the connection had not been dead. Dead TCP connections are still caught by websocket pings
  (20 s interval, 20 s timeout).
- **D-29 Backup direction.** `make backup` runs on the desktop and pulls over SSH (the user's
  request). It first takes an SQLite online-backup copy of the state database on the laptop
  (`snapshot-state`), then rsyncs `data/raw/` with `--append-verify`, which is safe because raw files
  are append-only. It never deletes on either side and refuses to run on the collector laptop.
- **D-30 Entry point (HANDOFF section 0 open question).** User decision 2026-11-17: keep the
  default. The 5-minute snapshot is used now, and the graduation snapshot is added in M3.
- **D-31 Second launch source (HANDOFF section 0 changed with the user's approval, 2026-11-17).**
  PumpPortal's free stream missed about 78% of launches: all those in version-1 transactions
  (section C). Added: one websocket to the public Solana RPC (`wss://api.mainnet-beta.solana.com`),
  `logsSubscribe` with `mentions: [TSLvdd1pWpHVjahSpsvCXUbgwsL3JAcvokwaKt1eokM]`, which is the
  pump.fun mint authority and appears in every create; commitment `confirmed`. Free, no key, create
  logs only (not a trade stream), not Helius.
  - Each create's logs carry the CreateEvent and the dev buy's TradeEvent, so no RPC call is needed
    per launch. Every notification is written to raw (`publicrpc/new_token`, failed and non-create
    ones too; about 9 KB each, about 26 MB/day compressed).
  - Merge: both sources insert into `tokens`. The first source to report a mint sets `launch_obs`
    (t0, D-02), and `launch_sources` records every source that saw it. The public RPC is usually
    about 0.3 s earlier than PumpPortal. Fields from the public RPC use PumpPortal's names and units.
  - Disconnects: reconnect with backoff 1/2/5/10/30/60 s. A source is also reconnected after 120 s
    with no message at all, or 300 s with no launch while connected (PumpPortal too). The whole
    collector restarts only when no source has delivered a launch for 600 s (D-26).
  - Launches created while the stream is reconnecting are not backfilled. They are caught only if
    PumpPortal saw them; the daily coverage check measures the loss.
  - Rate limiting: HTTP 429 at the handshake, close codes 1008/1013, or an error mentioning
    rate/limit/429. Each is logged as `rpcws_rate_limited`, backed off 60/300/900 s, and shown in
    `make status` with an alert.
  - **Fallback if it gets rate-limited (pre-approved by the user and built on 2026-11-17: D-33):** the same
    subscription on FluxRPC's websocket, the dashboard's "WebSocket" value. FluxRPC's free plan
    includes websocket access. Launch logs are about 245 MB/day uncompressed on the wire, about
    7.4 GB/month, under its 10 GB/month. Second choice: Alchemy's websocket, after measuring its
    compute-unit cost per notification.
  - Daily coverage check (`collector/coverage.py`), the user's condition:
    - Once a day, on the hour ending 5 minutes earlier, and only once the public-RPC stream has run
      for the whole hour.
    - Chain side: all successful transactions mentioning the mint authority
      (`getSignaturesForAddress`, about 2 pages).
    - A signature recorded as a launch counts as seen. Up to 60 unseen signatures are fetched and
      classified as create or not (PumpSwap pool creation also mentions the authority); the
      missed-create count is extrapolated from that sample.
    - Coverage = seen / (seen + estimated missed), overall and per source. It costs about
      70 Helius credits a day.
    - `make status` and `report-day1` show the last check and alert below 95%.
    - `python -m rugwatch.collector.main coverage-check --minutes N` runs a check on demand.
  - Collector version 0.2.0. New state: `launch_sources`, `heartbeats.rpcws_connected`.
- **D-32 Data collected before the fix; the data clock.** Options were: exclude it, keep it
  flagged, or bump `universe_version`. Decision: **keep it, flagged, and exclude it from every model,
  benchmark and claim.** It may be used only for pipeline development (parser, labels, audit
  tooling).
  - Flag: `launch_obs < data_clock_start`. The start is stored in the state database
    (`meta.data_clock_start`) and in raw (`collector/data_clock`); collector versions 0.1.0 vs 0.2.0
    also tell the periods apart.
  - Why not bump `universe_version`: the universe is not frozen yet (rule 5.1.6 bumps it only after
    the day-14 freeze), `config.yaml` values change only through section 5 rules, and the flag above
    is exact.
  - **The data clock restarted at 2026-11-17 12:11:51 UTC** (the first start of 0.2.0). Everything
    "day N" is counted from it. The scheduler's 24 h rate estimates count only decisions after it
    (otherwise the jump from ~22% to full coverage would have overstated every sampling
    probability for a day), and `report-day1` counts only launches after it.
  - New dates: report-day1 2026-11-18 after 12:15 UTC; day 3 2026-11-20 12:15; report-week1
    2026-11-24 12:15; audit from day 8, 2026-11-25; universe freeze and M1 exit 2026-12-01 12:15 UTC.
    The dates are unchanged; only the hour moves.
  - The 10-day uptime check does not depend on the data clock.
  - Tokens tracked before the clock are polled until their tracking ends (about 24 h later), because
    each already has a snapshot and stopping halfway would leave it incomplete. This costs credits
    but nothing else.
- **D-33 FluxRPC websocket fallback for the create-log stream (pre-approved by the user,
  2026-11-17).**
  - Endpoint: `wss://ws.eu.fluxrpc.com?key=<FLUXRPC_API_KEY>`. The host is from
    fluxrpc.com/docs/websocket; the key parameter has the same form as their HTTP endpoint.
    `FLUXRPC_WS_URL` overrides it. Verified with `make check-env`: create logs arrive. The HTTP host
    with `wss://` returned HTTP 503. FluxRPC documents `logsSubscribe` with a `mentions` filter.
  - When the public endpoint rate-limits us (HTTP 429 at the handshake, close codes 1008/1013, or a
    rate-limit error), the stream switches to FluxRPC at once.
  - On the fallback, it goes back to the public endpoint after 1 hour (a planned reconnect of about
    1 s; if the public endpoint still rate-limits, it switches straight back). It also goes back
    after 3 consecutive FluxRPC failures, or at once if FluxRPC rate-limits too.
  - Every switch is an `rpcws_switch` event with its reason. `make status` shows the endpoint in use
    (flagged while on FluxRPC) and the switches in the last 24 h.
  - Bandwidth: every byte received from and sent to FluxRPC (websocket messages, and HTTP requests
    and responses of the emergency RPC tier) is counted per UTC calendar month in
    `bandwidth_ledger`. The counts are uncompressed payload sizes, so they are an upper bound.
    FluxRPC's own billing cycle is unknown, hence the calendar month.
  - `make status` shows the month's usage against 10 GB and alerts from 80%. The fallback is not
    used above 90% (9 GB). Then the stream stays on the public endpoint with the long backoff,
    PumpPortal still runs, and the daily coverage check shows the loss.
  - A full month on FluxRPC would need about 8.4 GB (about 9 KB x about 31k creates a day), so the
    hourly return to the public endpoint matters.
  - The raw `source` and `launch_sources.source` name the endpoint (`publicrpc` or `fluxrpc`), so
    the coverage check reports them separately. Collector 0.2.1. The data clock is unchanged.

## C. M1 measurements so far (2026-11-17, trial runs of ~10 minutes)

- Bonding-curve layout decoded against the official IDL (pump-public-docs, 2026-10-13): 166-byte
  accounts, fields verified on live accounts. Coins may be Token-2022. (HANDOFF 6 item 1)
- Curve fee: 125 bps on ordinary trades (95 protocol + 30 creator). (item 2, first evidence)
- PumpPortal: ~0.3 pump.fun launches per second (~26-28k/day), no drops in the trials. (item 3)
- Helius: 1 credit per call confirmed in the docs; one 429 in ~230 calls at 8 rps. (item 4)
- Alchemy: HTTP 403 "SOLANA_MAINNET is not enabled for this app". FluxRPC: HTTP 401. (item 5)
- Launch observed_at minus creation block time: median 1.1 s, p90 2.4 s (n=9). (item 11)

First 3.2 h of the live service (2026-11-17 02:49 to 06:01 UTC; not yet a full day):

- 1,519 launches (about 150 per 20 min at this hour, lower than during the trials: compare the
  24 h count with the public figure in `report-day1`); 0 websocket drops; 0 RPC errors in 2,965 calls.
- 59 filter passes at 5 SOL (about 20/hour), all tracked; 87 tracked in total, 87 snapshots complete.
- D-13 confirmed live: 87 of 87 snapshots used the oldest-first call; 27 of 87 (31%) had truncated
  signature pages and would have had `early_trades` missing under the page walk.
- Fee per side on fee-paying early trades: min 125 bps, mean 173 bps, max 203 bps (n=1,033); 141
  zero-fee trades. So 125 bps is the floor, not the typical fee. (item 2)
- Credits: about 1,140/hour and still rising while 24 h polling accumulates (projection 670k/30 d
  after 3 h). Snapshot 1,198, filter 765, price 747, balance 712, holders 167, creator 162.
- Storage: 18.7 MB in 3.2 h (signature pages 55%), about 140 MB/day and rising.
- Early-buyer accounts: 701 tracked, 267 flagged insider (bought within one slot of creation).

First 7.9 h (to 2026-11-17 10:41 UTC):

- **Storage**: 38.0 MB (about 115 MB/day). By kind: signature pages 19.7 MB (52%; each page holds
  up to 1,000 base58 signatures, which barely compress, ratio 3.6), account reads 11.2 MB,
  transactions 4.4 MB, PumpPortal 1.5 MB. Account reads compressed badly only because of the small
  frames, which D-27 fixes. Signature pages are fixed by `max_signature_pages: 3` (snapshot and
  creator history) and stay. Expected after D-27: about 80-100 MB/day, i.e. about 12 GB over the
  project's 17 weeks. The plan's ~20 MB/day estimate did not allow for 1,000-signature pages on
  busy curves.
- **Launch coverage (HANDOFF 6 item 3): PumpPortal's free stream delivers only about 22% of
  pump.fun launches.** Independent count, 10:39-11:37 UTC: 1,128 successful transactions touching
  the pump.fun mint authority (`TSLvdd1pWpHVjahSpsvCXUbgwsL3JAcvokwaKt1eokM`), about 27k/day, which
  matches the public figure. PumpPortal delivered 253, all of which were among them. In a sample of
  the missed ones, 13 of 14 were `CreateV2` launches in **version-1 transactions** (the 14th was a
  PumpSwap `CreatePool`). Every delivered launch was a v0 transaction. PumpPortal's free stream
  appears to skip v1 transactions, so the current universe is a biased subset of launches. The
  10-minute trials before the service started saw ~0.3 launches/s, so this probably began recently.
  A free fix was tested: the public Solana RPC websocket, `logsSubscribe` mentioning the mint
  authority, delivered 25 creates in 75 s, each with the full CreateEvent (mint, bonding curve,
  creator, quote mint, token program) in its logs, at 0 credits. Using it changes the section 0
  data-access rule, so it waits for the user's decision (open item below).
- PumpPortal: 7 reconnects after 60 s of silence, 17 s disconnected in total (see D-28).
- Clock: NTP offset -6.7 ms (D-21).

After the second launch source (data clock start 2026-11-17 12:11:51 UTC):

- On-demand coverage check, 12:12:30-12:30:30 UTC: 385 creates on chain, 385 seen (100.0%).
  Public RPC alone: 100.0%. PumpPortal: 25.7%. The 3 unseen signatures were not creates.
  385 creates in 18 min is about 31k/day.
- First 24 min: public RPC 540 launches, PumpPortal 144. Public RPC is usually about 0.3 s earlier.
  The public-RPC stream had no drops and no rate limiting. PumpPortal dropped once (12:13,
  reconnected after 9.6 s).
- Raw storage of the public-RPC stream: 0.3 MB in 24 min (about 19 MB/day).
- First automatic daily check (window 12:16-13:16 UTC): about 1,481 creates on chain (about
  35k/day at this hour); all sources 99.6%, public RPC 99.4%, PumpPortal 26.5%.

## D. Open items

- Launch discovery: resolved by D-31 (approved 2026-11-17); first coverage numbers in section C.
- Day 3: measure RugCheck / GoPlus limits and on-time coverage; verify the Solana Tracker response
  shape; record Alchemy cost per method once the network is enabled.
- Storage: signature pages dominate (about 100-250 KB per tracked token). Measure in `report-week1`.
